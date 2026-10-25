"""
Location clustering, resolution and the home-address defect it replaces.

    python -m pytest test_locations.py

Covers documentation/specs/location-spec.md section 11. The tests that matter
most are the ones that would pass vacuously if written carelessly:

  * test_an_event_with_no_gps_member_gets_no_coordinate ARMS the home-address
    bug by populating user_settings first. Without that the fixture proves
    nothing, because a catalog with no home set never triggered the fallback -
    which is precisely why the defect survived in the field (spec 3.1).
  * test_the_cell_boundary_does_not_split_one_place finds a REAL precision-6
    cell boundary rather than assuming one, because a prefix-only
    implementation passes every other clustering test (spec 4.3).
"""

import json
import os
import sqlite3
import sys

import pytest
import pygeohash

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import ge_extract_locations as ge          # noqa: E402
import ge_resolve_locations as gr          # noqa: E402
import ev_extract_events as ev             # noqa: E402
import workflow_defaults                   # noqa: E402
import workflow_scheduler as sched         # noqa: E402
import workflow_triggers as trig           # noqa: E402


# ─── Fixtures ─────────────────────────────────────────────────────────────────

SCHEMA = '''
CREATE TABLE file_contents (
    id INTEGER PRIMARY KEY, file_path TEXT, file_name TEXT,
    master_datetime TEXT, master_datetime_source TEXT,
    creation_date TEXT, file_type TEXT DEFAULT 'image',
    added_at TIMESTAMP, removed_at TIMESTAMP, image_group_id INTEGER);
CREATE TABLE exif_data (
    id INTEGER PRIMARY KEY, file_id INTEGER, creation_date TEXT,
    latitude REAL, longitude REAL, altitude REAL, geohash TEXT,
    raw_exif_json TEXT);
CREATE TABLE events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    start_time TIMESTAMP NOT NULL, end_time TIMESTAMP NOT NULL,
    latitude REAL, longitude REAL, description TEXT,
    file_count INTEGER DEFAULT 0, type TEXT DEFAULT 'Auto',
    cover_file_id INTEGER, location_source TEXT,
    created_at TIMESTAMP, updated_at TIMESTAMP);
CREATE TABLE event_files (
    id INTEGER PRIMARY KEY AUTOINCREMENT, event_id INTEGER, file_id INTEGER);
CREATE TABLE locations (
    id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL,
    latitude REAL, longitude REAL, description TEXT, source TEXT,
    map_image_path TEXT, confirmed INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP, updated_at TIMESTAMP);
CREATE TABLE location_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, location_id INTEGER,
    event_id INTEGER, UNIQUE (location_id, event_id));
CREATE TABLE workflow_executions (
    id INTEGER PRIMARY KEY, processed_files INT, total_files INT, status TEXT,
    completed_at TIMESTAMP, error_message TEXT, skipped_files INT,
    stop_requested INT);
CREATE TABLE alternative_versions (
    id INTEGER PRIMARY KEY, original_file_id INTEGER, rung TEXT);
'''


class Cat:
    """A catalog directory with a workflow_results.db and helpers to fill it."""

    def __init__(self, path):
        self.path = path
        self.db = os.path.join(path, 'workflow_results.db')
        conn = sqlite3.connect(self.db)
        conn.executescript(SCHEMA)
        conn.commit()
        conn.close()
        self._next = 1

    def conn(self):
        c = sqlite3.connect(self.db)
        c.row_factory = sqlite3.Row
        return c

    def add_file(self, lat=None, lon=None, when='2019-04-21 10:00:00', name=None):
        """One catalogued file, with coordinates only if given."""
        fid = self._next
        self._next += 1
        c = self.conn()
        c.execute('INSERT INTO file_contents (id, file_name, file_path, '
                  'master_datetime, creation_date, added_at) '
                  'VALUES (?, ?, ?, ?, ?, ?)',
                  (fid, name or f'IMG_{fid:04d}.JPG',
                   f'/c/Images/{name or f"IMG_{fid:04d}.JPG"}', when, when, when))
        gh = pygeohash.encode(lat, lon, precision=10) if lat is not None else None
        c.execute('INSERT INTO exif_data (file_id, creation_date, latitude, '
                  'longitude, geohash) VALUES (?, ?, ?, ?, ?)',
                  (fid, when, lat, lon, gh))
        c.commit()
        c.close()
        return fid

    def add_event(self, file_ids, type='Auto', lat=None, lon=None,
                  location_source=None):
        c = self.conn()
        cur = c.cursor()
        cur.execute('INSERT INTO events (name, start_time, end_time, latitude, '
                    'longitude, type, location_source, file_count) '
                    "VALUES ('E', '2019-04-21 10:00:00', '2019-04-21 12:00:00', "
                    '?, ?, ?, ?, ?)',
                    (lat, lon, type, location_source, len(file_ids)))
        eid = cur.lastrowid
        cur.executemany('INSERT INTO event_files (event_id, file_id) VALUES (?, ?)',
                        [(eid, f) for f in file_ids])
        c.commit()
        c.close()
        return eid

    def add_location(self, name):
        """A location typed by hand: confirmed, as the Add Location form makes it."""
        c = self.conn()
        cur = c.cursor()
        cur.execute('INSERT INTO locations (name, confirmed) VALUES (?, 1)', (name,))
        lid = cur.lastrowid
        c.commit()
        c.close()
        return lid

    def model(self):
        """
        A LocationModel over this fixture's database.

        Not CatalogDatabase: its constructor runs init_db(), which builds the
        entire catalog schema and then fails against the narrow one here - the
        first thing it wanted was alternative_versions.characteristic. The
        model only ever needs get_connection(), so that is all this provides.
        """
        from models import LocationModel
        outer = self

        class _Shim:
            db_path = outer.db

            @staticmethod
            def get_connection():
                return outer.conn()

        return LocationModel(_Shim())

    def confirm(self, location_id, name=None):
        """What a person does on the Locations page: confirm, maybe renaming."""
        c = self.conn()
        if name:
            c.execute('UPDATE locations SET name = ?, confirmed = 1 WHERE id = ?',
                      (name, location_id))
        else:
            c.execute('UPDATE locations SET confirmed = 1 WHERE id = ?', (location_id,))
        c.commit()
        c.close()

    def location_for_cluster(self, prefix):
        """The locations row the clustering workflow made for this cluster."""
        c = self.conn()
        row = c.execute('SELECT location_id FROM location_clusters '
                        'WHERE geohash_prefix = ?', (prefix,)).fetchone()
        c.close()
        return row[0] if row else None

    def locations(self):
        c = self.conn()
        rows = [dict(r) for r in c.execute(
            'SELECT * FROM locations ORDER BY id')]
        c.close()
        return rows

    def tag_event(self, event_id, location_id):
        c = self.conn()
        c.execute('INSERT INTO location_events (location_id, event_id) VALUES (?, ?)',
                  (location_id, event_id))
        c.commit()
        c.close()

    def cluster(self):
        return ge.execute(None, self.path, [], lambda *a, **k: None)

    def resolve(self):
        return gr.execute(None, self.path, [], lambda *a, **k: None)

    def file_location(self, file_id):
        c = self.conn()
        row = c.execute('SELECT * FROM file_locations WHERE file_id = ?',
                        (file_id,)).fetchone()
        c.close()
        return dict(row) if row else None

    def clusters(self):
        c = self.conn()
        rows = [dict(r) for r in c.execute(
            'SELECT * FROM location_clusters ORDER BY file_count DESC')]
        c.close()
        return rows


