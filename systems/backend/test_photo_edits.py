"""
Basic photo editing: a turn or flip kept in the database, applied to every
derivative and crop, never to the original.

    python -m pytest test_photo_edits.py

See documentation/specs/basic-photo-editing.md section 9. Uses the isolated
app from test_photo_dates_batch.py: backend/database/ is the running
container's live mount and must not be opened from here.
"""

import hashlib
import os
import sqlite3
import threading
import time

import pytest
from PIL import Image

from test_photo_dates_batch import (  # noqa: F401  (fixtures)
    app_module, catalog, client,
)
from workflows.lib import orientation as orient

RED, GREEN, WHITE = (255, 0, 0), (0, 255, 0), (255, 255, 255)
OPS = {'rotate_right': Image.Transpose.ROTATE_270, 'rotate_left': Image.Transpose.ROTATE_90,
       'flip_horizontal': Image.Transpose.FLIP_LEFT_RIGHT,
       'flip_vertical': Image.Transpose.FLIP_TOP_BOTTOM}


# ─── The arithmetic (spec 4.1) ───────────────────────────────────────────────

def _marked(w=4, h=2):
    img = Image.new('RGB', (w, h), WHITE)
    img.putpixel((0, 0), RED)
    img.putpixel((w - 1, 0), GREEN)
    return img


def _same(a, b):
    return a.size == b.size and list(a.getdata()) == list(b.getdata())


def test_each_tool_matches_the_pixels_from_every_state():
    img = _marked()
    for v in range(1, 9):
        for tool, op in OPS.items():
            assert _same(orient.apply_user(img, orient.compose(v, tool)),
                         orient.apply_user(img, v).transpose(op)), (v, tool)


def test_group_properties():
    for v in range(1, 9):
        r = v
        for _ in range(4):
            r = orient.compose(r, 'rotate_right')
        assert r == v
        for flip in ('flip_horizontal', 'flip_vertical'):
            assert orient.compose(orient.compose(v, flip), flip) == v
    assert orient.compose(orient.compose(1, 'flip_horizontal'), 'flip_vertical') == 3


def test_delta_takes_one_edit_to_another():
    img = _marked()
    for a in range(1, 9):
        for b in range(1, 9):
            d = orient.delta(a, b)
            assert orient.then(a, d) == b
            assert _same(orient.apply_user(orient.apply_user(img, a), d),
                         orient.apply_user(img, b))


def test_boxes_follow_the_pixels_and_round_trip():
    W, H = 40, 20
    x, y, w, h = 3, 2, 5, 4
    img = Image.new('L', (W, H), 0)
    img.paste(255, (x, y, x + w, y + h))
    for d in range(1, 9):
        x2, y2, w2, h2, W2, H2 = orient.transform_box(x, y, w, h, W, H, d)
        turned = orient.apply_user(img, d)
        assert turned.size == (W2, H2)
        assert turned.getbbox() == (x2, y2, x2 + w2, y2 + h2), d
        back = orient.transform_box(x2, y2, w2, h2, W2, H2, orient.delta(d, 1))
        assert back == (x, y, w, h, W, H)


# ─── The feature, end to end ─────────────────────────────────────────────────

def _db(catalog):  # noqa: F811
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    return conn


def _sha(path):
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest()


