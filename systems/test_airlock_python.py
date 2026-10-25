#!/usr/bin/env python3
"""
Which Python the native services are built with (deploy/airlock-env.sh).

    python -m pytest systems/test_airlock_python.py

The interpreter must be within [AIRLOCK_PYTHON_MIN, AIRLOCK_PYTHON_MAX]: too old
and current Flask publishes nothing for it; too new and TensorFlow (deepface)
has no wheel yet - on a new Mac with Homebrew's 3.14, pip stopped with
ResolutionImpossible. 3.12, which the containers use, is preferred. With none
in range, `auto` mode runs the service in a container instead.
"""

import os
import stat
import subprocess

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)


def _fake(folder, version, *names):
    os.makedirs(folder, exist_ok=True)
    for name in names:
        path = os.path.join(folder, name)
        with open(path, 'w') as fh:
            fh.write(f'#!/bin/sh\necho {version}\n')
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)


def _run(path_dir, script):
    env = {'PATH': f'{path_dir}:/usr/bin:/bin', 'HOME': os.environ.get('HOME', '/tmp')}
    out = subprocess.run(
        ['bash', '-c', f'source "{REPO}/deploy/airlock-env.sh" >/dev/null 2>&1; {script}'],
        env=env, capture_output=True, text=True, timeout=60)
    return out.stdout.strip()


@pytest.mark.parametrize('installed, chosen', [
    ({'3.14': ['python3.14', 'python3']}, None),                     # too new only
    ({'3.14': ['python3.14', 'python3'], '3.12': ['python3.12']}, 'python3.12'),
    ({'3.13': ['python3.13'], '3.12': ['python3.12']}, 'python3.12'),  # tested first
    ({'3.13': ['python3.13']}, 'python3.13'),
    ({'3.9': ['python3']}, None),                                    # too old only
])
def test_the_interpreter_is_in_range(tmp_path, installed, chosen):
    for version, names in installed.items():
        _fake(str(tmp_path / 'bin'), version, *names)
    got = _run(str(tmp_path / 'bin'), 'airlock_python')
    assert (os.path.basename(got) if got else None) == chosen
    status = _run(str(tmp_path / 'bin'), 'airlock_python_status')
    if chosen is None:
        assert status.startswith('no: no python from 3.10 to 3.13 found')
    else:
        assert status.startswith('ok ')


def test_a_venv_built_with_a_python_above_the_ceiling_is_replaced(tmp_path):
    _fake(str(tmp_path / 'venv' / 'bin'), '3.14', 'python3')
    got = _run('/nonexistent', f'source "{REPO}/tools/airlock-venv.sh"; airlock_venv_state '
               f'"{tmp_path}/venv" "{REPO}/systems/backend-deepface/requirements.txt" gunicorn')
    assert got == 'wrong-python built with Python 3.14, above the 3.13 ceiling'
