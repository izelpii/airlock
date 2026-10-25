#!/usr/bin/env python3
"""
migrate_fix_video_dates.py
==========================
Re-read QuickTime metadata for videos that were imported with a filesystem date.

Why they are wrong
------------------
The importer only ran metadata extraction for IMAGES. A .MOV carries no EXIF
block, so `exif_data` came back None and the date fell through to the
filesystem — the moment the file was imported, not the moment it was filmed.
Videos ended up dated "today", with no exif_data row at all, and so no camera,
no GPS and no dimensions either.

`extract_exif_data_with_exiftool()` now reads QuickTime tags, and the importer
calls it for videos. This fixes the ones already in the catalog.

Which tag
---------
QuickTime:CreationDate, with its UTC offset dropped, so the stored value is
LOCAL wall-clock — the same thing EXIF:DateTimeOriginal is for a photo.

NOT QuickTime:CreateDate, which is UTC. Verified against unedited clips, where
CreateDate equals CreationDate converted to UTC to the second. Using it would
shift every video by the UTC offset and push anything filmed after roughly 8pm
onto the following day.

What this does NOT do
---------------------
Rename files or move them between dated folders. The path in `file_contents`
stays valid, album symlinks and deployments keep resolving, and the folder name
becomes a record of when the file arrived rather than when it was filmed. Only
the metadata changes.

EVENTS ARE NOT REBUILT either. Events were grouped using the wrong dates, so
after this runs, re-run the event extraction workflow to regroup them.

Usage
-----
    python backend/migrate_fix_video_dates.py --dry-run
    python backend/migrate_fix_video_dates.py
    python backend/migrate_fix_video_dates.py --catalog-id 67
    python backend/migrate_fix_video_dates.py --all       # also re-read videos
                                                          # that already have a date

Idempotent. By default only videos whose date came from the filesystem (or that
have no exif_data row) are touched; --all re-reads every video.
"""

import argparse
import os
import shutil
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, 'workflows'))

from catalog import Catalog  # noqa: E402
from media_types import is_video_file  # noqa: E402
from migration_utils import resolve_catalog_dir  # noqa: E402
from lib.utilities import (  # noqa: E402
    extract_exif_data_with_exiftool,
    resolve_master_datetime,
    compute_geohash,
)

# raw_exif_json is NOT here: it lives in exif_raw, written separately below.
# Putting it back into exif_data would undo migrate_split_raw_exif.py for
# every row this script touches.
_EXIF_COLUMNS = (
    'creation_date', 'latitude', 'longitude', 'altitude', 'camera_make',
    'camera_model', 'lens_model', 'focal_length', 'aperture', 'iso',
    'shutter_speed', 'flash', 'orientation', 'width', 'height',
)


def _resolve(stored, root):
    if not stored:
        return None
    return stored if os.path.isabs(stored) else os.path.join(root, stored)


def _write_exif(conn, file_id, file_name, data):
    """Insert or update the exif_data row for one file."""
    geohash = compute_geohash(data.get('latitude'), data.get('longitude'))
    values = [data.get(c) for c in _EXIF_COLUMNS] + [geohash]
    existing = conn.execute('SELECT id FROM exif_data WHERE file_id = ?',
                            (file_id,)).fetchone()
    if existing:
        assignments = ', '.join(f'{c} = ?' for c in _EXIF_COLUMNS) + ', geohash = ?'
        conn.execute(f'UPDATE exif_data SET {assignments} WHERE file_id = ?',
                     values + [file_id])
        exif_id = existing[0]
    else:
        cols = ', '.join(('file_id', 'file_name') + _EXIF_COLUMNS + ('geohash',))
        marks = ', '.join('?' * (len(_EXIF_COLUMNS) + 3))
        cur = conn.execute(f'INSERT INTO exif_data ({cols}) VALUES ({marks})',
                           [file_id, file_name] + values)
        exif_id = cur.lastrowid

    # The raw dump, in its own table. CREATE IF NOT EXISTS because this script
    # must work against a catalog the backend has not opened since the upgrade.
    conn.execute('''CREATE TABLE IF NOT EXISTS exif_raw (
                        exif_id       INTEGER PRIMARY KEY,
                        raw_exif_json TEXT,
                        FOREIGN KEY (exif_id) REFERENCES exif_data (id)
                            ON DELETE CASCADE)''')
    if data.get('raw_exif_json'):
        conn.execute('INSERT OR REPLACE INTO exif_raw (exif_id, raw_exif_json) '
                     'VALUES (?, ?)', (exif_id, data.get('raw_exif_json')))


