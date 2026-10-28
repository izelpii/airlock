#!/usr/bin/env python3
"""
Consistency checks between each service's start-podman.sh and its pod manifest.

Every service has two launchers — a shell script and a `podman play kube`
manifest — and nothing keeps them in step. The failure mode has no symptom until
someone uses the path nobody has been using:

    gateway-pod.yaml predated the TLS work. It had no port 443, no
    SERVER_DOMAIN, and no certificate mount, while airlock.conf.template has a
    `listen 443 ssl` block that loads /etc/nginx/certs/fullchain.pem. nginx does
    not fall back to HTTP without it — it fails to start. That manifest was
    broken for as long as everyone used start-podman.sh instead.

So this compares what the two actually declare: published container ports,
environment variables and mount points. Differences that are known and harmless
are listed in KNOWN_DIFFERENCES with a reason each; anything else fails.

That list is the point as much as the check is. A difference either has a reason
written next to it or it is a bug, and "someone will remember" has already been
tried.

Run directly:  python systems/test_pod_manifests.py
Or under pytest.
"""

import glob
import os
import re
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))


# ── Known, deliberate differences ─────────────────────────────────────────────
# (service, kind, name) -> why it is not drift. Verified 2026-09-06.
#
# kind is 'env' or 'mount'; the tuple says which side has the extra.
KNOWN_DIFFERENCES = {
    ('backend-geoloc', 'mount', '/app/cache'):
        'the service never reads a cache directory; the mount is vestigial',
    ('backend-vision', 'env', 'OLLAMA_MODEL'):
        "identical to the application's own default in app.py",
    ('backend-vision', 'env', 'PYTHONUNBUFFERED'):
        'affects log flushing only',
    ('backend', 'env', 'FLASK_ENV'):
        'not read anywhere in the backend code or its Dockerfile',
    # backend-transcode passes these three as ${VAR:+-e "VAR=$VAR"} - present
    # only when the invoking environment sets them. Omitting them from the
    # manifest is the SAME behaviour, not a difference: the service's defaults
    # apply either way. Writing value: "" instead would be worse, because an
    # empty TRANSCODE_ENCODER is not the same as an absent one.
    ('backend-transcode', 'env', 'TRANSCODE_VAAPI_DEVICE'):
        'optional in the script (${VAR:+...}); absent means the same thing',
    ('backend-transcode', 'env', 'TRANSCODE_JOBS'):
        'optional in the script (${VAR:+...}); absent means the same thing',
    ('backend-transcode', 'env', 'TRANSCODE_ENCODER'):
        'optional in the script (${VAR:+...}); absent means the same thing',
    ('backend', 'mount', '/storage'):
        'NOT drift - the script mounts it as "${STORAGE_ROOT_HOST}":'
        '"${STORAGE_ROOT_CONTAINER}", and this file reads shell text rather '
        'than running it, so the container path is an unexpanded variable here '
        'while the manifest spells it out. STORAGE_ROOT_CONTAINER defaults to '
        '/storage in deploy/airlock-env.sh and both launchers agree in fact',
}

# Two differences that looked real and are not, recorded so they are not added
# back: DISABLE_WORKFLOW_SCHEDULER appears only in the script's image smoke-test
# container, which _service_run_command() excludes; /data/catalog is a
# commented-out example, which both readers now skip.


def services():
    """(name, script path, manifest path) for every service that has both."""
    out = []
    for sh in sorted(glob.glob(os.path.join(_HERE, '*', 'start-podman.sh'))):
        svc = os.path.basename(os.path.dirname(sh))
        manifests = glob.glob(os.path.join(_HERE, svc, '*pod*.yaml'))
        if manifests:
            out.append((svc, sh, manifests[0]))
    return out


# ── What the shell script declares ────────────────────────────────────────────
# Only the final `podman run` that starts the service. Scripts also run
# throwaway containers to validate the image or the config, and those carry
# their own flags - counting them would report drift that is not there.

def _service_run_command(script_text):
    """The launching `podman run`, not the preflight ones."""
    runs = [m.start() for m in re.finditer(r'podman run', script_text)]
    if not runs:
        return ''
    # The launcher is the one that names the container: the preflight runs are
    # --rm throwaways with --entrypoint.
    named = [i for i in runs if '--name' in script_text[i:i + 400]]
    start = (named[-1] if named else runs[-1])
    return script_text[start:start + 2000]


def _shell_defaults(script_text):
    """
    {VAR: value} for the simple assignments these scripts use at the top.

    Handles VAR="literal" and VAR="${VAR:-default}", which is how every one of
    them declares a tunable. Without this, a mount written as
    -v "${A}":"${B}":ro reads as no mount at all, and the check silently stops
    checking - the failure mode this file exists to prevent, one level up.
    """
    out = {}
    for m in re.finditer(r'^([A-Z_][A-Z0-9_]*)="([^"\n]*)"\s*$', script_text, re.M):
        name, raw = m.group(1), m.group(2)
        inner = re.fullmatch(r'\$\{' + name + r':-(.*)\}', raw)
        out[name] = inner.group(1) if inner else raw
    return out


def _expand(text, defaults):
    """Substitute ${VAR} and $VAR from *defaults*, leaving unknowns alone."""
    def sub(m):
        return defaults.get(m.group(1) or m.group(2), m.group(0))
    return re.sub(r'\$\{([A-Z_][A-Z0-9_]*)\}|\$([A-Z_][A-Z0-9_]*)', sub, text)


