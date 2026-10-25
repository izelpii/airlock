#!/usr/bin/env python3
"""
Identify people's candidates, counted and paged on the server.

    python -m pytest test_identify_people_paging.py

The page used to receive every unidentified face in the catalog, each with its
best match - on `family` 469,839 faces and 188 MB per visit - and count and
page them in the browser. Now /faces/unidentified/summary counts per person
and /faces/unidentified returns one person's candidates a page at a time.

What has to survive the move is the page's own definition of a candidate: a
face counts for the ONE person who is its best match, and only when the
similarity its card shows - rounded - meets the minimum.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _db(catalog):
    return sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))


def _summary(client, **params):
    r = client.get('/api/catalogs/1/faces/unidentified/summary', query_string=params)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _page(client, **params):
    r = client.get('/api/catalogs/1/faces/unidentified', query_string=params)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _ids(body):
    return [f['face_id'] for f in body['faces']]


class World:
    """People, photos, faces and scores, written straight into the catalog."""

    def __init__(self, client, catalog):
        _summary(client)                     # the first request builds the schema
        self.catalog = catalog
        conn = _db(catalog)
        conn.execute("INSERT INTO people (id, name) VALUES (1, 'Ana'), (2, 'Ben')")
        # A reference face per person, which the scores point at.
        conn.execute("INSERT INTO file_contents (id, file_path, file_name, file_type, "
                     "master_datetime) VALUES (1, 'Images/ref.jpg', 'ref.jpg', '.jpg', "
                     "'2020-01-01T10:00:00')")
        conn.execute("INSERT INTO face_detections (id, file_id, x, y, w, h, person_id, "
                     "source) VALUES (1, 1, 0, 0, 10, 10, 1, 'Manual'), "
                     "(2, 1, 0, 0, 10, 10, 2, 'Manual')")
        conn.commit()
        conn.close()
        self.next_id = 100

    def face(self, scores, source='Automatic', removed=False, contextual=None):
        """An unidentified face, with {person_id: cosine distance} scores."""
        fid = self.next_id
        self.next_id += 1
        conn = _db(self.catalog)
        conn.execute("INSERT INTO file_contents (id, file_path, file_name, file_type, "
                     "master_datetime, removed_at) VALUES (?, ?, ?, '.jpg', "
                     "'2021-05-05T10:00:00', ?)",
                     (fid, f'Images/{fid}.jpg', f'{fid}.jpg',
                      '2026-01-01' if removed else None))
        conn.execute("INSERT INTO face_detections (id, file_id, x, y, w, h, source) "
                     "VALUES (?, ?, 0, 0, 10, 10, ?)", (fid, fid, source))
        for pid, d in scores.items():
            conn.execute("INSERT INTO face_similarity_scores (query_face_detection_id, "
                         "reference_face_detection_id, person_id, distance, threshold, "
                         "contextual_distance) VALUES (?, ?, ?, ?, 0.4, ?)",
                         (fid, pid, pid, d, (contextual or {}).get(pid)))
        conn.commit()
        conn.close()
        return fid


def _people(summary):
    return {int(k): (v['candidates'], v['matched']) for k, v in summary['people'].items()}


# ── what counts ──────────────────────────────────────────────────────────────

def test_a_face_counts_only_for_its_best_match(client, catalog):
    w = World(client, catalog)
    w.face({1: 0.20, 2: 0.30})               # Ana's, not Ben's
    w.face({2: 0.10})
    s = _summary(client, metric='cosine')
    assert _people(s) == {1: (1, 1), 2: (1, 1)}
    assert s['has_scores'] and s['has_unidentified']


def test_the_minimum_compares_the_rounded_similarity_the_card_shows(client, catalog):
    w = World(client, catalog)
    w.face({1: 0.405})                       # card shows round(59.5) = 60
    w.face({1: 0.406})                       # card shows round(59.4) = 59
    assert _people(_summary(client, min_similarity=60)) == {1: (1, 2)}
    assert _page(client, person_id=1, min_similarity=60)['total'] == 1


def test_a_face_that_is_named_manual_or_removed_is_not_a_candidate(client, catalog):
    w = World(client, catalog)
    keep = w.face({1: 0.2})
    named = w.face({1: 0.2})
    w.face({1: 0.2}, source='Manual')
    w.face({1: 0.2}, removed=True)
    conn = _db(catalog)
    conn.execute('UPDATE face_detections SET person_id = 2 WHERE id = ?', (named,))
    conn.commit()
    conn.close()
    assert _ids(_page(client, person_id=1)) == [keep]
    assert _people(_summary(client)) == {1: (1, 1)}


def test_rejecting_a_candidate_hands_the_face_to_its_next_best(client, catalog):
    w = World(client, catalog)
    fid = w.face({1: 0.2, 2: 0.3})
    r = client.delete(f'/api/catalogs/1/faces/{fid}/similarity-scores/1')
    assert r.status_code == 200, r.get_json()
    assert _people(_summary(client)) == {2: (1, 1)}
    assert _ids(_page(client, person_id=2)) == [fid]


def test_the_contextual_metric_uses_its_own_column(client, catalog):
    w = World(client, catalog)
    w.face({1: 0.2})                                         # no contextual score
    ctx = w.face({1: 0.2, 2: 0.25}, contextual={2: 0.1})     # contextual best is Ben
    assert _people(_summary(client, metric='contextual')) == {2: (1, 1)}
    body = _page(client, person_id=2, metric='contextual')
    assert _ids(body) == [ctx]
    best = body['faces'][0]['best_match']
    assert best['metric'] == 'contextual'
    assert best['metric_distance'] == 0.1 and best['distance'] == 0.25


# ── paging ───────────────────────────────────────────────────────────────────

def test_pages_are_closest_first_and_laid_end_to_end_are_everything(client, catalog):
    w = World(client, catalog)
    by_distance = [w.face({1: d}) for d in (0.30, 0.10, 0.20, 0.25, 0.15)]
    expected = [by_distance[i] for i in (1, 4, 2, 3, 0)]
    pages = [_page(client, person_id=1, limit=2, offset=o) for o in (0, 2, 4)]
    assert [i for p in pages for i in _ids(p)] == expected
    assert {p['total'] for p in pages} == {5}
    assert (pages[1]['limit'], pages[1]['offset']) == (2, 2)


def test_a_card_has_what_it_draws(client, catalog):
    w = World(client, catalog)
    fid = w.face({1: 0.2})
    face = _page(client, person_id=1)['faces'][0]
    assert face['face_id'] == fid
    assert face['file_path'] == f'Images/{fid}.jpg'
    assert face['master_datetime'] == '2021-05-05T10:00:00'
    assert {'x', 'y', 'w', 'h', 'image_width', 'image_height', 'ai_crop_path'} <= set(face)
    assert face['best_match']['person_id'] == 1
    assert face['best_match']['person_name'] == 'Ana'
    assert face['best_match']['distance'] == 0.2


def test_past_the_last_page_is_empty_but_still_knows_the_total(client, catalog):
    w = World(client, catalog)
    w.face({1: 0.2})
    body = _page(client, person_id=1, limit=10, offset=10)
    assert body['faces'] == [] and body['total'] == 1


def test_there_is_no_unpaged_everything(client, catalog):
    World(client, catalog)
    r = client.get('/api/catalogs/1/faces/unidentified')
    assert r.status_code == 400              # person_id is required


def test_nonsense_is_refused(client, catalog):
    World(client, catalog)
    for q in ('person_id=x', 'person_id=1&limit=ten', 'person_id=1&min_similarity=hi'):
        assert client.get(f'/api/catalogs/1/faces/unidentified?{q}').status_code == 400, q
    assert client.get('/api/catalogs/1/faces/unidentified/summary?min_similarity=hi'
                      ).status_code == 400


def test_an_empty_catalog_has_no_scores_and_nothing_waiting(client, catalog):
    s = _summary(client)
    assert s['people'] == {} and not s['has_scores'] and not s['has_unidentified']
