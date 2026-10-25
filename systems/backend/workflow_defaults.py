"""
Default workflows for a new catalog.

Implements documentation/specs/workflows-default-spec.md.

Why this exists at all: workflow_triggers.py resolves a chain by matching script
basenames against the workflows registered for that catalog, and silently skips
anything absent —

    wf = registered.get(script)
    if wf is None:
        continue

— so a catalog with no workflows has no automation. An import finishes, the
chain fires, every script is unregistered, nothing runs, and nothing reports a
problem. Seeding the defaults is what makes that machinery reachable.
"""

import json
import logging
import os

_LOG = logging.getLogger(__name__)

_HERE = os.path.dirname(os.path.abspath(__file__))
WORKFLOWS_DIR = os.path.join(_HERE, 'workflows')
MANIFEST_PATH = os.path.join(WORKFLOWS_DIR, 'defaults.json')


def load_manifest(path=None):
    """
    The manifest entries, validated. Never raises.

    Read on every call rather than cached at import: the file lives in
    workflows/, the one directory bind-mounted into the backend container, so it
    can be edited without rebuilding the image — caching would throw that away
    and keep seeding a stale set until restart. It is read once per catalog
    creation, not on a hot path.

    A malformed or missing manifest means "no defaults". Catalog creation must
    not fail because this file is being edited.
    """
    path = path or MANIFEST_PATH
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            data = json.load(fh)
    except FileNotFoundError:
        _LOG.warning('workflow defaults manifest not found at %s', path)
        return []
    except (json.JSONDecodeError, OSError) as exc:
        _LOG.warning('workflow defaults manifest at %s is unreadable: %s', path, exc)
        return []

    entries = data.get('workflows') if isinstance(data, dict) else None
    if not isinstance(entries, list):
        _LOG.warning('workflow defaults manifest has no "workflows" list')
        return []

    seen = set()
    valid = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        script = (entry.get('script') or '').strip()
        if not script:
            _LOG.warning('workflow defaults: entry with no script, skipped')
            continue

        # The script must actually exist. An entry naming a missing file would
        # otherwise create a workflow that fails only when someone runs it.
        if not os.path.isfile(os.path.join(WORKFLOWS_DIR, script)):
            _LOG.warning('workflow defaults: %s is not in %s, skipped',
                         script, WORKFLOWS_DIR)
            continue

        # Duplicates are worth shouting about: _workflows_by_script() in
        # workflow_triggers builds a dict keyed on basename, so a second row for
        # the same script is silently shadowed rather than erroring.
        if script in seen:
            _LOG.error('workflow defaults: %s listed more than once, '
                       'keeping the first', script)
            continue
        seen.add(script)

        valid.append({
            'script': script,
            'name': (entry.get('name') or '').strip() or _fallback_name(script),
            'description': (entry.get('description') or '').strip(),
            'type': (entry.get('type') or '').strip() or None,
            'ingestion': bool(entry.get('ingestion')),
            'default': bool(entry.get('default', True)),
            'order': entry.get('order', 999),
        })

    valid.sort(key=lambda e: (e['order'], e['script']))
    return valid


def _fallback_name(script):
    """The old title-cased filename, for an entry that forgot a name."""
    return script.replace('_', ' ').replace('.py', '').title()


def default_entries(path=None):
    """Only the entries marked as defaults for a new catalog."""
    return [e for e in load_manifest(path) if e['default']]


def manifest_by_script(path=None):
    """Whole manifest keyed by script basename, for enriching other responses."""
    return {e['script']: e for e in load_manifest(path)}


def is_ingestion_script(script, path=None):
    """
    True when this script needs a source_directory to do anything.

    Derived from the manifest rather than a hardcoded pair of filenames, so the
    knowledge lives in one place (spec section 4.3).
    """
    if not script:
        return False
    entry = manifest_by_script(path).get(os.path.basename(script))
    return bool(entry and entry['ingestion'])


def needs_configuration(workflow, path=None):
    """
    True for an ingestion workflow with no source_directory.

    Such a workflow currently walks nothing and reports success, so this is the
    flag both the UI and the enqueue guard key off (spec section 4.2).
    """
    script = workflow.get('python_script') or ''
    if not is_ingestion_script(script, path):
        return False
    # A storage route counts as configured: the literal source_directory is
    # derived from it at launch, so an ingestion workflow pointed at a route is
    # ready to run even with the text field empty
    # (local-storage-spec.md section 6.4).
    if workflow.get('source_route_id'):
        return False
    return not (workflow.get('source_directory') or '').strip()


