"""
Saved searches. documentation/architecture/search-spec.md section 7.

One thing with two halves (owner, 2026-10-19): its QUERY - the search's query
string, so it can be run again - and its CHOSEN FILES - photos and videos picked
from the results, a fixed set kept the way an Event keeps its photos, for
operations later (make it an album first; not built yet).

Temporary: idle for 30 days (nothing done to it) it is flagged, and at 60 days
idle it is deleted by the daily sweep - the saved search and its list, never
the photos or videos (owner, decision 6).

Shared within the catalog, like Events: everyone who can see the catalog sees
them; changing one needs editor access (the routes' rule, not this module's).
"""

import sqlite3
from datetime import datetime, timedelta, timezone

IDLE_DAYS = 30
DELETE_DAYS = 60
WARN_DAYS = 7                   # the Dashboard counts those due within this many days


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS saved_searches (
        id                INTEGER PRIMARY KEY AUTOINCREMENT,
        name              TEXT NOT NULL,
        note              TEXT,
        query             TEXT NOT NULL DEFAULT '',
        status            TEXT NOT NULL DEFAULT 'open',     -- open | done
        created_by        INTEGER,
        created_by_name   TEXT,
        created_at        TEXT NOT NULL,
        updated_at        TEXT NOT NULL,
        last_run_at       TEXT,
        last_activity_at  TEXT NOT NULL,
        last_operation    TEXT,
        last_operation_at TEXT)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS saved_search_files (
        saved_search_id   INTEGER NOT NULL,
        file_id           INTEGER NOT NULL,
        added_at          TEXT NOT NULL,
        added_by          INTEGER,
        added_from        TEXT,
        UNIQUE (saved_search_id, file_id))''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ssf_file ON saved_search_files (file_id)')
    conn.commit()


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt):
    return dt.isoformat()


def _parse(value):
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except (TypeError, ValueError):
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def lifecycle(row, now=None):
    """{idle, delete_on} for a saved search's row."""
    now = now or _now()
    last = _parse(row['last_activity_at']) or now
    return {'idle': now - last >= timedelta(days=IDLE_DAYS),
            'delete_on': _iso(last + timedelta(days=DELETE_DAYS))}


def touch(conn, sid):
    t = _iso(_now())
    conn.execute('UPDATE saved_searches SET last_activity_at = ?, updated_at = ? WHERE id = ?',
                 (t, t, sid))


def public(row, counts=None, now=None):
    d = dict(row)
    d.update(lifecycle(row, now))
    if counts:
        d.update(counts)
    return d


def counts(conn, sid):
    """How many photos and videos, the dates they span, a cover."""
    r = conn.execute(
        'SELECT COUNT(*), MIN(fc.master_datetime), MAX(fc.master_datetime), '
        'SUM(fc.removed_at IS NOT NULL) '
        'FROM saved_search_files ssf JOIN file_contents fc ON fc.id = ssf.file_id '
        'WHERE ssf.saved_search_id = ?', (sid,)).fetchone()
    cover = conn.execute(
        'SELECT ssf.file_id FROM saved_search_files ssf JOIN file_contents fc ON fc.id = ssf.file_id '
        'WHERE ssf.saved_search_id = ? AND fc.removed_at IS NULL '
        'ORDER BY ssf.added_at, ssf.rowid LIMIT 1', (sid,)).fetchone()
    return {'file_count': r[0] or 0, 'first_taken': r[1], 'last_taken': r[2],
            'removed_count': r[3] or 0, 'cover_file_id': cover[0] if cover else None}


def list_all(conn, status='open'):
    conn.row_factory = sqlite3.Row
    rows = conn.execute('SELECT * FROM saved_searches WHERE status = ? '
                        'ORDER BY COALESCE(last_run_at, updated_at) DESC, id DESC', (status,)).fetchall()
    return [public(r, counts(conn, r['id'])) for r in rows]


def get(conn, sid):
    conn.row_factory = sqlite3.Row
    return conn.execute('SELECT * FROM saved_searches WHERE id = ?', (sid,)).fetchone()


