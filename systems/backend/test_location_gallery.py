#!/usr/bin/env python3
"""
The gallery on a location's page, and why it used to spin forever.

    python -m pytest test_location_gallery.py

`https://192.168.1.18/locations/9/347` — "Home Crenshaw Ln, Denton TX", 9,753
photographs — never finished loading. Three separate defects in one endpoint:

  1. exif_data had NO INDEX on file_id, the column it is joined on in 16
     places in app.py and in most of the workflows. The gallery's
     `LEFT JOIN exif_data ed ON fc.id = ed.file_id` therefore planned as
     `SCAN ed LEFT-JOIN`: a full scan of exif_data PER ROW. exif_data holds
     raw_exif_json, so one scan reads 7.2 GB — and the query wanted 9,753 of
     them. It was never going to finish.

  2. one query per photograph for its display derivative, 9,753 round trips,
     expressible as a correlated subquery.

  3. years_months asked the EVENT path only, so the year picker above the
     gallery was empty for every location the clustering workflow made —
     exactly the bug the endpoint's own docstring says was fixed for the
     gallery and which was never fixed here.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _db(catalog):
    return sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))


def _photo(catalog, name, when, lat=None, lon=None):
    conn = _db(catalog)
    cur = conn.execute(
        'INSERT INTO file_contents (file_path, file_name, file_type, '
        ' master_datetime, added_at) VALUES (?,?,?,?,?)',
        (os.path.join('Images', name), name, '.jpg', when, when))
    fid = cur.lastrowid
    conn.execute('INSERT INTO exif_data (file_id, file_name, creation_date, '
                 ' latitude, longitude) VALUES (?,?,?,?,?)',
                 (fid, name, when, lat, lon))
    conn.commit()
    conn.close()
    return fid


def _clustered_location(catalog, name, file_ids):
    """
    A location as the clustering workflow makes one: file_locations rows and
    NO location_events row. That shape is what broke the page.
    """
    conn = _db(catalog)
    cur = conn.execute('INSERT INTO locations (name, confirmed) VALUES (?, 0)',
                       (name,))
    lid = cur.lastrowid
    conn.executemany('INSERT INTO file_locations (file_id, location_id) '
                     ' VALUES (?,?)', [(f, lid) for f in file_ids])
    conn.commit()
    conn.close()
    return lid


def _gallery(client, catalog, lid, **params):
    q = '&'.join(f'{k}={v}' for k, v in params.items())
    r = client.get(f'/api/catalogs/{catalog.id}/locations/{lid}/photos'
                   + (f'?{q}' if q else ''))
    assert r.status_code == 200, r.get_json()
    return r.get_json()


# ── 1. the index that was missing ───────────────────────────────────────────

# Every table joined on a file id, and the column it is joined on. An index
# must LEAD with that column; one that merely contains it cannot be used for
# the lookup. Asking the question of every table found four missing, and
# pet_detections having one while face_detections did not is what showed this
# was oversight.
#
# exif_data is NOT in this set, and the split is the point: its index is the
# one that matters most and the only one too expensive to build inside a
# request, so init_db() creates the cheap three and
# migrate_add_file_id_indexes.py owns the expensive one. See
# test_the_expensive_index_is_the_scripts_job.
CHEAP_FILE_ID_JOINS = {
    'face_detections': 'file_id',
    'pet_detections': 'file_id',
    'event_files': 'file_id',
    'file_locations': 'file_id',
    'alternative_versions': 'original_file_id',
}


def _leads_with(conn, table, column):
    return [name for (name,) in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name=?",
        (table,))
        if [r[2] for r in conn.execute(f'PRAGMA index_info({name})')][:1]
        == [column]]


@pytest.mark.parametrize('table,column', sorted(CHEAP_FILE_ID_JOINS.items()))
def test_the_file_id_join_column_is_indexed(catalog, table, column):
    """
    Without this the join plans as a full scan of the inner table PER ROW of
    the outer query. These four are narrow tables, so init_db() builds them.
    """
    conn = _db(catalog)
    try:
        leads = _leads_with(conn, table, column)
    finally:
        conn.close()
    assert leads, f'no index leads with {table}.{column}'


def test_the_expensive_index_is_the_scripts_job(catalog):
    """
    exif_data.file_id must NOT be built by init_db().

    It is the index the gallery actually needs, but exif_data carries
    raw_exif_json, so its rows are mostly blob in overflow pages: collecting
    one integer column from all of them ran over an hour on the 27.8 GB family
    catalog without finishing, against 3.6 s on the 2.3 GB one. Building it in
    init_db() would stall the first API call after an upgrade for an unbounded
    time while holding a write lock, and the other seven gunicorn threads
    would raise "database is locked".

    So a backend that has never seen the migration script still STARTS. The
    gallery is slow until the script runs; it is not dead.
    """
    conn = _db(catalog)
    try:
        assert not _leads_with(conn, 'exif_data', 'file_id'), \
            'init_db() built the expensive index; it belongs to the script'
    finally:
        conn.close()

    import migrate_add_file_id_indexes as mig
    assert ('exif_data', 'idx_exif_file_id', 'file_id') in mig.INDEXES, \
        'the script must own the index init_db() declines to build'


def test_the_script_builds_what_init_db_left_out(catalog):
    """End to end: run the migration, and the plan stops scanning."""
    import migrate_add_file_id_indexes as mig
    assert mig.main([catalog.target_directory]) == 0

    conn = _db(catalog)
    try:
        assert _leads_with(conn, 'exif_data', 'file_id') == ['idx_exif_file_id']
    finally:
        conn.close()


def test_the_gallery_never_scans_exif_data(client, catalog):
    """
    The plan, not the clock. `SCAN ed` is the signature of the hang, and it
    survives any amount of test data being small — which is why this asserts
    on EXPLAIN QUERY PLAN rather than on a stopwatch.
    """
    ids = [_photo(catalog, f'p{i}.jpg', f'2019-04-0{i + 1} 10:00:00',
                  33.1, -97.1) for i in range(5)]
    lid = _clustered_location(catalog, 'Home', ids)
    _gallery(client, catalog, lid)              # ensure the schema is built

    import migrate_add_file_id_indexes as mig
    mig.main([catalog.target_directory])        # the index is the script's job

    conn = _db(catalog)
    try:
        plan = '\n'.join(str(r[-1]) for r in conn.execute("""
            EXPLAIN QUERY PLAN
            SELECT fc.id, ed.latitude FROM file_contents fc
            JOIN file_locations fl ON fl.file_id = fc.id
            LEFT JOIN exif_data ed ON fc.id = ed.file_id
            WHERE fl.location_id = ?""", (lid,)))
    finally:
        conn.close()
    assert 'SCAN ed' not in plan, plan
    assert 'idx_exif_file_id' in plan, plan


# ── 2. the gallery finds clustered photographs at all ───────────────────────

def test_a_clustered_location_lists_its_photographs(client, catalog):
    ids = [_photo(catalog, f'c{i}.jpg', f'2019-04-0{i + 1} 10:00:00',
                  33.1, -97.1) for i in range(3)]
    lid = _clustered_location(catalog, 'Home', ids)
    got = _gallery(client, catalog, lid)
    assert [p['file_id'] for p in got['photos']] == ids


def test_the_display_derivative_comes_back_with_the_row(client, catalog):
    """
    The N+1 became a correlated subquery; this is the behaviour that had to
    survive that. A photograph with a 1200 rung shows it, one without shows
    the original and reports no characteristic.
    """
    with_alt = _photo(catalog, 'big.jpg', '2020-05-01 10:00:00', 33.1, -97.1)
    without = _photo(catalog, 'plain.jpg', '2020-05-02 10:00:00', 33.1, -97.1)
    conn = _db(catalog)
    conn.execute(
        'INSERT INTO alternative_versions (original_file_id, '
        ' alternative_file_path, alternative_file_name, characteristic, '
        ' rung, width) VALUES (?,?,?,?,?,?)',
        (with_alt, 'Derivatives/big-1200.jpg', 'big-1200.jpg',
         'TYPE DISPLAY 1200', '1200', 1200))
    conn.commit()
    conn.close()

    lid = _clustered_location(catalog, 'Home', [with_alt, without])
    by_id = {p['file_id']: p for p in _gallery(client, catalog, lid)['photos']}

    assert by_id[with_alt]['display_name'] == 'big-1200.jpg'
    assert by_id[with_alt]['characteristic'] == 'TYPE DISPLAY 1200'
    assert by_id[without]['display_name'] == 'plain.jpg'
    assert by_id[without]['characteristic'] is None


# ── 3. the year picker above the gallery ────────────────────────────────────

def test_a_clustered_location_offers_its_years(client, catalog):
    """
    years_months queried the event path only. Every clustered location has no
    location_events row, so the picker was empty above a gallery full of
    photographs.
    """
    ids = [_photo(catalog, 'y1.jpg', '2015-06-01 10:00:00', 33.1, -97.1),
           _photo(catalog, 'y2.jpg', '2015-07-02 10:00:00', 33.1, -97.1),
           _photo(catalog, 'y3.jpg', '2018-02-03 10:00:00', 33.1, -97.1)]
    lid = _clustered_location(catalog, 'Home', ids)
    ym = _gallery(client, catalog, lid)['years_months']
    assert {int(y) for y in ym} == {2015, 2018}
    assert sorted(ym[[k for k in ym if int(k) == 2015][0]]) == [6, 7]


def test_the_year_filter_narrows_the_gallery(client, catalog):
    a = _photo(catalog, 'a.jpg', '2015-06-01 10:00:00', 33.1, -97.1)
    b = _photo(catalog, 'b.jpg', '2018-02-03 10:00:00', 33.1, -97.1)
    lid = _clustered_location(catalog, 'Home', [a, b])
    assert [p['file_id'] for p in
            _gallery(client, catalog, lid, year=2015)['photos']] == [a]
    assert [p['file_id'] for p in
            _gallery(client, catalog, lid, year=2018, month=2)['photos']] == [b]


def test_an_event_tagged_location_still_works(client, catalog):
    """
    The event path is a UNION, not a replacement: a location somebody tagged
    onto an event still lists that event's photographs.
    """
    fid = _photo(catalog, 'ev.jpg', '2016-08-09 10:00:00')
    conn = _db(catalog)
    cur = conn.execute("INSERT INTO events (name, type, start_time, end_time) "
                       "VALUES ('Trip','Auto','2016-08-09','2016-08-09')")
    eid = cur.lastrowid
    conn.execute('INSERT INTO event_files (event_id, file_id) VALUES (?,?)',
                 (eid, fid))
    cur = conn.execute("INSERT INTO locations (name, confirmed) VALUES ('Tagged',1)")
    lid = cur.lastrowid
    conn.execute('INSERT INTO location_events (location_id, event_id) '
                 ' VALUES (?,?)', (lid, eid))
    conn.commit()
    conn.close()

    got = _gallery(client, catalog, lid)
    assert [p['file_id'] for p in got['photos']] == [fid]
    assert {int(y) for y in got['years_months']} == {2016}


def test_a_file_reachable_both_ways_is_listed_once(client, catalog):
    """UNION, not UNION ALL."""
    fid = _photo(catalog, 'both.jpg', '2017-03-04 10:00:00', 33.1, -97.1)
    conn = _db(catalog)
    cur = conn.execute("INSERT INTO events (name, type, start_time, end_time) "
                       "VALUES ('T','Auto','2017-03-04','2017-03-04')")
    eid = cur.lastrowid
    conn.execute('INSERT INTO event_files (event_id, file_id) VALUES (?,?)',
                 (eid, fid))
    cur = conn.execute("INSERT INTO locations (name, confirmed) VALUES ('Both',1)")
    lid = cur.lastrowid
    conn.execute('INSERT INTO location_events (location_id, event_id) VALUES (?,?)',
                 (lid, eid))
    conn.execute('INSERT INTO file_locations (file_id, location_id) VALUES (?,?)',
                 (fid, lid))
    conn.commit()
    conn.close()
    assert [p['file_id'] for p in
            _gallery(client, catalog, lid)['photos']] == [fid]
