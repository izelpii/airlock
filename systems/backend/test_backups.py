"""
Catalog backups: cards, the database copy, the schedule, the status, and a
real restic run against a repository in a temporary folder.

    python -m pytest test_backups.py

documentation/specs/backup-backrest-spec.md. The end-to-end tests need the
restic binary the backend image carries; elsewhere they skip.
"""

import json
import os
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import backup_restic  # noqa: E402
import backup_secrets  # noqa: E402
import backup_ssh  # noqa: E402
import backups  # noqa: E402
from backup_restic import ResticError  # noqa: E402
from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402

KEY = 'k' * 64
HAVE_RESTIC = shutil.which('restic') is not None
needs_restic = pytest.mark.skipif(not HAVE_RESTIC, reason='restic is not installed here')


# ── secrets ──────────────────────────────────────────────────────────────────

def test_secrets_round_trip_and_never_store_empty():
    token = backup_secrets.encrypt(KEY, 'hunter2')
    assert token and 'hunter2' not in token
    assert backup_secrets.decrypt(KEY, token) == 'hunter2'
    assert backup_secrets.encrypt(KEY, '') is None and backup_secrets.decrypt(KEY, None) is None


def test_a_secret_from_another_key_says_so():
    token = backup_secrets.encrypt(KEY, 'hunter2')
    with pytest.raises(ValueError, match='enter it again'):
        backup_secrets.decrypt('x' * 64, token)


# ── repositories and SSH ─────────────────────────────────────────────────────

def test_sftp_uris_in_both_spellings():
    assert backup_ssh.parse_sftp('sftp:izelpi@192.168.1.16:/media/usb1/backup') == \
        ('izelpi', '192.168.1.16', 22, '/media/usb1/backup')
    assert backup_ssh.parse_sftp('sftp://izelpi@backup-host:2222//media/usb1/backup') == \
        ('izelpi', 'backup-host', 2222, '/media/usb1/backup')
    assert backup_ssh.parse_sftp('sftp:backup-host:/srv/b') == (None, 'backup-host', 22, '/srv/b')
    assert backup_ssh.parse_sftp('/srv/restic') is None
    assert backup_ssh.parse_sftp('rest:https://host/repo') is None


def test_an_sftp_repository_goes_through_airlocks_own_key(monkeypatch):
    monkeypatch.setattr(backup_ssh, 'ensure_key', lambda: backup_ssh.KEY)
    cmd = backup_restic.Repo('sftp:izelpi@backup-host:/media/usb1/backup', 'pw')._args('snapshots')
    opt = cmd[cmd.index('-o') + 1]
    assert opt.startswith('sftp.command=ssh -i ') and 'StrictHostKeyChecking=yes' in opt
    assert opt.endswith('izelpi@backup-host -s sftp') and cmd[-1] == 'snapshots'
    assert '-o' not in backup_restic.Repo('/srv/restic', 'pw')._args('snapshots')


def test_the_password_travels_in_the_environment_only():
    repo = backup_restic.Repo('/srv/restic', 'hunter22', ['AWS_ACCESS_KEY_ID=abc', 'junk'])
    assert 'hunter22' not in ' '.join(repo._args('backup', '/x'))
    env = repo._env()
    assert env['RESTIC_PASSWORD'] == 'hunter22' and env['AWS_ACCESS_KEY_ID'] == 'abc'
    assert repo.extra_env == ['AWS_ACCESS_KEY_ID=abc']


@pytest.mark.parametrize('stderr,kind', [
    ('izelpi@backup-host: Permission denied (publickey).', 'key'),
    ('Host key verification failed.', 'hostkey'),
    ('Fatal: wrong password or no key found', 'password'),
    ('Fatal: unable to open config file: stat /x/config: no such file or directory\n'
     'Is there a repository at the following location?', 'missing'),
    ('unable to create lock in backend: repository is already locked by PID 7', 'locked'),
    ('ssh: connect to host backup-host port 22: Connection refused', 'unreachable'),
])
def test_restic_errors_become_sentences(stderr, kind):
    assert backup_restic._explain(stderr, 'sftp:backup-host:/b').kind == kind


