#!/usr/bin/env python3
"""
The photo gallery's index, and the day it opens.

    python -m pytest test_photo_calendar.py

WHY THESE ENDPOINTS EXIST. /photos returned the whole catalog, because the
page needed two unrelated things from it: the list of days to draw the
navigation, and the photographs of the one day being looked at. Measured on
`family`, 370,751 files:

    /photos            301,195 rows   190.4 MB   ~21 s to build
    /photos/calendar     6,134 rows    154.6 KB    1.3 s

Three orders of magnitude for the half the page needs before it can show
anything. The page "sometimes never finishes loading" was that.

THE INVARIANT THAT MATTERS is that the two agree. The calendar says a day
holds n photographs; opening that day must return n. They are separate
queries, so the day expression and the file-type list are shared constants -
and these tests exist because the failure is a day that appears in the
navigation and opens empty.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _add(catalog, name, when, file_type='.jpg', removed=False, group=None,
         primary=None):
    """A photograph taken at `when` (a 'YYYY-MM-DD HH:MM:SS' string)."""
    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    rel = os.path.join('Images', name)
    cur = conn.execute(
        'INSERT INTO file_contents (file_path, file_name, file_type, '
        ' master_datetime, added_at, removed_at, image_group_id) '
        ' VALUES (?,?,?,?,?,?,?)',
        (rel, name, file_type, when, when,
         '2026-01-01' if removed else None, group))
    fid = cur.lastrowid
    if group is not None and primary is not None:
        conn.execute('INSERT OR IGNORE INTO image_groups (id, primary_file_id) '
                     ' VALUES (?,?)', (group, primary))
    conn.commit()
    conn.close()
    return fid


def _cal(client, catalog):
    r = client.get(f'/api/catalogs/{catalog.id}/photos/calendar')
    assert r.status_code == 200, r.get_json()
    return {tuple(d[:3]): d[3] for d in r.get_json()['days']}


def _day(client, catalog, y, m, d):
    r = client.get(f'/api/catalogs/{catalog.id}/photos'
                   f'?year={y}&month={m}&day={d}')
    assert r.status_code == 200, r.get_json()
    return r.get_json()


def test_the_calendar_counts_the_days(client, catalog):
    _add(catalog, 'a.jpg', '2024-03-05 10:00:00')
    _add(catalog, 'b.jpg', '2024-03-05 11:00:00')
    _add(catalog, 'c.jpg', '2024-03-06 09:00:00')
    assert _cal(client, catalog) == {(2024, 3, 5): 2, (2024, 3, 6): 1}


def test_opening_a_day_returns_exactly_what_the_calendar_promised(client, catalog):
    """
    THE invariant. Two separate queries; a day that appears in the navigation
    and opens empty is the failure mode this guards.
    """
    for i in range(4):
        _add(catalog, f'x{i}.jpg', f'2021-07-14 0{i}:00:00')
    _add(catalog, 'other.jpg', '2021-07-15 10:00:00')

    cal = _cal(client, catalog)
    for (y, m, d), n in cal.items():
        assert len(_day(client, catalog, y, m, d)) == n, \
            f'the calendar says {n} for {y}-{m}-{d} but the day differs'


def test_a_removed_photo_is_in_neither(client, catalog):
    _add(catalog, 'gone.jpg', '2020-01-02 10:00:00', removed=True)
    _add(catalog, 'here.jpg', '2020-01-02 11:00:00')
    assert _cal(client, catalog) == {(2020, 1, 2): 1}
    assert len(_day(client, catalog, 2020, 1, 2)) == 1


def test_a_video_is_in_neither(client, catalog):
    _add(catalog, 'clip.mov', '2020-02-03 10:00:00', file_type='.mov')
    _add(catalog, 'still.jpg', '2020-02-03 11:00:00')
    assert _cal(client, catalog) == {(2020, 2, 3): 1}
    assert len(_day(client, catalog, 2020, 2, 3)) == 1


def test_the_secondary_of_a_raw_jpeg_pair_is_in_neither(client, catalog):
    """
    One row per PHOTOGRAPH. A RAW+JPG pair is one picture, and the calendar
    must count it once or every such day opens one short.
    """
    jpg = _add(catalog, 'pair.jpg', '2019-05-05 10:00:00')
    _add(catalog, 'pair.cr3', '2019-05-05 10:00:00', file_type='.cr3',
         group=1, primary=jpg)
    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    conn.execute('UPDATE file_contents SET image_group_id = 1 WHERE id = ?', (jpg,))
    conn.commit()
    conn.close()

    assert _cal(client, catalog) == {(2019, 5, 5): 1}
    assert len(_day(client, catalog, 2019, 5, 5)) == 1


def test_a_month_or_a_year_can_be_asked_for(client, catalog):
    _add(catalog, 'p1.jpg', '2022-08-01 10:00:00')
    _add(catalog, 'p2.jpg', '2022-08-20 10:00:00')
    _add(catalog, 'p3.jpg', '2022-09-01 10:00:00')

    r = client.get(f'/api/catalogs/{catalog.id}/photos?year=2022&month=8')
    assert len(r.get_json()) == 2
    r = client.get(f'/api/catalogs/{catalog.id}/photos?year=2022')
    assert len(r.get_json()) == 3


def test_a_deep_link_can_find_its_day_without_the_catalog(client, catalog):
    """
    /photos/9/363120 must open on the right day without loading 300,000 rows
    to discover which day that is - the other reason the page fetched
    everything.
    """
    _add(catalog, 'early.jpg', '2018-01-01 10:00:00')
    target = _add(catalog, 'wanted.jpg', '2023-11-17 15:30:00')

    r = client.get(f'/api/catalogs/{catalog.id}/photos/{target}/day')
    assert r.status_code == 200, r.get_json()
    assert r.get_json() == {'year': 2023, 'month': 11, 'day': 17}


def test_an_unknown_file_has_no_day(client, catalog):
    r = client.get(f'/api/catalogs/{catalog.id}/photos/999999/day')
    assert r.status_code == 404


def test_a_nonsense_date_is_refused_not_ignored(client, catalog):
    """
    Silently returning the whole catalog for ?year=banana is how a 190 MB
    response comes back to a page that asked for one day.
    """
    r = client.get(f'/api/catalogs/{catalog.id}/photos?year=banana')
    assert r.status_code == 400


def test_without_a_date_it_still_returns_everything(client, catalog):
    """Kept working for any caller that predates the scoping."""
    _add(catalog, 'one.jpg', '2020-01-01 10:00:00')
    _add(catalog, 'two.jpg', '2021-01-01 10:00:00')
    r = client.get(f'/api/catalogs/{catalog.id}/photos')
    assert len(r.get_json()) == 2


# ── Schema setup is not a per-request cost ──────────────────────────────────

def test_the_catalog_schema_is_ensured_once_per_process(app_module, catalog):
    """
    CatalogDatabase.__init__ ran init_db() every time - 28 CREATE TABLE, 23
    CREATE INDEX, 27 ALTER TABLE and six ensure_* modules - and it is
    constructed by every API call that touches a catalog.

    Measured on homeserver: 62 ms each. A location with 6,404 photographs asks for
    6,404 thumbnails, so that page spent 397 SECONDS re-checking that the
    tables exist before serving a byte of image. With one worker and eight
    threads it saturated the backend; the images crawled and a save queued
    behind them looked like the server was down.

    PRAGMA schema_version is the guard, so a schema someone else changed is
    still noticed.
    """
    import time

    target = catalog.target_directory
    app_module.CatalogDatabase(target)            # first: builds it

    # schema_version must not move once the schema is settled.
    def version():
        c = sqlite3.connect(os.path.join(target, 'workflow_results.db'))
        try:
            return c.execute('PRAGMA schema_version').fetchone()[0]
        finally:
            c.close()

    settled = version()
    t = time.time()
    for _ in range(25):
        app_module.CatalogDatabase(target)
    each = (time.time() - t) / 25

    assert version() == settled, \
        'constructing it again rewrote the schema'
    # Generous: the point is orders of magnitude, not a stopwatch. The old
    # path could not come close to this on any machine.
    assert each < 0.020, f'{each * 1000:.0f} ms per construction'


def test_a_changed_schema_is_re_ensured(app_module, catalog):
    """
    The memo must not become "never check again": a table dropped by anything
    else has to come back, or the guard turns one slow page into a broken one.
    """
    target = catalog.target_directory
    app_module.CatalogDatabase(target)

    db = os.path.join(target, 'workflow_results.db')
    conn = sqlite3.connect(db)
    conn.execute('DROP TABLE IF EXISTS queue_settings')
    conn.commit()
    conn.close()

    app_module.CatalogDatabase(target)            # notices via schema_version

    conn = sqlite3.connect(db)
    try:
        names = {r[0] for r in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert 'queue_settings' in names, 'a dropped table was not rebuilt'


def test_wal_is_set_once_not_on_every_connection(app_module, catalog):
    """
    get_connection() ran `PRAGMA journal_mode=WAL` every time, and that is not
    a free statement: it takes an exclusive lock and fsyncs. Measured at
    2.26 ms, paid by every connection the application opens - which is to say
    by every query it makes, not merely by the schema setup above.

    WAL is a persistent property written into the database header, so once is
    enough. This is the larger of the two savings; it applies everywhere.
    """
    import time

    db = app_module.CatalogDatabase(catalog.target_directory)
    db.get_connection().close()                   # first: converts to WAL

    path = os.path.join(catalog.target_directory, 'workflow_results.db')
    c = sqlite3.connect(path)
    try:
        assert c.execute('PRAGMA journal_mode').fetchone()[0].lower() == 'wal'
    finally:
        c.close()

    t = time.time()
    for _ in range(40):
        db.get_connection().close()
    each = (time.time() - t) / 40
    assert each < 0.001, f'{each * 1000:.2f} ms per connection'


def test_runtime_rows_are_seeded_even_though_the_schema_is_memoised(
        app_module, catalog):
    """
    Not everything init_db() did was schema work. Two of its statements are
    keyed to the DATA - the default people categories and a decorator for
    every deployment - and rows arrive while the process is running, so
    PRAGMA schema_version cannot see when they are needed. They have to run on
    every construction, which is why they moved out of init_db().

    This is the regression the first version of the memo caused: it skipped
    them along with the schema.
    """
    target = catalog.target_directory
    app_module.CatalogDatabase(target)
    path = os.path.join(target, 'workflow_results.db')

    before = sqlite3.connect(path)
    try:
        settled = before.execute('PRAGMA schema_version').fetchone()[0]
        before.execute('DELETE FROM people_categories')
        before.commit()
    finally:
        before.close()

    app_module.CatalogDatabase(target)            # no DDL: the memo holds

    after = sqlite3.connect(path)
    try:
        names = sorted(n for (n,) in
                       after.execute('SELECT name FROM people_categories'))
        assert after.execute('PRAGMA schema_version').fetchone()[0] == settled, \
            'the schema was rebuilt; this test would then prove nothing'
    finally:
        after.close()
    assert names == ['Family', 'Other'], names
