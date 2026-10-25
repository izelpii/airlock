"""
Publishing a deployment to a server, phase 3: servers, runs, preflight and the
Preview run (documentation/specs/deployment-publish-to-server-spec.md).

    python -m pytest test_publish.py

The server is a LocalRemote: a folder standing in for its home, reached with
the same rsync and shell commands an SSHRemote sends over SSH. Everything but
the SSH hop is real.
"""

import json
import os
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import deployment_pack  # noqa: E402
import publish  # noqa: E402
import publish_ssh  # noqa: E402
from test_deployment_pack import catalogs  # noqa: F401,E402  (fixture)
from test_photo_dates_batch import app_module  # noqa: F401,E402  (fixture)


@pytest.fixture(autouse=True)
def key_store(tmp_path, monkeypatch):
    d = str(tmp_path / 'publish-ssh')
    monkeypatch.setattr(publish_ssh, 'SSH_DIR', d)
    monkeypatch.setattr(publish_ssh, 'KNOWN_HOSTS', os.path.join(d, 'known_hosts'))


@pytest.fixture
def catalog(catalogs, app_module):  # noqa: F811
    """The packer's fixture catalog, given a database with one deployment of album 1."""
    target = os.path.join(catalogs['root'], 'family')
    app_module.CatalogDatabase(target)
    db = os.path.join(target, 'workflow_results.db')
    conn = sqlite3.connect(db)
    conn.execute("INSERT INTO albums (id, name, updated_at) VALUES (1, 'Summer', '2026-01-01 00:00:00')")
    conn.execute("INSERT INTO album_deployments (id, slug, name) VALUES (1, 'album2026', 'Album 2026')")
    conn.execute('INSERT INTO album_deployment_albums (deployment_id, album_id) VALUES (1, 1)')
    conn.commit()
    conn.close()
    albums = os.path.join(target, 'cache', 'Albums', '00001')
    os.makedirs(albums, exist_ok=True)
    with open(os.path.join(albums, 'album.json'), 'w') as fh:
        json.dump({'deployed_at': '2026-06-01T00:00:00'}, fh)
    return {**catalogs, 'target': target, 'db': db}


def _server(**kw):
    s = {'id': 1, 'host': 'h', 'port': 22, 'ssh_user': 'ubuntu', 'remote_dir': 'galleries/album2026',
         'public_url': 'https://photos.example.com/', 'domain': 'photos.example.com',
         'gallery_image': 'localhost/album-gallery:amd64'}
    s.update(kw)
    return s


def _ctx(catalog, **kw):
    ctx = {'republish': lambda: {}, 'conn_factory': lambda: sqlite3.connect(catalog['db']),
           'catalogs_root': catalog['root'], 'catalog_slug': 'family',
           'catalog_dir': catalog['target'], 'deployment_id': 1,
           'deployment_slug': 'album2026', 'first_publish': False}
    ctx.update(kw)
    return ctx


def _run(catalog):
    conn = sqlite3.connect(catalog['db'])
    publish.ensure_schema(conn)
    conn.execute("INSERT INTO deployment_servers (id, deployment_id, name, host, ssh_user, "
                 "remote_dir, public_url) VALUES (1, 1, 's', 'h', 'ubuntu', 'galleries/album2026', "
                 "'https://photos.example.com/') ON CONFLICT(id) DO NOTHING")
    run_id = publish.start_run(conn, 1, 'preview')
    conn.close()
    return publish.Run(catalog['db'], run_id)


def _populate(catalog, home):
    """The first publish, by hand: the stage rsynced across, and the server's .env."""
    info = deployment_pack.inspect(catalog['root'], 'family', 'album2026')
    stage = deployment_pack.stage_dir_for(catalog['target'], 'album2026')
    deployment_pack.refresh_stage(info, stage, 'localhost/album-gallery:amd64', 'photos.example.com')
    dest = os.path.join(home, 'galleries', 'album2026')
    os.makedirs(dest, exist_ok=True)
    subprocess.run(['rsync', '-a', stage + '/', dest + '/'], check=True)
    with open(os.path.join(dest, '.env'), 'w') as fh:
        fh.write('ALBUM_SESSION_SECRET=keep-me\n')
    return dest


