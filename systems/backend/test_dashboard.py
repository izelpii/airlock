"""
The Dashboard: one request, cached counts, live activity.

    python -m pytest test_dashboard.py

documentation/specs/dashboard-specs.md. What has to hold: the numbers are the
library's, the expensive ones come from a cache that is rebuilt when the
catalog moves, a finished workflow or twelve hours make it stale, a stale cache
is still served at once, and On this day is computed once a day.
"""

import json
import os
import sqlite3
import sys
import time
from datetime import date, datetime, timedelta

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import dashboard  # noqa: E402
from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402

TODAY = date.today()


def _db(catalog):
    return sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))


def _file(conn, fid, name, when, ftype='.jpg', size=1000, removed=None):
    conn.execute("INSERT INTO file_contents (id, file_path, file_name, file_type, file_size, "
                 "master_datetime, removed_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                 (fid, f'Images/{name}', name, ftype, size, when, removed))


@pytest.fixture
def library(client, catalog):
    client.get('/api/catalogs/1/people')                 # builds the schema
    conn = _db(catalog)
    md = TODAY.strftime('%m-%d')
    _file(conn, 1, 'a.jpg', f'2019-{md}T10:00:00')        # on this day, 2019
    _file(conn, 2, 'b.jpg', f'2019-{md}T11:00:00')        # on this day, 2019 again
    _file(conn, 3, 'c.jpg', f'2021-{md}T09:00:00')        # on this day, 2021
    _file(conn, 4, 'd.mov', '2021-03-01T09:00:00', ftype='.mov', size=5000)
    _file(conn, 5, 'e.jpg', '2022-03-01T09:00:00')
    _file(conn, 6, 'gone.jpg', '2022-03-02T09:00:00', removed='2026-01-01')
    soon = (TODAY + timedelta(days=3)).strftime('%m-%d')
    conn.execute("INSERT INTO people (id, name, date_of_birth) VALUES (1, 'Ana', ?), "
                 "(2, 'Ben', NULL)", (f'2015-{soon}',))
    conn.execute("INSERT INTO events (name, start_time, end_time) VALUES "
                 "('Trip', '2021-03-01', '2021-03-02')")
    conn.executemany("INSERT INTO face_detections (file_id, x, y, w, h, person_id, source) "
                     "VALUES (?, 0, 0, 10, 10, ?, ?)",
                     [(1, 1, 'Manual'), (2, 1, 'Manual'), (3, None, 'Automatic')])
    conn.execute("INSERT INTO workflows (id, name, description, python_script) "
                 "VALUES (1, 'Import', '', 'workflows/io_picture_import_v2_workflow.py')")
    conn.execute("INSERT INTO workflow_executions (workflow_id, workflow_name, status, "
                 "started_at) VALUES (1, 'Import', 'failed', datetime('now', '-1 day')), "
                 "(1, 'Import', 'running', datetime('now'))")
    conn.commit()
    conn.close()
    return client


def _dash(client):
    r = client.get('/api/catalogs/1/dashboard')
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_one_request_draws_the_page(library):
    d = _dash(library)
    lib = d['stats']['library']
    assert lib['photos'] == {'count': 4, 'bytes': 4000}       # the removed one is not here
    assert lib['videos'] == {'count': 1, 'bytes': 5000}
    assert (lib['people'], lib['pets'], lib['events']) == (2, 0, 1)
    assert [(y['year'], y['photos'], y['videos']) for y in d['stats']['years']] == [
        ('2019', 2, 0), ('2021', 1, 1), ('2022', 1, 0)]
    assert d['stats']['top_people'] == [{'id': 1, 'name': 'Ana', 'photos': 2}]
    assert d['stats']['attention']['faces_unidentified'] == 1
    assert [(y['year'], y['count']) for y in d['on_this_day']['years']] == [
        ('2021', 1), ('2019', 2)]
    assert [b['name'] for b in d['live']['birthdays']] == ['Ana']
    assert d['live']['birthdays'][0]['in_days'] == 3
    assert [r['status'] for r in d['live']['activity']['running']] == ['running']
    assert d['live']['failed_7d'] == 1
    assert isinstance(d['live']['storage'], list) and d['live']['storage']
    assert d['live']['storage'][0]['total'] > 0


def test_the_cache_is_used_until_the_catalog_moves(library, catalog):
    first = _dash(library)
    again = _dash(library)
    assert again['stats_computed_at'] == first['stats_computed_at']
    assert again['stats_stale'] is False

    conn = _db(catalog)                                # an import adds a file
    _file(conn, 7, 'f.jpg', '2023-05-05T10:00:00')
    conn.commit()
    conn.close()
    assert _dash(library)['stats_stale'] is True       # served at once, still the old count

    fresh = library.post('/api/catalogs/1/dashboard/refresh').get_json()
    assert fresh['stats']['library']['photos']['count'] == 5
    assert fresh['stats_stale'] is False


def test_a_finished_workflow_or_twelve_hours_make_it_stale(library, catalog):
    _dash(library)
    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    conn = sqlite3.connect(db_path)
    cached = dashboard._read_stats(conn)
    assert dashboard._is_stale(conn, cached) is False

    time.sleep(1.1)                                    # computed_at has second precision
    dashboard.mark_stale(conn)
    assert dashboard._is_stale(conn, cached) is True

    dashboard.rebuild(db_path)
    cached = dashboard._read_stats(conn)
    assert dashboard._is_stale(conn, cached) is False
    cached['computed_at'] = (datetime.now() - timedelta(hours=13)).replace(
        microsecond=0).isoformat()
    assert dashboard._is_stale(conn, cached) is True
    conn.close()


def test_on_this_day_is_computed_once_a_day(library, catalog):
    conn = _db(catalog)
    first = dashboard.on_this_day(conn, TODAY)
    _file(conn, 8, 'late.jpg', f'2020-{TODAY:%m-%d}T10:00:00')
    conn.commit()
    assert dashboard.on_this_day(conn, TODAY) == first              # same day: kept
    tomorrow = dashboard.on_this_day(conn, TODAY + timedelta(days=1))
    assert tomorrow['date'] != first['date']                        # a new day: recounted
    conn.close()


def test_an_older_shape_is_discarded(library, catalog):
    _dash(library)
    conn = _db(catalog)
    conn.execute("UPDATE catalog_properties SET value = ? WHERE key = ?",
                 (json.dumps({'stats': {'schema_version': 0}, 'fingerprint': 'x',
                              'computed_at': '2026-01-01T00:00:00'}), dashboard.STATS_KEY))
    conn.commit()
    conn.close()
    d = _dash(library)                                 # rebuilt rather than drawn
    assert d['stats']['schema_version'] == dashboard.SCHEMA_VERSION
    assert d['stats']['library']['photos']['count'] == 4


def test_an_empty_catalog_answers(client, catalog):
    d = _dash(client)
    assert d['stats']['library']['photos']['count'] == 0
    assert d['on_this_day']['years'] == []
    assert d['live']['activity']['running'] == []


def test_what_we_know_comes_from_the_content_reports_cache(library, catalog, app_module):  # noqa: F811
    """The card reads the Content report's cache and never builds the report:
    checking it is current costs a COUNT(*) over every file."""
    assert _dash(library)['live']['coverage'] is None          # not counted yet
    import content_report
    content_report.report(app_module.CatalogDatabase(catalog.target_directory))
    cov = _dash(library)['live']['coverage']
    assert cov['generated_at']
    rows = {r['what']: r for r in cov['rows']}
    assert rows['In an event']['used'] is True and rows['In an event']['of'] == 5
    assert rows['In an album']['used'] is False
