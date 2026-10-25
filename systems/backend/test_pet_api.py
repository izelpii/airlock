#!/usr/bin/env python3
"""
The pet API, and the one default that the whole design leans on.

A pet is a `people` row, so creating and editing one goes through the people
endpoints with ?kind=pet. The tests that matter here are about what happens
when a caller does NOT say which kind it wants - because the answer has to be
"people", or the face tagger starts offering the dog.

Note the fixtures: `client` is imported alongside `catalog`, and both are
needed. A test that takes `client` without `catalog` passes alone and fails in
the suite - that has been the cause of two separate bugs in this repository, so
every test below requests both.
"""

import json
import os
import sqlite3

import pytest

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401


def _db(catalog):
    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    return conn


def _pet_workflow_names():
    """
    The two pet workflows' names, from the manifest.

    Not repeated here: these tests are about the workflows being created and
    removed, not about what they are called, and hard-coding the names made
    three tests fail the day the names were made clearer.
    """
    import workflow_defaults
    return [e['name'] for e in workflow_defaults.load_manifest()
            if e['script'] in workflow_defaults.PET_SCRIPTS]


def _person(client, catalog, name, **kw):
    body = {'name': name}
    body.update(kw)
    r = client.post(f'/api/catalogs/{catalog.id}/people', json=body)
    assert r.status_code == 201, r.get_json()
    return r.get_json()['id']


# ── The default ──────────────────────────────────────────────────────────────

def test_people_defaults_to_people_only(client, catalog):
    """
    THE load-bearing default. Every existing caller - the People page, the face
    tagger, the album editor, the user-linking dialog - asks without a kind and
    must not be handed a pet.
    """
    _person(client, catalog, 'Izelpi', category='Family')
    _person(client, catalog, 'Rex', category='Family', kind='pet', species='dog')

    names = [p['name'] for p in client.get(
        f'/api/catalogs/{catalog.id}/people').get_json()]
    assert names == ['Izelpi'], f'a pet leaked into the default listing: {names}'


def test_kind_pet_lists_only_pets(client, catalog):
    _person(client, catalog, 'Izelpi')
    _person(client, catalog, 'Rex', kind='pet', species='dog')
    names = [p['name'] for p in client.get(
        f'/api/catalogs/{catalog.id}/people?kind=pet').get_json()]
    assert names == ['Rex']


def test_kind_all_lists_both(client, catalog):
    _person(client, catalog, 'Izelpi')
    _person(client, catalog, 'Rex', kind='pet')
    names = sorted(p['name'] for p in client.get(
        f'/api/catalogs/{catalog.id}/people?kind=all').get_json())
    assert names == ['Izelpi', 'Rex']


def test_a_nonsense_kind_is_refused(client, catalog):
    r = client.get(f'/api/catalogs/{catalog.id}/people?kind=aardvark')
    assert r.status_code == 400


# ── A pet is a person row ────────────────────────────────────────────────────

def test_a_pet_shares_the_household_category(client, catalog):
    """
    "My pet can be part of my family, but my sister's cat can be part of her
    family." The category vocabulary is shared, not forked.
    """
    _person(client, catalog, 'Izelpi', category='Family')
    pet = _person(client, catalog, 'Rex', category='Family', kind='pet',
                  species='dog')
    row = client.get(f'/api/catalogs/{catalog.id}/people/{pet}').get_json()
    assert row['category'] == 'Family'
    assert row['kind'] == 'pet'
    assert row['species'] == 'dog'


def test_kind_cannot_be_changed_after_creation(client, catalog):
    """
    Turning a dog into a person would orphan its pet_detections into a row the
    face chain now treats as a reference. Refused, with a reason.
    """
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    r = client.put(f'/api/catalogs/{catalog.id}/people/{pet}',
                   json={'kind': 'person'})
    assert r.status_code == 400
    assert 'cannot be changed' in r.get_json()['error']
    # and it really did not change
    assert client.get(f'/api/catalogs/{catalog.id}/people/{pet}'
                      ).get_json()['kind'] == 'pet'


def test_editing_a_pet_without_touching_kind_works(client, catalog):
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    r = client.put(f'/api/catalogs/{catalog.id}/people/{pet}',
                   json={'name': 'Rex the Second', 'kind': 'pet'})
    assert r.status_code == 200, r.get_json()
    assert client.get(f'/api/catalogs/{catalog.id}/people/{pet}'
                      ).get_json()['name'] == 'Rex the Second'


