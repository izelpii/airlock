"""
Publishing's SSH identities: one key per server, host keys pinned only after a
person confirmed them (deployment-publish-to-server-spec.md 3.5, 5.2, 8.2, 8.3).

    python -m pytest test_publish_ssh.py
"""

import os
import subprocess
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import publish_ssh as ps  # noqa: E402


@pytest.fixture(autouse=True)
def store(tmp_path, monkeypatch):
    d = str(tmp_path / 'publish-ssh')
    monkeypatch.setattr(ps, 'SSH_DIR', d)
    monkeypatch.setattr(ps, 'KNOWN_HOSTS', os.path.join(d, 'known_hosts'))
    return d


def _host_key(tmp_path, name):
    """A server's host key, as ssh-keyscan would print it."""
    path = str(tmp_path / name)
    subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', path], check=True)
    kind, key = open(path + '.pub').read().split()[:2]
    return kind, key


def _scanner(monkeypatch, host_key):
    kind, key = host_key
    monkeypatch.setattr(ps, '_keyscan',
                        lambda host, port: [f'{ps._host_entry(host, port)} {kind} {key}'])


# ── this machine's keys ──────────────────────────────────────────────────────

def test_a_key_per_server_private_and_never_returned(store):
    cred, line, fp = ps.create_key('airlock-publish family/album2026 -> photos.example.com')
    assert len(cred) == 16 and fp.startswith('SHA256:')
    assert line.startswith('ssh-ed25519 ') and line.endswith('-> photos.example.com')
    assert oct(os.stat(store).st_mode & 0o777) == '0o700'
    assert oct(os.stat(os.path.join(store, cred)).st_mode & 0o777) == '0o600'
    assert 'PRIVATE' not in line
    other, _, fp2 = ps.create_key('another server')
    assert other != cred and fp2 != fp


def test_the_authorized_keys_line_plain_or_confined(store):
    cred, key, _ = ps.create_key('x')
    assert ps.authorized_keys_line(cred) == key
    assert ps.authorized_keys_line(cred, rrsync_dir='galleries/album2026') == \
        f'restrict,command="rrsync galleries/album2026" {key}'


def test_a_regenerated_key_is_reported_as_changed(store):
    cred, _, fp = ps.create_key('x')
    assert not ps.key_changed(cred, fp)
    ps.forget_key(cred)
    assert not ps.has_key(cred) and ps.key_changed(cred, fp)
    with pytest.raises(ps.PublishKeyError, match='missing'):
        ps.public_key(cred)


def test_a_credential_id_cannot_name_another_file():
    for bad in ('../../etc/passwd', 'abc', '', None, 'ABCDEF0123456789'):
        with pytest.raises(ps.PublishKeyError):
            ps._key_path(bad)


# ── host keys ────────────────────────────────────────────────────────────────

def test_scanning_reads_the_fingerprint_without_storing_it(tmp_path, monkeypatch):
    _scanner(monkeypatch, _host_key(tmp_path, 'server_a'))
    fp = ps.scan_host('photos.example.com')
    assert fp.startswith('SHA256:')
    assert ps.pinned_fingerprint('photos.example.com') is None


def test_pinning_stores_only_the_confirmed_key(tmp_path, monkeypatch):
    _scanner(monkeypatch, _host_key(tmp_path, 'server_a'))
    seen = ps.scan_host('photos.example.com')
    with pytest.raises(ps.PublishKeyError, match='nothing was stored'):
        ps.pin_host('photos.example.com', 22, 'SHA256:somethingelse')
    assert ps.pinned_fingerprint('photos.example.com') is None
    assert ps.pin_host('photos.example.com', 22, seen) == seen
    assert ps.pinned_fingerprint('photos.example.com') == seen


def test_a_changed_server_is_refused_until_repinned(tmp_path, monkeypatch):
    _scanner(monkeypatch, _host_key(tmp_path, 'server_a'))
    old = ps.pin_host('photos.example.com', 22, ps.scan_host('photos.example.com'))
    _scanner(monkeypatch, _host_key(tmp_path, 'server_b'))          # replaced server
    new = ps.scan_host('photos.example.com')
    assert new != old
    assert ps.pinned_fingerprint('photos.example.com') == old      # still the old one
    with pytest.raises(ps.PublishKeyError):
        ps.pin_host('photos.example.com', 22, old)                 # the old confirmation does not carry over
    assert ps.pin_host('photos.example.com', 22, new) == new       # Re-pin, on purpose
    assert ps.pinned_fingerprint('photos.example.com') == new
    assert open(ps.KNOWN_HOSTS).read().count('photos.example.com') == 1


def test_a_port_other_than_22_is_its_own_entry(tmp_path, monkeypatch):
    _scanner(monkeypatch, _host_key(tmp_path, 'server_a'))
    fp = ps.pin_host('203.0.113.10', 2222, ps.scan_host('203.0.113.10', 2222))
    assert ps.pinned_fingerprint('203.0.113.10', 2222) == fp
    assert ps.pinned_fingerprint('203.0.113.10', 22) is None


# ── reaching a server ────────────────────────────────────────────────────────

def test_ssh_uses_this_servers_key_and_the_pinned_host_only(store):
    cred, _, _ = ps.create_key('x')
    opts = ps.ssh_options(cred, 22, control_path='/tmp/c')
    assert opts[opts.index('-i') + 1] == os.path.join(store, cred)
    for need in ('IdentitiesOnly=yes', 'StrictHostKeyChecking=yes', 'BatchMode=yes',
                 f'UserKnownHostsFile={ps.KNOWN_HOSTS}', 'ControlPath=/tmp/c'):
        assert need in opts
    rsh = ps.rsync_rsh(cred, 2222)
    assert rsh.startswith('ssh -i ') and '-p 2222' in rsh and 'ControlPath' not in rsh