def test_the_authorized_keys_line_allows_file_transfer_only(tmp_path, monkeypatch):
    if not shutil.which('ssh-keygen'):
        pytest.skip('no ssh-keygen here')
    monkeypatch.setattr(backup_ssh, 'SSH_DIR', str(tmp_path))
    monkeypatch.setattr(backup_ssh, 'KEY', str(tmp_path / 'id_ed25519'))
    line = backup_ssh.authorized_keys_line()
    assert line.startswith('restrict,command="/usr/lib/openssh/sftp-server" ssh-ed25519 ')
    assert line.endswith('airlock-backups')
    assert backup_ssh.public_key() in line                  # made once, then kept


# ── what a backup carries ────────────────────────────────────────────────────

def _target(**kw):
    t = {'id': 1, 'name': 'backup-host nightly', 'type': 'restic', 'scope': 'originals',
         'repo_uri': 'sftp:izelpi@backup-host:/media/usb1/backup', 'schedule_json': None,
         'schedule_tz': 'America/New_York', 'retention_json': None, 'grace_hours': 24,
         'created_at': '2026-01-01T00:00:00+00:00'}
    t.update(kw)
    return t


def test_originals_carry_the_folders_that_exist_and_the_database_copy(tmp_path):
    (tmp_path / 'Images').mkdir()
    (tmp_path / 'Videos').mkdir()                       # no Others/ on this catalog
    paths, excludes = backups.paths_for(_target(), str(tmp_path))
    assert paths == [f'{tmp_path}/Images', f'{tmp_path}/Videos', f'{tmp_path}/cache/backup']
    assert '**/.import-staging' in excludes


def test_everything_excludes_the_live_database(tmp_path):
    paths, excludes = backups.paths_for(_target(scope='everything'), str(tmp_path))
    assert paths == [str(tmp_path)]
    for live in ('workflow_results.db', 'workflow_results.db-wal', 'workflow_results.db-shm'):
        assert f'{tmp_path}/{live}' in excludes


def test_tags_name_the_catalog_and_the_card():
    assert backups.tags_for(_target(id=4), 'family') == ['airlock', 'catalog:family', 'card:4']


def test_the_host_is_this_installation():
    first = backups.host_name()
    assert first.startswith('airlock-') and len(first) == 20 and backups.host_name() == first


# ── the database copy ────────────────────────────────────────────────────────

def test_the_database_copy_is_whole_and_opens(tmp_path):
    db = tmp_path / 'workflow_results.db'
    conn = sqlite3.connect(db)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('CREATE TABLE t (x)')
    conn.executemany('INSERT INTO t VALUES (?)', [(i,) for i in range(500)])
    conn.commit()                                       # left open: a live writer
    app_db = tmp_path / 'catalogs.db'
    sqlite3.connect(app_db).execute('CREATE TABLE users (id)').connection.commit()

    backups.prepare_snapshot(str(tmp_path), str(app_db))
    copy = tmp_path / 'cache' / 'backup' / backups.SNAPSHOT_DB
    assert sqlite3.connect(copy).execute('SELECT COUNT(*) FROM t').fetchone()[0] == 500
    assert (tmp_path / 'cache' / 'backup' / backups.APP_SNAPSHOT_DB).exists()
    assert not list((tmp_path / 'cache' / 'backup').glob('*.tmp'))
    conn.close()


def test_no_room_for_the_copy_is_refused(tmp_path, monkeypatch):
    sqlite3.connect(tmp_path / 'workflow_results.db').execute('CREATE TABLE t (x)')
    monkeypatch.setattr(backups.shutil, 'disk_usage',
                        lambda p: type('U', (), {'free': 1024})())
    with pytest.raises(RuntimeError, match='Not enough free space'):
        backups.prepare_snapshot(str(tmp_path))


