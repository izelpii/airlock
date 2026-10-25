#!/usr/bin/env python3
"""
Consistency checks for workflows/defaults.json.

Implements the test the spec asks for (workflows-default-spec.md, section 9).

The failure this guards against has no other symptom. workflow_triggers resolves
a chain by matching script basenames against the workflows registered for a
catalog, and skips anything it cannot find:

    wf = registered.get(script)
    if wf is None:
        continue

So a typo in the manifest produces a workflow that appears in the UI, runs when
clicked, and is never part of any chain — with nothing logged and nothing to see.

Run directly:  python backend/test_workflow_defaults.py
Or under pytest.
"""

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import workflow_defaults as wd            # noqa: E402
from workflow_triggers import TRIGGERS, DEPENDS_ON   # noqa: E402


def _known_scripts():
    """
    Every script the trigger machinery knows, in either role.

    A script is either a chain MEMBER (a DEPENDS_ON key, or listed in a TRIGGERS
    chain) or an upstream DEPENDENCY (a DEPENDS_ON value). Ingestion workflows
    are only ever the latter — they start chains rather than being enqueued by
    them — so checking membership alone reports them as orphans incorrectly.
    """
    return (set(DEPENDS_ON)
            | {s for chain in TRIGGERS.values() for s in chain}
            | {s for deps in DEPENDS_ON.values() for s in deps})


def test_manifest_loads():
    assert wd.load_manifest(), 'manifest is empty or unreadable'


def test_every_script_exists_on_disk():
    for entry in wd.load_manifest():
        path = os.path.join(wd.WORKFLOWS_DIR, entry['script'])
        assert os.path.isfile(path), f"{entry['script']} is not in workflows/"


def test_no_duplicate_scripts():
    scripts = [e['script'] for e in wd.load_manifest()]
    assert len(scripts) == len(set(scripts)), f'duplicate scripts: {scripts}'


def test_manifest_scripts_are_known_to_the_trigger_map():
    known = _known_scripts()
    orphans = [e['script'] for e in wd.load_manifest() if e['script'] not in known]
    assert not orphans, (
        f'{orphans} are in the manifest but unknown to workflow_triggers. '
        'They would be created and then silently excluded from every chain.')


def test_trigger_map_scripts_are_all_in_the_manifest():
    manifest = {e['script'] for e in wd.load_manifest()}
    missing = sorted(_known_scripts() - manifest)
    assert not missing, (
        f'{missing} appear in the trigger map but are not in the manifest, so a '
        'new catalog would never have them and those chain links would be dead.')


def test_no_ingestion_default():
    """
    A new catalog is seeded with NO ingestion workflow.

    It used to get exactly one, unconnected, which could not run until someone
    pointed it at a folder by hand. Import workflows are now created with the
    storage route they read from, so seeding one here would leave an extra
    workflow that belongs to no route and imports nothing.
    """
    ingestion = [e for e in wd.default_entries() if e['ingestion']]
    assert not ingestion, (
        f'{[e["script"] for e in ingestion]} is seeded with a new catalog. '
        'An import workflow comes from creating a storage route; one seeded '
        'here has no source and cannot run.')


def test_needs_configuration_only_for_blank_ingestion():
    imp = 'workflows/io_picture_import_workflow.py'
    assert wd.needs_configuration({'python_script': imp, 'source_directory': ''})
    assert not wd.needs_configuration({'python_script': imp, 'source_directory': '/input'})
    assert not wd.needs_configuration(
        {'python_script': 'workflows/ev_extract_events.py', 'source_directory': ''})


def test_bad_manifest_never_raises():
    """A half-written manifest must not break catalog creation."""
    assert wd.load_manifest('/nonexistent/defaults.json') == []


if __name__ == '__main__':
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
