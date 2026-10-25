#!/usr/bin/env python3
"""
Every subprocess workflow must report its progress and its ending.

    python -m pytest test_subprocess_progress.py

NOTHING IN THE BACKEND WRITES THOSE COLUMNS FOR A SUBPROCESS. A workflow
launched with --execution-id records its own processed_files/total_files and
its own terminal status; the parent only watches. So a workflow that forgets
is not slightly worse, it is:

  - a progress bar stuck at 0 of 0 for the whole run, and
  - an execution row left 'running' for ever once it finishes - a phantom in
    the queue that nothing clears.

ai_embed_faces.py shipped with both faults on 2026-10-09. It worked perfectly -
22 embeddings a second - and reported nothing, which is how it was noticed:
somebody watched a bar that never moved. pe_identify_pets.py, written by the
same hand a day earlier, had it right. That is exactly the kind of difference
a test should hold rather than attention.
"""

import ast
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import workflow_defaults  # noqa: E402

WORKFLOWS = os.path.join(HERE, 'workflows')


def _main_block(tree):
    """The `if __name__ == '__main__':` node, or None."""
    for node in tree.body:
        if not isinstance(node, ast.If):
            continue
        src = ast.dump(node.test)
        if "'__main__'" in src or '"__main__"' in src:
            return node
    return None


def _entry_node(tree, path):
    """
    The AST to inspect for one workflow's subprocess behaviour.

    Usually the `__main__` block. But a workflow may DELEGATE - the two
    imports are one line, `v1.main(execute_fn=execute, ...)`, sharing a helper
    precisely so that "how completion and stops are recorded must not differ
    between the two". Following that is the difference between a guard and an
    exemption: the shared helper does all of this correctly, and it should
    have to keep doing it.

    Returns (node, where) so a failure can say which file it is really about.
    """
    main = _main_block(tree)
    if main is None:
        return None, path

    calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)]
    direct = [c for c in calls
              if isinstance(c.func, ast.Name) and c.func.id == 'execute']
    if direct:
        return main, path

    # One level of delegation, local or through an imported module.
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                aliases[a.asname or a.name] = a.name
    for call in calls:
        fn = call.func
        target_module, target_name = None, None
        if isinstance(fn, ast.Attribute) and isinstance(fn.value, ast.Name):
            target_module = aliases.get(fn.value.id)
            target_name = fn.attr
        elif isinstance(fn, ast.Name):
            target_name = fn.id
        if not target_name:
            continue
        if target_module:
            other = os.path.join(WORKFLOWS, target_module + '.py')
            if not os.path.exists(other):
                continue
            otree = ast.parse(open(other, encoding='utf-8').read())
            where = other
        else:
            otree, where = tree, path
        for node in ast.walk(otree):
            if (isinstance(node, ast.FunctionDef)
                    and node.name == target_name):
                return node, where
    return main, path


def _subprocess_scripts():
    out = []
    for entry in workflow_defaults.load_manifest():
        path = os.path.join(WORKFLOWS, entry['script'])
        if not os.path.exists(path):
            continue
        tree = ast.parse(open(path, encoding='utf-8').read())
        if _main_block(tree) is not None:
            out.append((entry['script'], entry['name'], path, tree))
    return out


def test_there_are_subprocess_workflows_to_check():
    """A floor, so a parsing change cannot make this file vacuous."""
    scripts = _subprocess_scripts()
    assert len(scripts) >= 5, [s[0] for s in scripts]
    assert any(s[0] == 'ai_embed_faces.py' for s in scripts)


def _writes_progress_sql(*paths):
    """
    Does this module itself UPDATE workflow_executions.processed_files?

    The mechanism-agnostic half of the check. The imports pass
    progress_callback=None and are RIGHT to: they keep a progress object and
    write the row themselves. What matters is that the number reaches the
    database during the run, not how.

    Deliberately not satisfied by lib/stopping.record_stopped(), which also
    writes processed_files - that only runs when somebody stops the job, and a
    workflow whose progress appears solely at the moment it is cancelled is the
    bug this file is about.
    """
    for path in paths:
        src = open(path, encoding='utf-8').read()
        for chunk in src.split('UPDATE workflow_executions')[1:]:
            if 'processed_files' in chunk[:200]:
                return True
    return False


@pytest.mark.parametrize('script,name,path,tree', _subprocess_scripts(),
                         ids=lambda v: v if isinstance(v, str) else '')
def test_it_reports_progress_somehow(script, name, path, tree):
    """
    Either hand execute() a real callback, or write the row yourself. What is
    not allowed is neither - which is what ai_embed_faces.py shipped with:
    `execute(None, a.target_dir, None, None, ...)`, where the fourth
    positional is progress_callback. The run worked at 22 embeddings a second
    and the bar sat at 0 of 0 for its whole life.
    """
    main, where = _entry_node(tree, path)
    calls = [n for n in ast.walk(main)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)
             and n.func.id in ('execute', 'execute_fn')]
    if not calls:
        # Delegation resolved to a helper that calls `(execute_fn or execute)`
        # through an expression rather than a plain name.
        calls = [n for n in ast.walk(main) if isinstance(n, ast.Call)]

    passes_callback = False
    for call in calls:
        cb = next((kw.value for kw in call.keywords
                   if kw.arg == 'progress_callback'), None)
        if cb is None and len(call.args) >= 4:
            cb = call.args[3]
        if cb is None:
            continue
        if not (isinstance(cb, ast.Constant) and cb.value is None):
            passes_callback = True
            break

    if passes_callback:
        return
    assert _writes_progress_sql(path, where), (
        f'{script} ({name}) passes progress_callback=None and never writes '
        f'workflow_executions.processed_files itself, so its progress bar '
        f'stays at 0 of 0 for the whole run')


@pytest.mark.parametrize('script,name,path,tree', _subprocess_scripts(),
                         ids=lambda v: v if isinstance(v, str) else '')
def test_it_records_a_terminal_status(script, name, path, tree):
    """
    Worse than no progress: an execution left 'running' after the process is
    gone is a phantom nothing clears, and the queue believes it.
    """
    main, where = _entry_node(tree, path)
    names = {n.func.id for n in ast.walk(main)
             if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert '_db_complete' in names, \
        (f'{script} ({name}): __main__ never records a terminal status, so a '
         f'finished run stays "running" for ever')


@pytest.mark.parametrize('script,name,path,tree', _subprocess_scripts(),
                         ids=lambda v: v if isinstance(v, str) else '')
def test_a_failure_is_recorded_too(script, name, path, tree):
    """
    An unhandled exception that leaves the row 'running' is the same phantom,
    reached by the path most likely to be taken on a bad day.
    """
    main, where = _entry_node(tree, path)
    handlers = [h for n in ast.walk(main) if isinstance(n, ast.Try)
                for h in n.handlers]
    assert handlers, f'{script}: {where} does not catch anything'
    recorded = any(
        isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
        and c.func.id == '_db_complete'
        for h in handlers for c in ast.walk(h))
    assert recorded, \
        f'{script} ({name}): a crash leaves the execution row "running"'
