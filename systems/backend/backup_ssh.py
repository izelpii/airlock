"""
Airlock's own SSH identity for SFTP backup repositories.
documentation/specs/backup-backrest-spec.md.

Two keys per installation, made the first time each is needed, kept in the
application's database folder (bind-mounted, so it survives a rebuild):

  id_ed25519        restic, over SFTP - restricted on the host to sftp-server
  id_ed25519_borg   borg - restricted on the host to `borg serve`
  id_ed25519_rsync  rsync - restricted on the host to `rrsync <folder>`

Two, because an authorized_keys line forces one command: the same key cannot
be both. Each public half is shown in the Backup tab with its line.

A backup host's key is learned the first time a repository on it is tested
(trust on first use, shown as a fingerprint) and pinned from then on: a host
whose key later changes is refused rather than trusted.
"""

import os
import re
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SSH_DIR = os.environ.get('BACKUP_SSH_DIR', os.path.join(APP_DIR, 'database', 'backup-ssh'))
KEY = os.path.join(SSH_DIR, 'id_ed25519')
KEY_BORG = os.path.join(SSH_DIR, 'id_ed25519_borg')
KEY_RSYNC = os.path.join(SSH_DIR, 'id_ed25519_rsync')
KNOWN_HOSTS = os.path.join(SSH_DIR, 'known_hosts')

# borg: ssh://user@host[:port]/absolute/path
_BORG = re.compile(r'^ssh://(?:(?P<user>[^@:/]+)@)?(?P<host>[^:/]+)(?::(?P<port>\d+))?(?P<path>/.*)$')

# sftp:user@host:/path   or   sftp://user@host:port//path
_SHORT = re.compile(r'^sftp:(?:(?P<user>[^@:/]+)@)?(?P<host>[^:/]+):(?P<path>.+)$')
_LONG = re.compile(r'^sftp://(?:(?P<user>[^@:/]+)@)?(?P<host>[^:/]+)(?::(?P<port>\d+))?/(?P<path>.*)$')


def parse_sftp(uri):
    """(user, host, port, path) for an SFTP repository URI, else None."""
    uri = (uri or '').strip()
    m = _LONG.match(uri)
    if m:
        return m['user'], m['host'], int(m['port'] or 22), m['path']
    m = _SHORT.match(uri)
    if m and not uri.startswith('sftp://'):
        return m['user'], m['host'], 22, m['path']
    return None


def parse_borg(uri):
    """(user, host, port, path) for a borg ssh:// repository, else None."""
    m = _BORG.match((uri or '').strip())
    return (m['user'], m['host'], int(m['port'] or 22), m['path']) if m else None


def ensure_key(kind='restic'):
    key, comment = {'borg': (KEY_BORG, 'airlock-borg'),
                    'rsync': (KEY_RSYNC, 'airlock-rsync')}.get(kind, (KEY, 'airlock-backups'))
    os.makedirs(SSH_DIR, exist_ok=True)
    os.chmod(SSH_DIR, 0o700)
    if not os.path.exists(key):
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', comment,
                        '-f', key], check=True, capture_output=True)
    return key


def public_key(kind='restic'):
    key = ensure_key(kind)
    with open(key + '.pub') as fh:
        return fh.read().strip()


def borg_authorized_keys_line(remote_path='borg', restrict_to='/'):
    """The borg key's line for the backup host: `borg serve` only, inside one folder."""
    return (f'command="{remote_path} serve --restrict-to-path {restrict_to}",restrict '
            f'{public_key("borg")}')


def borg_rsh():
    """What borg runs to reach the repository (BORG_RSH): the borg key, the pinned host."""
    key = ensure_key('borg')
    return (f'ssh -i {key} -o IdentitiesOnly=yes -o UserKnownHostsFile={KNOWN_HOSTS} '
            f'-o StrictHostKeyChecking=yes -o BatchMode=yes -o ServerAliveInterval=30 '
            f'-o ServerAliveCountMax=6')


def authorized_keys_line():
    """The line for the backup host: file transfer only - no shell, no forwarding."""
    return f'restrict,command="/usr/lib/openssh/sftp-server" {public_key()}'


def _host_entry(host, port):
    return host if port == 22 else f'[{host}]:{port}'


def known_fingerprint(host, port=22):
    if not os.path.exists(KNOWN_HOSTS):
        return None
    r = subprocess.run(['ssh-keygen', '-l', '-F', _host_entry(host, port), '-f', KNOWN_HOSTS],
                       capture_output=True, text=True)
    m = re.search(r'(SHA256:\S+)', r.stdout)
    return m.group(1) if m else None


def trust_host(host, port=22):
    """The host's key fingerprint, learning it the first time (TOFU)."""
    known = known_fingerprint(host, port)
    if known:
        return known
    os.makedirs(SSH_DIR, exist_ok=True)
    r = subprocess.run(['ssh-keyscan', '-T', '8', '-t', 'ed25519', '-p', str(port), host],
                       capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l and not l.startswith('#')]
    if not lines:
        raise RuntimeError(f'Could not read the SSH host key of {host}:{port} - is SSH running there?')
    with open(KNOWN_HOSTS, 'a') as fh:
        fh.write('\n'.join(lines) + '\n')
    os.chmod(KNOWN_HOSTS, 0o600)
    return known_fingerprint(host, port)


def sftp_command(user, host, port=22):
    """What restic runs to reach the repository (its sftp.command)."""
    ensure_key()
    target = f'{user}@{host}' if user else host
    return (f'ssh -i {KEY} -o IdentitiesOnly=yes -o UserKnownHostsFile={KNOWN_HOSTS} '
            f'-o StrictHostKeyChecking=yes -o BatchMode=yes -o ServerAliveInterval=30 '
            f'-o ServerAliveCountMax=6 -p {port} {target} -s sftp')


def rsync_authorized_keys_line(root):
    """The rsync key's line for the backup host: rsync only, inside one folder
    (rrsync ships with rsync, /usr/bin/rrsync on Ubuntu 22.04+)."""
    return f'command="rrsync {root}",restrict {public_key("rsync")}'


def rsync_rsh():
    """What rsync runs to reach the destination (-e): the rsync key, the pinned host."""
    key = ensure_key('rsync')
    return (f'ssh -i {key} -o IdentitiesOnly=yes -o UserKnownHostsFile={KNOWN_HOSTS} '
            f'-o StrictHostKeyChecking=yes -o BatchMode=yes -o ServerAliveInterval=30 '
            f'-o ServerAliveCountMax=6')
