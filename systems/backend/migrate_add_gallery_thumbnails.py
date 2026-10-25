"""
migrate_add_gallery_thumbnails.py
=================================
Backfill migration that generates the larger gallery thumbnails for images that
were imported before multiple thumbnail sizes existed.

Only the 72x72 thumbnail used by the Photos filmstrip was created originally.
The 160x160 gallery cards (People / Locations / Events) were being filled with
the full 1200px browser-friendly version, which the backend re-encoded on every
request. This creates the missing sizes listed in ``THUMBNAIL_SIZES`` from the
existing ``_alt.jpg`` files, so no original image has to be re-read.

Each generated thumbnail is recorded in ``alternative_versions`` with the
characteristic ``TYPE THUMBNAIL <size>``. Rows that already exist are left
alone, so the script is safe to run repeatedly.

Host vs container paths
-----------------------
A catalog's ``target_directory`` is stored as the backend container sees it
(``/catalogs/family``), because the pod bind-mounts the repository's
``catalogs/`` directory to ``/catalogs`` (see backend-pod.yaml). That path does
not exist on the host, so when the stored directory is missing this script
falls back to the same catalog under the repository's ``catalogs/`` folder and
reports the substitution.

That fallback is ``migration_utils.resolve_catalog_dir`` - the directory-level
counterpart of the ``resolve_catalog_db`` the other migrations use, so every
migration resolves catalog paths by one rule. ``--catalogs-root`` overrides the
directory searched.

Usage
-----
    python backend/migrate_add_gallery_thumbnails.py                  # all catalogs
    python backend/migrate_add_gallery_thumbnails.py --catalog-id 3   # single catalog
    python backend/migrate_add_gallery_thumbnails.py --dry-run        # preview only
    python backend/migrate_add_gallery_thumbnails.py --size 320       # one size only
    python backend/migrate_add_gallery_thumbnails.py --catalogs-root /mnt/catalogs
"""

import argparse
import os
import sqlite3
import sys

# ── path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)
sys.path.insert(0, _HERE)

from catalog import Catalog  # noqa: E402
from migration_utils import repo_root, resolve_catalog_dir  # noqa: E402
from workflows.lib.utilities import create_thumbnail, THUMBNAIL_SIZES  # noqa: E402

# Where catalog directories live when running on the host rather than in the
# container. Mirrors the catalogs volume hostPath in backend-pod.yaml, and is
# the same fallback the other migrations get through migration_utils.
_HOST_CATALOGS_ROOT = os.path.join(repo_root(), 'catalogs')


# ── helpers ───────────────────────────────────────────────────────────────────

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone()
    return bool(row and row[0])


def _resolve_path(stored: str, target_dir: str) -> str:
    """Return an absolute path for a stored value that may be relative."""
    if stored and not os.path.isabs(stored):
        return os.path.join(target_dir, stored)
    return stored


