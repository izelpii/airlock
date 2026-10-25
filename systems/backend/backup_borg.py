"""
Running BorgBackup for catalog backups - the Backup tab's second type, beside
restic (backup_restic.py, unchanged by this). documentation/features/backups.md.

borg needs borg on BOTH ends: Airlock runs the client here, in the backend,
and reaches the repository over SSH, where `borg serve` reads and writes it
(on backup-host: /opt/borg/bin/borg - documentation/guides/BORG_INSTALL.md). The
SSH key is Airlock's borg key (backup_ssh.KEY_BORG), restricted on the host to
`borg serve --restrict-to-path`.

The repository and passphrase travel in the environment (BORG_REPO,
BORG_PASSPHRASE), never on a command line. Repositories are created
`repokey-blake2`: the key is stored in the repository, encrypted with the
passphrase - as with restic, the passphrase alone opens it.

borg has no tags. A card's archives are told apart by NAME:
  <host>-<catalog>-card<id>-<UTC time>
so listing and pruning use the prefix (--glob-archives '<prefix>*'), and two
installations or two cards sharing one repository never touch each other's.

borg's errors are turned into sentences a person can act on (_explain).
"""

import json
import os
import re
import subprocess

import backup_ssh

APP_DIR = os.path.dirname(os.path.abspath(__file__))
# borg's own state: the security dir (what it knows about each repository) and
# the chunks/files caches that make the second backup fast. Bind-mounted.
BASE_DIR = os.environ.get('BORG_BASE_DIR_AIRLOCK',
                          os.path.join(APP_DIR, 'database', 'borg'))
BORG = os.environ.get('BORG_BIN', 'borg')
DEFAULT_REMOTE_PATH = '/opt/borg/bin/borg'
LOCK_WAIT = '1800'                          # seconds a run waits for another's lock
ARCHIVE_NAME = re.compile(r'^[A-Za-z0-9._:+-]{1,200}$')
REMOTE_PATH = re.compile(r'^[A-Za-z0-9_./-]{1,200}$')

# borg 1.x exit codes: 0 success, 1 warning (e.g. a file changed while read -
# the archive exists), 2 error. 128+N: killed by signal N.
WARNING = 1


class BorgError(Exception):
    def __init__(self, message, kind='error'):
        super().__init__(message)
        self.kind = kind


def _explain(text, uri):
    s = text or ''
    if 'Permission denied (publickey' in s:
        return BorgError(f'The backup host refused Airlock\'s borg SSH key for {uri}: add the '
                         'borg key line shown in this tab to its authorized_keys.', 'key')
    if 'Host key verification failed' in s or 'REMOTE HOST IDENTIFICATION HAS CHANGED' in s:
        return BorgError('The backup host\'s SSH key is not the one Airlock first saw. If it was '
                         'reinstalled, remove its line from database/backup-ssh/known_hosts.', 'hostkey')
    if 'passphrase supplied' in s.lower() or 'passphrase is incorrect' in s.lower():
        return BorgError('The repository passphrase is wrong.', 'password')
    if 'PathNotAllowed' in s or 'is not allowed' in s:
        return BorgError(f'{uri} is outside the folder Airlock\'s borg key may use on that host '
                         '(--restrict-to-path in its authorized_keys line).', 'restricted')
    if 'command not found' in s or 'No such file or directory' in s and 'serve' in s \
            or 'unexpected RPC data format' in s:
        return BorgError('borg is not at the path given for the backup host - check "borg on '
                         'the server" (on backup-host: /opt/borg/bin/borg).', 'remote')
    if 'does not exist' in s or 'is not a valid repository' in s:
        return BorgError(f'There is no borg repository at {uri} yet.', 'missing')
    if 'already exists' in s:
        return BorgError(f'A repository already exists at {uri}.', 'exists')
    if 'Failed to create/acquire the lock' in s or 'lock' in s.lower() and 'timeout' in s.lower():
        return BorgError('The repository is busy (locked by another borg). Try again shortly.',
                         'locked')
    if 'Connection refused' in s or 'Connection timed out' in s or 'No route to host' in s \
            or 'Could not resolve hostname' in s or 'Connection closed by remote host' in s:
        return BorgError(f'Cannot reach the backup host for {uri}.', 'unreachable')
    lines = [l for l in s.strip().splitlines() if l.strip()]
    return BorgError((lines[-1] if lines else 'borg failed')[:400])