@pytest.fixture
def cat(tmp_path, monkeypatch):
    # No network in a unit test: backend-geoloc is unreachable and the workflow
    # must complete anyway (spec 11, "Degradation").
    monkeypatch.setenv('GEOLOC_URL', 'http://127.0.0.1:9')
    return Cat(str(tmp_path))


# A place and a second place 2 km away, in La Plata.
A = (-33.868820, 151.209290)
B = (-33.888820, 151.209290)


def _place(cat, point, n, **kw):
    """n photos within a few metres of point."""
    return [cat.add_file(point[0] + i * 0.00002, point[1], **kw) for i in range(n)]


# ─── Clustering (spec 4.3) ────────────────────────────────────────────────────

def test_photos_taken_together_cluster_and_distant_ones_do_not(cat):
    _place(cat, A, 4)
    _place(cat, B, 3)
    cat.cluster()
    assert len(cat.clusters()) == 2


def test_a_lone_photo_is_not_a_place(cat):
    _place(cat, A, 4)
    cat.add_file(-30.0, -60.0)      # one photo from a motorway
    cat.cluster()
    assert [c['file_count'] for c in cat.clusters()] == [4]


def test_the_cell_boundary_does_not_split_one_place(cat):
    """
    Two photos either side of a geohash cell boundary are in different buckets
    however close they are. A prefix-only implementation fails this and passes
    everything else, so the boundary is located rather than assumed.
    """
    lat, lon, step = 10.0, 20.0, 0.00005
    base = pygeohash.encode(lat, lon, precision=6)
    lo = hi = None
    for k in range(1, 5000):
        probe = lat + k * step
        if pygeohash.encode(probe, lon, precision=6) != base:
            lo, hi = lat + (k - 1) * step, probe
            break
    assert lo is not None, 'no precision-6 cell boundary found near the seed'
    assert pygeohash.encode(lo, lon, 6) != pygeohash.encode(hi, lon, 6)
    assert ge.haversine_m(lo, lon, hi, lon) < ge.MERGE_METRES

    ids = ([cat.add_file(lo - i * 0.000005, lon) for i in range(3)]
           + [cat.add_file(hi + i * 0.000005, lon) for i in range(3)])
    cat.cluster()

    c = cat.conn()
    found = c.execute(
        'SELECT COUNT(DISTINCT cluster_id) FROM location_cluster_files '
        f'WHERE file_id IN ({",".join("?" * len(ids))})', ids).fetchone()[0]
    c.close()
    assert found == 1, 'the two sides of the cell boundary must be one place'


def test_a_rerun_keeps_a_confirmed_name(cat):
    """
    The clustering workflow runs after every import, so it meets its own
    earlier output constantly. A name somebody chose must survive that.
    """
    _place(cat, A, 4)
    cat.cluster()
    prefix = cat.clusters()[0]['geohash_prefix']
    home = cat.location_for_cluster(prefix)
    assert home, 'the workflow should have created a location for the cluster'
    cat.confirm(home, "Abuela's house")

    _place(cat, A, 2)               # two more photos at the same location
    cat.cluster()

    after = cat.clusters()
    assert len(after) == 1
    assert after[0]['location_id'] == home, 'the cluster must keep its location'
    assert after[0]['file_count'] == 6
    rows = cat.locations()
    assert len(rows) == 1, 'a re-run must not add a second location'
    assert rows[0]['name'] == "Abuela's house", 'the chosen name must survive'
    assert rows[0]['confirmed'] == 1


