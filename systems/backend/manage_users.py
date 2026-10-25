#!/usr/bin/env python3
"""
manage_users.py
===============
Command-line account administration, including the way back in when you are
locked out of the UI.

documentation/specs/user-authentication-spec.md section 12 names lockout as the
largest risk of the whole change, and it is operational rather than technical:
the migration mints a password, shows it once, and if that output is lost the
only recovery is here. This ships with the migration, not after it.

Usage
-----
    python backend/manage_users.py list
    python backend/manage_users.py create <username> --display "Full Name" [--email ..] [--admin]
    python backend/manage_users.py reset-password <username> [--password ...]
    python backend/manage_users.py rename <username> <new-username>
    python backend/manage_users.py set-admin <username> --on|--off
    python backend/manage_users.py activate <username> --on|--off
    python backend/manage_users.py sessions <username> [--revoke]
    python backend/manage_users.py access <catalog_id> [--grant <username> --role owner|editor|viewer]
                                                       [--revoke <username>]

Passwords given with --password appear in your shell history; omit it and one is
generated and printed instead.
"""

import argparse
import os
import secrets
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)

import auth  # noqa: E402


def connect():
    path = os.path.join(_HERE, 'database', 'catalogs.db')
    if not os.path.exists(path):
        print(f'Application database not found: {path}', file=sys.stderr)
        sys.exit(1)
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    auth.init_schema(conn)
    return conn


def need_user(conn, username):
    row = auth.get_user_by_username(conn, username)
    if row is None:
        print(f'No such user: {username}', file=sys.stderr)
        sys.exit(1)
    return row


def cmd_list(conn, args):
    rows = conn.execute('SELECT * FROM users ORDER BY id').fetchall()
    if not rows:
        print('No users. Run migrate_add_users.py first.')
        return
    print(f'{"id":>3}  {"username":<20} {"display name":<24} {"admin":<6} {"active":<7} catalogs')
    for r in rows:
        cats = conn.execute(
            'SELECT catalog_id, role FROM catalog_access WHERE user_id = ? ORDER BY catalog_id',
            (r['id'],)).fetchall()
        summary = ', '.join(f'{c["catalog_id"]}:{c["role"]}' for c in cats) or '-'
        print(f'{r["id"]:>3}  {r["username"]:<20} {r["display_name"][:24]:<24} '
              f'{"yes" if r["is_admin"] else "no":<6} '
              f'{"yes" if r["is_active"] else "NO":<7} {summary}')


def cmd_create(conn, args):
    if auth.get_user_by_username(conn, args.username):
        print(f'User already exists: {args.username}', file=sys.stderr)
        sys.exit(1)
    password, generated = resolve_password(args)
    conn.execute(
        'INSERT INTO users (username, email, display_name, password_hash, is_admin, '
        '                   must_change_password) VALUES (?,?,?,?,?,?)',
        (args.username, args.email, args.display or args.username,
         auth.hash_password(password), 1 if args.admin else 0, 1 if generated else 0))
    conn.commit()
    print(f'Created {args.username}')
    announce(password, generated)


def cmd_reset_password(conn, args):
    user = need_user(conn, args.username)
    password, generated = resolve_password(args)
    conn.execute(
        'UPDATE users SET password_hash = ?, must_change_password = ?, '
        '                 updated_at = CURRENT_TIMESTAMP WHERE id = ?',
        (auth.hash_password(password), 1 if generated else 0, user['id']))
    # Every existing session for this account dies: a password reset that leaves
    # old sessions alive does not lock anyone out.
    auth.delete_user_sessions(conn, user['id'])
    conn.commit()
    print(f'Password reset for {user["username"]}; all their sessions were signed out.')
    announce(password, generated)


