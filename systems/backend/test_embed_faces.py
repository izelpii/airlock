#!/usr/bin/env python3
"""
Embed Faces, and the one property that was asked for by name: stopping it must
not cost work, and running it again must not redo any.

    python -m pytest test_embed_faces.py

Embedding used to be a side effect of scoring, inside the per-face loop of two
other workflows. On `family` that reached 4,455 of 445,684 crops - 1% - over
the whole life of the feature, because every run was stopped before it got
far, and the cost landed on whoever tagged a face.
See documentation/specs/face-workflow-optimization.md.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workflows'))

import ai_embed_faces as ef          # noqa: E402
import embedding_models              # noqa: E402

SFACE = embedding_models.model_id('SFace')
VGG = embedding_models.model_id('VGG-Face')


class _NoSleep:
    """time, with sleep() removed, so retry backoff does not slow the suite."""
    @staticmethod
    def sleep(_):
        return None

    @staticmethod
    def time():
        import time as _t
        return _t.time()


@pytest.fixture
def catalog(tmp_path):
    """A catalog with crops on disk and no embeddings."""
    target = tmp_path / 'cat'
    (target / 'cache' / 'faces-ai').mkdir(parents=True)
    db = str(target / 'workflow_results.db')
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY, file_name TEXT);
        CREATE TABLE face_detections (id INTEGER PRIMARY KEY, file_id INTEGER);
        CREATE TABLE ai_face_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_detection_id INTEGER, source_file_id INTEGER,
            file_path TEXT, file_name TEXT,
            embedding TEXT, embedding_model_id INTEGER);
        CREATE TABLE catalog_properties (key TEXT PRIMARY KEY, value TEXT);
    """)
    conn.execute("INSERT INTO catalog_properties VALUES ('embedding_model','SFace')")
    for i in range(1, 11):
        rel = os.path.join('cache', 'faces-ai', f'{i}.jpg')
        (target / rel).write_bytes(b'jpeg')
        conn.execute('INSERT INTO ai_face_contents '
                     '(face_detection_id, source_file_id, file_path, file_name) '
                     'VALUES (?,?,?,?)', (i, i, rel, f'{i}.jpg'))
    conn.commit()
    conn.close()
    return str(target), db


def _conn(db):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    return c


def _fake_embed(monkeypatch, fail_after=None, calls=None):
    """Replace the HTTP call. Counts how many crops were actually embedded."""
    def go(path, model):
        if calls is not None:
            calls.append(path)
        if fail_after is not None and len(calls or []) > fail_after:
            return None, 'pretend failure'
        return [0.1] * embedding_models.dims(model), None
    monkeypatch.setattr(ef, 'embed_one', go)


def test_it_embeds_every_crop(catalog, monkeypatch):
    target, db = catalog
    calls = []
    _fake_embed(monkeypatch, calls=calls)
    out = ef.execute(None, target, None, None, catalog=object.__new__(type('C', (), {})))
    assert out['embedded'] == 10
    assert out['remaining'] == 0

    c = _conn(db)
    rows = c.execute('SELECT embedding, embedding_model_id FROM ai_face_contents').fetchall()
    c.close()
    assert all(r['embedding'] and r['embedding_model_id'] == SFACE for r in rows)
    assert len(json.loads(rows[0]['embedding'])) == 128


def test_a_second_run_does_nothing(catalog, monkeypatch):
    """
    THE property that was asked for. The embedding IS the watermark: a crop
    that has one under the active model can never be selected again, so there
    is no column to forget to update and no way for the two to disagree.
    """
    target, db = catalog
    calls = []
    _fake_embed(monkeypatch, calls=calls)
    ef.execute(None, target, None, None, catalog=object())
    assert len(calls) == 10

    calls.clear()
    out = ef.execute(None, target, None, None, catalog=object())
    assert calls == [], 'it re-embedded crops that were already done'
    assert out['embedded'] == 0
    assert out['remaining'] == 0


