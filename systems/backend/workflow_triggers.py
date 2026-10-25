#!/usr/bin/env python3
"""
Which user actions cause which workflows to run.

Kept declarative and in one file so the pipeline can be read without tracing
call sites. Every edge below is a data dependency established by reading the
scripts - see documentation/specs/workflow-scheduling-spec.md section 3, which
names the table that creates each one.
"""

import json
import math
import os

import workflow_scheduler as sched


# ── The ingestion scripts ─────────────────────────────────────────────────────
# Either one imports photos: the phased import, or the streaming one
# (documentation/features/ingestion-architecture.md). Everything that follows an
# import - its trigger, what depends on it - treats them as the same thing.
INGEST_SCRIPTS = frozenset({
    'io_picture_import_workflow.py',
    'io_picture_import_v2_workflow.py',
})

# ── What must run before what ─────────────────────────────────────────────────
# Keys and values are script basenames. This is the hard dependency graph; the
# queue uses it to decide which items in a chain can start together.
DEPENDS_ON = {
    'ev_extract_events.py': set(INGEST_SCRIPTS),
    'ai_auto_detect_faces_workflow.py': set(INGEST_SCRIPTS),
    # Manual faces come from the user, not from another workflow.
    'ai_ingest_manual_faces.py': set(),
    # The crops it embeds are written by Auto Detect Faces.
    'ai_embed_faces.py': {'ai_auto_detect_faces_workflow.py'},
    'ai_face_recognition_workflow.py': {
        'ai_auto_detect_faces_workflow.py',   # face_detections('Automatic')
        'ai_ingest_manual_faces.py',          # face_contents - INNER JOIN, silent if empty
        'ai_embed_faces.py'},                 # the vectors it compares
    # ai_ml_faces_recognition_workflow.py ("Match Faces Within Events") is
    # RETIRED from every chain. It compared faces with people confirmed in the
    # same event, which Match Faces by Appearance has done for every face against
    # every reference since v5.4.31; and its same-photo exclusion is applied by
    # Match Faces by Appearance when it writes (v5.4.34) and by the contextual
    # workflow's C1. Run after Match Faces by Appearance, the two undid each
    # other's rows every time. The script stays, for a catalog that has the
    # workflow and wants to run it by hand.
    'ai_contextual_recognition_workflow.py': {
        'ai_face_recognition_workflow.py',    # reads the cosine distances
        'ev_extract_events.py'},              # event_files - its heaviest signal
    # Clustering coordinates has nothing to do with events, so this does NOT
    # depend on ev_extract_events: an edge there would serialise two unrelated
    # passes for no reason.
    'ge_extract_locations.py': set(INGEST_SCRIPTS),
    'ge_resolve_locations.py': {
        'ge_extract_locations.py'} | set(INGEST_SCRIPTS) | {
        'ev_extract_events.py'},              # event_files - the inheritance input
    # Pets. Detection happens INSIDE Auto Detect Faces, which is why that is
    # the dependency rather than an import: the two detectors share one decode.
    'pe_identify_pets.py': {
        'ai_auto_detect_faces_workflow.py'},  # pet_detections
    'pe_cluster_pets.py': {
        'pe_identify_pets.py'},               # ai_pet_contents.embedding
}


