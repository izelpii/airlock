#!/usr/bin/env python3
"""
Embedding provenance, and the guard that makes it worth having.

These tests exist because the failure being prevented is silent. Before
provenance, changing the face model would have put 4096-d and 128-d vectors in
one column and every distance between them would have been a plausible-looking
float that meant nothing - no exception, no log line, just wrong similarity
scores and wrong clusters. So most of what is asserted here is that something
*raises* or that a row is *invisible*, not that a number came out right.

See documentation/specs/face-recognition-migration-spec.md.
"""

import json
import os
import sqlite3
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import embedding_models as em
from face_recognition_schema import ensure_face_recognition_schema


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _catalog(path, legacy_embeddings=0, named=0, unembedded=0):
    """
    A catalog database shaped like a real one, with `legacy_embeddings` rows
    carrying VGG-Face vectors and NO provenance - which is exactly the state
    every existing catalog is in.
    """
    conn = sqlite3.connect(path)
    conn.executescript('''
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY, file_path TEXT);
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE face_detections (
            id INTEGER PRIMARY KEY, file_id INTEGER, person_id INTEGER, source TEXT);
        CREATE TABLE ai_face_contents (
            id INTEGER PRIMARY KEY, file_path TEXT, file_name TEXT,
            face_detection_id INTEGER UNIQUE, source_file_id INTEGER, embedding TEXT);
        CREATE TABLE face_contents (
            id INTEGER PRIMARY KEY, file_path TEXT, file_name TEXT,
            face_detection_id INTEGER, embedding TEXT);
    ''')
    conn.execute("INSERT INTO people VALUES (1, 'Izelpi')")
    vgg = json.dumps([0.01] * em.dims('VGG-Face'))
    fid = 0
    for _ in range(legacy_embeddings):
        fid += 1
        conn.execute('INSERT INTO face_detections VALUES (?,?,?,?)',
                     (fid, fid, None, 'Automatic'))
        conn.execute('INSERT INTO ai_face_contents '
                     '(file_path,file_name,face_detection_id,source_file_id,embedding) '
                     'VALUES (?,?,?,?,?)', (f'{fid}.jpg', f'{fid}.jpg', fid, fid, vgg))
    for _ in range(unembedded):
        fid += 1
        conn.execute('INSERT INTO face_detections VALUES (?,?,?,?)',
                     (fid, fid, None, 'Automatic'))
        conn.execute('INSERT INTO ai_face_contents '
                     '(file_path,file_name,face_detection_id,source_file_id,embedding) '
                     'VALUES (?,?,?,?,NULL)', (f'{fid}.jpg', f'{fid}.jpg', fid, fid))
    for _ in range(named):
        fid += 1
        conn.execute('INSERT INTO face_detections VALUES (?,?,?,?)',
                     (fid, fid, 1, 'Manual'))
        conn.execute('INSERT INTO face_contents '
                     '(file_path,file_name,face_detection_id,embedding) '
                     'VALUES (?,?,?,?)', (f'r{fid}.jpg', f'r{fid}.jpg', fid, vgg))
    conn.commit()
    conn.close()
    ensure_face_recognition_schema(path)
    return path


@pytest.fixture
def catalog(tmp_path):
    return _catalog(str(tmp_path / 'workflow_results.db'),
                    legacy_embeddings=5, named=2, unembedded=3)


# ── The registry ─────────────────────────────────────────────────────────────

def test_the_registry_and_the_table_cannot_disagree(catalog):
    """
    The table is seeded from MODELS, so a model added in code appears in the
    database without a migration script - the mechanism that let three
    definitions of face_similarity_scores drift is not repeated here.
    """
    conn = sqlite3.connect(catalog)
    rows = dict((name, (mid, dims)) for mid, name, dims
                in conn.execute('SELECT id, name, dims FROM embedding_models'))
    conn.close()
    assert set(rows) == set(em.MODELS)
    for name, spec in em.MODELS.items():
        assert rows[name] == (spec['id'], spec['dims'])


def test_vgg_face_is_4096_dimensional():
    """
    Measured on the family catalog: json_array_length(embedding) = 4096 for all
    4,455 of them. The deepface service's docstring said 512, which was simply
    wrong, and a dimension check built on 512 would have rejected every real
    embedding.
    """
    assert em.dims('VGG-Face') == 4096
    assert em.dims('SFace') == 128


def test_an_unknown_model_name_is_refused_loudly():
    with pytest.raises(ValueError) as e:
        em.model_id('ResNet-Of-Theseus')
    assert 'unknown embedding model' in str(e.value)


# ── NULL means legacy ────────────────────────────────────────────────────────

