#!/usr/bin/env python3
"""
Queue photos for face re-detection.

Detection results are cached twice over: file_contents.face_detection_scanned_at
marks an image as done, and the auto-detect workflow skips anything already
marked. So a change that makes detection *better* has no effect on photos that
were already scanned - they are simply never looked at again.

This clears that state so the next Auto Detect Faces run reprocesses them.

What it touches:
  - file_contents.face_detection_scanned_at  -> NULL (the image gets rescanned)
  - face_detections WHERE source='Automatic' -> deleted
  - ai_face_contents rows orphaned by that   -> deleted

What it never touches:
  - face_detections with source 'Manual' or 'Auto-Confirmed', and any person
    assignment attached to them. The workflow preserves these too, and skips
    new boxes that overlap them.

Cropped face JPEGs under <catalog>/cache/faces-ai/ are left on disk. They are
regenerated under new ids, so the old ones become unreferenced; --list-orphan-crops
prints them so you can remove them yourself.

Dry run by default. Pass --apply to write.

    python3 rescan_faces.py --catalog family
    python3 rescan_faces.py --catalog family --apply
    python3 rescan_faces.py --catalog family --file-id 353 --apply
"""

import argparse
import os
import sqlite3
import sys


def resolve_db(catalog, explicit_db):
    if explicit_db:
        return explicit_db
    here = os.path.dirname(os.path.abspath(__file__))
    root = os.path.abspath(os.path.join(here, '..', '..'))
    return os.path.join(root, 'catalogs', catalog, 'workflow_results.db')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--catalog', default='family', help='Catalog directory name')
    ap.add_argument('--db', help='Path to workflow_results.db (overrides --catalog)')
    ap.add_argument('--file-id', type=int, action='append',
                    help='Only this file id; repeatable. Default: every image.')
    ap.add_argument('--apply', action='store_true', help='Actually write the changes')
    ap.add_argument('--list-orphan-crops', action='store_true',
                    help='Print face crop files no longer referenced by any row')
    args = ap.parse_args()

    db = resolve_db(args.catalog, args.db)
    if not os.path.exists(db):
        print(f'No database at {db}')
        return 1

    conn = sqlite3.connect(db, timeout=60)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()

    where, params = '', []
    if args.file_id:
        marks = ','.join('?' * len(args.file_id))
        where = f' WHERE file_id IN ({marks})'
        params = list(args.file_id)

    scanned = cur.execute(
        'SELECT COUNT(*) FROM file_contents WHERE face_detection_scanned_at IS NOT NULL'
        + (f" AND id IN ({','.join('?' * len(args.file_id))})" if args.file_id else ''),
        params).fetchone()[0]
    auto = cur.execute(
        "SELECT COUNT(*) FROM face_detections WHERE source='Automatic'"
        + (f" AND file_id IN ({','.join('?' * len(args.file_id))})" if args.file_id else ''),
        params).fetchone()[0]
    protected = cur.execute(
        "SELECT COUNT(*) FROM face_detections WHERE source IN ('Manual','Auto-Confirmed')"
        + (f" AND file_id IN ({','.join('?' * len(args.file_id))})" if args.file_id else ''),
        params).fetchone()[0]

    print(f'Database : {db}')
    print(f'Scope    : {"file ids " + ",".join(map(str, args.file_id)) if args.file_id else "entire catalog"}')
    print()
    print(f'  images marked scanned        : {scanned:>6}  -> will be cleared')
    print(f'  Automatic face detections    : {auto:>6}  -> will be deleted')
    print(f'  Manual / Auto-Confirmed      : {protected:>6}  -> KEPT')
    print()

    if args.list_orphan_crops:
        rows = cur.execute('''
            SELECT a.id, a.file_path FROM ai_face_contents a
            LEFT JOIN face_detections d ON a.face_detection_id = d.id
            WHERE d.id IS NULL
        ''').fetchall()
        print(f'Unreferenced crop rows on disk: {len(rows)}')
        for r in rows:
            print(f'  {r["file_path"]}')
        print()

    if not args.apply:
        print('Dry run - nothing written. Re-run with --apply.')
        conn.close()
        return 0

    cur.execute(
        "DELETE FROM ai_face_contents WHERE face_detection_id IN ("
        "SELECT id FROM face_detections WHERE source='Automatic'"
        + (f" AND file_id IN ({','.join('?' * len(args.file_id))})" if args.file_id else '')
        + ')', params)
    crops_gone = cur.rowcount

    cur.execute(
        "DELETE FROM face_detections WHERE source='Automatic'"
        + (f" AND file_id IN ({','.join('?' * len(args.file_id))})" if args.file_id else ''),
        params)
    dets_gone = cur.rowcount

    cur.execute(
        'UPDATE file_contents SET face_detection_scanned_at = NULL'
        + (f" WHERE id IN ({','.join('?' * len(args.file_id))})" if args.file_id else ''),
        params)
    cleared = cur.rowcount

    # Rows left dangling by earlier deletes: this schema declares ON DELETE
    # CASCADE, but SQLite only enforces it when PRAGMA foreign_keys is on, and
    # the application does not turn it on. Clean them up while we are here.
    cur.execute('''
        DELETE FROM ai_face_contents WHERE face_detection_id NOT IN
        (SELECT id FROM face_detections)
    ''')
    orphans_gone = cur.rowcount

    conn.commit()
    conn.close()

    print(f'  cleared scanned marks        : {cleared}')
    print(f'  deleted Automatic detections : {dets_gone}')
    print(f'  deleted their crop rows      : {crops_gone}')
    print(f'  deleted pre-existing orphans : {orphans_gone}')
    print()
    print('Now run Auto Detect Faces. Old crop JPEGs under cache/faces-ai/ are')
    print('unreferenced; --list-orphan-crops prints them if you want to remove them.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
