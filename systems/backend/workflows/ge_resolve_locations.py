## Resolve Locations
"""
Write, per file, where it was taken and what location that is.

Implements documentation/specs/location-spec.md sections 4.1, 4.5 and 6.

Two facts, resolved from two ladders, with separate provenance:

    coordinates:  manual > exif > event*     (* off by default - 4.5c)
    location:     manual > cluster > a person's tag on the event >
                  the event's member vote

A coordinate is a MEASUREMENT and a location is a HUMAN JUDGEMENT. Nearly every
awkwardness in the old design came from storing both in one column with no
record of which was which, so file_locations keeps coord_source and location_source
apart and this workflow is the only thing that writes them.

Inheritance is the valuable part: 195,194 files in the family catalog sit in an
event with no coordinates of their own, and 22,873 in second-catalog. It comes
from the event's MEMBERS, never from events.latitude - that column used to hold
the catalog owner's home address when no member carried GPS, and propagating it
would have fabricated a hometown for 107,737 files (spec 3.1).
"""

import os
import sys
import sqlite3
from collections import Counter

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


# ─── Settings ─────────────────────────────────────────────────────────────────

# An event needs at least this many members with a resolved location before their
# agreement means anything. One photo is not a consensus.
INHERIT_MIN_MEMBERS = int(os.environ.get('LOCATION_INHERIT_MIN_MEMBERS', '2'))

# The winning location must hold at least this share of the vote. This IS the
# coherence test: an event may legitimately span MAX_GEO_DISTANCE_KM (50 km), so
# a walk across one neighbourhood produces a clear winner and a road trip
# produces no majority and inherits nothing. Refusing to answer is the correct
# output for a 50 km event (spec 4.5b).
INHERIT_MIN_SHARE = float(os.environ.get('LOCATION_INHERIT_MIN_SHARE', '0.6'))

# Off by default, and the spec argues at length that it should stay off: a
# coordinate nobody measured makes every distance query lie, and because the
# clustering workflow READS coordinates, letting inherited ones into the pool
# would have run n+1 cluster run n's inventions (spec 4.5c, 4.8).
INHERIT_COORDINATES = os.environ.get('LOCATION_INHERIT_COORDINATES', '0') == '1'


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


CLEAR_LOCATION = ('location_id = NULL, location_source = NULL, '
               'location_event_id = NULL, location_share = NULL')


# ─── Phases ───────────────────────────────────────────────────────────────────

def phase_exif(conn) -> int:
    """
    Every file whose camera recorded a position gets coord_source='exif'.

    A manual override is never overwritten - that is the top of the coordinate
    ladder. `IS NOT` rather than `!=` because coord_source is NULL for most rows
    and `!=` against NULL is NULL, which would silently match nothing.
    """
    cur = conn.cursor()
    cur.execute('''
        INSERT INTO file_locations
            (file_id, latitude, longitude, coord_source, coord_radius_m)
        SELECT e.file_id, e.latitude, e.longitude, 'exif', 0
          FROM exif_data e
          JOIN file_contents fc ON fc.id = e.file_id
         WHERE e.latitude IS NOT NULL AND e.longitude IS NOT NULL
           AND fc.removed_at IS NULL
        ON CONFLICT (file_id) DO UPDATE SET
            latitude = excluded.latitude,
            longitude = excluded.longitude,
            coord_source = 'exif',
            coord_radius_m = 0,
            updated_at = CURRENT_TIMESTAMP
         WHERE file_locations.coord_source IS NOT 'manual'
           -- Only when something differs. Every run used to rewrite all
           -- ~350,000 rows (and their updated_at) on `family` whether or not
           -- anything had changed; the count is now of rows that did.
           AND (file_locations.latitude IS NOT excluded.latitude
                OR file_locations.longitude IS NOT excluded.longitude
                OR file_locations.coord_source IS NOT 'exif'
                OR file_locations.coord_radius_m IS NOT 0)
    ''')
    n = cur.rowcount
    conn.commit()
    return n


