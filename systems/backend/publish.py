"""
Publish a deployment to a server - phase 3: servers, runs, preflight, preview.
documentation/specs/deployment-publish-to-server-spec.md.

  servers     deployment_servers: where a deployment is served from. No secret
              is stored here - the key is named by credential_id and lives in
              database/publish-ssh/ (publish_ssh.py).
  runs        deployment_publish_runs: one per Preview (Publish and Set up come
              in phases 4 and 5), with the same phase / progress_json / "at"
              shape as the backup runs.
  preflight   republish, then warn about albums older than what they show and
              events with photos their album section does not have (6.1).
  stage       deployment_pack.refresh_stage, in <catalog>/cache/Publish (6.2).
  remote      over SSH: is there a bundle, how much space, is docker there (6.3).
  preview     rsync --dry-run --itemize-changes, counted and checked (6.4).
  publish     the preview, then three sends - media, album data, deletions -
              and a check of the live site (6.5, 6.6).

Nothing here imports app.py: the routes hand it the catalog, the republish
step and the remote. The remote is an object (SSHRemote; LocalRemote for tests),
so the whole run is exercised with real rsync and real shell commands without a
server.
"""

import json
import os
import re
import shlex
import signal
import sqlite3
import subprocess
import threading
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import deployment_pack
import publish_ssh

LOG_LINES = 2000                # the newest lines a run row keeps (5.3)
MASS_DELETE_SHARE = 0.10        # deletions above this share of the server's files...
MASS_DELETE_COUNT = 1000        # ...or this many, need an explicit confirmation (6.4)
FREE_MARGIN = 1 << 30           # 1 GB left on the server after a publish (6.4)
DEFAULT_IMAGE = 'localhost/album-gallery:amd64'

_HOST = re.compile(r'^[A-Za-z0-9.-]+$|^\[?[0-9A-Fa-f:]+\]?$')
_USER = re.compile(r'^[a-z_][a-z0-9_.-]{0,31}$')
_IMAGE = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._/:@-]*$')


class PublishError(Exception):
    """Stops a run or refuses a request, phrased for a person."""

    def __init__(self, message, code='error', status=400):
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


# ── schema ───────────────────────────────────────────────────────────────────

def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS deployment_servers (
        id              INTEGER PRIMARY KEY AUTOINCREMENT,
        deployment_id   INTEGER NOT NULL,
        name            TEXT NOT NULL,
        host            TEXT NOT NULL,
        port            INTEGER NOT NULL DEFAULT 22,
        ssh_user        TEXT NOT NULL,
        remote_dir      TEXT NOT NULL,
        public_url      TEXT NOT NULL,
        gallery_image   TEXT NOT NULL DEFAULT 'localhost/album-gallery:amd64',
        host_key        TEXT,
        credential_id   TEXT,
        key_fingerprint TEXT,
        last_run_id     INTEGER,
        created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS deployment_publish_runs (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        server_id     INTEGER NOT NULL,
        kind          TEXT NOT NULL,
        status        TEXT NOT NULL,
        phase         TEXT,
        files_new     INTEGER, files_changed INTEGER, files_deleted INTEGER,
        bytes_sent    INTEGER,
        log           TEXT,
        error         TEXT,
        started_at    TIMESTAMP, finished_at TIMESTAMP,
        started_by    INTEGER,
        progress_json TEXT,
        result_json   TEXT)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_publish_runs_server '
                 'ON deployment_publish_runs (server_id, id)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_deployment_servers_deployment '
                 'ON deployment_servers (deployment_id)')


# ── servers ──────────────────────────────────────────────────────────────────

