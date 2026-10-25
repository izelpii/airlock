#!/usr/bin/env python3
"""
Workflow names, and getting them onto catalogs that already exist.

    python -m pytest test_workflow_renames.py

WHY THE OLD NAMES WERE A PROBLEM. On 2026-10-09 three workflows were read as
one thing making inconsistent progress:

    Auto Detect Faces              98.8 %   of 301,025 files
    Face Recognition               11.9 %   of 469,847 faces
    Contextual face recognition   100.0 %   of 55,981 - the embedded subset

Three stages, three populations, three nearly identical names. "I already ran
the face recognition workflow and got almost to 99%" was a reasonable thing to
conclude and was about a different workflow.
"""

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import workflow_defaults                                   # noqa: E402
from models import CatalogDatabase, WorkflowModel          # noqa: E402


def _scripts_to_names():
    return {e['script']: e['name'] for e in workflow_defaults.load_manifest()}


def test_the_face_chain_reads_as_a_sequence():
    """
    Each name should say what that stage DOES, so the order is legible without
    opening anything.
    """
    names = _scripts_to_names()
    assert names['ai_auto_detect_faces_workflow.py'] == 'Find Faces'
    assert names['ai_embed_faces.py'] == 'Measure Faces'
    assert names['ai_ingest_manual_faces.py'] == 'Learn Tagged Faces'
    assert names['ai_face_recognition_workflow.py'] == 'Match Faces by Appearance'
    # Match Faces Within Events is retired (v5.4.34) - see the next test.
    assert 'ai_ml_faces_recognition_workflow.py' not in names
    assert names['ai_contextual_recognition_workflow.py'] == 'Re-rank Matches by Context'


def test_no_two_workflows_share_a_name():
    """
    Two rows with one name is unresolvable in the UI, and the queue shows names
    rather than scripts.
    """
    names = [e['name'] for e in workflow_defaults.load_manifest()]
    dupes = {n for n in names if names.count(n) > 1}
    assert not dupes, f'duplicate workflow names: {dupes}'


def test_no_name_is_a_prefix_of_another():
    """
    "Face Recognition" and "Additional ML Face Recognition" were distinguished
    only by a prefix, which is exactly how two lines in a list get conflated.
    """
    names = sorted(e['name'] for e in workflow_defaults.load_manifest())
    bad = [(a, b) for a in names for b in names
           if a != b and b.lower().startswith(a.lower())]
    assert not bad, f'one name contains another: {bad}'


def test_every_legacy_name_maps_to_a_real_script():
    """A typo here renames nothing, silently."""
    scripts = _scripts_to_names()
    for script in workflow_defaults.LEGACY_NAMES:
        assert script in scripts, f'{script} is not in the manifest'


def test_no_legacy_name_is_still_in_use():
    """
    If a current name also appears in LEGACY_NAMES, the rename pass would
    rename a row to itself for ever, or worse, flap between two names.
    """
    current = set(_scripts_to_names().values())
    for script, olds in workflow_defaults.LEGACY_NAMES.items():
        clash = current & olds
        assert not clash, f'{script}: {clash} is both a current and a legacy name'


@pytest.fixture
def existing_catalog(tmp_path):
    """A catalog created before the rename, with the OLD names stored."""
    target = tmp_path / 'cat'
    target.mkdir()
    cdb = CatalogDatabase(str(target))
    model = WorkflowModel(cdb)
    model.create(name='Face Recognition', description='old text',
                 source_directory='',
                 python_script='workflows/ai_face_recognition_workflow.py',
                 workflow_type='AI / ML')
    model.create(name='Auto Detect Faces', description='old text',
                 source_directory='',
                 python_script='workflows/ai_auto_detect_faces_workflow.py',
                 workflow_type='AI / ML')
    # ...and one the user renamed themselves.
    model.create(name='Dad photos - group the animals', description='mine',
                 source_directory='',
                 python_script='workflows/pe_cluster_pets.py',
                 workflow_type='AI / ML')
    return cdb


def test_an_existing_catalog_is_renamed(existing_catalog):
    changed = workflow_defaults.rename_renamed_defaults(existing_catalog)
    got = {old: new for old, new in changed}
    assert got.get('Face Recognition') == 'Match Faces by Appearance'
    assert got.get('Auto Detect Faces') == 'Find Faces'

    names = {os.path.basename(w['python_script']): w['name']
             for w in WorkflowModel(existing_catalog).get_all()}
    assert names['ai_face_recognition_workflow.py'] == 'Match Faces by Appearance'
    assert names['ai_auto_detect_faces_workflow.py'] == 'Find Faces'


