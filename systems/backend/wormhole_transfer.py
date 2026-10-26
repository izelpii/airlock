"""
wormhole_transfer.py — album transfer state, and the launch path for the runner.

See documentation/specs/wormhole-album-transfer-spec.md.

This module owns the two tables, the row helpers and the subprocess launch. The
bytes are moved by wormhole_runner.py, which runs as a separate process and touches
no database: the parent owns all state, which keeps SQLite single-writer.
"""

import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

# ── constants ─────────────────────────────────────────────────────────────────

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      'wormhole_runner.py')

# Progress is written to the database at most this often per transfer. A 4 GB file
# at 64 KB per chunk is 65 000 events; a row update per event would make SQLite the
# bottleneck in a network transfer (spec 6.3).
FLUSH_INTERVAL = 0.5

ACTIVE_STATUSES = ('pending', 'waiting_peer', 'connected', 'transferring')

# ── sending to several people (album-mass-transfer-spec.md) ──────────────────

# A batch is 2 to 20 people: one person is the ordinary Transfer button, and
# twenty waiting codes is ~1.5 GB of runners at 74 MB each (spec 2.3, 3.2).
MIN_BATCH = 2
MAX_BATCH = 20
# Across single and batched sends, per catalog: ~2.9 GB of waiting runners
# (spec 3.9).
MAX_ACTIVE_SENDS = 40
# The one deadline a batch shares. Rejected, not clamped, above this (3.3).
MAX_TTL_HOURS = 72
MAX_LABEL = 40

# The statuses a deadline ends. A member past `connected` has used its code,
# and a transfer in progress is never cut off by a clock (spec 3.4).
EXPIRABLE_STATUSES = ('pending', 'waiting_peer')
# The members "New codes" applies to (spec 3.6).
RENEWABLE_STATUSES = ('expired', 'failed', 'cancelled')

# Runners started by THIS process. The startup sweep (reap_orphans) skips them:
# it runs in a thread, and a transfer started in the first second after a
# restart must not be mistaken for one the previous process left behind.
_LAUNCHED_PIDS = set()


# ── schema ────────────────────────────────────────────────────────────────────

def ensure_schema(conn):
    """
    Both tables plus the index, idempotent.

    Called from CatalogDatabase.init_db, so a catalog gains them on the next open
    with nothing for the user to run.
    """
    conn.execute('''
        CREATE TABLE IF NOT EXISTS wormhole_transfers (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            direction      TEXT    NOT NULL,
            album_id       INTEGER,
            album_name     TEXT,
            status         TEXT    NOT NULL DEFAULT 'pending',
            wormhole_code  TEXT,
            destination    TEXT,
            route_id       INTEGER,
            total_files    INTEGER NOT NULL DEFAULT 0,
            total_bytes    INTEGER NOT NULL DEFAULT 0,
            done_files     INTEGER NOT NULL DEFAULT 0,
            done_bytes     INTEGER NOT NULL DEFAULT 0,
            skipped_bytes  INTEGER NOT NULL DEFAULT 0,
            pid            INTEGER,
            error_message  TEXT,
            started_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at   TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE TABLE IF NOT EXISTS wormhole_transfer_files (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            transfer_id  INTEGER NOT NULL,
            file_id      INTEGER,
            sha256       TEXT    NOT NULL,
            file_name    TEXT    NOT NULL,
            size_bytes   INTEGER NOT NULL DEFAULT 0,
            sent_bytes   INTEGER NOT NULL DEFAULT 0,
            status       TEXT    NOT NULL DEFAULT 'pending',
            error        TEXT,
            UNIQUE (transfer_id, sha256)
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_wh_files_transfer
        ON wormhole_transfer_files (transfer_id, status)
    ''')
    # Sending to several people (album-mass-transfer-spec.md section 5). A
    # member's deadline is the batch's, read through the join and never copied,
    # so moving it is one UPDATE.
    conn.execute('''
        CREATE TABLE IF NOT EXISTS wormhole_transfer_batches (
            id           INTEGER PRIMARY KEY AUTOINCREMENT,
            album_id     INTEGER NOT NULL,
            album_name   TEXT,
            expires_at   TIMESTAMP NOT NULL,
            created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            cancelled_at TIMESTAMP
        )
    ''')
    conn.execute('''
        CREATE INDEX IF NOT EXISTS idx_wh_batches_album
        ON wormhole_transfer_batches (album_id)
    ''')
    have = {r[1] for r in conn.execute('PRAGMA table_info(wormhole_transfers)')}
    for column, kind in (('batch_id', 'INTEGER'), ('recipient_label', 'TEXT'),
                         ('recipient_order', 'INTEGER')):
        if column not in have:
            conn.execute(f'ALTER TABLE wormhole_transfers ADD COLUMN {column} {kind}')


# ── reading ───────────────────────────────────────────────────────────────────

