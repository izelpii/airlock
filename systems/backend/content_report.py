"""
The Content report: what is in a catalog, and what is missing from it.

documentation/specs/contents-spec.md. One pass per table, cached in
catalog_properties, and NOTHING here reads exif_data.raw_exif_json - it averages
22 KB a row and 7.2 GB across a 361 K-file catalog, so a report that parsed it
would cost more than the import that wrote it (spec section 4).

Separate module rather than more of app.py, which is already twelve thousand
lines: the queries are the substance of the report and they are worth being able
to read in one place and test without a web server.
"""
import json
import os
import sqlite3
import time
from datetime import datetime

from media_types import VIDEO_EXTENSIONS, RAW_EXTENSIONS

CACHE_KEY = 'content_report'

# Bumped when a section changes shape. A cached report from an older version is
# discarded rather than rendered, because the page would otherwise read fields
# that are not there and show blanks that look like zeroes.
# 2: added 'places'.
# 3: renamed it to 'locations' - the application had been using "place" and
#    "location" interchangeably for one concept, and only one of them is a
#    word the UI uses.
SCHEMA_VERSION = 3

# Which rungs are stills and which are video, because their DENOMINATORS differ
# and conflating them is the easiest way to make this report lie. Measured on
# family (2026-10-06): 302,731 stills and 58,362 videos of 361,093 live files, so
# comparing a still rung against every live file overstates its gap eightfold.
VIDEO_RUNG_PREFIX = 'video_'


def _rows(conn, sql, args=()):
    try:
        return conn.execute(sql, args).fetchall()
    except sqlite3.Error:
        # A catalog whose schema predates a table reports that section as
        # absent rather than taking the whole report down with it.
        return []


def _one(conn, sql, args=(), default=0):
    rows = _rows(conn, sql, args)
    return rows[0][0] if rows and rows[0][0] is not None else default


def _classify(by_ext):
    """
    Live counts split into stills and videos, from the extension.

    By extension and not by a stored kind, because media_types is already the
    one place that decides what a file is - and emphatically NOT by
    duration_seconds, which is only set once a video has been probed. On the
    measured catalog that column is populated for 31,352 of 58,362 videos, so
    using it as the video count would hide 27,010 unprocessed videos by
    excluding them from their own denominator.
    """
    live = sum(by_ext.values())
    videos = sum(n for e, n in by_ext.items() if e in VIDEO_EXTENSIONS)
    raw = sum(n for e, n in by_ext.items() if e in RAW_EXTENSIONS)
    return {'live': live, 'videos': videos, 'stills': live - videos, 'raw': raw}


def _totals(conn, split):
    first, last = (_rows(conn, 'SELECT MIN(master_datetime), MAX(master_datetime) '
                               'FROM file_contents WHERE removed_at IS NULL '
                               'AND master_datetime IS NOT NULL') or [(None, None)])[0]
    return {
        'live_files': split['live'],
        'stills': split['stills'],
        'videos': split['videos'],
        'raw': split['raw'],
        'removed_files': _one(conn, 'SELECT COUNT(*) FROM file_contents '
                                    'WHERE removed_at IS NOT NULL'),
        'total_bytes': _one(conn, 'SELECT COALESCE(SUM(file_size), 0) FROM file_contents '
                                  'WHERE removed_at IS NULL'),
        'earliest': first,
        'latest': last,
    }


def _provenance(conn):
    """
    Where each file's authoritative date came from (master-date-spec.md).

    The time chart is built on master_datetime, so this is the chart's own
    margin of error: on the measured catalog 33,112 files - 9% - are placed on
    the time axis from a directory or file name rather than a camera timestamp.
    """
    out = [{'source': r[0] or 'unknown', 'files': r[1]} for r in _rows(
        conn, 'SELECT master_datetime_source, COUNT(*) FROM file_contents '
              'WHERE removed_at IS NULL GROUP BY 1 ORDER BY 2 DESC')]
    inferred = sum(r['files'] for r in out
                   if r['source'] in ('directory', 'filename', 'filesystem', 'system'))
    return {'sources': out, 'inferred': inferred}


