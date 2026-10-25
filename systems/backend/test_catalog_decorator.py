"""
Catalog card decorators: one per catalog, picked at random from the theme once
and then kept.

    python -m pytest test_catalog_decorator.py
"""

import os
import re
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import catalog as catalog_module  # noqa: E402
from catalog import Catalog, DECORATOR_COUNT, DECORATOR_THEME  # noqa: E402

PATTERN = re.compile(rf'^{DECORATOR_THEME}/cardDeco(\d+)$')
PUBLIC = os.path.join(HERE, '..', 'frontend', 'public')


@pytest.fixture
def db(tmp_path, monkeypatch):
    path = str(tmp_path / 'catalogs.db')
    monkeypatch.setattr(Catalog, 'DB_PATH', path)
    catalog_module.ensure_schema()
    return path


def _new(name, tmp_path):
    cat = Catalog(name=name, file_strategy='local', target_directory=str(tmp_path / name))
    cat.save()
    return cat


def _number(decorator):
    m = PATTERN.match(decorator or '')
    assert m, decorator
    return int(m.group(1))


def test_a_new_catalog_gets_a_decorator_from_the_theme(db, tmp_path):
    cat = _new('Family', tmp_path)
    assert 1 <= _number(cat.decorator) <= DECORATOR_COUNT
    assert Catalog.get_by_id(cat.id).decorator == cat.decorator
    assert cat.to_dict()['decorator'] == cat.decorator


def test_an_update_keeps_the_decorator(db, tmp_path):
    cat = _new('Family', tmp_path)
    picked = cat.decorator
    again = Catalog.get_by_id(cat.id)
    again.description = 'changed'
    again.save()
    assert Catalog.get_by_id(cat.id).decorator == picked


def test_the_picks_are_random(db, tmp_path):
    picks = {_number(_new(f'C{i}', tmp_path).decorator) for i in range(40)}
    assert len(picks) > 5


def test_existing_catalogs_are_given_one_once(db, tmp_path):
    a = _new('A', tmp_path)
    b = _new('B', tmp_path)
    conn = sqlite3.connect(db)
    conn.execute('UPDATE catalog_meta SET decorator = NULL WHERE id = ?', (a.id,))
    conn.commit()
    conn.close()
    catalog_module.ensure_schema()                      # the migration again
    assert _number(Catalog.get_by_id(a.id).decorator)   # filled
    assert Catalog.get_by_id(b.id).decorator == b.decorator   # untouched


def test_a_pick_from_another_theme_moves_across_keeping_its_number(db, tmp_path):
    """Catalogs moved from theme1 to theme2 (2026-10-20): same card, new theme."""
    a = _new('A', tmp_path)
    conn = sqlite3.connect(db)
    conn.execute("UPDATE catalog_meta SET decorator = 'theme1/cardDeco7' WHERE id = ?", (a.id,))
    conn.commit()
    conn.close()
    catalog_module.ensure_schema()
    assert Catalog.get_by_id(a.id).decorator == f'{DECORATOR_THEME}/cardDeco7'
    assert DECORATOR_THEME == 'theme2'


def test_every_decorator_in_the_theme_exists_with_its_bw_twin():
    for n in range(1, DECORATOR_COUNT + 1):
        for suffix in ('', 'bw'):
            path = os.path.join(PUBLIC, DECORATOR_THEME, f'cardDeco{n}{suffix}.png')
            assert os.path.isfile(path), path


# ─── Deployments (theme1) ─────────────────────────────────────────────────────

from card_decorators import DEPLOYMENT_THEME, DECORATORS_PER_THEME  # noqa: E402
# The app, imported with every database in a temporary folder.
from test_photo_dates_batch import app_module  # noqa: E402,F401  (fixture)

DEPLOYMENT_PATTERN = re.compile(rf'^{DEPLOYMENT_THEME}/cardDeco(\d+)$')


def test_every_deployment_decorator_exists_with_its_bw_twin():
    for n in range(1, DECORATORS_PER_THEME + 1):
        for suffix in ('', 'bw'):
            path = os.path.join(PUBLIC, DEPLOYMENT_THEME, f'cardDeco{n}{suffix}.png')
            assert os.path.isfile(path), path


def test_existing_deployments_are_given_one_once(tmp_path):
    from models import CatalogDatabase
    target = str(tmp_path / 'cat')
    db_ = CatalogDatabase(target)
    conn = db_.get_connection()
    conn.execute("INSERT INTO album_deployments (slug, name) VALUES ('a', 'A')")
    conn.execute("INSERT INTO album_deployments (slug, name, decorator) "
                 "VALUES ('b', 'B', 'theme1/cardDeco7')")
    # A pick from when deployments used theme2: moved across, same number.
    conn.execute("INSERT INTO album_deployments (slug, name, decorator) "
                 "VALUES ('c', 'C', 'theme2/cardDeco13')")
    conn.execute("UPDATE album_deployments SET decorator = NULL WHERE slug = 'a'")
    conn.commit()
    conn.close()
    CatalogDatabase(target)                                  # the migration again
    conn = db_.get_connection()
    got = dict(conn.execute('SELECT slug, decorator FROM album_deployments'))
    conn.close()
    assert DEPLOYMENT_PATTERN.match(got['a'])
    assert got['b'] == 'theme1/cardDeco7'
    assert got['c'] == 'theme1/cardDeco13'


def test_a_new_deployment_gets_a_theme1_decorator(app_module, monkeypatch, tmp_path):
    from types import SimpleNamespace
    cat = SimpleNamespace(id=1, name='T', slug='t', target_directory=str(tmp_path / 'c'),
                          ingest_mode='copy', source_root=None, image_rungs=[])
    app_module.CatalogDatabase(cat.target_directory)
    monkeypatch.setattr(app_module.Catalog, 'get_by_id',
                        classmethod(lambda cls, cid: cat if cid == 1 else None))
    monkeypatch.setattr(app_module, '_publish_deployment', lambda *a, **k: None)
    client = app_module.app.test_client()
    r = client.post('/api/catalogs/1/deployments', json={'name': 'Album 2026'})
    assert r.status_code in (200, 201), r.get_json()
    body = r.get_json()
    dep = body.get('deployment', body)
    assert DEPLOYMENT_PATTERN.match(dep['decorator']), dep
    listed = client.get('/api/catalogs/1/deployments').get_json()
    listed = listed.get('deployments', listed) if isinstance(listed, dict) else listed
    assert [d['decorator'] for d in listed] == [dep['decorator']]
