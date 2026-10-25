#!/usr/bin/env python3
"""
Authentication and authorization for airlock.

Implements documentation/specs/user-authentication-spec.md sections 4-6.

Three tables in the application database (catalogs.db, beside catalog_meta,
because identity spans catalogs):

    users            an account with a scrypt password hash
    catalog_access   (catalog_id, user_id, role) - ownership AND sharing
    sessions         server-side, so revocation is immediate

The session is a server-side row addressed by an opaque cookie rather than a
signed cookie or a JWT. Both of those are self-contained: once issued they stay
valid until they expire, so "log out everywhere", "disable this account" and
"revoke this share" cannot take effect immediately without a server-side
denylist - which is this table, arrived at the long way round. See spec 4.3.

Cookie, not bearer token: nine frontend views build raw <img src> URLs, and an
img tag cannot set an Authorization header. See spec 5.1.
"""

import functools
import os
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import g, jsonify, request
from werkzeug.security import check_password_hash, generate_password_hash

# ── Constants ─────────────────────────────────────────────────────────────────

COOKIE_NAME = 'airlock_session'
# Its name before the rename to airlock (v5.6.5). Still accepted, and swapped
# for the new one on the next response, so nobody is signed out by the rename.
# Remove in v5.7: anyone who has not visited since then signs in again.
LEGACY_COOKIE_NAME = 'shoebox_session'
SESSION_DAYS = 30
MIN_PASSWORD_LENGTH = 12

ROLES = ('viewer', 'editor', 'owner')
_ROLE_RANK = {role: i for i, role in enumerate(ROLES)}

# Paths reachable without a session. Everything else is denied by default, so a
# route added without a decorator fails as an unexpected 401 rather than as a
# silent hole (spec 6.1, "fail closed").
PUBLIC_PATHS = {
    '/api/auth/login',
    '/api/auth/session',
    '/api/auth/setup',
    '/api/auth/setup-required',
    '/gateway-health',
}

# A dummy hash to check against when the username does not exist, so a missing
# account and a wrong password take comparable time (spec 5.5).
_DUMMY_HASH = generate_password_hash('not-a-real-password')


# ── Secret key (spec 5.3) ─────────────────────────────────────────────────────

def load_secret_key(app_dir=None):
    """
    Return the Flask secret key, generating and persisting one on first use.

    It must be stable across restarts or every session dies on each deploy,
    which is indistinguishable from a broken login. database/ is a mounted
    volume, so a key stored there survives image rebuilds the same way
    catalogs.db does.
    """
    env = os.environ.get('AIRLOCK_SECRET_KEY')
    if env:
        return env

    if app_dir is None:
        app_dir = os.path.dirname(os.path.abspath(__file__))
    key_path = os.path.join(app_dir, 'database', 'secret_key')

    if os.path.exists(key_path):
        with open(key_path) as fh:
            value = fh.read().strip()
        if value:
            return value

    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    value = secrets.token_urlsafe(48)
    with open(key_path, 'w') as fh:
        fh.write(value)
    os.chmod(key_path, 0o600)
    return value


# ── Schema ────────────────────────────────────────────────────────────────────