def _locations(conn):
    """
    Where each file's LOCATION came from, beside where its date came from.

    The same shape of question as _provenance and the same reason for asking:
    the map is only as trustworthy as the weakest source feeding it. On the
    measured catalog 158,088 files carry a coordinate the camera recorded and
    195,194 sit in an event with none of their own, so how many of those got a
    place by inference - and by which kind - is the number that says how much
    of the map is evidence. See location-spec.md 8.4.

    Returns zeros rather than failing on a catalog that predates the feature:
    this is one card on a report, not a reason for the page not to load.
    """
    out = {'measured': 0, 'cluster': 0, 'event_tagged': 0, 'event_voted': 0,
           'manual': 0, 'with_location': 0, 'without_location': 0,
           'clusters_proposed': 0,
           'available': False}
    # Asked of sqlite_master rather than inferred from a failing query: _rows
    # swallows sqlite3.Error by design, so every count would come back 0 on a
    # catalog with no file_locations and the card would read "nothing placed"
    # where the honest answer is "this catalog has not been resolved yet".
    if not _rows(conn, "SELECT 1 FROM sqlite_master WHERE type='table' "
                       "AND name='file_locations'"):
        return out
    out['available'] = True
    try:
        out['measured'] = _one(
            conn, "SELECT COUNT(*) FROM file_locations WHERE coord_source = 'exif'")
        for source, share_is_null, n in _rows(
                conn, 'SELECT location_source, location_share IS NULL, COUNT(*) '
                      'FROM file_locations GROUP BY 1, 2'):
            if source == 'event':
                out['event_tagged' if share_is_null else 'event_voted'] += n
            elif source in ('cluster', 'manual'):
                out[source] += n
        out['with_location'] = (out['cluster'] + out['event_tagged']
                                + out['event_voted'] + out['manual'])
        # Live files with no resolved location at all - the work still to do.
        out['without_location'] = max(0, _one(
            conn, 'SELECT COUNT(*) FROM file_contents WHERE removed_at IS NULL')
            - out['with_location'])
        out['clusters_proposed'] = _one(
            conn, "SELECT COUNT(*) FROM location_clusters WHERE status = 'proposed'")
    except sqlite3.Error:
        pass
    return out


def _formats(conn, by_ext_bytes):
    return [{'ext': ext or 'unknown', 'files': files, 'bytes': size,
             'kind': ('video' if ext in VIDEO_EXTENSIONS
                      else 'raw' if ext in RAW_EXTENSIONS else 'still')}
            for ext, (files, size) in sorted(
                by_ext_bytes.items(), key=lambda kv: -kv[1][0])]


def _rungs(conn, split):
    """
    Derivative coverage: what the gallery can actually show and a deployment can
    actually pack (file-sizes-spec.md section 9.2).

    COUNT(DISTINCT original_file_id), not COUNT(*): a file can hold more than one
    row for the same rung, and on the measured catalog the raw row count reports
    288,872 files at the 1200 rung where 293,408 distinct files have one.
    """
    rows = _rows(conn, 'SELECT rung, COUNT(DISTINCT original_file_id) '
                       'FROM alternative_versions WHERE rung IS NOT NULL GROUP BY 1')
    out = []
    for rung, files in sorted(rows, key=lambda r: -r[1]):
        is_video = str(rung).startswith(VIDEO_RUNG_PREFIX)
        denominator = split['videos'] if is_video else split['stills']
        out.append({'rung': rung, 'kind': 'video' if is_video else 'still',
                    'files': files, 'denominator': denominator,
                    'missing': max(0, denominator - files)})
    return out


def _groups(conn, split):
    """Files that are one capture: Live Photos and RAW+JPEG (file-unity-spec.md)."""
    rows = [{'kind': r[0], 'match_rule': r[1] or 'unknown', 'groups': r[2]}
            for r in _rows(conn, 'SELECT kind, match_rule, COUNT(*) FROM image_groups '
                                 'GROUP BY 1, 2 ORDER BY 3 DESC')]
    return {
        'rules': rows,
        # The match rule is reported beside the count because the rules differ in
        # strength: serial+shuttercount is a strong identity claim and
        # subsec_datetime is an inference.
        'files_grouped': _one(conn, 'SELECT COUNT(*) FROM file_contents '
                                    'WHERE image_group_id IS NOT NULL AND removed_at IS NULL'),
        'files_ungrouped': max(0, split['live'] - _one(
            conn, 'SELECT COUNT(*) FROM file_contents '
                  'WHERE image_group_id IS NOT NULL AND removed_at IS NULL')),
    }


