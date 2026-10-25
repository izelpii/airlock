#!/usr/bin/env python3
"""
Re-rank Matches by Context can be stopped.

    python -m pytest test_rerank_stop.py

It was 'never' in WORKFLOW_STOP_MODES, so the queue offered no Stop while it
ran - on `family` for half an hour at a time. The reason given was that it
rewrote every score in one pass; since v5.5.0 it writes a chunk of faces per
transaction, and its other writes are single transactions.
"""

import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import ai_contextual_recognition_workflow as wf    # noqa: E402
import workflow_scheduler as sched                  # noqa: E402

SCRIPT = 'ai_contextual_recognition_workflow.py'


def test_the_queue_offers_stop():
    assert sched.stop_mode(SCRIPT) == 'item'
    assert sched.stop_refusal(SCRIPT, 'Re-rank Matches by Context') is None


@pytest.fixture
def many_files(tmp_path):
    db = str(tmp_path / 'workflow_results.db')
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY, file_path TEXT,
            file_type TEXT, removed_at TIMESTAMP, perceptual_hash TEXT);
        CREATE TABLE alternative_versions (id INTEGER PRIMARY KEY,
            original_file_id INTEGER, alternative_file_path TEXT, rung TEXT,
            width INTEGER);
    """)
    (tmp_path / 'a.jpg').write_bytes(b'not read: dhash is replaced')
    c.executemany('INSERT INTO file_contents (id, file_path, file_type) VALUES (?, ?, ?)',
                  [(i, 'a.jpg', '.jpg') for i in range(1, 2 * wf.HASH_COMMIT_EVERY + 201)])
    c.commit()
    c.close()
    return str(tmp_path), db


def test_a_stop_while_hashing_keeps_the_hashes_already_made(many_files, monkeypatch):
    target, db = many_files
    monkeypatch.setattr(wf, 'dhash', lambda p: 'f' * 16)
    monkeypatch.setattr(wf, '_stop_check', lambda: True)
    with pytest.raises(wf._Stopped):
        wf.backfill_perceptual_hashes(db, target)
    c = sqlite3.connect(db)
    kept = c.execute('SELECT COUNT(*) FROM file_contents '
                     'WHERE perceptual_hash IS NOT NULL').fetchone()[0]
    c.close()
    # The first batch is committed before the stop is noticed - not all of
    # them, and not none, which is what one commit at the end gave.
    assert kept == wf.HASH_COMMIT_EVERY


def test_clustering_stops_between_blocks(monkeypatch):
    np = pytest.importorskip('numpy')
    calls = []
    monkeypatch.setattr(wf, '_scan_block', lambda start: calls.append(start) or (
        start, 1.0, np.empty(0, dtype=np.int32), np.empty(0, dtype=np.int32)))
    monkeypatch.setattr(wf, '_stop_check', lambda: len(calls) >= 2)
    matrix = np.zeros((wf.CLUSTER_BLOCK_SIZE * 5, 4), dtype=np.float32)
    with pytest.raises(wf._Stopped):
        wf._scan(matrix, 0.7)
    assert len(calls) == 2


def test_a_stopped_run_is_recorded_as_stopped(monkeypatch, tmp_path):
    recorded = {}

    def _stopping(catalog, db, target, cb, execution_id, progress):
        progress.update(done=4000, total=10000)
        raise wf._Stopped('re-ranking faces')
    monkeypatch.setattr(wf, '_execute', _stopping)
    monkeypatch.setattr(wf, 'record_stopped',
                        lambda db, eid, processed_files=None, message=None, **_:
                        recorded.update(eid=eid, done=processed_files, message=message))

    out = wf.execute(None, str(tmp_path), [], None, catalog=object(), execution_id=7)
    assert out['stopped'] is True
    assert recorded['eid'] == 7 and recorded['done'] == 4000
    assert '4,000 of 10,000' in recorded['message']
    # The check is put back: nothing outside a run can be stopped by it.
    assert wf._stop_check() is False
