#!/usr/bin/env python3
"""
No module defines the same top-level name twice.

    python -m pytest test_no_shadowed_definitions.py

Python keeps the last definition without a word. app.py had two
`_import_running` functions: the backups' (taking a catalog) and, added later
for date batches, one taking a connection. The second replaced the first, every
backup scheduler tick failed with "'Catalog' object has no attribute 'execute'",
and no scheduled backup ran for weeks (v6.7.1). Each test passed, because each
called its own module's name - which by then was always the second.
"""

import ast
import collections
import glob
import os

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
MODULES = sorted(
    p for p in glob.glob(os.path.join(HERE, '*.py'))
    + glob.glob(os.path.join(HERE, 'workflows', '*.py'))
    + glob.glob(os.path.join(HERE, 'workflows', 'lib', '*.py'))
    if not os.path.basename(p).startswith('test_'))


@pytest.mark.parametrize('path', MODULES, ids=lambda p: os.path.relpath(p, HERE))
def test_no_top_level_name_is_defined_twice(path):
    with open(path, encoding='utf-8') as fh:
        tree = ast.parse(fh.read(), filename=path)
    names = collections.Counter(
        node.name for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    twice = sorted(name for name, n in names.items() if n > 1)
    assert not twice, f'defined more than once, so only the last one is used: {twice}'
