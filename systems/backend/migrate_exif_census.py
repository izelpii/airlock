#!/usr/bin/env python3
"""
Which exif_data rows can be read at all, and which are on damaged pages.

    python migrate_exif_census.py /catalogs/family
    python migrate_exif_census.py --from 300000 /catalogs/family

WHY A SUBPROCESS PER BATCH. The family catalog holds at least one row -
exif_data.id 301045 - that cannot be read at any timeout. Not its blob, not
even `SELECT id, file_name`. Its neighbours read in milliseconds. That is a
corrupt page, consistent with the SSD failure this catalog survived.

SQLite is doing that inside a C call, so Python cannot interrupt it: a signal
handler never runs and a thread cannot be killed. The only way to survive such
a row is to read in a CHILD PROCESS and kill the child. So this walks ids in
batches, each batch in its own short-lived child, and on a timeout bisects the
batch down to the single id responsible, records it, and steps over it.

The output is a list of unreadable ids - the census every other decision needs:
how much is damaged, which photographs, and which queries will hang until the
rows are dealt with.
"""

import argparse
import json
import os
import subprocess
import sys

BATCH = 500
TIMEOUT = 20            # seconds for one child; a healthy batch takes ~0.1s


CHILD = r'''
import sqlite3, sys, json
db, lo, hi = sys.argv[1], int(sys.argv[2]), int(sys.argv[3])
c = sqlite3.connect("file:" + db + "?mode=ro", uri=True)
ids = [r[0] for r in c.execute(
    "SELECT id FROM exif_data WHERE id > ? AND id <= ? ORDER BY id", (lo, hi))]
# READ THE BLOB, not a narrow column. A 39 KB raw_exif_json lives in
# OVERFLOW PAGES, and selecting file_id faults in only the row's main page -
# so a row whose overflow chain is damaged passes that check and still hangs
# --copy. The first version of this census did exactly that and under-reported.
# Reading the value is the only check that exercises what --copy exercises.
n = 0
for i in ids:
    row = c.execute("SELECT raw_exif_json FROM exif_data WHERE id = ?",
                    (i,)).fetchone()
    if row is not None:
        len(row[0] or "")      # force the value out of its overflow chain
    n += 1
print(json.dumps({"checked": n, "ids": len(ids)}))
c.close()
'''


def _probe(db, lo, hi, timeout=TIMEOUT):
    """True if every row in (lo, hi] could be read; False on timeout or error."""
    try:
        r = subprocess.run([sys.executable, '-c', CHILD, db, str(lo), str(hi)],
                           capture_output=True, text=True, timeout=timeout)
        return r.returncode == 0
    except subprocess.TimeoutExpired:
        return False


def _bisect(db, lo, hi, bad, log):
    """Narrow an unreadable range down to the individual ids responsible."""
    if hi - lo <= 1:
        bad.append(hi)
        log(f'    UNREADABLE id {hi}')
        return
    mid = (lo + hi) // 2
    for a, b in ((lo, mid), (mid, hi)):
        if b <= a:
            continue
        if not _probe(db, a, b, timeout=max(5, TIMEOUT // 2)):
            _bisect(db, a, b, bad, log)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split('\n')[1])
    ap.add_argument('target_directory')
    ap.add_argument('--from', dest='start', type=int, default=0,
                    help='begin after this exif_data.id')
    ap.add_argument('--out', help='write the bad ids here as JSON')
    args = ap.parse_args(argv)

    db = os.path.join(args.target_directory, 'workflow_results.db')
    if not os.path.exists(db):
        print(f'no database at {db}', file=sys.stderr)
        return 2
    print(db)

    import sqlite3
    c = sqlite3.connect('file:' + db + '?mode=ro', uri=True)
    top = c.execute('SELECT MAX(id) FROM exif_data').fetchone()[0] or 0
    c.close()
    print(f'  walking exif_data ids {args.start + 1}..{top} '
          f'in batches of {BATCH}, {TIMEOUT}s each')

    bad = []
    lo = args.start
    while lo < top:
        hi = min(lo + BATCH, top)
        if not _probe(db, lo, hi):
            print(f'  batch {lo + 1}..{hi} did not come back; bisecting',
                  flush=True)
            _bisect(db, lo, hi, bad, lambda m: print(m, flush=True))
        lo = hi
        if (lo // BATCH) % 20 == 0:
            print(f'    ...through id {lo}, {len(bad)} unreadable so far',
                  flush=True)

    print(f'  {len(bad)} unreadable ids: {bad}')
    if args.out:
        with open(args.out, 'w') as fh:
            json.dump(bad, fh)
        print(f'  written to {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