def test_the_workflow_creates_an_unconfirmed_location(cat):
    """
    One location per cluster, unconfirmed and obviously so. The name is the
    geocoder's guess, or - as here, with no geocoder reachable - the
    coordinates, which is recognisable and plainly nobody's choice.
    """
    _place(cat, A, 4)
    cat.cluster()
    rows = cat.locations()
    assert len(rows) == 1
    assert rows[0]['confirmed'] == 0
    assert rows[0]['source'] == 'cluster'
    assert rows[0]['name'].startswith('Near '), rows[0]['name']
    assert rows[0]['latitude'] == pytest.approx(A[0], abs=1e-3)


def test_a_rejected_cluster_loses_its_unconfirmed_location(cat):
    """
    Dismissing a proposal has to take the card away. A CONFIRMED location
    survives the same action - rejecting the grouping that suggested a name is
    not the same as deleting somebody's work.
    """
    _place(cat, A, 4)
    cat.cluster()
    model = cat.model()
    cluster_id = cat.clusters()[0]['id']

    model.reject_cluster(cluster_id, delete_unconfirmed_location=True)
    assert cat.locations() == [], 'the unconfirmed location should be gone'

    # Now with a confirmed one.
    cat.cluster()
    loc = cat.location_for_cluster(cat.clusters()[0]['geohash_prefix'])
    if loc is None:
        return      # a rejected cluster is not re-proposed, which is correct
    cat.confirm(loc, 'Mine')
    model.reject_cluster(cat.clusters()[0]['id'], delete_unconfirmed_location=True)
    assert [r['name'] for r in cat.locations()] == ['Mine']


def test_a_rejected_cluster_is_not_proposed_again(cat):
    _place(cat, A, 4)
    cat.cluster()
    c = cat.conn()
    c.execute("UPDATE location_clusters SET status = 'rejected'")
    c.commit()
    c.close()
    cat.cluster()
    assert [x['status'] for x in cat.clusters()] == ['rejected']


def test_clustering_completes_with_no_geo_tagged_files(cat):
    """second-catalog has 17 coordinates across 24,269 files (spec 8.1b)."""
    for _ in range(5):
        cat.add_file()
    res = cat.cluster()
    assert res['clusters'] == 0
    assert cat.clusters() == []


def test_clustering_completes_when_the_geocoder_is_unreachable(cat):
    _place(cat, A, 4)
    cat.cluster()
    assert cat.clusters()[0]['suggestions'] is None


# ─── Resolution (spec 4.1, 6) ─────────────────────────────────────────────────

def test_a_measured_coordinate_is_recorded_as_measured(cat):
    fid = _place(cat, A, 3)[0]
    cat.resolve()
    fl = cat.file_location(fid)
    assert fl['coord_source'] == 'exif'
    assert fl['coord_radius_m'] == 0
    assert fl['latitude'] == pytest.approx(A[0], abs=1e-6)


def test_a_file_in_an_accepted_cluster_takes_its_place(cat):
    ids = _place(cat, A, 4)
    cat.cluster()
    home = cat.location_for_cluster(cat.clusters()[0]['geohash_prefix'])
    cat.confirm(home, "Abuela's house")
    cat.resolve()
    fl = cat.file_location(ids[0])
    assert (fl['location_id'], fl['location_source']) == (home, 'cluster')


def test_a_file_in_no_event_still_gets_a_place(cat):
    """The case the event-only design could not express."""
    ids = _place(cat, A, 4)
    cat.cluster()
    home = cat.location_for_cluster(cat.clusters()[0]['geohash_prefix'])
    cat.confirm(home, 'Home')
    cat.resolve()
    c = cat.conn()
    assert c.execute('SELECT COUNT(*) FROM event_files').fetchone()[0] == 0
    c.close()
    assert cat.file_location(ids[0])['location_id'] == home


def test_a_manual_place_is_never_overwritten(cat):
    ids = _place(cat, A, 4)
    cat.cluster()
    home = cat.location_for_cluster(cat.clusters()[0]['geohash_prefix'])
    cat.confirm(home, 'Home')
    mine = cat.add_location('Somewhere I chose')
    c = cat.conn()
    c.execute("INSERT INTO file_locations (file_id, location_id, location_source) "
              "VALUES (?, ?, 'manual')", (ids[0], mine))
    c.commit()
    c.close()
    cat.resolve()
    fl = cat.file_location(ids[0])
    assert (fl['location_id'], fl['location_source']) == (mine, 'manual')


# ─── Inheritance (spec 4.5) ───────────────────────────────────────────────────

def test_a_persons_tag_on_an_event_reaches_files_with_no_gps(cat):
    """
    The second-catalog case: no coordinates anywhere, so no vote can be cast
    and this is the only path that places anything (spec 4.5a).
    """
    blind = [cat.add_file() for _ in range(5)]
    eid = cat.add_event(blind, type='Manual')
    band = cat.add_location('Rehearsal hall')
    cat.tag_event(eid, band)
    cat.resolve()
    for fid in blind:
        fl = cat.file_location(fid)
        assert (fl['location_id'], fl['location_source']) == (band, 'event')
        assert fl['location_share'] is None, 'a tag is not a vote'
        assert fl['location_event_id'] == eid
        assert fl['latitude'] is None, 'a place must not invent a coordinate'


def test_members_with_gps_place_the_members_without(cat):
    seeing = _place(cat, A, 8)
    blind = [cat.add_file() for _ in range(4)]
    eid = cat.add_event(seeing + blind)
    cat.cluster()
    home = cat.location_for_cluster(cat.clusters()[0]['geohash_prefix'])
    cat.confirm(home, 'Home')
    res = cat.resolve()

    assert res['voted_events'] == 1
    for fid in blind:
        fl = cat.file_location(fid)
        assert (fl['location_id'], fl['location_source']) == (home, 'event')
        assert fl['location_share'] == pytest.approx(1.0)
        assert fl['latitude'] is None, 'inherit the place, not the coordinate'
        assert fl['location_event_id'] == eid