def seed_default_workflows(catalog_db, path=None):
    """
    Create any missing default workflows in one catalog's database.

    Idempotent, keyed on python_script: re-running adds only what is absent,
    which is what makes the seed endpoint and the migration safe to repeat and
    makes a half-finished seed self-healing.

    Returns {'created': [names], 'skipped': [names]}.
    """
    from models import WorkflowModel

    entries = default_entries(path)
    if not entries:
        return {'created': [], 'skipped': []}

    model = WorkflowModel(catalog_db)
    existing = {os.path.basename(w.get('python_script') or '')
                for w in model.get_all()}

    created, skipped = [], []
    for entry in entries:
        if entry['script'] in existing:
            skipped.append(entry['name'])
            continue
        model.create(
            name=entry['name'],
            description=entry['description'],
            # Ingestion workflows are created UNCONFIGURED: the source directory
            # is a host path only the user knows, and a wrong value is worse than
            # an empty one because an empty one cannot run (spec section 4.1).
            source_directory='',
            python_script=f"workflows/{entry['script']}",
            workflow_type=entry['type'],
        )
        created.append(entry['name'])

    return {'created': created, 'skipped': skipped}


# ── Pet workflows: created on demand, not seeded ─────────────────────────────
# The `workflows` table has no `enabled` column, so EXISTENCE IS THE SWITCH.
# That is why the two pet entries carry "default": false in defaults.json and
# arrive through here instead - exactly how a storage route creates its import
# workflow rather than one being seeded with no source.
#
# A household with no pets therefore has no pet workflow rows at all: nothing
# to run, nothing in the queue, nothing to explain. See
# documentation/specs/pet-recognition-spec.md section 10.

PET_SCRIPTS = ('pe_identify_pets.py', 'pe_cluster_pets.py')


def create_pet_workflows(catalog_db, path=None):
    """
    Create the pet workflows for one catalog, from their defaults.json entries.

    Built from the manifest rather than from literals here so that their names,
    descriptions and types live in one place with every other workflow's - a
    second copy would drift, and the queue resolves chains by script basename
    so a typo produces a workflow that runs when clicked and is in no chain.

    Idempotent: returns the names it created, which is empty on a second call.
    """
    from models import WorkflowModel

    by_script = manifest_by_script(path)
    model = WorkflowModel(catalog_db)
    existing = {os.path.basename(w.get('python_script') or '')
                for w in model.get_all()}

    created = []
    for script in PET_SCRIPTS:
        if script in existing:
            continue
        entry = by_script.get(script)
        if not entry:
            raise KeyError(f'{script} is not in workflows/defaults.json - the '
                           f'manifest is where its name and type live')
        model.create(
            name=entry['name'],
            description=entry['description'],
            source_directory='',
            python_script=f'workflows/{script}',
            workflow_type=entry['type'],
        )
        created.append(entry['name'])
    return created


def remove_pet_workflows(catalog_db):
    """
    Delete the pet workflows for one catalog.

    The DATA IS LEFT ALONE - detections, crops, groups and named pets all
    survive. Turning the feature off is not a request to forget the household's
    animals, and switching it back on should find them where they were.

    sync_default_workflows()'s watermark means these deletions stick rather than
    being re-seeded on the next start, because the pet scripts are not defaults.
    """
    from models import WorkflowModel

    model = WorkflowModel(catalog_db)
    removed = []
    for w in model.get_all():
        if os.path.basename(w.get('python_script') or '') in PET_SCRIPTS:
            try:
                model.delete(w['id'])
                removed.append(w.get('name'))
            except Exception:
                continue
    return removed


# ── Keeping an existing catalog in step with the manifest ────────────────────
# The key in catalog_properties holding every default script this catalog has
# ever been offered, as a JSON list.
SEEN_PROPERTY = 'default_workflows_seen'


