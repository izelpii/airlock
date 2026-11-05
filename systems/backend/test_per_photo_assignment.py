#!/usr/bin/env python3
"""
Constraint C3, one person per photo (resolve_per_photo_assignment), and the
write lock it holds.

    python -m pytest test_per_photo_assignment.py

It used to UPDATE every score row and then read and sort them all inside that
transaction, holding the catalog's write lock for most of an hour on `family`:
the scheduler could not record finished workflows and edits failed with
"database is locked". It now reads and decides first, and writes only the
marks that change, in one short transaction.
"""

import os
import sqlite3
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import ai_contextual_recognition_workflow as wf    # noqa: E402


def _catalog(tmp_path, scores, marked=()):
    """scores: (score_id, face_id, file_id, person_id, contextual_distance)."""
    path = str(tmp_path / 'workflow_results.db')
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE face_detections (id INTEGER PRIMARY KEY, file_id INTEGER, person_id INTEGER)')
    conn.execute('CREATE TABLE face_similarity_scores (id INTEGER PRIMARY KEY, query_face_detection_id INTEGER, '
                 'person_id INTEGER, contextual_distance REAL, contextual_assigned INTEGER DEFAULT 0)')
    for sid, face, file_id, person, dist in scores:
        conn.execute('INSERT OR IGNORE INTO face_detections (id, file_id) VALUES (?, ?)', (face, file_id))
        conn.execute('INSERT INTO face_similarity_scores VALUES (?, ?, ?, ?, ?)',
                     (sid, face, person, dist, 1 if sid in marked else 0))
    conn.commit()
    conn.close()
    return path


def _marked(path):
    conn = sqlite3.connect(path)
    ids = {r[0] for r in conn.execute(
        'SELECT id FROM face_similarity_scores WHERE contextual_assigned = 1')}
    conn.close()
    return ids


# One photo (file 10) with two faces that both look most like person 1, and
# a second photo (file 20) whose only candidate is below the threshold.
SCORES = [
    (1, 100, 10, 1, 0.10),   # face 100 → person 1, the best pair in the photo
    (2, 101, 10, 1, 0.20),   # face 101 → person 1: taken, so...
    (3, 101, 10, 2, 0.30),   # ...face 101 → person 2
    (4, 200, 20, 3, 0.70),   # p = 0.30, below ASSIGNMENT_MIN_P
]


def test_one_person_per_photo(tmp_path):
    path = _catalog(tmp_path, SCORES)
    assert wf.resolve_per_photo_assignment(path) == 2
    assert _marked(path) == {1, 3}


def test_stale_marks_are_cleared(tmp_path):
    path = _catalog(tmp_path, SCORES, marked={2, 4})
    wf.resolve_per_photo_assignment(path)
    assert _marked(path) == {1, 3}


def test_a_confirmed_face_loses_its_mark(tmp_path):
    path = _catalog(tmp_path, SCORES, marked={1, 3})
    conn = sqlite3.connect(path)
    conn.execute('UPDATE face_detections SET person_id = 1 WHERE id = 100')
    conn.commit()
    conn.close()
    wf.resolve_per_photo_assignment(path)
    assert _marked(path) == {2}     # face 101 now gets person 1


def test_no_lock_is_taken_when_nothing_changes(tmp_path):
    """With every mark already right, it reads and finishes while another
    writer holds the lock - it never asks for it."""
    path = _catalog(tmp_path, SCORES, marked={1, 3})
    holder = sqlite3.connect(path, isolation_level=None)
    holder.execute('BEGIN IMMEDIATE')
    try:
        result = {}
        t = threading.Thread(target=lambda: result.update(n=wf.resolve_per_photo_assignment(path)))
        t.start()
        t.join(5)
        assert not t.is_alive(), 'it waited for the write lock'
        assert result['n'] == 2
    finally:
        holder.execute('ROLLBACK')
        holder.close()
