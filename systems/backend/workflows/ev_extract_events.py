## Create Events
import os
import sys
import sqlite3
import math
import urllib.request
import json
from datetime import datetime, timedelta
from typing import Optional

# Add parent directory to path to import catalog module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Events are made of photos and videos only. An 'Others' file - anything the
# importer kept without recognising it - has nothing the event page can show,
# so an event built from them looks empty while claiming N files (oldserver's event
# 117: six macOS .fseventsd logs, dated like the photos beside them).
from media_types import is_media_file  # noqa: E402


# ─── Subprocess progress helpers ──────────────────────────────────────────────

def _wal_pragmas(conn, timeout_ms: int = 30000):
    """
    Best-effort connection pragmas for a catalog database.

    journal_mode is deliberately NOT allowed to raise. It is PERSISTENT - written
    into the database header when the file is created and inherited by every
    later connection - so setting it again here is redundant. It is also the
    statement most likely to fail under contention, because switching journal
    mode needs an exclusive lock and a header read. Letting it throw is how a
    transient fault killed workflows that had already done their work, with
    "disk I/O error" and "file is not a database" both raised from this one line.

    busy_timeout is set first and matters more: it makes a concurrent writer
    wait rather than fail.
    """
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
    """Write progress directly to workflow_executions (used when running as subprocess)."""
    # NEVER raises. This runs from the progress callback inside the main loop,
    # so an exception here kills a workflow that is otherwise working - which is
    # exactly what happened three times: runs died on the pragma above after
    # finishing their real work. Progress reporting is not worth a job.
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
            conn.execute(
                'UPDATE workflow_executions SET processed_files=?, total_files=? WHERE id=?',
                (processed, total, execution_id)
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[progress] could not record {processed}/{total}: {exc}', file=sys.stderr)


def _db_complete(catalog_db_path: str, execution_id: int, status: str,
                 error_message: str = None, skipped_files: int = 0):
    """Mark execution finished in the DB (used when running as subprocess)."""
    # Never raises either. This is usually called FROM an exception handler, so
    # throwing here replaces the real error with a database one and loses the
    # original - and leaves the execution stuck at 'running' for good, holding a
    # concurrency slot that nothing ever frees.
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
            conn.execute(
                '''UPDATE workflow_executions
                   SET status=?, completed_at=CURRENT_TIMESTAMP,
                       error_message=?, skipped_files=?
                   WHERE id=?''',
                (status, error_message, skipped_files, execution_id)
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[complete] could not mark execution {execution_id} as {status}: {exc}',
              file=sys.stderr)


# Maximum gap in hours between files to be grouped into the same event
EVENT_GAP_HOURS = 2

# Maximum distance in kilometres between an asset's GPS location and the
# event's established location for the asset to still be grouped into that
# event.  Assets whose GPS location is farther away will start a new event.
MAX_GEO_DISTANCE_KM = 50.0


def parse_timestamp(ts_str: Optional[str]) -> Optional[datetime]:
    """
    Try to parse a timestamp string in various formats.
    Returns a naive datetime object (tzinfo stripped) or None if parsing fails.
    """
    if not ts_str:
        return None

    # datetime.fromisoformat() handles ISO 8601 with timezone offsets
    # (e.g. "2026-01-07T14:11:25-05:00") which strptime format strings cannot.
    # Strip tzinfo so all datetimes in this workflow are naive and comparable.
    try:
        dt = datetime.fromisoformat(ts_str.strip())
        return dt.replace(tzinfo=None)
    except (ValueError, AttributeError):
        pass

    formats = [
        '%Y:%m:%d %H:%M:%S',        # EXIF format
        '%Y-%m-%dT%H:%M:%S.%f',     # ISO 8601 with microseconds
        '%Y-%m-%dT%H:%M:%S',        # ISO 8601
        '%Y-%m-%d %H:%M:%S.%f',     # ISO-like with microseconds
        '%Y-%m-%d %H:%M:%S',        # ISO-like
        '%Y-%m-%dT%H:%M:%SZ',       # ISO 8601 UTC
        '%Y-%m-%dT%H:%M:%S.%fZ',    # ISO 8601 UTC with microseconds
        '%Y-%m-%d',                 # Date only
    ]
    for fmt in formats:
        try:
            return datetime.strptime(ts_str.strip(), fmt)
        except (ValueError, AttributeError):
            continue
    return None


def best_timestamp_for_file(row: dict) -> Optional[datetime]:
    """
    Return the best available datetime for a file_contents row.
    Priority: master_datetime (canonical) > added_at fallback.
    master_datetime is set at ingestion time by resolve_master_datetime() and
    already encodes the EXIF > directory > filename > filesystem hierarchy.
    """
    # Primary: use the pre-resolved canonical date
    dt = parse_timestamp(row.get('master_datetime'))
    if dt:
        return dt

    # Fallback for legacy rows where master_datetime has not yet been backfilled
    dt = parse_timestamp(row.get('added_at'))
    if dt:
        return dt

    return None


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """
    Return the great-circle distance in kilometres between two points
    specified by (latitude, longitude) in decimal degrees.
    """
    R = 6371.0  # Earth radius in km
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = (math.sin(dphi / 2) ** 2
         + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2)
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


# Events created between commits (and progress reports) - see execute().
COMMIT_EVERY = 250


def group_lat_lon(group: list) -> tuple:
    """
    Return the (latitude, longitude) that represents the group's established
    location: the lat/lon of the *first* item in the group that has GPS data.
    Returns (None, None) if no item in the group has GPS data.
    """
    for _, row in group:
        lat = row.get('latitude')
        lon = row.get('longitude')
        if lat is not None and lon is not None:
            return lat, lon
    return None, None


def group_files(dated: list):
    """
    Dated files (sorted by time) into events, by time AND distance, with each
    group's established location: ([[(datetime, row), ...], ...], [(lat, lon), ...]).
    """
    gap = timedelta(hours=EVENT_GAP_HOURS)
    groups = []   # list of list[(datetime, row)]
    # Each group's established location - the first member with GPS - kept
    # beside the group and set once. group_lat_lon(groups[-1]) used to be asked
    # for every file, scanning the group from its first member: quadratic in a
    # group with no GPS, and `family` has ~195,000 files in such events.
    group_locs = []

    def _gps(row):
        lat, lon = row.get('latitude'), row.get('longitude')
        return (lat, lon) if lat is not None and lon is not None else (None, None)

    for item in dated:
        dt, row = item
        if not groups:
            groups.append([item])
            group_locs.append(_gps(row))
            continue

        last_dt = groups[-1][-1][0]

        # Time gap check: if too far apart in time, always start a new group
        if dt - last_dt > gap:
            groups.append([item])
            group_locs.append(_gps(row))
            continue

        # Within the time window — now apply geo check if applicable.
        # The established location of the current (last) group.
        event_lat, event_lon = group_locs[-1]
        item_lat = row.get('latitude')
        item_lon = row.get('longitude')

        if (event_lat is not None and event_lon is not None
                and item_lat is not None and item_lon is not None):
            # Both the event and the new asset have GPS data — check distance.
            dist_km = haversine_km(event_lat, event_lon, item_lat, item_lon)
            if dist_km > MAX_GEO_DISTANCE_KM:
                # Too far away: start a new group even though time is close.
                groups.append([item])
                group_locs.append(_gps(row))
                continue

        # Either no GPS data to compare, or within acceptable distance.
        groups[-1].append(item)
        if group_locs[-1][0] is None:
            group_locs[-1] = _gps(row)
    return groups, group_locs


def format_event_name(start_dt: datetime, index: int) -> str:
    """Generate a human-readable event name from its start datetime."""
    return f"Event {start_dt.strftime('%Y-%m-%d %H:%M')} (#{index})"


def remove_empty_events(conn) -> int:
    """
    Delete every event - Auto or Manual - left with no photo or video in it.

    Events empty out when their files are removed from the catalog, re-dated
    into another event, or purged. A link to a removed or vanished file, or to
    a file that is not a photo or video, does not count, and is dropped first.
    Surviving events get their stored file_count recounted from the links, so
    the Events page shows the truth.

    Nothing that points at an event cascades (the application never enables
    PRAGMA foreign_keys), so its location links are deleted and the columns
    that name it are cleared here. Returns how many events were deleted.
    """
    cur = conn.cursor()
    cur.execute('''
        DELETE FROM event_files
        WHERE file_id NOT IN (SELECT id FROM file_contents WHERE removed_at IS NULL)
    ''')
    not_media = [fid for fid, name in cur.execute('''
        SELECT DISTINCT fc.id, fc.file_name FROM event_files ef
        JOIN file_contents fc ON fc.id = ef.file_id
    ''').fetchall() if not is_media_file(name)]
    for i in range(0, len(not_media), 500):
        chunk = not_media[i:i + 500]
        cur.execute(f"DELETE FROM event_files WHERE file_id IN ({','.join('?' * len(chunk))})",
                    chunk)
    empty = [r[0] for r in cur.execute('''
        SELECT id FROM events
        WHERE id NOT IN (SELECT DISTINCT event_id FROM event_files)
    ''').fetchall()]
    if empty:
        marks = ','.join('?' * len(empty))
        for sql in (f'DELETE FROM location_events WHERE event_id IN ({marks})',
                    f'UPDATE album_sections SET source_event_id = NULL '
                    f'WHERE source_event_id IN ({marks})',
                    f'UPDATE face_similarity_scores_audit SET source_event_id = NULL '
                    f'WHERE source_event_id IN ({marks})'):
            try:
                cur.execute(sql, empty)
            except sqlite3.OperationalError:
                pass  # a table or column this catalog does not have yet
        cur.execute(f'DELETE FROM events WHERE id IN ({marks})', empty)
    cur.execute('''
        UPDATE events SET file_count =
            (SELECT COUNT(*) FROM event_files ef WHERE ef.event_id = events.id)
    ''')
    conn.commit()
    return len(empty)


def execute(source_dir, target_dir, files, progress_callback,
            catalog_id=None, catalog=None, execution_id=None):
    """
    Extract Events workflow.

    Reads all files already catalogued in the catalog's workflow_results.db,
    groups them by proximity in time (within EVENT_GAP_HOURS), creates one
    Event per group, maps every file to its event via event_files, and
    updates each event's start/end times to reflect the earliest and latest
    file timestamps in the group.

    Args:
        source_dir:        Source directory (not used directly; files come from DB)
        target_dir:        Catalog target directory (contains workflow_results.db)
        files:             List of file paths collected by the runner (used for
                           progress reporting only)
        progress_callback: Callback(processed_count) for progress updates
        catalog_id:        Catalog ID (unused; kept for interface compatibility)
        catalog:           Catalog instance (unused; kept for interface compatibility)
        execution_id:      Workflow execution ID for logging

    Returns:
        dict: Statistics about the execution
    """
    results_db_path = os.path.join(target_dir, 'workflow_results.db')

    if not os.path.exists(results_db_path):
        raise FileNotFoundError(
            f"Catalog database not found at {results_db_path}. "
            "Run a file-import workflow first."
        )

    conn = sqlite3.connect(results_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    # Enable WAL mode so concurrent readers/writers don't block each other
    _wal_pragmas(conn, 30000)
    cursor = conn.cursor()

    # Ensure events and event_files tables exist (idempotent)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            start_time TIMESTAMP NOT NULL,
            end_time TIMESTAMP NOT NULL,
            latitude REAL,
            longitude REAL,
            description TEXT,
            file_count INTEGER DEFAULT 0,
            type TEXT DEFAULT 'Auto',
            -- 'member' when a file in this event carried GPS, else NULL. Never
            -- the owner's home address - see location-spec.md 4.8.
            location_source TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    # This workflow can be the first thing to touch a catalog database, so the
    # table above may be ITS creation - but it may equally already exist from
    # before location_source did. The INSERT below names the column, so add it
    # here rather than relying on models.py having opened the catalog first.
    try:
        cursor.execute('ALTER TABLE events ADD COLUMN location_source TEXT')
        conn.commit()
    except Exception:
        pass  # Column already exists

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS event_files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event_id INTEGER NOT NULL,
            file_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE CASCADE,
            FOREIGN KEY (file_id) REFERENCES file_contents (id) ON DELETE CASCADE,
            UNIQUE (event_id, file_id)
        )
    ''')
    # Migrate existing events table: add 'type' column if it doesn't exist
    try:
        cursor.execute("ALTER TABLE events ADD COLUMN type TEXT DEFAULT 'Auto'")
        conn.commit()
    except Exception:
        pass  # Column already exists
    # Step 3 filters on it; normally added by models.CatalogDatabase already.
    try:
        cursor.execute('ALTER TABLE file_contents ADD COLUMN removed_at TIMESTAMP')
        conn.commit()
    except Exception:
        pass  # Column already exists

    conn.commit()

    # ------------------------------------------------------------------
    # 1. Collect file IDs already associated with Manual events (preserve)
    # ------------------------------------------------------------------
    cursor.execute('''
        SELECT DISTINCT ef.file_id
        FROM event_files ef
        JOIN events e ON ef.event_id = e.id
        WHERE e.type = 'Manual'
    ''')
    manual_file_ids = {row[0] for row in cursor.fetchall()}

    # ------------------------------------------------------------------
    # 2. Delete only Auto events and their associations
    # ------------------------------------------------------------------
    cursor.execute('''
        DELETE FROM location_events
        WHERE event_id IN (SELECT id FROM events WHERE type = 'Auto')
    ''')
    cursor.execute('''
        DELETE FROM event_files
        WHERE event_id IN (SELECT id FROM events WHERE type = 'Auto')
    ''')
    cursor.execute("DELETE FROM events WHERE type = 'Auto'")
    conn.commit()

    # ------------------------------------------------------------------
    # 3. Load all catalogued files NOT already in a Manual event
    # ------------------------------------------------------------------
    # One member per IMAGE: a group's secondary (the RAW behind its JPG, a Live
    # Photo's clip behind its still) is represented by its primary, as on the
    # Photos page (file-unity-spec.md section 6.4). Checked for first: a catalog
    # the backend has not opened since grouping arrived has no such table.
    has_groups = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='image_groups'").fetchone()
    secondaries_clause = '''
          AND fc.id NOT IN (
              SELECT f.id FROM file_contents f
                JOIN image_groups g ON g.id = f.image_group_id
               WHERE f.id <> g.primary_file_id
          )''' if has_groups else ''
    cursor.execute(f'''
        SELECT
            fc.id,
            fc.file_path,
            fc.file_name,
            fc.file_type,
            fc.master_datetime,
            fc.added_at,
            ed.latitude,
            ed.longitude
        FROM file_contents fc
        LEFT JOIN exif_data ed ON fc.id = ed.file_id
        -- Files the user removed from the catalog belong to no event, ever.
        WHERE fc.removed_at IS NULL{secondaries_clause}
        ORDER BY fc.id
    ''')
    all_rows = [dict(r) for r in cursor.fetchall()]

    # Filter out files already covered by a Manual event, and anything that is
    # not a photo or video (see is_media_file above).
    skipped_files = 0
    non_media_files = 0
    eligible_rows = []
    for row in all_rows:
        if not is_media_file(row['file_name']):
            non_media_files += 1
        elif row['id'] in manual_file_ids:
            skipped_files += 1
        else:
            eligible_rows.append(row)
    if non_media_files:
        print(f"[extract_events] Not photos or videos, left out of events: {non_media_files}")

    if not eligible_rows:
        empty_removed = remove_empty_events(conn)
        conn.close()
        print(f"[extract_events] Empty events removed: {empty_removed}")
        return {
            'files_processed': 0,
            'files_failed': 0,
            'events_created': 0,
            'files_without_timestamp': 0,
            'skipped_files': skipped_files,
            'empty_events_removed': empty_removed,
            'non_media_files': non_media_files,
        }

    # Attach parsed datetime to each eligible row
    dated = []
    undated = []
    for row in eligible_rows:
        dt = best_timestamp_for_file(row)
        if dt is not None:
            dated.append((dt, row))
        else:
            undated.append(row)

    # Sort by datetime ascending
    dated.sort(key=lambda x: x[0])

    total_files = len(eligible_rows)
    processed = 0
    failed = 0

    # Report the real total to the Workflow Manager so the progress bar
    # shows meaningful percentages even though no source directory is used.
    if progress_callback:
        progress_callback(0, total_files)

    # ------------------------------------------------------------------
    # 4. Group dated files into events (by time AND geo proximity)
    # ------------------------------------------------------------------
    groups, group_locs = group_files(dated)

    # ------------------------------------------------------------------
    # 5. Create events and event_files records
    # ------------------------------------------------------------------
    events_created = 0

    for idx, group in enumerate(groups, start=1):
        timestamps = [item[0] for item in group]
        start_dt = min(timestamps)
        end_dt = max(timestamps)

        # Representative location: lat/lon of the FIRST asset in the group
        # that carries GPS data (consistent with the grouping decision above).
        #
        # When no member carries GPS this stays (None, None) and location_source
        # stays NULL. It used to fall back to the catalog owner's home address,
        # which put an invented coordinate in the column that otherwise holds a
        # measurement, with nothing recording which of the two a row held - see
        # location-spec.md 4.8 and 9.1. "We do not know" is the honest answer and
        # the one every reader of this column can act on.
        event_lat, event_lon = group_locs[idx - 1]
        event_location_source = 'member' if event_lat is not None else None

        event_name = format_event_name(start_dt, idx)
        file_count = len(group)

        description = (
            f"Auto-generated event from {file_count} file(s) "
            f"between {start_dt.strftime('%Y-%m-%d %H:%M')} "
            f"and {end_dt.strftime('%Y-%m-%d %H:%M')}."
        )

        cursor.execute('''
            INSERT INTO events
                (name, start_time, end_time, latitude, longitude, description,
                 file_count, type, location_source)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
            event_name,
            start_dt.strftime('%Y-%m-%d %H:%M:%S'),
            end_dt.strftime('%Y-%m-%d %H:%M:%S'),
            event_lat,
            event_lon,
            description,
            file_count,
            'Auto',
            event_location_source,
        ))
        event_id = cursor.lastrowid
        events_created += 1

        for _, row in group:
            try:
                cursor.execute('''
                    INSERT OR IGNORE INTO event_files (event_id, file_id)
                    VALUES (?, ?)
                ''', (event_id, row['id']))
                processed += 1
            except Exception as e:
                failed += 1
                print(f"Error linking file {row['id']} to event {event_id}: {e}")

        # Commit before calling progress_callback to release the write lock.
        # The callback opens its own connection to the same DB; holding an
        # open write transaction here causes SQLite lock contention and hangs.
        #
        # Every COMMIT_EVERY groups rather than every group: one commit and one
        # progress connection per event was thousands of each on `family`.
        if idx % COMMIT_EVERY == 0 or idx == len(groups):
            conn.commit()
            if progress_callback:
                progress_callback(processed + len(undated), total_files)

    # ------------------------------------------------------------------
    # 6. Report undated files (no timestamp available)
    # ------------------------------------------------------------------
    files_without_timestamp = len(undated)
    if undated and progress_callback:
        processed += len(undated)
        progress_callback(processed, total_files)

    # ------------------------------------------------------------------
    # 7. Delete events with no photo or video left in them
    # ------------------------------------------------------------------
    empty_removed = remove_empty_events(conn)
    conn.close()

    print(f"[extract_events] Events created: {events_created}")
    print(f"[extract_events] Empty events removed: {empty_removed}")
    print(f"[extract_events] Files linked to events: {processed - failed - files_without_timestamp}")
    print(f"[extract_events] Files without timestamp: {files_without_timestamp}")
    if failed:
        print(f"[extract_events] Errors: {failed}")

    return {
        'files_processed': processed - failed,
        'files_failed': failed,
        'events_created': events_created,
        'files_without_timestamp': files_without_timestamp,
        'skipped_files': skipped_files,
        'empty_events_removed': empty_removed,
        'non_media_files': non_media_files,
    }

# ─── Standalone subprocess entry point ────────────────────────────────────────

if __name__ == '__main__':
    """
    When launched as a subprocess by the backend, arguments are:
        --execution-id  <int>   workflow_executions.id
        --target-dir    <path>  catalog target directory (contains workflow_results.db)
        --catalog-id    <int>   catalog id
    """
    import argparse

    parser = argparse.ArgumentParser(description='Extract events workflow (subprocess mode)')
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
        _db_complete(catalog_db_path, args.execution_id, 'completed',
                     skipped_files=result.get('skipped_files', 0) if isinstance(result, dict) else 0)
        print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)