def test_an_interrupted_run_resumes_without_redoing_work(catalog, monkeypatch):
    """
    The other half of the ask. Kill it part-way; the committed embeddings
    stay, and the next run picks up exactly the ones still missing.
    """
    target, db = catalog
    calls = []

    # Embed four, then behave as though the process died.
    def die_after_four(path, model):
        calls.append(path)
        if len(calls) > 4:
            raise KeyboardInterrupt('pretend somebody pressed ctrl-C')
        return [0.1] * 128, None
    monkeypatch.setattr(ef, 'embed_one', die_after_four)
    monkeypatch.setattr(ef, 'COMMIT_EVERY', 1)      # commit as we go

    with pytest.raises(KeyboardInterrupt):
        ef.execute(None, target, None, None, catalog=object())

    c = _conn(db)
    done = c.execute('SELECT COUNT(*) FROM ai_face_contents '
                     ' WHERE embedding IS NOT NULL').fetchone()[0]
    c.close()
    assert done == 4, f'expected 4 committed before the interrupt, got {done}'

    # Resume. It must embed the remaining SIX and not touch the four.
    calls.clear()
    _fake_embed(monkeypatch, calls=calls)
    out = ef.execute(None, target, None, None, catalog=object())
    assert out['embedded'] == 6, f'resumed run embedded {out["embedded"]}, expected 6'
    assert len(calls) == 6, 'it re-embedded crops that survived the interrupt'
    assert out['remaining'] == 0


def test_an_embedding_from_another_model_is_redone(catalog, monkeypatch):
    """
    A VGG-Face vector is not a worse SFace vector, it is an incomparable one.
    It must be treated as absent, or the catalog ends up with two embedding
    spaces in one column.
    """
    target, db = catalog
    c = sqlite3.connect(db)
    c.execute('UPDATE ai_face_contents SET embedding = ?, embedding_model_id = ? '
              ' WHERE id <= 3', (json.dumps([0.5] * 4096), VGG))
    c.commit()
    c.close()

    calls = []
    _fake_embed(monkeypatch, calls=calls)
    out = ef.execute(None, target, None, None, catalog=object())
    assert out['embedded'] == 10, 'the three VGG-Face rows should have been redone'

    c = _conn(db)
    ids = {r['embedding_model_id'] for r in
           c.execute('SELECT embedding_model_id FROM ai_face_contents')}
    c.close()
    assert ids == {SFACE}


def test_a_missing_crop_file_is_skipped_not_fatal(catalog, monkeypatch):
    """One unreadable file must not stop four hundred thousand."""
    target, db = catalog
    os.remove(os.path.join(target, 'cache', 'faces-ai', '5.jpg'))
    _fake_embed(monkeypatch, calls=[])
    out = ef.execute(None, target, None, None, catalog=object())
    assert out['embedded'] == 9
    assert out['skipped'] == 1


def test_a_dead_service_stops_early_instead_of_grinding(catalog, monkeypatch):
    """
    A detector that is down fails on everything. Walking all 400,000 to find
    that out wastes hours and writes nothing, so it gives up - and says how
    many embeddings are safely committed.
    """
    target, db = catalog
    monkeypatch.setattr(ef, 'embed_one',
                        lambda p, m: (None, 'HTTP 500: it fell over'))
    monkeypatch.setattr(ef, 'MAX_CONSECUTIVE_FAILURES', 3)
    monkeypatch.setattr(ef, 'time', _NoSleep())      # do not wait out backoff
    with pytest.raises(RuntimeError) as exc:
        ef.execute(None, target, None, None, catalog=object())
    # It must say WHAT failed, not guess. The first version claimed "the
    # deepface service is almost certainly down" while the service was up,
    # logging nothing, and embedding the next crop by hand.
    assert 'HTTP 500: it fell over' in str(exc.value), str(exc.value)


def test_the_work_list_query_is_indexable(catalog, monkeypatch):
    """
    This runs on every import. `!= active` and `OR` cannot use an index and
    scan the whole table; enumerating the other model ids can, and the partial
    index covers the NULL case while shrinking to nothing as work completes.
    """
    target, db = catalog
    c = _conn(db)
    ef.ensure_backfill_index(c)
    c.commit()
    plan = [r[3] for r in c.execute(
        'EXPLAIN QUERY PLAN SELECT COUNT(*) FROM ai_face_contents '
        ' WHERE embedding_model_id IN (1)')]
    c.close()
    assert any('INDEX' in p.upper() for p in plan), plan


def test_other_model_ids_comes_from_the_registry(catalog):
    """
    Hard-coding them would leave a third model's embeddings in place as though
    they were usable the day one is added.
    """
    assert ef._other_model_ids(SFACE) == [VGG]
    assert ef._other_model_ids(VGG) == [SFACE]


# ── Is it actually wired in? ─────────────────────────────────────────────────
#
# A workflow that is not registered for a catalog is SKIPPED BY EVERY TRIGGER
# CHAIN, silently and by design. So "it exists" and "it runs" are different
# claims and both need asserting.

