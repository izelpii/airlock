"""
Airlock's SSH identities for publishing deployments to servers.
documentation/specs/deployment-publish-to-server-spec.md sections 3.5, 5.2, 8.2, 8.3.

The same shape as backup_ssh.py, in a folder of its own beside it:

  database/publish-ssh/           0700, in the bind-mounted database folder, so
                                  a rebuild keeps it - never in a catalog, which
                                  is copied, packed and transferred
    <credential_id>, .pub         one ed25519 key per server, 0600
    known_hosts                   the servers' host keys, pinned

Differences from the backups, on purpose:

  * One key per SERVER, not per purpose: revoking one (its line on the server,
    or Forget server here) touches no other.
  * A host key is never learned silently. scan_host() only reads it and
    returns its fingerprint for the person to confirm; pin_host() stores it
    after scanning again and checking it is the one they saw. A changed key is
    refused until somebody re-pins it on purpose.
  * The server row keeps the fingerprint of the key it was given
    (key_fingerprint). A key folder lost and regenerated - what made backup-host
    refuse homeserver's backups - is then reported as "this machine's key changed",
    not as an unexplained refusal.

The private key never leaves this module: nothing here returns it, prints it or
logs it.
"""

import os
import re
import secrets
import subprocess

APP_DIR = os.path.dirname(os.path.abspath(__file__))
SSH_DIR = os.environ.get('PUBLISH_SSH_DIR', os.path.join(APP_DIR, 'database', 'publish-ssh'))
KNOWN_HOSTS = os.path.join(SSH_DIR, 'known_hosts')

_CREDENTIAL = re.compile(r'^[0-9a-f]{16}$')
_FINGERPRINT = re.compile(r'(SHA256:\S+)')


class PublishKeyError(Exception):
    """Something about a key or a host key, phrased for a person."""


def _dir():
    os.makedirs(SSH_DIR, exist_ok=True)
    os.chmod(SSH_DIR, 0o700)
    return SSH_DIR


def _key_path(credential_id):
    # The id names a file: anything but 16 hex digits could walk out of the folder.
    if not _CREDENTIAL.match(credential_id or ''):
        raise PublishKeyError(f'not a credential id: {credential_id!r}')
    return os.path.join(SSH_DIR, credential_id)


def _fingerprint_of(path):
    r = subprocess.run(['ssh-keygen', '-l', '-f', path], capture_output=True, text=True)
    m = _FINGERPRINT.search(r.stdout)
    return m.group(1) if m else None


# ── this machine's keys ──────────────────────────────────────────────────────

def create_key(comment):
    """
    A new key pair for one server. Returns (credential_id, public key line,
    fingerprint). The comment names it on the server's side, e.g.
    "airlock-publish family/album2026 -> photos.example.com".
    """
    _dir()
    credential_id = secrets.token_hex(8)
    path = _key_path(credential_id)
    clean = re.sub(r'[^\w./@:>+-]+', ' ', comment or 'airlock-publish').strip()[:120]
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-C', clean, '-f', path],
                   check=True, capture_output=True)
    os.chmod(path, 0o600)
    return credential_id, public_key(credential_id), key_fingerprint(credential_id)


def has_key(credential_id):
    return os.path.exists(_key_path(credential_id))


def public_key(credential_id):
    try:
        with open(_key_path(credential_id) + '.pub') as fh:
            return fh.read().strip()
    except FileNotFoundError:
        raise PublishKeyError('this server\'s key is missing on this machine - '
                              'forget the server and add it again')


def key_fingerprint(credential_id):
    return _fingerprint_of(_key_path(credential_id) + '.pub')


def key_changed(credential_id, recorded_fingerprint):
    """True when the key on disk is not the one the server was given (or is gone)."""
    if not recorded_fingerprint:
        return False
    if not has_key(credential_id):
        return True
    return key_fingerprint(credential_id) != recorded_fingerprint