# ── reading rsync, and the safety rules ──────────────────────────────────────

ITEMIZED = """\
cd+++++++++ catalogs/family/cache/Deployments/album2026/00002/
>f+++++++++ catalogs/family/cache/Deployments/album2026/00002/c_alt.jpg
>f.st...... catalogs/family/cache/Deployments/album2026/albums.json
.d..t...... catalogs/
*deleting   catalogs/family/cache/Deployments/album2026/00001/b_alt.jpg

Number of files: 12 (reg: 9, dir: 3)
Total transferred file size: 1,234,567 bytes
"""


def test_the_plan_is_read_from_rsync():
    plan = publish.parse_itemize(ITEMIZED)
    assert (plan['new'], plan['changed'], plan['deleted']) == (1, 1, 1)
    assert plan['bytes'] == 1234567
    assert plan['deletions'] == ['catalogs/family/cache/Deployments/album2026/00001/b_alt.jpg']


def test_the_safety_rules():
    plenty = {'free_bytes': 50 << 30, 'remote_files': 100}
    ok = publish.check_plan(publish.parse_itemize(ITEMIZED), plenty)
    assert ok == {'stop': None, 'confirm': None, 'up_to_date': False}

    env = publish.parse_itemize('*deleting   .env\n')
    assert 'env' in publish.check_plan(env, plenty)['stop']

    big = publish.parse_itemize('>f+++++++++ a.jpg\nTotal transferred file size: 5000000000 bytes\n')
    assert 'Grow its disk' in publish.check_plan(big, {'free_bytes': 2 << 30, 'remote_files': 1})['stop']

    many = publish.parse_itemize(''.join(f'*deleting   p{i}.jpg\n' for i in range(20)))
    assert '20 of the 100' in publish.check_plan(many, plenty)['confirm']

    nothing = publish.parse_itemize('Total transferred file size: 0 bytes\n')
    assert publish.check_plan(nothing, plenty)['up_to_date']


# ── the preview, end to end ──────────────────────────────────────────────────

def test_an_empty_server_needs_setup_first(catalog, tmp_path):
    run = _run(catalog)
    remote = publish.LocalRemote(_server(), str(tmp_path / 'home'))
    result = publish.run_preview(run, _ctx(catalog), _server(), remote)
    assert result['needs_setup'] and 'Set it up' in result['message']
    assert result['stage']['linked'] and result['stage']['files'] == 8


def test_an_unchanged_gallery_is_up_to_date_and_env_is_never_touched(catalog, tmp_path):
    home = str(tmp_path / 'home')
    dest = _populate(catalog, home)
    run = _run(catalog)
    publish.execute(run, 'preview', _ctx(catalog), _server(), publish.LocalRemote(_server(), home))
    conn = sqlite3.connect(catalog['db'])
    conn.row_factory = sqlite3.Row
    row = publish.get_run(conn, run.id)
    conn.close()
    assert row['status'] == 'succeeded', row['error']
    assert row['result']['up_to_date'] and row['result']['plan']['deleted'] == 0
    assert row['result']['server']['has_bundle'] and row['result']['server']['remote_files'] == 12
    assert open(os.path.join(dest, '.env')).read() == 'ALBUM_SESSION_SECRET=keep-me\n'
    assert 'Previewing' in row['log']


def test_a_removed_photo_and_an_edited_one_show_in_the_plan(catalog, tmp_path):
    home = str(tmp_path / 'home')
    dest = _populate(catalog, home)
    section_path = os.path.join(catalog['album'], 'section-1.json')
    section = json.load(open(section_path))
    section['photos'] = section['photos'][:1]
    json.dump(section, open(section_path, 'w'))
    os.remove(os.path.join(catalog['album'], 'b_alt.jpg'))
    src = catalog['files']['a_alt.jpg']
    with open(src + '.n', 'wb') as fh:
        fh.write(b'turned' * 99)
    os.replace(src + '.n', src)

    run = _run(catalog)
    result = publish.run_preview(run, _ctx(catalog), _server(),
                                 publish.LocalRemote(_server(), home))
    assert result['plan']['deleted'] == 1 and result['deletions'][0].endswith('b_alt.jpg')
    assert result['plan']['changed'] >= 2           # a_alt.jpg and section-1.json
    assert not result['up_to_date'] and not result['stop']
    assert os.path.exists(os.path.join(dest, 'catalogs/family/cache/Deployments/'
                                             'album2026/00001/b_alt.jpg'))   # a dry run


