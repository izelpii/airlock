"""
The Dashboard: a catalog at a glance. documentation/specs/dashboard-specs.md.

Three tiers of freshness (spec section 4), so the page never waits on a
summary query:

  live         every request - each query here is milliseconds: what is
               running, what just ran, failed runs, alerts, disks, birthdays,
               albums.
  daily        On this day - a month-day across years cannot use the date
               index, so it is computed once per calendar day.
  cached       everything that scans the library: counts, years, coverage,
               top people, the attention counts that cost more than a few ms.
               One JSON value in catalog_properties, as content_report.py
               keeps its report.

A stale cache is served at once and rebuilt in the background; only a catalog
that has never been counted waits for the first build. Measured on homeserver's
`family` (370,751 files, 480,979 faces): the cached part is ~2 s of reads,
the request with a cache well under 50 ms.

Separate from app.py for the same reason content_report.py is: the queries are
the substance, and they can be read in one place and tested without a server.
"""

import json
import os
import sqlite3
import threading
from datetime import date, datetime, timedelta

from media_types import VIDEO_EXTENSIONS

STATS_KEY = 'dashboard_stats'
DAY_KEY = 'dashboard_on_this_day'
DIRTY_KEY = 'dashboard_dirty_at'

# Bumped when the stats change shape: a cached value of an older shape is
# discarded rather than drawn, because the page would show blanks as zeroes.
SCHEMA_VERSION = 1

# Twice a day, for what the fingerprint cannot see (a person renamed, a
# location confirmed, a file removed).
MAX_AGE_HOURS = float(os.environ.get('DASHBOARD_MAX_AGE_HOURS', '12') or 12)

ON_THIS_DAY_YEARS = 12
TOP_PEOPLE = 5
BIRTHDAY_DAYS = 14
RECENT_RUNS = 5

_VIDEO_EXTS = tuple(sorted(VIDEO_EXTENSIONS))


# ── small helpers ────────────────────────────────────────────────────────────

def _has(conn, table):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type IN ('table','view') "
                        "AND name = ?", (table,)).fetchone() is not None


def _cols(conn, table):
    return {r[1] for r in conn.execute(f'PRAGMA table_info({table})')}


def _one(conn, sql, args=(), default=0):
    try:
        row = conn.execute(sql, args).fetchone()
    except sqlite3.Error:
        return default
    return default if row is None or row[0] is None else row[0]


def _get_prop(conn, key):
    if not _has(conn, 'catalog_properties'):
        return None
    row = conn.execute('SELECT value FROM catalog_properties WHERE key = ?', (key,)).fetchone()
    return row[0] if row else None


def _set_prop(conn, key, value):
    conn.execute('CREATE TABLE IF NOT EXISTS catalog_properties '
                 '(key TEXT PRIMARY KEY, value TEXT)')
    conn.execute('INSERT OR REPLACE INTO catalog_properties (key, value) VALUES (?, ?)',
                 (key, value))
    conn.commit()


def _now():
    return datetime.now().replace(microsecond=0)


# ── the fingerprint ──────────────────────────────────────────────────────────

def fingerprint(conn):
    """
    What has to move before the counts are worth redoing.

    The highest ids, not COUNT(*): COUNT over file_contents is 0.3 s on
    `family`, which a fingerprint read on every request cannot afford; a MAX
    of a rowid is one b-tree descent. An import, a render and a face run each
    move one of these. What they cannot see - a file removed, a name changed -
    is what MAX_AGE_HOURS and the scheduler's dirty mark are for.
    """
    def top(table):
        return _one(conn, f'SELECT MAX(rowid) FROM {table}') if _has(conn, table) else 0
    return f"{SCHEMA_VERSION}:{top('file_contents')}:{top('alternative_versions')}:" \
           f"{top('face_detections')}"


# ── the cached stats ─────────────────────────────────────────────────────────

