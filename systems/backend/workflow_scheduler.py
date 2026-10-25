#!/usr/bin/env python3
"""
The workflow queue runner.

A daemon thread inside the backend process, not cron. The reasoning is recorded
in documentation/specs/workflow-scheduling-spec.md section 2.1; the short version
is that this process already owns the workflows table, the execution records, the
subprocess launcher and the Socket.IO server, and a cron process would own none
of them.

The runner owns nothing that is not in the database. Queue state lives in
workflow_queue in each catalog database, so a backend restart loses no work and a
second runner (if gunicorn is ever given more than one worker) is fenced off by a
lease rather than racing.
"""

import json
import os
import socket
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta

from catalog import Catalog
from workflow_queue_schema import (
    ensure_workflow_queue_schema,
    ensure_scheduler_lease_schema,
    TERMINAL_STATUSES,
)

POLL_INTERVAL = float(os.environ.get('QUEUE_POLL_INTERVAL', '2'))
MAX_CONCURRENT_WORKFLOWS = int(os.environ.get('MAX_CONCURRENT_WORKFLOWS', '2'))
LEASE_TTL_SECONDS = 90
LEASE_REFRESH_SECONDS = 30

# Chain depth 1 means: a trigger may enqueue work, and that work may not enqueue
# further work. See spec section 5.2 - without this, a contextual run that
# auto-confirms faces re-triggers the chain that produced it, forever.
MAX_CHAIN_DEPTH = 1


# ── Write sets ────────────────────────────────────────────────────────────────
# Which tables each workflow WRITES. Two workflows may run at the same time only
# if their write sets are disjoint; SQLite allows one writer at a time, but the
# real hazard is semantic, not lock contention - two workflows rewriting
# face_similarity_scores from different premises produce a table that reflects
# neither. Derived from reading each script; see spec section 3.
WORKFLOW_WRITE_SETS = {
    'io_picture_import_workflow.py': {
        'file_contents', 'exif_data', 'alternative_versions', 'file_operations'},
    'io_picture_import_v2_workflow.py': {
        'file_contents', 'exif_data', 'alternative_versions', 'file_operations'},
    'ev_extract_events.py': {
        'events', 'event_files', 'location_events'},
    # 'locations' IS in here, and was not at first. This workflow now writes an
    # unconfirmed locations row per cluster, so the Locations page has one kind
    # of thing to show; a write set that omitted it would let this run beside
    # something else writing the same table. Not 'file_locations' though - that
    # is the resolve pass's, and keeping them apart is what lets the two run
    # independently.
    'ge_extract_locations.py': {
        'location_clusters', 'location_cluster_files', 'locations'},
    # Not 'file_contents', though master_datetime lives there and the obvious
    # move would be to match it. file_contents is in four other write sets, and
    # this workflow resolves AFTER clustering and events exist - so declaring it
    # would serialise locations behind every import and every face pass for a
    # column none of them touch. See location-spec.md 4.2.
    'ge_resolve_locations.py': {
        'file_locations'},
    # 'pet_detections' is in here because the pet detector rides along inside
    # this workflow rather than being a pass of its own: the ~97 ms of read,
    # decode, resize and POST is 98 % of the per-image cost and is paid once
    # for both detectors. A second pass would pay it again over 373,020 files.
    'ai_auto_detect_faces_workflow.py': {
        'face_detections', 'ai_face_contents', 'file_contents', 'pet_detections'},
    'ai_ingest_manual_faces.py': {
        'face_contents'},
    # Only the embedding columns of ai_face_contents, but the write set is per
    # TABLE, so this conflicts with Auto Detect Faces and the two scoring
    # passes - which is right: all four write that table, and letting them
    # interleave is how 413,865 rows get written twice.
    'ai_embed_faces.py': {
        'ai_face_contents'},
    'ai_face_recognition_workflow.py': {
        'face_similarity_scores', 'ai_face_contents', 'face_contents'},
    'ai_ml_faces_recognition_workflow.py': {
        'face_similarity_scores', 'face_similarity_scores_audit', 'ai_face_contents'},
    # Kept apart, as the two location workflows are: disjoint write sets are
    # what lets these two run independently of each other. Identify writes the
    # crops, the measurements and the scores; Group writes only the groups.
    'pe_identify_pets.py': {
        'ai_pet_contents', 'pet_similarity_scores', 'pet_detections'},
    'pe_cluster_pets.py': {
        'pet_clusters', 'pet_cluster_members'},
    'ai_contextual_recognition_workflow.py': {
        'face_similarity_scores', 'face_similarity_scores_audit', 'person_cooccurrence',
        'face_clusters', 'face_cluster_members', 'auto_tag_audit',
        'face_detections', 'file_contents'},
}


