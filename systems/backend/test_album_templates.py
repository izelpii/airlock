"""
The year album template (documentation/specs/template-albums-spec.md).

Runs over a real catalog database, built by CatalogDatabase with the
application's own schema, in a temporary directory.

    python -m pytest test_album_templates.py
"""

import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import album_templates as at  # noqa: E402


@pytest.fixture
def conn(tmp_path):
    from models import CatalogDatabase
    db = CatalogDatabase(str(tmp_path))
    c = db.get_connection()
    yield c
    c.close()


_next_file = [1000]


def event(conn, name, start, photos=3, end=None):
    """An event with `photos` files in event_files. Returns its id."""
    eid = conn.execute(
        'INSERT INTO events (name, start_time, end_time) VALUES (?, ?, ?)',
        (name, start, end or start)).lastrowid
    for _ in range(photos):
        _next_file[0] += 1
        conn.execute('INSERT INTO event_files (event_id, file_id) VALUES (?, ?)',
                     (eid, _next_file[0]))
    conn.commit()
    return eid


def counts(conn):
    return {t: conn.execute(f'SELECT COUNT(*) FROM {t}').fetchone()[0]
            for t in ('albums', 'album_categories', 'album_sections', 'section_photos')}


# ── 1-5: year_options ────────────────────────────────────────────────────────

def test_years_newest_first_with_months_and_counts(conn):
    event(conn, 'Ski', '2024-01-10 09:00:00', photos=4)
    event(conn, 'Birthday', '2024-01-20 18:00:00', photos=2)
    event(conn, 'Concert', '2024-03-02 20:00:00', photos=5)
    event(conn, 'Beach', '2024-07-14 11:00:00', photos=1)
    event(conn, 'Old', '2023-12-31 23:00:00', photos=2)

    opts = at.year_options(conn)
    assert [y['year'] for y in opts['years']] == [2024, 2023]
    y2024 = opts['years'][0]
    assert y2024['event_count'] == 4 and y2024['photo_count'] == 12
    assert [(m['name'], m['event_count'], m['photo_count']) for m in y2024['months']] == [
        ('January', 2, 6), ('March', 1, 5), ('July', 1, 1)]


def test_both_stored_formats_land_in_the_same_month(conn):
    event(conn, 'From the workflow', '2024-05-01 10:00:00')
    event(conn, 'From the Events page', '2024-05-09T14:30')
    months = at.year_options(conn)['years'][0]['months']
    assert [(m['month'], m['event_count']) for m in months] == [(5, 2)]


def test_an_event_belongs_to_the_month_it_starts_in(conn):
    event(conn, 'Road trip', '2024-06-30 08:00:00', end='2024-07-02 20:00:00')
    months = at.year_options(conn)['years'][0]['months']
    assert [m['name'] for m in months] == ['June']


def test_events_without_photos_are_skipped_and_counted(conn):
    event(conn, 'Has photos', '2024-02-01 10:00:00', photos=2)
    event(conn, 'Empty', '2024-04-01 10:00:00', photos=0)
    y = at.year_options(conn)['years'][0]
    assert [m['name'] for m in y['months']] == ['February']   # April has only the empty one
    assert y['skipped_empty_events'] == 1

    # A year with nothing but empty events is not offered at all.
    event(conn, 'Empty year', '2019-04-01 10:00:00', photos=0)
    assert 2019 not in [y['year'] for y in at.year_options(conn)['years']]


def test_undated_events_are_excluded_and_counted(conn):
    event(conn, 'Fine', '2024-02-01 10:00:00')
    event(conn, 'Garbage', 'sometime last summer')
    event(conn, 'Month 13', '2024-13-01 10:00:00')
    opts = at.year_options(conn)
    assert opts['undated_events'] == 2
    assert opts['years'][0]['event_count'] == 1


def test_no_events_gives_no_years(conn):
    assert at.year_options(conn) == {'template': 'year', 'years': [], 'undated_events': 0}


# ── 6: create_year_album ─────────────────────────────────────────────────────

def test_create_year_album_builds_months_and_event_sections(conn):
    # Inserted out of order on purpose: the album must come out chronological.
    beach = event(conn, 'Beach', '2024-07-14 11:00:00', photos=2)
    ski = event(conn, 'Ski', '2024-01-10 09:00:00', photos=3)
    bday = event(conn, 'Birthday', '2024-01-20T18:00', photos=1)
    concert = event(conn, 'Concert', '2024-03-02 20:00:00', photos=4)
    event(conn, 'Other year', '2023-05-05 10:00:00')

    album_id, summary = at.create_year_album(conn, 2024, description=' A year ')
    assert summary == {'year': 2024, 'months': 3, 'events': 4, 'photos': 10,
                       'skipped_empty_events': 0, 'undated_events': 0}

    album = conn.execute('SELECT * FROM albums WHERE id=?', (album_id,)).fetchone()
    assert (album['name'], album['description'], album['mode']) == ('2024', 'A year', 'tools')

    cats = conn.execute('SELECT id, name, parent_id, position FROM album_categories '
                        'WHERE album_id=? ORDER BY position', (album_id,)).fetchall()
    assert [(c['name'], c['parent_id'], c['position']) for c in cats] == [
        ('January', None, 1), ('March', None, 2), ('July', None, 3)]
    cat_id = {c['name']: c['id'] for c in cats}

    secs = conn.execute('SELECT * FROM album_sections WHERE album_id=? ORDER BY position',
                        (album_id,)).fetchall()
    assert [(s['name'], s['position'], s['source_event_id'], s['category_id']) for s in secs] == [
        ('Ski', 1, ski, cat_id['January']),
        ('Birthday', 2, bday, cat_id['January']),
        ('Concert', 3, concert, cat_id['March']),
        ('Beach', 4, beach, cat_id['July'])]
    for s in secs:
        n = conn.execute('SELECT COUNT(*) FROM section_photos WHERE section_id=?',
                         (s['id'],)).fetchone()[0]
        expected = conn.execute('SELECT COUNT(*) FROM event_files WHERE event_id=?',
                                (s['source_event_id'],)).fetchone()[0]
        assert n == expected


