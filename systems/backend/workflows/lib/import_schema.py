"""
Tables for the phased picture import (documentation/features/parallelism.md
section 4).

Called from two places, like idx_fc_sha256: CatalogDatabase.init_db() in the
backend, and the import workflow itself, because the workflow runs as its own
process against a database the backend may never have opened. Dependency-free
on purpose - models.py imports this, and must not pull in PIL or exiftool.
"""

# Ordered. The ordinal is the position in this tuple, plus one.
PHASES = ('scan', 'probe', 'resolve', 'place', 'render')

# How much of the progress bar each phase is worth (spec section 8.3). A guess
# until the first benchmark; tune so the bar moves roughly linearly in time.
PHASE_WEIGHTS = {'scan': 0.05, 'probe': 0.35, 'resolve': 0.05,
                 'place': 0.25, 'render': 0.30}

# Item states, by the phase that ends in them (spec section 4.2).
TERMINAL_STATES = ('ignored', 'already_imported', 'probe_failed', 'duplicate',
                   'place_failed', 'rendered', 'render_skipped', 'render_failed')
FAILED_STATES = ('probe_failed', 'place_failed', 'render_failed')

# Rows of a completed import are purged this long after it finished.
ITEM_RETENTION_DAYS = 30

# An import that was stopped, interrupted or failed keeps its rows for less
# time, and the reason is what those rows are FOR: the unplaced ones are the
# probe-adoption cache that makes resuming cheap (spec section 7.2), and nobody
# resumes a month-old import. Its placed rows hold nothing either way - their
# metadata is released the moment it reaches exif_data.
ITEM_RETENTION_ABANDONED_DAYS = 7

# The states that mean "this file is in the catalog now" - placed, and the
# render outcomes that follow placement. Defined HERE rather than in the
# workflow because the metadata-release rule below is stated in terms of it and
# both are the same fact. Must match io_picture_import_workflow._PLACED_STATES.
PLACED_STATES = ('placed', 'rendered', 'render_skipped', 'render_failed')


def release_placed_metadata(cursor, states=PLACED_STATES):
    """
    Drop `metadata_json` from every item that is already in the catalog, and
    return how many rows that was.

    THE DUPLICATION THIS REMOVES. metadata_json is the exiftool dump an item
    carries between PROBE and PLACE. `_record_placed` copies it into
    exif_raw.raw_exif_json - the permanent home, one row per file - and from
    that instant the item's copy is a second copy of the same bytes. It is also
    the bigger one in aggregate: one import of 340k files left 443,391 placed
    items holding 15.9 GB of metadata beside the 7.2 GB in exif_data.
    (family on oldserver, 2026-10-06.)
    *
    Safe by the adoption contract, not by luck: _adopt_probe refuses rows in
    these states ("Placed rows are not adopted - their metadata is cleared once
    it is in exif_data"), and SCAN's resume fast path skips the files outright.
    So no run, this one or a later one, can read what this clears.
    *
    match_keys_json is deliberately NOT cleared here. The grouping pass runs
    AFTER place and reads it from placed rows; it also averages ~20 bytes, so
    there is nothing to win by racing it.
    """
    marks = ','.join('?' * len(states))
    cur = cursor.execute(
        f'UPDATE import_items SET metadata_json=NULL '
        f'WHERE metadata_json IS NOT NULL AND state IN ({marks})', states)
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def prune_old_items(cursor):
    """
    Delete the items of imports that ended long enough ago, and return the
    count. Two windows, because the rows mean different things:

        completed  ITEM_RETENTION_DAYS       nothing needs them; the catalog has
                                             the files and the phase rows remain
        abandoned  ITEM_RETENTION_ABANDONED  their unplaced rows are a resume
                   _DAYS                     cache whose value expires

    A RUNNING execution is never touched, whatever its age looks like - the
    status column is the only guard that matters here, so it is explicit.
    """
    cur = cursor.execute(
        """DELETE FROM import_items WHERE execution_id IN (
               SELECT id FROM workflow_executions
                WHERE status = 'completed'
                  AND completed_at < datetime('now', ?)
               UNION
               SELECT id FROM workflow_executions
                WHERE status IN ('stopped', 'interrupted', 'failed')
                  AND COALESCE(completed_at, started_at) < datetime('now', ?))""",
        (f'-{ITEM_RETENTION_DAYS} days', f'-{ITEM_RETENTION_ABANDONED_DAYS} days'))
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def reclaimable_bytes(cursor):
    """
    Free pages the database is holding but not using, in bytes.

    SQLite does not shrink a file when rows are deleted or a column is nulled -
    the pages go on the freelist and are reused by the next insert. So after a
    release the file stays the same size and simply stops growing, which is the
    right trade for an import: VACUUM needs an exclusive lock and room for a
    second copy. This number is what a VACUUM would hand back.
    """
    page = cursor.execute('PRAGMA page_size').fetchone()[0]
    free = cursor.execute('PRAGMA freelist_count').fetchone()[0]
    return page * free


