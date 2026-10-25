#!/usr/bin/env python3
"""
A thumbnail that was never built is not a reason to send the original.

    python -m pytest test_thumbnail_fallback.py

The endpoint used to redirect to /image when no thumbnail existed, which serves
the ORIGINAL - a 4 MB JPEG, or a HEIC or CR3 the backend must decode - into a
72 or 213 pixel box. One is a slow tile; a gallery of them is the backend
decoding full-resolution photographs in a loop while the page waits.

Measured on `family` (308,289 images), the files with no thumbnail of their own
are 4,263 .cr3, 2,355 .heic and 569 .cr2 - so the fallback fired almost
exclusively on the formats that are most expensive to decode.

MOST OF THOSE ARE NOT MISSING ANYTHING. 7,119 of 7,198 are the RAW half of a
RAW+JPG pair, which has no derivatives by design: the primary holds them and
_derivative_owner() resolves to it. Resolved properly, 104 images in the whole
catalog genuinely lack a thumbnail - 0.03 % - and they are 66 .heic, 27 .cr3
and 11 .jpg whose render never completed.
"""

import os
import sqlite3
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _photo(catalog, name, file_type='.jpg'):
    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    cur = conn.execute(
        'INSERT INTO file_contents (file_path, file_name, file_type) '
        ' VALUES (?,?,?)', (os.path.join('Images', name), name, file_type))
    fid = cur.lastrowid
    conn.commit()
    conn.close()
    return fid


def _thumbnail(catalog, file_id, size=320):
    rel = os.path.join('cache', f'{file_id}_thumb_{size}.jpg')
    path = os.path.join(catalog.target_directory, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'wb') as fh:
        fh.write(b'\xff\xd8\xff\xdb' + b'0' * 64)       # enough to send
    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    conn.execute(
        'INSERT INTO alternative_versions (original_file_id, '
        ' alternative_file_path, alternative_file_name, characteristic, width) '
        ' VALUES (?,?,?,?,?)',
        (file_id, rel, os.path.basename(rel), f'TYPE THUMBNAIL {size}', size))
    conn.commit()
    conn.close()


def test_a_built_thumbnail_is_served(client, catalog):
    fid = _photo(catalog, 'a.jpg')
    _thumbnail(catalog, fid)
    r = client.get(f'/api/catalogs/{catalog.id}/photos/{fid}/thumbnail?size=320')
    assert r.status_code == 200
    assert r.mimetype == 'image/jpeg'


def test_a_missing_thumbnail_is_404_not_the_original(client, catalog):
    """
    THE change. A redirect here is how a 26 MB CR3 ends up being decoded to
    fill a 72 pixel square.
    """
    fid = _photo(catalog, 'raw.cr3', file_type='.cr3')
    r = client.get(f'/api/catalogs/{catalog.id}/photos/{fid}/thumbnail?size=320')
    assert r.status_code == 404, \
        f'expected no thumbnail, got {r.status_code} -> {r.headers.get("Location")}'
    assert 'Location' not in r.headers, 'it still redirects to the full image'
    assert 'thumbnail' in (r.get_json() or {}).get('error', '')


def test_it_never_redirects_whatever_the_size_asked_for(client, catalog):
    fid = _photo(catalog, 'b.heic', file_type='.heic')
    for size in (72, 213, 320, 4000):
        r = client.get(f'/api/catalogs/{catalog.id}/photos/{fid}'
                       f'/thumbnail?size={size}')
        assert r.status_code == 404, size
        assert 'Location' not in r.headers, size


def test_a_smaller_thumbnail_is_used_rather_than_the_original(client, catalog):
    """
    Asking for 320 when only 72 was built serves the 72 - never upscaled, and
    never the original. A small picture beats a slow one.
    """
    fid = _photo(catalog, 'c.jpg')
    _thumbnail(catalog, fid, size=72)
    r = client.get(f'/api/catalogs/{catalog.id}/photos/{fid}/thumbnail?size=320')
    assert r.status_code == 200
    assert 'Location' not in r.headers


def test_the_raw_half_of_a_pair_uses_the_primarys_thumbnail(client, catalog):
    """
    Why the catalog figure looked ten times worse than it is. 7,119 of 7,198
    files 'without a thumbnail' are the RAW half of a RAW+JPG pair, which has
    no derivatives BY DESIGN - the primary holds them. _derivative_owner()
    resolves to it, so the secondary serves the pair's thumbnail rather than
    404ing or decoding a CR3.
    """
    jpg = _photo(catalog, 'pair.jpg')
    raw = _photo(catalog, 'pair.cr3', file_type='.cr3')
    _thumbnail(catalog, jpg)

    conn = sqlite3.connect(os.path.join(catalog.target_directory,
                                        'workflow_results.db'))
    conn.execute('INSERT INTO image_groups (id, primary_file_id) VALUES (1, ?)',
                 (jpg,))
    conn.execute('UPDATE file_contents SET image_group_id = 1 WHERE id IN (?,?)',
                 (jpg, raw))
    conn.commit()
    conn.close()

    r = client.get(f'/api/catalogs/{catalog.id}/photos/{raw}/thumbnail?size=320')
    assert r.status_code == 200, 'the RAW should serve its pair\'s thumbnail'
    assert r.mimetype == 'image/jpeg'
