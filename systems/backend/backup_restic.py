"""
Running restic for catalog backups. documentation/specs/backup-backrest-spec.md.

Airlock runs restic itself, in the backend, where the catalogs are; the
repository is on another machine. Every call gets the repository and its
password through the environment (RESTIC_REPOSITORY, RESTIC_PASSWORD), never
on a command line a process listing would show, and --json where restic has it.

restic's own errors are turned into sentences a person can act on (_explain).
"""

import json
import os
import subprocess

import backup_ssh

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CACHE_DIR = os.environ.get('RESTIC_CACHE_DIR',
                           os.path.join(APP_DIR, 'database', 'restic-cache'))
RESTIC = os.environ.get('RESTIC_BIN', 'restic')


class ResticError(Exception):
    def __init__(self, message, kind='error'):
        super().__init__(message)
        self.kind = kind


def _explain(stderr, uri):
    s = stderr or ''
    if 'Permission denied (publickey' in s:
        return ResticError(f'The backup host refused Airlock\'s SSH key for {uri}: add the key shown '
                           'in this tab to its authorized_keys.', 'key')
    if 'Host key verification failed' in s or 'REMOTE HOST IDENTIFICATION HAS CHANGED' in s:
        return ResticError('The backup host\'s SSH key is not the one Airlock first saw. If it was '
                           'reinstalled, remove its line from database/backup-ssh/known_hosts.', 'hostkey')
    if 'wrong password' in s or 'no key found' in s:
        return ResticError('The repository password is wrong.', 'password')
    if 'Is there a repository at the following location' in s or 'unable to open config file' in s \
            or 'does not exist' in s:
        return ResticError(f'There is no restic repository at {uri} yet.', 'missing')
    if 'repository is already locked' in s:
        return ResticError('The repository is busy (locked by another restic, perhaps a prune on '
                           'the backup host). Try again shortly.', 'locked')
    if 'Connection refused' in s or 'Connection timed out' in s or 'No route to host' in s \
            or 'Could not resolve hostname' in s:
        return ResticError(f'Cannot reach the backup host for {uri}.', 'unreachable')
    tail = s.strip().splitlines()[-1] if s.strip() else 'restic failed'
    return ResticError(tail[:400])


class Repo:
    """One restic repository: its URI, password and any extra environment."""

    def __init__(self, uri, password, env=None):
        self.uri = (uri or '').strip()
        self.password = password or ''
        self.extra_env = [e for e in (env or []) if isinstance(e, str) and '=' in e]

    def _env(self):
        env = dict(os.environ)
        env.update(RESTIC_REPOSITORY=self.uri, RESTIC_PASSWORD=self.password,
                   RESTIC_CACHE_DIR=CACHE_DIR, RESTIC_PROGRESS_FPS='0.5')
        for item in self.extra_env:
            k, v = item.split('=', 1)
            env[k.strip()] = v
        return env

    def _args(self, *args):
        cmd = [RESTIC]
        sftp = backup_ssh.parse_sftp(self.uri)
        if sftp:
            user, host, port, _ = sftp
            cmd += ['-o', f'sftp.command={backup_ssh.sftp_command(user, host, port)}']
        return cmd + list(args)

    def run(self, *args, timeout=120):
        """A short command; raises ResticError with the reason."""
        os.makedirs(CACHE_DIR, exist_ok=True)
        try:
            r = subprocess.run(self._args(*args), env=self._env(), capture_output=True,
                               text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise ResticError(f'restic took longer than {timeout} s to answer for {self.uri}',
                              'unreachable')
        except FileNotFoundError:
            raise ResticError('restic is not installed in this backend (rebuild its image).')
        if r.returncode != 0:
            raise _explain(r.stderr, self.uri)
        return r.stdout

    def popen(self, *args):
        """A long command (backup, check, restore), for the caller to stream."""
        os.makedirs(CACHE_DIR, exist_ok=True)
        return subprocess.Popen(self._args(*args), env=self._env(), stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, text=True, bufsize=1)

    # ── short questions ─────────────────────────────────────────────────────

    def probe(self):
        """('ok', repo id) | raises ResticError (.kind 'missing' when there is none)."""
        if backup_ssh.parse_sftp(self.uri):
            user, host, port, _ = backup_ssh.parse_sftp(self.uri)
            backup_ssh.trust_host(host, port)
        out = self.run('cat', 'config', '--json', '--no-lock', timeout=60)
        return json.loads(out).get('id')

    def init(self):
        self.run('init', '--json', timeout=180)

    def snapshots(self, tags):
        out = self.run('snapshots', '--json', '--no-lock', '--tag', ','.join(tags), timeout=180)
        return json.loads(out or '[]') or []

    def forget(self, tags, keep):
        args = ['forget', '--json', '--retry-lock', '10m', '--tag', ','.join(tags),
                '--group-by', 'host,paths']
        for k in ('daily', 'weekly', 'monthly', 'yearly'):
            if int(keep.get(k) or 0) > 0:
                args += [f'--keep-{k}', str(int(keep[k]))]
        if len(args) == 8:                      # nothing to keep by: keep everything
            return None
        # forget needs the repository to itself. A lock left by a backup the
        # backend restart cut off lingers until restic calls it stale (30 min
        # without a refresh); plain `unlock` removes only those, so it is safe
        # with the Mac or backup-host using the repository too. Never --remove-all.
        self.unlock()
        return self.run(*args, timeout=600)

    def unlock(self):
        """Remove STALE locks only. Best effort: a failure here is forget's to report."""
        try:
            self.run('unlock', timeout=120)
            return True
        except ResticError:
            return False
