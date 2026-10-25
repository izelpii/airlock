"""
One way to ask backend-geoloc what is near a coordinate.

Implements documentation/specs/location-spec.md section 7.3, with one deviation
that section did not anticipate and that is worth stating plainly.

The spec said the backend would expose GET /api/catalogs/<id>/geocode and that
BOTH the clustering workflow and the Locations page would call it. The page can:
it has a session. The workflow cannot - it is a detached subprocess with no
signed-in user, so an authenticated backend endpoint answers it 401. That is
exactly how ev_extract_events' home-address fallback came to degrade silently:
it called {BACKEND_URL}/api/user/location, got 401 once the API required a
session, and returned (None, None) without saying so (location-spec.md 3.1).

So the single code path lives HERE, in a module, rather than behind an HTTP
endpoint:

    the workflow  ->  this module  ->  backend-geoloc
    the browser   ->  backend /api/catalogs/<id>/geocode  ->  this module

One implementation, one etiquette delay, one place to change. The browser still
never reaches an internal service, which was the point of 7.3.

Deliberately importing only the standard library, for the reason lib/stopping.py
gives: lib/utilities.py pulls in PIL, exiftool and pillow_heif, and a workflow
that wants one HTTP call should not take those on.
"""

import json
import os
import threading
import time
import urllib.parse
import urllib.request

# Where backend-geoloc lives. Same env-var-with-a-default shape as
# ALBUMS_BACKEND_URL in app.py - a bare literal is what a config-only migration
# misses (see the comment there).
DEFAULT_GEOLOC_URL = 'http://geoloc.airlock.lan'

# Nominatim's usage policy asks for at most one request a second from an
# application, and backend-geoloc makes SEVERAL upstream calls per /poi (it
# probes offsets around the point). One cluster at a time with a gap between is
# the honest reading of that, and the clustering workflow has at most a few
# thousand clusters to ask about - a run that takes an hour of wall clock in the
# background is not a problem, being rate-limited out of the service is.
DEFAULT_MIN_INTERVAL_SECONDS = 1.1

# MEASURED, not guessed. backend-geoloc probes several offsets around the point
# through Nominatim and then queries OSMnx, and from inside the backend
# container that took 6.7s for a coordinate in La Plata and 14.6s for one in
# Texas. A 20s ceiling - which this had - leaves almost no headroom, and the
# only symptom is "Geocoding service unavailable", which is wrong: the service
# is there and working, just slowly.
#
# Nothing else in the path is tighter. The gateway allows a 600s read and the
# browser's axios client sets no timeout at all, which is also what the page
# used to have when it called backend-geoloc directly - so this timeout is one
# this change INTRODUCED. 60s is a bound rather than a limit anyone should hit.
DEFAULT_TIMEOUT_SECONDS = 60.0


# Read PER CALL, not frozen into a module constant at import time. The constant
# version of this cost an afternoon: a test set GEOLOC_URL with monkeypatch,
# the module had already captured the default, and every call went to the real
# host and spent ten seconds timing out - the suite was testing the opposite of
# what it claimed. A module whose configuration is fixed at import is a module
# whose tests can lie about it.

def geoloc_url():
    return os.environ.get('GEOLOC_URL') or DEFAULT_GEOLOC_URL


def min_interval_seconds():
    return float(os.environ.get('GEOLOC_MIN_INTERVAL') or DEFAULT_MIN_INTERVAL_SECONDS)


def timeout_seconds():
    return float(os.environ.get('GEOLOC_TIMEOUT') or DEFAULT_TIMEOUT_SECONDS)

_lock = threading.Lock()
# The monotonic time at which the NEXT call may start. Not "when the last one
# happened": reserving the slot up front is what lets a caller release the lock
# before it waits.
_next_slot = [0.0]

# How many requests may be in flight against backend-geoloc at once.
#
# ONE, because that is what it can do. It is a single Flask process running an
# OSMnx query per request, and eight concurrent callers were measured: three
# answered in 10-21s and FIVE died at the 60s timeout. Spacing calls 1.1s apart
# does not prevent that - each caller waits only for its own slot and then they
# all overlap.
#
# Bounded by HOW LONG a caller will wait for its turn, which is the part that
# matters in gunicorn: a thread blocked here is one of eight not serving
# anything. A caller that cannot get in quickly is told so, which is a better
# answer than a minute of silence and more likely to succeed on a retry once
# the geocoder's own cache is warm.
_in_flight = threading.BoundedSemaphore(
    int(os.environ.get('GEOLOC_CONCURRENCY', '1')))
QUEUE_WAIT_SECONDS = float(os.environ.get('GEOLOC_QUEUE_WAIT', '10'))

