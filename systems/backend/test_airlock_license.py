"""
The license notice (airlock_license.py): a signed file says who this copy is
licensed to; anything else reads as unlicensed or invalid, and nothing stops.

    python -m pytest test_airlock_license.py
"""

import base64
import json
import os
import sys

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import airlock_license as L  # noqa: E402

LIC = {'id': 'abc-123', 'name': 'Ana Silva', 'email': 'ana@example.com',
       'edition': 'Family', 'issued_at': '2026-10-25'}


@pytest.fixture
def key(monkeypatch):
    """A throwaway signing key, installed as the public key for the test."""
    k = Ed25519PrivateKey.generate()
    pub = base64.b64encode(k.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()
    monkeypatch.setattr(L, 'PUBLIC_KEY', pub)
    monkeypatch.setattr(L.verify, '__defaults__', (pub,))
    L._cache.update(key=None, value=None)
    return k


def _write(tmp_path, monkeypatch, doc, name='airlock.license'):
    path = tmp_path / name
    path.write_text(json.dumps(doc) if not isinstance(doc, str) else doc)
    monkeypatch.setenv('AIRLOCK_LICENSE_FILE', str(path))
    return path


def _signed(k, lic=LIC):
    return {'format': L.FORMAT, 'license': dict(lic),
            'signature': base64.b64encode(k.sign(L.canonical(lic))).decode()}


def test_the_built_in_public_key_is_an_ed25519_key():
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    Ed25519PublicKey.from_public_bytes(base64.b64decode(L.PUBLIC_KEY))


def test_a_signed_license_names_its_holder_but_not_their_email(key, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, _signed(key))
    assert L.status() == {'status': 'licensed', 'name': 'Ana Silva', 'edition': 'Family',
                          'id': 'abc-123', 'issued_at': '2026-10-25'}


def test_the_signature_does_not_depend_on_key_order_or_spacing(key, tmp_path, monkeypatch):
    doc = _signed(key)
    text = json.dumps({'signature': doc['signature'], 'format': doc['format'],
                       'license': dict(reversed(list(doc['license'].items())))}, indent=4)
    _write(tmp_path, monkeypatch, text)
    assert L.status()['status'] == 'licensed'


@pytest.mark.parametrize('change', [
    lambda d: d['license'].update(edition='Commercial'),
    lambda d: d['license'].update(name='Ana Silva '),
    lambda d: d['license'].pop('email'),
    lambda d: d.update(signature=base64.b64encode(b'x' * 64).decode()),
    lambda d: d.update(signature='not base64!'),
    lambda d: d.update(format='airlock-license/2'),
])
def test_any_change_makes_it_invalid(key, tmp_path, monkeypatch, change):
    doc = _signed(key)
    change(doc)
    _write(tmp_path, monkeypatch, doc)
    assert L.status()['status'] == 'invalid'


def test_signed_with_another_key_is_invalid(key, tmp_path, monkeypatch):
    _write(tmp_path, monkeypatch, _signed(Ed25519PrivateKey.generate()))
    assert L.status() == {'status': 'invalid', 'reason': 'the signature does not match'}


def test_no_file_is_unlicensed_and_garbage_is_invalid(key, tmp_path, monkeypatch):
    monkeypatch.setenv('AIRLOCK_LICENSE_FILE', str(tmp_path / 'none.license'))
    assert L.status() == {'status': 'unlicensed'}
    _write(tmp_path, monkeypatch, '{not json')
    assert L.status()['status'] == 'invalid'


def test_installing_a_license_needs_no_restart(key, tmp_path, monkeypatch):
    path = tmp_path / 'airlock.license'
    monkeypatch.setenv('AIRLOCK_LICENSE_FILE', str(path))
    assert L.status()['status'] == 'unlicensed'
    path.write_text(json.dumps(_signed(key)))
    assert L.status()['status'] == 'licensed'
    path.write_text(json.dumps(_signed(key, {**LIC, 'edition': 'Commercial'})))
    assert L.status()['edition'] == 'Commercial'


def test_the_default_place_is_the_repository_license_folder(monkeypatch):
    monkeypatch.delenv('AIRLOCK_LICENSE_FILE', raising=False)
    monkeypatch.delenv('AIRLOCK_LICENSE_DIR', raising=False)
    assert L.LICENSE_DIR == os.path.normpath(os.path.join(HERE, '..', '..', 'license'))


def test_any_license_file_in_the_folder_is_found(key, tmp_path, monkeypatch):
    monkeypatch.delenv('AIRLOCK_LICENSE_FILE', raising=False)
    monkeypatch.setenv('AIRLOCK_LICENSE_DIR', str(tmp_path))
    assert L.status() == {'status': 'unlicensed'}             # empty folder
    issued = tmp_path / '2026-10-25-ana-silva-abc12345.license'
    issued.write_text(json.dumps(_signed(key)))
    (tmp_path / 'README.md').write_text('not a license')
    assert L.license_path() == str(issued)
    assert L.status()['name'] == 'Ana Silva'
    # airlock.license wins when it is there.
    (tmp_path / 'airlock.license').write_text(json.dumps(_signed(key, {**LIC, 'name': 'Rui'})))
    assert L.status()['name'] == 'Rui'