def cmd_rename(conn, args):
    """
    Change a username.

    Sessions and catalog access are keyed by user id, not by name, so a rename
    does not sign anyone out or disturb who can reach which catalog.
    """
    user = need_user(conn, args.username)
    new_name = args.new_username.strip()
    if not new_name:
        print('New username cannot be empty.', file=sys.stderr)
        sys.exit(1)

    # UNIQUE is COLLATE NOCASE, so "Izelpi" would collide with "izelpi".
    clash = auth.get_user_by_username(conn, new_name)
    if clash and clash['id'] != user['id']:
        print(f'That username is already taken: {new_name}', file=sys.stderr)
        sys.exit(1)

    conn.execute('UPDATE users SET username = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                 (new_name, user['id']))
    conn.commit()
    print(f'{user["username"]} -> {new_name}')
    print('  Sessions and catalog access are unaffected (both key on user id).')


def cmd_set_admin(conn, args):
    user = need_user(conn, args.username)
    value = 1 if args.on else 0
    if not value:
        others = conn.execute(
            'SELECT COUNT(*) AS n FROM users WHERE is_admin = 1 AND id != ?',
            (user['id'],)).fetchone()['n']
        if others == 0:
            print('Refusing: that is the only administrator.', file=sys.stderr)
            sys.exit(1)
    conn.execute('UPDATE users SET is_admin = ? WHERE id = ?', (value, user['id']))
    conn.commit()
    print(f'{user["username"]} admin = {bool(value)}')


def cmd_activate(conn, args):
    user = need_user(conn, args.username)
    value = 1 if args.on else 0
    conn.execute('UPDATE users SET is_active = ? WHERE id = ?', (value, user['id']))
    if not value:
        auth.delete_user_sessions(conn, user['id'])
    conn.commit()
    print(f'{user["username"]} active = {bool(value)}')


def cmd_sessions(conn, args):
    user = need_user(conn, args.username)
    if args.revoke:
        auth.delete_user_sessions(conn, user['id'])
        print(f'Signed out all sessions for {user["username"]}.')
        return
    rows = conn.execute(
        'SELECT id, created_at, last_seen_at, expires_at, ip, user_agent '
        'FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC', (user['id'],)).fetchall()
    if not rows:
        print('No active sessions.')
        return
    for r in rows:
        print(f'  {r["id"][:12]}…  last seen {r["last_seen_at"]}  '
              f'ip {r["ip"] or "-"}  {(r["user_agent"] or "")[:50]}')


def cmd_access(conn, args):
    catalog_id = args.catalog_id
    if args.grant:
        user = need_user(conn, args.grant)
        if args.role not in auth.ROLES:
            print(f'Role must be one of {", ".join(auth.ROLES)}', file=sys.stderr)
            sys.exit(1)
        auth.grant_access(conn, catalog_id, user['id'], args.role)
        print(f'{user["username"]} -> catalog {catalog_id} as {args.role}')
    elif args.revoke:
        user = need_user(conn, args.revoke)
        role = auth.role_for(conn, user['id'], catalog_id)
        if role == 'owner' and auth.owner_count(conn, catalog_id) <= 1:
            print('Refusing: that is the last owner of this catalog.', file=sys.stderr)
            sys.exit(1)
        conn.execute('DELETE FROM catalog_access WHERE catalog_id = ? AND user_id = ?',
                     (catalog_id, user['id']))
        conn.commit()
        print(f'Revoked {user["username"]} from catalog {catalog_id}')

    rows = conn.execute(
        'SELECT u.username, u.display_name, a.role FROM catalog_access a '
        'JOIN users u ON u.id = a.user_id WHERE a.catalog_id = ? ORDER BY a.role, u.username',
        (catalog_id,)).fetchall()
    print(f'\nAccess to catalog {catalog_id}:')
    for r in rows:
        print(f'  {r["role"]:<7} {r["username"]} ({r["display_name"]})')
    if not rows:
        print('  (nobody — this catalog is unreachable in the UI)')


def resolve_password(args):
    """Return (password, generated) — generated means it must be changed at login."""
    if getattr(args, 'password', None):
        problem = auth.password_problem(args.password)
        if problem:
            print(f'  {problem}', file=sys.stderr)
            sys.exit(1)
        return args.password, False
    return secrets.token_urlsafe(12), True


def announce(password, generated):
    """Print a generated password once. Nothing is printed for a chosen one."""
    if generated:
        print('  ' + '-' * 54)
        print(f'  password: {password}')
        print('  Shown once. A change is required at first login.')
        print('  ' + '-' * 54)


def main():
    parser = argparse.ArgumentParser(description='airlock account administration.')
    sub = parser.add_subparsers(dest='command', required=True)

    sub.add_parser('list')

    p = sub.add_parser('create')
    p.add_argument('username')
    p.add_argument('--display')
    p.add_argument('--email')
    p.add_argument('--password')
    p.add_argument('--admin', action='store_true')

    p = sub.add_parser('reset-password')
    p.add_argument('username')
    p.add_argument('--password')

    p = sub.add_parser('rename')
    p.add_argument('username')
    p.add_argument('new_username')

    p = sub.add_parser('set-admin')
    p.add_argument('username')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--on', action='store_true')
    g.add_argument('--off', action='store_true')

    p = sub.add_parser('activate')
    p.add_argument('username')
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument('--on', action='store_true')
    g.add_argument('--off', action='store_true')

    p = sub.add_parser('sessions')
    p.add_argument('username')
    p.add_argument('--revoke', action='store_true')

    p = sub.add_parser('access')
    p.add_argument('catalog_id', type=int)
    p.add_argument('--grant')
    p.add_argument('--role', default='viewer')
    p.add_argument('--revoke')

    args = parser.parse_args()
    conn = connect()
    try:
        {
            'list': cmd_list,
            'create': cmd_create,
            'reset-password': cmd_reset_password,
            'rename': cmd_rename,
            'set-admin': cmd_set_admin,
            'activate': cmd_activate,
            'sessions': cmd_sessions,
            'access': cmd_access,
        }[args.command](conn, args)
    finally:
        conn.close()
    return 0


if __name__ == '__main__':
    sys.exit(main())