def init_schema(conn):
    """Create the auth tables. Safe to call on every start."""
    cur = conn.cursor()
    cur.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id                   INTEGER PRIMARY KEY AUTOINCREMENT,
            username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
            email                TEXT UNIQUE COLLATE NOCASE,
            display_name         TEXT NOT NULL,
            password_hash        TEXT NOT NULL,
            is_active            INTEGER NOT NULL DEFAULT 1,
            is_admin             INTEGER NOT NULL DEFAULT 0,
            must_change_password INTEGER NOT NULL DEFAULT 0,
            created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login_at        TIMESTAMP
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_users_username ON users(username)')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS catalog_access (
            catalog_id  INTEGER NOT NULL,
            user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            role        TEXT NOT NULL CHECK (role IN ('owner','editor','viewer')),
            granted_by  INTEGER REFERENCES users(id),
            granted_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (catalog_id, user_id)
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_catalog_access_user ON catalog_access(user_id)')

    cur.execute('''
        CREATE TABLE IF NOT EXISTS sessions (
            id            TEXT PRIMARY KEY,
            user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
            created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_seen_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            expires_at    TIMESTAMP NOT NULL,
            user_agent    TEXT,
            ip            TEXT
        )
    ''')
    cur.execute('CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id)')

    # ── Link to a person ─────────────────────────────────────────────────────
    #
    # users lives in the application database; people lives in each catalog's
    # workflow_results.db. SQLite cannot express a foreign key across databases,
    # so this is a soft reference, and the PAIR is what identifies the person:
    # people.id is only unique WITHIN a catalog, so person_id alone becomes
    # ambiguous the moment there is a second catalog.
    #
    # Added by ALTER rather than in the CREATE above so an existing installation
    # picks this up on the next start with no separate migration step.
    existing = {r[1] for r in cur.execute('PRAGMA table_info(users)')}
    if 'person_catalog_id' not in existing:
        cur.execute('ALTER TABLE users ADD COLUMN person_catalog_id INTEGER')
    if 'person_id' not in existing:
        cur.execute('ALTER TABLE users ADD COLUMN person_id INTEGER')

    # Partial index: one user per person, while any number of users may have no
    # link. Without the WHERE clause every unlinked user would collide on NULL.
    cur.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_person "
        "ON users(person_catalog_id, person_id) WHERE person_id IS NOT NULL"
    )

    conn.commit()


# ── Passwords ─────────────────────────────────────────────────────────────────

def hash_password(password):
    return generate_password_hash(password)


def verify_password(stored_hash, password):
    return check_password_hash(stored_hash or _DUMMY_HASH, password)


def password_problem(password):
    """Return a complaint string, or None when the password is acceptable."""
    if not password or len(password) < MIN_PASSWORD_LENGTH:
        return f'Password must be at least {MIN_PASSWORD_LENGTH} characters.'
    return None


# ── Data access ───────────────────────────────────────────────────────────────

def _now():
    return datetime.now(timezone.utc)


def user_public(row):
    """The user shape returned to the browser. Never includes the hash."""
    if row is None:
        return None
    return {
        'id': row['id'],
        'username': row['username'],
        'email': row['email'],
        'display_name': row['display_name'],
        'is_admin': bool(row['is_admin']),
        'is_active': bool(row['is_active']),
        'must_change_password': bool(row['must_change_password']),
        'last_login_at': row['last_login_at'],
        'person_catalog_id': _opt(row, 'person_catalog_id'),
        'person_id': _opt(row, 'person_id'),
    }


def _opt(row, key):
    """Read a column that may not exist yet on an un-migrated row."""
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def any_users_exist(conn):
    return conn.execute('SELECT 1 FROM users LIMIT 1').fetchone() is not None


def get_user_by_username(conn, username):
    return conn.execute(
        'SELECT * FROM users WHERE username = ? COLLATE NOCASE', (username,)
    ).fetchone()


def get_user(conn, user_id):
    return conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()


def create_session(conn, user_id, user_agent=None, ip=None):
    session_id = secrets.token_urlsafe(32)
    expires = _now() + timedelta(days=SESSION_DAYS)
    conn.execute(
        'INSERT INTO sessions (id, user_id, expires_at, user_agent, ip) VALUES (?,?,?,?,?)',
        (session_id, user_id, expires.isoformat(), (user_agent or '')[:400], ip)
    )
    conn.commit()
    return session_id


# How stale sessions.last_seen_at may get before a request refreshes it. It used
# to be written on EVERY request: a gallery page fires dozens at once (one per
# thumbnail), each queued for catalogs.db's single write lock, and one that
# waited too long failed the whole request with "database is locked" - a
# thumbnail lost to a timestamp nobody reads to the second.
LAST_SEEN_REFRESH = timedelta(minutes=5)


def _parse_utc(value):
    """A stored timestamp as an aware UTC datetime, or None."""
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _best_effort_write(conn, sql, params):
    """
    A write that must never fail the request it rides on. A busy database skips
    it; the next request (or the next expiry check) does it instead.
    """
    try:
        conn.execute(sql, params)
        conn.commit()
    except sqlite3.OperationalError:
        conn.rollback()


