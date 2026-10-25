#!/usr/bin/env python3
"""
Point a catalog at a different face-embedding model.

    tools/migrate_embedding_model.py <workflow_results.db> --to SFace
    tools/migrate_embedding_model.py <workflow_results.db> --to SFace --apply

Dry run unless --apply. See documentation/specs/face-recognition-migration-spec.md.

WHAT IT DOES
    1. records the catalog's choice in catalog_properties
    2. deletes the rows derived from the old model's distances
    3. clears the old model's embeddings

WHAT IT DOES NOT TOUCH, EVER
    face_detections - every box, and every person_id.

    A name is a judgement somebody made; an embedding is a measurement a model
    made. This migration invalidates measurements and must leave judgements
    exactly where they were. The script counts named detections before and
    after and refuses to report success if that number moved.

AFTERWARDS
    Re-run the chain: Ingest Manual Faces -> Face Recognition -> Additional ML
    -> Contextual. The references re-embed first, then scoring proceeds as it
    would on a fresh catalog. Nothing re-reads an original photograph: the
    crops in ai_face_contents are what get embedded.

    Then re-tune the threshold. documentation/tests/bench_threshold.py measures
    it; embedding_models.py carries it; it is PROVISIONAL for any model whose
    'measured' flag is false.
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__))), 'systems', 'backend'))

import embedding_models                                      # noqa: E402
from face_recognition_schema import ensure_face_recognition_schema   # noqa: E402


# Everything derived from a distance, and therefore meaningless once the model
# that produced the distances changes. Ordered children-first so that a run
# against a database with foreign keys enabled behaves too.
DERIVED_TABLES = (
    'face_similarity_scores_audit',
    'face_similarity_scores',
    'face_cluster_members',
    'face_clusters',
    'person_cooccurrence',
    'auto_tag_audit',
)


def _table_exists(conn, name):
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _has_column(conn, table, column):
    return any(r[1] == column
               for r in conn.execute(f'PRAGMA table_info({table})'))


def explain_busy(database, paused):
    """How to quiet a catalog, in the order that actually works."""
    print('  To migrate, quiet the catalog first - in this order, because')
    print('  stopping a workflow without pausing the queue just starts the next:')
    print()
    if not paused:
        print('    1. pause the queue')
        print(f'       sqlite3 {database} \\')
        # One statement, on one line: two quoted arguments would be two
        # separate statements to sqlite3 and the ON CONFLICT clause would
        # be a syntax error on its own.
        print("         \"INSERT INTO queue_settings (key,value) "
              "VALUES ('paused','1') ON CONFLICT(key) "
              "DO UPDATE SET value='1';\"")
    else:
        print('    1. the queue is already paused')
    print('    2. stop whatever is running, from Systems -> Workflows, and wait')
    print('       for the process to exit (pgrep -af workflows/)')
    print('    3. run this again with --apply')
    print('    4. afterwards, unpause:')
    print(f'       sqlite3 {database} \\')
    print("         \"UPDATE queue_settings SET value='0' WHERE key='paused';\"")
    print()
    print('  --force skips this check. It is there for a catalog whose')
    print('  workflow_executions row says running but whose process is gone;')
    print('  it is not a way to migrate a live catalog.')
    print()


def busy_with(conn):
    """
    What is using this catalog right now.

    Checked before anything is written, because the alternative is what
    happened on 2026-10-08: the tool waited out its 120 s busy timeout behind a
    running Additional ML Face Recognition and then died on the first DELETE,
    having already committed the schema half of the job. Nothing was lost - the
    deletes were in a transaction that rolled back - but a migration that
    half-runs and reports a traceback is not a migration anybody should have to
    reason about.

    Stopping one workflow is not enough on its own: the queue hands straight on
    to the next in the chain, so the queue has to be paused as well.
    """
    reasons = []
    if _table_exists(conn, 'workflow_executions'):
        for (n,) in conn.execute(
                "SELECT COUNT(*) FROM workflow_executions WHERE status = 'running'"):
            if n:
                rows = conn.execute(
                    "SELECT id, workflow_id, processed_files FROM workflow_executions "
                    " WHERE status = 'running' ORDER BY id").fetchall()
                for r in rows:
                    reasons.append(f'execution {r[0]} is running '
                                   f'({r[2] or 0} files processed)')
    if _table_exists(conn, 'workflow_queue'):
        for (n,) in conn.execute(
                "SELECT COUNT(*) FROM workflow_queue "
                " WHERE status IN ('queued', 'blocked', 'running')"):
            if n:
                reasons.append(f'{n} item(s) waiting or running in the queue')
    paused = False
    if _table_exists(conn, 'queue_settings'):
        row = conn.execute(
            "SELECT value FROM queue_settings WHERE key = 'paused'").fetchone()
        paused = bool(row and row[0] == '1')
    return reasons, paused


def survey(conn):
    """What is in this catalog now, per model, plus the things to preserve."""
    out = {'embeddings': {}, 'references': {}, 'derived': {},
           'needs_columns': False}
    for table, key in (('ai_face_contents', 'embeddings'),
                       ('face_contents', 'references')):
        if not _table_exists(conn, table):
            continue
        # A catalog that has never run the provenance migration has no such
        # column, and a survey must still work there - that is precisely the
        # catalog somebody runs a dry run against first.
        if not _has_column(conn, table, 'embedding_model_id'):
            out['needs_columns'] = True
            n = conn.execute(f'SELECT COUNT(*) FROM {table} '
                             f' WHERE embedding IS NOT NULL').fetchone()[0]
            if n:
                out[key][embedding_models.LEGACY_MODEL] = n
            continue
        for mid, n in conn.execute(
                f'SELECT COALESCE(embedding_model_id, 1), COUNT(*) FROM {table} '
                f' WHERE embedding IS NOT NULL GROUP BY 1'):
            try:
                out[key][embedding_models.model_name(mid)] = n
            except ValueError:
                out[key][f'unknown-id-{mid}'] = n
    for table in DERIVED_TABLES:
        if _table_exists(conn, table):
            out['derived'][table] = conn.execute(
                f'SELECT COUNT(*) FROM {table}').fetchone()[0]
    out['named'] = conn.execute(
        'SELECT COUNT(*) FROM face_detections WHERE person_id IS NOT NULL'
    ).fetchone()[0] if _table_exists(conn, 'face_detections') else 0
    out['detections'] = conn.execute(
        'SELECT COUNT(*) FROM face_detections').fetchone()[0] \
        if _table_exists(conn, 'face_detections') else 0
    return out


def report(label, s):
    print(f'  {label}')
    print(f'    detections {s["detections"]:,}, of which named {s["named"]:,}'
          '   <- never touched')
    print(f'    embeddings: {s["embeddings"] or "none"}')
    print(f'    references: {s["references"] or "none"}')
    nonzero = {k: v for k, v in s['derived'].items() if v}
    print(f'    derived:    {nonzero or "none"}')


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('database', help="a catalog's workflow_results.db")
    ap.add_argument('--to', required=True, choices=sorted(embedding_models.MODELS),
                    help='the model this catalog should use from now on')
    ap.add_argument('--apply', action='store_true',
                    help='actually do it; otherwise this is a dry run')
    ap.add_argument('--force', action='store_true',
                    help='migrate even though the catalog looks busy; see the '
                         'note this prints when it refuses')
    args = ap.parse_args()

    if not os.path.exists(args.database):
        sys.exit(f'no such database: {args.database}')

    target = args.to
    target_id = embedding_models.model_id(target)
    spec = embedding_models.info(target)

    # The schema is NOT ensured here. Two reasons, both learned the hard way:
    #
    #   a dry run must write nothing at all, not "nothing except some additive
    #   idempotent schema" - so survey() works without those columns;
    #
    #   and --apply must decide whether the catalog is busy BEFORE it writes
    #   anything, or a refusal still leaves half the job done. The first
    #   version of this check sat after the ensure and did exactly that.
    #
    # It happens further down, once the busy check has passed.
    conn = sqlite3.connect(args.database, timeout=120)
    try:
        current = embedding_models.active_model(args.database)
        before = survey(conn)

        print(f'\n{args.database}')
        print(f'  current model: {current}')
        print(f'  target model:  {target} ({spec["dims"]}-d, id {target_id}, '
              f'licence {spec["licence"]})')
        if not spec['match_distance_measured']:
            print(f'  ! {target}\'s match threshold ({spec["match_distance"]}) is '
                  f'PROVISIONAL - measure it with bench_threshold.py after this')
        print()
        report('before:', before)

        stale = {m: n for m, n in before['embeddings'].items() if m != target}
        stale_refs = {m: n for m, n in before['references'].items() if m != target}
        to_delete = sum(v for v in before['derived'].values())

        print()
        print(f'  would clear {sum(stale.values()):,} embedding(s) '
              f'{stale or ""} and {sum(stale_refs.values()):,} reference '
              f'embedding(s) {stale_refs or ""}')
        print(f'  would delete {to_delete:,} derived row(s)')
        print(f'  would set catalog_properties[embedding_model] = {target}')
        if before['needs_columns']:
            print('  would add the embedding_model_id columns and the '
                  'face_embeddings view (this catalog has not seen them)')

        busy, paused = busy_with(conn)
        print(f'  queue: {"PAUSED" if paused else "running"}')
        for b in busy:
            print(f'  busy:  {b}')

        if not args.apply:
            print('\n  DRY RUN - nothing written. Re-run with --apply.\n')
            if busy:
                print('  NOTE: --apply would refuse while the catalog is busy.'
                      ' See below.\n')
                explain_busy(args.database, paused)
            return 0

        if busy and not args.force:
            print('\n  REFUSING: this catalog is busy, and a migration that '
                  'competes with a\n  running workflow either blocks until its '
                  'timeout or deletes rows the\n  workflow is still writing.\n')
            explain_busy(args.database, paused)
            return 2

        # Now, and not before: the catalog is quiet and we are committed.
        if before['needs_columns'] or not _table_exists(conn, 'embedding_models'):
            print('\n  ensuring the provenance schema...')
            conn.close()
            ensure_face_recognition_schema(args.database)
            conn = sqlite3.connect(args.database, timeout=120)

        print('\n  applying...')
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('BEGIN')
        for table in DERIVED_TABLES:
            if _table_exists(conn, table):
                n = conn.execute(f'DELETE FROM {table}').rowcount
                if n:
                    print(f'    {table}: {n:,} deleted')
        for table in ('ai_face_contents', 'face_contents'):
            if not _table_exists(conn, table):
                continue
            # Only the other model's rows. An embedding already under the
            # target model survives, which is what makes a resumed run cheap
            # instead of starting over.
            n = conn.execute(
                f'UPDATE {table} SET embedding = NULL, embedding_model_id = NULL '
                f' WHERE embedding IS NOT NULL '
                f'   AND COALESCE(embedding_model_id, 1) <> ?', (target_id,)).rowcount
            if n:
                print(f'    {table}: {n:,} embedding(s) cleared')
        conn.commit()

        embedding_models.set_active_model(args.database, target)

        after = survey(conn)
        print()
        report('after:', after)

        if after['named'] != before['named']:
            print(f'\n  FAILED: named detections went from {before["named"]} to '
                  f'{after["named"]}. That must never happen; investigate before '
                  f'running the workflows.')
            return 1

        print(f'\n  done. {after["named"]:,} named detection(s) preserved.')
        print('  next: Ingest Manual Faces -> Face Recognition -> Additional ML '
              '-> Contextual')
        if not spec['match_distance_measured']:
            print(f'  then: measure {target}\'s threshold and set '
                  f"'measured': True in embedding_models.py")
        print()
        return 0
    finally:
        conn.close()


if __name__ == '__main__':
    sys.exit(main())
