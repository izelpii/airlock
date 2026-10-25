"""
airlock_license.py - who this copy is licensed to.

A license is a small JSON file signed with Ed25519. This module holds the
PUBLIC key only and checks the file against it; licenses are issued elsewhere,
by a tool that holds the private key and is not part of this repository.

It is a good-faith notice, never a lock: whatever this finds - a valid license,
none, an altered one - Airlock runs exactly the same. The answer is shown, in
the version badge's tooltip, and nothing else reads it.

Where the file goes: the repository's `license/` folder, which the backend
container mounts read-only at /license. `airlock.license` there if it exists,
otherwise the newest `*.license` - so a file keeps the name it was issued
under. AIRLOCK_LICENSE_FILE points at one file instead. It is read again when
it changes, so installing one needs no restart.

THE FORMAT (airlock-license/1) - the issuing tool mirrors it; change both or
neither:

    {"format": "airlock-license/1",
     "license": {"id": "...", "name": "...", "email": "...", "edition": "...",
                 "issued_at": "YYYY-MM-DD"},
     "signature": "<base64 Ed25519 signature of canonical(license)>"}
"""

import base64
import json
import os
import threading

FORMAT = 'airlock-license/1'

# The public half of the signing key (raw 32 bytes, base64). Safe to publish:
# it can check a license, not make one.
PUBLIC_KEY = 'tSC6uSMl7RjXz4DDGl2r0ISLQcR3Yw2HITj/ptkQSLU='

# Two levels up from this file: the repository root on a host
# (systems/backend/ -> ./license), and / in the container (/app -> /license),
# where start-podman.sh and backend-pod.yaml mount it.
LICENSE_DIR = os.path.normpath(os.path.join(
    os.path.dirname(os.path.abspath(__file__)), '..', '..', 'license'))
PREFERRED = 'airlock.license'

_cache = {'key': None, 'value': None}
_lock = threading.Lock()


def license_path():
    """The file to read: AIRLOCK_LICENSE_FILE, else airlock.license in the
    license folder, else the newest *.license there, else the (missing)
    airlock.license - which reads as unlicensed."""
    if os.environ.get('AIRLOCK_LICENSE_FILE'):
        return os.environ['AIRLOCK_LICENSE_FILE']
    folder = os.environ.get('AIRLOCK_LICENSE_DIR') or LICENSE_DIR
    preferred = os.path.join(folder, PREFERRED)
    if os.path.exists(preferred):
        return preferred
    try:
        found = [os.path.join(folder, n) for n in os.listdir(folder)
                 if n.endswith('.license') and not n.startswith('.')]
    except OSError:
        found = []
    if found:
        return max(found, key=lambda p: (os.path.getmtime(p), p))
    return preferred


def canonical(license_obj):
    """The exact bytes that are signed: sorted keys, no spaces, UTF-8."""
    return json.dumps(license_obj, sort_keys=True, separators=(',', ':'),
                      ensure_ascii=False).encode('utf-8')


def verify(doc, public_key_b64=PUBLIC_KEY):
    """The license object if `doc` is a genuine license, else ValueError."""
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    if not isinstance(doc, dict) or doc.get('format') != FORMAT:
        raise ValueError('not an Airlock license')
    lic = doc.get('license')
    if not isinstance(lic, dict):
        raise ValueError('not an Airlock license')
    try:
        key = Ed25519PublicKey.from_public_bytes(base64.b64decode(public_key_b64))
        key.verify(base64.b64decode(doc.get('signature') or ''), canonical(lic))
    except (InvalidSignature, ValueError, TypeError):
        raise ValueError('the signature does not match')
    return lic


def _read(path):
    try:
        with open(path, encoding='utf-8') as fh:
            doc = json.load(fh)
    except FileNotFoundError:
        return {'status': 'unlicensed'}
    except (OSError, ValueError):
        return {'status': 'invalid', 'reason': 'the license file cannot be read'}
    try:
        lic = verify(doc)
    except ValueError as exc:
        return {'status': 'invalid', 'reason': str(exc)}
    # Name and edition are what the badge shows. The e-mail stays in the file:
    # every signed-in user can read this, and an address is not theirs to see.
    return {'status': 'licensed', 'name': lic.get('name') or '',
            'edition': lic.get('edition') or '', 'id': lic.get('id') or '',
            'issued_at': lic.get('issued_at') or ''}


def status():
    """
    {'status': 'licensed' | 'unlicensed' | 'invalid', ...}, cached until the
    file changes - /api/version is asked on every page load.
    """
    path = license_path()
    try:
        st = os.stat(path)
        key = (path, st.st_mtime_ns, st.st_size)
    except OSError:
        key = (path, None, None)
    with _lock:
        if _cache['key'] == key:
            return dict(_cache['value'])
    value = _read(path)
    with _lock:
        _cache['key'], _cache['value'] = key, value
    return dict(value)