def log_text(lines):
    """The messages in borg's --log-json stderr, as plain text (for _explain)."""
    out = []
    for line in lines:
        try:
            msg = json.loads(line)
        except ValueError:
            out.append(line.rstrip())
            continue
        if msg.get('type') == 'log_message':
            out.append(msg.get('message') or '')
    return '\n'.join(out)


def archive_prefix(ident):
    """<host>-<catalog>-card<id>- : this card's archives, and nobody else's."""
    slug = re.sub(r'[^A-Za-z0-9._-]+', '-', ident['catalog']).strip('-') or 'catalog'
    return f'{ident["host"]}-{slug}-card{ident["card"]}-'


def remote_path_of(options):
    rp = (options or {}).get('remote_path') or DEFAULT_REMOTE_PATH
    return rp if REMOTE_PATH.match(rp) else DEFAULT_REMOTE_PATH


class Repo:
    """One borg repository: its location, passphrase and the borg path on its host."""

    TYPE = 'borg'

    def __init__(self, uri, password, env=None, options=None):
        self.uri = (uri or '').strip()
        self.password = password or ''
        self.extra_env = [e for e in (env or []) if isinstance(e, str) and '=' in e]
        self.options = dict(options or {})

    def ssh_host(self):
        p = backup_ssh.parse_borg(self.uri)
        return (p[1], p[2]) if p else None

    def _env(self):
        env = dict(os.environ)
        env.update(BORG_REPO=self.uri, BORG_PASSPHRASE=self.password,
                   BORG_BASE_DIR=BASE_DIR, BORG_REMOTE_PATH=remote_path_of(self.options),
                   BORG_RSH=backup_ssh.borg_rsh(),
                   # Never a question on a terminal nobody is at.
                   BORG_RELOCATED_REPO_ACCESS_IS_OK='no',
                   BORG_UNKNOWN_UNENCRYPTED_REPO_ACCESS_IS_OK='no',
                   BORG_CHECK_I_KNOW_WHAT_I_AM_DOING='NO',
                   BORG_DELETE_I_KNOW_WHAT_I_AM_DOING='NO',
                   BORG_DISPLAY_PASSPHRASE='no',
                   # Archive times are written in the process's zone: UTC.
                   TZ='UTC')
        for item in self.extra_env:
            k, v = item.split('=', 1)
            env[k.strip()] = v
        return env

    def run(self, *args, timeout=120):
        """A short command; raises BorgError with the reason."""
        os.makedirs(BASE_DIR, exist_ok=True)
        try:
            r = subprocess.run([BORG, *args], env=self._env(), capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise BorgError(f'borg took longer than {timeout} s to answer for {self.uri}',
                            'unreachable')
        except FileNotFoundError:
            raise BorgError('borg is not installed in this backend (rebuild its image).')
        if r.returncode not in (0, WARNING):
            raise _explain(log_text(r.stderr.splitlines()) or r.stderr, self.uri)
        return r.stdout

    def popen(self, *args, cwd=None):
        """A long command (create, check, extract), for the caller to stream.
        Progress and log messages come as JSON lines on stderr (--log-json)."""
        os.makedirs(BASE_DIR, exist_ok=True)
        return subprocess.Popen([BORG, *args], env=self._env(), cwd=cwd,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, bufsize=1)

    # ── short questions ─────────────────────────────────────────────────────

    def probe(self):
        """The repository id | raises BorgError (.kind 'missing' when there is none)."""
        host = self.ssh_host()
        if host:
            backup_ssh.trust_host(*host)
        out = self.run('info', '--json', '--lock-wait', '60', timeout=300)
        return (json.loads(out).get('repository') or {}).get('id')

    def init(self):
        self.run('init', '--encryption', 'repokey-blake2', '--make-parent-dirs', timeout=300)

    def snapshots(self, ident):
        """This card's archives, newest first, in the shape the Restore dialog reads."""
        out = self.run('list', '--json', '--glob-archives', archive_prefix(ident) + '*',
                       '--lock-wait', LOCK_WAIT, timeout=300)
        archives = (json.loads(out or '{}') or {}).get('archives') or []
        snaps = []
        for a in archives:
            # A stopped backup leaves '<name>.checkpoint': partial, never offered
            # for a restore; prune removes it once a complete archive follows.
            if '.checkpoint' in (a.get('name') or ''):
                continue
            start = a.get('start') or a.get('time')
            snaps.append({'id': a.get('name'), 'short_id': (a.get('id') or '')[:8],
                          'time': (start + '+00:00') if start and '+' not in start else start,
                          'host': ident['host'], 'paths': [], 'files': None, 'bytes': None})
        snaps.sort(key=lambda s: s['time'] or '', reverse=True)
        return snaps

    def prune(self, ident, keep):
        """The card's retention on its own archives, then compact - borg frees the
        space only on compact, and no other program maintains a borg repository."""
        args = ['prune', '--glob-archives', archive_prefix(ident) + '*', '--lock-wait', LOCK_WAIT]
        for k in ('daily', 'weekly', 'monthly', 'yearly'):
            if int(keep.get(k) or 0) > 0:
                args += [f'--keep-{k}', str(int(keep[k]))]
        if len(args) == 5:                      # nothing to keep by: keep everything
            return None
        self.run(*args, timeout=1800)
        return self.run('compact', '--lock-wait', LOCK_WAIT, timeout=3 * 3600)

    # ── long commands ───────────────────────────────────────────────────────

    def create_args(self, ident, paths, excludes, stable_inodes=True):
        args = ['create', '--json', '--log-json', '--progress', '--lock-wait', LOCK_WAIT,
                '--compression', 'lz4', '--exclude-caches']
        if not stable_inodes:
            # borg's files cache keys on ctime, size and inode by default; on a
            # filesystem that invents inode numbers (exFAT) that re-reads every
            # file on every run. Modification time and size instead.
            args += ['--files-cache', 'mtime,size']
        for e in excludes:
            # Glob patterns as restic reads them ('**' crosses folders, '*' does not).
            args += ['--exclude', f'sh:{e.lstrip("/")}' if '*' in e else e]
        return args + [f'::{archive_prefix(ident)}{{utcnow:%Y-%m-%dT%H:%M:%S}}', *paths]

    @staticmethod
    def check_args():
        return ['check', '--log-json', '--progress', '--lock-wait', LOCK_WAIT]

    @staticmethod
    def extract_args(archive, include=None):
        """Run with cwd = the restore folder: borg extracts into the current folder."""
        args = ['extract', '--log-json', '--progress', '--lock-wait', LOCK_WAIT, f'::{archive}']
        if include:
            args.append(include.lstrip('/'))
        return args


def parse_event(line):
    """One --log-json line: ('progress', {...}) | ('log', text) | None."""
    try:
        msg = json.loads(line)
    except ValueError:
        return None
    t = msg.get('type')
    if t == 'archive_progress' and not msg.get('finished'):
        return ('progress', {'percent': None, 'bytes_done': msg.get('original_size') or 0,
                             'bytes_total': 0, 'files_done': msg.get('nfiles') or 0,
                             'files_total': 0, 'seconds_remaining': None})
    if t == 'progress_percent' and not msg.get('finished'):
        cur, total = msg.get('current') or 0, msg.get('total') or 0
        return ('progress', {'percent': round(100 * cur / total, 1) if total else None,
                             'bytes_done': 0, 'bytes_total': 0, 'files_done': 0,
                             'files_total': 0, 'seconds_remaining': None})
    if t == 'log_message':
        return ('log', msg.get('message') or '')
    return None


def create_summary(stdout):
    """borg create --json's result, in the card's terms."""
    try:
        archive = (json.loads(stdout or '{}') or {}).get('archive') or {}
    except ValueError:
        return {}
    stats = archive.get('stats') or {}
    return {'snapshot_id': archive.get('name'), 'files_new': None,
            'data_added': stats.get('deduplicated_size'),
            'total_files_processed': stats.get('nfiles'),
            'total_bytes_processed': stats.get('original_size')}
