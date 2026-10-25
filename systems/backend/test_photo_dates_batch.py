"""
Changing many photos' dates at once (POST /photos/dates), and the single-photo
endpoint it shares its core with.

    python -m pytest test_photo_dates_batch.py

See documentation/specs/mass-edit-event-photo-dates-spec.md section 7.

app.py is imported with every database pointed at a temporary folder: the real
backend/database/ is the running container's live mount, and opening it from
here is how a WAL file gets corrupted.
"""

import importlib
import json
import os
import sqlite3
import sys
import time
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)


# ─── The app, isolated ────────────────────────────────────────────────────────

@pytest.fixture(scope='session')
def app_module(tmp_path_factory):
    tmp = tmp_path_factory.mktemp('appdb')
    os.environ['DISABLE_WORKFLOW_SCHEDULER'] = '1'
    os.environ['AIRLOCK_AUTH_REQUIRED'] = '0'

    import catalog as catalog_module
    import models
    catalog_module.Catalog.DB_PATH = str(tmp / 'catalogs.db')
    original_init = models.Database.__init__

    def _init(self, db_path=None):
        original_init(self, db_path or str(tmp / 'catalogs.db'))
    models.Database.__init__ = _init

    import auth
    original_install = auth.install

    def _install(app, db, enabled=True):
        # Its secret key lives beside the live database; a test key will do.
        app.secret_key = 'test'
        try:
            return original_install(app, db, enabled=enabled)
        except Exception:
            return None
    auth.install = _install

    module = importlib.import_module('app')
    module.app.config['TESTING'] = True
    return module


@pytest.fixture
def catalog(app_module, tmp_path, monkeypatch):
    """
    A fresh catalog per test - and a fresh process to go with it, as far as the
    date-batch machinery can tell.

    app_module is SESSION scoped, so app.py's module globals and its daemon
    threads outlive every test. Two things leak without the cleanup below, and
    both of them made this file flaky for a long time:

      1. _date_batch_threads. Every test gets a new catalog whose batch ids
         restart at 1, and the fixture always calls it catalog 1 - so a
         straggler from the previous test discards the key belonging to this
         test's batch, and a running batch reads as 'interrupted'. (Keying that
         set by (catalog_id, batch_id) fixed the same collision between two
         real catalogs; it cannot help here, where every catalog IS id 1.)

      2. The threads themselves. A worker still re-dating files in the previous
         test's tmp_path finds the directory deleted underneath it, and the
         sqlite error surfaces in whichever test happens to be running.

    So: clear the registry going in, and wait for the workers going out.
    """
    target = tmp_path / 'catalog'
    target.mkdir()
    cat = SimpleNamespace(id=1, name='Test', target_directory=str(target),
                          ingest_mode='copy', source_root=None)
    monkeypatch.setattr(app_module.Catalog, 'get_by_id',
                        classmethod(lambda cls, cid: cat if cid == 1 else None))
    app_module.CatalogDatabase(str(target))        # creates every table

    with app_module._date_batch_threads_lock:
        app_module._date_batch_threads.clear()

    yield cat

    import threading as _t
    deadline = time.time() + 10
    for t in [t for t in _t.enumerate() if t.name.startswith('date-batch-')]:
        t.join(max(0, deadline - time.time()))
    with app_module._date_batch_threads_lock:
        app_module._date_batch_threads.clear()


@pytest.fixture
def client(app_module):
    return app_module.app.test_client()


def _db(catalog):
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    return conn


