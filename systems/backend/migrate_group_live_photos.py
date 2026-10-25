#!/usr/bin/env python3
"""
migrate_group_live_photos.py
============================
Group iPhone Live Photos that were imported before the system understood them.

A Live Photo is a still and a ~3 s .MOV. Imported before this feature they
became a photo AND a video: a tile on each page, two event members, and a video
preview to transcode for every one of them - 35,000 clips on oldserver's family
import, a third of its video render queue. This links each clip to its still
(image_groups kind 'live_photo', the still primary), exactly as the importer
now does for new files.

WHAT IT DOES
  * pairs a still with a clip only when both carry the same Apple
    ContentIdentifier (lib/grouping.py, find_live_photos) - never by name;
  * records an image_groups row and sets image_group_id on both files;
  * repoints event and album links from the clip to the still;
  * marks a clip the import has not rendered yet as a group secondary, so a
    resumed import skips its preview (import_items.group_role).

WHAT IT DOES NOT DO
  * touch either original, or delete a preview already made for a clip.
  * run while an import is running in the catalog: the import's own writer
    holds the database, and its render queue is in memory. Stop the import,
    run this, then start the import again.

Usage (exiftool lives in the backend container):
    podman exec workflow-backend python /app/migrate_group_live_photos.py --dry-run \
        --keys-cache /catalogs/live-keys.json
    podman exec workflow-backend python /app/migrate_group_live_photos.py --catalog-id 3 \
        --keys-cache /catalogs/live-keys.json      # reuses the dry run's reading
"""

import argparse
import json
import os
import re
import shutil as _shutil
import sqlite3
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(_HERE))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.join(_HERE, 'workflows'))

from catalog import Catalog  # noqa: E402
from migration_utils import resolve_catalog_dir  # noqa: E402
from lib.grouping import read_match_keys, find_live_photos  # noqa: E402
from lib.utilities import is_image_file, is_video_file  # noqa: E402

# The catalog's own "<14 digits>-<10 hex>-" prefix: the original name follows it.
_CATALOG_PREFIX = re.compile(r'^\d{14}-[0-9a-fA-F]{10}-')

# exiftool is asked in slices, so a long run shows progress and one unreadable
# file costs a slice rather than everything.
CHUNK = 1000


def _resolve(stored, root):
    return None if not stored else (stored if os.path.isabs(stored)
                                    else os.path.join(root, stored))


def _original_stem(file_name):
    return os.path.splitext(_CATALOG_PREFIX.sub('', file_name or '', count=1))[0].lower()


def _has_table(conn, name):
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                        (name,)).fetchone() is not None


def _running_import(conn):
    """An import execution still marked running, or None."""
    if not (_has_table(conn, 'workflow_executions') and _has_table(conn, 'workflows')):
        return None
    for r in conn.execute(
            "SELECT e.id, e.workflow_name, w.python_script FROM workflow_executions e "
            "JOIN workflows w ON w.id = e.workflow_id WHERE e.status = 'running'"):
        if 'import' in os.path.basename(r['python_script'] or ''):
            return r
    return None


def _repoint(conn, table, column, secondary_id, primary_id):
    """Move a link from the secondary to the primary, without creating a duplicate."""
    if not _has_table(conn, table):
        return 0
    conn.execute(f'UPDATE OR IGNORE {table} SET {column} = ? WHERE {column} = ?',
                 (primary_id, secondary_id))
    return conn.execute(f'DELETE FROM {table} WHERE {column} = ?', (secondary_id,)).rowcount