# ── the schema ───────────────────────────────────────────────────────────────

def test_a_backrest_era_table_is_kept_aside_when_it_has_cards(tmp_path):
    conn = sqlite3.connect(tmp_path / 'app.db')
    conn.execute('CREATE TABLE backup_targets (id INTEGER PRIMARY KEY, server_url TEXT)')
    conn.execute("INSERT INTO backup_targets VALUES (1, 'http://br:9898')")
    backups.ensure_schema(conn.cursor())
    assert conn.execute('SELECT COUNT(*) FROM backup_targets_backrest').fetchone()[0] == 1
    cols = {r[1] for r in conn.execute('PRAGMA table_info(backup_targets)')}
    assert 'repo_uri' in cols and 'server_url' not in cols
    backups.ensure_schema(conn.cursor())                # and again: nothing to do


def test_runs_cut_off_by_a_restart_are_interrupted(tmp_path):
    conn = sqlite3.connect(tmp_path / 'app.db')
    backups.ensure_schema(conn.cursor())
    conn.execute("INSERT INTO backup_targets (id, catalog_id, name, repo_uri) VALUES (1, 1, 'n', '/r')")
    conn.execute("INSERT INTO backup_runs (target_id, kind, status, started_at) "
                 "VALUES (1, 'backup', 'running', '2026-10-16T02:00:00+00:00')")
    conn.commit()
    assert backups.interrupt_stale_runs(conn) == 1
    t = backups.get_target(conn, 1, 1)
    runs = backups.latest_runs(conn, 1)
    assert runs[0]['status'] == 'interrupted' and t['last_backup_status'] == 'interrupted'
    assert backups.status(t, runs, None)['state'] == 'failed'


# ── the schedule ─────────────────────────────────────────────────────────────