def _coverage(conn, split):
    """
    Each line is a pair: how much is covered, and how much is not. The second
    number is the point of the section.

    `used` distinguishes "nothing here yet" from "nothing worked". On the
    measured catalog albums, people, locations and tags are all empty, so an
    unused feature is the common case and must not render as a failure
    (spec section 5.7).
    """
    live = split['live']
    events_files = _one(conn, 'SELECT COUNT(DISTINCT file_id) FROM event_files')
    geo = _one(conn, 'SELECT COUNT(*) FROM exif_data WHERE latitude IS NOT NULL')
    scanned = _one(conn, 'SELECT COUNT(*) FROM file_contents '
                         'WHERE face_detection_scanned_at IS NOT NULL AND removed_at IS NULL')
    detections = _one(conn, 'SELECT COUNT(*) FROM face_detections')
    attributed = _one(conn, 'SELECT COUNT(*) FROM face_detections WHERE person_id IS NOT NULL')
    people = _one(conn, 'SELECT COUNT(*) FROM people')
    albums = _one(conn, 'SELECT COUNT(*) FROM albums')
    album_files = _one(conn, 'SELECT COUNT(DISTINCT file_id) FROM section_photos')
    videos_probed = _one(conn, 'SELECT COUNT(*) FROM file_contents '
                               'WHERE duration_seconds IS NOT NULL AND removed_at IS NULL')

    events = _one(conn, 'SELECT COUNT(*) FROM events')
    return [
        {'what': 'In an event', 'covered': events_files, 'of': live,
         'used': events > 0,
         'detail': '%s event(s)' % format(events, ',')},
        {'what': 'Has coordinates', 'covered': geo, 'of': live, 'used': geo > 0,
         'detail': 'from EXIF'},
        {'what': 'Face-detection scanned', 'covered': scanned, 'of': live,
         'used': scanned > 0,
         'detail': '%s face(s) found' % format(detections, ',')},
        {'what': 'Faces attributed to a person', 'covered': attributed, 'of': detections,
         'used': people > 0,
         'detail': '%s person(s) named' % format(people, ',')},
        {'what': 'Videos probed for duration', 'covered': videos_probed,
         'of': split['videos'], 'used': split['videos'] > 0,
         'detail': 'a video with no duration has had no preview made'},
        {'what': 'In an album', 'covered': album_files, 'of': live, 'used': albums > 0,
         'detail': '%s album(s)' % format(albums, ',')},
    ]


def _ingestion(conn):
    """
    From workflow_executions and import_phases ONLY.

    Never from import_items: retention prunes those rows 30 days after a
    completed run and 7 after an abandoned one (parallelism.md section 4.1), so
    a history built on them silently shortens as it ages. The counts below are
    the ones each run recorded when it ended.
    """
    try:
        ids = [r[0] for r in conn.execute('SELECT id, python_script FROM workflows')
               if os.path.basename(r[1] or '') in
               ('io_picture_import_workflow.py', 'io_picture_import_v2_workflow.py')]
    except sqlite3.Error:
        ids = []
    runs = []
    if ids:
        marks = ','.join('?' * len(ids))
        runs = [{'id': r[0], 'status': r[1], 'started_at': r[2], 'completed_at': r[3],
                 'new_files': r[4], 'duplicate_files': r[5], 'ignored_files': r[6],
                 'total_files': r[7],
                 'recorded': r[4] is not None}
                for r in _rows(conn,
                               f'SELECT id, status, started_at, completed_at, new_files, '
                               f'duplicate_files, ignored_files, total_files '
                               f'FROM workflow_executions WHERE workflow_id IN ({marks}) '
                               f'ORDER BY started_at DESC, id DESC LIMIT 12', ids)]
    operations = [{'operation': '%s / %s' % (r[0], r[1] or 'unknown'), 'files': r[2]}
                  for r in _rows(conn, 'SELECT operation_type, status, COUNT(*) '
                                       'FROM file_operations GROUP BY 1, 2 '
                                       'ORDER BY 3 DESC LIMIT 10')]
    return {'runs': runs, 'operations': operations,
            'failed_operations': _one(conn, "SELECT COUNT(*) FROM file_operations "
                                            "WHERE status = 'failed'")}


