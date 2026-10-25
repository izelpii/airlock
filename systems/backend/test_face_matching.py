#!/usr/bin/env python3
"""
Match Faces by Appearance: every face, every reference, a chunk at a time.

    python -m pytest test_face_matching.py

The workflow compared one face at a time - a photograph decoded and cropped
for nothing, the references rebuilt from Python lists, four connections and
two commits - ~5 hours for `family`'s 469,000 faces, holding 14 GB, and
reporting nothing for its first two minutes. It now reads, compares and
writes a chunk at a time.

It also skipped the references a face had already been scored against - and
with only the best reference per person stored, that meant comparing against
everything but the best and then deleting the best: a face's best match
alternated between its best and second-best reference from run to run.
"""

import json
import math
import os
import sqlite3
import sys
from types import SimpleNamespace

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import embedding_models                         # noqa: E402
import ai_face_recognition_workflow as wf       # noqa: E402
from face_recognition_schema import ensure_face_recognition_schema  # noqa: E402

MODEL = 'SFace'
DIMS = embedding_models.dims(MODEL)
MODEL_ID = embedding_models.model_id(MODEL)


def _vec(seed):
    v = np.random.default_rng(seed).normal(size=DIMS)
    return (v / np.linalg.norm(v)).tolist()


def _near(base, seed, amount):
    v = np.asarray(base) + amount * np.asarray(_vec(seed))
    return (v / np.linalg.norm(v)).tolist()


@pytest.fixture
def cat(tmp_path):
    target = tmp_path / 'cat'
    target.mkdir()
    db = str(target / 'workflow_results.db')
    conn = sqlite3.connect(db)
    conn.executescript(f"""
        PRAGMA journal_mode=WAL;
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY, file_path TEXT,
                                    file_name TEXT, removed_at TIMESTAMP);
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE face_detections (id INTEGER PRIMARY KEY, file_id INTEGER,
            x INTEGER, y INTEGER, w INTEGER, h INTEGER, person_id INTEGER,
            source TEXT DEFAULT 'Automatic');
        CREATE TABLE face_contents (id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_detection_id INTEGER, file_path TEXT, embedding TEXT, year INTEGER);
        CREATE TABLE ai_face_contents (id INTEGER PRIMARY KEY AUTOINCREMENT,
            face_detection_id INTEGER, source_file_id INTEGER, file_path TEXT,
            file_name TEXT, embedding TEXT);
        CREATE TABLE catalog_properties (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO catalog_properties VALUES ('embedding_model', '{MODEL}');
        INSERT INTO file_contents (id, file_path, file_name) VALUES (1, 'a.jpg', 'a.jpg');
        INSERT INTO people VALUES (1, 'Ana'), (2, 'Ben');
    """)
    conn.commit()
    conn.close()
    ensure_face_recognition_schema(db)
    return SimpleNamespace(db=db, target=str(target), next_id=1)


def _ref(cat, person, vec, file_id=None):
    """A named face - on its own photograph unless file_id says otherwise."""
    fid = cat.next_id
    cat.next_id += 1
    conn = sqlite3.connect(cat.db)
    if file_id is None:
        file_id = 10000 + fid
        conn.execute('INSERT INTO file_contents (id, file_path, file_name) VALUES (?, ?, ?)',
                     (file_id, f'r{fid}.jpg', f'r{fid}.jpg'))
    conn.execute("INSERT INTO face_detections (id, file_id, x, y, w, h, person_id, source) "
                 "VALUES (?, ?, 0, 0, 1, 1, ?, 'Manual')", (fid, file_id, person))
    conn.execute('INSERT INTO face_contents (face_detection_id, file_path, embedding, '
                 'embedding_model_id) VALUES (?, ?, ?, ?)',
                 (fid, f'ref{fid}.jpg', json.dumps(vec), MODEL_ID))
    conn.commit()
    conn.close()
    return fid


def _face(cat, vec=None):
    fid = cat.next_id
    cat.next_id += 1
    conn = sqlite3.connect(cat.db)
    conn.execute('INSERT INTO face_detections (id, file_id, x, y, w, h) '
                 'VALUES (?, 1, 0, 0, 1, 1)', (fid,))
    if vec is not None:
        conn.execute('INSERT INTO ai_face_contents (face_detection_id, source_file_id, '
                     'file_path, file_name, embedding, embedding_model_id) '
                     "VALUES (?, 1, 'c.jpg', 'c.jpg', ?, ?)", (fid, json.dumps(vec), MODEL_ID))
    conn.commit()
    conn.close()
    return fid