def test_existing_rows_are_not_rewritten(catalog):
    """
    The point of the NULL sentinel: 445,684 rows on a 24 GB database that lives
    on a disk with uncorrectable read errors are left exactly as they were.
    """
    conn = sqlite3.connect(catalog)
    nulls = conn.execute('SELECT COUNT(*) FROM ai_face_contents '
                         ' WHERE embedding_model_id IS NULL').fetchone()[0]
    total = conn.execute('SELECT COUNT(*) FROM ai_face_contents').fetchone()[0]
    conn.close()
    assert nulls == total, 'the migration must not backfill'


def test_the_view_resolves_null_to_the_legacy_model(catalog):
    conn = sqlite3.connect(catalog)
    ids = [r[0] for r in conn.execute(
        'SELECT DISTINCT embedding_model_id FROM face_embeddings')]
    conn.close()
    assert ids == [em.model_id('VGG-Face')]


def test_the_view_hides_rows_with_no_embedding(catalog):
    """
    441,229 of 445,684 rows on the family catalog have no embedding, and almost
    every caller that wants embeddings wants only those that have one.
    """
    conn = sqlite3.connect(catalog)
    in_view = conn.execute('SELECT COUNT(*) FROM face_embeddings').fetchone()[0]
    in_table = conn.execute('SELECT COUNT(*) FROM ai_face_contents').fetchone()[0]
    conn.close()
    assert in_view == 5
    assert in_table == 8


def test_model_name_maps_none_to_the_legacy_model():
    assert em.model_name(None) == 'VGG-Face'
    assert em.model_name(1) == 'VGG-Face'
    assert em.model_name(2) == 'SFace'


# ── Choosing a model ─────────────────────────────────────────────────────────

def test_a_catalog_with_no_property_is_the_legacy_model(catalog):
    """Everything already in such a catalog came from VGG-Face."""
    assert em.active_model(catalog) == 'VGG-Face'


def test_a_catalog_remembers_its_model(catalog):
    em.set_active_model(catalog, 'SFace')
    assert em.active_model(catalog) == 'SFace'


def test_catalogs_migrate_independently(tmp_path):
    """
    Per catalog, not per deployment: second-catalog has no embeddings at all
    and goes first, while family is still on the old model.
    """
    a = _catalog(str(tmp_path / 'a.db'), legacy_embeddings=1)
    b = _catalog(str(tmp_path / 'b.db'), legacy_embeddings=1)
    em.set_active_model(b, 'SFace')
    assert em.active_model(a) == 'VGG-Face'
    assert em.active_model(b) == 'SFace'


def test_the_env_override_wins(catalog, monkeypatch):
    em.set_active_model(catalog, 'VGG-Face')
    monkeypatch.setenv('AIRLOCK_EMBEDDING_MODEL', 'SFace')
    assert em.active_model(catalog) == 'SFace'


def test_a_nonsense_override_is_refused(catalog, monkeypatch):
    monkeypatch.setenv('AIRLOCK_EMBEDDING_MODEL', 'not-a-model')
    with pytest.raises(ValueError):
        em.active_model(catalog)


def test_a_database_without_catalog_properties_still_answers(tmp_path):
    """A catalog old enough to lack the table is pre-migration by definition."""
    p = str(tmp_path / 'bare.db')
    sqlite3.connect(p).close()
    assert em.active_model(p) == 'VGG-Face'


# ── The guard ────────────────────────────────────────────────────────────────

def test_comparing_across_models_raises():
    with pytest.raises(em.EmbeddingModelMismatch) as e:
        em.check_comparable(em.model_id('VGG-Face'), em.model_id('SFace'))
    assert 'not a meaningful number' in str(e.value)


def test_none_is_comparable_with_the_legacy_model():
    """That is the whole point of the sentinel."""
    em.check_comparable(None, em.model_id('VGG-Face'))
    em.check_comparable(em.model_id('VGG-Face'), None)
    em.check_comparable(None, None)


def test_a_wrong_length_embedding_raises():
    """
    The id says what a row claims to be; the length says what it is. They
    disagree when something stored an embedding without setting the column.
    """
    with pytest.raises(em.EmbeddingModelMismatch) as e:
        em.check_embedding([0.0] * 512, 'VGG-Face')
    assert '4096' in str(e.value) and '512' in str(e.value)


def test_a_right_length_embedding_passes():
    em.check_embedding([0.0] * 4096, 'VGG-Face')
    em.check_embedding([0.0] * 128, 'SFace')