def test_preflight_names_stale_albums_and_unsynced_events(catalog, tmp_path):
    conn = sqlite3.connect(catalog['db'])
    conn.execute("INSERT INTO file_contents (id, file_path, file_name, removed_at) "
                 "VALUES (5, 'x', 'x.jpg', NULL)")
    conn.execute("INSERT INTO album_sections (id, album_id, name, position, source_event_id) "
                 "VALUES (1, 1, 'Beach', 0, 3)")
    conn.execute("INSERT INTO section_photos (section_id, file_id) VALUES (1, 5)")
    conn.execute("INSERT INTO events (id, name, start_time, end_time) "
                 "VALUES (3, 'Beach day', '2026-05-01T10:00:00', '2026-05-01T18:00:00')")
    conn.execute("INSERT INTO file_contents (id, file_path, file_name) VALUES (6, 'y', 'y.jpg')")
    conn.execute("INSERT INTO event_files (event_id, file_id) VALUES (3, 5), (3, 6)")
    conn.execute("INSERT INTO photo_edits (file_id, orientation, version, render_status, updated_at) "
                 "VALUES (5, 6, 1, 'ready', '2026-09-01T00:00:00+00:00')")
    conn.commit()
    stale = publish.stale_albums(conn, catalog['target'], 1)
    events = publish.unsynced_events(conn, 1)
    conn.close()
    assert stale == [{'album_id': 1, 'name': 'Summer',
                      'reason': 'a photo in it was turned or flipped'}]
    assert events[0]['event'] == 'Beach day' and events[0]['missing'] == 1


def test_one_run_at_a_time_and_a_restart_frees_the_server(catalog):
    run = _run(catalog)
    conn = sqlite3.connect(catalog['db'])
    with pytest.raises(publish.PublishError) as e:
        publish.start_run(conn, 1, 'preview')
    assert e.value.code == 'busy'
    assert publish.interrupt_stale(conn) == 1
    assert publish.start_run(conn, 1, 'preview') != run.id
    conn.close()


def test_stop_ends_the_running_command(catalog, tmp_path):
    run = _run(catalog)
    publish.register(run)
    remote = publish.LocalRemote(_server(), str(tmp_path / 'home'))
    out = {}

    def go():
        try:
            publish._call(run, remote.command('sleep 30'))
        except publish.Stopped:
            out['stopped'] = True
    t = threading.Thread(target=go)
    t.start()
    for _ in range(50):
        if run.proc:
            break
        time.sleep(0.05)
    assert publish.stop_run(run.id)
    t.join(10)
    publish.unregister(run)
    assert out.get('stopped')


# ── the routes ───────────────────────────────────────────────────────────────

@pytest.fixture
def client(app_module, catalog, monkeypatch):  # noqa: F811
    cat = SimpleNamespace(id=1, name='Family', slug='family', target_directory=catalog['target'],
                          ingest_mode='copy', source_root=None)
    monkeypatch.setattr(app_module.Catalog, 'get_by_id',
                        classmethod(lambda cls, cid: cat if cid == 1 else None))
    monkeypatch.setattr(app_module, '_publish_deployment', lambda *a, **k: {'skipped': []})
    monkeypatch.setattr(app_module, '_write_auth_file', lambda *a, **k: None)
    return app_module.app.test_client()


BASE = '/api/catalogs/1/deployments/1/servers'
FORM = {'name': 'EC2', 'host': '203.0.113.10', 'ssh_user': 'ubuntu',
        'remote_dir': 'galleries/album2026', 'public_url': 'https://photos.example.com'}


def _wait_runs():
    for t in [t for t in threading.enumerate() if t.name.startswith('publish-')]:
        t.join(30)


