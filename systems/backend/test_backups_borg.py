"""
Catalog backups with BorgBackup - the second type, beside restic
(test_backups.py, which this does not change).

    python -m pytest test_backups_borg.py

documentation/features/backups.md. The end-to-end tests run the borg binary
the backend image carries, against a repository in a temporary folder;
elsewhere they skip.
"""

import json
import os
import shutil
import sqlite3
import sys
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import backup_borg  # noqa: E402
import backup_secrets  # noqa: E402
import backup_ssh  # noqa: E402
import backups  # noqa: E402
from backup_borg import BorgError  # noqa: E402
from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402

HAVE_BORG = shutil.which('borg') is not None
needs_borg = pytest.mark.skipif(not HAVE_BORG, reason='borg is not installed here')
IDENT = {'host': 'airlock-abc123', 'catalog': 'family', 'card': 4}


# ── the pieces ───────────────────────────────────────────────────────────────

def test_borg_uris():
    assert backup_ssh.parse_borg('ssh://izelpi@192.168.1.16/media/usb1/borg/family') == \
        ('izelpi', '192.168.1.16', 22, '/media/usb1/borg/family')
    assert backup_ssh.parse_borg('ssh://izelpi@backup-host:2222/srv/borg') == \
        ('izelpi', 'backup-host', 2222, '/srv/borg')
    assert backup_ssh.parse_borg('/srv/borg') is None
    assert backup_borg.Repo('/srv/borg', 'pw').ssh_host() is None
    assert backup_borg.Repo('ssh://izelpi@backup-host/srv/b', 'pw').ssh_host() == ('backup-host', 22)


def test_a_cards_archives_are_named_for_it():
    assert backup_borg.archive_prefix(IDENT) == 'airlock-abc123-family-card4-'
    args = backup_borg.Repo('/r', 'pw').create_args(
        IDENT, ['/catalogs/family/Images'], ['/catalogs/family/tmp', '**/.import-staging'])
    assert args[-2] == '::airlock-abc123-family-card4-{utcnow:%Y-%m-%dT%H:%M:%S}'
    assert args[-1] == '/catalogs/family/Images'
    # Globs keep restic's meaning ('**' crosses folders); plain paths stay paths.
    assert '/catalogs/family/tmp' in args and 'sh:**/.import-staging' in args


def test_the_passphrase_and_borg_path_travel_in_the_environment(monkeypatch):
    monkeypatch.setattr(backup_ssh, 'ensure_key', lambda kind='restic': '/k')
    repo = backup_borg.Repo('ssh://izelpi@backup-host/srv/b', 'hunter22', [], {'remote_path': '/opt/borg/bin/borg'})
    env = repo._env()
    assert env['BORG_PASSPHRASE'] == 'hunter22' and env['BORG_REPO'] == 'ssh://izelpi@backup-host/srv/b'
    assert env['BORG_REMOTE_PATH'] == '/opt/borg/bin/borg'
    assert env['BORG_RSH'].startswith('ssh -i /k ') and 'StrictHostKeyChecking=yes' in env['BORG_RSH']
    assert 'hunter22' not in ' '.join(repo.create_args(IDENT, ['/x'], []))
    # A remote path that is not a plain path is never handed to a shell.
    assert backup_borg.Repo('/r', 'p', [], {'remote_path': 'borg; rm -rf /'})._env()[
        'BORG_REMOTE_PATH'] == backup_borg.DEFAULT_REMOTE_PATH


def test_borgs_own_key_is_not_restics(tmp_path, monkeypatch):
    if not shutil.which('ssh-keygen'):
        pytest.skip('no ssh-keygen here')
    monkeypatch.setattr(backup_ssh, 'SSH_DIR', str(tmp_path))
    monkeypatch.setattr(backup_ssh, 'KEY', str(tmp_path / 'id_ed25519'))
    monkeypatch.setattr(backup_ssh, 'KEY_BORG', str(tmp_path / 'id_ed25519_borg'))
    restic_key, borg_key = backup_ssh.public_key(), backup_ssh.public_key('borg')
    assert restic_key != borg_key and borg_key.endswith('airlock-borg')
    line = backup_ssh.borg_authorized_keys_line('/opt/borg/bin/borg', '/media/usb1/borg')
    assert line.startswith('command="/opt/borg/bin/borg serve --restrict-to-path /media/usb1/borg",restrict ssh-ed25519 ')