def _add_file(catalog, name, day='20261002', sha='9a4bfb64ba', with_alt=True,
              image_group_id=None, removed=False):
    """A catalogued photo on disk, filed the way the import files it."""
    stored = f'{day}235026-{sha}-{name}'
    rel = os.path.join('Images', day[:4], day[4:6], day[6:], stored)
    path = os.path.join(catalog.target_directory, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as f:
        f.write(b'jpeg')
    conn = _db(catalog)
    cur = conn.execute(
        "INSERT INTO file_contents (file_path, file_name, file_size, file_type, sha256_hash, "
        "master_datetime, master_datetime_source, image_group_id, removed_at) "
        "VALUES (?, ?, 4, '.jpg', ?, ?, 'filesystem', ?, ?)",
        (rel, stored, 'f' * 54 + sha, f'{day[:4]}-{day[4:6]}-{day[6:]}T23:50:26',
         image_group_id, '2026-10-02' if removed else None))
    fid = cur.lastrowid
    conn.execute("INSERT INTO exif_data (file_id, file_name, creation_date) VALUES (?, ?, ?)",
                 (fid, stored, '0000:00:00 00:00:00'))
    if with_alt:
        alt_rel = os.path.join('cache', 'Images', day[:4], day[4:6], day[6:],
                               os.path.splitext(stored)[0] + '_alt_1200.jpg')
        alt_abs = os.path.join(catalog.target_directory, alt_rel)
        os.makedirs(os.path.dirname(alt_abs), exist_ok=True)
        with open(alt_abs, 'wb') as f:
            f.write(b'alt')
        conn.execute(
            "INSERT INTO alternative_versions (original_file_id, alternative_file_path, "
            "alternative_file_name, characteristic, rung, width) "
            "VALUES (?, ?, ?, 'TYPE ALTERNATIVE 1200', '1200', 1200)",
            (fid, alt_rel, os.path.basename(alt_rel)))
    conn.commit()
    conn.close()
    return fid


def _add_event(catalog, file_ids, kind='Auto', start='2026-10-02 23:50:26'):
    conn = _db(catalog)
    cur = conn.execute(
        "INSERT INTO events (name, start_time, end_time, file_count, type) VALUES (?, ?, ?, ?, ?)",
        (f'Event {start[:16]} (#1)', start, start, len(file_ids), kind))
    eid = cur.lastrowid
    for fid in file_ids:
        conn.execute('INSERT INTO event_files (event_id, file_id) VALUES (?, ?)', (eid, fid))
    conn.commit()
    conn.close()
    return eid


def _add_create_events_workflow(catalog, name='Create Events'):
    conn = _db(catalog)
    cur = conn.execute(
        "INSERT INTO workflows (name, description, python_script, workflow_type) "
        "VALUES (?, '', 'workflows/ev_extract_events.py', 'Create Events')", (name,))
    conn.commit()
    conn.close()
    return cur.lastrowid


def _wait(client, batch_id, timeout=10):
    deadline = time.time() + timeout
    while time.time() < deadline:
        batch = client.get(f'/api/catalogs/1/photos/dates/batches/{batch_id}').get_json()
        if batch['status'] != 'running':
            return batch
        time.sleep(0.05)
    raise AssertionError('batch did not finish')


def _post(client, changes, **extra):
    return client.post('/api/catalogs/1/photos/dates',
                       json={'changes': changes, **extra})


def _row(catalog, fid):
    conn = _db(catalog)
    row = dict(conn.execute('SELECT * FROM file_contents WHERE id = ?', (fid,)).fetchone())
    row['exif'] = conn.execute('SELECT creation_date FROM exif_data WHERE file_id = ?',
                               (fid,)).fetchone()['creation_date']
    row['alt'] = [dict(r) for r in conn.execute(
        'SELECT * FROM alternative_versions WHERE original_file_id = ?', (fid,))]
    row['events'] = [r['event_id'] for r in conn.execute(
        'SELECT event_id FROM event_files WHERE file_id = ?', (fid,))]
    conn.close()
    return row


NY = 'America/New_York'


# ─── The batch ────────────────────────────────────────────────────────────────

def test_a_batch_redates_every_file_and_queues_create_events(client, catalog):
    a = _add_file(catalog, '200301010600_arboritro 1.jpg')
    b = _add_file(catalog, 'b.jpg', sha='0000000002')
    c = _add_file(catalog, 'c.jpg', sha='0000000003')
    event = _add_event(catalog, [a, b, c])
    wf = _add_create_events_workflow(catalog, name='My renamed events')

    r = _post(client, [
        {'file_ids': [a, b], 'datetime': '2003-01-01T06:00', 'timezone': NY},
        {'file_ids': [c], 'datetime': '2003-01-01T18:00', 'timezone': NY},
    ], event_id=event)
    assert r.status_code == 202
    assert r.get_json()['file_count'] == 3

    batch = _wait(client, r.get_json()['batch_id'])
    assert batch['status'] == 'rebuilding'
    assert batch['event_id'] == event
    assert batch['file_ids'] == [a, b, c]
    assert batch['done'] == batch['total'] == 3
    assert {u['file_id'] for u in batch['result']['updated']} == {a, b, c}
    assert batch['result']['failed'] == []
    assert batch['result']['detached_from_events'] == [event]

    row = _row(catalog, a)
    assert row['master_datetime'] == '2003-01-01T06:00:00-05:00'
    assert row['master_datetime_source'] == 'manual'
    assert row['exif'] == '2003:01:01 06:00:00'
    assert row['file_path'] == os.path.join(
        'Images', '2003', '01', '01', '20030101060000-9a4bfb64ba-200301010600_arboritro 1.jpg')
    assert os.path.exists(os.path.join(catalog.target_directory, row['file_path']))
    alt = row['alt'][0]
    assert alt['alternative_file_path'].startswith(os.path.join('cache', 'Images', '2003', '01', '01'))
    assert alt['alternative_file_name'].endswith('_alt_1200.jpg')
    assert os.path.exists(os.path.join(catalog.target_directory, alt['alternative_file_path']))
    assert row['events'] == []                        # detached, for the rebuild to place

    conn = _db(catalog)
    q = conn.execute('SELECT * FROM workflow_queue WHERE id = ?', (batch['queue_id'],)).fetchone()
    assert q['workflow_id'] == wf                     # found by script, not by name
    assert q['trigger_source'] == 'date_change'
    assert conn.execute('SELECT file_count FROM events WHERE id = ?',
                        (event,)).fetchone()['file_count'] == 0
    conn.close()


def test_a_reference_catalog_changes_dates_without_moving_files(client, catalog):
    catalog.ingest_mode = 'reference'
    catalog.source_root = catalog.target_directory
    a = _add_file(catalog, 'a.jpg', with_alt=False)
    before = _row(catalog, a)['file_path']
    batch = _wait(client, _post(client, [
        {'file_ids': [a], 'datetime': '2003-01-01T12:00', 'timezone': 'UTC'}],
        rebuild_events=False).get_json()['batch_id'])
    row = _row(catalog, a)
    assert batch['result']['updated'][0]['file_renamed'] is False
    assert row['file_path'] == before
    assert row['master_datetime'] == '2003-01-01T12:00:00+00:00'


def test_a_raw_jpeg_pair_keeps_one_date(client, catalog):
    conn = _db(catalog)
    group = conn.execute("INSERT INTO image_groups (primary_file_id) VALUES (0)").lastrowid
    conn.commit()
    conn.close()
    jpg = _add_file(catalog, 'IMG_1.jpg', image_group_id=group)
    raw = _add_file(catalog, 'IMG_1.cr3', sha='000000000c', image_group_id=group, with_alt=False)
    batch = _wait(client, _post(client, [
        {'file_ids': [jpg], 'datetime': '2003-01-01T12:00', 'timezone': NY}],
        rebuild_events=False).get_json()['batch_id'])
    by_id = {u['file_id']: u for u in batch['result']['updated']}
    assert set(by_id) == {jpg, raw}
    assert by_id[raw]['via_group'] == jpg
    assert by_id[jpg]['via_group'] is None
    assert _row(catalog, raw)['master_datetime'] == '2003-01-01T12:00:00-05:00'


def test_one_failure_does_not_stop_the_batch(client, catalog):
    a = _add_file(catalog, 'a.jpg')
    b = _add_file(catalog, 'b.jpg', sha='0000000002')
    gone = _add_file(catalog, 'gone.jpg', sha='0000000004', removed=True)
    # Something already sits where b would go.
    clash = os.path.join(catalog.target_directory, 'Images', '2003', '01', '01',
                         '20030101120000-0000000002-b.jpg')
    os.makedirs(os.path.dirname(clash), exist_ok=True)
    open(clash, 'wb').close()

    batch = _wait(client, _post(client, [
        {'file_ids': [a, b, gone, 99999], 'datetime': '2003-01-01T12:00', 'timezone': NY}],
        rebuild_events=False).get_json()['batch_id'])
    failed = {f['file_id']: f['error'] for f in batch['result']['failed']}
    assert failed == {b: 'file_move_conflict', gone: 'removed', 99999: 'not_found'}
    assert [u['file_id'] for u in batch['result']['updated']] == [a]
    assert _row(catalog, b)['master_datetime_source'] == 'filesystem'   # untouched


@pytest.mark.parametrize('changes, why', [
    ([], 'non-empty'),
    ([{'file_ids': [1], 'datetime': '2003-01-01T12:00', 'timezone': 'Mars/Olympus'}], 'timezone'),
    ([{'file_ids': [1], 'datetime': '1850-01-01T12:00', 'timezone': NY}], 'year'),
    ([{'file_ids': [1], 'datetime': 'yesterday', 'timezone': NY}], 'format'),
    ([{'file_ids': [], 'datetime': '2003-01-01T12:00', 'timezone': NY}], 'file_ids'),
    ([{'file_ids': [1], 'datetime': '2003-01-01T12:00', 'timezone': NY},
      {'file_ids': [1], 'datetime': '2004-01-01T12:00', 'timezone': NY}], 'more than once'),
])
def test_bad_requests_change_nothing(client, catalog, changes, why):
    r = _post(client, changes)
    assert r.status_code == 400
    assert why in r.get_json()['error']
    conn = _db(catalog)
    assert conn.execute('SELECT COUNT(*) FROM date_change_batches').fetchone()[0] == 0
    conn.close()


def test_a_file_in_a_manual_event_is_set_free(client, catalog):
    a = _add_file(catalog, 'a.jpg')
    b = _add_file(catalog, 'b.jpg', sha='0000000002')
    manual = _add_event(catalog, [a, b], kind='Manual')
    _add_create_events_workflow(catalog)
    batch = _wait(client, _post(client, [
        {'file_ids': [a], 'datetime': '2003-01-01T12:00', 'timezone': NY}]).get_json()['batch_id'])
    assert batch['result']['detached_from_events'] == [manual]
    assert _row(catalog, a)['events'] == []
    assert _row(catalog, b)['events'] == [manual]
    conn = _db(catalog)
    assert conn.execute('SELECT file_count FROM events WHERE id = ?',
                        (manual,)).fetchone()['file_count'] == 1
    conn.close()


def test_without_a_rebuild_each_file_is_placed_and_nothing_is_queued(client, catalog):
    a = _add_file(catalog, 'a.jpg')
    _add_event(catalog, [a])
    _add_create_events_workflow(catalog)
    batch = _wait(client, _post(client, [
        {'file_ids': [a], 'datetime': '2003-01-01T12:00', 'timezone': NY}],
        rebuild_events=False).get_json()['batch_id'])
    assert batch['status'] == 'completed'
    assert batch['queue_id'] is None
    events = _row(catalog, a)['events']
    assert len(events) == 1
    conn = _db(catalog)
    assert conn.execute('SELECT type FROM events WHERE id = ?', (events[0],)).fetchone()['type'] == 'Manual'
    assert conn.execute('SELECT COUNT(*) FROM workflow_queue').fetchone()[0] == 0
    conn.close()


def test_a_catalog_without_create_events_still_places_every_file(client, catalog):
    a = _add_file(catalog, 'a.jpg')
    batch = _wait(client, _post(client, [
        {'file_ids': [a], 'datetime': '2003-01-01T12:00', 'timezone': NY}]).get_json()['batch_id'])
    assert batch['status'] == 'completed'
    assert batch['result']['rebuild_status'] == 'no_create_events_workflow'
    assert len(_row(catalog, a)['events']) == 1


def test_a_running_import_makes_the_catalog_busy(client, catalog, app_module):
    a = _add_file(catalog, 'a.jpg')
    conn = _db(catalog)
    wid = conn.execute(
        "INSERT INTO workflows (name, description, python_script, workflow_type) "
        "VALUES ('Import', '', 'workflows/io_picture_import_v2_workflow.py', 'Import')").lastrowid
    conn.execute("INSERT INTO workflow_executions (workflow_id, workflow_name, status, started_at) "
                 "VALUES (?, 'Import', 'running', ?)",
                 (wid, time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())))
    conn.commit()
    conn.close()
    r = _post(client, [{'file_ids': [a], 'datetime': '2003-01-01T12:00', 'timezone': NY}])
    assert r.status_code == 409
    assert r.get_json()['error'] == 'catalog_busy'
    conn = _db(catalog)
    assert conn.execute('SELECT COUNT(*) FROM date_change_batches').fetchone()[0] == 0
    conn.close()


