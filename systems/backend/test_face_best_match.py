#!/usr/bin/env python3
"""
face_best_match: each waiting face's best match, stored and kept current.

    python -m pytest test_face_best_match.py

The table replaces a query the Identify people endpoints ran per request. So
the test that matters is equivalence: after any mix of the changes that can
move a best match - scores written, rewritten and deleted, faces named and
deleted, photos removed, people deleted - draining leaves exactly what that
query returns. ORACLE below is the query, as it was.
"""

import os
import random
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import face_best_match as fbm                 # noqa: E402
from models import CatalogDatabase            # noqa: E402
from test_photo_dates_batch import app_module  # noqa: F401,E402

# The per-request query face_best_match replaced (app.py before v5.5.6),
# for one metric's column.
ORACLE = '''
    SELECT b.face_id, b.person_id, b.d
      FROM (SELECT fss.query_face_detection_id AS face_id, fss.person_id,
                   MIN(fss.{col}) AS d
              FROM face_similarity_scores fss
              JOIN people p ON p.id = fss.person_id
             WHERE fss.{col} IS NOT NULL
             GROUP BY fss.query_face_detection_id) b
      JOIN face_detections fd ON fd.id = b.face_id
                             AND fd.source = 'Automatic' AND fd.person_id IS NULL
      JOIN file_contents fc ON fc.id = fd.file_id AND fc.removed_at IS NULL'''


def _expected(conn):
    return {(m, f, p, round(d, 9))
            for m, col in fbm.METRICS.items()
            for f, p, d in conn.execute(ORACLE.format(col=col))}


def _stored(conn):
    return {(m, f, p, round(d, 9)) for m, f, p, d in conn.execute(
        'SELECT metric, face_id, person_id, d FROM face_best_match')}


class Catalog:
    def __init__(self, tmp_path):
        target = tmp_path / 'cat'
        target.mkdir()
        self.db = CatalogDatabase(str(target))
        self.path = self.db.db_path
        self.conn = sqlite3.connect(self.path)
        self.rng = random.Random(7)
        self.next_face = 1000

    def people(self, n):
        self.conn.executemany('INSERT INTO people (id, name) VALUES (?, ?)',
                              [(i, f'P{i}') for i in range(1, n + 1)])
        self.conn.commit()

    def face(self, scores, contextual=None, source='Automatic'):
        fid = self.next_face
        self.next_face += 1
        self.conn.execute("INSERT INTO file_contents (id, file_path, file_name, file_type) "
                          "VALUES (?, ?, ?, '.jpg')", (fid, f'Images/{fid}.jpg', f'{fid}.jpg'))
        self.conn.execute("INSERT INTO face_detections (id, file_id, x, y, w, h, source) "
                          "VALUES (?, ?, 0, 0, 10, 10, ?)", (fid, fid, source))
        for pid, d in scores.items():
            self.conn.execute(
                'INSERT INTO face_similarity_scores (query_face_detection_id, '
                'reference_face_detection_id, person_id, distance, threshold, '
                'contextual_distance) VALUES (?, ?, ?, ?, 0.4, ?)',
                (fid, pid, pid, d, (contextual or {}).get(pid)))
        self.conn.commit()
        return fid

    def dirty(self):
        return {r[0] for r in self.conn.execute('SELECT face_id FROM face_best_match_dirty')}

    def drain(self):
        assert fbm.drain(self.conn) == 0
        return _stored(self.conn)


@pytest.fixture
def cat(tmp_path):
    c = Catalog(tmp_path)
    yield c
    c.conn.close()


def test_the_metrics_are_the_endpoints_metrics(app_module):
    # Through the shared fixture: a bare `import app` would start the scheduler,
    # open the real catalogs.db, and leave auth on for every test after it.
    assert fbm.METRICS == app_module._METRIC_COLUMN


def test_a_new_catalog_needs_no_full_build(cat):
    assert cat.dirty() == set()


def test_a_face_is_stored_under_its_best_match_only(cat):
    cat.people(2)
    f = cat.face({1: 0.2, 2: 0.3}, contextual={1: 0.6, 2: 0.1})
    assert cat.drain() == {('cosine', f, 1, 0.2), ('contextual', f, 2, 0.1)}


# ── each thing a best match depends on ───────────────────────────────────────

def test_rejecting_a_score_hands_the_face_to_the_next_best(cat):
    cat.people(2)
    f = cat.face({1: 0.2, 2: 0.3})
    cat.drain()
    cat.conn.execute('DELETE FROM face_similarity_scores WHERE query_face_detection_id = ? '
                     'AND person_id = 1', (f,))
    cat.conn.commit()
    assert cat.dirty() == {f}
    assert ('cosine', f, 2, 0.3) in cat.drain()


