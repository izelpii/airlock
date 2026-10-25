#!/usr/bin/env python3
"""
The photo gallery's scope, one page at a time.

    python -m pytest test_photo_paging.py

"All" of 2022 on `family` is 29,680 photographs and 19 MB, and "All" of the
catalog is 301,195 and 190 MB - and the strip that shows them draws a dozen
tiles. ?limit=&offset= hands it a page; ?around= hands it the page holding the
photograph already on screen, so widening a scope keeps the user's place
without fetching everything before it.

What has to hold: the pages, laid end to end, ARE the scope - in one fixed
order, with nothing twice and nothing missing - and `position` is the index of
the photograph in that order.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402
from test_photo_calendar import _add  # noqa: E402


def _get(client, catalog, **params):
    r = client.get(f'/api/catalogs/{catalog.id}/photos', query_string=params)
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def _ids(body):
    return [p['id'] for p in body['photos']]


def test_pages_laid_end_to_end_are_the_scope_oldest_first(client, catalog):
    ids = [_add(catalog, f'p{i}.jpg', f'2022-08-{i + 1:02d} 10:00:00')
           for i in range(7)]
    pages = [_get(client, catalog, year=2022, limit=3, offset=o)
             for o in (0, 3, 6)]
    assert [i for pg in pages for i in _ids(pg)] == ids
    assert {pg['total'] for pg in pages} == {7}
    assert (pages[1]['limit'], pages[1]['offset']) == (3, 3)


def test_a_burst_sharing_one_second_never_straddles_pages(client, catalog):
    ids = [_add(catalog, f'b{i}.jpg', '2022-08-01 10:00:00') for i in range(5)]
    seen = [i for o in (0, 2, 4)
            for i in _ids(_get(client, catalog, year=2022, limit=2, offset=o))]
    assert seen == ids


def test_the_total_is_the_scope_not_the_page(client, catalog):
    _add(catalog, 'a.jpg', '2022-08-01 10:00:00')
    _add(catalog, 'b.jpg', '2022-08-02 10:00:00')
    _add(catalog, 'c.jpg', '2022-09-01 10:00:00')
    _add(catalog, 'gone.jpg', '2022-08-03 10:00:00', removed=True)
    _add(catalog, 'clip.mov', '2022-08-04 10:00:00', file_type='.mov')

    assert _get(client, catalog, year=2022, month=8, limit=1)['total'] == 2
    assert _get(client, catalog, year=2022, limit=1)['total'] == 3
    assert _get(client, catalog, limit=1)['total'] == 3


def test_around_returns_the_page_holding_the_photograph(client, catalog):
    ids = [_add(catalog, f'p{i}.jpg', f'2022-0{1 + i // 3}-1{i % 3} 10:00:00')
           for i in range(8)]
    body = _get(client, catalog, year=2022, limit=3, around=ids[4])
    assert body['position'] == 4
    assert body['offset'] == 3
    assert _ids(body) == ids[3:6]


def test_around_is_measured_within_the_scope(client, catalog):
    _add(catalog, 'old.jpg', '2021-12-31 10:00:00')
    first = _add(catalog, 'jan.jpg', '2022-01-01 10:00:00')
    body = _get(client, catalog, year=2022, limit=10, around=first)
    assert body['position'] == 0
    body = _get(client, catalog, limit=10, around=first)
    assert body['position'] == 1


def test_around_a_photograph_outside_the_scope_has_no_position(client, catalog):
    elsewhere = _add(catalog, 'elsewhere.jpg', '2019-05-05 10:00:00')
    _add(catalog, 'here.jpg', '2022-01-01 10:00:00')
    body = _get(client, catalog, year=2022, limit=10, around=elsewhere)
    assert body['position'] is None
    assert body['offset'] == 0
    assert len(body['photos']) == 1


def test_position_breaks_ties_by_id_like_the_pages_do(client, catalog):
    ids = [_add(catalog, f'b{i}.jpg', '2022-08-01 10:00:00') for i in range(4)]
    for want, fid in enumerate(ids):
        assert _get(client, catalog, year=2022, limit=2, around=fid)['position'] == want


def test_the_secondary_of_a_raw_jpeg_pair_is_not_paged(client, catalog):
    primary = _add(catalog, 'pair.jpg', '2022-08-01 10:00:00')
    _add(catalog, 'pair.cr2', '2022-08-01 10:00:00', file_type='.cr2',
         group=1, primary=primary)
    import sqlite3
    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    conn.execute('UPDATE file_contents SET image_group_id = 1 WHERE id = ?',
                 (primary,))
    conn.commit()
    conn.close()
    body = _get(client, catalog, year=2022, limit=10)
    assert _ids(body) == [primary] and body['total'] == 1


def test_without_a_limit_it_is_still_a_bare_array_newest_first(client, catalog):
    a = _add(catalog, 'a.jpg', '2022-08-01 10:00:00')
    b = _add(catalog, 'b.jpg', '2022-08-02 10:00:00')
    body = _get(client, catalog, year=2022)
    assert isinstance(body, list)
    assert [p['id'] for p in body] == [b, a]


def test_a_bad_limit_or_around_is_a_400(client, catalog):
    for q in ('limit=ten', 'limit=10&around=x', 'limit=10&offset=-'):
        r = client.get(f'/api/catalogs/{catalog.id}/photos?{q}')
        assert r.status_code == 400, q


# ── The date: master_datetime, as a range the index answers ─────────────────
#
# master_datetime is THE date of a photograph, decided at ingest and never
# missing. The scoped queries filter on it as a text range so that
# idx_fc_master_datetime serves them: on `family` a day went from 0.45 s (a
# scan) to 0.08 s.

import sqlite3  # noqa: E402


def _set(catalog, fid, **cols):
    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    for col, val in cols.items():
        conn.execute(f'UPDATE file_contents SET {col} = ? WHERE id = ?', (val, fid))
    conn.commit()
    conn.close()


def test_a_photo_is_filed_by_master_datetime_not_added_at(client, catalog):
    fid = _add(catalog, 'p.jpg', '2019-06-01T10:00:00')
    _set(catalog, fid, added_at='2026-10-10 12:00:00')
    assert _ids(_get(client, catalog, year=2019, month=6, day=1, limit=10)) == [fid]
    assert _get(client, catalog, year=2026, limit=10)['total'] == 0


def test_day_one_is_not_days_ten_to_nineteen(client, catalog):
    first = _add(catalog, 'd1.jpg', '2022-01-01T10:00:00')
    _add(catalog, 'd10.jpg', '2022-01-10T10:00:00')
    _add(catalog, 'd19.jpg', '2022-01-19T10:00:00')
    assert _ids(_get(client, catalog, year=2022, month=1, day=1, limit=10)) == [first]


def test_december_does_not_run_into_january(client, catalog):
    dec = _add(catalog, 'dec.jpg', '2022-12-31T23:59:59')
    _add(catalog, 'jan.jpg', '2023-01-01T00:00:00')
    assert _ids(_get(client, catalog, year=2022, month=12, limit=10)) == [dec]
    assert _get(client, catalog, year=2022, limit=10)['total'] == 1


def test_a_day_without_a_month_is_refused(client, catalog):
    r = client.get(f'/api/catalogs/{catalog.id}/photos?year=2022&day=3')
    assert r.status_code == 400


def test_a_scoped_page_is_served_by_the_date_index(app_module, client, catalog):
    """The plan, not the clock: a stopwatch passes on ten rows either way."""
    _add(catalog, 'p.jpg', '2022-08-01T10:00:00')
    _get(client, catalog, year=2022, limit=10)          # schema built
    rng, params = app_module._photo_date_range(2022, 8)
    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    try:
        plan = '\n'.join(str(r[-1]) for r in conn.execute(
            f'EXPLAIN QUERY PLAN SELECT fc.id FROM file_contents fc '
            f'WHERE {app_module._PHOTO_TYPE_FILTER} AND {rng} '
            f'ORDER BY {app_module._PHOTO_DAY_SQL}, fc.id LIMIT 200', params))
    finally:
        conn.close()
    assert 'idx_fc_master_datetime' in plan, plan
    assert 'TEMP B-TREE' not in plan, plan
