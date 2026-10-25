"""
Each waiting face's best match, kept in a table instead of worked out per request.

Identify people shows, per person, the unidentified faces whose closest person
is them. Worked out on every request, that meant grouping all of
face_similarity_scores - 12.5 million rows on `family` by 2026-10-11, every
face scored against every person - for 11 s per summary and 13 s per page.
The paging (v5.4.20) was right; the grouping underneath it had outgrown it.

So the answer is stored, in `face_best_match`: one row per waiting face per
metric, holding the person, the distance and the score row. It holds ONLY
faces still waiting for somebody - Automatic, unnamed, photo not removed - so
the summary is a scan of one index, with no joins.

Keeping it current without trusting every writer
------------------------------------------------
Scores, faces, people and photos are changed from a dozen places: two scoring
workflows, Find Faces, the auto-confirm in Re-rank, a reject, a delete, a
person's removal. Rather than each remembering to update this table, triggers
note the affected face in `face_best_match_dirty`, and drain() recomputes only
those. A face's best match depends on exactly four things, and each has its
trigger:

    its score rows        face_similarity_scores insert / delete / update
    whether it waits      face_detections person_id or source changes, delete
    its photo             file_contents.removed_at changes
    the people            a person deleted (their faces pass to the next best)

A full rebuild is needed once, when the table is new, and is marked by a
sentinel row (FULL_REBUILD). It also happens when so many faces are dirty -
after a scoring run - that walking every face in ranges is cheaper than one
at a time.

Who drains
----------
A request drains inline when little is pending (INLINE_MAX). With more, it
starts one background thread and answers from the table as it stands, saying
so (`stale`), and the page polls. While a scoring workflow is running the
backlog is left alone: the workflow is about to dirty the same faces again.
"""

import logging
import sqlite3
import threading
import time

_LOG = logging.getLogger(__name__)

# metric -> score column. Mirrors app._METRIC_COLUMN; a test keeps them equal.
METRICS = {'cosine': 'distance', 'contextual': 'contextual_distance'}

FULL_REBUILD = -1     # sentinel face_id in the dirty table: recompute everything
INLINE_MAX = 5000     # a request drains this many faces itself (~0.3 s)
FULL_AT = 50000       # past this many dirty faces, walk all faces in ranges
BATCH = 2000          # faces per transaction when draining one at a time
RANGE = 20000         # face ids per transaction in a full rebuild

# Scripts that rewrite scores wholesale. Draining while one runs is wasted work.
SCORING_SCRIPTS = ('ai_face_recognition_workflow.py',
                   'ai_contextual_recognition_workflow.py')

_METRIC_LIST = ', '.join(f"'{m}'" for m in METRICS)


def _table_exists(cur, name):
    return cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                       (name,)).fetchone() is not None


