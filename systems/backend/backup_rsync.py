"""
Running rsync for catalog backups - the Backup tab's third type, beside restic
(backup_restic.py) and borg (backup_borg.py), which this does not change.
documentation/features/backups.md.

The simplest of the three: a plain copy of the catalog in a folder on another
machine, browsable with any file manager. No encryption, no versions - one
mirror, brought up to date by each backup. What the mirror no longer needs
(files deleted or replaced in the catalog) is not destroyed but moved aside,
into `.airlock-replaced/<UTC time>/` inside it, so a deletion in the catalog
is never silently propagated; clearing that folder is left to the owner.

A destination is `user@host:/absolute/path` over SSH, or a local folder. Over
SSH the key is Airlock's rsync key (backup_ssh.KEY_RSYNC), locked on the host
with `rrsync <folder>` (shipped with rsync): rsync only, inside that folder.
rrsync takes paths RELATIVE to its folder, so the card stores the folder it
was locked to (options['root'], the destination's parent by default) and the
client sends the path below it.

rsync's errors are turned into sentences a person can act on (_explain).
"""

import os
import posixpath
import re
import subprocess

import backup_ssh

RSYNC = os.environ.get('RSYNC_BIN', 'rsync')
REPLACED_DIR = '.airlock-replaced'
SNAPSHOT_ID = 'current'                     # the one copy there is

# user@host:/path - user optional, path absolute.
_SSH = re.compile(r'^(?:(?P<user>[^@:/\s]+)@)?(?P<host>[^:/\s]+):(?P<path>/\S*)$')

# rsync exit codes: 0 done; 23 some files could not be transferred, 24 some
# vanished while being copied - the copy exists, with gaps: warnings.
WARNINGS = (23, 24)


class RsyncError(Exception):
    def __init__(self, message, kind='error'):
        super().__init__(message)
        self.kind = kind


def parse_dest(uri):
    """(user, host, path) for an SSH destination, else None (a local folder)."""
    m = _SSH.match((uri or '').strip())
    return (m['user'], m['host'], m['path']) if m else None


def _explain(text, uri):
    s = text or ''
    if 'Permission denied (publickey' in s:
        return RsyncError(f'The backup host refused Airlock\'s rsync SSH key for {uri}: add the '
                          'rsync key line shown in this tab to its authorized_keys.', 'key')
    if 'Host key verification failed' in s or 'REMOTE HOST IDENTIFICATION HAS CHANGED' in s:
        return RsyncError('The backup host\'s SSH key is not the one Airlock first saw. If it was '
                          'reinstalled, remove its line from database/backup-ssh/known_hosts.', 'hostkey')
    if 'rrsync' in s and ('not allowed' in s or 'outside' in s or 'refusing' in s.lower()):
        return RsyncError(f'{uri} is outside the folder Airlock\'s rsync key may use on that host '
                          '(the rrsync folder in its authorized_keys line).', 'restricted')
    if 'No such file or directory' in s and ('change_dir' in s or 'link_stat' in s):
        return RsyncError(f'There is no folder at {uri} yet.', 'missing')
    if 'Permission denied' in s:
        return RsyncError(f'The backup host does not let Airlock write to {uri}.', 'denied')
    if 'rsync: command not found' in s or 'rsync: not found' in s \
            or 'remote command not found' in s or 'rrsync: not found' in s:
        return RsyncError('rsync is not installed on the backup host.', 'remote')
    if 'No space left on device' in s:
        return RsyncError('The backup disk is full.', 'full')
    if 'Connection refused' in s or 'Connection timed out' in s or 'No route to host' in s \
            or 'Could not resolve hostname' in s or 'connection unexpectedly closed' in s:
        return RsyncError(f'Cannot reach the backup host for {uri}.', 'unreachable')
    lines = [l for l in s.strip().splitlines() if l.strip()]
    return RsyncError((lines[-1] if lines else 'rsync failed')[:400])


