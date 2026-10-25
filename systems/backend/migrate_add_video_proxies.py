#!/usr/bin/env python3
"""
migrate_add_video_proxies.py
============================
Backfill playable proxies and poster frames for videos ingested before video
support existed.

Implements documentation/specs/video-spec.md section 11.

Videos already in a catalog have no row in alternative_versions at all, so they
are invisible in every gallery. This gives each one:

  a POSTER  - a square still, registered as TYPE THUMBNAIL 320, which is what
              makes a video appear in a thumbnail grid at all;
  a PROXY   - only when the source is not already browser-playable. Most phone
              and camera video is H.264/AAC in a faststart MP4 and needs
              nothing, so this usually writes no file (spec section 4).

Requires ffmpeg and ffprobe. Without them the script reports what it would have
done and exits without writing, rather than failing per file.

Usage
-----
    python backend/migrate_add_video_proxies.py --dry-run
    python backend/migrate_add_video_proxies.py
    python backend/migrate_add_video_proxies.py --catalog-id 54
    python backend/migrate_add_video_proxies.py --catalogs-root /mnt/catalogs

Refreshing
----------
    python backend/migrate_add_video_proxies.py --refresh-proxies [--dry-run]

also REBUILDS every existing proxy that is smaller than the current settings
would make it (lib.utilities.VIDEO_PROXY_MAX_EDGE, VIDEO_PROXY_CRF) - for when
those settings are raised, as they were from 720/CRF 28 to 1280/CRF 23. The new
file keeps the old one's name, so published albums keep pointing at it; publish
the albums again for their manifests to carry the new dimensions.

Idempotent: each of the two steps is skipped when its own row already exists,
so a video that got a poster but whose transcode failed is repaired on a re-run.
A video that needs no proxy writes no row and is therefore re-probed each time -
cheap, and the only way a failed transcode is ever distinguishable from one that
was never needed.
"""

import argparse
import os
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
    create_video_proxy,
    create_video_poster,
    _ffmpeg_available,
    _ffprobe_streams,
    VIDEO_PROXY_MAX_EDGE,
)


def _parse_creation_date(row):
    """Best-effort date for the cache/Videos/YYYY/MM/DD path."""
    from datetime import datetime
    raw = row['master_datetime'] if 'master_datetime' in row.keys() else None
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y:%m:%d %H:%M:%S'):
        try:
            return datetime.strptime(str(raw)[:19], fmt)
        except (TypeError, ValueError):
            continue
    # Fall back to the 14-digit stamp every ingested filename starts with.
    name = row['file_name'] or ''
    if len(name) >= 8 and name[:8].isdigit():
        try:
            return datetime.strptime(name[:8], '%Y%m%d')
        except ValueError:
            pass
    return None


def _refresh_proxy(conn, row, source, target_dir, dry_run):
    """
    Rebuild one video's proxy when it is smaller than the current settings
    would make it. Returns 'refreshed', 'current', 'kept' or 'failed'.
    """
    proxy = conn.execute(
        "SELECT id, alternative_file_path, alternative_file_name, width, height "
        "FROM alternative_versions WHERE original_file_id = ? "
        "AND UPPER(characteristic) LIKE '%VIDEO PROXY%' ORDER BY id LIMIT 1",
        (row['id'],)).fetchone()
    info = _ffprobe_streams(source)
    if not proxy or not info:
        return 'failed'
    want = min(max(info['width'], info['height']), VIDEO_PROXY_MAX_EDGE)
    have = max(proxy['width'] or 0, proxy['height'] or 0)
    # x264 needs even dimensions, so a copy can land a pixel short of `want`.
    if have >= want - 2:
        return 'current'
    if dry_run:
        print(f"    [dry-run] would rebuild {row['file_name']}: {have}px -> {want}px")
        return 'refreshed'

    stem = proxy['alternative_file_name'][:-len('_proxy.mp4')] \
        if proxy['alternative_file_name'].endswith('_proxy.mp4') else None
    new = create_video_proxy(source, target_dir, creation_date=_parse_creation_date(row),
                             name_stem=stem)
    if new is None:
        print(f"    [fail] could not rebuild the proxy for {row['file_name']}")
        return 'failed'
    if not new.get('path'):
        # Small and browser-ready enough to play as-is. The existing proxy
        # still works, and a gallery needs one, so it stays.
        print(f"    [kept] {row['file_name']}: original plays as-is; existing proxy kept")
        return 'kept'
    rel = os.path.relpath(new['path'], target_dir)
    old_abs = os.path.join(target_dir, proxy['alternative_file_path'])
    conn.execute(
        'UPDATE alternative_versions SET alternative_file_path = ?, alternative_file_name = ?, '
        'alternative_file_size = ?, characteristic = ?, width = ?, height = ? WHERE id = ?',
        (rel, new['name'], new['size'], new['characteristic'], new['width'], new['height'],
         proxy['id']))
    conn.commit()
    if os.path.abspath(old_abs) != os.path.abspath(new['path']) and os.path.exists(old_abs):
        os.remove(old_abs)
    print(f"    [rebuilt] {row['file_name']}: {have}px -> {new['width']}x{new['height']} "
          f"({new['size'] // 1024} KB)")
    return 'refreshed'


