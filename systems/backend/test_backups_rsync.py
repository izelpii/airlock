"""
Catalog backups with rsync - the third type, beside restic (test_backups.py)
and borg (test_backups_borg.py), which this does not change.

    python -m pytest test_backups_rsync.py

documentation/features/backups.md. The end-to-end tests run the rsync the
backend image carries, into a folder; elsewhere they skip.
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

import backup_rsync  # noqa: E402
import backup_ssh  # noqa: E402
import backups  # noqa: E402
from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402

needs_rsync = pytest.mark.skipif(shutil.which('rsync') is None, reason='rsync is not installed here')


# ── the pieces ───────────────────────────────────────────────────────────────

def test_destinations():
    assert backup_rsync.parse_dest('izelpi@192.168.1.16:/media/usb1/rsync/family') == \
        ('izelpi', '192.168.1.16', '/media/usb1/rsync/family')
    assert backup_rsync.parse_dest('backup-host:/srv/copy') == (None, 'backup-host', '/srv/copy')
    assert backup_rsync.parse_dest('/srv/copy') is None
    assert backup_rsync.Repo('/srv/copy').ssh_host() is None


def test_over_ssh_the_path_is_sent_relative_to_the_locked_folder():
    """rrsync takes paths relative to its folder."""
    repo = backup_rsync.Repo('izelpi@backup-host:/media/usb1/rsync/family')
    assert repo.root() == '/media/usb1/rsync'
    assert repo.target() == 'izelpi@backup-host:family'
    assert repo.target('Images') == 'izelpi@backup-host:family/Images'
    deep = backup_rsync.Repo('izelpi@backup-host:/media/usb1/rsync/a/family', options={'root': '/media/usb1/rsync'})
    assert deep.target() == 'izelpi@backup-host:a/family'
    with pytest.raises(backup_rsync.RsyncError):
        backup_rsync.Repo('izelpi@backup-host:/elsewhere/family', options={'root': '/media/usb1/rsync'}).target()
    assert backup_rsync.Repo('/srv/copy').target() == '/srv/copy'


def test_excludes_are_anchored_at_the_catalog():
    root = '/catalogs/family'
    paths, excludes = backups.paths_for({'scope': 'everything'}, root)
    rules = backup_rsync.excludes_for(root, excludes)
    assert '/workflow_results.db' in rules and '/tmp' in rules
    assert '.import-staging' in rules and '/cache/backup/*.tmp' in rules
    assert '/.airlock-replaced/' in rules


def test_progress_and_stats():
    p = backup_rsync.parse_progress('    1,234,567  45%   10.00MB/s    0:00:12 (xfr#5, to-chk=10/100)')
    assert p['percent'] == 45.0 and p['bytes_done'] == 1234567 and p['files_done'] == 90
    assert backup_rsync.parse_progress('sending incremental file list') is None
    stats = backup_rsync.parse_stats('Number of files: 120 (reg: 99, dir: 21)\n'
                                     'Number of regular files transferred: 3\n'
                                     'Total file size: 570,870,212 bytes\n'
                                     'Total transferred file size: 1,024 bytes\n')
    assert (stats['files_new'], stats['data_added'], stats['total_bytes_processed']) == (3, 1024, 570870212)


def test_rsyncs_own_key_and_line(tmp_path, monkeypatch):
    if not shutil.which('ssh-keygen'):
        pytest.skip('no ssh-keygen here')
    monkeypatch.setattr(backup_ssh, 'SSH_DIR', str(tmp_path))
    for name in ('KEY', 'KEY_BORG', 'KEY_RSYNC'):
        monkeypatch.setattr(backup_ssh, name, str(tmp_path / getattr(backup_ssh, name).rsplit('/', 1)[1]))
    keys = {backup_ssh.public_key(k) for k in ('restic', 'borg', 'rsync')}
    assert len(keys) == 3
    line = backup_ssh.rsync_authorized_keys_line('/media/usb1/rsync')
    assert line.startswith('command="rrsync /media/usb1/rsync",restrict ssh-ed25519 ')
    assert line.endswith('airlock-rsync')


@pytest.mark.parametrize('text,kind', [
    ('izelpi@backup-host: Permission denied (publickey).', 'key'),
    ('Host key verification failed.', 'hostkey'),
    ('rsync: [sender] change_dir "/x" failed: No such file or directory (2)', 'missing'),
    ('rsync: [Receiver] mkdir "/x" failed: Permission denied (13)', 'denied'),
    ('ssh: connect to host backup-host port 22: Connection refused', 'unreachable'),
    ('rsync: write failed on "/x/a.jpg": No space left on device (28)', 'full'),
])
def test_rsync_errors_become_sentences(text, kind):
    assert backup_rsync._explain(text, 'izelpi@backup-host:/x').kind == kind


def test_verify_counts_files_not_folders():
    out = ('.d..t...... ./\n>f+++++++++ Images/new.jpg\n>fc.t...... Images/changed.jpg\n'
           '*deleting   Images/gone.jpg\ncd+++++++++ Images/2025/\n.f..t...... Images/touched.jpg\n')
    assert [l.split(' ', 1)[1] for l in backup_rsync.changed_files(out)] == \
        ['Images/new.jpg', 'Images/changed.jpg', 'Images/gone.jpg']


# ── through the routes, into a folder ────────────────────────────────────────

@pytest.fixture
def copy_dir(monkeypatch, app_module, catalog, tmp_path):  # noqa: F811
    conn = app_module.db.get_connection()
    conn.execute('DELETE FROM backup_targets')
    conn.execute('DELETE FROM backup_runs')
    conn.commit()
    conn.close()
    monkeypatch.setattr(backups, 'PROGRESS_EVERY', 0.0)
    monkeypatch.setenv('BACKUP_RESTORE_ROOT', str(tmp_path / 'restores'))
    root = catalog.target_directory
    os.makedirs(os.path.join(root, 'Images', '2024'), exist_ok=True)
    for name in ('a.jpg', 'b.jpg'):
        with open(os.path.join(root, 'Images', '2024', name), 'wb') as fh:
            fh.write(os.urandom(200_000))
    return str(tmp_path / 'copy' / 'family')


def _wait(bid, seconds=60):
    end = time.time() + seconds
    while backups.active(bid) and time.time() < end:
        time.sleep(0.2)
    assert not backups.active(bid), 'the run did not finish'


def _card(client, bid):  # noqa: F811
    return [c for c in client.get('/api/catalogs/1/backups').get_json()['backups'] if c['id'] == bid][0]


@needs_rsync
def test_copy_verify_and_restore_with_rsync(client, catalog, copy_dir):  # noqa: F811
    root = catalog.target_directory
    d = client.post('/api/catalogs/1/backups/test', json={'type': 'rsync', 'repo_uri': copy_dir}).get_json()
    assert not d['ok'] and d['kind'] == 'missing', d
    d = client.post('/api/catalogs/1/backups/test',
                    json={'type': 'rsync', 'repo_uri': copy_dir, 'create': True}).get_json()
    assert d['ok'] and d['created'], d
    r = client.post('/api/catalogs/1/backups', json={'type': 'rsync', 'name': 'plain copy',
                                                      'repo_uri': copy_dir, 'schedule': {'kind': 'manual'}})
    assert r.status_code == 201, r.get_json()
    card = r.get_json()
    bid = card['id']
    assert card['type'] == 'rsync' and not card['has_repo_password']

    assert client.post(f'/api/catalogs/1/backups/{bid}/run').status_code == 202
    _wait(bid)
    card = _card(client, bid)
    assert card['last_backup']['status'] == 'success', card
    assert card['status']['state'] == 'up_to_date' and card['last_backup']['files_new'] >= 2
    # A plain copy in the catalog's own layout, readable as it is.
    for name in ('a.jpg', 'b.jpg'):
        with open(os.path.join(copy_dir, 'Images', '2024', name), 'rb') as a, \
                open(os.path.join(root, 'Images', '2024', name), 'rb') as b:
            assert a.read() == b.read()
    assert sqlite3.connect(os.path.join(copy_dir, 'cache', 'backup', backups.SNAPSHOT_DB)) \
        .execute('SELECT 1').fetchone() == (1,)

    assert client.post(f'/api/catalogs/1/backups/{bid}/verify').status_code == 202
    _wait(bid)
    assert _card(client, bid)['last_verify']['status'] == 'success'

    # Damage the copy: verify says so.
    with open(os.path.join(copy_dir, 'Images', '2024', 'a.jpg'), 'r+b') as fh:
        fh.write(b'XXXX')
    # A different time, as an edited file has: within the same second as the
    # original, rsync's quick check would think them the same and not repair it.
    os.utime(os.path.join(copy_dir, 'Images', '2024', 'a.jpg'), (1_000_000_000, 1_000_000_000))
    client.post(f'/api/catalogs/1/backups/{bid}/verify')
    _wait(bid)
    card = _card(client, bid)
    assert card['last_verify']['status'] == 'failed' and 'a.jpg' in card['last_verify']['error']

    # A file deleted from the catalog is moved aside in the copy, not lost.
    # (A second apart: rsync's quick check compares times to the second, and the
    # new database copy must not look like the last one.)
    time.sleep(1.1)
    os.remove(os.path.join(root, 'Images', '2024', 'b.jpg'))
    client.post(f'/api/catalogs/1/backups/{bid}/run')
    _wait(bid)
    assert not os.path.exists(os.path.join(copy_dir, 'Images', '2024', 'b.jpg'))
    replaced = os.path.join(copy_dir, backup_rsync.REPLACED_DIR)
    stamps = os.listdir(replaced)
    assert len(stamps) == 1 and os.path.exists(os.path.join(replaced, stamps[0], 'Images', '2024', 'b.jpg'))
    # ...but the superseded database copies are not kept (40 GB a night on family).
    assert not os.listdir(os.path.join(replaced, stamps[0], 'cache', 'backup'))
    client.post(f'/api/catalogs/1/backups/{bid}/verify')
    _wait(bid)
    assert _card(client, bid)['last_verify']['status'] == 'success'   # the repaired a.jpg

    snaps = client.get(f'/api/catalogs/1/backups/{bid}/snapshots').get_json()['snapshots']
    assert [s['id'] for s in snaps] == ['current']
    r = client.post(f'/api/catalogs/1/backups/{bid}/restore',
                    json={'snapshot_id': 'current', 'folder': 'back', 'path': root + '/Images/2024'})
    assert r.status_code == 202, r.get_json()
    _wait(bid)
    back = r.get_json()['target']
    with open(os.path.join(back, 'Images', '2024', 'a.jpg'), 'rb') as a, \
            open(os.path.join(root, 'Images', '2024', 'a.jpg'), 'rb') as b:
        assert a.read() == b.read()
    assert not os.path.exists(os.path.join(back, 'cache'))
    assert not os.path.exists(os.path.join(back, backup_rsync.REPLACED_DIR))
    assert _card(client, bid)['last_restore']['status'] == 'success'
    assert client.post(f'/api/catalogs/1/backups/{bid}/restore',
                       json={'snapshot_id': 'abcdef12'}).status_code == 400


@needs_rsync
def test_an_rsync_card_needs_no_password_but_others_still_do(client, catalog, copy_dir):  # noqa: F811
    os.makedirs(copy_dir)
    assert client.post('/api/catalogs/1/backups', json={
        'type': 'rsync', 'name': 'c', 'repo_uri': copy_dir}).status_code == 201
    r = client.post('/api/catalogs/1/backups', json={'type': 'restic', 'name': 'r', 'repo_uri': copy_dir})
    assert r.status_code == 400 and 'password' in r.get_json()['error']
    assert client.post('/api/catalogs/1/backups', json={
        'type': 'nope', 'name': 'x', 'repo_uri': copy_dir}).status_code == 400
    assert 'rsync' in json.dumps(client.get('/api/catalogs/1/backups').get_json()['ssh'])