# ── Stop modes ────────────────────────────────────────────────────────────────
# Whether a RUNNING workflow can be stopped, and how. See
# workflows-review-spec.md sections 2.5, 2.6 and 3.4.
#
#   'item'   the script has a per-item loop with a durable boundary - it commits
#            per item, so stopping between two items leaves a state the next run
#            simply continues from. Every one of these is already incremental, so
#            resuming is just running it again.
#
#   'never'  the script rebuilds a table from scratch: it deletes first and
#            repopulates after. A partial run is worse than either endpoint -
#            stopping ev_extract_events after its DELETE commits leaves the
#            catalog with FEWER events than before it started. Both are short.
#
#   'unsafe' not listed here. Unlisted scripts run on the threading path in
#            app.py, and a Python thread cannot be killed. Guessing is worse
#            than refusing.
WORKFLOW_STOP_MODES = {
    'io_picture_import_workflow.py':         'item',
    'io_picture_import_v2_workflow.py':      'item',
    'ai_auto_detect_faces_workflow.py':      'item',
    'ai_ingest_manual_faces.py':             'item',
    # 'item', and it means it: each embedding is committed as it is made, so a
    # stop costs at most COMMIT_EVERY crops and the next run resumes from the
    # data rather than from a checkpoint.
    'ai_embed_faces.py':                     'item',
    'ai_face_recognition_workflow.py':       'item',
    'ai_ml_faces_recognition_workflow.py':   'item',
    'ev_extract_events.py':                  'never',
    'ge_extract_locations.py':               'item',
    # 'item' where ev_extract_events has to be 'never', and the difference is
    # earned rather than assumed: this one scopes its delete-then-insert to one
    # event per transaction, so a stop between two events leaves earlier events
    # resolved and later ones exactly as they were.
    'ge_resolve_locations.py':               'item',
    # 'item' since v5.6.10. It was 'never' while it rewrote every score row in
    # one pass; v5.5.0 made it write one chunk of faces per transaction, and its
    # other writes are single transactions, so a stop between them leaves each
    # score either re-ranked or as the previous run left it.
    'ai_contextual_recognition_workflow.py': 'item',
    # 'item' for both: Identify commits per batch of crops, and Group scopes
    # its delete-then-rebuild to the unresolved groups, so a stop between
    # items leaves earlier work intact and later work exactly as it was.
    #
    # THIS MAP ALSO DERIVES _is_subprocess_workflow. An entry missing here puts
    # the workflow on the threading path, where workflows/ is not on sys.path,
    # and it dies with `No module named 'lib'`. That has reached the field
    # twice.
    'pe_identify_pets.py': 'item',
    'pe_cluster_pets.py': 'item',
}

# Why a workflow refuses to stop, in words a user can act on.
STOP_REFUSALS = {
    'never': ('{name} rebuilds its results in one pass - it clears the old ones '
              'before writing the new. Stopping it would leave less than there '
              'was before it started. It should finish shortly.'),
    'unsafe': ('{name} runs inside the backend process rather than as its own '
               'program, and cannot be interrupted. It will finish on its own.'),
}


def script_basename(python_script):
    return os.path.basename(python_script or '')


def stop_mode(python_script):
    """'item', 'never' or 'unsafe' - see WORKFLOW_STOP_MODES."""
    return WORKFLOW_STOP_MODES.get(script_basename(python_script), 'unsafe')


def stop_refusal(python_script, workflow_name):
    """The reason this workflow cannot be stopped, or None if it can."""
    mode = stop_mode(python_script)
    if mode == 'item':
        return None
    return STOP_REFUSALS[mode].format(name=workflow_name or script_basename(python_script))


def write_set(python_script):
    """Tables a workflow writes. An unknown script is assumed to write everything."""
    return WORKFLOW_WRITE_SETS.get(script_basename(python_script))


def conflicts(script_a, script_b):
    """True when two workflows must not run at the same time."""
    a, b = write_set(script_a), write_set(script_b)
    if a is None or b is None:
        return True          # unknown script: serialise, do not guess
    return bool(a & b)


# ── Process identity ──────────────────────────────────────────────────────────

def process_is_execution(pid, execution_id):
    """
    True when `pid` is still the process running `execution_id`.

    A pid on its own is not safe to act on: it can be recycled, and signalling a
    recycled pid means killing something unrelated. The command line settles it -
    every workflow subprocess is launched with `--execution-id <id>`, so a pid
    that does not carry ours is not ours.

    Reads /proc, which is the container's own procfs. Absent (macOS, a stripped
    image), the answer is "cannot confirm" - False - and every caller treats
    that as "do not signal", which is the safe direction.
    """
    if not pid:
        return False
    try:
        with open(f'/proc/{int(pid)}/cmdline', 'rb') as fh:
            argv = fh.read().split(b'\0')
    except (OSError, ValueError):
        return False
    return b'--execution-id' in argv and str(execution_id).encode() in argv


# ── Which machine owns an execution ───────────────────────────────────────────
#
# A pid is only meaningful on the machine that issued it. Recovery has to decide
# whether a 'running' row belongs to a process that died or to one it simply
# cannot see, and /proc alone cannot tell those apart: a process on another host
# looks exactly like a process that exited.
#
# The identity is a file rather than a hostname because the container is started
# without --hostname, so its hostname is the container id and changes on every
# recreate - which would make every pre-existing row look foreign. database/ is
# a bind mount, so a file there is stable per machine for the same reason
# auth.load_secret_key() stores the session key beside it.

_INSTANCE_ID = None


def instance_id():
    """This machine's stable id, created on first use."""
    global _INSTANCE_ID
    if _INSTANCE_ID:
        return _INSTANCE_ID
    env = os.environ.get('AIRLOCK_INSTANCE_ID')
    if env:
        _INSTANCE_ID = env.strip()
        return _INSTANCE_ID
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'database', 'instance_id')
    try:
        if os.path.exists(path):
            with open(path) as fh:
                value = fh.read().strip()
            if value:
                _INSTANCE_ID = value
                return _INSTANCE_ID
        os.makedirs(os.path.dirname(path), exist_ok=True)
        value = uuid.uuid4().hex
        with open(path, 'w') as fh:
            fh.write(value)
        _INSTANCE_ID = value
    except OSError as e:
        # Never fatal. An ephemeral id degrades recovery to what it did before
        # this existed; refusing to start would be a worse trade.
        print(f'[scheduler] could not persist an instance id ({e}) - using a temporary one')
        _INSTANCE_ID = uuid.uuid4().hex
    return _INSTANCE_ID


