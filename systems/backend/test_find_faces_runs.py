#!/usr/bin/env python3
"""
Find Faces, run end to end: decode once, one connection per worker, the crop
rows and the scanned mark in one transaction, and only outstanding images.

    python -m pytest test_find_faces_runs.py

The real loop, on real JPEGs, with the detector faked at the HTTP edge. On
`family` a run took 11.6 hours; what is checked here is what made it slow
and what made it wrong:

  - every face decoded its photograph again (~469,000 extra decodes)
  - a connection per step and per face, committing at synchronous=FULL
  - every image submitted to the pool at once, every future held
  - a file marked scanned BEFORE its crops were made
"""

import os
import sqlite3
import sys

import pytest
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import ai_auto_detect_faces_workflow as wf    # noqa: E402


class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self._payload


@pytest.fixture
def cat(tmp_path):
    target = tmp_path / 'cat'
    (target / 'Images').mkdir(parents=True)
    db = str(target / 'workflow_results.db')
    conn = sqlite3.connect(db)
    conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY, file_path TEXT,
            file_name TEXT, file_type TEXT, removed_at TIMESTAMP,
            image_group_id INTEGER, exif_data_id INTEGER, master_datetime TEXT,
            face_detection_scanned_at TIMESTAMP);
        CREATE TABLE image_groups (id INTEGER PRIMARY KEY, primary_file_id INTEGER);
        CREATE TABLE exif_data (id INTEGER PRIMARY KEY, creation_date TEXT);
        CREATE TABLE alternative_versions (id INTEGER PRIMARY KEY,
            original_file_id INTEGER, alternative_file_path TEXT,
            alternative_file_name TEXT, characteristic TEXT, rung TEXT,
            width INTEGER, height INTEGER);
        CREATE TABLE face_detections (id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER, x INTEGER, y INTEGER, w INTEGER, h INTEGER,
            confidence REAL, person_id INTEGER, source TEXT,
            image_width INTEGER, image_height INTEGER, created_at TEXT);
        CREATE TABLE catalog_properties (key TEXT PRIMARY KEY, value TEXT);
    """)
    for i in range(1, 6):
        name = f'p{i}.jpg'
        Image.new('RGB', (800, 600), (40 * i, 80, 120)).save(target / 'Images' / name)
        conn.execute("INSERT INTO file_contents (id, file_path, file_name, file_type, "
                     "master_datetime) VALUES (?, ?, ?, '.jpg', '2021-05-05T10:00:00')",
                     (i, f'Images/{name}', name))
    conn.commit()
    conn.close()
    return str(target), db


@pytest.fixture
def detector(monkeypatch):
    """One face per photograph; every upload recorded."""
    uploads = []
    monkeypatch.setattr(wf.requests, 'get', lambda *a, **k: _Resp({}, 200))

    def post(url, files=None, **k):
        uploads.append(url)
        return _Resp({'success': True, 'faces': [
            {'x': 100, 'y': 100, 'w': 120, 'h': 120, 'confidence': 0.99}]})
    monkeypatch.setattr(wf.requests, 'post', post)
    monkeypatch.setattr(wf, 'DETECTION_CONCURRENCY', 2)
    return uploads


def _run(target):
    return wf.execute(None, target, [], None, catalog=object())


def _q(db, sql, *p):
    c = sqlite3.connect(db)
    try:
        return c.execute(sql, p).fetchall()
    finally:
        c.close()


def test_each_photograph_is_decoded_once_and_its_faces_cropped_from_that(cat, detector, monkeypatch):
    target, db = cat
    reopened = []
    monkeypatch.setattr(wf, '_open_and_orient',
                        lambda path: reopened.append(path) or Image.new('RGB', (800, 600)))
    out = _run(target)
    assert out['faces_detected'] == 5 and out['face_crops_saved'] == 5
    assert reopened == []                              # no second decode
    assert len(_q(db, 'SELECT id FROM ai_face_contents')) == 5
    for (path,) in _q(db, 'SELECT file_path FROM ai_face_contents'):
        assert os.path.exists(os.path.join(target, path))


def test_a_downscaled_detection_does_not_lend_its_pixels_to_the_crop(cat, detector, monkeypatch):
    """Crops come from full resolution: only an unscaled decode is reused."""
    target, db = cat
    monkeypatch.setattr(wf, 'DETECTION_MAX_DIM', 400)  # forces a resize
    reopened = []
    real = wf._open_and_orient
    monkeypatch.setattr(wf, '_open_and_orient',
                        lambda path: reopened.append(path) or real(path))
    _run(target)
    assert len(reopened) == 5


def test_a_file_is_marked_scanned_with_its_crops_and_not_before(cat, detector, monkeypatch):
    target, db = cat

    def boom(*a, **k):
        raise RuntimeError('disk full')
    monkeypatch.setattr(wf, '_get_new_detections_for_file', boom)
    _run(target)
    # The crash came after detection and before the crops: nothing is marked,
    # so the next run does these files again rather than never.
    assert _q(db, 'SELECT COUNT(*) FROM file_contents '
                  'WHERE face_detection_scanned_at IS NOT NULL')[0][0] == 0


def test_only_outstanding_images_are_loaded(cat, detector):
    target, db = cat
    c = sqlite3.connect(db)
    c.execute("UPDATE file_contents SET face_detection_scanned_at = 'x' WHERE id IN (1, 2, 3)")
    c.commit()
    c.close()
    images, total = wf.get_outstanding_images(db, pets_enabled=False)
    assert total == 5
    assert [i['file_id'] for i in images] == [4, 5]
    assert all(i['need_faces'] and not i['need_pets'] for i in images)
    out = _run(target)
    assert out['images_skipped'] == 3 and len(detector) == 2


def test_a_second_run_finds_nothing_to_do(cat, detector):
    target, db = cat
    _run(target)
    detector.clear()
    out = _run(target)
    assert detector == [] and out['images_skipped'] == 5


def test_a_workers_connection_is_kept_and_commits_at_normal(cat):
    _, db = cat
    c1 = wf._conn(db)
    c2 = wf._conn(db)
    assert c1 is c2
    assert c1.execute('PRAGMA synchronous').fetchone()[0] == 1   # NORMAL
    wf.close_thread_connections()
    assert wf._conn(db) is not c1                                 # reopened after close
    wf.close_thread_connections()
