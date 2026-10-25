"""
Machinery for the phased picture import (documentation/features/parallelism.md
sections 6 and 8): settings, the single writer thread, the progress tracker and
the pool runner. Nothing here knows what a photo is - the phases themselves live
in io_picture_import_workflow.py.

The rule everything below serves: worker threads never write to SQLite. They
return plain results, and ONE thread turns them into batched transactions. N
workers each committing per file is N writers queueing on one WAL lock with an
fsync apiece; one writer committing every 500 results is the same rows at a
five-hundredth of the commits.
"""

import json
import os
import queue
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, as_completed, wait
from datetime import datetime

from .import_schema import PHASES, PHASE_WEIGHTS


# ─── Settings (spec section 6.2) ─────────────────────────────────────────────

def _env_int(name, auto):
    """An integer setting where 0, empty or garbage means 'work it out'."""
    try:
        value = int(os.environ.get(name, '0') or 0)
    except ValueError:
        value = 0
    return value if value > 0 else auto


def _env_float(name, default):
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def load_settings():
    cpus = os.cpu_count() or 2
    return {
        'probe_workers': _env_int('IMPORT_PROBE_CONCURRENCY', min(8, cpus)),
        'probe_chunk': _env_int('IMPORT_PROBE_CHUNK', 64),
        'place_workers': _env_int('IMPORT_PLACE_CONCURRENCY', 4),
        # Copy each file into the catalog while probe hashes it, so place
        # renames instead of reading the source a second time. 0 turns it off,
        # so, like the snapshot setting below, this is not an _env_int.
        'stage_copies': os.environ.get('IMPORT_STAGE_COPIES', '1').strip().lower()
                        not in ('0', 'false', 'no', 'off'),
        'render_workers': _env_int('IMPORT_RENDER_CONCURRENCY', max(1, cpus - 1)),
        'render_executor': os.environ.get('IMPORT_RENDER_EXECUTOR', 'thread'),
        'video_workers': _env_int('IMPORT_VIDEO_CONCURRENCY', 1),
        'commit_batch': _env_int('IMPORT_COMMIT_BATCH', 500),
        'page_size': _env_int('IMPORT_PAGE_SIZE', 2000),
        # 0 is meaningful here - it switches snapshots off (spec S5) - so this
        # one is not an _env_int.
        'progress_flush_seconds': _env_float('IMPORT_PROGRESS_FLUSH_SECONDS', 2.0),
    }


def now_iso():
    return datetime.now().isoformat(timespec='seconds')