def _utc(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


def test_daily_is_due_once_its_time_has_passed_in_the_cards_zone():
    t = _target(schedule_json=json.dumps({'kind': 'daily', 'time': '02:00'}))
    # 02:00 New York (EDT) is 06:00 UTC.
    assert backups.last_due(t, _utc('2026-10-16T05:59:00')) == _utc('2026-10-15T06:00:00')
    assert backups.last_due(t, _utc('2026-10-16T06:01:00')) == _utc('2026-10-16T06:00:00')
    assert backups.is_due(t, _utc('2026-10-16T06:01:00'))
    t['last_run_started_at'] = '2026-10-16T06:01:30+00:00'
    assert not backups.is_due(t, _utc('2026-10-16T06:05:00'))
    assert backups.next_due(t, _utc('2026-10-16T06:05:00')) == _utc('2026-10-17T06:00:00')


def test_weekly_and_manual():
    t = _target(schedule_json=json.dumps({'kind': 'weekly', 'time': '03:00', 'weekday': 6}),
                schedule_tz='UTC')
    # 2026-10-16 is a Friday; the last Sunday 03:00 was the 11th.
    assert backups.last_due(t, _utc('2026-10-16T12:00:00')) == _utc('2026-10-11T03:00:00')
    manual = _target(schedule_json=json.dumps({'kind': 'manual'}))
    assert backups.last_due(manual) is None and not backups.is_due(manual)


def test_a_card_made_after_the_last_slot_does_not_run_at_once():
    t = _target(schedule_json=json.dumps({'kind': 'daily', 'time': '02:00'}),
                created_at='2026-10-16 07:00:00')       # SQLite's CURRENT_TIMESTAMP
    assert not backups.is_due(t, _utc('2026-10-16T08:00:00'))


# ── status ───────────────────────────────────────────────────────────────────

NOW = _utc('2026-10-16T12:00:00')


def test_no_backup_yet_is_needed():
    s = backups.status(_target(), [], '1:10:5:3', NOW)
    assert (s['state'], s['reason']) == ('needed', 'No backup yet')


def test_up_to_date_then_changed_then_old():
    t = _target(last_backup_at='2026-10-16T06:30:00+00:00', last_backup_status='success',
                last_fingerprint='1:10:5:3',
                schedule_json=json.dumps({'kind': 'daily', 'time': '02:00'}))
    assert backups.status(t, [], '1:10:5:3', NOW)['state'] == 'up_to_date'
    # A new derivative only: originals do not care, everything does.
    assert backups.status(t, [], '1:10:6:3', NOW)['state'] == 'up_to_date'
    assert backups.status(dict(t, scope='everything'), [], '1:10:6:3', NOW)['state'] == 'needed'
    changed = backups.status(t, [], '1:11:5:3', NOW)
    assert (changed['state'], changed['reason']) == (
        'needed', 'The catalog has changed since the last backup')
    old = backups.status(t, [], '1:10:5:3', NOW + timedelta(days=3))
    assert (old['state'], old['reason']) == ('needed', 'Last backup 3 days ago')


def test_running_failed_and_a_stopped_run():
    t = _target(last_backup_at='2026-10-16T06:30:00+00:00', last_backup_status='success')
    running = [{'kind': 'backup', 'status': 'running', 'phase': 'Backing up',
                'progress_json': json.dumps({'percent': 25.0, 'bytes_total': 1000})}]
    s = backups.status(t, running, None, NOW)
    assert (s['state'], s['progress']['percent'], s['reason']) == ('running', 25.0, 'Backing up')
    failed = dict(t, last_backup_status='failed', last_error='disk full')
    assert backups.status(failed, [], None, NOW) == {'state': 'failed', 'reason': 'disk full'}
    # Stopped by hand: the last good backup still stands.
    assert backups.status(dict(t, last_backup_status='cancelled'), [], None, NOW)['state'] == 'up_to_date'


# ── the routes ───────────────────────────────────────────────────────────────

class FakeRepo:
    """restic, for routes that only need to know the repository opens."""
    repos = {}                                   # uri -> password

    def __init__(self, uri, password, env=None):
        self.uri, self.password, self.extra_env = uri, password, list(env or [])

    def probe(self):
        if self.uri not in FakeRepo.repos:
            raise ResticError(f'There is no restic repository at {self.uri} yet.', 'missing')
        if FakeRepo.repos[self.uri] != self.password:
            raise ResticError('The repository password is wrong.', 'password')
        return 'abc123'

    def init(self):
        FakeRepo.repos[self.uri] = self.password


@pytest.fixture
def fake_repo(monkeypatch, app_module, catalog, tmp_path):  # noqa: F811
    FakeRepo.repos = {'/srv/restic': 'correct horse'}
    conn = app_module.db.get_connection()          # the app database outlives a test
    conn.execute('DELETE FROM backup_targets')
    conn.execute('DELETE FROM backup_runs')
    conn.commit()
    conn.close()
    monkeypatch.setattr(app_module, 'ResticRepo', FakeRepo)
    monkeypatch.setattr(backups, 'Repo', FakeRepo)
    monkeypatch.setattr(backup_ssh, 'SSH_DIR', str(tmp_path / 'ssh'))
    monkeypatch.setattr(backup_ssh, 'KEY', str(tmp_path / 'ssh' / 'id_ed25519'))
    return FakeRepo


def _create(client, **kw):  # noqa: F811
    body = {'name': 'backup-host nightly', 'scope': 'originals', 'repo_uri': '/srv/restic',
            'repo_password': 'correct horse',
            'schedule': {'kind': 'daily', 'time': '02:00'}, 'schedule_tz': 'America/New_York'}
    body.update(kw)
    return client.post('/api/catalogs/1/backups', json=body)


def test_test_connection_opens_or_creates_the_repository(client, catalog, fake_repo):  # noqa: F811
    d = client.post('/api/catalogs/1/backups/test',
                    json={'repo_uri': '/srv/restic', 'repo_password': 'correct horse'}).get_json()
    assert d['ok'] and d['repo_id'] == 'abc123' and not d['created']
    d = client.post('/api/catalogs/1/backups/test',
                    json={'repo_uri': '/srv/restic', 'repo_password': 'nope'}).get_json()
    assert not d['ok'] and d['kind'] == 'password'
    d = client.post('/api/catalogs/1/backups/test',
                    json={'repo_uri': '/srv/new', 'repo_password': 'longenough'}).get_json()
    assert not d['ok'] and d['kind'] == 'missing'
    d = client.post('/api/catalogs/1/backups/test',
                    json={'repo_uri': '/srv/new', 'repo_password': 'longenough',
                          'create': True}).get_json()
    assert d['ok'] and d['created']
    # Create again with another password: it exists - said so, never overwritten.
    d = client.post('/api/catalogs/1/backups/test',
                    json={'repo_uri': '/srv/new', 'repo_password': 'another one',
                          'create': True}).get_json()
    assert not d['ok'] and d['kind'] == 'exists' and 'already exists' in d['error']
    assert fake_repo.repos['/srv/new'] == 'longenough'


def test_create_list_and_never_return_a_secret(client, catalog, fake_repo, app_module):  # noqa: F811
    r = _create(client)
    assert r.status_code == 201, r.get_json()
    card = r.get_json()
    assert card['has_repo_password'] and 'correct horse' not in json.dumps(card)
    assert card['status']['state'] == 'needed' and card['type'] == 'restic'
    listed = client.get('/api/catalogs/1/backups').get_json()
    assert [c['name'] for c in listed['backups']] == ['backup-host nightly']
    assert 'correct horse' not in json.dumps(listed)
    assert 'restore_root' in listed and 'ssh' in listed
    conn = app_module.db.get_connection()
    enc = conn.execute('SELECT repo_secret_enc FROM backup_targets').fetchone()[0]
    conn.close()
    assert enc and 'correct horse' not in enc


def test_a_wrong_password_and_a_duplicate_name_are_refused(client, catalog, fake_repo):  # noqa: F811
    r = _create(client, repo_password='wrong')
    assert r.status_code == 400 and r.get_json()['kind'] == 'password'
    assert _create(client).status_code == 201
    assert _create(client).status_code == 409


def test_edit_keeps_the_password_unless_given_and_delete(client, catalog, fake_repo):  # noqa: F811
    card = _create(client).get_json()
    r = client.put(f"/api/catalogs/1/backups/{card['id']}",
                   json={'scope': 'everything', 'repo_uri': '/srv/restic'})
    assert r.status_code == 200 and r.get_json()['scope'] == 'everything', r.get_json()
    r = client.put(f"/api/catalogs/1/backups/{card['id']}", json={'repo_password': 'wrong'})
    assert r.status_code == 400
    r = client.delete(f"/api/catalogs/1/backups/{card['id']}")
    assert r.status_code == 200
    assert client.get('/api/catalogs/1/backups').get_json()['backups'] == []


def test_a_signed_in_owner_can_create(client, catalog, fake_repo, app_module, monkeypatch):  # noqa: F811
    """With authentication on, current_user() is a sqlite3.Row - which has no
    .get() (v5.8.1)."""
    mem = sqlite3.connect(':memory:')
    mem.row_factory = sqlite3.Row
    user = mem.execute("SELECT 7 AS id, 'izelpi' AS username, 1 AS is_admin").fetchone()
    monkeypatch.setattr(app_module.auth, 'current_user', lambda: user)
    assert _create(client).status_code == 201
    conn = app_module.db.get_connection()
    created_by = conn.execute('SELECT created_by FROM backup_targets').fetchone()[0]
    conn.close()
    assert created_by == 7


def test_the_dashboard_shows_each_card_without_a_secret(client, catalog, fake_repo):  # noqa: F811
    _create(client)
    b = client.get('/api/catalogs/1/dashboard').get_json()['live']['backups']
    assert (b['count'], b['needed'], b['running']) == (1, 1, 0)
    assert b['cards'][0]['name'] == 'backup-host nightly'
    assert b['cards'][0]['status']['reason'] == 'No backup yet'
    assert 'correct horse' not in json.dumps(b)


def test_nothing_to_stop_and_a_bad_snapshot_id(client, catalog, fake_repo):  # noqa: F811
    card = _create(client).get_json()
    assert client.post(f"/api/catalogs/1/backups/{card['id']}/cancel").status_code == 409
    r = client.post(f"/api/catalogs/1/backups/{card['id']}/restore", json={'snapshot_id': 'x;rm'})
    assert r.status_code == 400


# ── the real thing: restic, a repository in a folder ─────────────────────────

@pytest.fixture
def real_repo(monkeypatch, app_module, catalog, tmp_path):  # noqa: F811
    conn = app_module.db.get_connection()
    conn.execute('DELETE FROM backup_targets')
    conn.execute('DELETE FROM backup_runs')
    conn.commit()
    conn.close()
    monkeypatch.setattr(backup_restic, 'CACHE_DIR', str(tmp_path / 'cache'))
    monkeypatch.setattr(backups, 'PROGRESS_EVERY', 0.0)
    monkeypatch.setenv('BACKUP_RESTORE_ROOT', str(tmp_path / 'restores'))
    root = catalog.target_directory
    os.makedirs(os.path.join(root, 'Images', '2024'), exist_ok=True)
    with open(os.path.join(root, 'Images', '2024', 'a.jpg'), 'wb') as fh:
        fh.write(os.urandom(200_000))
    return str(tmp_path / 'repo')


def _wait(backup_id, seconds=60):
    end = time.time() + seconds
    while backups.active(backup_id) and time.time() < end:
        time.sleep(0.2)
    assert not backups.active(backup_id), 'the run did not finish'


@needs_restic
def test_back_up_verify_and_restore_for_real(client, catalog, real_repo):  # noqa: F811
    d = client.post('/api/catalogs/1/backups/test',
                    json={'repo_uri': real_repo, 'repo_password': 'correct horse',
                          'create': True}).get_json()
    assert d['ok'] and d['created'], d
    card = _create(client, repo_uri=real_repo).get_json()
    bid = card['id']

    assert client.post(f'/api/catalogs/1/backups/{bid}/run').status_code == 202
    _wait(bid)
    card = client.get('/api/catalogs/1/backups').get_json()['backups'][0]
    assert card['last_backup']['status'] == 'success', card
    assert card['status']['state'] == 'up_to_date' and card['last_backup']['snapshot_id']

    snaps = client.get(f'/api/catalogs/1/backups/{bid}/snapshots').get_json()
    assert len(snaps['snapshots']) == 1 and snaps['snapshots'][0]['host'].startswith('airlock-')

    assert client.post(f'/api/catalogs/1/backups/{bid}/verify').status_code == 202
    _wait(bid)
    card = client.get('/api/catalogs/1/backups').get_json()['backups'][0]
    assert card['last_verify']['status'] == 'success', card

    r = client.post(f'/api/catalogs/1/backups/{bid}/restore',
                    json={'snapshot_id': snaps['snapshots'][0]['id'], 'folder': 'check'})
    assert r.status_code == 202, r.get_json()
    _wait(bid)
    restored = r.get_json()['target'] + catalog.target_directory
    with open(os.path.join(restored, 'Images', '2024', 'a.jpg'), 'rb') as a, \
            open(os.path.join(catalog.target_directory, 'Images', '2024', 'a.jpg'), 'rb') as b:
        assert a.read() == b.read()
    copy = os.path.join(restored, 'cache', 'backup', backups.SNAPSHOT_DB)
    assert sqlite3.connect(copy).execute('SELECT 1').fetchone() == (1,)
    card = client.get('/api/catalogs/1/backups').get_json()['backups'][0]
    assert card['last_restore']['status'] == 'success'


@needs_restic
def test_a_wrong_stored_password_fails_the_run_with_a_sentence(client, catalog, real_repo, app_module):  # noqa: F811
    backup_restic.Repo(real_repo, 'correct horse').init()
    card = _create(client, repo_uri=real_repo).get_json()
    conn = app_module.db.get_connection()
    conn.execute('UPDATE backup_targets SET repo_secret_enc = ?',
                 (backup_secrets.encrypt(app_module.app.secret_key, 'not it'),))
    conn.commit()
    conn.close()
    client.post(f"/api/catalogs/1/backups/{card['id']}/run")
    _wait(card['id'])
    card = client.get('/api/catalogs/1/backups').get_json()['backups'][0]
    assert card['status'] == {'state': 'failed', 'reason': 'The repository password is wrong.'}


@needs_restic
def test_forget_keeps_only_this_cards_snapshots_by_its_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_restic, 'CACHE_DIR', str(tmp_path / 'cache'))
    repo = backup_restic.Repo(str(tmp_path / 'repo'), 'correct horse')
    repo.init()
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'f').write_text('x')
    for card in ('card:1', 'card:1', 'card:2'):
        repo.run('backup', '--host', 'airlock-x', '--tag', 'airlock', '--tag', card,
                 str(tmp_path / 'data'))
    repo.forget(['airlock', 'card:1'], {'daily': 1, 'weekly': 0, 'monthly': 0, 'yearly': 0})
    assert len(repo.snapshots(['airlock', 'card:1'])) == 1
    assert len(repo.snapshots(['airlock', 'card:2'])) == 1


