"""
Accelerated services (documentation/specs/accelerated-services-spec.md):
the shared video module, backend-transcode, the import's service branch and
its local fallback, and how each machine picks a service's mode.

    python -m pytest test_accelerated_services.py

Needs ffmpeg/ffprobe on PATH for the video tests; they are skipped without.
"""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, os.path.join(HERE, 'workflows'))

from lib import video_proxy as vp  # noqa: E402

needs_ffmpeg = pytest.mark.skipif(not vp.available(), reason='ffmpeg/ffprobe not installed')


def _clip(path, codec='libx264', size='640x360', pix_fmt='yuv420p', faststart=True,
          seconds=2, audio=True):
    """A short synthetic video with the properties a test needs."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
           f'testsrc=size={size}:rate=25:duration={seconds}']
    if audio:
        cmd += ['-f', 'lavfi', '-i', f'sine=duration={seconds}', '-c:a', 'aac']
    cmd += ['-c:v', codec, '-pix_fmt', pix_fmt]
    if faststart:
        cmd += ['-movflags', '+faststart']
    subprocess.run(cmd + [path], check=True)
    return path


def _stream(path):
    out = subprocess.run(['ffprobe', '-v', 'error', '-select_streams', 'v:0', '-show_entries',
                          'stream=codec_name,profile,pix_fmt,width,height', '-of', 'json', path],
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)['streams'][0]


# ─── The shared module ───────────────────────────────────────────────────────

@needs_ffmpeg
def test_the_ladder_passthrough_remux_transcode(tmp_path):
    ok = _clip(str(tmp_path / 'ok.mp4'))
    late_index = _clip(str(tmp_path / 'late.mp4'), faststart=False)
    big = _clip(str(tmp_path / 'big.mp4'), size='1920x1080')
    assert vp.plan(vp.probe(ok), ok) == 'passthrough'
    assert vp.plan(vp.probe(late_index), late_index) == 'remux'
    assert vp.plan(vp.probe(big), big) == 'transcode'


@needs_ffmpeg
def test_a_10_bit_source_becomes_an_8_bit_preview(tmp_path):
    # iPhone HDR is 10-bit; a High 10 proxy does not play in Firefox or Chrome.
    src = _clip(str(tmp_path / 'hdr.mp4'), size='1920x1080', pix_fmt='yuv420p10le')
    assert _stream(src)['pix_fmt'] == 'yuv420p10le'
    out = str(tmp_path / 'proxy.mp4')
    r = vp.make_proxy(src, out, str(tmp_path / 'proxy.partial.mp4'))
    assert r['action'] == 'transcode' and r['encoder'] == 'libx264'
    s = _stream(out)
    assert (s['pix_fmt'], s['profile'], s['width'], s['height']) == ('yuv420p', 'High', 1280, 720)


@needs_ffmpeg
def test_a_failing_hardware_encoder_falls_back_to_software(tmp_path):
    if 'h264_nvenc' in vp.usable_encoders(('h264_nvenc',)):
        pytest.skip('this machine has a working NVENC')
    src = _clip(str(tmp_path / 'big.mp4'), size='1920x1080')
    out = str(tmp_path / 'proxy.mp4')
    r = vp.make_proxy(src, out, str(tmp_path / 'proxy.partial.mp4'),
                      encoders=('h264_nvenc', 'not_an_encoder'))
    assert r['tried'] == ['h264_nvenc', 'libx264'] and r['encoder'] == 'libx264'
    assert os.path.exists(out) and not os.path.exists(str(tmp_path / 'proxy.partial.mp4'))


# ─── backend-transcode ───────────────────────────────────────────────────────

def _load_service(catalogs_root, monkeypatch):
    monkeypatch.setenv('CATALOGS_ROOT', str(catalogs_root))
    monkeypatch.setenv('TRANSCODE_ENCODER', 'libx264')     # deterministic in tests
    path = os.path.join(REPO, 'systems', 'backend-transcode', 'app.py')
    spec = importlib.util.spec_from_file_location('transcode_app', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def catalogs(tmp_path):
    root = tmp_path / 'catalogs'
    (root / 'fam' / 'Videos').mkdir(parents=True)
    (root / 'fam' / 'cache').mkdir()
    (root / 'other').mkdir()
    return root


@needs_ffmpeg
def test_service_makes_a_proxy_and_a_poster(catalogs, monkeypatch):
    _clip(str(catalogs / 'fam' / 'Videos' / 'a.mov'), size='1920x1080')
    app = _load_service(catalogs, monkeypatch).app.test_client()
    r = app.post('/transcode', json={'catalog': 'fam', 'source': 'Videos/a.mov',
                                     'proxy': 'cache/Videos/2025/a_proxy.mp4'})
    assert r.status_code == 200, r.json
    assert r.json['action'] == 'transcode' and r.json['width'] == 1280
    assert (catalogs / 'fam' / 'cache' / 'Videos' / '2025' / 'a_proxy.mp4').exists()
    r = app.post('/poster', json={'catalog': 'fam', 'source': 'Videos/a.mov',
                                  'poster': 'cache/Videos/2025/a_poster.jpg'})
    assert r.status_code == 200 and r.json['width'] == 320
    h = app.get('/health').json
    assert h['stats']['transcodes'] == 1 and h['stats']['posters'] == 1


@needs_ffmpeg
@pytest.mark.parametrize('body, why', [
    ({'catalog': '../x', 'source': 'Videos/a.mov', 'proxy': 'cache/p.mp4'}, 'catalog'),
    ({'catalog': '/etc', 'source': 'Videos/a.mov', 'proxy': 'cache/p.mp4'}, 'catalog'),
    ({'catalog': 'fam', 'source': '/etc/passwd', 'proxy': 'cache/p.mp4'}, 'relative'),
    ({'catalog': 'fam', 'source': '../other/x.mov', 'proxy': 'cache/p.mp4'}, 'relative'),
    ({'catalog': 'fam', 'source': 'Videos/a.mov', 'proxy': 'Videos/p.mp4'}, 'cache/'),
    ({'catalog': 'fam', 'source': 'Videos/missing.mov', 'proxy': 'cache/p.mp4'}, 'not found'),
    ({'catalog': 'nope', 'source': 'Videos/a.mov', 'proxy': 'cache/p.mp4'}, 'no catalog'),
])
def test_service_refuses_paths_outside_a_catalog(catalogs, monkeypatch, body, why):
    _clip(str(catalogs / 'fam' / 'Videos' / 'a.mov'))
    app = _load_service(catalogs, monkeypatch).app.test_client()
    r = app.post('/transcode', json=body)
    assert r.status_code == 400 and why in r.json['error']


@needs_ffmpeg
def test_service_refuses_a_symlink_out_of_the_catalog(catalogs, monkeypatch, tmp_path):
    outside = _clip(str(tmp_path / 'outside.mov'))
    os.symlink(outside, catalogs / 'fam' / 'Videos' / 'link.mov')
    app = _load_service(catalogs, monkeypatch).app.test_client()
    r = app.post('/transcode', json={'catalog': 'fam', 'source': 'Videos/link.mov',
                                     'proxy': 'cache/p.mp4'})
    assert r.status_code == 400 and 'outside' in r.json['error']


# ─── The import: service first, local fallback ───────────────────────────────

@needs_ffmpeg
def test_the_import_uses_the_service_and_falls_back_without_it(catalogs, monkeypatch):
    from datetime import datetime
    from werkzeug.serving import make_server
    from lib import utilities as u

    service = _load_service(catalogs, monkeypatch)
    server = make_server('127.0.0.1', 0, service.app, threaded=True)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        src = _clip(str(catalogs / 'fam' / 'Videos' / 'b.mov'), size='1920x1080')
        monkeypatch.setenv('TRANSCODE_URL', f'http://127.0.0.1:{server.server_port}')
        monkeypatch.setattr(u, '_CATALOGS_MOUNT', str(catalogs))
        monkeypatch.setattr(u, '_TRANSCODE_DOWN_UNTIL', 0)
        r = u.create_video_proxy(src, str(catalogs / 'fam'), creation_date=datetime(2025, 1, 2),
                                 name_stem='b')
        assert r['action'] == 'transcode' and os.path.exists(r['path'])
        assert service._stats['transcodes'] == 1            # made BY the service
        poster = u.create_video_poster(src, str(catalogs / 'fam'),
                                       creation_date=datetime(2025, 1, 2), name_stem='b')
        assert poster and service._stats['posters'] == 1
    finally:
        server.shutdown()

    # Service gone: made here, the same way.
    os.remove(r['path'])
    r2 = u.create_video_proxy(src, str(catalogs / 'fam'), creation_date=datetime(2025, 1, 2),
                              name_stem='b')
    assert r2['action'] == 'transcode' and r2['encoder'] == 'libx264'
    assert os.path.exists(r2['path'])
    assert service._stats['transcodes'] == 1                # not by the service


@needs_ffmpeg
def test_a_catalog_outside_the_mount_never_goes_to_the_service(tmp_path, monkeypatch):
    from datetime import datetime
    from lib import utilities as u
    src = _clip(str(tmp_path / 'cat' / 'Videos' / 'c.mov'), size='1920x1080')
    monkeypatch.setenv('TRANSCODE_URL', 'http://127.0.0.1:9')   # would refuse
    monkeypatch.setattr(u, '_CATALOGS_MOUNT', '/no/such/mount')
    r = u.create_video_proxy(src, str(tmp_path / 'cat'), creation_date=datetime(2025, 1, 2),
                             name_stem='c')
    assert r['action'] == 'transcode'


# ─── Modes ───────────────────────────────────────────────────────────────────

def _mode(service, env=None, uname='Darwin', arch='arm64', nvidia_smi=None,
          ffmpeg=True, tmp_path=None):
    """
    airlock_service_mode on a FAKED platform.

    The platform is set through AIRLOCK_OS / AIRLOCK_ARCH rather than by faking
    `uname` on PATH: those two variables are what every decision in
    airlock-env.sh reads, and a fake `uname` that ignores its arguments cannot
    answer -s and -m differently anyway.

    `ffmpeg` writes a stub ffmpeg/ffprobe pair, because host mode for transcode
    is conditional on them being there (spec 3.1.1); AIRLOCK_PYTHON is pinned
    to this interpreter so the other host-mode condition is deterministic
    wherever the suite runs.
    """
    fake = tmp_path / 'bin'
    fake.mkdir(exist_ok=True)
    if nvidia_smi:
        (fake / 'nvidia-smi').write_text(f'#!/bin/sh\necho "{nvidia_smi}"\n')
        (fake / 'nvidia-smi').chmod(0o755)
    if ffmpeg:
        for tool in ('ffmpeg', 'ffprobe'):
            (fake / tool).write_text('#!/bin/sh\necho "ffmpeg version 7.0-stub"\n')
            (fake / tool).chmod(0o755)
    e = {k: v for k, v in os.environ.items() if not k.startswith('AIRLOCK_')}
    e.update({'AIRLOCK_OS': uname, 'AIRLOCK_ARCH': arch,
              'AIRLOCK_PYTHON': sys.executable})
    e.update(env or {})
    e['PATH'] = f'{fake}:{e["PATH"]}'
    out = subprocess.run(
        ['bash', '-c', f'source "{REPO}/deploy/airlock-env.sh" >/dev/null 2>&1; '
                       f'airlock_service_mode {service}'],
        capture_output=True, text=True, env=e)
    return out.stdout.strip()


def test_mode_auto_on_apple_silicon_is_native_for_both(tmp_path):
    """Metal and VideoToolbox are reachable only outside the podman VM."""
    assert _mode('DEEPFACE', tmp_path=tmp_path).startswith('host macOS on Apple Silicon')
    assert _mode('TRANSCODE', tmp_path=tmp_path).startswith('host macOS on Apple Silicon')


def test_mode_auto_on_an_intel_mac_splits_the_two_services(tmp_path):
    """
    The Intel Mac is the case that proves auto is per-service, not per-OS.

    VideoToolbox exists there, so transcode still goes native. TensorFlow has
    published no macOS x86_64 wheel since 2.16 while deepface needs >= 2.20, so
    native deepface cannot even be INSTALLED - the container, which is
    linux/amd64, is the only way to run it (spec 3.1.1).
    """
    deepface = _mode('DEEPFACE', uname='Darwin', arch='x86_64', tmp_path=tmp_path)
    assert deepface.startswith('container')
    assert 'wheels' in deepface
    assert _mode('TRANSCODE', uname='Darwin', arch='x86_64',
                 tmp_path=tmp_path).startswith('host macOS on Intel')


@pytest.mark.skipif(shutil.which('ffmpeg') is not None,
                    reason='ffmpeg is installed here, so its absence cannot be faked')
def test_mode_host_without_ffmpeg_falls_back_to_container(tmp_path):
    """
    The failure this whole arrangement exists to prevent: host mode chosen on a
    machine with no ffmpeg, starting a service that cannot run. A container
    carries its own, so the fallback costs the accelerator and not the service.
    """
    out = _mode('TRANSCODE', ffmpeg=False, tmp_path=tmp_path)
    assert out.startswith('container')
    assert 'ffmpeg' in out


@pytest.mark.skipif(shutil.which('ffmpeg') is not None,
                    reason='ffmpeg is installed here, so its absence cannot be faked')
def test_mode_host_asked_for_explicitly_still_checks_the_host(tmp_path):
    out = _mode('TRANSCODE', {'AIRLOCK_TRANSCODE_MODE': 'host'},
                ffmpeg=False, tmp_path=tmp_path)
    assert out.startswith('container AIRLOCK_TRANSCODE_MODE=host, but ')
    assert 'ffmpeg' in out


def test_mode_deepface_is_unsupported_natively_on_32_bit_arm(tmp_path):
    out = _mode('DEEPFACE', {'AIRLOCK_DEEPFACE_MODE': 'host'},
                uname='Linux', arch='arm32', tmp_path=tmp_path)
    assert out.startswith('container')
    assert 'TensorFlow' in out


def test_mode_auto_without_a_gpu_on_linux_is_a_plain_container(tmp_path):
    assert _mode('TRANSCODE', uname='Linux', arch='x86_64', tmp_path=tmp_path) \
        == 'container no usable GPU (nvidia-smi not found)'


def test_mode_gpu_that_cannot_be_done_falls_back_and_says_why(tmp_path):
    out = _mode('DEEPFACE', {'AIRLOCK_DEEPFACE_MODE': 'gpu'}, uname='Linux',
                arch='x86_64', nvidia_smi='NVIDIA GeForce RTX 3060',
                tmp_path=tmp_path)
    # No CDI spec / old podman on this machine: never a half-working GPU mode.
    assert out.startswith('container AIRLOCK_DEEPFACE_MODE=gpu, but ')


# `host` is deliberately NOT in this list: it is conditional on the host being
# able to run a native process, which the three tests above cover.
@pytest.mark.parametrize('value, expected', [
    ('off', 'off'), ('container', 'container'), ('bogus', 'container')])
def test_explicit_modes(tmp_path, value, expected):
    assert _mode('TRANSCODE', {'AIRLOCK_TRANSCODE_MODE': value}, tmp_path=tmp_path) \
        .split()[0] == expected
