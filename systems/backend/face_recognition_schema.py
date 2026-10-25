#!/usr/bin/env python3
"""
The single definition of the face-recognition schema.

This exists because there were three.

`face_similarity_scores` was created independently by models.py (new catalogs),
by ai_face_recognition_workflow.py (CREATE TABLE IF NOT EXISTS at run time) and
by migrate_add_face_similarity_scores.py - and they had drifted. models.py had
`euclidean_distance`; the workflow did not, so a catalog whose table happened to
be created by a workflow run could never store a euclidean distance, silently
and permanently. None of the three had the contextual columns, which arrived
only through a migration, so every new catalog started life missing them.

Whichever path runs first now produces the same schema, and adding a column
means editing one list.

`ensure_face_recognition_schema()` both creates and upgrades: it creates
anything missing and ALTERs in any column a pre-existing table lacks. It is
idempotent and safe to call on every startup, every workflow run and every
migration.
"""

import sqlite3

from face_best_match import ensure_best_match_schema

# The registry is the source of truth for ids and dimensions; the table below is
# seeded from it. Imported defensively so that a catalog can still be opened if
# this module is somehow unavailable - the schema matters more than the seed.
try:
    from embedding_models import MODELS as _EMBEDDING_MODELS
except Exception:            # pragma: no cover - import-path accidents only
    _EMBEDDING_MODELS = {'VGG-Face': {'id': 1, 'dims': 4096},
                         'SFace':    {'id': 2, 'dims': 128}}


# Ordered (name, declaration) for face_similarity_scores. Append here - never in
# a migration script - when the recognition pipeline gains a column.
FACE_SIMILARITY_COLUMNS = (
    ('id',                          'INTEGER PRIMARY KEY AUTOINCREMENT'),
    ('query_face_detection_id',     'INTEGER NOT NULL'),
    ('reference_face_detection_id', 'INTEGER NOT NULL'),
    ('person_id',                   'INTEGER NOT NULL'),
    # Cosine distance: the primary appearance metric.
    ('distance',                    'REAL NOT NULL'),
    # Euclidean L2 distance: secondary appearance metric, NULL until computed.
    ('euclidean_distance',          'REAL'),
    ('model',                       "TEXT NOT NULL DEFAULT 'VGG-Face'"),
    ('distance_metric',             "TEXT NOT NULL DEFAULT 'cosine'"),
    ('verified',                    'INTEGER NOT NULL DEFAULT 0'),
    ('threshold',                   'REAL NOT NULL'),
    ('computed_at',                 'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
    # Contextual metric: appearance fused with event, time and co-occurrence
    # evidence. Stored as 1 - p so it shares the lower-is-closer convention.
    ('contextual_distance',         'REAL'),
    ('contextual_evidence',         'TEXT'),
    ('contextual_computed_at',      'TIMESTAMP'),
    ('contextual_assigned',         'INTEGER'),
)

# Columns a pre-existing table may lack. Anything without a NOT NULL and no
# default cannot be added by ALTER TABLE, so only nullable columns belong here -
# which is every column added after the table's original definition.
_ADDABLE = {name for name, decl in FACE_SIMILARITY_COLUMNS
            if 'NOT NULL' not in decl and 'PRIMARY KEY' not in decl}

_FSS_FOREIGN_KEYS = '''
                FOREIGN KEY (query_face_detection_id)     REFERENCES face_detections (id) ON DELETE CASCADE,
                FOREIGN KEY (reference_face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
                FOREIGN KEY (person_id)                   REFERENCES people (id)          ON DELETE CASCADE,
                UNIQUE (query_face_detection_id, reference_face_detection_id)'''


def _table_exists(cur, name):
    cur.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,))
    return cur.fetchone() is not None


def _columns(cur, table):
    cur.execute(f'PRAGMA table_info({table})')
    return {row[1] for row in cur.fetchall()}