# What recovery is allowed to conclude about a 'running' row.
EXEC_ALIVE = 'alive'        # confirmed still running, and ours
EXEC_GONE = 'gone'          # confirmed over
EXEC_UNKNOWN = 'unknown'    # ours, but unconfirmable - no procfs to ask
EXEC_FOREIGN = 'foreign'    # started by a different machine; not ours to judge


def process_state(pid, execution_id):
    """
    'alive', 'gone' or 'unknown' for the process that should be running
    `execution_id`, from procfs alone.

      'alive'    /proc names this pid and its command line carries
                 --execution-id <execution_id>.

      'gone'     procfs answered and the process is not there, or the pid was
                 recycled and now belongs to something else. Also the answer
                 when no pid was ever recorded: a thread-path workflow cannot
                 outlive the process that started it, and a subprocess that
                 never reached set_pid() never began.

      'unknown'  procfs cannot answer at all - no /proc (macOS, a stripped
                 image). Distinct from 'gone' on purpose: 'gone' is a finding,
                 'unknown' is the absence of one, and only one of them justifies
                 declaring a workflow failed.
    """
    if not pid:
        return EXEC_GONE
    if not os.path.isdir('/proc'):
        return EXEC_UNKNOWN
    try:
        with open(f'/proc/{int(pid)}/cmdline', 'rb') as fh:
            argv = fh.read().split(b'\0')
    except FileNotFoundError:
        return EXEC_GONE            # procfs works and has no such process
    except ValueError:
        return EXEC_GONE            # an unusable pid is not a live process
    except OSError:
        return EXEC_UNKNOWN         # procfs is there but would not answer
    if b'--execution-id' in argv and str(execution_id).encode() in argv:
        return EXEC_ALIVE
    return EXEC_GONE                # the pid was recycled; ours ended


def execution_state(pid, execution_id, owner=None):
    """
    As process_state, plus 'foreign' when the row was started by another
    machine. An owner that is absent is treated as ours: rows predate the
    column, and assuming they are ours preserves the behaviour they were
    written under.
    """
    if owner and owner != instance_id():
        return EXEC_FOREIGN
    return process_state(pid, execution_id)


# ── Connections ───────────────────────────────────────────────────────────────

def _conn(db_path):
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    return conn


def has_owner_column(conn):
    """
    True when workflow_executions carries owner_instance.

    recover() can run against a catalog database before models.py has had a
    chance to ALTER it in - the scheduler's first pass only ensures the queue
    tables. Selecting a column that is not there yet would raise and skip
    recovery entirely, which is worse than recovering without the owner.
    """
    try:
        return any(r[1] == 'owner_instance'
                   for r in conn.execute('PRAGMA table_info(workflow_executions)'))
    except sqlite3.Error:
        return False


# ── Queue operations ──────────────────────────────────────────────────────────

def list_queue(catalog_db_path, include_finished=False, limit=200):
    ensure_workflow_queue_schema(catalog_db_path)
    conn = _conn(catalog_db_path)
    if include_finished:
        rows = conn.execute(
            'SELECT q.*, e.processed_files, e.total_files, e.status AS execution_status '
            'FROM workflow_queue q '
            'LEFT JOIN workflow_executions e ON e.id = q.execution_id '
            'ORDER BY CASE q.status WHEN \'running\' THEN 0 WHEN \'queued\' THEN 1 '
            '  WHEN \'blocked\' THEN 2 ELSE 3 END, q.priority DESC, q.position, q.id '
            'LIMIT ?', (limit,)).fetchall()
    else:
        # 'stopped' is terminal but stays in the default listing: it is the only
        # place the resume link can live, and hiding it would mean a user who
        # stopped something has no way back to it. Resuming supersedes the row,
        # which drops it out of this list again.
        rows = conn.execute(
            'SELECT q.*, e.processed_files, e.total_files, e.status AS execution_status '
            'FROM workflow_queue q '
            'LEFT JOIN workflow_executions e ON e.id = q.execution_id '
            'WHERE q.status IN (\'queued\', \'blocked\', \'running\', \'stopped\') '
            'ORDER BY CASE q.status WHEN \'running\' THEN 0 WHEN \'stopped\' THEN 1 '
            '  WHEN \'queued\' THEN 2 ELSE 3 END, q.priority DESC, q.position, q.id '
            'LIMIT ?', (limit,)).fetchall()
    out = []
    for r in rows:
        d = dict(r)
        if d.get('trigger_detail'):
            try:
                d['trigger_detail'] = json.loads(d['trigger_detail'])
            except Exception:
                pass
        if d.get('depends_on'):
            try:
                d['depends_on'] = json.loads(d['depends_on'])
            except Exception:
                d['depends_on'] = []
        else:
            d['depends_on'] = []
        d['waiting_seconds'] = _seconds_until(d.get('not_before'))
        # A running row with a stop pending is "stopping" in the UI. It is a
        # derived state, not a stored one: after a restart nothing is stopping,
        # because nothing is running.
        d['stop_requested'] = bool(d.get('stop_requested'))
        d['stoppable'] = stop_mode(d.get('script_name')) == 'item'
        out.append(d)
    conn.close()
    return out


