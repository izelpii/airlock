#!/usr/bin/env python3
"""
Measure and Match Pets names an animal only near the pet's CENTRE.

    python -m pytest test_pet_auto_naming.py

It named any unnamed animal within PET_MATCH_DISTANCE of any ONE photograph of
a pet - with only one pet the margin always holds - which is chaining again: on
`family`, after the 2,357 photographs outside Luna's core were unnamed, 834
of them were that close to one of her 5,021 and would have been put straight
back. Now the animal must also be within PET_MATCH_CENTRE_DISTANCE of the
mean of the pet's photographs.

Vectors on a circle: 1 - cos(angle) is the distance. 1.0 rad from the centre
is 0.46, far outside 0.28; 0.05 rad between two crops is 0.001.
"""

import json
import os
import sqlite3
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import pe_identify_pets as pe                      # noqa: E402
from test_pet_identify_locking import _catalog     # noqa: E402


def _vec(angle):
    v = [0.0] * 8
    v[0], v[1] = float(np.cos(angle)), float(np.sin(angle))
    return v


def _world(tmp_path, named_angles, unnamed_angles):
    """Pet 7 named on `named_angles`; the rest unnamed. Returns unnamed ids."""
    path = _catalog(tmp_path, detections=len(named_angles) + len(unnamed_angles))
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO people (id, name, kind) VALUES (7, 'Luna', 'pet')")
    det = 0
    unnamed = []
    for angle, named in ([(a, True) for a in named_angles]
                         + [(a, False) for a in unnamed_angles]):
        det += 1
        conn.execute('UPDATE pet_detections SET person_id = ? WHERE id = ?',
                     (7 if named else None, det))
        conn.execute('INSERT INTO ai_pet_contents (file_path, file_name, '
                     ' pet_detection_id, source_file_id, embedding) '
                     "VALUES ('c.jpg', 'c.jpg', ?, 1, ?)", (det, json.dumps(_vec(angle))))
        if not named:
            unnamed.append(det)
    conn.commit()
    conn.close()
    return path, unnamed


def _named(path, ids):
    conn = sqlite3.connect(path)
    try:
        return {i: conn.execute('SELECT person_id FROM pet_detections WHERE id = ?',
                                (i,)).fetchone()[0] for i in ids}
    finally:
        conn.close()


def _score(path):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return pe.phase_score(conn, 1, None)
    finally:
        conn.close()




def test_near_her_core_is_named(tmp_path):
    path, (near,) = _world(tmp_path, [0.0] * 20, [0.05])
    _score(path)
    assert _named(path, [near]) == {near: 7}


def test_near_one_odd_photograph_but_far_from_her_centre_is_not(tmp_path):
    """The 834: within 0.22 of one of her photographs, not of her."""
    path, (stray,) = _world(tmp_path, [0.0] * 20 + [1.0], [1.05])
    _score(path)
    assert _named(path, [stray]) == {stray: None}


def test_the_score_is_still_recorded_for_one_left_unnamed(tmp_path):
    """Scores are candidates for review; only the automatic NAME got stricter."""
    path, (stray,) = _world(tmp_path, [0.0] * 20 + [1.0], [1.05])
    _score(path)
    conn = sqlite3.connect(path)
    try:
        row = conn.execute('SELECT person_id, cosine_distance FROM pet_similarity_scores '
                           'WHERE pet_detection_id = ?', (stray,)).fetchone()
    finally:
        conn.close()
    assert row[0] == 7 and row[1] < 0.01