# ── The switch, which lives on the CATALOG now ───────────────────────────────
#
# It used to be a toggle on the Pets page with its own endpoint. It is now part
# of creating a catalog, beside the face recognition metric, and
# apply_pets_enabled() is the single writer - so these test that function and
# the rule that decides a new catalog's default, rather than an endpoint.

def test_a_catalog_that_has_never_been_asked_is_off(app_module, client, catalog):
    """
    Absent means OFF, and it has to, even though new catalogs are created ON.
    Every catalog that existed before this switch has no property; reading
    absence as "on" would start the detector and add two workflows to all of
    them at once.
    """
    assert app_module.pets_enabled_for(catalog.target_directory) is False
    s = client.get(f'/api/catalogs/{catalog.id}/pets/settings').get_json()
    assert s['enabled'] is False
    assert s['detections'] == 0 and s['pets'] == 0


def test_a_new_catalog_looks_for_animals(app_module, catalog):
    """ON by default: the household with a dog should not have to go looking."""
    assert app_module.pets_default_for_new_catalog(
        {}, False, catalog.target_directory) is True


def test_an_explicit_choice_beats_the_default(app_module, catalog):
    assert app_module.pets_default_for_new_catalog(
        {'pets_enabled': False}, False, catalog.target_directory) is False
    assert app_module.pets_default_for_new_catalog(
        {'pets_enabled': True}, False, catalog.target_directory) is True


def test_re_attaching_a_catalog_keeps_its_own_answer(app_module, catalog):
    """
    A folder carried from another machine has already decided. Same rule as
    ingest_mode: a default must not overrule what is recorded with the data -
    otherwise re-registering a catalog someone deliberately turned pets OFF for
    silently turns them back on.
    """
    td = catalog.target_directory
    assert app_module.pets_default_for_new_catalog({}, True, td) is False

    app_module.apply_pets_enabled(td, app_module.CatalogDatabase(td), True)
    assert app_module.pets_default_for_new_catalog({}, True, td) is True

    # and an explicit value still wins over the folder's own
    assert app_module.pets_default_for_new_catalog({'pets_enabled': False}, True, td) is False


def test_switching_on_creates_the_two_workflows(app_module, client, catalog):
    """
    `workflows` has no `enabled` column, so existence IS the switch - the same
    mechanism a storage route uses to create its import workflow.
    """
    td = catalog.target_directory
    created, removed = app_module.apply_pets_enabled(
        td, app_module.CatalogDatabase(td), True)
    assert sorted(created) == sorted(_pet_workflow_names()), created
    assert removed == []
    assert app_module.pets_enabled_for(td) is True

    conn = _db(catalog)
    scripts = sorted(os.path.basename(w['python_script']) for w in
                     conn.execute('SELECT python_script FROM workflows'))
    conn.close()
    assert 'pe_identify_pets.py' in scripts
    assert 'pe_cluster_pets.py' in scripts

    assert client.get(f'/api/catalogs/{catalog.id}/pets/settings'
                      ).get_json()['enabled'] is True


def test_switching_on_twice_creates_nothing_further(app_module, catalog):
    td = catalog.target_directory
    app_module.apply_pets_enabled(td, app_module.CatalogDatabase(td), True)
    created, _ = app_module.apply_pets_enabled(
        td, app_module.CatalogDatabase(td), True)
    assert created == []


def test_switching_off_removes_the_workflows_but_keeps_the_data(app_module, client, catalog):
    """
    Turning the feature off is not a request to forget the household's animals.
    """
    td = catalog.target_directory
    app_module.apply_pets_enabled(td, app_module.CatalogDatabase(td), True)
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')

    created, removed = app_module.apply_pets_enabled(
        td, app_module.CatalogDatabase(td), False)
    assert created == []
    assert sorted(removed) == sorted(_pet_workflow_names()), removed
    assert app_module.pets_enabled_for(td) is False

    conn = _db(catalog)
    n = conn.execute('SELECT COUNT(*) FROM workflows').fetchone()[0]
    conn.close()
    assert n == 0
    # the pet itself survives
    assert client.get(f'/api/catalogs/{catalog.id}/people/{pet}'
                      ).get_json()['name'] == 'Rex'