def test_an_event_that_spans_two_places_inherits_nothing(cat):
    """A road trip must not be assigned a place."""
    here = _place(cat, A, 4)
    there = _place(cat, B, 4)
    blind = [cat.add_file() for _ in range(3)]
    cat.add_event(here + there + blind)
    cat.cluster()
    prefixes = [c['geohash_prefix'] for c in cat.clusters()]
    one = cat.location_for_cluster(prefixes[0])
    two = cat.location_for_cluster(prefixes[1])
    cat.confirm(one, 'One')
    cat.confirm(two, 'Two')
    res = cat.resolve()

    assert res['abstained_events'] == 1
    assert res['voted_events'] == 0
    for fid in blind:
        assert cat.file_location(fid) is None


def test_a_tag_outranks_a_vote(cat):
    seeing = _place(cat, A, 8)
    blind = [cat.add_file() for _ in range(3)]
    eid = cat.add_event(seeing + blind)
    cat.cluster()
    voted_for = cat.location_for_cluster(cat.clusters()[0]['geohash_prefix'])
    cat.confirm(voted_for, 'What the photos say')
    chosen = cat.add_location('What a person says')
    cat.tag_event(eid, chosen)
    cat.resolve()
    assert cat.file_location(blind[0])['location_id'] == chosen


def test_a_file_with_its_own_place_does_not_inherit(cat):
    seeing = _place(cat, A, 4)
    other = _place(cat, B, 4)
    eid = cat.add_event(seeing + other)
    cat.cluster()
    prefixes = [c['geohash_prefix'] for c in cat.clusters()]
    first = cat.location_for_cluster(prefixes[0])
    second = cat.location_for_cluster(prefixes[1])
    cat.confirm(first, 'First')
    cat.confirm(second, 'Second')
    cat.tag_event(eid, cat.add_location('The event as a whole'))
    cat.resolve()
    # Both halves keep their own cluster's place; neither takes the event's.
    assert cat.file_location(seeing[0])['location_source'] == 'cluster'
    assert cat.file_location(other[0])['location_source'] == 'cluster'


def test_an_inherited_place_is_dropped_when_its_event_disappears(cat):
    """
    Create Events deletes and recreates every Auto event, so an event id is not
    durable (spec 3.2, 4.6).
    """
    blind = [cat.add_file() for _ in range(3)]
    eid = cat.add_event(blind, type='Manual')
    cat.tag_event(eid, cat.add_location('Hall'))
    cat.resolve()
    assert cat.file_location(blind[0])['location_source'] == 'event'

    c = cat.conn()
    c.execute('DELETE FROM location_events')
    c.execute('DELETE FROM event_files')
    c.execute('DELETE FROM events')
    c.commit()
    c.close()
    cat.resolve()

    fl = cat.file_location(blind[0])
    assert fl['location_source'] is None
    assert fl['location_id'] is None


# ─── The home-address defect (spec 3.1, 4.8, 9.1) ─────────────────────────────

def _armed_catalog(tmp_path, monkeypatch, with_gps, files=4):
    """
    A catalog whose owner HAS a home location set, so the old fallback would
    fire. Arming the fixture is the point: with user_settings empty the test
    passes without testing anything, which is how the defect survived.
    """
    app_db = str(tmp_path / 'catalogs.db')
    conn = sqlite3.connect(app_db)
    conn.executescript('''
        CREATE TABLE user_settings (id INTEGER PRIMARY KEY, user_id INTEGER,
            lat REAL, long REAL);
        CREATE TABLE catalog_access (user_id INTEGER, catalog_id INTEGER, role TEXT);
    ''')
    conn.execute('INSERT INTO user_settings (id, user_id, lat, long) '
                 'VALUES (1, 1, 40.7580, -73.9855)')
    conn.execute("INSERT INTO catalog_access VALUES (1, 1, 'owner')")
    conn.commit()
    conn.close()
    monkeypatch.setenv('AIRLOCK_APP_DB', app_db)

    cat = Cat(str(tmp_path))
    for i in range(files):
        if with_gps:
            cat.add_file(A[0] + i * 0.00002, A[1],
                         when=f'2019-04-21 10:0{i}:00')
        else:
            cat.add_file(when=f'2019-04-21 10:0{i}:00')
    return cat, app_db


def test_user_settings_fixture_is_actually_armed(tmp_path, monkeypatch):
    """Guards every test below: an unarmed fixture proves nothing."""
    _, app_db = _armed_catalog(tmp_path, monkeypatch, with_gps=False)
    conn = sqlite3.connect(app_db)
    lat, lon = conn.execute('SELECT lat, long FROM user_settings').fetchone()
    conn.close()
    assert lat is not None and lon is not None


def test_an_event_with_no_gps_member_gets_no_coordinate(tmp_path, monkeypatch):
    """
    The regression test for the defect. ev_extract_events used to write the
    owner's home address here, into the column that otherwise holds a
    measurement, with nothing recording which of the two a row contained.
    """
    cat, _ = _armed_catalog(tmp_path, monkeypatch, with_gps=False)
    ev.execute(None, cat.path, [], lambda *a, **k: None, catalog_id=1)

    c = cat.conn()
    rows = [dict(r) for r in c.execute(
        'SELECT latitude, longitude, location_source FROM events')]
    c.close()
    assert rows, 'the fixture must produce at least one event'
    for r in rows:
        assert r['latitude'] is None
        assert r['longitude'] is None
        assert r['location_source'] is None