# ─── Following a batch ────────────────────────────────────────────────────────

def test_the_batch_completes_when_its_create_events_run_does(client, catalog, app_module):
    a = _add_file(catalog, 'a.jpg')
    _add_create_events_workflow(catalog)
    batch = _wait(client, _post(client, [
        {'file_ids': [a], 'datetime': '2003-01-01T12:00', 'timezone': NY}]).get_json()['batch_id'])
    assert batch['status'] == 'rebuilding'

    # The scheduler's hook, as it calls it when the queue row is reaped.
    app_module._finish_rebuilding_batches(catalog, batch['queue_id'], 'completed')
    done = client.get(f"/api/catalogs/1/photos/dates/batches/{batch['id']}").get_json()
    assert done['status'] == 'completed'
    assert done['result']['rebuild_status'] == 'completed'
    assert done['finished_at']


def test_a_finished_queue_row_is_noticed_on_read(client, catalog):
    a = _add_file(catalog, 'a.jpg')
    _add_create_events_workflow(catalog)
    batch = _wait(client, _post(client, [
        {'file_ids': [a], 'datetime': '2003-01-01T12:00', 'timezone': NY}]).get_json()['batch_id'])
    conn = _db(catalog)
    conn.execute("UPDATE workflow_queue SET status = 'failed' WHERE id = ?", (batch['queue_id'],))
    conn.commit()
    conn.close()
    done = client.get(f"/api/catalogs/1/photos/dates/batches/{batch['id']}").get_json()
    assert done['status'] == 'completed'
    assert done['result']['rebuild_status'] == 'failed'


