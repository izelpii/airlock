#!/usr/bin/env python3
"""
migrate_purge_ignored_files.py
==============================
Remove rows for files that should never have been imported.

The importer now skips sidecars and operating-system files
(media_types.IGNORED_*), but catalogs built before that still hold rows for
them: .AAE edit recipes, .DS_Store, Thumbs.db, AppleDouble forks. They have no
date and nothing to display, so they pad counts, show as blanks in galleries,
and can be picked as an event cover.

What it removes, per file: the file_contents row, its exif_data, its
alternative_versions rows and the derivative files those point at, its
event_files links, its section_photos links, and its face_detections. Anything
whose absence would leave a dangling reference.

Refuses to touch a file that is currently an EVENT COVER or is in an ALBUM
unless --force, and says which. Neither should happen for a .DS_Store, and if it
has, that is worth seeing rather than silently undoing.

The originals on disk are NOT deleted. This is a catalog cleanup, not a file
manager: a .AAE sitting beside its photo is the user's file.

Usage
-----
    python backend/migrate_purge_ignored_files.py --dry-run
    python backend/migrate_purge_ignored_files.py
    python backend/migrate_purge_ignored_files.py --catalog-id 67
"""

import argparse
import os
import re
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from catalog import Catalog  # noqa: E402
from media_types import is_ignored_file, ignore_reason  # noqa: E402
from migration_utils import resolve_catalog_dir  # noqa: E402


_STORED_PREFIX = re.compile(r'^\d{14}-[0-9a-f]{10}-')


def _original_name(stored_name):
    """
    Strip the importer's 'YYYYMMDDHHMMSS-<hash10>-' prefix.

    The stored name is a decorated copy of the original, and the decoration
    defeats the deny list: '20260904025417-d3cc802c3e-.DS_Store' is not called
    '.ds_store' and does not start with a dot, so is_ignored_file() says no. The
    live importer tests the SOURCE path, which has no prefix; only this cleanup
    sees the decorated form.
    """
    return _STORED_PREFIX.sub('', stored_name or '')


def _is_ignored_stored(stored_name):
    """True when either the stored or the undecorated name is on the deny list."""
    return is_ignored_file(stored_name) or is_ignored_file(_original_name(stored_name))


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)).fetchone()


def _in_use(conn, file_id):
    """Reasons this row should not be deleted silently."""
    reasons = []
    row = conn.execute('SELECT COUNT(*) FROM events WHERE cover_file_id = ?',
                       (file_id,)).fetchone()
    if row and row[0]:
        reasons.append(f'is the cover of {row[0]} event(s)')
    if _table_exists(conn, 'section_photos'):
        row = conn.execute('SELECT COUNT(*) FROM section_photos WHERE file_id = ?',
                           (file_id,)).fetchone()
        if row and row[0]:
            reasons.append(f'is in {row[0]} album section(s)')
    return reasons


def migrate_catalog(catalog, dry_run=False, catalogs_root=None, force=False):
    summary = {'purged': 0, 'in_use': 0, 'files_removed': 0, 'unreachable': 0}

    target_dir = resolve_catalog_dir(catalog, catalogs_root) if catalog.target_directory else None
    db_path = os.path.join(target_dir, 'workflow_results.db') if target_dir else None
    if not db_path or not os.path.exists(db_path):
        print(f'  [skip] {catalog.target_directory} not reachable from here.')
        summary['unreachable'] = 1
        return summary

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    conn.row_factory = sqlite3.Row
    try:
        victims = [r for r in conn.execute('SELECT id, file_name, file_path FROM file_contents')
                   if _is_ignored_stored(r['file_name'] or '')]
        if not victims:
            print('    nothing to purge')
            return summary

        for row in victims:
            blockers = [] if force else _in_use(conn, row['id'])
            if blockers:
                print(f"    [kept] {row['file_name']}: {'; '.join(blockers)}")
                summary['in_use'] += 1
                continue

            derivatives = [r['alternative_file_path'] for r in conn.execute(
                'SELECT alternative_file_path FROM alternative_versions '
                'WHERE original_file_id = ?', (row['id'],)).fetchall()]

            if dry_run:
                print(f"    [dry-run] would purge {row['file_name']} "
                      f"({ignore_reason(_original_name(row['file_name']))})"
                      + (f' + {len(derivatives)} derivative file(s)' if derivatives else ''))
            else:
                for table, column in (('exif_data', 'file_id'),
                                      ('alternative_versions', 'original_file_id'),
                                      ('event_files', 'file_id'),
                                      ('section_photos', 'file_id'),
                                      ('face_detections', 'file_id'),
                                      ('file_operations', 'file_id')):
                    if _table_exists(conn, table):
                        try:
                            conn.execute(f'DELETE FROM {table} WHERE {column} = ?', (row['id'],))
                        except sqlite3.OperationalError:
                            pass  # column absent in an older schema
                conn.execute('DELETE FROM file_contents WHERE id = ?', (row['id'],))

                # Only the DERIVATIVES this catalog generated. The original is
                # the user's file and is left where it is.
                for rel in derivatives:
                    if not rel:
                        continue
                    path = rel if os.path.isabs(rel) else os.path.join(target_dir, rel)
                    try:
                        if os.path.isfile(path):
                            os.unlink(path)
                            summary['files_removed'] += 1
                    except OSError as exc:
                        print(f'      could not remove {path}: {exc}')
                print(f"    [purged] {row['file_name']} "
                      f"({ignore_reason(_original_name(row['file_name']))})")
            summary['purged'] += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Remove catalog rows for sidecars and operating-system files.')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true',
                        help='Purge even if the file is an event cover or in an album.')
    parser.add_argument('--catalogs-root')
    args = parser.parse_args()

    catalogs = ([Catalog.get_by_id(args.catalog_id)] if args.catalog_id else Catalog.get_all())
    if args.catalog_id and not catalogs[0]:
        print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
        return 1

    total = {'purged': 0, 'in_use': 0, 'files_removed': 0, 'unreachable': 0}
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id})")
        result = migrate_catalog(cat, dry_run=args.dry_run,
                                 catalogs_root=args.catalogs_root, force=args.force)
        for k in total:
            total[k] += result.get(k, 0)

    print(f"\n{'Would purge' if args.dry_run else 'Purged'}: {total['purged']} row(s).")
    if total['files_removed']:
        print(f"  Derivative files removed: {total['files_removed']}")
    if total['in_use']:
        print(f"  Kept because they are in use: {total['in_use']} (see above; --force overrides)")
    if total['unreachable']:
        print(f"  Catalogs not reachable from here: {total['unreachable']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
