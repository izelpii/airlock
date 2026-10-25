#!/usr/bin/env python3
"""
The pet detector's ride-along inside Auto Detect Faces.

Section 5 of documentation/specs/pet-recognition-spec.md. Everything downstream
of this - clustering, scoring, the three pages - has its own tests and none of
it matters if the tap never fills the pipe.

Four things are asserted here, and three of them are about what must NOT happen:

  1. OFF COSTS ZERO. Not nearly zero. A catalog with pets switched off must
     come out of a detection run byte-identical in schema: no pet tables, no
     pets_scanned_at column, no HTTP call attempted.
  2. THE TWO WATERMARKS ARE INDEPENDENT. Every file in an existing catalog is
     already face-scanned and none is pet-scanned. Sharing one column would
     mean the pet pass never looks at any of them, and the feature would
     appear to work while finding nothing.
  3. A PET FAILURE NEVER TAKES THE FACE PASS WITH IT, and never marks a file
     examined - so fixing the detector and re-running loses no work.
  4. A HAND-NAMED BOX SURVIVES RE-DETECTION, exactly as a Manual face does.
"""

import io
import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workflows'))

import ai_auto_detect_faces_workflow as wf   # noqa: E402
import workflow_triggers as trig             # noqa: E402
from pet_schema import ensure_pet_schema     # noqa: E402


# ── Fixtures ─────────────────────────────────────────────────────────────────

