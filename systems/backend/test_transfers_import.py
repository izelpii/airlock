#!/usr/bin/env python3
"""
Every catalog has an "Import transfers" workflow, and a received album is
imported and then set aside (documentation/features/transfers-import.md).

    python -m pytest test_transfers_import.py

Before this, nothing imported what another Airlock sent: albums sat in
<catalog>/transfers/ until someone built a workflow by hand - which a storage
route could only reach where the catalogs happen to sit inside the storage
root, and which reference- and move-mode catalogs refused anyway.
"""

import json
import os
import sqlite3
import sys

import pytest

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, 'workflows'))


def _transfers_workflows(client, catalog):
    r = client.get(f'/api/catalogs/{catalog.id}/workflows')
    assert r.status_code == 200, r.get_json()
    root = os.path.join(catalog.target_directory, 'transfers')
    return [w for w in r.get_json() if os.path.normpath(w.get('source_directory') or '')
            == os.path.normpath(root)]


# ── always there ─────────────────────────────────────────────────────────────

def test_every_catalog_has_the_transfers_import_once(client, catalog):
    first = _transfers_workflows(client, catalog)
    assert [w['name'] for w in first] == ['Import transfers']
    assert first[0]['workflow_type'] == 'Import'
    assert first[0]['python_script'].endswith('io_picture_import_v2_workflow.py')
    assert os.path.isdir(os.path.join(catalog.target_directory, 'transfers'))
    # The list and the graph both ensure it; neither adds a second.
    client.get(f'/api/catalogs/{catalog.id}/workflow-graph')
    assert [w['id'] for w in _transfers_workflows(client, catalog)] == [first[0]['id']]


def test_it_comes_back_when_deleted(client, catalog):
    (wf,) = _transfers_workflows(client, catalog)
    r = client.delete(f'/api/catalogs/{catalog.id}/workflows/{wf["id"]}')
    assert r.status_code == 200
    again = _transfers_workflows(client, catalog)
    assert len(again) == 1 and again[0]['id'] != wf['id']


def test_it_is_wired_into_the_pipeline_like_any_import(client, catalog, app_module):
    import workflow_defaults
    workflow_defaults.seed_default_workflows(app_module.CatalogDatabase(catalog.target_directory))
    g = client.get(f'/api/catalogs/{catalog.id}/workflow-graph').get_json()
    (node,) = [n for n in g['nodes'] if n['name'] == 'Import transfers']
    assert node['type'] == 'Import' and not node['needs_configuration']
    downstream = {e['to'] for e in g['edges'] if e['from'] == node['id']}
    assert downstream, 'nothing waits for the transfers import'


# ── launched as the transfers import ─────────────────────────────────────────

class _FakeProc:
    pid = 4242


def test_it_runs_with_transfers_and_a_route_import_does_not(app_module, catalog, monkeypatch):
    seen = []
    monkeypatch.setattr(app_module.subprocess, 'Popen',
                        lambda cmd, **kw: seen.append(cmd) or _FakeProc())
    monkeypatch.setattr(app_module, '_start_subprocess_watcher', lambda *a, **k: None)
    wf = app_module._ensure_transfers_import_workflow(catalog)
    app_module.start_workflow_execution(catalog, wf)
    assert '--transfers' in seen[-1]
    assert seen[-1][seen[-1].index('--source-dir') + 1] == \
        os.path.join(catalog.target_directory, 'transfers')

    other = dict(wf, id=wf['id'] + 1000, source_directory='/storage/photos')
    app_module.start_workflow_execution(catalog, other)
    assert '--transfers' not in seen[-1]


def test_a_completed_receive_queues_it(app_module, catalog):
    app_module._queue_transfers_import(catalog, 7)
    wf = app_module._ensure_transfers_import_workflow(catalog)
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    rows = conn.execute('SELECT workflow_id, trigger_source, trigger_detail, status '
                        'FROM workflow_queue').fetchall()
    conn.close()
    assert [(r[0], r[1], r[3]) for r in rows] == [(wf['id'], 'transfer_received', 'queued')]
    assert json.loads(rows[0][2]) == {'transfer_id': 7}


class _WatchedProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_the_watcher_calls_back_only_on_completion(tmp_path):
    import wormhole_transfer as wt
    from test_wormhole_batches import FakeCatalogDB
    db = FakeCatalogDB(str(tmp_path / 'w.db'))
    tid = wt.create_receive_transfer(db, '1-a-b', None, None)
    called = []
    wt._watch(db, tid, _WatchedProc(['{"event": "finished"}\n']),
              lambda *a: None, None, on_completed=called.append)
    assert called == [tid]
    tid2 = wt.create_receive_transfer(db, '2-a-b', None, None)
    wt._watch(db, tid2, _WatchedProc(['{"event": "error", "message": "x"}\n'], 1),
              lambda *a: None, None, on_completed=called.append)
    assert called == [tid], 'a failed receive queues nothing'


# ── after the import: set aside ─────────────────────────────────────────────

