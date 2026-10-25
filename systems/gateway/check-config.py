#!/usr/bin/env python3
"""
Structural check of the gateway nginx config, for environments without nginx.

`nginx -t` is the real test and start-podman.sh runs it inside the image before
starting. This catches the class of error that produced
"[emerg] proxy_read_timeout directive is duplicate" without needing nginx: a
single-value directive set twice in the same context, counting what
`include proxy.conf` pulls in.

    python3 check-config.py
"""

import os
import re
import sys
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))

# Directives that may legitimately repeat in one context (they append).
ADDITIVE = {
    'proxy_set_header', 'add_header', 'proxy_hide_header', 'include', 'set',
    'server', 'location', 'upstream', 'listen', 'server_name', 'map',
    'rewrite', 'return', 'access_log', 'error_log',
}


def strip_comments(text):
    return '\n'.join(l for l in text.split('\n') if not l.strip().startswith('#'))


def parse(text):
    """
    Return [(label, [directive names])] for every brace-delimited block.

    Each block gets a unique id: "location /" appears in eight server blocks and
    they are eight separate contexts, not one. Keying by label alone reports
    every one of them as a duplicate of the others.
    """
    text = strip_comments(text)
    blocks = []                 # [label, [directives]]
    stack = []                  # indices into blocks
    buf = []
    for ch in text:
        if ch == '{':
            label = ' '.join(''.join(buf).split())
            blocks.append([label or '<anonymous>', []])
            stack.append(len(blocks) - 1)
            buf = []
        elif ch == '}':
            if stack:
                stack.pop()
            buf = []
        elif ch == ';':
            stmt = ' '.join(''.join(buf).split())
            if stmt:
                target = blocks[stack[-1]][1] if stack else top_level
                target.append(stmt.split()[0])
            buf = []
        else:
            buf.append(ch)
    return blocks


top_level = []


def main():
    tpl = open(os.path.join(HERE, 'airlock.conf.template')).read()
    inc = [l.strip().split()[0] for l in
           strip_comments(open(os.path.join(HERE, 'proxy.conf')).read()).split('\n')
           if l.strip()]
    inc_single = [d for d in inc if d not in ADDITIVE]

    if tpl.count('{') != tpl.count('}'):
        print(f'FAIL braces unbalanced: {tpl.count("{")} open, {tpl.count("}")} close')
        return 1
    print(f'ok   braces balanced ({tpl.count("{")} blocks)')
    print(f'ok   proxy.conf contributes single-value directives: {inc_single or "none"}')

    problems = 0
    for label, directives in parse(tpl):
        expanded = list(directives)
        if 'include' in directives:
            expanded += inc_single
        for name, count in Counter(expanded).items():
            if count > 1 and name not in ADDITIVE:
                print(f'FAIL "{name}" set {count}x in one context: {label}')
                problems += 1

    if problems:
        print(f'\n{problems} duplicate-directive problem(s) - nginx would refuse to start')
        return 1

    # Every upstream referenced must be declared.
    declared = set(re.findall(r'^upstream\s+(\S+)', strip_comments(tpl), re.M))
    used = set(re.findall(r'proxy_pass\s+http://([A-Za-z0-9_]+)', tpl))
    missing = used - declared
    print(f'ok   upstreams declared: {len(declared)}, referenced: {len(used)}')
    if missing:
        print(f'FAIL proxy_pass to undeclared upstream(s): {sorted(missing)}')
        return 1

    names = re.findall(r'server_name\s+([^;]+);', tpl)
    print(f'ok   server_name entries: {sum(len(n.split()) for n in names)}')
    print('\nStructural check passed. nginx -t still runs in start-podman.sh.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
