#!/usr/bin/env python3
"""
Move exif_data.raw_exif_json into its own table, exif_raw.

    python migrate_split_raw_exif.py --check    /catalogs/family
    python migrate_split_raw_exif.py --copy     /catalogs/family
    python migrate_split_raw_exif.py --clear    /catalogs/family
    python migrate_split_raw_exif.py --vacuum   /catalogs/family

RUN WITH THE BACKEND STOPPED. Every phase takes a write lock for a long time.

WHY. raw_exif_json averages 22 KB a row and 7.2 GB across a 361 K-file catalog
(contents-spec section 4). Inside exif_data, every query that reads whole rows
pays for it whether it wants the blob or not. Measured on homeserver's family
catalog (27.8 GB):

    reading exif_data.file_id alone      626,000 rows/s
    COUNT(*) over the SAME rows          exceeds 360 s

Identical row count. The only difference is whether the read has to traverse
the blob. It is also why CREATE INDEX on exif_data(file_id) ran over an hour
there without finishing, which left the location gallery with no index to use
and made a location of 9,753 photographs never load.

Almost nothing reads it. /photos does not, and neither does the single-photo
view - both use the promoted columns (camera_make, lens_model, iso, ...). The
blob is effectively write-only.

THE PHASES ARE SEPARATE ON PURPOSE, because --copy is safe and --clear is not
reversible without the copy having worked.

  --check   report sizes and how much has been moved. Changes nothing.
  --copy    INSERT the blobs into exif_raw. The old column keeps its data, so
            this phase is safe to interrupt and safe to re-run: readers
            COALESCE the two, so either source answers correctly throughout.
  --clear   NULL the old column, once --copy covers every row. This phase
            VERIFIES that first and refuses otherwise. Space is not returned
            to the filesystem until --vacuum, but scans get fast immediately:
            the freed overflow pages leave the row, so a table scan stops
            walking them.
  --vacuum  rebuild the file to return the freed space. Needs free disk space
            roughly equal to the database and takes a long time; purely a
            housekeeping step, and the performance win is already in by then.
"""

import argparse
import json
import os
import sqlite3
import sys
import time

BATCH = 2000


def _not_in(skip):
    """
    ` AND id NOT IN (...)`, or nothing.

    Inlined rather than parameterised because it joins a fragment that already
    carries its own placeholders in a fixed order; the values are ints this
    module parsed itself, so there is nothing to quote.
    """
    if not skip:
        return ' '
    return ' AND id NOT IN (%s) ' % ','.join(str(int(i)) for i in skip)


def _not_in_ed(skip):
    """_not_in, for a query that aliases exif_data as `ed`."""
    if not skip:
        return ' '
    return ' AND ed.id NOT IN (%s) ' % ','.join(str(int(i)) for i in skip)


def _one(conn, sql, args=()):
    return conn.execute(sql, args).fetchone()[0]


