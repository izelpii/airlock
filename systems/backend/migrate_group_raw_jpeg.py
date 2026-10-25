#!/usr/bin/env python3
"""
migrate_group_raw_jpeg.py
=========================
Group RAW+JPG pairs that were imported before the system understood them.

Implements documentation/specs/file-unity-spec.md section 5.2.

A camera set to RAW+JPG writes two files for one shutter actuation. Imported
before this feature they became two independent images: two gallery tiles, two
event members, two sets of derivatives, two faces to confirm. This links them.

WHAT IT DOES
  * finds pairs with the same rule the importer uses - several signals must
    agree, and a timestamp alone is never enough (lib/grouping.py);
  * records an image_groups row and sets image_group_id on both files;
  * repoints event and album links from the secondary to the primary;
  * deletes the SECONDARY's derivatives, which are a second rendering of the
    same photograph - but only where the primary demonstrably has the same one.

WHAT IT DOES NOT DO
  * touch either original. Both files stay on disk and stay addressable.
  * delete the only copy of a rendering. See --dry-run first.

Usage
-----
    python backend/migrate_group_raw_jpeg.py --dry-run
    python backend/migrate_group_raw_jpeg.py
    python backend/migrate_group_raw_jpeg.py --catalog-id 67
    python backend/migrate_group_raw_jpeg.py --keep-derivatives   # link only
"""

import argparse
import json
import os
import shutil as _shutil
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, 'workflows'))

from catalog import Catalog  # noqa: E402
from migration_utils import resolve_catalog_dir  # noqa: E402
from lib.grouping import read_match_keys, find_groups  # noqa: E402
from lib.utilities import is_raw_file, is_image_file  # noqa: E402


def _resolve(stored, root):
    return None if not stored else (stored if os.path.isabs(stored)
                                    else os.path.join(root, stored))


def _characteristic_set(conn, file_id):
    return {r[0] for r in conn.execute(
        'SELECT characteristic FROM alternative_versions WHERE original_file_id = ?',
        (file_id,))}


def _repoint(conn, table, column, secondary_id, primary_id):
    """Move a link from the secondary to the primary, without creating a duplicate."""
    try:
        conn.execute(f'UPDATE OR IGNORE {table} SET {column} = ? WHERE {column} = ?',
                     (primary_id, secondary_id))
        cur = conn.execute(f'DELETE FROM {table} WHERE {column} = ?', (secondary_id,))
        return cur.rowcount
    except sqlite3.OperationalError:
        return 0