def test_there_is_no_second_door_to_this_setting(client, catalog):
    """
    The old PUT /pets/settings is gone on purpose. Two endpoints that can write
    one property are two endpoints that can disagree about it, and the switch
    now belongs to the catalog.
    """
    r = client.put(f'/api/catalogs/{catalog.id}/pets/settings',
                   json={'enabled': True})
    assert r.status_code == 405, \
        'the pets switch should only be settable through the catalog'


# ── Naming ───────────────────────────────────────────────────────────────────

def _detection(catalog, file_id=10, person_id=None, species='dog'):
    conn = _db(catalog)
    # file_name is NOT NULL. The first version of this helper omitted it and
    # INSERT OR IGNORE swallowed the constraint failure, so the JOIN below
    # found nothing and the test failed somewhere else entirely.
    conn.execute('INSERT OR IGNORE INTO file_contents (id, file_path, file_name) '
                 " VALUES (?, 'Images/a.jpg', 'a.jpg')", (file_id,))
    cur = conn.execute('INSERT INTO pet_detections '
                       '(file_id,x,y,w,h,person_id,species,confidence,'
                       ' image_width,image_height) '
                       'VALUES (?,10,10,80,80,?,?,0.9,1600,1200)',
                       (file_id, person_id, species))
    did = cur.lastrowid
    conn.commit()
    conn.close()
    return did


def test_a_detection_can_be_named_and_unnamed(client, catalog):
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    det = _detection(catalog)

    r = client.post(f'/api/catalogs/{catalog.id}/pet-detections/{det}/assign',
                    json={'person_id': pet})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['person_id'] == pet

    r = client.delete(f'/api/catalogs/{catalog.id}/pet-detections/{det}/assign')
    assert r.status_code == 200
    conn = _db(catalog)
    row = conn.execute('SELECT person_id FROM pet_detections WHERE id=?',
                       (det,)).fetchone()
    conn.close()
    assert row['person_id'] is None, 'the detection must survive being unnamed'


def test_a_pet_detection_cannot_be_assigned_to_a_person(client, catalog):
    """
    The mirror of the contamination the separate tables prevent: a human in pet
    scoring is as meaningless as a dog in face scoring.
    """
    human = _person(client, catalog, 'Izelpi')
    det = _detection(catalog)
    r = client.post(f'/api/catalogs/{catalog.id}/pet-detections/{det}/assign',
                    json={'person_id': human})
    assert r.status_code == 400
    assert 'person, not a pet' in r.get_json()['error']


# ── Groups ───────────────────────────────────────────────────────────────────

def _cluster(catalog, detection_ids, species='dog'):
    conn = _db(catalog)
    cur = conn.execute('INSERT INTO pet_clusters (centroid, member_count, '
                       ' cohesion, species) VALUES (?,?,?,?)',
                       (json.dumps([0.1] * 512), len(detection_ids), 0.12, species))
    cid = cur.lastrowid
    for d in detection_ids:
        conn.execute('INSERT INTO pet_cluster_members (cluster_id, '
                     ' pet_detection_id, distance_to_centroid) VALUES (?,?,?)',
                     (cid, d, 0.1))
    conn.commit()
    conn.close()
    return cid


def test_naming_a_group_creates_the_pet_and_names_every_member(client, catalog):
    """
    One request, and it is the whole point of the feature: naming the dog once
    turns every member detection into a link from a photograph to that pet,
    with nobody tagging anything by hand.
    """
    dets = [_detection(catalog, file_id=f) for f in (10, 11, 12)]
    cid = _cluster(catalog, dets)

    r = client.post(f'/api/catalogs/{catalog.id}/pets/clusters/{cid}/assign',
                    json={'name': 'Rex', 'category': 'Family', 'species': 'dog'})
    assert r.status_code == 200, r.get_json()
    body = r.get_json()
    assert body['detections_named'] == 3

    pet = body['person_id']
    row = client.get(f'/api/catalogs/{catalog.id}/people/{pet}').get_json()
    assert row['kind'] == 'pet' and row['category'] == 'Family'

    # and the view now links all three photographs to it
    conn = _db(catalog)
    n = conn.execute("SELECT COUNT(*) FROM subject_appearances "
                     " WHERE kind='pet' AND person_id=?", (pet,)).fetchone()[0]
    conn.close()
    assert n == 3


def test_a_group_cannot_be_named_twice(client, catalog):
    cid = _cluster(catalog, [_detection(catalog)])
    client.post(f'/api/catalogs/{catalog.id}/pets/clusters/{cid}/assign',
                json={'name': 'Rex'})
    r = client.post(f'/api/catalogs/{catalog.id}/pets/clusters/{cid}/assign',
                    json={'name': 'Mittens'})
    assert r.status_code == 409


