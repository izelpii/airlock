#!/usr/bin/env python3
"""
Schema for the workflow queue.

Two tables, in two different databases, for a reason:

  workflow_queue    lives in each CATALOG database, beside workflows and
                    workflow_executions, because a queue entry references a
                    workflow id that is catalog-scoped.

  scheduler_lease   lives in the APP database, because it is a property of the
                    running backend process, not of any catalog.

Both functions are idempotent and are called on startup rather than from a
standalone migration script. documentation/features/CONTAINERIZATION_TRADEOFFS.md
records why: a migration that only runs when someone remembers to run it, with a
container path in the catalog registry, silently did nothing for weeks.

See documentation/specs/workflow-scheduling-spec.md sections 4.1 and 4.3.
"""

import sqlite3


# Terminal states - a row in one of these will never run again.
#
# 'stopped' is deliberately its own state rather than a flavour of 'failed'
# (workflows-review-spec.md section 2.3): a stop is something the user did and
# can undo by resuming, a failure is something that went wrong. Only one of the
# two should offer a resume link, and only one belongs in a list of problems.
TERMINAL_STATUSES = ('completed', 'failed', 'cancelled', 'superseded', 'stopped')
ACTIVE_STATUSES = ('queued', 'blocked', 'running')


# Everything this module creates. Checked before anything is written, because
# that check is the difference between a read and a write - see below.
_QUEUE_OBJECTS = ('workflow_queue', 'queue_settings',
                  'idx_wq_status', 'idx_wq_coalesce')
_QUEUE_LATE_COLUMNS = ('not_before', 'stop_requested')


def queue_schema_is_current(conn):
    """
    True when every object below already exists, so nothing needs writing.

    Two cheap reads of sqlite_master and one PRAGMA, and they replace a run of
    DDL that TAKES A WRITE LOCK EVERY TIME - including when there is nothing
    whatsoever to do.
    """
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE name IN (?,?,?,?)",
        _QUEUE_OBJECTS).fetchall()
    if len({r[0] for r in rows}) < len(_QUEUE_OBJECTS):
        return False
    cols = {r[1] for r in conn.execute('PRAGMA table_info(workflow_queue)')}
    return all(c in cols for c in _QUEUE_LATE_COLUMNS)


