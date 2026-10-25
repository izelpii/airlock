#!/usr/bin/env python3
"""
Paging the Locations page.

    python -m pytest test_location_paging.py

WHY. The page fetched every location and drew a card for each. The JSON was
never the problem - 1,041 locations is 337 KB in 0.27 s on `family` - but
every card fetches a MAP TILE and a thumbnail, so one click meant a thousand
cards and some two thousand image requests. A page of cards is a page of
requests.

THE SHAPE IS DELIBERATE. Paged, the endpoint answers with an object; unpaged,
with a bare array. A caller that read a page as though it were the whole list
would show a thousand locations as eighteen, which looks like data loss rather
than a bug - so the two cannot be confused by accident.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _locations(catalog, n, confirmed=0):
    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    for i in range(n):
        conn.execute('INSERT INTO locations (name, confirmed) VALUES (?,?)',
                     (f'Place {i:03d}', confirmed))
    conn.commit()
    conn.close()


def test_unpaged_is_still_a_bare_array(client, catalog):
    """
    The event-tagging picker asks for ?confirmed=1 with no limit, and every
    bundle older than this change expects a list. Breaking that shape would
    empty a dropdown somewhere far from here.
    """
    _locations(catalog, 3)
    body = client.get(f'/api/catalogs/{catalog.id}/locations').get_json()
    assert isinstance(body, list)
    assert len(body) == 3


def test_a_page_carries_the_total_so_the_pager_can_count(client, catalog):
    _locations(catalog, 30)
    body = client.get(f'/api/catalogs/{catalog.id}/locations'
                      f'?limit=10&offset=0').get_json()
    assert isinstance(body, dict)
    assert len(body['locations']) == 10
    assert body['total'] == 30      # the WHOLE list, not the page
    assert body['limit'] == 10 and body['offset'] == 0


def test_the_pages_tile_the_list_exactly(client, catalog):
    """
    No location seen twice, none missed. The LIMIT goes after the ORDER BY, so
    a page is a page of the same ordering the whole list would have had -
    paging a differently-ordered query is what shows one place on two pages
    and another on none.
    """
    _locations(catalog, 25)
    seen = []
    for offset in (0, 10, 20):
        body = client.get(f'/api/catalogs/{catalog.id}/locations'
                          f'?limit=10&offset={offset}').get_json()
        seen.extend(x['id'] for x in body['locations'])
    assert len(seen) == 25
    assert len(set(seen)) == 25, 'a location appeared on two pages'

    whole = client.get(f'/api/catalogs/{catalog.id}/locations').get_json()
    assert seen == [x['id'] for x in whole], \
        'the paged order differs from the unpaged order'


def test_confirmed_first_survives_paging(client, catalog):
    """
    The ordering puts settled places first and the biggest proposals next. A
    page that reordered would scatter the confirmed ones through the list.
    """
    _locations(catalog, 5, confirmed=0)
    _locations(catalog, 2, confirmed=1)
    first = client.get(f'/api/catalogs/{catalog.id}/locations'
                       f'?limit=3&offset=0').get_json()['locations']
    assert [bool(x['confirmed']) for x in first][:2] == [True, True]


def test_an_offset_past_the_end_is_empty_not_an_error(client, catalog):
    """
    Reachable by resizing the window, and by rejecting the last proposal on
    the last page. An error there would be a red banner for a normal action.
    """
    _locations(catalog, 5)
    body = client.get(f'/api/catalogs/{catalog.id}/locations'
                      f'?limit=10&offset=50').get_json()
    assert body['locations'] == []
    assert body['total'] == 5


def test_a_nonsense_limit_is_refused(client, catalog):
    r = client.get(f'/api/catalogs/{catalog.id}/locations?limit=banana')
    assert r.status_code == 400


def test_the_limit_is_capped(client, catalog):
    """
    ?limit=100000 would be the whole list again, with the cards and the map
    tiles that come with it.
    """
    _locations(catalog, 5)
    body = client.get(f'/api/catalogs/{catalog.id}/locations'
                      f'?limit=99999&offset=0').get_json()
    assert body['limit'] <= 500


def test_only_confirmed_can_be_paged_too(client, catalog):
    _locations(catalog, 6, confirmed=0)
    _locations(catalog, 4, confirmed=1)
    body = client.get(f'/api/catalogs/{catalog.id}/locations'
                      f'?confirmed=1&limit=3&offset=0').get_json()
    assert body['total'] == 4, 'the total must count the filtered list'
    assert len(body['locations']) == 3
    assert all(x['confirmed'] for x in body['locations'])