def test_an_unnamed_group_creates_no_people_row(client, catalog):
    """A row in `people` means somebody said who this is."""
    _cluster(catalog, [_detection(catalog)])
    assert client.get(f'/api/catalogs/{catalog.id}/people?kind=all'
                      ).get_json() == []


def test_a_rejected_group_stops_being_offered(client, catalog):
    dets = [_detection(catalog, file_id=f) for f in (10, 11, 12)]
    cid = _cluster(catalog, dets)
    assert len(client.get(f'/api/catalogs/{catalog.id}/pets/clusters'
                          ).get_json()) == 1
    assert client.post(f'/api/catalogs/{catalog.id}/pets/clusters/{cid}/reject'
                       ).status_code == 200
    assert client.get(f'/api/catalogs/{catalog.id}/pets/clusters'
                      ).get_json() == []


def test_small_groups_are_not_offered(client, catalog):
    """
    A group of one is a detection, and offering it as a group to name wastes
    the attention the review page exists to spend well.
    """
    _cluster(catalog, [_detection(catalog)])              # member_count 1
    assert client.get(f'/api/catalogs/{catalog.id}/pets/clusters'
                      ).get_json() == []
    assert len(client.get(f'/api/catalogs/{catalog.id}/pets/clusters'
                          '?min_members=1').get_json()) == 1


def test_a_pets_detections_are_listed_with_crop_urls(client, catalog):
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    det = _detection(catalog, person_id=pet)
    rows = client.get(f'/api/catalogs/{catalog.id}/pets/{pet}/detections'
                      ).get_json()['detections']
    assert len(rows) == 1
    assert rows[0]['id'] == det
    # no crop row yet, so no url - the identify pass writes those
    assert 'crop_url' not in rows[0]


# ── The card picture ─────────────────────────────────────────────────────────

def _crop(catalog, detection_id, path=None):
    """A crop on disk for a detection - the row the identify pass writes."""
    path = path or f'cache/pets-ai/{detection_id}.jpg'
    conn = _db(catalog)
    src = conn.execute('SELECT file_id FROM pet_detections WHERE id = ?',
                       (detection_id,)).fetchone()[0]
    conn.execute('INSERT INTO ai_pet_contents (pet_detection_id, file_path, '
                 ' file_name, source_file_id) VALUES (?, ?, ?, ?)',
                 (detection_id, path, os.path.basename(path), src))
    conn.commit()
    conn.close()


def test_a_pet_with_no_chosen_picture_still_gets_one(client, catalog):
    """
    A card with no picture is worse than a card with a picture nobody chose, so
    the most confident crop stands in until somebody stars one. Otherwise every
    pet looks blank between being named and being curated.
    """
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    weak = _detection(catalog, file_id=10, person_id=pet)
    strong = _detection(catalog, file_id=11, person_id=pet)
    conn = _db(catalog)
    conn.execute('UPDATE pet_detections SET confidence=0.4 WHERE id=?', (weak,))
    conn.execute('UPDATE pet_detections SET confidence=0.9 WHERE id=?', (strong,))
    conn.commit()
    conn.close()
    _crop(catalog, weak)
    _crop(catalog, strong)

    pets = client.get(f'/api/catalogs/{catalog.id}/people?kind=pet').get_json()
    assert pets[0]['profile_detection_id'] == strong
    assert pets[0]['cover_crop_url'].endswith(f'/pet-detections/{strong}/crop?v=0')
    assert pets[0]['photo_count'] == 2


def test_a_detection_with_no_crop_is_never_the_cover(client, catalog):
    """
    cover_crop_url points at /pet-detections/<id>/crop, which 404s without a
    row in ai_pet_contents. A card showing a broken image is worse than one
    showing the paw.
    """
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    uncropped = _detection(catalog, file_id=10, person_id=pet)
    cropped = _detection(catalog, file_id=11, person_id=pet)
    conn = _db(catalog)
    conn.execute('UPDATE pet_detections SET confidence=0.99 WHERE id=?', (uncropped,))
    conn.execute('UPDATE pet_detections SET confidence=0.10 WHERE id=?', (cropped,))
    conn.commit()
    conn.close()
    _crop(catalog, cropped)          # only the LOW-confidence one has a crop

    pets = client.get(f'/api/catalogs/{catalog.id}/people?kind=pet').get_json()
    assert pets[0]['profile_detection_id'] == cropped, \
        'the most confident detection has no crop, so it cannot be the cover'