def build_stats(conn, metric='cosine'):
    """Everything that scans the library. ~2 s on `family`; never on a request
    that already has a cache."""
    stats = {'schema_version': SCHEMA_VERSION}

    # ONE pass over file_contents gives the library, its bytes, its years and
    # the face-scan coverage: grouped by year and extension, a few hundred rows.
    marks = ','.join('?' * len(_VIDEO_EXTS))
    years, photos, videos = {}, {'count': 0, 'bytes': 0}, {'count': 0, 'bytes': 0}
    scanned = 0
    # Added by Find Faces on its first run: a catalog never scanned lacks it.
    scanned_expr = ('SUM(face_detection_scanned_at IS NOT NULL)'
                    if 'face_detection_scanned_at' in _cols(conn, 'file_contents') else '0')
    for y, is_video, n, size, sc in conn.execute(f'''
            SELECT substr(master_datetime, 1, 4),
                   lower(file_type) IN ({marks}),
                   COUNT(*), SUM(file_size),
                   {scanned_expr}
              FROM file_contents
             WHERE removed_at IS NULL
             GROUP BY 1, 2''', _VIDEO_EXTS):
        bucket = videos if is_video else photos
        bucket['count'] += n
        bucket['bytes'] += size or 0
        if not is_video:
            scanned += sc or 0
        year = y if (y or '').isdigit() else 'undated'
        entry = years.setdefault(year, {'year': year, 'photos': 0, 'videos': 0})
        entry['videos' if is_video else 'photos'] += n
    stats['library'] = {'photos': photos, 'videos': videos}
    stats['years'] = sorted(years.values(), key=lambda e: (e['year'] == 'undated', e['year']))

    kinds = dict(conn.execute("SELECT COALESCE(kind, 'person'), COUNT(*) FROM people "
                              "GROUP BY 1").fetchall()) if 'kind' in _cols(conn, 'people') \
        else {'person': _one(conn, 'SELECT COUNT(*) FROM people')}
    stats['library']['people'] = kinds.get('person', 0)
    stats['library']['pets'] = kinds.get('pet', 0)
    stats['library']['events'] = _one(conn, 'SELECT COUNT(*) FROM events') \
        if _has(conn, 'events') else 0
    confirmed = proposed = 0
    if _has(conn, 'locations'):
        if 'confirmed' in _cols(conn, 'locations'):
            for c, n in conn.execute('SELECT COALESCE(confirmed, 1), COUNT(*) FROM locations '
                                     'GROUP BY 1'):
                if c:
                    confirmed += n
                else:
                    proposed += n
        else:
            confirmed = _one(conn, 'SELECT COUNT(*) FROM locations')
    stats['library']['locations'] = {'confirmed': confirmed, 'proposed': proposed}

    # Coverage: how far the automatic work has reached.
    faces_total = _one(conn, 'SELECT COUNT(*) FROM face_detections')
    faces_named = _one(conn, 'SELECT COUNT(*) FROM face_detections WHERE person_id IS NOT NULL')
    placed = _one(conn, 'SELECT COUNT(*) FROM file_locations WHERE location_id IS NOT NULL') \
        if _has(conn, 'file_locations') else 0
    previews = _one(conn, "SELECT COUNT(DISTINCT original_file_id) FROM alternative_versions "
                          "WHERE rung = '1200'") if _has(conn, 'alternative_versions') else 0
    live = photos['count'] + videos['count']
    stats['coverage'] = [
        {'key': 'placed', 'label': 'With a place', 'done': placed, 'of': live},
        {'key': 'scanned', 'label': 'Scanned for faces', 'done': scanned, 'of': photos['count']},
        {'key': 'previews', 'label': 'Previews made', 'done': min(previews, photos['count']),
         'of': photos['count']},
        {'key': 'named', 'label': 'Faces named', 'done': faces_named, 'of': faces_total},
    ]

    # Who appears most - one grouped pass (face_detections has no person_id
    # index; see v5.6.20).
    person_only = ("COALESCE(p.kind, 'person') = 'person'"
                   if 'kind' in _cols(conn, 'people') else '1')
    stats['top_people'] = [
        {'id': pid, 'name': name, 'photos': n} for pid, name, n in conn.execute(f'''
            SELECT p.id, p.name, t.n FROM (
                SELECT person_id, COUNT(DISTINCT file_id) AS n
                  FROM face_detections WHERE person_id IS NOT NULL
                 GROUP BY person_id) t
              JOIN people p ON p.id = t.person_id
             WHERE {person_only}
             ORDER BY t.n DESC LIMIT {TOP_PEOPLE}''')
    ]

    # The attention counts that cost more than a few milliseconds.
    attention = {}
    attention['faces_unidentified'] = _one(conn, '''
        SELECT COUNT(*) FROM face_detections fd JOIN file_contents fc ON fc.id = fd.file_id
         WHERE fd.source = 'Automatic' AND fd.person_id IS NULL AND fc.removed_at IS NULL''')
    attention['faces_suggested'] = _one(
        conn, 'SELECT COUNT(*) FROM face_best_match WHERE metric = ?', (metric,)) \
        if _has(conn, 'face_best_match') else 0
    attention['face_groups'] = _one(
        conn, 'SELECT COUNT(*) FROM face_clusters WHERE resolved_person_id IS NULL') \
        if _has(conn, 'face_clusters') else 0
    attention['pet_groups'] = _one(
        conn, 'SELECT COUNT(*) FROM pet_clusters WHERE resolved_person_id IS NULL') \
        if _has(conn, 'pet_clusters') and 'resolved_person_id' in _cols(conn, 'pet_clusters') \
        else 0
    attention['locations_proposed'] = proposed
    stats['attention'] = attention

    # What the catalog itself takes on disk.
    derivative_bytes = _one(conn, 'SELECT SUM(alternative_file_size) FROM alternative_versions') \
        if 'alternative_file_size' in _cols(conn, 'alternative_versions') else 0
    stats['catalog_bytes'] = {
        'originals': photos['bytes'] + videos['bytes'],
        'previews': derivative_bytes,
    }
    return stats