def phase_cluster(conn) -> int:
    """
    A file whose own coordinate falls in a cluster takes that cluster's
    location, confirmed or not.

    Not only confirmed ones: the clustering workflow writes a location per
    cluster now, so a file can be placed as soon as it is grouped, and
    confirming is about trust and about what the event picker offers - not about
    whether the photo has a location at all. A REJECTED cluster confers nothing.

    Also clears locations from clusters that have since been rejected, or whose
    location was deleted. A stale location is worse than none: it is
    indistinguishable from a current one.
    """
    cur = conn.cursor()
    cur.execute(f'''
        UPDATE file_locations SET {CLEAR_LOCATION}, updated_at = CURRENT_TIMESTAMP
         WHERE location_source = 'cluster'
           AND file_id NOT IN (
               SELECT lcf.file_id FROM location_cluster_files lcf
                 JOIN location_clusters lc ON lc.id = lcf.cluster_id
                WHERE lc.status <> 'rejected' AND lc.location_id IS NOT NULL)
    ''')
    cur.execute('''
        INSERT INTO file_locations (file_id, location_id, location_source)
        SELECT lcf.file_id, lc.location_id, 'cluster'
          FROM location_cluster_files lcf
          JOIN location_clusters lc ON lc.id = lcf.cluster_id
         WHERE lc.status <> 'rejected' AND lc.location_id IS NOT NULL
        ON CONFLICT (file_id) DO UPDATE SET
            location_id = excluded.location_id,
            location_source = 'cluster',
            location_event_id = NULL,
            location_share = NULL,
            updated_at = CURRENT_TIMESTAMP
         WHERE file_locations.location_source IS NOT 'manual'
           -- Only when something differs - see phase_exif.
           AND (file_locations.location_id IS NOT excluded.location_id
                OR file_locations.location_source IS NOT 'cluster'
                OR file_locations.location_event_id IS NOT NULL
                OR file_locations.location_share IS NOT NULL)
    ''')
    n = cur.rowcount
    conn.commit()
    return n


def phase_clear_orphans(conn) -> int:
    """
    Drop inherited locations whose event no longer exists.

    Create Events deletes and recreates every Auto event on each run, so an
    event id is not durable (spec 3.2). Rows sourced from a vanished event are
    cleared here rather than tracked, which is cheap and always correct.
    """
    cur = conn.cursor()
    cur.execute(f'''
        UPDATE file_locations SET {CLEAR_LOCATION}, updated_at = CURRENT_TIMESTAMP
         WHERE location_source = 'event'
           AND (location_event_id IS NULL
                OR location_event_id NOT IN (SELECT id FROM events))
    ''')
    n = cur.rowcount
    conn.commit()
    return n


def event_location(conn, event_id):
    """
    The location an event confers on its members, and how we know.

    Returns (location_id, share) or (None, None):

      a person's tag  -> (id, None)   location_events, spec 4.5a
      a member vote   -> (id, share)  spec 4.5b

    A tag outranks a vote because an explicit human judgement beats a derived
    majority. It is also the ONLY path that works at zero GPS, which is what the
    second-catalog catalog needs: 17 coordinates across 24,269 files, so nothing
    there has a vote to cast.

    The denominator of the vote is the members that have a RESOLVED PLACE, not
    every member with coordinates. A member whose coordinate sits in no accepted
    cluster yet does not disagree with anything - it simply does not know - and
    counting it against the winner would hold inheritance hostage until most
    clusters were named, which is exactly the composability 4.5b point 4 asks
    for. INHERIT_MIN_MEMBERS is what stops two photos locating a 500-file event.
    """
    row = conn.execute(
        'SELECT location_id FROM location_events WHERE event_id = ? '
        'ORDER BY location_id LIMIT 1', (event_id,)).fetchone()
    if row:
        # ORDER BY makes the choice deterministic where an event carries more
        # than one tag; location_events allows that and file_locations holds one
        # resolved location, so something has to choose. Hierarchy and many locations
        # per file are deferred together - see spec 10.
        return row[0], None

    votes = Counter()
    for (loc_id,) in conn.execute(
            '''SELECT fl.location_id
                 FROM event_files ef
                 JOIN file_locations fl ON fl.file_id = ef.file_id
                WHERE ef.event_id = ?
                  AND fl.coord_source = 'exif'
                  AND fl.location_id IS NOT NULL''', (event_id,)):
        votes[loc_id] += 1

    total = sum(votes.values())
    if total < INHERIT_MIN_MEMBERS:
        return None, None
    winner, count = votes.most_common(1)[0]
    share = count / float(total)
    if share < INHERIT_MIN_SHARE:
        return None, None       # the event spans locations; say nothing
    return winner, share