def ensure_best_match_schema(cur):
    """
    Create the table, its index, the dirty list and the triggers. Idempotent.
    Called from ensure_face_recognition_schema, after face_similarity_scores.

    Returns a list of the changes made.
    """
    changed = []
    new = not _table_exists(cur, 'face_best_match')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS face_best_match (
            metric    TEXT    NOT NULL,
            face_id   INTEGER NOT NULL,
            person_id INTEGER NOT NULL,
            d         REAL    NOT NULL,
            score_id  INTEGER NOT NULL,
            PRIMARY KEY (metric, face_id)
        ) WITHOUT ROWID''')
    # The summary scans (metric, person_id); a page reads one person's slice in
    # distance order and stops at LIMIT.
    cur.execute('CREATE INDEX IF NOT EXISTS idx_fbm_person '
                'ON face_best_match (metric, person_id, d, face_id)')
    cur.execute('CREATE TABLE IF NOT EXISTS face_best_match_dirty '
                '(face_id INTEGER PRIMARY KEY)')
    # A new table over existing scores needs one full build. Over none - a new
    # catalog - it is already correct, and the triggers take it from here.
    if new and cur.execute('SELECT 1 FROM face_similarity_scores LIMIT 1').fetchone():
        cur.execute('INSERT OR IGNORE INTO face_best_match_dirty VALUES (?)',
                    (FULL_REBUILD,))
        changed.append('face_best_match created (full build pending)')
    elif new:
        changed.append('face_best_match created')

    mark = 'INSERT OR IGNORE INTO face_best_match_dirty (face_id) VALUES'
    triggers = {
        'face_similarity_scores': [
            ('trg_fbm_fss_ins', 'AFTER INSERT', '',
             f'{mark} (NEW.query_face_detection_id);'),
            ('trg_fbm_fss_del', 'AFTER DELETE', '',
             f'{mark} (OLD.query_face_detection_id);'),
            # Only the columns a best match reads. Re-rank also rewrites
            # contextual_assigned and Match by Appearance euclidean_distance on
            # every row; neither should dirty anything.
            ('trg_fbm_fss_upd',
             'AFTER UPDATE OF distance, contextual_distance, person_id, '
             'query_face_detection_id', '',
             f'{mark} (NEW.query_face_detection_id); '
             f'{mark} (OLD.query_face_detection_id);'),
        ],
        'face_detections': [
            ('trg_fbm_fd_upd', 'AFTER UPDATE OF person_id, source',
             'WHEN OLD.person_id IS NOT NEW.person_id OR OLD.source IS NOT NEW.source',
             f'{mark} (NEW.id);'),
            ('trg_fbm_fd_del', 'AFTER DELETE', '', f'{mark} (OLD.id);'),
        ],
        'file_contents': [
            ('trg_fbm_fc_removed', 'AFTER UPDATE OF removed_at',
             'WHEN OLD.removed_at IS NOT NEW.removed_at',
             'INSERT OR IGNORE INTO face_best_match_dirty (face_id) '
             'SELECT id FROM face_detections WHERE file_id = NEW.id;'),
        ],
        'people': [
            ('trg_fbm_people_del', 'AFTER DELETE', '',
             'INSERT OR IGNORE INTO face_best_match_dirty (face_id) '
             f'SELECT face_id FROM face_best_match WHERE metric IN ({_METRIC_LIST}) '
             'AND person_id = OLD.id;'),
        ],
    }
    for table, defs in triggers.items():
        # A catalog opened by a path that has not created this table yet gets
        # its trigger on the next open, when it has.
        if not _table_exists(cur, table):
            continue
        for name, event, when, body in defs:
            if cur.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                           (name,)).fetchone():
                continue
            cur.execute(f'CREATE TRIGGER {name} {event} ON {table} {when} '
                        f'BEGIN {body} END')
            changed.append(f'trigger {name} created')
    return changed


# ── Recomputing ──────────────────────────────────────────────────────────────

def _insert_sql(metric, faces):
    """
    Best rows for the faces `faces` selects (a SQL condition on face ids, with
    `{f}` standing for the face id column), waiting faces only.

    SQLite returns a GROUP BY's bare columns from the row holding MIN(), so
    person_id and score_id are the best row's own - the same query the
    endpoints ran per request before this table existed.
    """
    col = METRICS[metric]
    return f'''
        INSERT INTO face_best_match (metric, face_id, person_id, d, score_id)
        SELECT '{metric}', b.face_id, b.person_id, b.d, b.score_id
          FROM (SELECT fss.query_face_detection_id AS face_id, fss.person_id,
                       MIN(fss.{col}) AS d, fss.id AS score_id
                  FROM face_similarity_scores fss
                  JOIN people p ON p.id = fss.person_id
                 WHERE fss.{col} IS NOT NULL
                   AND {faces.replace('{f}', 'fss.query_face_detection_id')}
                 GROUP BY fss.query_face_detection_id) b
          JOIN face_detections fd ON fd.id = b.face_id
                                 AND fd.source = 'Automatic' AND fd.person_id IS NULL
          JOIN file_contents fc ON fc.id = fd.file_id AND fc.removed_at IS NULL'''


def _recompute(conn, faces, params):
    """Replace the rows for the selected faces. Caller holds the transaction."""
    conn.execute(f'DELETE FROM face_best_match WHERE metric IN ({_METRIC_LIST}) '
                 f'AND {faces.replace("{f}", "face_id")}', params)
    for metric in METRICS:
        conn.execute(_insert_sql(metric, faces), params)


def pending(conn):
    """(dirty faces, whether a full rebuild is marked)."""
    n = conn.execute('SELECT COUNT(*) FROM face_best_match_dirty').fetchone()[0]
    full = conn.execute('SELECT 1 FROM face_best_match_dirty WHERE face_id = ?',
                        (FULL_REBUILD,)).fetchone() is not None
    return n, full


def _drain_batch(conn):
    """Recompute up to BATCH dirty faces in one transaction. Returns how many."""
    conn.execute('BEGIN IMMEDIATE')
    try:
        row = conn.execute('SELECT face_id FROM face_best_match_dirty WHERE face_id > 0 '
                           'ORDER BY face_id LIMIT 1 OFFSET ?', (BATCH - 1,)).fetchone()
        hi = row[0] if row else conn.execute(
            'SELECT MAX(face_id) FROM face_best_match_dirty').fetchone()[0]
        if hi is None or hi <= 0:
            conn.commit()
            return 0
        sel = ('{f} IN (SELECT face_id FROM face_best_match_dirty '
               'WHERE face_id BETWEEN 1 AND ?)')
        n = conn.execute('SELECT COUNT(*) FROM face_best_match_dirty '
                         'WHERE face_id BETWEEN 1 AND ?', (hi,)).fetchone()[0]
        _recompute(conn, sel, (hi,))
        conn.execute('DELETE FROM face_best_match_dirty WHERE face_id BETWEEN 1 AND ?',
                     (hi,))
        conn.commit()
        return n
    except Exception:
        conn.rollback()
        raise


def _max_face_id(conn):
    """The highest face id either table knows - deleted faces still have rows."""
    return max(conn.execute('SELECT COALESCE(MAX(id), 0) FROM face_detections').fetchone()[0],
               conn.execute('SELECT COALESCE(MAX(face_id), 0) FROM face_best_match '
                            f'WHERE metric IN ({_METRIC_LIST})').fetchone()[0])


def _full_rebuild(conn, pause=0.0):
    """
    Recompute every face, RANGE ids per transaction, so a writer elsewhere -
    an import, a workflow - waits at most one range. A face dirtied after its
    range was done stays dirty, which is correct. The sentinel goes last, so an
    interrupted rebuild starts again.
    """
    conn.execute('INSERT OR IGNORE INTO face_best_match_dirty VALUES (?)', (FULL_REBUILD,))
    conn.commit()
    hi = _max_face_id(conn)
    lo = 1
    while lo <= hi:
        top = lo + RANGE - 1
        conn.execute('BEGIN IMMEDIATE')
        try:
            _recompute(conn, '{f} BETWEEN ? AND ?', (lo, top))
            conn.execute('DELETE FROM face_best_match_dirty WHERE face_id BETWEEN ? AND ?',
                         (lo, top))
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        lo = top + 1
        if pause:
            time.sleep(pause)
    conn.execute('DELETE FROM face_best_match_dirty WHERE face_id = ?', (FULL_REBUILD,))
    conn.commit()


def drain(conn, max_faces=None, pause=0.0):
    """
    Bring face_best_match up to date. With max_faces, stop after about that
    many (a request's budget). Returns the number still dirty.
    """
    conn.commit()                 # BEGIN IMMEDIATE needs no transaction open
    n, full = pending(conn)
    if full or n > FULL_AT:
        if max_faces is not None:
            return n              # too big for a request; the caller backgrounds it
        _full_rebuild(conn, pause)
    done = 0
    while max_faces is None or done < max_faces:
        k = _drain_batch(conn)
        if not k:
            break
        done += k
        if pause:
            time.sleep(pause)
    return pending(conn)[0]


# ── Draining from the web process ────────────────────────────────────────────

_running = set()
_running_lock = threading.Lock()


def scoring_workflow_running(conn):
    like = ' OR '.join('w.python_script LIKE ?' for _ in SCORING_SCRIPTS)
    try:
        return conn.execute(
            f"SELECT 1 FROM workflow_executions we JOIN workflows w ON w.id = we.workflow_id "
            f"WHERE we.status = 'running' AND ({like}) LIMIT 1",
            tuple(f'%{s}' for s in SCORING_SCRIPTS)).fetchone() is not None
    except sqlite3.Error:
        return False


def _background(db_path):
    try:
        conn = sqlite3.connect(db_path, timeout=60)
        try:
            started = time.time()
            left = drain(conn, pause=0.05)
            _LOG.info('face_best_match: drained %s in %.1f s (%d left)',
                      db_path, time.time() - started, left)
        finally:
            conn.close()
    except Exception as exc:
        _LOG.warning('face_best_match: background drain of %s failed: %s', db_path, exc)
    finally:
        with _running_lock:
            _running.discard(db_path)


def ensure_fresh(conn, db_path):
    """
    Called by a request before it reads face_best_match. Drains inline when
    little is pending; otherwise starts the background drain (one per catalog)
    and returns at once.

    Returns True when the table is up to date, False when the caller is about
    to read it as it stands.
    """
    n, full = pending(conn)
    if not n:
        return True
    if full and _max_face_id(conn) <= RANGE:
        # A small catalog's full build is one range: cheaper than a thread.
        try:
            return drain(conn) == 0
        except sqlite3.OperationalError as exc:
            _LOG.info('face_best_match: inline build skipped: %s', exc)
            return False
    if not full and n <= INLINE_MAX:
        try:
            return drain(conn, max_faces=INLINE_MAX) == 0
        except sqlite3.OperationalError as exc:
            # Locked by a writer for longer than the timeout: answer from the
            # table as it stands rather than fail the page.
            _LOG.info('face_best_match: inline drain skipped: %s', exc)
            return False
    # A full build is never put off: without it the table is empty. A backlog
    # left by a scoring run in progress is - the run will dirty it again.
    if not full and scoring_workflow_running(conn):
        return False
    with _running_lock:
        if db_path in _running:
            return False
        _running.add(db_path)
    threading.Thread(target=_background, args=(db_path,), daemon=True,
                     name='face-best-match').start()
    return False