def ensure_face_recognition_schema(db, verbose=False):
    """
    Create or upgrade every table the face-recognition pipeline needs.

    `db` is a path or an open sqlite3.Connection. A connection passed in is left
    open and uncommitted-to by this function's caller's choice - it commits, but
    does not close what it did not open.

    Returns a list of the changes made, empty when everything was already there.
    """
    owns = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(db, timeout=60) if owns else db
    cur = conn.cursor()
    changed = []

    def note(msg):
        changed.append(msg)
        if verbose:
            print(f'  + {msg}')

    # ── face_similarity_scores ────────────────────────────────────────────
    if not _table_exists(cur, 'face_similarity_scores'):
        cols = ',\n                '.join(f'{n} {d}' for n, d in FACE_SIMILARITY_COLUMNS)
        cur.execute(f'''
            CREATE TABLE face_similarity_scores (
                {cols},{_FSS_FOREIGN_KEYS}
            )
        ''')
        note('face_similarity_scores created')
    else:
        have = _columns(cur, 'face_similarity_scores')
        for name, decl in FACE_SIMILARITY_COLUMNS:
            if name in have or name not in _ADDABLE:
                continue
            try:
                cur.execute(f'ALTER TABLE face_similarity_scores ADD COLUMN {name} {decl}')
                note(f'face_similarity_scores.{name} added')
            except Exception as e:
                changed.append(f'face_similarity_scores.{name} COULD NOT be added: {e}')
                if verbose:
                    print(f'  ! {changed[-1]}')

    for idx, cols in (('idx_fss_query_face', 'query_face_detection_id'),
                      ('idx_fss_person',     'person_id'),
                      ('idx_fss_distance',   'distance'),
                      ('idx_fss_contextual', 'contextual_distance')):
        cur.execute(f'CREATE INDEX IF NOT EXISTS {idx} ON face_similarity_scores({cols})')

    # ── face_best_match ───────────────────────────────────────────────────
    # Each waiting face's best match, stored, with the triggers that keep it
    # current. See face_best_match.py for why.
    for msg in ensure_best_match_schema(cur):
        note(msg)

    # ── face_similarity_scores_audit ──────────────────────────────────────
    cur.execute('''
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
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (execution_id)                REFERENCES workflow_executions (id)     ON DELETE SET NULL,
            FOREIGN KEY (source_face_detection_id)    REFERENCES face_detections (id)         ON DELETE CASCADE,
            FOREIGN KEY (face_similarity_score_id)    REFERENCES face_similarity_scores (id)  ON DELETE SET NULL,
            FOREIGN KEY (candidate_person_id)         REFERENCES people (id)                  ON DELETE SET NULL,
            FOREIGN KEY (reference_face_detection_id) REFERENCES face_detections (id)         ON DELETE SET NULL,
            FOREIGN KEY (source_file_id)              REFERENCES file_contents (id)           ON DELETE SET NULL,
            FOREIGN KEY (source_event_id)             REFERENCES events (id)                  ON DELETE SET NULL
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_fssa_source_face ON face_similarity_scores_audit(source_face_detection_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_fssa_execution   ON face_similarity_scores_audit(execution_id)')

    # ── person_cooccurrence ───────────────────────────────────────────────
    cur.execute('''
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
    cur.execute('CREATE INDEX IF NOT EXISTS idx_pcooc_a ON person_cooccurrence(person_a_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_pcooc_b ON person_cooccurrence(person_b_id)')

    # ── face clusters ─────────────────────────────────────────────────────
    cur.execute('''
        CREATE TABLE IF NOT EXISTS face_clusters (
            id                  INTEGER PRIMARY KEY AUTOINCREMENT,
            centroid            TEXT    NOT NULL,
            member_count        INTEGER NOT NULL DEFAULT 0,
            cohesion            REAL,
            max_member_distance REAL,
            resolved_person_id  INTEGER,
            resolved_at         TIMESTAMP,
            created_at          TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (resolved_person_id) REFERENCES people (id) ON DELETE SET NULL
        )
    ''')
    cur.execute('''
        CREATE TABLE IF NOT EXISTS face_cluster_members (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            cluster_id           INTEGER NOT NULL,
            face_detection_id    INTEGER NOT NULL UNIQUE,
            distance_to_centroid REAL NOT NULL,
            created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (cluster_id)        REFERENCES face_clusters (id)   ON DELETE CASCADE,
            FOREIGN KEY (face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_fcm_cluster ON face_cluster_members(cluster_id)')

    # ── auto-confirm audit trail ──────────────────────────────────────────
    cur.execute('''
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
            FOREIGN KEY (person_id)         REFERENCES people (id)          ON DELETE CASCADE
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ata_face ON auto_tag_audit(face_detection_id)')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_ata_exec ON auto_tag_audit(execution_id)')

    # ── perceptual_hash on file_contents ──────────────────────────────────
    # Guarded: on a brand-new catalog this may run before file_contents exists,
    # depending on the order models.py builds the schema in.
    if _table_exists(cur, 'file_contents'):
        if 'perceptual_hash' not in _columns(cur, 'file_contents'):
            try:
                cur.execute('ALTER TABLE file_contents ADD COLUMN perceptual_hash TEXT')
                note('file_contents.perceptual_hash added')
            except Exception as e:
                changed.append(f'file_contents.perceptual_hash COULD NOT be added: {e}')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_fc_phash ON file_contents(perceptual_hash)')

    # ── embedding provenance ──────────────────────────────────────────────
    # Which model produced each stored embedding. See embedding_models.py for
    # why, and documentation/specs/face-recognition-migration-spec.md for the
    # migration this enables.
    #
    # The id/name/dims are seeded from embedding_models.MODELS so the table and
    # the code cannot disagree. The table exists so a human reading the
    # database can tell what a 2 means, and so the FK has something to point
    # at - the application reads the registry, not these rows.
    cur.execute('''
        CREATE TABLE IF NOT EXISTS embedding_models (
            id   INTEGER PRIMARY KEY,
            name TEXT    NOT NULL UNIQUE,
            dims INTEGER NOT NULL
        )
    ''')
    for _name, _spec in sorted(_EMBEDDING_MODELS.items(), key=lambda kv: kv[1]['id']):
        cur.execute('INSERT INTO embedding_models (id, name, dims) VALUES (?, ?, ?) '
                    'ON CONFLICT(id) DO UPDATE SET name = excluded.name, '
                    '                              dims = excluded.dims',
                    (_spec['id'], _name, _spec['dims']))

    # NULL means the legacy model (id 1). Deliberately not backfilled: NULL
    # occupies no space in a SQLite record, so existing rows cost nothing AND
    # are never rewritten - which matters more than the bytes on a 24 GB
    # database sitting on a disk with uncorrectable read errors.
    for _table in ('ai_face_contents', 'face_contents'):
        if not _table_exists(cur, _table):
            continue
        if 'embedding_model_id' not in _columns(cur, _table):
            try:
                cur.execute(f'ALTER TABLE {_table} ADD COLUMN embedding_model_id '
                            f'INTEGER REFERENCES embedding_models(id)')
                note(f'{_table}.embedding_model_id added')
            except Exception as e:
                changed.append(f'{_table}.embedding_model_id COULD NOT be added: {e}')
                if verbose:
                    print(f'  ! {changed[-1]}')
        cur.execute(f'CREATE INDEX IF NOT EXISTS idx_{_table[:12]}_emb_model '
                    f'ON {_table}(embedding_model_id)')

    # The view every reader uses, so that no query anywhere has to remember
    # what NULL means. Dropped and recreated rather than IF NOT EXISTS: a view
    # holds no data, and this way a changed definition actually takes effect
    # instead of silently keeping the old one.
    if _table_exists(cur, 'ai_face_contents'):
        cur.execute('DROP VIEW IF EXISTS face_embeddings')
        cur.execute('''
            CREATE VIEW face_embeddings AS
                SELECT id,
                       face_detection_id,
                       source_file_id,
                       file_path,
                       embedding,
                       COALESCE(embedding_model_id, 1) AS embedding_model_id
                  FROM ai_face_contents
                 WHERE embedding IS NOT NULL
        ''')

    conn.commit()
    if owns:
        conn.close()
    return changed


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('usage: face_recognition_schema.py <workflow_results.db> [...]')
        sys.exit(2)
    for path in sys.argv[1:]:
        print(f'{path}:')
        made = ensure_face_recognition_schema(path, verbose=True)
        if not made:
            print('  already up to date')