def test_a_measured_coordinate_is_still_recorded_with_its_provenance(tmp_path,
                                                                     monkeypatch):
    cat, _ = _armed_catalog(tmp_path, monkeypatch, with_gps=True)
    ev.execute(None, cat.path, [], lambda *a, **k: None, catalog_id=1)

    c = cat.conn()
    rows = [dict(r) for r in c.execute(
        'SELECT latitude, location_source FROM events')]
    c.close()
    assert rows
    for r in rows:
        assert r['latitude'] == pytest.approx(A[0], abs=1e-4)
        assert r['location_source'] == 'member'


def test_grouping_is_unchanged_by_removing_the_fallback(tmp_path, monkeypatch):
    """
    The claim that makes the removal safe, asserted rather than argued: the
    fallback was read AFTER grouping finished, for the INSERT alone, so a
    populated home location cannot change an event boundary.
    """
    def boundaries(armed):
        d = tmp_path / ('armed' if armed else 'bare')
        d.mkdir()
        if armed:
            cat, _ = _armed_catalog(d, monkeypatch, with_gps=False, files=0)
        else:
            monkeypatch.delenv('AIRLOCK_APP_DB', raising=False)
            cat = Cat(str(d))
        # Two clusters in time, one 6 hours after the other: EVENT_GAP_HOURS is
        # 2, so this must always be two events.
        for i in range(3):
            cat.add_file(when=f'2019-04-21 10:0{i}:00')
        for i in range(3):
            cat.add_file(when=f'2019-04-21 16:0{i}:00')
        ev.execute(None, cat.path, [], lambda *a, **k: None, catalog_id=1)
        c = cat.conn()
        out = sorted(tuple(r) for r in c.execute(
            'SELECT e.start_time, e.end_time, COUNT(ef.file_id) FROM events e '
            'LEFT JOIN event_files ef ON ef.event_id = e.id GROUP BY e.id'))
        c.close()
        return out

    armed = boundaries(armed=True)
    bare = boundaries(armed=False)
    assert armed == bare
    assert len(armed) == 2, 'the fixture must produce two events to compare'


# ─── No circularity (spec 4.5c, 5 invariant) ──────────────────────────────────

def test_inherited_coordinates_do_not_feed_the_next_clustering_run(cat,
                                                                   monkeypatch):
    """
    Clustering reads coordinates. If an inherited one entered that pool, run
    n+1 would cluster run n's inventions and the error would compound.
    """
    monkeypatch.setenv('LOCATION_INHERIT_COORDINATES', '1')
    seeing = _place(cat, A, 8)
    blind = [cat.add_file() for _ in range(4)]
    cat.add_event(seeing + blind)
    cat.cluster()
    before = [(c['geohash_prefix'], c['file_count'], round(c['latitude'], 9))
              for c in cat.clusters()]
    home = cat.location_for_cluster(cat.clusters()[0]['geohash_prefix'])
    cat.confirm(home, 'Home')
    cat.resolve()
    cat.cluster()
    after = [(c['geohash_prefix'], c['file_count'], round(c['latitude'], 9))
             for c in cat.clusters()]
    assert before == after


def test_clustering_never_reads_the_raw_exif_blob(cat):
    """
    The coordinates are promoted columns; raw_exif_json is the largest column in
    exif_data and must not be touched (spec 11).
    """
    import re
    src = open(os.path.join(HERE, 'workflows', 'ge_extract_locations.py')).read()
    # Strip the docstring and comments before looking: the word appears in both.
    code = re.sub(r'#.*', '', re.sub(r'"""[\s\S]*?"""', '', src))
    assert 'raw_exif_json' not in code


# ─── Registration (spec 7.1) ──────────────────────────────────────────────────
# A workflow is not registered until five separate places know about it, and a
# miss in any one of them is SILENT: workflow_triggers skips a script that is
# not registered for a catalog, so the automation is simply absent with nothing
# reporting a problem. Asserted rather than trusted for that reason.

GE = ('ge_extract_locations.py', 'ge_resolve_locations.py')


def test_both_workflows_are_in_the_default_manifest():
    scripts = {e['script'] for e in workflow_defaults.default_entries()}
    for s in GE:
        assert s in scripts, f'{s} is not a default, so a new catalog will not have it'


def test_the_manifest_orders_them_after_events_and_before_faces():
    order = {e['script']: e['order'] for e in workflow_defaults.load_manifest()}
    assert order['ev_extract_events.py'] < order['ge_extract_locations.py']
    assert order['ge_extract_locations.py'] < order['ge_resolve_locations.py']
    assert order['ge_resolve_locations.py'] < order['ai_auto_detect_faces_workflow.py']


def test_neither_needs_configuring_before_it_can_run():
    """Both read the catalog database, so there is no source directory to set.
    An ingestion workflow is created unconfigured and refuses to run; these
    must not be mistaken for one."""
    for s in GE:
        assert not workflow_defaults.is_ingestion_script(s)
        assert not workflow_defaults.needs_configuration(
            {'python_script': f'workflows/{s}', 'source_directory': ''})


def test_write_sets_let_them_run_beside_an_import_and_a_face_pass():
    for s in GE:
        assert sched.write_set(s), f'{s} has no write set, so it conflicts with everything'
    for other in ('io_picture_import_workflow.py', 'io_picture_import_v2_workflow.py',
                  'ai_auto_detect_faces_workflow.py', 'ev_extract_events.py'):
        for s in GE:
            assert not sched.conflicts(s, other), f'{s} would be serialised behind {other}'
    assert not sched.conflicts(*GE)