# Why each edge exists, in the words of the table that creates it. Kept beside
# DEPENDS_ON rather than inside it so the queue's dependency logic still reads a
# plain set - this is for showing a person the graph, not for scheduling.
DEPENDENCY_REASONS = {
    ('ai_face_recognition_workflow.py', 'ai_embed_faces.py'):
        'compares embeddings, which Embed Faces is what creates',
    ('ai_embed_faces.py', 'ai_auto_detect_faces_workflow.py'):
        'embeds the face crops that Auto Detect Faces writes',
    ('ev_extract_events.py', 'io_picture_import_workflow.py'):
        'file_contents — there is nothing to group until files are imported',
    ('ge_extract_locations.py', 'io_picture_import_workflow.py'):
        'exif_data.geohash — the coordinates arrive with an import',
    ('ge_resolve_locations.py', 'ge_extract_locations.py'):
        'location_clusters — a file takes its location from an accepted cluster',
    ('ge_resolve_locations.py', 'ev_extract_events.py'):
        'event_files — a photo with no GPS inherits the location its event confers, '
        'and Create Events rebuilds every Auto event',
    ('ai_auto_detect_faces_workflow.py', 'io_picture_import_workflow.py'):
        'file_contents — faces are found in imported photos',
    ('pe_identify_pets.py', 'ai_auto_detect_faces_workflow.py'):
        'pet_detections — animals are found during the face pass, which shares '
        'the decode',
    ('pe_cluster_pets.py', 'pe_identify_pets.py'):
        'ai_pet_contents.embedding — there is nothing to group until the crops '
        'are measured',
    ('ai_face_recognition_workflow.py', 'ai_auto_detect_faces_workflow.py'):
        "face_detections('Automatic') — the faces to score",
    ('ai_face_recognition_workflow.py', 'ai_ingest_manual_faces.py'):
        'face_contents — the reference set; an INNER JOIN, so silent if empty',
    ('ai_contextual_recognition_workflow.py', 'ai_face_recognition_workflow.py'):
        'face_similarity_scores — reads the cosine distances',
    ('ai_contextual_recognition_workflow.py', 'ev_extract_events.py'):
        'event_files — its heaviest signal',
}

# The streaming import is the same edge, for the same reasons.
for (_dep, _up), _why in list(DEPENDENCY_REASONS.items()):
    if _up == 'io_picture_import_workflow.py':
        DEPENDENCY_REASONS[(_dep, 'io_picture_import_v2_workflow.py')] = _why


def dependency_reason(dependent, dependency):
    """Why `dependent` must run after `dependency`, or a generic fallback."""
    return DEPENDENCY_REASONS.get(
        (dependent, dependency), 'reads data the other workflow writes')


# ── Which chain each user action starts ───────────────────────────────────────
TRIGGERS = {
    # An import invalidates everything downstream of file_contents.
    'import_completed': [
        'ev_extract_events.py',
        'ge_extract_locations.py',
        'ge_resolve_locations.py',
        'ai_auto_detect_faces_workflow.py',
        # Here, and deliberately NOT in face_confirmed. An import brings a few
        # hundred new crops, so this is seconds and invisible; putting it
        # behind a face tag is what charged somebody four hours for confirming
        # three faces.
        'ai_embed_faces.py',
        'ai_ingest_manual_faces.py',
        'ai_face_recognition_workflow.py',
        'ai_contextual_recognition_workflow.py',
        'pe_identify_pets.py',
        'pe_cluster_pets.py',
    ],
    # A human tagged a face. It is a new reference, and nothing downstream knows.
    'face_confirmed': [
        'ai_ingest_manual_faces.py',
        'ai_face_recognition_workflow.py',
        'ai_contextual_recognition_workflow.py',
    ],
    # Animals were detected - raised when Auto Detect Faces completes, because
    # its ride-along is the only thing that writes pet_detections. Not
    # conditional on having written any: see triggers_for_completed_workflow.
    'pets_detected': [
        'pe_identify_pets.py',
        'pe_cluster_pets.py',
    ],
    # Crops were measured, so there is something new to group.
    'pets_identified': [
        'pe_cluster_pets.py',
    ],
    # A pet was named, renamed, merged or deleted.
    #
    # This one matters as much as the others combined. Naming a pet changes the
    # REFERENCE SET, and without a re-score every photograph of Rex taken
    # before he was named stays unidentified for ever - the same silent loss a
    # missing events_changed would have caused for inherited locations.
    'pets_changed': [
        'pe_identify_pets.py',
        'pe_cluster_pets.py',
    ],
    # Detections changed without an import.
    #
    # Embed Faces leads, and must: Auto Detect Faces has just written crops
    # with no vectors, and every workflow below compares vectors. Without it
    # here the commonest path in the system - detect faces, then recognise
    # them - skips embedding entirely, which is the whole reason embedding was
    # separated out. DEPENDS_ON serialises the two, so the queue runs them in
    # this order rather than together.
    'faces_detected': [
        'ai_embed_faces.py',
        'ai_face_recognition_workflow.py',
        'ai_contextual_recognition_workflow.py',
    ],
    # Events were rebuilt or edited.
    #
    # ge_resolve_locations is here because Create Events DELETES every Auto
    # event and recreates it with a new id, so every location a photo inherited
    # from one is orphaned the moment that workflow finishes. Without this edge
    # the inherited locations silently disappear and stay gone until somebody
    # happens to run Resolve Locations by hand. location-spec.md 3.2 and 4.6.
    'events_changed': [
        'ge_resolve_locations.py',
        'ai_contextual_recognition_workflow.py',
    ],
    # A human accepted, merged, rejected or reopened a proposed location.
    #
    # The same shape as face_confirmed: accepting a cluster creates a location no
    # photo points at yet, and nothing downstream knows until the resolve pass
    # runs. Without this someone names a location, watches its photo count stay at
    # zero, and concludes it did not work. A REJECTION needs it just as much -
    # the files it located must lose that location - which is why this is
    # named for the change and not for the accept.
    'locations_changed': [
        'ge_resolve_locations.py',
    ],
    # Extract Locations finished, so cluster membership may have moved.
    #
    # Separate from locations_changed although it enqueues exactly the same thing,
    # and the reason is the QUIET PERIOD below rather than the outcome. A person
    # naming locations does it in bursts and wants the delay; a workflow finishing
    # happens once and will not repeat, so the same delay is two minutes of
    # latency for nothing. Two occasions with the same consequence and different
    # timing need two names - which is what the first version of this got wrong
    # by folding them together.
    'locations_clustered': [
        'ge_resolve_locations.py',
    ],
}

