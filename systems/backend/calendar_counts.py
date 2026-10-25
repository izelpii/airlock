"""
Photographs and videos per day, for Calendar view and the Dashboard's mini
calendar. documentation/architecture/calendar-view-spec.md section 4.

One GROUP BY over file_contents - about a second on `family` (370,751 files) -
so it is NOT run per request. The result is one JSON value in the catalog's
catalog_properties, kept by the Dashboard's own freshness rules (dashboard.py):
its fingerprint, its dirty mark (set whenever a workflow finishes) and its
maximum age. A stale value is served at once and recounted on a background
thread; only a catalog never counted waits, once.

A day counts what Photos and Videos show for it: not removed, not a cache/
derivative, one per image group (a RAW+JPG pair, a Live Photo's clip), dated
by master_datetime. Photo extensions are the Photos page's own list, passed in
by app.py, so a square saying 37 opens a day of 37.
"""

import json
import sqlite3
import threading
from datetime import date, datetime, timedelta

import dashboard
from media_types import VIDEO_EXTENSIONS

KEY = 'calendar_counts'
SCHEMA_VERSION = 2                         # 2: totals.mean (the colour's centre)
RECENT_DAYS = 371                          # the Dashboard's 53 weeks, and a little


def _types_sql(types):
    return '(' + ','.join(f"'{t}'" for t in sorted(types)) + ')'


def count(conn, photo_types):
    """The counts, now: {days, totals, undated}. `days` newest first, as
    [year, month, day, photos, videos, in_an_event]."""
    photos, videos = _types_sql(photo_types), _types_sql(VIDEO_EXTENSIONS)
    has_events = dashboard._has(conn, 'event_files')
    in_event = ('CASE WHEN EXISTS (SELECT 1 FROM event_files ef WHERE ef.file_id = fc.id) '
                'THEN 1 ELSE 0 END') if has_events else '0'
    where = f'''
        (LOWER(fc.file_type) IN {photos} OR LOWER(fc.file_type) IN {videos})
        AND fc.removed_at IS NULL
        AND fc.file_path NOT LIKE 'cache/%'
        AND fc.id NOT IN (SELECT f.id FROM file_contents f
                            JOIN image_groups g ON g.id = f.image_group_id
                           WHERE f.id <> g.primary_file_id)'''
    dated = "fc.master_datetime GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*'"
    rows = conn.execute(f'''
        SELECT substr(fc.master_datetime, 1, 10) AS d,
               SUM(CASE WHEN LOWER(fc.file_type) IN {photos} THEN 1 ELSE 0 END) AS p,
               SUM(CASE WHEN LOWER(fc.file_type) IN {videos} THEN 1 ELSE 0 END) AS v,
               SUM({in_event}) AS e
          FROM file_contents fc
         WHERE {where} AND {dated}
         GROUP BY d
         ORDER BY d DESC''').fetchall()
    undated = conn.execute(
        f'SELECT COUNT(*) FROM file_contents fc WHERE {where} AND NOT '
        f"(COALESCE(fc.master_datetime, '') GLOB '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]*')"
    ).fetchone()[0]
    days = []
    for d, p, v, e in rows:
        try:
            y, m, dd = int(d[:4]), int(d[5:7]), int(d[8:10])
            date(y, m, dd)                          # '2019-02-30' is not a day
        except ValueError:
            undated += p + v
            continue
        days.append([y, m, dd, p, v, e])
    totals = {'photos': sum(d[3] for d in days), 'videos': sum(d[4] for d in days),
              'days': len(days),
              'first_year': days[-1][0] if days else None,
              'last_year': days[0][0] if days else None,
              'busiest': max((d[3] + d[4] for d in days), default=0)}
    # The average day - over days that have anything - is the colour scale's
    # centre (owner, 2026-10-18): pink below it, green above.
    totals['mean'] = round((totals['photos'] + totals['videos']) / len(days), 2) if days else 0
    return {'days': days, 'totals': totals, 'undated': undated}


# ── the cached value ─────────────────────────────────────────────────────────

def _read(conn):
    raw = dashboard._get_prop(conn, KEY)
    if not raw:
        return None
    try:
        value = json.loads(raw)
    except ValueError:
        return None
    return value if value.get('schema_version') == SCHEMA_VERSION else None


def _is_stale(conn, value):
    """Exactly the Dashboard's rules: one definition of "the catalog changed"."""
    return dashboard._is_stale(conn, value)


def rebuild(db_path, photo_types):
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        fp = dashboard.fingerprint(conn)        # before: a change during the count re-marks it
        value = {**count(conn, photo_types), 'fingerprint': fp,
                 'computed_at': dashboard._now().isoformat(), 'schema_version': SCHEMA_VERSION}
        dashboard._set_prop(conn, KEY, json.dumps(value, separators=(',', ':')))
        return value
    finally:
        conn.close()


_building = set()
_building_lock = threading.Lock()


def _rebuild_in_background(db_path, photo_types):
    with _building_lock:
        if db_path in _building:
            return
        _building.add(db_path)

    def run():
        try:
            rebuild(db_path, photo_types)
        except Exception as exc:                        # never kills the server
            print(f'[calendar] recount failed for {db_path}: {exc}')
        finally:
            with _building_lock:
                _building.discard(db_path)
    threading.Thread(target=run, name='calendar-recount', daemon=True).start()


def get(db_path, photo_types, background=True, wait=True):
    """The counts: at once when there is a value, stale or not. With no value
    yet, count now - or, with wait=False (the Dashboard, which must stay
    milliseconds), start counting in the background and return None."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        value = _read(conn)
        stale = value is not None and _is_stale(conn, value)
    finally:
        conn.close()
    if value is None:
        if not wait:
            _rebuild_in_background(db_path, photo_types)
            return None
        return {**rebuild(db_path, photo_types), 'stale': False}
    if stale and background:
        _rebuild_in_background(db_path, photo_types)
    return {**value, 'stale': stale}


def public(value):
    """The response: never the fingerprint."""
    return {k: value.get(k) for k in ('days', 'totals', 'undated', 'computed_at', 'stale')}


def recent(value, today=None):
    """The Dashboard's slice: the last RECENT_DAYS days, and the busiest day of
    the whole catalog so the mini calendar's colours mean what Calendar view's do."""
    today = today or date.today()
    since = today - timedelta(days=RECENT_DAYS)
    days = [d for d in value.get('days') or [] if date(d[0], d[1], d[2]) > since]
    return {'days': days, 'busiest': (value.get('totals') or {}).get('busiest', 0),
            'mean': (value.get('totals') or {}).get('mean', 0),
            'computed_at': value.get('computed_at'), 'stale': value.get('stale')}