def _clean(fields, partial=False):
    """The server fields a person may set, checked. Raises PublishError."""
    out = {}

    def need(key):
        return not partial or key in fields

    if need('name'):
        name = str(fields.get('name') or '').strip()
        if not name:
            raise PublishError('Give the server a name.')
        out['name'] = name[:120]
    if need('host'):
        host = str(fields.get('host') or '').strip()
        if not host or not _HOST.match(host):
            raise PublishError('The host must be a name or an IP address, e.g. 203.0.113.10.')
        out['host'] = host
    if need('port'):
        try:
            port = int(fields.get('port') or 22)
        except (TypeError, ValueError):
            raise PublishError('The port must be a number.')
        if not 1 <= port <= 65535:
            raise PublishError('The port must be between 1 and 65535.')
        out['port'] = port
    if need('ssh_user'):
        user = str(fields.get('ssh_user') or '').strip()
        if not _USER.match(user):
            raise PublishError('The SSH user must be a plain user name, e.g. ubuntu.')
        out['ssh_user'] = user
    if need('remote_dir'):
        remote = str(fields.get('remote_dir') or '').strip().rstrip('/')
        parts = remote.split('/')
        if not remote or '..' in parts or any(c in remote for c in '\n\r\0\'"`$\\'):
            raise PublishError('The folder on the server must be a plain path, '
                               'e.g. galleries/album2026.')
        out['remote_dir'] = remote
    if need('public_url'):
        url = str(fields.get('public_url') or '').strip()
        parsed = urlparse(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            raise PublishError('The public address must look like https://photos.example.com/.')
        out['public_url'] = url if url.endswith('/') else url + '/'
    if need('gallery_image'):
        image = str(fields.get('gallery_image') or DEFAULT_IMAGE).strip()
        if not _IMAGE.match(image):
            raise PublishError('The gallery image must be an image name, '
                               'e.g. localhost/album-gallery:amd64.')
        out['gallery_image'] = image
    return out


def _server_dict(row):
    s = dict(row)
    s['key_missing'] = bool(s.get('credential_id')) and not publish_ssh.has_key(s['credential_id'])
    s['key_changed'] = bool(s.get('credential_id')) and publish_ssh.key_changed(
        s['credential_id'], s.get('key_fingerprint'))
    s['domain'] = urlparse(s['public_url']).hostname
    return s


def get_server(conn, server_id, deployment_id=None):
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM deployment_servers WHERE id = ?', (server_id,)).fetchone()
    if not row or (deployment_id is not None and row['deployment_id'] != deployment_id):
        raise PublishError('Server not found', 'not_found', 404)
    return _server_dict(row)


def list_servers(conn, deployment_id):
    conn.row_factory = sqlite3.Row
    out = []
    for row in conn.execute('SELECT * FROM deployment_servers WHERE deployment_id = ? '
                            'ORDER BY id', (deployment_id,)):
        s = _server_dict(row)
        last = conn.execute('SELECT id, kind, status, phase, error, started_at, finished_at, '
                            'files_new, files_changed, files_deleted, bytes_sent, result_json '
                            'FROM deployment_publish_runs WHERE server_id = ? '
                            'ORDER BY id DESC LIMIT 1', (s['id'],)).fetchone()
        s['last_run'] = _run_dict(last) if last else None
        out.append(s)
    return out


def add_server(conn, deployment_id, fields, key_comment):
    """A new server and its key. Returns the server with the line to put on it."""
    clean = _clean(fields)
    credential_id, _line, fingerprint = publish_ssh.create_key(key_comment)
    cur = conn.execute(
        'INSERT INTO deployment_servers (deployment_id, name, host, port, ssh_user, remote_dir, '
        'public_url, gallery_image, credential_id, key_fingerprint) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)',
        (deployment_id, clean['name'], clean['host'], clean['port'], clean['ssh_user'],
         clean['remote_dir'], clean['public_url'], clean['gallery_image'],
         credential_id, fingerprint))
    conn.commit()
    server = get_server(conn, cur.lastrowid)
    server['authorized_keys_line'] = publish_ssh.authorized_keys_line(credential_id)
    return server


def update_server(conn, server_id, deployment_id, fields):
    """Change what a server is, never its key. A new host or port unpins it."""
    current = get_server(conn, server_id, deployment_id)
    clean = _clean(fields, partial=True)
    if not clean:
        return current
    if ('host' in clean and clean['host'] != current['host']) or \
            ('port' in clean and clean['port'] != current['port']):
        clean['host_key'] = None
    cols = ', '.join(f'{k} = ?' for k in clean)
    conn.execute(f'UPDATE deployment_servers SET {cols} WHERE id = ?',
                 (*clean.values(), server_id))
    conn.commit()
    return get_server(conn, server_id)


def delete_server(conn, server_id, deployment_id=None):
    """Forget a server: its row, its runs and its key pair. The line on the
    server stays until somebody removes it there."""
    server = get_server(conn, server_id, deployment_id)
    conn.execute('DELETE FROM deployment_publish_runs WHERE server_id = ?', (server_id,))
    conn.execute('DELETE FROM deployment_servers WHERE id = ?', (server_id,))
    conn.commit()
    if server.get('credential_id'):
        publish_ssh.forget_key(server['credential_id'])
    return server


def delete_for_deployment(conn, deployment_id):
    """A deployment's servers, when the deployment itself is deleted."""
    try:
        ids = [r[0] for r in conn.execute(
            'SELECT id FROM deployment_servers WHERE deployment_id = ?', (deployment_id,))]
    except sqlite3.Error:
        return 0
    for sid in ids:
        delete_server(conn, sid)
    return len(ids)


def key_line(server):
    """The authorized_keys line for a server, shown again on request."""
    if not server.get('credential_id'):
        raise PublishError('This server has no key.', 'no_key')
    return publish_ssh.authorized_keys_line(server['credential_id'])


def scan(server):
    """The server's host key fingerprint, read for confirmation - not stored."""
    try:
        return publish_ssh.scan_host(server['host'], server['port'])
    except publish_ssh.PublishKeyError as e:
        raise PublishError(str(e), 'unreachable', 502)


def pin(conn, server, fingerprint):
    """Pin the confirmed host key (also Re-pin)."""
    try:
        seen = publish_ssh.pin_host(server['host'], server['port'], fingerprint)
    except publish_ssh.PublishKeyError as e:
        raise PublishError(str(e), 'host_key_mismatch', 409)
    conn.execute('UPDATE deployment_servers SET host_key = ? WHERE id = ?', (seen, server['id']))
    conn.commit()
    return seen


# ── runs ─────────────────────────────────────────────────────────────────────

def _run_dict(row):
    r = dict(row)
    for key in ('progress_json', 'result_json'):
        raw = r.pop(key, None)
        try:
            r[key.replace('_json', '')] = json.loads(raw) if raw else None
        except ValueError:
            r[key.replace('_json', '')] = None
    return r


def start_run(conn, server_id, kind, user_id=None):
    """A new run row, or PublishError('busy') naming the one already going (8.4)."""
    busy = conn.execute("SELECT id FROM deployment_publish_runs WHERE server_id = ? "
                        "AND status IN ('queued', 'running')", (server_id,)).fetchone()
    if busy:
        raise PublishError('A run is already going for this server.', 'busy', 409)
    cur = conn.execute(
        "INSERT INTO deployment_publish_runs (server_id, kind, status, phase, started_at, "
        "started_by) VALUES (?, ?, 'running', 'Starting', ?, ?)",
        (server_id, kind, _now(), user_id))
    conn.execute('UPDATE deployment_servers SET last_run_id = ? WHERE id = ?',
                 (cur.lastrowid, server_id))
    conn.commit()
    return cur.lastrowid


def get_run(conn, run_id, server_ids=None):
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM deployment_publish_runs WHERE id = ?', (run_id,)).fetchone()
    if not row or (server_ids is not None and row['server_id'] not in server_ids):
        raise PublishError('Run not found', 'not_found', 404)
    return _run_dict(row)


def interrupt_stale(conn):
    """At start-up: runs a previous process left going died with it (3.6)."""
    try:
        cur = conn.execute(
            "UPDATE deployment_publish_runs SET status = 'interrupted', finished_at = ?, "
            "error = COALESCE(error, 'The backend restarted while this ran') "
            "WHERE status IN ('queued', 'running')", (_now(),))
        conn.commit()
        return cur.rowcount
    except sqlite3.Error:
        return 0


class Run:
    """
    One run's handle: writes its phase, progress and log to its row (each
    write on a short connection of its own, so a busy catalog delays the line
    rather than failing the run - spec 3.9), and carries Stop.
    """

    def __init__(self, db_path, run_id):
        self.db_path = db_path
        self.id = run_id
        self.lines = []
        self.stopped = threading.Event()
        self.proc = None

    def _write(self, **fields):
        if 'progress_json' in fields and fields['progress_json'] is not None:
            p = dict(fields['progress_json'])
            p['at'] = _now()
            fields['progress_json'] = json.dumps(p)
        if 'result_json' in fields and fields['result_json'] is not None:
            fields['result_json'] = json.dumps(fields['result_json'])
        cols = ', '.join(f'{k} = ?' for k in fields)
        conn = sqlite3.connect(self.db_path, timeout=120)
        try:
            conn.execute(f'UPDATE deployment_publish_runs SET {cols} WHERE id = ?',
                         (*fields.values(), self.id))
            conn.commit()
        finally:
            conn.close()

    def phase(self, text, progress=None):
        self.log(f'── {text}')
        self._write(phase=text, progress_json=progress)

    def log(self, text):
        for line in str(text).splitlines() or ['']:
            self.lines.append(line)
        del self.lines[:-LOG_LINES]

    def flush_log(self):
        self._write(log='\n'.join(self.lines))

    def finish(self, status, error=None, **fields):
        self.flush_log()
        self._write(status=status, error=error, finished_at=_now(), progress_json=None, **fields)

    def check_stop(self):
        if self.stopped.is_set():
            raise Stopped()

    def stop(self):
        self.stopped.set()
        if self.proc and self.proc.poll() is None:
            # The whole process group: rsync runs ssh, and a shell runs its
            # command - signalling only the top one leaves a child holding the
            # output open, and Stop would wait for it to finish.
            try:
                os.killpg(self.proc.pid, signal.SIGTERM)
            except OSError:
                pass


class Stopped(Exception):
    pass


_runs = {}                      # run id -> Run, while it runs (Stop finds it here)
_runs_lock = threading.Lock()


def register(run):
    with _runs_lock:
        _runs[run.id] = run


def unregister(run):
    with _runs_lock:
        _runs.pop(run.id, None)


def stop_run(run_id):
    with _runs_lock:
        run = _runs.get(run_id)
    if run:
        run.stop()
        return True
    return False


# ── preflight (local) ────────────────────────────────────────────────────────

def _as_utc(value):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace('Z', '+00:00').replace(' ', 'T'))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def stale_albums(conn, catalog_dir, deployment_id):
    """
    Albums whose deployed folder is older than something they show (6.1.2): a
    photo removed from the catalog, a photo turned or flipped (3.8), or the
    album itself changed. Returned, never fixed: rebuilding re-reads sections,
    and the person should know the gallery is about to change.
    """
    conn.row_factory = sqlite3.Row
    out = []
    albums = conn.execute(
        'SELECT a.id, a.name, a.updated_at FROM albums a '
        'JOIN album_deployment_albums da ON da.album_id = a.id '
        'WHERE da.deployment_id = ? ORDER BY da.position, a.id', (deployment_id,)).fetchall()
    for album in albums:
        manifest = os.path.join(catalog_dir, 'cache', 'Albums', f"{album['id']:05d}", 'album.json')
        try:
            with open(manifest, encoding='utf-8') as fh:
                deployed = _as_utc(json.load(fh).get('deployed_at'))
        except (OSError, ValueError):
            out.append({'album_id': album['id'], 'name': album['name'],
                        'reason': 'it has not been built yet'})
            continue
        if deployed is None:
            continue
        reasons = []
        if (_as_utc(album['updated_at']) or deployed) > deployed:
            reasons.append('the album changed')
        files = 'SELECT sp.file_id FROM section_photos sp JOIN album_sections s ' \
                'ON s.id = sp.section_id WHERE s.album_id = ?'
        removed = conn.execute(f'SELECT MAX(removed_at) FROM file_contents '
                               f'WHERE id IN ({files})', (album['id'],)).fetchone()[0]
        if removed and _as_utc(removed) and _as_utc(removed) > deployed:
            reasons.append('a photo in it was removed')
        try:
            edited = conn.execute(f'SELECT MAX(updated_at) FROM photo_edits '
                                  f'WHERE file_id IN ({files})', (album['id'],)).fetchone()[0]
        except sqlite3.Error:
            edited = None
        if edited and _as_utc(edited) and _as_utc(edited) > deployed:
            reasons.append('a photo in it was turned or flipped')
        if reasons:
            out.append({'album_id': album['id'], 'name': album['name'],
                        'reason': ', '.join(reasons)})
    return out