@pytest.mark.parametrize('text,kind', [
    ('izelpi@backup-host: Permission denied (publickey).', 'key'),
    ('Host key verification failed.', 'hostkey'),
    ('passphrase supplied in BORG_PASSPHRASE, by BORG_PASSCOMMAND or via BORG_PASSPHRASE_FD is incorrect.', 'password'),
    ('Repository /srv/x does not exist.', 'missing'),
    ('/srv/x is not a valid repository. Check repo config.', 'missing'),
    ('Remote: sh: 1: /opt/borg/bin/borg: command not found', 'remote'),
    ('Failed to create/acquire the lock /srv/x/lock.exclusive (timeout).', 'locked'),
    ('ssh: connect to host backup-host port 22: Connection refused', 'unreachable'),
])
def test_borg_errors_become_sentences(text, kind):
    assert backup_borg._explain(text, 'ssh://izelpi@backup-host/srv/x').kind == kind


def test_progress_lines():
    ev = backup_borg.parse_event(json.dumps({'type': 'archive_progress', 'original_size': 500,
                                             'nfiles': 7, 'finished': False}))
    assert ev == ('progress', {'percent': None, 'bytes_done': 500, 'bytes_total': 0,
                               'files_done': 7, 'files_total': 0, 'seconds_remaining': None})
    ev = backup_borg.parse_event(json.dumps({'type': 'progress_percent', 'current': 1,
                                             'total': 4, 'finished': False}))
    assert ev[1]['percent'] == 25.0
    assert backup_borg.parse_event(json.dumps({'type': 'log_message', 'message': 'x'})) == ('log', 'x')
    assert backup_borg.parse_event('not json') is None


def test_an_older_table_gains_the_options_column(tmp_path):
    conn = sqlite3.connect(tmp_path / 'app.db')
    conn.execute(backups.SCHEMA.replace(
        "        options_json           TEXT,               -- per type, not secret: borg's remote_path\n", ''))
    backups.ensure_schema(conn.cursor())
    assert 'options_json' in {r[1] for r in conn.execute('PRAGMA table_info(backup_targets)')}


# ── the routes, with borg replaced ───────────────────────────────────────────

class FakeBorg(backup_borg.Repo):
    repos = {}

    def probe(self):
        if self.uri not in FakeBorg.repos:
            raise BorgError(f'There is no borg repository at {self.uri} yet.', 'missing')
        if FakeBorg.repos[self.uri] != self.password:
            raise BorgError('The repository passphrase is wrong.', 'password')
        return 'b0rg'

    def init(self):
        FakeBorg.repos[self.uri] = self.password


def _clear(app_module):  # noqa: F811
    conn = app_module.db.get_connection()          # the app database outlives a test
    conn.execute('DELETE FROM backup_targets')
    conn.execute('DELETE FROM backup_runs')
    conn.commit()
    conn.close()


@pytest.fixture
def fake_borg(monkeypatch, app_module, catalog, tmp_path):  # noqa: F811
    FakeBorg.repos = {'/srv/borg': 'correct horse'}
    _clear(app_module)
    monkeypatch.setattr(app_module.backup_borg, 'Repo', FakeBorg)
    monkeypatch.setattr(backups.backup_borg, 'Repo', FakeBorg)
    monkeypatch.setattr(backup_ssh, 'SSH_DIR', str(tmp_path / 'ssh'))
    monkeypatch.setattr(backup_ssh, 'KEY', str(tmp_path / 'ssh' / 'id_ed25519'))
    monkeypatch.setattr(backup_ssh, 'KEY_BORG', str(tmp_path / 'ssh' / 'id_ed25519_borg'))
    return FakeBorg