def test_a_rewritten_distance_moves_the_face(cat):
    cat.people(2)
    f = cat.face({1: 0.2, 2: 0.3})
    cat.drain()
    cat.conn.execute('UPDATE face_similarity_scores SET distance = 0.05 WHERE person_id = 2')
    cat.conn.commit()
    assert ('cosine', f, 2, 0.05) in cat.drain()


def test_columns_a_best_match_does_not_read_dirty_nothing(cat):
    """Re-rank rewrites contextual_assigned on every row; Match by Appearance
    rewrites euclidean_distance. 12 M rows of either must not dirty anything."""
    cat.people(1)
    cat.face({1: 0.2})
    cat.drain()
    cat.conn.execute('UPDATE face_similarity_scores SET contextual_assigned = 1, '
                     'euclidean_distance = 0.7, verified = 1')
    cat.conn.commit()
    assert cat.dirty() == set()


def test_a_named_face_leaves_and_comes_back_when_unnamed(cat):
    cat.people(1)
    f = cat.face({1: 0.2})
    cat.drain()
    cat.conn.execute('UPDATE face_detections SET person_id = 1 WHERE id = ?', (f,))
    cat.conn.commit()
    assert cat.drain() == set()
    cat.conn.execute('UPDATE face_detections SET person_id = NULL WHERE id = ?', (f,))
    cat.conn.commit()
    assert cat.drain() == {('cosine', f, 1, 0.2)}


def test_an_auto_confirmed_face_leaves(cat):
    cat.people(1)
    f = cat.face({1: 0.2})
    cat.drain()
    cat.conn.execute("UPDATE face_detections SET source = 'Auto-Confirmed' WHERE id = ?", (f,))
    cat.conn.commit()
    assert cat.drain() == set()


def test_a_deleted_face_leaves(cat):
    cat.people(1)
    f = cat.face({1: 0.2})
    cat.drain()
    cat.conn.execute('DELETE FROM face_detections WHERE id = ?', (f,))
    cat.conn.commit()
    assert cat.drain() == set()


def test_a_removed_photo_takes_its_faces_and_restoring_it_brings_them_back(cat):
    cat.people(1)
    f = cat.face({1: 0.2})
    cat.drain()
    cat.conn.execute("UPDATE file_contents SET removed_at = '2026-10-11' WHERE id = ?", (f,))
    cat.conn.commit()
    assert cat.drain() == set()
    cat.conn.execute('UPDATE file_contents SET removed_at = NULL WHERE id = ?', (f,))
    cat.conn.commit()
    assert cat.drain() == {('cosine', f, 1, 0.2)}


def test_a_deleted_person_passes_their_faces_to_the_next_best(cat):
    """The scores stay - the app does not enable foreign keys - so the face
    moves only because the trigger notices the person went."""
    cat.people(2)
    f = cat.face({1: 0.2, 2: 0.3})
    cat.drain()
    cat.conn.execute('DELETE FROM people WHERE id = 1')
    cat.conn.commit()
    assert cat.drain() == {('cosine', f, 2, 0.3)}


# ── equivalence with the query it replaced ───────────────────────────────────

def test_any_mix_of_changes_drains_to_what_the_old_query_returned(cat):
    rng = cat.rng
    cat.people(6)
    faces = []
    for _ in range(300):
        pids = rng.sample(range(1, 7), rng.randint(1, 6))
        faces.append(cat.face({p: rng.random() for p in pids},
                              contextual={p: rng.random() for p in pids if rng.random() < .7},
                              source=rng.choice(['Automatic'] * 5 + ['Manual'])))
    cat.drain()
    for _ in range(400):
        f = rng.choice(faces)
        op = rng.randrange(7)
        q = cat.conn.execute
        if op == 0:
            q('UPDATE face_similarity_scores SET distance = ? WHERE query_face_detection_id = ? '
              'AND person_id = ?', (rng.random(), f, rng.randint(1, 6)))
        elif op == 1:
            q('UPDATE face_similarity_scores SET contextual_distance = ? '
              'WHERE query_face_detection_id = ?', (rng.random(), f))
        elif op == 2:
            q('DELETE FROM face_similarity_scores WHERE query_face_detection_id = ? '
              'AND person_id = ?', (f, rng.randint(1, 6)))
        elif op == 3:
            # A second reference for a person (scores are unique per
            # face and reference, not per face and person).
            p = rng.randint(1, 6)
            q('INSERT OR IGNORE INTO face_similarity_scores (query_face_detection_id, '
              'reference_face_detection_id, person_id, distance, threshold) '
              'VALUES (?, ?, ?, ?, 0.4)', (f, 100 + p, p, rng.random()))
        elif op == 4:
            q('UPDATE face_detections SET person_id = ? WHERE id = ?',
              (rng.choice([None, None, 3]), f))
        elif op == 5:
            q('UPDATE file_contents SET removed_at = ? WHERE id = ?',
              (rng.choice([None, '2026-10-11']), f))
        else:
            q('DELETE FROM face_detections WHERE id = ?', (f,))
        if rng.random() < 0.1:
            cat.conn.commit()
            assert cat.drain() == _expected(cat.conn)
    cat.conn.execute('DELETE FROM people WHERE id = 4')
    cat.conn.commit()
    assert cat.drain() == _expected(cat.conn)