def unsynced_events(conn, deployment_id):
    """
    Album sections built from an event that has since gained photos (6.1.3):
    sections are a stored arrangement, so the gallery will not show them. Named,
    not fixed - re-importing replaces the person's arrangement.
    """
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute('''
            SELECT a.name AS album, s.name AS section, e.id AS event_id, e.name AS event,
                   COUNT(ef.file_id) AS missing
              FROM album_deployment_albums da
              JOIN albums a ON a.id = da.album_id
              JOIN album_sections s ON s.album_id = a.id AND s.source_event_id IS NOT NULL
              JOIN events e ON e.id = s.source_event_id
              JOIN event_files ef ON ef.event_id = e.id
              JOIN file_contents fc ON fc.id = ef.file_id AND fc.removed_at IS NULL
             WHERE da.deployment_id = ?
               AND NOT EXISTS (SELECT 1 FROM section_photos sp
                                WHERE sp.section_id = s.id AND sp.file_id = ef.file_id)
             GROUP BY s.id ORDER BY a.name, s.position''', (deployment_id,)).fetchall()
    except sqlite3.Error:
        return []
    return [dict(r) for r in rows]


# ── the remote ───────────────────────────────────────────────────────────────

class SSHRemote:
    """A server reached over SSH with its own key and its pinned host key."""

    def __init__(self, server, control_path=None):
        if not server.get('host_key'):
            raise PublishError('Connect to the server first, to confirm its identity.',
                               'not_pinned', 409)
        if server.get('key_missing'):
            raise PublishError('This server\'s key is missing on this machine - forget the '
                               'server and add it again.', 'key_missing', 409)
        if server.get('key_changed'):
            raise PublishError('This machine\'s key for the server changed. Add the new line '
                               'to the server\'s authorized_keys.', 'key_changed', 409)
        self.server = server
        self.control_path = control_path
        self.opts = publish_ssh.ssh_options(server['credential_id'], server['port'], control_path)
        self.target = f"{server['ssh_user']}@{server['host']}"

    def command(self, script):
        return ['ssh', *self.opts, self.target, script]

    def rsync_target(self):
        return f"{self.target}:{self.server['remote_dir']}/"

    def rsync_rsh(self):
        return publish_ssh.rsync_rsh(self.server['credential_id'], self.server['port'],
                                     self.control_path)

    def close(self):
        if self.control_path:
            subprocess.run(['ssh', *self.opts, '-O', 'exit', self.target],
                           capture_output=True, timeout=10)