def _read_keys(paths, cache_path, log):
    """
    ContentIdentifier for every path, from the cache file when it has them.

    Reading them is the slow part - three hours for 150k files on oldserver's USB
    disks - so a dry run's reading is kept for the real run, and a run cut
    short does not start from nothing.
    """
    keys = {}
    if cache_path and os.path.exists(cache_path):
        with open(cache_path) as f:
            keys = json.load(f)
        log(f'    {len(keys)} identifier(s) from {cache_path}')
    todo = [p for p in paths if p not in keys]
    if todo:
        log(f'    reading ContentIdentifier from {len(todo)} file(s)...')
    for i in range(0, len(todo), CHUNK):
        chunk = todo[i:i + CHUNK]
        got = read_match_keys(chunk, log=log)
        if not got:
            # exiftool exits non-zero when ANY file in a batch is unreadable,
            # and read_match_keys then returns nothing for all of them. One
            # file a time, so one bad file costs one file (first seen on
            # oldserver: 1,000 files lost to one).
            log(f'      retrying {len(chunk)} file(s) one at a time')
            for p in chunk:
                got.update(read_match_keys([p], log=lambda *a: None))
        # Only what matching needs, so the cache stays small.
        keys.update({p: {'content_id': (k or {}).get('content_id')} for p, k in got.items()})
        log(f'      {min(i + CHUNK, len(todo))}/{len(todo)}')
        if cache_path and (i // CHUNK) % 10 == 9:
            _save_keys(keys, cache_path)
    if cache_path and todo:
        _save_keys(keys, cache_path)
    return keys


def _save_keys(keys, cache_path):
    tmp = cache_path + '.partial'
    with open(tmp, 'w') as f:
        json.dump(keys, f)
    os.replace(tmp, cache_path)


def migrate_catalog(catalog, dry_run=False, catalogs_root=None, force=False, log=print,
                    keys_cache=None):
    summary = {'groups': 0, 'links_moved': 0, 'renders_skipped': 0,
               'candidates': 0, 'unreachable': 0, 'busy': 0}

    target_dir = resolve_catalog_dir(catalog, catalogs_root) if catalog.target_directory else None
    db_path = os.path.join(target_dir, 'workflow_results.db') if target_dir else None
    if not db_path or not os.path.exists(db_path):
        log(f'  [skip] {catalog.target_directory} not reachable from here.')
        summary['unreachable'] = 1
        return summary
    # Originals resolve against the source library in a reference catalog.
    originals_root = (getattr(catalog, 'source_root', None)
                      if getattr(catalog, 'ingest_mode', 'copy') == 'reference'
                      else None) or target_dir

    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute('PRAGMA busy_timeout=30000')
    conn.row_factory = sqlite3.Row
    try:
        running = _running_import(conn)
        if running and not force:
            log(f"  [skip] import '{running['workflow_name']}' (execution {running['id']}) "
                f'is running. Stop it first, then run this again (or --force if that '
                f'row is stale).')
            summary['busy'] = 1
            return summary

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

        # Only names that exist as both a still and a video are worth asking
        # exiftool about: 84k files instead of 300k on oldserver. The name is a
        # filter, never the rule - the pairing itself is the ContentIdentifier.
        rows = [dict(r) for r in conn.execute(
            'SELECT id, file_name, file_path FROM file_contents '
            'WHERE image_group_id IS NULL AND removed_at IS NULL')]
        stills, videos = {}, {}
        for r in rows:
            name = r['file_name'] or ''
            if is_video_file(name):
                videos.setdefault(_original_stem(name), []).append(r)
            elif is_image_file(name):
                stills.setdefault(_original_stem(name), []).append(r)
        by_path = {}
        for stem in stills.keys() & videos.keys():
            for r in stills[stem] + videos[stem]:
                p = _resolve(r['file_path'], originals_root)
                if p and os.path.exists(p):
                    by_path[p] = r
        summary['candidates'] = len(by_path)
        if not by_path:
            log('    no ungrouped still+video names to consider')
            return summary

        paths = sorted(by_path)
        keys = _read_keys(paths, keys_cache, log)
        groups = find_live_photos(paths, keys, is_image_file, is_video_file, log=log)
        if not groups:
            log('    no Live Photos found')
            return summary

        has_items = _has_table(conn, 'import_items')
        if has_items:
            # import_items has millions of rows and no index on file_id, so each
            # update below was a full scan - days for 37k groups on oldserver. One
            # scan to build this instead; kept, it is small.
            log('    indexing import_items by file_id...')
            conn.execute('CREATE INDEX IF NOT EXISTS idx_ii_file_id ON import_items (file_id)')
            conn.commit()
        touched_events = set()
        for n, g in enumerate(groups, 1):
            still, clip = by_path[g['primary']], by_path[g['secondary']]
            summary['groups'] += 1
            if dry_run:
                continue
            gid = conn.execute(
                'INSERT INTO image_groups (primary_file_id, kind, match_rule, match_evidence) '
                'VALUES (?, ?, ?, ?)',
                (still['id'], g['kind'], g['match_rule'], json.dumps(g['match_evidence']))
            ).lastrowid
            conn.execute('UPDATE file_contents SET image_group_id = ? WHERE id IN (?, ?)',
                         (gid, still['id'], clip['id']))

            if _has_table(conn, 'event_files'):
                touched_events.update(r[0] for r in conn.execute(
                    'SELECT event_id FROM event_files WHERE file_id = ?', (clip['id'],)))
            for table in ('event_files', 'section_photos'):
                summary['links_moved'] += _repoint(conn, table, 'file_id', clip['id'], still['id'])

            if has_items:
                key = f'live-{gid}'
                conn.execute("UPDATE import_items SET group_key = ?, group_role = 'primary' "
                             'WHERE file_id = ?', (key, still['id']))
                conn.execute("UPDATE import_items SET group_key = ?, group_role = 'secondary' "
                             'WHERE file_id = ?', (key, clip['id']))
                summary['renders_skipped'] += conn.execute(
                    "UPDATE import_items SET state = 'render_skipped', "
                    "updated_at = CURRENT_TIMESTAMP WHERE file_id = ? AND state = 'placed'",
                    (clip['id'],)).rowcount
            if n % 500 == 0:
                conn.commit()
                log(f'      grouped {n}/{len(groups)}')

        # An event that lost a clip but already had its still is one member
        # shorter: keep the stored count honest.
        for eid in touched_events:
            conn.execute('UPDATE events SET file_count = (SELECT COUNT(*) FROM event_files '
                         'WHERE event_id = ?) WHERE id = ?', (eid, eid))
        if not dry_run:
            conn.commit()
    finally:
        conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Link Live Photo clips already imported to their stills.')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--force', action='store_true',
                        help='Run even though an import execution is marked running.')
    parser.add_argument('--catalogs-root')
    parser.add_argument('--keys-cache',
                        help='JSON file to keep the identifiers read, and reuse them '
                             '(a dry run then makes the real run fast).')
    args = parser.parse_args()

    if _shutil.which('exiftool') is None:
        print('exiftool is not installed here, so nothing can be matched.\n'
              'In this deployment it lives in the backend container:\n'
              '    podman exec workflow-backend python /app/migrate_group_live_photos.py',
              file=sys.stderr)
        return 1

    catalogs = ([Catalog.get_by_id(args.catalog_id)] if args.catalog_id else Catalog.get_all())
    if args.catalog_id and not catalogs[0]:
        print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
        return 1

    total = {'groups': 0, 'links_moved': 0, 'renders_skipped': 0, 'unreachable': 0, 'busy': 0}
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id})")
        cache = None
        if args.keys_cache:
            cache = (args.keys_cache if len(catalogs) == 1
                     else f'{args.keys_cache}.{cat.id}')
        result = migrate_catalog(cat, dry_run=args.dry_run,
                                 catalogs_root=args.catalogs_root, force=args.force,
                                 keys_cache=cache)
        for k in total:
            total[k] += result.get(k, 0)

    print(f"\n{'Would group' if args.dry_run else 'Grouped'}: {total['groups']} Live Photo(s).")
    if total['links_moved']:
        print(f"  Event/album links moved to the still: {total['links_moved']}")
    if total['renders_skipped']:
        print(f"  Clip previews the import will no longer make: {total['renders_skipped']}")
    if total['busy']:
        print(f"  Catalogs skipped because an import is running: {total['busy']}")
    if total['unreachable']:
        print(f"  Catalogs not reachable from here: {total['unreachable']}")
    return 1 if total['busy'] else 0


if __name__ == '__main__':
    sys.exit(main())