def test_it_is_a_default_so_new_catalogs_get_it():
    import workflow_defaults
    entry = next((e for e in workflow_defaults.load_manifest()
                  if e['script'] == 'ai_embed_faces.py'), None)
    assert entry is not None, 'not in defaults.json at all'
    assert entry.get('default') is True, 'exists but is not seeded with a catalog'
    # Between detection (30), which makes the crops, and recognition (50),
    # which needs their vectors.
    assert 30 < entry['order'] < 50, entry['order']


def test_an_existing_catalog_is_offered_it_too(tmp_path):
    """
    The harder half. Adding a workflow to the manifest does nothing for a
    catalog that already exists unless sync_default_workflows offers it, and
    the pipeline drawing looks complete without it.
    """
    import workflow_defaults
    from models import CatalogDatabase, set_catalog_property, get_catalog_property

    target = tmp_path / 'existing'
    target.mkdir()
    cdb = CatalogDatabase(str(target))

    # A catalog from before Embed Faces existed: it has been offered everything
    # else, so only the new script should arrive.
    import json as _json
    seen = [e['script'] for e in workflow_defaults.load_manifest()
            if e.get('default') and e['script'] != 'ai_embed_faces.py']
    set_catalog_property(cdb.db_path, 'default_workflows_seen', _json.dumps(seen))

    # Read the name from the manifest rather than repeating it: this test is
    # about the script arriving, not about what it is called this month.
    wanted = next(e['name'] for e in workflow_defaults.load_manifest()
                  if e['script'] == 'ai_embed_faces.py')

    out = workflow_defaults.sync_default_workflows(cdb)
    assert wanted in out['created'], \
        f'an existing catalog was never offered it: {out}'

    # ...and only once. A second sync must not add it again.
    again = workflow_defaults.sync_default_workflows(cdb)
    assert wanted not in again['created'], \
        'offered twice - the watermark did not record it'


def test_it_runs_after_detection_and_before_recognition():
    """
    THE wiring that makes it useful. faces_detected fires when Auto Detect
    Faces completes and has just written crops with no vectors; without Embed
    Faces in that chain the commonest path in the system - detect, then
    recognise - skips embedding entirely and the separation achieves nothing.
    """
    import workflow_triggers as trig
    for chain in ('import_completed', 'faces_detected'):
        assert 'ai_embed_faces.py' in trig.TRIGGERS[chain], \
            f'missing from the {chain} chain'

    # and the queue has to order them, not merely enqueue them together
    assert 'ai_auto_detect_faces_workflow.py' in trig.DEPENDS_ON['ai_embed_faces.py']
    assert 'ai_embed_faces.py' in trig.DEPENDS_ON['ai_face_recognition_workflow.py']


def test_it_is_NOT_in_the_face_confirmed_chain():
    """
    Deliberate, and the reason the workflow exists. Confirming three faces used
    to discover 413,865 missing embeddings and spend four hours on them behind
    a click. Embedding belongs to imports and to deliberate runs, not to
    tagging.
    """
    import workflow_triggers as trig
    assert 'ai_embed_faces.py' not in trig.TRIGGERS['face_confirmed'], \
        'tagging a face would pay for embedding again'


def test_it_has_a_write_set_and_a_stop_mode():
    import workflow_scheduler as sched
    assert sched.write_set('ai_embed_faces.py') == {'ai_face_contents'}
    assert sched.stop_mode('ai_embed_faces.py') == 'item'


# ── Recognition must no longer embed ────────────────────────────────────────