def test_adding_a_server_makes_its_key_and_shows_the_line(client):
    r = client.post(BASE, json=FORM)
    assert r.status_code == 201, r.json
    s = r.json
    assert s['authorized_keys_line'].startswith('ssh-ed25519 ')
    assert s['authorized_keys_line'].endswith('airlock-publish family/album2026 -> 203.0.113.10')
    assert s['public_url'] == 'https://photos.example.com/' and s['host_key'] is None
    assert 'PRIVATE' not in json.dumps(r.json)
    listed = client.get(BASE).json['servers']
    assert [x['name'] for x in listed] == ['EC2'] and listed[0]['last_run'] is None
    assert client.get(f"{BASE}/{s['id']}/key").json['authorized_keys_line'] == \
        s['authorized_keys_line']
    bad = client.post(BASE, json={**FORM, 'remote_dir': '../../etc'})
    assert bad.status_code == 400


def test_pinning_asks_first_and_moving_the_server_unpins_it(client, tmp_path, monkeypatch):
    sid = client.post(BASE, json=FORM).json['id']
    key = str(tmp_path / 'hostkey')
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', key], check=True)
    kind, body = open(key + '.pub').read().split()[:2]
    monkeypatch.setattr(publish_ssh, '_keyscan', lambda h, p: [f'{h} {kind} {body}'])

    asked = client.post(f'{BASE}/{sid}/pin', json={}).json
    assert asked['fingerprint'].startswith('SHA256:') and asked['pinned'] is None
    assert client.post(f'{BASE}/{sid}/pin',
                       json={'confirm': True, 'fingerprint': 'SHA256:wrong'}).status_code == 409
    pinned = client.post(f'{BASE}/{sid}/pin',
                         json={'confirm': True, 'fingerprint': asked['fingerprint']}).json
    assert pinned['pinned'] == asked['fingerprint']
    moved = client.put(f'{BASE}/{sid}', json={'host': '203.0.113.11'}).json
    assert moved['host_key'] is None


def test_a_preview_runs_in_the_background_and_reports(client, app_module, tmp_path,
                                                     monkeypatch, catalog):
    sid = client.post(BASE, json=FORM).json['id']
    home = str(tmp_path / 'home')
    _populate(catalog, home)
    monkeypatch.setattr(app_module, '_publish_remote',
                        lambda server, run_id: publish.LocalRemote(server, home))
    r = client.post(f'{BASE}/{sid}/preview')
    assert r.status_code == 202
    run_id = r.json['run_id']
    _wait_runs()
    run = client.get(f'/api/catalogs/1/deployments/1/publish-runs/{run_id}').json
    assert run['status'] == 'succeeded', run.get('error')
    assert run['result']['up_to_date'] and run['result']['checksum']    # first publish
    assert client.get(BASE).json['servers'][0]['last_run']['id'] == run_id
    assert client.post(f'/api/catalogs/1/deployments/1/publish-runs/{run_id}/stop').status_code == 409


def test_a_preview_needs_a_pinned_server(client):
    sid = client.post(BASE, json=FORM).json['id']
    run_id = client.post(f'{BASE}/{sid}/preview').json['run_id']
    _wait_runs()
    run = client.get(f'/api/catalogs/1/deployments/1/publish-runs/{run_id}').json
    assert run['status'] == 'failed' and 'confirm its identity' in run['error']


def test_forgetting_a_server_removes_its_key(client):
    s = client.post(BASE, json=FORM).json
    assert publish_ssh.has_key(s['credential_id'])
    r = client.delete(f"{BASE}/{s['id']}")
    assert r.status_code == 200 and 'authorized_keys' in r.json['note']
    assert not publish_ssh.has_key(s['credential_id'])
    assert client.get(BASE).json['servers'] == []


def test_publishing_is_for_owners_only():
    import auth
    assert '/servers' in auth._OWNER_RULE_FRAGMENTS
    assert '/publish-runs' in auth._OWNER_RULE_FRAGMENTS


# ── phase 4: publish - apply and verify ──────────────────────────────────────

def _site(locked=True, broken=None):
    """A fetch() answering as the live gallery would (spec 6.6)."""
    def fetch(url):
        path = url.split('photos.example.com', 1)[1]
        if broken and broken in path:
            return 404, {}, 'Not found'
        if path == '/':
            return 302, {'location': '/d/family/album2026/'}, ''
        if path.endswith('/deployment.json'):
            return 200, {}, json.dumps({'slug': 'album2026'})
        if path.endswith('/albums.json'):
            return (401, {}, '') if locked else (200, {}, '["00001"]')
        if path == '/health':
            return 200, {}, json.dumps({'revision': 'c0ffee1'})
        return 404, {}, ''
    return fetch