def test_thresholds_are_per_model_and_not_interchangeable():
    """
    match_distance is an IDENTIFICATION bar and verification_distance is not.
    At VGG-Face's verification bar of 0.68, 99.7% of strangers were confidently
    mislabelled on the family catalog; the measured identification bar is 0.35.
    """
    assert em.match_distance('VGG-Face') == 0.35
    assert em.verification_distance('VGG-Face') == 0.68
    assert em.match_distance('VGG-Face') < em.verification_distance('VGG-Face')
    assert em.match_distance('SFace') < em.verification_distance('SFace')


def test_an_unmeasured_threshold_says_so():
    """
    SFace's bar is derived, not measured. The API passes this through so a page
    can avoid presenting a guess as a measurement.
    """
    assert em.info('VGG-Face')['match_distance_measured'] is True
    assert em.info('SFace')['match_distance_measured'] is False


# ── Isolation: the state that would otherwise only exist in production ───────

def test_scoring_sees_only_the_active_models_rows(catalog):
    """
    Build a half-migrated catalog deliberately - some rows VGG-Face, some
    SFace - and assert each model's query returns only its own. This is the
    state a stopped migration leaves behind, and the one nobody would think to
    create by hand.
    """
    conn = sqlite3.connect(catalog)
    sface = json.dumps([0.02] * em.dims('SFace'))
    conn.execute('UPDATE ai_face_contents SET embedding = ?, embedding_model_id = ? '
                 ' WHERE face_detection_id IN (1, 2)',
                 (sface, em.model_id('SFace')))
    conn.commit()

    def visible(model):
        return [r[0] for r in conn.execute(
            'SELECT face_detection_id FROM face_embeddings '
            ' WHERE embedding_model_id = ? ORDER BY 1', (em.model_id(model),))]

    assert visible('SFace') == [1, 2]
    assert visible('VGG-Face') == [3, 4, 5]
    # And every vector a query returns has that model's length, so a caller
    # that stacks them into a matrix cannot be handed a ragged one.
    for model in ('SFace', 'VGG-Face'):
        for (emb,) in conn.execute(
                'SELECT embedding FROM face_embeddings WHERE embedding_model_id = ?',
                (em.model_id(model),)):
            assert len(json.loads(emb)) == em.dims(model)
    conn.close()


# ── Idempotence ──────────────────────────────────────────────────────────────

def test_the_schema_migration_runs_twice_with_no_further_change(catalog):
    second = ensure_face_recognition_schema(catalog)
    assert not [c for c in second if 'added' in c or 'created' in c]


def test_the_schema_applies_to_a_database_that_has_never_seen_it(tmp_path):
    p = _catalog(str(tmp_path / 'fresh.db'), legacy_embeddings=1)
    conn = sqlite3.connect(p)
    cols = {r[1] for r in conn.execute('PRAGMA table_info(ai_face_contents)')}
    conn.close()
    assert 'embedding_model_id' in cols


# ── No literals ──────────────────────────────────────────────────────────────

def test_the_model_name_is_not_hardcoded_in_the_pipeline():
    """
    It was, in 28 places across 11 files, which is how a model swap would have
    become a flag day.

    Checked with the AST rather than by grepping lines, because the first
    version of this test flagged three docstrings that merely *mention*
    VGG-Face while explaining where a measured number came from. Prose about a
    model is documentation; a string literal equal to a model name is a
    decision, and only the registry may make that decision.
    """
    import ast

    root = os.path.dirname(os.path.abspath(__file__))
    offenders = []
    for rel in ('workflows/ai_face_recognition_workflow.py',
                'workflows/ai_ml_faces_recognition_workflow.py',
                'workflows/ai_ingest_manual_faces.py',
                'workflows/ai_contextual_recognition_workflow.py'):
        path = os.path.join(root, rel)
        tree = ast.parse(open(path, encoding='utf-8').read())
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node.value in em.MODELS):
                offenders.append(f'{rel}:{node.lineno}: {node.value!r}')
    assert not offenders, ('a model name used as a value outside the registry:\n'
                           + '\n'.join(offenders))


# ── The cutover tool ─────────────────────────────────────────────────────────

def _run_tool(db, *args):
    tool = os.path.join(os.path.dirname(os.path.dirname(
        os.path.dirname(os.path.abspath(__file__)))), 'tools',
        'migrate_embedding_model.py')
    return subprocess.run([sys.executable, tool, db, *args],
                          capture_output=True, text=True)