def ensure_workflow_queue_schema(db, verbose=False):
    """
    Create the queue table in a catalog database. Accepts a path or a connection.

    Only writes when something is actually missing. The common call is three
    reads and no transaction at all.

    WHY, AND WHAT THIS IS *NOT*. list_queue() calls this, and the UI polls
    list_queue every couple of seconds, so it used to run `CREATE TABLE IF NOT
    EXISTS`, two `CREATE INDEX IF NOT EXISTS` and a `commit()` several times a
    minute per catalog, for ever, to discover each time that there was nothing
    to do. Not doing that is worth having on a path polled that often.

    It is NOT the cause of the 2026-10-09 outage, although this function is
    where the error surfaced - every failing request showed
    "database is locked" raised from the CREATE TABLE below, which made it look
    like the culprit. Measured afterwards, against a real concurrent writer:
    the old version returned in 0.00 s. A no-op `CREATE TABLE IF NOT EXISTS`
    does not take a write lock, so ordinary writer contention was never going
    to block it.

    What actually happened was worse and is recorded in
    documentation/fixes/2026-10-09-backend-starved-by-a-wedged-workflow.md: a
    detection pass wedged while holding an EXCLUSIVE lock, which blocked plain
    SELECTs too. This function was simply the first statement every request
    happened to run against that catalog.
    """
    owns = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(db, timeout=60) if owns else db

    try:
        if queue_schema_is_current(conn):
            if verbose:
                print('  = workflow_queue already current')
            return

        cur = conn.cursor()

        cur.execute('''
            CREATE TABLE IF NOT EXISTS workflow_queue (
                id               INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id      INTEGER NOT NULL,
                -- Denormalised so a queue row stays readable after its workflow is
                -- deleted; the FK cascade removes the row, but history views and
                -- in-flight socket payloads still want the name.
                workflow_name    TEXT    NOT NULL,
                script_name      TEXT,
                status           TEXT    NOT NULL DEFAULT 'queued',
                position         INTEGER NOT NULL DEFAULT 0,
                priority         INTEGER NOT NULL DEFAULT 0,
                trigger_source   TEXT    NOT NULL DEFAULT 'manual',
                trigger_detail   TEXT,
                coalesce_key     TEXT,
                depends_on       TEXT,
                chain_depth      INTEGER NOT NULL DEFAULT 0,
                source_directory TEXT,
                execution_id     INTEGER,
                enqueued_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                started_at       TIMESTAMP,
                finished_at      TIMESTAMP,
                error_message    TEXT,
                stop_requested   INTEGER NOT NULL DEFAULT 0,
                FOREIGN KEY (workflow_id)  REFERENCES workflows (id)           ON DELETE CASCADE,
                FOREIGN KEY (execution_id) REFERENCES workflow_executions (id) ON DELETE SET NULL
            )
        ''')
        # Quiet period. A row is not runnable until this time, and every further
        # trigger of the same kind pushes it further out. Added by ALTER for
        # databases created before it existed.
        cur.execute('PRAGMA table_info(workflow_queue)')
        _cols = {r[1] for r in cur.fetchall()}
        if 'not_before' not in _cols:
            try:
                cur.execute('ALTER TABLE workflow_queue ADD COLUMN not_before TIMESTAMP')
            except Exception as e:
                print(f'  ! could not add workflow_queue.not_before: {e}')

        # Mirrors workflow_executions.stop_requested so the queue panel can show
        # "stopping..." without joining to the execution row on every poll. The
        # execution column is the one the running script reads; this one is for the
        # UI (workflows-review-spec.md section 3.2).
        if 'stop_requested' not in _cols:
            try:
                cur.execute('ALTER TABLE workflow_queue '
                            'ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0')
            except Exception as e:
                print(f'  ! could not add workflow_queue.stop_requested: {e}')

        cur.execute('CREATE INDEX IF NOT EXISTS idx_wq_status   ON workflow_queue(status, priority DESC, position)')
        cur.execute('CREATE INDEX IF NOT EXISTS idx_wq_coalesce ON workflow_queue(coalesce_key, status)')

        # Per-catalog runtime flags. Pause has to survive a backend restart, or a
        # user who paused the queue and closed the tab comes back to it running.
        cur.execute('''
            CREATE TABLE IF NOT EXISTS queue_settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        conn.commit()
        if verbose:
            print('  + workflow_queue + queue_settings ensured')
    finally:
        if owns:
            conn.close()



def ensure_scheduler_lease_schema(db, verbose=False):
    """Create the lease table in the app database."""
    owns = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(db, timeout=60) if owns else db
    conn.execute('''
        CREATE TABLE IF NOT EXISTS scheduler_lease (
            id        INTEGER PRIMARY KEY CHECK (id = 1),
            owner     TEXT      NOT NULL,
            heartbeat TIMESTAMP NOT NULL
        )
    ''')
    conn.commit()
    if owns:
        conn.close()
    if verbose:
        print('  + scheduler_lease ensured')


if __name__ == '__main__':
    import os
    import sys
    if len(sys.argv) < 2:
        print('usage: workflow_queue_schema.py <workflow_results.db> [...]')
        sys.exit(2)
    for path in sys.argv[1:]:
        print(f'{path}:')
        ensure_workflow_queue_schema(path, verbose=True)
    # The app database (database/catalogs.db) is deliberately NOT touched here.
    # It uses a rollback journal rather than WAL, and writing to it from outside
    # the container - over a bind mount whose file locking is not reliable -
    # leaves a hot journal behind. app.py creates this table at startup, from
    # inside the container, where locking works. Run the backend rather than
    # this script.
