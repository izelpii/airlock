"""
Tagging a face queues Match Faces by Appearance for the next 3 am, not after five
minutes (owner, 2026-10-21): it runs for hours, and people tag while they use
the system. Anything that wants it now still gets it now.

    python -m pytest test_face_match_overnight.py
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import workflow_scheduler as sched  # noqa: E402
import workflow_triggers as trig  # noqa: E402

MATCH = {'id': 9, 'name': 'Match Faces by Appearance',
         'python_script': 'workflows/ai_face_recognition_workflow.py'}
UTC = timezone.utc


def _row(path, qid):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute('SELECT not_before, trigger_detail FROM workflow_queue '
                                 'WHERE id = ?', (qid,)).fetchone())
    finally:
        conn.close()


# ── when ─────────────────────────────────────────────────────────────────────

def test_the_next_3am_in_new_york_from_a_utc_clock():
    # 14:00 in New York (EDT, UTC-4) is 18:00 UTC: tonight's 3 am is 07:00 UTC.
    assert trig.next_nightly(datetime(2026, 10, 21, 18, 0, tzinfo=UTC)) == datetime(2026, 10, 22, 7, 0)
    # 1 am in New York: 3 am the same night.
    assert trig.next_nightly(datetime(2026, 10, 22, 5, 0, tzinfo=UTC)) == datetime(2026, 10, 22, 7, 0)
    # Just past 3 am: tomorrow's.
    assert trig.next_nightly(datetime(2026, 10, 22, 7, 1, tzinfo=UTC)) == datetime(2026, 10, 23, 7, 0)
    # The night summer time ends (1 November): 3 am EST is 08:00 UTC.
    assert trig.next_nightly(datetime(2026, 11, 1, 4, 30, tzinfo=UTC)) == datetime(2026, 11, 1, 8, 0)


def test_only_match_faces_by_appearance_waits_for_the_night():
    now = datetime(2026, 10, 21, 18, 0, tzinfo=UTC)
    delay, overnight = trig.delay_for('face_confirmed', 'ai_face_recognition_workflow.py', now)
    assert overnight and delay == 13 * 3600
    assert trig.delay_for('face_confirmed', 'ai_ingest_manual_faces.py', now) == (300, False)
    assert trig.delay_for('face_confirmed', 'ai_contextual_recognition_workflow.py', now) == (300, False)
    # An import or a detection pass is not a person tagging: no change for those.
    assert trig.delay_for('faces_detected', 'ai_face_recognition_workflow.py', now) == (0, False)


# ── the queue ────────────────────────────────────────────────────────────────

def _night(path):
    delay, overnight = trig.delay_for('face_confirmed', 'ai_face_recognition_workflow.py')
    return sched.enqueue(path, MATCH, trigger_source='face_confirmed',
                         trigger_detail={'faces': 1, 'nightly': True},
                         delay_seconds=delay, overnight=overnight)


def test_a_tag_queues_it_for_the_night_and_more_tags_fold_in(tmp_path):
    path = str(tmp_path / 'workflow_results.db')
    sqlite3.connect(path).close()
    qid, created = _night(path)
    assert created
    row = _row(path, qid)
    at = datetime.fromisoformat(row['not_before'])
    assert timedelta(0) <= at - trig.next_nightly() < timedelta(seconds=2)   # 3 am, not before
    assert '"nightly": true' in row['trigger_detail']
    again, created = _night(path)
    assert (again, created) == (qid, False)
    assert abs(datetime.fromisoformat(_row(path, qid)['not_before']) - at) < timedelta(seconds=2)


def test_an_import_or_the_run_button_starts_it_now(tmp_path):
    path = str(tmp_path / 'workflow_results.db')
    sqlite3.connect(path).close()
    qid, _ = _night(path)
    # An import finishing, or Run on the Workflows page: no delay.
    same, created = sched.enqueue(path, MATCH, trigger_source='manual')
    assert (same, created) == (qid, False)
    row = _row(path, qid)
    assert row['not_before'] is None and 'nightly' not in row['trigger_detail']


def test_a_tag_never_postpones_a_run_already_due(tmp_path):
    path = str(tmp_path / 'workflow_results.db')
    sqlite3.connect(path).close()
    qid, _ = sched.enqueue(path, MATCH, trigger_source='faces_detected')     # due now
    same, created = _night(path)
    assert (same, created) == (qid, False)
    assert _row(path, qid)['not_before'] is None


def test_now_in_the_queue_skips_the_wait(tmp_path):
    path = str(tmp_path / 'workflow_results.db')
    sqlite3.connect(path).close()
    qid, _ = _night(path)
    ok, _msg = sched.run_now(path, qid)
    assert ok and _row(path, qid)['not_before'] is None


def test_the_queue_reports_how_long_until_the_night(tmp_path):
    path = str(tmp_path / 'workflow_results.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE workflow_executions (id INTEGER PRIMARY KEY, processed_files INTEGER, '
                 'total_files INTEGER, status TEXT)')
    conn.close()
    qid, _ = _night(path)
    item = next(i for i in sched.list_queue(path) if i['id'] == qid)
    expected = (trig.next_nightly() - datetime.now()).total_seconds()
    assert abs(item['waiting_seconds'] - expected) < 5
    assert item['trigger_detail']['nightly'] is True
    assert item['waiting_seconds'] > 0 and timedelta(seconds=item['waiting_seconds']) <= timedelta(days=1)
