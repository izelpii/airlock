#!/usr/bin/env python3
"""
Every service name the gateway answers to must resolve everywhere it is used.

THE BUG THIS EXISTS FOR
-----------------------
backend-pets was built, deployed, healthy, and completely unreachable. The
gateway had a `server_name pets.airlock.lan` block and routed it correctly; the
backend container simply could not resolve the name, because the list of
--add-host flags in systems/backend/start-podman.sh had not been extended. The
symptom was not an error at startup. It was a workflow that ran to completion,
reported success, detected faces perfectly, and found no animals at all - until
the ride-along's own failure counter turned it off and said why.

transcode.airlock.lan was missing from two of the same lists, and had been for
longer, for exactly the same reason: a name lives in four files and nothing
compares them.

THE FOUR PLACES
---------------
  1. systems/gateway/airlock.conf.template  - server_name, the source of truth
  2. systems/gateway/hosts-entries.txt      - the host's /etc/hosts, which the
                                              start-all.sh health checks use
  3. systems/backend/start-podman.sh        - --add-host, how the running
                                              backend and its workflows resolve
  4. systems/backend/backend-pod.yaml       - hostAliases, the same thing for
                                              `podman play kube`

The gateway template is authoritative: a name it does not serve is not a
service, and a name it serves that nothing can resolve is this bug.
"""

import io
import os
import re

_HERE = os.path.dirname(os.path.abspath(__file__))

GATEWAY_CONF = os.path.join(_HERE, 'gateway', 'airlock.conf.template')
HOSTS_FILE = os.path.join(_HERE, 'gateway', 'hosts-entries.txt')
BACKEND_SCRIPT = os.path.join(_HERE, 'backend', 'start-podman.sh')
BACKEND_POD = os.path.join(_HERE, 'backend', 'backend-pod.yaml')

# Names the backend never calls, so it does not need to resolve them. Each one
# needs a reason, for the same reason KNOWN_DIFFERENCES in test_pod_manifests.py
# does: "someone will remember" has already been tried.
BACKEND_NEED_NOT_RESOLVE = {
    'gallery.airlock.lan':
        'the album gallery is a browser destination; no backend code calls it',
}


def _read(path):
    return io.open(path, encoding='utf-8').read()


def gateway_names():
    """
    Every literal <name>.airlock.lan the gateway serves.

    Regex server_name blocks (the per-catalog ones) are skipped on purpose:
    they match a pattern, not a name, and there is nothing to put in /etc/hosts
    for them.
    """
    names = set()
    for line in _read(GATEWAY_CONF).splitlines():
        line = line.strip()
        if not line.startswith('server_name') or line.startswith('#'):
            continue
        if '~^' in line:
            continue                       # a regex block, not a name
        for tok in re.findall(r'[a-z0-9][a-z0-9-]*\.airlock\.lan', line):
            names.add(tok)
    return names


def test_the_gateway_serves_the_names_we_think_it_does():
    """A sanity floor, so a parsing change cannot make this suite vacuous."""
    names = gateway_names()
    assert len(names) >= 8, f'only found {names} - did the template change shape?'
    for expected in ('backend.airlock.lan', 'deepface.airlock.lan',
                     'pets.airlock.lan'):
        assert expected in names, f'{expected} is not served by the gateway'

    # transcode is deliberately NOT one of them, and finding that out is what
    # this file is for. TRANSCODE_URL carries :5004 and goes straight to the
    # service, so there is no server_name block for it - which means the
    # gateway template is the source of truth for names the gateway serves,
    # not for every name that has to resolve. Hence the reverse check below.
    assert 'transcode.airlock.lan' not in names, \
        'if the gateway now fronts transcode, TRANSCODE_URL should lose its port'


def test_every_gateway_name_is_in_hosts_entries():
    """
    Without this the start-all.sh health check for that service cannot resolve
    it, and the service is reported down - or worse, never checked.
    """
    hosts = _read(HOSTS_FILE)
    missing = sorted(n for n in gateway_names() if n not in hosts)
    assert not missing, (
        f'{missing} are served by the gateway but absent from '
        f'systems/gateway/hosts-entries.txt, so nothing on the host can reach '
        f'them by name')


def test_every_gateway_name_resolves_inside_the_backend_container():
    """
    THE one that was broken. The backend and every workflow it launches resolve
    service names through these --add-host flags and nothing else.
    """
    script = _read(BACKEND_SCRIPT)
    added = set(re.findall(r'--add-host=([a-z0-9.-]+\.airlock\.lan):', script))
    missing = sorted(n for n in gateway_names()
                     if n not in added and n not in BACKEND_NEED_NOT_RESOLVE)
    assert not missing, (
        f'{missing} are served by the gateway but have no --add-host in '
        f'systems/backend/start-podman.sh. A workflow calling one gets '
        f'"Name or service not known" per image, which several of them are '
        f'built to survive quietly - so this does not fail loudly, it just '
        f'finds nothing.')


def test_every_name_the_backend_resolves_is_also_in_hosts_entries():
    """
    The other direction, and it catches the case the gateway cannot see.

    transcode.airlock.lan is reached on :5004 without the gateway, so no
    server_name mentions it and the sweep above would never notice it missing
    from the host's /etc/hosts - which it was. A name worth adding to a
    container is a name a person will type.
    """
    script = _read(BACKEND_SCRIPT)
    added = set(re.findall(r'--add-host=([a-z0-9.-]+\.airlock\.lan):', script))
    hosts = _read(HOSTS_FILE)
    missing = sorted(n for n in added if n not in hosts)
    assert not missing, (
        f'{missing} resolve inside the backend container but are absent from '
        f'systems/gateway/hosts-entries.txt, so the same name works in a '
        f'workflow and fails in a shell on the host')


