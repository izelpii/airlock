#!/usr/bin/env python3
"""
The gallery keeps its session secret, so a restart signs nobody out.

    python -m pytest systems/album-gallery/test_session_secret.py

Before, ALBUM_SESSION_SECRET unset (every installation that had not set it by
hand) meant a key per process: each restart of the gallery - every deploy,
every reboot - signed every visitor out of every password-protected deployment.
Now the key is generated once into ALBUM_STATE_DIR and reused; the variable
still wins when set.
"""

import json
import os
import shutil
import stat
import subprocess
import tempfile
import time
import urllib.request

import pytest

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.join(_HERE, 'server.mjs')
_PORT = int(os.environ.get('GALLERY_SECRET_TEST_PORT', '3188'))

pytestmark = pytest.mark.skipif(shutil.which('node') is None, reason='node is not installed')


def _health(env):
    proc = subprocess.Popen(['node', _SERVER], env=dict(os.environ, PORT=str(_PORT), **env),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(100):
            try:
                with urllib.request.urlopen(f'http://127.0.0.1:{_PORT}/health', timeout=2) as r:
                    return json.loads(r.read())
            except Exception:
                time.sleep(0.05)
        raise RuntimeError('the gallery did not start')
    finally:
        proc.terminate()
        proc.wait(timeout=10)


@pytest.fixture
def state():
    path = tempfile.mkdtemp(prefix='gallery-state-')
    yield path
    shutil.rmtree(path, ignore_errors=True)


def test_the_secret_is_generated_once_and_kept(state):
    env = {'ALBUM_STATE_DIR': state, 'ALBUM_SESSION_SECRET': ''}
    assert _health(env)['session_secret'] == 'kept'
    path = os.path.join(state, 'session-secret')
    first = open(path).read()
    assert len(first.strip()) >= 32
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    assert _health(env)['session_secret'] == 'kept'
    assert open(path).read() == first, 'a restart must reuse the kept secret'


def test_the_variable_wins_and_is_never_written(state):
    env = {'ALBUM_STATE_DIR': state, 'ALBUM_SESSION_SECRET': 'set-on-purpose-' + 'x' * 32}
    assert _health(env)['session_secret'] == 'environment'
    assert not os.path.exists(os.path.join(state, 'session-secret'))


def test_an_unusable_kept_file_is_replaced(state):
    path = os.path.join(state, 'session-secret')
    with open(path, 'w') as fh:
        fh.write('short\n')
    assert _health({'ALBUM_STATE_DIR': state, 'ALBUM_SESSION_SECRET': ''})['session_secret'] == 'kept'
    assert len(open(path).read().strip()) >= 32


def test_without_a_state_folder_it_says_so():
    assert _health({'ALBUM_SESSION_SECRET': '', 'ALBUM_STATE_DIR': ''})['session_secret'] == 'process'