def get_transfer(catalog_db, transfer_id):
    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT * FROM wormhole_transfers WHERE id = ?',
                           (transfer_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_transfers(catalog_db, limit=50):
    conn = catalog_db.get_connection()
    try:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM wormhole_transfers ORDER BY id DESC LIMIT ?',
            (limit,)).fetchall()]
    finally:
        conn.close()


def list_files(catalog_db, transfer_id, page=1, per_page=50):
    """
    One page of the file list, plus the total.

    Paginated on the SERVER: an album of 5 000 photos must not send 5 000 rows to
    the browser to draw one progress bar (spec 6.2).
    """
    page = max(1, int(page or 1))
    per_page = min(500, max(1, int(per_page or 50)))
    conn = catalog_db.get_connection()
    try:
        total = conn.execute(
            'SELECT COUNT(*) FROM wormhole_transfer_files WHERE transfer_id = ?',
            (transfer_id,)).fetchone()[0]
        rows = conn.execute(
            'SELECT * FROM wormhole_transfer_files WHERE transfer_id = ? '
            'ORDER BY id LIMIT ? OFFSET ?',
            (transfer_id, per_page, (page - 1) * per_page)).fetchall()
        return {'files': [dict(r) for r in rows], 'total': total,
                'page': page, 'per_page': per_page}
    finally:
        conn.close()