def migrate_catalog(catalog, sizes, dry_run: bool = False, catalogs_root=None) -> dict:
    """Generate the missing thumbnail sizes for *catalog*. Returns a summary dict."""
    summary = {'created': 0, 'existing': 0, 'alt_missing': 0, 'error': 0, 'skipped': 0}

    if not catalog.target_directory:
        print(f"  [skip] Catalog '{catalog.name}' has no target_directory.")
        summary['skipped'] = 1
        return summary

    target_dir = resolve_catalog_dir(catalog, catalogs_root)
    if not target_dir:
        print(f"  [skip] Directory {catalog.target_directory} not found on this machine, "
              f"and no match under {catalogs_root or _HOST_CATALOGS_ROOT}.")
        summary['skipped'] = 1
        return summary

    db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(db_path):
        print(f"  [skip] No workflow_results.db at {db_path}.")
        summary['skipped'] = 1
        return summary

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')

    if not _table_exists(conn, 'alternative_versions'):
        print("  [skip] No alternative_versions table.")
        conn.close()
        return summary

    # Every browser-friendly alt version is a thumbnail source. TYPE JPG covers
    # images that were already under 1200px wide and so were never scaled.
    alts = conn.execute('''
        SELECT id, original_file_id, alternative_file_path, alternative_file_name
        FROM alternative_versions
        WHERE UPPER(characteristic) LIKE '%TYPE JPG%'
        ORDER BY original_file_id
    ''').fetchall()
    print(f"  {len(alts)} source alt version(s) found.")

    for size in sizes:
        characteristic = f'TYPE THUMBNAIL {size}'

        # file_ids that already have this size, so reruns are cheap and idempotent
        have = {
            r['original_file_id'] for r in conn.execute(
                'SELECT original_file_id FROM alternative_versions '
                'WHERE UPPER(characteristic) = ?', (characteristic.upper(),)
            ).fetchall()
        }
        todo = [a for a in alts if a['original_file_id'] not in have]
        summary['existing'] += len(alts) - len(todo)
        print(f"  size {size}: {len(todo)} to create, {len(alts) - len(todo)} already present.")

        for alt in todo:
            alt_path = _resolve_path(alt['alternative_file_path'], target_dir)
            if not os.path.exists(alt_path):
                print(f"    [missing] {alt['alternative_file_path']}")
                summary['alt_missing'] += 1
                continue

            if dry_run:
                summary['created'] += 1
                continue

            try:
                info = create_thumbnail(
                    alt_path, alt['alternative_file_name'], target_dir, size=size
                )
                if not info:
                    summary['error'] += 1
                    continue

                rel_path = os.path.relpath(info['path'], target_dir)
                conn.execute('''
                    INSERT INTO alternative_versions
                    (original_file_id, alternative_file_path, alternative_file_name,
                     alternative_file_size, characteristic, width, height, rung)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (alt['original_file_id'], rel_path, info['name'],
                      info['size'], info['characteristic'],
                      info['width'], info['height'], f'thumb{size}'))
                summary['created'] += 1
            except Exception as exc:
                print(f"    [error] {alt['alternative_file_name']}: {exc}")
                summary['error'] += 1

        if not dry_run:
            conn.commit()

    conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Backfill gallery thumbnails for previously imported images.'
    )
    parser.add_argument('--catalog-id', type=int, help='Only migrate this catalog.')
    parser.add_argument('--dry-run', action='store_true',
                        help='Report what would be created without writing anything.')
    parser.add_argument('--size', type=int, action='append',
                        help='Only generate this size (repeatable). Defaults to all.')
    parser.add_argument('--catalogs-root',
                        help='Directory holding the catalog folders on this machine. '
                             'Used when a stored target_directory (a container path) '
                             f'does not exist here. Default: {_HOST_CATALOGS_ROOT}')
    args = parser.parse_args()

    sizes = args.size or list(THUMBNAIL_SIZES)

    if args.catalog_id:
        catalog = Catalog.get_by_id(args.catalog_id)
        if not catalog:
            print(f"Error: catalog with id={args.catalog_id} not found.")
            sys.exit(1)
        catalogs = [catalog]
    else:
        catalogs = Catalog.get_all()

    total = {'created': 0, 'existing': 0, 'alt_missing': 0, 'error': 0, 'skipped': 0}

    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id}), target={cat.target_directory}")
        result = migrate_catalog(cat, sizes, dry_run=args.dry_run,
                                 catalogs_root=args.catalogs_root)
        for k in total:
            total[k] += result.get(k, 0)

    action = 'Would create' if args.dry_run else 'Created'
    print(f"\n{action}: {total['created']} thumbnail(s) "
          f"for size(s) {', '.join(str(s) for s in sizes)}.")
    if total['existing']:
        print(f"  Already present: {total['existing']}")
    if total['alt_missing']:
        print(f"  Skipped (source alt file missing): {total['alt_missing']}")
    if total['error']:
        print(f"  Errors: {total['error']}")
    if total['skipped']:
        print(f"  Catalogs skipped (not reachable from here): {total['skipped']}")
    if total['skipped'] == len(catalogs):
        print("\nNo catalog was reachable. The stored target_directory values are the "
              "paths the backend container sees; pass --catalogs-root to point at the "
              "matching directories on this machine, or run this inside the container.")
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
