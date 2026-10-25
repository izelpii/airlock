"""
Tagging faces waits five quiet minutes before Learn Tagged Faces starts.

    python -m pytest test_face_tag_quiet.py

A person tags in bursts. Each tag raises face_confirmed; the chain's first
workflow, Learn Tagged Faces, must not start until five minutes after the LAST
tag - every further automatic request restarts the timer - so it learns every
face of the burst at once instead of a partial set, with the next run queued
behind it. Starting it by hand (Run) skips the wait.
"""

import io
import os
import sqlite3
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import workflow_scheduler as sched  # noqa: E402
import workflow_triggers as trig  # noqa: E402

LEARN = {'id': 7, 'name': 'Learn Tagged Faces', 'python_script': 'workflows/ai_ingest_manual_faces.py'}


def _not_before(path, qid):
    conn = sqlite3.connect(path)
    try:
        value = conn.execute('SELECT not_before FROM workflow_queue WHERE id = ?', (qid,)).fetchone()[0]
    finally:
        conn.close()
    return datetime.fromisoformat(value) if value else None


def test_learn_tagged_faces_leads_the_chain_and_waits_five_minutes():
    assert trig.TRIGGERS['face_confirmed'][0] == 'ai_ingest_manual_faces.py'
    assert trig.quiet_seconds('face_confirmed') == 300


def test_every_further_tag_restarts_the_five_minutes(tmp_path):
    path = str(tmp_path / 'workflow_results.db')
    sqlite3.connect(path).close()
    delay = trig.quiet_seconds('face_confirmed')

    qid, created = sched.enqueue(path, LEARN, trigger_source='face_confirmed',
                                 trigger_detail={'faces': 1}, delay_seconds=delay)
    assert created
    first = _not_before(path, qid)
    assert timedelta(seconds=295) < first - datetime.now() <= timedelta(seconds=300)

    # Four minutes later, as far as the queue can tell: one minute left.
    conn = sqlite3.connect(path)
    conn.execute('UPDATE workflow_queue SET not_before = ? WHERE id = ?',
                 ((datetime.now() + timedelta(seconds=60)).isoformat(), qid))
    conn.commit()
    conn.close()

    again, created = sched.enqueue(path, LEARN, trigger_source='face_confirmed',
                                   trigger_detail={'faces': 1}, delay_seconds=delay)
    assert (again, created) == (qid, False)              # the same row, not a second run
    assert _not_before(path, qid) - datetime.now() > timedelta(seconds=295)

    # Run by hand: first in line, no wait.
    sched.promote(path, qid)
    assert _not_before(path, qid) is None


def test_a_face_drawn_and_named_raises_the_trigger():
    """Learn Tagged Faces reads source='Manual' rows with a person - which is
    exactly what drawing a box and naming it creates."""
    src = io.open(os.path.join(HERE, 'app.py'), encoding='utf-8').read()
    body = src[src.index('def create_manual_face('):src.index('def get_all_manual_faces(')]
    assert '_fire_face_confirmed(catalog' in body
