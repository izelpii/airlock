#!/usr/bin/env python3
"""
What the album gallery must and must not serve.

These are the rules from documentation/specs/albums-enhancement-spec.md that
fail SILENTLY when they break. A locked deployment whose images are still
reachable looks locked; a dotfile that leaks looks like a normal 200; a
containment check comparing a resolved path against an unresolved mount point
looks exactly like "the album is empty". None of them shows up in the UI.

Deliberately self-contained: it builds a catalogs root out of plain files and
symlinks and runs server.mjs against it. No backend, no database, no Python
image library — so it tests the gallery and nothing else, and it still runs on a
machine where the rest of the system does not.

Run directly:  python systems/album-gallery/test_deployment_routes.py
Or under pytest.
"""

import http.cookiejar
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

_HERE = os.path.dirname(os.path.abspath(__file__))
_SERVER = os.path.join(_HERE, 'server.mjs')
_PORT = int(os.environ.get('GALLERY_TEST_PORT', '3187'))
_BASE = f'http://127.0.0.1:{_PORT}'

# A one-pixel JPEG, so no imaging library is needed to have a real image on disk.
_JPEG = bytes.fromhex(
    'ffd8ffe000104a46494600010100000100010000ffdb004300ffffffffffffffffffffff'
    'ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff'
    'ffffffffffffffffffffffffffffffffffffffffffffffffffffc2000b080001000101011'
    '100ffc40014000100000000000000000000000000000009ffda0008010100013f10'
)

# scrypt:32768:8:1$<salt>$<hex>, exactly the format auth.py writes, for the
# password "a-good-password". Hardcoded on purpose: this asserts that the
# gallery can verify a hash THIS SYSTEM produced, so regenerating it here with a
# different implementation would test nothing.
_HASH = (
    'scrypt:32768:8:1$LvH1oafI99COOrZl$'
    'a54dc10a3b880f87c39d4c9a4afbacc8631636730fd3757d9837f0c29de89142'
    'c8c6065cb683a289854a6b0709a05fc1894fa1b01a5b9d4258f5fc4445b567b5'
)
_PASSWORD = 'a-good-password'


def build_fixture(root):
    """
    Two catalogs, each with a deployment called 'summer-2019' - the collision
    that only works because the catalog is in the URL.

    The shapes matter and are the real ones: album folders inside a deployment
    are RELATIVE symlinks two levels up into cache/Albums, whose contents are in
    turn relative symlinks into cache/Images.
    """
    for catalog_dir, locked in (('family-dir', False), ('work-dir', True)):
        cache = os.path.join(root, catalog_dir, 'cache')
        images = os.path.join(cache, 'Images')
        album = os.path.join(cache, 'Albums', '00001')
        deployment = os.path.join(cache, 'Deployments', 'summer-2019')
        os.makedirs(images, exist_ok=True)
        os.makedirs(album, exist_ok=True)
        os.makedirs(deployment, exist_ok=True)

        with open(os.path.join(images, 'photo_alt.jpg'), 'wb') as fh:
            fh.write(_JPEG)
        os.symlink(os.path.join('..', '..', 'Images', 'photo_alt.jpg'),
                   os.path.join(album, 'photo_alt.jpg'))
        with open(os.path.join(album, 'album.json'), 'w') as fh:
            json.dump({'id': 1, 'name': catalog_dir, 'sections': []}, fh)

        os.symlink(os.path.join('..', '..', 'Albums', '00001'),
                   os.path.join(deployment, '00001'))
        with open(os.path.join(deployment, 'albums.json'), 'w') as fh:
            json.dump(['00001'], fh)
        with open(os.path.join(deployment, 'deployment.json'), 'w') as fh:
            json.dump({'slug': 'summer-2019', 'name': f'{catalog_dir} gallery',
                       'requires_password': locked, 'enabled': True,
                       'albums': [{'folder': '00001', 'photo_count': 1}]}, fh)
        if locked:
            path = os.path.join(deployment, '.auth.json')
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, 'w') as fh:
                json.dump({'password_hash': _HASH}, fh)

        # A folder that exists on disk but is NOT in albums.json. The manifest
        # is what decides what exists, so this must stay unreachable.
        stale = os.path.join(deployment, '00099')
        os.makedirs(stale, exist_ok=True)
        with open(os.path.join(stale, 'secret.jpg'), 'wb') as fh:
            fh.write(_JPEG)

        # Something a gallery must never serve, sitting where the mount reaches.
        with open(os.path.join(root, catalog_dir, 'workflow_results.db'), 'w') as fh:
            fh.write('not a gallery asset')

    with open(os.path.join(root, 'catalogs.json'), 'w') as fh:
        json.dump({'family': 'family-dir', 'work': 'work-dir'}, fh)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def request(path, jar=None, method='GET', payload=None, follow=True):
    handlers = []
    if jar is not None:
        handlers.append(urllib.request.HTTPCookieProcessor(jar))
    if not follow:
        handlers.append(_NoRedirect())
    opener = urllib.request.build_opener(*handlers)
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {'Content-Type': 'application/json'} if data else {}
    req = urllib.request.Request(_BASE + path, data=data, headers=headers, method=method)
    try:
        response = opener.open(req, timeout=10)
        return response.status, response.read(), dict(response.headers)
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read(), dict(exc.headers)


