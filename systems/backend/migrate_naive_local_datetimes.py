#!/usr/bin/env python3
"""
migrate_naive_local_datetimes.py
================================
Stop claiming UTC for photo times whose zone was never known.

resolve_master_datetime() used to append "Z" to every time it could not find an
offset for - a still camera that records none (a Canon EOS 6D), and a date read
from a folder or file name. "Z" declares UTC, so the frontend converted those
times to the viewer's zone and showed them shifted: a 6D photo taken at 20:46 in
New York displayed as 16:46. They are local times as recorded, and are now
stored without a zone ("2026-09-25T20:46:10"), which every reader shows as-is.

What it changes: master_datetime ending in "Z" where master_datetime_source is
  'exif'                   and the file is NOT a video (QuickTime metadata is
                           UTC by specification, so a video's "Z" is right);
  'directory', 'filename'  for any file.
Everything else - manual date edits, filesystem and system times, any time with
a real offset - is left alone. Only the trailing "Z" is removed; the clock time,
file names and folders are unchanged, so nothing on disk moves.

Usage
-----
    python backend/migrate_naive_local_datetimes.py --dry-run
    python backend/migrate_naive_local_datetimes.py --catalog-id 7
    python backend/migrate_naive_local_datetimes.py --catalogs-root /mnt/catalogs

Idempotent: a second run finds nothing ending in "Z" to change.
"""

import argparse
import os
import sqlite3
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)

from catalog import Catalog  # noqa: E402
from media_types import is_video_file  # noqa: E402
from migration_utils import resolve_catalog_dir  # noqa: E402


def migrate_catalog(catalog, dry_run=False, catalogs_root=None):
    target_dir = resolve_catalog_dir(catalog, catalogs_root) if catalog.target_directory else None
    if not target_dir:
        print(f"  [skip] {catalog.target_directory} not reachable from here.")
        return 0
    db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(db_path):
        print(f"  [skip] No workflow_results.db at {db_path}.")
        return 0

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, file_name, master_datetime, master_datetime_source FROM file_contents "
        "WHERE master_datetime LIKE '%Z' "
        "AND master_datetime_source IN ('exif', 'directory', 'filename')").fetchall()
    fix = [r for r in rows
           if r['master_datetime_source'] != 'exif' or not is_video_file(r['file_name'] or '')]
    by_source = Counter(r['master_datetime_source'] for r in fix)
    kept_videos = len(rows) - len(fix)
    print(f"  {len(fix)} time(s) to store without a zone {dict(by_source)}; "
          f"{kept_videos} video time(s) left as UTC.")
    for r in fix[:5]:
        print(f"    {r['file_name']}: {r['master_datetime']} -> {r['master_datetime'][:-1]}")

    if fix and not dry_run:
        conn.executemany(
            'UPDATE file_contents SET master_datetime = ? WHERE id = ? AND master_datetime = ?',
            [(r['master_datetime'][:-1], r['id'], r['master_datetime']) for r in fix])
        conn.commit()
    conn.close()
    return len(fix)


def main():
    parser = argparse.ArgumentParser(
        description='Store offset-less photo times as local (no "Z").')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--catalogs-root',
                        help='Directory holding catalog folders on this machine, when the '
                             'stored target_directory is a container path.')
    args = parser.parse_args()

    if args.catalog_id:
        catalog = Catalog.get_by_id(args.catalog_id)
        if not catalog:
            print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
            return 1
        catalogs = [catalog]
    else:
        catalogs = Catalog.get_all()

    total = 0
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id}), target={cat.target_directory}")
        total += migrate_catalog(cat, dry_run=args.dry_run, catalogs_root=args.catalogs_root)
    print(f"\n{'Would change' if args.dry_run else 'Changed'} {total} time(s).")
    return 0


if __name__ == '__main__':
    sys.exit(main())
