"""
Secrets a backup card keeps - a repository password, cloud keys -
encrypted at rest. documentation/specs/backup-backrest-spec.md section 9.

Fernet, keyed from the application's own secret key (database/secret_key) so
there is no second key file to lose. The derivation is domain-separated: the
session-signing key itself is never used as an encryption key.

A secret is decrypted only to be handed to restic, through its environment;
it is never returned to the browser or logged.
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken


def _fernet(secret_key):
    raw = secret_key.encode() if isinstance(secret_key, str) else bytes(secret_key)
    digest = hashlib.sha256(b'airlock-backup-secrets:' + raw).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(secret_key, value):
    """None and '' stay None: an unset secret is not an encrypted empty string."""
    if not value:
        return None
    return _fernet(secret_key).encrypt(value.encode()).decode()


def decrypt(secret_key, token):
    if not token:
        return None
    try:
        return _fernet(secret_key).decrypt(token.encode()).decode()
    except InvalidToken:
        # The secret key changed (a reinstall without database/secret_key):
        # the card has to be given its passwords again.
        raise ValueError('a stored password can no longer be read - enter it again')
