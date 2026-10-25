#!/usr/bin/env python3
"""
What version of airlock is this.

One source of truth, the VERSION file at the repository root, resolved three
ways because the answer has to be available in three different situations:

  1. AIRLOCK_VERSION in the environment. This is how a CONTAINER knows: the
     image carries no .git and the build context is systems/backend, so the
     root VERSION file cannot even be COPYed in. start-podman.sh passes it as a
     build argument and the Dockerfile freezes it into the image.

  2. The VERSION file itself, found by walking up from this file. This is the
     development checkout, where the file is two directories above.

  3. 'unknown'. Said plainly rather than guessed at - a wrong version number is
     worse than an absent one when somebody is working out which build is
     running.

Where a .git is reachable, `git describe` adds how far the working tree has
moved past the release, which is the thing worth knowing day to day:

    5.0.0              an exact release
    5.0.0+12.abc1234   12 commits past it

What the three numbers mean is in CHANGELOG.md, under "What moves which
number", and the short version is: EVERY COMMIT ADVANCES THE PATCH and is
tagged, a new capability advances the minor, and the major is reserved for
enough accumulated capability to be a different product. A `+N` suffix here
therefore means N commits that have not been versioned yet, which should
normally be zero.

Every release is a tag, including the thirteen 4.x capability releases, so an
older commit describes against the release it belongs to. See CHANGELOG.md for
why v4.0.0 is tagged on the first commit in this repository even though the
release itself predates it.
"""

import os
import subprocess

_HERE = os.path.dirname(os.path.abspath(__file__))
UNKNOWN = 'unknown'


def _from_file():
    """The VERSION file, walking up from here. None when it is not there."""
    path = _HERE
    for _ in range(5):          # systems/backend -> systems -> repo root
        candidate = os.path.join(path, 'VERSION')
        if os.path.isfile(candidate):
            try:
                with open(candidate, encoding='utf-8') as fh:
                    value = fh.read().strip()
                if value:
                    return value
            except OSError:
                return None
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


def _git_detail():
    """
    (commits_since_tag, short_sha) from git, or None.

    Never raises and never waits long: this is called to decorate a version
    string, and a slow or absent git must not hold up a request. A checkout with
    no tags yet answers None rather than failing.
    """
    try:
        out = subprocess.run(
            ['git', 'describe', '--tags', '--long', '--dirty', '--always'],
            cwd=_HERE, capture_output=True, text=True, timeout=3)
        if out.returncode != 0:
            return None
        text = out.stdout.strip()
    except Exception:
        return None
    if not text:
        return None
    # v5.0.0-12-gabc1234[-dirty]
    parts = text.split('-')
    if len(parts) >= 3 and parts[-1] == 'dirty':
        parts = parts[:-1] + ['dirty']
    for i, part in enumerate(parts):
        if part.startswith('g') and len(part) >= 8 and i >= 2:
            try:
                return int(parts[i - 1]), part[1:]
            except ValueError:
                return None
    return None


def base_version():
    """The declared release number: 'AIRLOCK_VERSION', the file, or 'unknown'."""
    return (os.environ.get('AIRLOCK_VERSION') or '').strip() or \
        _from_file() or UNKNOWN


def version_info():
    """
    Everything the /api/version endpoint and the UI need.

    `version` is what to print. `release` is the declared number alone, which is
    what matches a tag. The rest is present only when git could be asked.
    """
    declared = base_version()
    # A baked value already carries its detail - '5.0.0+12.abc1234' - because
    # start-podman.sh describes the checkout at build time. Split it: `release`
    # has to stay the bare number, since that is the half that matches a git
    # tag and can be checked out. Printing the decorated form as the release
    # would send somebody looking for a tag that cannot exist.
    release = declared.split('+', 1)[0]
    detail = _git_detail()
    info = {'release': release, 'version': declared, 'source': 'file'}
    if os.environ.get('AIRLOCK_VERSION'):
        # Frozen at build time, so it describes the IMAGE rather than whatever
        # checkout happens to be on disk beside it.
        info['source'] = 'build'
    if detail:
        commits, sha = detail
        info['commits_since_release'] = commits
        info['commit'] = sha
        # Only when nothing was baked in: a build-time value already describes
        # the image, and the checkout on disk beside it may be anywhere.
        if commits and info['source'] != 'build':
            info['version'] = f'{release}+{commits}.{sha}'
        info['source'] = 'git' if info['source'] == 'file' else info['source']
    return info


if __name__ == '__main__':
    import json
    print(json.dumps(version_info(), indent=2))
