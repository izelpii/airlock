"""
Sending an album to several people: the batch rules, the deadline, and the
restart sweep (documentation/specs/album-mass-transfer-spec.md).

    python -m pytest test_wormhole_batches.py
"""

import os
import sqlite3
import sys
from datetime import timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import wormhole_transfer as wt  # noqa: E402

ALBUM = {'id': 3, 'name': 'Summer 2019'}
FILES = [{'file_id': 1, 'sha256': 'a' * 64, 'name': 'IMG_1.jpg', 'size': 1000,
          'path': '/x/IMG_1.jpg', 'relpath': 'IMG_1.jpg'},
         {'file_id': 2, 'sha256': 'b' * 64, 'name': 'IMG_2.jpg', 'size': 2000,
          'path': '/x/IMG_2.jpg', 'relpath': 'IMG_2.jpg'}]


class FakeCatalogDB:
    """The two things wormhole_transfer asks of a CatalogDatabase."""

    def __init__(self, path):
        self.path = path
        conn = sqlite3.connect(path)
        wt.ensure_schema(conn)
        conn.commit()
        conn.close()

    def get_connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
def db(tmp_path):
    return FakeCatalogDB(str(tmp_path / 'workflow_results.db'))


def _rows(db, sql, args=()):
    conn = db.get_connection()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _set(db, transfer_id, **fields):
    wt.update(db, transfer_id, **fields)


def _move_deadline(db, batch_id, delta):
    conn = db.get_connection()
    try:
        conn.execute('UPDATE wormhole_transfer_batches SET expires_at = ? WHERE id = ?',
                     (wt.to_utc_text(wt.utc_now() + delta), batch_id))
        conn.commit()
    finally:
        conn.close()


# ── the schema ───────────────────────────────────────────────────────────────

def test_an_existing_catalog_gains_the_columns(tmp_path):
    path = str(tmp_path / 'old.db')
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE wormhole_transfers (id INTEGER PRIMARY KEY, "
                 "direction TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending')")
    conn.execute("INSERT INTO wormhole_transfers (direction) VALUES ('send')")
    conn.commit()
    wt.ensure_schema(conn)
    wt.ensure_schema(conn)                                   # idempotent
    cols = {r[1] for r in conn.execute('PRAGMA table_info(wormhole_transfers)')}
    assert {'batch_id', 'recipient_label', 'recipient_order'} <= cols
    assert conn.execute('SELECT batch_id FROM wormhole_transfers').fetchone() == (None,)
    conn.close()


# ── creating ─────────────────────────────────────────────────────────────────

def test_a_batch_is_one_ordinary_transfer_per_person(db):
    made = wt.create_batch(db, ALBUM, FILES, ['Mom', '', 'Ana'], 72)
    assert len(made['transfer_ids']) == 3
    rows = _rows(db, 'SELECT * FROM wormhole_transfers ORDER BY id')
    assert [r['recipient_label'] for r in rows] == ['Mom', '', 'Ana']
    assert [r['recipient_order'] for r in rows] == [1, 2, 3]
    assert all(r['direction'] == 'send' and r['batch_id'] == made['batch_id'] and
               r['total_files'] == 2 and r['total_bytes'] == 3000 for r in rows)
    # Each has its own file rows - each is a complete transfer.
    assert len(_rows(db, 'SELECT * FROM wormhole_transfer_files')) == 6
    left = wt.from_utc_text(made['expires_at']) - wt.utc_now()
    assert timedelta(hours=71, minutes=59) < left <= timedelta(hours=72)
    labels = [m['label'] for m in wt.batch_payload(db, made['batch_id'])['members']]
    assert labels == ['Mom', 'Person 2', 'Ana']


@pytest.mark.parametrize('labels, ttl, message', [
    (['only one'], 72, '2 to 20'),
    ([''] * 21, 72, '2 to 20'),
    (['', ''], 100, 'at most 72'),
    (['', ''], 0, 'expire'),
    (['', ''], 'soon', 'expire'),
    (['x' * 41, ''], 72, '40 characters'),
])
def test_the_limits(db, labels, ttl, message):
    with pytest.raises(wt.BatchError, match=message):
        wt.create_batch(db, ALBUM, FILES, labels, ttl)
    assert _rows(db, 'SELECT * FROM wormhole_transfer_batches') == []


