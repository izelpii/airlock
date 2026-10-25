"""
The location map endpoint.

    python -m pytest test_location_map.py

documentation/specs/location-map-spec.md section 8. The first test is the one
that matters: a marker's count has to come through the FILES, because
location_events is empty on every real catalog and a map built on it shows
every marker reading zero while looking entirely correct.
"""

import os
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

from test_photo_dates_batch import app_module, catalog, client  # noqa: E402,F401


def _conn(catalog):
    c = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    c.row_factory = sqlite3.Row
    return c


def _location(catalog, name, lat, lng, confirmed=0):
    c = _conn(catalog)
    cur = c.cursor()
    cur.execute('INSERT INTO locations (name, latitude, longitude, confirmed) '
                'VALUES (?, ?, ?, ?)', (name, lat, lng, confirmed))
    lid = cur.lastrowid
    c.commit()
    c.close()
    return lid


def _photo_at(catalog, file_id, location_id, when='2019-04-21 10:00:00',
              event_id=None):
    """A file resolved to a location, optionally also inside an event."""
    c = _conn(catalog)
    c.execute('INSERT INTO file_contents (id, file_name, file_path, file_type, '
              'master_datetime, added_at) VALUES (?, ?, ?, ?, ?, ?)',
              (file_id, f'IMG_{file_id}.JPG', f'/c/IMG_{file_id}.JPG', '.jpg',
               when, when))
    c.execute("INSERT INTO file_locations (file_id, location_id, location_source) "
              "VALUES (?, ?, 'cluster')", (file_id, location_id))
    if event_id is not None:
        c.execute('INSERT OR IGNORE INTO events (id, name, start_time, end_time, type) '
                  "VALUES (?, ?, ?, ?, 'Auto')",
                  (event_id, f'E{event_id}', when, when))
        c.execute('INSERT INTO event_files (event_id, file_id) VALUES (?, ?)',
                  (event_id, file_id))
    c.commit()
    c.close()


def _map(client, **params):
    res = client.get('/api/catalogs/1/locations/map', query_string=params)
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


# ─── Where the count comes from (spec section 3) ──────────────────────────────

def test_events_are_counted_through_the_files(catalog, client):
    """
    THE test. location_events is empty - as it is on every real catalog - and
    the events still have to be found, through the files resolved to the
    location.
    """
    loc = _location(catalog, 'Abuela', -33.9, 151.2)
    _photo_at(catalog, 1, loc, event_id=10)
    _photo_at(catalog, 2, loc, event_id=10)
    _photo_at(catalog, 3, loc, event_id=11)

    c = _conn(catalog)
    assert c.execute('SELECT COUNT(*) FROM location_events').fetchone()[0] == 0
    c.close()

    body = _map(client, zoom=12)
    assert len(body['groups']) == 1
    assert body['groups'][0]['events'] == 2, 'two distinct events across three files'
    assert body['groups'][0]['files'] == 3


def test_an_event_spanning_several_files_counts_once(catalog, client):
    loc = _location(catalog, 'Abuela', -33.9, 151.2)
    for fid in range(1, 8):
        _photo_at(catalog, fid, loc, event_id=10)
    assert _map(client, zoom=12)['groups'][0]['events'] == 1


def test_a_location_with_no_events_still_appears(catalog, client):
    """The LEFT JOIN. A location with photos and no event is a real thing, and
    hiding it would make this map disagree with the Locations page."""
    loc = _location(catalog, 'Quiet', 10.0, 20.0)
    _photo_at(catalog, 1, loc)              # no event
    body = _map(client, zoom=12)
    assert len(body['groups']) == 1
    assert body['groups'][0]['events'] == 0
    assert body['groups'][0]['files'] == 1