def _seconds_until(timestamp):
    """Whole seconds until an ISO timestamp, or 0 if it has passed / is unset."""
    if not timestamp:
        return 0
    try:
        remaining = (datetime.fromisoformat(timestamp) - datetime.now()).total_seconds()
    except (TypeError, ValueError):
        return 0
    return max(0, int(remaining))


def run_now(catalog_db_path, queue_id):
    """
    Skip the quiet period for one item.

    The delay is a guess about whether the user has finished. Sometimes they know
    better, and waiting three minutes for a workflow you deliberately asked for
    is worse than the duplicate runs the delay exists to prevent.
    """
    conn = _conn(catalog_db_path)
    try:
        row = conn.execute('SELECT status, not_before FROM workflow_queue WHERE id = ?',
                           (queue_id,)).fetchone()
        if not row:
            return False, 'Queue item not found'
        if row['status'] not in ('queued', 'blocked'):
            return False, f"Item is already {row['status']}"
        conn.execute('UPDATE workflow_queue SET not_before = NULL WHERE id = ?', (queue_id,))
        conn.commit()
        return True, 'Starting now'
    finally:
        conn.close()


def _next_position(conn):
    row = conn.execute('SELECT COALESCE(MAX(position), 0) + 1 AS p FROM workflow_queue').fetchone()
    return row['p'] if row else 1


def enqueue(catalog_db_path, workflow, trigger_source='manual', trigger_detail=None,
            source_directory=None, depends_on=None, chain_depth=0, priority=0,
            coalesce=True, delay_seconds=0, overnight=False):
    """
    Add a workflow to the queue.

    Returns (queue_id, created). `created` is False when the request was folded
    into an existing queued row.

    `delay_seconds` sets a quiet period: the row will not start until that long
    after the LAST trigger of its kind. Coalescing alone is not enough for an
    action a user repeats - it folds pending rows, but the moment the first run
    starts, the next confirmation has nothing to fold into and queues a second
    run. Someone tagging faces for ten minutes gets a run every time the previous
    one finishes, each immediately made stale by the next tag.

    With a quiet period, every tag pushes the start time further out, and the
    chain runs once, when the person has actually stopped.

    `overnight` (workflow_triggers.NIGHTLY): delay_seconds reaches the next
    night. It never postpones a row already due to run - something else wanted
    it now - and a row waiting for the night is started by the next request that
    wants it now (a delay of 0: an import, a detection pass, the Run button).
    """
    ensure_workflow_queue_schema(catalog_db_path)
    script = script_basename(workflow.get('python_script'))
    # Coalesce on the workflow alone, NOT on (workflow, trigger).
    #
    # The spec proposed "<workflow_id>:<trigger_source>". That folds twenty
    # face confirmations into one run, but not an import chain and a
    # detection chain that both want Face Recognition - and those overlap
    # constantly, because finishing auto-detect raises faces_detected while
    # the import chain that started it has the same workflow queued. Two runs
    # of the same workflow back to back is pure waste: the second finds the
    # first has already done the work and skips everything.
    #
    # The queue entry means "this workflow needs to run"; two reasons do not
    # make it need to run twice.
    key = str(workflow['id']) if coalesce else None

    conn = _conn(catalog_db_path)
    try:
        if key:
            existing = conn.execute(
                "SELECT id, trigger_detail, depends_on, not_before FROM workflow_queue "
                "WHERE coalesce_key = ? AND status IN ('queued', 'blocked') "
                "ORDER BY id LIMIT 1", (key,)).fetchone()
            if existing:
                # The folding chain's dependencies join the row's own. Without
                # this a re-raised chain could run out of order: on oldserver an
                # import chain had ML and Contextual waiting on Face Recognition
                # #74; face detection then queued a SECOND Face Recognition (#77,
                # since #74 was already running) and folded into the waiting ML
                # and Contextual rows - which kept depending on #74 alone, ran as
                # soon as it finished, and so never saw what #77 found.
                if depends_on:
                    try:
                        old_deps = json.loads(existing['depends_on'] or '[]')
                    except Exception:
                        old_deps = []
                    merged = sorted(set(old_deps) | set(depends_on))
                    if merged != sorted(old_deps):
                        # 'queued' becomes 'blocked'; the unblock pass promotes
                        # it again once every dependency has finished.
                        conn.execute(
                            "UPDATE workflow_queue SET depends_on = ?, "
                            "status = CASE WHEN status = 'queued' THEN 'blocked' "
                            "ELSE status END WHERE id = ?",
                            (json.dumps(merged), existing['id']))
                # Fold in: bump the count so the UI can say "12 faces confirmed"
                # rather than showing twelve identical rows.
                detail = {}
                if existing['trigger_detail']:
                    try:
                        detail = json.loads(existing['trigger_detail'])
                    except Exception:
                        detail = {}
                detail['coalesced'] = int(detail.get('coalesced', 1)) + 1
                if trigger_detail:
                    detail.update({k: v for k, v in trigger_detail.items()
                                   if k != 'coalesced'})
                waiting_overnight = bool(detail.get('nightly')) and not overnight
                if overnight and existing['not_before'] is None:
                    # Already due: whatever queued it wants it now. Do not
                    # postpone that to the night.
                    detail.pop('nightly', None)
                    conn.execute('UPDATE workflow_queue SET trigger_detail = ? WHERE id = ?',
                                 (json.dumps(detail), existing['id']))
                elif delay_seconds > 0 and not waiting_overnight:
                    # Reset the timer: the person is still working.
                    conn.execute(
                        'UPDATE workflow_queue SET trigger_detail = ?, not_before = ? '
                        'WHERE id = ?',
                        (json.dumps(detail),
                         (datetime.now() + timedelta(seconds=delay_seconds)).isoformat(),
                         existing['id']))
                elif waiting_overnight and delay_seconds == 0:
                    # Waiting for the night, and now something wants it at
                    # once (an import, a detection pass, the Run button).
                    detail.pop('nightly', None)
                    conn.execute('UPDATE workflow_queue SET trigger_detail = ?, '
                                 'not_before = NULL WHERE id = ?',
                                 (json.dumps(detail), existing['id']))
                else:
                    conn.execute(
                        'UPDATE workflow_queue SET trigger_detail = ? WHERE id = ?',
                        (json.dumps(detail), existing['id']))
                conn.commit()
                return existing['id'], False

        status = 'blocked' if depends_on else 'queued'
        not_before = ((datetime.now() + timedelta(seconds=delay_seconds)).isoformat()
                      if delay_seconds > 0 else None)
        cur = conn.execute(
            'INSERT INTO workflow_queue '
            '(workflow_id, workflow_name, script_name, status, position, priority, '
            ' trigger_source, trigger_detail, coalesce_key, depends_on, chain_depth, '
            ' source_directory, not_before) '
            'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
            (workflow['id'], workflow.get('name') or script, script, status,
             _next_position(conn), priority, trigger_source,
             json.dumps(trigger_detail) if trigger_detail else None,
             key, json.dumps(depends_on) if depends_on else None,
             chain_depth,
             source_directory if source_directory is not None
             else workflow.get('source_directory'),
             not_before))
        conn.commit()
        return cur.lastrowid, True
    finally:
        conn.close()