class Gallery:
    """server.mjs, running against a throwaway catalogs root."""

    def __enter__(self):
        self.root = tempfile.mkdtemp(prefix='gallery-test-')
        build_fixture(self.root)
        self.proc = subprocess.Popen(
            ['node', _SERVER],
            env=dict(os.environ, CATALOGS_ROOT=self.root, PORT=str(_PORT),
                     ALBUM_SESSION_SECRET='fixed-key-so-the-test-is-deterministic',
                     GALLERY_REVISION='c0ffee1'),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(100):
            try:
                request('/d/family/summer-2019/albums.json')
                return self
            except Exception:
                time.sleep(0.05)
        raise RuntimeError('the gallery did not start')

    def __exit__(self, *exc):
        self.proc.terminate()
        self.proc.wait(timeout=10)
        shutil.rmtree(self.root, ignore_errors=True)


# ── The checks ────────────────────────────────────────────────────────────────

def check_two_catalogs_may_share_a_deployment_slug(fail):
    status, body, _ = request('/d/family/summer-2019/albums.json')
    if status != 200:
        fail(f'family/summer-2019 should serve its manifest, got {status}')
    status, _, _ = request('/d/work/summer-2019/albums.json')
    if status != 401:
        fail(f'work/summer-2019 is a different, locked deployment, got {status}')


def check_an_image_resolves_through_the_symlinks(fail):
    """
    The failure _deploy_album already hit once: links that resolve on the host
    and dangle where the gallery reads them. This is the check that catches it -
    and the one that catches a containment base that was not itself resolved,
    which is how a symlinked mount point (macOS /var) silently empties an album.
    """
    status, body, headers = request('/d/family/summer-2019/albums/00001/photo_alt.jpg')
    if status != 200 or not body.startswith(b'\xff\xd8'):
        fail(f'an image must load through the deployment symlinks, got {status}')
    if headers.get('Content-Type') != 'image/jpeg':
        fail(f'wrong content type: {headers.get("Content-Type")}')


def check_the_manifest_is_authoritative(fail):
    """A folder on disk but absent from albums.json does not exist."""
    status, _, _ = request('/d/family/summer-2019/albums/00099/secret.jpg')
    if status != 404:
        fail(f'a folder missing from albums.json must be unreachable, got {status}')


def check_dotfiles_are_never_served(fail):
    for path in ('/d/work/summer-2019/.auth.json',
                 '/d/work/summer-2019/albums/.auth.json',
                 '/d/work/summer-2019/albums/00001/../.auth.json'):
        status, body, _ = request(path)
        if status != 404 or b'scrypt' in body:
            fail(f'{path} must be a flat 404, got {status} {body[:40]!r}')


def check_traversal_is_refused(fail):
    for path in ('/d/family/summer-2019/albums/00001/../../../workflow_results.db',
                 '/d/family/summer-2019/albums/00001/%2e%2e/%2e%2e/%2e%2e/workflow_results.db'):
        status, body, _ = request(path)
        if status != 404 or b'not a gallery asset' in body:
            fail(f'{path} must not escape the catalog cache, got {status}')


def check_unknown_names_are_indistinguishable(fail):
    """
    The same answer for a catalog that does not exist and a deployment that does
    not, so the URL space cannot be walked.
    """
    for path in ('/d/nosuch/summer-2019/albums.json',
                 '/d/family/nosuch/albums.json',
                 '/d/family/NotASlug/albums.json'):
        status, _, _ = request(path)
        if status != 404:
            fail(f'{path} should be 404, got {status}')


def check_a_locked_deployment_gates_everything(fail):
    """
    In front of the manifest AND the assets AND the album list - not only the
    HTML. A gallery that merely looks locked has its images one URL guess away.
    """
    status, body, _ = request('/d/work/summer-2019/deployment.json')
    meta = json.loads(body)
    if status != 200 or meta.get('requires_password') is not True:
        fail('a locked deployment must still say it needs a password')
    if 'albums' in meta:
        fail(f'a locked deployment must not reveal its album list: {sorted(meta)}')
    if meta.get('name') != 'work-dir gallery':
        fail('a locked deployment should still give its name')

    for path in ('/d/work/summer-2019/albums.json',
                 '/d/work/summer-2019/albums/00001/photo_alt.jpg',
                 '/d/work/summer-2019/albums/00001/album.json'):
        status, _, _ = request(path)
        if status != 401:
            fail(f'{path} must be 401 before login, got {status}')


def check_the_password_unlocks_it(fail):
    jar = http.cookiejar.CookieJar()
    status, _, _ = request('/d/work/summer-2019/login', jar, 'POST', {'password': 'wrong'})
    if status != 401:
        fail(f'a wrong password must be refused, got {status}')

    status, _, headers = request('/d/work/summer-2019/login', jar, 'POST',
                                 {'password': _PASSWORD})
    if status != 200:
        fail(f'the right password must be accepted, got {status}')
    if 'HttpOnly' not in headers.get('Set-Cookie', ''):
        fail('the session cookie must be HttpOnly')
    if not any(c.name == 'sb_album_work_summer_2019' for c in jar):
        fail(f'the cookie must be keyed on catalog AND slug: {[c.name for c in jar]}')

    for path in ('/d/work/summer-2019/albums.json',
                 '/d/work/summer-2019/albums/00001/photo_alt.jpg'):
        status, _, _ = request(path, jar)
        if status != 200:
            fail(f'{path} must be 200 after login, got {status}')


def check_a_session_does_not_travel(fail):
    """
    Unlocking family/summer-2019 must grant nothing on work/summer-2019. Under
    this URL scheme that is a real pair of URLs, not a hypothetical one.
    """
    jar = http.cookiejar.CookieJar()
    request('/d/work/summer-2019/login', jar, 'POST', {'password': _PASSWORD})
    value = [c for c in jar if c.name == 'sb_album_work_summer_2019'][0].value

    forged = value.replace('work', 'family', 1)
    req = urllib.request.Request(_BASE + '/d/work/summer-2019/albums.json')
    req.add_header('Cookie', f'sb_album_work_summer_2019={forged}')
    try:
        status = urllib.request.urlopen(req, timeout=10).status
    except urllib.error.HTTPError as exc:
        status = exc.code
    if status != 401:
        fail(f'a session signed for another deployment must be rejected, got {status}')


def check_the_trailing_slash_redirect_is_relative(fail):
    """
    Behind nginx the browser is at /albums/family/summer-2019; an absolute /d/…
    Location would send it somewhere that does not exist from outside.
    """
    status, _, headers = request('/d/family/summer-2019', follow=False)
    if status != 301:
        fail(f'a bare deployment URL should redirect, got {status}')
    if headers.get('Location') != 'summer-2019/':
        fail(f'the redirect must be relative, got {headers.get("Location")!r}')


def check_health_answers_and_reports_a_mounted_root(fail):
    """
    /health must be a real check, not the SPA shell.

    The failure this guards against is silent and total: without its own route,
    /health falls through to the single-page-app fallback and returns 200 with
    index.html - a probe that passes while the gallery serves nothing at all.
    """
    status, body, headers = request('/health')
    if status != 200:
        fail(f'/health should answer 200, got {status}')
        return
    ctype = headers.get('Content-Type', '')
    if 'json' not in ctype:
        fail(f'/health answered {ctype!r} - the SPA fallback is catching it')
        return
    payload = json.loads(body)
    if payload.get('status') != 'ok' or not payload.get('serving'):
        fail(f'a mounted CATALOGS_ROOT should report serving, got {payload}')
    if payload.get('catalogs_root') != 'ok':
        fail(f'catalogs_root should be "ok" here, got {payload.get("catalogs_root")!r}')
    if payload.get('catalogs') != 2:
        fail(f'the fixture has two catalogs, /health reports {payload.get("catalogs")}')


def check_health_names_no_catalog_or_deployment(fail):
    """
    It is not behind the password gate, so it must not hand an anonymous caller
    the slugs to try - the same rule that keeps a locked deployment from
    revealing its album list (spec section 5.3).
    """
    _, body, _ = request('/health')
    text = body.decode()
    for secret in ('family', 'work', 'summer-2019', 'family-dir', 'work-dir'):
        if secret in text:
            fail(f'/health leaked {secret!r}: {text}')


def check_the_removed_dimensions_api_is_gone(fail):
    status, _, _ = request('/api/dimensions', method='POST', payload={'paths': []})
    if status not in (404, 405):
        fail(f'/api/dimensions should no longer exist, got {status}')


def check_health_reports_the_gallery_revision(fail):
    """
    The Publish to server button compares a server's gallery with this
    machine's by the revision baked in at build time (GALLERY_REVISION).
    """
    _, body, _ = request('/health')
    revision = json.loads(body).get('revision')
    if revision != 'c0ffee1':
        fail(f'/health should report the build revision c0ffee1, got {revision!r}')


CHECKS = [value for name, value in sorted(globals().items()) if name.startswith('check_')]


def run():
    failures = []
    with Gallery():
        for check in CHECKS:
            problems = []
            check(problems.append)
            label = check.__name__[len('check_'):].replace('_', ' ')
            if problems:
                failures.extend(problems)
                print(f'  FAIL  {label}')
                for problem in problems:
                    print(f'          {problem}')
            else:
                print(f'  PASS  {label}')
    return failures


# pytest picks these up as individual tests; running the file goes through run().
def _pytest_factory(check):
    def test(gallery_server):
        problems = []
        check(problems.append)
        assert not problems, '; '.join(problems)
    test.__name__ = f'test_{check.__name__[len("check_"):]}'
    return test


try:
    import pytest

    @pytest.fixture(scope='module')
    def gallery_server():
        with Gallery() as gallery:
            yield gallery

    for _check in CHECKS:
        globals()[f'test_{_check.__name__[len("check_"):]}'] = _pytest_factory(_check)
except ImportError:
    pass


if __name__ == '__main__':
    if shutil.which('node') is None:
        print('node is not installed; skipping.')
        sys.exit(0)
    _failures = run()
    print()
    print(f'{len(_failures)} check(s) failed' if _failures else 'all checks passed')
    sys.exit(1 if _failures else 0)
