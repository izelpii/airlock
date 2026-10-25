"""
A linked user's picture follows the person's profile photo in the catalog.

    python -m pytest test_person_avatar.py

2026-10-04: the avatar was cached per person, so the first one ever built
(2026-09-09) was served forever and a new profile photo never reached the user.
"""

import os
import sqlite3

from PIL import Image

from test_photo_dates_batch import app_module, catalog  # noqa: F401  (fixtures)


def _photo(cat, name, colour):
    rel = os.path.join('Images', name)
    path = os.path.join(cat.target_directory, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', (400, 300), colour).save(path, 'JPEG')
    conn = sqlite3.connect(os.path.join(cat.target_directory, 'workflow_results.db'))
    fid = conn.execute("INSERT INTO file_contents (file_path, file_name, file_type) "
                       "VALUES (?, ?, '.jpg')", (rel, name)).lastrowid
    conn.commit()
    conn.close()
    return fid


def _db(cat):
    conn = sqlite3.connect(os.path.join(cat.target_directory, 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    return conn


def _colour(path):
    r, g, b = Image.open(path).convert('RGB').resize((1, 1)).getpixel((0, 0))
    return 'red' if r > b else 'blue'


def test_the_avatar_follows_the_profile_photo(app_module, catalog):
    red = _photo(catalog, 'red.jpg', (220, 20, 20))
    blue = _photo(catalog, 'blue.jpg', (20, 20, 220))
    conn = _db(catalog)
    pid = conn.execute("INSERT INTO people (name, profile_photo_file_id) VALUES ('Izelpi', ?)",
                       (red,)).lastrowid
    for fid in (red, blue):
        conn.execute('INSERT INTO face_detections (file_id, person_id, x, y, w, h, '
                     'image_width, image_height) VALUES (?, ?, 150, 100, 80, 80, 400, 300)',
                     (fid, pid))
    conn.commit()
    conn.close()

    first = app_module._person_avatar_file(1, pid)
    assert first and _colour(first) == 'red'
    assert app_module._person_avatar_file(1, pid) == first          # cached

    conn = _db(catalog)
    conn.execute('UPDATE people SET profile_photo_file_id = ? WHERE id = ?', (blue, pid))
    conn.commit()
    conn.close()

    second = app_module._person_avatar_file(1, pid)
    assert second != first and _colour(second) == 'blue'
    assert not os.path.exists(first)                                # stale one gone


def test_a_legacy_person_only_cache_file_is_not_served(app_module, catalog):
    blue = _photo(catalog, 'blue.jpg', (20, 20, 220))
    conn = _db(catalog)
    pid = conn.execute("INSERT INTO people (name, profile_photo_file_id) VALUES ('Dani', ?)",
                       (blue,)).lastrowid
    conn.execute('INSERT INTO face_detections (file_id, person_id, x, y, w, h, '
                 'image_width, image_height) VALUES (?, ?, 150, 100, 80, 80, 400, 300)',
                 (blue, pid))
    conn.commit()
    conn.close()
    legacy = os.path.join(catalog.target_directory, 'cache', 'avatars', f'person_{pid}.jpg')
    os.makedirs(os.path.dirname(legacy), exist_ok=True)
    Image.new('RGB', (256, 256), (220, 20, 20)).save(legacy, 'JPEG')   # the old, red face

    path = app_module._person_avatar_file(1, pid)
    assert _colour(path) == 'blue'
    assert not os.path.exists(legacy)