def _tree(root):
    out = {}
    for base, _dirs, files in os.walk(root):
        for f in files:
            p = os.path.join(base, f)
            out[os.path.relpath(p, root)] = open(p, 'rb').read()
    return out


def _finished(catalog, run):
    conn = sqlite3.connect(catalog['db'])
    conn.row_factory = sqlite3.Row
    row = publish.get_run(conn, run.id)
    conn.close()
    return row


def test_progress_lines_are_read():
    m = publish._PROGRESS2.match('    1,234,567  45%   10.20MB/s    0:00:10 (xfr#3, to-chk=10/20)')
    assert m and m.group(1) == '1,234,567' and m.group(2) == '45'


def test_a_publish_sends_the_change_keeps_env_and_checks_the_site(catalog, tmp_path):
    home = str(tmp_path / 'home')
    dest = _populate(catalog, home)
    section_path = os.path.join(catalog['album'], 'section-1.json')
    section = json.load(open(section_path))
    section['photos'] = section['photos'][:1]
    json.dump(section, open(section_path, 'w'))
    os.remove(os.path.join(catalog['album'], 'b_alt.jpg'))

    run = _run(catalog)
    publish.execute(run, 'publish', _ctx(catalog, locked=True, fetch=_site()), _server(),
                    publish.LocalRemote(_server(), home))
    row = _finished(catalog, run)
    assert row['status'] == 'succeeded', row['error']
    r = row['result']
    assert r['applied'] and r['plan']['deleted'] == 1
    assert all(c['ok'] for c in r['checks']) and r['server_revision'] == 'c0ffee1'
    stage = deployment_pack.stage_dir_for(catalog['target'], 'album2026')
    served = _tree(dest)
    assert served.pop('.env') == b'ALBUM_SESSION_SECRET=keep-me\n'
    assert served == _tree(stage)                    # the server is the stage, exactly
    for phase in ('Sending media', 'Sending album data', 'Removing old files', 'Checking the site'):
        assert phase in row['log']

    again = _run(catalog)
    publish.execute(again, 'publish', _ctx(catalog, locked=True, fetch=_site()), _server(),
                    publish.LocalRemote(_server(), home))
    second = _finished(catalog, again)
    assert second['status'] == 'succeeded' and second['result']['up_to_date']
    assert second['result']['applied'] is False


def test_mass_deletions_wait_for_a_confirmation(catalog, tmp_path, monkeypatch):
    monkeypatch.setattr(publish, 'MASS_DELETE_SHARE', 0.01)
    home = str(tmp_path / 'home')
    dest = _populate(catalog, home)
    os.remove(os.path.join(catalog['album'], 'b_alt.jpg'))
    section_path = os.path.join(catalog['album'], 'section-1.json')
    section = json.load(open(section_path))
    section['photos'] = section['photos'][:1]
    json.dump(section, open(section_path, 'w'))
    gone = os.path.join(dest, 'catalogs/family/cache/Deployments/album2026/00001/b_alt.jpg')

    run = _run(catalog)
    publish.execute(run, 'publish', _ctx(catalog, fetch=_site()), _server(),
                    publish.LocalRemote(_server(), home))
    row = _finished(catalog, run)
    assert row['status'] == 'failed' and 'confirm' in row['error']
    assert row['result']['needs_confirmation'] and os.path.exists(gone)   # nothing sent

    run = _run(catalog)
    publish.execute(run, 'publish', _ctx(catalog, locked=True, fetch=_site(),
                                         confirm_deletions=True),
                    _server(), publish.LocalRemote(_server(), home))
    assert _finished(catalog, run)['status'] == 'succeeded' and not os.path.exists(gone)


def test_an_empty_server_is_not_published_to(catalog, tmp_path):
    run = _run(catalog)
    publish.execute(run, 'publish', _ctx(catalog, fetch=_site()), _server(),
                    publish.LocalRemote(_server(), str(tmp_path / 'home')))
    row = _finished(catalog, run)
    assert row['status'] == 'failed' and 'Set it up' in row['error']