def migrate_catalog(catalog, dry_run=False, catalogs_root=None, keep_derivatives=False):
    summary = {'groups': 0, 'links_moved': 0, 'derivs_removed': 0,
               'bytes_freed': 0, 'unreachable': 0}

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
        # The column and table may not exist yet if the backend has not opened
        # this catalog since the upgrade - the same case migrate_add_video_
        # duration.py handles with _ensure_column.
        cols = {r[1] for r in conn.execute('PRAGMA table_info(file_contents)')}
        if 'image_group_id' not in cols:
            conn.execute('ALTER TABLE file_contents ADD COLUMN image_group_id INTEGER')
        conn.execute('''CREATE TABLE IF NOT EXISTS image_groups (
                            id INTEGER PRIMARY KEY AUTOINCREMENT,
                            primary_file_id INTEGER NOT NULL,
                            kind TEXT NOT NULL DEFAULT 'raw_jpeg',
                            match_rule TEXT, match_evidence TEXT,
                            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''')
        conn.commit()

        rows = [dict(r) for r in conn.execute(
            'SELECT id, file_name, file_path, image_group_id FROM file_contents')]
        by_path, paths = {}, []
        for r in rows:
            if r['image_group_id'] is not None:
                continue                      # already grouped - idempotent
            if not is_image_file(r['file_name'] or ''):
                continue
            p = _resolve(r['file_path'], target_dir)
            if p and os.path.exists(p):
                by_path[p] = r
                paths.append(p)

        if not paths:
            print('    no ungrouped images to consider')
            return summary

        print(f'    scanning {len(paths)} image(s)...')
        keys = read_match_keys(paths, log=lambda *a: None)
        groups = find_groups(paths, keys, is_raw_file, is_image_file, log=print)
        if not groups:
            print('    no RAW+JPG pairs found')
            return summary

        for g in groups:
            prow, srow = by_path[g['primary']], by_path[g['secondary']]
            pid, sid = prow['id'], srow['id']
            label = f"{os.path.basename(g['primary'])[15:]} + {os.path.basename(g['secondary'])[15:]}"

            # Which of the secondary's derivatives the primary also has. Only
            # those may go: never delete the only copy of a rendering.
            p_chars = _characteristic_set(conn, pid)
            removable = [dict(r) for r in conn.execute(
                'SELECT id, characteristic, alternative_file_path, alternative_file_size '
                'FROM alternative_versions WHERE original_file_id = ?', (sid,))
                if r['characteristic'] in p_chars]
            kept_back = conn.execute(
                'SELECT COUNT(*) FROM alternative_versions WHERE original_file_id = ?',
                (sid,)).fetchone()[0] - len(removable)
            freed = sum(r['alternative_file_size'] or 0 for r in removable)

            if dry_run:
                print(f'    [dry-run] {label}  [{g["match_rule"]}]'
                      + (f'  would free {len(removable)} derivative(s), {freed // 1024} KB'
                         if removable and not keep_derivatives else ''))
                if kept_back:
                    print(f'              {kept_back} derivative(s) kept: the primary has no equivalent')
                summary['groups'] += 1
                summary['derivs_removed'] += 0 if keep_derivatives else len(removable)
                summary['bytes_freed'] += 0 if keep_derivatives else freed
                continue

            cur = conn.execute(
                'INSERT INTO image_groups (primary_file_id, kind, match_rule, match_evidence) '
                'VALUES (?, ?, ?, ?)',
                (pid, g['kind'], g['match_rule'], json.dumps(g['match_evidence'])))
            gid = cur.lastrowid
            conn.execute('UPDATE file_contents SET image_group_id = ? WHERE id IN (?, ?)',
                         (gid, pid, sid))

            moved = 0
            for table, column in (('event_files', 'file_id'), ('section_photos', 'file_id')):
                moved += _repoint(conn, table, column, sid, pid)
            summary['links_moved'] += moved

            if not keep_derivatives:
                for d in removable:
                    path = _resolve(d['alternative_file_path'], target_dir)
                    try:
                        if path and os.path.isfile(path):
                            os.unlink(path)
                    except OSError as exc:
                        print(f'      could not remove {path}: {exc}')
                        continue
                    conn.execute('DELETE FROM alternative_versions WHERE id = ?', (d['id'],))
                    summary['derivs_removed'] += 1
                    summary['bytes_freed'] += d['alternative_file_size'] or 0

            print(f'    [grouped] {label}  [{g["match_rule"]}]'
                  + (f'  moved {moved} link(s)' if moved else '')
                  + (f'  freed {len(removable)} derivative(s)'
                     if removable and not keep_derivatives else ''))
            summary['groups'] += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Link RAW+JPG pairs already imported into one logical image.')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--keep-derivatives', action='store_true',
                        help='Link the pairs but leave the secondary\'s derivatives on disk.')
    parser.add_argument('--catalogs-root')
    args = parser.parse_args()

    if _shutil.which('exiftool') is None:
        print('exiftool is not installed here, so nothing can be matched.\n'
              'In this deployment it lives in the backend container:\n'
              '    podman exec workflow-backend python /app/migrate_group_raw_jpeg.py',
              file=sys.stderr)
        return 1

    catalogs = ([Catalog.get_by_id(args.catalog_id)] if args.catalog_id else Catalog.get_all())
    if args.catalog_id and not catalogs[0]:
        print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
        return 1

    total = {'groups': 0, 'links_moved': 0, 'derivs_removed': 0,
             'bytes_freed': 0, 'unreachable': 0}
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id})")
        result = migrate_catalog(cat, dry_run=args.dry_run, catalogs_root=args.catalogs_root,
                                 keep_derivatives=args.keep_derivatives)
        for k in total:
            total[k] += result.get(k, 0)

    print(f"\n{'Would group' if args.dry_run else 'Grouped'}: {total['groups']} image(s).")
    if total['links_moved']:
        print(f"  Event/album links moved to the primary: {total['links_moved']}")
    if total['derivs_removed']:
        print(f"  Duplicate derivatives removed: {total['derivs_removed']} "
              f"({total['bytes_freed'] // 1024} KB)")
    if total['unreachable']:
        print(f"  Catalogs not reachable from here: {total['unreachable']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