def test_a_label_that_is_not_text_is_read_as_text(db):
    made = wt.create_batch(db, ALBUM, FILES, [7, None], 72)
    labels = [m['label'] for m in wt.batch_payload(db, made['batch_id'])['members']]
    assert labels == ['7', 'Person 2']


def test_at_most_40_sends_at_once_per_catalog(db):
    wt.create_batch(db, ALBUM, FILES, [''] * 20, 72)
    wt.create_batch(db, {'id': 4, 'name': 'B'}, FILES, [''] * 19, 72)
    with pytest.raises(wt.BatchError, match='39 send'):
        wt.create_batch(db, {'id': 5, 'name': 'C'}, FILES, ['', ''], 72)


# ── one send per album, amended (3.8) ────────────────────────────────────────

def test_batch_members_do_not_block_the_transfer_button(db):
    wt.create_batch(db, ALBUM, FILES, ['', ''], 72)
    assert wt.active_transfer_for_album(db, ALBUM['id']) is None
    single = wt.create_send_transfer(None, db, ALBUM, FILES)
    assert wt.active_transfer_for_album(db, ALBUM['id'])['id'] == single


def test_a_batch_stays_open_until_everyone_has_it_or_it_is_stopped(db):
    made = wt.create_batch(db, ALBUM, FILES, ['', ''], 72)
    a, b = made['transfer_ids']
    assert wt.open_batch_for_album(db, ALBUM['id'])['id'] == made['batch_id']
    _set(db, a, status='completed')
    _set(db, b, status='expired')                 # still someone to reach
    assert wt.open_batch_for_album(db, ALBUM['id'])['id'] == made['batch_id']
    _set(db, b, status='completed')
    assert wt.open_batch_for_album(db, ALBUM['id']) is None


def test_stop_all_closes_the_batch(db, monkeypatch):
    monkeypatch.setattr(wt.os, 'kill', lambda *a: None)
    made = wt.create_batch(db, ALBUM, FILES, ['', ''], 72)
    assert wt.cancel_batch(db, made['batch_id']) == 2
    statuses = {r['status'] for r in _rows(db, 'SELECT status FROM wormhole_transfers')}
    assert statuses == {'cancelled'}
    assert wt.open_batch_for_album(db, ALBUM['id']) is None


# ── adding people ────────────────────────────────────────────────────────────

def test_a_person_added_later_takes_the_next_number_and_the_deadline(db):
    made = wt.create_batch(db, ALBUM, FILES, ['', ''], 72)
    tid = wt.add_to_batch(db, made['batch_id'], ALBUM, FILES, '')
    row = wt.get_transfer(db, tid)
    assert (row['batch_id'], row['recipient_order']) == (made['batch_id'], 3)


def test_no_more_than_20_and_not_on_an_expired_deadline(db):
    made = wt.create_batch(db, ALBUM, FILES, [''] * 20, 72)
    with pytest.raises(wt.BatchError, match='at most 20'):
        wt.add_to_batch(db, made['batch_id'], ALBUM, FILES, '')
    other = wt.create_batch(db, {'id': 4, 'name': 'B'}, FILES, ['', ''], 72)
    _move_deadline(db, other['batch_id'], timedelta(seconds=-1))
    with pytest.raises(wt.BatchError, match='expired'):
        wt.add_to_batch(db, other['batch_id'], ALBUM, FILES, '')


# ── new codes (3.6) ──────────────────────────────────────────────────────────

def test_new_codes_reset_the_unfinished_and_move_the_deadline(db):
    made = wt.create_batch(db, ALBUM, FILES, ['', '', '', ''], 1)
    a, b, c, d = made['transfer_ids']
    _set(db, a, status='completed', done_files=2)
    _set(db, b, status='expired', wormhole_code='7-old-code')
    _set(db, c, status='failed', error_message='x', done_bytes=500)
    _set(db, d, status='waiting_peer', wormhole_code='8-still-good')

    renewed = wt.renew_batch(db, made['batch_id'], 48)
    assert renewed['transfer_ids'] == [b, c]
    rows = {r['id']: r for r in _rows(db, 'SELECT * FROM wormhole_transfers')}
    assert rows[a]['status'] == 'completed'
    assert rows[b]['status'] == rows[c]['status'] == 'pending'
    assert rows[b]['wormhole_code'] is None and rows[c]['error_message'] is None
    assert rows[c]['done_bytes'] == 0
    assert rows[d]['wormhole_code'] == '8-still-good'       # untouched, adopts the deadline
    left = wt.from_utc_text(wt.get_batch(db, made['batch_id'])['expires_at']) - wt.utc_now()
    assert timedelta(hours=47) < left <= timedelta(hours=48)