def script_declares(path):
    text = open(path).read()
    cmd = _expand(_service_run_command(text), _shell_defaults(text))
    env = set(re.findall(r'-e\s+"?([A-Z_][A-Z0-9_]*)=', cmd))
    # The container side may be quoted too: -v "$SRC":"/storage":ro
    mounts = set(re.findall(r'-v\s+"?[^":]+"?:"?(/[^:\s"]+)', cmd))
    # The container side of -p host:container. The host side is often a shell
    # variable; the container side is the stable fact and the one the manifest
    # states as containerPort.
    ports = set(re.findall(r'-p\s+"?[^:"]+:(\d+)', cmd))
    return {'env': env, 'mount': mounts, 'port': ports}


# ── What the manifest declares ────────────────────────────────────────────────
# PyYAML when it is installed, a narrow reader when it is not. The manifests are
# not in the backend image's requirements and pyyaml is absent from the
# container, so this has to work without it.

def manifest_declares(path):
    text = open(path).read()
    try:
        import yaml
    except ImportError:
        return _manifest_declares_textually(text)

    doc = yaml.safe_load(text)
    container = doc['spec']['containers'][0]
    return {
        'env': {e['name'] for e in container.get('env') or []},
        'mount': {m['mountPath'] for m in container.get('volumeMounts') or []},
        'port': {str(p['containerPort']) for p in container.get('ports') or []},
    }


def _manifest_declares_textually(text):
    """
    Fallback reader for the subset these manifests use.

    Comment lines are stripped first. Without that this reader sees the
    commented-out example mounts these manifests carry and the pyyaml reader
    does not, so the same repository would pass on one machine and fail on
    another - which is worse than either answer.
    """
    text = '\n'.join(l for l in text.split('\n') if not l.lstrip().startswith('#'))
    return {
        'env': set(re.findall(r'name:\s*([A-Z_][A-Z0-9_]*)\s*\n\s*value:', text)),
        'mount': set(re.findall(r'mountPath:\s*(\S+)', text)),
        'port': set(re.findall(r'containerPort:\s*(\d+)', text)),
    }


# ── The check ─────────────────────────────────────────────────────────────────

def differences(svc, script, manifest):
    """Unexplained differences for one service, as readable strings."""
    found = []
    for kind in ('env', 'mount', 'port'):
        for name in sorted(script[kind] - manifest[kind]):
            if (svc, kind, name) not in KNOWN_DIFFERENCES:
                found.append(f'{kind} {name!r} is in start-podman.sh but NOT in the manifest')
        for name in sorted(manifest[kind] - script[kind]):
            if (svc, kind, name) not in KNOWN_DIFFERENCES:
                found.append(f'{kind} {name!r} is in the manifest but NOT in start-podman.sh')
    return found


def test_every_service_has_both_launchers():
    for sh in sorted(glob.glob(os.path.join(_HERE, '*', 'start-podman.sh'))):
        svc = os.path.basename(os.path.dirname(sh))
        assert glob.glob(os.path.join(_HERE, svc, '*pod*.yaml')), (
            f'{svc} has a start-podman.sh but no pod manifest. Either add one or '
            f'say in the script that podman play kube is not supported here.')


def test_launchers_agree():
    problems = []
    for svc, sh, yml in services():
        for d in differences(svc, script_declares(sh), manifest_declares(yml)):
            problems.append(f'{svc}: {d}')
    assert not problems, (
        'start-podman.sh and the pod manifest disagree:\n  '
        + '\n  '.join(problems)
        + '\n\nEither make them match, or add the difference to KNOWN_DIFFERENCES '
          'in this file WITH THE REASON. gateway-pod.yaml silently lacked TLS for '
          'months because nothing checked this.')


def test_known_differences_are_still_real():
    """
    An entry that no longer describes a real difference is stale.

    Kept honest deliberately: a KNOWN_DIFFERENCES list nobody prunes becomes a
    list of things that used to be true, and then it stops being read at all.
    """
    live = set()
    for svc, sh, yml in services():
        script, manifest = script_declares(sh), manifest_declares(yml)
        for kind in ('env', 'mount', 'port'):
            for name in (script[kind] ^ manifest[kind]):
                live.add((svc, kind, name))
    stale = sorted(k for k in KNOWN_DIFFERENCES if k not in live)
    assert not stale, (
        f'these KNOWN_DIFFERENCES entries no longer match any real difference '
        f'and should be deleted: {stale}')


def test_gateway_manifest_serves_tls():
    """
    The specific regression this file was written for.

    airlock.conf.template has a `listen 443 ssl` block loading
    /etc/nginx/certs/fullchain.pem. A manifest without the port, the domain and
    the certificate mount does not degrade to HTTP - nginx fails to start.
    """
    path = os.path.join(_HERE, 'gateway', 'gateway-pod.yaml')
    if not os.path.exists(path):
        return
    decl = manifest_declares(path)
    assert '443' in decl['port'], 'gateway manifest does not publish 443'
    assert 'SERVER_DOMAIN' in decl['env'], 'gateway manifest does not set SERVER_DOMAIN'
    assert '/etc/nginx/certs' in decl['mount'], (
        'gateway manifest does not mount the certificates; nginx will fail to start')


if __name__ == '__main__':
    try:
        import yaml  # noqa: F401
    except ImportError:
        print('  (pyyaml not installed - using the textual reader)\n')
    failures = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'  PASS  {name}')
            except AssertionError as exc:
                failures += 1
                print(f'  FAIL  {name}: {exc}')
    print(f'\n{"all checks passed" if not failures else f"{failures} failed"}')
    sys.exit(1 if failures else 0)