def _catalog(tmp_path, pets_enabled=None):
    """
    A catalog shaped like a real one: files already face-scanned, as every
    existing catalog's are.
    """
    path = str(tmp_path / 'catalog.db')
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE file_contents (
            id INTEGER PRIMARY KEY, file_name TEXT NOT NULL, file_path TEXT,
            face_detection_scanned_at TIMESTAMP, removed_at TIMESTAMP);
        CREATE TABLE people (id INTEGER PRIMARY KEY, name TEXT,
                             category TEXT DEFAULT 'Other',
                             date_of_birth TEXT, profile_photo_file_id INTEGER);
        CREATE TABLE face_detections (id INTEGER PRIMARY KEY, file_id INTEGER,
                                      person_id INTEGER, source TEXT,
                                      x INTEGER, y INTEGER, w INTEGER, h INTEGER);
        CREATE TABLE face_contents (id INTEGER PRIMARY KEY, embedding TEXT);
        CREATE TABLE ai_face_contents (id INTEGER PRIMARY KEY,
                                       face_detection_id INTEGER, embedding TEXT);
        CREATE TABLE catalog_properties (key TEXT PRIMARY KEY, value TEXT);
    """)
    if pets_enabled is not None:
        conn.execute('INSERT INTO catalog_properties (key, value) VALUES (?,?)',
                     ('pets_enabled', pets_enabled))
    conn.commit()
    conn.close()
    return path


def _schema_fingerprint(path):
    conn = sqlite3.connect(path)
    try:
        return sorted(
            conn.execute("SELECT type, name, COALESCE(sql,'') FROM sqlite_master"))
    finally:
        conn.close()


@pytest.fixture(autouse=True)
def _reset_module_state():
    """
    PETS_ENABLED and the failure counters are module globals, resolved once per
    run by execute(). Tests must not inherit each other's.
    """
    wf.PETS_ENABLED = False
    wf._pets_consecutive_failures = 0
    wf._pets_disabled_midrun = False
    yield
    wf.PETS_ENABLED = False
    wf._pets_consecutive_failures = 0
    wf._pets_disabled_midrun = False


# ── 1. Off costs zero ────────────────────────────────────────────────────────

def test_a_catalog_with_pets_off_grows_no_pet_schema(tmp_path):
    """
    The promise in execute()'s own comment. Five tables, two columns and a view
    is not a small thing to add to somebody who never asked for animals.
    """
    path = _catalog(tmp_path)          # no pets_enabled property at all
    before = _schema_fingerprint(path)

    import models
    assert models.get_catalog_property(path, 'pets_enabled') != '1'

    assert _schema_fingerprint(path) == before, \
        'reading the switch must not itself create anything'
    names = {n for _t, n, _s in before}
    assert 'pet_detections' not in names
    assert 'subject_appearances' not in names


def _drive(tmp_path, monkeypatch, path, posts, deepface_payload=None):
    """
    Run detect_faces_for_image against a real file with both detectors faked.

    Everything up to prepare_detection_jpeg is real - that is where the shared
    decode lives, and it is the whole reason the ride-along is in this workflow
    - so these tests go through the actual branch rather than restating it.
    """
    img = tmp_path / 'a.jpg'
    img.write_bytes(b'not really a jpeg')
    monkeypatch.setattr(wf, 'get_alternative_version_for_detection',
                        lambda *a, **k: None)
    monkeypatch.setattr(wf, 'prepare_detection_jpeg',
                        lambda p: (b'prepared', 1200, 800))

    def _post(url, *a, **k):
        posts.append(url)
        if 'detect' in url and 'pets' in url:
            return _Resp({'detections': [
                {'x': 1, 'y': 2, 'w': 3, 'h': 4,
                 'confidence': 0.7, 'species': 'dog'}]})
        return _Resp(deepface_payload or {'success': True, 'faces': []})
    monkeypatch.setattr(wf.requests, 'post', _post)
    return str(img)


def test_with_pets_off_nothing_asks_the_detector_anything(tmp_path, monkeypatch):
    """
    Not "it posts and ignores the answer": it must not post. The pet service is
    not even started on a catalog that does not want it, and a 60-second
    connect timeout per image would turn a working face pass into a crawl.
    """
    path = _catalog(tmp_path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO file_contents (id, file_name) VALUES (1, 'a.jpg')")
    conn.commit()
    conn.close()
    before = _schema_fingerprint(path)

    posts = []
    img = _drive(tmp_path, monkeypatch, path, posts)
    result = wf.detect_faces_for_image(path, 1, img, 'a.jpg', '.jpg',
                                       target_dir=str(tmp_path),
                                       do_faces=True, do_pets=False)

    assert result['success'] is True
    assert result['pets_count'] == 0
    assert not [u for u in posts if 'pets' in u], \
        f'the pet detector was asked anyway: {posts}'
    assert _schema_fingerprint(path) == before, \
        'a face pass with pets off must leave the schema untouched'


def test_a_pets_only_visit_never_asks_the_face_detector(tmp_path, monkeypatch):
    """
    The point of the second watermark. A file face-scanned before pets existed
    comes back for the animals half ONLY - re-running VGG-Face over four
    hundred thousand already-scanned files would cost days for no new fact.
    """
    path = _catalog(tmp_path, pets_enabled='1')
    ensure_pet_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO file_contents (id, file_name, "
                 " face_detection_scanned_at) VALUES (1, 'a.jpg', '2020-01-01 00:00:00')")
    conn.commit()
    conn.close()

    posts = []
    img = _drive(tmp_path, monkeypatch, path, posts)
    result = wf.detect_faces_for_image(path, 1, img, 'a.jpg', '.jpg',
                                       target_dir=str(tmp_path),
                                       do_faces=False, do_pets=True)

    assert result['faces_skipped'] is True
    assert result['faces_count'] == 0
    assert result['pets_count'] == 1
    assert not [u for u in posts if 'deepface' in u], \
        f'the face detector was asked for an already-scanned file: {posts}'

    conn = sqlite3.connect(path)
    face_wm, pet_wm = conn.execute(
        'SELECT face_detection_scanned_at, pets_scanned_at FROM file_contents '
        ' WHERE id = 1').fetchone()
    conn.close()
    assert face_wm == '2020-01-01 00:00:00', 'the face watermark must not move'
    assert pet_wm is not None, 'the pet watermark must'


def test_the_watermark_query_is_safe_on_a_catalog_without_the_column(tmp_path):
    """
    An old catalog has no pets_scanned_at. Asking anyway must return "none
    scanned" rather than raising - this runs before ensure_pet_schema on the
    very first pets-on run.
    """
    path = _catalog(tmp_path)
    assert wf.get_files_already_pet_scanned(path) == set()


# ── 2. The two watermarks are independent ────────────────────────────────────

def test_a_face_scanned_file_is_still_outstanding_for_pets(tmp_path):
    """
    THE bug this feature could most easily have shipped with. Every file in
    second-catalog is already face-scanned; if the pet pass reused that column
    it would visit nothing, report success, and find no animals anywhere.
    """
    path = _catalog(tmp_path, pets_enabled='1')
    ensure_pet_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO file_contents (id, file_name, "
                 " face_detection_scanned_at) VALUES (1, 'a.jpg', CURRENT_TIMESTAMP)")
    conn.commit()
    conn.close()

    assert wf.get_files_already_scanned(path) == {1}, 'faces: done'
    assert wf.get_files_already_pet_scanned(path) == set(), 'pets: not looked at'

    # The filter execute() applies.
    files_already_scanned = wf.get_files_already_scanned(path)
    files_already_pet_scanned = wf.get_files_already_pet_scanned(path)
    outstanding = [fid for fid in (1,)
                   if fid not in files_already_scanned
                   or fid not in files_already_pet_scanned]
    assert outstanding == [1]


# ── 3. The detector's answer, stored ─────────────────────────────────────────

class _Resp:
    def __init__(self, payload, status=200):
        self._payload, self.status_code, self.text = payload, status, str(payload)

    def json(self):
        return self._payload


def _pets_on(tmp_path, monkeypatch, payload, status=200):
    path = _catalog(tmp_path, pets_enabled='1')
    ensure_pet_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO file_contents (id, file_name) VALUES (1, 'a.jpg')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(wf.requests, 'post',
                        lambda *a, **k: _Resp(payload, status))
    return path


def test_boxes_are_stored_in_the_prepared_images_coordinate_space(tmp_path, monkeypatch):
    """
    The one chain documentation/fixes/ has nine files about. The detector sees
    the downscaled jpeg, so its boxes are in THOSE pixels, and the crop
    rescales from image_width/image_height stored beside them. Storing the
    original's dimensions here puts every pet crop in the wrong place.
    """
    path = _pets_on(tmp_path, monkeypatch, {'detections': [
        {'x': 10, 'y': 20, 'w': 30, 'h': 40, 'confidence': 0.91, 'species': 'dog'}]})

    n = wf.detect_pets_in_prepared(path, 1, b'jpegbytes', 1200, 800, 'a.jpg')
    assert n == 1

    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    row = conn.execute('SELECT * FROM pet_detections').fetchone()
    conn.close()
    assert (row['x'], row['y'], row['w'], row['h']) == (10, 20, 30, 40)
    assert (row['image_width'], row['image_height']) == (1200, 800)
    assert row['species'] == 'dog'
    assert row['source'] == 'Automatic'
    assert abs(row['confidence'] - 0.91) < 1e-6


def test_the_watermark_moves_in_the_same_transaction_as_the_boxes(tmp_path, monkeypatch):
    path = _pets_on(tmp_path, monkeypatch, {'detections': [
        {'x': 1, 'y': 1, 'w': 9, 'h': 9, 'confidence': 0.5, 'species': 'cat'}]})
    wf.detect_pets_in_prepared(path, 1, b'x', 100, 100, 'a.jpg')

    conn = sqlite3.connect(path)
    scanned = conn.execute('SELECT pets_scanned_at FROM file_contents '
                           ' WHERE id = 1').fetchone()[0]
    conn.close()
    assert scanned is not None


def test_a_file_with_no_animals_is_still_marked_examined(tmp_path, monkeypatch):
    """
    Otherwise every landscape in the catalog is re-sent to the detector on
    every run, for ever. "Looked at and found nothing" is an answer.
    """
    path = _pets_on(tmp_path, monkeypatch, {'detections': []})
    assert wf.detect_pets_in_prepared(path, 1, b'x', 100, 100, 'a.jpg') == 0

    conn = sqlite3.connect(path)
    assert conn.execute('SELECT pets_scanned_at FROM file_contents '
                        ' WHERE id = 1').fetchone()[0] is not None
    assert conn.execute('SELECT COUNT(*) FROM pet_detections').fetchone()[0] == 0
    conn.close()


# ── 4. A judgement survives re-detection ─────────────────────────────────────

def test_re_detection_replaces_automatic_boxes_and_keeps_manual_ones(tmp_path, monkeypatch):
    """
    Same rule as a Manual face detection. Somebody drew that box or named it;
    a second opinion from the detector does not get to delete it.
    """
    path = _pets_on(tmp_path, monkeypatch, {'detections': [
        {'x': 5, 'y': 5, 'w': 5, 'h': 5, 'confidence': 0.8, 'species': 'dog'}]})
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO people (id, name) VALUES (1, 'Rex')")
    conn.execute("INSERT INTO pet_detections (file_id,x,y,w,h,species,source,person_id)"
                 " VALUES (1,100,100,50,50,'dog','Manual',1)")
    conn.execute("INSERT INTO pet_detections (file_id,x,y,w,h,species,source)"
                 " VALUES (1,200,200,50,50,'cat','Automatic')")
    conn.commit()
    conn.close()

    wf.detect_pets_in_prepared(path, 1, b'x', 100, 100, 'a.jpg')

    conn = sqlite3.connect(path)
    rows = conn.execute('SELECT source, x, person_id FROM pet_detections '
                        ' ORDER BY source, x').fetchall()
    conn.close()
    assert rows == [('Automatic', 5, None), ('Manual', 100, 1)], \
        'the old Automatic box goes, the Manual one stays named'


def test_a_named_detection_survives_re_detection(tmp_path, monkeypatch):
    """
    THE one that silently emptied the Pets page.

    Naming a detection by hand sets source='Manual', but naming a GROUP - which
    is how pets are actually named, one click for all of them - writes
    person_id and leaves source alone. The delete was `source = 'Automatic'`,
    so every cluster-named detection was deleted by the next detection run: the
    pet kept its `people` row and lost every photograph it had been found in.
    """
    path = _pets_on(tmp_path, monkeypatch, {'detections': [
        {'x': 500, 'y': 500, 'w': 40, 'h': 40, 'confidence': 0.9, 'species': 'dog'}]})
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO people (id, name, kind) VALUES (1, 'Rex', 'pet')")
    # exactly what assign_pet_cluster writes: person_id set, source untouched
    conn.execute("INSERT INTO pet_detections (id,file_id,x,y,w,h,species,source,person_id)"
                 " VALUES (99,1,10,10,60,60,'dog','Automatic',1)")
    # and an unnamed automatic one, which SHOULD be replaced
    conn.execute("INSERT INTO pet_detections (file_id,x,y,w,h,species,source)"
                 " VALUES (1,300,300,20,20,'cat','Automatic')")
    conn.commit()
    conn.close()

    wf.detect_pets_in_prepared(path, 1, b'x', 1000, 1000, 'a.jpg')

    conn = sqlite3.connect(path)
    rows = conn.execute('SELECT id, person_id, source FROM pet_detections '
                        ' ORDER BY id').fetchall()
    conn.close()
    ids = [r[0] for r in rows]
    assert 99 in ids, 'the named detection was deleted - the pet just lost a photograph'
    assert (99, 1, 'Automatic') in rows
    assert len(rows) == 2, f'expected the named one plus the new one, got {rows}'


def test_a_new_box_over_a_named_one_is_not_inserted_twice(tmp_path, monkeypatch):
    """
    The named box is kept, so the detector finding the same animal again must
    not add an unnamed duplicate on top of it - the duplicate the face path
    documents at its own overlap check.
    """
    path = _pets_on(tmp_path, monkeypatch, {'detections': [
        {'x': 12, 'y': 12, 'w': 58, 'h': 58, 'confidence': 0.9, 'species': 'dog'}]})
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO people (id, name, kind) VALUES (1, 'Rex', 'pet')")
    conn.execute("INSERT INTO pet_detections (id,file_id,x,y,w,h,species,source,person_id)"
                 " VALUES (99,1,10,10,60,60,'dog','Automatic',1)")
    conn.commit()
    conn.close()

    wf.detect_pets_in_prepared(path, 1, b'x', 1000, 1000, 'a.jpg')

    conn = sqlite3.connect(path)
    rows = conn.execute('SELECT id, person_id FROM pet_detections').fetchall()
    conn.close()
    assert rows == [(99, 1)], \
        f'the same animal was inserted again beside its named box: {rows}'


def test_an_unnamed_automatic_box_is_still_replaced(tmp_path, monkeypatch):
    """
    The preservation must not turn into "never delete anything": a stale
    automatic box nobody named is exactly what re-detection is for.
    """
    path = _pets_on(tmp_path, monkeypatch, {'detections': [
        {'x': 1, 'y': 1, 'w': 9, 'h': 9, 'confidence': 0.6, 'species': 'cat'}]})
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO pet_detections (id,file_id,x,y,w,h,species,source)"
                 " VALUES (77,1,800,800,50,50,'dog','Automatic')")
    conn.commit()
    conn.close()

    wf.detect_pets_in_prepared(path, 1, b'x', 1000, 1000, 'a.jpg')

    conn = sqlite3.connect(path)
    ids = [r[0] for r in conn.execute('SELECT id FROM pet_detections')]
    conn.close()
    assert 77 not in ids, 'a stale unnamed box should be replaced'
    assert len(ids) == 1


# ── 5. A pet failure is contained ────────────────────────────────────────────

def test_a_failing_detector_marks_nothing_so_a_re_run_loses_no_work(tmp_path, monkeypatch):
    path = _pets_on(tmp_path, monkeypatch, {'error': 'boom'}, status=503)
    assert wf.detect_pets_in_prepared(path, 1, b'x', 100, 100, 'a.jpg') == 0

    conn = sqlite3.connect(path)
    assert conn.execute('SELECT pets_scanned_at FROM file_contents '
                        ' WHERE id = 1').fetchone()[0] is None, \
        'a file the detector never saw must not be marked examined'
    conn.close()


def test_a_pet_failure_never_raises(tmp_path, monkeypatch):
    """
    Faces are what people already depend on. A pet detector that is down, or
    returning nonsense, must not fail the run that is also detecting faces.
    """
    path = _catalog(tmp_path, pets_enabled='1')
    ensure_pet_schema(path)

    def _explode(*a, **k):
        raise ConnectionError('pets.airlock.lan: no route to host')
    monkeypatch.setattr(wf.requests, 'post', _explode)

    assert wf.detect_pets_in_prepared(path, 1, b'x', 100, 100, 'a.jpg') == 0


def test_repeated_failures_switch_the_ride_along_off_for_the_rest_of_the_run(
        tmp_path, monkeypatch):
    """
    Ten per-image 60-second timeouts is one thing; forty thousand is a face
    pass that never finishes. Past the limit it stops asking and says so once.
    """
    path = _catalog(tmp_path, pets_enabled='1')
    ensure_pet_schema(path)
    attempts = []

    def _explode(*a, **k):
        attempts.append(1)
        raise ConnectionError('down')
    monkeypatch.setattr(wf.requests, 'post', _explode)

    for i in range(wf.PETS_MAX_CONSECUTIVE_FAILURES + 5):
        wf.detect_pets_in_prepared(path, i, b'x', 100, 100, 'a.jpg')

    assert wf._pets_disabled_midrun is True
    assert len(attempts) == wf.PETS_MAX_CONSECUTIVE_FAILURES, \
        'it must stop asking, not keep timing out quietly'


def test_one_success_resets_the_failure_run(tmp_path, monkeypatch):
    """
    "Consecutive" has to mean consecutive. A restart part-way through a long
    pass must not count towards a limit set for a detector that is really down.
    """
    path = _catalog(tmp_path, pets_enabled='1')
    ensure_pet_schema(path)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO file_contents (id, file_name) VALUES (1, 'a.jpg')")
    conn.commit()
    conn.close()

    state = {'fail': True}

    def _flaky(*a, **k):
        if state['fail']:
            raise ConnectionError('down')
        return _Resp({'detections': []})
    monkeypatch.setattr(wf.requests, 'post', _flaky)

    for _ in range(wf.PETS_MAX_CONSECUTIVE_FAILURES - 1):
        wf.detect_pets_in_prepared(path, 1, b'x', 100, 100, 'a.jpg')
    assert wf._pets_disabled_midrun is False

    state['fail'] = False
    wf.detect_pets_in_prepared(path, 1, b'x', 100, 100, 'a.jpg')
    assert wf._pets_consecutive_failures == 0

    state['fail'] = True
    for _ in range(wf.PETS_MAX_CONSECUTIVE_FAILURES - 1):
        wf.detect_pets_in_prepared(path, 1, b'x', 100, 100, 'a.jpg')
    assert wf._pets_disabled_midrun is False, \
        'the run of failures restarted, so the limit has not been reached'


# ── 6. The chain ─────────────────────────────────────────────────────────────

def test_auto_detect_faces_raises_both_chains():
    """
    One pass now finds two different things, and they invalidate different
    downstream work. Collapsing them into one trigger means either the face
    chain running for a dog or the pet chain never running at all.
    """
    assert trig.triggers_for_completed_workflow(
        'ai_auto_detect_faces_workflow.py') == ('faces_detected', 'pets_detected')


def test_measuring_pets_queues_the_grouping_pass():
    """
    Without this a newly detected animal is embedded and then sits unclustered
    until somebody runs Cluster Pets by hand.
    """
    assert trig.triggers_for_completed_workflow('pe_identify_pets.py') == \
        ('pets_identified',)
    assert 'pe_cluster_pets.py' in trig.TRIGGERS['pets_identified']


def test_the_grouping_pass_raises_nothing_so_the_chain_terminates():
    assert trig.triggers_for_completed_workflow('pe_cluster_pets.py') == ()


def test_every_pet_trigger_is_actually_raised_somewhere():
    """
    A named trigger nothing raises is the silent half-finish this whole map
    exists to prevent, and all three pet triggers were in that state.

      pets_detected   <- Auto Detect Faces completing (above)
      pets_identified <- Identify Pets completing (above)
      pets_changed    <- the HTTP path, when a human names or unnames an animal
    """
    raised_by_a_workflow = set()
    for script in ('ai_auto_detect_faces_workflow.py', 'pe_identify_pets.py',
                   'pe_cluster_pets.py'):
        raised_by_a_workflow.update(trig.triggers_for_completed_workflow(script))
    assert {'pets_detected', 'pets_identified'} <= raised_by_a_workflow

    here = os.path.dirname(os.path.abspath(__file__))
    app_src = io.open(os.path.join(here, 'app.py'), encoding='utf-8').read()
    assert "'pets_changed'" in app_src, \
        'naming a pet changes the reference set; if nothing raises this, every ' \
        'other photograph of that animal stays unidentified for ever'


def test_naming_a_pet_waits_out_a_quiet_period_but_detection_does_not():
    """
    Same split as locations: a person working through a list of groups does it
    in bursts and wants the delay; a workflow finishing happens once.
    """
    assert trig.quiet_seconds('pets_changed') > 0
    assert trig.quiet_seconds('pets_detected') == 0
    assert trig.quiet_seconds('pets_identified') == 0


# ── A file that cannot be read is not a detector that is down ───────────────

def test_an_unreadable_file_does_not_look_like_a_dead_detector(tmp_path, monkeypatch):
    """
    2026-10-09: ten undecodable BMPs in a row aborted a detection pass with
    "the detector is almost certainly down or crash-looping" - stated with
    confidence, about a service that was answering perfectly. The remaining
    3,715 files then could not be completed at all, because the abort fired in
    the same place on every retry.
    """
    path = _catalog(tmp_path, pets_enabled='1')
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO file_contents (id, file_name) VALUES (1, 'a.bmp')")
    conn.commit()
    conn.close()

    img = tmp_path / 'a.bmp'
    img.write_bytes(b'not really a bitmap')
    monkeypatch.setattr(wf, 'get_alternative_version_for_detection',
                        lambda *a, **k: None)

    def _cannot_decode(p):
        raise OSError('Unsupported BMP pixel depth (0)')
    monkeypatch.setattr(wf, 'prepare_detection_jpeg', _cannot_decode)

    posted = []
    monkeypatch.setattr(wf.requests, 'post',
                        lambda *a, **k: posted.append(a))

    result = wf.detect_faces_for_image(path, 1, str(img), 'a.bmp', '.bmp',
                                       target_dir=str(tmp_path))

    assert result['success'] is False
    assert result.get('unreadable') is True, \
        'a decode failure must be distinguishable from a detector failure'
    assert 'Unsupported BMP' in result['error']
    assert posted == [], 'nothing should have been sent to a detector'


def test_a_detector_failure_is_still_a_detector_failure(tmp_path, monkeypatch):
    """
    The guard must not turn into "nothing is ever the detector's fault": a
    service that is down still has to abort the run rather than mark four
    hundred thousand files as attempted.
    """
    path = _catalog(tmp_path, pets_enabled='1')
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO file_contents (id, file_name) VALUES (1, 'a.jpg')")
    conn.commit()
    conn.close()
    img = tmp_path / 'a.jpg'
    img.write_bytes(b'jpeg')

    monkeypatch.setattr(wf, 'get_alternative_version_for_detection',
                        lambda *a, **k: None)
    monkeypatch.setattr(wf, 'prepare_detection_jpeg',
                        lambda p: (b'prepared', 1200, 800))

    def _down(*a, **k):
        raise ConnectionError('deepface.airlock.lan: no route to host')
    monkeypatch.setattr(wf.requests, 'post', _down)

    result = wf.detect_faces_for_image(path, 1, str(img), 'a.jpg', '.jpg',
                                       target_dir=str(tmp_path))
    assert result['success'] is False
    assert not result.get('unreadable'), \
        'a dead detector was misreported as an unreadable file'