def _create(client, **kw):  # noqa: F811
    body = {'name': 'backup-host borg', 'type': 'borg', 'scope': 'originals', 'repo_uri': '/srv/borg',
            'repo_password': 'correct horse', 'options': {'remote_path': '/opt/borg/bin/borg'},
            'schedule': {'kind': 'manual'}}
    body.update(kw)
    return client.post('/api/catalogs/1/backups', json=body)


def test_test_connection_opens_or_creates_a_borg_repository(client, catalog, fake_borg):  # noqa: F811
    d = client.post('/api/catalogs/1/backups/test', json={
        'type': 'borg', 'repo_uri': '/srv/borg', 'repo_password': 'correct horse'}).get_json()
    assert d['ok'] and d['repo_id'] == 'b0rg' and not d['created']
    d = client.post('/api/catalogs/1/backups/test', json={
        'type': 'borg', 'repo_uri': '/srv/new', 'repo_password': 'longenough', 'create': True}).get_json()
    assert d['ok'] and d['created']
    d = client.post('/api/catalogs/1/backups/test', json={
        'type': 'borg', 'repo_uri': '/srv/new', 'repo_password': 'another one', 'create': True}).get_json()
    assert not d['ok'] and d['kind'] == 'exists'
    d = client.post('/api/catalogs/1/backups/test', json={
        'type': 'borg', 'repo_uri': '/srv/borg', 'repo_password': 'x',
        'options': {'remote_path': 'borg && evil'}}).get_json()
    assert not d['ok'] and 'plain path' in d['error']


def test_a_borg_card_is_created_with_its_options_and_no_secret(client, catalog, fake_borg, app_module):  # noqa: F811
    r = _create(client)
    assert r.status_code == 201, r.get_json()
    card = r.get_json()
    assert card['type'] == 'borg' and card['options'] == {'remote_path': '/opt/borg/bin/borg'}
    listed = client.get('/api/catalogs/1/backups').get_json()
    assert listed['ssh']['borg']['public_key'].endswith('airlock-borg')
    assert listed['ssh']['public_key'] != listed['ssh']['borg']['public_key']
    assert 'correct horse' not in json.dumps(listed)
    assert _create(client, name='bad', repo_password='wrong').status_code == 400
    # Changing where borg is on the server is checked against the repository.
    r = client.put(f"/api/catalogs/1/backups/{card['id']}",
                   json={'options': {'remote_path': '/usr/bin/borg'}})
    assert r.status_code == 200 and r.get_json()['options']['remote_path'] == '/usr/bin/borg'


def test_restic_cards_are_untouched_by_borg(client, catalog, fake_borg):  # noqa: F811
    assert _create(client, type='rsync').status_code == 400
    r = client.post('/api/catalogs/1/backups/1/restore', json={'snapshot_id': 'x;rm'})
    assert r.status_code in (400, 404)


# ── the real thing: borg, a repository in a folder ───────────────────────────

@pytest.fixture
def real_borg(monkeypatch, app_module, catalog, tmp_path):  # noqa: F811
    _clear(app_module)
    monkeypatch.setattr(backup_borg, 'BASE_DIR', str(tmp_path / 'borg-home'))
    monkeypatch.setattr(backup_ssh, 'SSH_DIR', str(tmp_path / 'ssh'))
    monkeypatch.setattr(backup_ssh, 'KEY_BORG', str(tmp_path / 'ssh' / 'id_ed25519_borg'))
    monkeypatch.setattr(backups, 'PROGRESS_EVERY', 0.0)
    monkeypatch.setenv('BACKUP_RESTORE_ROOT', str(tmp_path / 'restores'))
    root = catalog.target_directory
    os.makedirs(os.path.join(root, 'Images', '2024'), exist_ok=True)
    with open(os.path.join(root, 'Images', '2024', 'a.jpg'), 'wb') as fh:
        fh.write(os.urandom(300_000))
    return str(tmp_path / 'borg-repo')