def create(conn, name, query, note=None, user=None):
    t = _iso(_now())
    cur = conn.execute(
        'INSERT INTO saved_searches (name, note, query, created_by, created_by_name, created_at, '
        'updated_at, last_activity_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
        (name, note, query or '', user['id'] if user else None,
         (user['display_name'] if user else None), t, t, t))
    conn.commit()
    return cur.lastrowid


def add_files(conn, sid, file_ids, user=None, added_from=None):
    """(added, already there). Only files that exist."""
    if not file_ids:
        return 0, 0
    before = conn.execute('SELECT COUNT(*) FROM saved_search_files WHERE saved_search_id = ?',
                          (sid,)).fetchone()[0]
    t = _iso(_now())
    conn.execute('CREATE TEMP TABLE IF NOT EXISTS ss_add (id INTEGER PRIMARY KEY)')
    conn.execute('DELETE FROM temp.ss_add')
    conn.executemany('INSERT OR IGNORE INTO temp.ss_add (id) VALUES (?)', ((int(i),) for i in file_ids))
    conn.execute(
        'INSERT OR IGNORE INTO saved_search_files (saved_search_id, file_id, added_at, added_by, added_from) '
        'SELECT ?, a.id, ?, ?, ? FROM temp.ss_add a JOIN file_contents fc ON fc.id = a.id',
        (sid, t, user['id'] if user else None, added_from))
    after = conn.execute('SELECT COUNT(*) FROM saved_search_files WHERE saved_search_id = ?',
                         (sid,)).fetchone()[0]
    asked = conn.execute('SELECT COUNT(*) FROM temp.ss_add a JOIN file_contents fc ON fc.id = a.id').fetchone()[0]
    touch(conn, sid)
    conn.commit()
    return after - before, asked - (after - before)


def remove_files(conn, sid, file_ids):
    marks = ','.join('?' * len(file_ids))
    cur = conn.execute(f'DELETE FROM saved_search_files WHERE saved_search_id = ? AND file_id IN ({marks})',
                       (sid, *file_ids))
    touch(conn, sid)
    conn.commit()
    return cur.rowcount


def files_page(conn, sid, limit=60, offset=0):
    rows = conn.execute(
        'SELECT ssf.file_id, ssf.added_at, fc.file_name, fc.file_type, fc.master_datetime, '
        'fc.removed_at FROM saved_search_files ssf JOIN file_contents fc ON fc.id = ssf.file_id '
        'WHERE ssf.saved_search_id = ? ORDER BY fc.master_datetime DESC, fc.id DESC LIMIT ? OFFSET ?',
        (sid, limit, offset)).fetchall()
    total = conn.execute('SELECT COUNT(*) FROM saved_search_files WHERE saved_search_id = ?',
                         (sid,)).fetchone()[0]
    return [dict(file_id=r[0], added_at=r[1], file_name=r[2], file_type=r[3], taken=r[4],
                 removed_at=r[5]) for r in rows], total


def delete(conn, sid):
    """The saved search and its list - never the files."""
    conn.execute('DELETE FROM saved_search_files WHERE saved_search_id = ?', (sid,))
    conn.execute('DELETE FROM saved_searches WHERE id = ?', (sid,))
    conn.commit()


def sweep(conn, now=None):
    """Delete saved searches idle for DELETE_DAYS. Returns their names."""
    now = now or _now()
    cutoff = _iso(now - timedelta(days=DELETE_DAYS))
    rows = conn.execute('SELECT id, name FROM saved_searches WHERE last_activity_at < ?',
                        (cutoff,)).fetchall()
    for sid, _ in rows:
        delete(conn, sid)
    return [r[1] for r in rows]


def due_soon(conn, now=None):
    """How many will be deleted within WARN_DAYS - the Dashboard's attention item."""
    now = now or _now()
    lo = _iso(now - timedelta(days=DELETE_DAYS))
    hi = _iso(now - timedelta(days=DELETE_DAYS - WARN_DAYS))
    try:
        return conn.execute('SELECT COUNT(*) FROM saved_searches WHERE last_activity_at >= ? '
                            'AND last_activity_at < ?', (lo, hi)).fetchone()[0]
    except sqlite3.OperationalError:
        return 0