# ── Quiet periods ─────────────────────────────────────────────────────────────
# How long to wait, after the LAST occurrence of a trigger, before its chain may
# start. Every further occurrence pushes the start time out again.
#
# This is for actions a person repeats in a burst. Tagging faces is the clear
# case: someone works through a photo naming eight people, and without a quiet
# period the chain starts after the first tag, the second tag queues it again
# behind the running one, and so on - several full recognition passes, each made
# stale by the next tag before it finishes.
#
# Discrete events get no delay. An import finishing happens once and is not
# followed by more of itself, so waiting would be pure latency.
TRIGGER_QUIET_SECONDS = {
    # Five minutes: long enough that someone working through a page of faces
    # is still tagging when the previous tag's timer would have run out, so
    # Learn Tagged Faces starts once, with every face, not with the first few.
    'face_confirmed': int(os.environ.get('QUIET_FACE_CONFIRMED', '300')),
    'import_completed': 0,
    'faces_detected': 0,
    'events_changed': int(os.environ.get('QUIET_EVENTS_CHANGED', '30')),
    # Naming locations is done in bursts for the same reason tagging faces is -
    # someone works down the proposed list naming eight of them - and each
    # accept would otherwise queue a resolve pass made stale by the next.
    'locations_changed': int(os.environ.get('QUIET_LOCATIONS_CHANGED', '120')),
    # A discrete event, like an import finishing: it happens once and is not
    # followed by more of itself, so waiting would be pure latency.
    'locations_clustered': 0,
    # Also discrete: the detection pass finished, once.
    'pets_detected': 0,
    'pets_identified': 0,
    # Naming pets is bursty in the way naming locations is - somebody works
    # down the review page naming three groups - and each one would otherwise
    # queue a re-score made stale by the next.
    'pets_changed': int(os.environ.get('QUIET_PETS_CHANGED', '120')),
}


def quiet_seconds(trigger_name):
    return TRIGGER_QUIET_SECONDS.get(trigger_name, 0)


# ── Overnight ─────────────────────────────────────────────────────────────────
# Workflows a trigger queues for the NEXT NIGHTLY_AT rather than after its quiet
# period (owner, 2026-10-21). Match Faces by Appearance compares every face with
# every reference - hours on `family` - and tagging a face is something people do
# while using the system, which it then made slow for the rest of the day. The
# learning still happens once a day, when nobody is using it; anyone can start
# it at once with "now" in the queue, or from the Workflows page.
#
# Only this one. Ingest Manual Faces still runs after the quiet period (quick,
# and it makes the reference crops), and Contextual Recognition depends on
# Match Faces by Appearance, so it follows it whenever that runs.
NIGHTLY = {
    ('face_confirmed', 'ai_face_recognition_workflow.py'),
}
NIGHTLY_AT = os.environ.get('NIGHTLY_LEARNING_AT', '03:00')
# The containers run on UTC; "3 am" means the household's 3 am.
NIGHTLY_TZ = os.environ.get('NIGHTLY_LEARNING_TZ', 'America/New_York')