def active_transfer_for_album(catalog_db, album_id):
    """
    The running SINGLE transfer for this album, if there is one (decision 3.6).

    Batch members are not counted: every receive has its own folder and state,
    so a batch and a single send of one album cannot collide on the far side,
    and neither blocks the other (album-mass-transfer-spec.md 3.8).
    """
    conn = catalog_db.get_connection()
    try:
        placeholders = ','.join('?' * len(ACTIVE_STATUSES))
        row = conn.execute(
            f'SELECT * FROM wormhole_transfers WHERE album_id = ? AND direction = '
            f"'send' AND batch_id IS NULL AND status IN ({placeholders}) "
            f'ORDER BY id DESC LIMIT 1',
            (album_id, *ACTIVE_STATUSES)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def payload(catalog_db, transfer_id):
    t = get_transfer(catalog_db, transfer_id)
    if not t:
        return None
    t['is_active'] = t['status'] in ACTIVE_STATUSES
    return t


# ── album contents ────────────────────────────────────────────────────────────

def album_files(catalog, catalog_db, album_id):
    """
    The album's ORIGINAL files, de-duplicated by file id.

    One photo in two sections is one file, and one transfer. Paths resolve against
    the ORIGINAL root, not target_directory: a reference-mode catalog's originals
    live outside the catalog by definition, and the wrong root would silently send
    nothing (spec 3.3).
    """
    import app                                    # circular at import time only
    root = app._original_root(catalog)
    conn = catalog_db.get_connection()
    try:
        rows = conn.execute('''
            SELECT DISTINCT fc.id, fc.file_name, fc.file_path, fc.file_size,
                            fc.sha256_hash
            FROM section_photos sp
            JOIN album_sections s ON s.id = sp.section_id
            JOIN file_contents fc ON fc.id = sp.file_id
            WHERE s.album_id = ?
            ORDER BY fc.id
        ''', (album_id,)).fetchall()
    finally:
        conn.close()

    out = []
    for r in rows:
        # No hash means no identity, and identity is what resume is built on. In
        # this installation every row has one, so this is a guard rather than a
        # branch anyone should hit.
        if not r['sha256_hash']:
            continue
        abs_path = r['file_path']
        if not os.path.isabs(abs_path):
            abs_path = os.path.join(root, abs_path)
        out.append({
            'file_id': r['id'],
            'sha256': r['sha256_hash'],
            'name': r['file_name'],
            'size': int(r['file_size'] or 0),
            'path': abs_path,
            'relpath': r['file_path'],
        })
    return out


# ── creating ──────────────────────────────────────────────────────────────────

def create_send_transfer(catalog, catalog_db, album, files):
    conn = catalog_db.get_connection()
    try:
        cur = conn.execute(
            '''INSERT INTO wormhole_transfers
               (direction, album_id, album_name, status, total_files, total_bytes)
               VALUES ('send', ?, ?, 'pending', ?, ?)''',
            (album['id'], album['name'], len(files),
             sum(f['size'] for f in files)))
        transfer_id = cur.lastrowid
        conn.executemany(
            '''INSERT OR IGNORE INTO wormhole_transfer_files
               (transfer_id, file_id, sha256, file_name, size_bytes)
               VALUES (?, ?, ?, ?, ?)''',
            [(transfer_id, f['file_id'], f['sha256'], f['name'], f['size'])
             for f in files])
        conn.commit()
        return transfer_id
    finally:
        conn.close()


def create_receive_transfer(catalog_db, code, route_id, destination):
    conn = catalog_db.get_connection()
    try:
        cur = conn.execute(
            '''INSERT INTO wormhole_transfers
               (direction, status, wormhole_code, route_id, destination)
               VALUES ('receive', 'pending', ?, ?, ?)''',
            (code, route_id, destination))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def update(catalog_db, transfer_id, **fields):
    if not fields:
        return
    fields['updated_at'] = datetime.now().isoformat(' ', 'seconds')
    sets = ', '.join(f'{k} = ?' for k in fields)
    conn = catalog_db.get_connection()
    try:
        conn.execute(f'UPDATE wormhole_transfers SET {sets} WHERE id = ?',
                     list(fields.values()) + [transfer_id])
        conn.commit()
    finally:
        conn.close()


# ── where a received album lands ──────────────────────────────────────────────
#
# ALWAYS the catalog's own directory, never a storage route and never /input.
#
# A storage route is an ingestion SOURCE. The tree is mounted read-write only so
# a move-mode import can take files out of a route an owner marked writable
# (ingest-move-spec.md 8.1); nothing else writes there, and a received album has
# no business in someone's source folder. /input is a source too, and is
# deliberately left alone.
#
# The catalog directory is writable in BOTH ingest modes - a reference-mode
# catalog reads its originals in place but still writes derivatives, albums and
# deployments here - so it is the one place a receive can always land, and the
# received files travel with the catalog folder like everything else in it.

def transfers_root(catalog):
    """`<catalog>/transfers` — the parent of every received transfer."""
    return os.path.join(catalog.target_directory or '', 'transfers')


def transfer_state_dir(catalog, transfer_id):
    """
    Where a receive keeps its partial files and its completed-file record.

    Under `cache/`, NOT inside the transfer folder. The transfer folder is what a
    workflow will ingest, and a partial file there would be imported as a corrupt
    original: media_types.is_ignored_file() tests the basename, and a blob named
    by its sha256 has no extension and no leading dot to be ignored by.
    """
    return os.path.join(catalog.target_directory or '', 'cache', 'Transfers',
                        f'{int(transfer_id):05d}')


def transfer_dir(catalog, transfer_id, label=None):
    """
    One folder per transfer, named by its id and optionally labelled.

    The id leads and is zero-padded like an album folder, because it is what
    makes the name unique: the album's own name is only known once the peer
    sends its manifest, which is after this directory has to exist. A label the
    user typed is appended when there is one, so the folder is still readable
    from a file browser.

    Nothing else is written into it - no marker file, no metadata - because the
    next step is a workflow that ingests this directory, and everything in here
    should be a photo.
    """
    name = f'{int(transfer_id):05d}'
    slug = slugify(label) if label else ''
    if slug and slug != 'album':
        name = f'{name}-{slug}'
    return os.path.join(transfers_root(catalog), name)


SLUG_RE = re.compile(r'[^a-z0-9]+')


def slugify(name):
    s = SLUG_RE.sub('-', (name or '').lower()).strip('-')
    return s or 'album'


# ── launching, and the watcher that turns events into state ───────────────────

def launch(catalog_db, transfer_id, mode, emit, manifest=None, code=None,
           destination=None, scratch_dir=None, state_dir=None, expires_file=None,
           on_completed=None):
    """
    Start the runner and the thread that reads it.

    `emit` is socketio.emit, passed in rather than imported: this module is
    imported BY app.py, and importing back would be a cycle.

    Returns the pid. Like a workflow's, the pid is stored rather than held: the
    backend may be replaced while a transfer keeps going, and a value in the
    database survives that where an object in memory does not.

    `on_completed(transfer_id)` is called once the transfer ends `completed` -
    for a receive, app.py queues the catalog's transfers import with it.
    """
    cmd = [sys.executable, '-u', RUNNER, '--mode', mode,
           '--transfer-id', str(transfer_id)]
    manifest_path = None
    if mode == 'send':
        manifest_path = os.path.join(
            scratch_dir or '/tmp', f'wh-manifest-{transfer_id}.json')
        with open(manifest_path, 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh)
        cmd += ['--manifest', manifest_path]
        if expires_file:
            cmd += ['--expires-file', expires_file]
    else:
        cmd += ['--code', code, '--dest', destination]
        if state_dir:
            cmd += ['--state-dir', state_dir]

    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, text=True, bufsize=1)
    _LAUNCHED_PIDS.add(proc.pid)
    update(catalog_db, transfer_id, pid=proc.pid, status='waiting_peer')

    thread = threading.Thread(
        target=_watch, args=(catalog_db, transfer_id, proc, emit, manifest_path,
                             on_completed),
        daemon=True)
    thread.start()
    return proc.pid


