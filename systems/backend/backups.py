"""
Catalog backups - the Backup tab's cards. documentation/specs/backup-backrest-spec.md.

A card is one destination for one catalog: a restic repository (on another
machine - backup-host), its password, a scope, a schedule and a retention. Airlock
runs restic itself, here in the backend, where the catalogs are; Backrest, if
any, lives only on the repository's machine, to browse and maintain it.

What runs where:

  backup   restic backup of the scope's paths, tagged airlock / catalog:<slug> /
           card:<id>, then `restic forget` with the card's retention - its own
           snapshots only. Never `prune`: that is the repository machine's job
           (backup-host's Backrest), so two programs never compete to rewrite it.
  verify   restic check.
  restore  restic restore into a folder outside the catalog (spec 7).

Each is a row in backup_runs while it runs (progress included) and after, so a
card's status survives a restart; a run cut off by one is marked interrupted.

The database is copied first (spec 4.2): workflow_results.db is live and in WAL
mode, so VACUUM INTO writes a consistent copy to cache/backup/ and the backup
carries that copy, never the live file.

Airlock schedules the runs (scheduler thread in app.py), in each card's own
timezone, and waits while an import is running.

Types. Each backup type is an engine module, chosen by the card's `type`:
restic (backup_restic.py, the code above and below), borg (backup_borg.py,
the `_borg_*` functions) and rsync (backup_rsync.py, the `_rsync_*` functions). The rows, the schedule, the status,
Stop and the database copy are shared; only running the tool differs, so a
new type adds a module and its branch without touching the others.
"""

import json
import re
import os
import shutil
import signal
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:                        # pragma: no cover - Python < 3.9
    ZoneInfo = None

import backup_borg
import backup_rsync
import backup_secrets
from backup_restic import Repo, ResticError, _explain

TYPES = ('restic', 'borg', 'rsync')
SCOPES = ('originals', 'everything')
ORIGINALS_DIRS = ('Images', 'Videos', 'Others')
SNAPSHOT_DIRNAME = os.path.join('cache', 'backup')
CATALOG_DB = 'workflow_results.db'
SNAPSHOT_DB = 'workflow_results.snapshot.db'
APP_SNAPSHOT_DB = 'catalogs.snapshot.db'

DEFAULT_SCHEDULE = {'kind': 'daily', 'time': '02:00', 'weekday': 6}
DEFAULT_RETENTION = {'daily': 7, 'weekly': 4, 'monthly': 12, 'yearly': 3}
DEFAULT_GRACE_HOURS = 24
PROGRESS_EVERY = 3.0                       # seconds between progress writes
RETRY_LOCK = '30m'                         # how long a run waits for another's lock

SCHEMA = '''
    CREATE TABLE IF NOT EXISTS backup_targets (
        id                     INTEGER PRIMARY KEY AUTOINCREMENT,
        catalog_id             INTEGER NOT NULL,
        name                   TEXT NOT NULL,
        type                   TEXT NOT NULL DEFAULT 'restic',
        scope                  TEXT NOT NULL DEFAULT 'originals',
        repo_uri               TEXT NOT NULL,      -- sftp:user@host:/path, a local path, rest:...
        repo_secret_enc        TEXT,               -- the repository password, encrypted
        repo_env_enc           TEXT,               -- extra environment (cloud keys), encrypted JSON
        options_json           TEXT,               -- per type, not secret: borg's remote_path, rsync's root
        schedule_json          TEXT,
        schedule_tz            TEXT,
        retention_json         TEXT,
        grace_hours            INTEGER NOT NULL DEFAULT 24,
        -- the last known state, so a card renders without asking anything
        last_backup_at         TEXT,
        last_backup_status     TEXT,               -- success | failed | interrupted | cancelled
        last_snapshot_id       TEXT,
        last_files_new         INTEGER,
        last_data_added        INTEGER,
        last_error             TEXT,
        last_fingerprint       TEXT,               -- the catalog as the last good backup saw it
        last_run_started_at    TEXT,               -- what the scheduler compares against
        last_verify_at         TEXT,
        last_verify_status     TEXT,
        created_by             INTEGER,
        created_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at             TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        UNIQUE (catalog_id, name)
    )
'''

RUNS_SCHEMA = '''
    CREATE TABLE IF NOT EXISTS backup_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        target_id     INTEGER NOT NULL,
        kind          TEXT NOT NULL,               -- backup | verify | restore
        status        TEXT NOT NULL,               -- running | success | failed | interrupted | cancelled
        phase         TEXT,
        started_at    TEXT NOT NULL,
        ended_at      TEXT,
        progress_json TEXT,
        summary_json  TEXT,
        error         TEXT,
        detail        TEXT                         -- what was restored, and where to
    )
'''


def ensure_schema(cursor):
    # v5.8.x drove Backrest and had a server_url column. Its table is replaced;
    # one that holds cards is kept aside rather than dropped.
    cols = {r[1] for r in cursor.execute('PRAGMA table_info(backup_targets)')}
    if 'server_url' in cols:
        n = cursor.execute('SELECT COUNT(*) FROM backup_targets').fetchone()[0]
        if n:
            cursor.execute('ALTER TABLE backup_targets RENAME TO backup_targets_backrest')
        else:
            cursor.execute('DROP TABLE backup_targets')
    cursor.execute(SCHEMA)
    cols = {r[1] for r in cursor.execute('PRAGMA table_info(backup_targets)')}
    if 'options_json' not in cols:                 # added with borg (v5.10)
        cursor.execute('ALTER TABLE backup_targets ADD COLUMN options_json TEXT')
    cursor.execute(RUNS_SCHEMA)
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_backup_targets_catalog '
                   'ON backup_targets(catalog_id)')
    cursor.execute('CREATE INDEX IF NOT EXISTS idx_backup_runs_target '
                   'ON backup_runs(target_id, id)')