def test_a_name_the_user_chose_is_left_alone(existing_catalog):
    """
    The condition is "its current name is one we shipped", not "its script is
    one we renamed". Anything else is the user's and renaming it would lose
    what they meant.
    """
    workflow_defaults.rename_renamed_defaults(existing_catalog)
    names = {os.path.basename(w['python_script']): w['name']
             for w in WorkflowModel(existing_catalog).get_all()}
    assert names['pe_cluster_pets.py'] == 'Dad photos - group the animals'


def test_renaming_twice_changes_nothing_the_second_time(existing_catalog):
    """
    Self-limiting without a watermark: once renamed, the old name is gone and
    the row can never match again. This runs on every workflow listing, so it
    has to be free when there is nothing to do.
    """
    first = workflow_defaults.rename_renamed_defaults(existing_catalog)
    assert first
    assert workflow_defaults.rename_renamed_defaults(existing_catalog) == []


def test_the_description_follows_the_name(existing_catalog):
    """Half an update describes the old stage under the new title."""
    workflow_defaults.rename_renamed_defaults(existing_catalog)
    row = next(w for w in WorkflowModel(existing_catalog).get_all()
               if w['python_script'].endswith('ai_face_recognition_workflow.py'))
    assert row['description'] != 'old text'
    assert 'unnamed face' in row['description'].lower()


def test_sync_renames_even_when_there_is_nothing_new_to_add(existing_catalog):
    """
    THE placement bug this is here to prevent. sync_default_workflows returns
    early when every script has already been offered - which is true of every
    catalog that predates the rename, i.e. all of them. A rename after that
    return would reach nobody.
    """
    from models import set_catalog_property
    seen = [e['script'] for e in workflow_defaults.load_manifest()]
    set_catalog_property(existing_catalog.db_path,
                         workflow_defaults.SEEN_PROPERTY, json.dumps(seen))

    out = workflow_defaults.sync_default_workflows(existing_catalog)
    assert out['created'] == [], 'nothing new should be added here'
    assert out.get('renamed'), 'the early return skipped the rename'


# ── The half-deployed state ─────────────────────────────────────────────────

def test_the_wiring_check_passes_on_a_correct_tree():
    """
    Whatever else it does, it must be silent when the repository is right -
    otherwise it is noise and will be ignored the day it matters.
    """
    assert workflow_defaults.check_manifest_against_wiring() == []


def test_it_notices_a_workflow_the_compiled_maps_have_never_heard_of(monkeypatch):
    """
    THE case it exists for: workflows/ is bind-mounted and the maps are baked
    into the image, so a new workflow appears, is created for every catalog,
    can be clicked - and is in no trigger chain. On 2026-10-09 that state
    shipped and was noticed only because somebody saw an unconnected node on
    the pipeline canvas.
    """
    import workflow_scheduler as sched
    import workflow_triggers as trig

    # The mounted manifest knows about it...
    entries = workflow_defaults.load_manifest() + [{
        'script': 'ai_brand_new.py', 'name': 'Brand New',
        'description': 'x', 'type': 'AI / ML', 'default': True, 'order': 99,
    }]
    monkeypatch.setattr(workflow_defaults, 'load_manifest', lambda path=None: entries)
    # ...and the baked maps do not. (They are unchanged - that IS the fault.)
    assert 'ai_brand_new.py' not in {s for v in trig.TRIGGERS.values() for s in v}
    assert sched.write_set('ai_brand_new.py') is None

    problems = workflow_defaults.check_manifest_against_wiring()
    joined = ' '.join(problems)
    assert 'ai_brand_new.py' in joined
    assert 'no trigger chain' in joined, problems
    assert 'write set' in joined, problems
    assert 'stop mode' in joined, problems


