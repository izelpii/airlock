#!/usr/bin/env python3
"""
Identify groups, a page of clusters at a time.

    python -m pytest test_face_clusters_paging.py

`family` has 9,479 unresolved face clusters. /faces/clusters returned all of
them with up to 24 members each - 15.5 MB, a member query per cluster - and
the page drew ~227,000 faces, saying "0 groups" for most of a minute while it
did. Now it returns one page, largest first, with totals for all of them.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _get(client, **params):
    r = client.get('/api/catalogs/1/faces/clusters', query_string=params)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _seed(client, catalog, sizes, resolved=()):
    """Clusters of the given sizes, each member a face on its own photo."""
    client.get('/api/catalogs/1/faces/unidentified/summary')     # builds the schema
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    conn.execute("INSERT INTO people (id, name) VALUES (1, 'Ana')")
    face = 100
    ids = []
    for n, size in enumerate(sizes):
        cur = conn.execute("INSERT INTO face_clusters (centroid, member_count, cohesion, "
                           "resolved_person_id) VALUES ('[1,0]', ?, ?, ?)",
                           (size, 0.1 + n / 100, 1 if n in resolved else None))
        cid = cur.lastrowid
        ids.append(cid)
        for _ in range(size):
            conn.execute("INSERT INTO file_contents (id, file_path, file_name, file_type, "
                         "master_datetime) VALUES (?, ?, ?, '.jpg', '2021-01-01T00:00:00')",
                         (face, f'Images/{face}.jpg', f'{face}.jpg'))
            conn.execute("INSERT INTO face_detections (id, file_id, x, y, w, h) "
                         "VALUES (?, ?, 0, 0, 10, 10)", (face, face))
            conn.execute("INSERT INTO face_cluster_members (cluster_id, face_detection_id, "
                         "distance_to_centroid) VALUES (?, ?, 0.1)", (cid, face))
            face += 1
    conn.commit()
    conn.close()
    return ids


def test_a_page_is_the_largest_clusters_with_totals_for_all(client, catalog):
    ids = _seed(client, catalog, sizes=[2, 5, 3, 4, 6])
    body = _get(client, limit=2)
    assert [c['member_count'] for c in body['clusters']] == [6, 5]
    assert body['total'] == 5 and body['total_faces'] == 20
    assert (body['limit'], body['offset']) == (2, 0)
    assert len(body['clusters'][0]['members']) == 6
    assert ids  # seeded


def test_pages_laid_end_to_end_are_every_cluster_once(client, catalog):
    _seed(client, catalog, sizes=[2, 2, 2, 3, 3])
    seen = [c['id'] for o in (0, 2, 4) for c in _get(client, limit=2, offset=o)['clusters']]
    assert len(seen) == 5 and len(set(seen)) == 5


def test_named_clusters_are_left_out_unless_asked_for(client, catalog):
    _seed(client, catalog, sizes=[4, 3, 2], resolved=(0,))
    body = _get(client)
    assert [c['member_count'] for c in body['clusters']] == [3, 2]
    assert body['total'] == 2 and body['total_faces'] == 5
    assert _get(client, include_resolved=1)['total'] == 3


def test_members_are_only_built_for_the_page(client, catalog, app_module, monkeypatch):
    _seed(client, catalog, sizes=[5, 4, 3, 2])
    asked = []
    real = app_module._cluster_person_suggestions
    monkeypatch.setattr(app_module, '_cluster_person_suggestions',
                        lambda conn, ids: asked.append(list(ids)) or real(conn, ids))
    body = _get(client, limit=2, offset=1)
    assert [c['member_count'] for c in body['clusters']] == [4, 3]
    assert asked == [[c['id'] for c in body['clusters']]]


def test_a_bad_limit_is_a_400(client, catalog):
    _seed(client, catalog, sizes=[2])
    r = client.get('/api/catalogs/1/faces/clusters?limit=lots')
    assert r.status_code == 400


def _tag(catalog, face_ids, person=1):
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    conn.executemany("UPDATE face_detections SET person_id = ?, source = 'Manual' WHERE id = ?",
                     [(person, f) for f in face_ids])
    conn.commit()
    conn.close()


def test_a_group_whose_faces_were_all_tagged_elsewhere_is_not_listed(client, catalog):
    """homeserver, 2026-10-19: 1,079 groups whose every face had since been tagged
    one at a time. Listed, each Assign tagged nothing and the group came back."""
    _seed(client, catalog, sizes=[3, 2])          # faces 100-102, then 103-104
    _tag(catalog, [100, 101, 102])
    body = _get(client)
    assert [c['member_count'] for c in body['clusters']] == [2] and body['total'] == 1
    # Part-tagged: only the faces still unnamed are shown and counted.
    _tag(catalog, [103])
    c = _get(client)['clusters'][0]
    assert c['member_count'] == 1 and [m['face_detection_id'] for m in c['members']] == [104]


def test_assigning_a_group_with_nothing_left_resolves_it(client, catalog):
    ids = _seed(client, catalog, sizes=[3])
    _tag(catalog, [100, 101, 102])
    r = client.post(f'/api/catalogs/1/faces/clusters/{ids[0]}/assign', json={'person_id': 1})
    d = r.get_json()
    assert r.status_code == 200 and d['assigned'] == 0 and d['resolved'] is True
    assert _get(client, include_resolved=1)['clusters'][0]['resolved_person_id'] == 1