def next_nightly(now=None):
    """
    The next NIGHTLY_AT in NIGHTLY_TZ, as a naive datetime in the process's own
    local time - what workflow_queue.not_before is compared against. Today's, if
    it has not passed yet (a tag at 1 am runs at 3 am that night).
    """
    from datetime import datetime, time as dtime, timedelta
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(NIGHTLY_TZ)
    except Exception:
        tz = None
    try:
        at = dtime(*(int(x) for x in NIGHTLY_AT.split(':', 1)))
    except (TypeError, ValueError):
        at = dtime(3, 0)
    local_now = now or datetime.now().astimezone()
    here = local_now.astimezone(tz) if tz else local_now
    target = datetime.combine(here.date(), at, tzinfo=here.tzinfo)
    if target <= here:
        target = datetime.combine(here.date() + timedelta(days=1), at, tzinfo=here.tzinfo)
    return target.astimezone(local_now.tzinfo).replace(tzinfo=None)


def delay_for(trigger_name, script, now=None):
    """(seconds to wait, overnight?) for one workflow of a trigger's chain."""
    if (trigger_name, script) in NIGHTLY:
        from datetime import datetime
        local_now = now or datetime.now().astimezone()
        wait = (next_nightly(local_now) - local_now.replace(tzinfo=None)).total_seconds()
        # Rounded up: never a fraction of a second before the hour.
        return max(1, int(math.ceil(wait))), True
    return quiet_seconds(trigger_name), False


# Human-readable provenance for the queue panel.
TRIGGER_LABELS = {
    'import_completed': 'photos imported',
    'face_confirmed': 'faces confirmed',
    'faces_detected': 'face detection finished',
    'pets_detected': 'animals detected',
    'pets_identified': 'animals measured',
    'pets_changed': 'pets named or changed',
    'events_changed': 'events rebuilt',
    'locations_changed': 'locations changed',
    'locations_clustered': 'locations clustered',
    'manual': 'started by hand',
    # The event date editor's Confirm (mass-edit-event-photo-dates-spec.md
    # section 5.1): Create Events, queued to regroup the re-dated photos.
    'date_change': 'photo dates changed',
    # Provenance matters here as much as anywhere: a resumed row looks exactly
    # like a hand-started one otherwise, and the user loses the thread between
    # what they stopped and what is now running.
    'resume': 'resumed after a stop',
}


def describe(trigger_source, detail=None):
    """One sentence for the UI: 'queued by: 12 faces confirmed'."""
    base = TRIGGER_LABELS.get(trigger_source, trigger_source)
    if isinstance(detail, dict):
        n = detail.get('coalesced')
        if n and n > 1:
            faces = detail.get('faces')
            if trigger_source == 'face_confirmed' and faces:
                total = n * int(faces) if int(faces) > 1 else n
                return f'{total} faces confirmed'
            return f'{n} x {base}'
    return base


def _is_unconfigured_ingestion(workflow):
    """
    True for an ingestion workflow that has no source directory yet.

    Imported lazily and defensively: workflow_triggers is reachable from the
    scheduler thread, and a missing or malformed manifest must never stop a
    chain from running the workflows that ARE configured.
    """
    try:
        import workflow_defaults
    except ImportError:
        return False
    try:
        return workflow_defaults.needs_configuration(workflow)
    except Exception:
        return False


def _workflows_by_script(conn):
    """Map script basename -> workflow row, for the workflows registered here."""
    out = {}
    for row in conn.execute('SELECT * FROM workflows'):
        out[os.path.basename(row['python_script'] or '')] = dict(row)
    return out


