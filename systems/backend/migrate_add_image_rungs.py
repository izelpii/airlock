"""
migrate_add_image_rungs.py
==========================
Generate the extra image sizes a catalog has enabled, for photos imported
before it was enabled.

See documentation/specs/file-sizes-spec.md section 7.5.

This script does the SECOND of the two backfill jobs. The first - labelling
every existing ``alternative_versions`` row with its ``rung`` - runs
automatically in ``models.py`` when a catalog database is opened, because every
query in the application filters on that column and an unlabelled row would
simply vanish from the UI. There is nothing to run by hand for that.

What is left is expensive and therefore explicit: rendering a new derivative
from each ORIGINAL, at each rung the catalog has turned on.

Rendered from the original, never from the 1200px base
------------------------------------------------------
Scaling a 1200px file up to 2048 produces a larger file carrying no more
detail. Every rung here is rendered from the source image, which is also why
this is slow: it reads a multi-megabyte original per photo. The ``full`` rung
is the extreme case - it writes one too, at about 2.3 GB per 1,000 photos in
this installation's reference catalog.

Skipping duplicates
-------------------
A rung produces ``min(original_width, rung_width)`` pixels. When that is no
wider than a rung already on disk, the file would be a byte-for-byte duplicate
of one already stored, so it is not written at all (spec 4.9). In this
installation's reference catalog that applies to 16% of photos at 2048.

Resumable
---------
A file that already has a row for a rung is skipped, so an interrupted run
costs only the photo it was working on. Safe to run repeatedly.

Host vs container paths
-----------------------
A catalog's ``target_directory`` is stored as the backend container sees it
(``/catalogs/family``). That path does not exist on the host, so when the stored
directory is missing this script falls back to the same catalog under the
repository's ``catalogs/`` folder, through ``migration_utils.resolve_catalog_dir``
- the same rule every other migration uses. ``--catalogs-root`` overrides it.

Usage
-----
    python backend/migrate_add_image_rungs.py                  # all catalogs
    python backend/migrate_add_image_rungs.py --catalog-id 1   # one catalog
    python backend/migrate_add_image_rungs.py --dry-run        # preview only
    python backend/migrate_add_image_rungs.py --rung 2048      # one rung only
    python backend/migrate_add_image_rungs.py --catalogs-root /mnt/catalogs
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
from workflows.lib.orientation import user_orientation  # noqa: E402
from workflows.lib.utilities import (  # noqa: E402
    create_alternate_version, IMAGE_RUNGS, MANDATORY_RUNGS, rung_width, rungs_for,
)

_HOST_CATALOGS_ROOT = os.path.join(repo_root(), 'catalogs')


def _table_exists(conn, table):
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,)).fetchone()
    return bool(row and row[0])


def _resolve(stored, target_dir):
    if stored and not os.path.isabs(stored):
        return os.path.join(target_dir, stored)
    return stored


def migrate_catalog(catalog, only_rung=None, dry_run=False, catalogs_root=None):
    summary = {'created': 0, 'duplicate': 0, 'existing': 0,
               'missing_original': 0, 'error': 0, 'skipped': 0}

    # Extras only. The base is generated at import and is never this script's
    # job - if it is missing, the photo was never imported successfully.
    wanted = [r for r in rungs_for(catalog.image_rungs) if r not in MANDATORY_RUNGS]
    if only_rung:
        wanted = [r for r in wanted if r == only_rung]
    if not wanted:
        print(f"  [skip] Catalog '{catalog.name}' has no extra image sizes enabled.")
        summary['skipped'] = 1
        return summary

    target_dir = resolve_catalog_dir(catalog, catalogs_root)
    if not target_dir:
        print(f"  [skip] Directory {catalog.target_directory} not found on this "
              f"machine, and no match under {catalogs_root or _HOST_CATALOGS_ROOT}.")
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
        print('  [skip] No alternative_versions table.')
        conn.close()
        return summary

    print(f"  sizes to generate: {', '.join(wanted)}")

    # One row per photo that has a base derivative: the base's own width is the
    # floor a new rung has to beat, and file_contents gives the original to
    # render from.
    photos = conn.execute("""
        SELECT av.original_file_id AS file_id,
               av.width            AS base_width,
               fc.file_path, fc.file_name,
               ed.creation_date
        FROM alternative_versions av
        JOIN file_contents fc ON fc.id = av.original_file_id
        LEFT JOIN exif_data ed ON ed.file_id = fc.id
        WHERE av.rung = '1200'
        ORDER BY av.original_file_id
    """).fetchall()
    print(f'  {len(photos)} photo(s) with a base derivative.')

    # The widest aspect-preserving derivative each photo ALREADY has. A rung
    # produces min(original_width, rung_width) pixels, so anything no wider
    # than this would be a byte-for-byte duplicate of a file already on disk
    # (spec 4.9).
    #
    # This used to compare against the base alone, which was right while 2048
    # was the largest rung anyone would enable second. It is not right for
    # `full`: on a catalog that already has 2560, every photo whose original is
    # 2560 or narrower would get a second copy of the same pixels - at full-res
    # file sizes. Seeded from the database and kept current as rungs are
    # written, so one run cannot duplicate its own output either.
    _ladder = ','.join('?' * len(IMAGE_RUNGS))
    widest = {r['original_file_id']: r['w'] or 0 for r in conn.execute(
        f'SELECT original_file_id, MAX(width) AS w FROM alternative_versions '
        f'WHERE rung IN ({_ladder}) GROUP BY original_file_id',
        list(IMAGE_RUNGS)).fetchall()}

    for rung in sorted(wanted, key=rung_width):
        spec = IMAGE_RUNGS[rung]
        have = {r['original_file_id'] for r in conn.execute(
            'SELECT original_file_id FROM alternative_versions WHERE rung = ?',
            (rung,)).fetchall()}
        todo = [p for p in photos if p['file_id'] not in have]
        summary['existing'] += len(photos) - len(todo)
        print(f'  [{rung}] {len(todo)} to generate, '
              f'{len(photos) - len(todo)} already present.')
        if dry_run:
            continue

        for index, photo in enumerate(todo, 1):
            if index % 100 == 0 or index == len(todo):
                print(f'    [{rung}] {index}/{len(todo)}')
            original = _resolve(photo['file_path'], target_dir)
            if not os.path.exists(original):
                summary['missing_original'] += 1
                continue
            try:
                info = create_alternate_version(
                    original, target_dir, photo['file_id'],
                    max_width=spec['max_width'], suffix=spec['suffix'],
                    # The Basic Editor's turn or flip, so a new rung matches
                    # the ones the photo already has.
                    user_orientation=user_orientation(conn, photo['file_id']))
                if not info:
                    summary['error'] += 1
                    continue
                # No wider than what is already on disk means the same pixels
                # under a different name (spec 4.9).
                if info['width'] <= widest.get(photo['file_id'],
                                               photo['base_width'] or 0):
                    try:
                        os.remove(info['path'])
                    except OSError:
                        pass
                    summary['duplicate'] += 1
                    continue
                conn.execute('''
                    INSERT INTO alternative_versions
                    (original_file_id, alternative_file_path, alternative_file_name,
                     alternative_file_size, characteristic, width, height, rung)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (photo['file_id'],
                      os.path.relpath(info['path'], target_dir), info['name'],
                      info['size'], info['characteristic'],
                      info['width'], info['height'], rung))
                # Committed per photo, not per run: this is a long job, and an
                # interrupted one must keep the work it already did.
                conn.commit()
                widest[photo['file_id']] = info['width']
                summary['created'] += 1
            except Exception as exc:
                print(f"    [error] {photo['file_name']}: {exc}")
                summary['error'] += 1

    conn.close()
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='Generate the extra image sizes a catalog has enabled, '
                    'for photos imported before it was enabled.')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--rung', choices=sorted(
        r for r in IMAGE_RUNGS if r not in MANDATORY_RUNGS))
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--catalogs-root')
    args = parser.parse_args(argv)

    catalogs = ([Catalog.get_by_id(args.catalog_id)] if args.catalog_id
                else Catalog.get_all())
    catalogs = [c for c in catalogs if c]
    if not catalogs:
        print('No catalogs found.')
        return 1

    totals = {}
    for catalog in catalogs:
        print(f"\nCatalog {catalog.id}: {catalog.name}")
        summary = migrate_catalog(catalog, only_rung=args.rung,
                                  dry_run=args.dry_run,
                                  catalogs_root=args.catalogs_root)
        for key, value in summary.items():
            totals[key] = totals.get(key, 0) + value

    print('\n' + '─' * 60)
    print('created          :', totals.get('created', 0))
    print('already present  :', totals.get('existing', 0))
    print('same pixels      :', totals.get('duplicate', 0),
          '(narrower than the rung; not stored)')
    print('original missing :', totals.get('missing_original', 0))
    print('errors           :', totals.get('error', 0))
    if args.dry_run:
        print('\n(dry run - nothing was written)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
