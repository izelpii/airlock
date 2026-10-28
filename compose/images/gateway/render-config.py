#!/usr/bin/env python3
"""
The compose gateway's nginx configuration, made from the application's.

    python3 render-config.py <airlock.conf.template> > <compose template>

The application's template (systems/gateway/airlock.conf.template) sends every
upstream to ONE host - the machine itself, where each service publishes a port:

    upstream airlock_backend { server ${UPSTREAM_HOST}:5000; }

On a compose network each service has a name of its own and publishes nothing,
so each upstream is rewritten to its service, re-resolved at runtime:

    upstream airlock_backend { zone airlock_backend 64k; server backend:5000 resolve; }

`resolve` matters twice over. A container that restarts comes back with a new
address, which a name resolved once at start would never notice; and a service
that is not running (an optional one, or one still starting) does not stop
nginx from starting, as an unresolvable static name would - that path answers
502 until it appears.

Everything else - server blocks, TLS, the ACME path, the per-catalog gallery
hosts - is the application's, byte for byte. Anything this script does not
recognise stops the build: a new upstream added to the application must be
mapped here, or the compose gateway would route it nowhere.
"""

import re
import sys

# The application's upstream name -> the compose service that serves it.
SERVICES = {
    'airlock_frontend': 'frontend',
    'airlock_gallery': 'gallery',
    'airlock_backend': 'backend',
    'airlock_deepface': 'deepface',
    'airlock_geoloc': 'geoloc',
    'airlock_vision': 'vision',
    'airlock_albums': 'albums',
    'airlock_pets': 'pets',
}
# Ollama is outside the stack by default (on the host), so its address is
# decided when the container starts: 16-airlock-ollama.envsh.
OLLAMA = 'airlock_ollama'

UPSTREAM = re.compile(
    r'^upstream\s+(?P<name>\w+)\s*\{\s*server\s+\$\{UPSTREAM_HOST\}:(?P<port>\d+);\s*\}\s*$')

RESOLVER = ('# Compose: names are resolved through the container\'s own DNS server\n'
            '# (Docker: 127.0.0.11; podman: aardvark), read from resolv.conf at start.\n'
            'resolver ${NGINX_LOCAL_RESOLVERS} valid=10s ipv6=off;\n\n')


def render(template):
    out, seen, first = [], set(), True
    for line in template.splitlines(keepends=True):
        m = UPSTREAM.match(line.rstrip('\n'))
        if not m:
            out.append(line)
            continue
        name, port = m.group('name'), m.group('port')
        if first:
            out.append(RESOLVER)
            first = False
        if name == OLLAMA:
            out.append(f'upstream {name} {{ zone {name} 64k; server ${{AIRLOCK_OLLAMA_SERVER}}; }}\n')
        elif name in SERVICES:
            out.append(f'upstream {name} {{ zone {name} 64k; server {SERVICES[name]}:{port} resolve; }}\n')
        else:
            raise SystemExit(f'render-config: upstream {name!r} has no compose service - '
                             'add it to SERVICES in compose/images/gateway/render-config.py')
        seen.add(name)

    text = ''.join(out)
    missing = (set(SERVICES) | {OLLAMA}) - seen
    if missing:
        raise SystemExit(f'render-config: the template no longer has {sorted(missing)}; '
                         'update SERVICES to match the application')
    if any('${UPSTREAM_HOST}' in line.split('#', 1)[0] for line in text.splitlines()):
        raise SystemExit('render-config: ${UPSTREAM_HOST} is used outside an upstream block; '
                         'the compose gateway does not know where that should point')
    return text


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    with open(sys.argv[1], encoding='utf-8') as fh:
        sys.stdout.write(render(fh.read()))
