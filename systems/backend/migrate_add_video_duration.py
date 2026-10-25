#!/usr/bin/env python3
"""
migrate_add_video_duration.py
=============================
Backfill file_contents.duration_seconds for videos ingested before it existed.

Implements documentation/specs/video-page-spec.md section 4.2.

The Videos page shows a duration badge on every card and a running time in the
detail panel. Nothing computed it until now: _ffprobe_streams() has always
returned the duration during ingestion, and create_video_proxy() threw it away.
This reads it back for videos already in a catalog, so nothing has to be
re-encoded and no page has to run ffprobe per request.

Which file is probed, in order:

  1. the VIDEO PROXY, when one exists - it is the file the browser will actually
     play, so its length is the one the scrubber will show;
  2. otherwise the ORIGINAL, which is what a passthrough video plays. That is
     the common case: most phone video is already browser-ready and has no
     proxy row at all (video-spec.md section 4).

Requires ffprobe. Without it the script says so and exits without writing.

Usage
-----
    python backend/migrate_add_video_duration.py --dry-run
    python backend/migrate_add_video_duration.py
    python backend/migrate_add_video_duration.py --catalog-id 67
    python backend/migrate_add_video_duration.py --catalogs-root /mnt/catalogs
    python backend/migrate_add_video_duration.py --recheck   # re-probe every video

Idempotent: a video that already has a duration is skipped unless --recheck.
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
from lib.utilities import _ffprobe_streams  # noqa: E402


def _resolve(stored, root):
    """A stored path may be relative to the catalog; absolute ones pass through."""
    if not stored:
        return None
    return stored if os.path.isabs(stored) else os.path.join(root, stored)


def _ensure_column(conn):
    """
    Add duration_seconds if this catalog's database predates it.

    CatalogDatabase.init_db() does this too, but this script must work against a
    catalog the backend has not opened since the upgrade - which is exactly the
    catalog that needs backfilling.
    """
    cols = {r[1] for r in conn.execute('PRAGMA table_info(file_contents)')}
    if 'duration_seconds' not in cols:
        conn.execute('ALTER TABLE file_contents ADD COLUMN duration_seconds REAL')
        conn.commit()
        print('    added file_contents.duration_seconds')


def migrate_catalog(catalog, dry_run=False, catalogs_root=None, recheck=False):
    summary = {'updated': 0, 'skipped': 0, 'unreadable': 0, 'unreachable': 0}

    if not catalog.target_directory:
        print(f"  [skip] Catalog '{catalog.name}' has no target_directory.")
        summary['unreachable'] = 1
        return summary

    target_dir = resolve_catalog_dir(catalog, catalogs_root)
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
        _ensure_column(conn)

        rows = [r for r in conn.execute(
            'SELECT id, file_name, file_path, duration_seconds FROM file_contents'
        ).fetchall() if is_video_file(r['file_name'] or '')]

        if not rows:
            print('    no videos in this catalog')
            return summary

        for row in rows:
            if row['duration_seconds'] and not recheck:
                summary['skipped'] += 1
                continue

            # The proxy is what the browser plays, so its length is the one the
            # scrubber will show. Absent for every passthrough video.
            proxy = conn.execute(
                "SELECT alternative_file_path FROM alternative_versions "
                "WHERE original_file_id = ? AND UPPER(characteristic) LIKE '%VIDEO PROXY%' "
                "LIMIT 1", (row['id'],)).fetchone()

            candidates = []
            if proxy and proxy['alternative_file_path']:
                candidates.append(_resolve(proxy['alternative_file_path'], target_dir))
            candidates.append(_resolve(row['file_path'], target_dir))

            duration = None
            for path in candidates:
                if not path or not os.path.exists(path):
                    continue
                info = _ffprobe_streams(path)
                if info and info.get('duration'):
                    duration = float(info['duration'])
                    break

            if duration is None:
                print(f"    [unreadable] {row['file_name']}")
                summary['unreadable'] += 1
                continue

            source = 'proxy' if (proxy and proxy['alternative_file_path']) else 'original'
            if dry_run:
                print(f"    [dry-run] {row['file_name']}: {duration:.1f}s (from the {source})")
            else:
                conn.execute('UPDATE file_contents SET duration_seconds = ? WHERE id = ?',
                             (duration, row['id']))
                print(f"    [set] {row['file_name']}: {duration:.1f}s (from the {source})")
            summary['updated'] += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()

    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Backfill video durations into file_contents.duration_seconds.')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--recheck', action='store_true',
                        help='Re-probe videos that already have a duration.')
    parser.add_argument('--catalogs-root',
                        help='Directory holding catalog folders on this machine, when '
                             'the stored target_directory is a container path.')
    args = parser.parse_args()

    if shutil.which('ffprobe') is None:
        print('ffprobe is not installed here, so nothing can be probed.\n'
              'In this deployment it lives in the backend container:\n'
              '    podman exec workflow-backend python '
              '/app/migrate_add_video_duration.py', file=sys.stderr)
        return 1

    if args.catalog_id:
        catalog = Catalog.get_by_id(args.catalog_id)
        if not catalog:
            print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
            return 1
        catalogs = [catalog]
    else:
        catalogs = Catalog.get_all()

    total = {'updated': 0, 'skipped': 0, 'unreadable': 0, 'unreachable': 0}
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id}), target={cat.target_directory}")
        result = migrate_catalog(cat, dry_run=args.dry_run,
                                 catalogs_root=args.catalogs_root, recheck=args.recheck)
        for k in total:
            total[k] += result.get(k, 0)

    verb = 'Would set' if args.dry_run else 'Set'
    print(f"\n{verb}: {total['updated']} duration(s).")
    if total['skipped']:
        print(f"  Already had one: {total['skipped']}")
    if total['unreadable']:
        print(f"  Could not be probed: {total['unreadable']}")
    if total['unreachable']:
        print(f"  Catalogs not reachable from here: {total['unreachable']}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