# "  1,234,567  45%   10.00MB/s    0:00:12 (xfr#5, to-chk=10/100)"
_PROGRESS = re.compile(r'^\s*([\d,]+)\s+(\d+)%\s+\S+\s+(\d+):(\d+):(\d+)(?:.*to-chk=(\d+)/(\d+))?')


def parse_progress(line):
    """One --info=progress2 line: progress, or None."""
    m = _PROGRESS.match(line)
    if not m:
        return None
    done, pct = int(m[1].replace(',', '')), int(m[2])
    total_files = int(m[7]) if m[7] else 0
    return {'percent': float(pct), 'bytes_done': done,
            'bytes_total': int(done * 100 / pct) if pct else 0,
            'files_done': total_files - int(m[6]) if m[7] else 0, 'files_total': total_files,
            'seconds_remaining': None}


def parse_stats(text):
    """The --stats block, in the card's terms."""
    def num(label):
        m = re.search(rf'^{label}:\s*([\d,]+)', text or '', re.M)
        return int(m[1].replace(',', '')) if m else None
    return {'snapshot_id': None,
            'files_new': num('Number of regular files transferred'),
            'data_added': num('Total transferred file size'),
            'total_files_processed': num('Number of files'),
            'total_bytes_processed': num('Total file size')}


def excludes_for(root, excludes):
    """backups.paths_for's excludes (absolute paths and restic-style globs) as
    rsync filters, anchored at the catalog folder - the transfer's root."""
    root = root.rstrip('/')
    out = []
    for e in excludes:
        if e.startswith('**/'):
            out.append(e[3:])                        # a name anywhere
        elif e == root or e.startswith(root + '/'):
            out.append('/' + e[len(root) + 1:])      # anchored at the catalog
        else:
            out.append(e)
    return out + [f'/{REPLACED_DIR}/']


