"""
The deployment packer: what a bundle ships, and the stage kept between
publishes (documentation/specs/deployment-publish-to-server-spec.md phase 1).

    python -m pytest test_deployment_pack.py
"""

import json
import os
import subprocess
import sys
import tarfile
import time

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import deployment_pack as dp  # noqa: E402

REPO = os.path.dirname(os.path.dirname(HERE))


def _file(path, data=b'x'):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(data)
    return path


@pytest.fixture
def catalogs(tmp_path):
    """
    A catalog with one published deployment, laid out as _deploy_album and
    _publish_deployment write it: a symlink farm into cache/Images.

      album 00001, one section in format 2 (section-1.json):
        photo a: 1200 + 2048 sources, a 320 thumbnail
        photo b: 1200 only
      packed_rungs: 1200 and thumb320 - so a's 2048 does not ship.
    """
    root = tmp_path / 'catalogs'
    cat = root / 'family'
    images = cat / 'cache' / 'Images' / '2026' / '10' / '01'
    files = {n: _file(str(images / n), n.encode() * 50) for n in
             ('a_alt.jpg', 'a_alt_2048.jpg', 'a_thumb_320.jpg', 'b_alt.jpg', 'stray.jpg')}
    dep = cat / 'cache' / 'Deployments' / 'album2026'
    album = dep / '00001'
    album.mkdir(parents=True)
    for n in ('a_alt.jpg', 'a_alt_2048.jpg', 'a_thumb_320.jpg', 'b_alt.jpg', 'stray.jpg'):
        os.symlink(files[n], album / n)
    (album / 'album.json').write_text(json.dumps({'sections': [{
        'id': 1, 'cover': {'alt_path': 'a_alt.jpg', 'thumb_path': 'a_thumb_320.jpg'},
        'photos_file': 'section-1.json'}]}))
    (album / 'section-1.json').write_text(json.dumps({'photos': [
        {'alt_path': 'a_alt.jpg',
         'sources': [{'rung': '1200', 'path': 'a_alt.jpg'},
                     {'rung': '2048', 'path': 'a_alt_2048.jpg'}],
         'thumbnails': [{'rung': 'thumb320', 'path': 'a_thumb_320.jpg'}]},
        {'alt_path': 'b_alt.jpg', 'sources': [{'rung': '1200', 'path': 'b_alt.jpg'}]},
    ]}))
    (dep / 'deployment.json').write_text(json.dumps({
        'name': 'Album 2026', 'requires_password': True,
        'packed_rungs': ['1200', 'thumb320'],
        'generated_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())}))
    (dep / 'albums.json').write_text(json.dumps(['00001']))
    (dep / '.auth.json').write_text('{"hash": "h"}')
    (root / 'catalogs.json').write_text(json.dumps({'family': 'family'}))
    return {'root': str(root), 'dep': str(dep), 'album': str(album),
            'images': str(images), 'files': files}


def _album_out(stage):
    return os.path.join(stage, 'catalogs', 'family', 'cache', 'Deployments',
                        'album2026', '00001')


# ── what ships ───────────────────────────────────────────────────────────────

def test_the_manifest_is_the_shipping_list(catalogs):
    info = dp.inspect(catalogs['root'], 'family', 'album2026')
    assert info['wanted'] == {'00001': {'a_alt.jpg', 'a_thumb_320.jpg', 'b_alt.jpg'}}
    assert info['locked'] and info['name'] == 'Album 2026'
    assert info['published'] == 'just now'


def test_refusals_say_what_to_do(catalogs, tmp_path):
    with pytest.raises(dp.PackError, match='not in'):
        dp.inspect(catalogs['root'], 'nope', 'album2026')
    with pytest.raises(dp.PackError, match='Found: album2026'):
        dp.inspect(catalogs['root'], 'family', 'other')

    os.remove(os.path.join(catalogs['album'], 'b_alt.jpg'))
    os.symlink('/nowhere/b.jpg', os.path.join(catalogs['album'], 'b_alt.jpg'))
    info = dp.inspect(catalogs['root'], 'family', 'album2026')
    with pytest.raises(dp.PackError, match='dangling'):
        dp.build_node(info, str(tmp_path / 'out'), 'img', 'photos.example.com')

    outside = _file(str(tmp_path / 'elsewhere.jpg'))
    os.remove(os.path.join(catalogs['album'], 'b_alt.jpg'))
    os.symlink(outside, os.path.join(catalogs['album'], 'b_alt.jpg'))
    with pytest.raises(dp.PackError, match='outside the catalog cache'):
        dp.build_node(info, str(tmp_path / 'out2'), 'img', 'photos.example.com')


def test_an_old_publish_is_refused_by_max_age(catalogs):
    meta_path = os.path.join(catalogs['dep'], 'deployment.json')
    meta = json.load(open(meta_path))
    meta['generated_at'] = '2026-01-01T00:00:00Z'
    json.dump(meta, open(meta_path, 'w'))
    info = dp.inspect(catalogs['root'], 'family', 'album2026')
    with pytest.raises(dp.PackError, match='--max-age'):
        dp.check_age(info, 60)
    dp.check_age(info, None)


def test_the_node_bundle_ships_what_is_named_and_prunes_the_rest(catalogs, tmp_path):
    info = dp.inspect(catalogs['root'], 'family', 'album2026')
    out = str(tmp_path / 'bundle')
    counts = dp.build_node(info, out, 'localhost/album-gallery:amd64', 'photos.example.com')
    album = _album_out(out)
    assert sorted(f for f in os.listdir(album) if f.endswith('.jpg')) == \
        ['a_alt.jpg', 'a_thumb_320.jpg', 'b_alt.jpg']
    assert counts['skipped'] == 2          # the 2048 rung and the stray file
    section = json.load(open(os.path.join(album, 'section-1.json')))
    assert [s['rung'] for s in section['photos'][0]['sources']] == ['1200']
    assert not os.path.islink(os.path.join(album, 'a_alt.jpg'))
    auth = os.path.join(os.path.dirname(album), '.auth.json')
    assert oct(os.stat(auth).st_mode & 0o777) == '0o600'
    assert 'localhost/album-gallery:amd64' in open(os.path.join(out, 'compose.yml')).read()
    assert 'photos.example.com {' in open(os.path.join(out, 'Caddyfile')).read()
    assert json.load(open(os.path.join(out, 'catalogs', 'catalogs.json'))) == {'family': 'family'}
    # The catalog's own manifest is untouched.
    original = json.load(open(os.path.join(catalogs['album'], 'section-1.json')))
    assert len(original['photos'][0]['sources']) == 2


# ── the stage kept between publishes ─────────────────────────────────────────

def _mtimes(stage):
    out = {}
    for root, _dirs, files in os.walk(stage):
        for f in files:
            p = os.path.join(root, f)
            out[os.path.relpath(p, stage)] = os.stat(p).st_mtime_ns
    return out


def test_the_stage_links_media_and_a_second_refresh_changes_nothing(catalogs, tmp_path):
    info = dp.inspect(catalogs['root'], 'family', 'album2026')
    stage = str(tmp_path / 'stage')
    first = dp.refresh_stage(info, stage, 'img', 'photos.example.com')
    assert first['linked'] and first['placed'] == 3 and first['kept'] == 0
    linked = os.path.join(_album_out(stage), 'a_alt.jpg')
    assert os.stat(linked).st_ino == os.stat(catalogs['files']['a_alt.jpg']).st_ino

    before = _mtimes(stage)
    time.sleep(0.02)
    second = dp.refresh_stage(dp.inspect(catalogs['root'], 'family', 'album2026'),
                              stage, 'img', 'photos.example.com')
    assert (second['placed'], second['kept'], second['removed']) == (0, 3, 0)
    assert _mtimes(stage) == before       # nothing for rsync to look at


def test_a_photo_leaving_the_album_leaves_the_stage(catalogs, tmp_path):
    stage = str(tmp_path / 'stage')
    dp.refresh_stage(dp.inspect(catalogs['root'], 'family', 'album2026'),
                     stage, 'img', 'photos.example.com')
    section_path = os.path.join(catalogs['album'], 'section-1.json')
    section = json.load(open(section_path))
    section['photos'] = section['photos'][:1]
    json.dump(section, open(section_path, 'w'))
    os.remove(os.path.join(catalogs['album'], 'b_alt.jpg'))

    counts = dp.refresh_stage(dp.inspect(catalogs['root'], 'family', 'album2026'),
                              stage, 'img', 'photos.example.com')
    assert counts['removed'] == 1
    assert not os.path.exists(os.path.join(_album_out(stage), 'b_alt.jpg'))
    assert len(json.load(open(os.path.join(_album_out(stage), 'section-1.json')))['photos']) == 1


def test_a_photo_edited_in_place_is_linked_again(catalogs, tmp_path):
    """The Basic Editor rewrites a derivative at its own path (os.replace): a
    new file, so the stage's link to the old one must be replaced."""
    stage = str(tmp_path / 'stage')
    dp.refresh_stage(dp.inspect(catalogs['root'], 'family', 'album2026'),
                     stage, 'img', 'photos.example.com')
    src = catalogs['files']['a_alt.jpg']
    tmp = src + '.new'
    _file(tmp, b'turned' * 40)
    os.replace(tmp, src)

    counts = dp.refresh_stage(dp.inspect(catalogs['root'], 'family', 'album2026'),
                              stage, 'img', 'photos.example.com')
    assert counts['placed'] == 1
    staged = os.path.join(_album_out(stage), 'a_alt.jpg')
    assert open(staged, 'rb').read() == b'turned' * 40


def test_where_links_are_impossible_it_copies_and_still_skips_unchanged(
        catalogs, tmp_path, monkeypatch):
    real_link = os.link

    def no_links(*a, **k):
        raise OSError(18, 'Invalid cross-device link')
    monkeypatch.setattr(os, 'link', no_links)
    stage = str(tmp_path / 'stage')
    first = dp.refresh_stage(dp.inspect(catalogs['root'], 'family', 'album2026'),
                             stage, 'img', 'photos.example.com')
    assert not first['linked'] and first['placed'] == 3
    second = dp.refresh_stage(dp.inspect(catalogs['root'], 'family', 'album2026'),
                              stage, 'img', 'photos.example.com')
    assert (second['placed'], second['kept']) == (0, 3)

    monkeypatch.setattr(dp.shutil, 'disk_usage',
                        lambda p: type('U', (), {'free': 10})())
    os.replace(_file(catalogs['files']['b_alt.jpg'] + '.n', b'bigger' * 100),
               catalogs['files']['b_alt.jpg'])
    with pytest.raises(dp.PackError, match='needs'):
        dp.refresh_stage(dp.inspect(catalogs['root'], 'family', 'album2026'),
                         stage, 'img', 'photos.example.com')
    monkeypatch.setattr(os, 'link', real_link)


def test_the_stage_lives_on_the_catalogs_storage():
    assert dp.stage_dir_for('/catalogs/family', 'album2026') == \
        '/catalogs/family/cache/Publish/album2026/stage'


# ── the command line ─────────────────────────────────────────────────────────

def _cli(*args):
    return subprocess.run([sys.executable, os.path.join(REPO, 'tools', 'pack_deployment.py'),
                           *args], capture_output=True, text=True)


def test_the_command_line_still_makes_a_tarball(catalogs, tmp_path):
    r = _cli('--catalogs-root', catalogs['root'], '--catalog', 'family',
             '--deployment', 'album2026', '--domain', 'photos.example.com',
             '--image', 'img', '--out', str(tmp_path / 'dist'))
    assert r.returncode == 0, r.stderr
    tarball = [f for f in os.listdir(tmp_path / 'dist') if f.endswith('.tar.gz')][0]
    with tarfile.open(tmp_path / 'dist' / tarball) as t:
        names = t.getnames()
        auth = t.getmember('album2026/catalogs/family/cache/Deployments/album2026/.auth.json')
    assert 'album2026/compose.yml' in names
    assert 'album2026/catalogs/family/cache/Deployments/album2026/00001/b_alt.jpg' in names
    assert not any(n.endswith('a_alt_2048.jpg') for n in names)
    assert oct(auth.mode) == '0o600'
    assert 'dereferenced: 8 files' in r.stdout   # 3 photos, 5 manifests


def test_the_command_line_stage_refreshes(catalogs, tmp_path):
    args = ('--catalogs-root', catalogs['root'], '--catalog', 'family',
            '--deployment', 'album2026', '--domain', 'photos.example.com',
            '--image', 'img', '--stage', str(tmp_path / 'stage'), '--out', str(tmp_path / 'dist'))
    assert _cli(*args).returncode == 0
    r = _cli(*args)
    assert r.returncode == 0, r.stderr
    assert '0 new or changed, 3 unchanged, 0 removed' in r.stdout
    assert not os.listdir(tmp_path / 'dist')            # no tarball when staging
    r = _cli('--catalogs-root', catalogs['root'], '--catalog', 'nope', '--deployment', 'x')
    assert r.returncode == 1 and 'error: catalog "nope"' in r.stderr