def migrate_catalog(catalog, dry_run=False, catalogs_root=None, redo_all=False):
    summary = {'fixed': 0, 'skipped': 0, 'no_metadata': 0, 'missing': 0, 'unreachable': 0}

    target_dir = resolve_catalog_dir(catalog, catalogs_root) if catalog.target_directory else None
    if not target_dir:
        print(f'  [skip] {catalog.target_directory} not reachable from here.')
        summary['unreachable'] = 1
        return summary

    db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(db_path):
        print(f'  [skip] no workflow_results.db in {target_dir}')
        summary['unreachable'] = 1
        return summary

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    conn.row_factory = sqlite3.Row
    try:
        rows = [r for r in conn.execute(
            'SELECT id, file_name, file_path, master_datetime, master_datetime_source '
            'FROM file_contents').fetchall()
            if is_video_file(r['file_name'] or '')]
        if not rows:
            print('    no videos in this catalog')
            return summary

        for row in rows:
            if not redo_all and row['master_datetime_source'] == 'exif':
                summary['skipped'] += 1
                continue

            path = _resolve(row['file_path'], target_dir)
            if not path or not os.path.exists(path):
                print(f"    [missing] {row['file_name']}")
                summary['missing'] += 1
                continue

            data = extract_exif_data_with_exiftool(path)
            if not data or not data.get('creation_date'):
                # A video with no QuickTime date at all - screen recordings and
                # some Android exports. The filesystem date is the best answer
                # available, so it is left alone rather than blanked.
                print(f"    [no date] {row['file_name']} — keeping the filesystem date")
                summary['no_metadata'] += 1
                continue

            master_dt, master_src = resolve_master_datetime(path, exif_data=data)
            old = row['master_datetime']
            if dry_run:
                print(f"    [dry-run] {row['file_name']}\n"
                      f"              {old}  ->  {master_dt}  ({master_src})")
            else:
                _write_exif(conn, row['id'], row['file_name'], data)
                conn.execute(
                    'UPDATE file_contents SET master_datetime = ?, master_datetime_source = ? '
                    'WHERE id = ?', (master_dt, master_src, row['id']))
                print(f"    [fixed] {row['file_name']}: {old} -> {master_dt}")
            summary['fixed'] += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Re-read QuickTime dates for videos imported with a filesystem date.')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--all', dest='redo_all', action='store_true',
                        help='Re-read every video, not only the ones dated from the filesystem.')
    parser.add_argument('--catalogs-root')
    args = parser.parse_args()

    if shutil.which('exiftool') is None:
        print('exiftool is not installed here, so nothing can be read.\n'
              'In this deployment it lives in the backend container:\n'
              '    podman exec workflow-backend python /app/migrate_fix_video_dates.py',
              file=sys.stderr)
        return 1

    catalogs = ([Catalog.get_by_id(args.catalog_id)] if args.catalog_id
                else Catalog.get_all())
    if args.catalog_id and not catalogs[0]:
        print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
        return 1

    total = {'fixed': 0, 'skipped': 0, 'no_metadata': 0, 'missing': 0, 'unreachable': 0}
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id}), target={cat.target_directory}")
        result = migrate_catalog(cat, dry_run=args.dry_run,
                                 catalogs_root=args.catalogs_root, redo_all=args.redo_all)
        for k in total:
            total[k] += result.get(k, 0)

    verb = 'Would fix' if args.dry_run else 'Fixed'
    print(f"\n{verb}: {total['fixed']} video date(s).")
    for key, label in (('skipped', 'Already dated from metadata'),
                       ('no_metadata', 'No QuickTime date; filesystem date kept'),
                       ('missing', 'File not found on disk'),
                       ('unreachable', 'Catalogs not reachable from here')):
        if total[key]:
            print(f'  {label}: {total[key]}')
    if total['fixed'] and not args.dry_run:
        print('\nEvents were grouped using the OLD dates. Re-run the event '
              'extraction workflow so videos land in the right events.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
