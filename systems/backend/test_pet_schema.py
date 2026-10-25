#!/usr/bin/env python3
"""
The pet schema, and the five invariants the design rests on.

See documentation/specs/pet-recognition-spec.md section 3.8. Most of what is
asserted here is that something is INVISIBLE or IMPOSSIBLE rather than that a
number came out right, because the failures being designed against are silent
ones: a pet entering the human reference set, two embedding spaces meeting in
one distance, a model inventing a family member.
"""

import json
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from pet_schema import ensure_pet_schema


def _catalog(path):
    """A catalog shaped like a real one, before pets have ever been seen."""
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY, file_path TEXT,
                                    removed_at TIMESTAMP);
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT,
                             category TEXT DEFAULT 'Other',
                             date_of_birth TEXT,
                             profile_photo_file_id INTEGER);
        CREATE TABLE face_detections (id INTEGER PRIMARY KEY, file_id INTEGER,
                                      person_id INTEGER, source TEXT,
                                      x INTEGER, y INTEGER, w INTEGER, h INTEGER);
        CREATE TABLE face_contents (id INTEGER PRIMARY KEY, embedding TEXT);
        CREATE TABLE ai_face_contents (id INTEGER PRIMARY KEY,
                                       face_detection_id INTEGER, embedding TEXT);
    """)
    conn.commit()
    conn.close()
    ensure_pet_schema(path)
    return path


@pytest.fixture
def catalog(tmp_path):
    return _catalog(str(tmp_path / 'workflow_results.db'))


def _conn(path):
    c = sqlite3.connect(path)
    c.row_factory = sqlite3.Row
    return c


# ── The schema itself ────────────────────────────────────────────────────────

def test_every_table_and_the_view_are_created(catalog):
    c = _conn(catalog)
    objects = {r[0] for r in c.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}
    c.close()
    for t in ('pet_detections', 'ai_pet_contents', 'pet_similarity_scores',
              'pet_clusters', 'pet_cluster_members', 'subject_appearances'):
        assert t in objects, f'{t} missing'


def test_running_it_twice_changes_nothing(catalog):
    assert ensure_pet_schema(catalog) == []


def test_it_upgrades_a_catalog_that_has_never_seen_pets(tmp_path):
    """
    The realistic case: every existing catalog. No backfill is wanted, so an
    existing person must come out correct by DEFAULT rather than by UPDATE.
    """
    p = str(tmp_path / 'old.db')
    conn = sqlite3.connect(p)
    conn.executescript("""
        CREATE TABLE file_contents (id INTEGER PRIMARY KEY);
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT, category TEXT);
        CREATE TABLE face_detections (id INTEGER PRIMARY KEY, file_id INTEGER,
                                      person_id INTEGER);
    """)
    conn.execute("INSERT INTO people (name, category) VALUES ('Izelpi', 'Family')")
    conn.commit()
    conn.close()

    ensure_pet_schema(p)

    c = _conn(p)
    row = c.execute('SELECT name, category, kind, species FROM people').fetchone()
    c.close()
    assert row['kind'] == 'person', 'an existing row must default to person'
    assert row['species'] is None
    assert row['category'] == 'Family', 'category is the household and is untouched'


def test_pet_detections_carries_the_detection_coordinate_space(catalog):
    """
    image_width/image_height are not optional: boxes arrive in the detection
    image's space, not the original's, and the crop rescales from them.
    documentation/fixes/ has nine files about getting this wrong.
    """
    cols = {r[1] for r in _conn(catalog).execute('PRAGMA table_info(pet_detections)')}
    assert {'image_width', 'image_height'} <= cols


def test_the_detection_column_is_person_id_not_pet_id(catalog):
    """
    It is a foreign key to people.id exactly as face_detections.person_id is,
    and subject_appearances only unions cleanly if both mean the same thing.
    Every reviewer wants to rename it.
    """
    cols = {r[1] for r in _conn(catalog).execute('PRAGMA table_info(pet_detections)')}
    assert 'person_id' in cols
    assert 'pet_id' not in cols


# ── I1: a pet never enters the human reference set ───────────────────────────

def test_a_pet_cannot_enter_the_face_reference_set(catalog):
    """
    I1, and the reason the tables are separate.

    Every query that builds the face reference set starts FROM face_detections
    and joins outward. A pet in `people` with its boxes in pet_detections has
    no rows there, so it cannot appear - not because a filter excludes it, but
    because there is nothing to join to. Asserted by running the real shape of
    that query.
    """
    c = _conn(catalog)
    c.execute("INSERT INTO people (id,name,category,kind,species) "
              "VALUES (1,'Izelpi','Family','person',NULL)")
    c.execute("INSERT INTO people (id,name,category,kind,species) "
              "VALUES (2,'Rex','Family','pet','dog')")
    c.execute("INSERT INTO file_contents (id) VALUES (10)")
    c.execute("INSERT INTO face_detections (file_id,person_id,source) VALUES (10,1,'Manual')")
    c.execute("INSERT INTO pet_detections (file_id,x,y,w,h,person_id,source) "
              "VALUES (10,1,1,9,9,2,'Manual')")
    c.commit()

    # ai_ingest_manual_faces.py / ai_face_recognition_workflow.py shape
    refs = [r['name'] for r in c.execute("""
        SELECT p.name FROM face_detections fd
        INNER JOIN people p ON p.id = fd.person_id
        WHERE fd.source = 'Manual' AND fd.person_id IS NOT NULL
    """)]
    c.close()
    assert refs == ['Izelpi'], f'a pet reached the human reference set: {refs}'


def test_a_pet_in_the_family_category_is_still_invisible_to_faces(catalog):
    """
    The tempting shortcut was to keep pets out of `people` entirely. They are
    in it, in the same household category - and that is still safe, because
    isolation comes from the detection table and not from the category.
    """
    c = _conn(catalog)
    c.execute("INSERT INTO people (id,name,category,kind) VALUES (1,'Rex','Family','pet')")
    c.execute("INSERT INTO file_contents (id) VALUES (10)")
    c.execute("INSERT INTO pet_detections (file_id,x,y,w,h,person_id) VALUES (10,1,1,9,9,1)")
    c.commit()
    n = c.execute('SELECT COUNT(*) FROM face_detections '
                  ' WHERE person_id IS NOT NULL').fetchone()[0]
    c.close()
    assert n == 0


# ── I2: a cluster is not a pet ───────────────────────────────────────────────

def test_a_cluster_exists_without_any_people_row(catalog):
    """
    I2. An unknown animal is a group of detections, never a `people` row: a row
    in `people` means somebody said who this is. A detector that invented one
    per unrecognised animal would fill the family with strangers.
    """
    c = _conn(catalog)
    c.execute("INSERT INTO file_contents (id) VALUES (10)")
    c.execute("INSERT INTO pet_detections (id,file_id,x,y,w,h,person_id) "
              "VALUES (5,10,1,1,9,9,NULL)")
    c.execute("INSERT INTO pet_clusters (id,centroid,member_count) "
              "VALUES (1,?,1)", (json.dumps([0.1] * 512),))
    c.execute("INSERT INTO pet_cluster_members (cluster_id,pet_detection_id,"
              "distance_to_centroid) VALUES (1,5,0.0)")
    c.commit()
    assert c.execute('SELECT COUNT(*) FROM people').fetchone()[0] == 0
    assert c.execute('SELECT resolved_person_id FROM pet_clusters').fetchone()[0] is None
    c.close()


def test_naming_a_cluster_is_what_creates_the_pet(catalog):
    """The promotion: one UPDATE, as app.py does for face_clusters."""
    c = _conn(catalog)
    c.execute("INSERT INTO file_contents (id) VALUES (10)")
    c.execute("INSERT INTO pet_detections (id,file_id,x,y,w,h) VALUES (5,10,1,1,9,9)")
    c.execute("INSERT INTO pet_clusters (id,centroid,member_count) VALUES (1,'[]',1)")
    c.execute("INSERT INTO pet_cluster_members (cluster_id,pet_detection_id,"
              "distance_to_centroid) VALUES (1,5,0.0)")
    c.commit()

    c.execute("INSERT INTO people (name,category,kind,species) "
              "VALUES ('Rex','Family','pet','dog')")
    pet_id = c.execute('SELECT last_insert_rowid()').fetchone()[0]
    c.execute("UPDATE pet_clusters SET resolved_person_id=?, resolved_at=CURRENT_TIMESTAMP "
              " WHERE id=1", (pet_id,))
    c.execute("UPDATE pet_detections SET person_id=? WHERE id IN "
              "(SELECT pet_detection_id FROM pet_cluster_members WHERE cluster_id=1)",
              (pet_id,))
    c.commit()

    # and every member detection is now a link from a photograph to that pet
    assert c.execute("SELECT COUNT(*) FROM subject_appearances "
                     " WHERE kind='pet' AND person_id=?", (pet_id,)).fetchone()[0] == 1
    c.close()


# ── I3 / the view ────────────────────────────────────────────────────────────

def test_the_view_unions_people_and_pets(catalog):
    c = _conn(catalog)
    c.execute("INSERT INTO people (id,name,kind) VALUES (1,'Izelpi','person'),(2,'Rex','pet')")
    c.execute("INSERT INTO file_contents (id) VALUES (10),(11)")
    c.execute("INSERT INTO face_detections (file_id,person_id) VALUES (10,1)")
    c.execute("INSERT INTO pet_detections (file_id,x,y,w,h,person_id) "
              "VALUES (10,1,1,9,9,2),(11,1,1,9,9,2)")
    c.commit()

    def photos_with(name):
        return sorted(r[0] for r in c.execute("""
            SELECT DISTINCT sa.file_id FROM subject_appearances sa
            JOIN people p ON p.id = sa.person_id WHERE p.name = ?""", (name,)))

    assert photos_with('Izelpi') == [10]
    assert photos_with('Rex') == [10, 11], 'the same query shape must work for a pet'
    assert sorted(r[0] for r in c.execute(
        "SELECT DISTINCT file_id FROM subject_appearances WHERE kind='pet'")) == [10, 11]
    c.close()


def test_the_view_hides_unidentified_detections(catalog):
    """
    An unknown has no identity to link TO. Unknowns are met as groups on the
    review page, not found by searching for them one at a time.
    """
    c = _conn(catalog)
    c.execute("INSERT INTO file_contents (id) VALUES (10)")
    c.execute("INSERT INTO pet_detections (file_id,x,y,w,h,person_id) "
              "VALUES (10,1,1,9,9,NULL)")
    c.execute("INSERT INTO face_detections (file_id,person_id) VALUES (10,NULL)")
    c.commit()
    assert c.execute('SELECT COUNT(*) FROM pet_detections').fetchone()[0] == 1
    assert c.execute('SELECT COUNT(*) FROM subject_appearances').fetchone()[0] == 0
    c.close()


def test_the_view_stores_nothing(catalog):
    """
    A view, not a table - so the physical separation that I1 depends on
    survives it, and there is no second copy of the truth to drift.
    """
    kind = _conn(catalog).execute(
        "SELECT type FROM sqlite_master WHERE name='subject_appearances'").fetchone()[0]
    assert kind == 'view'


def test_the_view_is_rebuilt_rather_than_kept_stale(catalog):
    """
    DROP then CREATE, not IF NOT EXISTS: a changed definition has to take
    effect. Proven by replacing it with a wrong one and re-running.
    """
    c = sqlite3.connect(catalog)
    c.execute('DROP VIEW subject_appearances')
    c.execute('CREATE VIEW subject_appearances AS SELECT 1 AS file_id, '
              "1 AS person_id, 'wrong' AS kind, 1 AS detection_id")
    c.commit()
    c.close()

    ensure_pet_schema(catalog)

    c = _conn(catalog)
    sql = c.execute("SELECT sql FROM sqlite_master WHERE name='subject_appearances'").fetchone()[0]
    c.close()
    assert 'wrong' not in sql
    assert 'face_detections' in sql and 'pet_detections' in sql


# ── Category is the household, kind is the species ───────────────────────────

def test_a_pet_shares_its_household_category_with_its_owners(catalog):
    """
    "My pet can be part of my family, but my sister's cat can be part of her
    family." category is the household; kind is what species a row describes.
    An earlier draft asked which of 'Pets' or 'Family' the category should be -
    the question conflated two different facts.
    """
    c = _conn(catalog)
    c.execute("INSERT INTO people (name,category,kind) VALUES ('Izelpi','Family','person')")
    c.execute("INSERT INTO people (name,category,kind,species) "
              "VALUES ('Rex','Family','pet','dog')")
    c.execute("INSERT INTO people (name,category,kind,species) "
              "VALUES ('Mittens','Sister','pet','cat')")
    c.commit()
    family = sorted(r[0] for r in c.execute(
        "SELECT name FROM people WHERE category='Family'"))
    pets = sorted(r[0] for r in c.execute(
        "SELECT name FROM people WHERE kind='pet'"))
    c.close()
    assert family == ['Izelpi', 'Rex']
    assert pets == ['Mittens', 'Rex']


def test_the_people_page_and_the_pets_page_are_one_query_apart(catalog):
    c = _conn(catalog)
    c.execute("INSERT INTO people (name,kind) VALUES ('Izelpi','person'),('Rex','pet')")
    c.commit()
    assert [r[0] for r in c.execute("SELECT name FROM people WHERE kind='person'")] == ['Izelpi']
    assert [r[0] for r in c.execute("SELECT name FROM people WHERE kind='pet'")] == ['Rex']
    c.close()


# ── Deletion ─────────────────────────────────────────────────────────────────

def test_deleting_a_pet_leaves_its_detections_but_unnames_them(catalog):
    """
    ON DELETE SET NULL, matching face_detections. The measurement survives the
    judgement being withdrawn - the animal is still there in the photograph,
    it just has no name again. Note this application does not enable
    PRAGMA foreign_keys, so the test turns it on to check the DECLARATION is
    right; app code deletes explicitly.
    """
    c = _conn(catalog)
    c.execute('PRAGMA foreign_keys = ON')
    c.execute("INSERT INTO people (id,name,kind) VALUES (1,'Rex','pet')")
    c.execute("INSERT INTO file_contents (id) VALUES (10)")
    c.execute("INSERT INTO pet_detections (file_id,x,y,w,h,person_id) VALUES (10,1,1,9,9,1)")
    c.commit()
    c.execute('DELETE FROM people WHERE id=1')
    c.commit()
    rows = c.execute('SELECT person_id FROM pet_detections').fetchall()
    c.close()
    assert len(rows) == 1, 'the detection must survive'
    assert rows[0][0] is None, 'but it must no longer be named'


# ── The pet pass's own watermark ─────────────────────────────────────────────

def test_the_pet_pass_gets_its_own_watermark_column(catalog):
    """
    NOT face_detection_scanned_at, and this is the one that would have shipped
    broken. Every file in an existing catalog is already face-scanned and none
    is pet-scanned; sharing the column means the pet pass visits nothing,
    reports success, and finds no animals anywhere.

    Indexed, because the population query is "WHERE pets_scanned_at IS NULL"
    over every file in the catalog, once per run.
    """
    c = _conn(catalog)
    cols = {r[1]: r[2] for r in c.execute('PRAGMA table_info(file_contents)')}
    assert 'pets_scanned_at' in cols
    assert cols['pets_scanned_at'].upper().startswith('TIMESTAMP')

    idx = {r[1] for r in c.execute('PRAGMA index_list(file_contents)')}
    c.close()
    assert 'idx_fc_pets_scanned' in idx


def test_the_watermark_starts_null_for_every_existing_file(catalog):
    """
    Adding a column must not claim the catalog has already been examined.
    NULL means "not looked at", and that is what an ALTER TABLE gives - but it
    is worth asserting, because the opposite default would silently skip the
    entire backlog.
    """
    c = _conn(catalog)
    c.execute("INSERT INTO file_contents (id, file_path) VALUES (1, 'a.jpg')")
    c.commit()
    row = c.execute('SELECT pets_scanned_at FROM file_contents WHERE id=1').fetchone()
    c.close()
    assert row[0] is None


# ── Concurrency ──────────────────────────────────────────────────────────────

def test_ensuring_twice_does_not_touch_the_schema_again(catalog):
    """
    THE property that fixes the race, and the one worth asserting because it is
    deterministic.

    ensure_pet_schema runs on every catalog open. It used to DROP and CREATE
    the view every single time - which looked free, a view holds no data - and
    that is what made two threads opening one catalog able to collide with
    "view subject_appearances already exists", failing a request from the
    function whose job is to guarantee the schema.

    PRAGMA schema_version increments on any DDL, so it says plainly whether a
    second call wrote anything. The test is here rather than a threading one
    because a threading test of this passed against the broken code: the
    interleaving needs timing a test cannot reliably force, while "did it write
    when it had nothing to write" is exact.
    """
    def version():
        c = _conn(catalog)
        try:
            return c.execute('PRAGMA schema_version').fetchone()[0]
        finally:
            c.close()

    ensure_pet_schema(catalog)
    settled = version()

    ensure_pet_schema(catalog)
    assert version() == settled, \
        'the second call rewrote the schema; with nothing to change it must ' \
        'take no write lock at all'

    ensure_pet_schema(catalog)
    assert version() == settled


def test_a_concurrent_ensure_does_not_raise(catalog):
    """
    A smoke test, and described as one: eight threads calling ensure at once
    does NOT reliably reproduce the DROP/CREATE interleaving - it passed
    against the broken code - so it is here to catch a gross regression such as
    a deadlock or an unguarded write, not as proof the race is gone. The test
    above is the one with teeth.
    """
    import threading

    errors = []
    barrier = threading.Barrier(8)

    def go():
        try:
            barrier.wait(timeout=10)
            ensure_pet_schema(catalog)
        except Exception as e:             # noqa: BLE001 - the test IS the assertion
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)

    assert not errors, f'concurrent ensure_pet_schema raised: {errors}'

    c = _conn(catalog)
    n = c.execute("SELECT COUNT(*) FROM sqlite_master "
                  " WHERE type='view' AND name='subject_appearances'").fetchone()[0]
    c.close()
    assert n == 1