def fire(catalog, trigger_name, detail=None, chain_depth=0):
    """
    Enqueue the chain for a user action.

    Returns a list of (queue_id, workflow_name, created).

    Chain depth is the cycle guard. ai_contextual_recognition_workflow.py writes
    face_detections.person_id with source='Auto-Confirmed'; if that could raise
    face_confirmed, the chain would re-enqueue itself and run forever.

    Three defences, all required:

      1. A workflow subprocess cannot fire a trigger at all. Triggers are raised
         only from the backend process - the HTTP request path for user actions,
         and the scheduler's completion hook for chain continuation. A script
         writing person_id directly has no way in.
      2. face_confirmed is raised only from _assign_face_to_person() in the HTTP
         path, which is what a human tagging a face goes through. The contextual
         workflow does not.
      3. This depth cap, as a backstop for anything the first two miss.
    """
    if trigger_name not in TRIGGERS:
        raise ValueError(f'Unknown trigger: {trigger_name}')
    if chain_depth > sched.MAX_CHAIN_DEPTH:
        print(f'[triggers] {trigger_name} suppressed at chain depth {chain_depth}')
        return []
    if not catalog.target_directory:
        return []

    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    if not os.path.exists(db_path):
        return []

    sched.ensure_workflow_queue_schema(db_path)
    conn = sched._conn(db_path)
    try:
        registered = _workflows_by_script(conn)
    finally:
        conn.close()

    scripts = TRIGGERS[trigger_name]
    enqueued_ids = {}          # script basename -> queue id, for depends_on
    results = []

    for script in scripts:
        wf = registered.get(script)
        if wf is None:
            # A workflow the user has not added to this catalog. Skipping is
            # correct: the chain describes what SHOULD happen, the catalog
            # decides what CAN.
            continue

        # An ingestion workflow with no source directory imports nothing and
        # reports success. In a chain that is worse than at the button: nobody
        # is watching, so the catalog ends up looking processed and empty.
        # Defaults are created unconfigured on purpose
        # (workflows-default-spec.md 4.1-4.2), so this is a normal state for a
        # new catalog rather than a fault.
        if _is_unconfigured_ingestion(wf):
            print(f'[triggers] skipping {script}: no source directory configured')
            continue

        # Depend only on chain members that are actually being enqueued now.
        deps = [enqueued_ids[dep] for dep in DEPENDS_ON.get(script, set())
                if dep in enqueued_ids]

        # The whole chain waits, not just its first member: the later ones are
        # blocked behind it anyway, and the same deadline keeps the countdown
        # honest. A NIGHTLY workflow waits for the night instead.
        delay, overnight = delay_for(trigger_name, script)
        queue_id, created = sched.enqueue(
            db_path, wf,
            trigger_source=trigger_name,
            trigger_detail={**dict(detail or {}), **({'nightly': True} if overnight else {})},
            depends_on=deps or None,
            chain_depth=chain_depth + 1,
            delay_seconds=delay,
            overnight=overnight,
        )
        enqueued_ids[script] = queue_id
        results.append((queue_id, wf.get('name') or script, created))

    return results


def triggers_for_completed_workflow(script_name):
    """
    Map a finished workflow to the trigger(s) it raises - a tuple, possibly empty.

    Called by the scheduler when a queue item reaches a terminal state. This is
    the one place a workflow may cause more work, and it is deliberately narrow:
    only these scripts, only on completion.

    A tuple rather than a single name because Auto Detect Faces now finds two
    different things in one pass. Faces and animals invalidate different
    downstream work, and collapsing them into one trigger would mean either the
    face chain running for a dog or the pet chain never running at all.
    """
    script = os.path.basename(script_name or '')
    if script in INGEST_SCRIPTS:
        return ('import_completed',)
    if script == 'ai_auto_detect_faces_workflow.py':
        # pets_detected is raised whether or not this run wrote any pet rows,
        # and that is a deliberate simplification: no result dict survives the
        # subprocess for the scheduler to read, and the pet chain is only
        # REGISTERED on a catalog with pets switched on - so on every other
        # catalog fire() finds nothing to enqueue and this costs nothing. On a
        # pets-on catalog the cost of being wrong is two workflows that query,
        # find nothing new and finish, which is the same bargain
        # faces_detected already makes.
        return ('faces_detected', 'pets_detected')
    if script == 'pe_identify_pets.py':
        # Crops were measured, so there is something new to group. Without this
        # edge a newly detected animal is embedded and then sits unclustered
        # until somebody runs Cluster Pets by hand - the silent half-finish
        # this whole map exists to prevent.
        return ('pets_identified',)
    if script == 'ev_extract_events.py':
        return ('events_changed',)
    if script == 'ge_extract_locations.py':
        # A re-cluster can move a file into or out of an accepted cluster, which
        # changes the location it resolves to. Its own trigger, not locations_changed:
        # that one carries a two-minute quiet period for a person working
        # through a list, and a workflow finishing should not wait for it.
        return ('locations_clustered',)
    return ()
