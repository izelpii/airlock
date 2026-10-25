"""
Calendar view's counts and the Events day view.
documentation/architecture/calendar-view-spec.md.

    python -m pytest test_calendar_counts.py
"""

import os
import sqlite3
import sys
import time
from datetime import date

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import calendar_counts  # noqa: E402
import dashboard  # noqa: E402
from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _db(catalog):  # noqa: F811
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    return conn


def _file(catalog, when, ext='.jpg', path=None, removed=False, group=None):  # noqa: F811
    conn = _db(catalog)
    name = f'f{time.time_ns()}{ext}'
    cur = conn.execute(
        "INSERT INTO file_contents (file_path, file_name, file_size, file_type, sha256_hash, "
        "master_datetime, image_group_id, removed_at) VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
        (path or f'Images/{name}', name, ext, name, when, group,
         '2026-10-02' if removed else None))
    conn.commit()
    conn.close()
    return cur.lastrowid


def _event(catalog, name, start, end, file_ids):  # noqa: F811
    conn = _db(catalog)
    eid = conn.execute('INSERT INTO events (name, start_time, end_time) VALUES (?, ?, ?)',
                       (name, start, end)).lastrowid
    conn.executemany('INSERT INTO event_files (event_id, file_id) VALUES (?, ?)',
                     [(eid, f) for f in file_ids])
    conn.commit()
    conn.close()
    return eid


@pytest.fixture
def days(catalog):  # noqa: F811
    """16 Sep 2022: 3 photos (two in an evening event that crosses midnight)
    and a video; 17 Sep: the event's last photo; plus files that must not count."""
    a = _file(catalog, '2022-09-16T19:00:00')
    b = _file(catalog, '2022-09-16T23:30:00')
    c = _file(catalog, '2022-09-16T08:00:00')                    # in no event
    v = _file(catalog, '2022-09-16T12:00:00', ext='.mp4')        # in no event
    d = _file(catalog, '2022-09-17T00:30:00')
    _event(catalog, 'Party', '2022-09-16T19:00:00', '2022-09-17T00:30:00', [a, b, d])
    _event(catalog, 'Lunch', '2022-09-16T12:00:00', '2022-09-16T12:00:00', [v])
    # Not counted: removed, a cache/ derivative, the second half of a RAW+JPG pair.
    _file(catalog, '2022-09-16T10:00:00', removed=True)
    _file(catalog, '2022-09-16T10:00:00', path='cache/Images/x_alt_1200.jpg')
    primary = _file(catalog, '2022-09-18T10:00:00', ext='.cr3')
    conn = _db(catalog)
    gid = conn.execute('INSERT INTO image_groups (primary_file_id, kind) VALUES (?, ?)',
                       (primary, 'raw_jpg')).lastrowid
    conn.execute('UPDATE file_contents SET image_group_id = ? WHERE id = ?', (gid, primary))
    conn.commit()
    conn.close()
    _file(catalog, '2022-09-18T10:00:00', group=gid)
    _file(catalog, None)                                          # undated
    return {'a': a, 'v': v, 'c': c}


def _photo_types(app_module):  # noqa: F811
    return app_module._PHOTO_TYPES


def test_counts_photos_and_videos_by_photos_rules(app_module, catalog, days):  # noqa: F811
    conn = _db(catalog)
    got = calendar_counts.count(conn, _photo_types(app_module))
    conn.close()
    by = {tuple(r[:3]): r[3:] for r in got['days']}
    assert by[(2022, 9, 16)] == [3, 1, 3]          # 3 photos, 1 video, 3 in an event
    assert by[(2022, 9, 17)] == [1, 0, 1]
    assert by[(2022, 9, 18)] == [1, 0, 0]          # a RAW+JPG pair is one
    assert got['undated'] == 1
    assert got['totals']['busiest'] == 4 and got['totals']['days'] == 3
    assert got['totals']['mean'] == 2.0                     # 6 files on 3 days
    assert [r[:3] for r in got['days']] == sorted([r[:3] for r in got['days']], reverse=True)


def test_cached_until_the_catalog_changes(app_module, catalog, days, client):  # noqa: F811
    first = client.get('/api/catalogs/1/calendar').get_json()
    assert first['stale'] is False and 'fingerprint' not in first
    again = client.get('/api/catalogs/1/calendar').get_json()
    assert again['computed_at'] == first['computed_at'] and again['stale'] is False
    # A new file moves the fingerprint: served stale at once, recounted behind.
    _file(catalog, '2023-01-01T10:00:00')
    stale = client.get('/api/catalogs/1/calendar').get_json()
    assert stale['stale'] is True and stale['days'] == first['days']
    for _ in range(50):
        if not calendar_counts._building:
            break
        time.sleep(0.05)
    fresh = client.get('/api/catalogs/1/calendar').get_json()
    assert fresh['stale'] is False and fresh['days'][0][:3] == [2023, 1, 1]


def test_a_finished_workflow_marks_it_stale_and_refresh_recounts(app_module, catalog, days, client):  # noqa: F811
    client.get('/api/catalogs/1/calendar')
    conn = _db(catalog)
    time.sleep(1.1)                                 # computed_at has a resolution of seconds
    dashboard.mark_stale(conn)
    conn.commit()
    conn.close()
    assert client.get('/api/catalogs/1/calendar').get_json()['stale'] is True
    r = client.post('/api/catalogs/1/calendar/refresh').get_json()
    assert r['stale'] is False


def test_one_day_in_events(app_module, catalog, days, client):  # noqa: F811
    d = client.get('/api/catalogs/1/events?date=2022-09-16').get_json()
    assert d['total'] == 4 and d['not_in_an_event'] == 1      # the 08:00 photo
    assert [e['name'] for e in d['events']] == ['Lunch', 'Party']
    # The party crosses midnight: it is on the 17th too.
    d = client.get('/api/catalogs/1/events?date=2022-09-17').get_json()
    assert [e['name'] for e in d['events']] == ['Party'] and d['not_in_an_event'] == 0
    d = client.get('/api/catalogs/1/events?date=2022-09-19').get_json()
    assert d == {'date': '2022-09-19', 'total': 0, 'not_in_an_event': 0, 'events': []}
    assert client.get('/api/catalogs/1/events?date=16-09-2022').status_code == 400
    assert isinstance(client.get('/api/catalogs/1/events').get_json(), list)   # unchanged


def test_the_dashboard_gets_the_last_year_and_the_catalogs_busiest(app_module, catalog, days):  # noqa: F811
    value = calendar_counts.rebuild(os.path.join(catalog.target_directory, 'workflow_results.db'),
                                    _photo_types(app_module))
    recent = calendar_counts.recent(value, today=date(2023, 6, 1))
    assert [r[:3] for r in recent['days']] == [[2022, 9, 18], [2022, 9, 17], [2022, 9, 16]]
    assert recent['busiest'] == 4 and recent['mean'] == 2.0
    assert calendar_counts.recent(value, today=date(2024, 6, 1))['days'] == []