def _watch(catalog_db, transfer_id, proc, emit, manifest_path, on_completed=None):
    """
    Read the runner's events, keep the database current, mirror to the browser.

    Throttled: `file_progress` arrives once per 64 KB chunk, and both the row
    update and the socket emit ride the same half-second tick. Terminal events
    (a file finishing, an error) are never throttled - a bar that lags is fine,
    a file that never shows as done is not.
    """
    state = {'done_files': 0, 'done_bytes': 0, 'skipped_bytes': 0,
             'current': None, 'status': 'waiting_peer'}
    base_bytes = {'total': 0}
    last_flush = [0.0]

    def flush(force=False):
        now = time.time()
        if not force and now - last_flush[0] < FLUSH_INTERVAL:
            return
        last_flush[0] = now
        update(catalog_db, transfer_id,
               done_files=state['done_files'],
               done_bytes=state['done_bytes'] + base_bytes['total'],
               skipped_bytes=state['skipped_bytes'],
               status=state['status'])
        _emit(emit, 'transfer_progress', {
            'transfer_id': transfer_id,
            'status': state['status'],
            'done_files': state['done_files'],
            'done_bytes': state['done_bytes'] + base_bytes['total'],
            'skipped_bytes': state['skipped_bytes'],
            'current': state['current'],
        })

    def set_file(sha, **fields):
        if not fields:
            return
        sets = ', '.join(f'{k} = ?' for k in fields)
        conn = catalog_db.get_connection()
        try:
            conn.execute(
                f'UPDATE wormhole_transfer_files SET {sets} '
                f'WHERE transfer_id = ? AND sha256 = ?',
                list(fields.values()) + [transfer_id, sha])
            conn.commit()
        finally:
            conn.close()

    error_message = None
    incomplete = [None]
    expired = [False]
    try:
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue
            if not line.startswith('{'):
                # Twisted writes tracebacks here. Keep them in the log, out of
                # the event stream.
                print(f'[wormhole {transfer_id}] {line}', flush=True)
                continue
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            name = ev.get('event')

            if name == 'code':
                update(catalog_db, transfer_id, wormhole_code=ev['code'],
                       status='waiting_peer')
                _emit(emit, 'transfer_progress', {
                    'transfer_id': transfer_id, 'status': 'waiting_peer',
                    'code': ev['code']})

            elif name == 'connected':
                state['status'] = 'transferring'
                flush(force=True)

            elif name == 'manifest':
                # Receiving side: the file list only exists once the peer sends
                # it, so the rows are created here rather than at start.
                _receive_manifest(catalog_db, transfer_id, ev)
                _emit(emit, 'transfer_files_ready', {'transfer_id': transfer_id})

            elif name == 'file_start':
                state['current'] = {'sha256': ev['sha256'], 'name': ev['name'],
                                    'sent_bytes': ev.get('offset', 0),
                                    'size_bytes': ev.get('size', 0)}
                # A resumed file's already-present bytes count as done from the
                # first tick, so the bar does not jump backwards on resume.
                base_bytes['total'] += ev.get('offset', 0)
                set_file(ev['sha256'], status='sending',
                         sent_bytes=ev.get('offset', 0))
                flush(force=True)

            elif name == 'file_progress':
                if state['current'] and state['current']['sha256'] == ev['sha256']:
                    delta = ev['sent_bytes'] - state['current']['sent_bytes']
                    state['current']['sent_bytes'] = ev['sent_bytes']
                    state['done_bytes'] += max(0, delta)
                flush()

            elif name == 'file_done':
                state['done_files'] += 1
                state['current'] = None
                set_file(ev['sha256'], status='done',
                         sent_bytes=ev.get('size', 0))
                _emit(emit, 'transfer_file', {
                    'transfer_id': transfer_id, 'sha256': ev['sha256'],
                    'status': 'done', 'sent_bytes': ev.get('size', 0)})
                flush(force=True)

            elif name == 'file_skipped':
                state['done_files'] += 1
                state['skipped_bytes'] += ev.get('size', 0)
                base_bytes['total'] += ev.get('size', 0)
                set_file(ev['sha256'], status='skipped',
                         sent_bytes=ev.get('size', 0))
                _emit(emit, 'transfer_file', {
                    'transfer_id': transfer_id, 'sha256': ev['sha256'],
                    'status': 'skipped', 'sent_bytes': ev.get('size', 0)})
                flush(force=True)

            elif name == 'file_failed':
                set_file(ev['sha256'], status='failed',
                         error=ev.get('message'))
                _emit(emit, 'transfer_file', {
                    'transfer_id': transfer_id, 'sha256': ev['sha256'],
                    'status': 'failed', 'error': ev.get('message')})

            elif name == 'published':
                # The album is in place. Only now does anything exist under
                # transfers/ for a workflow to find.
                print(f'[wormhole {transfer_id}] published to '
                      f'{ev.get("destination")} '
                      f'({"atomic rename" if ev.get("atomic") else "per-file move"})',
                      flush=True)

            elif name == 'note':
                # Not a failure and not progress: something the log should carry
                # but the transfer's status should not change for.
                print(f'[wormhole {transfer_id}] {ev.get("message")}', flush=True)

            elif name == 'incomplete':
                # Staged, not published: a partial album must never appear in
                # the folder a workflow ingests. Resume continues from here.
                incomplete[0] = (f"{ev.get('missing')} file(s) did not arrive; "
                                 f"nothing was published. Resume to continue.")

            elif name == 'finished':
                state['status'] = 'completed'
                flush(force=True)

            elif name == 'expired':
                # Nobody used the code by its deadline; the runner has closed
                # the wormhole (album-mass-transfer-spec.md 3.5).
                expired[0] = True

            elif name == 'error':
                error_message = ev.get('message')

        proc.wait()
    except Exception as exc:                       # pragma: no cover
        error_message = str(exc)
    finally:
        if manifest_path and os.path.exists(manifest_path):
            try:
                os.remove(manifest_path)
            except OSError:
                pass

    row = get_transfer(catalog_db, transfer_id) or {}
    if incomplete[0] and not error_message:
        error_message = incomplete[0]
    if row.get('status') == 'cancelled':
        final = 'cancelled'
    elif expired[0] and not error_message:
        final = 'expired'
    elif error_message or proc.returncode not in (0, None):
        final = 'failed'
        error_message = error_message or f'runner exited {proc.returncode}'
    else:
        final = 'completed'
    update(catalog_db, transfer_id, status=final, error_message=error_message,
           completed_at=datetime.now().isoformat(' ', 'seconds'))
    _emit(emit, 'transfer_done', {'transfer_id': transfer_id,
                                  'status': final, 'error': error_message})
    if final == 'completed' and on_completed is not None:
        try:
            on_completed(transfer_id)
        except Exception as exc:                   # pragma: no cover
            print(f'[wormhole {transfer_id}] after-completion step failed: {exc}',
                  flush=True)