def instance_id():
    """This installation (workflow_scheduler's id, database/instance_id), for
    restic's --host: two Airlocks backing up a catalog of the same name into
    one repository never forget each other's snapshots."""
    import workflow_scheduler
    return workflow_scheduler.instance_id()


def host_name():
    return f'airlock-{instance_id()[:12]}'


# ── rows ─────────────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt):
    return dt.isoformat() if dt else None


def _parse(iso):
    if not iso:
        return None
    try:
        dt = datetime.fromisoformat(str(iso).replace('Z', '+00:00').replace(' ', 'T'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def list_targets(conn, catalog_id=None):
    conn.row_factory = sqlite3.Row
    if catalog_id is None:
        return [dict(r) for r in conn.execute('SELECT * FROM backup_targets ORDER BY id')]
    return [dict(r) for r in conn.execute(
        'SELECT * FROM backup_targets WHERE catalog_id = ? ORDER BY id', (catalog_id,))]


def get_target(conn, catalog_id, target_id):
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM backup_targets WHERE id = ? AND catalog_id = ?',
                       (target_id, catalog_id)).fetchone()
    return dict(row) if row else None


def update_target(conn, target_id, **fields):
    if not fields:
        return
    cols = ', '.join(f'{k} = ?' for k in fields)
    conn.execute(f'UPDATE backup_targets SET {cols}, updated_at = CURRENT_TIMESTAMP '
                 'WHERE id = ?', (*fields.values(), target_id))
    conn.commit()


def delete_target(conn, target_id):
    conn.execute('DELETE FROM backup_runs WHERE target_id = ?', (target_id,))
    conn.execute('DELETE FROM backup_targets WHERE id = ?', (target_id,))
    conn.commit()


def latest_runs(conn, target_id, limit=10):
    conn.row_factory = sqlite3.Row
    return [dict(r) for r in conn.execute(
        'SELECT * FROM backup_runs WHERE target_id = ? ORDER BY id DESC LIMIT ?',
        (target_id, limit))]


def interrupt_stale_runs(conn):
    """At start-up: a run still marked running died with the previous process."""
    now = _iso(_now())
    conn.execute("UPDATE backup_targets SET last_backup_status = 'interrupted', "
                 "last_error = 'The backend restarted during the backup' "
                 "WHERE id IN (SELECT target_id FROM backup_runs "
                 "WHERE status = 'running' AND kind = 'backup')")
    cur = conn.execute("UPDATE backup_runs SET status = 'interrupted', ended_at = ?, "
                       "error = COALESCE(error, 'The backend restarted while this ran') "
                       "WHERE status = 'running'", (now,))
    conn.commit()
    return cur.rowcount


def schedule_of(target):
    try:
        s = json.loads(target.get('schedule_json') or '')
    except ValueError:
        s = {}
    return {**DEFAULT_SCHEDULE, **(s or {})}


def retention_of(target):
    try:
        r = json.loads(target.get('retention_json') or '')
    except ValueError:
        r = {}
    return {**DEFAULT_RETENTION, **(r or {})}


def options_of(target):
    try:
        return json.loads(target.get('options_json') or '{}') or {}
    except ValueError:
        return {}


def make_repo(kind, uri, password, env=None, options=None):
    """The engine's repository object for a card's type."""
    if kind == 'borg':
        return backup_borg.Repo(uri, password, env, options)
    if kind == 'rsync':
        return backup_rsync.Repo(uri, None, None, options)
    return Repo(uri, password, env)


def repo_for(target, secret_key):
    if target.get('type') == 'rsync':                # no password: a plain copy
        return backup_rsync.Repo(target['repo_uri'], None, None, options_of(target))
    env = backup_secrets.decrypt(secret_key, target.get('repo_env_enc'))
    if target.get('type') == 'borg':
        return backup_borg.Repo(target['repo_uri'],
                                backup_secrets.decrypt(secret_key, target.get('repo_secret_enc')),
                                json.loads(env) if env else [], options_of(target))
    return Repo(target['repo_uri'],
                backup_secrets.decrypt(secret_key, target.get('repo_secret_enc')),
                json.loads(env) if env else [])


def ident_for(target, catalog_slug):
    """Who a borg archive belongs to - its name prefix (backup_borg.archive_prefix)."""
    return {'host': host_name(), 'catalog': catalog_slug, 'card': target['id']}


def tags_for(target, catalog_slug):
    return ['airlock', f'catalog:{catalog_slug}', f'card:{target["id"]}']


# ── what a backup carries ────────────────────────────────────────────────────

def paths_for(target, local_root):
    """(paths, excludes) for restic, as this process sees the catalog."""
    root = local_root.rstrip('/')
    snap = f'{root}/{SNAPSHOT_DIRNAME}'
    if target['scope'] == 'everything':
        return [root], [f'{root}/{CATALOG_DB}', f'{root}/{CATALOG_DB}-wal',
                        f'{root}/{CATALOG_DB}-shm', f'{root}/tmp',
                        '**/.import-staging', f'{snap}/*.tmp']
    # Originals AND the database copy: without it a restore is 370,751 renamed
    # files and none of the names, faces, events or albums (spec 4.1).
    paths = [f'{root}/{d}' for d in ORIGINALS_DIRS if os.path.isdir(os.path.join(root, d))]
    return paths + [snap], ['**/.import-staging', f'{snap}/*.tmp']


def prepare_snapshot(local_root, app_db_path=None):
    """
    A consistent copy of the catalog database - and of the application's -
    in cache/backup/, for the run that follows (spec 4.2, 4.3).

    VACUUM INTO reads inside one transaction, so writers carry on; the copy is
    written under a .tmp name and renamed, so a half-written file is never what
    gets backed up. Refuses when the disk lacks room for it.
    """
    src = os.path.join(local_root, CATALOG_DB)
    out_dir = os.path.join(local_root, SNAPSHOT_DIRNAME)
    os.makedirs(out_dir, exist_ok=True)
    need = sum(os.path.getsize(src + s) for s in ('', '-wal') if os.path.exists(src + s))
    free = shutil.disk_usage(out_dir).free
    if free < need * 1.1 + 512 * 2**20:
        raise RuntimeError(
            f'Not enough free space to copy the catalog database before the backup: '
            f'it needs {need / 1e9:.1f} GB and {free / 1e9:.1f} GB is free.')
    final = os.path.join(out_dir, SNAPSHOT_DB)
    tmp = final + '.tmp'
    if os.path.exists(tmp):
        os.remove(tmp)
    conn = sqlite3.connect(src, timeout=60)
    try:
        conn.execute('VACUUM INTO ?', (tmp,))
    finally:
        conn.close()
    os.replace(tmp, final)
    if app_db_path and os.path.exists(app_db_path):
        app_final = os.path.join(out_dir, APP_SNAPSHOT_DB)
        src_conn = sqlite3.connect(app_db_path, timeout=30)
        dst_conn = sqlite3.connect(app_final + '.tmp')
        try:
            src_conn.backup(dst_conn)
        finally:
            dst_conn.close()
            src_conn.close()
        os.replace(app_final + '.tmp', app_final)
    return os.path.getsize(final)


# Filesystems whose inode numbers are made up as files are read, not stored:
# the same file can have a different one on the next run. restic and borg both
# take a changed inode to mean "maybe changed" and read the whole file again -
# on homeserver, whose photos are on an exFAT drive, every backup re-read 2.7 TB
# (8 h) after the first had finished (2026-10-21).
UNSTABLE_INODE_FS = {'exfat', 'vfat', 'msdos', 'fat', 'ntfs', 'fuseblk',
                     'cifs', 'smb3', 'smbfs', '9p'}


def _mounts(path='/proc/mounts'):
    """[(mount point, filesystem type)], longest mount point first."""
    out = []
    try:
        with open(path) as f:
            for line in f:
                parts = line.split()
                if len(parts) >= 3:
                    # /proc/mounts escapes spaces and the like as \040 octal.
                    point = re.sub(r'\\([0-7]{3})', lambda m: chr(int(m.group(1), 8)), parts[1])
                    out.append((point, parts[2]))
    except OSError:
        pass
    return sorted(out, key=lambda m: len(m[0]), reverse=True)


def fs_type(path, mounts=None):
    """The filesystem a path is on, or None."""
    real = os.path.realpath(path)
    for point, kind in (mounts if mounts is not None else _mounts()):
        if real == point or real.startswith(point.rstrip('/') + '/'):
            return kind
    return None


def unstable_inodes(paths, mounts=None):
    """True when any path is on a filesystem without stable inode numbers."""
    mounts = mounts if mounts is not None else _mounts()
    for p in paths:
        kind = fs_type(p, mounts) or ''
        if kind in UNSTABLE_INODE_FS or kind.startswith('fuse'):
            return True
    return False


def _copy_catalog_db(conn, run_id, local_root, app_db_path):
    """
    prepare_snapshot, with its progress on the card: VACUUM INTO is one long
    statement (about 15 minutes for a 44 GB catalog), so a watcher reads the
    growing .tmp file's size meanwhile. The copy leaves out free pages, so the
    percentage is "about" and stops short of 100 until the copy is done.
    """
    _update_run(conn, run_id, phase='Copying the catalog database', progress_json=None)
    src = os.path.join(local_root, CATALOG_DB)
    total = sum(os.path.getsize(src + x) for x in ('', '-wal') if os.path.exists(src + x))
    tmp = os.path.join(local_root, SNAPSHOT_DIRNAME, SNAPSHOT_DB + '.tmp')
    stop = threading.Event()

    def watch():
        if not app_db_path or not total:
            return
        c = sqlite3.connect(app_db_path, timeout=30)
        try:
            while not stop.wait(PROGRESS_EVERY):
                try:
                    done = os.path.getsize(tmp)
                except OSError:
                    continue
                _update_run(c, run_id, progress_json=json.dumps({
                    'what': 'copy', 'percent': min(99.0, round(100 * done / total, 1)),
                    'bytes_done': done, 'bytes_total': total}))
        except sqlite3.Error:
            pass
        finally:
            c.close()

    watcher = threading.Thread(target=watch, name=f'backup-copy-{run_id}', daemon=True)
    watcher.start()
    try:
        return prepare_snapshot(local_root, app_db_path)
    finally:
        stop.set()
        watcher.join(timeout=10)
        _update_run(conn, run_id, progress_json=None)


# ── runs ─────────────────────────────────────────────────────────────────────

_active = {}               # target id -> {'kind', 'proc', 'cancel'}
_active_lock = threading.Lock()


class Cancelled(Exception):
    pass


def active(target_id):
    with _active_lock:
        a = _active.get(target_id)
        return dict(a) if a else None


def _claim(target_id, kind):
    with _active_lock:
        if target_id in _active:
            return False
        _active[target_id] = {'kind': kind, 'proc': None, 'cancel': False}
        return True


def _release(target_id):
    with _active_lock:
        _active.pop(target_id, None)


def _cancelled(target_id):
    return bool((active(target_id) or {}).get('cancel'))


def cancel(target_id):
    """Stop the run in progress: restic stops on SIGINT and leaves the
    repository consistent; the next backup reuses what was uploaded."""
    with _active_lock:
        a = _active.get(target_id)
        if not a:
            return False
        a['cancel'] = True
        proc = a.get('proc')
    if proc and proc.poll() is None:
        proc.send_signal(signal.SIGINT)
    return True


def _new_run(conn, target_id, kind, detail=None):
    cur = conn.execute('INSERT INTO backup_runs (target_id, kind, status, phase, started_at, detail) '
                       "VALUES (?, ?, 'running', 'Starting', ?, ?)",
                       (target_id, kind, _iso(_now()), detail))
    conn.commit()
    return cur.lastrowid


def _update_run(conn, run_id, **fields):
    # Every progress write carries when it was made: the card shows it, so a
    # long quiet stretch (a big file, a slow disk) still reads as alive.
    if fields.get('progress_json'):
        try:
            p = json.loads(fields['progress_json'])
            p['at'] = _iso(_now())
            fields['progress_json'] = json.dumps(p)
        except (ValueError, TypeError):
            pass
    cols = ', '.join(f'{k} = ?' for k in fields)
    conn.execute(f'UPDATE backup_runs SET {cols} WHERE id = ?', (*fields.values(), run_id))
    conn.commit()


def _stream(target_id, conn, run_id, proc, on_summary=None):
    """Read restic's --json lines: progress into the run row (throttled), the
    summary to on_summary. Returns (returncode, stderr)."""
    with _active_lock:
        if target_id in _active:
            _active[target_id]['proc'] = proc
    if _cancelled(target_id):
        proc.send_signal(signal.SIGINT)
    # stderr is drained alongside, so a chatty restic never blocks on a full pipe.
    err = []
    drain = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    drain.start()
    last = 0.0
    for line in proc.stdout:
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get('message_type')
        if kind in ('status', 'verbose_status') and time.monotonic() - last >= PROGRESS_EVERY:
            last = time.monotonic()
            _update_run(conn, run_id, progress_json=json.dumps({
                'percent': round(100 * float(msg.get('percent_done') or 0), 1),
                'bytes_done': msg.get('bytes_done') or msg.get('bytes_restored') or 0,
                'bytes_total': msg.get('total_bytes') or 0,
                'files_done': msg.get('files_done') or msg.get('files_restored') or 0,
                'files_total': msg.get('total_files') or 0,
                'seconds_remaining': msg.get('seconds_remaining'),
                # What restic is reading right now - the visible sign of work
                # when the numbers barely move (a resumed run re-reading files
                # the repository already holds).
                'current': (msg.get('current_files') or [None])[0]}))
        elif kind == 'summary' and on_summary:
            on_summary(msg)
    rc = proc.wait()
    drain.join(timeout=10)
    if _cancelled(target_id):
        raise Cancelled()
    return rc, ''.join(err)


def _run_thread(target, kind, connect, work):
    def run():
        conn = connect()
        try:
            work(conn)
        except Exception as exc:                         # already on the run row
            print(f'[backup] {target["name"]} {kind}: {exc}')
        finally:
            conn.close()
            _release(target['id'])
    threading.Thread(target=run, name=f'backup-{kind}-{target["id"]}', daemon=True).start()


def _end(conn, run_id, status, error=None, summary=None):
    _update_run(conn, run_id, status=status, ended_at=_iso(_now()), error=error,
                phase=None, summary_json=json.dumps(summary) if summary else None)


def start_backup(target, catalog_slug, local_root, fingerprint, secret_key, app_db_path, connect):
    """Copy the database, back up, apply the retention - on a thread. Returns
    False when this card already has something running."""
    if target.get('type') == 'borg':
        return _borg_backup(target, catalog_slug, local_root, fingerprint, secret_key,
                            app_db_path, connect)
    if target.get('type') == 'rsync':
        return _rsync_backup(target, local_root, fingerprint, app_db_path, connect)
    if not _claim(target['id'], 'backup'):
        return False

    def work(conn):
        run_id = _new_run(conn, target['id'], 'backup')
        update_target(conn, target['id'], last_run_started_at=_iso(_now()))
        summary = {}
        try:
            _copy_catalog_db(conn, run_id, local_root, app_db_path)
            if _cancelled(target['id']):
                raise Cancelled()
            repo = repo_for(target, secret_key)
            paths, excludes = paths_for(target, local_root)
            # backup-host's prune locks the repository for a while: wait, don't fail.
            args = ['backup', '--json', '--retry-lock', RETRY_LOCK, '--host', host_name()]
            if unstable_inodes(paths):
                # Size and modification time decide "unchanged", as restic's
                # documentation advises for such filesystems; otherwise every
                # run reads every file again.
                args.append('--ignore-inode')
            for t in tags_for(target, catalog_slug):
                args += ['--tag', t]
            for e in excludes:
                args += ['--exclude', e]
            _update_run(conn, run_id, phase='Backing up')
            rc, stderr = _stream(target['id'], conn, run_id, repo.popen(*args, *paths),
                                 on_summary=summary.update)
            # 3 = some files could not be read: the snapshot exists, incomplete.
            if rc not in (0, 3):
                raise _explain(stderr, target['repo_uri'])
            _update_run(conn, run_id, phase='Applying the retention', progress_json=None)
            try:
                repo.forget(tags_for(target, catalog_slug), retention_of(target))
            except ResticError as e:
                # The snapshot is safe; a busy repository only delays forgetting.
                summary['forget_error'] = str(e)
            warning = ('Some files could not be read: ' + stderr.strip()[-300:]) if rc == 3 else None
            _end(conn, run_id, 'success', error=warning, summary=summary)
            update_target(conn, target['id'],
                          last_backup_at=_iso(_now()), last_backup_status='success',
                          last_snapshot_id=summary.get('snapshot_id'),
                          last_files_new=summary.get('files_new'),
                          last_data_added=summary.get('data_added'),
                          last_fingerprint=fingerprint, last_error=warning)
        except Cancelled:
            _end(conn, run_id, 'cancelled', error='Stopped')
            update_target(conn, target['id'], last_backup_status='cancelled', last_error=None)
        except Exception as exc:
            _end(conn, run_id, 'failed', error=str(exc)[:500])
            update_target(conn, target['id'], last_backup_status='failed',
                          last_error=str(exc)[:500])
            raise

    _run_thread(target, 'backup', connect, work)
    return True


def start_verify(target, secret_key, connect, read_subset=None, local_root=None):
    if target.get('type') == 'borg':
        return _borg_verify(target, secret_key, connect)
    if target.get('type') == 'rsync':
        return _rsync_verify(target, local_root, connect)
    if not _claim(target['id'], 'verify'):
        return False

    def work(conn):
        run_id = _new_run(conn, target['id'], 'verify')
        try:
            _update_run(conn, run_id, phase='Checking the repository')
            args = ['check', '--json', '--retry-lock', RETRY_LOCK] + ([f'--read-data-subset={read_subset}'] if read_subset else [])
            rc, stderr = _stream(target['id'], conn, run_id,
                                 repo_for(target, secret_key).popen(*args))
            if rc != 0:
                raise _explain(stderr, target['repo_uri'])
            _end(conn, run_id, 'success')
            update_target(conn, target['id'], last_verify_at=_iso(_now()),
                          last_verify_status='success')
        except Cancelled:
            _end(conn, run_id, 'cancelled', error='Stopped')
        except Exception as exc:
            _end(conn, run_id, 'failed', error=str(exc)[:500])
            update_target(conn, target['id'], last_verify_at=_iso(_now()),
                          last_verify_status='failed')
            raise

    _run_thread(target, 'verify', connect, work)
    return True


def start_restore(target, secret_key, connect, snapshot_id, include, restore_dir,
                  local_root=None):
    if target.get('type') == 'borg':
        return _borg_restore(target, secret_key, connect, snapshot_id, include, restore_dir)
    if target.get('type') == 'rsync':
        return _rsync_restore(target, connect, include, restore_dir, local_root)
    if not _claim(target['id'], 'restore'):
        return False

    def work(conn):
        run_id = _new_run(conn, target['id'], 'restore',
                          detail=json.dumps({'snapshot': snapshot_id, 'path': include,
                                             'target': restore_dir}))
        try:
            os.makedirs(restore_dir, exist_ok=True)
            _update_run(conn, run_id, phase='Restoring')
            args = ['restore', '--json', snapshot_id, '--target', restore_dir]
            if include:
                args += ['--include', include]
            rc, stderr = _stream(target['id'], conn, run_id,
                                 repo_for(target, secret_key).popen(*args))
            if rc != 0:
                raise _explain(stderr, target['repo_uri'])
            _end(conn, run_id, 'success')
        except Cancelled:
            _end(conn, run_id, 'cancelled', error='Stopped')
        except Exception as exc:
            _end(conn, run_id, 'failed', error=str(exc)[:500])
            raise

    _run_thread(target, 'restore', connect, work)
    return True


# ── borg (backup_borg.py) ────────────────────────────────────────────────────
#
# The same runs as restic's above - a backup_runs row, progress, Stop, the card
# updated at the end - for borg's way of reporting: progress and messages are
# JSON lines on stderr (--log-json), the result is JSON on stdout.

def _stream_borg(target_id, conn, run_id, proc):
    """Returns (returncode, stdout, the log messages as text)."""
    with _active_lock:
        if target_id in _active:
            _active[target_id]['proc'] = proc
    if _cancelled(target_id):
        proc.send_signal(signal.SIGINT)
    out = []
    drain = threading.Thread(target=lambda: out.append(proc.stdout.read()), daemon=True)
    drain.start()
    log, last = [], 0.0
    for line in proc.stderr:
        ev = backup_borg.parse_event(line)
        if ev is None:
            if line.strip():
                log.append(line.rstrip())
        elif ev[0] == 'log':
            log.append(ev[1])
        elif time.monotonic() - last >= PROGRESS_EVERY:
            last = time.monotonic()
            _update_run(conn, run_id, progress_json=json.dumps(ev[1]))
    rc = proc.wait()
    drain.join(timeout=10)
    if _cancelled(target_id):
        raise Cancelled()
    return rc, ''.join(out), '\n'.join(log[-200:])


def _borg_backup(target, catalog_slug, local_root, fingerprint, secret_key, app_db_path, connect):
    if not _claim(target['id'], 'backup'):
        return False

    def work(conn):
        run_id = _new_run(conn, target['id'], 'backup')
        update_target(conn, target['id'], last_run_started_at=_iso(_now()))
        try:
            _copy_catalog_db(conn, run_id, local_root, app_db_path)
            if _cancelled(target['id']):
                raise Cancelled()
            repo = repo_for(target, secret_key)
            ident = ident_for(target, catalog_slug)
            paths, excludes = paths_for(target, local_root)
            _update_run(conn, run_id, phase='Backing up')
            rc, stdout, log = _stream_borg(target['id'], conn, run_id,
                                           repo.popen(*repo.create_args(
                                               ident, paths, excludes,
                                               stable_inodes=not unstable_inodes(paths))))
            # 1 = warnings (a file changed or vanished while read): the archive exists.
            if rc not in (0, backup_borg.WARNING):
                raise backup_borg._explain(log, target['repo_uri'])
            summary = backup_borg.create_summary(stdout)
            _update_run(conn, run_id, phase='Applying the retention', progress_json=None)
            try:
                repo.prune(ident, retention_of(target))
            except backup_borg.BorgError as e:
                summary['forget_error'] = str(e)
            warning = ('Finished with warnings: ' + log.strip()[-300:]) \
                if rc == backup_borg.WARNING else None
            _end(conn, run_id, 'success', error=warning, summary=summary)
            update_target(conn, target['id'],
                          last_backup_at=_iso(_now()), last_backup_status='success',
                          last_snapshot_id=summary.get('snapshot_id'),
                          last_files_new=None, last_data_added=summary.get('data_added'),
                          last_fingerprint=fingerprint, last_error=warning)
        except Cancelled:
            _end(conn, run_id, 'cancelled', error='Stopped')
            update_target(conn, target['id'], last_backup_status='cancelled', last_error=None)
        except Exception as exc:
            _end(conn, run_id, 'failed', error=str(exc)[:500])
            update_target(conn, target['id'], last_backup_status='failed',
                          last_error=str(exc)[:500])
            raise

    _run_thread(target, 'backup', connect, work)
    return True


def _borg_verify(target, secret_key, connect):
    if not _claim(target['id'], 'verify'):
        return False

    def work(conn):
        run_id = _new_run(conn, target['id'], 'verify')
        try:
            _update_run(conn, run_id, phase='Checking the repository')
            repo = repo_for(target, secret_key)
            rc, _, log = _stream_borg(target['id'], conn, run_id, repo.popen(*repo.check_args()))
            if rc != 0:
                raise backup_borg._explain(log, target['repo_uri'])
            _end(conn, run_id, 'success')
            update_target(conn, target['id'], last_verify_at=_iso(_now()),
                          last_verify_status='success')
        except Cancelled:
            _end(conn, run_id, 'cancelled', error='Stopped')
        except Exception as exc:
            _end(conn, run_id, 'failed', error=str(exc)[:500])
            update_target(conn, target['id'], last_verify_at=_iso(_now()),
                          last_verify_status='failed')
            raise

    _run_thread(target, 'verify', connect, work)
    return True


def _borg_restore(target, secret_key, connect, archive, include, restore_dir):
    if not _claim(target['id'], 'restore'):
        return False

    def work(conn):
        run_id = _new_run(conn, target['id'], 'restore',
                          detail=json.dumps({'snapshot': archive, 'path': include,
                                             'target': restore_dir}))
        try:
            os.makedirs(restore_dir, exist_ok=True)
            _update_run(conn, run_id, phase='Restoring')
            repo = repo_for(target, secret_key)
            rc, _, log = _stream_borg(target['id'], conn, run_id,
                                      repo.popen(*repo.extract_args(archive, include),
                                                 cwd=restore_dir))
            if rc not in (0, backup_borg.WARNING):
                raise backup_borg._explain(log, target['repo_uri'])
            _end(conn, run_id, 'success')
        except Cancelled:
            _end(conn, run_id, 'cancelled', error='Stopped')
        except Exception as exc:
            _end(conn, run_id, 'failed', error=str(exc)[:500])
            raise

    _run_thread(target, 'restore', connect, work)
    return True


# ── rsync (backup_rsync.py) ──────────────────────────────────────────────────
#
# One mirror, brought up to date: no password, no versions, no retention. What
# the mirror no longer needs is moved into .airlock-replaced/<time>/ inside it.
# Progress comes on stdout (--info=progress2), errors on stderr.

def _stream_rsync(target_id, conn, run_id, proc):
    """Returns (returncode, stdout's non-progress lines, stderr)."""
    with _active_lock:
        if target_id in _active:
            _active[target_id]['proc'] = proc
    if _cancelled(target_id):
        proc.send_signal(signal.SIGINT)
    err = []
    drain = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    drain.start()
    out, last, files = [], 0.0, (0, 0)
    for line in proc.stdout:
        progress = backup_rsync.parse_progress(line)
        if progress is None:
            if line.strip():
                out.append(line.rstrip())
            continue
        # Not every progress line carries the file count: keep the last one.
        if progress['files_total']:
            files = (progress['files_done'], progress['files_total'])
        else:
            progress['files_done'], progress['files_total'] = files
        if time.monotonic() - last >= PROGRESS_EVERY:
            last = time.monotonic()
            _update_run(conn, run_id, progress_json=json.dumps(progress))
    rc = proc.wait()
    drain.join(timeout=10)
    if _cancelled(target_id):
        raise Cancelled()
    return rc, '\n'.join(out[-5000:]), ''.join(err)


def _rsync_backup(target, local_root, fingerprint, app_db_path, connect):
    if not _claim(target['id'], 'backup'):
        return False

    def work(conn):
        run_id = _new_run(conn, target['id'], 'backup')
        update_target(conn, target['id'], last_run_started_at=_iso(_now()))
        try:
            _copy_catalog_db(conn, run_id, local_root, app_db_path)
            if _cancelled(target['id']):
                raise Cancelled()
            repo = repo_for(target, None)
            paths, excludes = paths_for(target, local_root)
            stamp = _now().strftime('%Y-%m-%dT%H%M%SZ')
            _update_run(conn, run_id, phase='Copying')
            rc, out, err = _stream_rsync(target['id'], conn, run_id, repo.popen(
                *repo.backup_args(local_root, paths, excludes, stamp)))
            if rc not in (0, *backup_rsync.WARNINGS):
                raise backup_rsync._explain(err, target['repo_uri'])
            summary = {**backup_rsync.parse_stats(out), 'snapshot_id': stamp}
            repo.drop_replaced_db_copies(stamp, SNAPSHOT_DIRNAME)
            warning = ('Some files could not be copied: ' + err.strip()[-300:]) \
                if rc in backup_rsync.WARNINGS else None
            _end(conn, run_id, 'success', error=warning, summary=summary)
            update_target(conn, target['id'],
                          last_backup_at=_iso(_now()), last_backup_status='success',
                          last_snapshot_id=stamp, last_files_new=summary.get('files_new'),
                          last_data_added=summary.get('data_added'),
                          last_fingerprint=fingerprint, last_error=warning)
        except Cancelled:
            _end(conn, run_id, 'cancelled', error='Stopped')
            update_target(conn, target['id'], last_backup_status='cancelled', last_error=None)
        except Exception as exc:
            _end(conn, run_id, 'failed', error=str(exc)[:500])
            update_target(conn, target['id'], last_backup_status='failed',
                          last_error=str(exc)[:500])
            raise

    _run_thread(target, 'backup', connect, work)
    return True


def _rsync_verify(target, local_root, connect):
    """The copy compared with the catalog, file by file, by content."""
    if not _claim(target['id'], 'verify'):
        return False

    def work(conn):
        run_id = _new_run(conn, target['id'], 'verify')
        try:
            _update_run(conn, run_id, phase='Comparing the copy with the catalog')
            repo = repo_for(target, None)
            paths, excludes = paths_for(target, local_root)
            rc, out, err = _stream_rsync(target['id'], conn, run_id, repo.popen(
                *repo.verify_args(local_root, paths, excludes)))
            if rc not in (0, *backup_rsync.WARNINGS):
                raise backup_rsync._explain(err, target['repo_uri'])
            changed = backup_rsync.changed_files(out)
            if changed:
                raise backup_rsync.RsyncError(
                    f'{len(changed)} file(s) in the copy differ from the catalog, e.g. '
                    f'{changed[0].split(" ", 1)[-1]} - a backup brings it up to date.', 'differs')
            _end(conn, run_id, 'success')
            update_target(conn, target['id'], last_verify_at=_iso(_now()),
                          last_verify_status='success')
        except Cancelled:
            _end(conn, run_id, 'cancelled', error='Stopped')
        except Exception as exc:
            _end(conn, run_id, 'failed', error=str(exc)[:500])
            update_target(conn, target['id'], last_verify_at=_iso(_now()),
                          last_verify_status='failed')
            raise

    _run_thread(target, 'verify', connect, work)
    return True


def _rsync_restore(target, connect, include, restore_dir, local_root):
    if not _claim(target['id'], 'restore'):
        return False

    def work(conn):
        run_id = _new_run(conn, target['id'], 'restore',
                          detail=json.dumps({'snapshot': backup_rsync.SNAPSHOT_ID,
                                             'path': include, 'target': restore_dir}))
        try:
            os.makedirs(restore_dir, exist_ok=True)
            _update_run(conn, run_id, phase='Restoring')
            repo = repo_for(target, None)
            rc, _, err = _stream_rsync(target['id'], conn, run_id, repo.popen(
                *repo.restore_args(local_root or '/', include, restore_dir)))
            if rc not in (0, *backup_rsync.WARNINGS):
                raise backup_rsync._explain(err, target['repo_uri'])
            _end(conn, run_id, 'success')
        except Cancelled:
            _end(conn, run_id, 'cancelled', error='Stopped')
        except Exception as exc:
            _end(conn, run_id, 'failed', error=str(exc)[:500])
            raise

    _run_thread(target, 'restore', connect, work)
    return True


# ── the schedule ─────────────────────────────────────────────────────────────

def _zone(target):
    if ZoneInfo and target.get('schedule_tz'):
        try:
            return ZoneInfo(target['schedule_tz'])
        except Exception:
            pass
    return timezone.utc


def last_due(target, now=None):
    """The most recent scheduled moment at or before now, or None (manual)."""
    s = schedule_of(target)
    if s['kind'] not in ('daily', 'weekly'):
        return None
    now = (now or _now()).astimezone(_zone(target))
    try:
        hh, mm = (int(x) for x in s['time'].split(':'))
    except (ValueError, AttributeError):
        hh, mm = 2, 0
    at = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if s['kind'] == 'daily':
        if at > now:
            at -= timedelta(days=1)
    else:
        at -= timedelta(days=(at.weekday() - int(s.get('weekday', 6))) % 7)
        if at > now:
            at -= timedelta(days=7)
    return at.astimezone(timezone.utc)


def next_due(target, now=None):
    last = last_due(target, now)
    if last is None:
        return None
    step = timedelta(days=1 if schedule_of(target)['kind'] == 'daily' else 7)
    return last + step


def is_due(target, now=None):
    """A scheduled moment has passed that no run has started since, and the
    card existed before it."""
    last = last_due(target, now)
    if last is None:
        return False
    started = _parse(target.get('last_run_started_at'))
    created = _parse(target.get('created_at'))
    return (started is None or started < last) and (created is None or created < last)


# ── the card's status ────────────────────────────────────────────────────────

_RUN_LABEL = {'backup': 'Backing up', 'verify': 'Verifying', 'restore': 'Restoring'}


def status(target, runs, fingerprint, now=None):
    """What the card shows (spec 5.3). `runs` newest first."""
    now = now or _now()
    running = next((r for r in runs if r['status'] == 'running'), None)
    if running:
        try:
            progress = json.loads(running.get('progress_json') or 'null')
        except ValueError:
            progress = None
        return {'state': 'running', 'kind': running['kind'],
                'reason': running.get('phase') or _RUN_LABEL.get(running['kind']),
                'progress': progress}
    last_status = target.get('last_backup_status')
    if last_status == 'failed':
        return {'state': 'failed', 'reason': target.get('last_error') or 'The last backup failed'}
    if last_status == 'interrupted':
        return {'state': 'failed',
                'reason': target.get('last_error') or 'The last backup was interrupted'}
    last_at = _parse(target.get('last_backup_at'))
    if not last_at:
        return {'state': 'needed', 'reason': 'No backup yet'}
    s = schedule_of(target)
    limit = {'daily': timedelta(days=1), 'weekly': timedelta(days=7)}.get(s['kind'])
    grace = timedelta(hours=int(target.get('grace_hours') or DEFAULT_GRACE_HOURS))
    if limit and now - last_at > limit + grace:
        return {'state': 'needed', 'reason': f'Last backup {(now - last_at).days} days ago'}
    if target.get('last_fingerprint') and fingerprint and \
            not _covers(target['scope'], target['last_fingerprint'], fingerprint):
        return {'state': 'needed', 'reason': 'The catalog has changed since the last backup'}
    return {'state': 'up_to_date', 'reason': target.get('last_error')}


def _covers(scope, then, now):
    """Is the catalog still what the last backup copied? The Dashboard's
    fingerprint is 'version:files:derivatives:faces'; originals care about
    files, and faces/names live in the database copy both scopes carry."""
    a, b = then.split(':'), now.split(':')
    if len(a) != 4 or len(b) != 4:
        return then == now
    if scope == 'everything':
        return a == b
    return a[1] == b[1] and a[3] == b[3]


def public(target, runs=(), state=None, now=None):
    """The card as the browser gets it: never a secret."""
    nd = next_due(target, now)
    last_restore = next((r for r in runs if r['kind'] == 'restore'), None)
    last_verify_run = next((r for r in runs if r['kind'] == 'verify'
                            and r['status'] != 'running'), None)
    return {
        'id': target['id'],
        'name': target['name'],
        'type': target['type'],
        'scope': target['scope'],
        'repo_uri': target['repo_uri'],
        'options': options_of(target),
        'has_repo_password': bool(target.get('repo_secret_enc')),
        'schedule': {**schedule_of(target), 'tz': target.get('schedule_tz') or 'UTC'},
        'retention': retention_of(target),
        'grace_hours': target.get('grace_hours'),
        'last_backup': {
            'at': target.get('last_backup_at'),
            'status': target.get('last_backup_status'),
            'snapshot_id': target.get('last_snapshot_id'),
            'files_new': target.get('last_files_new'),
            'data_added': target.get('last_data_added'),
        },
        'last_verify': {'at': target.get('last_verify_at'),
                        'status': target.get('last_verify_status'),
                        'error': last_verify_run.get('error') if last_verify_run else None},
        'last_restore': {'status': last_restore['status'], 'at': last_restore['started_at'],
                         'error': last_restore.get('error'),
                         **json.loads(last_restore.get('detail') or '{}')} if last_restore else None,
        'next_run': _iso(nd),
        'status': state,
    }
