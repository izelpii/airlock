#!/usr/bin/env python3
"""
The compose install stays an addition, and stays in step with the application.

    python -m pytest compose/tests/

documentation/specs/compose-install-spec.md, sections 0 and 16. Everything
here guards one of two failures that would otherwise be silent:

  * compose-only artifacts spreading into the application, or the application
    starting to depend on compose (section 0);
  * the application changing - a new service, upstream, setting or mount - and
    the compose install quietly falling behind (section 16, "Drift").

The compose file is read through `docker compose config` (or docker-compose),
which resolves anchors and .env exactly as Compose will; those tests skip where
neither is installed.
"""

import glob
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
COMPOSE = os.path.dirname(HERE)
REPO = os.path.dirname(COMPOSE)
IMAGES = os.path.join(COMPOSE, 'images')
sys.path.insert(0, IMAGES)
sys.path.insert(0, os.path.join(IMAGES, 'gateway'))

import render_layer  # noqa: E402

_spec = __import__('importlib.util').util.spec_from_file_location(
    'render_config', os.path.join(IMAGES, 'gateway', 'render-config.py'))
render_config = __import__('importlib.util').util.module_from_spec(_spec)
_spec.loader.exec_module(render_config)

SERVICES = render_layer.services()


def _git_files():
    out = subprocess.run(['git', 'ls-files', '--cached', '--others', '--exclude-standard'],
                         cwd=REPO, capture_output=True, text=True)
    if out.returncode != 0:
        pytest.skip('not a git checkout')
    return out.stdout.split()


# ── Section 0: an addition, in its own folder ─────────────────────────────────

# The only files outside compose/ that belong to the compose install.
ALLOWED_OUTSIDE = {'.github/workflows/compose-images.yml'}


def test_compose_artifacts_live_in_compose():
    stray = [f for f in _git_files()
             if re.search(r'(^|/)(docker-)?compose[^/]*\.ya?ml$', f)
             and not f.startswith('compose/') and f not in ALLOWED_OUTSIDE]
    assert not stray, f'compose files outside compose/: {stray}'


def test_the_application_does_not_depend_on_compose():
    """Removing compose/ must leave a working application: nothing under
    systems/ or deploy/, and neither start script, may refer into it."""
    offenders = []
    for f in _git_files():
        if not (f.startswith(('systems/', 'deploy/')) or f in ('start-all.sh', 'stop-all.sh')):
            continue
        if not f.endswith(('.py', '.sh', '.js', '.mjs', '.conf', '.template', '.yaml', '.yml',
                           'Dockerfile', '.json')):
            continue
        try:
            text = open(os.path.join(REPO, f), encoding='utf-8').read()
        except (OSError, UnicodeDecodeError):
            continue
        if re.search(r'(?<![\w-])compose/(images|ci|docker-compose)', text):
            offenders.append(f)
    assert not offenders, f'application files referring into compose/: {offenders}'


def test_the_workflow_file_only_calls_compose():
    path = os.path.join(REPO, '.github', 'workflows', 'compose-images.yml')
    text = open(path, encoding='utf-8').read()
    assert 'compose/ci/build.sh' in text
    assert 'docker build' not in text and 'buildx build' not in text, \
        'build logic belongs in compose/ci/build.sh, not in the workflow'


# ── The gateway: the application's configuration, rewritten ───────────────────

def _template():
    return open(os.path.join(REPO, 'systems', 'gateway', 'airlock.conf.template'),
                encoding='utf-8').read()


def test_the_gateway_config_renders_from_the_application_template():
    rendered = render_config.render(_template())
    assert 'resolver ${NGINX_LOCAL_RESOLVERS}' in rendered
    assert 'server backend:5000 resolve;' in rendered
    assert 'server ${AIRLOCK_OLLAMA_SERVER};' in rendered


def test_only_the_upstream_lines_change():
    original = [l for l in _template().splitlines() if not render_config.UPSTREAM.match(l)]
    rendered = [l for l in render_config.render(_template()).replace(render_config.RESOLVER, '').splitlines()
                if not l.startswith('upstream ')]
    assert original == rendered


def test_a_new_upstream_in_the_application_stops_the_build():
    with pytest.raises(SystemExit, match='no compose service'):
        render_config.render(_template() + '\nupstream airlock_newthing { server ${UPSTREAM_HOST}:5999; }\n')


