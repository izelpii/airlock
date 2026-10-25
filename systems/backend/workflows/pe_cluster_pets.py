#!/usr/bin/env python3
"""
Group Unknown Pets: gather unnamed animals that look like the same one.

A GROUP IS NOT A PET. No `people` row is created here, ever. A row in `people`
means somebody said who this is, and a detector that invented one per
unrecognised animal would fill the household with strangers - the pet form of
the defect location-spec.md section 4.8 exists to prevent, "never store a
coordinate nobody measured".

So this writes pet_clusters and pet_cluster_members, and nothing else. Naming
one is a separate, human act (POST /pets/clusters/<id>/assign), and THAT is
what creates the pet and writes person_id to every member - which is how
naming a dog once turns hundreds of photographs into "photos with Rex".

Reads ai_pet_contents.embedding for detections with person_id IS NULL. Does not
embed anything itself: that is Identify Pets' job, and this pass is cheap
enough to re-run whenever the user has named something.

See documentation/specs/pet-recognition-spec.md section 6.2.
"""

import json
import os
import sys
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

from lib.stopping import stop_requested, record_stopped, stop_message  # noqa: E402
from pet_schema import ensure_pet_schema  # noqa: E402

try:
    import numpy as np
except ImportError:                       # pragma: no cover
    np = None


# How close two crops must be to be called the same animal. Cosine distance on
# L2-normalised 512-d vectors, so 0 is identical and 1 is unrelated.
#
# Used twice: two crops this close are linked (connected_groups), and no
# member of a finished group may be further than this from its centre
# (trim_to_core).
#
# Measured on `family` (7,471 animals, one dog): linking alone chained 7,378
# of them into one group - Luna, a white shepherd, ~5,000 times, and ~2,000
# children, goats, other dogs, statues and cushions joined to her one near
# neighbour at a time; its furthest member was 1.09 from the centre. Sampled
# crops by distance from that centre: every one Luna up to 0.22, mostly her
# to ~0.30, mostly not by 0.35. Trimmed at 0.28 her group is 5,021 and clean.
#
# Deliberately tight: a group that splits one dog into two costs a second
# naming, where a group that merges two dogs costs a wrong name on every
# photograph of both.
CLUSTER_DISTANCE = float(os.environ.get('PET_CLUSTER_DISTANCE', '0.28'))

# A group of one is a detection. Offering it as a group to name wastes the
# attention the review page exists to spend well.
MIN_MEMBERS = int(os.environ.get('PET_CLUSTER_MIN_MEMBERS', '3'))


def _wal_pragmas(conn, timeout_ms: int = 30000):
    try:
        conn.execute('PRAGMA busy_timeout=%d' % timeout_ms)
    except sqlite3.Error:
        pass
    try:
        return conn.execute('PRAGMA journal_mode=WAL').fetchone()
    except sqlite3.Error:
        return None


