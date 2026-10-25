"""
Album templates: albums made in one step from what the catalog already knows.

The first and only template so far is "year": every event of one year, one
category per month that has events, one section per event
(documentation/specs/template-albums-spec.md).

Pure functions over an open sqlite3 connection (row_factory = sqlite3.Row), so
they are testable without Flask; app.py's routes are thin callers. Nothing here
deploys or publishes - the caller does that once, after the commit.
"""

import re
import sqlite3

TEMPLATES = ('year',)

# Must match MONTH_NAMES in systems/frontend/src/utils/dateGrouping.js.
MONTH_NAMES = (
    'January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December',
)

MIN_YEAR, MAX_YEAR = 1800, 2200

# events.start_time is stored two ways - 'YYYY-MM-DD HH:MM:SS' by the Extract
# Events workflow, 'YYYY-MM-DDTHH:MM' by the Events page - and both begin
# YYYY-MM. So the year and month are read from the text, never through a date
# parser or a timezone (spec 2.2).
_YEAR_MONTH = re.compile(r'^(\d{4})-(0[1-9]|1[0-2])')


class NoEventsError(ValueError):
    """The chosen year has no event with photos, so there is nothing to make."""


def year_month(start_time):
    """(year, month) an event belongs to - the month it STARTS in - or None if undated."""
    m = _YEAR_MONTH.match(start_time or '')
    return (int(m.group(1)), int(m.group(2))) if m else None


# ─── One photograph, one album entry ─────────────────────────────────────────
# A camera saving RAW+JPG writes two files per shot, grouped as one image
# (file-unity-spec.md): the JPG is the pair's primary and owns the display
# sizes; the RAW has none. An album holds the PRIMARY only - the RAW half has
# nothing a gallery can show, and listing it doubled every album built from
# events (oldserver's second-catalog: 9,584 RAW entries beside 9,584 JPGs).
#
# The ids of every secondary file, for `... NOT IN (PAIR_SECONDARY_IDS)`. The
# same rule GET /photos uses to show one tile per image.
PAIR_SECONDARY_IDS = (
    'SELECT f.id FROM file_contents f JOIN image_groups g ON g.id = f.image_group_id '
    'WHERE f.id <> g.primary_file_id'
)


def primary_file_id(conn, file_id):
    """The file an album should hold for this one: its pair's primary, or itself."""
    row = conn.execute(
        'SELECT g.primary_file_id FROM file_contents f '
        'JOIN image_groups g ON g.id = f.image_group_id WHERE f.id = ?', (file_id,)).fetchone()
    return row[0] if row and row[0] else file_id


def fold_pairs(conn, album_id):
    """
    Replace every pair secondary in an album's sections with its primary.

    Where the primary is already in the section the secondary is simply dropped
    (UPDATE OR IGNORE leaves it for the DELETE); where it is not, the photo
    stays in the album as its primary rather than being lost. Section covers
    move the same way. Idempotent; returns how many entries were dropped or
    replaced. Does not commit.
    """
    sections = f'SELECT id FROM album_sections WHERE album_id = {int(album_id)}'
    before = conn.execute(
        f'SELECT COUNT(*) FROM section_photos WHERE section_id IN ({sections}) '
        f'AND file_id IN ({PAIR_SECONDARY_IDS})').fetchone()[0]
    if not before:
        return 0
    conn.execute(f'''
        UPDATE OR IGNORE section_photos
        SET file_id = (SELECT g.primary_file_id FROM file_contents f
                       JOIN image_groups g ON g.id = f.image_group_id
                       WHERE f.id = section_photos.file_id)
        WHERE section_id IN ({sections}) AND file_id IN ({PAIR_SECONDARY_IDS})
    ''')
    conn.execute(f'DELETE FROM section_photos WHERE section_id IN ({sections}) '
                 f'AND file_id IN ({PAIR_SECONDARY_IDS})')
    conn.execute(f'''
        UPDATE album_sections
        SET cover_file_id = (SELECT g.primary_file_id FROM file_contents f
                             JOIN image_groups g ON g.id = f.image_group_id
                             WHERE f.id = album_sections.cover_file_id)
        WHERE album_id = ? AND cover_file_id IN ({PAIR_SECONDARY_IDS})
    ''', (album_id,))
    return before