def test_a_batch_left_running_by_a_restart_reads_as_interrupted(client, catalog):
    conn = _db(catalog)
    bid = conn.execute("INSERT INTO date_change_batches (event_id, status, file_ids, total, done) "
                       "VALUES (5, 'running', '[1,2]', 2, 1)").lastrowid
    conn.commit()
    conn.close()
    batch = client.get(f'/api/catalogs/1/photos/dates/batches/{bid}').get_json()
    assert batch['status'] == 'interrupted'
    assert batch['done'] == 1


def test_the_event_page_can_find_its_latest_batch_and_the_photos(client, catalog):
    a = _add_file(catalog, 'a.jpg')
    old = _add_event(catalog, [a])
    _wait(client, _post(client, [
        {'file_ids': [a], 'datetime': '2003-01-01T12:00', 'timezone': NY}],
        event_id=old, rebuild_events=False).get_json()['batch_id'])

    latest = client.get(f'/api/catalogs/1/photos/dates/batches?event_id={old}&latest=1')
    assert latest.status_code == 200
    assert latest.get_json()['file_ids'] == [a]
    assert client.get('/api/catalogs/1/photos/dates/batches?event_id=424242&latest=1').status_code == 404

    located = client.get(f'/api/catalogs/1/events/locate?file_ids={a},77777').get_json()
    assert located['77777'] == []
    assert len(located[str(a)]) == 1
    assert located[str(a)][0]['id'] != old
    assert located[str(a)][0]['start_time'].startswith('2003-01-01')


