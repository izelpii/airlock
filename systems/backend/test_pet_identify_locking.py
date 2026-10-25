#!/usr/bin/env python3
"""
Measure and Match Pets must not hold the catalog's write lock between batches.

    python -m pytest test_pet_identify_locking.py

On `family` it held it for the whole crop phase. Each batch's rows were left in
an open transaction while progress() - which writes workflow_executions on a
connection of its own - waited out its 30 s busy timeout behind them: 50 crops
per 35 s, progress stuck at 0, and Match Faces, running alongside, failing
face after face with "database is locked".

The progress callback here does what the real one does, a write on a second
connection, but with a short timeout and without swallowing the error: if the
phase still holds the lock when it reports, the test fails instead of
waiting.
"""

import json
import os
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import pe_identify_pets as pe                 # noqa: E402
from pet_schema import ensure_pet_schema      # noqa: E402


def _catalog(tmp_path, detections):
    path = str(tmp_path / 'workflow_results.db')
    conn = sqlite3.connect(path)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY, file_name TEXT,
                                    file_path TEXT, removed_at TIMESTAMP);
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, kind TEXT);
        CREATE TABLE workflow_executions (id INTEGER PRIMARY KEY,
                                          processed_files INTEGER,
                                          total_files INTEGER);
        INSERT INTO workflow_executions (id) VALUES (1);
        INSERT INTO file_contents (id, file_name, file_path) VALUES (1, 'a.jpg', 'a.jpg');
    """)
    conn.commit()
    conn.close()
    ensure_pet_schema(path)
    conn = sqlite3.connect(path)
    conn.executemany('INSERT INTO pet_detections (file_id, x, y, w, h, species) '
                     "VALUES (1, 0, 0, 10, 10, 'dog')", [()] * detections)
    conn.commit()
    conn.close()
    return path


def _strict_progress(path, calls):
    """A progress write that fails fast rather than waiting for the lock."""
    def progress(done, total):
        c = sqlite3.connect(path, timeout=0.2)
        try:
            c.execute('UPDATE workflow_executions SET processed_files=?, '
                      'total_files=? WHERE id=1', (done, total))
            c.commit()
        finally:
            c.close()
        calls.append(done)
    return progress


def _open(path):
    conn = sqlite3.connect(path, timeout=5)
    conn.row_factory = sqlite3.Row
    return conn


def test_the_crop_phase_lets_its_progress_be_written(tmp_path, monkeypatch):
    path = _catalog(tmp_path, detections=120)
    crop = tmp_path / 'crop.jpg'
    crop.write_bytes(b'x')
    monkeypatch.setattr(pe, 'crop_one',
                        lambda conn, det, target: ('cache/pets-ai/c.jpg', str(crop)))
    calls = []
    conn = _open(path)
    try:
        made, total = pe.phase_crop(conn, str(tmp_path), _strict_progress(path, calls))
    finally:
        conn.close()
    assert (made, total) == (120, 120)
    assert calls == [50, 100]


def test_the_score_phase_lets_its_progress_be_written(tmp_path):
    path = _catalog(tmp_path, detections=122)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO people (id, name, kind) VALUES (7, 'Rex', 'pet')")
    conn.execute('UPDATE pet_detections SET person_id = 7 WHERE id IN (1, 2)')
    for det_id in range(1, 123):
        vec = [1.0, float(det_id % 5), 0.5, 0.25]
        conn.execute('INSERT INTO ai_pet_contents (file_path, file_name, '
                     ' pet_detection_id, source_file_id, embedding) '
                     "VALUES ('c.jpg', 'c.jpg', ?, 1, ?)", (det_id, json.dumps(vec)))
    conn.commit()
    conn.close()

    calls = []
    conn = _open(path)
    try:
        pe.phase_score(conn, 1, _strict_progress(path, calls))
    finally:
        conn.close()
    assert calls == [0, 100]           # it counts from 0


# ── Group Unnamed Pets: the same lock, in its rebuild ───────────────────────
#
# It deleted the offered groups, then inserted the new ones reporting progress
# after each - on `family` 30 s a group behind its own open transaction, the
# counter at 0 throughout. It now computes every group first and writes them
# in one short transaction.

import pe_cluster_pets as pc                  # noqa: E402


def _measured(path, groups, named=None):
    """Unnamed animals with embeddings: `groups` lists of near-identical vectors."""
    conn = sqlite3.connect(path)
    det = 0
    for g, size in enumerate(groups):
        base = [0.0] * 8
        base[g] = 1.0
        for k in range(size):
            det += 1
            vec = list(base)
            vec[(g + 1) % 8] = 0.01 * k             # close, not identical
            conn.execute('UPDATE pet_detections SET person_id = NULL WHERE id = ?', (det,))
            conn.execute('INSERT INTO ai_pet_contents (file_path, file_name, '
                         ' pet_detection_id, source_file_id, embedding) '
                         "VALUES ('c.jpg', 'c.jpg', ?, 1, ?)", (det, json.dumps(vec)))
    if named:
        conn.execute("INSERT INTO people (id, name, kind) VALUES (7, 'Rex', 'pet')")
        conn.execute("INSERT INTO pet_clusters (id, centroid, member_count, "
                     " resolved_person_id, resolved_at) VALUES (99, '[]', 1, 7, "
                     " '2026-01-01')")
        conn.execute('INSERT INTO pet_cluster_members (cluster_id, pet_detection_id, '
                     ' distance_to_centroid) VALUES (99, ?, 0.0)', (named,))
        # A named group's member carries the pet's name; one that does not was
        # unnamed since, and is free to be grouped again (v5.4.29).
        conn.execute('UPDATE pet_detections SET person_id = 7 WHERE id = ?', (named,))
    conn.commit()
    conn.close()


def test_grouping_lets_its_progress_be_written_and_keeps_named_groups(tmp_path):
    path = _catalog(tmp_path, detections=10)
    _measured(path, groups=[4, 3, 3], named=1)       # animal 1 is in Rex's group
    calls = []
    result = pc.execute(None, str(tmp_path), [], _strict_progress(path, calls))
    assert result['clusters'] == 3
    assert calls and calls[-1] == 9                  # 10 animals, 1 spoken for

    conn = sqlite3.connect(path)
    try:
        offered = conn.execute('SELECT member_count FROM pet_clusters '
                               'WHERE resolved_at IS NULL ORDER BY member_count DESC'
                               ).fetchall()
        named = conn.execute('SELECT COUNT(*) FROM pet_clusters WHERE id = 99').fetchone()[0]
    finally:
        conn.close()
    assert [r[0] for r in offered] == [3, 3, 3]
    assert named == 1


def test_a_second_rebuild_replaces_the_offered_groups_not_adds_to_them(tmp_path):
    path = _catalog(tmp_path, detections=6)
    _measured(path, groups=[3, 3])
    pc.execute(None, str(tmp_path), [], None)
    pc.execute(None, str(tmp_path), [], _strict_progress(path, []))
    conn = sqlite3.connect(path)
    try:
        assert conn.execute('SELECT COUNT(*) FROM pet_clusters').fetchone()[0] == 2
        assert conn.execute('SELECT COUNT(*) FROM pet_cluster_members').fetchone()[0] == 6
    finally:
        conn.close()