def migrate_catalog(catalog, dry_run=False, catalogs_root=None, refresh=False):
    summary = {'passthrough': 0, 'proxied': 0, 'posters': 0, 'refreshed': 0,
               'skipped': 0, 'missing': 0, 'failed': 0}

    if not catalog.target_directory:
        print(f"  [skip] Catalog '{catalog.name}' has no target_directory.")
        return summary

    target_dir = resolve_catalog_dir(catalog, catalogs_root)
    if not target_dir:
        print(f"  [skip] {catalog.target_directory} not reachable from here.")
        return summary

    db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(db_path):
        print(f"  [skip] No workflow_results.db at {db_path}.")
        return summary

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row

    cols = {r[1] for r in conn.execute('PRAGMA table_info(file_contents)')}
    # A file removed from the catalog gets nothing new made for it.
    live = ' WHERE removed_at IS NULL' if 'removed_at' in cols else ''
    rows = [r for r in conn.execute(
        'SELECT id, file_name, file_path, master_datetime FROM file_contents' + live
    ).fetchall() if is_video_file(r['file_name'] or '')]
    print(f"  {len(rows)} video(s) in this catalog.")

    for row in rows:
        name = row['file_name']
        # The two steps are checked SEPARATELY. They used to share one guard -
        # "has a proxy row OR a poster row" - which meant a video whose poster
        # was written and whose transcode then FAILED counted as done, and no
        # re-run could ever repair it. That is not hypothetical: an ffmpeg bug
        # in the scale filter left 54 videos in this state, postered and
        # unplayable (see create_video_proxy).
        have = {(r['characteristic'] or '').upper()
                for r in conn.execute(
                    'SELECT characteristic FROM alternative_versions '
                    'WHERE original_file_id = ?', (row['id'],)).fetchall()}
        has_proxy = any('VIDEO PROXY' in c for c in have)
        has_poster = any('TYPE THUMBNAIL' in c for c in have)
        if refresh and has_proxy:
            source = row['file_path']
            if source and not os.path.isabs(source):
                source = os.path.join(target_dir, source)
            if not source or not os.path.exists(source):
                print(f"    [missing] {name}")
                summary['missing'] += 1
                continue
            outcome = _refresh_proxy(conn, row, source, target_dir, dry_run)
            if outcome == 'refreshed':
                summary['refreshed'] += 1
            elif outcome == 'failed':
                summary['failed'] += 1
            if has_poster:
                continue
        if has_proxy and has_poster:
            print(f"    [have] {name}")
            summary['skipped'] += 1
            continue

        source = row['file_path']
        if source and not os.path.isabs(source):
            source = os.path.join(target_dir, source)
        if not source or not os.path.exists(source):
            print(f"    [missing] {name}")
            summary['missing'] += 1
            continue

        if dry_run:
            print(f"    [dry-run] would process {name}")
            summary['proxied'] += 1
            continue

        created = _parse_creation_date(row)

        # A passthrough video writes NO row, so there is nothing to record that
        # it was examined - it is re-probed on every run. That costs one ffprobe
        # and a header read, which is the price of being able to repair a failed
        # transcode at all.
        proxy = None if has_proxy else create_video_proxy(
            source, target_dir, creation_date=created)
        if has_proxy:
            pass
        elif proxy is None:
            print(f"    [fail] proxy step failed for {name}")
            summary['failed'] += 1
        elif proxy.get('path'):
            rel = os.path.relpath(proxy['path'], target_dir)
            conn.execute(
                'INSERT INTO alternative_versions (original_file_id, alternative_file_path, '
                'alternative_file_name, alternative_file_size, characteristic, width, height, '
                'rung) VALUES (?,?,?,?,?,?,?,?)',
                (row['id'], rel, proxy['name'], proxy['size'],
                 proxy['characteristic'], proxy['width'], proxy['height'],
                 'video_proxy'))
            print(f"    [{proxy['action']}] {name} -> {proxy['name']}")
            summary['proxied'] += 1
        else:
            print(f"    [passthrough] {name} already browser-playable")
            summary['passthrough'] += 1

        poster = None if has_poster else create_video_poster(
            source, target_dir, creation_date=created)
        if poster:
            rel = os.path.relpath(poster['path'], target_dir)
            conn.execute(
                'INSERT INTO alternative_versions (original_file_id, alternative_file_path, '
                'alternative_file_name, alternative_file_size, characteristic, width, height, '
                'rung) VALUES (?,?,?,?,?,?,?,?)',
                (row['id'], rel, poster['name'], poster['size'],
                 poster['characteristic'], poster['width'], poster['height'],
                 'video_poster'))
            print(f"    [poster] {name} -> {poster['name']}")
            summary['posters'] += 1
        conn.commit()

    conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Backfill video proxies and poster frames.')
    parser.add_argument('--catalog-id', type=int)
    parser.add_argument('--dry-run', action='store_true')
    parser.add_argument('--refresh-proxies', action='store_true',
                        help='also rebuild existing proxies smaller than the current '
                             'settings would make them')
    parser.add_argument('--catalogs-root',
                        help='Directory holding catalog folders on this machine, when the '
                             'stored target_directory is a container path.')
    args = parser.parse_args()

    if not args.dry_run and not _ffmpeg_available():
        print('\nffmpeg and ffprobe are required. They ship in the backend image '
              '(see systems/backend/Dockerfile); run this inside that container, '
              'or install them locally.', file=sys.stderr)
        return 1

    if args.catalog_id:
        catalog = Catalog.get_by_id(args.catalog_id)
        if not catalog:
            print(f'Error: catalog {args.catalog_id} not found.', file=sys.stderr)
            return 1
        catalogs = [catalog]
    else:
        catalogs = Catalog.get_all()

    total = {'passthrough': 0, 'proxied': 0, 'posters': 0, 'refreshed': 0,
             'skipped': 0, 'missing': 0, 'failed': 0}
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id}), target={cat.target_directory}")
        result = migrate_catalog(cat, dry_run=args.dry_run,
                                 catalogs_root=args.catalogs_root,
                                 refresh=args.refresh_proxies)
        for k in total:
            total[k] += result.get(k, 0)

    verb = 'Would process' if args.dry_run else 'Processed'
    print(f"\n{verb}: {total['proxied']} proxied, {total['passthrough']} already playable, "
          f"{total['posters']} poster(s)"
          + (f", {total['refreshed']} rebuilt at the current size." if args.refresh_proxies
             else '.'))
    for key, label in (('skipped', 'Already had derivatives'),
                       ('missing', 'Source file missing'),
                       ('failed', 'Failed')):
        if total[key]:
            print(f"  {label}: {total[key]}")
    return 0


if __name__ == '__main__':
    sys.exit(main())