def _db_update_progress(catalog_db_path: str, execution_id: int,
                        processed: int, total: int):
    """Never raises: progress reporting is not worth a job."""
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
            conn.execute('UPDATE workflow_executions SET processed_files=?, '
                         ' total_files=? WHERE id=?',
                         (processed, total, execution_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[progress] could not record {processed}/{total}: {exc}',
              file=sys.stderr)


def _db_complete(catalog_db_path: str, execution_id: int, status: str,
                 error_message: str = None, skipped_files: int = 0):
    """Never raises: usually called from an exception handler."""
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
            conn.execute('''UPDATE workflow_executions
                               SET status=?, completed_at=CURRENT_TIMESTAMP,
                                   error_message=?, skipped_files=?
                             WHERE id=?''',
                         (status, error_message, skipped_files, execution_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[complete] could not mark execution {execution_id} as {status}: '
              f'{exc}', file=sys.stderr)


def load_unnamed_embeddings(conn):
    """
    (ids, matrix, species) for every unnamed animal that has been measured.

    Length is checked as well as presence: a vector of the wrong size means
    something wrote an embedding without the model that was supposed to, and
    np.array on a ragged list either raises or silently makes an object array
    depending on the numpy version. Neither is a good way to find out.
    """
    rows = conn.execute('''
        SELECT apc.pet_detection_id AS id, apc.embedding, pd.species
          FROM ai_pet_contents apc
          JOIN pet_detections pd ON pd.id = apc.pet_detection_id
         WHERE apc.embedding IS NOT NULL
           AND pd.person_id IS NULL
         ORDER BY apc.pet_detection_id
    ''').fetchall()

    ids, vecs, species, wrong = [], [], [], 0
    expected = None
    for r in rows:
        try:
            v = json.loads(r['embedding'])
        except (TypeError, ValueError):
            continue
        if expected is None:
            expected = len(v)
        if len(v) != expected:
            wrong += 1
            continue
        ids.append(r['id'])
        vecs.append(v)
        species.append(r['species'])
    if wrong:
        print(f'  {wrong} embedding(s) of a different length were ignored')
    if not ids:
        return [], None, []
    m = np.asarray(vecs, dtype=np.float32)
    # Re-normalise rather than trust: cosine as a dot product is only valid on
    # unit vectors, and the service normalises but a hand-written row may not.
    norms = np.linalg.norm(m, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return ids, m / norms, species


def connected_groups(matrix, threshold):
    """
    Single-link grouping: ids that are within `threshold` of each other, joined
    transitively.

    Single-link because the alternative - demanding every pair in a group be
    close - splits one animal across lighting and age, which is the common case
    in a family catalog. The cost is chaining: A close to B, B close to C, and
    A nothing like C. That is why `cohesion` and `max_member_distance` are
    stored per group, and why the review page can raise the bar: a high
    max_member_distance is the signal that a group is two animals.
    """
    n = matrix.shape[0]
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(i, j):
        a, b = find(i), find(j)
        if a != b:
            parent[b] = a

    # One matmul for the whole similarity matrix. 512-d by a few thousand rows
    # is small; doing it pairwise in Python is what would make this slow.
    sim = matrix @ matrix.T
    close = (1.0 - sim) <= threshold
    for i in range(n):
        for j in np.nonzero(close[i, i + 1:])[0]:
            union(i, i + 1 + int(j))

    groups = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)
    return list(groups.values())


def trim_to_core(matrix, members, cap):
    """
    The members of a group within `cap` of its centre, the centre recomputed
    from what is kept until it settles (three passes at most - one or two in
    practice). Single-link grouping never asks whether A and C are alike, only
    whether a path of close neighbours joins them; this is the question it
    skips. What is cut is not discarded - group_animals regroups it.
    """
    members = list(members)
    sub = matrix[members]
    keep = np.ones(len(members), dtype=bool)
    for _ in range(3):
        if not keep.any():
            break
        centroid = sub[keep].mean(axis=0)
        n = np.linalg.norm(centroid)
        if n > 0:
            centroid = centroid / n
        new = (1.0 - sub @ centroid) <= cap
        if (new == keep).all():
            break
        keep = new
    return [m for m, k in zip(members, keep) if k]


def group_animals(matrix, threshold, min_members=None, rounds=4):
    """
    Groups of row indices: linked by single-link at `threshold`, each trimmed to
    the members within `threshold` of its centre, and what was trimmed grouped
    again among itself - so a second animal chained onto the first still gets
    a group of its own. Groups smaller than min_members are dropped.
    """
    min_members = MIN_MEMBERS if min_members is None else min_members
    pool = list(range(matrix.shape[0]))
    groups = []
    for _ in range(rounds):
        if len(pool) < min_members:
            break
        trimmed = []
        for g in connected_groups(matrix[pool], threshold):
            g = [pool[i] for i in g]
            if len(g) < min_members:
                continue
            kept = trim_to_core(matrix, g, threshold)
            if len(kept) >= min_members:
                groups.append(kept)
                trimmed.extend(set(g) - set(kept))
            else:
                trimmed.extend(g)
        if not trimmed or sorted(trimmed) == sorted(pool):
            break
        pool = sorted(trimmed)
    return groups


def execute(source_dir, target_dir, files, progress_callback,
            catalog_id=None, catalog=None, execution_id=None):
    """
    Group Unknown Pets workflow.

    Rebuilds UNRESOLVED groups and leaves resolved ones alone. A group somebody
    has named, or rejected, is a decision - exactly as an accepted location
    cluster is - and re-offering it every run would make the review page
    useless.

    Returns statistics about the run.
    """
    catalog_db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(catalog_db_path):
        raise FileNotFoundError(f'no catalog database at {catalog_db_path}')

    ensure_pet_schema(catalog_db_path)

    if np is None:
        # Declared in backend/requirements.txt, so this is a broken install
        # rather than an expected state - say so plainly and stop.
        raise RuntimeError('numpy is required to group pets and is not installed')

    conn = sqlite3.connect(catalog_db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    _wal_pragmas(conn)
    try:
        ids, matrix, species = load_unnamed_embeddings(conn)
        total = len(ids)
        print(f'{total} unnamed animal(s) with a measurement')
        if progress_callback:
            progress_callback(0, max(total, 1))

        if total < MIN_MEMBERS:
            print(f'  fewer than {MIN_MEMBERS} - nothing to group')
            return {'clusters': 0, 'grouped': 0, 'skipped': True}

        if execution_id and stop_requested(catalog_db_path, execution_id):
            record_stopped(catalog_db_path, execution_id)
            return {'stopped': True, 'message': stop_message()}

        groups = group_animals(matrix, CLUSTER_DISTANCE)
        print(f'  {len(groups)} group(s) of {MIN_MEMBERS}+ members')

        # A detection already in a surviving group must not be put in a second
        # one: pet_cluster_members.pet_detection_id is UNIQUE, and an insert
        # that violated it would abort the whole pass. Surviving means
        # resolved - named (resolved_person_id) or rejected (resolved_at with
        # no person); `resolved_at IS NULL` is an offered group, rebuilt below.
        #
        # A member of a NAMED group is spoken for only while it still carries
        # that pet's name. Unnaming a photograph (the gallery's "not this pet")
        # clears pet_detections.person_id and leaves the membership row, so
        # this used to keep it out of every later grouping for good: on
        # `family` the 2,357 photographs taken off Luna - chained into her
        # group before v5.4.25 - were all still members of it, and the next
        # run grouped 17 of 2,450 unnamed animals. Those stale memberships are
        # deleted below, in the same transaction as the rebuild.
        spoken_for = {r[0] for r in conn.execute(
            'SELECT m.pet_detection_id FROM pet_cluster_members m '
            '  JOIN pet_clusters c ON c.id = m.cluster_id '
            '  JOIN pet_detections pd ON pd.id = m.pet_detection_id '
            ' WHERE (c.resolved_person_id IS NULL AND c.resolved_at IS NOT NULL) '
            '    OR (c.resolved_person_id IS NOT NULL '
            '        AND pd.person_id = c.resolved_person_id)')}

        # COMPUTE FIRST, WRITE AFTER. This deleted the offered groups and then
        # inserted the new ones one by one, reporting progress after each - and
        # progress() writes workflow_executions on a connection of its own,
        # which waited out its 30 s busy timeout behind this one's open
        # transaction every time. On `family`: 30 s per group, the counter at
        # 0 for the whole run, and the catalog locked against every other
        # workflow meanwhile. Now nothing is written until every group is
        # known, and then it is one short transaction, so the page never sees
        # half a rebuild either.
        pending = []
        grouped = 0
        for members in groups:
            members = [i for i in members if ids[i] not in spoken_for]
            if len(members) < MIN_MEMBERS:
                continue

            sub = matrix[members]
            centroid = sub.mean(axis=0)
            cn = np.linalg.norm(centroid)
            if cn > 0:
                centroid = centroid / cn
            dists = 1.0 - (sub @ centroid)
            modal = None
            present = [species[i] for i in members if species[i]]
            if present:
                modal = max(set(present), key=present.count)
                if len(set(present)) > 1:
                    # Worth printing, but NOT the alarm it was first written
                    # as ("a signal the group is wrong"). Measured since: on
                    # eight photographs of one white shepherd the detector
                    # says dog six times and cat twice, so a mixed group is
                    # as likely to be one animal the model hedged on as two
                    # animals wrongly joined. The modal label is taken and
                    # the grouping itself stays on the embedding, which is
                    # the evidence; species is a hint (spec 3.2).
                    print(f'  - group of {len(members)} mixes species '
                          f'{sorted(set(present))}, taking {modal!r}')
            pending.append((centroid, members, dists, modal))
            grouped += len(members)
            if progress_callback:
                progress_callback(min(grouped, total), max(total, 1))

        made = 0
        with conn:
            # Named groups keep only the members still named for them, and
            # say how many that is.
            conn.execute('DELETE FROM pet_cluster_members WHERE id IN ('
                         ' SELECT m.id FROM pet_cluster_members m'
                         '   JOIN pet_clusters c ON c.id = m.cluster_id'
                         '   JOIN pet_detections pd ON pd.id = m.pet_detection_id'
                         '  WHERE c.resolved_person_id IS NOT NULL'
                         '    AND pd.person_id IS NOT c.resolved_person_id)')
            conn.execute('UPDATE pet_clusters SET member_count = '
                         ' (SELECT COUNT(*) FROM pet_cluster_members m'
                         '   WHERE m.cluster_id = pet_clusters.id)'
                         ' WHERE resolved_person_id IS NOT NULL')
            conn.execute('DELETE FROM pet_cluster_members WHERE cluster_id IN '
                         ' (SELECT id FROM pet_clusters WHERE resolved_person_id IS NULL '
                         '    AND resolved_at IS NULL)')
            conn.execute('DELETE FROM pet_clusters '
                         ' WHERE resolved_person_id IS NULL AND resolved_at IS NULL')
            for centroid, members, dists, modal in pending:
                cur = conn.execute('''INSERT INTO pet_clusters
                                       (centroid, member_count, cohesion,
                                        max_member_distance, species)
                                      VALUES (?,?,?,?,?)''',
                                   (json.dumps([round(float(v), 6) for v in centroid]),
                                    len(members), float(dists.mean()),
                                    float(dists.max()), modal))
                cid = cur.lastrowid
                conn.executemany('INSERT INTO pet_cluster_members (cluster_id, '
                                 ' pet_detection_id, distance_to_centroid) '
                                 ' VALUES (?,?,?)',
                                 [(cid, ids[i], float(d)) for i, d in zip(members, dists)])
                made += 1
        print(f'Grouped {grouped} of {total} unnamed animal(s) into {made} group(s)')
        return {'clusters': made, 'grouped': grouped, 'unnamed': total,
                'distance': CLUSTER_DISTANCE, 'min_members': MIN_MEMBERS}
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Group Unknown Pets workflow (subprocess mode)')
    parser.add_argument('--execution-id', type=int, required=True)
    parser.add_argument('--target-dir', type=str, required=True)
    parser.add_argument('--catalog-id', type=int, required=True)
    args = parser.parse_args()

    catalog_db_path = os.path.join(args.target_dir, 'workflow_results.db')

    def subprocess_progress(processed, total=None):
        _db_update_progress(catalog_db_path, args.execution_id, processed,
                            total if total is not None else processed)

    try:
        result = execute(source_dir=None, target_dir=args.target_dir, files=[],
                         progress_callback=subprocess_progress,
                         catalog_id=args.catalog_id,
                         execution_id=args.execution_id)
        if not (isinstance(result, dict) and result.get('stopped')):
            _db_complete(catalog_db_path, args.execution_id, 'completed')
        print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)
