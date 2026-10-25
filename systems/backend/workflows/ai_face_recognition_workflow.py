## FACE RECOGNTION WORKFLOW

import json
import math
import os
import sys
import sqlite3
import time
import requests
from datetime import datetime
from typing import Dict, List, Optional, Tuple

# Add parent directory to path to import catalog module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Cooperative stop - see workflows-review-spec.md section 3.4. Checked between
# chunks, each committed whole, so leaving needs no checkpoint. NOT
# incremental: every run scores every face again - see execute().
from lib.stopping import stop_requested, record_stopped
from catalog import Catalog
from face_recognition_schema import ensure_face_recognition_schema
import embedding_models


# Which model this run embeds and scores with. Set once in execute() from the
# catalog's own property, because a catalog migrates independently of its
# neighbours. A module global rather than a threaded parameter: this workflow
# runs as a subprocess over exactly one catalog, which is the same reason
# DEEPFACE_COSINE_THRESHOLD can be one too.
ACTIVE_MODEL = embedding_models.LEGACY_MODEL
ACTIVE_MODEL_ID = embedding_models.model_id(embedding_models.LEGACY_MODEL)

_DEEPFACE_BASE        = os.environ.get('DEEPFACE_URL', 'http://deepface.airlock.lan')
DEEPFACE_EMBED_URL    = _DEEPFACE_BASE + '/api/embed'

# Fallback threshold used only when DeepFace does not return one in its response.
# Must track DeepFace's own tuned value for the active model + cosine - 0.68 for
# VGG-Face, 0.593 for SFace (deepface/config/threshold.py). The previous 0.4 here
# was an older DeepFace default and, when it applied, wrote a wrong `threshold`
# and a wrong `verified` flag into face_similarity_scores.
#
# Per-model, and reassigned in execute() with ACTIVE_MODEL, because a single
# number here would be silently wrong for whichever model it was not chosen for.
DEEPFACE_COSINE_THRESHOLD = embedding_models.verification_distance(
    embedding_models.LEGACY_MODEL)
# MAX_REFS_PER_PERSON was removed - see best_per_person() for the
# measurements. Comparisons are cached-embedding dot products, so there is no
# longer a cost to cap and capping cost 19 points of rank-1 accuracy.

# Faces compared, written and committed together. Each chunk is one read of
# its embeddings, one matrix product against every reference, and one write
# transaction - see execute() for why that replaced a loop of one face at a time.
CHUNK = int(os.environ.get('FACE_MATCH_CHUNK', '2000'))

# A breath after each chunk's commit, so a write waiting on this one gets in.
# Without it the next chunk's write followed so closely that SQLite's busy
# handler, which polls at up to 100 ms, kept missing the gap: saving a search
# on homeserver waited out its 30 s and failed with "database is locked" while this
# ran over 471,601 faces (2026-10-20). 236 chunks x 0.25 s is about a minute.
WRITE_PAUSE = float(os.environ.get('FACE_MATCH_WRITE_PAUSE', '0.25'))

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
    # face_best_match's triggers make each score write open a statement
    # journal; on disk that file is most of their cost (a first run's inserts
    # 3.4x slower, in memory 1.7x). Per connection, never persistent.
    try:
        conn.execute('PRAGMA temp_store=MEMORY')
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


def _abs(stored_path: str, target_dir: str) -> str:
    """Resolve a DB-stored path (relative to target_dir) to an absolute path."""
    if stored_path and not os.path.isabs(stored_path):
        return os.path.join(target_dir, stored_path)
    return stored_path


# ─── Database helpers ─────────────────────────────────────────────────────────

def ensure_table(catalog_db_path: str):
    """
    Create or upgrade the face-recognition schema for this catalog.

    This used to carry its own CREATE TABLE, which had fallen behind models.py -
    it lacked euclidean_distance entirely, so a catalog whose table happened to
    be created by a workflow run rather than by the backend could never store a
    euclidean distance, silently and permanently.
    """
    ensure_face_recognition_schema(catalog_db_path)

# numpy does the comparison: a chunk of faces against every reference in one
# matrix product. Declared in backend/requirements.txt.
try:
    import numpy as _np
except ImportError:
    _np = None


