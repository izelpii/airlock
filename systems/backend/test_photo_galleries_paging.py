#!/usr/bin/env python3
"""
A pet's and a person's photographs, a page at a time.

    python -m pytest test_photo_galleries_paging.py

The pet page asked for every photograph of the pet - capped, silently, at
500: Luna has ~5,000, so her page drew 500 thumbnails at once, said "500",
and the rest could not be reached. The person page asked once per year and
merged every answer in the browser to show twelve. Both now page on the
server and say how many there are in all.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402
from test_pet_api import _person  # noqa: E402


def _db(catalog):
    return sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))


def _files(catalog, dates):
    """One file per date, ids from 100."""
    conn = _db(catalog)
    ids = []
    for i, when in enumerate(dates):
        fid = 100 + i
        conn.execute('INSERT INTO file_contents (id, file_path, file_name, file_type, '
                     'master_datetime) VALUES (?, ?, ?, \'.jpg\', ?)',
                     (fid, f'Images/{fid}.jpg', f'{fid}.jpg', when))
        ids.append(fid)
    conn.commit()
    conn.close()
    return ids


# ── pets ─────────────────────────────────────────────────────────────────────

def _pet_with(client, catalog, dates):
    pet = _person(client, catalog, 'Luna', kind='pet', species='dog')
    conn = _db(catalog)
    for fid in _files(catalog, dates):
        conn.execute('INSERT INTO pet_detections (file_id, x, y, w, h, person_id, '
                     'species, confidence) VALUES (?, 0, 0, 10, 10, ?, \'dog\', 0.9)',
                     (fid, pet))
    conn.commit()
    conn.close()
    return pet


def _pet_page(client, catalog, pet, **params):
    r = client.get(f'/api/catalogs/{catalog.id}/pets/{pet}/detections', query_string=params)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_a_pets_page_is_newest_first_with_the_whole_count(client, catalog):
    pet = _pet_with(client, catalog, ['2019-01-01T10:00:00', '2021-06-01T10:00:00',
                                      '2021-07-01T10:00:00', '2023-03-01T10:00:00'])
    body = _pet_page(client, catalog, pet, limit=2)
    assert [d['master_datetime'][:4] for d in body['detections']] == ['2023', '2021']
    assert body['total'] == 4
    assert body['years'] == {'2019': 1, '2021': 2, '2023': 1}
    rest = _pet_page(client, catalog, pet, limit=2, offset=2)['detections']
    assert [d['master_datetime'][:7] for d in rest] == ['2021-06', '2019-01']


def test_a_pet_with_more_than_500_photographs_is_all_reachable(client, catalog):
    dates = [f'2020-01-01T{h:02d}:{m:02d}:00' for h in range(11) for m in range(60)][:620]
    pet = _pet_with(client, catalog, dates)
    assert _pet_page(client, catalog, pet, limit=10)['total'] == 620
    assert len(_pet_page(client, catalog, pet, limit=50, offset=600)['detections']) == 20


def test_a_pets_year_filter_counts_that_year(client, catalog):
    pet = _pet_with(client, catalog, ['2019-01-01T10:00:00', '2021-06-01T10:00:00'])
    body = _pet_page(client, catalog, pet, year=2021)
    assert body['total'] == 1 and len(body['detections']) == 1


# ── people ───────────────────────────────────────────────────────────────────

def _person_in(client, catalog, dates, faces_per_file=1):
    pid = _person(client, catalog, 'Ana')
    conn = _db(catalog)
    for fid in _files(catalog, dates):
        for _ in range(faces_per_file):
            conn.execute('INSERT INTO face_detections (file_id, x, y, w, h, person_id) '
                         'VALUES (?, 0, 0, 10, 10, ?)', (fid, pid))
    conn.commit()
    conn.close()
    return pid


def _person_page(client, catalog, pid, **params):
    r = client.get(f'/api/catalogs/{catalog.id}/people/{pid}/detected-photos',
                   query_string=params)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_a_persons_photos_span_every_year_in_one_paged_list(client, catalog):
    pid = _person_in(client, catalog, ['2015-05-05T10:00:00', '2019-01-01T10:00:00',
                                       '2026-02-02T10:00:00'])
    body = _person_page(client, catalog, pid, limit=2)
    assert body['total'] == 3
    assert [p['master_datetime'][:4] for p in body['photos']] == ['2015', '2019']
    assert [p['master_datetime'][:4]
            for p in _person_page(client, catalog, pid, limit=2, offset=2)['photos']] == ['2026']


def test_a_photo_with_the_person_twice_is_one_card(client, catalog):
    pid = _person_in(client, catalog, ['2019-01-01T10:00:00'], faces_per_file=2)
    assert _person_page(client, catalog, pid)['total'] == 1


def test_a_persons_year_filter(client, catalog):
    pid = _person_in(client, catalog, ['2015-05-05T10:00:00', '2019-01-01T10:00:00'])
    body = _person_page(client, catalog, pid, year=2019)
    assert body['total'] == 1 and body['photos'][0]['master_datetime'].startswith('2019')
    assert body['photos'][0]['display_path']


def test_nonsense_is_refused(client, catalog):
    pid = _person_in(client, catalog, ['2019-01-01T10:00:00'])
    for q in ('limit=x', 'year=x', 'offset=-'):
        r = client.get(f'/api/catalogs/{catalog.id}/people/{pid}/detected-photos?{q}')
        assert r.status_code == 400, q