def _receive_manifest(catalog_db, transfer_id, ev):
    """Create the file rows a receiving transfer learns about from its peer."""
    files = ev.get('files') or []
    album = ev.get('album') or {}
    conn = catalog_db.get_connection()
    try:
        conn.executemany(
            '''INSERT OR IGNORE INTO wormhole_transfer_files
               (transfer_id, sha256, file_name, size_bytes)
               VALUES (?, ?, ?, ?)''',
            [(transfer_id, f['sha256'], f['name'], f.get('size') or 0)
             for f in files])
        conn.execute(
            'UPDATE wormhole_transfers SET album_name = ?, total_files = ?, '
            'total_bytes = ? WHERE id = ?',
            (album.get('name'), len(files),
             sum(f.get('size') or 0 for f in files), transfer_id))
        conn.commit()
    finally:
        conn.close()


def _emit(emit, event, data):
    """socketio.emit, with a browser-less backend treated as normal, not an error."""
    try:
        emit(event, data)
    except Exception:
        pass


def cancel(catalog_db, transfer_id):
    """
    Stop a running transfer.

    SIGTERM, not SIGKILL: the runner closes the wormhole so the peer learns the
    session ended instead of waiting on a socket that will never speak again.
    Partial files are deliberately left in place - they are what the next resume
    starts from.
    """
    row = get_transfer(catalog_db, transfer_id)
    if not row:
        return False
    update(catalog_db, transfer_id, status='cancelled',
           completed_at=datetime.now().isoformat(' ', 'seconds'))
    pid = row.get('pid')
    if pid:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
    return True


# ── sending to several people ─────────────────────────────────────────────────
#
# documentation/specs/album-mass-transfer-spec.md.
#
# A batch is one row in wormhole_transfer_batches plus N ORDINARY send rows in
# wormhole_transfers pointing at it. Each member is a complete transfer - its own
# runner, code, status, file rows, Cancel - so none of the protocol, the runner's
# transfer logic or the transfer view changes (spec 3.1). What the batch adds is
# a name for the group, the labels, and the one deadline.
#
# These functions own the rows. Launching the runners is app.py's job: it holds
# the catalog, the manifest builder and socketio.emit.

class BatchError(ValueError):
    """A request the batch rules refuse; the message is shown to the user."""


def utc_now():
    return datetime.now(timezone.utc)


def to_utc_text(dt):
    """'2026-10-26T18:05:00Z' - unambiguous to the browser and to SQLite text order."""
    return dt.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def from_utc_text(text):
    return datetime.strptime(text, '%Y-%m-%dT%H:%M:%SZ').replace(tzinfo=timezone.utc)


def check_ttl(ttl_hours):
    """Hours until the deadline: more than 0, at most 72 (spec 3.3)."""
    try:
        hours = float(ttl_hours)
    except (TypeError, ValueError):
        raise BatchError('Choose when the codes expire.')
    if hours <= 0:
        raise BatchError('Choose when the codes expire.')
    if hours > MAX_TTL_HOURS:
        raise BatchError(f'Codes can wait at most {MAX_TTL_HOURS} hours (3 days).')
    return hours


def clean_label(label):
    label = str(label if label is not None else '').strip()
    if len(label) > MAX_LABEL:
        raise BatchError(f'A label can be at most {MAX_LABEL} characters.')
    return label


