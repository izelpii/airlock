#!/usr/bin/env python3
"""
Re-rank's perceptual hash backfill tries each file once it can be read, and
never again a file it cannot.

    python -m pytest test_perceptual_hash_backfill.py

On `family` 69,090 files had no hash: 62,000 videos plus RAW and HEIC files
with no base derivative. Every run opened each one, failed, and left it for
the next run. That is minutes of a setup that reported 0 of 0, and it read as
a hung workflow.
"""

import os
import sqlite3
import sys

import pytest
from PIL import Image

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import ai_contextual_recognition_workflow as wf    # noqa: E402


@pytest.fixture
def cat(tmp_path):
    db = str(tmp_path / 'workflow_results.db')
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY, file_path TEXT,
            file_type TEXT, removed_at TIMESTAMP, perceptual_hash TEXT);
        CREATE TABLE alternative_versions (id INTEGER PRIMARY KEY,
            original_file_id INTEGER, alternative_file_path TEXT, rung TEXT,
            width INTEGER);
    """)
    Image.new('RGB', (64, 48), (200, 10, 10)).save(tmp_path / 'a.jpg')
    Image.new('RGB', (64, 48), (10, 200, 10)).save(tmp_path / 'b_base.jpg')
    (tmp_path / 'c.mov').write_bytes(b'\0' * 4096)
    (tmp_path / 'd.cr3').write_bytes(b'\0' * 4096)
    c.executemany('INSERT INTO file_contents (id, file_path, file_type) VALUES (?, ?, ?)',
                  [(1, 'a.jpg', '.jpg'), (2, 'b.heic', '.heic'),
                   (3, 'c.mov', '.mov'), (4, 'd.cr3', '.cr3')])
    c.execute("INSERT INTO alternative_versions (original_file_id, alternative_file_path, "
              "rung, width) VALUES (2, 'b_base.jpg', '1200', 64)")
    c.commit()
    c.close()
    return str(tmp_path), db


def test_videos_and_raws_without_a_derivative_are_not_opened(cat, monkeypatch):
    target, db = cat
    opened = []
    real = wf.dhash
    monkeypatch.setattr(wf, 'dhash', lambda p: opened.append(os.path.basename(p)) or real(p))
    out = wf.backfill_perceptual_hashes(db, target)
    assert sorted(opened) == ['a.jpg', 'b_base.jpg']
    assert out['hashed'] == 2 and out['skipped'] == 0 and out['no_derivative_yet'] == 2


def test_a_file_waiting_is_hashed_once_its_derivative_exists(cat):
    target, db = cat
    wf.backfill_perceptual_hashes(db, target)
    Image.new('RGB', (64, 48), (10, 10, 200)).save(os.path.join(target, 'd_base.jpg'))
    c = sqlite3.connect(db)
    c.execute("INSERT INTO alternative_versions (original_file_id, alternative_file_path, "
              "rung, width) VALUES (4, 'd_base.jpg', '1200', 64)")
    c.commit()
    out = wf.backfill_perceptual_hashes(db, target)
    assert out['hashed'] == 1 and out['no_derivative_yet'] == 1
    assert c.execute('SELECT perceptual_hash IS NOT NULL FROM file_contents '
                     'WHERE id = 4').fetchone()[0] == 1
    c.close()


def test_hashing_reports_progress(cat, monkeypatch):
    target, db = cat
    seen = []
    wf.backfill_perceptual_hashes(db, target, lambda done, total: seen.append((done, total)))
    assert seen[-1] == (2, 2)


def test_workflow_subprocesses_are_unbuffered():
    """Without -u the log arrives in 8 KB blocks and a slow phase looks hung."""
    src = open(os.path.join(HERE, 'app.py')).read()
    assert "cmd = [sys.executable, '-u', script_path," in src
