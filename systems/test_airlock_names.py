#!/usr/bin/env python3
"""
The rename to airlock is finished: nothing reads the old names any more.

    python -m pytest systems/test_airlock_names.py

Settings were SHOEBOX_* until v5.6.3 and are AIRLOCK_* now
(documentation/specs/name-migration-spec.md). The fallbacks that kept the old
names working - env_compat.py, deploy/shoebox.env, SHOEBOX_* exported by hand -
were removed in v6.4.5. These tests keep it that way: no code uses the old
prefix, and the start scripts ignore an old env file or variable.
"""

import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(HERE, 'backend'))

# The only file allowed to say SHOEBOX_: this test, which names it to forbid it.
ALLOWED = {
    'systems/test_airlock_names.py',
}


def test_no_code_uses_the_old_prefix():
    files = subprocess.run(
        ['git', 'ls-files', '-z', '--', '.', ':(exclude)documentation',
         ':(exclude)CHANGELOG.md'],
        cwd=REPO, capture_output=True, text=True, check=True).stdout.split('\0')
    offenders = []
    for f in filter(None, files):
        if f in ALLOWED:
            continue
        try:
            text = open(os.path.join(REPO, f), encoding='utf-8').read()
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError):
            continue
        for n, line in enumerate(text.splitlines(), 1):
            if 'SHOEBOX_' in line:
                offenders.append(f'{f}:{n}: {line.strip()}')
    assert not offenders, '\n'.join(offenders)


def _source(env_files, extra_env=None, show='AIRLOCK_HOST_IP AIRLOCK_GPU'):
    """Source airlock-env.sh in a copy of the deploy dir with these env files."""
    import shutil
    import tempfile
    root = tempfile.mkdtemp()
    os.makedirs(os.path.join(root, 'deploy'))
    os.makedirs(os.path.join(root, 'tools'))
    shutil.copy(os.path.join(REPO, 'deploy', 'airlock-env.sh'), os.path.join(root, 'deploy'))
    for name, body in env_files.items():
        with open(os.path.join(root, 'deploy', name), 'w') as fh:
            fh.write(body)
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(('SHOEBOX_', 'AIRLOCK_'))}
    env.update(extra_env or {})
    script = (f'source "{root}/deploy/airlock-env.sh" >/dev/null 2>&1; '
              + '; '.join(f'echo "{v}=${{{v}-unset}}"' for v in show.split()))
    out = subprocess.run(['bash', '-c', script], env=env, capture_output=True,
                         text=True, timeout=60).stdout
    return dict(line.split('=', 1) for line in out.split())


def test_an_old_env_file_is_not_read():
    got = _source({'shoebox.env': 'SHOEBOX_HOST_IP=10.9.8.7\nSHOEBOX_GPU=1\n'})
    assert got['AIRLOCK_HOST_IP'] != '10.9.8.7'
    assert got['AIRLOCK_GPU'] == 'unset'


def test_an_old_name_exported_by_hand_is_not_read():
    got = _source({}, extra_env={'SHOEBOX_HOST_IP': '10.2.2.2'})
    assert got['AIRLOCK_HOST_IP'] != '10.2.2.2'


def test_the_env_file_is_read():
    got = _source({'airlock.env': 'AIRLOCK_HOST_IP=192.168.1.18\nAIRLOCK_GPU=1\n'})
    assert got == {'AIRLOCK_HOST_IP': '192.168.1.18', 'AIRLOCK_GPU': '1'}
