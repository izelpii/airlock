"""
The name index for Search. documentation/architecture/search-spec.md section 6.1.

Every name a file has been known by, in one trigram full-text index, so "any
part of a name, any case" is answered in milliseconds rather than by reading
1.4 million rows:

  stored     file_contents.file_name - the name in the catalog
  original   the path a file was imported from (file_operations move / copy /
             index) - the stored name often keeps nothing of it
  duplicate  the path of a copy an import turned away (duplicate_moved, or skip
             with "Duplicate hash:") and, when moved, where it went (DUPS/...) -
             a search hit that points to the catalog file it copies

A copy of the names, not a pointer: if import history were ever pruned, search
would still know every name a file had.

Kept current by CATCHING UP, not by triggers: an import writes up to ~1.4 M
file_operations rows, and a trigger would add an FTS insert to each, inside the
import's own transactions. search_state keeps how far each source has been
indexed (two rowid watermarks); after a workflow finishes, and on a search, the
rows past them are indexed. Until then a search also LIKEs that tail
(tail_clauses), so results are always complete.

Measured on homeserver's family (2026-10-19): 1,178,349 names, 24 s to build in
memory, ~310 MB, 1-50 ms a query.
"""

import sqlite3
import threading
import time

SCHEMA_VERSION = 1
BATCH = 20000
MIN_CHARS = 3                      # the trigram index's granularity

