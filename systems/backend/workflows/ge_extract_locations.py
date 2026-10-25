## Extract Locations
"""
Group the coordinates already in a catalog into proposals a person can name.

Implements documentation/specs/location-spec.md sections 4.3, 4.4 and 7.2.

The coordinates are already here: every geo-tagged file has had a precision-10
geohash written at import since the column was added, and until now nothing read
any of them. A PREFIX of that string is a proximity bucket, so clustering is one
indexed pass - GROUP BY substr(geohash, 1, N) - rather than an N-squared sweep of
distances over 158,000 rows.

It writes a locations row per cluster, UNCONFIRMED, named from the geocoder's
best guess - or from its coordinates where there is no guess to be had. A person
confirms it by keeping or changing that name, and only a confirmed location is
offered when tagging an event.

That is a change from this spec's section 4.4, which had the workflow write no
locations row at all on the grounds that "a reverse-geocoded name is a guess
about meaning". The guess is still a guess; what was wrong was the conclusion.
Keeping proposals in a separate table made the Locations page show two kinds of
card for one concept, and the objection 4.4 was really making - that guesses
must not be mistaken for answers - is answered better by a `confirmed` flag than
by a separate table: the distinction stays visible, in one list, exactly as
events distinguish Auto from Manual.

A REJECTED cluster gets no locations row, and keeps its rejection so the next
run does not propose it again.
"""

import os
import sys
import sqlite3
import math
import json

# BOTH directories, explicitly.
#
# `lib.stopping` lives in workflows/lib and `location_schema` in the backend
# directory above it, so two entries are needed. Run as a subprocess, Python
# puts the script's own directory on sys.path for free and only the second is
# strictly required - which is why the other workflows get away with one line.
# Imported instead, as app.py's threading path does with
# spec_from_file_location, that free entry is NOT there, and `from lib.stopping
# import ...` fails with "No module named 'lib'". Being importable either way
# costs one line and is not worth a dependency on how it was launched.
_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from lib.stopping import stop_requested, record_stopped, stop_message  # noqa: E402
from location_schema import ensure_location_schema  # noqa: E402
from lib.geocode import nearby, GeocodeUnavailable  # noqa: E402


# ─── Settings ─────────────────────────────────────────────────────────────────

# Geohash prefix length used to seed buckets. 6 is ~1.2 km, which on the family
# catalog yields 1,093 clusters with 3 or more files covering 99.6% of
# geo-tagged photos - a review queue the same order of magnitude as naming face
# clusters. Precision 8 (~38 m) would put 5,166 proposals in front of a person
# and precision 5's 5 km cells are too coarse to be a location. See spec 2.3.
PRECISION = int(os.environ.get('AIRLOCK_LOCATION_PRECISION', '6'))

# Two points either side of a geohash cell boundary land in different buckets
# however close they are. This is not hypothetical: splitting the family
# catalog's largest bucket shows 9vghtx6r with 20,120 files beside 9vghtx6q with
# 7,217 - one place, two cells. Buckets whose centroids are within this distance
# are merged (spec 4.3).
MERGE_METRES = float(os.environ.get('LOCATION_MERGE_METRES', '250'))

# A single photo from a motorway is not a place.
MIN_FILES = int(os.environ.get('LOCATION_MIN_FILES', '3'))

# A bucket this large at ~1.2 km lumps a home, its neighbourhood and a nearby
# park into one proposal, so it is re-bucketed one character finer and merged
# again. Only 3 buckets in the family catalog qualify, which is what makes this
# a narrow refinement rather than a redesign. 0 disables it.
SPLIT_FILES = int(os.environ.get('LOCATION_SPLIT_FILES', '5000'))

# How many characters past PRECISION a split may go.
MAX_SPLIT_DEPTH = int(os.environ.get('LOCATION_MAX_SPLIT_DEPTH', '2'))

EARTH_RADIUS_M = 6371000.0


def provisional_name(suggestions, lat, lon):
    """
    What to call a location nobody has named yet.

    The geocoder's first suggestion, or the coordinates. "Near -33.86882,
    151.20929" is a poor name and deliberately so: it is recognisable enough to
    find on the page, and obviously nobody's choice, which is the point of an
    unconfirmed row.
    """
    for s in (suggestions or []):
        name = (s.get('name') or '').strip()
        if name:
            return name
    return f'Near {lat:.5f}, {lon:.5f}' 


