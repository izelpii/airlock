#!/usr/bin/env python3
"""
Give a catalog's workflow_results.db back the space it is holding but not using.

    tools/reclaim_catalog_db.py --list                  # every catalog, what it holds
    tools/reclaim_catalog_db.py <catalog>               # release, then report
    tools/reclaim_catalog_db.py <catalog> --vacuum      # release, then shrink the file

A <catalog> is its directory, its database file, or just its name - the name is
resolved under --catalogs-root like every other tool here.

── What it reclaims ──────────────────────────────────────────────────────────
Two copies of the same exiftool dump. `exif_data.raw_exif_json` is the
permanent one, a row per file. `import_items.metadata_json` is the copy an item
carries between PROBE and PLACE, which `_record_placed` now clears as it writes
the permanent one - but any run from before that lands here, and a run that was
stopped used to keep every byte. On one 340k-file catalog that was 15.9 GB of
the 27.3 GB file.

Imports do this themselves now, at two points: per item as it is placed, and as
a sweep at the start of every run. This script exists for the case those cannot
cover - reclaiming BEFORE the next import rather than during it - and because
only an exclusive process can run VACUUM.

── Why --vacuum is not the default ───────────────────────────────────────────
Releasing rows does not shrink a SQLite file. The pages go on the freelist and
get reused, so the file stops growing but does not get smaller. VACUUM rewrites
it, which needs an EXCLUSIVE LOCK for the whole rewrite and room for a second
copy of the database on the same disk. Minutes of locked database is not
something to do to a running stack by default, so it is a flag, and the script
refuses it while a workflow is running.
"""
import argparse
import os
import sqlite3
import sys

# The release and retention rules live with the schema they belong to, so this
# tool and the import can never disagree about what is safe to clear.
HERE = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(REPO, 'systems', 'backend', 'workflows'))
from lib.import_schema import (release_placed_metadata, prune_old_items,   # noqa: E402
                               reclaimable_bytes)

DB_NAME = 'workflow_results.db'
# Same resolution order as tools/pack_deployment.py: the deployed path if the
# environment names one, else this repository's own catalogs directory - so the
# tool works from anywhere without a --catalogs-root on every call.
DEFAULT_CATALOGS_ROOT = os.environ.get(
    'CATALOGS_HOST_DIR', os.path.join(REPO, 'catalogs'))


def human(n):
    # 1000-based, and labelled to match. This divided by 1024 while printing
    # "GB", so it called a 27,313,201,152-byte file "25.4 GB" - disagreeing with
    # ls, df and every other figure quoted about the same file. Either
    # convention is defensible; silently mixing them is not.
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if abs(n) < 1000 or unit == 'TB':
            return f'{n:,.1f} {unit}' if unit != 'B' else f'{n:,.0f} B'
        n /= 1000.0


def _vacuum_scratch_dir(db):
    """
    Put VACUUM's scratch copy on the DATABASE's filesystem, and return the path.

    VACUUM rewrites the database into a temporary file and then moves it over
    the original. That temporary file does NOT go beside the database: with
    SQLITE_TMPDIR and TMPDIR unset, SQLite tries /var/tmp, /usr/tmp and /tmp,
    which on a normal Linux install are all on the ROOT filesystem - and a
    catalog lives on the big volume precisely because the root one is small.

    So a 27 GB database on a volume with 139 GB free failed with
    "database or disk is full", meaning the 3.8 GB root disk (oldserver,
    2026-10-06). The message names neither the file nor the filesystem.

    SQLITE_TMPDIR is read when a temp file is created, so setting it here -
    before the VACUUM, in this process - is enough. temp_store_directory does
    the same thing through the connection and is set too: it is deprecated, but
    it is also the one that works if the environment is ignored.
    """
    scratch = os.path.dirname(os.path.abspath(db)) or '.'
    os.environ['SQLITE_TMPDIR'] = scratch
    return scratch


def _free_bytes(path):
    st = os.statvfs(path)
    return st.f_bavail * st.f_frsize