def _run(cat, progress=None, execution_id=None):
    return wf.execute(None, cat.target, [], progress,
                      catalog=SimpleNamespace(id=1), execution_id=execution_id)


def _scores(cat):
    conn = sqlite3.connect(cat.db)
    try:
        return {(q, p): (r, d, e) for q, r, p, d, e in conn.execute(
            'SELECT query_face_detection_id, reference_face_detection_id, person_id, '
            '       distance, euclidean_distance FROM face_similarity_scores')}
    finally:
        conn.close()


def _brute(query, refs):
    """{person: (ref_id, distance)} the long way."""
    out = {}
    for rid, person, vec in refs:
        d = 1 - float(np.dot(query, vec))
        if person not in out or d < out[person][1]:
            out[person] = (rid, d)
    return out


# ── what is stored ──────────────────────────────────────────────────────────

def test_each_face_gets_its_best_reference_for_every_person(cat):
    refs = []
    for person, seeds in ((1, (1, 2, 3)), (2, (4, 5))):
        for s in seeds:
            v = _vec(s)
            refs.append((_ref(cat, person, v), person, v))
    faces = {_face(cat, _near(_vec(1), 100 + i, 0.4)): None for i in range(6)}
    conn = sqlite3.connect(cat.db)
    vecs = dict(conn.execute('SELECT face_detection_id, embedding FROM ai_face_contents'))
    conn.close()

    result = _run(cat)
    scores = _scores(cat)
    assert result['faces_compared'] == 6
    for fid in faces:
        want = _brute(json.loads(vecs[fid]), refs)
        for person, (rid, d) in want.items():
            got_rid, got_d, euclid = scores[(fid, person)]
            assert got_rid == rid
            assert got_d == pytest.approx(d, abs=1e-9)
            assert euclid == pytest.approx(math.sqrt(2 * d), abs=1e-9)
    assert len(scores) == 6 * 2                        # one row per face per person


def test_a_rerun_keeps_the_best_match_and_its_contextual_score(cat):
    """The flip: a re-run compared against everything but the stored best."""
    best = _vec(1)
    r_best = _ref(cat, 1, best)
    _ref(cat, 1, _near(best, 9, 0.8))                  # a worse reference of Ana
    fid = _face(cat, _near(best, 10, 0.1))
    _run(cat)
    assert _scores(cat)[(fid, 1)][0] == r_best
    conn = sqlite3.connect(cat.db)
    conn.execute('UPDATE face_similarity_scores SET contextual_distance = 0.05 '
                 'WHERE query_face_detection_id = ?', (fid,))
    conn.commit()
    conn.close()

    _run(cat)
    _run(cat)
    assert _scores(cat)[(fid, 1)][0] == r_best
    conn = sqlite3.connect(cat.db)
    ctx = conn.execute('SELECT contextual_distance FROM face_similarity_scores '
                       'WHERE query_face_detection_id = ?', (fid,)).fetchone()[0]
    conn.close()
    assert ctx == 0.05


def test_a_better_new_reference_replaces_the_old_row(cat):
    target = _vec(1)
    old = _ref(cat, 1, _near(target, 2, 0.6))
    fid = _face(cat, target)
    _run(cat)
    assert _scores(cat)[(fid, 1)][0] == old
    new = _ref(cat, 1, _near(target, 3, 0.05))
    _run(cat)
    scores = _scores(cat)
    assert scores[(fid, 1)][0] == new
    assert len([k for k in scores if k[0] == fid]) == 1


def test_a_face_without_an_embedding_is_counted_and_left_alone(cat):
    _ref(cat, 1, _vec(1))
    bare = _face(cat, None)
    with_vec = _face(cat, _vec(2))
    result = _run(cat)
    assert result['faces_not_embedded'] == 1
    assert {k[0] for k in _scores(cat)} == {with_vec}
    assert bare not in {k[0] for k in _scores(cat)}


def test_small_chunks_give_the_same_answer_as_one(cat, monkeypatch):
    for person, s in ((1, 1), (1, 2), (2, 3)):
        _ref(cat, person, _vec(s))
    for i in range(7):
        _face(cat, _vec(50 + i))
    monkeypatch.setattr(wf, 'CHUNK', 1000)
    _run(cat)
    whole = _scores(cat)
    conn = sqlite3.connect(cat.db)
    conn.execute('DELETE FROM face_similarity_scores')
    conn.commit()
    conn.close()
    monkeypatch.setattr(wf, 'CHUNK', 3)
    _run(cat)
    chunked = _scores(cat)
    # The same winners; distances equal to rounding - a matrix product's last
    # bit depends on the shape it was computed in.
    assert chunked.keys() == whole.keys()
    for k, (rid, d, e) in whole.items():
        assert chunked[k][0] == rid
        assert chunked[k][1] == pytest.approx(d, abs=1e-12)