@pytest.fixture
def photo(catalog):  # noqa: F811
    """A 400x200 photo with a red top-left corner, rendered as an import would."""
    from workflows.lib.render_once import render_image_once
    rel = os.path.join('Images', '2026', '10', '02', '20261002235026-abc-p.jpg')
    original = os.path.join(catalog.target_directory, rel)
    os.makedirs(os.path.dirname(original), exist_ok=True)
    img = Image.new('RGB', (400, 200), WHITE)
    img.paste(RED, (0, 0, 40, 40))
    img.save(original, 'JPEG', quality=95)

    conn = _db(catalog)
    fid = conn.execute(
        "INSERT INTO file_contents (file_path, file_name, file_size, file_type, sha256_hash, "
        "master_datetime) VALUES (?, ?, ?, '.jpg', ?, '2026-10-02T23:50:26')",
        (rel, os.path.basename(rel), os.path.getsize(original), _sha(original))).lastrowid
    result = render_image_once(
        {'id': fid, 'file_id': fid, 'target_path': original,
         'target_dir': catalog.target_directory, 'creation_ts': '20261002235026',
         'derivative_stem': None, 'existing': {}, 'rungs': None},
        lambda ts: None)
    assert result['ok'], result
    conn.executemany(
        'INSERT INTO alternative_versions (original_file_id, alternative_file_path, '
        'alternative_file_name, alternative_file_size, characteristic, width, height, rung) '
        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', result['rows'])

    # A tagged face in the right half and its crop.
    conn.execute("INSERT INTO people (id, name) VALUES (7, 'Ada')")
    det = conn.execute(
        "INSERT INTO face_detections (file_id, x, y, w, h, confidence, person_id, source, "
        "image_width, image_height) VALUES (?, 300, 50, 40, 60, 0.9, 7, 'Manual', 400, 200)",
        (fid,)).lastrowid
    crop = os.path.join(catalog.target_directory, 'cache', 'faces-ai', f'{det}.jpg')
    os.makedirs(os.path.dirname(crop), exist_ok=True)
    img.crop((292, 38, 348, 122)).save(crop)
    conn.execute("INSERT INTO ai_face_contents (file_path, file_name, face_detection_id, "
                 "source_file_id, embedding) VALUES (?, ?, ?, ?, '[1,2]')",
                 (os.path.relpath(crop, catalog.target_directory), f'{det}.jpg', det, fid))
    conn.commit()
    conn.close()
    return {'id': fid, 'original': original, 'sha': _sha(original),
            'mtime': os.path.getmtime(original), 'det': det, 'crop': crop}


def _wait(catalog, fid):  # noqa: F811
    deadline = time.time() + 30
    for t in [t for t in threading.enumerate() if t.name.startswith('photo-edit-')]:
        t.join(max(0, deadline - time.time()))
    conn = _db(catalog)
    try:
        row = conn.execute('SELECT render_status, render_error FROM photo_edits '
                           'WHERE file_id = ?', (fid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _base(catalog, fid):  # noqa: F811
    conn = _db(catalog)
    row = conn.execute("SELECT * FROM alternative_versions WHERE original_file_id = ? "
                       "AND rung = '1200'", (fid,)).fetchone()
    conn.close()
    return dict(row), Image.open(os.path.join(catalog.target_directory,
                                              row['alternative_file_path'])).convert('RGB')


def _red_at(img, x, y):
    r, g, b = img.getpixel((x, y))
    return r > 200 and g < 60 and b < 60


def test_rotating_rebuilds_derivatives_and_moves_faces_never_the_original(
        client, catalog, photo):  # noqa: F811
    fid = photo['id']
    before, _ = _base(catalog, fid)

    r = client.put(f'/api/catalogs/1/files/{fid}/edits',
                   json={'orientation': 6, 'base_version': 0})
    assert r.status_code == 202, r.json
    assert r.json['render_status'] == 'rendering'
    assert _wait(catalog, fid)['render_status'] == 'ready'

    # The original: not one byte.
    assert _sha(photo['original']) == photo['sha']
    assert os.path.getmtime(photo['original']) == photo['mtime']

    # Same path, turned pixels: the red corner is now top-right.
    after, img = _base(catalog, fid)
    assert after['alternative_file_path'] == before['alternative_file_path']
    assert (after['width'], after['height']) == (200, 400) == img.size
    assert _red_at(img, 195, 5) and not _red_at(img, 5, 5)

    conn = _db(catalog)
    face = dict(conn.execute('SELECT * FROM face_detections WHERE id = ?',
                             (photo['det'],)).fetchone())
    crop = dict(conn.execute('SELECT * FROM ai_face_contents WHERE face_detection_id = ?',
                             (photo['det'],)).fetchone())
    history = conn.execute('SELECT * FROM photo_edit_history').fetchall()
    scanned = conn.execute('SELECT * FROM file_contents WHERE id = ?', (fid,)).fetchone()
    conn.close()
    # (300,50,40,60) in 400x200, turned right: (200-50-60, 300, 60, 40) in 200x400.
    assert (face['x'], face['y'], face['w'], face['h']) == (90, 300, 60, 40)
    assert (face['image_width'], face['image_height']) == (200, 400)
    assert face['person_id'] == 7
    assert crop['embedding'] is None
    assert Image.open(photo['crop']).size[0] < Image.open(photo['crop']).size[1] * 2
    assert [(h['before_orientation'], h['after_orientation']) for h in history] == [(1, 6)]
    if 'face_detection_scanned_at' in scanned.keys():
        assert scanned['face_detection_scanned_at'] is None

    edit = client.get(f'/api/catalogs/1/files/{fid}/edits').json
    assert (edit['orientation'], edit['version'], edit['render_status']) == (6, 1, 'ready')
    assert edit['summary'] == 'Rotated 90° right'
    assert client.get(f'/api/catalogs/1/photos/{fid}').json['edit']['orientation'] == 6


def test_reset_returns_to_the_import_and_the_version_keeps_rising(
        client, catalog, photo):  # noqa: F811
    fid = photo['id']
    client.put(f'/api/catalogs/1/files/{fid}/edits', json={'orientation': 3, 'base_version': 0})
    _wait(catalog, fid)
    r = client.delete(f'/api/catalogs/1/files/{fid}/edits', json={'base_version': 1})
    assert r.status_code == 202
    assert _wait(catalog, fid) is None           # a finished reset leaves no row
    _, img = _base(catalog, fid)
    assert img.size == (400, 200) and _red_at(img, 5, 5)
    conn = _db(catalog)
    face = conn.execute('SELECT x, y FROM face_detections WHERE id = ?', (photo['det'],)).fetchone()
    conn.close()
    assert tuple(face) == (300, 50)
    edit = client.get(f'/api/catalogs/1/files/{fid}/edits').json
    assert (edit['orientation'], edit['version']) == (1, 2)
    versions = client.get('/api/catalogs/1/edits/versions').json['versions']
    assert versions == {str(fid): 2}


def test_refusals(client, catalog, photo):  # noqa: F811
    fid = photo['id']
    url = f'/api/catalogs/1/files/{fid}/edits'
    assert client.put(url, json={'orientation': 9}).status_code == 400
    assert client.put(url, json={'orientation': True}).status_code == 400
    assert client.put('/api/catalogs/1/files/9999/edits', json={'orientation': 6}).status_code == 404
    assert client.put(url, json={'orientation': 6, 'base_version': 5}).json['code'] == 'stale'
    # The same value is not an edit.
    r = client.put(url, json={'orientation': 1, 'base_version': 0})
    assert r.status_code == 200

    conn = _db(catalog)
    vid = conn.execute("INSERT INTO file_contents (file_path, file_name, file_type) "
                       "VALUES ('v.mp4', 'v.mp4', '.mp4')").lastrowid
    gone = conn.execute("INSERT INTO file_contents (file_path, file_name, file_type, removed_at) "
                        "VALUES ('g.jpg', 'g.jpg', '.jpg', '2026-10-01')").lastrowid
    conn.commit()
    conn.close()
    assert client.put(f'/api/catalogs/1/files/{vid}/edits',
                      json={'orientation': 6}).status_code == 415
    assert client.put(f'/api/catalogs/1/files/{gone}/edits',
                      json={'orientation': 6}).json['code'] == 'removed'


def test_a_second_save_waits_for_the_rebuild(client, catalog, photo, monkeypatch):  # noqa: F811
    started = threading.Event()
    release = threading.Event()

    import app as app_mod
    real = app_mod._photo_edit_rebuild

    def slow(cat, fid):
        started.set()
        release.wait(10)
        real(cat, fid)
    monkeypatch.setattr(app_mod, '_photo_edit_rebuild', slow)
    fid = photo['id']
    assert client.put(f'/api/catalogs/1/files/{fid}/edits',
                      json={'orientation': 6, 'base_version': 0}).status_code == 202
    started.wait(5)
    r = client.put(f'/api/catalogs/1/files/{fid}/edits', json={'orientation': 8, 'base_version': 1})
    assert r.status_code == 409 and r.json['code'] == 'rendering'
    release.set()
    assert _wait(catalog, fid)['render_status'] == 'ready'


def test_a_restart_sets_an_interrupted_rebuild_back_to_pending(catalog, photo):  # noqa: F811
    import photo_edits
    conn = _db(catalog)
    conn.execute("INSERT INTO photo_edits (file_id, orientation, version, render_status, "
                 "updated_at) VALUES (?, 6, 1, 'rendering', '2026-10-19')", (photo['id'],))
    conn.commit()
    assert photo_edits.interrupted(conn) == [photo['id']]
    assert conn.execute('SELECT render_status FROM photo_edits').fetchone()[0] == 'pending'
    conn.close()


# ─── Caching (spec 7.2) ──────────────────────────────────────────────────────

def test_thumbnails_are_immutable_only_under_a_version(client, catalog, photo):  # noqa: F811
    fid = photo['id']
    base = f'/api/catalogs/1/photos/{fid}/thumbnail?size=320'
    r = client.get(base + '&v=0')
    assert r.status_code == 200
    assert r.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    r = client.get(base)
    assert r.headers['Cache-Control'] == 'private, no-cache'
    etag = r.headers['ETag']
    assert client.get(base, headers={'If-None-Match': etag}).status_code == 304

    client.put(f'/api/catalogs/1/files/{fid}/edits', json={'orientation': 6, 'base_version': 0})
    _wait(catalog, fid)
    assert client.get(base, headers={'If-None-Match': etag}).status_code == 200


def test_server_built_thumbnail_urls_carry_the_version(app_module, catalog, photo):  # noqa: F811
    fid = photo['id']
    with app_module.app.test_request_context():
        assert app_module._thumb_url(1, fid).endswith('&v=0')
    conn = _db(catalog)
    conn.execute("INSERT INTO photo_edit_history (file_id, before_orientation, "
                 "after_orientation, created_at) VALUES (?, 1, 6, 'x')", (fid,))
    conn.commit()
    conn.close()
    with app_module.app.test_request_context():
        assert app_module._thumb_url(1, fid).endswith('&v=1')