def _wait(backup_id, seconds=90):
    end = time.time() + seconds
    while backups.active(backup_id) and time.time() < end:
        time.sleep(0.2)
    assert not backups.active(backup_id), 'the run did not finish'


def _card(client, bid):  # noqa: F811
    return [c for c in client.get('/api/catalogs/1/backups').get_json()['backups'] if c['id'] == bid][0]


@needs_borg
def test_back_up_verify_and_restore_with_borg(client, catalog, real_borg):  # noqa: F811
    d = client.post('/api/catalogs/1/backups/test', json={
        'type': 'borg', 'repo_uri': real_borg, 'repo_password': 'correct horse', 'create': True}).get_json()
    assert d['ok'] and d['created'], d
    card = _create(client, repo_uri=real_borg).get_json()
    bid = card['id']

    assert client.post(f'/api/catalogs/1/backups/{bid}/run').status_code == 202
    _wait(bid)
    card = _card(client, bid)
    assert card['last_backup']['status'] == 'success', card
    assert card['status']['state'] == 'up_to_date'
    assert card['last_backup']['snapshot_id'].startswith(f'{backups.host_name()}-')
    assert card['last_backup']['data_added'] > 0

    snaps = client.get(f'/api/catalogs/1/backups/{bid}/snapshots').get_json()
    assert len(snaps['snapshots']) == 1 and snaps['snapshots'][0]['time'].endswith('+00:00')

    assert client.post(f'/api/catalogs/1/backups/{bid}/verify').status_code == 202
    _wait(bid)
    assert _card(client, bid)['last_verify']['status'] == 'success'

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
    assert _card(client, bid)['last_restore']['status'] == 'success'

    # One folder only.
    r = client.post(f'/api/catalogs/1/backups/{bid}/restore',
                    json={'snapshot_id': snaps['snapshots'][0]['id'], 'folder': 'one',
                          'path': catalog.target_directory + '/Images/2024'})
    _wait(bid)
    one = r.get_json()['target'] + catalog.target_directory
    assert os.path.exists(os.path.join(one, 'Images', '2024', 'a.jpg'))
    assert not os.path.exists(os.path.join(one, 'cache'))


@needs_borg
def test_a_wrong_stored_passphrase_fails_the_run_with_a_sentence(client, catalog, real_borg, app_module):  # noqa: F811
    backup_borg.Repo(real_borg, 'correct horse').init()
    card = _create(client, repo_uri=real_borg).get_json()
    conn = app_module.db.get_connection()
    conn.execute('UPDATE backup_targets SET repo_secret_enc = ?',
                 (backup_secrets.encrypt(app_module.app.secret_key, 'not it'),))
    conn.commit()
    conn.close()
    client.post(f"/api/catalogs/1/backups/{card['id']}/run")
    _wait(card['id'])
    assert _card(client, card['id'])['status'] == {
        'state': 'failed', 'reason': 'The repository passphrase is wrong.'}


@needs_borg
def test_prune_keeps_only_this_cards_archives_by_its_retention(tmp_path, monkeypatch):
    monkeypatch.setattr(backup_borg, 'BASE_DIR', str(tmp_path / 'home'))
    repo = backup_borg.Repo(str(tmp_path / 'repo'), 'correct horse')
    repo.init()
    (tmp_path / 'data').mkdir()
    (tmp_path / 'data' / 'f').write_text('x')
    mine, other = dict(IDENT), dict(IDENT, card=5)
    for i, who in enumerate((mine, mine, other)):
        repo.run('create', f'::{backup_borg.archive_prefix(who)}2026-10-1{i}T02:00:00',
                 str(tmp_path / 'data'))
    repo.run('create', f'::{backup_borg.archive_prefix(mine)}2026-10-13T02:00:00.checkpoint',
             str(tmp_path / 'data'))
    assert all('.checkpoint' not in s['id'] for s in repo.snapshots(mine))
    repo.prune(mine, {'daily': 1, 'weekly': 0, 'monthly': 0, 'yearly': 0})
    assert len(repo.snapshots(mine)) == 1
    assert len(repo.snapshots(other)) == 1