def test_the_pod_manifest_resolves_the_same_names_as_the_script():
    """
    Two launchers for one service, and the manifest is the one nobody uses
    until the day they do. Same failure as the gateway manifest that predated
    TLS; see test_pod_manifests.py.
    """
    script = _read(BACKEND_SCRIPT)
    added = set(re.findall(r'--add-host=([a-z0-9.-]+\.airlock\.lan):', script))
    pod = _read(BACKEND_POD)
    aliased = set(re.findall(r'-\s*"([a-z0-9.-]+\.airlock\.lan)"', pod))
    assert added == aliased, (
        f'start-podman.sh and backend-pod.yaml disagree.\n'
        f'  only in the script:   {sorted(added - aliased)}\n'
        f'  only in the manifest: {sorted(aliased - added)}')


def test_the_pet_service_is_reachable_by_the_name_the_workflow_uses():
    """
    Pinned by name rather than left to the sweep above, because this is the
    pair that was actually broken and the workflow hard-codes the default.
    """
    from_workflow = 'pets.airlock.lan'
    assert from_workflow in gateway_names()
    assert from_workflow in _read(HOSTS_FILE)
    assert f'--add-host={from_workflow}:' in _read(BACKEND_SCRIPT)
    assert f'"{from_workflow}"' in _read(BACKEND_POD)

    wf = _read(os.path.join(_HERE, 'backend', 'workflows',
                            'ai_auto_detect_faces_workflow.py'))
    assert f'http://{from_workflow}' in wf, \
        'the ride-along no longer defaults to the name this test pins'


if __name__ == '__main__':
    import sys
    failed = 0
    for name, fn in sorted(globals().items()):
        if name.startswith('test_') and callable(fn):
            try:
                fn()
                print(f'  ok   {name}')
            except AssertionError as e:
                failed += 1
                print(f'  FAIL {name}\n       {e}')
    sys.exit(1 if failed else 0)


# ── The gateway must not take a word the application uses ───────────────────

APP_JS = os.path.join(_HERE, 'frontend', 'src', 'App.js')


def frontend_routes():
    """Top-level paths the single-page app owns, e.g. /pets, /people."""
    src = _read(APP_JS)
    out = set()
    for m in re.findall(r'path="(/[a-zA-Z0-9-]+)', src):
        out.add(m.rstrip('/'))
    return out


def gateway_path_locations():
    """
    Path prefixes the gateway proxies away, on the main server.

    Only `location /<word>/` forms matter: `location /` is the SPA fallback
    itself, and the regex/catalog blocks match hostnames rather than paths.
    """
    out = set()
    for line in _read(GATEWAY_CONF).splitlines():
        m = re.match(r'\s*location\s+(/[a-zA-Z0-9._-]+)/\s*\{', line)
        if m:
            out.add(m.group(1))
    return out


def test_the_frontend_routes_are_found():
    """A floor, so a parsing change cannot make the check below vacuous."""
    routes = frontend_routes()
    assert len(routes) > 10, routes
    for expected in ('/pets', '/people', '/photos'):
        assert expected in routes


def test_no_gateway_path_shadows_a_page():
    """
    THE bug this is for. `location /pets/ { proxy_pass ... }` was added for
    tidiness - "so the service naming is uniform ... it costs nothing" - and
    shadowed the Pets page. https://airlock.lan/pets 301'd to /pets/ and
    /pets/ answered with the pet detector's 404.

    It survived unnoticed because clicking Pets inside the app still worked:
    the router resolves that in the browser and never asks the server. Only a
    reload, a bookmark or a shared link hits nginx - so the failure is
    invisible to whoever adds it and obvious to whoever is sent the URL.

    Services are reached by NAME (<service>.airlock.lan), which costs the
    application nothing from its own path namespace.
    """
    clash = sorted(gateway_path_locations() & frontend_routes())
    assert not clash, (
        f'the gateway proxies {clash} away, but the app has a page at the '
        f'same path - reloading it will not work. Reach the service by name '
        f'instead: <service>.airlock.lan')


def test_the_pets_service_is_still_reachable_by_name():
    """Removing the path route must not have removed the service."""
    conf = _read(GATEWAY_CONF)
    assert re.search(r'server_name pets\.airlock\.lan[ ;]', conf)
    assert 'airlock_pets' in conf


def test_the_old_names_still_answer():
    """
    shoebox.lan was the domain before the rename to airlock (v5.6.4). Every
    service block answers its old name too, so nothing that still calls one
    breaks; the bare old domain and its catalog subdomains redirect, so a link
    anybody kept still opens.
    """
    conf = _read(GATEWAY_CONF)
    for name in gateway_names():
        service = name.split('.')[0]
        if name == 'airlock.lan':
            continue
        assert re.search(rf'server_name {service}\.airlock\.lan {service}\.shoebox\.lan;', conf), \
            f'{service}.shoebox.lan no longer answers'
    assert re.search(r'server_name shoebox\.lan;\s.*?return 301 https://\$\{SERVER_DOMAIN\}\$request_uri;',
                     conf, re.S), 'shoebox.lan does not redirect to the new domain'
    assert 'return 301 https://$old_catalog.airlock.lan$request_uri;' in conf, \
        'catalog subdomains of shoebox.lan do not redirect'