def test_starring_a_photograph_makes_it_the_cover(client, catalog):
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    best = _detection(catalog, file_id=10, person_id=pet)
    chosen = _detection(catalog, file_id=11, person_id=pet)
    conn = _db(catalog)
    conn.execute('UPDATE pet_detections SET confidence=0.99 WHERE id=?', (best,))
    conn.execute('UPDATE pet_detections SET confidence=0.20 WHERE id=?', (chosen,))
    conn.commit()
    conn.close()
    _crop(catalog, best)
    _crop(catalog, chosen)

    r = client.put(f'/api/catalogs/{catalog.id}/pets/{pet}/profile-photo',
                   json={'detection_id': chosen})
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['profile_photo_file_id'] == 11

    pets = client.get(f'/api/catalogs/{catalog.id}/people?kind=pet').get_json()
    assert pets[0]['profile_detection_id'] == chosen, \
        'a human choice must beat the most confident guess'


def test_the_star_stores_the_FILE_not_the_detection(client, catalog):
    """
    Why this matters: a detection run deletes and re-inserts a file's automatic
    boxes, so a stored detection id would dangle afterwards and the card would
    silently lose its picture. The file id is permanent, and the detection is
    looked up from (file, pet) when the card is drawn.
    """
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    det = _detection(catalog, file_id=10, person_id=pet)
    _crop(catalog, det)
    client.put(f'/api/catalogs/{catalog.id}/pets/{pet}/profile-photo',
               json={'detection_id': det})

    conn = _db(catalog)
    stored = conn.execute('SELECT profile_photo_file_id FROM people WHERE id=?',
                          (pet,)).fetchone()[0]
    # the detection is re-made with a NEW id, as a detection run would
    conn.execute('DELETE FROM pet_detections WHERE id=?', (det,))
    cur = conn.execute('INSERT INTO pet_detections '
                       '(file_id,x,y,w,h,person_id,species,confidence,'
                       ' image_width,image_height) '
                       "VALUES (10,10,10,80,80,?,'dog',0.9,1600,1200)", (pet,))
    new_det = cur.lastrowid
    conn.execute('INSERT INTO ai_pet_contents (pet_detection_id, file_path, '
                 ' file_name, source_file_id) '
                 " VALUES (?, 'cache/pets-ai/y.jpg', 'y.jpg', 10)", (new_det,))
    conn.commit()
    conn.close()

    assert stored == 10, 'the FILE should have been stored, not the detection'
    pets = client.get(f'/api/catalogs/{catalog.id}/people?kind=pet').get_json()
    assert pets[0]['profile_detection_id'] == new_det, \
        'the card should follow the file to its new detection, not go blank'


def test_the_gallery_says_which_photograph_is_starred(client, catalog):
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    a = _detection(catalog, file_id=10, person_id=pet)
    b = _detection(catalog, file_id=11, person_id=pet)
    _crop(catalog, a)
    _crop(catalog, b)
    client.put(f'/api/catalogs/{catalog.id}/pets/{pet}/profile-photo',
               json={'detection_id': b})

    rows = client.get(f'/api/catalogs/{catalog.id}/pets/{pet}/detections').get_json()['detections']
    flags = {r['id']: r['is_profile'] for r in rows}
    assert flags == {a: False, b: True}


def test_another_pets_photograph_cannot_be_starred(client, catalog):
    """
    Pointing at a detection that is not this animal's is the caller being
    wrong, and quietly succeeding would put the wrong dog on the card.
    """
    rex = _person(client, catalog, 'Rex', kind='pet', species='dog')
    bella = _person(client, catalog, 'Bella', kind='pet', species='cat')
    hers = _detection(catalog, file_id=10, person_id=bella)

    r = client.put(f'/api/catalogs/{catalog.id}/pets/{rex}/profile-photo',
                   json={'detection_id': hers})
    assert r.status_code == 404, r.get_json()


def test_a_person_has_no_pet_cover(client, catalog):
    """
    The cover resolution runs only for pets. A person's profile photo is a
    whole file plus a face box the frontend crops to, and handing the People
    page a pet-shaped field would be a second, conflicting answer.
    """
    _person(client, catalog, 'Izelpi', category='Family')
    people = client.get(f'/api/catalogs/{catalog.id}/people').get_json()
    assert 'cover_crop_url' not in people[0]


