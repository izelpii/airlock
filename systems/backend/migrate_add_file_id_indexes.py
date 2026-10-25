#!/usr/bin/env python3
"""
Build the file_id indexes a catalog is missing, outside a request.

    python migrate_add_file_id_indexes.py /catalogs/family
    python migrate_add_file_id_indexes.py --check /catalogs/family

WHY THIS EXISTS AS A SCRIPT. CatalogDatabase.init_db() creates these too, so a
catalog the backend opens will get them either way. But exif_data carries
raw_exif_json - 7.2 GB on the family catalog, inside a 27.8 GB database - and
building an index over it takes MINUTES while holding a write lock. Paying that
inside the first API call after an upgrade means:

  * that request appears to hang,
  * every other request queues behind the write lock, and
  * with eight gunicorn threads, several can enter init_db() at once and the
    losers raise "database is locked" after busy_timeout.

So run this once per catalog BEFORE restarting the backend on an upgrade. Then
init_db()'s CREATE INDEX IF NOT EXISTS is a no-op and nobody waits.

WHAT WAS MISSING AND WHY IT MATTERED. exif_data had no index on file_id, the
column it is joined on in 16 places in app.py and in most workflows. So
`LEFT JOIN exif_data ed ON fc.id = ed.file_id` planned as `SCAN ed LEFT-JOIN`:
a full scan of exif_data per row of the outer query. The location gallery is
where it became visible - 9,753 photographs at one location, each scanning
373,020 exif rows across 7.2 GB, and the page simply spun forever.

Asking the same question of every table joined on a file id found three more.
pet_detections had its index and face_detections did not, which is what showed
this was oversight rather than judgement.
"""

import argparse
import os
import sqlite3
import sys
import time

# table -> (index name, column). The column must LEAD the index; one that
# merely contains it cannot serve the lookup.
INDEXES = [
    ('exif_data',            'idx_exif_file_id',        'file_id'),
    ('face_detections',      'idx_face_det_file',       'file_id'),
    ('event_files',          'idx_event_files_file',    'file_id'),
    ('file_locations',       'idx_file_locations_file', 'file_id'),
]


def _tables(conn):
    return {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _leads_with(conn, table, column):
    """Names of indexes on `table` whose FIRST column is `column`."""
    found = []
    for (name,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
            (table,)):
        cols = [r[2] for r in conn.execute(f'PRAGMA index_info("{name}")')]
        if cols[:1] == [column]:
            found.append(name)
    return found


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('target_directory',
                    help="the catalog's Target Directory, holding "
                         'workflow_results.db')
    ap.add_argument('--check', action='store_true',
                    help='report what is missing and change nothing')
    args = ap.parse_args(argv)

    db_path = os.path.join(args.target_directory, 'workflow_results.db')
    if not os.path.exists(db_path):
        print(f'no database at {db_path}', file=sys.stderr)
        return 2

    size = os.path.getsize(db_path) / 1e9
    print(f'{db_path}  ({size:.1f} GB)')

    conn = sqlite3.connect(db_path, timeout=600)
    try:
        present = _tables(conn)
        built = skipped = absent = 0
        for table, index, column in INDEXES:
            if table not in present:
                print(f'  - {table:<18} table absent, nothing to index')
                absent += 1
                continue
            have = _leads_with(conn, table, column)
            if have:
                print(f'  = {table:<18} already served by {", ".join(have)}')
                skipped += 1
                continue
            if args.check:
                print(f'  ! {table:<18} MISSING an index leading with {column}')
                built += 1
                continue
            print(f'  + {table:<18} building {index} ...', end='', flush=True)
            t = time.time()
            conn.execute(f'CREATE INDEX IF NOT EXISTS "{index}" '
                         f'ON "{table}" ("{column}")')
            conn.commit()
            print(f' {time.time() - t:.1f}s')
            built += 1

        verb = 'would build' if args.check else 'built'
        print(f'{verb} {built}, already present {skipped}, table absent {absent}')
        return 1 if (args.check and built) else 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
