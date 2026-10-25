#!/usr/bin/env python3
"""
ensure_workflow_queue_schema() must not take a write lock when there is
nothing to do.

Found while investigating the 2026-10-09 outage, and deliberately NOT sold as
its fix. Every failing request that day showed "database is locked" raised from
the CREATE TABLE in this function, which made it look like the culprit;
measured afterwards against a real concurrent writer, the old version returned
in 0.00 s. A no-op `CREATE TABLE IF NOT EXISTS` does not take a write lock.
The real cause is in
documentation/fixes/2026-10-09-backend-starved-by-a-wedged-workflow.md.

What remains true is that list_queue() is polled every couple of seconds by the
UI, and used to run DDL and a commit each time to find out there was nothing to
do. These tests pin it to reads.

    python -m pytest test_queue_schema_locking.py
"""

import os
import sqlite3
import sys
import threading
import time

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from workflow_queue_schema import (          # noqa: E402
    ensure_workflow_queue_schema,
    queue_schema_is_current,
)


@pytest.fixture
def catalog_db(tmp_path):
    """A catalog database with the queue schema already in place."""
    path = str(tmp_path / 'workflow_results.db')
    conn = sqlite3.connect(path)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.executescript("""
        CREATE TABLE workflows (id INTEGER PRIMARY KEY, name TEXT);
        CREATE TABLE workflow_executions (id INTEGER PRIMARY KEY, status TEXT);
    """)
    conn.commit()
    conn.close()
    ensure_workflow_queue_schema(path)
    return path


def test_the_first_call_builds_it(catalog_db):
    conn = sqlite3.connect(catalog_db)
    try:
        assert queue_schema_is_current(conn)
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table','index')")}
        assert 'workflow_queue' in names
        assert 'queue_settings' in names
    finally:
        conn.close()


def test_a_second_call_writes_nothing(catalog_db):
    """
    PRAGMA schema_version moves on any DDL. It must not move here: a call with
    nothing to do has to be a pure read, or the status page is a writer.
    """
    def version():
        c = sqlite3.connect(catalog_db)
        try:
            return c.execute('PRAGMA schema_version').fetchone()[0]
        finally:
            c.close()

    before = version()
    ensure_workflow_queue_schema(catalog_db)
    ensure_workflow_queue_schema(catalog_db)
    assert version() == before, \
        'the queue schema was rewritten although nothing was missing'


def test_it_does_not_block_behind_a_writer(catalog_db):
    """
    THE regression test. While another connection holds a write transaction -
    a detection pass mid-file - ensuring the schema must still return at once.

    Before the fix this waited for the writer and then raised
    "database is locked"; with a 60-second busy timeout in production, that is
    one backend thread gone per poll.
    """
    holder = sqlite3.connect(catalog_db, timeout=30)
    holder.execute('PRAGMA busy_timeout=30000')
    holder.execute('BEGIN IMMEDIATE')          # a writer, holding the lock
    holder.execute("INSERT INTO workflows (name) VALUES ('busy')")
    try:
        start = time.time()
        ensure_workflow_queue_schema(catalog_db)   # must not block
        elapsed = time.time() - start
        assert elapsed < 2.0, \
            f'took {elapsed:.1f}s behind a writer - it is still taking a lock'
    finally:
        holder.rollback()
        holder.close()


def test_many_concurrent_callers_behind_a_writer_all_succeed(catalog_db):
    """
    Eight threads, the backend's thread count, polling while a writer holds the
    database. Every one must come back; it was all eight hanging together that
    took the server down rather than any single slow call.
    """
    holder = sqlite3.connect(catalog_db, timeout=30)
    holder.execute('PRAGMA busy_timeout=30000')
    holder.execute('BEGIN IMMEDIATE')
    holder.execute("INSERT INTO workflows (name) VALUES ('busy')")

    errors = []
    done = []

    def poll():
        try:
            ensure_workflow_queue_schema(catalog_db)
            done.append(1)
        except Exception as e:            # noqa: BLE001 - the test IS the assert
            errors.append(e)

    try:
        threads = [threading.Thread(target=poll) for _ in range(8)]
        start = time.time()
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=15)
        elapsed = time.time() - start
    finally:
        holder.rollback()
        holder.close()

    assert not errors, f'callers failed behind a writer: {errors}'
    assert len(done) == 8, f'only {len(done)} of 8 finished'
    assert elapsed < 5.0, f'8 callers took {elapsed:.1f}s behind one writer'


def test_a_missing_column_is_still_added(catalog_db):
    """
    The guard must not turn into "never write": an older catalog really is
    missing not_before, and the whole point of this function is to add it.
    """
    conn = sqlite3.connect(catalog_db)
    conn.executescript("""
        DROP TABLE workflow_queue;
        CREATE TABLE workflow_queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            workflow_id INTEGER NOT NULL,
            workflow_name TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'queued',
            position INTEGER NOT NULL DEFAULT 0,
            priority INTEGER NOT NULL DEFAULT 0,
            trigger_source TEXT NOT NULL DEFAULT 'manual',
            -- coalesce_key is here because idx_wq_coalesce needs it; the
            -- columns this test removes are the two LATE ones, which is what
            -- an older catalog actually lacks.
            coalesce_key TEXT,
            chain_depth INTEGER NOT NULL DEFAULT 0
        );
    """)
    conn.commit()
    assert not queue_schema_is_current(conn), 'not_before is missing'
    conn.close()

    ensure_workflow_queue_schema(catalog_db)

    conn = sqlite3.connect(catalog_db)
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(workflow_queue)')}
        assert 'not_before' in cols
        assert 'stop_requested' in cols
        assert queue_schema_is_current(conn)
    finally:
        conn.close()
