#!/usr/bin/env python3
"""
The single definition of the pet-recognition schema.

Written as one module for the reason face_recognition_schema.py exists: there
were three definitions of `face_similarity_scores` and they had drifted, so a
catalog whose table happened to be created by a workflow run was permanently
missing a column that models.py knew about. These tables have four paths that
can each be the first to touch a catalog database - models.py on open, the
detection ride-along in Auto Detect Faces, pe_identify_pets.py and
pe_cluster_pets.py - so one list here, and adding a column means editing one
list.

`ensure_pet_schema()` both creates and upgrades. It is idempotent and safe to
call on every startup and every workflow run.

See documentation/specs/pet-recognition-spec.md section 3.

WHY THE TABLES ARE SEPARATE FROM THE FACE ONES
----------------------------------------------
Not for tidiness, and not to keep pets out of the family: a pet IS a `people`
row (§6), in the same category as its owners, and the Pets page is the People
page with a different filter.

They are separate because the EMBEDDINGS ARE NOT COMPARABLE. A pet vector comes
from a 512-d animal-identification model and a face vector from VGG-Face or
SFace; a cosine distance between them is not a wrong number, it is not a number.
Keeping them in different tables means the two can never meet in one similarity
query by accident.

It also buys the isolation that keeps human recognition clean, as a side effect.
Every query that builds the face reference set starts `FROM face_detections` and
joins outward, so a pet with no rows there cannot enter it - not because a filter
excludes it, but because there is nothing to join to.
"""

import sqlite3