class Repo:
    """One rsync destination: a folder here or on another machine."""

    TYPE = 'rsync'

    def __init__(self, uri, password=None, env=None, options=None):
        self.uri = (uri or '').strip().rstrip('/') or '/'
        self.password, self.extra_env = None, []          # a plain copy: no secret
        self.options = dict(options or {})
        self.dest = parse_dest(self.uri)

    def ssh_host(self):
        return (self.dest[1], 22) if self.dest else None

    def root(self):
        """The folder the rsync key is locked to on the host (rrsync)."""
        if not self.dest:
            return None
        return (self.options.get('root') or posixpath.dirname(self.dest[2].rstrip('/')) or '/').rstrip('/') or '/'

    def target(self, sub=''):
        """What rsync is given for the destination (or a path inside it)."""
        if not self.dest:
            return posixpath.join(self.uri, sub) if sub else self.uri
        user, host, path = self.dest
        rel = posixpath.relpath(path, self.root()) if self.root() != '/' else path.lstrip('/')
        if rel.startswith('..'):
            raise RsyncError(f'{path} is not inside {self.root()}, the folder the rsync key '
                             'is locked to.', 'restricted')
        rel = posixpath.join(rel, sub) if sub else rel
        return f'{user + "@" if user else ""}{host}:{rel}'

    def _base(self):
        cmd = [RSYNC]
        if self.dest:
            cmd += ['-e', backup_ssh.rsync_rsh()]
        return cmd

    def run(self, *args, timeout=120):
        try:
            r = subprocess.run([*self._base(), *args], capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            raise RsyncError(f'rsync took longer than {timeout} s to answer for {self.uri}',
                             'unreachable')
        except FileNotFoundError:
            raise RsyncError('rsync is not installed in this backend (rebuild its image).')
        if r.returncode != 0:
            raise _explain(r.stderr, self.uri)
        return r.stdout

    def popen(self, *args, cwd=None):
        """Progress lines end in carriage returns; text mode reads them as lines."""
        return subprocess.Popen([*self._base(), *args], cwd=cwd, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)

    # ── short questions ─────────────────────────────────────────────────────

    def probe(self):
        """The folder's address | raises RsyncError (.kind 'missing' when it is not there)."""
        if self.dest:
            backup_ssh.trust_host(self.dest[1])
        self.run('--list-only', '--dirs', self.target() + '/', timeout=60)
        return self.uri

    def init(self):
        """Create the (empty) folder."""
        empty = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'airlock-rsync-empty')
        os.makedirs(empty, exist_ok=True)
        self.run('-r', '--mkpath', empty + '/', self.target() + '/', timeout=120)

    # ── long commands ───────────────────────────────────────────────────────

    def backup_args(self, local_root, paths, excludes, stamp):
        """Mirror the scope's paths into the destination, keeping the catalog's
        layout (Images/, Videos/, cache/backup/ …)."""
        root = local_root.rstrip('/')
        args = ['-a', '--relative', '--delete', '--delete-excluded', '--mkpath',
                '--backup', f'--backup-dir={REPLACED_DIR}/{stamp}',
                '--info=progress2', '--no-inc-recursive', '--stats', '--no-human-readable',
                '--partial', '--timeout=600']
        for e in excludes_for(root, excludes):
            args += ['--exclude', e]
        sources = [f'{root}/./{os.path.relpath(p, root)}' if p != root else f'{root}/./'
                   for p in paths]
        return args + sources + [self.target() + '/']

    def drop_replaced_db_copies(self, stamp, snapshot_dirname):
        """The database copies a backup replaced are superseded by the ones it
        just wrote - on family 40 GB a night if kept. Empty their folder in
        .airlock-replaced/<stamp>/ (photos and videos replaced stay there).
        Best effort: nothing to empty is not an error."""
        empty = os.path.join(os.environ.get('TMPDIR', '/tmp'), 'airlock-rsync-empty')
        os.makedirs(empty, exist_ok=True)
        dest = self.target(f'{REPLACED_DIR}/{stamp}/{snapshot_dirname}') + '/'
        try:
            self.run('-r', '--delete', empty + '/', dest, timeout=300)
        except RsyncError:
            pass

    def verify_args(self, local_root, paths, excludes):
        """Compare the copy with the catalog, by content: a dry run that lists
        every file that would change. None listed = the copy is whole."""
        root = local_root.rstrip('/')
        args = ['-a', '--relative', '--delete', '--dry-run', '--checksum', '--itemize-changes',
                '--out-format=%i %n', '--timeout=600']
        for e in excludes_for(root, excludes):
            args += ['--exclude', e]
        sources = [f'{root}/./{os.path.relpath(p, root)}' if p != root else f'{root}/./'
                   for p in paths]
        return args + sources + [self.target() + '/']

    def restore_args(self, local_root, include, restore_dir):
        """Copy the mirror (or one folder of it) into restore_dir, in the
        catalog's own layout."""
        rel = ''
        if include:
            root = local_root.rstrip('/')
            inc = include.rstrip('/')
            rel = os.path.relpath(inc, root) if inc == root or inc.startswith(root + '/') \
                else inc.lstrip('/')
            rel = '' if rel == '.' else rel
        src = self.target() + '/./' + (rel + ('/' if rel else ''))
        return ['-a', '--relative', '--info=progress2', '--no-inc-recursive', '--stats',
                '--no-human-readable', '--exclude', f'/{REPLACED_DIR}/', src, restore_dir.rstrip('/') + '/']


def changed_files(stdout):
    """verify_args' output: the files whose copy is missing, different or
    extra. Folders, and files differing only in attributes, do not count."""
    out = []
    for line in (stdout or '').splitlines():
        flags, _, name = line.partition(' ')
        name = name.strip()
        if not name or name.endswith('/'):
            continue
        if flags.startswith('*deleting') or (flags[:1] in ('>', 'c') and flags[1:2] in ('f', 'L')):
            out.append(f'{flags} {name}')
    return out