def test_the_cutover_preserves_every_name(tmp_path):
    """
    The most important test here. A name is a judgement somebody made; an
    embedding is a measurement a model made. This migration invalidates
    measurements and must leave judgements untouched.
    """
    db = _catalog(str(tmp_path / 'c.db'), legacy_embeddings=6, named=3)

    def names():
        conn = sqlite3.connect(db)
        rows = conn.execute('SELECT id, person_id, source FROM face_detections '
                            ' WHERE person_id IS NOT NULL ORDER BY id').fetchall()
        conn.close()
        return rows

    before = names()
    assert len(before) == 3

    r = _run_tool(db, '--to', 'SFace', '--apply')
    assert r.returncode == 0, r.stdout + r.stderr
    assert names() == before


def test_the_cutover_clears_the_old_models_embeddings(tmp_path):
    db = _catalog(str(tmp_path / 'c.db'), legacy_embeddings=6, named=3)
    assert _run_tool(db, '--to', 'SFace', '--apply').returncode == 0
    conn = sqlite3.connect(db)
    left = conn.execute('SELECT COUNT(*) FROM ai_face_contents '
                        ' WHERE embedding IS NOT NULL').fetchone()[0]
    refs = conn.execute('SELECT COUNT(*) FROM face_contents '
                        ' WHERE embedding IS NOT NULL').fetchone()[0]
    conn.close()
    assert (left, refs) == (0, 0)
    assert em.active_model(db) == 'SFace'


def test_a_dry_run_writes_nothing(tmp_path):
    db = _catalog(str(tmp_path / 'c.db'), legacy_embeddings=4, named=1)
    r = _run_tool(db, '--to', 'SFace')
    assert r.returncode == 0
    assert 'DRY RUN' in r.stdout
    conn = sqlite3.connect(db)
    left = conn.execute('SELECT COUNT(*) FROM ai_face_contents '
                        ' WHERE embedding IS NOT NULL').fetchone()[0]
    conn.close()
    assert left == 4
    assert em.active_model(db) == 'VGG-Face'


def test_a_dry_run_writes_no_schema_either(tmp_path):
    """
    "Nothing written" has to mean nothing, not "nothing except some additive
    idempotent schema". The first version of this tool called
    ensure_face_recognition_schema() before the dry-run check, so a dry run
    added two columns and created a table and a view while printing that it
    had written nothing - misleading whoever read that output about the state
    of a 24 GB production database.
    """
    p = str(tmp_path / "bare.db")
    conn = sqlite3.connect(p)
    conn.execute("CREATE TABLE face_detections (id INTEGER PRIMARY KEY, person_id INTEGER)")
    conn.execute("CREATE TABLE ai_face_contents (id INTEGER PRIMARY KEY, embedding TEXT)")
    conn.execute("CREATE TABLE face_contents (id INTEGER PRIMARY KEY, embedding TEXT)")
    conn.execute("INSERT INTO ai_face_contents VALUES (1, '[0.1]')")
    conn.commit()
    conn.close()

    r = _run_tool(p, "--to", "SFace")
    assert r.returncode == 0, r.stdout + r.stderr
    assert "DRY RUN" in r.stdout
    # and it still surveyed correctly, counting un-tagged embeddings as legacy
    assert "VGG-Face" in r.stdout

    conn = sqlite3.connect(p)
    objects = {row[0] for row in conn.execute("SELECT name FROM sqlite_master")}
    cols = {row[1] for row in conn.execute("PRAGMA table_info(ai_face_contents)")}
    conn.close()
    assert "embedding_models" not in objects
    assert "face_embeddings" not in objects
    assert "embedding_model_id" not in cols

def test_the_cutover_keeps_embeddings_already_under_the_target_model(tmp_path):
    """
    What makes a resumed migration cheap rather than a restart: rows already
    embedded under the target are left alone.
    """
    db = _catalog(str(tmp_path / 'c.db'), legacy_embeddings=4)
    conn = sqlite3.connect(db)
    conn.execute('UPDATE ai_face_contents SET embedding = ?, embedding_model_id = ? '
                 ' WHERE face_detection_id = 1',
                 (json.dumps([0.02] * em.dims('SFace')), em.model_id('SFace')))
    conn.commit()
    conn.close()

    assert _run_tool(db, '--to', 'SFace', '--apply').returncode == 0
    conn = sqlite3.connect(db)
    kept = conn.execute('SELECT face_detection_id FROM ai_face_contents '
                        ' WHERE embedding IS NOT NULL').fetchall()
    conn.close()
    assert kept == [(1,)]