# Answers already given, keyed on a rounded coordinate. See CACHE_TTL_SECONDS.
_cache = {}
CACHE_TTL_SECONDS = float(os.environ.get('GEOLOC_CACHE_TTL', '3600'))
CACHE_MAX_ENTRIES = 512


class GeocodeUnavailable(Exception):
    """backend-geoloc could not be reached, or did not answer usefully."""


def _cache_key(lat, lon, limit, dist):
    # Five decimals is about a metre - finer than any of this is meaningful,
    # and it makes two clicks on the same card one lookup.
    return (round(float(lat), 5), round(float(lon), 5), int(limit), int(dist))


def _cached(key):
    with _lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        when, value = hit
        if time.monotonic() - when > CACHE_TTL_SECONDS:
            _cache.pop(key, None)
            return None
        return value


def _remember(key, value):
    with _lock:
        # Oldest out first. A dict preserves insertion order, so this is the
        # whole eviction policy - 512 coordinates is far more than a person
        # looks at in an hour, and the workflow asks about each cluster once.
        if len(_cache) >= CACHE_MAX_ENTRIES:
            for k in list(_cache)[:len(_cache) - CACHE_MAX_ENTRIES + 1]:
                _cache.pop(k, None)
        _cache[key] = (time.monotonic(), value)


def nearby(lat, lon, limit=5, dist=500, timeout=None):
    """
    Points of interest near (lat, lon), as backend-geoloc orders them:
    [{'name', 'address', 'source'}, ...].

    Raises GeocodeUnavailable rather than returning [], because the two mean
    different things to a caller. An empty list is "there is nothing named
    here", which is a real answer about a field in the middle of nowhere and
    worth caching. A failure is "we do not know yet", and a cluster must not be
    recorded as having no suggestions because the service was down - the next
    run should ask again. location-spec.md 7.2 makes the whole suggestion phase
    optional for this reason.

    `timeout` lets the caller choose. The clustering workflow can afford to
    wait a minute; a person who clicked a button cannot, and neither can the
    backend - see the comment on the lock below.

    THE LOCK IS HELD ONLY TO RESERVE A SLOT, never across the request.
    It used to wrap the whole call, and that took the backend down: gunicorn
    runs ONE worker with EIGHT threads, so eight clicks on "Detect Nearby" each
    held this lock for up to 61 seconds in turn and the server answered nothing
    else for minutes. It looked exactly like a crash, and an unrelated location
    update failed in the middle of it.

    Reserving the slot keeps calls spaced for Nominatim's sake without any
    thread holding the lock while it waits. A caller still waits its turn -
    1.1s times the queue ahead of it - which is bounded and small, where the
    old behaviour was the request timeout times the queue.
    """
    if lat is None or lon is None:
        raise GeocodeUnavailable('no coordinate to look up')

    key = _cache_key(lat, lon, limit, dist)
    hit = _cached(key)
    if hit is not None:
        return hit

    limit_s = float(timeout) if timeout else timeout_seconds()
    queue_wait = min(QUEUE_WAIT_SECONDS, limit_s)
    if not _in_flight.acquire(timeout=queue_wait):
        raise GeocodeUnavailable(
            f'busy - another lookup is still running after {queue_wait:g}s. '
            'Try again in a moment; the geocoder answers faster once it has '
            'seen an area.')
    try:
        with _lock:
            now = time.monotonic()
            start_at = max(now, _next_slot[0])
            _next_slot[0] = start_at + min_interval_seconds()
        wait = start_at - time.monotonic()
        if wait > 0:
            time.sleep(wait)

        query = urllib.parse.urlencode({
            'lat': lat, 'lon': lon, 'limit': limit, 'dist': dist})
        url = f'{geoloc_url().rstrip("/")}/poi?{query}'
        try:
            with urllib.request.urlopen(url, timeout=limit_s) as resp:
                payload = json.loads(resp.read().decode('utf-8'))
        except Exception as exc:
            # A timeout and a refusal need different words. "unavailable" sent
            # somebody looking for a stopped container when the service was
            # running and merely slow.
            if isinstance(exc, TimeoutError) or 'timed out' in str(exc).lower():
                raise GeocodeUnavailable(
                    f'no answer within {limit_s:g}s - the geocoder is slow for '
                    f'this coordinate, not down') from exc
            raise GeocodeUnavailable(f'{url}: {exc}') from exc
    finally:
        _in_flight.release()

    if not isinstance(payload, dict) or 'pois' not in payload:
        raise GeocodeUnavailable(f'{url}: unexpected response {payload!r:.200}')

    pois = payload.get('pois') or []
    result = [p for p in pois if isinstance(p, dict) and p.get('name')]
    # Only a successful answer is remembered, empty list included - that is a
    # real answer about a field in the middle of nowhere. A failure never is:
    # the next caller must get to ask again.
    _remember(key, result)
    return result