class LocalRemote:
    """The same commands, on this machine, in a folder standing in for the
    server's home. For tests: real rsync, real shell, no SSH."""

    def __init__(self, server, home, path_prefix=None):
        self.server = server
        self.home = home
        self.path_prefix = path_prefix          # where a test's fake `docker` lives
        os.makedirs(home, exist_ok=True)

    def command(self, script):
        env = f'export PATH={shlex.quote(self.path_prefix)}:"$PATH"; ' if self.path_prefix else ''
        return ['sh', '-c', f'{env}cd {shlex.quote(self.home)} && {script}']

    def rsync_target(self):
        return os.path.join(self.home, self.server['remote_dir']) + '/'

    def rsync_rsh(self):
        return None

    def close(self):
        pass


def _call(run, argv, timeout=None):
    """Run a command as the run's current process (so Stop can end it)."""
    run.check_stop()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                            start_new_session=True)        # its own group, for Stop
    run.proc = proc
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, err = proc.communicate()
        raise PublishError(f'{argv[0]} took too long and was stopped.', 'timeout', 504)
    finally:
        run.proc = None
    run.check_stop()
    return proc.returncode, out, err


def _explain_ssh(err, server):
    text = (err or '').strip()
    if 'Host key verification failed' in text or 'REMOTE HOST IDENTIFICATION' in text:
        return PublishError("The server's identity changed. Check it, then confirm its new "
                            "key with Re-pin.", 'host_key_changed', 409)
    if 'Permission denied' in text:
        return PublishError("The server refused this machine's key. Add the line shown for "
                            "this server to ~/.ssh/authorized_keys there.", 'key_refused', 409)
    if any(s in text for s in ('Connection refused', 'timed out', 'No route to host',
                               'Could not resolve', 'Network is unreachable')):
        return PublishError(f"Could not reach {server['host']}:{server['port']} - is the "
                            f"server running, and SSH open to this machine?", 'unreachable', 502)
    return PublishError(f'The server answered with an error: {text[-300:]}', 'remote_error', 502)


def remote_preflight(run, remote):
    """What is on the server (6.3): a bundle?, free space, docker, file count."""
    d = shlex.quote(remote.server['remote_dir'])
    script = (f'D={d}; '
              f'if [ -f "$D/compose.yml" ]; then echo BUNDLE=yes; else echo BUNDLE=no; fi; '
              f'P="$D"; while [ ! -d "$P" ]; do P=$(dirname "$P"); done; '
              f'echo FREE=$(df -Pk "$P" | awk \'NR==2 {{print $4}}\'); '
              f'if command -v docker >/dev/null 2>&1; then echo DOCKER=yes; else echo DOCKER=no; fi; '
              f'echo FILES=$( [ -d "$D" ] && find "$D" -type f ! -path "$D/.env" | wc -l || echo 0)')
    rc, out, err = _call(run, remote.command(script), timeout=60)
    if rc != 0:
        raise _explain_ssh(err, remote.server)
    facts = dict(line.split('=', 1) for line in out.splitlines() if '=' in line)
    return {'has_bundle': facts.get('BUNDLE') == 'yes',
            'free_bytes': int(facts.get('FREE') or 0) * 1024,
            'docker': facts.get('DOCKER') == 'yes',
            'remote_files': int((facts.get('FILES') or '0').strip() or 0)}


# ── preview ──────────────────────────────────────────────────────────────────