# ── pet_detections ───────────────────────────────────────────────────────────
# Mirrors face_detections column for column, because the crop and rescale code
# copied alongside it depends on that shape.
#
# `person_id`, NOT `pet_id`. It is a foreign key to people.id exactly as its
# counterpart is, and the `subject_appearances` view below only works if both
# columns mean the same thing. Every reviewer wants to rename this; the answer
# is that the view is the point.
#
# `image_width` / `image_height` are NOT optional. Boxes arrive in the detection
# image's coordinate space - 1600 px by default, not the original's - and the
# crop has to rescale from them. face_detections carries these two for exactly
# this reason. Omitting them produces crops that are subtly offset, which is the
# failure documentation/fixes/ already has nine files about.
PET_DETECTION_COLUMNS = (
    ('id',           'INTEGER PRIMARY KEY AUTOINCREMENT'),
    ('file_id',      'INTEGER NOT NULL'),
    ('x',            'INTEGER NOT NULL'),
    ('y',            'INTEGER NOT NULL'),
    ('w',            'INTEGER NOT NULL'),
    ('h',            'INTEGER NOT NULL'),
    ('confidence',   'REAL'),
    ('person_id',    'INTEGER'),
    ('source',       "TEXT DEFAULT 'Automatic'"),
    ('species',      'TEXT'),
    ('image_width',  'INTEGER'),
    ('image_height', 'INTEGER'),
    ('created_at',   'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
)
_PET_DETECTION_FKS = '''
            ,
                FOREIGN KEY (file_id)   REFERENCES file_contents (id) ON DELETE CASCADE,
                FOREIGN KEY (person_id) REFERENCES people (id)        ON DELETE SET NULL'''


# ── ai_pet_contents ──────────────────────────────────────────────────────────
# The crop on disk and its embedding. Mirrors ai_face_contents, including the
# `embedding TEXT` that migrate_add_ai_face_embedding.py added to its
# counterpart.
#
# Embeddings are PERSISTED, not recomputed. A 512-d vector is about 2 KB of
# JSON; re-embedding every crop on each scoring run would make the identify
# pass cost what the detection pass costs, and the detection pass is the
# expensive one.
AI_PET_CONTENT_COLUMNS = (
    ('id',               'INTEGER PRIMARY KEY AUTOINCREMENT'),
    ('file_path',        'TEXT NOT NULL'),
    ('file_name',        'TEXT NOT NULL'),
    ('file_size',        'INTEGER'),
    ('sha256_hash',      'TEXT'),
    ('year',             'INTEGER'),
    ('month',            'INTEGER'),
    ('day',              'INTEGER'),
    ('pet_detection_id', 'INTEGER NOT NULL UNIQUE'),
    ('source_file_id',   'INTEGER NOT NULL'),
    ('embedding',        'TEXT'),
    ('created_at',       'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
)
_AI_PET_CONTENT_FKS = '''
            ,
                FOREIGN KEY (pet_detection_id) REFERENCES pet_detections (id) ON DELETE CASCADE,
                FOREIGN KEY (source_file_id)   REFERENCES file_contents (id)  ON DELETE CASCADE'''


# ── pet_similarity_scores ────────────────────────────────────────────────────
# Candidates, NOT decisions. pet_detections.person_id is written only by a
# person, or by the identify pass above a threshold somebody can see - the
# measurement-versus-judgement split location-spec.md section 4.1 draws.
PET_SIMILARITY_COLUMNS = (
    ('id',               'INTEGER PRIMARY KEY AUTOINCREMENT'),
    ('pet_detection_id', 'INTEGER NOT NULL'),
    ('person_id',        'INTEGER NOT NULL'),
    ('cosine_distance',  'REAL NOT NULL'),
    ('execution_id',     'INTEGER'),
    ('created_at',       'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
)
_PET_SIMILARITY_FKS = '''
            ,
                UNIQUE (pet_detection_id, person_id),
                FOREIGN KEY (pet_detection_id) REFERENCES pet_detections (id) ON DELETE CASCADE,
                FOREIGN KEY (person_id)        REFERENCES people (id)         ON DELETE CASCADE'''


# ── pet_clusters / pet_cluster_members ───────────────────────────────────────
# Mirrors face_clusters, the v4 group-recognition tables, including
# resolved_person_id as the promotion.
#
# A CLUSTER IS NOT A PET. No `people` row exists for an unnamed animal: a row in
# `people` means somebody told us who this is, and nothing else may create one.
# A detector that invented a row per unrecognised animal would fill the family
# with strangers - the pet form of the defect location-spec.md section 4.8
# exists to prevent, "never store a coordinate nobody measured".
PET_CLUSTER_COLUMNS = (
    ('id',                  'INTEGER PRIMARY KEY AUTOINCREMENT'),
    ('centroid',            'TEXT NOT NULL'),
    ('member_count',        'INTEGER NOT NULL DEFAULT 0'),
    ('cohesion',            'REAL'),
    ('max_member_distance', 'REAL'),
    ('species',             'TEXT'),
    ('resolved_person_id',  'INTEGER'),
    ('resolved_at',         'TIMESTAMP'),
    ('created_at',          'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
)
_PET_CLUSTER_FKS = '''
            ,
                FOREIGN KEY (resolved_person_id) REFERENCES people (id) ON DELETE SET NULL'''

PET_CLUSTER_MEMBER_COLUMNS = (
    ('id',                   'INTEGER PRIMARY KEY AUTOINCREMENT'),
    ('cluster_id',           'INTEGER NOT NULL'),
    ('pet_detection_id',     'INTEGER NOT NULL UNIQUE'),
    ('distance_to_centroid', 'REAL NOT NULL'),
    ('created_at',           'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
)
_PET_CLUSTER_MEMBER_FKS = '''
            ,
                FOREIGN KEY (cluster_id)       REFERENCES pet_clusters (id)   ON DELETE CASCADE,
                FOREIGN KEY (pet_detection_id) REFERENCES pet_detections (id) ON DELETE CASCADE'''


_INDEXES = (
    'CREATE INDEX IF NOT EXISTS idx_pet_det_file     ON pet_detections (file_id)',
    'CREATE INDEX IF NOT EXISTS idx_pet_det_person   ON pet_detections (person_id)',
    'CREATE INDEX IF NOT EXISTS idx_pet_det_species  ON pet_detections (species)',
    'CREATE INDEX IF NOT EXISTS idx_apc_detection    ON ai_pet_contents (pet_detection_id)',
    'CREATE INDEX IF NOT EXISTS idx_apc_source_file  ON ai_pet_contents (source_file_id)',
    'CREATE INDEX IF NOT EXISTS idx_pss_detection    ON pet_similarity_scores (pet_detection_id)',
    'CREATE INDEX IF NOT EXISTS idx_pss_person       ON pet_similarity_scores (person_id)',
    'CREATE INDEX IF NOT EXISTS idx_pcm_cluster      ON pet_cluster_members (cluster_id)',
    'CREATE INDEX IF NOT EXISTS idx_pet_clu_resolved ON pet_clusters (resolved_person_id)',
)


# ── people.kind / people.species ─────────────────────────────────────────────
# `category` is deliberately NOT touched. It is the household - "my pet can be
# part of my family, but my sister's cat can be part of her family" - and it
# already carries free text for humans. A pet takes a category exactly as a
# person does.
#
# So species needs its own column. An earlier draft of the spec asked whether a
# pet should be category 'Pets' or category 'Family'; the question conflated
# which household with what species, and the answer is that those are two
# different facts.
PEOPLE_COLUMNS = (
    # `kind` defaults to 'person' so every existing row is already correct and
    # no backfill is needed.
    ('kind',    "TEXT NOT NULL DEFAULT 'person'"),
    ('species', 'TEXT'),
)


def _table_exists(cur, name):
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _columns(cur, table):
    return {r[1] for r in cur.execute(f'PRAGMA table_info({table})')}


def _ensure_table(cur, name, columns, extra='', note=lambda m: None):
    if not _table_exists(cur, name):
        body = ',\n                '.join(f'{n} {d}' for n, d in columns)
        cur.execute(f'CREATE TABLE {name} (\n                {body}{extra}\n            )')
        note(f'created {name}')
        return
    have = _columns(cur, name)
    for col, decl in columns:
        if col in have:
            continue
        decl_add = decl.replace('PRIMARY KEY AUTOINCREMENT', '').replace(
            'PRIMARY KEY', '').replace('NOT NULL', '').replace('UNIQUE', '').strip()
        try:
            cur.execute(f'ALTER TABLE {name} ADD COLUMN {col} {decl_add}')
            note(f'added {name}.{col}')
        except sqlite3.Error:
            pass


def ensure_pet_schema(db, verbose=False):
    """
    Create or upgrade every table the pet feature needs.

    `db` is a path or an open sqlite3.Connection, matching
    ensure_face_recognition_schema and ensure_location_schema. A connection
    passed in is committed to but not closed - this does not close what it did
    not open.

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

    try:
        _ensure_table(cur, 'pet_detections', PET_DETECTION_COLUMNS,
                      _PET_DETECTION_FKS, note)
        _ensure_table(cur, 'ai_pet_contents', AI_PET_CONTENT_COLUMNS,
                      _AI_PET_CONTENT_FKS, note)
        _ensure_table(cur, 'pet_similarity_scores', PET_SIMILARITY_COLUMNS,
                      _PET_SIMILARITY_FKS, note)
        _ensure_table(cur, 'pet_clusters', PET_CLUSTER_COLUMNS,
                      _PET_CLUSTER_FKS, note)
        _ensure_table(cur, 'pet_cluster_members', PET_CLUSTER_MEMBER_COLUMNS,
                      _PET_CLUSTER_MEMBER_FKS, note)

        for stmt in _INDEXES:
            cur.execute(stmt)

        # ── people.kind / people.species ──────────────────────────────────
        # Guarded: on a brand-new catalog this can run before `people` exists,
        # depending on the order models.py builds the schema in.
        if _table_exists(cur, 'people'):
            have = _columns(cur, 'people')
            for col, decl in PEOPLE_COLUMNS:
                if col in have:
                    continue
                try:
                    cur.execute(f'ALTER TABLE people ADD COLUMN {col} {decl}')
                    note(f'added people.{col}')
                except sqlite3.Error as e:
                    changed.append(f'people.{col} COULD NOT be added: {e}')
                    if verbose:
                        print(f'  ! {changed[-1]}')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_people_kind ON people (kind)')

        # ── file_contents.pets_scanned_at ─────────────────────────────────
        # The pet pass needs its OWN watermark, not the face one.
        #
        # A file scanned for faces before pets were switched on has
        # face_detection_scanned_at set and has never been looked at for
        # animals. Sharing the column would mean those files are never
        # examined, which on an existing catalog is all of them - the feature
        # would appear to work and find nothing.
        #
        # Guarded: on a brand-new catalog this can run before file_contents
        # exists, depending on the order models.py builds the schema in.
        if _table_exists(cur, 'file_contents'):
            if 'pets_scanned_at' not in _columns(cur, 'file_contents'):
                try:
                    cur.execute('ALTER TABLE file_contents '
                                ' ADD COLUMN pets_scanned_at TIMESTAMP')
                    note('file_contents.pets_scanned_at added')
                except Exception as e:
                    changed.append(f'file_contents.pets_scanned_at COULD NOT '
                                   f'be added: {e}')
            cur.execute('CREATE INDEX IF NOT EXISTS idx_fc_pets_scanned '
                        ' ON file_contents (pets_scanned_at)')

        # ── subject_appearances ───────────────────────────────────────────
        # One surface to search: "photos with a pet or a person" as one query
        # rather than a UNION every caller has to remember - and album filters
        # are GENERATED, by a model, from the schema prompt in backend-albums.
        # Asking it to get a two-table union right every time, in a fragment
        # nobody reads, is how albums quietly miss half their photographs.
        #
        # A VIEW, not a table: it stores nothing, so the separation above
        # survives it and there is no second copy of the truth to drift.
        # `JOIN people` works through it because a pet IS a people row, which
        # makes "photos with Rex" and "photos with Izelpi" the same query.
        #
        # Identified detections only. An unknown has no identity to link TO,
        # and unknowns are met as groups on the review page rather than found
        # by searching for them one at a time.
        #
        # Recreated only when its definition has actually CHANGED, and this is
        # a correctness fix rather than an optimisation.
        #
        # It used to DROP then CREATE unconditionally - a view holds no data,
        # so that looked free, and it meant an edited definition took effect
        # instead of being silently kept. But ensure_pet_schema runs on EVERY
        # catalog open, and the backend is gunicorn with eight threads. Two
        # threads opening the same catalog at the same moment interleave:
        #
        #   thread A  DROP VIEW      thread B  DROP VIEW   (already gone, fine)
        #   thread A  CREATE VIEW    thread B  CREATE VIEW -> OperationalError:
        #                                       view subject_appearances
        #                                       already exists
        #
        # which failed the whole request, from a function whose job is to make
        # sure the schema is there. Comparing sqlite_master.sql first makes the
        # normal path a pure read with no write lock, and the catch covers the
        # narrow race that is left when the definition really did change.
        if _table_exists(cur, 'face_detections') and _table_exists(cur, 'pet_detections'):
            want = (
                "CREATE VIEW subject_appearances AS\n"
                "                    SELECT file_id, person_id, 'person' AS kind, id AS detection_id\n"
                "                      FROM face_detections WHERE person_id IS NOT NULL\n"
                "                    UNION ALL\n"
                "                    SELECT file_id, person_id, 'pet' AS kind, id AS detection_id\n"
                "                      FROM pet_detections  WHERE person_id IS NOT NULL"
            )
            row = cur.execute(
                "SELECT sql FROM sqlite_master "
                " WHERE type = 'view' AND name = 'subject_appearances'").fetchone()
            if (row[0] if row else None) != want:
                try:
                    cur.execute('DROP VIEW IF EXISTS subject_appearances')
                    cur.execute(want)
                    note('subject_appearances created')
                except sqlite3.OperationalError as e:
                    # Another connection won the race and created the same view
                    # from the same source. Nothing to do and nothing wrong.
                    if 'already exists' not in str(e):
                        raise

        conn.commit()
    finally:
        if owns:
            conn.close()
    return changed


if __name__ == '__main__':
    import sys
    if len(sys.argv) < 2:
        print('usage: pet_schema.py <workflow_results.db> [...]')
        sys.exit(2)
    for path in sys.argv[1:]:
        print(f'{path}:')
        changes = ensure_pet_schema(path, verbose=True)
        if not changes:
            print('  (already current)')