def _checkpoint(conn, db, label):
    """
    Fold the write-ahead log into the database and truncate it. Reports what
    happened, because the interesting outcome is the one that looks like
    success.

    IN WAL MODE A VACUUM WRITES ITS RESULT INTO THE LOG. The main file is
    rewritten when the log is checkpointed, and the log can only be RESET -
    which is what returns its space - when no other connection is reading. So
    with the stack up, a vacuum leaves a correctly compacted 17.2 GB database
    beside a 17.3 GB log: 33 GB on disk for a 17 GB database, looking exactly
    like the vacuum had not worked (oldserver, 2026-10-06).

    wal_checkpoint does not raise when it cannot finish - it returns
    (busy, log_pages, checkpointed_pages) with busy=1 - so the result has to be
    read rather than assumed.
    """
    try:
        busy, log_pages, done_pages = conn.execute(
            'PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
    except sqlite3.Error as exc:
        print(f'  ! could not checkpoint the log ({label}): {exc}')
        return False
    wal = db + '-wal'
    left = os.path.getsize(wal) if os.path.exists(wal) else 0
    if busy:
        print(f'  ! the write-ahead log still holds {human(left)} ({label}):'
              f' another connection is reading this database, so it cannot be reset')
        print('    Stop whatever has it open and run this again - on an airlock host'
              " that is the backend:  podman stop workflow-backend")
        return False
    if left:
        print(f'  write-ahead log      {human(left)} after the checkpoint ({label})')
    return True


def running_workflows(conn):
    """Names of executions still running, so --vacuum can refuse rather than block."""
    try:
        return [f"{r[0]} (execution {r[1]})" for r in conn.execute(
            "SELECT COALESCE(w.name, '?'), e.id FROM workflow_executions e "
            'LEFT JOIN workflows w ON w.id = e.workflow_id '
            "WHERE e.status = 'running'")]
    except sqlite3.Error:
        return []


def describe(db):
    """How much of this file is the duplicated metadata, without changing anything."""
    # PRAGMA query_only, NOT ?mode=ro.
    #
    # These databases run in WAL mode, and a WAL database whose -shm file does
    # not exist yet cannot be opened read-only at all: SQLite has to create that
    # file, and a read-only connection cannot. The failure is
    # "unable to open database file", which reads like a missing or unreadable
    # database and is neither.
    #
    # query_only gives the same guarantee for a stronger reason - the engine
    # refuses writes on this connection whatever the caller asks it to do - and
    # leaves WAL working normally. ?immutable=1 would also open it, and is the
    # wrong tool: it tells SQLite the file cannot change, which is false while
    # an import is running and can return torn pages.
    conn = sqlite3.connect(db, timeout=30000)
    try:
        conn.execute('PRAGMA query_only=1')
        try:
            held, rows = conn.execute(
                'SELECT COALESCE(SUM(LENGTH(metadata_json)), 0), COUNT(*) '
                'FROM import_items WHERE metadata_json IS NOT NULL '
                "AND state IN ('placed','rendered','render_skipped','render_failed')"
            ).fetchone()
        except sqlite3.Error:
            held, rows = 0, 0          # a catalog that has never been imported into
        # The -wal counts: it is real disk usage, and after a release it holds
        # the cleared pages until something checkpoints them. One release left
        # a 10.4 GB WAL beside a 27.3 GB database.
        wal = db + '-wal'
        return {'file': os.path.getsize(db), 'held': held, 'rows': rows,
                'wal': os.path.getsize(wal) if os.path.exists(wal) else 0,
                'free': reclaimable_bytes(conn)}
    finally:
        conn.close()


def reclaim(db, vacuum=False):
    info = describe(db)
    print(f'  file                 {human(info["file"])}')
    if info['wal']:
        print(f'  write-ahead log      {human(info["wal"])}')
    print(f'  duplicated metadata  {human(info["held"])} on {info["rows"]:,} placed item(s)')
    print(f'  already free inside  {human(info["free"])}')

    conn = sqlite3.connect(db, timeout=30000)
    try:
        busy = running_workflows(conn)
        if busy and vacuum:
            # A VACUUM would block them for its whole duration, and a workflow
            # that times out mid-import is a worse outcome than a large file.
            print(f'  ! not vacuuming: {", ".join(busy)} still running')
            vacuum = False
        released = prune = 0
        with conn:
            released = release_placed_metadata(conn)
            prune = prune_old_items(conn)
        print(f'  released             {released:,} item(s)')
        print(f'  pruned               {prune:,} expired item(s)')
        free = reclaimable_bytes(conn)
        print(f'  free inside now      {human(free)}')
        # Fold the WAL into the database before measuring or rewriting. After a
        # release the cleared pages are all in there, so this is what turns
        # "a 27 GB file plus a 10 GB log" into one smaller file to work on.
        conn.isolation_level = None              # pragmas and VACUUM: no transaction
        _checkpoint(conn, db, 'before')

        if vacuum:
            scratch = _vacuum_scratch_dir(db)
            conn.execute(f"PRAGMA temp_store_directory = '{scratch}'")
            page = conn.execute('PRAGMA page_size').fetchone()[0]
            pages = conn.execute('PRAGMA page_count').fetchone()[0]
            need = page * (pages - conn.execute('PRAGMA freelist_count').fetchone()[0])
            have = _free_bytes(scratch)
            print(f'  vacuum would write   {human(need)} into {scratch}'
                  f' ({human(have)} free)')
            # Refused HERE rather than discovered 18 GB into the rewrite: the
            # error SQLite raises for this says "database or disk is full",
            # which sounds like the database and is not.
            if have < need * 1.1:
                print('  ! not vacuuming: that filesystem does not have room for the '
                      'rewrite plus a 10% margin')
                print(f'    Free up space, or point SQLITE_TMPDIR at a filesystem '
                      f'that has {human(int(need * 1.1))}.')
                return
            print('  vacuuming (exclusive; this takes a while on a large file)...')
            conn.execute('VACUUM')
            # The checkpoint comes FIRST and the measurement second. Reading the
            # size straight after VACUUM reports the file as it was: the new
            # database is still in the log at that point, and this printed
            # "file now 27.3 GB" for a database it had just rewritten to 17.2.
            _checkpoint(conn, db, 'after the vacuum')
            print(f'  file now             {human(os.path.getsize(db))}')
        elif free > 1_000_000_000:
            print('  re-run with --vacuum to hand that back to the disk')
    finally:
        conn.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('catalog', nargs='?',
                    help='a catalog name, its directory, or its workflow_results.db')
    ap.add_argument('--vacuum', action='store_true', help='shrink the file afterwards')
    ap.add_argument('--list', action='store_true', help='report every catalog')
    ap.add_argument('--catalogs-root', '--root', dest='root',
                    default=DEFAULT_CATALOGS_ROOT,
                    help=f'default: {DEFAULT_CATALOGS_ROOT}')
    args = ap.parse_args()

    if args.list:
        found = False
        for entry in sorted(os.listdir(args.root)) if os.path.isdir(args.root) else []:
            db = os.path.join(args.root, entry, DB_NAME)
            if os.path.exists(db):
                found = True
                try:
                    info = describe(db)
                except (sqlite3.Error, OSError) as exc:
                    # One locked or foreign database must not end the listing.
                    print(f'{entry}: could not read ({exc})')
                    continue
                print(f'{entry}: file {human(info["file"])}, duplicated metadata '
                      f'{human(info["held"])} on {info["rows"]:,} item(s), '
                      f'free inside {human(info["free"])}')
        if not found:
            print(f'no catalog databases under {args.root}')
        return 0

    if not args.catalog:
        ap.error('name a catalog directory, or use --list')
    target = args.catalog
    if target.endswith('.db'):
        db = target
    elif os.path.isdir(target):
        db = os.path.join(target, DB_NAME)
    else:
        # A bare name: 'family' means <catalogs-root>/family.
        db = os.path.join(args.root, target, DB_NAME)
    if not os.path.exists(db):
        print(f'no such database: {db}')
        return 1
    print(db)
    reclaim(db, vacuum=args.vacuum)
    return 0


if __name__ == '__main__':
    sys.exit(main())