def test_recognition_no_longer_creates_embeddings():
    """
    The change that makes the separation mean something.

    Measure Faces existed for a day while ai_face_recognition_workflow.py still
    called save_query_embedding() inside its per-face loop - so starting the
    wrong workflow still cost four hours and the new one sat unused beside it.
    A separation that leaves the expensive path reachable from the UI is
    decorative.
    """
    import ast
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, 'workflows',
                            'ai_face_recognition_workflow.py'),
               encoding='utf-8').read()
    tree = ast.parse(src)

    # The AST, not the text: a COMMENT naming these is how the removal stays
    # explained, and a substring check would forbid the explanation.
    defined = {n.name for n in ast.walk(tree)
               if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    called = {n.func.id for n in ast.walk(tree)
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}

    for gone in ('save_query_embedding', 'compute_embedding_for_bytes'):
        assert gone not in defined, f'{gone} is still defined here'
        assert gone not in called, f'{gone} is still called here'

    # Reference faces - 45, not 469,847 - are still backfilled here, which is
    # cheap and keeps a model cutover self-healing.
    assert 'compute_embedding_for_face' in defined


def test_a_face_with_no_embedding_is_reported_not_embedded(tmp_path):
    """
    Skipped and counted, and the run says what to do. Silence here is how
    "nobody matched" and "most of the catalog was never looked at" became
    indistinguishable.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    src = open(os.path.join(here, 'workflows',
                            'ai_face_recognition_workflow.py'),
               encoding='utf-8').read()
    assert 'not_embedded' in src
    assert 'faces_not_embedded' in src, 'the count is not in the result dict'
    assert 'Measure Faces' in src, 'the run does not say what to run instead'


def test_a_transient_failure_is_retried_before_it_counts(catalog, monkeypatch):
    """
    A four-hour job must not die of a hiccup. On 2026-10-09 one did: 25 crops
    failed in a row, the service was up throughout and logging nothing, and
    the very next crop embedded first time by hand. Whatever it was, it
    passed - and without a retry one bad second cost the rest of the pass.
    """
    target, db = catalog
    attempts = []

    def flaky(path, model):
        attempts.append(path)
        # fail the first two attempts at every crop, succeed on the third
        if attempts.count(path) < 3:
            return None, 'HTTP 502: briefly unavailable'
        return [0.1] * 128, None

    monkeypatch.setattr(ef, 'embed_one', flaky)
    monkeypatch.setattr(ef, 'time', _NoSleep())
    out = ef.execute(None, target, None, None, catalog=object())
    assert out['embedded'] == 10, 'a retried hiccup still lost work'
    assert out['failed'] == 0
    assert len(attempts) == 30, 'expected three attempts per crop'


def test_it_gives_up_on_a_crop_that_never_works(catalog, monkeypatch):
    """
    The other side: a crop that genuinely cannot be embedded must be given up
    on quickly, because there may be thousands of them and each one costs
    three attempts plus backoff.
    """
    target, db = catalog
    attempts = []

    def one_bad(path, model):
        attempts.append(path)
        if path.endswith('3.jpg'):
            return None, 'the response carried no embedding'
        return [0.1] * 128, None

    monkeypatch.setattr(ef, 'embed_one', one_bad)
    monkeypatch.setattr(ef, 'time', _NoSleep())
    out = ef.execute(None, target, None, None, catalog=object())
    assert out['embedded'] == 9
    assert out['failed'] == 1
    assert attempts.count([a for a in attempts if a.endswith('3.jpg')][0]) == 3


def test_progress_can_be_written_after_a_crop_goes_missing(tmp_path, monkeypatch):
    """
    Commits follow `embedded`, progress follows `i`; one missing crop puts them
    out of step, and then progress came with uncommitted updates and its own
    write waited 30 s behind them and failed. The callback here writes as the
    real one does, on a second connection, but fails fast instead of waiting.
    """
    target = tmp_path / 'cat'
    (target / 'cache' / 'faces-ai').mkdir(parents=True)
    db = str(target / 'workflow_results.db')
    conn = sqlite3.connect(db)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY, file_name TEXT);
        CREATE TABLE face_detections (id INTEGER PRIMARY KEY, file_id INTEGER);
        CREATE TABLE ai_face_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_detection_id INTEGER, source_file_id INTEGER,
            file_path TEXT, file_name TEXT,
            embedding TEXT, embedding_model_id INTEGER);
        CREATE TABLE catalog_properties (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE workflow_executions (id INTEGER PRIMARY KEY,
            processed_files INTEGER, total_files INTEGER);
        INSERT INTO workflow_executions (id) VALUES (1);
    """)
    conn.execute("INSERT INTO catalog_properties VALUES ('embedding_model','SFace')")
    for i in range(1, 61):
        rel = os.path.join('cache', 'faces-ai', f'{i}.jpg')
        if i != 1:                                   # the first crop is missing
            (target / rel).write_bytes(b'jpeg')
        conn.execute('INSERT INTO ai_face_contents '
                     '(face_detection_id, source_file_id, file_path, file_name) '
                     'VALUES (?,?,?,?)', (i, i, rel, f'{i}.jpg'))
    conn.commit()
    conn.close()
    _fake_embed(monkeypatch)
    monkeypatch.setattr(ef, 'COMMIT_EVERY', 25)
    seen = []

    def progress(done, total):
        c = sqlite3.connect(db, timeout=0.2)
        try:
            c.execute('UPDATE workflow_executions SET processed_files=?, total_files=? '
                      'WHERE id=1', (done, total))
            c.commit()
        finally:
            c.close()
        seen.append(done)

    out = ef.execute(None, str(target), None, progress,
                     catalog=object.__new__(type('C', (), {})))
    assert out['embedded'] == 59
    assert 50 in seen and seen[-1] == 60
