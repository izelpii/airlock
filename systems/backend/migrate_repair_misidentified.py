#!/usr/bin/env python3
"""
migrate_repair_misidentified.py
===============================
Repair files a catalog imported under the wrong identity, judged by what their
bytes are rather than what their names say (media_types.sniff).

Found on oldserver's family import (2026-10-04), where 3,094 files failed to get a
preview:

  * macOS AppleDouble metadata whose names had been rewritten, so the '._'
    rule never saw them (2,269). Not photos: REMOVED from the catalog the way
    the app's "Remove from catalog" does - links dropped, row kept with
    removed_at - so they appear on Removed files, where the originals can be
    deleted by hand.
  * Videos named like photos - Live Photo clips exported as .JPG (721).
    RENAMED to the extension their content has, moved from Images/ to Videos/
    under the same date, and queued for a preview again.
  * JPEGs cut short (85). Queued again: the import now renders them as far as
    their data goes (lib/utilities.py, LOAD_TRUNCATED_IMAGES).
  * Previews that failed for want of disk space - the catalog's RAID filled up
    on 2026-10-04 and 2,672 video previews failed, either with ENOSPC or as
    "no poster frame could be made" when the poster could not be written.
    Queued again. A video that truly has no frame (9 near-empty .MOVs) simply
    fails again, which costs seconds; telling the two apart from the error is
    not possible.

Only files whose preview FAILED are examined - a few thousand reads, not one per
photo - and only in copy catalogs for the rename: a reference catalog's
originals belong to the user and are never renamed.

Run with the import stopped (it refuses otherwise), then start the import
again: it renders what was queued. Run migrate_group_live_photos.py after it to
pair the recovered clips with their stills.

Usage (in the backend container):
    podman exec workflow-backend python /app/migrate_repair_misidentified.py --catalog-id 9 --dry-run
    podman exec workflow-backend python /app/migrate_repair_misidentified.py --catalog-id 9
"""

import argparse
import os
import shutil
import sqlite3
import sys
from collections import Counter

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, 'workflows'))

from catalog import Catalog  # noqa: E402
from media_types import sniff, is_video_file  # noqa: E402
from migration_utils import resolve_catalog_dir  # noqa: E402
from migrate_group_live_photos import _has_table, _running_import  # noqa: E402

TRUNCATION_MARKERS = ('truncated', 'broken data stream')
DISK_FULL_MARKERS = ('no space left', 'disk is full', 'errno 28')
POSTER_MARKER = 'no poster frame'


def _resolve(stored, root):
    return None if not stored else (stored if os.path.isabs(stored)
                                    else os.path.join(root, stored))


def _remove_from_catalog(conn, file_id):
    """The app's "Remove from catalog" (remove_catalog_file), for a file with no
    derivatives: every link dropped, the row kept with removed_at."""
    for sql in ('DELETE FROM event_files WHERE file_id = ?',
                'UPDATE events SET cover_file_id = NULL WHERE cover_file_id = ?',
                'DELETE FROM section_photos WHERE file_id = ?',
                'UPDATE album_sections SET cover_file_id = NULL WHERE cover_file_id = ?',
                'UPDATE people SET profile_photo_file_id = NULL WHERE profile_photo_file_id = ?'):
        try:
            conn.execute(sql, (file_id,))
        except sqlite3.OperationalError:
            pass                                    # a table this catalog lacks
    conn.execute('UPDATE file_contents SET removed_at = CURRENT_TIMESTAMP '
                 'WHERE id = ? AND removed_at IS NULL', (file_id,))