def test_a_failed_site_check_says_the_data_was_sent(catalog, tmp_path):
    home = str(tmp_path / 'home')
    _populate(catalog, home)
    os.remove(os.path.join(catalog['album'], 'b_alt.jpg'))
    section_path = os.path.join(catalog['album'], 'section-1.json')
    section = json.load(open(section_path))
    section['photos'] = section['photos'][:1]
    json.dump(section, open(section_path, 'w'))
    run = _run(catalog)
    publish.execute(run, 'publish', _ctx(catalog, locked=True,
                                         fetch=_site(broken='deployment.json')),
                    _server(), publish.LocalRemote(_server(), home))
    row = _finished(catalog, run)
    assert row['status'] == 'failed'
    assert row['error'].startswith('Sent, but the site check failed: The gallery answers')
    assert row['result']['applied']


def test_a_changed_stack_restarts_and_says_when_it_could_not(catalog, tmp_path):
    home = str(tmp_path / 'home')
    _populate(catalog, home)                   # with localhost/album-gallery:amd64
    server = _server(gallery_image='registry.example.com/album-gallery:2')
    run = _run(catalog)
    publish.execute(run, 'publish', _ctx(catalog, locked=True, fetch=_site()), server,
                    publish.LocalRemote(server, home))
    row = _finished(catalog, run)
    assert row['status'] == 'succeeded', row['error']
    assert 'Restarting the gallery' in row['log']
    assert any('docker compose up -d' in w for w in row['result']['warnings'])   # no docker here
    compose = open(os.path.join(home, 'galleries/album2026/compose.yml')).read()
    assert 'registry.example.com/album-gallery:2' in compose


def test_publish_through_the_route(client, app_module, tmp_path, monkeypatch, catalog):
    sid = client.post(BASE, json=FORM).json['id']
    home = str(tmp_path / 'home')
    _populate(catalog, home)
    os.remove(os.path.join(catalog['album'], 'b_alt.jpg'))
    section_path = os.path.join(catalog['album'], 'section-1.json')
    section = json.load(open(section_path))
    section['photos'] = section['photos'][:1]
    json.dump(section, open(section_path, 'w'))
    monkeypatch.setattr(app_module, '_publish_remote',
                        lambda server, run_id: publish.LocalRemote(server, home))
    monkeypatch.setattr(app_module, '_publish_fetch', _site(locked=False))
    run_id = client.post(f'{BASE}/{sid}/publish', json={}).json['run_id']
    _wait_runs()
    run = client.get(f'/api/catalogs/1/deployments/1/publish-runs/{run_id}').json
    assert run['status'] == 'succeeded', run.get('error')
    assert run['files_deleted'] == 1 and run['result']['applied']
    assert not os.path.exists(os.path.join(
        home, 'galleries/album2026/catalogs/family/cache/Deployments/album2026/00001/b_alt.jpg'))


# ── phase 5: set up an empty server ──────────────────────────────────────────

FAKE_DOCKER = r'''#!/bin/sh
# A stand-in for docker on the "server": answers from files beside it, records calls.
here=$(dirname "$0")
echo "$*" >> "$here/calls.log"
case "$1" in
  info)    [ -f "$here/denied" ] && exit 1; exit 0 ;;
  compose) [ "$2" = "up" ] && [ -f "$here/compose_fails" ] && { echo "no such image" >&2; exit 1; }
           exit 0 ;;
  ps)      [ -f "$here/ps.txt" ] && cat "$here/ps.txt"; exit 0 ;;
  image)   grep -qx "$3" "$here/images.txt" 2>/dev/null; exit $? ;;
  volume|run) exit 0 ;;
esac
exit 0
'''


@pytest.fixture
def docker(tmp_path):
    d = tmp_path / 'fakebin'
    d.mkdir()
    (d / 'docker').write_text(FAKE_DOCKER)
    (d / 'docker').chmod(0o755)
    (d / 'images.txt').write_text('localhost/album-gallery:amd64\n')
    return d


def _setup(catalog, tmp_path, docker, **ctx):
    run = _run(catalog)
    home = str(tmp_path / 'home')
    remote = publish.LocalRemote(_server(), home, path_prefix=str(docker))
    publish.execute(run, 'setup', _ctx(catalog, locked=True, fetch=_site(), verify_wait=0, **ctx),
                    _server(), remote)
    return _finished(catalog, run), home