def rsync_argv(stage, remote, dry_run=True, checksum=False):
    argv = ['rsync', '-a', '--delete', '--exclude=/.env', '--itemize-changes', '--stats',
            '--partial-dir=.rsync-partial']
    if dry_run:
        argv.append('--dry-run')
    if checksum:
        argv.append('--checksum')
    rsh = remote.rsync_rsh()
    if rsh:
        argv += ['-e', rsh]
    return argv + [stage.rstrip('/') + '/', remote.rsync_target()]


_STAT = re.compile(r'^Total transferred file size: ([\d,.]+)')


def parse_itemize(output):
    """
    rsync --itemize-changes --stats, counted: files to send new or changed,
    deletions by name, and the bytes to send. Attribute-only changes (a
    directory's time) send nothing and are not counted.
    """
    new = changed = 0
    deletions = []
    sends = []
    bytes_to_send = 0
    for line in output.splitlines():
        if line.startswith('*deleting '):
            deletions.append(line[len('*deleting '):].strip())
            continue
        m = _STAT.match(line)
        if m:
            bytes_to_send = int(m.group(1).replace(',', '').split('.')[0])
            continue
        if len(line) > 12 and line[0] in '<>c' and line[1] == 'f':
            path = line[12:].strip()
            sends.append(path)
            if line[2:11].startswith('+'):
                new += 1
            else:
                changed += 1
    return {'new': new, 'changed': changed, 'deleted': len(deletions),
            'deletions': deletions, 'sends': sends, 'bytes': bytes_to_send}


def check_plan(plan, facts):
    """The safety rules of 6.4: {'stop': reason|None, 'confirm': reason|None,
    'up_to_date': bool}."""
    if '.env' in plan['deletions'] or '.env' in plan['sends']:
        return {'stop': 'The plan would touch the server\'s .env - the exclude did not apply. '
                        'Nothing was sent.', 'confirm': None, 'up_to_date': False}
    if plan['bytes'] > max(0, facts['free_bytes'] - FREE_MARGIN):
        return {'stop': f"It needs {deployment_pack.human(plan['bytes'])} and the server has "
                        f"{deployment_pack.human(facts['free_bytes'])} free (1 GB must stay "
                        f"free). Grow its disk first.", 'confirm': None, 'up_to_date': False}
    confirm = None
    remote = facts.get('remote_files') or 0
    if plan['deleted'] > MASS_DELETE_COUNT or \
            (remote and plan['deleted'] > MASS_DELETE_SHARE * remote):
        confirm = (f"It would delete {plan['deleted']:,} of the {remote:,} files on the server. "
                   f"If the deployment was not emptied on purpose, check it first.")
    up_to_date = not (plan['new'] or plan['changed'] or plan['deleted'])
    return {'stop': None, 'confirm': confirm, 'up_to_date': up_to_date}


def run_preview(run, ctx, server, remote):
    """
    A Preview run (spec 6.1-6.4). `ctx` carries what the routes know:
      republish()        rewrite the deployment folder (_publish_deployment)
      conn_factory()     a connection to the catalog database
      catalogs_root, catalog_slug, catalog_dir, deployment_id, deployment_slug
      first_publish      no publish has succeeded to this server yet
    Returns the result written to the run.
    """
    result, stage = _prepare_local(run, ctx, server)

    run.phase('Checking the server')
    facts = remote_preflight(run, remote)
    result['server'] = facts
    if not facts['has_bundle'] and facts['remote_files'] == 0:
        result['needs_setup'] = True
        result['message'] = ('The server has no gallery yet. Set it up first (or follow the '
                             'guide\'s first publish), then preview again.')
        return result
    if not facts['docker']:
        result['warnings'].append('Docker was not found on the server.')

    run.phase('Previewing')
    checksum = bool(ctx.get('first_publish'))
    rc, out, err = _call(run, rsync_argv(stage, remote, dry_run=True, checksum=checksum),
                         timeout=3600)
    if rc not in (0, 24):                       # 24: files vanished mid-scan - harmless here
        raise _explain_ssh(err, server)
    plan = parse_itemize(out)
    verdict = check_plan(plan, facts)
    result.update({'plan': {k: plan[k] for k in ('new', 'changed', 'deleted', 'bytes')},
                   'deletions': plan['deletions'][:200], 'sends': plan['sends'],
                   'checksum': checksum, **verdict})
    run.log(f"plan: {plan['new']} new, {plan['changed']} changed, {plan['deleted']} to delete, "
            f"{deployment_pack.human(plan['bytes'])} to send")
    return result


def _prepare_local(run, ctx, server):
    """The part of every run that happens here: republish, the album checks
    (6.1), and the stage refreshed (6.2). Returns (result, stage)."""
    result = {'warnings': []}

    run.phase('Republishing')
    stats = ctx['republish']()
    for s in (stats or {}).get('skipped') or []:
        result['warnings'].append(f"Album \"{s['name']}\" is not in the gallery: {s['reason']}.")

    run.phase('Checking the albums')
    conn = ctx['conn_factory']()
    try:
        for a in stale_albums(conn, ctx['catalog_dir'], ctx['deployment_id']):
            result['warnings'].append(f"Album \"{a['name']}\" needs rebuilding: {a['reason']}.")
        for e in unsynced_events(conn, ctx['deployment_id']):
            result['warnings'].append(
                f"Event \"{e['event']}\" has {e['missing']} photo(s) that section "
                f"\"{e['section']}\" of \"{e['album']}\" does not.")
    finally:
        conn.close()
    result['stale'] = bool(result['warnings'])

    run.phase('Staging')
    info = deployment_pack.inspect(ctx['catalogs_root'], ctx['catalog_slug'],
                                   ctx['deployment_slug'])
    stage = deployment_pack.stage_dir_for(ctx['catalog_dir'], ctx['deployment_slug'])
    counts = deployment_pack.refresh_stage(info, stage, server['gallery_image'], server['domain'])
    run.log(f"stage: {counts['files']} files, {deployment_pack.human(counts['bytes'])}; "
            f"{counts['placed']} new or changed, {counts['removed']} removed")
    result['stage'] = {k: counts[k] for k in ('files', 'bytes', 'placed', 'kept', 'removed', 'linked')}
    run.check_stop()
    return result, stage