def test_a_full_build_over_existing_scores_matches_too(cat, monkeypatch):
    """What the first start after this change does on a real catalog. A small
    RANGE walks it in several transactions, as `family` will be."""
    rng = cat.rng
    cat.people(4)
    for _ in range(120):
        cat.face({p: rng.random() for p in rng.sample(range(1, 5), rng.randint(1, 4))})
    cat.conn.execute('DELETE FROM face_best_match')
    cat.conn.execute('DELETE FROM face_best_match_dirty')
    cat.conn.execute('INSERT INTO face_best_match_dirty VALUES (?)', (fbm.FULL_REBUILD,))
    cat.conn.commit()
    monkeypatch.setattr(fbm, 'RANGE', 25)
    assert cat.drain() == _expected(cat.conn)
    assert cat.dirty() == set()


def test_an_interrupted_full_build_starts_again(cat, monkeypatch):
    cat.people(1)
    for _ in range(60):
        cat.face({1: cat.rng.random()})
    cat.conn.execute('INSERT INTO face_best_match_dirty VALUES (?)', (fbm.FULL_REBUILD,))
    cat.conn.commit()
    monkeypatch.setattr(fbm, 'RANGE', 25)
    calls = {'n': 0}
    real = fbm._recompute

    def fail_second(conn, faces, params):
        calls['n'] += 1
        if calls['n'] == 2:
            raise sqlite3.OperationalError('database is locked')
        real(conn, faces, params)
    monkeypatch.setattr(fbm, '_recompute', fail_second)
    with pytest.raises(sqlite3.OperationalError):
        fbm.drain(cat.conn)
    assert fbm.pending(cat.conn)[1], 'the sentinel must survive an interruption'
    monkeypatch.setattr(fbm, '_recompute', real)
    assert cat.drain() == _expected(cat.conn)


# ── who drains ───────────────────────────────────────────────────────────────

def test_a_request_drains_a_small_backlog_itself(cat):
    cat.people(1)
    cat.face({1: 0.2})
    assert fbm.ensure_fresh(cat.conn, cat.path) is True
    assert cat.dirty() == set()


def test_a_large_backlog_goes_to_one_background_thread(cat, monkeypatch):
    monkeypatch.setattr(fbm, 'INLINE_MAX', 5)
    cat.people(1)
    for _ in range(20):
        cat.face({1: cat.rng.random()})
    assert fbm.ensure_fresh(cat.conn, cat.path) is False
    for t in __import__('threading').enumerate():
        if t.name == 'face-best-match':
            t.join(10)
    assert fbm.ensure_fresh(cat.conn, cat.path) is True
    assert _stored(cat.conn) == _expected(cat.conn)


def test_a_backlog_is_left_while_a_scoring_run_is_going(cat, monkeypatch):
    monkeypatch.setattr(fbm, 'INLINE_MAX', 5)
    cat.people(1)
    for _ in range(20):
        cat.face({1: cat.rng.random()})
    cat.conn.execute("INSERT INTO workflows (id, name, description, source_directory, "
                     "python_script) VALUES (4, 'Match', '', '', "
                     "'workflows/ai_face_recognition_workflow.py')")
    cat.conn.execute("INSERT INTO workflow_executions (workflow_id, workflow_name, status) "
                     "VALUES (4, 'Match', 'running')")
    cat.conn.commit()
    assert fbm.ensure_fresh(cat.conn, cat.path) is False
    assert not any(t.name == 'face-best-match'
                   for t in __import__('threading').enumerate())
    assert len(cat.dirty()) == 20
