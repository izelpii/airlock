#!/usr/bin/env python3
"""
migrate_add_users.py
====================
Create the authentication tables and migrate the existing implicit user into a
real account.

Implements documentation/specs/user-authentication-spec.md section 4.4.

Before this migration airlock had one user in all but name: `user_settings`
holds a single row, and `catalog_meta.owner` is a TEXT column carrying that same
person's display name. This turns that into:

  users            one account, admin, from the user_settings row
  catalog_access   one 'owner' row per catalog, matched by display name
  sessions         empty

`catalog_meta.owner` is deliberately LEFT IN PLACE. It becomes a display
fallback, and keeping it means this migration can be reversed by dropping three
tables. `user_settings` also stays - it holds the home lat/long that geolocation
uses - and gains a user_id column.

Usage
-----
    python backend/migrate_add_users.py                      # generate a password
    python backend/migrate_add_users.py --password '...'     # choose one
    python backend/migrate_add_users.py --username izelpi     # override the derived name
    python backend/migrate_add_users.py --dry-run

Idempotent: running it twice creates nothing the second time and says so.
"""

import argparse
import os
import secrets
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

import auth  # noqa: E402


def app_db_path():
    return os.path.join(_HERE, 'database', 'catalogs.db')


def derive_username(email, display_name):
    """A sane default: the email local part, else the display name squashed."""
    if email and '@' in email:
        return email.split('@', 1)[0].strip().lower()
    return (display_name or 'admin').strip().lower().replace(' ', '.')


def main():
    parser = argparse.ArgumentParser(
        description='Create auth tables and migrate the existing user into an account.')
    parser.add_argument('--password', help='Password for the migrated account.')
    parser.add_argument('--username', help='Override the derived username.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report what would change without writing.')
    args = parser.parse_args()

    db_path = app_db_path()
    if not os.path.exists(db_path):
        print(f'Application database not found: {db_path}', file=sys.stderr)
        return 1

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row

    print(f'Database: {db_path}')

    # ── 1. Tables ────────────────────────────────────────────────────────────
    if args.dry_run:
        print('  [dry-run] would create tables: users, catalog_access, sessions')
    else:
        auth.init_schema(conn)
        print('  Tables ready: users, catalog_access, sessions')

    existing = conn.execute('SELECT COUNT(*) AS n FROM users').fetchone()['n'] \
        if not args.dry_run else 0

    # ── 2. The account ───────────────────────────────────────────────────────
    generated = None
    if existing:
        print(f'  {existing} user(s) already exist; not creating another.')
        admin = conn.execute(
            'SELECT * FROM users WHERE is_admin = 1 ORDER BY id LIMIT 1').fetchone()
    else:
        settings = conn.execute(
            'SELECT * FROM user_settings ORDER BY id LIMIT 1').fetchone()
        if settings:
            display_name = settings['name'] or 'Administrator'
            email = settings['email']
        else:
            display_name, email = 'Administrator', None
            print('  No user_settings row; creating a generic administrator.')

        username = args.username or derive_username(email, display_name)

        password = args.password
        if password:
            problem = auth.password_problem(password)
            if problem:
                print(f'  {problem}', file=sys.stderr)
                return 1
            must_change = 0
        else:
            # No plaintext exists to migrate, so mint one and force a change.
            generated = secrets.token_urlsafe(12)
            password = generated
            must_change = 1

        if args.dry_run:
            print(f'  [dry-run] would create admin "{username}" '
                  f'({display_name} <{email}>)')
            admin = None
        else:
            cur = conn.execute(
                'INSERT INTO users (username, email, display_name, password_hash, '
                '                   is_admin, must_change_password) '
                'VALUES (?,?,?,?,1,?)',
                (username, email, display_name, auth.hash_password(password), must_change))
            conn.commit()
            admin = conn.execute('SELECT * FROM users WHERE id = ?',
                                 (cur.lastrowid,)).fetchone()
            print(f'  Created admin "{username}" ({display_name})')

    # ── 3. Catalog ownership ─────────────────────────────────────────────────
    catalogs = conn.execute(
        'SELECT id, name, owner FROM catalog_meta ORDER BY id').fetchall()
    granted = skipped = unmatched = 0

    for cat in catalogs:
        if not args.dry_run:
            already = conn.execute(
                'SELECT role FROM catalog_access WHERE catalog_id = ?',
                (cat['id'],)).fetchone()
            if already:
                print(f'    catalog {cat["id"]} "{cat["name"]}": access already set')
                skipped += 1
                continue

        owner_name = (cat['owner'] or '').strip()

        if args.dry_run:
            # The account does not exist yet in a dry run, so a match lookup here
            # would report "no matching user" for an owner that will in fact match.
            print(f'    [dry-run] catalog {cat["id"]} "{cat["name"]}" '
                  f'-> owner (from "{owner_name}")')
            granted += 1
            continue

        match = conn.execute(
            'SELECT * FROM users WHERE display_name = ? COLLATE NOCASE',
            (owner_name,)).fetchone() if owner_name else None

        target = match or admin
        if match is None and owner_name:
            # Never drop an owner silently - that is how a catalog becomes
            # unreachable with no message saying why.
            print(f'    catalog {cat["id"]} "{cat["name"]}": owner "{owner_name}" '
                  f'matched no user; assigning to the administrator')
            unmatched += 1

        if target is None:
            print(f'    catalog {cat["id"]}: no user to assign; skipped')
            continue

        auth.grant_access(conn, cat['id'], target['id'], 'owner', granted_by=target['id'])
        print(f'    catalog {cat["id"]} "{cat["name"]}" -> {target["username"]} (owner)')
        granted += 1

    # ── 4. user_settings gains an owner ──────────────────────────────────────
    if not args.dry_run:
        cols = [r[1] for r in conn.execute('PRAGMA table_info(user_settings)')]
        if 'user_id' not in cols:
            conn.execute('ALTER TABLE user_settings ADD COLUMN user_id INTEGER')
            print('  user_settings: added user_id')
        if admin is not None:
            conn.execute('UPDATE user_settings SET user_id = ? WHERE user_id IS NULL',
                         (admin['id'],))
        conn.commit()

    conn.close()

    # ── Summary ──────────────────────────────────────────────────────────────
    verb = 'Would grant' if args.dry_run else 'Granted'
    print(f'\n{verb} owner access on {granted} catalog(s).')
    if skipped:
        print(f'  Already had access rows: {skipped}')
    if unmatched:
        print(f'  Owner strings with no matching user: {unmatched} (assigned to admin)')

    if generated:
        print('\n' + '=' * 62)
        print('  This password is shown ONCE. Write it down now.')
        print(f'    password: {generated}')
        print('  You will be asked to change it at first login.')
        print('  Lost it?  python backend/manage_users.py reset-password <username>')
        print('=' * 62)

    return 0


if __name__ == '__main__':
    sys.exit(main())