# ─── The suggestion ───────────────────────────────────────────────────────────

def test_event_files_carry_the_file_name_date(client, catalog):
    a = _add_file(catalog, '200301010600_arboritro 1.jpg')
    b = _add_file(catalog, 'IMG-20190815-WA0001.jpg', sha='0000000002')
    c = _add_file(catalog, 'DSC_0042.jpg', sha='0000000003')
    event = _add_event(catalog, [a, b, c])
    rows = {f['id']: f for f in client.get(f'/api/catalogs/1/events/{event}/files').get_json()}
    assert (rows[a]['filename_datetime'], rows[a]['filename_has_time']) == ('2003-01-01T06:00:00', True)
    assert (rows[b]['filename_datetime'], rows[b]['filename_has_time']) == ('2019-08-15T12:00:00', False)
    # The catalog's own "20261002235026-…-" prefix is not the name's date.
    assert (rows[c]['filename_datetime'], rows[c]['filename_has_time']) == (None, False)


# ─── The single-photo endpoint, after the refactor ────────────────────────────

def test_the_single_photo_endpoint_behaves_as_before(client, catalog):
    a = _add_file(catalog, 'a.jpg')
    b = _add_file(catalog, 'b.jpg', sha='0000000002')
    event = _add_event(catalog, [a, b])

    r = client.put(f'/api/catalogs/1/photos/{a}/date',
                   json={'datetime': '2003-01-01T12:00', 'timezone': NY})
    body = r.get_json()
    assert r.status_code == 200
    assert body['master_datetime'] == '2003-01-01T12:00:00-05:00'
    assert body['master_datetime_source'] == 'manual'
    assert body['file_renamed'] is True
    assert body['event_action'] == 'new_event_created'
    assert _row(catalog, a)['events'] == [body['event_id']] != [event]

    clash = os.path.join(catalog.target_directory, 'Images', '2003', '01', '01',
                         '20030101120000-0000000002-b.jpg')
    open(clash, 'wb').close()
    r = client.put(f'/api/catalogs/1/photos/{b}/date',
                   json={'datetime': '2003-01-01T12:00', 'timezone': NY})
    assert (r.status_code, r.get_json()) == (409, {'error': 'file_move_conflict'})

    assert client.put(f'/api/catalogs/1/photos/{b}/date',
                      json={'datetime': '2003-01-01T12:00', 'timezone': 'Nope/Nope'}).status_code == 400
    assert client.put('/api/catalogs/1/photos/99999/date',
                      json={'datetime': '2003-01-01T12:00', 'timezone': NY}).status_code == 404
