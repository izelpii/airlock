#!/usr/bin/env python3
"""
Rebuilding exif_data, and keeping what points at it.

    python -m pytest test_exif_rebuild.py

The family catalog's exif_data cannot be scanned past id 301044 - rowid
lookups work, range scans never return - which is why CREATE INDEX on
exif_data(file_id) ran for hours and why the location gallery had no index to
use. The table has to be rebuilt by rowid.

The corruption itself cannot be reproduced in a test. What CAN be pinned is
everything the rebuild must preserve while doing it, which is where a
drop-and-rename goes wrong: ids, columns, indexes, and the rows that point at
those ids from elsewhere.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402

import migrate_rebuild_exif_data as rb  # noqa: E402


def _db(catalog):
    return sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))


def _photo(catalog, name, lat=None):
    """A file with an exif_data row, linked both ways as the import links them."""
    conn = _db(catalog)
    cur = conn.execute(
        'INSERT INTO file_contents (file_path, file_name, file_type, '
        ' master_datetime, added_at) VALUES (?,?,?,?,?)',
        (os.path.join('Images', name), name, '.jpg',
         '2020-01-01 10:00:00', '2020-01-01 10:00:00'))
    fid = cur.lastrowid
    cur = conn.execute('INSERT INTO exif_data (file_id, file_name, latitude, '
                       ' camera_make) VALUES (?,?,?,?)',
                       (fid, name, lat, 'Canon'))
    eid = cur.lastrowid
    conn.execute('UPDATE file_contents SET exif_data_id = ? WHERE id = ?',
                 (eid, fid))
    conn.commit()
    conn.close()
    return fid, eid


def test_the_rebuild_preserves_ids(catalog):
    """
    file_contents.exif_data_id points at exif_data.id, so a rebuild that
    renumbered would silently detach every photograph from its EXIF. The ids
    are carried across explicitly for that reason.
    """
    made = [_photo(catalog, f'p{i}.jpg') for i in range(5)]
    assert rb.main([catalog.target_directory, '--rebuild']) == 0

    conn = _db(catalog)
    try:
        for fid, eid in made:
            row = conn.execute('SELECT file_id, camera_make FROM exif_data '
                               'WHERE id = ?', (eid,)).fetchone()
            assert row == (fid, 'Canon'), (eid, row)
            link = conn.execute('SELECT exif_data_id FROM file_contents '
                                'WHERE id = ?', (fid,)).fetchone()[0]
            assert link == eid
    finally:
        conn.close()


def test_the_rebuild_keeps_every_column(catalog):
    _photo(catalog, 'cols.jpg')
    conn = _db(catalog)
    before = [r[1] for r in conn.execute('PRAGMA table_info(exif_data)')]
    conn.close()

    assert rb.main([catalog.target_directory, '--rebuild']) == 0

    conn = _db(catalog)
    try:
        after = [r[1] for r in conn.execute('PRAGMA table_info(exif_data)')]
    finally:
        conn.close()
    assert after == before, 'the rebuilt table has a different shape'


def test_the_rebuild_recreates_the_indexes(catalog):
    """
    A rebuilt table with no indexes would be a different performance bug
    wearing the fix's clothes - the geohash and latitude indexes are what the
    location workflows use.
    """
    _photo(catalog, 'idx.jpg', lat=33.1)
    conn = _db(catalog)
    before = {n for (n,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' "
        "AND tbl_name='exif_data' AND sql IS NOT NULL")}
    conn.close()
    assert before, 'no indexes to begin with; this test would prove nothing'

    assert rb.main([catalog.target_directory, '--rebuild']) == 0

    conn = _db(catalog)
    try:
        after = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='exif_data' AND sql IS NOT NULL")}
    finally:
        conn.close()
    assert before <= after, sorted(before - after)


def test_skipped_ids_are_dropped_and_nothing_else_is(catalog, tmp_path):
    """
    The only data this is allowed to lose: the rows named by --skip, which
    cannot be read by any means. Everything else must survive exactly.
    """
    keep = [_photo(catalog, f'k{i}.jpg') for i in range(3)]
    gone_fid, gone_eid = _photo(catalog, 'gone.jpg')
    skip_file = tmp_path / 'bad.json'
    skip_file.write_text(json.dumps([gone_eid]))

    assert rb.main([catalog.target_directory, '--rebuild',
                    '--skip', str(skip_file)]) == 0

    conn = _db(catalog)
    try:
        ids = {r[0] for r in conn.execute('SELECT id FROM exif_data')}
        assert gone_eid not in ids
        for _, eid in keep:
            assert eid in ids
        # The photograph itself is untouched - only its EXIF row went.
        assert conn.execute('SELECT file_name FROM file_contents WHERE id = ?',
                            (gone_fid,)).fetchone()[0] == 'gone.jpg'
    finally:
        conn.close()


def test_the_scratch_table_is_gone_and_the_damaged_one_is_kept(catalog):
    """
    No `exif_data_rebuilt` left over, and `exif_data_damaged` deliberately
    still there.

    DROP TABLE has to walk a table's b-tree to free its pages, and on the real
    catalog that traversal goes through the damaged leaf linkage: the drop sat
    at 97% CPU and never returned, exactly like every other scan. So the old
    table is renamed aside rather than dropped. Its pages stay in the file,
    referenced by nothing - the cheaper thing to give up than the only
    operation that gets us a scannable table.
    """
    _photo(catalog, 'tidy.jpg')
    assert rb.main([catalog.target_directory, '--rebuild']) == 0
    conn = _db(catalog)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert 'exif_data_rebuilt' not in names, 'scratch table left behind'
    assert 'exif_data' in names
    assert 'exif_data_damaged' in names, \
        'the old table must be kept, not dropped - the drop is what hangs'


def test_a_second_rebuild_still_works(catalog):
    """
    Re-running must not trip over its own leftovers: exif_data_damaged already
    exists, and the indexes it carries still hold the names the new table
    wants, which are unique across the database.
    """
    _photo(catalog, 'again.jpg', lat=33.1)
    assert rb.main([catalog.target_directory, '--rebuild']) == 0
    assert rb.main([catalog.target_directory, '--rebuild']) == 0
    conn = _db(catalog)
    try:
        assert conn.execute('SELECT COUNT(*) FROM exif_data').fetchone()[0] == 1
        idx = {n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='exif_data' AND sql IS NOT NULL")}
    finally:
        conn.close()
    assert idx, 'the indexes did not survive a second rebuild'


def test_the_index_can_be_built_afterwards(catalog):
    """
    The whole purpose. On the damaged table CREATE INDEX never finished,
    because it needs a full scan; on the rebuilt one it must work - and then
    the gallery's join stops planning as `SCAN ed`.
    """
    _photo(catalog, 'after.jpg')
    assert rb.main([catalog.target_directory, '--rebuild']) == 0

    import migrate_add_file_id_indexes as idx
    assert idx.main([catalog.target_directory]) == 0

    conn = _db(catalog)
    try:
        leads = [n for (n,) in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index' "
            "AND tbl_name='exif_data'")
            if [r[2] for r in conn.execute(f'PRAGMA index_info({n})')][:1]
            == ['file_id']]
        assert leads, 'no index leads with exif_data.file_id'
        plan = '\n'.join(str(r[-1]) for r in conn.execute("""
            EXPLAIN QUERY PLAN
            SELECT fc.id FROM file_contents fc
            LEFT JOIN exif_data ed ON fc.id = ed.file_id"""))
    finally:
        conn.close()
    assert 'SCAN ed' not in plan, plan


def test_check_changes_nothing(catalog):
    _photo(catalog, 'look.jpg')
    conn = _db(catalog)
    before = conn.execute('SELECT COUNT(*) FROM exif_data').fetchone()[0]
    conn.close()
    assert rb.main([catalog.target_directory, '--check']) == 0
    conn = _db(catalog)
    try:
        assert conn.execute('SELECT COUNT(*) FROM exif_data').fetchone()[0] == before
        assert conn.execute("SELECT COUNT(*) FROM sqlite_master "
                            "WHERE name LIKE '%rebuilt%'").fetchone()[0] == 0
    finally:
        conn.close()
