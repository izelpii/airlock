"""
The photos a location lists.

    python -m pytest test_location_photos.py

documentation/specs/location-redesign-spec.md section 5. The endpoint joined
event_files -> location_events ONLY, so a location's card could say "5 photos"
from file_locations while its gallery showed none - two honest answers to
different questions. Every location the clustering workflow produces has
file_locations rows and no location_events row at all, so its gallery was always
empty.

The tests below are the ones that would have caught that, and the one that would
have caught it sooner: comparing the count the card shows against the length of
the list the gallery gets. Nothing compared them.
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
    c = sqlite3.connect(os.path.join(catalog.target_directory,
                                     'workflow_results.db'))
    c.row_factory = sqlite3.Row
    return c


def _photo(catalog, file_id, name, when='2019-04-21 10:00:00'):
    c = _conn(catalog)
    c.execute('INSERT INTO file_contents (id, file_name, file_path, file_type, '
              'master_datetime, added_at) VALUES (?, ?, ?, ?, ?, ?)',
              (file_id, name, f'/c/Images/{name}', '.jpg', when, when))
    c.commit()
    c.close()
    return file_id


def _location(catalog, name, confirmed=1):
    c = _conn(catalog)
    cur = c.cursor()
    cur.execute('INSERT INTO locations (name, latitude, longitude, confirmed) '
                'VALUES (?, -33.9, 151.2, ?)', (name, confirmed))
    lid = cur.lastrowid
    c.commit()
    c.close()
    return lid


def _link_direct(catalog, file_id, location_id):
    """The per-file link the resolve workflow writes (location-spec.md 4.1)."""
    c = _conn(catalog)
    c.execute("INSERT INTO file_locations (file_id, location_id, location_source) "
              "VALUES (?, ?, 'cluster')", (file_id, location_id))
    c.commit()
    c.close()


def _link_via_event(catalog, file_id, location_id, event_id=1):
    """The older path: a location tagged onto an event the file belongs to."""
    c = _conn(catalog)
    c.execute('INSERT OR IGNORE INTO events (id, name, start_time, end_time, type) '
              "VALUES (?, 'E', '2019-04-21 09:00:00', '2019-04-21 12:00:00', 'Auto')",
              (event_id,))
    c.execute('INSERT INTO event_files (event_id, file_id) VALUES (?, ?)',
              (event_id, file_id))
    c.execute('INSERT OR IGNORE INTO location_events (location_id, event_id) '
              'VALUES (?, ?)', (location_id, event_id))
    c.commit()
    c.close()


def _gallery(client, location_id, **params):
    res = client.get(f'/api/catalogs/1/locations/{location_id}/photos',
                     query_string=params)
    assert res.status_code == 200, res.get_data(as_text=True)
    body = res.get_json()
    # {'photos': [...], 'years_months': {...}} - the page uses both.
    return body['photos'] if isinstance(body, dict) else body


def _card_count(client, location_id):
    """What the Locations page prints on the card, from file_locations."""
    res = client.get('/api/catalogs/1/locations')
    assert res.status_code == 200
    row = next(r for r in res.get_json() if r['id'] == location_id)
    return row['file_count']


# ─── The bug ──────────────────────────────────────────────────────────────────

def test_a_location_with_only_direct_links_lists_its_photos(catalog, client):
    """
    The case that was broken. Every location the clustering workflow makes is
    this one: file_locations rows, no location_events row.
    """
    loc = _location(catalog, 'Abuela', confirmed=0)
    for i in range(1, 6):
        _link_direct(catalog, _photo(catalog, i, f'IMG_{i}.JPG'), loc)

    photos = _gallery(client, loc)
    assert len(photos) == 5, f'the gallery showed {len(photos)} of 5'


def test_the_card_count_and_the_gallery_agree(catalog, client):
    """
    They disagreed - 5 against 0 - and nothing compared them, which is why it
    took somebody noticing the page to find it.
    """
    loc = _location(catalog, 'Abuela', confirmed=0)
    for i in range(1, 6):
        _link_direct(catalog, _photo(catalog, i, f'IMG_{i}.JPG'), loc)

    assert _card_count(client, loc) == len(_gallery(client, loc))


def test_the_event_path_still_works(catalog, client):
    """
    A location somebody tagged onto an event still lists that event's photos.
    The direct link replaced the event path as the PRIMARY one; it did not
    remove it (location-spec.md 4.7).
    """
    loc = _location(catalog, 'Rehearsal hall')
    for i in range(1, 4):
        _link_via_event(catalog, _photo(catalog, i, f'IMG_{i}.JPG'), loc)

    assert len(_gallery(client, loc)) == 3


def test_a_photo_reachable_both_ways_is_listed_once(catalog, client):
    """UNION, not UNION ALL."""
    loc = _location(catalog, 'Both')
    fid = _photo(catalog, 1, 'IMG_1.JPG')
    _link_direct(catalog, fid, loc)
    _link_via_event(catalog, fid, loc)

    photos = _gallery(client, loc)
    assert len(photos) == 1, f'listed {len(photos)} times'


def test_both_kinds_of_link_are_listed_together(catalog, client):
    loc = _location(catalog, 'Mixed')
    _link_direct(catalog, _photo(catalog, 1, 'IMG_1.JPG'), loc)
    _link_via_event(catalog, _photo(catalog, 2, 'IMG_2.JPG'), loc)

    assert len(_gallery(client, loc)) == 2


# ─── Still true of the things that already worked ─────────────────────────────

def test_a_removed_photo_is_not_listed(catalog, client):
    loc = _location(catalog, 'Abuela')
    fid = _photo(catalog, 1, 'IMG_1.JPG')
    _link_direct(catalog, fid, loc)
    c = _conn(catalog)
    c.execute("UPDATE file_contents SET removed_at = '2026-10-07' WHERE id = ?",
              (fid,))
    c.commit()
    c.close()

    assert _gallery(client, loc) == []


def test_the_year_filter_applies_to_both_halves(catalog, client):
    """
    The filters are built once and their parameters supplied twice. A filter
    that reached only one half of the union would quietly over-report.
    """
    loc = _location(catalog, 'Abuela')
    _link_direct(catalog, _photo(catalog, 1, 'A.JPG', '2019-04-21 10:00:00'), loc)
    _link_via_event(catalog, _photo(catalog, 2, 'B.JPG', '2019-05-02 10:00:00'), loc)
    _link_direct(catalog, _photo(catalog, 3, 'C.JPG', '2021-07-01 10:00:00'), loc)
    _link_via_event(catalog, _photo(catalog, 4, 'D.JPG', '2021-08-01 10:00:00'), loc)

    assert len(_gallery(client, loc, year=2019)) == 2
    assert len(_gallery(client, loc, year=2021)) == 2
    assert len(_gallery(client, loc, year=2019, month=4)) == 1


def test_a_location_with_no_photos_lists_none(catalog, client):
    assert _gallery(client, _location(catalog, 'Empty')) == []


def test_an_unknown_location_is_a_404(catalog, client):
    # `catalog` as well as `client`, even though nothing is put in it: that
    # fixture is what patches Catalog.get_by_id. Without it this passed alone
    # and failed in the full suite, where another test had left a real catalog
    # for get_by_id to find.
    res = client.get('/api/catalogs/1/locations/999/photos')
    assert res.status_code == 404


# ─── Paging ───────────────────────────────────────────────────────────────────
#
# The gallery shows twelve tiles and used to be sent every photograph of the
# location to draw them - 9,257 on location 347 of `family`.

def _page(client, location_id, **params):
    res = client.get(f'/api/catalogs/1/locations/{location_id}/photos',
                     query_string=params)
    assert res.status_code == 200, res.get_data(as_text=True)
    return res.get_json()


def test_a_page_is_the_slice_of_the_whole_list(catalog, client):
    loc = _location(catalog, 'Abuela')
    for i in range(1, 8):
        _link_direct(catalog, _photo(catalog, i, f'IMG_{i}.JPG',
                                     f'2019-04-2{i} 10:00:00'), loc)
    everything = [p['file_id'] for p in _gallery(client, loc)]

    pages = [_page(client, loc, limit=3, offset=o) for o in (0, 3, 6)]
    assert [len(p['photos']) for p in pages] == [3, 3, 1]
    assert [p['file_id'] for pg in pages for p in pg['photos']] == everything
    assert {p['total'] for p in pages} == {7}
    assert (pages[1]['limit'], pages[1]['offset']) == (3, 3)


def test_the_total_counts_both_kinds_of_link_once(catalog, client):
    loc = _location(catalog, 'Mixed')
    both = _photo(catalog, 1, 'IMG_1.JPG')
    _link_direct(catalog, both, loc)
    _link_via_event(catalog, both, loc)
    _link_via_event(catalog, _photo(catalog, 2, 'IMG_2.JPG'), loc)

    assert _page(client, loc, limit=12)['total'] == 2


def test_the_year_filter_applies_to_the_total(catalog, client):
    loc = _location(catalog, 'Abuela')
    _link_direct(catalog, _photo(catalog, 1, 'A.JPG', '2019-04-21 10:00:00'), loc)
    _link_direct(catalog, _photo(catalog, 2, 'B.JPG', '2021-07-01 10:00:00'), loc)

    body = _page(client, loc, limit=12, year=2019)
    assert body['total'] == 1
    assert [p['file_name'] for p in body['photos']] == ['A.JPG']


def test_photographs_from_one_second_do_not_straddle_pages(catalog, client):
    """A burst shares its timestamp; the id breaks the tie the same way each time."""
    loc = _location(catalog, 'Burst')
    for i in range(1, 6):
        _link_direct(catalog, _photo(catalog, i, f'IMG_{i}.JPG',
                                     '2019-04-21 10:00:00'), loc)

    seen = [p['file_id'] for o in (0, 2, 4)
            for p in _page(client, loc, limit=2, offset=o)['photos']]
    assert seen == [1, 2, 3, 4, 5]


def test_without_a_limit_it_is_still_everything(catalog, client):
    loc = _location(catalog, 'Abuela')
    for i in range(1, 4):
        _link_direct(catalog, _photo(catalog, i, f'IMG_{i}.JPG'), loc)

    body = _page(client, loc)
    assert len(body['photos']) == 3
    assert 'total' not in body


def test_a_bad_limit_is_a_400(catalog, client):
    loc = _location(catalog, 'Abuela')
    res = client.get(f'/api/catalogs/1/locations/{loc}/photos?limit=twelve')
    assert res.status_code == 400