def _ops_db(tmp_path, ops):
    path = str(tmp_path / 'workflow_results.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE file_operations (id INTEGER PRIMARY KEY, execution_id INTEGER, '
                 'source_path TEXT, status TEXT)')
    conn.executemany('INSERT INTO file_operations (execution_id, source_path, status) '
                     'VALUES (?, ?, ?)', ops)
    conn.commit()
    conn.close()
    return path


def test_imported_albums_move_to_consumed(tmp_path):
    import io_picture_import_workflow as v1
    root = tmp_path / 'transfers'
    for album in ('00001-done', '00002-failed', '00003-arrived-during', '00004-done'):
        (root / album).mkdir(parents=True)
        (root / album / 'a.jpg').write_bytes(b'x')
    (root / 'consumed' / '00004-done').mkdir(parents=True)      # already a 00004 there
    db = _ops_db(tmp_path, [
        (5, str(root / '00001-done' / 'a.jpg'), 'success'),
        (5, str(root / '00002-failed' / 'a.jpg'), 'failed'),
        (5, str(root / '00004-done' / 'a.jpg'), 'skipped'),        # a duplicate is done too
        (4, str(root / '00003-arrived-during' / 'a.jpg'), 'success'),  # another run's
    ])
    assert v1._consume_transfer_albums(str(root), db, 5) == 2
    assert sorted(os.listdir(root)) == ['00002-failed', '00003-arrived-during', 'consumed']
    assert sorted(os.listdir(root / 'consumed')) == ['00001-done', '00004-done', '00004-done (2)']


def test_the_walk_skips_consumed(tmp_path):
    import io_picture_import_workflow as v1
    root = tmp_path / 'transfers'
    (root / 'consumed' / 'old').mkdir(parents=True)
    (root / 'consumed' / 'old' / 'a.jpg').write_bytes(b'x')
    skip = (str(root / 'consumed'),)
    assert not v1._first_file(str(root), skip=skip), 'only consumed albums: nothing new'
    (root / 'new').mkdir()
    (root / 'new' / 'b.jpg').write_bytes(b'x')
    files = [p for kind, p, _ in v1._walk(str(root), skip=skip) if kind == 'file']
    assert files == [str(root / 'new' / 'b.jpg')]


# ── the importer's entry point with --transfers ──────────────────────────────

def _exec_db(path):
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE IF NOT EXISTS workflow_executions (id INTEGER PRIMARY KEY, '
                 'status TEXT, completed_at TEXT, error_message TEXT, skipped_files INTEGER)')
    conn.execute("INSERT INTO workflow_executions (id, status) VALUES (9, 'running')")
    conn.commit()
    conn.close()


def _run_main(monkeypatch, tmp_path, ingest_mode, make_files=True):
    import io_picture_import_workflow as v1
    from types import SimpleNamespace
    target = tmp_path / 'cat'
    root = target / 'transfers'
    (root / 'consumed' / 'old').mkdir(parents=True)
    (root / 'consumed' / 'old' / 'z.jpg').write_bytes(b'x')
    if make_files:
        (root / '00001-new').mkdir()
        (root / '00001-new' / 'a.jpg').write_bytes(b'x')
    db = str(target / 'workflow_results.db')
    _ops_db(target, [])
    _exec_db(db)
    cat = SimpleNamespace(id=3, ingest_mode=ingest_mode, source_root='/storage/library')
    monkeypatch.setattr(v1.Catalog, 'get_by_id', classmethod(lambda cls, cid: cat))
    seen = {}

    def fake_execute(**kw):
        c = kw['catalog']
        seen['mode'] = c.ingest_mode
        seen['skip'] = getattr(c, 'import_skip_dirs', ())
        conn = sqlite3.connect(db)
        conn.execute('INSERT INTO file_operations (execution_id, source_path, status) '
                     'VALUES (9, ?, ?)', (str(root / '00001-new' / 'a.jpg'), 'success'))
        conn.commit()
        conn.close()
        return {'skipped_files': 0}

    monkeypatch.setattr(sys, 'argv', ['import', '--execution-id', '9', '--source-dir', str(root),
                                      '--target-dir', str(target), '--catalog-id', '3',
                                      '--transfers'])
    v1.main(execute_fn=fake_execute)
    status = sqlite3.connect(db).execute(
        'SELECT status FROM workflow_executions WHERE id = 9').fetchone()[0]
    return seen, status, root


@pytest.mark.parametrize('mode', ['reference', 'move', 'copy'])
def test_a_received_album_is_copied_whatever_the_catalog_mode(monkeypatch, tmp_path, mode):
    seen, status, root = _run_main(monkeypatch, tmp_path, mode)
    assert seen['mode'] == 'copy'
    assert seen['skip'] == (str(root / 'consumed'),)
    assert status == 'completed'
    assert sorted(os.listdir(root / 'consumed')) == ['00001-new', 'old']
    assert sorted(os.listdir(root)) == ['consumed']


def test_nothing_received_yet_completes_instead_of_failing(monkeypatch, tmp_path):
    seen, status, root = _run_main(monkeypatch, tmp_path, 'copy', make_files=False)
    assert status == 'completed'
    assert seen == {}, 'no import ran'