def test_a_location_without_coordinates_is_not_mapped(catalog, client):
    """There is nowhere to put it."""
    c = _conn(catalog)
    cur = c.cursor()
    cur.execute("INSERT INTO locations (name, confirmed) VALUES ('Nowhere', 1)")
    nowhere = cur.lastrowid
    c.commit()
    c.close()
    _photo_at(catalog, 1, nowhere, event_id=10)
    assert _map(client, zoom=12)['groups'] == []


def test_a_removed_photo_does_not_count(catalog, client):
    loc = _location(catalog, 'Abuela', -33.9, 151.2)
    _photo_at(catalog, 1, loc, event_id=10)
    c = _conn(catalog)
    c.execute("UPDATE file_contents SET removed_at = '2026-10-08' WHERE id = 1")
    c.commit()
    c.close()
    body = _map(client, zoom=12)
    assert body['groups'][0]['files'] == 0
    assert body['groups'][0]['events'] == 0


# ─── Grouping (spec section 4) ────────────────────────────────────────────────

@pytest.mark.parametrize('zoom,expected', [(3, 0), (5, 0), (6, 1), (8, 1),
                                           (9, 2), (11, 2), (12, 3), (19, 3)])
def test_zoom_chooses_the_rounding(catalog, client, zoom, expected):
    _location(catalog, 'A', 1.0, 2.0)
    assert _map(client, zoom=zoom)['round_to'] == expected


def test_an_absent_or_silly_zoom_clamps_rather_than_failing(catalog, client):
    _location(catalog, 'A', 1.0, 2.0)
    assert _map(client)['round_to'] in (0, 1, 2, 3)
    assert _map(client, zoom=99)['round_to'] == 3
    assert _map(client, zoom=-5)['round_to'] == 0
    assert _map(client, zoom='banana')['round_to'] in (0, 1, 2, 3)


def test_nearby_locations_merge_as_you_zoom_out(catalog, client):
    """
    Two locations 0.02° apart - about 2 km. Separate at 111 m cells, one marker
    at 1.1 km cells.
    """
    a = _location(catalog, 'A', 10.000, 20.000)
    b = _location(catalog, 'B', 10.020, 20.000)
    _photo_at(catalog, 1, a, event_id=10)
    _photo_at(catalog, 2, b, event_id=11)

    assert len(_map(client, zoom=12)['groups']) == 2      # 3 dp
    merged = _map(client, zoom=5)['groups']               # 0 dp
    assert len(merged) == 1
    assert merged[0]['locations'] == 2
    assert merged[0]['events'] == 2
    assert merged[0]['name'] is None, 'a group of several has no single name'


def test_a_group_of_one_carries_its_name_and_id(catalog, client):
    loc = _location(catalog, 'Abuela', -33.9, 151.2, confirmed=1)
    _photo_at(catalog, 1, loc, event_id=10)
    g = _map(client, zoom=12)['groups'][0]
    assert g['locations'] == 1
    assert g['name'] == 'Abuela'
    assert g['ids'] == [loc]
    assert g['verified'] == 1


def test_the_marker_sits_at_the_mean_not_the_grid_point(catalog, client):
    """Rounding decides the grouping; it must not decide the position, or the
    markers land on a lattice instead of on the places."""
    a = _location(catalog, 'A', 10.00, 20.00)
    b = _location(catalog, 'B', 10.40, 20.00)
    _photo_at(catalog, 1, a)
    _photo_at(catalog, 2, b)
    g = _map(client, zoom=5)['groups'][0]       # 0 dp -> one group
    assert g['locations'] == 2
    assert g['lat'] == pytest.approx(10.20), 'the mean of 10.00 and 10.40'


def test_the_groups_sum_to_the_total_at_every_zoom(catalog, client):
    """Grouping must neither lose nor duplicate a location."""
    for i in range(12):
        loc = _location(catalog, f'L{i}', 10 + i * 0.3, 20 + i * 0.3)
        _photo_at(catalog, 100 + i, loc, event_id=200 + i)
    for zoom in (3, 6, 9, 12):
        body = _map(client, zoom=zoom)
        assert sum(g['locations'] for g in body['groups']) == body['total']['locations'] == 12
        assert sum(g['events'] for g in body['groups']) == body['total']['events'] == 12


