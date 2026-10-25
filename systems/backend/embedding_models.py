#!/usr/bin/env python3
"""
The one place that knows which model produced a face embedding.

Before this module, 'VGG-Face' was a string literal in 28 places across 11
files and nothing recorded which model any stored embedding came from. That is
fine while there is exactly one model and nobody ever changes it. The moment a
second one appears, 4096-d vectors and 128-d vectors share a column and every
distance computed between them is a number that means nothing - with no error
anywhere, just quietly wrong similarity scores, wrong clusters, and a
confidence figure shown to a person.

So: embeddings carry the id of the model that made them, scoring only ever sees
one model's rows, and comparing across models raises instead of returning a
plausible-looking float.

See documentation/specs/face-recognition-migration-spec.md.

HOW A CATALOG CHOOSES
---------------------
`catalog_properties['embedding_model']`, read per run. Absent means VGG-Face,
because that is what every embedding written before this module came from. A
catalog migrates independently of its neighbours, which is what lets
second-catalog go first.

STORAGE
-------
`embedding_model_id INTEGER` on ai_face_contents and face_contents, with NULL
meaning id 1. NULL costs no bytes in a SQLite record and - more to the point -
needs no UPDATE over existing rows, which matters when the database is 24 GB
and sits on a disk with uncorrectable read errors. Readers use the
`face_embeddings` view, which COALESCEs the NULL away so no caller handles the
sentinel.
"""

import os
import sqlite3


CATALOG_PROPERTY = 'embedding_model'

# The model every embedding written before this module came from. NULL in
# embedding_model_id means this.
LEGACY_MODEL = 'VGG-Face'


# ── The registry ─────────────────────────────────────────────────────────────
#
# `id` is what goes in the database and must never be reused or renumbered.
# `dims` is the length the embedding must have, and is a cheap second check on
# top of the id - a 4096-float vector tagged as SFace is a bug worth catching.
#
# `match_distance` is an IDENTIFICATION threshold, not a verification one, and
# the difference is large. See systems/frontend/src/utils/similarity.js: 0.68 is
# DeepFace's tuned VGG-Face verification bar - given two faces, same person? -
# measured on balanced pairs. Identification takes the nearest of N people, a
# minimum over N draws, so its distances are far smaller by construction and a
# verification bar accepts nearly anything. At 0.68, 99.7% of strangers were
# confidently mislabelled on the Family catalog. 0.35 was measured there with
# documentation/tests/bench_threshold.py and holds that to 3.1%.
MODELS = {
    'VGG-Face': {
        'id': 1,
        'dims': 4096,
        # Measured. documentation/tests/bench_threshold.py, Family catalog,
        # each person held out: recall 95.1%, precision 99.6%.
        'match_distance': 0.35,
        'measured': True,
        # DeepFace's own tuned VERIFICATION bar for this model and cosine, used
        # only as a fallback when a /api/compare-multiple response omits one.
        # deepface/config/threshold.py reports 0.68 for VGG-Face.
        'verification_distance': 0.68,
        'licence': 'CC-BY, non-commercial research only',
    },
    'SFace': {
        'id': 2,
        'dims': 128,
        # PROVISIONAL - NOT MEASURED. Derived, so that it is at least the right
        # order of magnitude rather than a guess: DeepFace's SFace cosine
        # verification bar is ~0.593, and for VGG-Face the measured
        # identification bar came out at 0.35 against a 0.68 verification bar,
        # a ratio of 0.51. 0.593 * 0.51 ~= 0.30.
        #
        # Run bench_threshold.py on a catalog carrying SFace embeddings and
        # replace this, then set 'measured': True. Until then the API reports
        # it as provisional and the UI says so.
        'match_distance': 0.30,
        'measured': False,
        'verification_distance': 0.593,
        'licence': 'Apache 2.0',
    },
}

# id -> name, built from the above so the two can never disagree.
BY_ID = {spec['id']: name for name, spec in MODELS.items()}


class EmbeddingModelMismatch(Exception):
    """
    Two embeddings from different models were about to be compared.

    Raised rather than logged. A distance between a 4096-d VGG-Face vector and
    a 128-d SFace vector is not a wrong number, it is not a number - and the
    failure this whole module exists to prevent is the one that returns a
    plausible float instead of complaining.
    """