def promote(catalog_db_path, queue_id):
    """
    Put an item at the front and clear any quiet period.

    This is what "Run Workflow" means now: the same queue as everything else,
    but first in line and with no wait. Routing it through the queue rather than
    straight to the launcher is what makes its progress visible in the panel -
    a direct execution has no queue row, so there is nothing for the panel to
    report on.

    It still cannot jump a *conflicting* running workflow. Two workflows
    rewriting face_similarity_scores from different premises produce a table
    that reflects neither, and a button labelled Run is not worth that.
    """
    conn = _conn(catalog_db_path)
    try:
        row = conn.execute('SELECT status FROM workflow_queue WHERE id = ?',
                           (queue_id,)).fetchone()
        if not row:
            return False, 'Queue item not found'
        if row['status'] == 'running':
            return True, 'Already running'
        if row['status'] not in ('queued', 'blocked'):
            return False, f"Item is already {row['status']}"
        top = conn.execute(
            "SELECT COALESCE(MAX(priority), 0) + 1 AS p FROM workflow_queue "
            "WHERE status IN ('queued', 'blocked')").fetchone()['p']
        conn.execute(
            'UPDATE workflow_queue SET priority = ?, not_before = NULL WHERE id = ?',
            (top, queue_id))
        conn.commit()
        return True, 'Moved to the front of the queue'
    finally:
        conn.close()


def cancel(catalog_db_path, queue_id):
    """
    Cancel a queued or blocked row. Returns (ok, message).

    Still refuses a RUNNING row: stopping one is request_stop() below, which is a
    different operation with a different outcome. This one only removes work
    that has not begun.
    """
    conn = _conn(catalog_db_path)
    try:
        row = conn.execute('SELECT status FROM workflow_queue WHERE id = ?',
                           (queue_id,)).fetchone()
        if not row:
            return False, 'Queue item not found'
        if row['status'] == 'running':
            return False, 'Cannot cancel an item that is already running'
        # A stopped row is terminal but still on screen, because it carries the
        # resume link. Cancelling it is how the user says "no, leave it" and
        # clears it from the panel.
        if row['status'] in TERMINAL_STATUSES and row['status'] != 'stopped':
            return False, f"Item is already {row['status']}"
        conn.execute(
            "UPDATE workflow_queue SET status = 'cancelled', finished_at = ? WHERE id = ?",
            (datetime.now().isoformat(), queue_id))
        conn.commit()
        return True, 'Cancelled'
    finally:
        conn.close()


def mark_stop_requested(catalog_db_path, queue_id):
    """Mirror the execution's stop flag onto the queue row, for the UI."""
    conn = _conn(catalog_db_path)
    try:
        conn.execute('UPDATE workflow_queue SET stop_requested = 1 WHERE id = ?',
                     (queue_id,))
        conn.commit()
    finally:
        conn.close()


def mark_stopped(catalog_db_path, queue_id, remove=False, message=None):
    """
    Put a running queue row into its terminal state after a stop.

    `remove` is the difference between the two verbs the UI offers: a stop stays
    as 'stopped' and can be resumed, a stop-and-remove becomes 'cancelled' and
    is not offered one. Neither undoes work the run already did - see
    workflows-review-spec.md section 3.6.
    """
    conn = _conn(catalog_db_path)
    try:
        conn.execute(
            'UPDATE workflow_queue SET status = ?, finished_at = ?, '
            'error_message = COALESCE(?, error_message) WHERE id = ?',
            ('cancelled' if remove else 'stopped',
             datetime.now().isoformat(), message, queue_id))
        conn.commit()
    finally:
        conn.close()