def phase_inherit(conn, results_db_path, execution_id, progress_callback):
    """
    Give every file with no location of its own the location its event confers.

    Delete-then-insert PER EVENT, inside one transaction each. That is what
    earns this workflow stop mode 'item' where ev_extract_events has to be
    'never': a stop between two events leaves earlier events resolved and later
    ones exactly as they were. A table-wide DELETE followed by a rebuild would
    leave less than there was.

    Not a resume, though it was described as one: a re-run does the three bulk
    phases again and walks every event from the first. That is correct - each
    step is idempotent, so a re-run lands where an uninterrupted run would -
    and the bulk phases now skip rows that would not change.
    """
    event_ids = [r[0] for r in conn.execute(
        'SELECT DISTINCT event_id FROM event_files ORDER BY event_id')]
    total = len(event_ids)
    placed = tagged = voted = abstained = 0
    stopped = False

    for index, event_id in enumerate(event_ids, start=1):
        loc_id, share = event_location(conn, event_id)

        cur = conn.cursor()
        # Clear this event's inherited locations first, so an event that no longer
        # confers one stops conferring it.
        cur.execute(f'''
            UPDATE file_locations SET {CLEAR_LOCATION}, updated_at = CURRENT_TIMESTAMP
             WHERE location_source = 'event'
               AND file_id IN (SELECT file_id FROM event_files WHERE event_id = ?)
        ''', (event_id,))

        if loc_id is None:
            abstained += 1
            conn.commit()
        else:
            # Only files with NO location: a file's own cluster or a manual tag
            # outranks what its event thinks (spec 6).
            cur.execute('''
                INSERT INTO file_locations
                    (file_id, location_id, location_source, location_event_id, location_share)
                SELECT ef.file_id, ?, 'event', ?, ?
                  FROM event_files ef
                  JOIN file_contents fc ON fc.id = ef.file_id
                 WHERE ef.event_id = ? AND fc.removed_at IS NULL
                ON CONFLICT (file_id) DO UPDATE SET
                    location_id = excluded.location_id,
                    location_source = 'event',
                    location_event_id = excluded.location_event_id,
                    location_share = excluded.location_share,
                    updated_at = CURRENT_TIMESTAMP
                 WHERE file_locations.location_source IS NULL
            ''', (loc_id, event_id, share, event_id))
            placed += cur.rowcount
            if share is None:
                tagged += 1
            else:
                voted += 1
            conn.commit()

        if progress_callback and (index % 50 == 0 or index == total):
            progress_callback(index, total)

        if execution_id and stop_requested(results_db_path, execution_id):
            print(f'\n[resolve] Stop requested after {index} of {total} events.')
            stopped = True
            break

    return {'events': total, 'files_inherited': placed, 'tagged_events': tagged,
            'voted_events': voted, 'abstained_events': abstained,
            'stopped': stopped, 'processed': index if event_ids else 0}


# ─── The workflow ─────────────────────────────────────────────────────────────

def execute(source_dir, target_dir, files, progress_callback,
            catalog_id=None, catalog=None, execution_id=None):
    """
    Resolve Locations workflow.

    Args:
        source_dir:        Unused; everything comes from the catalog database
        target_dir:        Catalog target directory (contains workflow_results.db)
        files:             Unused
        progress_callback: Callback(processed, total) for progress updates
        catalog_id:        Unused; kept for interface compatibility
        catalog:           Unused; kept for interface compatibility
        execution_id:      Workflow execution ID, for progress and stopping

    Returns:
        dict: statistics about the run, including a per-source breakdown
    """
    results_db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(results_db_path):
        raise FileNotFoundError(
            f"Catalog database not found at {results_db_path}. "
            "Run a file-import workflow first.")

    conn = sqlite3.connect(results_db_path, timeout=30)
    _wal_pragmas(conn, 30000)
    ensure_location_schema(conn)

    if INHERIT_COORDINATES:
        # Deliberately loud. Turning this on puts coordinates nobody measured
        # into the column the clustering workflow reads.
        print('[resolve] WARNING: LOCATION_INHERIT_COORDINATES=1 - inherited '
              'coordinates will be written. See location-spec.md 4.5c.')

    exif_rows = phase_exif(conn)
    cluster_rows = phase_cluster(conn)
    orphans = phase_clear_orphans(conn)
    inh = phase_inherit(conn, results_db_path, execution_id, progress_callback)

    # What a person actually wants to know: how much of the map is evidence.
    breakdown = dict(conn.execute(
        '''SELECT COALESCE(location_source, 'none'), COUNT(*)
             FROM file_locations GROUP BY location_source''').fetchall())
    measured = conn.execute(
        "SELECT COUNT(*) FROM file_locations WHERE coord_source = 'exif'").fetchone()[0]
    conn.close()

    print(f'[resolve] coordinates: {measured} measured  |  locations: '
          + ', '.join(f'{k} {v}' for k, v in sorted(breakdown.items()))
          + f'  |  events: {inh["tagged_events"]} tagged, {inh["voted_events"]} voted, '
            f'{inh["abstained_events"]} abstained for want of a majority')
    if orphans:
        print(f'[resolve] cleared {orphans} location(s) inherited from events that '
              f'no longer exist')

    stats = {
        'processed_files': exif_rows + cluster_rows + inh['files_inherited'],
        'coordinates_measured': measured,
        'locations_by_source': breakdown,
        'events': inh['events'],
        'files_inherited': inh['files_inherited'],
        'tagged_events': inh['tagged_events'],
        'voted_events': inh['voted_events'],
        'abstained_events': inh['abstained_events'],
        'orphans_cleared': orphans,
        'skipped_files': 0,
    }

    if inh['stopped']:
        if execution_id:
            record_stopped(results_db_path, execution_id,
                           processed_files=inh['processed'],
                           message=stop_message(inh['processed'], inh['events']))
        stats['stopped'] = True
    return stats


# ─── Standalone subprocess entry point ────────────────────────────────────────

if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Resolve locations workflow (subprocess mode)')
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
