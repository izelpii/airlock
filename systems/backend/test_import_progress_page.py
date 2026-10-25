"""
What the Import Progress page is told while an import is running.

    python -m pytest test_import_progress_page.py

homeserver, 2026-10-12: an "Import Files" run spent six minutes on housekeeping,
then an hour converting videos an earlier run had left. For the first six
minutes the page showed an older, interrupted import in its place, and for the
hour its render counter sat still between videos. Both read as a hang.
"""

import os
import sqlite3
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workflows'))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _imports(client):
    r = client.get('/api/catalogs/1/imports')
    assert r.status_code == 200, r.get_json()
    return r.get_json()['imports']


def test_an_import_files_run_is_listed_before_it_has_phase_rows(client, catalog):
    _imports(client)                         # the first request builds the schema
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    old = conn.execute("INSERT INTO workflows (name, description, python_script) VALUES "
                       "('Import old', '', 'workflows/io_picture_import_workflow.py')"
                       ).lastrowid
    new = conn.execute("INSERT INTO workflows (name, description, python_script) VALUES "
                       "('Import new', '', 'workflows/io_picture_import_v2_workflow.py')"
                       ).lastrowid
    conn.execute("INSERT INTO workflow_executions (id, workflow_id, workflow_name, status, "
                 "started_at) VALUES (37, ?, 'Import old', 'interrupted', '2026-10-07 12:31')",
                 (old,))
    conn.execute("INSERT INTO import_phases (execution_id, phase, ordinal, status) "
                 "VALUES (37, 'scan', 1, 'completed')")
    # Just started: housekeeping is still running, so no phase rows yet.
    conn.execute("INSERT INTO workflow_executions (id, workflow_id, workflow_name, status, "
                 "started_at) VALUES (131, ?, 'Import new', 'running', '2026-10-12 16:50')",
                 (new,))
    conn.commit()
    conn.close()

    listed = _imports(client)
    assert [(i['execution_id'], i['status']) for i in listed[:2]] == [
        (131, 'running'), (37, 'interrupted')]


def _reporter(tmp_path):
    """A _StreamRun stand-in with just what _report_videos touches."""
    import io_picture_import_v2_workflow as v2
    import threading
    submitted = []
    fake = SimpleNamespace(
        _video_lock=threading.Lock(), _videos_now={}, _videos_reported_at=0.0,
        _videos_reported_any=False,
        progress=SimpleNamespace(phases={'render': {'detail': {}}}, dirty=False),
        writer=SimpleNamespace(submit=lambda fn: submitted.append(fn)))

    def report(force=True):
        v2._StreamRun._report_videos(fake, force=force)
        for fn in submitted:
            fn(None)
        submitted.clear()
        return fake.progress.phases['render']['detail']
    return fake, report


def test_a_video_being_converted_is_reported_with_what_is_written(tmp_path):
    fake, report = _reporter(tmp_path)
    src = tmp_path / 'IMG_2936.MOV'
    src.write_bytes(b'x' * 1000)
    partial = tmp_path / 'IMG_2936_proxy.partial.t2-1.mp4'
    partial.write_bytes(b'y' * 300)
    fake._videos_now[1] = ('IMG_2936.MOV', str(src), 0.0,
                           str(tmp_path / 'IMG_2936_proxy.partial*'))

    detail = report()
    assert fake.progress.dirty
    [v] = detail['videos_now']
    assert (v['name'], v['size'], v['written']) == ('IMG_2936.MOV', 1000, 300)
    assert v['seconds'] >= 0

    # Finished: the line goes, once - and then nothing more is written.
    fake._videos_now.clear()
    assert 'videos_now' not in report()
    fake.progress.dirty = False
    report()
    assert not fake.progress.dirty


def test_reports_are_throttled(tmp_path):
    fake, report = _reporter(tmp_path)
    fake._videos_now[1] = ('a.mov', str(tmp_path / 'missing.mov'), 0.0,
                           str(tmp_path / 'a_proxy.partial*'))
    assert report(force=False)['videos_now'][0]['size'] is None   # source unreadable: no size
    fake.progress.phases['render']['detail'].clear()
    assert 'videos_now' not in report(force=False)                # within the interval