# ── publish: apply and verify ────────────────────────────────────────────────

PROGRESS_EVERY = 3.0
_PROGRESS2 = re.compile(r'^\s*([\d,]+)\s+(\d+)%')
_SENT = re.compile(r'^Total transferred file size: ([\d,]+)')


def _stream(run, argv, phase, timeout=None):
    """
    A long rsync, its progress (--info=progress2) written to the run every few
    seconds while it goes. Returns (returncode, the other output lines, stderr).
    """
    run.check_stop()
    proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            start_new_session=True)
    run.proc = proc
    err = []
    drain = threading.Thread(target=lambda: err.append(proc.stderr.read()), daemon=True)
    drain.start()
    lines, buf, last = [], b'', 0.0
    started = time.monotonic()
    try:
        while True:
            chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, 'read1') \
                else proc.stdout.read(65536)
            if not chunk:
                break
            buf += chunk
            # progress2 rewrites one line with \r; everything else ends in \n.
            parts = re.split(rb'[\r\n]', buf)
            buf = parts.pop()
            for raw in parts:
                line = raw.decode('utf-8', 'replace')
                m = _PROGRESS2.match(line)
                if m:
                    if time.monotonic() - last >= PROGRESS_EVERY:
                        last = time.monotonic()
                        run._write(phase=phase, progress_json={
                            'percent': int(m.group(2)),
                            'bytes_done': int(m.group(1).replace(',', ''))})
                elif line.strip():
                    lines.append(line)
            if timeout and time.monotonic() - started > timeout:
                os.killpg(proc.pid, signal.SIGTERM)
                raise PublishError('The send took too long and was stopped.', 'timeout', 504)
        if buf.strip():
            lines.append(buf.decode('utf-8', 'replace'))
        rc = proc.wait()
    finally:
        run.proc = None
        drain.join(timeout=10)
    run.check_stop()
    return rc, lines, (err[0].decode('utf-8', 'replace') if err and err[0] else '')


def _send_argv(stage, remote, filters, delete=False, checksum=False, progress=True):
    argv = ['rsync', '-a', '--exclude=/.env', '--partial-dir=.rsync-partial', '--stats']
    if progress:
        argv.append('--info=progress2')
    if delete:
        argv.append('--delete')
    if checksum:
        argv.append('--checksum')
    argv += filters
    rsh = remote.rsync_rsh()
    if rsh:
        argv += ['-e', rsh]
    return argv + [stage.rstrip('/') + '/', remote.rsync_target()]


def apply(run, stage, remote, checksum=False):
    """
    The three passes of 6.5, so a visitor mid-browse never sees a manifest
    naming a file not there yet, or loses a file a live manifest still names:
    media first (no deletions), then the album data, then the deletions. The
    server's .env is excluded from every one. Returns the bytes sent.
    """
    run.phase('Preparing the folder')
    # rsync makes only the last folder of the target: galleries/<slug> on a
    # fresh server needs galleries/ first (found over real SSH, phase 3).
    rc, _out, err = _call(run, remote.command(
        f"mkdir -p {shlex.quote(remote.server['remote_dir'])}"), timeout=60)
    if rc != 0:
        raise _explain_ssh(err, remote.server)

    sent = 0
    passes = [
        ('Sending media', ['--exclude=*.json'], False),
        ('Sending album data', ['--include=*/', '--include=*.json', '--exclude=*'], False),
        ('Removing old files', [], True),
    ]
    for phase, filters, delete in passes:
        run.phase(phase, {'percent': 0})
        # The album data by content: small files, and a manifest rewritten in
        # the same second at the same size must still reach the server.
        by_content = checksum or phase == 'Sending album data'
        rc, lines, err = _stream(run, _send_argv(stage, remote, filters, delete=delete,
                                                 checksum=by_content and not delete),
                                 phase, timeout=6 * 3600)
        if rc not in (0, 24):
            raise _explain_ssh(err, remote.server)
        for line in lines:
            m = _SENT.match(line)
            if m:
                sent += int(m.group(1).replace(',', ''))
        run.log(f'{phase}: done')
    return sent


def restart_if_needed(run, remote, sends):
    """docker compose up -d when the stack's own files changed (6.5). It
    recreates only what changed; data needs no restart - the gallery reads from
    disk."""
    if not ({'compose.yml', 'Caddyfile'} & set(sends)):
        return None
    run.phase('Restarting the gallery')
    d = shlex.quote(remote.server['remote_dir'])
    rc, out, err = _call(run, remote.command(f'cd {d} && docker compose up -d'), timeout=600)
    if rc != 0:
        return (f'compose.yml or the Caddyfile changed, and `docker compose up -d` on the '
                f'server failed: {(err or out).strip()[-200:]}')
    return None


def _count_files(stage):
    n = 0
    for root, _dirs, files in os.walk(stage):
        n += len(files)
    return n