def test_forget_clears_stale_locks_first_and_never_all_of_them(monkeypatch):
    """A backup the backend restart cut off leaves a lock; forget needs the
    repository to itself, so it runs plain `unlock` (stale locks only) first."""
    calls = []
    repo = backup_restic.Repo('/nowhere', 'pw')
    monkeypatch.setattr(repo, 'run', lambda *a, **k: calls.append(a) or '')
    repo.forget(['airlock', 'card:1'], {'daily': 7})
    assert calls[0] == ('unlock',)
    assert calls[1][0] == 'forget' and '--retry-lock' in calls[1]
    assert not any('--remove-all' in c for c in calls)

    calls.clear()
    def refuse(*a, **k):
        calls.append(a)
        if a[0] == 'unlock':
            raise backup_restic.ResticError('busy')
        return ''
    monkeypatch.setattr(repo, 'run', refuse)
    repo.forget(['airlock', 'card:1'], {'daily': 7})        # unlock failing is not fatal
    assert [c[0] for c in calls] == ['unlock', 'forget']


def test_the_database_copy_shows_its_progress_then_clears_it(tmp_path, monkeypatch):
    """VACUUM INTO is one long statement: a watcher reports the copy's size."""
    (tmp_path / 'workflow_results.db').write_bytes(b'x' * 1000)
    app_db = tmp_path / 'catalogs.db'
    conn = sqlite3.connect(app_db)
    backups.ensure_schema(conn.cursor())
    conn.execute("INSERT INTO backup_targets (id, catalog_id, name, repo_uri) VALUES (1, 1, 'n', '/r')")
    run_id = conn.execute("INSERT INTO backup_runs (target_id, kind, status, started_at) "
                          "VALUES (1, 'backup', 'running', 'x')").lastrowid
    conn.commit()
    monkeypatch.setattr(backups, 'PROGRESS_EVERY', 0.02)

    def slow_copy(local_root, app_db_path=None):
        tmp = os.path.join(local_root, backups.SNAPSHOT_DIRNAME, backups.SNAPSHOT_DB + '.tmp')
        os.makedirs(os.path.dirname(tmp), exist_ok=True)
        with open(tmp, 'wb') as f:
            f.write(b'y' * 400)
            f.flush()
            time.sleep(0.3)
        return 400
    monkeypatch.setattr(backups, 'prepare_snapshot', slow_copy)
    seen = []
    real = backups._update_run
    monkeypatch.setattr(backups, '_update_run',
                        lambda c, rid, **f: (seen.append(f.get('progress_json')), real(c, rid, **f)))
    backups._copy_catalog_db(conn, run_id, str(tmp_path), str(app_db))
    progress = [json.loads(p) for p in seen if p]
    assert progress and progress[0]['what'] == 'copy' and progress[0]['percent'] == 40.0
    assert progress[0]['bytes_total'] == 1000
    assert conn.execute('SELECT progress_json, phase FROM backup_runs').fetchone() == \
        (None, 'Copying the catalog database')