def _mark_busy(db, execution_status='running', queue_status='queued'):
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE IF NOT EXISTS workflow_executions '
                 '(id INTEGER PRIMARY KEY, workflow_id INTEGER, status TEXT, '
                 ' processed_files INTEGER)')
    conn.execute('CREATE TABLE IF NOT EXISTS workflow_queue '
                 '(id INTEGER PRIMARY KEY, status TEXT)')
    conn.execute('DELETE FROM workflow_executions')
    conn.execute('DELETE FROM workflow_queue')
    conn.execute('INSERT INTO workflow_executions VALUES (47, 1, ?, 250)',
                 (execution_status,))
    conn.execute('INSERT INTO workflow_queue VALUES (1, ?)', (queue_status,))
    conn.commit()
    conn.close()


def test_a_busy_catalog_is_refused_before_anything_is_written(tmp_path):
    """
    What actually happened on 2026-10-08: the tool waited out its 120 s busy
    timeout behind a running Additional ML Face Recognition and died on the
    first DELETE, having already committed the schema half of the job.

    Two things are asserted. It refuses - exit 2, not a traceback. And it
    refuses BEFORE writing, which the first version of this check did not: it
    sat after ensure_face_recognition_schema(), so a refusal still left two new
    columns, a table and a view behind.
    """
    db = _catalog(str(tmp_path / 'c.db'), legacy_embeddings=4, named=2)
    # drop the provenance schema so "wrote nothing" is observable
    conn = sqlite3.connect(db)
    conn.execute('DROP VIEW IF EXISTS face_embeddings')
    conn.execute('DROP TABLE IF EXISTS embedding_models')
    conn.commit()
    conn.close()
    _mark_busy(db)

    r = _run_tool(db, '--to', 'SFace', '--apply')
    assert r.returncode == 2, r.stdout + r.stderr
    assert 'REFUSING' in r.stdout
    assert 'pause the queue' in r.stdout

    conn = sqlite3.connect(db)
    objects = {row[0] for row in conn.execute('SELECT name FROM sqlite_master')}
    still = conn.execute('SELECT COUNT(*) FROM ai_face_contents '
                         ' WHERE embedding IS NOT NULL').fetchone()[0]
    conn.close()
    assert 'embedding_models' not in objects, 'refused and still wrote schema'
    assert still == 4, 'refused and still cleared embeddings'
    assert em.active_model(db) == 'VGG-Face'


def test_the_printed_pause_sql_is_one_valid_statement(tmp_path):
    """
    It was two quoted strings, which sqlite3 reads as two statements - the
    second being a bare ON CONFLICT clause, a syntax error. Instructions that
    do not run are worse than no instructions.
    """
    db = _catalog(str(tmp_path / 'c.db'), legacy_embeddings=1)
    _mark_busy(db)
    conn = sqlite3.connect(db)
    conn.execute('CREATE TABLE IF NOT EXISTS queue_settings '
                 '(key TEXT PRIMARY KEY, value TEXT)')
    conn.commit()
    conn.close()

    r = _run_tool(db, '--to', 'SFace', '--apply')
    sql = [l.strip().strip('"') for l in r.stdout.splitlines()
           if 'INSERT INTO queue_settings' in l]
    assert sql, r.stdout
    conn = sqlite3.connect(db)
    conn.executescript(sql[0])        # raises if it is not valid SQL
    value = conn.execute("SELECT value FROM queue_settings "
                         " WHERE key = 'paused'").fetchone()[0]
    conn.close()
    assert value == '1'


def test_force_overrides_the_busy_check(tmp_path):
    """
    For a catalog whose execution row says running but whose process is gone -
    a stopped container, a killed workflow - not as a way to migrate a live one.
    """
    db = _catalog(str(tmp_path / 'c.db'), legacy_embeddings=3, named=1)
    _mark_busy(db)
    r = _run_tool(db, '--to', 'SFace', '--apply', '--force')
    assert r.returncode == 0, r.stdout + r.stderr
    assert em.active_model(db) == 'SFace'


def test_a_quiet_catalog_is_not_reported_busy(tmp_path):
    db = _catalog(str(tmp_path / 'c.db'), legacy_embeddings=2, named=1)
    _mark_busy(db, execution_status='completed', queue_status='cancelled')
    r = _run_tool(db, '--to', 'SFace', '--apply')
    assert r.returncode == 0, r.stdout + r.stderr
    assert 'REFUSING' not in r.stdout


def test_the_cutover_is_idempotent(tmp_path):
    db = _catalog(str(tmp_path / 'c.db'), legacy_embeddings=4, named=2)
    assert _run_tool(db, '--to', 'SFace', '--apply').returncode == 0
    assert _run_tool(db, '--to', 'SFace', '--apply').returncode == 0
    conn = sqlite3.connect(db)
    named = conn.execute('SELECT COUNT(*) FROM face_detections '
                         ' WHERE person_id IS NOT NULL').fetchone()[0]
    conn.close()
    assert named == 2