def verify_site(public_url, catalog_slug, deployment_slug, locked, fetch):
    """
    The live site, from this machine (6.6). `fetch(url)` -> (status, headers,
    body), never following redirects. Returns (checks, failures): each check a
    {'check', 'ok', 'detail'}.
    """
    base = public_url.rstrip('/')
    prefix = f'/d/{catalog_slug}/{deployment_slug}/'
    checks = []

    def check(name, ok, detail=''):
        checks.append({'check': name, 'ok': bool(ok), 'detail': detail})

    try:
        status, headers, _ = fetch(base + '/')
        where = (headers or {}).get('location') or (headers or {}).get('Location') or ''
        check('The address opens the gallery', status in (301, 302, 307, 308) and
              where.rstrip('/').endswith(prefix.rstrip('/')),
              f'{status} -> {where or "(no redirect)"}')

        status, _, body = fetch(base + prefix + 'deployment.json')
        try:
            slug = json.loads(body or '{}').get('slug')
        except ValueError:
            slug = None
        check('The gallery answers', status == 200 and slug == deployment_slug,
              f'deployment.json: {status}, slug {slug!r}')

        status, _, _ = fetch(base + prefix + 'albums.json')
        if locked:
            check('The password is required', status == 401, f'albums.json without it: {status}')
        else:
            check('The albums are listed', status == 200, f'albums.json: {status}')

        status, _, body = fetch(base + '/health')
        try:
            revision = json.loads(body or '{}').get('revision')
        except ValueError:
            revision = None
    except Exception as e:                       # unreachable, TLS, DNS
        check('The site can be reached', False, str(e)[:200])
        revision = None
    failures = [c for c in checks if not c['ok']]
    return checks, failures, revision


def run_publish(run, ctx, server, remote):
    """Preview, then - unless it says stop, or wants a confirmation it did not
    get - apply and verify (6.5, 6.6)."""
    result = run_preview(run, ctx, server, remote)
    if result.get('needs_setup') or result.get('stop'):
        return result
    if result.get('confirm') and not ctx.get('confirm_deletions'):
        result['needs_confirmation'] = True
        return result
    if result.get('up_to_date'):
        result['applied'] = False
        return result

    stage = deployment_pack.stage_dir_for(ctx['catalog_dir'], ctx['deployment_slug'])
    result['bytes_sent'] = apply(run, stage, remote, checksum=result.get('checksum'))
    result['applied'] = True
    problem = restart_if_needed(run, remote, result.get('sends') or [])
    if problem:
        result['warnings'].append(problem)

    run.phase('Checking the site')
    facts = remote_preflight(run, remote)
    staged = _count_files(stage)
    checks, failures, revision = verify_site(
        server['public_url'], ctx['catalog_slug'], ctx['deployment_slug'],
        ctx.get('locked', False), ctx['fetch'])
    same = facts['remote_files'] == staged
    checks.append({'check': 'Every file arrived', 'ok': same,
                   'detail': f"{facts['remote_files']} on the server, {staged} staged"})
    if not same:
        failures.append(checks[-1])
    result['checks'] = checks
    result['server_revision'] = revision
    if failures:
        result['verify_failed'] = '; '.join(f"{c['check']}: {c['detail']}" for c in failures)
    return result


# ── set up: an empty server, once (spec 7) ───────────────────────────────────

_VOLUME = re.compile(r'^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$')
SETUP_VERIFY_TRIES = 6           # Caddy's first certificate can take a minute
SETUP_VERIFY_WAIT = 20.0


def server_checks(run, remote, stage_bytes):
    """
    What an empty server must have before the first gallery goes on it (7.3):
    docker and compose usable by this user, nothing else holding 443 or 80,
    room for the stage, and the gallery image when it is a local one. Returns
    (problems that stop setup, warnings).
    """
    image = remote.server['gallery_image']
    script = ('if docker info >/dev/null 2>&1; then echo DOCKER=ok; '
              'elif command -v docker >/dev/null 2>&1; then echo DOCKER=denied; '
              'else echo DOCKER=missing; fi; '
              'if docker compose version >/dev/null 2>&1; then echo COMPOSE=ok; else echo COMPOSE=missing; fi; '
              'docker ps --format "{{.Names}} {{.Ports}}" 2>/dev/null | '
              'grep -E ":(443|80)->" | sed "s/^/PORT=/"; '
              f'if docker image inspect {shlex.quote(image)} >/dev/null 2>&1; '
              'then echo IMAGE=yes; else echo IMAGE=no; fi')
    rc, out, err = _call(run, remote.command(script), timeout=120)
    if rc != 0 and not out:
        raise _explain_ssh(err, remote.server)
    facts = {}
    ports = []
    for line in out.splitlines():
        if line.startswith('PORT='):
            ports.append(line[5:].split()[0])
        elif '=' in line:
            k, v = line.split('=', 1)
            facts[k] = v.strip()

    stop, warnings = [], []
    if facts.get('DOCKER') == 'missing':
        stop.append('Docker is not installed on the server (guide section 2.1).')
    elif facts.get('DOCKER') == 'denied':
        stop.append(f"{remote.server['ssh_user']} cannot use Docker: add it to the docker group "
                    f"and log in again (guide section 2.1).")
    if facts.get('DOCKER') == 'ok' and facts.get('COMPOSE') != 'ok':
        stop.append('Docker Compose is missing on the server (docker-compose-v2, guide 2.1).')
    if ports:
        stop.append(f"Port 443 or 80 is already used by {', '.join(sorted(set(ports)))}. "
                    f"Stop that first: only one gallery can hold them.")
    if facts.get('IMAGE') == 'no':
        if image.startswith('localhost/'):
            stop.append(f'The gallery image {image} is not on the server. Copy it there first '
                        f'(guide section 3).')
        else:
            warnings.append(f'The gallery image {image} is not on the server yet; Docker will '
                            f'try to download it.')
    return stop, warnings


