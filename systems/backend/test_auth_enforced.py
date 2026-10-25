#!/usr/bin/env python3
"""
Login is enforced when it is switched on.

    python -m pytest test_auth_enforced.py

Every other test runs with authentication off (test_photo_dates_batch's
fixture), so nothing would notice if a rename made enforcement silently stop.
That nearly happened: the flag was the config key 'SHOEBOX_AUTH_ENABLED', read
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
    assert 'SHOEBOX_AUTH_ENABLED' not in app.config


def test_no_code_reads_the_old_key():
    here = os.path.dirname(os.path.abspath(__file__))
    for name in ('auth.py', 'app.py'):
        for n, line in enumerate(open(os.path.join(here, name)), 1):
            code = line.split('#', 1)[0]
            assert 'SHOEBOX_AUTH_ENABLED' not in code, f'{name}:{n}: {line.strip()}'


# ── The session cookie's rename (v5.6.5) ─────────────────────────────────────

def test_the_new_cookie_signs_in(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    client.set_cookie(auth.COOKIE_NAME, _session(app))
    r = client.get('/api/things')
    assert r.status_code == 200
    assert not _set_cookies(r), 'nothing to migrate'


def test_the_old_cookie_still_signs_in_and_is_swapped_for_the_new(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    sid = _session(app)
    client.set_cookie('shoebox_session', sid)
    r = client.get('/api/things')
    assert r.status_code == 200
    cookies = _set_cookies(r)
    assert cookies['airlock_session'].startswith(f'airlock_session={sid};')
    assert 'HttpOnly' in cookies['airlock_session']
    assert 'Expires=Thu, 01 Jan 1970' in cookies['shoebox_session'], 'old one cleared'


def test_an_old_cookie_for_no_session_is_not_migrated(tmp_path):
    client = _app(tmp_path, enabled=True).test_client()
    client.set_cookie('shoebox_session', 'not-a-session')
    r = client.get('/api/things')
    assert r.status_code == 401
    assert not _set_cookies(r)


def test_signing_out_with_the_old_cookie_clears_both_and_sets_nothing(tmp_path):
    app = _app(tmp_path, enabled=True)
    client = app.test_client()
    client.set_cookie('shoebox_session', _session(app))
    cookies = _set_cookies(client.post('/api/logout'))
    assert set(cookies) == {'airlock_session', 'shoebox_session'}
    assert all('Expires=Thu, 01 Jan 1970' in c for c in cookies.values()), \
        'the migration must not re-set a cookie that logout is clearing'
