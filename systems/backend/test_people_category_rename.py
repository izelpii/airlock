"""
Renaming a people category from the People page.

    python -m pytest test_people_category_rename.py

A person's category is text on the person, so a rename has to move them too;
Family and Other are re-seeded whenever missing, so they cannot be renamed;
and renaming onto a name in use would merge two categories, so it is refused.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_photo_dates_batch import app_module, catalog, client  # noqa: F401,E402


def _rename(client, old, new):
    return client.put('/api/catalogs/1/people-categories/rename',
                      json={'from': old, 'to': new})


def _person(client, name, category):
    r = client.post('/api/catalogs/1/people', json={'name': name, 'category': category})
    assert r.status_code in (200, 201), r.get_json()


@pytest.fixture
def people(client, catalog):
    client.post('/api/catalogs/1/people-categories', json={'name': 'Freinds'})
    client.post('/api/catalogs/1/people-categories', json={'name': 'Band'})
    _person(client, 'Ana', 'Freinds')
    _person(client, 'Ben', 'Freinds')
    _person(client, 'Cy', 'Band')
    return client


def _categories(client):
    return client.get('/api/catalogs/1/people-categories').get_json()


def _by_name(client):
    return {p['name']: p['category'] for p in client.get('/api/catalogs/1/people').get_json()}


def test_a_rename_moves_everyone_in_the_category(people):
    r = _rename(people, 'Freinds', 'Friends')
    assert r.status_code == 200, r.get_json()
    assert r.get_json()['people_moved'] == 2
    cats = _categories(people)
    assert 'Friends' in cats and 'Freinds' not in cats
    assert _by_name(people) == {'Ana': 'Friends', 'Ben': 'Friends', 'Cy': 'Band'}


@pytest.mark.parametrize('built_in', ['Family', 'Other'])
def test_the_built_in_categories_cannot_be_renamed(people, built_in):
    assert _rename(people, built_in, 'Relatives').status_code == 409
    assert built_in in _categories(people)


def test_a_name_in_use_is_refused_rather_than_merged(people):
    r = _rename(people, 'Freinds', 'Band')
    assert r.status_code == 409
    assert _by_name(people) == {'Ana': 'Freinds', 'Ben': 'Freinds', 'Cy': 'Band'}


def test_an_unknown_category_is_not_found(people):
    assert _rename(people, 'Nobody', 'Somebody').status_code == 404


def test_a_name_with_a_slash_works(people):
    assert _rename(people, 'Band', 'Band / Orchestra').status_code == 200
    assert _by_name(people)['Cy'] == 'Band / Orchestra'