# ── Animals in one photograph (the viewer's overlay) ─────────────────────────

def test_a_photographs_animals_come_back_in_the_detection_space(client, catalog):
    """
    The viewer draws these on the SAME canvas as the faces, so they have to
    arrive in the same coordinate space with the same field names: x/y/w/h in
    image_width x image_height, which is the PREPARED image and not the
    original. Dropping either dimension makes the boxes unscalable and they
    land somewhere arbitrary - the chain documentation/fixes/ has nine files
    about.
    """
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    det = _detection(catalog, file_id=10, person_id=pet)
    _crop(catalog, det)

    r = client.get(f'/api/catalogs/{catalog.id}/images/10/pet-detections')
    assert r.status_code == 200, r.get_json()
    pets = r.get_json()['pets']
    assert len(pets) == 1
    got = pets[0]
    for field in ('x', 'y', 'w', 'h', 'image_width', 'image_height',
                  'species', 'confidence', 'source'):
        assert field in got, f'{field} is missing, so the box cannot be drawn'
    assert (got['image_width'], got['image_height']) == (1600, 1200)
    assert got['person_name'] == 'Rex'
    assert got['crop_url'].endswith(f'/pet-detections/{det}/crop?v=0')


def test_an_unnamed_animal_is_still_reported(client, catalog):
    """The overlay shows what is THERE, named or not - that is the point of it."""
    det = _detection(catalog, file_id=10, person_id=None)
    pets = client.get(f'/api/catalogs/{catalog.id}/images/10/pet-detections'
                      ).get_json()['pets']
    assert [p['id'] for p in pets] == [det]
    assert pets[0]['person_id'] is None
    assert pets[0]['person_name'] is None


def test_a_photograph_with_no_animals_returns_an_empty_list(client, catalog):
    """
    Not a 404. The viewer asks for every photograph it shows, and most
    photographs have no animals in them; an error for the normal case would
    fill the console and teach everyone to ignore it.
    """
    r = client.get(f'/api/catalogs/{catalog.id}/images/999/pet-detections')
    assert r.status_code == 200
    assert r.get_json()['pets'] == []


def test_only_this_photographs_animals_come_back(client, catalog):
    pet = _person(client, catalog, 'Rex', kind='pet', species='dog')
    here = _detection(catalog, file_id=10, person_id=pet)
    _detection(catalog, file_id=11, person_id=pet)

    pets = client.get(f'/api/catalogs/{catalog.id}/images/10/pet-detections'
                      ).get_json()['pets']
    assert [p['id'] for p in pets] == [here]


def test_a_named_animal_reports_what_it_IS_not_what_was_guessed(client, catalog):
    """
    The detector's species is wrong about a quarter of the time, so once
    somebody has named an animal the viewer must stop quoting the guess at
    them. The real case: a photograph of a dog the model scored as `cat` at
    0.56, under a pet the user had already named and recorded as a dog - the
    page was arguing with its own user.

    Both are returned; the caller picks. `species` stays because an UNNAMED
    box has nothing else to show.
    """
    pet = _person(client, catalog, 'Luna', kind='pet', species='dog')
    _detection(catalog, file_id=10, person_id=pet, species='cat')

    got = client.get(f'/api/catalogs/{catalog.id}/images/10/pet-detections'
                     ).get_json()['pets'][0]
    assert got['species'] == 'cat', 'the detector said what it said'
    assert got['person_species'] == 'dog', 'and the user overruled it'


def test_an_unnamed_animal_has_no_confirmed_species(client, catalog):
    """Nothing to overrule the guess with, so the caller falls back to it."""
    _detection(catalog, file_id=10, person_id=None, species='cat')
    got = client.get(f'/api/catalogs/{catalog.id}/images/10/pet-detections'
                     ).get_json()['pets'][0]
    assert got['person_species'] is None
    assert got['species'] == 'cat'


def test_the_pets_own_gallery_agrees_with_the_viewer(client, catalog):
    """
    Two surfaces showing the same detections must not disagree about the
    animal's species.
    """
    pet = _person(client, catalog, 'Luna', kind='pet', species='dog')
    _detection(catalog, file_id=10, person_id=pet, species='cat')
    rows = client.get(f'/api/catalogs/{catalog.id}/pets/{pet}/detections').get_json()['detections']
    assert rows[0]['person_species'] == 'dog'