def model_id(name):
    """The integer stored in embedding_model_id for `name`."""
    try:
        return MODELS[name]['id']
    except KeyError:
        raise ValueError(f'unknown embedding model: {name!r}. '
                         f'Known: {", ".join(sorted(MODELS))}')


def model_name(mid):
    """The name for a stored id. NULL/None means the legacy model."""
    if mid is None:
        return LEGACY_MODEL
    try:
        return BY_ID[mid]
    except KeyError:
        raise ValueError(f'unknown embedding model id: {mid!r}')


def dims(name):
    return MODELS[name]['dims']


def match_distance(name):
    """The identification bar: nearer than this is a confident match."""
    return MODELS[name]['match_distance']


def verification_distance(name):
    """
    The verification bar, used only where a DeepFace response omits its own.

    Not the same question as match_distance and not interchangeable with it -
    see the comment above MODELS.
    """
    return MODELS[name]['verification_distance']


def info(name):
    """Everything the API and the UI need about one model."""
    spec = MODELS[name]
    return {
        'name': name,
        'id': spec['id'],
        'dims': spec['dims'],
        'match_distance': spec['match_distance'],
        'match_distance_measured': spec['measured'],
        'verification_distance': spec['verification_distance'],
        'licence': spec['licence'],
    }


def active_model(catalog_db_path):
    """
    Which model this catalog embeds with.

    AIRLOCK_EMBEDDING_MODEL overrides, for tests and for a one-off run against
    a catalog without writing the property. Otherwise the catalog's own
    property, and failing that the legacy model - because an absent property
    means nobody has migrated this catalog, and everything already in it came
    from VGG-Face.
    """
    env = os.environ.get('AIRLOCK_EMBEDDING_MODEL')
    if env:
        if env not in MODELS:
            raise ValueError(f'AIRLOCK_EMBEDDING_MODEL={env!r} is not a known model')
        return env

    # Read it here rather than through models.py: this module is imported by
    # workflows that must not drag the whole model layer in, and a two-column
    # key/value read does not justify the dependency.
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            row = conn.execute(
                "SELECT value FROM catalog_properties WHERE key = ?",
                (CATALOG_PROPERTY,)).fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        # No catalog_properties yet - a catalog this old is pre-migration by
        # definition.
        return LEGACY_MODEL

    if row and row[0] in MODELS:
        return row[0]
    return LEGACY_MODEL


def set_active_model(catalog_db_path, name):
    """Record the intent. The cutover tool calls this; nothing else should."""
    model_id(name)        # raises on an unknown name before writing it
    conn = sqlite3.connect(catalog_db_path, timeout=60)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS catalog_properties '
                     '(key TEXT PRIMARY KEY, value TEXT)')
        conn.execute('INSERT INTO catalog_properties (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                     (CATALOG_PROPERTY, name))
        conn.commit()
    finally:
        conn.close()
    return name


# ── The guard ────────────────────────────────────────────────────────────────

def check_comparable(a_model_id, b_model_id, context=''):
    """
    Raise unless two embeddings came from the same model.

    Call this at every point two embeddings meet. None is the legacy model, so
    None and 1 are comparable - that is the whole point of the sentinel.
    """
    a = a_model_id if a_model_id is not None else MODELS[LEGACY_MODEL]['id']
    b = b_model_id if b_model_id is not None else MODELS[LEGACY_MODEL]['id']
    if a != b:
        where = f' ({context})' if context else ''
        raise EmbeddingModelMismatch(
            f'refusing to compare a {model_name(a)} embedding with a '
            f'{model_name(b)} one{where}. A distance between different '
            f'embedding spaces is not a meaningful number.')


def check_embedding(vector, name, context=''):
    """
    Raise unless `vector` has the length `name` produces.

    The id says what a row claims to be; this says what it is. They disagree
    when something wrote an embedding without updating the column, which is the
    bug this pairing is here to catch.
    """
    expected = dims(name)
    got = len(vector) if vector is not None else 0
    if got != expected:
        where = f' ({context})' if context else ''
        raise EmbeddingModelMismatch(
            f'{name} embeddings have {expected} dimensions, got {got}{where}')


if __name__ == '__main__':
    import json
    import sys
    if len(sys.argv) > 1:
        for path in sys.argv[1:]:
            print(f'{path}: {active_model(path)}')
    else:
        print(json.dumps([info(n) for n in sorted(MODELS)], indent=2))