# ── Names that have changed ──────────────────────────────────────────────────
#
# A catalog stores its own copy of a workflow's name, so editing defaults.json
# renames the workflow for NEW catalogs and leaves every existing one showing
# the old word for ever. That is how a deployment ends up with two catalogs
# disagreeing about what the same script is called.
#
# The names being replaced were genuinely confusing, and cost real time on
# 2026-10-09: "Face Recognition" at 11.9 %, "Contextual face recognition"
# reporting 100 % of a far smaller denominator, and "Auto Detect Faces" at
# 98.8 % were read as one thing making inconsistent progress, when they are
# three stages with three different populations.
#
# script -> {every name it has ever shipped with}. A row is renamed only if its
# current name is one of these: anything else is a name the USER chose, and
# renaming that would be rude and would lose information.
LEGACY_NAMES = {
    'ai_auto_detect_faces_workflow.py':    {'Auto Detect Faces'},
    'ai_embed_faces.py':                   {'Embed Faces'},
    'ai_ingest_manual_faces.py':           {'Ingest Manual Faces'},
    'ai_face_recognition_workflow.py':     {'Face Recognition'},
    # ai_ml_faces_recognition_workflow.py is retired (v5.4.34): not in the
    # manifest, so there is no current name to bring an old one up to. Its row
    # is removed instead - see RETIRED_SCRIPTS.
    'ai_contextual_recognition_workflow.py': {'Contextual face recognition',
                                              'Contextual Face Recognition'},
    'pe_identify_pets.py':                 {'Identify Pets'},
    'pe_cluster_pets.py':                  {'Group Unknown Pets'},
    # These two differed only by a parenthetical - "Ingest Files" and
    # "Ingest Files (phased)" - which is the same failure in miniature. Caught
    # by test_no_name_is_a_prefix_of_another while fixing the face chain.
    'io_picture_import_v2_workflow.py':    {'Ingest Files'},
    'io_picture_import_workflow.py':       {'Ingest Files (phased)'},
}


def rename_renamed_defaults(catalog_db, path=None):
    """
    Bring a catalog's stored workflow names up to date with the manifest.

    Renames a row only when its current name is one this script has actually
    shipped with (LEGACY_NAMES). A name the user typed is left alone - there is
    no watermark for this and there does not need to be, because the condition
    is self-limiting: once renamed, the old name is gone and the row will never
    match again.

    Descriptions follow the name, because they were rewritten together and half
    an update would describe the old stage under the new title.

    Returns [(old, new)]. Never raises: this runs while answering a request
    about workflows.
    """
    from models import WorkflowModel

    changed = []
    by_script = manifest_by_script(path)
    try:
        model = WorkflowModel(catalog_db)
        rows = model.get_all()
    except Exception as exc:
        _LOG.warning('could not read workflows while renaming defaults: %s', exc)
        return changed

    for w in rows:
        script = os.path.basename(w.get('python_script') or '')
        entry = by_script.get(script)
        if not entry or script not in LEGACY_NAMES:
            continue
        current = (w.get('name') or '').strip()
        if current == entry['name'] or current not in LEGACY_NAMES[script]:
            continue
        try:
            model.update(w['id'], name=entry['name'],
                         description=entry['description'])
            changed.append((current, entry['name']))
        except Exception as exc:
            _LOG.warning('could not rename %s: %s', script, exc)
    if changed:
        _LOG.info('renamed %d default workflow(s): %s', len(changed), changed)
    return changed


# ── Workflows that have been retired ─────────────────────────────────────────
#
# Leaving the manifest stops a script being offered to NEW catalogs; every
# existing one keeps its row, and the Workflows page draws it as a card wired to
# nothing. That reads as a broken pipeline, not a retired step.
#
# script -> the version that retired it. The script itself stays in the tree.
RETIRED_SCRIPTS = {
    # Match Faces Within Events, retired from every chain in v5.4.34: Match Faces
    # by Appearance compares every face with every reference and applies its
    # same-photo rule. Its card was removed from existing catalogs in v5.5.3.
    'ai_ml_faces_recognition_workflow.py': 'v5.4.34',
}


def _has_work_in_flight(conn, workflow_id):
    """A run or queue entry that a deleted row would leave pointing at nothing."""
    checks = (
        "SELECT 1 FROM workflow_executions WHERE workflow_id = ? "
        "AND status IN ('running', 'pending') LIMIT 1",
        "SELECT 1 FROM workflow_queue WHERE workflow_id = ? "
        "AND status IN ('queued', 'blocked', 'running') LIMIT 1",
    )
    for sql in checks:
        try:
            if conn.execute(sql, (workflow_id,)).fetchone():
                return True
        except Exception:
            # A catalog without the table has nothing in it.
            continue
    return False


