#!/usr/bin/env python3
"""
Re-rank Matches by Context: the fast signals give exactly the old answers.

    python -m pytest test_context_signals.py

The time, device, location, age and event signals walked a person's whole
list of confirmed appearances for every candidate row - 12.2 M rows on
`family`, signal_time the largest single cost. They now search per-person
indexes sorted once per run. Checked on `family` itself: 780,000 rows, old and
new, not one distance or evidence string different. Here: against plain
reference implementations - the old loops - on randomised appearances, with
same-file entries and several hits inside a window, which is where an index
can go wrong (the device and location evidence names the FIRST qualifying
appearance in list order, and its exact gap).
"""

import math
import os
import random
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import ai_contextual_recognition_workflow as cw   # noqa: E402


# ── the old loops, verbatim in substance ────────────────────────────────────

def ref_time(qdt, qfile, apps):
    best = None
    for a in apps:
        if a['file_id'] == qfile or a['datetime'] is None:
            continue
        d = abs((qdt - a['datetime']).total_seconds()) / 60.0
        if best is None or d < best:
            best = d
    return best


def ref_first(qdt, qfile, apps, key, value, window):
    for a in apps:
        if a['file_id'] == qfile or not a.get(key) or not a['datetime']:
            continue
        if a[key] != value:
            continue
        if abs((qdt - a['datetime']).total_seconds()) <= window.total_seconds():
            return a
    return None


def _apps(rng, n):
    base = datetime(2020, 1, 1)
    out = []
    for _ in range(n):
        dt = None if rng.random() < 0.05 else base + timedelta(
            seconds=rng.choice([rng.randrange(0, 86400 * 30), rng.randrange(0, 600)]))
        out.append({'file_id': rng.randrange(1, 40), 'datetime': dt,
                    'year': dt.year if dt else None, 'event_id': rng.choice([None, 1, 2, 3]),
                    'camera': rng.choice(['a|x', 'b|y', '']),
                    'geohash': rng.choice(['9q8yyk', '9q8yyz', None])})
    return out


def _ctx(apps):
    return {'confirmed_by_person': {7: apps},
            'person_index': cw.build_person_index({7: apps}),
            'files': {}}


def test_time_matches_the_old_loop():
    rng = random.Random(1)
    for _ in range(400):
        apps = _apps(rng, rng.randrange(0, 30))
        ctx = _ctx(apps)
        qdt = datetime(2020, 1, 1) + timedelta(seconds=rng.randrange(0, 86400 * 30))
        qfile = rng.randrange(1, 40)
        s, _ = cw.signal_time(qdt, qfile, 7, ctx)
        want = ref_time(qdt, qfile, apps)
        assert s == (0.0 if want is None else math.exp(-want / cw.TIME_HALFLIFE_MIN))


def test_device_and_location_name_the_same_first_appearance():
    rng = random.Random(2)
    for _ in range(400):
        apps = _apps(rng, rng.randrange(0, 40))
        ctx = _ctx(apps)
        qdt = datetime(2020, 1, 1) + timedelta(seconds=rng.randrange(0, 600))
        qfile = rng.randrange(1, 40)
        for camera in ('a|x', 'b|y'):
            ctx['files'] = {qfile: {'camera': camera, 'datetime': qdt, 'geohash': '9q8yyk'}}
            face = {'file_id': qfile}
            s, detail = cw.signal_device_burst(face, 7, ctx)
            hit = ref_first(qdt, qfile, apps, 'camera', camera,
                            timedelta(seconds=cw.DEVICE_BURST_SEC))
            if hit is None:
                assert (s, detail) == (0.0, '')
            else:
                secs = abs((qdt - hit['datetime']).total_seconds())
                assert detail == f'same camera, {secs:.0f}s from a confirmed appearance'
            s, detail = cw.signal_geo(face, 7, ctx)
            hit = ref_first(qdt, qfile, apps, 'geohash', '9q8yyk',
                            timedelta(hours=cw.GEO_WINDOW_HOURS))
            if hit is None:
                assert (s, detail) == (0.0, '')
            else:
                hours = abs((qdt - hit['datetime']).total_seconds()) / 3600.0
                assert detail == (f'same location (9q8yyk) within {hours:.1f} h '
                                  f'of a confirmed appearance')


def test_event_and_age_match_the_old_loops():
    rng = random.Random(3)
    for _ in range(400):
        apps = _apps(rng, rng.randrange(0, 30))
        ctx = _ctx(apps)
        qfile = rng.randrange(1, 40)
        for event in (1, 2, 3):
            n = len({a['file_id'] for a in apps
                     if a['event_id'] == event and a['file_id'] != qfile})
            s, _ = cw.signal_event(qfile, event, 7, ctx)
            assert s == (0.0 if n == 0 else min(1.0, n / cw.EVENT_SATURATION))
        qyear = rng.choice([2010, 2019, 2020, 2021, 2035])
        years = [a['year'] for a in apps if a['year'] is not None]
        s, _ = cw.signal_age_gap(qyear, 7, ctx)
        if not years or min(abs(qyear - y) for y in years) <= 1:
            assert s == 0.0
        else:
            gap = min(abs(qyear - y) for y in years)
            assert s == -min(1.0, gap / cw.AGE_GAP_FULL_YEARS)


def test_a_person_with_no_appearances_scores_nothing():
    ctx = {'confirmed_by_person': {}, 'person_index': {}, 'files': {}}
    assert cw.signal_time(datetime(2020, 1, 1), 1, 7, ctx) == (0.0, '')
    assert cw.signal_age_gap(2020, 7, ctx) == (0.0, '')
    assert cw.signal_event(1, 1, 7, ctx) == (0.0, '')
