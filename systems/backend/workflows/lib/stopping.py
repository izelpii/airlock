"""
Cooperative stop for workflow subprocesses.

Implements documentation/specs/workflows-review-spec.md section 3.4.

A workflow is a detached subprocess with no channel back to the backend, so a
stop is a flag in the database it is already writing to: the backend sets
workflow_executions.stop_requested, and the script notices at its next item
boundary and leaves cleanly.

Every workflow is incremental - a re-run skips what an earlier run finished
(spec section 2.4) - so a clean stop between two items needs no checkpoint. The
next run simply carries on.

Deliberately in its own module rather than in lib/utilities.py: that module
imports PIL, exiftool and pillow_heif, and the recognition workflows should not
take those on just to read one integer. Only sqlite3 and time here, which is
also what lets it be imported from a subprocess with a different sys.path setup.
"""

import sqlite3
import time

# How long a cached answer stays good. The import loop asks once per file, which
# already costs a commit and an fsync, so the read would be noise there. The
# detection loop completes several items a second across four threads, and there
# is no reason to ask four times for an answer that changes once.
_TTL_SECONDS = 1.0

_cache = {}


def stop_requested(catalog_db_path, execution_id):
    """
    True when the user has asked this execution to stop.

    Never raises: a workflow must not die because the flag could not be read. A
    database that is momentarily locked answers "no", and the next item asks
    again - a stop that takes one extra item is not a problem, a workflow that
    crashes on a locked read is.
    """
    if not execution_id:
        return False

    key = (catalog_db_path, execution_id)
    cached = _cache.get(key)
    now = time.monotonic()
    if cached is not None and now - cached[0] < _TTL_SECONDS:
        return cached[1]

    value = False
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=5)
        try:
            row = conn.execute(
                'SELECT stop_requested FROM workflow_executions WHERE id = ?',
                (execution_id,)).fetchone()
            value = bool(row and row[0])
        finally:
            conn.close()
    except sqlite3.Error:
        # Includes "no such column" on a database that predates the feature.
        value = False

    _cache[key] = (now, value)
    return value


def record_stopped(catalog_db_path, execution_id, processed_files=None,
                   skipped_files=0, message=None):
    """
    Write the terminal row for a workflow that stopped when asked.

    Its own status, not 'failed': a stop is something the user did, and only a
    stopped run is offered a resume link (spec section 2.3).
    """
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            if processed_files is None:
                conn.execute(
                    "UPDATE workflow_executions SET status = 'stopped', "
                    'completed_at = CURRENT_TIMESTAMP, error_message = ?, '
                    'skipped_files = ? WHERE id = ?',
                    (message, skipped_files, execution_id))
            else:
                conn.execute(
                    "UPDATE workflow_executions SET status = 'stopped', "
                    'completed_at = CURRENT_TIMESTAMP, error_message = ?, '
                    'processed_files = ?, skipped_files = ? WHERE id = ?',
                    (message, processed_files, skipped_files, execution_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'WARNING: could not record the stop for execution '
              f'{execution_id}: {exc}')


def stop_message(processed, total, reason=None):
    """The one sentence every stopped run puts in its log. `reason` when the run
    stopped itself (a full disk) rather than being asked to."""
    why = f'Stopped: {reason},' if reason else 'Stopped by request'
    return (f'{why} after {processed} of {total} item(s). '
            f'Running this workflow again continues from here - what is already '
            f'done will be skipped.')