ORIGINAL_OPS = ('move', 'copy', 'index')


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS search_names (
        id       INTEGER PRIMARY KEY,
        file_id  INTEGER,
        kind     TEXT NOT NULL,          -- stored | original | duplicate
        op_id    INTEGER,                -- the file_operations row, for original / duplicate
        text     TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_search_names_file ON search_names (file_id)')
    conn.execute("CREATE VIRTUAL TABLE IF NOT EXISTS search_names_fts USING fts5("
                 "text, content='search_names', content_rowid='id', tokenize='trigram')")
    conn.execute('CREATE TABLE IF NOT EXISTS search_state (key TEXT PRIMARY KEY, value TEXT)')
    # Search by person reads face_detections by person - which had no index:
    # "photos of Ana" read all 481,018 faces on family. Narrow rows: seconds.
    conn.execute('CREATE INDEX IF NOT EXISTS idx_face_det_person '
                 'ON face_detections (person_id, file_id)')
    # Search's default order, newest first, undated last: an index on the very
    # expression it sorts by, so an unfiltered page reads 50 rows, not 373,020.
    # A file's history - its original path, the copies turned away as its
    # duplicates - by file: file_operations had no index on file_id, so each
    # results page and each hash search read all 1.37 M rows on family.
    conn.execute('CREATE INDEX IF NOT EXISTS idx_fo_file ON file_operations (file_id)')
    conn.execute("CREATE INDEX IF NOT EXISTS idx_fc_sort_date "
                 "ON file_contents (COALESCE(master_datetime, ''))")
    if _get(conn, 'schema_version') != str(SCHEMA_VERSION):
        conn.execute('DELETE FROM search_names')
        conn.execute("INSERT INTO search_names_fts(search_names_fts) VALUES ('delete-all')")
        _set(conn, 'fc_rowid', '0')
        _set(conn, 'fo_rowid', '0')
        _set(conn, 'schema_version', str(SCHEMA_VERSION))
    conn.commit()


def _get(conn, key):
    row = conn.execute('SELECT value FROM search_state WHERE key = ?', (key,)).fetchone()
    return row[0] if row else None


def _set(conn, key, value):
    conn.execute('INSERT INTO search_state (key, value) VALUES (?, ?) '
                 'ON CONFLICT(key) DO UPDATE SET value = excluded.value', (key, str(value)))


def watermarks(conn):
    return int(_get(conn, 'fc_rowid') or 0), int(_get(conn, 'fo_rowid') or 0)


def _duplicate_sql():
    return ("(fo.operation_type = 'duplicate_moved' OR "
            "(fo.operation_type = 'skip' AND fo.details LIKE 'Duplicate hash:%'))")


def behind(conn):
    """How many rows are not indexed yet (stored names + operations)."""
    fc, fo = watermarks(conn)
    a = conn.execute('SELECT COUNT(*) FROM file_contents WHERE rowid > ?', (fc,)).fetchone()[0]
    b = conn.execute('SELECT COUNT(*) FROM file_operations WHERE rowid > ?', (fo,)).fetchone()[0]
    return a + b


def _index_batch(conn):
    """Index one batch past the watermarks. Returns how many rows it consumed."""
    fc, fo = watermarks(conn)
    rows = conn.execute('SELECT rowid, id, file_name FROM file_contents WHERE rowid > ? '
                        'ORDER BY rowid LIMIT ?', (fc, BATCH)).fetchall()
    consumed = len(rows)
    for rowid, fid, name in rows:
        if name:
            cur = conn.execute("INSERT INTO search_names (file_id, kind, text) VALUES (?, 'stored', ?)",
                               (fid, name))
            conn.execute('INSERT INTO search_names_fts (rowid, text) VALUES (?, ?)', (cur.lastrowid, name))
        fc = rowid
    _set(conn, 'fc_rowid', fc)

    ops = conn.execute(
        'SELECT fo.rowid, fo.id, fo.file_id, fo.operation_type, fo.source_path, fo.target_path, '
        f'{_duplicate_sql()} AS dup FROM file_operations fo WHERE fo.rowid > ? '
        'ORDER BY fo.rowid LIMIT ?', (fo, BATCH)).fetchall()
    consumed += len(ops)
    for rowid, op_id, file_id, op_type, source, target, dup in ops:
        texts = []
        if dup:
            texts = [('duplicate', source), ('duplicate', target if op_type == 'duplicate_moved' else None)]
        elif op_type in ORIGINAL_OPS:
            texts = [('original', source)]
        for kind, text in texts:
            if text and file_id is not None:
                cur = conn.execute('INSERT INTO search_names (file_id, kind, op_id, text) '
                                   'VALUES (?, ?, ?, ?)', (file_id, kind, op_id, text))
                conn.execute('INSERT INTO search_names_fts (rowid, text) VALUES (?, ?)',
                             (cur.lastrowid, text))
        fo = rowid
    _set(conn, 'fo_rowid', fo)
    conn.commit()
    return consumed


def catch_up(db_path, max_seconds=None):
    """Index everything past the watermarks, batch by batch (each committed),
    so an import running beside it is never held for long."""
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        ensure_schema(conn)
        started = time.monotonic()
        total = 0
        while True:
            n = _index_batch(conn)
            total += n
            if n == 0 or (max_seconds and time.monotonic() - started > max_seconds):
                return total
    finally:
        conn.close()


_building = set()
_building_lock = threading.Lock()


def building(db_path):
    with _building_lock:
        return db_path in _building


def catch_up_in_background(db_path):
    """One catch-up per catalog at a time; never raises."""
    with _building_lock:
        if db_path in _building:
            return False
        _building.add(db_path)

    def run():
        try:
            n = catch_up(db_path)
            if n:
                print(f'[search] indexed {n} rows for {db_path}')
        except Exception as exc:
            print(f'[search] catch-up failed for {db_path}: {exc}')
        finally:
            with _building_lock:
                _building.discard(db_path)
    threading.Thread(target=run, name='search-index', daemon=True).start()
    return True


def rebuild(db_path):
    """Drop the index and build it again (POST /search/index)."""
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        ensure_schema(conn)
        conn.execute('DELETE FROM search_names')
        conn.execute("INSERT INTO search_names_fts(search_names_fts) VALUES ('delete-all')")
        _set(conn, 'fc_rowid', '0')
        _set(conn, 'fo_rowid', '0')
        conn.commit()
    finally:
        conn.close()
    return catch_up_in_background(db_path)


# ── matching names ───────────────────────────────────────────────────────────

def _like(text):
    # Unescaped: the trigram index serves a plain LIKE. A '%' or '_' typed in a
    # name then matches loosely, and name_hits checks every hit holds the text
    # exactly - so "IMG_4260" does not also return "IMG-4260".
    return '%' + text + '%'


def name_hits(conn, text, kinds=('stored', 'original', 'duplicate')):
    """
    Every (file_id, kind, op_id, matched text) whose name or path contains
    `text`, any case: the index, plus a LIKE over what it has not reached yet.
    Returns a list of dicts.
    """
    if len(text) < MIN_CHARS:
        raise ValueError(f'A name search needs at least {MIN_CHARS} characters')
    like = _like(text)
    lowered = text.lower()
    kind_marks = ','.join('?' * len(kinds))
    hits = [dict(file_id=r[0], kind=r[1], op_id=r[2], text=r[3]) for r in conn.execute(
        f"SELECT sn.file_id, sn.kind, sn.op_id, sn.text FROM search_names_fts f "
        f"JOIN search_names sn ON sn.id = f.rowid "
        f"WHERE f.text LIKE ? AND sn.kind IN ({kind_marks})",
        (like, *kinds)) if lowered in (r[3] or '').lower()]
    fc, fo = watermarks(conn)
    # The tail: what the index has not reached. Normally empty.
    if 'stored' in kinds:
        hits += [dict(file_id=r[0], kind='stored', op_id=None, text=r[1]) for r in conn.execute(
            'SELECT id, file_name FROM file_contents WHERE rowid > ? AND file_name LIKE ?',
            (fc, like)) if lowered in (r[1] or '').lower()]
    tail = conn.execute(
        'SELECT fo.id, fo.file_id, fo.operation_type, fo.source_path, fo.target_path, '
        f'{_duplicate_sql()} AS dup FROM file_operations fo WHERE fo.rowid > ? '
        'AND (fo.source_path LIKE ? OR fo.target_path LIKE ?)',
        (fo, like, like)).fetchall()
    for op_id, file_id, op_type, source, target, dup in tail:
        if file_id is None:
            continue
        kind = 'duplicate' if dup else 'original' if op_type in ORIGINAL_OPS else None
        if kind not in kinds:
            continue
        for t in (source, target if dup else None):
            if t and lowered in t.lower():
                hits.append(dict(file_id=file_id, kind=kind, op_id=op_id, text=t))
    return hits