def ensure_import_schema(cursor):
    """Create import_items / import_phases and the executions column. Idempotent."""
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS import_items (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id     INTEGER NOT NULL,
            source_path      TEXT    NOT NULL,
            size             INTEGER,
            mtime_ns         INTEGER,
            -- 'image' | 'raw' | 'video' | 'other' | 'ignored'
            kind             TEXT    NOT NULL,
            state            TEXT    NOT NULL,
            -- probe
            probed           INTEGER NOT NULL DEFAULT 0,
            sha256           TEXT,
            metadata_json    TEXT,
            match_keys_json  TEXT,
            new_filename     TEXT,
            creation_ts      TEXT,
            date_source      TEXT,
            master_datetime  TEXT,
            master_dt_source TEXT,
            -- resolve: exactly one of these is set on a duplicate. The first
            -- names the file_contents row it matched, the second the item in
            -- this same run that won.
            duplicate_of     INTEGER,
            duplicate_item   INTEGER,
            group_key        TEXT,
            group_role       TEXT,
            -- on the primary: {kind, match_rule, match_evidence}, held until
            -- PLACE can write the image_groups row
            group_info       TEXT,
            -- place
            file_id          INTEGER,
            target_path      TEXT,
            derivative_stem  TEXT,
            staged_path      TEXT,
            phase_error      TEXT,
            updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ii_exec_state '
                   'ON import_items (execution_id, state, id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ii_exec_sha '
                   'ON import_items (execution_id, sha256)')
    # Probe adoption on resume (spec section 7.2).
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_ii_source '
                   'ON import_items (source_path, size, mtime_ns)')

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS import_phases (
            execution_id   INTEGER NOT NULL,
            phase          TEXT    NOT NULL,
            ordinal        INTEGER NOT NULL,
            -- 'pending'|'running'|'completed'|'stopped'|'failed'|'skipped'
            status         TEXT    NOT NULL,
            total          INTEGER,
            done           INTEGER NOT NULL DEFAULT 0,
            failed         INTEGER NOT NULL DEFAULT 0,
            skipped        INTEGER NOT NULL DEFAULT 0,
            bytes_done     INTEGER NOT NULL DEFAULT 0,
            workers        INTEGER,
            started_at     TEXT,
            finished_at    TEXT,
            updated_at     TEXT,
            rate_per_sec   REAL,
            detail_json    TEXT,
            PRIMARY KEY (execution_id, phase)
        )
    ''')

    # The copy probe made while hashing, relative to the catalog directory
    # (ingestion-performance-oldserver.md). Added to databases created before it.
    item_cols = {r[1] for r in cursor.execute('PRAGMA table_info(import_items)')}
    if 'staged_path' not in item_cols:
        cursor.execute('ALTER TABLE import_items ADD COLUMN staged_path TEXT')

    # Which folder an execution imported from, for the Import Progress report.
    # Written by the import itself, which is the one party that always knows.
    cols = {r[1] for r in cursor.execute('PRAGMA table_info(workflow_executions)')}
    if cols and 'source_directory' not in cols:
        cursor.execute('ALTER TABLE workflow_executions ADD COLUMN source_directory TEXT')