def supersede(catalog_db_path, queue_id):
    """
    Retire a stopped row that has been resumed.

    'superseded' already means "replaced by a later row" and is already excluded
    from the default listing, so the panel shows the new row and not both.
    """
    conn = _conn(catalog_db_path)
    try:
        conn.execute(
            "UPDATE workflow_queue SET status = 'superseded', finished_at = ? "
            "WHERE id = ? AND status = 'stopped'",
            (datetime.now().isoformat(), queue_id))
        conn.commit()
    finally:
        conn.close()


def get_item(catalog_db_path, queue_id):
    """One queue row as a dict, or None."""
    conn = _conn(catalog_db_path)
    try:
        row = conn.execute('SELECT * FROM workflow_queue WHERE id = ?',
                           (queue_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def reorder(catalog_db_path, queue_id, new_position):
    """
    Move a queued item. Refuses a move that would put an item ahead of something
    it depends on - the dependency is a data fact, not a preference.
    """
    conn = _conn(catalog_db_path)
    try:
        row = conn.execute('SELECT * FROM workflow_queue WHERE id = ?', (queue_id,)).fetchone()
        if not row:
            return False, 'Queue item not found'
        if row['status'] not in ('queued', 'blocked'):
            return False, f"Cannot reorder an item that is {row['status']}"

        deps = json.loads(row['depends_on']) if row['depends_on'] else []
        if deps:
            dep_rows = conn.execute(
                'SELECT position FROM workflow_queue WHERE id IN (%s) '
                "AND status NOT IN ('completed','cancelled','superseded')"
                % ','.join('?' * len(deps)), deps).fetchall()
            for d in dep_rows:
                if new_position <= d['position']:
                    return False, ('That position is ahead of a workflow this one '
                                   'depends on')
        conn.execute('UPDATE workflow_queue SET position = ? WHERE id = ?',
                     (new_position, queue_id))
        conn.commit()
        return True, 'Reordered'
    finally:
        conn.close()


def is_paused(catalog_db_path):
    ensure_workflow_queue_schema(catalog_db_path)
    conn = _conn(catalog_db_path)
    try:
        row = conn.execute("SELECT value FROM queue_settings WHERE key = 'paused'").fetchone()
        return bool(row and row['value'] == '1')
    finally:
        conn.close()


def set_paused(catalog_db_path, paused):
    ensure_workflow_queue_schema(catalog_db_path)
    conn = _conn(catalog_db_path)
    try:
        conn.execute("INSERT INTO queue_settings (key, value) VALUES ('paused', ?) "
                     "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                     ('1' if paused else '0',))
        conn.commit()
    finally:
        conn.close()


# ── The runner ────────────────────────────────────────────────────────────────

class WorkflowScheduler:
    """
    Polls every catalog's queue and starts what is runnable.

    `start_execution` and `emit` are injected rather than imported so this module
    does not import app.py, which imports this module. They are, respectively,
    app.start_workflow_execution and socketio.emit.
    """

    def __init__(self, app_db_path, start_execution, emit=None, on_finished=None):
        self.app_db_path = app_db_path
        self.start_execution = start_execution
        self.emit = emit or (lambda *a, **k: None)
        self.on_finished = on_finished or (lambda *a, **k: None)
        self.owner = f'{socket.gethostname()}:{os.getpid()}'
        self._thread = None
        self._stop = threading.Event()
        # Set to make the loop run a pass now rather than at the next poll. A
        # button press should not visibly wait out POLL_INTERVAL.
        self._wake = threading.Event()
        self._has_lease = False

    # ── lease ─────────────────────────────────────────────────────────────
    def _acquire_or_refresh_lease(self):
        """
        Take the lease if free or stale, refresh it if already ours.

        gunicorn runs one worker today and the Dockerfile explains why, but a
        flag change should not silently produce two schedulers racing to start
        the same queue item.
        """
        try:
            ensure_scheduler_lease_schema(self.app_db_path)
            conn = _conn(self.app_db_path)
            now = datetime.now()
            cutoff = (now - timedelta(seconds=LEASE_TTL_SECONDS)).isoformat()
            row = conn.execute('SELECT owner, heartbeat FROM scheduler_lease WHERE id = 1').fetchone()
            if row is None:
                conn.execute('INSERT INTO scheduler_lease (id, owner, heartbeat) VALUES (1, ?, ?)',
                             (self.owner, now.isoformat()))
                conn.commit()
                got = True
            elif row['owner'] == self.owner or (row['heartbeat'] or '') < cutoff:
                conn.execute('UPDATE scheduler_lease SET owner = ?, heartbeat = ? WHERE id = 1',
                             (self.owner, now.isoformat()))
                conn.commit()
                got = True
            else:
                got = False
            conn.close()
            if got and not self._has_lease:
                print(f'[scheduler] lease acquired by {self.owner}')
            self._has_lease = got
            return got
        except Exception as e:
            print(f'[scheduler] lease error: {e}')
            return False

    # ── recovery ──────────────────────────────────────────────────────────
    def recover(self, catalog_db_path):
        """
        Fail anything left running by a process that is no longer here.

        Deliberately does not re-run: io_picture_import is not idempotent
        mid-copy, and silently repeating an import is worse than stopping.
        """
        conn = _conn(catalog_db_path)
        try:
            owner_col = ('e.owner_instance' if has_owner_column(conn)
                         else 'NULL AS owner_instance')
            rows = conn.execute(
                "SELECT q.id, q.execution_id, e.status AS exec_status, e.pid, "
                f"{owner_col} "
                "FROM workflow_queue q "
                "LEFT JOIN workflow_executions e ON e.id = q.execution_id "
                "WHERE q.status = 'running'").fetchall()
            recovered = 0
            for r in rows:
                # Mostly, a 'running' row here belongs to a process that no longer
                # exists - recover() only runs at startup, and the subprocess was
                # usually killed before it could record anything, so its execution
                # row still says 'running' too.
                #
                # But not always, and the ways it can be otherwise are different
                # from each other. The workflows are children of the gunicorn
                # worker, not of the container; if only the worker was replaced
                # they were reparented and are still going. Burying a live import
                # as 'failed' would then be a lie the UI repeats until it
                # finishes. A row started by another machine is not ours to judge
                # at all. And "procfs could not tell me" is not evidence of
                # death. Only EXEC_GONE is.
                if r['exec_status'] == 'completed':
                    status, msg = 'completed', None
                elif r['exec_status'] == 'stopped':
                    status, msg = 'stopped', None
                else:
                    state = execution_state(r['pid'], r['execution_id'],
                                            r['owner_instance'])
                    if state == EXEC_ALIVE:
                        print(f'[scheduler] execution {r["execution_id"]} survived the '
                              f'restart (pid {r["pid"]}) - left running')
                        continue
                    if state == EXEC_FOREIGN:
                        # Another machine started it and only that machine can
                        # see whether it is still going. Failing it here would
                        # mark a live workflow dead from across the network.
                        print(f'[scheduler] execution {r["execution_id"]} belongs to '
                              f'instance {r["owner_instance"]} - left alone')
                        continue
                    status = 'failed'
                    if state == EXEC_UNKNOWN:
                        # Still failed - leaving it 'running' would hold one of
                        # the MAX_CONCURRENT_WORKFLOWS slots open forever - but
                        # said as the guess it is, not as a finding.
                        msg = ('the backend restarted while this workflow was '
                               'running, and whether it survived could not be '
                               'checked on this machine')
                    else:
                        msg = 'backend restarted while this workflow was running'
                conn.execute(
                    'UPDATE workflow_queue SET status = ?, finished_at = ?, '
                    'error_message = ? WHERE id = ?',
                    (status, datetime.now().isoformat(), msg, r['id']))
                recovered += 1
            if recovered:
                conn.commit()
                print(f'[scheduler] recovered {recovered} interrupted queue item(s)')
            return recovered
        finally:
            conn.close()

    # ── one pass over one catalog ─────────────────────────────────────────
    def _reap(self, conn):
        """Move running rows whose execution finished into a terminal state."""
        changed = []
        rows = conn.execute(
            "SELECT q.id, q.workflow_name, q.script_name, q.chain_depth, "
            "       q.execution_id, e.status AS exec_status, e.error_message "
            "FROM workflow_queue q "
            "LEFT JOIN workflow_executions e ON e.id = q.execution_id "
            "WHERE q.status = 'running'").fetchall()
        for r in rows:
            if r['exec_status'] in ('completed', 'failed', 'interrupted', 'stopped'):
                # 'stopped' carries through as itself. Folding it into 'failed'
                # would put a deliberate stop in the same bucket as a crash and
                # lose the only signal the panel uses to offer a resume link.
                status = {'completed': 'completed',
                          'stopped': 'stopped'}.get(r['exec_status'], 'failed')
                conn.execute(
                    'UPDATE workflow_queue SET status = ?, finished_at = ?, '
                    'error_message = ? WHERE id = ?',
                    (status, datetime.now().isoformat(), r['error_message'], r['id']))
                item = dict(r)
                item['status'] = status
                changed.append(item)
        if changed:
            conn.commit()
        return changed

    def _unblock(self, conn):
        """Promote blocked rows whose dependencies have all finished."""
        changed = False
        rows = conn.execute(
            "SELECT id, depends_on FROM workflow_queue WHERE status = 'blocked'").fetchall()
        for r in rows:
            try:
                deps = json.loads(r['depends_on']) if r['depends_on'] else []
            except Exception:
                deps = []
            if not deps:
                conn.execute("UPDATE workflow_queue SET status = 'queued' WHERE id = ?", (r['id'],))
                changed = True
                continue
            dep_rows = conn.execute(
                'SELECT id, status FROM workflow_queue WHERE id IN (%s)'
                % ','.join('?' * len(deps)), deps).fetchall()
            statuses = {d['id']: d['status'] for d in dep_rows}
            # A dependency that no longer exists cannot block anything.
            outstanding = [d for d in deps
                           if statuses.get(d) in ('queued', 'blocked', 'running')]
            if outstanding:
                continue
            failed = [d for d in deps if statuses.get(d) in ('failed', 'cancelled')]
            if failed:
                # Do not run work whose input never got produced - it would
                # "succeed" against stale data, which is the failure mode this
                # whole feature exists to prevent.
                conn.execute(
                    "UPDATE workflow_queue SET status = 'cancelled', finished_at = ?, "
                    "error_message = 'a workflow this one depends on did not complete' "
                    "WHERE id = ?", (datetime.now().isoformat(), r['id']))
            else:
                conn.execute("UPDATE workflow_queue SET status = 'queued' WHERE id = ?", (r['id'],))
            changed = True
        if changed:
            conn.commit()
        return changed

    def _admit(self, catalog, conn):
        """Start as many runnable items as the concurrency and write-set rules allow."""
        started = []
        running = conn.execute(
            "SELECT script_name FROM workflow_queue WHERE status = 'running'").fetchall()
        running_scripts = [r['script_name'] for r in running]

        while len(running_scripts) < MAX_CONCURRENT_WORKFLOWS:
            # A row inside its quiet period is not a candidate yet. Later rows
            # still are: one workflow waiting out a tagging session should not
            # hold up an unrelated one.
            candidates = conn.execute(
                "SELECT * FROM workflow_queue WHERE status = 'queued' "
                'AND (not_before IS NULL OR not_before <= ?) '
                'ORDER BY priority DESC, position, id',
                (datetime.now().isoformat(),)).fetchall()
            picked = None
            for cand in candidates:
                if any(conflicts(cand['script_name'], s) for s in running_scripts):
                    continue
                picked = cand
                break
            if picked is None:
                break

            wf = conn.execute('SELECT * FROM workflows WHERE id = ?',
                              (picked['workflow_id'],)).fetchone()
            if wf is None:
                conn.execute(
                    "UPDATE workflow_queue SET status = 'failed', finished_at = ?, "
                    "error_message = 'workflow no longer exists' WHERE id = ?",
                    (datetime.now().isoformat(), picked['id']))
                conn.commit()
                continue

            # Claim the row BEFORE starting anything. The candidate SELECT above
            # and this UPDATE are separate statements, so between them another
            # runner may have taken the same row - a second gunicorn worker, or
            # one day a second machine. `AND status = 'queued'` makes the claim
            # the thing that decides: exactly one UPDATE reports a changed row,
            # and the loser starts nothing.
            #
            # Order matters as much as the guard. Starting the workflow first
            # and recording the claim afterwards means a lost race has already
            # spawned the subprocess - and recover()'s own comment notes that
            # io_picture_import is not idempotent mid-copy, which makes running
            # it twice at once worse than running it twice in sequence.
            claim = conn.execute(
                "UPDATE workflow_queue SET status = 'running', started_at = ? "
                "WHERE id = ? AND status = 'queued'",
                (datetime.now().isoformat(), picked['id']))
            conn.commit()
            if claim.rowcount != 1:
                # Someone else has it. It is no longer 'queued', so the next
                # candidate SELECT will not offer it again and this cannot spin.
                print(f'[scheduler] queue #{picked["id"]} was claimed elsewhere')
                continue

            try:
                execution_id = self.start_execution(
                    catalog, dict(wf), picked['source_directory'])
            except Exception as e:
                # The row is already 'running' from the claim; put it back into a
                # terminal state rather than leaving it held by nothing.
                conn.execute(
                    'UPDATE workflow_queue SET status = ?, finished_at = ?, '
                    'error_message = ? WHERE id = ?',
                    ('failed', datetime.now().isoformat(), str(e), picked['id']))
                conn.commit()
                print(f'[scheduler] failed to start {picked["workflow_name"]}: {e}')
                continue

            conn.execute('UPDATE workflow_queue SET execution_id = ? WHERE id = ?',
                         (execution_id, picked['id']))
            conn.commit()
            running_scripts.append(picked['script_name'])
            started.append((picked['id'], picked['workflow_name'], execution_id))
            print(f'[scheduler] started {picked["workflow_name"]} '
                  f'(queue #{picked["id"]}, execution {execution_id})')
        return started

    def tick(self):
        """One pass over every catalog. Returns True if anything changed."""
        changed_any = False
        try:
            catalogs = Catalog.get_all()
        except Exception as e:
            print(f'[scheduler] could not list catalogs: {e}')
            return False

        for catalog in catalogs:
            if not catalog.target_directory:
                continue
            db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
            if not os.path.exists(db_path):
                continue
            try:
                ensure_workflow_queue_schema(db_path)
                conn = _conn(db_path)
                try:
                    finished = self._reap(conn)
                    unblocked = self._unblock(conn)
                    started = []
                    if not is_paused(db_path):
                        started = self._admit(catalog, conn)
                finally:
                    conn.close()

                for item in finished:
                    changed_any = True
                    try:
                        self.on_finished(catalog, item)
                    except Exception as e:
                        print(f'[scheduler] on_finished hook failed: {e}')
                if unblocked or started:
                    changed_any = True
                if finished or started or unblocked:
                    self.emit('queue_updated', {'catalog_id': catalog.id})
            except Exception as e:
                print(f'[scheduler] error servicing catalog {catalog.id}: {e}')
        return changed_any

    # ── lifecycle ─────────────────────────────────────────────────────────
    def _loop(self):
        last_refresh = 0.0
        first = True
        while not self._stop.is_set():
            try:
                now = time.time()
                if first or now - last_refresh >= LEASE_REFRESH_SECONDS:
                    if not self._acquire_or_refresh_lease():
                        # Another process owns the queue. Idle rather than race.
                        self._stop.wait(POLL_INTERVAL * 5)
                        continue
                    last_refresh = now
                if first:
                    for catalog in Catalog.get_all():
                        if catalog.target_directory:
                            p = os.path.join(catalog.target_directory, 'workflow_results.db')
                            if os.path.exists(p):
                                ensure_workflow_queue_schema(p)
                                self.recover(p)
                    first = False
                self.tick()
            except Exception as e:
                print(f'[scheduler] loop error: {e}')
            # Wake early if something was just enqueued and asked to run now.
            if self._wake.wait(POLL_INTERVAL):
                self._wake.clear()

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True,
                                        name='workflow-scheduler')
        self._thread.start()
        print(f'[scheduler] started (poll {POLL_INTERVAL}s, '
              f'max concurrent {MAX_CONCURRENT_WORKFLOWS})')

    def kick(self):
        """Run a scheduling pass immediately instead of at the next poll."""
        self._wake.set()

    def stop(self):
        self._stop.set()
        self._wake.set()