def lookup_session(conn, session_id):
    """Return the user row for a live session, deleting it lazily when expired."""
    # u.* LAST and the session columns aliased: with "s.id, u.*" the row's 'id'
    # key resolves to the SESSION id, so every user['id'] would silently be the
    # session string instead of the user's primary key.
    row = conn.execute(
        'SELECT u.*, s.id AS session_id, s.expires_at AS session_expires_at, '
        '       s.last_seen_at AS session_last_seen_at '
        'FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.id = ?', (session_id,)
    ).fetchone()
    if row is None:
        return None

    try:
        expires = datetime.fromisoformat(row['session_expires_at'])
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        expires = _now() - timedelta(seconds=1)

    if expires <= _now() or not row['is_active']:
        _best_effort_write(conn, 'DELETE FROM sessions WHERE id = ?', (session_id,))
        return None

    last_seen = _parse_utc(row['session_last_seen_at'])
    if last_seen is None or _now() - last_seen >= LAST_SEEN_REFRESH:
        _best_effort_write(conn, 'UPDATE sessions SET last_seen_at = ? WHERE id = ?',
                           (_now().isoformat(), session_id))
    return row


def delete_session(conn, session_id):
    conn.execute('DELETE FROM sessions WHERE id = ?', (session_id,))
    conn.commit()


def delete_user_sessions(conn, user_id, keep=None):
    if keep:
        conn.execute('DELETE FROM sessions WHERE user_id = ? AND id != ?', (user_id, keep))
    else:
        conn.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
    conn.commit()


def sweep_expired_sessions(conn):
    conn.execute('DELETE FROM sessions WHERE expires_at < ?', (_now().isoformat(),))
    conn.commit()


# ── Catalog access ────────────────────────────────────────────────────────────

def role_for(conn, user_id, catalog_id):
    row = conn.execute(
        'SELECT role FROM catalog_access WHERE user_id = ? AND catalog_id = ?',
        (user_id, catalog_id)
    ).fetchone()
    return row['role'] if row else None


def accessible_catalog_ids(conn, user_id):
    rows = conn.execute(
        'SELECT catalog_id FROM catalog_access WHERE user_id = ?', (user_id,)
    ).fetchall()
    return [r['catalog_id'] for r in rows]


def role_satisfies(role, minimum):
    if role is None:
        return False
    return _ROLE_RANK[role] >= _ROLE_RANK[minimum]


def owner_count(conn, catalog_id):
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM catalog_access WHERE catalog_id = ? AND role = 'owner'",
        (catalog_id,)
    ).fetchone()
    return row['n'] if row else 0


def grant_access(conn, catalog_id, user_id, role, granted_by=None):
    conn.execute(
        'INSERT INTO catalog_access (catalog_id, user_id, role, granted_by) VALUES (?,?,?,?) '
        'ON CONFLICT(catalog_id, user_id) DO UPDATE SET role = excluded.role',
        (catalog_id, user_id, role, granted_by)
    )
    conn.commit()


# ── Login throttling (spec 5.5) ───────────────────────────────────────────────
#
# In memory on purpose: losing the counters on restart is acceptable at this
# scale and avoids a dependency. Keyed by username and by source IP so neither
# a single account nor a single client can be hammered.

_ATTEMPTS = {}
_ATTEMPTS_LOCK = threading.Lock()
_MAX_ATTEMPTS = 10
_WINDOW_SECONDS = 15 * 60
_MAX_LOCKOUT = 15 * 60


def _throttle_state(key):
    return _ATTEMPTS.setdefault(key, {'count': 0, 'first': time.time(), 'until': 0.0})


def throttle_retry_after(keys):
    """Seconds the caller must wait, or 0 when the attempt may proceed."""
    now = time.time()
    with _ATTEMPTS_LOCK:
        wait = 0
        for key in keys:
            state = _throttle_state(key)
            if state['until'] > now:
                wait = max(wait, int(state['until'] - now) + 1)
        return wait


def record_failure(keys):
    now = time.time()
    with _ATTEMPTS_LOCK:
        for key in keys:
            state = _throttle_state(key)
            if now - state['first'] > _WINDOW_SECONDS:
                state.update(count=0, first=now)
            state['count'] += 1
            if state['count'] >= _MAX_ATTEMPTS:
                over = state['count'] - _MAX_ATTEMPTS
                state['until'] = now + min(60 * (2 ** over), _MAX_LOCKOUT)