def test_setup_makes_a_running_gallery_from_nothing(catalog, tmp_path, docker):
    row, home = _setup(catalog, tmp_path, docker)
    assert row['status'] == 'succeeded', row['error']
    dest = os.path.join(home, 'galleries', 'album2026')
    env = open(os.path.join(dest, '.env')).read()
    assert env.startswith('ALBUM_SESSION_SECRET=') and len(env.strip()) > 40
    assert oct(os.stat(os.path.join(dest, '.env')).st_mode & 0o777) == '0o600'
    served = _tree(dest)
    served.pop('.env')
    assert served == _tree(deployment_pack.stage_dir_for(catalog['target'], 'album2026'))
    calls = open(docker / 'calls.log').read()
    assert 'compose up -d' in calls and all(c['ok'] for c in row['result']['checks'])
    for phase in ('Writing the session secret', 'Starting the gallery', 'Checking the site'):
        assert phase in row['log']


def test_setup_refuses_a_server_that_already_has_a_gallery(catalog, tmp_path, docker):
    _populate(catalog, str(tmp_path / 'home'))
    row, _ = _setup(catalog, tmp_path, docker)
    assert row['status'] == 'failed' and 'Use Publish' in row['error']


@pytest.mark.parametrize('prepare, says', [
    (lambda d: (d / 'denied').write_text(''), 'cannot use Docker'),
    (lambda d: (d / 'ps.txt').write_text('test-caddy-1 0.0.0.0:443->443/tcp\n'), 'test-caddy-1'),
    (lambda d: (d / 'images.txt').write_text(''), 'not on the server'),
])
def test_setup_stops_before_sending_when_the_server_is_not_ready(catalog, tmp_path, docker,
                                                                prepare, says):
    prepare(docker)
    row, home = _setup(catalog, tmp_path, docker)
    assert row['status'] == 'failed' and says in row['error'], row['error']
    assert not os.path.exists(os.path.join(home, 'galleries', 'album2026', 'compose.yml'))


def test_setup_keeps_an_existing_secret_and_carries_certificates(catalog, tmp_path, docker):
    dest = tmp_path / 'home' / 'galleries' / 'album2026'
    dest.mkdir(parents=True)
    (dest / '.env').write_text('ALBUM_SESSION_SECRET=already\n')    # a folder with only .env
    row, _ = _setup(catalog, tmp_path, docker, carry_certificates_from='second-catalog_caddy_data')
    assert row['status'] == 'succeeded', row['error']
    assert (dest / '.env').read_text() == 'ALBUM_SESSION_SECRET=already\n'
    calls = open(docker / 'calls.log').read()
    assert 'volume create album2026_caddy_data' in calls
    assert 'run --rm -v second-catalog_caddy_data:/from:ro -v album2026_caddy_data:/to caddy:2' in calls


def test_setup_says_when_the_gallery_would_not_start(catalog, tmp_path, docker):
    (docker / 'compose_fails').write_text('')
    row, home = _setup(catalog, tmp_path, docker)
    assert row['status'] == 'failed' and 'docker compose up -d` failed' in row['error']
    assert os.path.exists(os.path.join(home, 'galleries', 'album2026', 'compose.yml'))


def test_setup_waits_for_the_certificate_then_reports(catalog, tmp_path, docker):
    attempts = []

    def slow_site(url):
        attempts.append(url)
        if len(attempts) <= 4:                  # the first round fails, as before a certificate
            raise OSError('certificate not ready')
        return _site()(url)
    run = _run(catalog)
    remote = publish.LocalRemote(_server(), str(tmp_path / 'home'), path_prefix=str(docker))
    publish.execute(run, 'setup', _ctx(catalog, locked=True, fetch=slow_site, verify_wait=0),
                    _server(), remote)
    row = _finished(catalog, run)
    assert row['status'] == 'succeeded', row['error']
    assert 'site not ready yet (try 1 of 6)' in row['log']


def test_a_bad_certificate_volume_name_is_refused(catalog, tmp_path, docker):
    row, _ = _setup(catalog, tmp_path, docker, carry_certificates_from='x; rm -rf ~')
    assert row['status'] == 'failed' and 'Docker volume name' in row['error']
