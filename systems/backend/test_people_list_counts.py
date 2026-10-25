"""
The People page's photograph counts.

    python -m pytest test_people_list_counts.py

GET /people counted each person's photographs with a query per person, and
face_detections has no index on person_id, so every count read the whole
table: 2.4 s for 31 people on `family`. One grouped pass answers the same
question; these pin down that it still answers it the same way.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def test_each_person_counts_distinct_photographs(client, catalog):
    client.get('/api/catalogs/1/people')            # the first request builds the schema
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    conn.execute("INSERT INTO people (id, name) VALUES (1, 'Ana'), (2, 'Ben'), (3, 'Cy')")
    conn.executemany("INSERT INTO file_contents (id, file_path, file_name, file_type, "
                     "master_datetime) VALUES (?, ?, ?, '.jpg', '2021-01-01T10:00:00')",
                     [(i, f'Images/{i}.jpg', f'{i}.jpg') for i in (10, 11, 12)])
    conn.executemany(
        "INSERT INTO face_detections (file_id, x, y, w, h, person_id, source) "
        "VALUES (?, 0, 0, 10, 10, ?, 'Manual')",
        [(10, 1), (10, 1),          # Ana twice in one photograph: one photograph
         (11, 1), (12, 2),
         (12, None)])               # an unnamed face counts for nobody
    conn.commit()
    conn.close()

    people = client.get('/api/catalogs/1/people').get_json()
    assert {p['name']: p['face_count'] for p in people} == {'Ana': 2, 'Ben': 1, 'Cy': 0}


def test_an_empty_catalog_and_the_pet_list_still_answer(client, catalog):
    assert client.get('/api/catalogs/1/people').get_json() == []
    assert client.get('/api/catalogs/1/people?kind=all').status_code == 200