def _sort_key(event):
    # The two stored formats differ at the date/time separator. Normalised,
    # a 'T' event no longer sorts after every ' ' event of the same day.
    return ((event['start_time'] or '').replace('T', ' '), event['id'])


def _dated_events(conn):
    """
    Every event with its (year, month) and photo count, plus the undated count.

    One query: a year's worth of events is small, and grouping in Python keeps
    the date rule in one place (year_month) instead of repeating it in SQL.
    """
    rows = conn.execute(
        'SELECT e.id, e.name, e.start_time, '
        '       (SELECT COUNT(*) FROM event_files ef WHERE ef.event_id = e.id '
        f'          AND ef.file_id NOT IN ({PAIR_SECONDARY_IDS})) AS photos '
        'FROM events e').fetchall()
    dated, undated = [], 0
    for r in rows:
        ym = year_month(r['start_time'])
        if ym is None:
            undated += 1
            continue
        dated.append({'id': r['id'], 'name': r['name'], 'start_time': r['start_time'],
                      'year': ym[0], 'month': ym[1], 'photos': r['photos']})
    return dated, undated


def year_options(conn):
    """
    What the template window offers (spec 5.1): years newest first, each with
    the months that would become categories and what they would hold.

    photo_count counts event_files rows, so a photo in two events counts twice -
    exactly what the album would hold. A RAW+JPG pair counts once, as the album
    holds it (PAIR_SECONDARY_IDS). A year whose events all lack photos is
    left out: it would make an empty album.
    """
    dated, undated = _dated_events(conn)
    by_year = {}
    for ev in dated:
        y = by_year.setdefault(ev['year'], {'months': {}, 'skipped': 0})
        if not ev['photos']:
            y['skipped'] += 1
            continue
        mo = y['months'].setdefault(ev['month'], {'event_count': 0, 'photo_count': 0})
        mo['event_count'] += 1
        mo['photo_count'] += ev['photos']

    years = []
    for year in sorted(by_year, reverse=True):
        months = by_year[year]['months']
        if not months:
            continue
        years.append({
            'year': year,
            'event_count': sum(m['event_count'] for m in months.values()),
            'photo_count': sum(m['photo_count'] for m in months.values()),
            'months': [{'month': m, 'name': MONTH_NAMES[m - 1], **months[m]}
                       for m in sorted(months)],
            'skipped_empty_events': by_year[year]['skipped'],
        })
    return {'template': 'year', 'years': years, 'undated_events': undated}


