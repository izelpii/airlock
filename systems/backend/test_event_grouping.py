#!/usr/bin/env python3
"""
Create Events groups exactly as before, without rescanning each group.

    python -m pytest test_event_grouping.py

group_lat_lon(groups[-1]) - the group's first member with GPS - was asked for
every dated file, scanning the group from its start: quadratic in a group with
no GPS, and `family` has ~195,000 files in such events. group_files keeps each
group's location beside it instead. These compare it with the old loop.
"""

import os
import random
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import ev_extract_events as ev   # noqa: E402


def old_grouping(dated):
    """The loop as it was, with group_lat_lon asked every time."""
    gap = timedelta(hours=ev.EVENT_GAP_HOURS)
    groups = []
    for item in dated:
        dt, row = item
        if not groups:
            groups.append([item])
            continue
        if dt - groups[-1][-1][0] > gap:
            groups.append([item])
            continue
        lat, lon = ev.group_lat_lon(groups[-1])
        ilat, ilon = row.get('latitude'), row.get('longitude')
        if (lat is not None and lon is not None and ilat is not None and ilon is not None
                and ev.haversine_km(lat, lon, ilat, ilon) > ev.MAX_GEO_DISTANCE_KM):
            groups.append([item])
            continue
        groups[-1].append(item)
    return groups, [ev.group_lat_lon(g) for g in groups]


def _dated(rng, n):
    t = datetime(2020, 1, 1)
    out = []
    for i in range(n):
        t += timedelta(minutes=rng.choice([1, 5, 30, 200, 2000]))
        gps = rng.random()
        row = {'id': i}
        if gps < 0.5:
            row['latitude'] = rng.choice([-34.6, -34.61, 40.4, None])
            row['longitude'] = rng.choice([-58.4, -58.41, -3.7, None])
        out.append((t, row))
    return out


def test_the_same_groups_and_locations_as_the_old_loop():
    rng = random.Random(5)
    for _ in range(300):
        dated = _dated(rng, rng.randrange(0, 60))
        new_groups, new_locs = ev.group_files(dated)
        old_groups, old_locs = old_grouping(dated)
        assert [[r['id'] for _, r in g] for g in new_groups] == \
               [[r['id'] for _, r in g] for g in old_groups]
        assert new_locs == old_locs


def test_a_big_group_without_gps_is_linear():
    """50,000 files a minute apart, no GPS: one group, found quickly."""
    t = datetime(2020, 1, 1)
    dated = [(t + timedelta(minutes=i), {'id': i}) for i in range(50_000)]
    import time
    start = time.time()
    groups, locs = ev.group_files(dated)
    assert len(groups) == 1 and locs == [(None, None)]
    assert time.time() - start < 5        # the old loop: ~1.25 billion steps