def test_both_can_be_stopped_at_an_item_boundary():
    for s in GE:
        assert sched.stop_mode(s) == 'item'
        assert sched.stop_refusal(s, s) is None


def test_resolve_runs_after_events_and_clustering_does_not_wait_for_them():
    """
    Clustering coordinates has nothing to do with events, so an edge there would
    serialise two unrelated passes. Resolving DOES need them - it reads
    event_files for the inheritance.
    """
    assert 'ev_extract_events.py' not in trig.DEPENDS_ON['ge_extract_locations.py']
    assert trig.INGEST_SCRIPTS <= trig.DEPENDS_ON['ge_extract_locations.py']
    assert 'ev_extract_events.py' in trig.DEPENDS_ON['ge_resolve_locations.py']
    assert 'ge_extract_locations.py' in trig.DEPENDS_ON['ge_resolve_locations.py']


def test_every_dependency_edge_states_its_reason():
    for s in GE:
        for dep in trig.DEPENDS_ON[s]:
            assert trig.dependency_reason(s, dep), f'{s} <- {dep} has no reason'


def test_an_import_queues_both_of_them():
    for s in GE:
        assert s in trig.TRIGGERS['import_completed'], \
            f'{s} would never run on its own after an import'


def test_rebuilding_events_queues_the_resolve_pass():
    """
    The edge that prevents silent data loss. Create Events DELETES every Auto
    event and recreates it with a new id, so every place a photo inherited is
    orphaned the moment it finishes. Without this the inherited places vanish
    and stay gone until somebody runs Resolve Locations by hand.
    """
    assert 'ge_resolve_locations.py' in trig.TRIGGERS['events_changed']
    assert trig.triggers_for_completed_workflow('ev_extract_events.py') == ('events_changed',)


def test_naming_a_place_queues_the_resolve_pass():
    assert 'ge_resolve_locations.py' in trig.TRIGGERS['locations_changed']
    # In bursts, like tagging faces: eight accepts must not mean eight passes.
    assert trig.quiet_seconds('locations_changed') > 0
    assert trig.describe('locations_changed')


def test_clustering_again_queues_the_resolve_pass():
    assert trig.triggers_for_completed_workflow('ge_extract_locations.py') == ('locations_clustered',)
    assert 'ge_resolve_locations.py' in trig.TRIGGERS['locations_clustered']


def test_a_finished_workflow_does_not_wait_out_a_human_quiet_period():
    """
    locations_clustered and locations_changed enqueue the same thing, and exist
    separately only because of this. A person naming places does it in bursts
    and wants the delay; Extract Locations finishing happens once and will not
    repeat, so the same delay is two minutes of latency for nothing - the same
    reasoning that gives import_completed a quiet period of zero.
    """
    assert trig.quiet_seconds('locations_clustered') == 0
    assert trig.quiet_seconds('locations_changed') > 0
    assert (trig.TRIGGERS['locations_clustered']
            == trig.TRIGGERS['locations_changed']), \
        'if these ever enqueue different things, the split needs a better reason'


def test_the_resolve_pass_raises_nothing_so_the_chain_terminates():
    assert trig.triggers_for_completed_workflow('ge_resolve_locations.py') == ()


# ─── The manifest as a whole (workflows-default-spec.md section 9) ────────────
# That spec names this risk and asks for exactly this test, and it had never
# been written: "A typo in the manifest produces a workflow that appears in the
# UI, runs when clicked, and is never included in any chain - silently, since
# unregistered scripts are skipped by design."
#
# This is the test that would have caught the location workflows being absent
# from TRIGGERS, so it covers the whole manifest rather than only them.

def test_every_default_script_exists_on_disk():
    for entry in workflow_defaults.load_manifest():
        script = os.path.join(HERE, 'workflows', entry['script'])
        assert os.path.exists(script), \
            f"{entry['script']} is in defaults.json but not on disk"


def test_every_default_script_is_in_the_trigger_map():
    """
    A workflow in no chain only ever runs when somebody clicks it. Being in
    DEPENDS_ON is not enough: that map only GATES. TRIGGERS is what enqueues.
    """
    for entry in workflow_defaults.load_manifest():
        script = entry['script']
        if script in trig.INGEST_SCRIPTS:
            continue        # an import is started by a person or a schedule
        in_triggers = any(script in scripts for scripts in trig.TRIGGERS.values())
        assert in_triggers, \
            f'{script} is a default but no trigger queues it, so it will only ' \
            'ever run when clicked'


def test_every_default_script_has_a_write_set_and_a_stop_mode():
    for entry in workflow_defaults.load_manifest():
        script = entry['script']
        assert sched.write_set(script) is not None, \
            f'{script} has no write set, so it is assumed to conflict with everything'
        assert sched.stop_mode(script) in ('item', 'never'), \
            f'{script} has no stop mode, so a running one cannot be stopped'


def test_every_script_named_in_the_trigger_map_is_a_real_file():
    """The other direction: a typo in TRIGGERS or DEPENDS_ON is just as quiet."""
    named = set()
    for scripts in trig.TRIGGERS.values():
        named.update(scripts)
    for dependent, deps in trig.DEPENDS_ON.items():
        named.add(dependent)
        named.update(deps)
    for script in named:
        assert os.path.exists(os.path.join(HERE, 'workflows', script)), \
            f'{script} is named in the trigger map but is not on disk'