# ─── Subprocess progress helpers (same contract as the other workflows) ───────

def _wal_pragmas(conn, timeout_ms: int = 30000):
    try:
        conn.execute('PRAGMA busy_timeout=%d' % timeout_ms)
    except sqlite3.Error:
        pass
    try:
        return conn.execute('PRAGMA journal_mode=WAL').fetchone()
    except sqlite3.Error:
        return None


def _db_update_progress(catalog_db_path: str, execution_id: int,
                        processed: int, total: int):
    """Never raises: progress reporting is not worth a job."""
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
            conn.execute(
                'UPDATE workflow_executions SET processed_files=?, total_files=? WHERE id=?',
                (processed, total, execution_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[progress] could not record {processed}/{total}: {exc}', file=sys.stderr)


def _db_complete(catalog_db_path: str, execution_id: int, status: str,
                 error_message: str = None, skipped_files: int = 0):
    """Never raises: usually called from an exception handler."""
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
            conn.execute(
                '''UPDATE workflow_executions
                   SET status=?, completed_at=CURRENT_TIMESTAMP,
                       error_message=?, skipped_files=?
                   WHERE id=?''',
                (status, error_message, skipped_files, execution_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[complete] could not mark execution {execution_id} as {status}: {exc}',
              file=sys.stderr)


# ─── Geometry ─────────────────────────────────────────────────────────────────

def haversine_m(lat1, lon1, lat2, lon2) -> float:
    """Great-circle distance in METRES. Metres, not kilometres, because every
    threshold in this workflow is a few hundred of them."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return EARTH_RADIUS_M * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def centroid(members):
    """
    Mean latitude and longitude of (file_id, lat, lon) members.

    A plain arithmetic mean, which is wrong in two cases that cannot arise here
    and are recorded so nobody has to re-derive them: across the ±180° meridian,
    and near a pole. A bucket is at most one geohash cell wide - 5 km at the
    coarsest precision this workflow will accept - so its members never straddle
    either. A MERGED bucket is bounded by MERGE_METRES on top of that.
    """
    n = float(len(members))
    return (sum(m[1] for m in members) / n, sum(m[2] for m in members) / n)


def radius_m(members, lat, lon) -> float:
    """Distance from the centroid to the furthest member."""
    return max((haversine_m(lat, lon, m[1], m[2]) for m in members), default=0.0)


# ─── Bucketing ────────────────────────────────────────────────────────────────

def bucket_by_prefix(rows, precision):
    """{prefix: [(file_id, lat, lon, geohash), ...]} for one prefix length."""
    buckets = {}
    for row in rows:
        buckets.setdefault(row[3][:precision], []).append(row)
    return buckets


def split_oversized(buckets, depth=0):
    """
    Re-bucket any bucket over SPLIT_FILES one character finer, recursively.

    Returns {prefix: members} where a split bucket's members appear under their
    LONGER prefixes and the oversized prefix itself is gone. The prefix length
    is recoverable from len(prefix), which is why location_clusters stores
    precision per row rather than assuming one for the table.
    """
    if not SPLIT_FILES or depth >= MAX_SPLIT_DEPTH:
        return buckets

    out = {}
    changed = False
    for prefix, members in buckets.items():
        if len(members) > SPLIT_FILES and len(prefix) < 12:
            finer = bucket_by_prefix(members, len(prefix) + 1)
            if len(finer) > 1:
                out.update(finer)
                changed = True
                continue
        out[prefix] = members
    return split_oversized(out, depth + 1) if changed else out


def merge_adjacent(buckets):
    """
    Merge buckets whose centroids are within MERGE_METRES, repairing the cell
    boundary split (spec 4.3).

    Returns [(prefix, members)] where prefix is the LEXICOGRAPHICALLY SMALLEST
    of the merged group. That choice is what gives a merged cluster a stable
    identity across runs: location_clusters is keyed on (geohash_prefix,
    precision), so a cluster a person has accepted must come back under the same
    key next time. It is stable as long as the same buckets merge; new photos
    that shift a centroid enough to change which buckets merge will change the
    key, and the cluster is then proposed afresh. Rare, and the alternative - a
    synthetic id - would make a re-run unable to recognise its own earlier work
    at all.

    The sweep is over bucket CENTROIDS, 1,510 of them on the family catalog,
    never over the 158,000 photos.
    """
    items = sorted(buckets.items())
    centroids = {p: centroid(m) for p, m in items}
    parent = {p: p for p, _ in items}

    def find(p):
        while parent[p] != p:
            parent[p] = parent[parent[p]]
            p = parent[p]
        return p

    for i, (pa, _) in enumerate(items):
        la, oa = centroids[pa]
        for pb, _ in items[i + 1:]:
            lb, ob = centroids[pb]
            # Cheap reject before the trigonometry: MERGE_METRES of latitude is
            # a fixed number of degrees, so anything further north or south than
            # that cannot possibly be within range.
            if abs(lb - la) > (MERGE_METRES / 111320.0) * 1.5:
                continue
            if haversine_m(la, oa, lb, ob) <= MERGE_METRES:
                ra, rb = find(pa), find(pb)
                if ra != rb:
                    parent[max(ra, rb)] = min(ra, rb)

    groups = {}
    for p, members in items:
        groups.setdefault(find(p), []).extend(members)
    return sorted(groups.items())


# ─── The workflow ─────────────────────────────────────────────────────────────

def execute(source_dir, target_dir, files, progress_callback,
            catalog_id=None, catalog=None, execution_id=None):
    """
    Extract Locations workflow.

    Buckets every geo-tagged file by geohash prefix, merges buckets that are
    really one place, drops buckets too small to be a place, asks backend-geoloc
    what is near each survivor, and writes them as PROPOSALS.

    Args:
        source_dir:        Unused; coordinates come from the catalog database
        target_dir:        Catalog target directory (contains workflow_results.db)
        files:             Unused; the population is every geo-tagged file
        progress_callback: Callback(processed, total) for progress updates
        catalog_id:        Unused; kept for interface compatibility
        catalog:           Unused; kept for interface compatibility
        execution_id:      Workflow execution ID, for progress and stopping

    Returns:
        dict: statistics about the run
    """
    results_db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(results_db_path):
        raise FileNotFoundError(
            f"Catalog database not found at {results_db_path}. "
            "Run a file-import workflow first.")

    conn = sqlite3.connect(results_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    _wal_pragmas(conn, 30000)
    ensure_location_schema(conn)
    cur = conn.cursor()

    # ── scan ──────────────────────────────────────────────────────────────────
    # ORDER BY geohash, not by the prefix: the partial index is on the whole
    # column, so this order comes from the index itself and prefixes are
    # contiguous within it. Only promoted columns are read - raw_exif_json is
    # the largest column in exif_data and must not be touched (spec 11).
    cur.execute('''
        SELECT e.file_id, e.latitude, e.longitude, e.geohash, fc.master_datetime
          FROM exif_data e
          JOIN file_contents fc ON fc.id = e.file_id
         WHERE e.geohash IS NOT NULL
           AND e.latitude IS NOT NULL AND e.longitude IS NOT NULL
           AND fc.removed_at IS NULL
         ORDER BY e.geohash
    ''')
    rows = [(r['file_id'], r['latitude'], r['longitude'], r['geohash'],
             r['master_datetime']) for r in cur.fetchall()]

    # A geohash shorter than the requested precision cannot be bucketed at it.
    # compute_geohash returning something unexpected used to be invisible; now
    # it is counted and reported (spec 9.2).
    short = [r for r in rows if len(r[3] or '') < PRECISION]
    rows = [r for r in rows if len(r[3] or '') >= PRECISION]

    print(f'[locations] {len(rows)} geo-tagged files, precision {PRECISION}'
          + (f', {len(short)} skipped for a short geohash' if short else ''))

    if not rows:
        conn.close()
        if progress_callback:
            progress_callback(0, 0)
        # Not an error. A digitised collection can have almost no GPS at all -
        # second-catalog has 17 coordinates across 24,269 files - and the
        # Locations page says what to do instead (spec 8.1b).
        return {'processed_files': 0, 'clusters': 0, 'files_clustered': 0,
                'skipped_files': len(short),
                'message': 'No geo-tagged files to cluster.'}

    # ── bucket, split, merge ──────────────────────────────────────────────────
    buckets = bucket_by_prefix(rows, PRECISION)
    seeded = len(buckets)
    buckets = split_oversized(buckets)
    merged = merge_adjacent(buckets)

    # ── filter ────────────────────────────────────────────────────────────────
    keep = [(p, m) for p, m in merged if len(m) >= MIN_FILES]
    dropped = len(merged) - len(keep)
    print(f'[locations] {seeded} seed buckets -> {len(buckets)} after splitting '
          f'-> {len(merged)} after merging -> {len(keep)} with >= {MIN_FILES} files '
          f'({dropped} too small)')

    total = len(keep)
    seen_prefixes = []
    clusters_written = 0
    locations_created = 0
    files_clustered = 0
    suggestions_fetched = 0
    geocode_down = False
    stopped = False

    for index, (prefix, members) in enumerate(keep, start=1):
        lat, lon = centroid(members)
        rad = radius_m(members, lat, lon)
        stamps = sorted(m[4] for m in members if m[4])

        # ── suggest ───────────────────────────────────────────────────────────
        # Entirely optional. A cluster with no suggestion is still a cluster a
        # person can name, and once the service has failed once this run we stop
        # asking - a thousand clusters each waiting for a timeout would turn a
        # minute of work into hours.
        suggestions = None
        existing = cur.execute(
            'SELECT id, status, location_id, suggestions FROM location_clusters '
            'WHERE geohash_prefix = ? AND precision = ?',
            (prefix, len(prefix))).fetchone()
        if existing and existing['suggestions']:
            suggestions = existing['suggestions']   # already asked, keep it
        elif not geocode_down:
            try:
                pois = nearby(lat, lon)
                suggestions = json.dumps(pois)
                suggestions_fetched += 1
            except GeocodeUnavailable as exc:
                geocode_down = True
                print(f'[locations] geocoding unavailable, continuing without '
                      f'suggestions: {exc}')

        # ── commit, per bucket ────────────────────────────────────────────────
        # An UPDATE on conflict rather than a REPLACE: status and location_id
        # are a person's decisions and must survive a re-run (spec 7.2).
        cur.execute('''
            INSERT INTO location_clusters
                (geohash_prefix, precision, latitude, longitude, radius_metres,
                 file_count, first_seen, last_seen, suggestions)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (geohash_prefix, precision) DO UPDATE SET
                latitude = excluded.latitude,
                longitude = excluded.longitude,
                radius_metres = excluded.radius_metres,
                file_count = excluded.file_count,
                first_seen = excluded.first_seen,
                last_seen = excluded.last_seen,
                suggestions = COALESCE(excluded.suggestions, suggestions),
                updated_at = CURRENT_TIMESTAMP
        ''', (prefix, len(prefix), lat, lon, rad, len(members),
              stamps[0] if stamps else None, stamps[-1] if stamps else None,
              suggestions))

        cluster_id = (existing['id'] if existing else cur.lastrowid)

        # ── the locations row ─────────────────────────────────────────────────
        # One per cluster, unconfirmed, so the page has one kind of thing to
        # show. A cluster someone has REJECTED gets none, and an existing row
        # is never renamed: the name may be the one a person chose, and this
        # workflow re-runs after every import.
        location_id = existing['location_id'] if existing else None
        rejected = bool(existing and existing['status'] == 'rejected')
        if location_id is None and not rejected:
            try:
                pois = json.loads(suggestions) if suggestions else []
            except (ValueError, TypeError):
                pois = []
            cur.execute(
                "INSERT INTO locations (name, latitude, longitude, source, confirmed) "
                "VALUES (?, ?, ?, 'cluster', 0)",
                (provisional_name(pois, lat, lon), lat, lon))
            location_id = cur.lastrowid
            cur.execute('UPDATE location_clusters SET location_id = ? WHERE id = ?',
                        (location_id, cluster_id))
            locations_created += 1
        elif location_id is not None:
            # Keep the centroid current as photos are added. The NAME is left
            # alone, confirmed or not - a geocoder guess that someone has been
            # looking at is still the thing they were looking at.
            cur.execute('UPDATE locations SET latitude = ?, longitude = ?, '
                        'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                        (lat, lon, location_id))

        cur.execute('DELETE FROM location_cluster_files WHERE cluster_id = ?',
                    (cluster_id,))
        cur.executemany(
            'INSERT OR IGNORE INTO location_cluster_files '
            '(cluster_id, file_id, distance_metres) VALUES (?, ?, ?)',
            [(cluster_id, m[0], haversine_m(lat, lon, m[1], m[2])) for m in members])
        conn.commit()

        seen_prefixes.append((prefix, len(prefix)))
        clusters_written += 1
        files_clustered += len(members)

        # After every cluster, not every tenth: on a first run each one can
        # wait 10-60 s on the geocoder, so ten of them was up to ten minutes
        # of a counter that did not move. A cluster is already its own commit,
        # and ~1,000 progress writes cost nothing beside the geocoding.
        if progress_callback:
            progress_callback(index, total)

        if execution_id and stop_requested(results_db_path, execution_id):
            print(f'\n[locations] Stop requested after {index} of {total} clusters.')
            stopped = True
            break

    # ── retire proposals that no longer exist ─────────────────────────────────
    # A bucket that merged into a neighbour, or fell below MIN_FILES, leaves a
    # row behind. Only 'proposed' rows are removed: an accepted cluster is a
    # locations row somebody is using, and a rejected one must stay rejected or
    # the next run proposes it again. Skipped after a stop, when "not seen" only
    # means "not reached".
    retired = 0
    locations_retired = 0
    if not stopped and seen_prefixes:
        cur.execute('CREATE TEMP TABLE IF NOT EXISTS _seen '
                    '(geohash_prefix TEXT, precision INTEGER)')
        cur.execute('DELETE FROM _seen')
        cur.executemany('INSERT INTO _seen VALUES (?, ?)', seen_prefixes)
        # The UNCONFIRMED locations row goes with the cluster that created it.
        # A confirmed one does not: somebody named that, and a name outlives the
        # grouping that suggested it. Done before the cluster rows are deleted,
        # while the link still exists.
        cur.execute('''
            DELETE FROM locations WHERE confirmed = 0 AND id IN (
                SELECT location_id FROM location_clusters
                 WHERE status = 'proposed' AND location_id IS NOT NULL
                   AND (geohash_prefix, precision) NOT IN (SELECT * FROM _seen))
        ''')
        locations_retired = cur.rowcount
        cur.execute('''
            DELETE FROM location_cluster_files WHERE cluster_id IN (
                SELECT id FROM location_clusters WHERE status = 'proposed'
                  AND (geohash_prefix, precision) NOT IN (SELECT * FROM _seen))
        ''')
        cur.execute('''
            DELETE FROM location_clusters WHERE status = 'proposed'
              AND (geohash_prefix, precision) NOT IN (SELECT * FROM _seen)
        ''')
        retired = cur.rowcount
        conn.commit()
        if locations_retired:
            print(f'[locations] {locations_retired} unconfirmed location(s) '
                  f'retired with their clusters')

    conn.close()

    print(f'[locations] {clusters_written} clusters, {files_clustered} files, '
          f'{locations_created} new locations, {suggestions_fetched} geocoded, '
          f'{retired} stale proposals retired')

    if stopped:
        if execution_id:
            record_stopped(results_db_path, execution_id,
                           processed_files=clusters_written,
                           skipped_files=len(short),
                           message=stop_message(clusters_written, total))
        return {'processed_files': clusters_written, 'clusters': clusters_written,
                'files_clustered': files_clustered, 'skipped_files': len(short),
                'stopped': True}

    return {'processed_files': clusters_written, 'clusters': clusters_written,
            'files_clustered': files_clustered, 'locations_created': locations_created,
            'suggestions': suggestions_fetched, 'retired': retired,
            'skipped_files': len(short)}


# ─── Standalone subprocess entry point ────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Extract locations workflow (subprocess mode)')
    parser.add_argument('--execution-id', type=int, required=True)
    parser.add_argument('--target-dir', type=str, required=True)
    parser.add_argument('--catalog-id', type=int, required=True)
    args = parser.parse_args()

    catalog_db_path = os.path.join(args.target_dir, 'workflow_results.db')

    def subprocess_progress(processed, total=None):
        _db_update_progress(catalog_db_path, args.execution_id,
                            processed, total if total is not None else processed)

    try:
        result = execute(
            source_dir=None,
            target_dir=args.target_dir,
            files=[],
            progress_callback=subprocess_progress,
            catalog_id=args.catalog_id,
            execution_id=args.execution_id,
        )
        if not (isinstance(result, dict) and result.get('stopped')):
            _db_complete(catalog_db_path, args.execution_id, 'completed',
                         skipped_files=result.get('skipped_files', 0)
                         if isinstance(result, dict) else 0)
        print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)