def add_event_section(conn, album_id, event_id, event_name, category_id, position=None):
    """
    Create - or, when this album already has one for the event, refresh - the
    section for one event, and fill it with the event's photos.

    The one way an event becomes a section, shared by the tools editor's
    import-event and the year template, so both make identical sections and a
    later re-import treats a template section like any other (spec 6).

    Re-import replaces the photos and refreshes the name, and deliberately does
    NOT move the section's category: the user filed it somewhere, and re-syncing
    an event is not a request to move it (album-gallery-enhancement-spec.md 5.2).

    position=None appends album-wide (max + 1). Positions are album-wide under
    UNIQUE(album_id, position); order within a category is those same numbers
    read among siblings.

    Does not commit and does not deploy. Returns (section_id, was_reimport).
    """
    # One entry per photograph: a RAW+JPG pair is added as its primary only.
    # In the order they were TAKEN (models.TAKEN_ORDER_SQL), so a photo added
    # to the catalog later - or arriving from a merged event - lands among its
    # neighbours rather than at the end, which is where catalog id put it.
    from models import TAKEN_ORDER_SQL
    file_ids = [r[0] for r in conn.execute(
        'SELECT ef.file_id FROM event_files ef LEFT JOIN file_contents fc ON fc.id = ef.file_id '
        f'WHERE ef.event_id=? AND ef.file_id NOT IN ({PAIR_SECONDARY_IDS}) '
        f'ORDER BY {TAKEN_ORDER_SQL}, ef.file_id',
        (event_id,)).fetchall()]

    existing = conn.execute(
        'SELECT id FROM album_sections WHERE album_id=? AND source_event_id=?',
        (album_id, event_id)).fetchone()
    if existing:
        section_id = existing[0]
        conn.execute('DELETE FROM section_photos WHERE section_id=?', (section_id,))
        conn.execute('UPDATE album_sections SET name=? WHERE id=?', (event_name, section_id))
        was_reimport = True
    else:
        if position is None:
            position = conn.execute(
                'SELECT COALESCE(MAX(position), 0) FROM album_sections WHERE album_id=?',
                (album_id,)).fetchone()[0] + 1
        cur = conn.execute(
            'INSERT INTO album_sections (album_id, name, position, source_event_id, category_id) '
            'VALUES (?, ?, ?, ?, ?)',
            (album_id, event_name, position, event_id, category_id))
        section_id = cur.lastrowid
        was_reimport = False

    conn.executemany(
        'INSERT OR IGNORE INTO section_photos (section_id, file_id, position) VALUES (?, ?, ?)',
        [(section_id, fid, idx) for idx, fid in enumerate(file_ids, start=1)])
    return section_id, was_reimport


def validate_year(value):
    """The year as an int, or ValueError with a message for the user."""
    try:
        year = int(value)
    except (TypeError, ValueError):
        raise ValueError('year must be a whole number, e.g. 2024')
    if isinstance(value, bool) or not MIN_YEAR <= year <= MAX_YEAR:
        raise ValueError(f'year must be between {MIN_YEAR} and {MAX_YEAR}')
    return year


def create_year_album(conn, year, name=None, description=None):
    """
    The year template (spec 4, 5.2 steps 1-6): one album, 'with tools', with a
    root category per month that has events and a section per event in it.

    One transaction. Any error rolls back, so no partial album survives - this
    application never enables foreign keys, so nothing else would clean up.
    Raises NoEventsError, and writes nothing, when the year has no event with
    photos.

    Returns (album_id, summary).
    """
    year = validate_year(year)
    name = (str(year) if name is None else str(name)).strip()
    if not name:
        raise ValueError('name is required')
    description = (description or '').strip() or None

    dated, undated = _dated_events(conn)
    in_year = [e for e in dated if e['year'] == year]
    events = sorted((e for e in in_year if e['photos']), key=_sort_key)
    skipped = len(in_year) - len(events)
    if not events:
        raise NoEventsError(f'No events with photos in {year}')

    if not conn.in_transaction:
        # Take the write lock now, not at the first INSERT: nothing can slip
        # between reading the events and writing the album.
        conn.execute('BEGIN IMMEDIATE')
    try:
        album_id = conn.execute(
            "INSERT INTO albums (name, description, mode) VALUES (?, ?, 'tools')",
            (name, description)).lastrowid

        # Months first, in calendar order, so their positions are 1, 2, ...
        category_of = {}
        for pos, month in enumerate(sorted({e['month'] for e in events}), start=1):
            category_of[month] = conn.execute(
                'INSERT INTO album_categories (album_id, parent_id, name, position) '
                'VALUES (?, NULL, ?, ?)', (album_id, MONTH_NAMES[month - 1], pos)).lastrowid

        # Then the events, chronologically, so album-wide positions put each
        # month's events in time order within it.
        for pos, ev in enumerate(events, start=1):
            add_event_section(conn, album_id, ev['id'], ev['name'],
                              category_of[ev['month']], position=pos)
        conn.commit()
    except BaseException:
        conn.rollback()
        raise

    return album_id, {
        'year': year,
        'months': len(category_of),
        'events': len(events),
        'photos': sum(e['photos'] for e in events),
        'skipped_empty_events': skipped,
        'undated_events': undated,
    }