# ── Image layers ──────────────────────────────────────────────────────────────

def _app_dockerfile(source):
    return open(os.path.join(REPO, source, 'Dockerfile'), encoding='utf-8').read()


@pytest.mark.parametrize('name', sorted(SERVICES))
def test_every_layer_renders(name):
    assert render_layer.render(name).strip()


@pytest.mark.parametrize('name', sorted(n for n, s in SERVICES.items() if s['layer'] == 'anyuid'))
def test_an_anyuid_layer_runs_the_applications_command(name):
    """ENTRYPOINT resets an inherited CMD, so the layer repeats it - from the
    application's Dockerfile, so the two cannot differ."""
    svc = SERVICES[name]
    text = render_layer.render(name)
    cmd = json.loads(re.search(r'^CMD (.+)$', text, re.M).group(1))
    assert cmd == render_layer.base_cmd(svc['source'])
    app = _app_dockerfile(svc['source'])
    assert re.search(r'^USER appuser', app, re.M), \
        f'{svc["source"]} no longer runs as appuser - is the anyuid layer still right?'
    for owned in svc['owned']:
        assert owned.startswith(('/home/appuser', '/app')), \
            f'{name}: {owned} - only the image\'s own folders may be re-owned at start'


def test_layer_kinds_match_the_images_users():
    """An image that runs as root needs no anyuid layer, and one that runs as
    appuser must have it - or Docker writes its volumes as uid 1000."""
    for name, svc in SERVICES.items():
        if not svc.get('source'):
            continue
        runs_as_appuser = bool(re.search(r'^USER appuser', _app_dockerfile(svc['source']), re.M))
        assert runs_as_appuser == (svc['layer'] == 'anyuid'), name


def test_every_file_a_base_build_copies_is_in_git_or_vendored():
    """A Dockerfile that COPYs a file git doesn't have works for the script
    install, whose start script copies it in first, and fails in CI unless
    services.json says where build.sh gets it (v6.6.1: transcode's
    video_proxy.py)."""
    tracked = set(_git_files())
    missing = []
    for name, svc in SERVICES.items():
        source = svc.get('source')
        if not source:
            continue
        vendor = svc.get('vendor', {})
        for to, frm in vendor.items():
            assert frm in tracked, f'{name}: vendor source {frm} is not in git'
        for line in _app_dockerfile(source).splitlines():
            m = re.match(r'COPY\s+(?!--from)(?:--\S+\s+)*(.+)\s+\S+$', line.strip())
            if not m:
                continue
            for src in m.group(1).split():
                if any(c in src for c in '*?') or src in ('.', './'):
                    continue
                path = os.path.normpath(os.path.join(source, src))
                if path in tracked or any(f.startswith(path + '/') for f in tracked) \
                        or src.rstrip('/') in vendor:
                    continue
                missing.append(f'{name}: {src}')
    assert not missing, f'COPYed, but neither in git nor vendored by build.sh: {missing}'


def test_baked_files_are_the_ones_the_script_install_fetches():
    deploy = open(os.path.join(REPO, 'deploy', 'deploy.sh'), encoding='utf-8').read()
    pets = open(os.path.join(REPO, 'tools', 'fetch_pet_weights.sh'), encoding='utf-8').read()
    for svc in SERVICES.values():
        for item in svc.get('fetch', []):
            assert item['url'] in deploy or item['url'] in pets, item['url']
            if item['url'] in deploy:
                assert item['sha256'] in deploy, f'{item["url"]}: checksum differs from deploy.sh'


def test_make_certs_still_takes_what_the_certs_image_passes():
    script = open(os.path.join(REPO, 'systems', 'gateway', 'make-certs.sh'), encoding='utf-8').read()
    for flag in ('--no-auto-ip', '--ca-label', '--force'):
        assert flag in script, f'make-certs.sh no longer accepts {flag}'
    assert 'CERT_DIR="$SCRIPT_DIR/certs"' in script, \
        'make-certs.sh no longer writes beside itself; compose mounts ./certs at /work/certs'


# ── The compose file ──────────────────────────────────────────────────────────

def _compose_cli():
    if shutil.which('docker') and subprocess.run(['docker', 'compose', 'version'],
                                                 capture_output=True).returncode == 0:
        return ['docker', 'compose']
    if shutil.which('docker-compose'):
        return ['docker-compose']
    pytest.skip('no compose CLI')


