#!/usr/bin/env python3
"""
Module-level code runs top to bottom, so a constant used above its definition
is a NameError at import.

This has now happened twice:

  ai_ml_faces_recognition_workflow.py - a threshold computed from
  embedding_models before the import that provided it. Caught by running the
  workflow the way the scheduler does.

  backend-deepface/app.py:305 - `for _model in WARMUP_MODELS:` in the start-up
  block, with WARMUP_MODELS defined 24 lines further down next to the endpoints
  that use it. Nothing in the suite imports that file, so it crashed every
  gunicorn worker on the first real start instead:
      NameError: name 'WARMUP_MODELS' is not defined

Both were invisible to the suite because importing those modules needs
TensorFlow, a catalog, or `workflows/` on sys.path. This checks the ordering
statically instead, with no imports at all.

Deliberately narrow: a name is only reported when it IS bound at module level
LATER in the same file. An unknown name is somebody else's problem (eslint for
the frontend, the runtime for a genuine typo); an out-of-order one is this
bug, and precision here is what keeps the test quiet enough to trust.
"""

import ast
import io
import os

import pytest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Files whose module level does real work at import: service entry points and
# the workflows the scheduler launches as scripts.
TARGETS = [
    'systems/backend-deepface/app.py',
    'systems/backend-vision/app.py',
    'systems/backend-geoloc/app.py',
    'systems/backend-albums/app.py',
    'systems/backend-transcode/app.py',
    'systems/backend/app.py',
    'systems/backend/embedding_models.py',
    'systems/backend/face_recognition_schema.py',
    'systems/backend/location_schema.py',
    'systems/backend/version.py',
    'systems/backend/workflow_scheduler.py',
    'systems/backend/workflow_triggers.py',
]
TARGETS += [f'systems/backend/workflows/{f}' for f in sorted(
    os.listdir(os.path.join(REPO, 'systems', 'backend', 'workflows')))
    if f.endswith('.py')]


def _bound_by(node):
    """Every module-level name this statement binds."""
    out = set()
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        out.add(node.name)
    elif isinstance(node, (ast.Import, ast.ImportFrom)):
        for a in node.names:
            out.add((a.asname or a.name).split('.')[0])
    elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
        targets = node.targets if isinstance(node, ast.Assign) else [node.target]
        for t in targets:
            for n in ast.walk(t):
                if isinstance(n, ast.Name):
                    out.add(n.id)
    elif isinstance(node, (ast.For, ast.AsyncFor)):
        for n in ast.walk(node.target):
            if isinstance(n, ast.Name):
                out.add(n.id)
    elif isinstance(node, (ast.With, ast.AsyncWith)):
        for item in node.items:
            if item.optional_vars is not None:
                for n in ast.walk(item.optional_vars):
                    if isinstance(n, ast.Name):
                        out.add(n.id)
    elif isinstance(node, ast.Try):
        for h in node.handlers:
            if h.name:
                out.add(h.name)
    return out


def _loads_now(node):
    """
    Names this statement READS when the module is imported.

    Three scopes are skipped, because their code runs later or elsewhere:

      function and class BODIES - called after the module is fully bound.
      Their decorators and argument defaults are NOT skipped: those are
      evaluated at definition time, which is import time.

      lambdas - same reason.

      comprehensions - these have their own scope in Python 3, so `name` in
      `{name for name, decl in COLS}` is local to the comprehension and not a
      module-level read. Missing this flagged four such variables as
      out-of-order on the first run of this test. Note the one asymmetry: the
      FIRST generator's iterable is evaluated in the enclosing scope, so it is
      walked without the comprehension's own bindings.
    """
    found = []

    def walk(n, shadowed):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in n.decorator_list:
                walk(d, shadowed)
            for d in n.args.defaults + [x for x in n.args.kw_defaults if x]:
                walk(d, shadowed)
            return
        if isinstance(n, ast.ClassDef):
            for d in n.decorator_list:
                walk(d, shadowed)
            for b in n.bases:
                walk(b, shadowed)
            return
        if isinstance(n, ast.Lambda):
            return
        if isinstance(n, (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)):
            inner = set(shadowed)
            for gen in n.generators:
                for t in ast.walk(gen.target):
                    if isinstance(t, ast.Name):
                        inner.add(t.id)
            for i, gen in enumerate(n.generators):
                walk(gen.iter, shadowed if i == 0 else inner)
                for cond in gen.ifs:
                    walk(cond, inner)
            parts = ([n.key, n.value] if isinstance(n, ast.DictComp) else [n.elt])
            for part in parts:
                walk(part, inner)
            return
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Load):
            if n.id not in shadowed:
                found.append((n.id, n.lineno))
            return
        for child in ast.iter_child_nodes(n):
            walk(child, shadowed)

    walk(node, frozenset())
    return found