def test_a_new_code_for_one_person_only(db):
    made = wt.create_batch(db, ALBUM, FILES, ['', '', ''], 72)
    a, b, c = made['transfer_ids']
    _set(db, a, status='expired')
    _set(db, b, status='expired')
    assert wt.renew_batch(db, made['batch_id'], 72, [b])['transfer_ids'] == [b]
    assert wt.get_transfer(db, a)['status'] == 'expired'
    with pytest.raises(wt.BatchError, match='Only someone'):
        wt.renew_batch(db, made['batch_id'], 72, [c])          # still waiting
    with pytest.raises(wt.BatchError, match='at most 72'):
        wt.renew_batch(db, made['batch_id'], 73, [a])


def test_nobody_to_renew(db):
    made = wt.create_batch(db, ALBUM, FILES, ['', ''], 72)
    with pytest.raises(wt.BatchError, match='Nobody'):
        wt.renew_batch(db, made['batch_id'], 72)


def test_renewing_a_stopped_batch_reopens_it(db, monkeypatch):
    monkeypatch.setattr(wt.os, 'kill', lambda *a: None)
    made = wt.create_batch(db, ALBUM, FILES, ['', ''], 72)
    wt.cancel_batch(db, made['batch_id'])
    wt.renew_batch(db, made['batch_id'], 24)
    assert wt.get_batch(db, made['batch_id'])['cancelled_at'] is None
    assert wt.open_batch_for_album(db, ALBUM['id'])['id'] == made['batch_id']


# ── what the batch view is given ─────────────────────────────────────────────

def test_codes_are_shown_only_while_waiting(db):
    made = wt.create_batch(db, ALBUM, FILES, ['', '', ''], 72)
    a, b, c = made['transfer_ids']
    _set(db, a, status='waiting_peer', wormhole_code='1-a-a', pid=99)
    _set(db, b, status='transferring', wormhole_code='2-b-b')
    _set(db, c, status='expired', wormhole_code='3-c-c')
    p = wt.batch_payload(db, made['batch_id'])
    codes = [m['wormhole_code'] for m in p['members']]
    assert codes == ['1-a-a', None, None]
    assert 'pid' not in p['members'][0]
    assert p['counts'] == {'waiting_peer': 1, 'transferring': 1, 'expired': 1}
    assert p['expired'] is False and p['is_open'] is True


def test_the_history_lists_each_batch_once(db):
    made = wt.create_batch(db, ALBUM, FILES, ['', '', ''], 72)
    _set(db, made['transfer_ids'][0], status='completed')
    (b,) = wt.list_batches(db)
    assert (b['id'], b['people'], b['is_active'], b['is_open'], b['expired']) == \
        (made['batch_id'], 3, True, True, False)
    assert b['counts'] == {'completed': 1, 'pending': 2}


# ── the deadline file ────────────────────────────────────────────────────────

def test_the_expiry_file_holds_the_deadline_as_unix_time(db, tmp_path):
    made = wt.create_batch(db, ALBUM, FILES, ['', ''], 72)
    path = wt.write_expiry_file(str(tmp_path), made['batch_id'], made['expires_at'])
    assert path.endswith(f"wh-batch-{made['batch_id']}.expires")
    with open(path) as fh:
        assert int(fh.read()) == int(wt.from_utc_text(made['expires_at']).timestamp())
    assert not os.path.exists(path + '.tmp')


def test_the_runner_reads_the_file(tmp_path):
    runner = pytest.importorskip('wormhole_runner')
    path = tmp_path / 'd.expires'
    path.write_text('1793000000')
    assert runner.read_deadline(str(path)) == 1793000000.0
    assert runner.read_deadline(str(tmp_path / 'missing')) is None
    path.write_text('half a num')
    assert runner.read_deadline(str(path)) is None


