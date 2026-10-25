"""
Browser caching of images: an ETag checked before any work, and an hour of
max-age, on every image endpoint that is not content-addressed.

    python -m pytest test_image_caching.py

Uses the isolated app from test_photo_dates_batch.py: backend/database/ is the
running container's live mount and must not be opened from here.
"""

import os

import pytest

from test_photo_dates_batch import (  # noqa: F401  (fixtures)
    app_module, catalog, client, _add_file, _row,
)

CACHE = 'private, max-age=3600'      # a photo's own image: pixels never change
REVALIDATE = 'private, no-cache'     # rewritten in place at a stable URL


@pytest.fixture
def no_pillow(monkeypatch):
    """Fail the test if anything decodes an image: a 304 must cost nothing."""
    import PIL.Image

    def _boom(*a, **k):
        raise AssertionError('a conditional hit decoded the image')
    monkeypatch.setattr(PIL.Image, 'open', _boom)


def _photo(catalog):
    fid = _add_file(catalog, 'a.jpg')
    return fid, f'/api/catalogs/1/photos/{fid}/image'


def test_the_full_size_image_carries_an_etag_and_an_hour(client, catalog):
    _fid, url = _photo(catalog)
    r = client.get(url)
    assert r.status_code == 200
    assert r.headers['Cache-Control'] == CACHE
    assert r.headers.get('ETag')


def test_a_repeat_view_is_a_304_without_decoding(client, catalog, no_pillow):
    _fid, url = _photo(catalog)
    etag = client.get(url).headers['ETag']
    r = client.get(url, headers={'If-None-Match': etag})
    assert r.status_code == 304
    assert r.headers['ETag'] == etag
    assert r.headers['Cache-Control'] == CACHE
    assert r.data == b''


def test_a_changed_file_is_served_again(client, catalog):
    fid, url = _photo(catalog)
    etag = client.get(url).headers['ETag']
    alt = os.path.join(catalog.target_directory, _row(catalog, fid)['alt'][0]['alternative_file_path'])
    with open(alt, 'ab') as f:
        f.write(b'more')
    r = client.get(url, headers={'If-None-Match': etag})
    assert r.status_code == 200
    assert r.headers['ETag'] != etag


def test_the_original_download_is_cached_too(client, catalog, no_pillow):
    _fid, url = _photo(catalog)
    r = client.get(url + '?original=1')
    assert r.status_code == 200
    assert r.headers['Cache-Control'] == CACHE
    assert client.get(url + '?original=1',
                      headers={'If-None-Match': r.headers['ETag']}).status_code == 304


def test_thumbnails_are_immutable_under_a_version(client, catalog):
    import sqlite3
    fid = _add_file(catalog, 'a.jpg')
    thumb = os.path.join(catalog.target_directory, 'cache', 't_thumb_320.jpg')
    open(thumb, 'wb').close()
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'))
    conn.execute(
        "INSERT INTO alternative_versions (original_file_id, alternative_file_path, "
        "alternative_file_name, characteristic, width) VALUES (?, ?, ?, 'TYPE THUMBNAIL 320', 320)",
        (fid, 'cache/t_thumb_320.jpg', 't_thumb_320.jpg'))
    conn.commit()
    conn.close()
    # A versioned URL names one edit of the photo, so it can be kept a year;
    # without the version it is revalidated (basic-photo-editing.md 7.2).
    r = client.get(f'/api/catalogs/1/photos/{fid}/thumbnail?size=320&v=0')
    assert r.headers['Cache-Control'] == 'public, max-age=31536000, immutable'
    r = client.get(f'/api/catalogs/1/photos/{fid}/thumbnail?size=320')
    assert r.headers['Cache-Control'] == REVALIDATE
    assert r.headers.get('ETag')


@pytest.mark.parametrize('url', ['/api/catalogs/files?path=PLACEHOLDER',
                                 '/api/people/photos/PLACEHOLDER'])
def test_catalog_files_and_people_photos(client, catalog, app_module, monkeypatch, url):
    path = os.path.join(catalog.target_directory, 'face.jpg')
    with open(path, 'wb') as f:
        f.write(b'not really a jpeg')
    # Access control is not under test here.
    monkeypatch.setattr(app_module, '_resolve_within_catalogs', lambda p: (path, None))
    url = url.replace('PLACEHOLDER', 'face.jpg')

    r = client.get(url)
    assert r.status_code == 200
    # A profile photo is rewritten in place: an hour of max-age showed the
    # previous face after re-linking (2026-10-04). Asked every time, a 304.
    assert r.headers['Cache-Control'] == REVALIDATE
    import PIL.Image

    def _boom(*a, **k):
        raise AssertionError('a conditional hit decoded the image')
    monkeypatch.setattr(PIL.Image, 'open', _boom)
    assert client.get(url, headers={'If-None-Match': r.headers['ETag']}).status_code == 304