def forget_key(credential_id):
    """Delete a server's key pair. The line on the server stays until removed there."""
    for path in (_key_path(credential_id), _key_path(credential_id) + '.pub'):
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def authorized_keys_line(credential_id, rrsync_dir=None):
    """
    The line to append to ~/.ssh/authorized_keys on the server.

    v1 is unrestricted: publishing also runs `docker compose` there (spec 8.3).
    With rrsync_dir it is confined to rsync inside that folder - the documented
    hardening, at the cost of every docker step needing another key or a person.
    """
    key = public_key(credential_id)
    if rrsync_dir:
        return f'restrict,command="rrsync {rrsync_dir}" {key}'
    return key


# ── the servers' host keys ───────────────────────────────────────────────────

def _host_entry(host, port):
    return host if int(port) == 22 else f'[{host}]:{int(port)}'


def _keyscan(host, port):
    r = subprocess.run(['ssh-keyscan', '-T', '8', '-t', 'ed25519', '-p', str(int(port)), host],
                       capture_output=True, text=True)
    lines = [l for l in r.stdout.splitlines() if l.strip() and not l.startswith('#')]
    if not lines:
        raise PublishKeyError(f'could not read the SSH host key of {host}:{port} - '
                              f'is the server running, and port {port} open to this machine?')
    return lines


def _fingerprint_of_lines(lines):
    _dir()
    probe = os.path.join(SSH_DIR, f'.scan-{secrets.token_hex(4)}')
    try:
        with open(probe, 'w') as fh:
            fh.write('\n'.join(lines) + '\n')
        return _fingerprint_of(probe)
    finally:
        os.remove(probe)


def scan_host(host, port=22):
    """The server's host key fingerprint, read but NOT stored - for a person to
    confirm before pin_host()."""
    return _fingerprint_of_lines(_keyscan(host, port))


def pinned_fingerprint(host, port=22):
    if not os.path.exists(KNOWN_HOSTS):
        return None
    r = subprocess.run(['ssh-keygen', '-l', '-F', _host_entry(host, port), '-f', KNOWN_HOSTS],
                       capture_output=True, text=True)
    m = _FINGERPRINT.search(r.stdout)
    return m.group(1) if m else None


def forget_host(host, port=22):
    if os.path.exists(KNOWN_HOSTS):
        subprocess.run(['ssh-keygen', '-R', _host_entry(host, port), '-f', KNOWN_HOSTS],
                       capture_output=True)
        try:
            os.remove(KNOWN_HOSTS + '.old')
        except FileNotFoundError:
            pass


def pin_host(host, port, confirmed_fingerprint):
    """
    Store the server's host key - only if it is still the one the person
    confirmed. Scans again rather than trusting an earlier answer: between the
    question and the click, the key must not change unnoticed. Replaces any
    key pinned before (this is also Re-pin). Returns the fingerprint.
    """
    lines = _keyscan(host, port)
    seen = _fingerprint_of_lines(lines)
    if not confirmed_fingerprint or seen != confirmed_fingerprint:
        raise PublishKeyError(
            f'the server\'s key is {seen}, not the {confirmed_fingerprint} that was '
            f'confirmed - nothing was stored. Check the server and try again.')
    forget_host(host, port)
    _dir()
    with open(KNOWN_HOSTS, 'a') as fh:
        fh.write('\n'.join(lines) + '\n')
    os.chmod(KNOWN_HOSTS, 0o600)
    return seen


# ── reaching a server ────────────────────────────────────────────────────────

def ssh_options(credential_id, port=22, control_path=None):
    """
    The ssh options for a publish: this server's key and nothing else, the
    pinned host key or no connection, and never a prompt. With control_path,
    one connection is shared by every step of a run (spec 6.3).
    """
    opts = ['-i', _key_path(credential_id), '-o', 'IdentitiesOnly=yes',
            '-o', f'UserKnownHostsFile={KNOWN_HOSTS}', '-o', 'StrictHostKeyChecking=yes',
            '-o', 'BatchMode=yes', '-o', 'ServerAliveInterval=30',
            '-o', 'ServerAliveCountMax=6', '-p', str(int(port))]
    if control_path:
        opts += ['-o', 'ControlMaster=auto', '-o', f'ControlPath={control_path}',
                 '-o', 'ControlPersist=60']
    return opts


def rsync_rsh(credential_id, port=22, control_path=None):
    """What rsync runs to reach the server (its -e)."""
    return ' '.join(['ssh', *ssh_options(credential_id, port, control_path)])
