#!/usr/bin/env python3
"""
raw_exif_json lives in its own table, and is fetched one file at a time.

    python -m pytest test_raw_exif_split.py

WHY. raw_exif_json averages 22 KB a row and 7.2 GB across a 361 K-file
catalog (contents-spec section 4). Inside exif_data every query that reads
whole rows paid for it whether it wanted the blob or not. Measured on homeserver's
family catalog (27.8 GB):

    reading exif_data.file_id alone      626,000 rows/s
    COUNT(*) over the SAME rows          exceeds 360 s

Identical row count; the only difference is whether the read traverses the
blob. It is also why CREATE INDEX on exif_data(file_id) ran over an hour there
without finishing, leaving the location gallery with no index and a location of
9,753 photographs that never loaded.

And almost nothing reads it: /photos does not, and the single-photo view does
not - both use the promoted columns. The one reader was the catalog file list,
which carried it for every file while the UI showed it for the one expanded
row.

TWO INVARIANTS, and they are what these tests are for:
  1. a catalog that has NOT been migrated still answers, because readers
     COALESCE exif_raw with the old column;
  2. --clear refuses to run until every blob is safely copied.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402

import migrate_split_raw_exif as split  # noqa: E402

RAW = {'Artist': 'Izelpi', 'LensModel': '24-70mm', 'FNumber': 2.8}


def _db(catalog):
    return sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))


def _file_with_exif(catalog, name, raw=RAW, where='new'):
    """
    A photograph with raw EXIF, stored either the new way or the old way.

    `where='old'` writes the blob into exif_data.raw_exif_json, which is what
    every catalog looks like before the migration runs.
    """
    conn = _db(catalog)
    cur = conn.execute(
        'INSERT INTO file_contents (file_path, file_name, file_type, '
        ' master_datetime, added_at) VALUES (?,?,?,?,?)',
        (os.path.join('Images', name), name, '.jpg',
         '2020-01-01 10:00:00', '2020-01-01 10:00:00'))
    fid = cur.lastrowid
    blob = json.dumps(raw) if raw is not None else None
    if where == 'old':
        cur = conn.execute('INSERT INTO exif_data (file_id, file_name, '
                           ' raw_exif_json) VALUES (?,?,?)', (fid, name, blob))
    else:
        cur = conn.execute('INSERT INTO exif_data (file_id, file_name) '
                           ' VALUES (?,?)', (fid, name))
        if blob is not None:
            conn.execute('INSERT INTO exif_raw (exif_id, raw_exif_json) '
                         ' VALUES (?,?)', (cur.lastrowid, blob))
    conn.execute('UPDATE file_contents SET exif_data_id = ? WHERE id = ?',
                 (cur.lastrowid, fid))
    conn.commit()
    conn.close()
    return fid


def _exif(client, catalog, fid):
    r = client.get(f'/api/catalogs/{catalog.id}/files/{fid}/exif')
    assert r.status_code == 200, r.get_json()
    return r.get_json()['exif_data']


# ── the table exists, and the blob is not in the hot one ────────────────────

def test_exif_raw_exists_and_is_keyed_on_exif_data_id(catalog):
    """
    Keyed on exif_data.id, not file_id: exif_data.file_id carries no
    uniqueness constraint, so keying on it would invent one.
    """
    conn = _db(catalog)
    try:
        cols = {r[1]: r for r in conn.execute('PRAGMA table_info(exif_raw)')}
        assert set(cols) == {'exif_id', 'raw_exif_json'}, sorted(cols)
        assert cols['exif_id'][5] == 1, 'exif_id must be the primary key'
    finally:
        conn.close()


def test_the_catalog_file_list_does_not_carry_the_blob(client, catalog):
    """
    The whole point. 22 KB a file, for a detail the UI shows one row at a
    time, is not something the list may send.
    """
    _file_with_exif(catalog, 'a.jpg')
    # Through /files rather than ?include=files: both build their rows with
    # the same helper, and this one does not need Catalog.to_dict(), which
    # the namespace this fixture hands back does not have.
    r = client.get(f'/api/catalogs/{catalog.id}/files')
    assert r.status_code == 200, r.get_json()
    files = r.get_json()['files']
    assert files, 'no files came back; this test would prove nothing'
    for f in files:
        assert 'raw_exif_json' not in f, f
        assert 'exif_data' not in f, \
            'exif_data belongs to /files/<id>/exif now, not the list'


# ── the lazy endpoint ───────────────────────────────────────────────────────

def test_one_files_exif_comes_back_parsed(client, catalog):
    fid = _file_with_exif(catalog, 'b.jpg')
    assert _exif(client, catalog, fid) == RAW


def test_an_unmigrated_catalog_still_answers(client, catalog):
    """
    INVARIANT 1. The old column is the fallback, so a catalog nobody has run
    the migration against keeps working. This is what makes the code
    deployable before the data moves.
    """
    fid = _file_with_exif(catalog, 'old.jpg', where='old')
    assert _exif(client, catalog, fid) == RAW


def test_a_file_with_no_exif_is_not_an_error(client, catalog):
    fid = _file_with_exif(catalog, 'none.jpg', raw=None)
    assert _exif(client, catalog, fid) is None


def test_an_unknown_file_is_not_an_error(client, catalog):
    assert _exif(client, catalog, 999999) is None


def test_a_corrupt_blob_reads_as_absent(client, catalog):
    """Not a 500. The page shows "no EXIF", which is true enough."""
    fid = _file_with_exif(catalog, 'bad.jpg')
    conn = _db(catalog)
    conn.execute("UPDATE exif_raw SET raw_exif_json = '{not json'")
    conn.commit()
    conn.close()
    assert _exif(client, catalog, fid) is None


# ── the migration ───────────────────────────────────────────────────────────

def test_copy_then_clear_moves_the_blob(client, catalog):
    fid = _file_with_exif(catalog, 'move.jpg', where='old')
    target = catalog.target_directory

    assert split.main([target, '--copy']) == 0
    conn = _db(catalog)
    try:
        assert conn.execute('SELECT COUNT(*) FROM exif_raw').fetchone()[0] == 1
        # still in BOTH after --copy: that is what makes it interruptible
        assert conn.execute('SELECT COUNT(*) FROM exif_data '
                            'WHERE raw_exif_json IS NOT NULL').fetchone()[0] == 1
    finally:
        conn.close()
    assert _exif(client, catalog, fid) == RAW

    assert split.main([target, '--clear']) == 0
    conn = _db(catalog)
    try:
        assert conn.execute('SELECT COUNT(*) FROM exif_data '
                            'WHERE raw_exif_json IS NOT NULL').fetchone()[0] == 0
    finally:
        conn.close()
    assert _exif(client, catalog, fid) == RAW, \
        'the blob must still be readable after the old column is cleared'


def test_clear_refuses_before_copy(catalog):
    """
    INVARIANT 2. --copy is reversible and --clear is not, so --clear verifies
    that every blob is in exif_raw and refuses otherwise.
    """
    _file_with_exif(catalog, 'unsafe.jpg', where='old')
    assert split.main([catalog.target_directory, '--clear']) == 2

    conn = _db(catalog)
    try:
        assert conn.execute('SELECT COUNT(*) FROM exif_data '
                            'WHERE raw_exif_json IS NOT NULL').fetchone()[0] == 1, \
            'it refused and cleared anyway'
    finally:
        conn.close()


def test_copy_is_idempotent(catalog):
    _file_with_exif(catalog, 'twice.jpg', where='old')
    t = catalog.target_directory
    assert split.main([t, '--copy']) == 0
    assert split.main([t, '--copy']) == 0
    conn = _db(catalog)
    try:
        assert conn.execute('SELECT COUNT(*) FROM exif_raw').fetchone()[0] == 1
    finally:
        conn.close()


def test_check_changes_nothing(catalog):
    _file_with_exif(catalog, 'look.jpg', where='old')
    t = catalog.target_directory
    assert split.main([t, '--check']) == 0
    conn = _db(catalog)
    try:
        assert conn.execute('SELECT COUNT(*) FROM exif_raw').fetchone()[0] == 0
        assert conn.execute('SELECT COUNT(*) FROM exif_data '
                            'WHERE raw_exif_json IS NOT NULL').fetchone()[0] == 1
    finally:
        conn.close()


def test_copy_resumes_from_where_it_stopped(catalog):
    """
    --copy walks exif_data forward by id and takes the highest id already in
    exif_raw as its starting point, so an interrupted run continues rather
    than starting over. On a 361 K-row catalog "starting over" would mean
    re-reading the table this whole change exists to stop reading.
    """
    ids = [_file_with_exif(catalog, f'r{i}.jpg', raw={'n': i}, where='old')
           for i in range(5)]
    conn = _db(catalog)
    exif_ids = [r[0] for r in conn.execute(
        'SELECT id FROM exif_data ORDER BY id')]
    # Pretend a previous run got through the first two.
    for e in exif_ids[:2]:
        conn.execute('INSERT INTO exif_raw (exif_id, raw_exif_json) '
                     "VALUES (?, '{\"n\": -1}')", (e,))
    conn.commit()
    conn.close()

    assert split.main([catalog.target_directory, '--copy']) == 0

    conn = _db(catalog)
    try:
        got = dict(conn.execute('SELECT exif_id, raw_exif_json FROM exif_raw'))
        assert len(got) == len(ids), got
        # The two it skipped keep what was there - not re-copied, not clobbered.
        for e in exif_ids[:2]:
            assert json.loads(got[e]) == {'n': -1}
    finally:
        conn.close()


def test_copy_steps_over_rows_with_no_blob(catalog):
    """
    A gap - an exif_data row with no raw EXIF - must not stop the walk. The
    ORDER BY skips it; this pins that, because an early version of the loop
    had a branch that tried to step over gaps by hand and could only ever
    break out.
    """
    _file_with_exif(catalog, 'one.jpg', raw={'n': 1}, where='old')
    _file_with_exif(catalog, 'gap.jpg', raw=None)
    _file_with_exif(catalog, 'two.jpg', raw={'n': 2}, where='old')

    assert split.main([catalog.target_directory, '--copy']) == 0

    conn = _db(catalog)
    try:
        blobs = sorted(json.loads(r[0])['n'] for r in conn.execute(
            'SELECT raw_exif_json FROM exif_raw'))
        assert blobs == [1, 2], blobs
    finally:
        conn.close()


def test_copy_steps_over_ids_on_a_skip_list(catalog, tmp_path):
    """
    A row on a damaged page cannot be read at all - not its blob, not even its
    narrow columns - and SQLite hangs inside a C call where no Python timeout
    can reach it. The family catalog has such rows (exif_data 301044 and
    301045, from the SSD failure this catalog survived), and they stalled
    --copy indefinitely.

    They cannot be read, so they are not selected. migrate_exif_census.py
    finds them; --skip steps over them; the COALESCE fallback means the rows
    left behind are no worse off than before.
    """
    keep = _file_with_exif(catalog, 'keep.jpg', raw={'n': 1}, where='old')
    drop = _file_with_exif(catalog, 'drop.jpg', raw={'n': 2}, where='old')
    conn = _db(catalog)
    bad = conn.execute('SELECT id FROM exif_data WHERE file_id = ?',
                       (drop,)).fetchone()[0]
    conn.close()

    skip_file = tmp_path / 'bad.json'
    skip_file.write_text(json.dumps([bad]))
    assert split.main([catalog.target_directory, '--copy',
                       '--skip', str(skip_file)]) == 0

    conn = _db(catalog)
    try:
        moved = [r[0] for r in conn.execute('SELECT exif_id FROM exif_raw')]
        assert bad not in moved, 'the unreadable id was selected anyway'
        assert len(moved) == 1, moved
    finally:
        conn.close()


def test_clear_refuses_while_a_skipped_id_still_holds_its_blob(catalog, tmp_path):
    """
    --skip must not become a way to lose data. A skipped row keeps its blob in
    the old column, so --clear's verification still sees it as uncopied and
    refuses - which is correct: clearing it would destroy the only copy.
    """
    _file_with_exif(catalog, 'keep.jpg', raw={'n': 1}, where='old')
    drop = _file_with_exif(catalog, 'drop.jpg', raw={'n': 2}, where='old')
    conn = _db(catalog)
    bad = conn.execute('SELECT id FROM exif_data WHERE file_id = ?',
                       (drop,)).fetchone()[0]
    conn.close()
    skip_file = tmp_path / 'bad.json'
    skip_file.write_text(json.dumps([bad]))

    assert split.main([catalog.target_directory, '--copy',
                       '--skip', str(skip_file)]) == 0
    assert split.main([catalog.target_directory, '--clear']) == 2, \
        '--clear must refuse while a skipped row is the only copy'


def test_clear_with_the_same_skip_list_proceeds(catalog, tmp_path):
    """
    The other half of the previous test. --clear refuses when a row's blob is
    nowhere else; given the SAME skip list that --copy used, it clears the
    readable rows and leaves the damaged ones holding their only copy.
    """
    keep = _file_with_exif(catalog, 'keep.jpg', raw={'n': 1}, where='old')
    drop = _file_with_exif(catalog, 'drop.jpg', raw={'n': 2}, where='old')
    conn = _db(catalog)
    ids = dict(conn.execute('SELECT file_id, id FROM exif_data'))
    conn.close()
    bad = ids[drop]
    skip_file = tmp_path / 'bad.json'
    skip_file.write_text(json.dumps([bad]))

    arg = ['--skip', str(skip_file)]
    assert split.main([catalog.target_directory, '--copy'] + arg) == 0
    assert split.main([catalog.target_directory, '--clear'] + arg) == 0

    conn = _db(catalog)
    try:
        left = dict(conn.execute(
            'SELECT id, raw_exif_json FROM exif_data'))
        assert left[ids[keep]] is None, 'the readable row was not cleared'
        assert left[bad] is not None, \
            'the damaged row lost the only copy of its blob'
    finally:
        conn.close()


def test_the_migration_never_range_scans_exif_data(catalog):
    """
    THE CONSTRAINT THAT SHAPES BOTH PHASES.

    The family catalog's exif_data has damaged b-tree leaf linkage around id
    301044-301046. Every row there reads fine by rowid - a lookup descends
    from the root to its leaf - but a RANGE SCAN walks leaf page to leaf page
    and never returns. Measured: `ORDER BY id LIMIT 1000` came back in 0.07 s,
    `LIMIT 1050` never did, and adding `id NOT IN (301044,301045,301046)` did
    not help, because the scan still traverses the chain to decide what to
    skip. Meanwhile all 350,540 rows were read individually without trouble.

    So --copy and --clear walk the id space arithmetically and fetch by rowid.
    This test reads the source: any `FROM exif_data` carrying an ORDER BY or a
    range predicate on id would reintroduce the hang, on a catalog where it
    cannot be tested because the corruption is not reproducible here.
    """
    import inspect
    src = inspect.getsource(split.copy) + inspect.getsource(split.clear)
    # Strip docstrings and comments: both describe the scan they avoid.
    import re
    body = re.sub(r'#.*', '', re.sub(r'"""[\s\S]*?"""', '', src))

    assert 'ORDER BY' not in body.upper(), \
        'a range scan crept back into the migration'
    assert 'id >' not in body and 'id <' not in body, \
        'a range predicate on id means a scan, not a rowid lookup'
    # What it must use instead.
    assert 'WHERE id = ?' in body, 'the point lookup is gone'
