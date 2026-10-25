"""
The filename tier of the import's date resolution
(io_picture_import_workflow.extract_date_from_filename).

    python -m pytest test_filename_dates.py
"""

import os
import sys
from datetime import datetime

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

from io_picture_import_workflow import (  # noqa: E402
    extract_date_from_filename, get_creation_date_from_organized_structure,
)


def _when(name):
    d = extract_date_from_filename(name)
    if d is None:
        return None
    return (d['year'], d['month'], d['day'], d['hour'], d['minute'], d['second'])


@pytest.mark.parametrize('name, expected', [
    # Date only
    ('20030101.jpg',                       ('2003', '01', '01', None, None, None)),
    ('IMG-20190815-WA0001.jpg',            ('2019', '08', '15', None, None, None)),
    ('2003-01-01.jpg',                     ('2003', '01', '01', None, None, None)),
    ('2003_01_01 party.jpg',               ('2003', '01', '01', None, None, None)),
    ('2003.01.01.jpg',                     ('2003', '01', '01', None, None, None)),
    # YYYYMMDDHHMM - the Olympus scan that was filed under its import day
    ('200301010600_arboritro 1.jpg',       ('2003', '01', '01', '06', '00', None)),
    # With seconds, milliseconds, and separators
    ('20190815143022.jpg',                 ('2019', '08', '15', '14', '30', '22')),
    ('IMG_20190815_143022.jpg',            ('2019', '08', '15', '14', '30', '22')),
    ('PXL_20230101_123456789.jpg',         ('2023', '01', '01', '12', '34', '56')),
    ('2019-08-15 14.30.22.jpg',            ('2019', '08', '15', '14', '30', '22')),
    ('2019-08-15_14-30-22.jpg',            ('2019', '08', '15', '14', '30', '22')),
    ('Screenshot 2023-01-01 at 10.30.00.png', ('2023', '01', '01', '10', '30', '00')),
    # Year and month only
    ('2003-05 trip.jpg',                   ('2003', '05', None, None, None, None)),
])
def test_recognised(name, expected):
    assert _when(name) == expected


@pytest.mark.parametrize('name', [
    # The Facebook id that was once filed in the year 9981
    '12189981_10156158419245024_1234567890_n.jpg',
    # Digit runs that are not 8, 12, 14 or 17 long
    '2003010106.jpg',
    '200301010.jpg',
    # Impossible dates and years
    '20030230.jpg',
    '20031301.jpg',
    '19690101.jpg',
    '00000000.jpg',
    # An impossible time glued on: the whole run is not a timestamp
    '200301019999.jpg',
    'DSC_0042.jpg',
])
def test_rejected(name):
    assert extract_date_from_filename(name) is None


def test_impossible_time_behind_a_separator_keeps_the_date():
    assert _when('2003-01-01 9999.jpg') == ('2003', '01', '01', None, None, None)


def test_a_later_match_is_used_when_the_first_is_not_a_date():
    assert _when('99990101_20030101.jpg')[:3] == ('2003', '01', '01')


def test_filename_time_is_used_instead_of_noon(tmp_path):
    src = tmp_path / '01'
    src.mkdir()
    f = src / '200301010600_arboritro 1.jpg'
    f.write_bytes(b'x')
    blank_exif = {'creation_date': '0000:00:00 00:00:00'}
    assert get_creation_date_from_organized_structure(
        str(f), str(tmp_path), blank_exif) == datetime(2003, 1, 1, 6, 0, 0)


def test_date_only_filename_still_gets_noon(tmp_path):
    f = tmp_path / 'IMG-20190815-WA0001.jpg'
    f.write_bytes(b'x')
    assert get_creation_date_from_organized_structure(
        str(f), str(tmp_path), None) == datetime(2019, 8, 15, 12, 0, 0)