def run_setup(run, ctx, server, remote):
    """
    Set up an empty server (spec 7): checks, the first full send, .env with a
    secret made ON the server, optionally the certificates of an older gallery
    carried over, then start it and check the site - retried, because Caddy
    gets its first certificate on the first start.
    """
    carry = (ctx.get('carry_certificates_from') or '').strip()
    if carry and not _VOLUME.match(carry):
        raise PublishError('The certificate volume must be a Docker volume name, '
                           'e.g. second-catalog_caddy_data.')

    result, stage = _prepare_local(run, ctx, server)

    run.phase('Checking the server')
    facts = remote_preflight(run, remote)
    result['server'] = facts
    if facts['has_bundle']:
        raise PublishError('This server folder already holds a gallery. Use Publish to '
                           'update it; Set up is only for an empty server.', 'not_empty', 409)
    stop, warnings = server_checks(run, remote, result['stage']['bytes'])
    need = int(result['stage']['bytes'] * 1.1)
    if facts['free_bytes'] < need:
        stop.append(f"The gallery needs {deployment_pack.human(need)} and the server has "
                    f"{deployment_pack.human(facts['free_bytes'])} free. Grow its disk first "
                    f"(guide section 2.3).")
    result['warnings'] += warnings
    if stop:
        result['stop'] = ' '.join(stop)
        return result

    result['bytes_sent'] = apply(run, stage, remote)
    result['applied'] = True
    d = shlex.quote(server['remote_dir'])

    run.phase('Writing the session secret')
    # Made on the server, so it never travels; kept if one is already there.
    secret = ('$(openssl rand -hex 32 2>/dev/null || '
              'head -c 32 /dev/urandom | od -An -tx1 | tr -d " \\n")')
    rc, _out, err = _call(run, remote.command(
        f'cd {d} && if [ ! -f .env ]; then (umask 077; '
        f'printf "ALBUM_SESSION_SECRET=%s\\n" "{secret}" > .env); fi'), timeout=60)
    if rc != 0:
        raise PublishError(f'Could not write .env on the server: {err.strip()[-200:]}')

    project = os.path.basename(server['remote_dir'].rstrip('/'))
    if carry:
        run.phase('Carrying over the certificates')
        target = shlex.quote(f'{project}_caddy_data')
        rc, _out, err = _call(run, remote.command(
            f'docker volume inspect {shlex.quote(carry)} >/dev/null && '
            f'docker volume create {target} >/dev/null && '
            f'docker run --rm -v {shlex.quote(carry)}:/from:ro -v {target}:/to caddy:2 '
            f'sh -c "cp -a /from/. /to/"'), timeout=600)
        if rc != 0:
            result['warnings'].append(f'The certificates of {carry} were not carried over '
                                      f'({err.strip()[-150:]}); Caddy will request new ones.')

    run.phase('Starting the gallery')
    rc, out, err = _call(run, remote.command(f'cd {d} && docker compose up -d'), timeout=900)
    if rc != 0:
        raise PublishError(f'The files are on the server, but `docker compose up -d` failed: '
                           f'{(err or out).strip()[-300:]}', 'compose_failed', 502)

    run.phase('Checking the site')
    tries = ctx.get('verify_tries', SETUP_VERIFY_TRIES)
    wait = ctx.get('verify_wait', SETUP_VERIFY_WAIT)
    for attempt in range(1, tries + 1):
        checks, failures, revision = verify_site(
            server['public_url'], ctx['catalog_slug'], ctx['deployment_slug'],
            ctx.get('locked', False), ctx['fetch'])
        if not failures or attempt == tries:
            break
        run.log(f'site not ready yet (try {attempt} of {tries}): '
                + '; '.join(f"{c['check']}: {c['detail']}" for c in failures))
        for _ in range(int(wait * 10)):
            run.check_stop()
            time.sleep(0.1)
    result['checks'] = checks
    result['server_revision'] = revision
    if failures:
        result['verify_failed'] = '; '.join(f"{c['check']}: {c['detail']}" for c in failures)
    return result


def execute(run, kind, ctx, server, remote):
    """Run a kind of run to its end, writing the outcome. Never raises."""
    try:
        if kind == 'preview':
            result = run_preview(run, ctx, server, remote)
        elif kind == 'publish':
            result = run_publish(run, ctx, server, remote)
        elif kind == 'setup':
            result = run_setup(run, ctx, server, remote)
        else:
            raise PublishError(f'{kind} is not available yet.', 'not_available')
        plan = result.get('plan') or {}
        counts = dict(files_new=plan.get('new'), files_changed=plan.get('changed'),
                      files_deleted=plan.get('deleted'),
                      bytes_sent=result.get('bytes_sent', plan.get('bytes')))
        result.pop('sends', None)
        if result.get('stop'):
            run.finish('failed', error=result['stop'], result_json=result)
        elif result.get('needs_setup') and kind == 'publish':
            run.finish('failed', error=result['message'], result_json=result)
        elif result.get('needs_confirmation'):
            run.finish('failed', error=result['confirm'] + ' Publish again and confirm.',
                       result_json=result)
        elif result.get('verify_failed'):
            run.log(f"check failed: {result['verify_failed']}")
            run.finish('failed', error='Sent, but the site check failed: '
                       + result['verify_failed'], result_json=result, **counts)
        else:
            run.finish('succeeded', result_json=result, **counts)
    except Stopped:
        run.finish('stopped', error='Stopped')
    except PublishError as e:
        run.log(f'error: {e.message}')
        run.finish('failed', error=e.message)
    except deployment_pack.PackError as e:
        run.log(f'error: {e}')
        run.finish('failed', error=str(e))
    except Exception as e:                      # a bug: say so rather than hang
        run.log(f'unexpected: {e!r}')
        run.finish('failed', error=f'Unexpected error: {e}')
    finally:
        try:
            remote.close()
        except Exception:
            pass