def _module_level_bindings(stmt):
    """
    Names this top-level statement binds AT MODULE SCOPE.

    Descends into if/try/for/with bodies, because those execute at import and
    bind in the module namespace - but NOT into function or class bodies, whose
    assignments are local to them. Conflating the two made every `for name, ...`
    inside a function look like a late module-level binding.
    """
    out = set()

    def walk(n):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(n.name)         # the def itself binds a module-level name
            return
        if isinstance(n, ast.Lambda):
            return
        out.update(_bound_by(n))
        for child in ast.iter_child_nodes(n):
            walk(child)

    walk(stmt)
    return out


def offenders(source, rel='<test>'):
    """Out-of-order module-level reads in `source`. Empty when it is fine."""
    tree = ast.parse(source, filename=rel)

    defined_at = {}
    for stmt in tree.body:
        for name in _module_level_bindings(stmt):
            defined_at.setdefault(name, stmt.lineno)

    bound = set()
    found = []
    for stmt in tree.body:
        for name, lineno in _loads_now(stmt):
            if name in bound:
                continue
            later = defined_at.get(name)
            if later is not None and later > stmt.lineno:
                found.append(f'{rel}:{lineno}: {name!r} is read here but only '
                             f'bound at line {later}')
        bound |= _module_level_bindings(stmt)
    return found


# ── Does the check actually catch the bug it was written for? ────────────────
#
# Asked because the first attempt to verify it by reverting the real file
# silently failed: moving a block that had already been fixed preserved the
# order, the test passed, and that looked like proof. These are the two cases
# that matter, stated outright.

BROKEN = """
import os

if os.environ.get('WARM', '1') == '1':
    for _m in WARMUP_MODELS:
        print(_m)

WARMUP_MODELS = ['SFace']
"""

FIXED = """
import os

WARMUP_MODELS = ['SFace']

if os.environ.get('WARM', '1') == '1':
    for _m in WARMUP_MODELS:
        print(_m)
"""


def test_the_check_catches_a_late_definition():
    found = offenders(BROKEN)
    assert found, 'the check missed exactly the bug it exists for'
    assert "'WARMUP_MODELS'" in found[0]
    assert ':5:' in found[0], f'wrong line reported: {found[0]}'


def test_the_check_is_quiet_when_the_order_is_right():
    assert offenders(FIXED) == []


def test_a_comprehension_variable_is_not_a_module_level_read():
    """
    `name` here is local to the comprehension, and is also a loop variable
    inside a function further down. Flagging it produced four false positives
    on the first run.
    """
    assert offenders("""
COLS = (('a', 'TEXT'),)
ADDABLE = {name for name, decl in COLS if 'x' not in decl}

def f():
    for name, decl in COLS:
        print(name, decl)
""") == []


def test_a_function_body_name_is_not_a_module_level_binding():
    assert offenders("""
def f():
    total = 1
    return total

print(f())
""") == []


@pytest.mark.parametrize('rel', TARGETS)
def test_module_level_names_are_defined_before_use(rel):
    path = os.path.join(REPO, rel)
    if not os.path.exists(path):
        pytest.skip(f'{rel} not present')
    found = offenders(io.open(path, encoding='utf-8').read(), rel)

    assert not found, (
        'module-level code runs top to bottom - these are NameErrors at '
        'import:\n  ' + '\n  '.join(found))
