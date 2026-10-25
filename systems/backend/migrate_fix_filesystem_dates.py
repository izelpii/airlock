#!/usr/bin/env python3
"""
migrate_fix_filesystem_dates.py
===============================
Recover dates for files that were filed under the day they were IMPORTED.

The bug
-------
An EXIF block whose DateTimeOriginal reads '0000:00:00 00:00:00' - common in
scans, phone exports and re-encodes - is a non-empty string. The importer's
`if exif_data.get('creation_date')` therefore treated the placeholder as a real
date and labelled the source "exif". resolve_master_datetime() then failed to
parse it and, because its documented directory and filename tiers were never
implemented, fell straight through to the filesystem: the date of the copy.

The date was never actually lost. `get_creation_date_from_organized_structure()`
had already worked it out from the source folder (…/2005/10/…) to build the
filename, so it is sitting in the 14-digit prefix of every stored name:

    20051001120000-6826a63377-20051030000000-95f8073e33df.jpg
    ^^^^^^^^^^^^^^ the date the file should have had

This reads that prefix back. It needs no source file and no exiftool, which
matters because the originals may no longer be attached.

Scope
-----
Only rows whose master_datetime_source is 'filesystem' AND whose filename prefix
disagrees with the stored date. A file legitimately dated from the filesystem -
one with no metadata and no dated folder - has a prefix that matches, so it is
left alone.

Filenames and folders are NOT changed; they already carry the right date.

EVENTS ARE NOT REBUILT. Re-run event extraction afterwards so these files land
in the right events.

Usage
-----
    python backend/migrate_fix_filesystem_dates.py --dry-run
    python backend/migrate_fix_filesystem_dates.py
    python backend/migrate_fix_filesystem_dates.py --catalog-id 67
"""

import argparse
import os
import sqlite3
import sys
from datetime import datetime

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, 'workflows'))

from catalog import Catalog  # noqa: E402
from migration_utils import resolve_catalog_dir  # noqa: E402

_MIN_YEAR = 1971
_MAX_YEAR = datetime.now().year + 2
# Years that mean "no date was set", not a date. Mirrors _is_valid_year in
# workflows/lib/utilities.py.
_BAD_YEARS = {1970, 2000}


def _prefix_datetime(file_name):
    """The 14-digit timestamp the importer put at the front of the name, or None."""
    if not file_name or len(file_name) < 14 or not file_name[:14].isdigit():
        return None
    try:
        dt = datetime.strptime(file_name[:14], '%Y%m%d%H%M%S')
    except ValueError:
        return None
    if dt.year < _MIN_YEAR or dt.year > _MAX_YEAR or dt.year in _BAD_YEARS:
        return None
    return dt


def migrate_catalog(catalog, dry_run=False, catalogs_root=None):
    summary = {'fixed': 0, 'agreed': 0, 'no_prefix': 0, 'unreachable': 0}

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
        rows = conn.execute(
            "SELECT id, file_name, master_datetime, master_datetime_source "
            "FROM file_contents WHERE master_datetime_source = 'filesystem'").fetchall()
        if not rows:
            print('    nothing dated from the filesystem')
            return summary

        for row in rows:
            dt = _prefix_datetime(row['file_name'])
            if dt is None:
                summary['no_prefix'] += 1
                continue
            iso = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
            if iso == (row['master_datetime'] or ''):
                # The prefix came from the filesystem too. Correctly dated.
                summary['agreed'] += 1
                continue
            if dry_run:
                print(f"    [dry-run] {row['file_name'][:60]}\n"
                      f"              {row['master_datetime']}  ->  {iso}")
            else:
                conn.execute(
                    "UPDATE file_contents SET master_datetime = ?, "
                    "master_datetime_source = 'directory' WHERE id = ?", (iso, row['id']))
                print(f"    [fixed] {row['file_name'][:52]}: "
                      f"{row['master_datetime']} -> {iso}")
            summary['fixed'] += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Recover dates for files filed under their import date.')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--catalogs-root')
    args = parser.parse_args()

    catalogs = ([Catalog.get_by_id(args.catalog_id)] if args.catalog_id else Catalog.get_all())
    if args.catalog_id and not catalogs[0]:
        print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
        return 1

    total = {'fixed': 0, 'agreed': 0, 'no_prefix': 0, 'unreachable': 0}
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id})")
        result = migrate_catalog(cat, dry_run=args.dry_run, catalogs_root=args.catalogs_root)
        for k in total:
            total[k] += result.get(k, 0)

    print(f"\n{'Would fix' if args.dry_run else 'Fixed'}: {total['fixed']} date(s).")
    for key, label in (('agreed', 'Correctly dated from the filesystem already'),
                       ('no_prefix', 'No usable timestamp prefix'),
                       ('unreachable', 'Catalogs not reachable from here')):
        if total[key]:
            print(f'  {label}: {total[key]}')
    if total['fixed'] and not args.dry_run:
        print('\nRe-run the event extraction workflow so these files regroup.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