def test_default_and_explicit_names(conn):
    event(conn, 'A', '2024-01-01 10:00:00')
    a, _ = at.create_year_album(conn, '2024')
    b, _ = at.create_year_album(conn, 2024, name='Our year')
    names = dict(conn.execute('SELECT id, name FROM albums').fetchall())
    assert names[a] == '2024' and names[b] == 'Our year'     # a second run is a new album


@pytest.mark.parametrize('year', [None, 'abc', 1799, 2201, True, 20.5])
def test_invalid_year_is_refused(conn, year):
    event(conn, 'A', '2024-01-01 10:00:00')
    with pytest.raises(ValueError):
        at.create_year_album(conn, year)
    assert counts(conn)['albums'] == 0


def test_blank_name_is_refused(conn):
    event(conn, 'A', '2024-01-01 10:00:00')
    with pytest.raises(ValueError):
        at.create_year_album(conn, 2024, name='   ')


# ── 7-8: nothing half-made ───────────────────────────────────────────────────

def test_a_year_without_photos_creates_nothing(conn):
    event(conn, 'Empty', '2024-04-01 10:00:00', photos=0)
    event(conn, 'Elsewhere', '2023-04-01 10:00:00')
    with pytest.raises(at.NoEventsError):
        at.create_year_album(conn, 2024)
    assert counts(conn) == {'albums': 0, 'album_categories': 0,
                            'album_sections': 0, 'section_photos': 0}


def test_a_failure_midway_rolls_everything_back(conn, monkeypatch):
    event(conn, 'One', '2024-01-01 10:00:00')
    event(conn, 'Two', '2024-02-01 10:00:00')
    event(conn, 'Three', '2024-03-01 10:00:00')
    real = at.add_event_section
    calls = []

    def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError('disk on fire')
        return real(*args, **kwargs)
    monkeypatch.setattr(at, 'add_event_section', failing)

    with pytest.raises(RuntimeError):
        at.create_year_album(conn, 2024)
    assert counts(conn) == {'albums': 0, 'album_categories': 0,
                            'album_sections': 0, 'section_photos': 0}
    assert not conn.in_transaction


# ── 9: the shared helper still behaves as import-event did ──────────────────

def test_reimport_replaces_photos_and_keeps_the_category(conn):
    ev = event(conn, 'Ski', '2024-01-10 09:00:00', photos=3)
    album_id, _ = at.create_year_album(conn, 2024)
    sec, cat = conn.execute('SELECT id, category_id FROM album_sections WHERE album_id=?',
                            (album_id,)).fetchone()

    # The event changes: renamed, one photo dropped, two added.
    conn.execute("UPDATE events SET name='Ski week' WHERE id=?", (ev,))
    conn.execute('DELETE FROM event_files WHERE event_id=? AND file_id = '
                 '(SELECT MIN(file_id) FROM event_files WHERE event_id=?)', (ev, ev))
    conn.executemany('INSERT INTO event_files (event_id, file_id) VALUES (?, ?)',
                     [(ev, 90001), (ev, 90002)])

    section_id, was_reimport = at.add_event_section(conn, album_id, ev, 'Ski week',
                                                    category_id=None)
    conn.commit()
    assert (section_id, was_reimport) == (sec, True)
    row = conn.execute('SELECT name, category_id FROM album_sections WHERE id=?',
                       (sec,)).fetchone()
    assert (row['name'], row['category_id']) == ('Ski week', cat)   # not moved to the root
    photos = [r[0] for r in conn.execute(
        'SELECT file_id FROM section_photos WHERE section_id=? ORDER BY position', (sec,))]
    assert len(photos) == 4 and photos[-2:] == [90001, 90002]


def test_first_import_appends_at_the_end_of_the_album(conn):
    event(conn, 'Ski', '2024-01-10 09:00:00')
    album_id, _ = at.create_year_album(conn, 2024)
    later = event(conn, 'Later', '2025-01-01 10:00:00', photos=2)
    section_id, was_reimport = at.add_event_section(conn, album_id, later, 'Later', None)
    conn.commit()
    pos = conn.execute('SELECT position FROM album_sections WHERE id=?', (section_id,)).fetchone()[0]
    assert (was_reimport, pos) == (False, 2)