def mark_stale(conn):
    """Called when a workflow finishes: the next request rebuilds, so the
    numbers after an import or a face run are the new ones."""
    _set_prop(conn, DIRTY_KEY, _now().isoformat())


def _read_stats(conn):
    raw = _get_prop(conn, STATS_KEY)
    if not raw:
        return None
    try:
        cached = json.loads(raw)
    except ValueError:
        return None
    if cached.get('stats', {}).get('schema_version') != SCHEMA_VERSION:
        return None
    return cached


def _is_stale(conn, cached):
    if cached.get('fingerprint') != fingerprint(conn):
        return True
    computed = cached.get('computed_at') or ''
    dirty = _get_prop(conn, DIRTY_KEY) or ''
    if dirty and dirty > computed:
        return True
    try:
        age = _now() - datetime.fromisoformat(computed)
    except ValueError:
        return True
    return age > timedelta(hours=MAX_AGE_HOURS)


def rebuild(db_path, metric='cosine'):
    """Count and store. Its own connection: also run on a background thread."""
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        fp = fingerprint(conn)          # before: a change during the build re-marks it
        stats = build_stats(conn, metric)
        value = {'stats': stats, 'fingerprint': fp, 'computed_at': _now().isoformat()}
        _set_prop(conn, STATS_KEY, json.dumps(value))
        return value
    finally:
        conn.close()


_building = set()
_building_lock = threading.Lock()


def _rebuild_in_background(db_path, metric):
    with _building_lock:
        if db_path in _building:
            return
        _building.add(db_path)

    def run():
        try:
            rebuild(db_path, metric)
        except Exception as exc:                       # never kills the server
            print(f'[dashboard] rebuild failed for {db_path}: {exc}')
        finally:
            with _building_lock:
                _building.discard(db_path)
    threading.Thread(target=run, name='dashboard-rebuild', daemon=True).start()


def building(db_path):
    with _building_lock:
        return db_path in _building


def stats(conn, db_path, metric='cosine', background=True):
    """The cached stats: at once when there is a cache, stale or not."""
    cached = _read_stats(conn)
    if cached is None:
        return dict(rebuild(db_path, metric), stale=False)
    stale = _is_stale(conn, cached)
    if stale and background:
        _rebuild_in_background(db_path, metric)
    return dict(cached, stale=stale)


# ── daily: on this day ───────────────────────────────────────────────────────

def on_this_day(conn, today=None):
    """One photograph per earlier year taken on today's month and day, newest
    year first - computed once a day and kept with its date."""
    today = today or date.today()
    raw = _get_prop(conn, DAY_KEY)
    if raw:
        try:
            cached = json.loads(raw)
            if cached.get('date') == today.isoformat():
                return cached
        except ValueError:
            pass
    md = today.strftime('%m-%d')
    marks = ','.join('?' * len(_VIDEO_EXTS))
    rows = conn.execute(f'''
        SELECT substr(master_datetime, 1, 4) AS y, MIN(id), COUNT(*)
          FROM file_contents
         WHERE removed_at IS NULL
           AND substr(master_datetime, 6, 5) = ?
           AND lower(file_type) NOT IN ({marks})
           AND substr(master_datetime, 1, 4) < ?
         GROUP BY y ORDER BY y DESC LIMIT {ON_THIS_DAY_YEARS}''',
        (md, *_VIDEO_EXTS, str(today.year))).fetchall()
    value = {'date': today.isoformat(),
             'years': [{'year': y, 'file_id': fid, 'count': n} for y, fid, n in rows]}
    try:
        _set_prop(conn, DAY_KEY, json.dumps(value))
    except sqlite3.Error:
        pass                            # a busy database costs only tomorrow's speed
    return value


# ── live ─────────────────────────────────────────────────────────────────────

