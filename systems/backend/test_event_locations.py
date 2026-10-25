"""
Adding a location from an event.

    python -m pytest test_event_locations.py

POST /events/<id>/locations still passed lat=/long= to
LocationModel.create_location after v4.13 renamed its parameters to
latitude/longitude, so every save from the Events and Event pages raised
TypeError and the page said "Failed to save location".
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


@pytest.fixture
def event(client, catalog):
    client.get('/api/catalogs/1/locations')          # the first request builds the schema
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    conn.execute("INSERT INTO events (id, name, start_time, end_time) VALUES "
                 "(8, 'Picnic', '2026-06-01 10:00', '2026-06-01 16:00')")
    conn.commit()
    conn.close()
    return 8


# Both spellings: the Events and Event pages send lat/long; latitude/longitude
# is the canonical one (location-spec.md 9.3).
@pytest.mark.parametrize('lat_key, long_key', [('lat', 'long'), ('latitude', 'longitude')])
def test_a_location_saved_from_an_event_keeps_its_coordinates(client, event,
                                                              lat_key, long_key):
    r = client.post(f'/api/catalogs/1/events/{event}/locations', json={
        'name': 'Lake house', lat_key: 33.2, long_key: -97.1,
        'description': None, 'source': 'Manual'})
    assert r.status_code == 201, r.get_json()
    loc = r.get_json()
    assert (loc['name'], loc['latitude'], loc['longitude']) == ('Lake house', 33.2, -97.1)

    linked = client.get(f'/api/catalogs/1/events/{event}/locations').get_json()
    assert [l['id'] for l in linked] == [loc['id']]
