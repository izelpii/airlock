## CONTEXTUAL FACE RECOGNITION WORKFLOW
##
## Re-scores existing face_similarity_scores rows by fusing the appearance
## distance with relational context: events, time proximity, who-appears-with-whom,
## person priors and age plausibility.
##
## Spec: documentation/specs/contextual-similarity-metric-spec.md  (Phase 1)
##
## This workflow makes NO DeepFace calls. It is a pure function of the catalog
## database: it reads the cosine distance the recognition workflow already stored
## and writes contextual_distance / contextual_evidence beside it.

import json
import math
import os
import sqlite3
import sys
from collections import defaultdict
from bisect import bisect_left, bisect_right
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple

# Add parent directory to path to import catalog module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import Catalog
import embedding_models
from media_types import RAW_EXTENSIONS, VIDEO_EXTENSIONS
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from lib.stopping import stop_requested, record_stopped  # noqa: E402

# ─── Stopping ─────────────────────────────────────────────────────────────────
# 'item' in WORKFLOW_STOP_MODES. Every write this workflow makes is either one
# transaction (co-occurrence, per-photo assignment, clusters) or one chunk of
# faces committed on its own (scores, perceptual hashes), so a stop between two
# of them leaves each score as either this run's or the previous run's - both
# complete. It used to be 'never', from when every score row was rewritten in
# one pass; v5.5.0 made the writes chunked and that reason went away.

class _Stopped(Exception):
    """Raised at a boundary when the user has asked this run to stop."""

    def __init__(self, where):
        super().__init__(where)
        self.where = where


# Set by execute(): a no-argument check, so the helpers below need no ids.
_stop_check = lambda: False  # noqa: E731


def _check_stop(where):
    if _stop_check():
        raise _Stopped(where)

# Which model this run's embeddings come from. Set in execute(); the clustering
# pass below stacks these vectors into one matrix, so a mixture of 4096-d and
# 128-d would be a ragged array - a crash rather than a wrong answer, but there
# is no reason to find out that way.
ACTIVE_MODEL = embedding_models.LEGACY_MODEL
ACTIVE_MODEL_ID = embedding_models.model_id(embedding_models.LEGACY_MODEL)

# numpy powers the clustering matmul. Declared in backend/requirements.txt; the
# workflow degrades gracefully (skipping clustering) rather than failing if absent.
try:
    import numpy as np
except ImportError:
    np = None


# ─── Model constants (spec §6.2) ──────────────────────────────────────────────

WEIGHTS_VERSION = '1.0.0'

# Appearance calibration — the logistic that turns a cosine distance into a
# probability. APP_TAU is the distance at which the model is 50/50.
#
# These are NOT guesses: they are maximum-likelihood fitted to the catalog's own
# confirmed faces (Platt scaling over leave-one-out pairs) by
#     python3 documentation/tests/test_contextual_metric.py --fit
# Re-run that whenever a meaningful number of new faces has been tagged, and
# update these two numbers from its output.
#
# History: the spec drafted 0.40 (an older DeepFace threshold). DeepFace's
# installed threshold is 0.68, but using the *match threshold* as the 50/50 point
# proved badly overconfident in practice — predictions above 0.9 were correct only
# about half the time — because DeepFace's threshold is a permissive "could be a
# match" bar, not a break-even point. The fitted values below sit between the
# same-person mean (0.563) and different-person mean (0.775) for this catalog.
#
# Fitted on the Family catalog, 44 leave-one-out pairs (8 same-person):
#   NLL 11.198 vs 16.841 for tau=0.68 / slope=12.0
APP_TAU   = 0.58
APP_SLOPE = 16.2

# Contextual signal weights, in log-odds units
W_EVENT     = 1.4
W_TIME      = 1.2
W_COOC      = 1.0
W_DUPLICATE = 1.6    # Phase 3 — not yet emitted
W_PRIOR     = 0.3
W_GEO       = 0.6    # Phase 3
W_DEVICE    = 0.5    # Phase 3
W_AGE_GAP   = 0.8    # applied to a negative signal

# Signal parameters
TIME_HALFLIFE_MIN = 120     # matches EVENT_GAP_HOURS = 2 in ev_extract_events.py
EVENT_SATURATION  = 3       # n_conf at which the event signal saturates
EVENT_COOC_WEIGHT = 0.6     # event-scope nPMI is worth 60% of file-scope nPMI
AGE_GAP_FULL_YEARS = 10.0   # year gap at which the age penalty saturates

# Phase 3 signal parameters
NEAR_DUP_WINDOW_MIN  = 10   # only compare photos within this many minutes
NEAR_DUP_MAX_HAMMING = 6    # dHash bit distance treated as "near-identical"
GEO_PRECISION        = 6    # geohash prefix length (~1.2 km)
GEO_WINDOW_HOURS     = 24   # co-location counts within this window
DEVICE_BURST_SEC     = 60   # same camera within this gap is one burst

# Quality damping
MIN_FACE_AREA_RATIO = 0.02
MIN_CONFIDENCE      = 0.90