def _ensure_table(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS exif_raw (
            exif_id       INTEGER PRIMARY KEY,
            raw_exif_json TEXT,
            FOREIGN KEY (exif_id) REFERENCES exif_data (id) ON DELETE CASCADE
        )
    ''')
    conn.commit()


def _counts(conn):
    """
    (blobs still in the old column, rows already in exif_raw).

    The first number is a scan of exif_data and there is no way round that -
    it is the only place the answer lives. The second is a count over a
    primary key, so it is instant.

    What this does NOT do is ask "which rows are missing from exif_raw" with a
    correlated NOT EXISTS. That reads as the obvious phrasing and it is a
    trap: paired with LIMIT it rescans exif_data from the beginning on every
    batch, so copying 361 K rows in batches of 2,000 would run ~180 scans of
    the very table whose scan costs minutes. --copy walks the rowid index
    forward instead.
    """
    left = _one(conn, 'SELECT COUNT(*) FROM exif_data '
                      'WHERE raw_exif_json IS NOT NULL')
    moved = _one(conn, 'SELECT COUNT(*) FROM exif_raw')
    return left, moved


def check(conn, path):
    _ensure_table(conn)
    print(f'  database            {os.path.getsize(path) / 1e9:.1f} GB')
    print(f'  rows in exif_raw    {_one(conn, "SELECT COUNT(*) FROM exif_raw")}')
    t = time.time()
    left = _one(conn, 'SELECT COUNT(*) FROM exif_data '
                      'WHERE raw_exif_json IS NOT NULL')
    print(f'  still in exif_data  {left}        '
          f'(one scan, {time.time() - t:.1f}s)')
    print('  -> run --copy' if left else
          '  old column is empty; nothing left but --vacuum for space')
    return 0


def copy(conn, path, skip=()):
    """
    Copy each blob into exif_raw using POINT LOOKUPS, never a range scan.

    THIS IS NOT A STYLE CHOICE. The family catalog's exif_data has damaged
    b-tree leaf linkage around id 301044-301046: every row in that region can
    be read individually - a rowid lookup descends from the root and lands
    directly on its leaf - but a range scan, which walks leaf page to leaf
    page, never comes back. `ORDER BY id LIMIT 2000` hangs there for ever, and
    excluding the rows with `id NOT IN (...)` does NOT help, because the scan
    still has to traverse the same chain to decide what to skip. That was
    measured: LIMIT 1000 returns in 0.07 s, LIMIT 1050 never returns, with or
    without the exclusion, while 350,540 individual lookups all succeed.

    So this walks the id space ARITHMETICALLY - i, i+1, i+2 - and asks for
    each one by rowid. A missing id costs one failed lookup. 350 K lookups run
    in well under a minute, which is a price worth paying to never touch the
    broken chain.
    """
    _ensure_table(conn)
    skipset = {int(i) for i in skip}
    top = _one(conn, 'SELECT MAX(id) FROM exif_data') or 0
    last = _one(conn, 'SELECT COALESCE(MAX(exif_id), 0) FROM exif_raw')
    if last:
        print(f'  resuming after exif_data.id {last}')
    print(f'  walking ids {last + 1}..{top} by rowid lookup')

    done = missing = 0
    started = time.time()
    i = last + 1
    while i <= top:
        hi = min(i + BATCH - 1, top)
        rows = []
        for k in range(i, hi + 1):
            if k in skipset:
                continue
            r = conn.execute('SELECT raw_exif_json FROM exif_data '
                             'WHERE id = ?', (k,)).fetchone()
            if r is None:
                missing += 1
            elif r[0] is not None:
                rows.append((k, r[0]))
        if rows:
            conn.executemany(
                'INSERT OR IGNORE INTO exif_raw (exif_id, raw_exif_json) '
                'VALUES (?, ?)', rows)
            conn.commit()
            done += len(rows)
        print(f'    through id {hi}: {done} copied  '
              f'({time.time() - started:.0f}s elapsed)', flush=True)
        i = hi + 1
    print(f'  copied {done} in {time.time() - started:.1f}s '
          f'({missing} ids had no row)')
    return 0


def clear(conn, path, skip=()):
    """
    NULL the old column, by point lookup, once the blob is safely in exif_raw.

    Point lookups for the same reason as --copy: the range scan cannot be
    trusted on this table. And the safety check moves PER ROW, which is
    stronger than the single aggregate it replaced - a row is cleared only
    when exif_raw is confirmed to hold its blob, one id at a time, so there is
    no window in which a count said yes for rows that individually said no.

    Batched commits, because one UPDATE over 361 K rows holding 7.2 GB keeps a
    write lock long enough to make the backend look dead.
    """
    _ensure_table(conn)
    skipset = {int(i) for i in skip}
    top = _one(conn, 'SELECT MAX(id) FROM exif_data') or 0
    print(f'  walking ids 1..{top} by rowid lookup')

    cleared = kept = 0
    uncopied = []
    started = time.time()
    i = 1
    while i <= top:
        hi = min(i + BATCH - 1, top)
        ready = []
        for k in range(i, hi + 1):
            if k in skipset:
                continue
            got = conn.execute(
                'SELECT 1 FROM exif_raw WHERE exif_id = ?', (k,)).fetchone()
            if got:
                ready.append(k)
                continue
            # No copy in exif_raw. Clearing this row would be the one
            # unrecoverable mistake available here, so it is left alone - but
            # whether that matters depends on whether it HAS a blob, and the
            # two cases must not be conflated. A row that never had one is
            # nothing to report; a row that has one and no copy means --copy
            # is unfinished, and the operator has to hear about it.
            has = conn.execute('SELECT raw_exif_json IS NOT NULL '
                               'FROM exif_data WHERE id = ?', (k,)).fetchone()
            if has and has[0]:
                uncopied.append(k)
            else:
                kept += 1
        if ready:
            conn.executemany('UPDATE exif_data SET raw_exif_json = NULL '
                             'WHERE id = ?', [(k,) for k in ready])
            conn.commit()
            cleared += len(ready)
        print(f'    through id {hi}: {cleared} cleared  '
              f'({time.time() - started:.0f}s elapsed)', flush=True)
        i = hi + 1
    print(f'  cleared {cleared} in {time.time() - started:.1f}s '
          f'({kept} had no blob to clear)')
    if uncopied:
        print(f'  {len(uncopied)} rows STILL HOLD the only copy of their blob '
              f'and were left alone: {uncopied[:10]}'
              f'{"..." if len(uncopied) > 10 else ""}', file=sys.stderr)
        print('  run --copy, or --skip them if they are unreadable',
              file=sys.stderr)
        return 2
    print('  scans are fast now; --vacuum returns the disk space')
    return 0


def vacuum(conn, path):
    before = os.path.getsize(path)
    print(f'  {before / 1e9:.1f} GB before; this takes a while')
    t = time.time()
    conn.execute('VACUUM')
    after = os.path.getsize(path)
    print(f'  {after / 1e9:.1f} GB after, {(before - after) / 1e9:.1f} GB returned '
          f'in {time.time() - t:.1f}s')
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('target_directory')
    for name in ('check', 'copy', 'clear', 'vacuum'):
        ap.add_argument(f'--{name}', action='store_true')
    ap.add_argument('--skip', metavar='FILE',
                    help='JSON list of exif_data ids to step over, as '
                         'migrate_exif_census.py --out writes it. A row on a '
                         'damaged page cannot be read AT ALL - not even its '
                         'narrow columns - and SQLite hangs inside a C call '
                         'where no timeout can reach it, so the only way past '
                         'one is to not select it.')
    args = ap.parse_args(argv)

    phases = [n for n in ('check', 'copy', 'clear', 'vacuum')
              if getattr(args, n)] or ['check']

    path = os.path.join(args.target_directory, 'workflow_results.db')
    if not os.path.exists(path):
        print(f'no database at {path}', file=sys.stderr)
        return 2
    print(path)

    skip = []
    if args.skip:
        with open(args.skip) as fh:
            skip = [int(i) for i in json.load(fh)]
        print(f'  stepping over {len(skip)} unreadable ids')

    conn = sqlite3.connect(path, timeout=3600)
    # 256 MB of page cache, not the 2 MB default and not the gigabyte this
    # first asked for. Every phase here STREAMS - it walks ids forward once
    # and never looks back - so a cache larger than the working set buys
    # nothing and gives SQLite a quarter-million pages to manage. The first
    # version set -1000000 and stalled at 99% CPU with no I/O and no progress
    # partway through the family catalog.
    conn.execute('PRAGMA cache_size = -256000')
    conn.execute('PRAGMA temp_store = MEMORY')
    try:
        for name in phases:
            print(f'── {name} ──')
            fn = {'check': check, 'copy': copy,
                  'clear': clear, 'vacuum': vacuum}[name]
            rc = fn(conn, path, skip) if name in ('copy', 'clear') \
                else fn(conn, path)
            if rc:
                return rc
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
