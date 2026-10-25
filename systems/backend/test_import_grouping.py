"""
The bucketed find_groups must give exactly what the exhaustive version gave
(documentation/features/parallelism.md section 5.3.1).

    python -m pytest test_import_grouping.py
"""

import os
import random
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workflows'))

from lib.grouping import find_groups, pair_reason  # noqa: E402

RAW = {'.cr3', '.cr2', '.nef', '.arw', '.dng'}


def is_raw(p):
    return os.path.splitext(p)[1].lower() in RAW


def is_image(p):
    return os.path.splitext(p)[1].lower() in RAW | {'.jpg', '.heic', '.png'}


def find_groups_exhaustive(paths, keys_by_path, is_raw, is_image, log=print):
    """The pre-bucketing implementation, verbatim apart from the name."""
    candidates = [p for p in paths if is_image(p)]
    used = set()
    groups = []
    candidates.sort()
    for i, a in enumerate(candidates):
        if a in used:
            continue
        matches = []
        for b in candidates[i + 1:]:
            if b in used:
                continue
            reason = pair_reason(a, keys_by_path.get(a, {}), b,
                                 keys_by_path.get(b, {}), is_raw)
            if reason:
                matches.append((b, reason))
        if not matches:
            continue
        if len(matches) > 1:
            continue
        b, (rule, evidence) = matches[0]
        primary, secondary = (b, a) if is_raw(a) else (a, b)
        used.add(a)
        used.add(b)
        groups.append({'primary': primary, 'secondary': secondary,
                       'kind': 'raw_jpeg', 'match_rule': rule,
                       'match_evidence': evidence})
    return groups


def _random_batch(rng, n):
    # A small vocabulary on purpose, so collisions - pairs, triples, near
    # misses, conflicting makes - happen often.
    paths, keys = [], {}
    for k in range(n):
        stem = f'IMG_{rng.randint(0, n // 3):04d}'
        folder = rng.choice(['a', 'b', 'c'])
        ext = rng.choice(['.jpg', '.jpg', '.cr3', '.heic', '.png', '.txt'])
        p = f'/src/{folder}/{stem}{ext}'
        if p in keys:
            continue
        paths.append(p)
        keys[p] = {
            'serial': rng.choice([None, 'S1', 'S2']),
            'shutter_count': rng.choice([None, str(rng.randint(1, n // 4 + 1))]),
            'subsec': rng.choice([None, None, f'2020:01:01 10:00:0{rng.randint(0, 3)}.5']),
            'datetime': rng.choice([None, f'2020:01:01 10:00:0{rng.randint(0, 3)}']),
            'make': rng.choice([None, 'Canon', 'Canon', 'Nikon']),
            'model': rng.choice([None, 'R5']),
        }
        if rng.random() < 0.1:
            keys.pop(p)                  # a file exiftool could not read
    rng.shuffle(paths)
    return paths, keys


def test_bucketed_matches_exhaustive():
    rng = random.Random(1234)
    for _ in range(1000):
        paths, keys = _random_batch(rng, rng.randint(1, 60))
        quiet = lambda *a, **k: None  # noqa: E731
        assert (find_groups(paths, keys, is_raw, is_image, log=quiet)
                == find_groups_exhaustive(paths, keys, is_raw, is_image))


def test_scales_linearly():
    # 20k images in 10k RAW+JPG pairs: the exhaustive version would make
    # 2x10^8 comparisons here. The bucketed one should take well under a second.
    import time
    paths, keys = [], {}
    for k in range(10000):
        for ext in ('.jpg', '.cr3'):
            p = f'/src/IMG_{k:05d}{ext}'
            paths.append(p)
            keys[p] = {'serial': 'S1', 'shutter_count': str(k)}
    started = time.monotonic()
    groups = find_groups(paths, keys, is_raw, is_image)
    assert len(groups) == 10000
    assert time.monotonic() - started < 5