def remove_retired_workflows(catalog_db):
    """
    Delete the rows of retired workflows from one catalog.

    History is untouched: workflow_executions keeps its own workflow_name and has
    no foreign key to the row. A row with a run or queue entry in flight is left
    for the next call, so nothing in progress loses its workflow.

    Self-limiting like the rename: once deleted, nothing matches again, and the
    manifest no longer offers the script, so nothing brings it back.

    Returns [names]. Never raises.
    """
    from models import WorkflowModel

    removed = []
    try:
        model = WorkflowModel(catalog_db)
        rows = [w for w in model.get_all()
                if os.path.basename(w.get('python_script') or '') in RETIRED_SCRIPTS]
    except Exception as exc:
        _LOG.warning('could not read workflows while removing retired ones: %s', exc)
        return removed
    if not rows:
        return removed

    for w in rows:
        try:
            conn = catalog_db.get_connection()
            try:
                busy = _has_work_in_flight(conn, w['id'])
            finally:
                conn.close()
            if busy:
                _LOG.info('retired workflow %s still has work in flight; kept for now',
                          w.get('name'))
                continue
            model.delete(w['id'])
            removed.append(w.get('name'))
        except Exception as exc:
            _LOG.warning('could not remove retired workflow %s: %s', w.get('name'), exc)
    if removed:
        _LOG.info('removed %d retired workflow(s): %s', len(removed), removed)
    return removed


def sync_default_workflows(catalog_db, path=None):
    """
    Add defaults this catalog has never been offered, and nothing else.

    seed_default_workflows() adds whatever is missing, which is right for an
    explicit "add the defaults" action and wrong to run on its own: a workflow
    somebody deliberately deleted is missing, so every call would put it back.

    So this keeps a watermark - the set of default scripts this catalog has been
    offered - in catalog_properties, the table that already exists for derived
    per-catalog values. A script in the manifest but not in the watermark is NEW
    and gets created. A script in the watermark is left alone whether it is
    present or not, so a deletion sticks.

    The reason it has to be automatic at all: a workflow that is not registered
    for a catalog is SKIPPED BY EVERY TRIGGER CHAIN, silently and by design
    (see this module's own docstring). Adding a workflow to the manifest
    therefore does nothing for any catalog that already existed, and the
    pipeline drawing looks complete without it. That is not a state a person can
    be expected to notice.

    ONE EXCEPTION, on the first run for a catalog that predates the watermark:
    there is no record, so "never offered" and "offered and deleted" are
    indistinguishable, and this creates what is missing. A default somebody had
    removed before this existed comes back once, visibly, and can be removed
    again - after which the watermark remembers. The alternative, bootstrapping
    the watermark from what is present and creating nothing, would mean the
    catalogs that most need a newly added workflow never get it.

    Returns {'created': [names], 'skipped': [names]}. Never raises: this runs
    while answering a request about workflows, and a catalog whose database is
    momentarily locked must list its workflows rather than fail.
    """
    from models import WorkflowModel, get_catalog_property, set_catalog_property

    result = {'created': [], 'skipped': []}
    entries = default_entries(path)
    if not entries:
        return result

    db_path = getattr(catalog_db, 'db_path', None)
    if not db_path:
        return result

    try:
        raw = get_catalog_property(db_path, SEEN_PROPERTY)
        seen = set(json.loads(raw)) if raw else set()
    except (ValueError, TypeError):
        seen = set()

    # Before the early return below, and that placement is the point: a
    # catalog that has already been offered every script takes that return,
    # and a rename placed after it would reach no existing catalog at all -
    # which is every catalog this change is for.
    result['renamed'] = rename_renamed_defaults(catalog_db, path)
    # Same reason it sits above the return.
    result['retired'] = remove_retired_workflows(catalog_db)

    manifest_scripts = {e['script'] for e in entries}
    todo = [e for e in entries if e['script'] not in seen]
    if not todo:
        return result

    try:
        model = WorkflowModel(catalog_db)
        existing = {os.path.basename(w.get('python_script') or '')
                    for w in model.get_all()}
    except Exception as exc:
        _LOG.warning('could not read workflows while syncing defaults: %s', exc)
        return result

    for entry in todo:
        if entry['script'] in existing:
            result['skipped'].append(entry['name'])
            continue
        try:
            model.create(
                name=entry['name'],
                description=entry['description'],
                source_directory='',
                python_script=f"workflows/{entry['script']}",
                workflow_type=entry['type'],
            )
            result['created'].append(entry['name'])
            _LOG.info('added default workflow %s to %s', entry['name'], db_path)
        except Exception as exc:
            _LOG.warning('could not add default workflow %s: %s', entry['name'], exc)

    # The watermark covers the WHOLE manifest, not only what was created: a
    # script that was already present has plainly been offered, and recording it
    # is what stops a later deletion being undone.
    try:
        set_catalog_property(db_path, SEEN_PROPERTY,
                             json.dumps(sorted(seen | manifest_scripts)))
    except Exception as exc:
        # Not fatal, but it does mean the next call tries again. Logged so a
        # catalog that keeps re-adding a deleted workflow is explicable.
        _LOG.warning('could not record the default-workflow watermark: %s', exc)

    return result