def test_it_notices_the_image_being_NEWER_than_the_mount(monkeypatch):
    """
    The other direction, which happens when a rebuild lands before a pull: the
    baked chain names a script that workflows/ does not contain.
    """
    import workflow_triggers as trig
    patched = dict(trig.TRIGGERS)
    patched['import_completed'] = list(patched['import_completed']) + ['ai_vanished.py']
    monkeypatch.setattr(trig, 'TRIGGERS', patched)

    problems = workflow_defaults.check_manifest_against_wiring()
    assert any('ai_vanished.py' in p and 'not in workflows/' in p
               for p in problems), problems


def test_an_ingest_script_is_not_reported(monkeypatch):
    """
    Imports are started by a person or a schedule and are deliberately in no
    chain. Reporting them would make the check cry wolf every startup.
    """
    problems = workflow_defaults.check_manifest_against_wiring()
    assert not any('io_picture_import' in p for p in problems), problems



def test_match_faces_within_events_is_retired_not_deleted():
    """
    Out of the manifest and every chain: Match Faces by Appearance compares
    every face with every reference, and applies the same-photo rule itself.
    Run after it, the two undid each other's rows. The script stays in the
    tree; existing catalogs lose the row (see the tests below).
    """
    import workflow_triggers as trig
    script = 'ai_ml_faces_recognition_workflow.py'
    assert script not in _scripts_to_names()
    assert not any(script in chain for chain in trig.TRIGGERS.values())
    assert script not in trig.DEPENDS_ON
    assert not any(script in deps for deps in trig.DEPENDS_ON.values())
    assert os.path.exists(os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                       'workflows', script))


# ── Retired workflows leave existing catalogs ───────────────────────────────

def _add_retired(cdb, name='Match Faces Within Events'):
    return WorkflowModel(cdb).create(
        name=name, description='old', source_directory='',
        python_script='workflows/ai_ml_faces_recognition_workflow.py',
        workflow_type='AI / ML')


def _scripts(cdb):
    return {os.path.basename(w['python_script'])
            for w in WorkflowModel(cdb).get_all()}


def test_a_retired_workflow_is_removed_from_an_existing_catalog(existing_catalog):
    """Otherwise the Workflows page draws it as a card wired to nothing."""
    _add_retired(existing_catalog)
    removed = workflow_defaults.remove_retired_workflows(existing_catalog)
    assert removed == ['Match Faces Within Events']
    assert 'ai_ml_faces_recognition_workflow.py' not in _scripts(existing_catalog)
    assert 'ai_face_recognition_workflow.py' in _scripts(existing_catalog)
    assert workflow_defaults.remove_retired_workflows(existing_catalog) == []


def test_its_run_history_is_kept(existing_catalog):
    wid = _add_retired(existing_catalog)
    conn = existing_catalog.get_connection()
    conn.execute("INSERT INTO workflow_executions (workflow_id, workflow_name, status) "
                 "VALUES (?, 'Match Faces Within Events', 'completed')", (wid,))
    conn.commit()
    conn.close()
    workflow_defaults.remove_retired_workflows(existing_catalog)
    conn = existing_catalog.get_connection()
    n = conn.execute('SELECT COUNT(*) FROM workflow_executions WHERE workflow_id = ?',
                     (wid,)).fetchone()[0]
    conn.close()
    assert n == 1


def test_one_still_running_is_left_until_it_stops(existing_catalog):
    wid = _add_retired(existing_catalog)
    conn = existing_catalog.get_connection()
    conn.execute("INSERT INTO workflow_executions (workflow_id, workflow_name, status) "
                 "VALUES (?, 'Match Faces Within Events', 'running')", (wid,))
    conn.commit()
    conn.close()
    assert workflow_defaults.remove_retired_workflows(existing_catalog) == []
    assert 'ai_ml_faces_recognition_workflow.py' in _scripts(existing_catalog)


def test_sync_removes_it_even_when_there_is_nothing_new_to_add(existing_catalog):
    """The same early-return trap as the rename."""
    from models import set_catalog_property
    seen = [e['script'] for e in workflow_defaults.load_manifest()]
    set_catalog_property(existing_catalog.db_path,
                         workflow_defaults.SEEN_PROPERTY, json.dumps(seen))
    _add_retired(existing_catalog, name='Additional ML Face Recognition')
    out = workflow_defaults.sync_default_workflows(existing_catalog)
    assert out.get('retired') == ['Additional ML Face Recognition']


def test_no_retired_script_is_still_in_the_manifest():
    assert not set(workflow_defaults.RETIRED_SCRIPTS) & set(_scripts_to_names())