def _birthdays(conn, today):
    if 'date_of_birth' not in _cols(conn, 'people'):
        return []
    out = []
    kind = "COALESCE(kind, 'person') = 'person'" if 'kind' in _cols(conn, 'people') else '1'
    for pid, name, dob in conn.execute(
            f"SELECT id, name, date_of_birth FROM people WHERE {kind} "
            "AND date_of_birth IS NOT NULL AND date_of_birth != ''"):
        # Stored as YYYY-MM-DD, --MM-DD or YYYY; only the first two have a day.
        part = dob[5:] if len(dob) == 10 and dob[4] == '-' else (dob[2:] if dob.startswith('--') else None)
        if not part:
            continue
        try:
            m, d = int(part[:2]), int(part[3:5])
            when = date(today.year, m, d)
        except ValueError:
            continue
        if when < today:
            try:
                when = date(today.year + 1, m, d)
            except ValueError:
                continue
        days = (when - today).days
        if days <= BIRTHDAY_DAYS:
            turning = None
            if len(dob) == 10 and dob[:4].isdigit():
                turning = when.year - int(dob[:4])
            out.append({'id': pid, 'name': name, 'date': when.isoformat(),
                        'in_days': days, 'turning': turning})
    return sorted(out, key=lambda b: b['in_days'])


def live(conn, today=None):
    """Everything read on every request. Each query is milliseconds."""
    today = today or date.today()
    out = {}
    q = conn.execute
    exec_cols = _cols(conn, 'workflow_executions') if _has(conn, 'workflow_executions') else set()

    def runs(sql, args=()):
        names = ['id', 'workflow_name', 'status', 'started_at', 'completed_at',
                 'processed_files', 'total_files']
        return [dict(zip(names, r)) for r in q(sql, args)]

    activity = {'running': [], 'queued': 0, 'next': None, 'recent': [], 'last_import': None}
    if exec_cols:
        base = ('SELECT id, workflow_name, status, started_at, completed_at, '
                'processed_files, total_files FROM workflow_executions')
        activity['running'] = runs(base + " WHERE status = 'running' ORDER BY id")
        activity['recent'] = runs(base + " WHERE status != 'running' ORDER BY id DESC "
                                  f"LIMIT {RECENT_RUNS}")
        out['failed_7d'] = _one(conn, "SELECT COUNT(*) FROM workflow_executions "
                                      "WHERE status IN ('failed', 'interrupted') "
                                      "AND started_at > datetime('now', '-7 days')")
        if _has(conn, 'import_phases') and {'new_files', 'duplicate_files'} <= exec_cols:
            row = q('''SELECT e.id, e.workflow_name, e.status, e.started_at, e.completed_at,
                              e.total_files, e.new_files, e.duplicate_files
                         FROM workflow_executions e
                        WHERE e.id = (SELECT MAX(execution_id) FROM import_phases)''').fetchone()
            if row:
                activity['last_import'] = dict(zip(
                    ['id', 'workflow_name', 'status', 'started_at', 'completed_at',
                     'total_files', 'new_files', 'duplicate_files'], row))
    else:
        out['failed_7d'] = 0
    if _has(conn, 'workflow_queue'):
        activity['queued'] = _one(conn, "SELECT COUNT(*) FROM workflow_queue "
                                        "WHERE status = 'queued'")
        nxt = q("SELECT workflow_name FROM workflow_queue WHERE status = 'queued' "
                "ORDER BY priority DESC, position, id LIMIT 1").fetchone()
        activity['next'] = nxt[0] if nxt else None
    out['activity'] = activity

    out['birthdays'] = _birthdays(conn, today)

    albums = {'albums': 0, 'deployments': 0, 'latest': None}
    if _has(conn, 'albums'):
        albums['albums'] = _one(conn, 'SELECT COUNT(*) FROM albums')
    if _has(conn, 'album_deployments'):
        albums['deployments'] = _one(conn, 'SELECT COUNT(*) FROM album_deployments')
        row = q('SELECT name, slug, updated_at FROM album_deployments WHERE enabled = 1 '
                'ORDER BY updated_at DESC, id DESC LIMIT 1').fetchone()
        if row:
            albums['latest'] = {'name': row[0], 'slug': row[1], 'updated_at': row[2]}
    out['albums'] = albums
    return out


def storage(catalog, routes):
    """Every drive the catalog uses, deduplicated by device - always live:
    shutil.disk_usage costs microseconds."""
    import storage_alerts
    extra = [(f'storage route "{r.get("name")}"', r.get('container_path'))
             for r in routes if r.get('container_path') and os.path.isdir(r['container_path'])]
    drives = []
    for e in storage_alerts.measure([catalog], extra=extra):
        parts = []
        for who in e.get('users', {}).values():
            parts.extend(who.get('parts', []))
        drives.append({'mount': e['mount'], 'total': e['total'], 'used': e['used'],
                       'free': e['free'], 'percent': e['percent'],
                       'severity': e['severity'], 'parts': parts})
    return sorted(drives, key=lambda d: d['mount'])


def database_bytes(db_path):
    total = 0
    for suffix in ('', '-wal', '-shm'):
        try:
            total += os.path.getsize(db_path + suffix)
        except OSError:
            pass
    return total
