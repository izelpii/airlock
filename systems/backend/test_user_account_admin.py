"""
An account's page, by id: an administrator sets someone else's password and
sees or ends their sessions; everyone else only their own.

    python -m pytest test_user_account_admin.py

The routes behind /users/:userId (views/UserSettings.js), which replaced
Account Settings in v5.13.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth  # noqa: E402
from test_photo_dates_batch import app_module, client  # noqa: F401,E402


def _row(**kw):
    mem = sqlite3.connect(':memory:')
    mem.row_factory = sqlite3.Row
    cols = ', '.join(f'? AS {k}' for k in kw)
    return mem.execute(f'SELECT {cols}', list(kw.values())).fetchone()


@pytest.fixture
def people(app_module):  # noqa: F811
    """An administrator, and Bea with two signed-in devices."""
    conn = app_module.db.get_connection()
    conn.execute('DELETE FROM sessions')
    conn.execute("DELETE FROM users WHERE username IN ('boss', 'bea')")
    boss = conn.execute("INSERT INTO users (username, display_name, password_hash, is_admin) "
                        "VALUES ('boss', 'Boss', 'x', 1)").lastrowid
    bea = conn.execute("INSERT INTO users (username, display_name, password_hash, is_admin) "
                       "VALUES ('bea', 'Bea', ?, 0)", (auth.hash_password('old password 123'),)).lastrowid
    conn.commit()
    for _ in range(2):
        auth.create_session(conn, bea)
    conn.close()
    return {'boss': boss, 'bea': bea}


def _as(monkeypatch, app_module, user_id, admin):  # noqa: F811
    monkeypatch.setattr(app_module.auth, 'current_user',
                        lambda: _row(id=user_id, is_admin=1 if admin else 0, display_name='x'))


def _sessions(app_module, user_id):  # noqa: F811
    conn = app_module.db.get_connection()
    try:
        return conn.execute('SELECT COUNT(*) FROM sessions WHERE user_id = ?', (user_id,)).fetchone()[0]
    finally:
        conn.close()


def test_an_administrator_sets_someone_elses_password(client, app_module, people, monkeypatch):  # noqa: F811
    _as(monkeypatch, app_module, people['boss'], admin=True)
    assert client.post(f"/api/users/{people['bea']}/password", json={'new': 'short'}).status_code == 400
    r = client.post(f"/api/users/{people['bea']}/password", json={'new': 'a brand new password'})
    assert r.status_code == 200, r.get_json()
    conn = app_module.db.get_connection()
    row = conn.execute('SELECT password_hash, must_change_password FROM users WHERE id = ?',
                       (people['bea'],)).fetchone()
    conn.close()
    assert auth.verify_password(row['password_hash'], 'a brand new password')
    assert row['must_change_password'] == 1                      # theirs to choose next
    assert _sessions(app_module, people['bea']) == 0             # and signed out everywhere
    assert client.post('/api/users/999999/password', json={'new': 'a brand new password'}).status_code == 404


def test_your_own_password_needs_your_current_one(client, app_module, people, monkeypatch):  # noqa: F811
    _as(monkeypatch, app_module, people['boss'], admin=True)
    r = client.post(f"/api/users/{people['boss']}/password", json={'new': 'a brand new password'})
    assert r.status_code == 409


def test_devices_are_the_holders_or_an_administrators_to_see_and_end(client, app_module, people, monkeypatch):  # noqa: F811
    _as(monkeypatch, app_module, people['boss'], admin=True)
    assert len(client.get(f"/api/users/{people['bea']}/sessions").get_json()) == 2
    _as(monkeypatch, app_module, people['bea'], admin=False)
    assert len(client.get(f"/api/users/{people['bea']}/sessions").get_json()) == 2     # her own
    assert client.get(f"/api/users/{people['boss']}/sessions").status_code == 403
    assert client.delete(f"/api/users/{people['boss']}/sessions").status_code == 403
    _as(monkeypatch, app_module, people['boss'], admin=True)
    assert client.delete(f"/api/users/{people['bea']}/sessions").status_code == 200
    assert _sessions(app_module, people['bea']) == 0