# ── Is the deployment internally consistent? ─────────────────────────────────

def check_manifest_against_wiring():
    """
    Does the MOUNTED manifest agree with the COMPILED wiring? Returns problems.

    THE DEPLOYMENT SHAPE THAT MAKES THIS NECESSARY. systems/backend/workflows is
    bind-mounted into the container; everything else in the backend is baked
    into the image. So the two halves of a workflow definition arrive by
    different routes and can be different versions:

        workflows/defaults.json    mounted  - live the moment it is edited
        workflows/<script>.py      mounted  - live
        workflow_triggers.py       baked    - needs a rebuild
        workflow_scheduler.py      baked    - needs a rebuild

    Adding a workflow therefore half-deploys by itself. The row is created, it
    appears in the UI, it can be clicked - and it is in no trigger chain, has
    no write set and no stop mode, because the maps that hold those are still
    the previous image. That is not "not deployed yet"; it is the silent-skip
    failure this module's docstring warns about, arriving through the
    deployment instead of through a typo. It happened on 2026-10-09 and was
    noticed only because somebody looked at the pipeline canvas and saw a node
    with no edges.

    test_locations.py asserts these same invariants against the repository.
    This asserts them against the RUNNING SYSTEM, where the two halves can
    disagree however correct the repository is.
    """
    import workflow_scheduler as sched
    import workflow_triggers as trig

    problems = []
    try:
        entries = load_manifest()
    except Exception as exc:
        return [f'could not read workflows/defaults.json: {exc}']

    manifest_scripts = {e['script'] for e in entries}
    in_a_chain = {s for scripts in trig.TRIGGERS.values() for s in scripts}

    for entry in entries:
        script = entry['script']
        if script in trig.INGEST_SCRIPTS:
            continue          # started by a person or a schedule, never chained
        if script not in in_a_chain:
            problems.append(
                f"{script} ({entry['name']}) is in defaults.json but in no "
                f"trigger chain, so it will only ever run when clicked")
        if sched.write_set(script) is None:
            problems.append(f'{script} has no write set, so the queue assumes '
                            f'it conflicts with everything')
        if sched.stop_mode(script) not in ('item', 'never'):
            problems.append(f'{script} has no stop mode, so a running one '
                            f'cannot be stopped')

    # The other direction: the baked maps naming something the mount does not
    # have. That is the image being NEWER than workflows/, which happens when a
    # rebuild lands before a pull.
    for script in sorted(in_a_chain - manifest_scripts):
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'workflows', script)
        if not os.path.exists(path):
            problems.append(f'{script} is named in a trigger chain but is not '
                            f'in workflows/ at all')
    return problems


def report_manifest_wiring(logger=None):
    """
    Say loudly, once, at startup, if the two halves disagree.

    Loud on purpose. The symptom without this is a workflow that looks present
    and quietly never runs, and the only way anyone found it was by noticing an
    unconnected node on a canvas.
    """
    try:
        problems = check_manifest_against_wiring()
    except Exception as exc:
        _LOG.warning('could not check the workflow wiring: %s', exc)
        return []
    if not problems:
        return []
    say = (logger or _LOG).error
    say('=' * 72)
    say('WORKFLOW WIRING MISMATCH - the backend image is probably older than '
        'systems/backend/workflows/')
    say('')
    for p in problems:
        say(f'  - {p}')
    say('')
    say('  workflows/ is bind-mounted and the rest of the backend is baked in,')
    say('  so a new workflow appears and does nothing until the image is')
    say('  rebuilt:   ./start-all.sh --build=backend')
    say('=' * 72)
    return problems