# Clustering (Phase 2).
# CLUSTER_MAX_DISTANCE is deliberately far tighter than the match threshold:
# one confirmation labels a whole cluster, so clusters must be pure. It was also
# checked against this catalog's real pairwise distribution — connected components
# chain badly as it loosens (largest cluster 27 faces at 0.30, 50 at 0.40,
# 81 at 0.50), so 0.30 is both the spec value and the empirically safe one.
CLUSTER_MAX_DISTANCE = 0.30
CLUSTER_BLOCK_SIZE   = 256    # rows per block: 256 x ~469,000 float32 is ~0.5 GB on `family`
CLUSTER_WORKERS      = max(1, min(4, (os.cpu_count() or 2) // 2))  # x OpenBLAS's 2 threads each
CLUSTER_PARALLEL_MIN = 20000  # rows below which forking workers costs more than it saves
# All-pairs clustering is quadratic, and at this model's 0.30 the faces chain:
# on `family` (480,000 faces) one component held 348,567 of them, and splitting
# it costs hours. Above this many faces the step is skipped and the existing
# groups are kept, so Re-rank finishes instead of sitting at 100 %.
CLUSTER_MAX_FACES    = 60000
CLUSTER_MIN_SIZE     = 2      # singletons are not stored as clusters

# Purity guard. Connected components chains: A~B and B~C groups A with C even when
# they are far apart, so one intermediate face can weld two people into a single
# cluster. Because a confirmation labels the whole cluster, that would propagate a
# wrong label to every member. A component whose widest internal pair exceeds
# CLUSTER_MAX_SPREAD is re-clustered at a tighter threshold until every part is
# coherent, so tight sub-groups survive instead of the whole group being lost.
CLUSTER_MAX_SPREAD      = 0.45
CLUSTER_REFINE_FACTOR   = 0.70
CLUSTER_MIN_THRESHOLD   = 0.10
CLUSTER_MAX_REFINE_DEPTH = 4

# Per-photo assignment (Phase 2, constraint C3)
ASSIGNMENT_MIN_P = 0.50       # below this a face is left unassigned

# Auto-confirm (Phase 4) — disabled until the evaluation harness clears it
AUTO_CONFIRM_ENABLED    = False
AUTO_CONFIRM_MIN_P      = 0.995
AUTO_CONFIRM_MIN_MARGIN = 0.15

_SCRIPT_BASENAME = os.path.basename(os.path.abspath(__file__))


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


# ─── Small numeric helpers ────────────────────────────────────────────────────

def sigmoid(x: float) -> float:
    """Numerically stable logistic function."""
    if x >= 0:
        z = math.exp(-x)
        return 1.0 / (1.0 + z)
    z = math.exp(x)
    return z / (1.0 + z)


def parse_dt(value: Optional[str]) -> Optional[datetime]:
    """
    Parse the timestamp formats this catalog stores:
      master_datetime  ISO 8601        '2026-08-29T17:27:45' / with 'Z' / offset
      creation_date    EXIF            '2026:08:29 17:27:45'
      added_at         SQLite default  '2026-08-29 17:27:45'
    Returns None when unparseable.
    """
    if not value:
        return None
    text = str(value).strip()
    if not text:
        return None
    # EXIF style: colons in the date part
    if len(text) >= 19 and text[4] == ':' and text[7] == ':':
        text = text[:10].replace(':', '-') + ' ' + text[11:19]
    text = text.replace('T', ' ')
    if text.endswith('Z'):
        text = text[:-1]

    # Strip any UTC offset. Both signs must be handled, and only in the TIME part -
    # the date itself contains hyphens, so a naive split on '-' would destroy it.
    # A negative offset ("2025-11-16T15:08:27-06:00") is the common case for this
    # catalog and was previously unparsed, which silently dropped 20 of 47 files -
    # including every burst frame - out of the time-based signals.
    #
    # The offset is discarded rather than converted, deliberately: EXIF
    # creation_date carries local wall-clock time with no offset at all, so
    # normalising only the offset-bearing values to UTC would put two timestamps
    # from the same burst hours apart whenever they came from different sources.
    # Local wall-clock keeps every source on one footing, and every comparison the
    # model makes (burst, event, time proximity) is relative and within one zone.
    head, tail = text[:10], text[10:]
    for sign in ('+', '-'):
        if sign in tail:
            tail = tail.split(sign)[0]
    text = head + tail

    if '.' in text:
        text = text.split('.')[0]
    text = text.strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
        try:
            return datetime.strptime(text, fmt)
        except ValueError:
            continue
    return None


# ─── Schema ───────────────────────────────────────────────────────────────────

def ensure_schema(catalog_db_path: str) -> None:
    """
    Create the contextual columns, index and person_cooccurrence table if absent.
    Mirrors backend/models.py::init_db() and migrate_add_contextual_metric.py so a
    catalog that has not been migrated still works on first run.
    """
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    _wal_pragmas(conn, 30000)
    cursor = conn.cursor()

    cursor.execute('PRAGMA table_info(face_similarity_scores)')
    existing = {row[1] for row in cursor.fetchall()}
    for col, col_type in (('contextual_distance', 'REAL'),
                          ('contextual_evidence', 'TEXT'),
                          ('contextual_computed_at', 'TIMESTAMP')):
        if col not in existing:
            try:
                cursor.execute(
                    f'ALTER TABLE face_similarity_scores ADD COLUMN {col} {col_type}')
            except Exception:
                pass

    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_fss_contextual
        ON face_similarity_scores(contextual_distance)
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS person_cooccurrence (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            scope         TEXT    NOT NULL,
            person_a_id   INTEGER NOT NULL,
            person_b_id   INTEGER NOT NULL,
            co_count      INTEGER NOT NULL,
            count_a       INTEGER NOT NULL,
            count_b       INTEGER NOT NULL,
            npmi          REAL    NOT NULL,
            jaccard       REAL    NOT NULL,
            computed_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_a_id) REFERENCES people (id) ON DELETE CASCADE,
            FOREIGN KEY (person_b_id) REFERENCES people (id) ON DELETE CASCADE,
            UNIQUE (scope, person_a_id, person_b_id)
        )
    ''')
    # Perceptual hash for near-duplicate detection (Phase 3)
    cursor.execute('PRAGMA table_info(file_contents)')
    _fc_cols = {r[1] for r in cursor.fetchall()}
    if 'perceptual_hash' not in _fc_cols:
        try:
            cursor.execute('ALTER TABLE file_contents ADD COLUMN perceptual_hash TEXT')
        except Exception:
            pass
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_fc_phash ON file_contents(perceptual_hash)')

    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pcooc_a ON person_cooccurrence(person_a_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_pcooc_b ON person_cooccurrence(person_b_id)')

    # Face clusters (Phase 2) — groups of unidentified faces that are almost
    # certainly the same person, so one confirmation can label the whole group.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS face_clusters (
            id                 INTEGER PRIMARY KEY AUTOINCREMENT,
            centroid           TEXT    NOT NULL,
            member_count       INTEGER NOT NULL DEFAULT 0,
            cohesion           REAL,
            max_member_distance REAL,
            resolved_person_id INTEGER,
            resolved_at        TIMESTAMP,
            created_at         TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (resolved_person_id) REFERENCES people (id) ON DELETE SET NULL
        )
    ''')
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS face_cluster_members (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_id           INTEGER NOT NULL,
            face_detection_id    INTEGER NOT NULL UNIQUE,
            distance_to_centroid REAL NOT NULL,
            created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cluster_id) REFERENCES face_clusters (id) ON DELETE CASCADE,
            FOREIGN KEY (face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_fcm_cluster ON face_cluster_members(cluster_id)')

    # Assignment result from constraint C3 (Phase 2), stored beside the raw score
    cursor.execute('PRAGMA table_info(face_similarity_scores)')
    _cols = {r[1] for r in cursor.fetchall()}
    if 'contextual_assigned' not in _cols:
        try:
            cursor.execute(
                'ALTER TABLE face_similarity_scores ADD COLUMN contextual_assigned INTEGER DEFAULT 0')
        except Exception:
            pass

    # Auto-confirm audit trail (Phase 4). Every automatic assignment lands here
    # with the evidence that justified it, so a run can be reviewed and reverted.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS auto_tag_audit (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id        INTEGER,
            face_detection_id   INTEGER NOT NULL,
            person_id           INTEGER NOT NULL,
            contextual_distance REAL NOT NULL,
            confidence          REAL NOT NULL,
            evidence            TEXT NOT NULL,
            action              TEXT NOT NULL,
            reverted_at         TIMESTAMP,
            reverted_by         TEXT,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES people (id) ON DELETE CASCADE
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ata_face ON auto_tag_audit(face_detection_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ata_exec ON auto_tag_audit(execution_id)')

    # The audit table is created by ai_ml_faces_recognition_workflow.py; create it
    # here too so constraint eliminations can always be recorded.
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS face_similarity_scores_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER,
            rule_name TEXT NOT NULL,
            action_type TEXT NOT NULL,
            source_face_detection_id INTEGER NOT NULL,
            face_similarity_score_id INTEGER,
            candidate_person_id INTEGER,
            reference_face_detection_id INTEGER,
            source_file_id INTEGER,
            source_event_id INTEGER,
            previous_distance REAL,
            new_distance REAL,
            reason TEXT NOT NULL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    conn.commit()
    conn.close()


# ─── Co-occurrence graph (spec §4.4) ──────────────────────────────────────────

def build_cooccurrence_graph(catalog_db_path: str) -> Dict[Tuple[int, int], float]:
    """
    Rebuild person_cooccurrence from confirmed face detections, for both scopes:

        scope 'file'  — two people confirmed in the same photo
        scope 'event' — two people confirmed anywhere in the same event

    Edge weight is normalised pointwise mutual information:

        p(P)   = count(P) / N
        p(P,Q) = co_count(P,Q) / N
        nPMI   = log(p(P,Q) / (p(P)p(Q))) / -log(p(P,Q))     in [-1, 1]

    nPMI corrects for base rate: a person photographed constantly co-occurs with
    everyone, and raw counts would read that as affinity.

    Returns the combined lookup used by signal 3:
        {(person_a, person_b): max(npmi_file, EVENT_COOC_WEIGHT * npmi_event)}
    with both key orderings present for cheap lookup.
    """
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # group_key -> set(person_id), for each scope
    groups: Dict[str, Dict[int, set]] = {'file': defaultdict(set), 'event': defaultdict(set)}

    cursor.execute('''
        SELECT fd.person_id, fd.file_id, ef.event_id
        FROM face_detections fd
        LEFT JOIN event_files ef ON ef.file_id = fd.file_id
        WHERE fd.person_id IS NOT NULL
    ''')
    for row in cursor.fetchall():
        groups['file'][row['file_id']].add(row['person_id'])
        if row['event_id'] is not None:
            groups['event'][row['event_id']].add(row['person_id'])

    rows_to_write = []
    npmi_by_scope: Dict[str, Dict[Tuple[int, int], float]] = {'file': {}, 'event': {}}

    for scope, group_map in groups.items():
        # Only groups with at least one person contribute to the base rate
        units = [people for people in group_map.values() if people]
        total = len(units)
        if total == 0:
            continue

        count: Dict[int, int] = defaultdict(int)
        co_count: Dict[Tuple[int, int], int] = defaultdict(int)

        for people in units:
            ordered = sorted(people)
            for p in ordered:
                count[p] += 1
            for i, a in enumerate(ordered):
                for b in ordered[i + 1:]:
                    co_count[(a, b)] += 1

        for (a, b), co in co_count.items():
            p_a = count[a] / total
            p_b = count[b] / total
            p_ab = co / total
            if p_ab <= 0 or p_a <= 0 or p_b <= 0:
                continue
            pmi = math.log(p_ab / (p_a * p_b))
            denom = -math.log(p_ab)
            # p_ab == 1.0 means the pair co-occurs in every unit: perfectly
            # associated, and the nPMI denominator degenerates to 0.
            npmi = 1.0 if denom == 0 else max(-1.0, min(1.0, pmi / denom))
            union = count[a] + count[b] - co
            jaccard = co / union if union > 0 else 0.0
            rows_to_write.append((scope, a, b, co, count[a], count[b], npmi, jaccard))
            npmi_by_scope[scope][(a, b)] = npmi

    # Full rebuild — cheap, and avoids incremental drift
    cursor.execute('DELETE FROM person_cooccurrence')
    cursor.executemany('''
        INSERT INTO person_cooccurrence
            (scope, person_a_id, person_b_id, co_count, count_a, count_b, npmi, jaccard)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
    ''', rows_to_write)
    conn.commit()
    conn.close()

    # Combined lookup for signal 3, symmetric
    combined: Dict[Tuple[int, int], float] = {}
    for key in set(npmi_by_scope['file']) | set(npmi_by_scope['event']):
        value = max(npmi_by_scope['file'].get(key, 0.0),
                    EVENT_COOC_WEIGHT * npmi_by_scope['event'].get(key, 0.0))
        a, b = key
        combined[(a, b)] = value
        combined[(b, a)] = value

    return combined


# ─── Context indexes (spec §6.3 step 4) ───────────────────────────────────────

def load_context_indexes(catalog_db_path: str) -> Dict:
    """
    Load every relational fact the signals need, in one pass, held in memory.

    Returns a dict with:
      files            file_id -> {datetime, event_id, geohash, camera, width, height}
      confirmed_by_person   person_id -> [ {file_id, datetime, event_id, year, ...} ]
      confirmed_in_file     file_id   -> {person_id, ...}          (constraint C1)
      people           person_id -> {name, dob, appearances}
      max_appearances  int                                          (signal 5)
    """
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # ── File facts ────────────────────────────────────────────────────────────
    files: Dict[int, Dict] = {}
    cursor.execute('''
        SELECT
            fc.id AS file_id,
            fc.master_datetime AS ts,
            ed.geohash,
            ed.camera_make,
            ed.camera_model,
            ed.width,
            ed.height,
            fc.perceptual_hash,
            ef.event_id
        FROM file_contents fc
        LEFT JOIN exif_data   ed ON fc.exif_data_id = ed.id
        LEFT JOIN event_files ef ON ef.file_id = fc.id
    ''')
    for row in cursor.fetchall():
        dt = parse_dt(row['ts'])
        files[row['file_id']] = {
            'datetime': dt,
            'year': dt.year if dt else None,
            'event_id': row['event_id'],
            'geohash': (row['geohash'] or '')[:6] or None,
            'camera': f"{row['camera_make'] or ''}|{row['camera_model'] or ''}",
            'width': row['width'],
            'height': row['height'],
            'phash': row['perceptual_hash'],
        }

    # ── Confirmed appearances ─────────────────────────────────────────────────
    confirmed_by_person: Dict[int, List[Dict]] = defaultdict(list)
    confirmed_in_file: Dict[int, set] = defaultdict(set)
    confirmed_faces_by_file: Dict[int, List[Dict]] = defaultdict(list)
    cursor.execute('''
        SELECT fd.id AS face_id, fd.person_id, fd.file_id,
               fd.x, fd.y, fd.w, fd.h, fd.image_width, fd.image_height
        FROM face_detections fd
        WHERE fd.person_id IS NOT NULL
    ''')
    for row in cursor.fetchall():
        confirmed_faces_by_file[row['file_id']].append({
            'person_id': row['person_id'],
            'x': row['x'], 'y': row['y'], 'w': row['w'], 'h': row['h'],
            'image_width': row['image_width'], 'image_height': row['image_height'],
        })
        finfo = files.get(row['file_id'], {})
        confirmed_by_person[row['person_id']].append({
            'face_id': row['face_id'],
            'file_id': row['file_id'],
            'datetime': finfo.get('datetime'),
            'year': finfo.get('year'),
            'event_id': finfo.get('event_id'),
            'geohash': finfo.get('geohash'),
            'camera': finfo.get('camera'),
        })
        confirmed_in_file[row['file_id']].add(row['person_id'])

    # ── People ────────────────────────────────────────────────────────────────
    people: Dict[int, Dict] = {}
    cursor.execute('SELECT id, name, date_of_birth FROM people')
    for row in cursor.fetchall():
        appearances = len({a['file_id'] for a in confirmed_by_person.get(row['id'], [])})
        people[row['id']] = {
            'name': row['name'],
            'dob': parse_dt(row['date_of_birth']),
            'appearances': appearances,
        }

    conn.close()

    max_appearances = max((p['appearances'] for p in people.values()), default=0)

    return {
        'files': files,
        'confirmed_by_person': confirmed_by_person,
        'confirmed_in_file': confirmed_in_file,
        'confirmed_faces_by_file': confirmed_faces_by_file,
        'people': people,
        'max_appearances': max_appearances,
        'person_index': build_person_index(confirmed_by_person),
    }


def build_person_index(confirmed_by_person: Dict[int, List[Dict]]) -> Dict[int, Dict]:
    """
    Each person's confirmed appearances, pre-sorted for the signals that search
    them - built once per run.

    The time, device, location and age signals each walked a person's whole
    list for every candidate row: on `family` 12.2 M rows, and signal_time was
    the single largest cost of the run. Sorted by datetime they become a
    binary search plus a look at the neighbours.

    Entries are (datetime, position in confirmed_by_person, file_id). The
    position is kept because the device and location signals report the FIRST
    qualifying appearance in list order, and their evidence text names its
    exact gap - so the fast path must choose the same one.
    """
    index: Dict[int, Dict] = {}
    for pid, apps in confirmed_by_person.items():
        timed, by_camera, by_geo = [], defaultdict(list), defaultdict(list)
        events: Dict[int, set] = defaultdict(set)
        years = set()
        for pos, a in enumerate(apps):
            if a['event_id'] is not None:
                events[a['event_id']].add(a['file_id'])
            if a['year'] is not None:
                years.add(a['year'])
            if a['datetime'] is None:
                continue
            entry = (a['datetime'], pos, a['file_id'])
            timed.append(entry)
            if a.get('camera'):
                by_camera[a['camera']].append(entry)
            if a.get('geohash'):
                by_geo[a['geohash'][:GEO_PRECISION]].append(entry)
        timed.sort()
        for lst in list(by_camera.values()) + list(by_geo.values()):
            lst.sort()
        index[pid] = {
            'timed': timed, 'timed_dts': [e[0] for e in timed],
            'by_camera': {k: (v, [e[0] for e in v]) for k, v in by_camera.items()},
            'by_geo': {k: (v, [e[0] for e in v]) for k, v in by_geo.items()},
            'events': events,
            'years': sorted(years),
        }
    return index


def _first_within(entries_and_dts, when: datetime, window: timedelta,
                  exclude_file: int):
    """
    Of the entries within `window` of `when` (inclusive), the one EARLIEST in
    the person's list, ignoring `exclude_file`. None if there is none.
    """
    if not entries_and_dts:
        return None
    entries, dts = entries_and_dts
    lo = bisect_left(dts, when - window)
    hi = bisect_right(dts, when + window)
    best = None
    for entry in entries[lo:hi]:
        if entry[2] != exclude_file and (best is None or entry[1] < best[1]):
            best = entry
    return best


# ─── Signals (spec §4.3) ──────────────────────────────────────────────────────

def signal_event(query_file_id: int, event_id: Optional[int],
                 person_id: int, ctx: Dict) -> Tuple[float, str]:
    """
    Signal 1 — event co-membership.
    s = min(1, n_conf / EVENT_SATURATION), where n_conf counts OTHER files in the
    same event carrying a confirmed face of this person. Same-file confirmations
    are excluded: that situation is constraint C1, not evidence.
    """
    if event_id is None:
        return 0.0, ''
    files_in_event = ctx['person_index'].get(person_id, {}).get('events', {}).get(event_id, ())
    n_conf = len(files_in_event) - (1 if query_file_id in files_in_event else 0)
    if n_conf == 0:
        return 0.0, ''
    s = min(1.0, n_conf / EVENT_SATURATION)
    return s, f'{n_conf} other confirmed photo(s) of this person in event {event_id}'


def signal_time(query_dt: Optional[datetime], query_file_id: int,
                person_id: int, ctx: Dict) -> Tuple[float, str]:
    """
    Signal 2 — time proximity.
    s = exp(-dt_minutes / TIME_HALFLIFE_MIN) to the nearest confirmed appearance
    of this person in a different file. Decays to ~0.37 at 2 h, ~0 after a day.
    """
    if query_dt is None:
        return 0.0, ''
    idx = ctx['person_index'].get(person_id)
    best_min = None
    if idx and idx['timed']:
        # The nearest appearance in another file is the nearest one on either
        # side of query_dt once same-file entries are stepped over.
        timed, j = idx['timed'], bisect_left(idx['timed_dts'], query_dt)
        for k in (range(j - 1, -1, -1), range(j, len(timed))):
            for i in k:
                if timed[i][2] != query_file_id:
                    delta_min = abs((query_dt - timed[i][0]).total_seconds()) / 60.0
                    if best_min is None or delta_min < best_min:
                        best_min = delta_min
                    break
    if best_min is None:
        return 0.0, ''
    s = math.exp(-best_min / TIME_HALFLIFE_MIN)
    if best_min < 90:
        human = f'{best_min:.0f} min'
    elif best_min < 60 * 48:
        human = f'{best_min / 60:.1f} h'
    else:
        human = f'{best_min / 1440:.1f} days'
    return s, f'nearest confirmed appearance {human} away'


def signal_cooccurrence(query_file_id: int, person_id: int,
                        ctx: Dict, cooc: Dict[Tuple[int, int], float]) -> Tuple[float, str]:
    """
    Signal 3 — co-occurrence affinity.
    s = max over people already confirmed in this photo of nPMI(person, other),
    clamped to [0, 1]. Negative affinity is treated as no evidence rather than
    counter-evidence: two people rarely photographed together may simply not have
    been photographed together yet.
    """
    others = ctx['confirmed_in_file'].get(query_file_id, set())
    best = 0.0
    best_other = None
    for other in others:
        if other == person_id:
            continue
        value = cooc.get((person_id, other), 0.0)
        if value > best:
            best = value
            best_other = other
    if best <= 0 or best_other is None:
        return 0.0, ''
    s = max(0.0, min(1.0, best))
    other_name = ctx['people'].get(best_other, {}).get('name', f'person {best_other}')
    return s, f'nPMI {best:.2f} with {other_name}, confirmed in this photo'


def signal_prior(person_id: int, ctx: Dict) -> Tuple[float, str]:
    """
    Signal 5 — person base rate.
    s = log(1 + appearances) / log(1 + max_appearances). Deliberately small weight:
    it should break ties, never outvote appearance.
    """
    max_app = ctx['max_appearances']
    if max_app <= 0:
        return 0.0, ''
    appearances = ctx['people'].get(person_id, {}).get('appearances', 0)
    if appearances <= 0:
        return 0.0, ''
    s = math.log1p(appearances) / math.log1p(max_app)
    return s, f'{appearances} confirmed appearance(s) in the catalog'


def signal_age_gap(query_year: Optional[int], person_id: int,
                   ctx: Dict) -> Tuple[float, str]:
    """
    Signal 8 — age-gap penalty (negative signal).
    s = -min(1, |query_year - nearest_confirmed_year| / AGE_GAP_FULL_YEARS).
    Faces drift with age and VGG-Face degrades across years, so a reference from a
    decade away is weaker evidence than one from the same year.
    """
    if query_year is None:
        return 0.0, ''
    years = ctx['person_index'].get(person_id, {}).get('years')
    if not years:
        return 0.0, ''
    j = bisect_left(years, query_year)
    gap = min(abs(query_year - years[i]) for i in (j - 1, j) if 0 <= i < len(years))
    if gap <= 1:
        return 0.0, ''
    s = -min(1.0, gap / AGE_GAP_FULL_YEARS)
    return s, f'nearest confirmed reference is {gap} year(s) away'


def quality_factor(face: Dict, file_info: Dict) -> Tuple[float, str]:
    """
    Signal 9 — detection-quality damping (a multiplier on the whole context sum).

        quality = min(1, area_ratio / MIN_FACE_AREA_RATIO) * min(1, confidence / MIN_CONFIDENCE)

    A tiny or low-confidence detection has unreliable appearance evidence, so
    context must not be allowed to carry it over the line on its own. This is the
    guard against the model's characteristic failure: confidently labelling a
    six-pixel blur because the event says that person was there.
    """
    width = face.get('image_width') or file_info.get('width')
    height = face.get('image_height') or file_info.get('height')
    area_term = 1.0
    if width and height and width > 0 and height > 0:
        area_ratio = (face['w'] * face['h']) / float(width * height)
        area_term = min(1.0, area_ratio / MIN_FACE_AREA_RATIO)
    conf = face.get('confidence')
    conf_term = 1.0 if conf is None else min(1.0, float(conf) / MIN_CONFIDENCE)
    q = max(0.0, min(1.0, area_term * conf_term))
    detail = f'area_term {area_term:.2f}, confidence_term {conf_term:.2f}'
    return q, detail


# ─── Perceptual hashing (Phase 3) ─────────────────────────────────────────────

def dhash(image_path: str, hash_size: int = 8) -> Optional[str]:
    """
    Difference hash: grayscale, resize to (hash_size+1) x hash_size, then compare
    each pixel with its right-hand neighbour to yield hash_size^2 bits.

    dHash rather than a DCT-based pHash because it is a few lines of Pillow, needs
    no numpy, and is exactly as good at the one question asked here: "is this the
    next frame of the same burst?" Returns 16 hex chars, or None if unreadable.
    """
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(image_path) as img:
            img = img.convert('L').resize((hash_size + 1, hash_size),
                                          Image.Resampling.LANCZOS)
            pixels = list(img.getdata())
    except Exception:
        return None

    bits = 0
    for row in range(hash_size):
        base = row * (hash_size + 1)
        for col in range(hash_size):
            bits <<= 1
            if pixels[base + col] > pixels[base + col + 1]:
                bits |= 1
    return f'{bits:016x}'


def hamming(hash_a: str, hash_b: str) -> Optional[int]:
    """Bit distance between two hex-encoded hashes; None if either is missing."""
    if not hash_a or not hash_b:
        return None
    try:
        return bin(int(hash_a, 16) ^ int(hash_b, 16)).count('1')
    except ValueError:
        return None


# Hashes committed per transaction. A commit is a few ms against ~10 s of dhash
# per 2,000 files, so batching keeps the speed of the old single commit while a
# stop (or a kill) keeps what was already hashed.
HASH_COMMIT_EVERY = 2000


def backfill_perceptual_hashes(catalog_db_path: str, target_dir: str,
                               progress_callback=None) -> Dict:
    """
    Compute a perceptual hash for every file that lacks one.

    Incremental by design: files that already have a hash are skipped, so this is a
    one-time cost per photo spread across runs. Hashes the 1200px BASE derivative
    rather than the original, so HEIC and CR3 files are covered without decoding
    raw data - and pinned to the base, because the hashes already stored were
    computed from it and a hash of a different render is not comparable.

    A file with no base derivative is hashed from its original only when Pillow
    can read it. A video, a RAW or a HEIC with no derivative cannot be, and on
    `family` those were 69,090 files tried again, and failed again, on every run
    - about 5 ms each, minutes of a setup that reports nothing. They are left
    for when a derivative exists, which the query picks up by itself.
    """
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT fc.id AS file_id, fc.file_path, fc.file_type, "
        "       (SELECT av.alternative_file_path FROM alternative_versions av "
        "         WHERE av.original_file_id = fc.id "
        "           AND av.rung IN ('1200','1600','2048','2560') "
        "         ORDER BY (av.rung = '1200') DESC, av.width ASC, av.id ASC "
        "         LIMIT 1) AS alt_path "
        "FROM file_contents fc "
        "WHERE fc.perceptual_hash IS NULL AND fc.removed_at IS NULL"
    )
    rows = cursor.fetchall()
    rows, waiting = _hashable(rows)
    total = len(rows)

    hashed = skipped = 0
    updates = []

    def _write():
        # In batches, so a stop - or a kill - keeps the hashes already made.
        if updates:
            cursor.executemany(
                'UPDATE file_contents SET perceptual_hash = ? WHERE id = ?', updates)
            conn.commit()
            updates.clear()

    for done, row in enumerate(rows, 1):
        if progress_callback and done % 500 == 0:
            progress_callback(done, total)
        if done % HASH_COMMIT_EVERY == 1 and done > 1:
            # The batch before this row is hashed: keep it, then ask.
            _write()
            try:
                _check_stop('filling in perceptual hashes')
            except _Stopped:
                conn.close()
                raise
        path = row['alt_path'] or row['file_path']
        if path and not os.path.isabs(path):
            path = os.path.join(target_dir, path)
        if not path or not os.path.exists(path):
            skipped += 1
            continue
        value = dhash(path)
        if value:
            updates.append((value, row['file_id']))
            hashed += 1
        else:
            skipped += 1

    _write()
    conn.close()
    if progress_callback and total:
        progress_callback(total, total)
    return {'hashed': hashed, 'skipped': skipped, 'already': 0,
            'no_derivative_yet': waiting}


def _hashable(rows) -> Tuple[list, int]:
    """
    Split rows into those dhash can read and a count of those it cannot until
    a base derivative exists: no derivative, and an original Pillow cannot open.
    """
    unreadable = VIDEO_EXTENSIONS | RAW_EXTENSIONS | {'.heic', '.heif'}
    keep, waiting = [], 0
    for row in rows:
        ext = (row['file_type'] or os.path.splitext(row['file_path'] or '')[1]).lower()
        if not row['alt_path'] and ext in unreadable:
            waiting += 1
        else:
            keep.append(row)
    return keep, waiting


def build_near_duplicate_index(ctx: Dict) -> Dict[int, List[Tuple[int, float]]]:
    """
    Map each file to its visually near-identical neighbours.

    Only files within NEAR_DUP_WINDOW_MIN of each other are compared, which keeps
    this linear-ish instead of O(N^2): a burst frame is by definition seconds away
    from its siblings, so a wide time window buys nothing but work.

    Returns file_id -> [(other_file_id, phash_similarity)], similarity in [0, 1].
    """
    dated = [(fid, info['datetime'], info.get('phash'))
             for fid, info in ctx['files'].items()
             if info.get('datetime') and info.get('phash')]
    dated.sort(key=lambda t: t[1])

    index: Dict[int, List[Tuple[int, float]]] = defaultdict(list)
    for i, (fid, dt, ph) in enumerate(dated):
        for j in range(i + 1, len(dated)):
            other_id, other_dt, other_ph = dated[j]
            if (other_dt - dt).total_seconds() > NEAR_DUP_WINDOW_MIN * 60:
                break                        # sorted by time - nothing later qualifies
            bits = hamming(ph, other_ph)
            if bits is None or bits > NEAR_DUP_MAX_HAMMING:
                continue
            similarity = 1.0 - bits / 64.0
            index[fid].append((other_id, similarity))
            index[other_id].append((fid, similarity))
    return index


def _box_iou(a: Dict, b: Dict) -> float:
    """
    Intersection-over-union of two face boxes, each normalised by its own image
    dimensions so boxes from differently-sized versions are comparable.
    """
    def norm(box):
        w = box.get('image_width') or 0
        h = box.get('image_height') or 0
        if not w or not h:
            return None
        return (box['x'] / w, box['y'] / h, box['w'] / w, box['h'] / h)

    na, nb = norm(a), norm(b)
    if na is None or nb is None:
        return 0.0
    ax, ay, aw, ah = na
    bx, by, bw, bh = nb
    ix1, iy1 = max(ax, bx), max(ay, by)
    ix2, iy2 = min(ax + aw, bx + bw), min(ay + ah, by + bh)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


# ─── Phase 3 signals ──────────────────────────────────────────────────────────

def signal_near_duplicate(face: Dict, person_id: int, ctx: Dict,
                          near_dup: Dict[int, List[Tuple[int, float]]]) -> Tuple[float, str]:
    """
    Signal 4 - near-duplicate propagation.

    s = phash_similarity(f, f') * box_iou(q, q') for the best near-duplicate photo
    f' that has a confirmed face of this person in a similar position.

    The strongest signal in the model when it fires: a burst frame is effectively
    the same photo, so a face in the same place is almost certainly the same person.
    Both factors are required - a near-identical frame with the face somewhere else
    is a different person standing in a different spot.
    """
    neighbours = near_dup.get(face['file_id'])
    if not neighbours:
        return 0.0, ''
    best = 0.0
    best_detail = ''
    for other_file_id, phash_sim in neighbours:
        for confirmed in ctx['confirmed_faces_by_file'].get(other_file_id, []):
            if confirmed['person_id'] != person_id:
                continue
            iou = _box_iou(face, confirmed)
            if iou <= 0:
                continue
            value = phash_sim * iou
            if value > best:
                best = value
                best_detail = (f'near-duplicate photo (visual match '
                               f'{phash_sim:.2f}, box overlap {iou:.2f})')
    return min(1.0, best), best_detail


def signal_geo(face: Dict, person_id: int, ctx: Dict) -> Tuple[float, str]:
    """
    Signal 6 - co-location. Fires when this photo shares a geohash prefix with a
    confirmed appearance of the person within GEO_WINDOW_HOURS.

    Independent of event grouping, so it rescues photos that failed to group into
    an event but were plainly taken at the same place and time.
    """
    info = ctx['files'].get(face['file_id'], {})
    geohash = info.get('geohash')
    when = info.get('datetime')
    if not geohash or not when:
        return 0.0, ''
    hit = _first_within(
        ctx['person_index'].get(person_id, {}).get('by_geo', {}).get(geohash[:GEO_PRECISION]),
        when, timedelta(hours=GEO_WINDOW_HOURS), face['file_id'])
    if hit:
        hours = abs((when - hit[0]).total_seconds()) / 3600.0
        return 1.0, (f'same location ({geohash[:GEO_PRECISION]}) within '
                     f'{hours:.1f} h of a confirmed appearance')
    return 0.0, ''


def signal_device_burst(face: Dict, person_id: int, ctx: Dict) -> Tuple[float, str]:
    """
    Signal 7 - same-device burst. Same camera make+model within DEVICE_BURST_SEC of
    a confirmed appearance: one photographer firing a burst, which stays strong even
    when the framing changes enough to defeat the near-duplicate signal.
    """
    info = ctx['files'].get(face['file_id'], {})
    camera = info.get('camera')
    when = info.get('datetime')
    if not camera or camera == '|' or not when:
        return 0.0, ''
    hit = _first_within(
        ctx['person_index'].get(person_id, {}).get('by_camera', {}).get(camera),
        when, timedelta(seconds=DEVICE_BURST_SEC), face['file_id'])
    if hit:
        seconds = abs((when - hit[0]).total_seconds())
        return 1.0, (f'same camera, {seconds:.0f}s from a confirmed appearance')
    return 0.0, ''


# ─── Audit ────────────────────────────────────────────────────────────────────

# ─── Scoring ──────────────────────────────────────────────────────────────────

def score_candidate(face: Dict, candidate: Dict, ctx: Dict,
                    cooc: Dict[Tuple[int, int], float],
                    near_dup: Optional[Dict[int, List[Tuple[int, float]]]] = None) -> Dict:
    """
    Fuse appearance and context for one (query face, candidate person) pair.

        L_total = L_app + quality * sum(w_k * s_k)
        p       = sigmoid(L_total)
        contextual_distance = 1 - p

    Returns {'distance', 'p', 'evidence'} where evidence is the JSON-ready
    breakdown that gets stored in contextual_evidence.
    """
    file_info = ctx['files'].get(face['file_id'], {})
    person_id = candidate['person_id']

    d_app = candidate['distance']
    l_app = APP_SLOPE * (APP_TAU - d_app)

    quality, quality_detail = quality_factor(face, file_info)

    terms = []
    for name, weight, (s, detail) in (
        ('event',   W_EVENT,   signal_event(face['file_id'], file_info.get('event_id'), person_id, ctx)),
        ('time',    W_TIME,    signal_time(file_info.get('datetime'), face['file_id'], person_id, ctx)),
        ('cooc',    W_COOC,    signal_cooccurrence(face['file_id'], person_id, ctx, cooc)),
        ('prior',   W_PRIOR,   signal_prior(person_id, ctx)),
        ('age_gap', W_AGE_GAP, signal_age_gap(file_info.get('year'), person_id, ctx)),
        ('near_duplicate', W_DUPLICATE,
         signal_near_duplicate(face, person_id, ctx, near_dup or {})),
        ('geo',     W_GEO,     signal_geo(face, person_id, ctx)),
        ('device',  W_DEVICE,  signal_device_burst(face, person_id, ctx)),
    ):
        if s == 0.0:
            continue
        terms.append({
            'signal': name,
            's': round(s, 4),
            'w': weight,
            'contrib': round(weight * s, 4),
            'detail': detail,
        })

    ctx_sum = sum(t['contrib'] for t in terms)
    l_total = l_app + quality * ctx_sum
    p = sigmoid(l_total)

    evidence = {
        'weights_version': WEIGHTS_VERSION,
        'tau': APP_TAU,
        'slope': APP_SLOPE,
        'd_app': round(d_app, 6),
        'l_app': round(l_app, 4),
        'quality': round(quality, 4),
        'quality_detail': quality_detail,
        'terms': terms,
        'context_sum': round(ctx_sum, 4),
        'l_total': round(l_total, 4),
        'p': round(p, 6),
    }
    return {'distance': 1.0 - p, 'p': p, 'evidence': evidence}


# ─── Data loading for the scoring pass ────────────────────────────────────────

def load_query_faces(catalog_db_path: str) -> Dict[int, Dict]:
    """All unidentified Automatic detections, keyed by face_detection id."""
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, file_id, x, y, w, h, confidence, image_width, image_height
        FROM face_detections
        WHERE source = 'Automatic' AND person_id IS NULL
    ''')
    faces = {row['id']: dict(row) for row in cursor.fetchall()}
    conn.close()
    return faces


# Faces scored, written and committed together. See execute().
CHUNK = int(os.environ.get('CONTEXT_CHUNK', '2000'))


def _has_candidate_rows(catalog_db_path: str) -> bool:
    """Whether face_similarity_scores has anything to re-score."""
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    try:
        return conn.execute('SELECT 1 FROM face_similarity_scores '
                            'WHERE distance IS NOT NULL LIMIT 1').fetchone() is not None
    finally:
        conn.close()


def _faces_with_candidates(catalog_db_path: str) -> List[int]:
    """
    Query faces with score rows, in id order - from idx_fss_query_face, not by
    loading the rows. Step 6 of the spec: this workflow re-scores rows the
    cosine workflow created; it never invents new pairs.
    """
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    try:
        return [r[0] for r in conn.execute(
            'SELECT DISTINCT query_face_detection_id FROM face_similarity_scores '
            'WHERE distance IS NOT NULL ORDER BY query_face_detection_id')]
    finally:
        conn.close()


# ─── Clustering (Phase 2, spec §7.3) ──────────────────────────────────────────

def _scan_block(start):
    """
    One block of the upper triangle of `_SCAN`'s similarity matrix: rows
    start..start+CLUSTER_BLOCK_SIZE against themselves and every row after.

    Returns (start, lowest similarity seen, linked rows, linked columns), the
    pairs as int32 row numbers into the scanned matrix with row < column. A
    module function reading a module global so a forked worker can run it on
    the parent's matrix without copying it.
    """
    sub, min_similarity = _SCAN
    stop = min(start + CLUSTER_BLOCK_SIZE, len(sub))
    block = sub[start:stop] @ sub[start:].T
    lowest = float(block.min())
    if min_similarity is None:
        empty = np.empty(0, dtype=np.int32)
        return start, lowest, empty, empty
    rows, cols = np.nonzero(block > min_similarity)
    del block
    keep = rows < cols                      # self-pairs and the lower half of the diagonal block
    return (start, lowest, (rows[keep] + start).astype(np.int32),
            (cols[keep] + start).astype(np.int32))


_SCAN = None


def _scan(sub, min_similarity, progress_callback=None):
    """
    Every pair of rows of `sub` more similar than `min_similarity`, and the
    lowest similarity of any pair - both from one pass over the upper triangle.

    Each pair is computed once. The pass before v5.5.2 computed every row
    against all rows and threw the lower half away, and ran on the 2 threads
    the image's OpenBLAS is built for. On `family` (480,000 faces) that was ~4
    hours with no progress. A large scan is now split across forked workers.
    `min_similarity=None` skips collecting pairs.
    """
    global _SCAN
    n = len(sub)
    starts = list(range(0, n, CLUSTER_BLOCK_SIZE))
    _SCAN = (sub, min_similarity)
    rows, cols, lowest = [], [], 1.0
    try:
        if n >= CLUSTER_PARALLEL_MIN and CLUSTER_WORKERS > 1:
            import multiprocessing
            from concurrent.futures import ProcessPoolExecutor
            with ProcessPoolExecutor(CLUSTER_WORKERS,
                                     mp_context=multiprocessing.get_context('fork')) as pool:
                results = pool.map(_scan_block, starts)
                for done, (_, low, r, c) in enumerate(results, 1):
                    lowest = min(lowest, low)
                    rows.append(r)
                    cols.append(c)
                    if progress_callback and (done % 20 == 0 or done == len(starts)):
                        progress_callback(done, len(starts))
                    if done % 20 == 0 and _stop_check():
                        # Without cancelling, leaving the `with` waits for every
                        # block still queued in the workers.
                        pool.shutdown(wait=True, cancel_futures=True)
                        raise _Stopped('clustering unidentified faces')
        else:
            for start in starts:
                _check_stop('clustering unidentified faces')
                _, low, r, c = _scan_block(start)
                lowest = min(lowest, low)
                rows.append(r)
                cols.append(c)
    finally:
        _SCAN = None
    if not rows:
        empty = np.empty(0, dtype=np.int32)
        return lowest, empty, empty
    return lowest, np.concatenate(rows), np.concatenate(cols)


def _connected(n, rows, cols):
    """
    Connected components of n nodes joined by the pairs (rows[i], cols[i]),
    as lists of node numbers.

    Vectorised hook-and-compress: each pass hooks the larger root of every
    still-split pair under the smaller, then compresses every chain to its
    root. ~51 million pairs on `family`, which one Python union per pair would
    take minutes over.
    """
    parent = np.arange(n, dtype=np.int64)
    a, b = rows.astype(np.int64), cols.astype(np.int64)
    while len(a):
        ra, rb = parent[a], parent[b]
        split = ra != rb
        a, b, ra, rb = a[split], b[split], ra[split], rb[split]
        if not len(a):
            break
        np.minimum.at(parent, np.maximum(ra, rb), np.minimum(ra, rb))
        while True:
            jumped = parent[parent]
            if np.array_equal(jumped, parent):
                break
            parent = jumped
    order = np.argsort(parent, kind='stable')
    cuts = np.flatnonzero(np.diff(parent[order])) + 1
    return [part.tolist() for part in np.split(order, cuts)]


def _components(indices, matrix, threshold):
    """Connected components over `indices`, linking pairs closer than `threshold`."""
    _, rows, cols = _scan(matrix[indices], 1.0 - threshold)
    return [[indices[i] for i in part]
            for part in _connected(len(indices), rows, cols)]


def _refine_cluster(members, matrix, threshold, depth=0):
    """
    Split a chained cluster into coherent sub-clusters.

    Connected components links A-B and B-C into one group even when A and C are
    far apart, so a single intermediate face can weld two people together. That
    matters here more than usual: one confirmation labels an entire cluster, so a
    chained cluster propagates a wrong label to every member.

    Rather than discard such a group, re-cluster it at a progressively tighter
    threshold until every part is internally coherent (max pairwise distance
    within CLUSTER_MAX_SPREAD). Tight sub-groups survive; only genuinely
    ambiguous faces fall out as singletons and return to the normal review queue.

    Observed on the Family catalog: one 27-member component contained 9 pairs
    more than 0.60 apart. Refinement breaks it into coherent parts.

    The spread and the tighter split come from one blocked scan. A component
    here can hold a large share of 480,000 faces, and its full pairwise matrix
    would not fit in memory.
    """
    if len(members) < CLUSTER_MIN_SIZE:
        return []

    tighter = threshold * CLUSTER_REFINE_FACTOR
    split_further = depth < CLUSTER_MAX_REFINE_DEPTH and tighter >= CLUSTER_MIN_THRESHOLD
    lowest, rows, cols = _scan(matrix[members],
                               1.0 - tighter if split_further else None)
    if 1.0 - lowest <= CLUSTER_MAX_SPREAD:
        return [members]
    if not split_further:
        return []

    out = []
    for part in _connected(len(members), rows, cols):
        out.extend(_refine_cluster([members[i] for i in part], matrix, tighter, depth + 1))
    return out


def _has_confirmed_faces(catalog_db_path: str) -> bool:
    """Whether any face has a person - the references Face Recognition needs."""
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    try:
        return conn.execute(
            'SELECT 1 FROM face_detections WHERE person_id IS NOT NULL LIMIT 1'
        ).fetchone() is not None
    finally:
        conn.close()


def build_face_clusters(catalog_db_path: str, progress_callback=None) -> Dict:
    """
    Group unidentified faces that are almost certainly the same person.

    Why: the bottleneck at catalog scale is human review, not CPU. Clustering
    collapses N faces into K identities, so one confirmation can label a whole
    group instead of one face at a time.

    Method (spec section 7.3): L2-normalise all cached embeddings once, compute the
    cosine matrix in blocks to bound memory, link pairs closer than
    CLUSTER_MAX_DISTANCE, and take connected components.

    Connected components chain - A~B and B~C puts A and C together even if they
    are far apart - which is why the threshold is tight and why each cluster also
    stores `cohesion` (mean distance to centroid) and `max_member_distance`. A
    cluster with poor cohesion has probably chained two people together and should
    be treated with suspicion rather than trusted blindly.

    Returns statistics; writes face_clusters + face_cluster_members.
    """
    if np is None:
        print('  numpy unavailable - skipping clustering')
        return {'clusters': 0, 'clustered_faces': 0, 'skipped': True}

    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute(
        "SELECT fe.face_detection_id, fe.embedding "
        "FROM face_embeddings fe "
        "INNER JOIN face_detections fd ON fd.id = fe.face_detection_id "
        "WHERE fe.embedding_model_id = ? "
        "  AND fd.person_id IS NULL "
        "  AND fd.source = 'Automatic' "
        "ORDER BY fe.face_detection_id",
        (ACTIVE_MODEL_ID,)
    )
    ids, vectors = [], []
    expected = embedding_models.dims(ACTIVE_MODEL)
    for row in cursor.fetchall():
        try:
            vec = json.loads(row['embedding'])
        except (TypeError, ValueError):
            continue
        # Length as well as id: np.array() on a ragged list silently produces an
        # object array on some numpy versions and raises on others, and neither
        # is a good way to learn that one row is tagged wrongly.
        if len(vec) != expected:
            continue
        vectors.append(vec)
        ids.append(row['face_detection_id'])

    n = len(ids)
    if n < CLUSTER_MIN_SIZE:
        print(f'  only {n} embedded unidentified face(s) - nothing to cluster')
        conn.close()
        return {'clusters': 0, 'clustered_faces': 0, 'skipped': False}

    if n > CLUSTER_MAX_FACES:
        print(f'  {n:,} embedded unidentified face(s) - more than CLUSTER_MAX_FACES '
              f'({CLUSTER_MAX_FACES:,}); clustering skipped, existing groups kept')
        conn.close()
        return {'clusters': 0, 'clustered_faces': 0, 'skipped': True}

    matrix = np.asarray(vectors, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    matrix /= norms

    min_similarity = 1.0 - CLUSTER_MAX_DISTANCE
    try:
        _, rows, cols = _scan(matrix, min_similarity, progress_callback)
    except _Stopped:
        conn.close()            # nothing written yet: the old groups stand
        raise
    print(f'  {len(rows):,} linked pair(s) among {n:,} face(s)')
    groups = {i: part for i, part in enumerate(_connected(n, rows, cols))}
    del rows, cols

    # Split any chained component into coherent sub-clusters before storing
    refined: List[List[int]] = []
    split_count = 0
    for members in groups.values():
        if len(members) < CLUSTER_MIN_SIZE:
            continue
        parts = _refine_cluster(members, matrix, CLUSTER_MAX_DISTANCE)
        if len(parts) != 1 or (parts and len(parts[0]) != len(members)):
            split_count += 1
        refined.extend(parts)
    if split_count:
        print(f'  {split_count} chained component(s) refined into coherent sub-clusters')

    cursor.execute('DELETE FROM face_cluster_members')
    cursor.execute('DELETE FROM face_clusters')

    clusters = clustered_faces = 0
    for members in refined:
        if len(members) < CLUSTER_MIN_SIZE:
            continue
        member_matrix = matrix[members]
        centroid = member_matrix.mean(axis=0)
        cn = np.linalg.norm(centroid)
        if cn > 0:
            centroid = centroid / cn
        dists = 1.0 - (member_matrix @ centroid)
        cursor.execute(
            'INSERT INTO face_clusters '
            '(centroid, member_count, cohesion, max_member_distance) '
            'VALUES (?, ?, ?, ?)',
            (json.dumps([float(x) for x in centroid]), len(members),
             float(dists.mean()), float(dists.max()))
        )
        cluster_id = cursor.lastrowid
        cursor.executemany(
            'INSERT INTO face_cluster_members '
            '(cluster_id, face_detection_id, distance_to_centroid) '
            'VALUES (?, ?, ?)',
            [(cluster_id, ids[m], float(d)) for m, d in zip(members, dists)]
        )
        clusters += 1
        clustered_faces += len(members)

    conn.commit()
    conn.close()
    print(f'  {clusters} cluster(s) covering {clustered_faces} of {n} face(s) '
          f'at d < {CLUSTER_MAX_DISTANCE}')
    return {'clusters': clusters, 'clustered_faces': clustered_faces, 'skipped': False}


# ─── Per-photo assignment (Phase 2, constraint C3) ────────────────────────────

def resolve_per_photo_assignment(catalog_db_path: str) -> int:
    """
    Constraint C3 - a person may hold at most one face per photo.

    Independent per-(face, person) scores can hand the same person to two faces in
    one photo. This resolves each photo as a small assignment problem: take the
    highest-confidence (face, person) pair, lock it in, remove that face and that
    person from contention, repeat. Hungarian would be optimal, but photos have a
    handful of faces and greedy-by-confidence is within noise of it.

    Marks the winning row with contextual_assigned = 1. The raw scores are left
    untouched, so the UI can show both the model's ranking and its resolution.

    Returns the number of assignments made.
    """
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    cursor.execute('UPDATE face_similarity_scores SET contextual_assigned = 0')

    cursor.execute(
        'SELECT fss.id, fss.query_face_detection_id AS face_id, fss.person_id, '
        '       fss.contextual_distance, fd.file_id '
        'FROM face_similarity_scores fss '
        'INNER JOIN face_detections fd ON fd.id = fss.query_face_detection_id '
        'WHERE fss.contextual_distance IS NOT NULL '
        '  AND fd.person_id IS NULL '
        'ORDER BY fd.file_id, fss.contextual_distance ASC'
    )

    by_file: Dict[int, List[sqlite3.Row]] = defaultdict(list)
    for row in cursor.fetchall():
        by_file[row['file_id']].append(row)

    assigned_ids = []
    for _file_id, rows in by_file.items():
        taken_faces, taken_people = set(), set()
        for row in sorted(rows, key=lambda r: r['contextual_distance']):
            p = 1.0 - row['contextual_distance']
            if p < ASSIGNMENT_MIN_P:
                break                      # rows are sorted; nothing better follows
            if row['face_id'] in taken_faces or row['person_id'] in taken_people:
                continue
            taken_faces.add(row['face_id'])
            taken_people.add(row['person_id'])
            assigned_ids.append(row['id'])

    if assigned_ids:
        cursor.executemany(
            'UPDATE face_similarity_scores SET contextual_assigned = 1 WHERE id = ?',
            [(i,) for i in assigned_ids])
    conn.commit()
    conn.close()
    return len(assigned_ids)


# ─── Auto-confirm (Phase 4) ───────────────────────────────────────────────────

def auto_confirm(catalog_db_path: str, execution_id: Optional[int]) -> Dict:
    """
    Assign identities without human review, but only where the evidence is
    overwhelming and unambiguous.

    Every one of these must hold before a face is tagged:
      1. p >= AUTO_CONFIRM_MIN_P                 - the model is near-certain
      2. p(best) - p(second) >= MIN_MARGIN       - and not torn between two people
      3. at least one contextual signal fired    - never on appearance alone
      4. the row survived constraints C1/C2      - guaranteed by contextual_distance
      5. it won the per-photo assignment (C3)    - one person per photo
      6. AUTO_CONFIRM_ENABLED is True            - a deliberate operator decision

    Faces are marked source='Auto-Confirmed', NOT 'Manual'. That single choice is
    what breaks the error-amplification loop: reference extraction selects
    source='Manual', so an auto-tag can never become a reference for future
    auto-tagging. Only a human confirming it promotes it to 'Manual', and only then
    does it start teaching the model.

    Every assignment is recorded in auto_tag_audit with its full evidence, so any
    run can be reviewed and reverted wholesale.
    """
    if not AUTO_CONFIRM_ENABLED:
        return {'auto_confirmed': 0, 'enabled': False}

    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Candidates: assignment winners on faces that are still unidentified
    cursor.execute(
        "SELECT fss.id, fss.query_face_detection_id AS face_id, fss.person_id, "
        "       fss.contextual_distance, fss.contextual_evidence "
        "FROM face_similarity_scores fss "
        "INNER JOIN face_detections fd ON fd.id = fss.query_face_detection_id "
        "WHERE fss.contextual_assigned = 1 "
        "  AND fss.contextual_distance IS NOT NULL "
        "  AND fd.person_id IS NULL "
        "  AND fd.source = 'Automatic'"
    )
    winners = cursor.fetchall()

    confirmed = 0
    now_iso = datetime.now().isoformat()

    for row in winners:
        p = 1.0 - row['contextual_distance']
        if p < AUTO_CONFIRM_MIN_P:
            continue

        try:
            evidence = json.loads(row['contextual_evidence'] or '{}')
        except (TypeError, ValueError):
            continue

        # Gate 3 - context must have contributed something
        if not evidence.get('terms'):
            continue

        # Gate 2 - clear daylight to the runner-up for this face
        cursor.execute(
            'SELECT MIN(contextual_distance) FROM face_similarity_scores '
            'WHERE query_face_detection_id = ? AND person_id != ? '
            '  AND contextual_distance IS NOT NULL',
            (row['face_id'], row['person_id'])
        )
        runner_up = cursor.fetchone()[0]
        if runner_up is not None:
            if p - (1.0 - runner_up) < AUTO_CONFIRM_MIN_MARGIN:
                continue

        cursor.execute(
            "UPDATE face_detections SET person_id = ?, source = 'Auto-Confirmed' "
            "WHERE id = ? AND person_id IS NULL",
            (row['person_id'], row['face_id'])
        )
        if cursor.rowcount == 0:
            continue

        cursor.execute(
            'INSERT INTO auto_tag_audit '
            '(execution_id, face_detection_id, person_id, contextual_distance, '
            ' confidence, evidence, action, created_at) '
            "VALUES (?, ?, ?, ?, ?, ?, 'auto_confirmed', ?)",
            (execution_id, row['face_id'], row['person_id'],
             row['contextual_distance'], p, row['contextual_evidence'], now_iso)
        )
        confirmed += 1

    conn.commit()
    conn.close()
    return {'auto_confirmed': confirmed, 'enabled': True}


def revert_auto_tags(catalog_db_path: str, execution_id: Optional[int] = None,
                     face_detection_id: Optional[int] = None,
                     reverted_by: str = 'user') -> int:
    """
    Undo auto-confirmations, restoring each face to person_id = NULL and
    source = 'Automatic'.

    Scope by execution_id (undo a whole run) or face_detection_id (undo one).
    Only rows still marked 'Auto-Confirmed' are touched, so a face a human has
    since confirmed is never silently un-confirmed.

    Returns the number of faces reverted.
    """
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    where, params = [], []
    if execution_id is not None:
        where.append('ata.execution_id = ?')
        params.append(execution_id)
    if face_detection_id is not None:
        where.append('ata.face_detection_id = ?')
        params.append(face_detection_id)
    if not where:
        conn.close()
        raise ValueError('revert_auto_tags requires execution_id or face_detection_id')

    cursor.execute(
        'SELECT ata.id, ata.face_detection_id, ata.person_id '
        'FROM auto_tag_audit ata '
        'INNER JOIN face_detections fd ON fd.id = ata.face_detection_id '
        "WHERE ata.action = 'auto_confirmed' AND ata.reverted_at IS NULL "
        "  AND fd.source = 'Auto-Confirmed' AND " + ' AND '.join(where),
        params
    )
    rows = cursor.fetchall()

    now_iso = datetime.now().isoformat()
    for row in rows:
        cursor.execute(
            "UPDATE face_detections SET person_id = NULL, source = 'Automatic' "
            "WHERE id = ? AND source = 'Auto-Confirmed'",
            (row['face_detection_id'],)
        )
        cursor.execute(
            'UPDATE auto_tag_audit SET reverted_at = ?, reverted_by = ? WHERE id = ?',
            (now_iso, reverted_by, row['id'])
        )
        cursor.execute(
            'INSERT INTO auto_tag_audit '
            '(execution_id, face_detection_id, person_id, contextual_distance, '
            ' confidence, evidence, action, created_at) '
            "SELECT execution_id, face_detection_id, person_id, contextual_distance, "
            "       confidence, evidence, 'reverted', ? "
            'FROM auto_tag_audit WHERE id = ?',
            (now_iso, row['id'])
        )

    conn.commit()
    conn.close()
    return len(rows)


# ─── Main execute ─────────────────────────────────────────────────────────────

def execute(source_dir, target_dir, files, progress_callback,
            catalog_id=None, catalog=None, execution_id=None):
    """
    Re-score every existing face_similarity_scores row with the contextual model.

    Pipeline (spec §6.3):
      1. ensure schema
      2. rebuild the person co-occurrence graph
      3. load all relational context into memory in one pass
      4. for each unidentified face: apply hard constraints C1/C2, then fuse
         appearance + context for each surviving candidate
      5. write contextual_distance + contextual_evidence back

    Makes no DeepFace calls. Prerequisites: ai_face_recognition_workflow (for the
    cosine distances this re-scores) and ev_extract_events (for the event signal).

    Returns statistics dict.
    """
    if catalog is None and catalog_id is not None:
        catalog = Catalog.get_by_id(catalog_id)
        if catalog is None:
            raise ValueError(f'Catalog with ID {catalog_id} not found')
    if catalog is None:
        raise ValueError('Either catalog or catalog_id must be provided')

    catalog_db_path = os.path.join(target_dir, 'workflow_results.db')

    global _stop_check
    _stop_check = ((lambda: stop_requested(catalog_db_path, execution_id))
                   if execution_id else (lambda: False))
    progress = {'done': 0, 'total': 0}      # for the stopped message
    try:
        return _execute(catalog, catalog_db_path, target_dir, progress_callback,
                        execution_id, progress)
    except _Stopped as stop:
        message = (f'Stopped by request while {stop.where}, after '
                   f"{progress['done']:,} of {progress['total']:,} face(s). Faces "
                   f'already re-ranked keep their new scores and the rest keep the '
                   f'previous run\'s; per-photo assignments and the groups on '
                   f'Identify groups are as the last complete run left them. Running '
                   f'it again re-ranks everything.')
        print(f'\n{message}')
        if execution_id:
            record_stopped(catalog_db_path, execution_id,
                           processed_files=progress['done'], message=message)
        return {'faces_scored': progress['done'], 'stopped': True,
                'faces_skipped': 0, 'faces_failed': 0}
    finally:
        _stop_check = lambda: False  # noqa: E731


def _execute(catalog, catalog_db_path, target_dir, progress_callback, execution_id,
             progress):
    """execute(), with a stop at any _check_stop() raising _Stopped."""
    print('Ensuring contextual schema...')
    ensure_schema(catalog_db_path)

    global ACTIVE_MODEL, ACTIVE_MODEL_ID
    ACTIVE_MODEL = embedding_models.active_model(catalog_db_path)
    ACTIVE_MODEL_ID = embedding_models.model_id(ACTIVE_MODEL)
    print(f'Embedding model: {ACTIVE_MODEL} '
          f'({embedding_models.dims(ACTIVE_MODEL)}-d, id {ACTIVE_MODEL_ID})')

    # ── Preconditions: fail loudly rather than silently scoring without context ─
    has_candidates = _has_candidate_rows(catalog_db_path)
    if not has_candidates and not _has_confirmed_faces(catalog_db_path):
        # Nothing to re-score: Face Recognition compares unknown faces with
        # CONFIRMED ones, and nobody has confirmed any yet, so it rightly found
        # no candidates. That is a catalog nobody has started tagging, not a
        # workflow run out of order - and it is exactly when clustering helps
        # most, since one confirmation can then label a whole group. Clustering
        # needs only the embeddings, so it runs; the run succeeds.
        print('No confirmed faces yet, so there are no candidates to re-score.')
        print('Clustering unidentified faces...')
        cluster_stats = build_face_clusters(catalog_db_path, progress_callback)
        return {
            'faces_scored': 0, 'scores_updated': 0, 'candidates_eliminated': 0,
            'cooccurrence_edges': 0, 'perceptual_hashes_added': 0,
            'near_duplicate_pairs': 0, 'assignments': 0, 'auto_confirmed': 0,
            'clusters_formed': cluster_stats['clusters'],
            'clustered_faces': cluster_stats['clustered_faces'],
            'faces_skipped': 0, 'faces_failed': 0,
        }
    if not has_candidates:
        raise ValueError(
            'No rows in face_similarity_scores. Run the Face Recognition workflow '
            '(ai_face_recognition_workflow.py) first — this workflow re-scores the '
            'candidates it produces, it does not generate them.'
        )

    print('Building person co-occurrence graph...')
    cooc = build_cooccurrence_graph(catalog_db_path)
    print(f'  {len(cooc) // 2} co-occurrence edge(s)')
    _check_stop('building the co-occurrence graph')

    print('Backfilling perceptual hashes (incremental)...')
    hash_stats = backfill_perceptual_hashes(catalog_db_path, target_dir, progress_callback)
    print(f"  {hash_stats['hashed']} hashed, {hash_stats['skipped']} skipped "
          f"(missing or unreadable file), {hash_stats['no_derivative_yet']} "
          f"waiting for a derivative (video, RAW or HEIC)")

    _check_stop('filling in perceptual hashes')
    print('Loading context indexes...')
    ctx = load_context_indexes(catalog_db_path)
    n_events = len({f['event_id'] for f in ctx['files'].values()
                    if f.get('event_id') is not None})
    print(f"  {len(ctx['files'])} file(s), {len(ctx['people'])} person(s), "
          f"{n_events} event(s) with files")
    if n_events == 0:
        print('  WARNING: no events found — the event signal will never fire. '
              'Run the Create Events workflow (ev_extract_events.py) for full context.')

    print('Building near-duplicate index...')
    near_dup = build_near_duplicate_index(ctx)
    print(f'  {sum(len(v) for v in near_dup.values()) // 2} near-duplicate pair(s) '
          f'within {NEAR_DUP_WINDOW_MIN} min at Hamming <= {NEAR_DUP_MAX_HAMMING}')

    query_faces = load_query_faces(catalog_db_path)
    face_ids = [fid for fid in _faces_with_candidates(catalog_db_path) if fid in query_faces]
    total = len(face_ids)
    print(f'{total} unidentified face(s) with candidates to score')
    progress['total'] = total

    if total == 0:
        return {'faces_scored': 0, 'scores_updated': 0, 'candidates_eliminated': 0,
                'cooccurrence_edges': len(cooc) // 2, 'faces_skipped': 0, 'faces_failed': 0}

    if progress_callback:
        progress_callback(0, total)

    conn = sqlite3.connect(catalog_db_path, timeout=30)
    _wal_pragmas(conn, 30000)
    conn.row_factory = sqlite3.Row

    faces_scored = 0
    scores_updated = 0
    unchanged = 0
    eliminated = 0
    failed = 0
    now_iso = datetime.now().isoformat()

    # A CHUNK OF FACES AT A TIME, and only what changed is written.
    #
    # Every score row - 12.2 M on `family` - used to be loaded at the start
    # (44 s, 6.7 GB) and every one rewritten with its evidence on every run,
    # ~4.7 GB, whether or not anything about it had changed. Now a chunk's rows
    # are read with their current contextual values; a row whose new distance
    # and evidence are the same as the stored ones is left alone, audit row
    # and all; and the rest are written in one transaction per chunk, with
    # progress reported after the commit.
    for start in range(0, total, CHUNK):
        if _stop_check():
            conn.close()
            raise _Stopped('re-ranking faces')
        chunk = face_ids[start:start + CHUNK]
        marks = ','.join('?' * len(chunk))
        candidates_by_face: Dict[int, List[Dict]] = defaultdict(list)
        for row in conn.execute(f'''
                SELECT id, query_face_detection_id, reference_face_detection_id,
                       person_id, distance, contextual_distance, contextual_evidence
                  FROM face_similarity_scores
                 WHERE query_face_detection_id IN ({marks}) AND distance IS NOT NULL
                 ORDER BY query_face_detection_id, id''', chunk):
            candidates_by_face[row['query_face_detection_id']].append(dict(row))

        updates, audits = [], []
        for face_id in chunk:
            try:
                face = query_faces[face_id]
                file_info = ctx['files'].get(face['file_id'], {})
                file_confirmed = ctx['confirmed_in_file'].get(face['file_id'], set())
                query_dt = file_info.get('datetime')

                for cand in candidates_by_face[face_id]:
                    person_id = cand['person_id']
                    dob = ctx['people'].get(person_id, {}).get('dob')

                    # ── Constraint C1 — same-image exclusion ──────────────────
                    # This person is already confirmed on a DIFFERENT face in
                    # this photo, so this face cannot also be them.
                    if person_id in file_confirmed:
                        distance, evidence = 1.0, json.dumps({
                            'weights_version': WEIGHTS_VERSION,
                            'eliminated_by': 'C1',
                            'reason': 'Person is already confirmed on another face in this photo',
                            'p': 0.0,
                        })
                        audit = ('contextual_c1_same_image',
                                 'C1 same-image exclusion: person already confirmed on another '
                                 'face in the same photo')
                    # ── Constraint C2 — photo predates the person's birth ─────
                    elif dob and query_dt and query_dt < dob:
                        distance, evidence = 1.0, json.dumps({
                            'weights_version': WEIGHTS_VERSION,
                            'eliminated_by': 'C2',
                            'reason': f'Photo dated {query_dt.date()} predates date of birth '
                                      f'{dob.date()}',
                            'p': 0.0,
                        })
                        audit = ('contextual_c2_before_birth',
                                 f'C2 before-birth exclusion: photo {query_dt.date()} predates '
                                 f'date of birth {dob.date()}')
                    # ── Fuse appearance + context ────────────────────────────
                    else:
                        result = score_candidate(face, cand, ctx, cooc, near_dup)
                        distance, evidence = result['distance'], json.dumps(result['evidence'])
                        audit = None

                    if audit:
                        eliminated += 1
                    if (cand['contextual_distance'] == distance
                            and cand['contextual_evidence'] == evidence):
                        unchanged += 1
                        continue
                    updates.append((distance, evidence, now_iso, cand['id']))
                    if audit:
                        audits.append((execution_id, audit[0], 'update', face_id, cand['id'],
                                       person_id, cand['reference_face_detection_id'],
                                       face['file_id'], file_info.get('event_id'),
                                       cand['distance'], 1.0, audit[1], None))

                faces_scored += 1
            except Exception as exc:
                failed += 1
                print(f'  ERROR scoring face {face_id}: {exc}')

        with conn:
            conn.executemany('''
                UPDATE face_similarity_scores
                   SET contextual_distance = ?,
                       contextual_evidence = ?,
                       contextual_computed_at = ?
                 WHERE id = ?''', updates)
            conn.executemany('''
                INSERT INTO face_similarity_scores_audit (
                    execution_id, rule_name, action_type, source_face_detection_id,
                    face_similarity_score_id, candidate_person_id,
                    reference_face_detection_id, source_file_id, source_event_id,
                    previous_distance, new_distance, reason, metadata
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', audits)
        scores_updated += len(updates)
        done = start + len(chunk)
        progress['done'] = done
        print(f'  {done:,} of {total:,} face(s): {scores_updated:,} row(s) written, '
              f'{unchanged:,} unchanged')
        if progress_callback:
            progress_callback(done, total)

    conn.close()

    # ── Phase 2, step 8: resolve C3 (one person per photo) ───────────────────
    _check_stop('re-ranking faces')
    print('\nResolving per-photo assignment (constraint C3)...')
    assignments = resolve_per_photo_assignment(catalog_db_path)
    print(f'  {assignments} assignment(s) made')

    # ── Phase 2: cluster the remaining unidentified faces ────────────────────
    _check_stop('resolving per-photo assignments')
    print('Clustering unidentified faces...')
    cluster_stats = build_face_clusters(catalog_db_path, progress_callback)

    # ── Phase 4, step 9: auto-confirm (off unless explicitly enabled) ────────
    auto_stats = auto_confirm(catalog_db_path, execution_id)
    if auto_stats['enabled']:
        print(f'Auto-confirm: {auto_stats["auto_confirmed"]} face(s) tagged')
    else:
        print('Auto-confirm: disabled (AUTO_CONFIRM_ENABLED = False)')

    print(f'\n{"=" * 60}')
    print('Contextual recognition complete!')
    print(f'  Faces scored:            {faces_scored}')
    print(f'  Score rows updated:      {scores_updated}  ({unchanged} unchanged, left alone)')
    print(f'  Candidates eliminated:   {eliminated}  (constraints C1/C2)')
    print(f'  Co-occurrence edges:     {len(cooc) // 2}')
    print(f'  Perceptual hashes added: {hash_stats["hashed"]}')
    print(f'  Near-duplicate pairs:    {sum(len(v) for v in near_dup.values()) // 2}')
    print(f'  Assignments (C3):        {assignments}')
    print(f'  Clusters formed:         {cluster_stats["clusters"]} '
          f'covering {cluster_stats["clustered_faces"]} face(s)')
    print(f'  Faces failed:            {failed}')
    print(f'{"=" * 60}')

    return {
        'faces_scored': faces_scored,
        'scores_updated': scores_updated,
        'candidates_eliminated': eliminated,
        'cooccurrence_edges': len(cooc) // 2,
        'perceptual_hashes_added': hash_stats['hashed'],
        'near_duplicate_pairs': sum(len(v) for v in near_dup.values()) // 2,
        'assignments': assignments,
        'auto_confirmed': auto_stats['auto_confirmed'],
        'clusters_formed': cluster_stats['clusters'],
        'clustered_faces': cluster_stats['clustered_faces'],
        'faces_skipped': 0,
        'faces_failed': failed,
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

    parser = argparse.ArgumentParser(
        description='Contextual face recognition workflow (subprocess mode)')
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
        if isinstance(result, dict) and result.get('stopped'):
            # execute() already wrote 'stopped'; 'completed' here would hide it.
            print(f'Subprocess execution {args.execution_id} stopped by request.')
        else:
            _db_complete(catalog_db_path, args.execution_id, 'completed',
                         skipped_files=result.get('faces_skipped', 0)
                         if isinstance(result, dict) else 0)
            print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)