def test_import_workflows_are_not_defaults():
    """
    An import workflow is NOT seeded. The catalog gets one per STORAGE ROUTE,
    named after it, created by _create_import_workflow_for_route - because a
    route is where the files come from and a workflow without one has nothing
    to read. Both import entries are therefore marked default:false, and
    anything that treats the manifest as "the defaults" without reading that
    flag will claim the catalog is missing "Ingest Files" when it is not.
    """
    for entry in workflow_defaults.load_manifest():
        if entry['script'] in trig.INGEST_SCRIPTS:
            assert entry['ingestion'] is True, \
                f"{entry['script']} must be marked ingestion"
            assert entry['default'] is False, \
                f"{entry['script']} must not be a default: imports come from " \
                'storage routes, and one seeded without a route cannot run'


def test_seeding_never_creates_an_import_workflow(tmp_path):
    """The other half of the same rule, asserted against the behaviour rather
    than the manifest: a seeded import workflow would have no storage route."""
    from models import CatalogDatabase, WorkflowModel
    cdb = CatalogDatabase(str(tmp_path))
    workflow_defaults.seed_default_workflows(cdb)
    scripts = {os.path.basename(w['python_script'])
               for w in WorkflowModel(cdb).get_all()}
    assert not (scripts & trig.INGEST_SCRIPTS), \
        'seeding produced an import workflow with no storage route'
    # And the ones that ARE defaults did arrive.
    assert 'ge_extract_locations.py' in scripts
    assert 'ev_extract_events.py' in scripts


def test_syncing_never_creates_an_import_workflow(tmp_path):
    from models import CatalogDatabase, WorkflowModel
    cdb = CatalogDatabase(str(tmp_path))
    workflow_defaults.sync_default_workflows(cdb)
    scripts = {os.path.basename(w['python_script'])
               for w in WorkflowModel(cdb).get_all()}
    assert not (scripts & trig.INGEST_SCRIPTS)


# ─── Keeping an existing catalog in step (spec 8.4b) ──────────────────────────

def _catalog_with(scripts, tmp_path):
    """A catalog whose workflows table holds exactly these scripts."""
    from models import CatalogDatabase, WorkflowModel
    cdb = CatalogDatabase(str(tmp_path))
    model = WorkflowModel(cdb)
    for script in scripts:
        model.create(name=script, description='', source_directory='',
                     python_script=f'workflows/{script}', workflow_type='Test')
    return cdb


def test_a_catalog_that_predates_the_workflows_gets_them(tmp_path):
    """
    The case both real catalogs are in: created before the location workflows
    existed, so they have no row for either - and an unregistered workflow is
    skipped by every trigger chain with nothing reporting it.
    """
    cdb = _catalog_with(['io_picture_import_workflow.py', 'ev_extract_events.py'],
                        tmp_path)
    result = workflow_defaults.sync_default_workflows(cdb)
    assert 'Extract Locations' in result['created']
    assert 'Resolve Locations' in result['created']

    from models import WorkflowModel
    have = {os.path.basename(w['python_script'])
            for w in WorkflowModel(cdb).get_all()}
    assert 'ge_extract_locations.py' in have
    assert 'ge_resolve_locations.py' in have


def test_syncing_twice_adds_nothing_the_second_time(tmp_path):
    cdb = _catalog_with(['io_picture_import_workflow.py'], tmp_path)
    first = workflow_defaults.sync_default_workflows(cdb)
    assert first['created']
    second = workflow_defaults.sync_default_workflows(cdb)
    assert second['created'] == []


def test_a_deleted_default_is_not_put_back(tmp_path):
    """
    The reason this is a watermark and not "add whatever is missing". A workflow
    somebody deliberately removed IS missing, so a plain seed would restore it
    on every request.
    """
    from models import WorkflowModel
    cdb = _catalog_with(['io_picture_import_workflow.py'], tmp_path)
    workflow_defaults.sync_default_workflows(cdb)

    model = WorkflowModel(cdb)
    victim = next(w for w in model.get_all()
                  if os.path.basename(w['python_script']) == 'ge_extract_locations.py')
    model.delete(victim['id'])

    result = workflow_defaults.sync_default_workflows(cdb)
    assert result['created'] == []
    have = {os.path.basename(w['python_script']) for w in model.get_all()}
    assert 'ge_extract_locations.py' not in have, 'a deliberate deletion must stick'


def test_the_watermark_records_every_default(tmp_path):
    """
    Every DEFAULT, not every manifest entry: the two import workflows are
    listed with default=false, because an import needs a source directory only
    the user knows and one created unconfigured cannot run. An entry that later
    flips to default=true is correctly absent from the watermark, and so gets
    created on the next sync.
    """
    from models import get_catalog_property
    cdb = _catalog_with([], tmp_path)
    workflow_defaults.sync_default_workflows(cdb)
    raw = get_catalog_property(cdb.db_path, workflow_defaults.SEEN_PROPERTY)
    seen = set(json.loads(raw))
    assert {e['script'] for e in workflow_defaults.default_entries()} <= seen
    assert 'ge_extract_locations.py' in seen
    assert 'ge_resolve_locations.py' in seen


# ─── Launched the way the runner launches them ────────────────────────────────
# The tests above import these modules, and this file puts both workflows/ and
# the backend directory on sys.path before it does - so none of them exercised
# how the workflow is actually started, and all 47 passed while a real run died
# on `from lib.stopping import ...` with "No module named 'lib'" before reaching
# a line of its own code.
#
# A test that imports a module cannot tell you whether it can be run. These run
# it, as its own program, with exactly the arguments app.py passes.

SUBPROCESS_SCRIPTS = ('ge_extract_locations.py', 'ge_resolve_locations.py')