# ── the watcher ──────────────────────────────────────────────────────────────

class FakeProc:
    def __init__(self, lines, returncode=0):
        self.stdout = iter(lines)
        self.returncode = returncode

    def wait(self):
        return self.returncode


def test_an_expired_event_ends_the_transfer_as_expired(db):
    made = wt.create_batch(db, ALBUM, FILES, ['', ''], 72)
    tid = made['transfer_ids'][0]
    events = []
    wt._watch(db, tid, FakeProc(['{"event": "code", "code": "4-x-y"}\n',
                                 '{"event": "expired"}\n']),
              lambda name, data: events.append((name, data)), None)
    row = wt.get_transfer(db, tid)
    assert (row['status'], row['error_message']) == ('expired', None)
    assert events[-1] == ('transfer_done', {'transfer_id': tid, 'status': 'expired',
                                            'error': None})


# ── after a restart (3.7) ────────────────────────────────────────────────────

def test_a_restart_ends_what_the_previous_process_left_running(db, monkeypatch):
    killed = []
    monkeypatch.setattr(wt.os, 'kill', lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(wt, '_is_runner', lambda pid: pid in (4242, 4343))
    made = wt.create_batch(db, ALBUM, FILES, ['', '', ''], 72)
    waiting, running, ours = made['transfer_ids']
    _set(db, waiting, status='waiting_peer', pid=4242)
    _set(db, running, status='transferring', pid=4343)
    _set(db, ours, status='waiting_peer', pid=777)
    single = wt.create_send_transfer(None, db, ALBUM, FILES)
    _set(db, single, status='waiting_peer', pid=os.getpid())   # some other process
    wt._LAUNCHED_PIDS.add(777)                                 # started by this one
    try:
        conn = sqlite3.connect(db.path)
        assert wt.reap_orphans(conn) == 3
        conn.close()
    finally:
        wt._LAUNCHED_PIDS.discard(777)

    assert sorted(killed) == [4242, 4343]                      # never os.getpid()
    rows = {r['id']: r for r in _rows(db, 'SELECT * FROM wormhole_transfers')}
    assert (rows[waiting]['status'], rows[waiting]['error_message']) == \
        ('failed', wt.RESTART_WAITING)
    assert (rows[running]['status'], rows[running]['error_message']) == \
        ('failed', wt.RESTART_RUNNING)
    assert rows[single]['status'] == 'failed'                  # singles too
    assert rows[ours]['status'] == 'waiting_peer'


def test_a_restart_after_the_deadline_marks_expired(db, monkeypatch):
    monkeypatch.setattr(wt, '_is_runner', lambda pid: False)
    made = wt.create_batch(db, ALBUM, FILES, ['', ''], 72)
    _move_deadline(db, made['batch_id'], timedelta(minutes=-5))
    for tid in made['transfer_ids']:
        _set(db, tid, status='waiting_peer', pid=5)
    conn = sqlite3.connect(db.path)
    wt.reap_orphans(conn)
    conn.close()
    assert {r['status'] for r in _rows(db, 'SELECT status FROM wormhole_transfers')} \
        == {'expired'}


def test_the_sweep_marks_only_waiting_members_past_the_deadline(db, monkeypatch):
    monkeypatch.setattr(wt, '_is_runner', lambda pid: pid == 1)
    made = wt.create_batch(db, ALBUM, FILES, ['', '', '', ''], 72)
    gone, alive, sending, _ = made['transfer_ids']
    _set(db, gone, status='waiting_peer', pid=9)
    _set(db, alive, status='waiting_peer', pid=1)       # its runner will expire it
    _set(db, sending, status='transferring', pid=9)
    assert wt.sweep(db) == 0                            # not yet past the deadline
    _move_deadline(db, made['batch_id'], timedelta(seconds=-1))
    assert wt.sweep(db) == 2                            # `gone`, and the pending one
    rows = {r['id']: r['status'] for r in _rows(db, 'SELECT * FROM wormhole_transfers')}
    assert rows[gone] == 'expired'
    assert rows[alive] == 'waiting_peer'
    assert rows[sending] == 'transferring'