def get_unidentified_face_ids(catalog_db_path: str) -> List[int]:
    """
    Every Automatic face detection with no person_id, on a file not removed,
    in id order. Ids only: the comparison needs nothing else about a face but
    its embedding, which is read a chunk at a time.
    """
    conn = sqlite3.connect(catalog_db_path)
    try:
        return [r[0] for r in conn.execute('''
            SELECT fd.id
              FROM face_detections fd
              JOIN file_contents fc ON fd.file_id = fc.id
             WHERE fd.source = 'Automatic' AND fd.person_id IS NULL
               -- Faces on a removed file are kept, not tagged.
               AND fc.removed_at IS NULL
             ORDER BY fd.id''')]
    finally:
        conn.close()


def get_reference_faces(catalog_db_path: str) -> List[Dict]:
    """
    Return all Manual face detections that have a person_id, joined to their
    pre-extracted face image in face_contents (populated by process_manual_faces workflow).
    Only returns references that have an extracted face file.
    Also returns the stored embedding (JSON string, may be NULL) and the year
    stored in face_contents (NULL if EXIF was unavailable).

    An embedding belonging to a different model is returned as NULL, so that
    backfill_reference_embeddings() recomputes it under the active model rather
    than this run comparing across embedding spaces. That is what makes the
    cutover self-healing: point a catalog at a new model and its references
    re-embed on the next run.
    """
    conn = sqlite3.connect(catalog_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT
            fd.id AS face_detection_id,
            fd.file_id,
            fd.x, fd.y, fd.w, fd.h,
            fd.person_id,
            p.name AS person_name,
            fcon.id AS face_content_id,
            fcon.file_path AS face_file_path,
            CASE WHEN COALESCE(fcon.embedding_model_id, 1) = :active_model_id
                 THEN fcon.embedding END AS embedding_json,
            fcon.year AS ref_year
        FROM face_detections fd
        INNER JOIN people p ON fd.person_id = p.id
        INNER JOIN face_contents fcon ON fcon.face_detection_id = fd.id
        WHERE fd.source = 'Manual' AND fd.person_id IS NOT NULL
        ORDER BY fd.id
    ''', {'active_model_id': ACTIVE_MODEL_ID})
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


def reference_matrix(reference_faces: List[Dict]):
    """
    The references as one unit-length matrix, its rows grouped by person:
    (matrix, reference_ids, person_ids, segments) where segments is a list of
    (person_id, start, end) row ranges. Built once per run - it used to be
    rebuilt from Python lists for every face.
    """
    refs = sorted((r for r in reference_faces if r.get('embedding')),
                  key=lambda r: (r['person_id'], r['face_detection_id']))
    m = _np.asarray([r['embedding'] for r in refs], dtype=_np.float64)
    norms = _np.linalg.norm(m, axis=1, keepdims=True)
    m = m / _np.where(norms > 0, norms, 1.0)
    people = [r['person_id'] for r in refs]
    segments, start = [], 0
    for i in range(1, len(people) + 1):
        if i == len(people) or people[i] != people[start]:
            segments.append((people[start], start, i))
            start = i
    return (m, _np.asarray([r['face_detection_id'] for r in refs]),
            _np.asarray(people), segments)


def load_query_embeddings(conn, face_ids: List[int]):
    """
    (ids, unit-length matrix) for those of `face_ids` that have this model's
    embedding. Read per chunk rather than all at once: holding every face's
    vector as Python lists for the whole run was 14 GB on `family`.

    A vector of the wrong length is dropped: the model id says what a row
    claims to be, the length says what it is.
    """
    marks = ','.join('?' * len(face_ids))
    expected = embedding_models.dims(ACTIVE_MODEL)
    ids, vecs = [], []
    for face_id, emb_json in conn.execute(
            f'SELECT face_detection_id, embedding FROM face_embeddings '
            f' WHERE embedding_model_id = ? AND face_detection_id IN ({marks})',
            [ACTIVE_MODEL_ID, *face_ids]):
        try:
            vec = json.loads(emb_json)
        except (ValueError, TypeError):
            continue
        if len(vec) != expected:
            continue
        ids.append(face_id)
        vecs.append(vec)
    if not ids:
        return [], None
    q = _np.asarray(vecs, dtype=_np.float64)
    norms = _np.linalg.norm(q, axis=1, keepdims=True)
    return ids, q / _np.where(norms > 0, norms, 1.0)


def best_per_person(query: 'object', refs) -> List[Tuple[int, List[Tuple[int, int, float]]]]:
    """
    For each query row, the closest reference of EVERY person:
    [(row, [(person_id, reference_id, cosine_distance), ...]), ...].

    Every reference, no cap and no date filter. There used to be both - at
    most three per person, preferring those within a year of the query - and
    the three were always the person's OLDEST confirmations. Measured by
    leave-one-out over all 287 confirmed faces
    (documentation/tests/bench_reference_selection.py), rank-1 accuracy:

        oldest 3 in year band (what shipped)   80.0%
        newest 3 in year band                  94.4%
        centroid of all references             96.1%
        3 sub-centroids per person             98.6%
        nearest of ALL references              98.9%   <- this

    Cosine distance is 1 - the dot product of unit vectors: the arithmetic
    DeepFace's own comparison does, verified against it over 600 pairs to
    within 5e-07.
    """
    matrix, ref_ids, _people, segments = refs
    dist = 1.0 - query @ matrix.T
    rows = _np.arange(dist.shape[0])
    # One argmin per PERSON over the whole chunk, not one per face per person.
    per_person = []
    for pid, a, b in segments:
        k = a + _np.argmin(dist[:, a:b], axis=1)
        per_person.append((pid, ref_ids[k], dist[rows, k]))
    return [(int(r), [(pid, int(rids[r]), float(ds[r])) for pid, rids, ds in per_person])
            for r in rows]


def people_in_the_same_photo(conn, face_ids: List[int]) -> Dict[int, set]:
    """
    {face_id: {person_id, ...}} - the people already named on ANOTHER face in
    the same photograph. One person cannot be two faces in one picture, so none
    of them is a candidate for this face.

    This was rule 1 of Match Faces Within Events, which deleted such rows -
    and this workflow, run before it, wrote them back every time. It is applied
    here, where the rows are written, so the appearance view respects it too
    (the contextual workflow's C1 applies it to the contextual one). Same
    definition as C1: any face with a person, whatever its source.
    """
    marks = ','.join('?' * len(face_ids))
    out: Dict[int, set] = {}
    for qid, pid in conn.execute(
            f'SELECT q.id, o.person_id FROM face_detections q '
            f'  JOIN face_detections o ON o.file_id = q.file_id AND o.id != q.id '
            f' WHERE q.id IN ({marks}) AND o.person_id IS NOT NULL', face_ids):
        out.setdefault(qid, set()).add(pid)
    return out


def write_chunk(conn, winners, now: str) -> int:
    """
    One chunk's scores, in ONE transaction on the run's one connection:
    [(query_id, [(person_id, reference_id, distance), ...]), ...].

    Each face keeps exactly the best reference per person. A row already there
    for that pair is left as it is - its contextual_distance and evidence,
    computed by the contextual workflow, survive the re-run - and every other
    row for the face is deleted: a winner that changed would otherwise leave
    the old, worse distance showing beside the new one.

    euclidean_l2 is sqrt(2 * cosine distance) for unit vectors, which is what
    DeepFace's euclidean_l2 metric is; it is filled in where missing.

    Returns rows inserted.
    """
    rows = []
    keep = []
    for qid, best in winners:
        for pid, rid, d in best:
            euclid = math.sqrt(max(0.0, 2.0 * d))
            rows.append((qid, rid, pid, d, euclid, ACTIVE_MODEL, 'cosine',
                         1 if d <= DEEPFACE_COSINE_THRESHOLD else 0,
                         DEEPFACE_COSINE_THRESHOLD, now))
            keep.append((qid, rid))
    query_ids = [qid for qid, _ in winners]
    with conn:
        before = conn.total_changes
        conn.executemany('''
            INSERT OR IGNORE INTO face_similarity_scores
            (query_face_detection_id, reference_face_detection_id, person_id,
             distance, euclidean_distance, model, distance_metric, verified,
             threshold, computed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', rows)
        inserted = conn.total_changes - before
        conn.executemany('''
            UPDATE face_similarity_scores SET euclidean_distance = ?
             WHERE query_face_detection_id = ? AND reference_face_detection_id = ?
               AND euclidean_distance IS NULL''',
            [(r[4], r[0], r[1]) for r in rows])
        # Keyed, and asked with NOT EXISTS so each row is one key lookup.
        # The first version was an unindexed temp table under a row-value
        # NOT IN, which SQLite answered by scanning the whole keep list for
        # every row: 52,000 x 52,000 per chunk, 99 s of a 101 s chunk in a
        # benchmark, and two hours for `family`'s first run. This is 0.4 s.
        conn.execute('CREATE TEMP TABLE IF NOT EXISTS _keep '
                     '(q INTEGER, r INTEGER, PRIMARY KEY (q, r)) WITHOUT ROWID')
        conn.execute('DELETE FROM _keep')
        conn.executemany('INSERT OR IGNORE INTO _keep VALUES (?, ?)', keep)
        marks = ','.join('?' * len(query_ids))
        conn.execute(f'''
            DELETE FROM face_similarity_scores
             WHERE query_face_detection_id IN ({marks})
               AND NOT EXISTS (
                   SELECT 1 FROM _keep k
                    WHERE k.q = face_similarity_scores.query_face_detection_id
                      AND k.r = face_similarity_scores.reference_face_detection_id)''',
            query_ids)
    return inserted


def deduplicate_scores_by_person(catalog_db_path: str) -> int:
    """
    For every (query_face_detection_id, person_id) pair in face_similarity_scores,
    keep only the single row with the lowest distance and delete all others.

    Returns the number of rows deleted.
    """
    conn = sqlite3.connect(catalog_db_path)
    cursor = conn.cursor()
    cursor.execute('''
        DELETE FROM face_similarity_scores
        WHERE id IN (
            SELECT id
            FROM (
                SELECT id,
                       ROW_NUMBER() OVER (
                           PARTITION BY query_face_detection_id, person_id
                           ORDER BY distance ASC, id ASC
                       ) AS rn
                FROM face_similarity_scores
            ) ranked
            WHERE rn > 1
        )
    ''')
    deleted = cursor.rowcount
    conn.commit()
    conn.close()
    return deleted


# ─── Embedding helpers ────────────────────────────────────────────────────────

def _verify_embedding(data: dict, what: str) -> Optional[List[float]]:
    """
    Accept an embedding only if the service produced it with the model we asked
    for, at the length that model produces.

    The service defaults to its own configured model when a request does not
    name one, so a version skew - an older image that ignores the `model` field
    - would otherwise return VGG-Face vectors that got stored tagged as SFace.
    That is precisely the silent corruption this whole change exists to stop,
    so it is checked on the way in rather than discovered later.
    """
    vec = data['embedding']
    served = data.get('model')
    if served and served != ACTIVE_MODEL:
        print(f'    Warning: asked {DEEPFACE_EMBED_URL} for {ACTIVE_MODEL} and '
              f'got {served} ({what}); discarding. The deepface service is '
              f'probably older than this workflow.')
        return None
    try:
        embedding_models.check_embedding(vec, ACTIVE_MODEL, what)
    except embedding_models.EmbeddingModelMismatch as e:
        print(f'    Warning: {e}; discarding')
        return None
    return vec


# There were two more functions here: save_query_embedding() and
# compute_embedding_for_bytes(), which let this workflow create a query face's
# embedding when the cache missed. Both are gone on purpose. Embedding query
# faces is Measure Faces' job (ai_embed_faces.py); doing it here as well is
# what made confirming three faces cost four hours, and would make that
# separation decorative. The one below stays because REFERENCE faces - 45 of
# them, not 469,847 - are still backfilled here, which is cheap and keeps a
# cutover self-healing.


def compute_embedding_for_face(face_path: str) -> Optional[List[float]]:
    """
    Call POST /api/embed on the DeepFace backend and return the embedding for
    the active model as a list of floats, or None on any error.
    """
    try:
        with open(face_path, 'rb') as fh:
            response = requests.post(
                DEEPFACE_EMBED_URL,
                files={'face_image': ('face.jpg', fh, 'image/jpeg')},
                data={'model': ACTIVE_MODEL},
                timeout=30,
            )
        if response.status_code == 200:
            data = response.json()
            if data.get('success') and data.get('embedding'):
                return _verify_embedding(data, face_path)
    except Exception as e:
        print(f'    Warning: embed call failed for {face_path}: {e}')
    return None


def backfill_reference_embeddings(catalog_db_path: str,
                                   reference_faces: List[Dict],
                                   target_dir: str = '') -> List[Dict]:
    """
    For any reference face that has no stored embedding, compute it now by
    calling POST /api/embed and persist it in face_contents.

    Modifies the dicts in-place (adds 'embedding' key as List[float] or None),
    then returns the same list.  Faces that already have an embedding are
    skipped instantly.

    This is a one-time cost per reference face: subsequent workflow runs will
    load the cached value from the DB and skip this function.
    """
    missing = [r for r in reference_faces if not r.get('embedding_json')]
    if missing:
        print(f'  Backfilling embeddings for {len(missing)} reference face(s) '
              f'(this happens once per face)...')

    conn = sqlite3.connect(catalog_db_path)
    _wal_pragmas(conn, 30000)
    cursor = conn.cursor()

    for ref in reference_faces:
        if ref.get('embedding_json'):
            # Already stored — deserialise and attach
            ref['embedding'] = json.loads(ref['embedding_json'])
            continue

        face_path = _abs(ref['face_file_path'], target_dir)
        if not os.path.exists(face_path):
            print(f'    Skipping missing file: {face_path}')
            ref['embedding'] = None
            continue

        embedding = compute_embedding_for_face(face_path)
        if embedding is not None:
            emb_json = json.dumps(embedding)
            cursor.execute(
                'UPDATE face_contents SET embedding = ?, embedding_model_id = ? '
                'WHERE id = ?',
                (emb_json, ACTIVE_MODEL_ID, ref['face_content_id'])
            )
            conn.commit()
            ref['embedding'] = embedding
        else:
            ref['embedding'] = None

    conn.close()
    return reference_faces


# ─── Image helpers ────────────────────────────────────────────────────────────

def execute(source_dir, target_dir, files, progress_callback,
            catalog_id=None, catalog=None, execution_id=None):
    """
    Compare all unidentified Automatic face detections against all known Manual faces
    and persist cosine similarity scores in face_similarity_scores.

    This workflow:
    1. Ensures the face_similarity_scores table exists
    2. Loads the ids of all Automatic faces with no person_id (query faces)
    3. Loads all Manual faces that have been extracted to face_contents
       (reference faces), as one matrix
    4. A chunk of query faces at a time: reads their cached embeddings,
       compares them with every reference, keeps the best reference per
       person, and writes the chunk in one transaction
    5. Removes any duplicate (face, person) rows a past run left

    Prerequisites: process_manual_faces workflow must have been run so that
    face_contents rows exist for all Manual+identified detections.

    Args:
        source_dir: Not used (workflow operates on catalog database)
        target_dir: Catalog target directory (contains workflow_results.db)
        files: Not used
        progress_callback: Called as progress_callback(current, total)
        catalog_id: Catalog ID (deprecated; use catalog)
        catalog: Catalog instance
        execution_id: Workflow execution ID for tracking

    Returns:
        dict with keys: faces_compared, scores_created, faces_skipped, faces_failed
    """
    # Resolve catalog
    if catalog is None and catalog_id is not None:
        catalog = Catalog.get_by_id(catalog_id)
        if catalog is None:
            raise ValueError(f'Catalog with ID {catalog_id} not found')
    if catalog is None:
        raise ValueError('Either catalog or catalog_id must be provided')

    catalog_db_path = os.path.join(target_dir, 'workflow_results.db')

    # Ensure table exists
    ensure_table(catalog_db_path)

    # Which model this catalog uses. Resolved before anything reads an
    # embedding, because every query below filters on it: an embedding from
    # another model is invisible to this run, not merely ranked lower.
    global ACTIVE_MODEL, ACTIVE_MODEL_ID, DEEPFACE_COSINE_THRESHOLD
    ACTIVE_MODEL = embedding_models.active_model(catalog_db_path)
    ACTIVE_MODEL_ID = embedding_models.model_id(ACTIVE_MODEL)
    DEEPFACE_COSINE_THRESHOLD = embedding_models.verification_distance(ACTIVE_MODEL)
    print(f'Embedding model: {ACTIVE_MODEL} '
          f'({embedding_models.dims(ACTIVE_MODEL)}-d, id {ACTIVE_MODEL_ID}, '
          f'fallback verification bar {DEEPFACE_COSINE_THRESHOLD})')

    if _np is None:
        raise RuntimeError('numpy is required to match faces and is not installed')

    # ── Setup, reported as it goes ───────────────────────────────────────────
    # The total is known from the first query, so the run says how many faces
    # it has from its first seconds. It used to report nothing until a setup
    # that loaded every face, every score and every embedding had finished:
    # two minutes and 14 GB on `family`, showing "0 of 0".
    print('Loading unidentified faces...')
    face_ids = get_unidentified_face_ids(catalog_db_path)
    total_to_process = len(face_ids)
    print(f'Found {total_to_process} unidentified face detections')
    if progress_callback:
        progress_callback(0, max(total_to_process, 1))

    print('Loading reference faces from face_contents...')
    reference_faces = get_reference_faces(catalog_db_path)
    total_ref = len(reference_faces)
    print(f'Found {total_ref} reference face images')

    if total_to_process == 0 or total_ref == 0:
        reason = 'no unidentified faces' if total_to_process == 0 else 'no reference faces in face_contents (run process_manual_faces first)'
        print(f'Nothing to compare: {reason}')
        return {
            'faces_compared': 0,
            'scores_created': 0,
            'faces_skipped': 0,
            'faces_failed': 0
        }

    # Backfill any reference faces that don't yet have a stored embedding.
    # This is a one-time cost per face; subsequent runs are instant (loaded from DB).
    print('Ensuring all reference embeddings are computed...')
    reference_faces = backfill_reference_embeddings(catalog_db_path, reference_faces, target_dir)
    refs = reference_matrix(reference_faces)
    refs_without_embedding = total_ref - len(refs[1])
    print(f'  {len(refs[1])} reference(s) of {len(refs[3])} people compared'
          + (f'; {refs_without_embedding} without an embedding left out'
             if refs_without_embedding else ''))
    if not len(refs[1]):
        print('Nothing to compare: no reference has an embedding')
        return {'faces_compared': 0, 'scores_created': 0,
                'faces_skipped': 0, 'faces_failed': 0}

    # ── Every face, every run, a chunk at a time ─────────────────────────────
    #
    # Every face is compared against every reference on every run. That is
    # what makes a new confirmation reach every face at once, and it is
    # affordable because it is arithmetic on cached embeddings.
    #
    # It was a loop of one face at a time: per face a photograph decoded and
    # cropped for nothing (the crop only mattered without an embedding, and
    # those faces are skipped), the references rebuilt from Python lists,
    # about four database connections and two commits, and a progress write
    # every fifth face. On `family`, 469,000 faces took ~5 hours at ~26 a
    # second. Each chunk below is one read, one matrix product, one write
    # transaction, then one stop check and one progress write.
    #
    # It also used to skip the references a face had already been scored
    # against - which, with only each person's best reference stored, meant
    # comparing against everything EXCEPT the best and then deleting the best:
    # a face's best match alternated between its best and second-best
    # reference from run to run.
    #
    # Faces with no embedding are counted and left alone - creating embeddings
    # is Measure Faces' job (ai_embed_faces.py), and doing it here made
    # confirming three faces cost four hours once.
    conn = sqlite3.connect(catalog_db_path, timeout=60)
    _wal_pragmas(conn)
    compared = 0
    not_embedded = 0
    total_scores_created = 0
    stopped = False
    try:
        for start in range(0, total_to_process, CHUNK):
            if execution_id and stop_requested(catalog_db_path, execution_id):
                print(f'\nStop requested - leaving after {start} of {total_to_process} face(s).')
                stopped = True
                break
            chunk = face_ids[start:start + CHUNK]
            ids, query = load_query_embeddings(conn, chunk)
            not_embedded += len(chunk) - len(ids)
            if ids:
                taken = people_in_the_same_photo(conn, ids)
                winners = [(ids[row], [b for b in best if b[0] not in taken.get(ids[row], ())])
                           for row, best in best_per_person(query, refs)]
                total_scores_created += write_chunk(conn, winners, datetime.now().isoformat())
                compared += len(ids)
                if WRITE_PAUSE > 0:
                    time.sleep(WRITE_PAUSE)
            done = start + len(chunk)
            print(f'  {done:,} of {total_to_process:,} face(s): '
                  f'{compared:,} compared, {not_embedded:,} without an embedding')
            # After the chunk's commit, so this write never waits on our own.
            if progress_callback:
                progress_callback(done, total_to_process)
    finally:
        conn.close()
    skipped = 0
    failed = 0

    if stopped:
        # Deduplication is deliberately skipped: it is a pass over the whole
        # score table, and the next run does it. Stopping should not be the
        # moment we start extra work.
        if execution_id:
            record_stopped(
                catalog_db_path, execution_id, processed_files=compared,
                skipped_files=skipped,
                message=(f'Stopped by request after {compared:,} of '
                         f'{total_to_process:,} face(s). Running it again '
                         f'compares every face again - each run scores every '
                         f'face against every reference, so that a new '
                         f'confirmation reaches all of them.'))
        return {
            'faces_compared': compared,
            'scores_created': total_scores_created,
            'faces_not_embedded': not_embedded,
            'stopped': True,
        }

    # ── Step: deduplicate — keep only the closest score per person per face ──
    print('\nDeduplicating scores: keeping lowest distance per person per face...')
    dupes_removed = deduplicate_scores_by_person(catalog_db_path)
    print(f'  {dupes_removed} duplicate candidate(s) removed')

    print(f'\n{"="*60}')
    print(f'Face recognition workflow complete!')
    print(f'  Query faces:       {total_to_process}')
    print(f'  Compared:          {compared}')
    print(f'  Skipped (done):    {skipped}')
    print(f'  Failed:            {failed}')
    print(f'  Scores created:    {total_scores_created}')
    print(f'  Duplicates removed:{dupes_removed}')
    if not_embedded:
        # The headline when it applies, because it is the difference between
        # "nobody matched" and "most of the catalog was never looked at".
        print('')
        print(f'  {not_embedded:,} face(s) HAVE NO EMBEDDING and were not compared.')
        print(f'  Run "Measure Faces" to create them; this pass then covers')
        print(f'  the whole catalog instead of {compared:,} of '
              f'{total_to_process:,}.')
    print(f'{"="*60}')

    return {
        'faces_compared': compared,
        'scores_created': total_scores_created,
        'faces_skipped': skipped,
        'faces_failed': failed,
        'faces_not_embedded': not_embedded,
        'duplicates_removed': dupes_removed
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

    parser = argparse.ArgumentParser(description='Face recognition workflow (subprocess mode)')
    parser.add_argument('--execution-id', type=int, required=True)
    parser.add_argument('--target-dir', type=str, required=True)
    parser.add_argument('--catalog-id', type=int, required=True)
    args = parser.parse_args()

    catalog_db_path = os.path.join(args.target_dir, 'workflow_results.db')

    catalog = Catalog.get_by_id(args.catalog_id)
    if catalog is None:
        print(f'ERROR: Catalog {args.catalog_id} not found', file=sys.stderr)
        _db_complete(catalog_db_path, args.execution_id, 'failed',
                     f'Catalog {args.catalog_id} not found')
        sys.exit(1)

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
            catalog=catalog,
            execution_id=args.execution_id,
        )
        files_processed = result.get('faces_compared', 0) if isinstance(result, dict) else 0
        if isinstance(result, dict) and result.get('stopped'):
            # execute() already wrote 'stopped'. Recording 'completed' here would
            # fire the downstream chain over a partial run.
            print(f'Subprocess execution {args.execution_id} stopped by request.')
        else:
            _db_complete(catalog_db_path, args.execution_id, 'completed',
                         skipped_files=result.get('faces_skipped', 0) if isinstance(result, dict) else 0)
            print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)