def migrate_catalog(catalog, dry_run=False, catalogs_root=None, force=False, log=print):
    summary = Counter()
    target_dir = resolve_catalog_dir(catalog, catalogs_root) if catalog.target_directory else None
    db_path = os.path.join(target_dir, 'workflow_results.db') if target_dir else None
    if not db_path or not os.path.exists(db_path):
        log(f'  [skip] {catalog.target_directory} not reachable from here.')
        summary['unreachable'] = 1
        return summary
    reference = getattr(catalog, 'ingest_mode', 'copy') == 'reference'
    originals_root = (getattr(catalog, 'source_root', None) if reference else None) or target_dir

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    conn.row_factory = sqlite3.Row
    try:
        running = _running_import(conn)
        if running and not force:
            log(f"  [skip] import '{running['workflow_name']}' (execution {running['id']}) "
                f'is running. Stop it first, then run this again.')
            summary['busy'] = 1
            return summary
        if not _has_table(conn, 'import_items'):
            log('    no import records; nothing to repair')
            return summary

        items = [dict(r) for r in conn.execute(
            "SELECT i.id AS item_id, i.kind, i.phase_error, i.file_id, i.target_path, "
            "       fc.file_path, fc.file_name, fc.removed_at "
            "FROM import_items i JOIN file_contents fc ON fc.id = i.file_id "
            "WHERE i.state = 'render_failed'")]
        log(f'    {len(items)} file(s) without a preview to examine')

        for it in items:
            if it['removed_at']:
                continue
            path = _resolve(it['file_path'], originals_root)
            found, real_ext = sniff(path) if path else (None, None)
            error = (it['phase_error'] or '').lower()

            if found == 'appledouble':
                summary['appledouble_removed'] += 1
                if not dry_run:
                    _remove_from_catalog(conn, it['file_id'])
                    conn.execute("UPDATE import_items SET state = 'ignored', kind = 'ignored', "
                                 "phase_error = 'macOS metadata (AppleDouble), not a photo or "
                                 "video; removed from the catalog' WHERE id = ?",
                                 (it['item_id'],))

            elif found == 'video' and not is_video_file(it['file_name'] or ''):
                if reference:
                    summary['video_reference_skipped'] += 1
                    continue
                new_name = os.path.splitext(it['file_name'])[0] + real_ext
                rel_dir = os.path.dirname(it['file_path'])
                parts = rel_dir.split(os.sep)
                if parts and parts[0] == 'Images':
                    parts[0] = 'Videos'
                new_rel = os.path.join(os.sep.join(parts), new_name)
                new_abs = os.path.join(target_dir, new_rel)
                if os.path.exists(new_abs):
                    log(f'      [skip] {new_rel} already exists')
                    summary['video_conflict'] += 1
                    continue
                summary['video_renamed'] += 1
                if not dry_run:
                    os.makedirs(os.path.dirname(new_abs), exist_ok=True)
                    shutil.move(path, new_abs)
                    conn.execute('UPDATE file_contents SET file_path = ?, file_name = ?, '
                                 'file_type = ? WHERE id = ?',
                                 (new_rel, new_name, real_ext, it['file_id']))
                    try:
                        conn.execute('UPDATE exif_data SET file_name = ? WHERE file_id = ?',
                                     (new_name, it['file_id']))
                    except sqlite3.OperationalError:
                        pass
                    conn.execute("UPDATE import_items SET state = 'placed', kind = 'video', "
                                 'target_path = ?, new_filename = ?, phase_error = NULL '
                                 'WHERE id = ?', (new_abs, new_name, it['item_id']))
                    conn.commit()                  # the file has moved: record it now

            elif (any(m in error for m in DISK_FULL_MARKERS)
                    or (POSTER_MARKER in error and it['kind'] == 'video')):
                summary['disk_full_requeued'] += 1
                if not dry_run:
                    conn.execute("UPDATE import_items SET state = 'placed', phase_error = NULL "
                                 'WHERE id = ?', (it['item_id'],))

            elif any(m in error for m in TRUNCATION_MARKERS):
                summary['truncated_requeued'] += 1
                if not dry_run:
                    conn.execute("UPDATE import_items SET state = 'placed', phase_error = NULL "
                                 'WHERE id = ?', (it['item_id'],))
            else:
                summary['left_as_is'] += 1

        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Repair files imported under the wrong identity (by content).')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true',
                        help='Run even though an import execution is marked running.')
    parser.add_argument('--catalogs-root')
    args = parser.parse_args()

    catalogs = ([Catalog.get_by_id(args.catalog_id)] if args.catalog_id else Catalog.get_all())
    if args.catalog_id and not catalogs[0]:
        print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
        return 1

    total = Counter()
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id})")
        total.update(migrate_catalog(cat, dry_run=args.dry_run,
                                     catalogs_root=args.catalogs_root, force=args.force))

    verb = 'Would' if args.dry_run else 'Did'
    print(f'\n{verb}:')
    labels = {
        'appledouble_removed': 'remove AppleDouble metadata files from the catalog',
        'video_renamed': 'rename videos named like photos, and queue their previews',
        'truncated_requeued': 'queue damaged JPEGs to render as far as they go',
        'disk_full_requeued': 'queue previews that failed for want of disk space',
        'video_reference_skipped': 'leave videos named like photos alone (reference catalog)',
        'video_conflict': 'skip a video whose corrected name is already taken',
        'left_as_is': 'leave failures that are none of these as they are',
        'busy': 'skip catalogs with an import running',
        'unreachable': 'skip catalogs not reachable from here',
    }
    for key, label in labels.items():
        if total[key]:
            print(f'  {total[key]:6d}  {label}')
    if not args.dry_run and (total['video_renamed'] or total['truncated_requeued']
                             or total['disk_full_requeued']):
        print('\nStart the import again to render what was queued, then run '
              'migrate_group_live_photos.py to pair recovered clips with their stills.')
    return 1 if total['busy'] else 0


if __name__ == '__main__':
    sys.exit(main())
