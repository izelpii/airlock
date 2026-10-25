#!/usr/bin/env python3
"""
Face clustering at catalog scale: each pair once, across workers, in blocks.

    python -m pytest test_face_clustering_scale.py

On `family` (480,000 unidentified faces, ~51 million linked pairs) the
clustering step of Re-rank by Context would have taken ~4 hours with no
progress: it computed every pair twice, on the 2 threads the image's OpenBLAS
allows, and refined a chained component through its full pairwise matrix - tens
of GB for one large component. The answers must not change, so each piece is
checked against the plain computation it replaces.
"""

import os
import random
import sys
from collections import defaultdict

import numpy as np
import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import ai_contextual_recognition_workflow as wf    # noqa: E402


def _faces(people=12, per=30, dims=16, noise=0.25, seed=1):
    rng = np.random.default_rng(seed)
    centres = rng.normal(size=(people, dims))
    m = np.repeat(centres, per, axis=0) + rng.normal(scale=noise, size=(people * per, dims))
    m = m.astype(np.float32)
    m /= np.linalg.norm(m, axis=1, keepdims=True)
    return m[rng.permutation(len(m))]


def _brute_components(matrix, threshold):
    """The pre-v5.5.2 computation: every pair, Python union-find."""
    n = len(matrix)
    parent = list(range(n))

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x
    dist = 1.0 - matrix @ matrix.T
    for r, c in zip(*np.where(dist < threshold)):
        if r < c:
            ra, rb = find(int(r)), find(int(c))
            if ra != rb:
                parent[ra] = rb
    groups = defaultdict(list)
    for i in range(n):
        groups[find(i)].append(i)
    return groups.values()


def _canon(parts):
    return sorted(sorted(p) for p in parts)


@pytest.fixture(params=['in process', 'forked workers'])
def mode(request, monkeypatch):
    monkeypatch.setattr(wf, 'CLUSTER_BLOCK_SIZE', 32)
    if request.param == 'forked workers':
        monkeypatch.setattr(wf, 'CLUSTER_PARALLEL_MIN', 10)
        monkeypatch.setattr(wf, 'CLUSTER_WORKERS', 2)
    else:
        monkeypatch.setattr(wf, 'CLUSTER_PARALLEL_MIN', 10 ** 9)
    return request.param


@pytest.mark.parametrize('threshold', [0.05, 0.2, 0.3, 0.6])
def test_components_match_the_plain_computation(mode, threshold):
    m = _faces()
    got = wf._components(list(range(len(m))), m, threshold)
    assert _canon(got) == _canon(_brute_components(m, threshold))


def test_each_pair_is_found_once(mode):
    m = _faces(people=3, per=20)
    _, rows, cols = wf._scan(m, 1.0 - 0.3)
    pairs = list(zip(rows.tolist(), cols.tolist()))
    assert all(r < c for r, c in pairs)
    assert len(pairs) == len(set(pairs))
    dist = 1.0 - m @ m.T
    expected = {(r, c) for r in range(len(m)) for c in range(r + 1, len(m)) if dist[r, c] < 0.3}
    assert set(pairs) == expected


def test_the_spread_is_the_widest_pair(mode):
    m = _faces(people=4, per=15)
    lowest, _, _ = wf._scan(m, None)
    assert lowest == pytest.approx(float((m @ m.T).min()), abs=1e-5)


def test_refinement_matches_the_full_matrix_version(mode):
    """The old refinement, inline: spread from the full matrix, then split."""
    m = _faces(people=6, per=25, noise=0.45, seed=7)

    def old_refine(members, threshold, depth=0):
        if len(members) < wf.CLUSTER_MIN_SIZE:
            return []
        sub = m[members]
        spread = float((1.0 - (sub @ sub.T)).max())
        if spread <= wf.CLUSTER_MAX_SPREAD or depth >= wf.CLUSTER_MAX_REFINE_DEPTH:
            return [members] if spread <= wf.CLUSTER_MAX_SPREAD else []
        tighter = threshold * wf.CLUSTER_REFINE_FACTOR
        if tighter < wf.CLUSTER_MIN_THRESHOLD:
            return []
        out = []
        for part in _brute_components(sub, tighter):
            out.extend(old_refine([members[i] for i in part], tighter, depth + 1))
        return out

    members = list(range(len(m)))
    assert _canon(wf._refine_cluster(members, m, wf.CLUSTER_MAX_DISTANCE)) == \
        _canon(old_refine(members, wf.CLUSTER_MAX_DISTANCE))


def test_connected_handles_long_chains_and_no_pairs():
    n = 2000
    order = list(range(n))
    random.Random(3).shuffle(order)
    rows = np.array(order[:-1], dtype=np.int32)
    cols = np.array(order[1:], dtype=np.int32)
    lo, hi = np.minimum(rows, cols), np.maximum(rows, cols)
    assert _canon(wf._connected(n, lo, hi)) == [list(range(n))]
    empty = np.empty(0, dtype=np.int32)
    assert _canon(wf._connected(5, empty, empty)) == [[0], [1], [2], [3], [4]]


def test_the_scan_reports_progress_from_the_workers(monkeypatch):
    monkeypatch.setattr(wf, 'CLUSTER_BLOCK_SIZE', 8)
    monkeypatch.setattr(wf, 'CLUSTER_PARALLEL_MIN', 10)
    monkeypatch.setattr(wf, 'CLUSTER_WORKERS', 2)
    seen = []
    m = _faces(people=4, per=50)
    wf._scan(m, 0.7, lambda done, total: seen.append((done, total)))
    assert seen and seen[-1] == (25, 25)


def test_too_many_faces_skips_clustering_and_keeps_the_groups(tmp_path, monkeypatch):
    """
    Re-rank sat at 100 % for hours on `family`: its last step clustered
    480,000 faces. Above the limit the step is skipped, and the groups
    Identify groups already shows are left as they are.
    """
    import json
    import sqlite3
    db = str(tmp_path / 'workflow_results.db')
    c = sqlite3.connect(db)
    c.executescript("""
        CREATE TABLE face_detections (id INTEGER PRIMARY KEY, person_id INTEGER, source TEXT);
        CREATE TABLE face_embeddings (face_detection_id INTEGER, embedding_model_id INTEGER,
                                      embedding TEXT);
        CREATE TABLE face_clusters (id INTEGER PRIMARY KEY, centroid TEXT, member_count INTEGER,
                                    cohesion REAL, max_member_distance REAL);
        CREATE TABLE face_cluster_members (cluster_id INTEGER, face_detection_id INTEGER,
                                           distance_to_centroid REAL);
        INSERT INTO face_clusters (id, centroid, member_count) VALUES (1, '[]', 2);
    """)
    dims = wf.embedding_models.dims(wf.ACTIVE_MODEL)
    for i in range(1, 6):
        c.execute("INSERT INTO face_detections VALUES (?, NULL, 'Automatic')", (i,))
        c.execute('INSERT INTO face_embeddings VALUES (?, ?, ?)',
                  (i, wf.ACTIVE_MODEL_ID, json.dumps([1.0] * dims)))
    c.commit()
    monkeypatch.setattr(wf, 'CLUSTER_MAX_FACES', 4)
    out = wf.build_face_clusters(db)
    assert out['skipped'] is True
    assert c.execute('SELECT COUNT(*) FROM face_clusters').fetchone()[0] == 1
    c.close()
