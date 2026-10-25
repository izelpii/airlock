#!/usr/bin/env python3
"""
master_datetime is THE date of a photograph, and nothing falls back from it.

    python -m pytest test_master_datetime_is_the_date.py

It is decided once, at ingest, by resolve_master_datetime - which always
returns one - and it is the only date that means "when this was taken". The
code used to hedge: COALESCE(master_datetime, added_at) in twenty queries, and
`master_datetime || creation_date` in a dozen places in the frontend. Each was
a second opinion about a date that is never missing, and the SQL ones also
kept idx_fc_master_datetime from serving any of those queries.

These tests read the source, because a fallback that comes back does not fail
anything else: it silently returns the same answer, more slowly, until the day
the two dates disagree.
"""

import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
FRONTEND_SRC = os.path.join(HERE, '..', 'frontend', 'src')

# A date column or field offered as the alternative to master_datetime.
SQL_FALLBACK = re.compile(
    r'COALESCE\(\s*(?:\w+\.)?master_datetime\s*,\s*(?:\w+\.)?'
    r'(?:added_at|creation_date|modification_time|creation_time)', re.I)
JS_FALLBACK = re.compile(r'master_datetime\s*\|\|\s*[\w?.\[\]]*'
                         r'(?:creation_date|added_at|modification_time)')


def _sources(root, ext, skip):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames
                       if d not in ('node_modules', '__pycache__', 'legacy', 'build')]
        for name in filenames:
            if name.endswith(ext) and not skip(name):
                yield os.path.join(dirpath, name)


def _offenders(paths, pattern):
    found = []
    for path in paths:
        with open(path, encoding='utf-8') as fh:
            for n, line in enumerate(fh, 1):
                if pattern.search(line):
                    found.append(f'{os.path.relpath(path, HERE)}:{n}: {line.strip()}')
    return found


def test_no_query_falls_back_from_master_datetime():
    # Tests and migrations may spell out the old expression to describe it.
    paths = _sources(HERE, '.py', lambda n: n.startswith(('test_', 'migrate_')))
    found = [f for f in _offenders(paths, SQL_FALLBACK)
             if not f.split(': ', 1)[1].startswith('#')]
    assert not found, 'falls back from master_datetime:\n' + '\n'.join(found)


def test_no_frontend_code_falls_back_from_master_datetime():
    if not os.path.isdir(FRONTEND_SRC):
        return
    paths = _sources(FRONTEND_SRC, '.js', lambda n: n.endswith('.test.js'))
    found = _offenders(paths, JS_FALLBACK)
    assert not found, 'falls back from master_datetime:\n' + '\n'.join(found)


def test_the_guard_recognises_what_it_guards_against():
    """Else a typo in a pattern passes everything forever."""
    assert SQL_FALLBACK.search('COALESCE(fc.master_datetime, fc.added_at)')
    assert SQL_FALLBACK.search('COALESCE(fc.master_datetime, ed.creation_date, fc.added_at)')
    assert not SQL_FALLBACK.search("COALESCE(master_datetime_source, 'unknown')")
    assert JS_FALLBACK.search('photo.master_datetime || photo.creation_date')
    assert JS_FALLBACK.search('a?.master_datetime || a?.creation_date || 0')
    assert not JS_FALLBACK.search("(d.master_datetime || '').slice(0, 4)")
