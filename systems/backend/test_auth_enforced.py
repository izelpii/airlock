#!/usr/bin/env python3
"""
Login is enforced when it is switched on.

    python -m pytest test_auth_enforced.py

Every other test runs with authentication off (test_photo_dates_batch's
fixture), so nothing would notice if a rename made enforcement silently stop.
That nearly happened: the flag was once a differently named config key, read
with .get() in ten places, and renaming the setter without every reader would
have made those readers see None and let everyone in. These tests switch it on
in a bare Flask app and check each way a request is turned away.
"""

import os
import sqlite3
import sys

import pytest
from flask import Flask, jsonify

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import auth                                   # noqa: E402


class _Db:
    def __init__(self, path):
        self.path = path

    def get_connection(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn


def _app(tmp_path, enabled):
    app = Flask(__name__)
    app.secret_key = 'test'
    db = _Db(str(tmp_path / 'catalogs.db'))
    auth.install(app, db, enabled=enabled)
    app.config['TEST_DB'] = db

    @app.route('/api/things')
    def things():
        return jsonify(ok=True)

    @app.route('/api/admin-thing')
    @auth.admin_required
    def admin_thing():
        return jsonify(ok=True)

    @app.route('/api/login-thing')
    @auth.login_required
    def login_thing():
        return jsonify(ok=True)

    @app.route('/api/logout', methods=['POST'])
    def logout():
        return auth.clear_session_cookies(jsonify(ok=True))

    return app


def _session(app):
    conn = app.config['TEST_DB'].get_connection()
    conn.execute("INSERT INTO users (username, display_name, password_hash) VALUES ('ana', 'Ana', 'x')")
    sid = auth.create_session(conn, conn.execute('SELECT id FROM users').fetchone()[0])
    conn.close()
    return sid


def _set_cookies(response):
    return {h.split('=', 1)[0]: h for h in response.headers.getlist('Set-Cookie')}


@pytest.mark.parametrize('path', ['/api/things', '/api/admin-thing', '/api/login-thing'])
def test_on_turns_away_a_request_with_no_session(tmp_path, path):
    client = _app(tmp_path, enabled=True).test_client()
    assert client.get(path).status_code == 401


@pytest.mark.parametrize('path', ['/api/things', '/api/admin-thing', '/api/login-thing'])
def test_off_lets_it_through(tmp_path, path):
    client = _app(tmp_path, enabled=False).test_client()
    assert client.get(path).status_code == 200


def test_the_flag_has_one_reader(tmp_path):
    app = _app(tmp_path, enabled=True)
    assert auth.enabled_for(app) is True


# ── The session cookie ───────────────────────────────────────────────────────

def test_the_new_cookie_signs_in(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    client.set_cookie(auth.COOKIE_NAME, _session(app))
    r = client.get('/api/things')
    assert r.status_code == 200
    assert not _set_cookies(r), 'nothing to migrate'


def test_a_session_under_any_other_cookie_name_does_not_sign_in(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    client.set_cookie('other_session', _session(app))
    r = client.get('/api/things')
    assert r.status_code == 401
    assert not _set_cookies(r)


def test_signing_out_clears_the_session_cookie(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    client.set_cookie(auth.COOKIE_NAME, _session(app))
    cookies = _set_cookies(client.post('/api/logout'))
    assert set(cookies) == {'airlock_session'}
    assert 'Expires=Thu, 01 Jan 1970' in cookies['airlock_session']