def record_success(keys):
    with _ATTEMPTS_LOCK:
        for key in keys:
            _ATTEMPTS.pop(key, None)


# ── Request plumbing ──────────────────────────────────────────────────────────

def unauthenticated(message='Authentication required'):
    return jsonify({'error': message, 'code': 'unauthenticated'}), 401


def forbidden(message='Not permitted'):
    return jsonify({'error': message, 'code': 'forbidden'}), 403


# Whether login is enforced, kept in app.config under one name and read
# through enabled_for() only. It was 'SHOEBOX_AUTH_ENABLED', read with .get()
# in eleven places: renaming the setter and missing one reader would have made
# that reader see None and skip the login check. One reader cannot drift.
_ENABLED_KEY = 'AUTH_ENABLED'


def enabled_for(app):
    """True when `app` enforces login."""
    return bool(app.config.get(_ENABLED_KEY))


def session_cookie(req):
    """The session id this request carries, under the current or the old name."""
    return req.cookies.get(COOKIE_NAME) or req.cookies.get(LEGACY_COOKIE_NAME)


def set_session_cookie(response, session_id, proto):
    """
    Attach the session cookie.

    Secure follows TLS rather than being hardcoded: a Secure cookie sent over
    plain HTTP is never returned by the browser, which presents as "login
    succeeds, then every request 401s" (spec 5.4, 10.2). `proto` comes from
    X-Forwarded-Proto, set by the gateway, and is https once nginx terminates
    TLS.
    """
    response.set_cookie(
        COOKIE_NAME, session_id,
        max_age=SESSION_DAYS * 24 * 3600,
        httponly=True,          # unreadable from JavaScript, so XSS cannot lift it
        samesite='Lax',         # blocks cross-site POSTs, the practical CSRF defence
        secure=(proto == 'https'),
        path='/',
    )
    return response


def clear_session_cookies(response):
    """Remove the session cookie under both names - signing out must not leave one."""
    response.delete_cookie(COOKIE_NAME, path='/')
    response.delete_cookie(LEGACY_COOKIE_NAME, path='/')
    return response


def install(app, db, enabled=True):
    """
    Wire authentication into *app*.

    ``enabled=False`` keeps identity available (login works, g.current_user is
    populated) while leaving every route reachable. That is spec phase 1, so the
    login flow can be exercised before enforcement is switched on.
    """
    app.config[_ENABLED_KEY] = bool(enabled)

    conn = db.get_connection()
    try:
        init_schema(conn)
    finally:
        conn.close()

    @app.before_request
    def _authenticate():
        g.current_user = None
        g.session_id = None
        g.db = db

        if request.method == 'OPTIONS':
            return None

        session_id = request.cookies.get(COOKIE_NAME)
        legacy = False
        if not session_id:
            session_id = request.cookies.get(LEGACY_COOKIE_NAME)
            legacy = bool(session_id)
        if session_id:
            c = db.get_connection()
            try:
                row = lookup_session(c, session_id)
            finally:
                c.close()
            if row is not None:
                g.current_user = row
                g.session_id = session_id
                if legacy:
                    g.migrate_session_cookie = session_id

        if not enabled_for(app):
            return None

        path = request.path.rstrip('/') or '/'
        if path in PUBLIC_PATHS or request.path in PUBLIC_PATHS:
            return None

        # Anything not under /api is the static UI, which is not a secret
        # (spec 3) - the enforcement is that its data calls return 401.
        if not request.path.startswith('/api/'):
            return None

        if g.current_user is None:
            return unauthenticated()

        return _enforce_catalog_role()

    @app.after_request
    def _migrate_session_cookie(response):
        # A valid session arrived under the old cookie name: hand it back under
        # the new one and clear the old. Not when this response is already
        # setting or clearing the session cookie - login and logout decide that
        # themselves, and re-setting it here would undo a logout.
        session_id = getattr(g, 'migrate_session_cookie', None)
        if not session_id:
            return response
        if any(h.startswith(COOKIE_NAME + '=') for h in response.headers.getlist('Set-Cookie')):
            return response
        set_session_cookie(response, session_id,
                           request.headers.get('X-Forwarded-Proto', request.scheme))
        response.delete_cookie(LEGACY_COOKIE_NAME, path='/')
        return response