def _config(*files):
    with tempfile.TemporaryDirectory() as tmp:
        env = os.path.join(tmp, '.env')
        with open(env, 'w') as fh:
            fh.write('STORAGE_ROOT=/tmp\n')
        args = _compose_cli() + ['--env-file', env]
        for f in files or ('docker-compose.yml',):
            args += ['-f', os.path.join(COMPOSE, f)]
        out = subprocess.run(args + ['config', '--format', 'json'],
                             capture_output=True, text=True, cwd=COMPOSE)
        assert out.returncode == 0, out.stderr
        return json.loads(out.stdout)


def test_the_compose_files_are_valid_together():
    _config('docker-compose.yml', 'docker-compose.gpu.yml', 'docker-compose.podman.yml')


def test_every_published_image_is_used_and_every_used_image_is_published():
    published = {s.get('publish_as', n) for n, s in SERVICES.items()}
    used = set()
    for svc in _config()['services'].values():
        m = re.search(r'/airlock-([a-z-]+):', svc.get('image', ''))
        if m:
            used.add(m.group(1))
    assert used == published


def test_the_gateway_answers_to_every_service_name():
    names = []
    with open(os.path.join(REPO, 'systems', 'gateway', 'hosts-entries.txt'), encoding='utf-8') as fh:
        for line in fh:
            if re.match(r'^\d', line):
                names.append(line.split()[1])
    aliases = _config()['services']['gateway']['networks']['airlock']['aliases']
    assert sorted(aliases) == sorted(names)


def test_only_the_gateway_publishes_ports():
    for name, svc in _config()['services'].items():
        assert not svc.get('ports') or name == 'gateway', f'{name} publishes {svc["ports"]}'


# The application's settings, per service, from its start scripts - each must be
# set by docker-compose.yml, documented for settings.env, or explained here.
SCRIPT_TO_SERVICE = {
    'backend': 'backend', 'backend-deepface': 'deepface', 'backend-pets': 'pets',
    'backend-vision': 'vision', 'backend-albums': 'albums', 'backend-geoloc': 'geoloc',
    'backend-transcode': 'transcode', 'album-gallery': 'gallery', 'gateway': 'gateway',
}
NOT_NEEDED = {
    ('backend', 'AIRLOCK_IMPORT_CHECK'): 'the image smoke test, at build time only',
    ('backend', 'DISABLE_WORKFLOW_SCHEDULER'): 'a debugging switch, off by default',
    ('gateway', 'UPSTREAM_HOST'): 'replaced by per-service upstreams (render-config.py)',
    ('deepface', 'DEEPFACE_WARMUP_MODELS'): 'optional; settings.env',
}


def _script_env(script):
    text = open(script, encoding='utf-8').read()
    return set(re.findall(r'-e\s+"?([A-Z][A-Z0-9_]+)=', text)) | \
        set(re.findall(r'\$\{([A-Z][A-Z0-9_]+):\+-e', text))


def test_every_setting_the_scripts_pass_reaches_compose():
    config = _config()['services']
    documented = open(os.path.join(COMPOSE, 'settings.env.example'), encoding='utf-8').read()
    missing = []
    for folder, service in SCRIPT_TO_SERVICE.items():
        script = os.path.join(REPO, 'systems', folder, 'start-podman.sh')
        for name in _script_env(script):
            if (service, name) in NOT_NEEDED:
                continue
            if name in (config[service].get('environment') or {}):
                continue
            if re.search(rf'^#?{name}=', documented, re.M):
                continue
            missing.append(f'{service}: {name}')
    assert not missing, f'settings the script install passes, missing from compose: {missing}'


def test_every_variable_the_compose_files_use_is_documented():
    used = set()
    for f in glob.glob(os.path.join(COMPOSE, 'docker-compose*.yml')):
        used |= set(re.findall(r'\$\{([A-Z][A-Z0-9_]*)', open(f, encoding='utf-8').read()))
    documented = open(os.path.join(COMPOSE, '.env.example'), encoding='utf-8').read()
    undocumented = sorted(v for v in used if not re.search(rf'^#?{v}=', documented, re.M)
                          and v not in ('SETTINGS_FILE', 'OLLAMA_VERSION'))
    assert not undocumented, f'used in a compose file but not in .env.example: {undocumented}'
