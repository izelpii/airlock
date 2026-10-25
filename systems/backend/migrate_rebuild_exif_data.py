#!/usr/bin/env python3
"""
Rebuild exif_data to escape damaged b-tree leaf linkage.

    python migrate_rebuild_exif_data.py --check  /catalogs/family
    python migrate_rebuild_exif_data.py --rebuild --skip bad.json /catalogs/family

RUN WITH THE BACKEND STOPPED. This drops and renames a table.

WHY. The family catalog's exif_data cannot be scanned past id 301044. Every
row reads fine by rowid - a lookup descends from the root to its leaf - but a
range scan walks leaf page to leaf page and never comes back. Measured:

    COUNT(*) WHERE id <= 301000   301,000 rows in 11.27 s
    COUNT(*) WHERE id <= 302000   never returns

That is damaged leaf linkage, residue of the SSD failure this catalog
survived, and it is independent of the row contents: it persisted after
migrate_split_raw_exif.py moved 7.2 GB of raw_exif_json out and cleared the
column. Three rows - 301044, 301045, 301046 - cannot be read at all.

IT IS ALSO WHY `CREATE INDEX ON exif_data (file_id)` RAN FOR HOURS WITHOUT
FINISHING, because CREATE INDEX needs a full table scan. Without that index
the location gallery plans as `SCAN ed LEFT-JOIN` - one full scan of exif_data
per row of the outer query - so a location holding 9,753 photographs never
loads. The index cannot be built until the table can be scanned, and the table
cannot be scanned until it is rebuilt.

HOW. Copy every row into a fresh table BY ROWID, walking the id space
arithmetically - 1, 2, 3 ... MAX(id) - so no scan of the damaged table ever
happens. Then drop the old table and rename. Ids are preserved, which matters:
file_contents.exif_data_id points at them.

WHAT IS LOST. The rows named by --skip, because they cannot be read by any
means. Their photographs keep their files, their thumbnails and their
promoted columns elsewhere; what goes is one exif_data row each, which
re-extraction from the original file restores.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

BATCH = 2000
NEW = 'exif_data_rebuilt'
OLD = 'exif_data_damaged'


def _sql_for(conn, name, kind='table'):
    row = conn.execute("SELECT sql FROM sqlite_master WHERE type=? AND name=?",
                       (kind, name)).fetchone()
    return row[0] if row else None


def _indexes_on(conn, table):
    """(name, sql) for every index the schema declares on `table`."""
    return [(n, s) for n, s in conn.execute(
        "SELECT name, sql FROM sqlite_master "
        "WHERE type='index' AND tbl_name=? AND sql IS NOT NULL", (table,))]


def check(conn, path, skip):
    top = conn.execute('SELECT MAX(id) FROM exif_data').fetchone()[0] or 0
    print(f'  exif_data MAX(id)   {top}')
    print(f'  unreadable ids      {sorted(int(i) for i in skip) or "none given"}')
    print(f'  indexes to recreate {[n for n, _ in _indexes_on(conn, "exif_data")]}')
    t = time.time()
    n = 0
    for k in range(1, min(top, 2000) + 1):
        if conn.execute('SELECT 1 FROM exif_data WHERE id = ?', (k,)).fetchone():
            n += 1
    rate = 2000 / max(time.time() - t, 1e-9)
    print(f'  rowid lookups       {rate:.0f}/s -> about {top / rate:.0f}s '
          f'for the whole table')
    return 0


def rebuild(conn, path, skip):
    skipset = {int(i) for i in skip}
    create = _sql_for(conn, 'exif_data')
    if not create:
        print('  no exif_data table', file=sys.stderr)
        return 2
    indexes = _indexes_on(conn, 'exif_data')
    top = conn.execute('SELECT MAX(id) FROM exif_data').fetchone()[0] or 0
    cols = [r[1] for r in conn.execute('PRAGMA table_info(exif_data)')]
    print(f'  {len(cols)} columns, ids 1..{top}, skipping {sorted(skipset)}')

    conn.execute(f'DROP TABLE IF EXISTS {NEW}')
    # Same columns, same declared types, same constraints - only the name
    # differs. Taken from sqlite_master rather than written out here, so a
    # column added since this script was written is carried across.
    conn.execute(create.replace('exif_data', NEW, 1))
    conn.commit()

    collist = ', '.join(f'"{c}"' for c in cols)
    marks = ', '.join('?' * len(cols))
    insert = f'INSERT INTO {NEW} ({collist}) VALUES ({marks})'

    copied = absent = 0
    started = time.time()
    i = 1
    while i <= top:
        hi = min(i + BATCH - 1, top)
        rows = []
        for k in range(i, hi + 1):
            if k in skipset:
                continue
            r = conn.execute(f'SELECT {collist} FROM exif_data WHERE id = ?',
                             (k,)).fetchone()
            if r is None:
                absent += 1
            else:
                rows.append(tuple(r))
        if rows:
            conn.executemany(insert, rows)
            conn.commit()
            copied += len(rows)
        if (hi // BATCH) % 10 == 0 or hi == top:
            print(f'    through id {hi}: {copied} copied '
                  f'({time.time() - started:.0f}s)', flush=True)
        i = hi + 1

    print(f'  copied {copied} rows in {time.time() - started:.1f}s '
          f'({absent} ids had no row, {len(skipset)} skipped)')

    # Prove the NEW table scans before destroying the old one. If this hangs
    # or disagrees, nothing has been dropped yet and the catalog is unharmed.
    t = time.time()
    n = conn.execute(f'SELECT COUNT(*) FROM {NEW}').fetchone()[0]
    print(f'  COUNT(*) on the rebuilt table: {n} in {time.time() - t:.2f}s')
    if n != copied:
        print(f'  REFUSING to swap: counted {n}, copied {copied}',
              file=sys.stderr)
        return 2

    print('  swapping')
    # RENAME THE DAMAGED TABLE ASIDE; DO NOT DROP IT.
    #
    # DROP TABLE has to walk the table's b-tree to put its pages on the
    # freelist, and that traversal goes straight through the damaged leaf
    # linkage this whole script exists to escape. Tried: the drop sat at 97%
    # CPU and never returned, exactly like every other scan of this table. A
    # rename only edits sqlite_master, so it costs nothing and touches no
    # page of the table itself.
    #
    # The cost is that the damaged pages stay in the file. They are
    # unreferenced by anything the application queries, and reclaiming them
    # would mean VACUUM, which is a whole-database rewrite and would hit the
    # same corruption. Space on disk is the cheaper thing to give up.
    #
    # legacy_alter_table, so RENAME does not try to rewrite references to
    # exif_data in other tables' definitions while the name is in flux.
    conn.execute('PRAGMA legacy_alter_table = ON')
    conn.execute(f'DROP TABLE IF EXISTS {OLD}')
    conn.execute(f'ALTER TABLE exif_data RENAME TO {OLD}')
    conn.execute(f'ALTER TABLE {NEW} RENAME TO exif_data')
    conn.execute('PRAGMA legacy_alter_table = OFF')
    print(f'    the damaged table is kept as {OLD}; it is referenced by '
          f'nothing and must not be dropped - the drop is what hangs')
    for name, sql in indexes:
        # The renamed-aside table still carries these index names, and an
        # index name is unique across the database. Dropping an index only
        # frees the INDEX's pages, not the table's, so it does not hit the
        # damaged chain.
        print(f'    recreating {name}')
        conn.execute(f'DROP INDEX IF EXISTS "{name}"')
        conn.execute(sql)
    conn.commit()

    t = time.time()
    n = conn.execute('SELECT COUNT(*) FROM exif_data').fetchone()[0]
    print(f'  COUNT(*) on exif_data: {n} in {time.time() - t:.2f}s')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('target_directory')
    ap.add_argument('--check', action='store_true')
    ap.add_argument('--rebuild', action='store_true')
    ap.add_argument('--skip', metavar='FILE',
                    help='JSON list of unreadable exif_data ids to drop')
    args = ap.parse_args(argv)

    path = os.path.join(args.target_directory, 'workflow_results.db')
    if not os.path.exists(path):
        print(f'no database at {path}', file=sys.stderr)
        return 2
    print(path)

    skip = []
    if args.skip:
        with open(args.skip) as fh:
            skip = [int(i) for i in json.load(fh)]

    conn = sqlite3.connect(path, timeout=3600)
    conn.execute('PRAGMA cache_size = -256000')
    conn.execute('PRAGMA temp_store = MEMORY')
    try:
        if args.rebuild:
            return rebuild(conn, path, skip)
        return check(conn, path, skip)
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