# Routes where a viewer or editor is not enough. Matched against the URL rule so
# the check cannot be forgotten when a route is added (spec 6.1, fail closed).
#
# '/password' is here for album deployments: setting one decides who can see the
# catalog's photographs from OUTSIDE this application entirely, which is the same
# kind of decision as sharing the catalog. /api/auth/password carries no
# catalog_id, so it never reaches this check.
#
# '/removed-files/purge' deletes catalog rows for good - the one step of removing
# a file that cannot be undone from inside the application.
# '/servers' and '/publish-runs': publishing puts a catalog's photographs on a
# public host - the same kind of decision as a deployment's password
# (deployment-publish-to-server-spec.md section 8.1). Owner only, reads included:
# a server's address and its key line are not for every viewer.
_OWNER_RULE_FRAGMENTS = ('/access', '/password', '/removed-files/purge',
                         '/servers', '/publish-runs')


def _minimum_role_for_request():
    """
    The role this request needs.

    Reads are viewer, mutations are editor, and sharing or reshaping the catalog
    itself is owner (spec 6.2). Derived from the method and the URL rule rather
    than from a decorator on each of the 97 catalog routes: a route added without
    a decorator would otherwise be unprotected, and this way it is protected by
    default and has to be opted out of deliberately.
    """
    rule = str(request.url_rule or '')

    if any(fragment in rule for fragment in _OWNER_RULE_FRAGMENTS):
        return 'owner'

    # Backups: anyone with the catalog sees its cards; only an owner creates,
    # changes, runs or restores one (backup-backrest-spec.md section 5.1).
    if '/backups' in rule and request.method not in ('GET', 'HEAD', 'OPTIONS'):
        return 'owner'

    # PUT/DELETE directly on /api/catalogs/<id> reshapes or removes the catalog.
    if rule.rstrip('/').endswith('/api/catalogs/<int:catalog_id>') \
            and request.method in ('PUT', 'DELETE'):
        return 'owner'

    if request.method in ('GET', 'HEAD', 'OPTIONS'):
        return 'viewer'
    return 'editor'


def _enforce_catalog_role():
    catalog_id = (request.view_args or {}).get('catalog_id')
    if catalog_id is None:
        return None

    minimum = _minimum_role_for_request()
    c = g.db.get_connection()
    try:
        role = role_for(c, g.current_user['id'], catalog_id)
    finally:
        c.close()

    if not role_satisfies(role, minimum):
        # Identical response whether the catalog does not exist or is merely
        # someone else's, so the API cannot be used to enumerate catalogs.
        return forbidden(f'{minimum} access required for this catalog')

    g.catalog_role = role
    return None


def current_user():
    return getattr(g, 'current_user', None)


def login_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from flask import current_app
        if enabled_for(current_app) and current_user() is None:
            return unauthenticated()
        return fn(*args, **kwargs)
    return wrapper


def admin_required(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        from flask import current_app
        if not enabled_for(current_app):
            return fn(*args, **kwargs)
        user = current_user()
        if user is None:
            return unauthenticated()
        if not user['is_admin']:
            return forbidden('Administrator access required')
        return fn(*args, **kwargs)
    return wrapper


def catalog_access(minimum='viewer'):
    """
    Require *minimum* role on the catalog named by the view's catalog_id.

    Applied to the 97 routes that already carry catalog_id, which is what makes
    authorization a single choke point rather than 97 decisions (spec 6.1).
    """
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            from flask import current_app
            if not enabled_for(current_app):
                return fn(*args, **kwargs)

            user = current_user()
            if user is None:
                return unauthenticated()

            catalog_id = kwargs.get('catalog_id')
            if catalog_id is None:
                return fn(*args, **kwargs)

            c = g.db.get_connection()
            try:
                role = role_for(c, user['id'], catalog_id)
            finally:
                c.close()

            if not role_satisfies(role, minimum):
                # Same response whether the catalog is missing or merely
                # inaccessible, so the API does not enumerate other people's
                # catalogs.
                return forbidden(f'{minimum} access required for this catalog')

            g.catalog_role = role
            return fn(*args, **kwargs)
        return wrapper
    return decorator