# ── stopping and progress ───────────────────────────────────────────────────

def test_a_stop_between_chunks_says_a_rerun_starts_over(cat, monkeypatch):
    _ref(cat, 1, _vec(1))
    for i in range(5):
        _face(cat, _vec(10 + i))
    monkeypatch.setattr(wf, 'CHUNK', 2)
    asks = iter([False, True])
    monkeypatch.setattr(wf, 'stop_requested', lambda db, eid: next(asks))
    recorded = {}
    monkeypatch.setattr(wf, 'record_stopped',
                        lambda db, eid, **kw: recorded.update(kw))
    result = _run(cat, execution_id=1)
    assert result['stopped'] and result['faces_compared'] == 2
    assert 'compares every face again' in recorded['message']
    assert 'skipped' not in recorded['message']


def test_progress_is_reported_from_the_start_and_can_always_be_written(cat, monkeypatch):
    _ref(cat, 1, _vec(1))
    for i in range(5):
        _face(cat, _vec(10 + i))
    monkeypatch.setattr(wf, 'CHUNK', 2)
    conn = sqlite3.connect(cat.db)
    conn.execute('CREATE TABLE workflow_executions (id INTEGER PRIMARY KEY, '
                 'processed_files INTEGER, total_files INTEGER)')
    conn.execute('INSERT INTO workflow_executions (id) VALUES (1)')
    conn.commit()
    conn.close()
    calls = []

    def progress(done, total):
        c = sqlite3.connect(cat.db, timeout=0.2)     # fails fast if we hold the lock
        try:
            c.execute('UPDATE workflow_executions SET processed_files=?, total_files=? '
                      'WHERE id=1', (done, total))
            c.commit()
        finally:
            c.close()
        calls.append((done, total))

    _run(cat, progress)
    assert calls[0] == (0, 5)                          # the total, before any comparing
    assert calls[-1] == (5, 5)
    assert [d for d, _ in calls] == [0, 2, 4, 5]


def test_the_stale_row_delete_is_a_key_lookup_not_a_scan(cat):
    """
    The delete scanned the whole keep list for every row - 99 s of a 101 s
    chunk in a benchmark, two hours for `family`. The plan must look the keep
    list up by key.
    """
    _ref(cat, 1, _vec(1))
    _face(cat, _vec(2))
    conn = sqlite3.connect(cat.db)
    wf.ACTIVE_MODEL = MODEL
    wf.write_chunk(conn, [(2, [(1, 1, 0.1)])], 'now')
    plan = ' | '.join(r[3] for r in conn.execute(
        'EXPLAIN QUERY PLAN DELETE FROM face_similarity_scores '
        ' WHERE query_face_detection_id IN (2) AND NOT EXISTS ('
        '   SELECT 1 FROM _keep k WHERE k.q = face_similarity_scores.query_face_detection_id'
        '     AND k.r = face_similarity_scores.reference_face_detection_id)'))
    conn.close()
    assert 'SCAN k' not in plan and 'SCAN _keep' not in plan, plan
    assert 'PRIMARY KEY' in plan, plan



# ── one person, one face per photograph ─────────────────────────────────────

def test_a_person_named_on_another_face_in_the_photo_is_not_a_candidate(cat):
    """
    Rule 1 of Match Faces Within Events, now applied where the rows are
    written - that workflow deleted these rows and this one wrote them back.
    """
    ana = _vec(1)
    _ref(cat, 1, ana)                                  # Ana's reference
    _ref(cat, 2, _vec(2))                              # Ben's
    _ref(cat, 1, _near(ana, 3, 0.05), file_id=1)       # Ana, named in photo 1
    face = _face(cat, _near(ana, 4, 0.05))             # another face in photo 1
    _run(cat)
    scores = _scores(cat)
    assert (face, 1) not in scores                     # not Ana - she is already there
    assert (face, 2) in scores                         # Ben still is


def test_naming_someone_in_the_photo_removes_their_old_candidate_row(cat):
    ana = _vec(1)
    _ref(cat, 1, ana)
    face = _face(cat, _near(ana, 4, 0.05))
    _run(cat)
    assert (face, 1) in _scores(cat)
    _ref(cat, 1, _near(ana, 5, 0.05), file_id=1)       # now Ana is named beside it
    _run(cat)
    assert (face, 1) not in _scores(cat)