def connect(db_path):
    """A connection with the pragmas every catalogue connection needs."""
    # isolation_level=None: nothing is opened implicitly. The writer opens and
    # closes its transactions explicitly, so a batch is exactly one BEGIN ...
    # COMMIT; readers never hold a snapshot between statements.
    conn = sqlite3.connect(db_path, timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=30000')
    except sqlite3.Error:
        pass
    return conn


# ─── Progress (spec section 8) ───────────────────────────────────────────────

class Progress:
    """
    In-memory counters for every phase, written out by the Writer.

    Mutated only on the writer thread (inside the callables it runs) and by the
    orchestrator between phases, when the writer is idle - so no lock. Readers on
    other threads see a slightly stale int, which is all a progress bar needs.
    """

    def __init__(self, execution_id, flush_seconds, progress_callback=None):
        self.execution_id = execution_id
        self.flush_seconds = flush_seconds
        self.progress_callback = progress_callback
        self.current = None
        self.total_files = 0
        self.phases = {p: {'status': 'pending', 'total': None, 'done': 0,
                           'failed': 0, 'skipped': 0, 'bytes_done': 0,
                           'workers': None, 'started_at': None,
                           'finished_at': None, 'rate_per_sec': None,
                           'detail': {}}
                       for p in PHASES}
        self._last_flush = 0.0
        self._rate_mark = None     # (monotonic time, handled count) at the last flush
        self.dirty = False

    # -- counters (writer thread) --
    def bump(self, phase, done=0, failed=0, skipped=0, bytes_done=0, **detail):
        ph = self.phases[phase]
        ph['done'] += done
        ph['failed'] += failed
        ph['skipped'] += skipped
        ph['bytes_done'] += bytes_done
        for k, v in detail.items():
            ph['detail'][k] = ph['detail'].get(k, 0) + v
        self.dirty = True

    def handled(self, phase):
        ph = self.phases[phase]
        return ph['done'] + ph['failed'] + ph['skipped']

    # -- the one number for the existing progress bar (spec section 8.3) --
    def overall_fraction(self):
        frac = 0.0
        for p in PHASES:
            ph = self.phases[p]
            if ph['status'] in ('completed', 'skipped'):
                share = 1.0
            elif ph['total']:
                share = min(1.0, self.handled(p) / ph['total'])
            else:
                share = 0.0
            frac += PHASE_WEIGHTS[p] * share
        return frac

    def processed_files(self):
        return int(round(self.total_files * self.overall_fraction()))

    # -- persistence (writer thread, inside a transaction) --
    def insert_rows(self, conn):
        for i, p in enumerate(PHASES, start=1):
            conn.execute(
                'INSERT OR IGNORE INTO import_phases (execution_id, phase, ordinal, status) '
                'VALUES (?, ?, ?, ?)', (self.execution_id, p, i, 'pending'))

    def write_phase(self, conn, phase):
        ph = self.phases[phase]
        conn.execute(
            '''UPDATE import_phases SET status=?, total=?, done=?, failed=?, skipped=?,
                      bytes_done=?, workers=?, started_at=?, finished_at=?,
                      updated_at=?, rate_per_sec=?, detail_json=?
                WHERE execution_id=? AND phase=?''',
            (ph['status'], ph['total'], ph['done'], ph['failed'], ph['skipped'],
             ph['bytes_done'], ph['workers'], ph['started_at'], ph['finished_at'],
             now_iso(), ph['rate_per_sec'],
             json.dumps(ph['detail']) if ph['detail'] else None,
             self.execution_id, phase))

    def snapshot_due(self):
        return (self.flush_seconds > 0 and self.dirty
                and time.monotonic() - self._last_flush >= self.flush_seconds)

    def write_snapshot(self, conn):
        """The throttled write: current phase row + workflow_executions."""
        self._last_flush = time.monotonic()
        self.dirty = False
        if self.execution_id is None or self.current is None:
            return
        self._update_rate()
        self.write_phase(conn, self.current)
        conn.execute('UPDATE workflow_executions SET processed_files=?, total_files=? '
                     'WHERE id=?',
                     (self.processed_files(), self.total_files, self.execution_id))

    def _update_rate(self):
        """EWMA of items per second across flushes (spec section 8.2)."""
        phase = self.current
        now = time.monotonic()
        handled = self.handled(phase)
        if self._rate_mark and self._rate_mark[2] == phase and now > self._rate_mark[0]:
            inst = (handled - self._rate_mark[1]) / (now - self._rate_mark[0])
            prev = self.phases[phase]['rate_per_sec']
            self.phases[phase]['rate_per_sec'] = round(
                inst if prev is None else 0.3 * inst + 0.7 * prev, 3)
        self._rate_mark = (now, handled, phase)

    def after_commit(self):
        """Hook for callers that still want a callback (thread-mode launches)."""
        if self.progress_callback:
            try:
                self.progress_callback(self.processed_files(), self.total_files)
            except TypeError:
                self.progress_callback(self.processed_files())
            except Exception as exc:          # never fatal (see _db_update_progress)
                print(f'[progress] callback failed: {exc}', file=sys.stderr)


# ─── The single writer (spec section 6.1) ────────────────────────────────────

class WriterError(Exception):
    pass


class Writer:
    """
    Owns the only write connection. Callers submit `fn(conn)` callables; the
    writer buffers them and runs a buffer as ONE transaction when it holds
    `batch` items, when the oldest has waited a second, or when flush() asks.

    Buffering the callables rather than opening the transaction on the first one
    matters: the write lock is held only while the batch actually executes, not
    for the second spent waiting for it to fill, so the backend's own writes
    slip in between batches.

    Each callable runs inside a SAVEPOINT. One that raises is rolled back alone
    and its `on_error(conn, exc)` runs instead - one bad row never costs the
    other 499.
    """

    _FLUSH = object()
    _STOP = object()

    def __init__(self, db_path, progress, batch=500, max_wait=1.0):
        self.db_path = db_path
        self.progress = progress
        self.batch = batch
        self.max_wait = max_wait
        self._q = queue.Queue(maxsize=max(64, batch * 4))
        self._fatal = None
        # Optional hooks, used by move mode (ingest-move-spec.md section 4):
        # before_commit(conn) runs first inside every transaction; after_commit()
        # runs on this thread once a transaction is durable - the only point at
        # which a source file may be deleted. has_pending() forces a commit when
        # before_commit has something to write and nothing else is buffered.
        self.before_commit = None
        self.after_commit = None
        self.has_pending = None
        self._thread = threading.Thread(target=self._run, name='import-writer',
                                        daemon=True)
        self._thread.start()

    # -- producer side --
    def submit(self, fn, on_error=None):
        self._raise_if_dead()
        self._q.put((fn, on_error))

    def flush(self):
        """Block until everything submitted so far is committed."""
        self._raise_if_dead()
        done = threading.Event()
        self._q.put((self._FLUSH, done))
        while not done.wait(0.5):
            self._raise_if_dead()
        self._raise_if_dead()

    def call(self, fn):
        """Run fn(conn) on the writer, committed, and return its result."""
        box = {}

        def _fn(conn):
            box['value'] = fn(conn)
        self.submit(_fn)
        self.flush()
        return box.get('value')

    def close(self):
        if self._thread.is_alive():
            self._q.put((self._STOP, None))
            self._thread.join()
        self._raise_if_dead()

    def _raise_if_dead(self):
        if self._fatal is not None:
            raise WriterError(f'import writer failed: {self._fatal}') from self._fatal

    # -- writer thread --
    def _run(self):
        try:
            self._conn = connect(self.db_path)
            try:
                self._conn.execute('PRAGMA journal_mode=WAL')
            except sqlite3.Error:
                pass          # persistent anyway; see _wal_pragmas in the workflow
            self._loop()
        except BaseException as exc:          # surfaced to the producer
            self._fatal = exc
            # Unblock anyone waiting on a flush.
            while True:
                try:
                    item = self._q.get_nowait()
                except queue.Empty:
                    break
                if item[0] is self._FLUSH:
                    item[1].set()
        finally:
            try:
                self._conn.close()
            except Exception:
                pass

    def _loop(self):
        buf = []
        first_at = None
        while True:
            timeout = 0.25
            try:
                fn, extra = self._q.get(timeout=timeout)
            except queue.Empty:
                fn, extra = None, None

            if fn is self._STOP:
                self._commit(buf)
                return
            if fn is self._FLUSH:
                self._commit(buf, force_snapshot=True)
                buf, first_at = [], None
                extra.set()
                continue
            if fn is not None:
                buf.append((fn, extra))
                if first_at is None:
                    first_at = time.monotonic()

            waited = first_at is not None and time.monotonic() - first_at >= self.max_wait
            if len(buf) >= self.batch or waited:
                self._commit(buf)
                buf, first_at = [], None
            elif not buf and self.progress.snapshot_due():
                # Nothing is finishing (one long transcode), but rate and ETA
                # should stay live: a snapshot-only commit, once per interval.
                self._commit([])

    def _commit(self, buf, force_snapshot=False):
        snapshot = force_snapshot or self.progress.snapshot_due()
        if not buf and not snapshot and not (self.has_pending and self.has_pending()):
            return
        conn = self._conn
        for attempt in range(5):
            try:
                conn.execute('BEGIN IMMEDIATE')
                break
            except sqlite3.OperationalError:
                if attempt == 4:
                    raise
                time.sleep(1 + attempt)
        try:
            if self.before_commit:
                self.before_commit(conn)
            for fn, on_error in buf:
                conn.execute('SAVEPOINT item')
                try:
                    fn(conn)
                    conn.execute('RELEASE item')
                except Exception as exc:
                    conn.execute('ROLLBACK TO item')
                    conn.execute('RELEASE item')
                    if on_error is None:
                        print(f'[writer] dropped a write: {exc}', file=sys.stderr)
                        continue
                    conn.execute('SAVEPOINT item')
                    try:
                        on_error(conn, exc)
                        conn.execute('RELEASE item')
                    except Exception as exc2:
                        conn.execute('ROLLBACK TO item')
                        conn.execute('RELEASE item')
                        print(f'[writer] could not record a failure ({exc}): {exc2}',
                              file=sys.stderr)
            if snapshot:
                self.progress.write_snapshot(conn)
            conn.execute('COMMIT')
        except Exception:
            try:
                conn.execute('ROLLBACK')
            except sqlite3.Error:
                pass
            raise
        if self.after_commit:
            try:
                self.after_commit()
            except Exception as exc:
                print(f'[writer] after-commit step failed: {exc}', file=sys.stderr)
        if snapshot:
            self.progress.after_commit()


# ─── Pool runner (spec section 6.1) ──────────────────────────────────────────

def run_pool(units, work_fn, on_result, workers, stop_check, executor=None):
    """
    Feed `units` to `workers` threads, calling on_result(result) on THIS thread
    as each finishes. At most 2 × workers units are in flight, so a 300k-item
    phase holds a few dozen results in memory, not 300k.

    work_fn must never raise; it returns a failure result instead. Returns True
    when stop_check() cut the phase short. `executor` is the pool class -
    ThreadPoolExecutor unless render was switched to processes.
    """
    stopped = False
    with (executor or ThreadPoolExecutor)(max_workers=max(1, workers)) as pool:
        in_flight = set()
        for unit in units:
            while len(in_flight) >= workers * 2:
                done, in_flight = wait(in_flight, return_when=FIRST_COMPLETED)
                for f in done:
                    on_result(f.result())
            if stop_check():
                stopped = True
                break
            in_flight.add(pool.submit(work_fn, unit))
        for f in as_completed(in_flight):
            on_result(f.result())
    return stopped


def paged(conn, sql, params, ids, page_size):
    """
    Yield rows for `ids`, in the order given, fetched `page_size` at a time.

    The id list is taken once at the start of a phase - a few MB for 300k
    items - so the order is fixed even while the writer is changing states.
    """
    for start in range(0, len(ids), page_size):
        chunk = ids[start:start + page_size]
        marks = ','.join('?' * len(chunk))
        rows = {r['id']: r for r in conn.execute(
            sql.format(ids=marks), (*params, *chunk))}
        for i in chunk:
            if i in rows:
                yield rows[i]


def chunked(iterable, size):
    buf = []
    for x in iterable:
        buf.append(x)
        if len(buf) >= size:
            yield buf
            buf = []
    if buf:
        yield buf
