"""
Search and saved searches. documentation/architecture/search-spec.md.

    python -m pytest test_search.py
"""

import os
import sqlite3
import sys
from datetime import datetime, timedelta, timezone

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import saved_searches  # noqa: E402
import search  # noqa: E402
import search_index  # noqa: E402
from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _db(catalog):  # noqa: F811
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    return conn


def _file(conn, fid, name, when, ext='.jpg', removed=None, sha=None, size=1000):
    conn.execute("INSERT INTO file_contents (id, file_path, file_name, file_type, file_size, "
                 "sha256_hash, master_datetime, removed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                 (fid, f'Images/{name}', name, ext, size, sha or f'{fid:064x}', when, removed))


def _op(conn, fid, op_type, source, target=None, details=None, status='moved'):
    conn.execute("INSERT INTO file_operations (file_id, operation_type, source_path, target_path, "
                 "status, details) VALUES (?, ?, ?, ?, ?, ?)",
                 (fid, op_type, source, target, status, details))


@pytest.fixture
def library(client, catalog, app_module):  # noqa: F811
    client.get('/api/catalogs/1/faces/unidentified/summary')       # builds the schema
    conn = _db(catalog)
    sha1 = 'aa' * 32
    _file(conn, 1, '20190704120000-aaaaaaaaaa-file1.jpg', '2019-07-04T12:00:00', sha=sha1)
    _op(conn, 1, 'move', '/in/holiday/file1.jpg', 'Images/2019/07/04/x.jpg')
    # The owner's example: turned away as a copy of file1.jpg.
    _op(conn, 1, 'duplicate_moved', '/in/again/file1000-notnew.jpg', '/in/DUPS/again/file1000-notnew.jpg',
        details=f'duplicate of x; SHA256: {sha1}')
    _file(conn, 2, '20200101090000-bbbbbbbbbb-IMG_0002.HEIC', '2020-01-01T09:00:00', ext='.heic')
    _op(conn, 2, 'move', '/in/iphone/YKKB8383.HEIC')
    _file(conn, 3, '20210704100000-cccccccccc-clip.mov', '2021-07-04T10:00:00', ext='.mov')
    _file(conn, 4, '20220101000000-dddddddddd-gone.jpg', '2022-01-01T00:00:00', removed='2026-10-01')
    conn.execute("INSERT INTO people (id, name, kind) VALUES (1, 'Ana', 'person'), (2, 'Teso', 'person'), "
                 "(3, 'Rex', 'pet')")
    conn.executemany("INSERT INTO face_detections (file_id, x, y, w, h, person_id) VALUES (?, 0, 0, 1, 1, ?)",
                     [(1, 1), (1, 2), (2, 1), (2, None)])
    conn.execute("INSERT INTO pet_detections (file_id, x, y, w, h, person_id) VALUES (3, 0, 0, 1, 1, 3)")
    lat, lng = 40.7128, -74.0060
    conn.execute("INSERT INTO exif_data (file_id, file_name, latitude, longitude, geohash, camera_make, "
                 "camera_model, width, height) VALUES (1, 'f1', ?, ?, ?, 'Apple', 'iPhone 12', 3000, 4000)",
                 (lat, lng, search.geohash(lat, lng, 9)))
    conn.execute("INSERT INTO exif_data (file_id, file_name, latitude, longitude, geohash, width, height) "
                 "VALUES (2, 'f2', 48.8566, 2.3522, ?, 4000, 3000)", (search.geohash(48.8566, 2.3522, 9),))
    conn.execute("INSERT INTO locations (id, name, latitude, longitude) VALUES (7, 'Lower Manhattan', ?, ?)",
                 (lat, lng))
    conn.execute("INSERT INTO file_locations (file_id, location_id) VALUES (1, 7)")
    conn.execute("INSERT INTO events (id, name, start_time, end_time) VALUES "
                 "(5, 'Fourth of July', '2019-07-04', '2019-07-04')")
    conn.execute("INSERT INTO event_files (event_id, file_id) VALUES (5, 1)")
    conn.commit()
    conn.close()
    return client


def _search(client, **params):  # noqa: F811
    r = client.get('/api/catalogs/1/search', query_string=params)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _ids(body):
    return [(x['kind'], x['file_id']) for x in body['results']]


def test_a_turned_away_duplicate_is_found_and_points_to_the_catalog_file(library):
    body = _search(library, q='NOTnew')                         # any part, any case
    assert _ids(body) == [('duplicate', 1)]
    dup = body['results'][0]
    assert dup['duplicate']['source_path'] == '/in/again/file1000-notnew.jpg'
    assert dup['duplicate']['moved_to'] == '/in/DUPS/again/file1000-notnew.jpg'
    assert dup['matched'] == {'on': 'duplicate', 'text': '/in/again/file1000-notnew.jpg'}
    assert dup['thumbnail_url'].endswith('/photos/1/thumbnail?size=320&v=0')
    assert _search(library, q='notnew', duplicates='0')['results'] == []


def test_the_name_a_file_came_with_and_its_folder(library):
    assert _ids(_search(library, q='YKKB8383')) == [('file', 2)]   # not in the stored name
    assert _search(library, q='YKKB8383')['results'][0]['matched']['on'] == 'original'
    assert ('file', 1) in _ids(_search(library, q='holiday'))
    assert _ids(_search(library, q='IMG_0002')) == [('file', 2)]


def test_people_pets_places_and_events_by_name(library):
    assert set(_ids(_search(library, q='person:Ana'))) == {('file', 1), ('file', 2)}
    assert _ids(_search(library, q='person:Ana person:Teso')) == [('file', 1)]    # all of
    assert _ids(_search(library, person='1,2', person_mode='any'))[0][0] == 'file'
    assert _ids(_search(library, q='pet:Rex')) == [('file', 3)]
    assert _ids(_search(library, q='place:Manhattan')) == [('file', 1)]
    assert _ids(_search(library, q='event:"Fourth of July"')) == [('file', 1)]
    assert _ids(_search(library, q='Teso')) == [('file', 1)]                    # a plain word
    assert _ids(_search(library, unnamed_faces='1')) == [('file', 2)]


def test_dates(library):
    assert _ids(_search(library, date='2019')) == [('file', 1)]
    assert _ids(_search(library, q='on:07-04')) == [('file', 3), ('file', 1)]
    assert _ids(_search(library, **{'from': '2019-12-31', 'to': '2020-01-01'})) == [('file', 2)]


def test_near_a_point_by_geohash(library):
    assert _ids(_search(library, near='40.713,-74.006', radius='1km')) == [('file', 1)]
    assert _ids(_search(library, q='near:48.85,2.35,5km')) == [('file', 2)]
    assert _search(library, near='0,0', radius='1km')['results'] == []


def test_a_hash_finds_the_file_and_its_turned_away_copies(library):
    assert _ids(_search(library, sha='aaaaaaaaaa')) == [('file', 1), ('duplicate', 1)]
    r = library.get('/api/catalogs/1/search', query_string={'sha': 'aaaa'})
    assert r.status_code == 400


def test_type_camera_removed(library):
    assert _ids(_search(library, type='video')) == [('file', 3)]
    assert _ids(_search(library, q='camera:iphone')) == [('file', 1)]
    assert ('file', 4) not in _ids(_search(library))
    assert ('file', 4) in _ids(_search(library, removed='1'))


def test_pages_end_to_end_and_a_short_word(library):
    p1 = _search(library, limit=2)
    p2 = _search(library, limit=2, after=p1['next_cursor'])
    assert len(p1['results']) == 2 and len(p2['results']) == 1 and p2['next_cursor'] is None
    assert len({x['file_id'] for x in p1['results'] + p2['results']}) == 3
    assert library.get('/api/catalogs/1/search', query_string={'q': 'ab'}).status_code == 400


def test_a_new_file_is_found_before_the_index_catches_up(library, catalog):  # noqa: F811
    _search(library, q='file1')                                  # builds the index
    conn = _db(catalog)
    search_index.catch_up(os.path.join(catalog.target_directory, 'workflow_results.db'))
    _file(conn, 9, '20230101000000-eeeeeeeeee-brandnew.jpg', '2023-01-01T00:00:00')
    _op(conn, 9, 'move', '/in/later/unseen-name.jpg')
    conn.commit()
    assert search_index.behind(conn) > 0
    hits = search_index.name_hits(conn, 'unseen-name')
    conn.close()
    assert [h['file_id'] for h in hits] == [9]                   # through the tail


def test_saved_searches(library, catalog):  # noqa: F811
    r = library.post('/api/catalogs/1/saved-searches',
                     json={'name': 'Ana', 'query': 'q=person%3AAna', 'add_all': True})
    assert r.status_code == 201, r.get_json()
    s = r.get_json()
    assert s['file_count'] == 2 and s['added'] == 2 and not s['idle']
    sid = s['id']
    # Adding the same again: already there. Ids from a duplicate row are its catalog file.
    r = library.post(f'/api/catalogs/1/saved-searches/{sid}/files', json={'file_ids': [1, 3]})
    assert r.get_json() == {'added': 1, 'already': 1, 'capped': False}
    files = library.get(f'/api/catalogs/1/saved-searches/{sid}/files').get_json()
    assert files['total'] == 3
    assert library.delete(f'/api/catalogs/1/saved-searches/{sid}/files',
                          json={'file_ids': [3]}).get_json() == {'removed': 1}
    assert library.get(f'/api/catalogs/1/saved-searches/{sid}?run=1').get_json()['last_run_at']
    listed = library.get('/api/catalogs/1/saved-searches').get_json()['saved_searches']
    assert [x['name'] for x in listed] == ['Ana']
    assert library.delete(f'/api/catalogs/1/saved-searches/{sid}').status_code == 200
    conn = _db(catalog)
    assert conn.execute('SELECT COUNT(*) FROM file_contents WHERE id IN (1, 2)').fetchone()[0] == 2
    conn.close()


def test_all_results_less_the_ones_unticked(library):
    """Select all, then untick some: the query's results less exclude_ids."""
    every = library.post('/api/catalogs/1/saved-searches',
                         json={'name': 'All', 'query': 'q=person%3AAna', 'add_all': True}).get_json()
    files = library.get(f"/api/catalogs/1/saved-searches/{every['id']}/files").get_json()['files']
    skip = files[0]['file_id']
    r = library.post('/api/catalogs/1/saved-searches',
                     json={'name': 'Most', 'query': 'q=person%3AAna', 'add_all': True,
                           'exclude_ids': [skip]}).get_json()
    assert r['added'] == 1
    empty = library.post('/api/catalogs/1/saved-searches', json={'name': 'Later'}).get_json()
    r = library.post(f"/api/catalogs/1/saved-searches/{empty['id']}/files",
                     json={'query': 'q=person%3AAna', 'exclude_ids': [skip]}).get_json()
    assert r == {'added': 1, 'already': 0, 'capped': False}
    kept = library.get(f"/api/catalogs/1/saved-searches/{empty['id']}/files").get_json()['files']
    assert skip not in [f['file_id'] for f in kept]


def test_a_save_behind_a_workflow_says_the_catalog_is_busy(library, catalog, app_module,  # noqa: F811
                                                            monkeypatch):
    """A workflow holding the write lock: a clear 503, not a bare 500."""
    monkeypatch.setattr(app_module, 'SAVED_SEARCH_WRITE_WAIT', 0.2)
    holder = _db(catalog)
    holder.execute('BEGIN IMMEDIATE')
    try:
        r = library.post('/api/catalogs/1/saved-searches', json={'name': 'Busy', 'file_ids': [1]})
    finally:
        holder.rollback()
        holder.close()
    assert r.status_code == 503
    assert r.get_json()['code'] == 'catalog_busy'
    assert library.post('/api/catalogs/1/saved-searches',
                        json={'name': 'Free', 'file_ids': [1]}).status_code == 201


def test_an_idle_saved_search_is_warned_about_then_deleted(library, catalog):  # noqa: F811
    conn = _db(catalog)
    saved_searches.ensure_schema(conn)
    now = datetime(2026, 10, 19, tzinfo=timezone.utc)
    sid = saved_searches.create(conn, 'old', '')
    conn.execute('UPDATE saved_searches SET last_activity_at = ? WHERE id = ?',
                 ((now - timedelta(days=55)).isoformat(), sid))
    conn.commit()
    row = saved_searches.get(conn, sid)
    life = saved_searches.lifecycle(row, now)
    assert life['idle'] and life['delete_on'].startswith('2026-10-24')
    assert saved_searches.due_soon(conn, now) == 1
    assert saved_searches.sweep(conn, now) == []                 # not yet
    assert saved_searches.sweep(conn, now + timedelta(days=6)) == ['old']
    conn.close()
