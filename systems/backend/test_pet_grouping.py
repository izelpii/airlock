#!/usr/bin/env python3
"""
Group Unnamed Pets: linked, then trimmed to each group's core.

    python -m pytest test_pet_grouping.py

Single-link grouping joins A and C whenever a path of close neighbours runs
between them, however unlike they are. On `family` that chained 7,378 of 7,471
animals into one group - Luna, ~5,000 times, and ~2,000 children, goats,
other dogs and statues - whose furthest member was 1.09 from its centre.
group_animals trims each group to the members within the threshold of its
centre and groups what it cut among itself.

The vectors here lie on a circle, so a distance is 1 - cos(angle apart):
0.6 rad apart is 0.175, linked at 0.28; 1.5 rad is 0.93, nothing alike.
"""

import math
import os
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import pe_cluster_pets as pc  # noqa: E402

CAP = 0.28


def _at(angles):
    m = np.zeros((len(angles), 8), dtype=np.float32)
    m[:, 0] = np.cos(angles)
    m[:, 1] = np.sin(angles)
    return m


def _spread(centre, n, width=0.04):
    return [centre + width * (i / max(1, n - 1) - 0.5) for i in range(n)]


def _furthest(matrix, group):
    c = matrix[group].mean(axis=0)
    c /= np.linalg.norm(c)
    return float((1 - matrix[group] @ c).max())


def test_linking_alone_chains_strays_onto_a_core():
    """The failure, reproduced: the precondition for the tests below."""
    angles = _spread(0.0, 20) + [0.6, 1.2, 1.8, 2.4, 3.0]
    groups = [g for g in pc.connected_groups(_at(angles), CAP) if len(g) >= 3]
    assert len(groups) == 1 and len(groups[0]) == 25


def test_strays_chained_onto_a_core_are_trimmed_off_it():
    angles = _spread(0.0, 20) + [0.6, 1.2, 1.8, 2.4, 3.0]
    m = _at(angles)
    groups = pc.group_animals(m, CAP)
    core = max(groups, key=len)
    assert set(range(20)) <= set(core)
    assert not {22, 23, 24} & set(core)              # 1.8 rad and beyond
    assert _furthest(m, core) <= CAP


def test_a_second_animal_chained_to_the_first_gets_its_own_group():
    a = _spread(0.0, 20)
    b = _spread(1.6, 15)
    bridge = [0.55, 1.05]                            # two photos joining them
    m = _at(a + b + bridge)
    assert len([g for g in pc.connected_groups(m, CAP) if len(g) >= 3]) == 1

    groups = sorted(pc.group_animals(m, CAP), key=len, reverse=True)
    a_ids, b_ids = set(range(20)), set(range(20, 35))
    assert a_ids <= set(groups[0]) and not b_ids & set(groups[0])
    assert b_ids <= set(groups[1]) and not a_ids & set(groups[1])
    assert all(_furthest(m, g) <= CAP for g in groups)


def test_a_tight_group_is_left_as_it_was():
    m = _at(_spread(0.0, 10))
    assert pc.group_animals(m, CAP) == [list(range(10))]


def test_fewer_than_the_minimum_is_no_group():
    m = _at(_spread(0.0, 2) + _spread(math.pi, 2))
    assert pc.group_animals(m, CAP) == []


# ── A photograph taken off a named pet is free to be grouped again ───────────
#
# Unnaming clears pet_detections.person_id and left the membership of the named
# group behind, which kept the photograph out of every later grouping: on
# `family`, 2,357 taken off Luna were all still her group's members, and the
# next run grouped 17 of 2,450 unnamed animals.

import json        # noqa: E402
import sqlite3     # noqa: E402

from test_pet_identify_locking import _catalog   # noqa: E402


def test_an_unnamed_member_of_a_named_group_is_regrouped(tmp_path):
    path = _catalog(tmp_path, detections=6)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO people (id, name, kind) VALUES (7, 'Luna', 'pet')")
    conn.execute("INSERT INTO pet_clusters (id, centroid, member_count, resolved_person_id, "
                 " resolved_at) VALUES (99, '[]', 3, 7, '2026-01-01')")
    for det in range(1, 7):
        angle = 0.0 if det <= 2 else 2.0               # 1-2 Luna; 3-6 another animal
        conn.execute('UPDATE pet_detections SET person_id = ? WHERE id = ?',
                     (7 if det <= 2 else None, det))   # 3 was Luna's, now unnamed
        conn.execute('INSERT INTO ai_pet_contents (file_path, file_name, pet_detection_id, '
                     " source_file_id, embedding) VALUES ('c.jpg', 'c.jpg', ?, 1, ?)",
                     (det, json.dumps([float(np.cos(angle)), float(np.sin(angle)), 0, 0,
                                       0, 0, 0, 0])))
    for det in (1, 2, 3):                              # the stale membership
        conn.execute('INSERT INTO pet_cluster_members (cluster_id, pet_detection_id, '
                     ' distance_to_centroid) VALUES (99, ?, 0.0)', (det,))
    conn.commit()
    conn.close()

    pc.execute(None, str(tmp_path), [], None)

    conn = sqlite3.connect(path)
    try:
        offered = conn.execute('SELECT m.pet_detection_id FROM pet_cluster_members m '
                               '  JOIN pet_clusters c ON c.id = m.cluster_id '
                               ' WHERE c.resolved_at IS NULL ORDER BY 1').fetchall()
        named = conn.execute('SELECT member_count FROM pet_clusters WHERE id = 99').fetchone()[0]
        named_members = conn.execute('SELECT pet_detection_id FROM pet_cluster_members '
                                     'WHERE cluster_id = 99 ORDER BY 1').fetchall()
    finally:
        conn.close()
    assert [r[0] for r in offered] == [3, 4, 5, 6]
    assert named == 2 and [r[0] for r in named_members] == [1, 2]


def test_a_rejected_groups_members_stay_out(tmp_path):
    path = _catalog(tmp_path, detections=3)
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO pet_clusters (id, centroid, member_count, resolved_at) "
                 "VALUES (98, '[]', 3, '2026-01-01')")
    for det in (1, 2, 3):
        conn.execute('INSERT INTO ai_pet_contents (file_path, file_name, pet_detection_id, '
                     " source_file_id, embedding) VALUES ('c.jpg', 'c.jpg', ?, 1, ?)",
                     (det, json.dumps([1.0, 0, 0, 0, 0, 0, 0, 0])))
        conn.execute('INSERT INTO pet_cluster_members (cluster_id, pet_detection_id, '
                     ' distance_to_centroid) VALUES (98, ?, 0.0)', (det,))
    conn.commit()
    conn.close()
    assert pc.execute(None, str(tmp_path), [], None)['clusters'] == 0