def display_label(row):
    """The label as typed, or `Person k` - never stored, always derived (3.2)."""
    return row.get('recipient_label') or f"Person {row.get('recipient_order') or '?'}"


def _active_send_count(conn):
    marks = ','.join('?' * len(ACTIVE_STATUSES))
    return conn.execute(
        f"SELECT COUNT(*) FROM wormhole_transfers WHERE direction = 'send' "
        f'AND status IN ({marks})', ACTIVE_STATUSES).fetchone()[0]


def _check_room(conn, adding):
    active = _active_send_count(conn)
    if active + adding > MAX_ACTIVE_SENDS:
        raise BatchError(
            f'{active} send(s) are already waiting or running in this catalog, '
            f'and at most {MAX_ACTIVE_SENDS} can be at once. Stop some on the '
            f'Transfers page, or wait for them to finish.')


def _insert_member(conn, batch_id, album, files, label, order):
    cur = conn.execute(
        '''INSERT INTO wormhole_transfers
           (direction, album_id, album_name, status, total_files, total_bytes,
            batch_id, recipient_label, recipient_order)
           VALUES ('send', ?, ?, 'pending', ?, ?, ?, ?, ?)''',
        (album['id'], album['name'], len(files), sum(f['size'] for f in files),
         batch_id, label, order))
    transfer_id = cur.lastrowid
    conn.executemany(
        '''INSERT OR IGNORE INTO wormhole_transfer_files
           (transfer_id, file_id, sha256, file_name, size_bytes)
           VALUES (?, ?, ?, ?, ?)''',
        [(transfer_id, f['file_id'], f['sha256'], f['name'], f['size'])
         for f in files])
    return transfer_id