# ─── The year filter (spec section 5) ─────────────────────────────────────────

def test_the_year_filter_changes_which_locations_appear(catalog, client):
    """
    Not only the counts. On the measured catalog 2011 has one location and 2024
    has 235, and that is the point of the control.
    """
    old = _location(catalog, 'Old', 10.0, 20.0)
    new = _location(catalog, 'New', 30.0, 40.0)
    _photo_at(catalog, 1, old, when='2011-06-01 10:00:00', event_id=10)
    _photo_at(catalog, 2, new, when='2024-06-01 10:00:00', event_id=11)

    assert _map(client, zoom=12)['total']['locations'] == 2
    only2011 = _map(client, zoom=12, **{'from': 2011, 'to': 2011})
    assert only2011['total']['locations'] == 1
    assert only2011['groups'][0]['name'] == 'Old'
    assert _map(client, zoom=12, **{'from': 2024, 'to': 2024})['groups'][0]['name'] == 'New'


def test_the_year_filter_also_narrows_the_events(catalog, client):
    loc = _location(catalog, 'Abuela', -33.9, 151.2)
    _photo_at(catalog, 1, loc, when='2019-04-21 10:00:00', event_id=10)
    _photo_at(catalog, 2, loc, when='2024-04-21 10:00:00', event_id=11)
    assert _map(client, zoom=12)['groups'][0]['events'] == 2
    narrowed = _map(client, zoom=12, **{'from': 2019, 'to': 2019})
    assert narrowed['groups'][0]['events'] == 1
    assert narrowed['groups'][0]['files'] == 1


def test_the_year_range_is_not_narrowed_by_the_filter(catalog, client):
    """
    The slider's own extent must not move as it is dragged, so `years` is
    computed over everything regardless of from/to.
    """
    a = _location(catalog, 'Old', 10.0, 20.0)
    b = _location(catalog, 'New', 30.0, 40.0)
    _photo_at(catalog, 1, a, when='2011-06-01 10:00:00')
    _photo_at(catalog, 2, b, when='2024-06-01 10:00:00')

    full = _map(client, zoom=12)['years']
    narrowed = _map(client, zoom=12, **{'from': 2024, 'to': 2024})['years']
    assert full == narrowed
    assert narrowed['min'] == 2011 and narrowed['max'] == 2024


def test_the_year_counts_are_locations_per_year(catalog, client):
    a = _location(catalog, 'A', 10.0, 20.0)
    b = _location(catalog, 'B', 30.0, 40.0)
    _photo_at(catalog, 1, a, when='2019-01-01 10:00:00')
    _photo_at(catalog, 2, a, when='2019-02-01 10:00:00')   # same location
    _photo_at(catalog, 3, b, when='2019-03-01 10:00:00')
    counts = _map(client, zoom=12)['years']['counts']
    assert counts['2019'] == 2, 'two locations, not three files'


# ─── Degrading ────────────────────────────────────────────────────────────────

def test_an_empty_catalog_is_an_empty_map(catalog, client):
    body = _map(client, zoom=5)
    assert body['groups'] == []
    assert body['total'] == {'locations': 0, 'events': 0, 'files': 0}
    assert body['years']['min'] is None


def test_an_unknown_catalog_is_a_404(catalog, client):
    # `catalog` as well as `client`, though nothing is put in it: that fixture
    # is what patches Catalog.get_by_id to answer only for id 1. Without it
    # this passes alone and fails in the full suite, where another test has
    # left a real catalog for get_by_id to find. Second time in this codebase -
    # test_location_photos.py has the same note.
    res = client.get('/api/catalogs/999/locations/map')
    assert res.status_code == 404