def _removed(conn):
    """Soft-removed, failed to place, and exact duplicates."""
    duplicate_groups = _one(conn, 'SELECT COUNT(*) FROM (SELECT sha256_hash '
                                  'FROM file_contents WHERE removed_at IS NULL '
                                  'AND sha256_hash IS NOT NULL GROUP BY sha256_hash '
                                  'HAVING COUNT(*) > 1)')
    return {
        'removed_files': _one(conn, 'SELECT COUNT(*) FROM file_contents '
                                    'WHERE removed_at IS NOT NULL'),
        'duplicate_groups': duplicate_groups,
        # Near-duplicates are deliberately absent rather than zero:
        # perceptual_hash is populated on 0 of 361,093 files, so there is no
        # source for the number (spec section 7).
        'perceptual_hashed': _one(conn, 'SELECT COUNT(*) FROM file_contents '
                                        'WHERE perceptual_hash IS NOT NULL'),
    }


def _fingerprint(conn):
    """
    What has to change before the report is worth recomputing.

    The live file count alone is not enough: rendering derivatives for an
    existing library changes the most actionable section in the report
    (section 5.5) without adding a file. These three counts are indexed, cost
    milliseconds, and move whenever anything the report shows has moved.
    """
    return '%s:%s:%s:%s' % (
        SCHEMA_VERSION,
        _one(conn, 'SELECT COUNT(*) FROM file_contents WHERE removed_at IS NULL'),
        _one(conn, 'SELECT COUNT(*) FROM alternative_versions'),
        _one(conn, 'SELECT COUNT(*) FROM face_detections'))


def build(conn):
    """Every section, from one connection. No caching, no Flask."""
    started = time.monotonic()
    by_ext_bytes = {(r[0] or '').lower(): (r[1], r[2] or 0) for r in _rows(
        conn, 'SELECT file_type, COUNT(*), SUM(file_size) FROM file_contents '
              'WHERE removed_at IS NULL GROUP BY 1')}
    by_ext = {e: v[0] for e, v in by_ext_bytes.items()}
    split = _classify(by_ext)
    return {
        'schema_version': SCHEMA_VERSION,
        'generated_at': datetime.now().isoformat(timespec='seconds'),
        'fingerprint': _fingerprint(conn),
        'totals': _totals(conn, split),
        'provenance': _provenance(conn),
        'locations': _locations(conn),
        'formats': _formats(conn, by_ext_bytes),
        'rungs': _rungs(conn, split),
        'groups': _groups(conn, split),
        'coverage': _coverage(conn, split),
        'ingestion': _ingestion(conn),
        'removed': _removed(conn),
        'built_in_seconds': round(time.monotonic() - started, 2),
    }


def report(catalog_db, refresh=False):
    """
    The report, from cache when the catalog has not moved.

    Cached in catalog_properties - the table that already exists for exactly
    this kind of derived value - keyed on a fingerprint rather than on age. A
    library that has not changed does not need recounting, however long ago it
    was counted; one that has changed needs it immediately.
    """
    conn = catalog_db.get_connection()
    try:
        conn.execute('PRAGMA query_only=1')
        current = _fingerprint(conn)
        if not refresh:
            cached = _read_cache(catalog_db)
            if cached and cached.get('fingerprint') == current:
                cached['from_cache'] = True
                return cached
        fresh = build(conn)
    finally:
        conn.close()
    fresh['from_cache'] = False
    _write_cache(catalog_db, fresh)
    return fresh


def _read_cache(catalog_db):
    try:
        conn = catalog_db.get_connection()
        try:
            row = conn.execute('SELECT value FROM catalog_properties WHERE key = ?',
                               (CACHE_KEY,)).fetchone()
        finally:
            conn.close()
        if not row:
            return None
        data = json.loads(row[0])
        return data if data.get('schema_version') == SCHEMA_VERSION else None
    except (sqlite3.Error, ValueError, TypeError):
        return None


def _write_cache(catalog_db, data):
    # Never fatal: a report that cannot be cached is still a report, and a
    # read-only or locked catalog must not turn a page into an error.
    try:
        conn = catalog_db.get_connection()
        try:
            conn.execute('CREATE TABLE IF NOT EXISTS catalog_properties '
                         '(key TEXT PRIMARY KEY, value TEXT)')
            conn.execute('INSERT INTO catalog_properties (key, value) VALUES (?, ?) '
                         'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                         (CACHE_KEY, json.dumps(data)))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error:
        pass