def test_progress_carries_when_it_was_written_and_the_file_being_read(tmp_path):
    conn = sqlite3.connect(tmp_path / 'app.db')
    backups.ensure_schema(conn.cursor())
    conn.execute("INSERT INTO backup_targets (id, catalog_id, name, repo_uri) VALUES (1, 1, 'n', '/r')")
    run_id = conn.execute("INSERT INTO backup_runs (target_id, kind, status, started_at) "
                          "VALUES (1, 'backup', 'running', 'x')").lastrowid

    class Proc:
        stdout = iter([json.dumps({'message_type': 'status', 'percent_done': 0.5,
                                   'current_files': ['/catalogs/family/Images/a.jpg']}) + '\n'])
        stderr = type('E', (), {'read': lambda self: ''})()

        def wait(self):
            return 0
    backups._stream(1, conn, run_id, Proc())
    p = json.loads(conn.execute('SELECT progress_json FROM backup_runs').fetchone()[0])
    assert p['current'] == '/catalogs/family/Images/a.jpg' and p['percent'] == 50.0 and p['at']


def test_filesystems_without_stable_inodes_are_recognised(tmp_path):
    """homeserver's photos are on exFAT: restic must not trust inode numbers there."""
    mounts_file = tmp_path / 'mounts'
    mounts_file.write_text(
        '/dev/mapper/vg-root / ext4 rw 0 0\n'
        '/dev/mapper/vg-root /catalogs ext4 rw 0 0\n'
        '/dev/sdc1 /catalogs/family/Images exfat rw 0 0\n'
        '/dev/sdd1 /mnt/my\\040drive vfat rw 0 0\n')
    mounts = backups._mounts(str(mounts_file))
    assert backups.fs_type('/catalogs/family/Images/2011/a.jpg', mounts) == 'exfat'
    assert backups.fs_type('/catalogs/family/cache/backup', mounts) == 'ext4'
    assert backups.fs_type('/catalogs/family/Imagesx', mounts) == 'ext4'
    assert backups.fs_type('/mnt/my drive/x', mounts) == 'vfat'
    assert backups.unstable_inodes(['/catalogs/family/cache/backup',
                                    '/catalogs/family/Images'], mounts)
    assert not backups.unstable_inodes(['/catalogs/family/cache/backup'], mounts)


def test_borg_keys_its_files_cache_on_mtime_and_size_where_inodes_move():
    import backup_borg
    repo = backup_borg.Repo('/nowhere', 'pw')
    plain = repo.create_args({'host': 'h', 'catalog': 'family', 'card': 1}, ['/a'], [])
    assert '--files-cache' not in plain
    moved = repo.create_args({'host': 'h', 'catalog': 'family', 'card': 1}, ['/a'], [], stable_inodes=False)
    assert moved[moved.index('--files-cache') + 1] == 'mtime,size'


@needs_restic
def test_restic_accepts_ignore_inode(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_restic, 'CACHE_DIR', str(tmp_path / 'cache'))
    repo = backup_restic.Repo(str(tmp_path / 'repo'), 'correct horse')
    repo.init()
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'f').write_text('x')
    repo.run('backup', '--ignore-inode', '--host', 'h', str(tmp_path / 'data'))