def _real_catalog(tmp_path):
    """A catalog with the REAL schema, since the script runs its own queries."""
    from models import CatalogDatabase
    cdb = CatalogDatabase(str(tmp_path))
    conn = sqlite3.connect(cdb.db_path)
    for i in range(1, 5):
        name = f'IMG_{i}.JPG'
        conn.execute('INSERT INTO file_contents (id, file_name, file_path, '
                     'master_datetime, added_at) VALUES (?, ?, ?, ?, ?)',
                     (i, name, '/c/' + name, '2019-04-21 10:00:00',
                      '2019-04-21 10:00:00'))
        lat, lon = A[0] + i * 0.00002, A[1]
        conn.execute('INSERT INTO exif_data (file_id, file_name, latitude, '
                     'longitude, geohash) VALUES (?, ?, ?, ?, ?)',
                     (i, name, lat, lon, pygeohash.encode(lat, lon, precision=10)))
    conn.commit()
    conn.close()
    return cdb


@pytest.mark.parametrize('script', SUBPROCESS_SCRIPTS)
def test_the_workflow_runs_as_its_own_program(script, tmp_path, monkeypatch):
    """
    Exactly app.py's subprocess contract: the interpreter, the script, and
    --execution-id / --target-dir / --catalog-id.
    """
    import subprocess
    from models import ExecutionModel, WorkflowModel

    cdb = _real_catalog(tmp_path)
    wf_id = WorkflowModel(cdb).create(
        name=script, description='', source_directory='',
        python_script=f'workflows/{script}', workflow_type='Locations')
    execution_id = ExecutionModel(cdb).create(wf_id, script)

    env = dict(os.environ, GEOLOC_URL='http://127.0.0.1:9')
    proc = subprocess.run(
        [sys.executable, os.path.join(HERE, 'workflows', script),
         '--execution-id', str(execution_id),
         '--target-dir', str(tmp_path),
         '--catalog-id', '1'],
        capture_output=True, text=True, timeout=120, env=env)

    assert proc.returncode == 0, \
        f'{script} exited {proc.returncode}\n--- stdout\n{proc.stdout}\n--- stderr\n{proc.stderr}'
    assert 'No module named' not in proc.stderr, proc.stderr

    conn = sqlite3.connect(cdb.db_path)
    status = conn.execute('SELECT status FROM workflow_executions WHERE id = ?',
                          (execution_id,)).fetchone()[0]
    conn.close()
    assert status == 'completed', f'{script} finished as {status!r}'


@pytest.mark.parametrize('script', SUBPROCESS_SCRIPTS)
def test_the_workflow_is_importable_without_help_from_the_launcher(script):
    """
    The threading path imports the module with spec_from_file_location, which
    does NOT put workflows/ on sys.path the way running it as a script does. A
    module that only works one of those two ways works by luck.
    """
    import subprocess
    probe = (
        'import importlib.util, sys\n'
        f'sys.path.insert(0, {os.path.join(HERE)!r})\n'
        f'spec = importlib.util.spec_from_file_location("probe", '
        f'{os.path.join(HERE, "workflows", script)!r})\n'
        'm = importlib.util.module_from_spec(spec)\n'
        'spec.loader.exec_module(m)\n'
        'assert callable(m.execute)\n'
    )
    proc = subprocess.run([sys.executable, '-c', probe],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 0, \
        f'{script} could not be imported the way the threading path does:\n{proc.stderr}'


def test_every_stoppable_workflow_runs_as_a_subprocess():
    """
    WORKFLOW_STOP_MODES is the authoritative list - its docstring says an
    unlisted script "runs on the threading path in app.py, and a Python thread
    cannot be killed". app.py used to keep a second hand-maintained copy of the
    same set, and the two had drifted: a script listed as stoppable that in fact
    ran in a thread both failed to import and advertised a Stop button that
    could not work.
    """
    import importlib
    spec = importlib.util.find_spec('app')
    assert spec is not None
    # Checked against the source rather than by importing app (which starts a
    # scheduler): the point is that no literal set is maintained here.
    src = open(os.path.join(HERE, 'app.py'), encoding='utf-8').read()
    body = src[src.index('def _is_subprocess_workflow'):]
    body = body[:body.index('\ndef ', 1)]
    assert 'WORKFLOW_STOP_MODES' in body, \
        '_is_subprocess_workflow must derive from WORKFLOW_STOP_MODES, not list scripts again'


@pytest.mark.parametrize('script', SUBPROCESS_SCRIPTS)
def test_the_subprocess_entry_point_takes_the_arguments_app_passes(script):
    src = open(os.path.join(HERE, 'workflows', script), encoding='utf-8').read()
    for flag in ('--execution-id', '--target-dir', '--catalog-id'):
        assert flag in src, f'{script} does not accept {flag}'


def test_a_rerun_with_nothing_changed_rewrites_nothing(cat):
    """
    The exif and cluster phases rewrote every file_locations row - ~350,000
    on `family`, updated_at and all - on every run, changed or not. A row
    that would come out the same is now left as it is; a real change is still
    written.
    """
    _place(cat, A, 4)
    cat.cluster()
    cat.resolve()
    c = cat.conn()
    c.execute("UPDATE file_locations SET updated_at = 'sentinel'")
    c.commit()
    c.close()

    cat.resolve()
    c = cat.conn()
    stamps = {r[0] for r in c.execute('SELECT updated_at FROM file_locations')}
    assert stamps == {'sentinel'}, 'an unchanged row was rewritten'

    moved = c.execute('SELECT file_id FROM file_locations LIMIT 1').fetchone()[0]
    c.execute('UPDATE exif_data SET latitude = latitude + 0.001 WHERE file_id = ?', (moved,))
    c.commit()
    c.close()
    cat.resolve()
    row = cat.file_location(moved)
    assert row['updated_at'] != 'sentinel', 'a real change must still be written'