def get_batch(catalog_db, batch_id):
    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT * FROM wormhole_transfer_batches WHERE id = ?',
                           (batch_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def batch_members(catalog_db, batch_id):
    conn = catalog_db.get_connection()
    try:
        return [dict(r) for r in conn.execute(
            'SELECT * FROM wormhole_transfers WHERE batch_id = ? '
            'ORDER BY recipient_order, id', (batch_id,)).fetchall()]
    finally:
        conn.close()


def open_batch_for_album(catalog_db, album_id):
    """
    The album's batch that still has someone to reach, if any (spec 3.8).

    Open means not stopped and not every member completed. A batch with expired
    members is still open: the user renews there rather than starting another.
    """
    conn = catalog_db.get_connection()
    try:
        row = conn.execute(
            '''SELECT b.* FROM wormhole_transfer_batches b
               WHERE b.album_id = ? AND b.cancelled_at IS NULL
                 AND EXISTS (SELECT 1 FROM wormhole_transfers t
                             WHERE t.batch_id = b.id AND t.status != 'completed')
               ORDER BY b.id DESC LIMIT 1''', (album_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_batch(catalog_db, album, files, labels, ttl_hours):
    """
    The batch row and one member per label, in one transaction.

    `labels` is one string per person, empty for "Person k". Returns
    {batch_id, transfer_ids, expires_at}; nothing is launched here.
    """
    if not isinstance(labels, (list, tuple)):
        raise BatchError('Say who the album is for.')
    if not MIN_BATCH <= len(labels) <= MAX_BATCH:
        raise BatchError(f'Send to {MIN_BATCH} to {MAX_BATCH} people at a time. '
                         f'For one person, use Transfer.')
    labels = [clean_label(l) for l in labels]
    hours = check_ttl(ttl_hours)
    expires_at = to_utc_text(utc_now() + timedelta(hours=hours))

    conn = catalog_db.get_connection()
    try:
        _check_room(conn, len(labels))
        cur = conn.execute(
            'INSERT INTO wormhole_transfer_batches (album_id, album_name, expires_at) '
            'VALUES (?, ?, ?)', (album['id'], album['name'], expires_at))
        batch_id = cur.lastrowid
        ids = [_insert_member(conn, batch_id, album, files, label, k)
               for k, label in enumerate(labels, start=1)]
        conn.commit()
    finally:
        conn.close()
    return {'batch_id': batch_id, 'transfer_ids': ids, 'expires_at': expires_at}


def add_to_batch(catalog_db, batch_id, album, files, label):
    """One more person, on the batch's deadline (spec 3.8). Returns the id."""
    label = clean_label(label)
    conn = catalog_db.get_connection()
    try:
        batch = conn.execute('SELECT * FROM wormhole_transfer_batches WHERE id = ?',
                             (batch_id,)).fetchone()
        if not batch:
            raise BatchError('That group no longer exists.')
        if batch['cancelled_at']:
            raise BatchError('This group was stopped. Start a new one from the album.')
        if from_utc_text(batch['expires_at']) <= utc_now():
            raise BatchError('The codes in this group have expired. Make new codes '
                             'first; the new person then gets the new deadline.')
        n, last = conn.execute(
            'SELECT COUNT(*), COALESCE(MAX(recipient_order), 0) '
            'FROM wormhole_transfers WHERE batch_id = ?', (batch_id,)).fetchone()
        if n >= MAX_BATCH:
            raise BatchError(f'A group can hold at most {MAX_BATCH} people.')
        _check_room(conn, 1)
        transfer_id = _insert_member(conn, batch_id, album, files, label, last + 1)
        conn.commit()
        return transfer_id
    finally:
        conn.close()


def renew_batch(catalog_db, batch_id, ttl_hours, transfer_ids=None):
    """
    New codes: reset the chosen members for a fresh runner, move the deadline.

    `transfer_ids` None means every member that is expired, failed or cancelled;
    a list means those members, each of which must be one of them. The deadline
    moves for the WHOLE batch - members still waiting adopt it through the expiry
    file the caller rewrites (spec 3.6). Returns {transfer_ids, expires_at}; the
    caller launches them.
    """
    hours = check_ttl(ttl_hours)
    conn = catalog_db.get_connection()
    try:
        batch = conn.execute('SELECT * FROM wormhole_transfer_batches WHERE id = ?',
                             (batch_id,)).fetchone()
        if not batch:
            raise BatchError('That group no longer exists.')
        members = [dict(r) for r in conn.execute(
            'SELECT id, status FROM wormhole_transfers WHERE batch_id = ?',
            (batch_id,)).fetchall()]
        renewable = {m['id'] for m in members if m['status'] in RENEWABLE_STATUSES}
        if transfer_ids is None:
            chosen = sorted(renewable)
        else:
            chosen = sorted({int(t) for t in transfer_ids})
            stray = [t for t in chosen if t not in renewable]
            if stray:
                raise BatchError('Only someone whose code expired, failed or was '
                                 'stopped can be given a new one.')
        if not chosen:
            raise BatchError('Nobody in this group needs a new code.')
        _check_room(conn, len(chosen))

        expires_at = to_utc_text(utc_now() + timedelta(hours=hours))
        conn.execute('UPDATE wormhole_transfer_batches SET expires_at = ?, '
                     'cancelled_at = NULL WHERE id = ?', (expires_at, batch_id))
        marks = ','.join('?' * len(chosen))
        # The same reset Resume does (app.py, resume_album_transfer): same rows,
        # same files, a fresh session. The peer's inventory decides what moves.
        conn.execute(
            f"UPDATE wormhole_transfers SET status = 'pending', error_message = NULL, "
            f'wormhole_code = NULL, completed_at = NULL, done_files = 0, '
            f'done_bytes = 0, skipped_bytes = 0, pid = NULL, '
            f"updated_at = ? WHERE id IN ({marks})",
            [datetime.now().isoformat(' ', 'seconds'), *chosen])
        conn.commit()
    finally:
        conn.close()
    return {'transfer_ids': chosen, 'expires_at': expires_at}


def cancel_batch(catalog_db, batch_id):
    """Stop all: every member still waiting or running is cancelled."""
    conn = catalog_db.get_connection()
    try:
        conn.execute('UPDATE wormhole_transfer_batches SET cancelled_at = ? '
                     'WHERE id = ?', (datetime.now().isoformat(' ', 'seconds'),
                                      batch_id))
        conn.commit()
    finally:
        conn.close()
    stopped = 0
    for m in batch_members(catalog_db, batch_id):
        if m['status'] in ACTIVE_STATUSES:
            cancel(catalog_db, m['id'])
            stopped += 1
    return stopped


def _summary(members):
    counts = {}
    for m in members:
        counts[m['status']] = counts.get(m['status'], 0) + 1
    return counts


def batch_payload(catalog_db, batch_id):
    """
    The batch, its deadline, and its members in order - what the batch view draws.

    A member's code is included only while it is waiting: once used it is spent,
    and showing a spent code invites someone to type it (spec 7.3).
    """
    batch = get_batch(catalog_db, batch_id)
    if not batch:
        return None
    members = batch_members(catalog_db, batch_id)
    out = []
    for m in members:
        m['label'] = display_label(m)
        m['is_active'] = m['status'] in ACTIVE_STATUSES
        if m['status'] not in EXPIRABLE_STATUSES:
            m['wormhole_code'] = None
        m.pop('pid', None)
        out.append(m)
    batch['members'] = out
    batch['counts'] = _summary(members)
    batch['expired'] = from_utc_text(batch['expires_at']) <= utc_now()
    batch['is_open'] = (not batch['cancelled_at']
                        and any(m['status'] != 'completed' for m in members))
    batch['max_people'] = MAX_BATCH
    return batch


def list_batches(catalog_db, limit=50):
    """Every batch with its member counts - one history entry each (7.4)."""
    conn = catalog_db.get_connection()
    try:
        batches = [dict(r) for r in conn.execute(
            'SELECT * FROM wormhole_transfer_batches ORDER BY id DESC LIMIT ?',
            (limit,)).fetchall()]
        for b in batches:
            members = [dict(r) for r in conn.execute(
                'SELECT status FROM wormhole_transfers WHERE batch_id = ?',
                (b['id'],)).fetchall()]
            b['people'] = len(members)
            b['counts'] = _summary(members)
            b['is_active'] = any(m['status'] in ACTIVE_STATUSES for m in members)
            # The same test as open_batch_for_album, so the album card and the
            # endpoint agree on whether this album already has a group.
            b['is_open'] = (not b['cancelled_at'] and
                            any(m['status'] != 'completed' for m in members))
            b['expired'] = from_utc_text(b['expires_at']) <= utc_now()
        return batches
    finally:
        conn.close()


# ── the deadline file ─────────────────────────────────────────────────────────

def expiry_file(scratch_dir, batch_id):
    """One per batch, read by every member's runner (wormhole_runner.Deadline)."""
    return os.path.join(scratch_dir, f'wh-batch-{int(batch_id)}.expires')


def write_expiry_file(scratch_dir, batch_id, expires_at):
    """Atomically, so a runner re-reading it never sees half a number."""
    path = expiry_file(scratch_dir, batch_id)
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(str(int(from_utc_text(expires_at).timestamp())))
    os.replace(tmp, path)
    return path


# ── after a restart, and on reading ───────────────────────────────────────────

def _is_runner(pid):
    """
    True only if `pid` is alive AND is a wormhole runner.

    The command line is checked because a pid can be reused: after a container
    restart the stored number may belong to anything, and signalling the wrong
    process is worse than leaving a stray one (spec 3.7). No /proc - a test, or a
    macOS host - means "cannot tell", which is treated as "not ours".
    """
    if not pid:
        return False
    try:
        with open(f'/proc/{int(pid)}/cmdline', 'rb') as fh:
            return b'wormhole_runner.py' in fh.read()
    except (OSError, ValueError):
        return False


RESTART_WAITING = 'Airlock restarted while this code was waiting. Generate a new code.'
RESTART_RUNNING = 'Airlock restarted while this transfer was running. Resume to continue.'


def reap_orphans(conn):
    """
    At start-up: every transfer the previous process left active is ended.

    Its watcher thread died with that process, so nothing would ever update the
    row again, and a waiting runner would die on its next write anyway (spec 2.6).
    A runner still alive is sent SIGTERM; the row becomes `expired` if it was a
    batch member waiting past its deadline, otherwise `failed` with a message
    that says what to do (spec 3.7). Takes a plain connection, like
    publish.interrupt_stale. Returns how many rows it changed.
    """
    ensure_schema(conn)
    conn.row_factory = None
    marks = ','.join('?' * len(ACTIVE_STATUSES))
    rows = conn.execute(
        f'''SELECT t.id, t.status, t.pid, b.expires_at
            FROM wormhole_transfers t
            LEFT JOIN wormhole_transfer_batches b ON b.id = t.batch_id
            WHERE t.status IN ({marks})''', ACTIVE_STATUSES).fetchall()
    now = utc_now()
    stamp = datetime.now().isoformat(' ', 'seconds')
    changed = 0
    for transfer_id, status, pid, expires_at in rows:
        if pid in _LAUNCHED_PIDS:
            continue
        if _is_runner(pid):
            try:
                os.kill(pid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        waiting = status in EXPIRABLE_STATUSES
        if waiting and expires_at and from_utc_text(expires_at) <= now:
            final, message = 'expired', None
        else:
            final = 'failed'
            message = RESTART_WAITING if waiting else RESTART_RUNNING
        conn.execute('UPDATE wormhole_transfers SET status = ?, error_message = ?, '
                     'completed_at = ?, updated_at = ? WHERE id = ?',
                     (final, message, stamp, stamp, transfer_id))
        changed += 1
    conn.commit()
    return changed


def sweep(catalog_db):
    """
    Mark `expired` any waiting member past its deadline whose runner is gone.

    The runner expires its own code (spec 3.5); this is the backstop for a row
    whose runner died without saying so. Cheap - one indexed query - so it runs
    on every list and batch read.
    """
    conn = catalog_db.get_connection()
    try:
        marks = ','.join('?' * len(EXPIRABLE_STATUSES))
        rows = conn.execute(
            f'''SELECT t.id, t.pid, b.expires_at FROM wormhole_transfers t
                JOIN wormhole_transfer_batches b ON b.id = t.batch_id
                WHERE t.status IN ({marks}) AND b.expires_at <= ?''',
            (*EXPIRABLE_STATUSES, to_utc_text(utc_now()))).fetchall()
        stamp = datetime.now().isoformat(' ', 'seconds')
        n = 0
        for r in rows:
            if _is_runner(r['pid']):
                continue
            conn.execute("UPDATE wormhole_transfers SET status = 'expired', "
                         'completed_at = ?, updated_at = ? WHERE id = ?',
                         (stamp, stamp, r['id']))
            n += 1
        if n:
            conn.commit()
        return n
    finally:
        conn.close()
