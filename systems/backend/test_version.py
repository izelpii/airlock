"""
Which build is this.

    python -m pytest test_version.py

See CHANGELOG.md. The version has to be answerable in three situations and the
tests are one per situation, because the one that breaks silently is the
container: it has no .git and the VERSION file is outside its build context, so
if the build argument is not passed it reports 'unknown' and nothing else
complains.
"""

import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
sys.path.insert(0, HERE)

import version as airlock_version  # noqa: E402


@pytest.fixture(autouse=True)
def _no_inherited_env(monkeypatch):
    """The ambient AIRLOCK_VERSION, if any, must not decide these tests."""
    monkeypatch.delenv('AIRLOCK_VERSION', raising=False)


def test_the_repository_declares_a_version():
    path = os.path.join(ROOT, 'VERSION')
    assert os.path.isfile(path), 'VERSION is the single source of truth'
    with open(path, encoding='utf-8') as fh:
        declared = fh.read().strip()
    assert declared, 'VERSION must not be empty'
    parts = declared.split('.')
    # A fourth number marks a commit that changes nothing Airlock does
    # (CHANGELOG.md, "What moves which number"): 6.5.4.1.
    assert len(parts) in (3, 4) and all(p.isdigit() for p in parts), \
        f'expected MAJOR.MINOR.PATCH or MAJOR.MINOR.PATCH.NOTE, got {declared!r}'


def test_it_reads_the_file_in_a_checkout():
    with open(os.path.join(ROOT, 'VERSION'), encoding='utf-8') as fh:
        declared = fh.read().strip()
    assert airlock_version.base_version() == declared


def test_the_environment_wins_over_the_file(monkeypatch):
    """How a container knows. It carries no VERSION file and no .git."""
    monkeypatch.setenv('AIRLOCK_VERSION', '9.9.9+7.deadbee')
    assert airlock_version.base_version() == '9.9.9+7.deadbee'
    info = airlock_version.version_info()
    assert info['version'] == '9.9.9+7.deadbee'
    # The bare number, NOT the decorated one. This is the half that matches a
    # git tag, and reporting '9.9.9+7.deadbee' as the release would send
    # somebody looking for a tag that cannot exist. It did, until the baked
    # path was tested.
    assert info['release'] == '9.9.9'
    assert info['source'] == 'build', 'a baked version describes the image'


def test_a_baked_version_is_not_overwritten_by_the_checkout(monkeypatch):
    """
    An image reports what it was built from. The .git beside it on disk, if any,
    may be on another branch entirely.
    """
    monkeypatch.setenv('AIRLOCK_VERSION', '9.9.9+7.deadbee')
    monkeypatch.setattr(airlock_version, '_git_detail', lambda: (999, 'cafef00d'))
    assert airlock_version.version_info()['version'] == '9.9.9+7.deadbee'


def test_an_absent_version_says_so_rather_than_guessing(monkeypatch):
    """
    'unknown' is a better answer than a plausible wrong one: the question is
    always "which code is running", and a confident lie there costs more than
    an admission.
    """
    monkeypatch.setattr(airlock_version, '_from_file', lambda: None)
    assert airlock_version.base_version() == airlock_version.UNKNOWN


def test_version_info_carries_the_release_separately():
    """
    `version` is what to print; `release` is what matches a git tag. The
    decorated form cannot be looked up with `git show`.
    """
    info = airlock_version.version_info()
    assert info['release'] in info['version']
    assert '+' not in info['release']


def test_the_declared_version_has_a_tag():
    """
    The point of the whole exercise: a release number you cannot check out is
    not tracking anything. Skipped where git is unavailable rather than failing,
    since the package has no .git inside a container.
    """
    with open(os.path.join(ROOT, 'VERSION'), encoding='utf-8') as fh:
        declared = fh.read().strip()
    try:
        out = subprocess.run(['git', 'tag', '-l', f'v{declared}'],
                             cwd=ROOT, capture_output=True, text=True, timeout=5)
    except Exception:
        pytest.skip('git is not available here')
    if out.returncode != 0:
        pytest.skip('not a git checkout')
    assert out.stdout.strip() == f'v{declared}', \
        f'VERSION says {declared} but there is no v{declared} tag'


def test_git_detail_never_raises():
    """
    It decorates a version string. A slow, absent or unusual git must not take
    a request down, so every failure path returns None.
    """
    assert airlock_version._git_detail() is None or \
        isinstance(airlock_version._git_detail(), tuple)


def test_the_dockerfile_takes_the_version_as_a_build_argument():
    """
    The silent failure this guards: without the ARG the image reports
    'unknown', and nothing anywhere complains.
    """
    with open(os.path.join(HERE, 'Dockerfile'), encoding='utf-8') as fh:
        dockerfile = fh.read()
    assert 'ARG AIRLOCK_VERSION' in dockerfile
    assert 'ENV AIRLOCK_VERSION' in dockerfile

    with open(os.path.join(HERE, 'start-podman.sh'), encoding='utf-8') as fh:
        start = fh.read()
    assert '--build-arg' in start and 'AIRLOCK_VERSION' in start, \
        'the build script must pass the version it read'


def test_version_is_the_newest_tag_so_no_commit_is_unversioned():
    """
    Every commit is versioned and tagged (CHANGELOG.md, "What moves which number"),
    so VERSION must name the NEWEST tag - not merely some tag that exists.

    What this catches: a commit that forgot to bump. The test above is happy
    with a stale VERSION as long as its tag exists somewhere in history, which
    is exactly the state a forgotten bump leaves behind - and a container built
    from it reports a version that belongs to different code.
    """
    with open(os.path.join(ROOT, 'VERSION'), encoding='utf-8') as fh:
        declared = fh.read().strip()
    try:
        out = subprocess.run(['git', 'tag', '-l', '--sort=-v:refname', 'v*'],
                             cwd=ROOT, capture_output=True, text=True, timeout=5)
    except Exception:
        pytest.skip('git is not available here')
    if out.returncode != 0 or not out.stdout.strip():
        pytest.skip('not a git checkout, or no tags')

    newest = out.stdout.split()[0]
    assert newest == f'v{declared}', (
        f'VERSION says {declared} but the newest tag is {newest}. Every commit '
        f'is tagged: a fix takes the next patch, a capability the next minor, '
        f'and a documentation-only change a fourth number (6.5.4.1).')
