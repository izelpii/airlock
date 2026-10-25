#!/usr/bin/env python3
"""
Embed Faces — turn every face crop into a vector, once.

WHY THIS IS ITS OWN WORKFLOW
----------------------------
It was not one. Embedding happened as a SIDE EFFECT of scoring, inside the
per-face loop of ai_face_recognition_workflow.py and
ai_ml_faces_recognition_workflow.py. Three things followed from that, and all
three were felt:

  1. It only advanced while somebody tolerated a full-catalog scoring run. On
     `family` the catalog reached 4,455 of 445,684 crops - 1 % - over the whole
     life of the feature, because every run was stopped before it got far.
  2. The cost landed on whoever tagged a face. Confirming three faces raised
     face_confirmed, which started a chain, which discovered 413,865 missing
     embeddings and set about computing them at 36 ms each. Four hours, behind
     a click that looked instant.
  3. Nothing could report the shortfall, because no component owned it.

Separating it fixes all three: the work becomes something you start, watch and
stop, and recognition can say "413,865 faces have no embedding" instead of
quietly paying for them.

RESUMING, WHICH IS THE WHOLE POINT
----------------------------------
Stopping this must never cost more than the one crop in flight, and running it
again must never redo a crop that is done.

There is NO watermark column, and that is deliberate. The embedding IS the
watermark:

    WHERE embedding IS NULL OR embedding_model_id IN (<every other model>)

A crop embedded under the active model cannot be selected again. Compare
file_contents.pets_scanned_at, which needs a column precisely because "looked
at, found nothing" leaves no trace - here, the result of the work is the record
of the work, so the two cannot disagree.

Each embedding is committed as it is made rather than batched into one
transaction at the end. A kill -9 then costs one crop, not an hour; the
alternative looks faster right up to the moment somebody stops it.

The query is written to be INDEXABLE, which matters because it runs on every
trigger. `embedding_model_id != 2` and `OR` cannot use an index and scan the
whole table - 0.18 s on 469,846 rows today, and worse as embeddings add ~1.25 GB
to it. Enumerating the other model ids uses idx_ai_face_cont_emb_model, and the
partial index below covers the NULL case while shrinking to nothing as the work
completes, so a finished catalog answers in no measurable time.

See documentation/specs/face-workflow-optimization.md.
"""

import json
import os
import sqlite3
import sys
import time
from typing import List, Optional

import requests

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import embedding_models                                     # noqa: E402
from catalog import Catalog                                 # noqa: E402
from face_recognition_schema import ensure_face_recognition_schema  # noqa: E402
from lib.stopping import stop_requested, record_stopped, stop_message  # noqa: E402

DEEPFACE_EMBED_URL = (os.environ.get('DEEPFACE_URL', 'http://deepface.airlock.lan')
                      + '/api/embed')
EMBED_TIMEOUT = float(os.environ.get('FACE_EMBED_TIMEOUT', '30'))

# How many consecutive failures before giving up. A crop that will not embed is
# skipped and the run continues - one unreadable file must not stop 400,000 -
# but a detector that is DOWN fails on everything, and grinding through the
# whole catalog to discover that wastes hours and writes nothing.
MAX_CONSECUTIVE_FAILURES = int(os.environ.get('FACE_EMBED_MAX_FAILURES', '25'))

# Commit every N embeddings. 1 would be safest and is too slow on 400,000 rows;
# this bounds what a stop can cost to N crops, which at 36 ms each is under a
# second of lost work.
COMMIT_EVERY = int(os.environ.get('FACE_EMBED_COMMIT_EVERY', '25'))


def _wal_pragmas(conn, timeout_ms: int = 30000):
    """Best-effort connection pragmas. journal_mode is never allowed to raise."""
    try:
        conn.execute('PRAGMA busy_timeout=%d' % timeout_ms)
    except sqlite3.Error:
        pass
    try:
        conn.execute('PRAGMA journal_mode=WAL')
    except sqlite3.Error:
        pass


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
    """
    Never raises: usually called from an exception handler.

    Without this a finished run stays 'running' for ever - a phantom in the
    queue that nothing clears, which is worse than reporting no progress at
    all. The backend does not write it; a subprocess workflow records its own
    terminal status.
    """
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


def ensure_backfill_index(conn) -> None:
    """
    The two indexes the work-list query needs. Both, because it has two halves.

    idx_ai_face_needs_embedding is PARTIAL - it holds only `embedding IS NULL`
    rows, so it shrinks as the backfill progresses and is empty when it
    finishes, which is what makes "is there anything to do?" free on a caught-up
    catalog however often the trigger fires.

    idx_ai_face_cont_emb_model covers the other half, finding crops embedded
    under a model that is no longer the active one. It existed on the reference
    deployment and in NO SCHEMA FILE - created by hand during the SFace
    migration and never written down, so every other catalog lacked it and this
    query would have silently scanned the whole table there. Created here so
    that the docstring above is true everywhere rather than on one machine.
    """
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ai_face_needs_embedding '
                 '  ON ai_face_contents(id) WHERE embedding IS NULL')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_ai_face_cont_emb_model '
                 '  ON ai_face_contents(embedding_model_id)')


def _other_model_ids(active_id: int) -> List[int]:
    """
    Every model id that is not the active one, so the query can use IN.

    Read from the registry rather than hard-coded, so adding a third model does
    not silently leave its embeddings in place as though they were usable.
    """
    return sorted(m['id'] for m in embedding_models.MODELS.values()
                  if m['id'] != active_id)


def crops_needing_embedding(conn, active_id: int, limit: Optional[int] = None):
    """
    Crops with no usable embedding, oldest first.

    `IN (other ids)` rather than `!= active`, because the first can use
    idx_ai_face_cont_emb_model and the second cannot.
    """
    others = _other_model_ids(active_id)
    marks = ','.join('?' * len(others)) if others else 'NULL'
    sql = (f'SELECT id, file_path, face_detection_id '
           f'  FROM ai_face_contents '
           f' WHERE embedding IS NULL '
           f'    OR embedding_model_id IN ({marks}) '
           f' ORDER BY id')
    if limit:
        sql += f' LIMIT {int(limit)}'
    return conn.execute(sql, others).fetchall()


def count_needing_embedding(conn, active_id: int) -> int:
    others = _other_model_ids(active_id)
    marks = ','.join('?' * len(others)) if others else 'NULL'
    return conn.execute(
        f'SELECT COUNT(*) FROM ai_face_contents '
        f' WHERE embedding IS NULL OR embedding_model_id IN ({marks})',
        others).fetchone()[0]


def embed_one(path: str, model: str):
    """
    One crop's vector as (vector, reason). Never raises.

    THE REASON IS THE POINT. The first version returned a bare None, so when
    25 crops failed in a row on 2026-10-09 the run gave up with a guess -
    "the deepface service is almost certainly down or serving a different
    model" - and the service was up, logging nothing, and embedding the very
    next crop by hand perfectly well. A four-hour job died and nothing
    anywhere recorded why.
    """
    try:
        with open(path, 'rb') as fh:
            r = requests.post(DEEPFACE_EMBED_URL,
                              files={'face_image': ('face.jpg', fh, 'image/jpeg')},
                              data={'model': model},
                              timeout=EMBED_TIMEOUT)
        if r.status_code != 200:
            return None, f'HTTP {r.status_code}: {r.text[:120]}'
        payload = r.json() or {}
        served = payload.get('model')
        if served and served != model:
            # Storing this would put two embedding spaces in one column, which
            # is the whole thing embedding_models exists to prevent.
            return None, f'asked for {model}, served {served}'
        vec = payload.get('embedding')
        if not vec:
            return None, 'the response carried no embedding'
        embedding_models.check_embedding(vec, model, 'face crop')
        return vec, None
    except embedding_models.EmbeddingModelMismatch as e:
        return None, str(e)
    except requests.exceptions.Timeout:
        return None, f'timed out after {EMBED_TIMEOUT:.0f}s'
    except requests.exceptions.RequestException as e:
        return None, f'{type(e).__name__}: {e}'
    except Exception as e:                      # noqa: BLE001
        return None, f'{type(e).__name__}: {e}'


def embed_with_retries(path: str, model: str, attempts: int = 3):
    """
    (vector, reason), retrying a failure before believing it.

    A four-hour job must not die of a hiccup. The run that prompted this had
    the service up throughout and logging nothing, so whatever those 25
    failures were, they passed - the very next crop embedded first time by
    hand. Without a retry, one bad second costs the rest of the pass.

    Short, bounded backoff: a crop that is genuinely unembeddable - a corrupt
    file, a face the model cannot read - must still be given up on quickly,
    because there may be thousands of them.
    """
    reason = None
    for attempt in range(attempts):
        vec, reason = embed_one(path, model)
        if vec is not None:
            return vec, None
        if attempt < attempts - 1:
            time.sleep(0.5 * (2 ** attempt))      # 0.5s, then 1s
    return None, reason


def execute(source_dir, target_dir, files, progress_callback,
            catalog_id=None, catalog=None, execution_id=None):
    """Embed every face crop that has no vector under this catalog's model."""
    if catalog is None and catalog_id is not None:
        catalog = Catalog.get_by_id(catalog_id)
    if catalog is None:
        raise ValueError('Either catalog or catalog_id must be provided')

    catalog_db_path = os.path.join(target_dir, 'workflow_results.db')
    ensure_face_recognition_schema(catalog_db_path)

    model = embedding_models.active_model(catalog_db_path)
    model_id = embedding_models.model_id(model)
    print(f'Embedding model: {model} ({embedding_models.dims(model)}-d, '
          f'id {model_id})')

    conn = sqlite3.connect(catalog_db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute('PRAGMA busy_timeout=60000')
        try:
            conn.execute('PRAGMA journal_mode=WAL')
        except sqlite3.Error:
            pass
        ensure_backfill_index(conn)
        conn.commit()

        total = count_needing_embedding(conn, model_id)
        print(f'{total:,} crop(s) need an embedding under {model}')
        # Before the first embedding, so the UI has a denominator from the
        # start rather than 0/0 until the first fifty are done.
        if progress_callback:
            progress_callback(0, total)
        if total == 0:
            # The common case once the catalog is caught up, and it has to be
            # cheap: this is reached on every import.
            print('Nothing to do - every crop already has one.')
            return {'embedded': 0, 'skipped': 0, 'failed': 0, 'remaining': 0}

        rows = crops_needing_embedding(conn, model_id)
        embedded = failed = missing = 0
        consecutive = 0
        # Distinct failure reasons, in order first seen. Printed once each
        # rather than per crop - 400,000 identical lines is not a log - and
        # quoted back in the error if the run gives up.
        recent_reasons = []
        started = time.time()

        for i, row in enumerate(rows, start=1):
            if execution_id and stop_requested(catalog_db_path, execution_id):
                # Everything committed so far stays. The next run picks up
                # exactly here, because the embeddings themselves are the
                # record of what is done.
                conn.commit()
                remaining = total - embedded
                print(f'\nStop requested - {embedded:,} embedded, '
                      f'{remaining:,} still to do. Nothing is lost; running '
                      f'this again continues from here.')
                record_stopped(catalog_db_path, execution_id,
                               processed_files=embedded, skipped_files=missing,
                               message=stop_message(embedded, total))
                return {'embedded': embedded, 'skipped': missing,
                        'failed': failed, 'remaining': remaining,
                        'stopped': True}

            path = row['file_path']
            if path and not os.path.isabs(path):
                path = os.path.join(target_dir, path)
            if not path or not os.path.isfile(path):
                missing += 1
                continue

            vec, why = embed_with_retries(path, model)
            if vec is None:
                failed += 1
                consecutive += 1
                if why and why not in recent_reasons:
                    recent_reasons.append(why)
                    print(f'  ! {os.path.basename(path)}: {why}')
                if consecutive >= MAX_CONSECUTIVE_FAILURES:
                    conn.commit()
                    # Say WHAT happened, not what probably happened.
                    detail = ('; '.join(recent_reasons[-3:])
                              if recent_reasons else 'no reason was recorded')
                    raise RuntimeError(
                        f'{consecutive} crops failed to embed in a row, each '
                        f'after 3 attempts - stopping. {embedded:,} embeddings '
                        f'are committed and re-running loses none of them. '
                        f'What the failures said: {detail}. '
                        f'Endpoint: {DEEPFACE_EMBED_URL}')
                continue
            consecutive = 0

            conn.execute(
                'UPDATE ai_face_contents SET embedding = ?, embedding_model_id = ? '
                ' WHERE id = ?',
                (json.dumps(vec), model_id, row['id']))
            embedded += 1

            if embedded % COMMIT_EVERY == 0:
                conn.commit()
            if progress_callback and (i % 50 == 0 or i == total):
                # COMMIT FIRST. progress() writes workflow_executions on a
                # connection of its own, and commits above follow `embedded`
                # while this follows `i` - in step only until one crop is
                # missing or fails. After that most reports came with up to
                # 24 updates uncommitted, and each waited out its 30 s busy
                # timeout behind this process's own transaction and failed:
                # 30 s lost per 50 crops, and a counter that stopped moving.
                conn.commit()
                progress_callback(i, total)
            if embedded % 1000 == 0:
                rate = embedded / max(0.001, time.time() - started)
                left = (total - embedded) / max(0.001, rate)
                print(f'  {embedded:,}/{total:,}  {rate:.0f}/s  '
                      f'~{left / 60:.0f} min left')

        conn.commit()
        if progress_callback:
            progress_callback(total, total)

        remaining = count_needing_embedding(conn, model_id)
        el = time.time() - started
        print(f'\n{"=" * 60}')
        print(f'Embedded {embedded:,} crop(s) in {el / 60:.1f} min')
        if missing:
            print(f'  {missing:,} crop file(s) were not on disk and were skipped')
        if failed:
            print(f'  {failed:,} failed to embed and will be retried next run')
            for why in recent_reasons[:5]:
                print(f'      {why}')
        print(f'  {remaining:,} still without an embedding')
        print(f'{"=" * 60}')
        return {'embedded': embedded, 'skipped': missing, 'failed': failed,
                'remaining': remaining}
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse

    try:
        import faulthandler
        import signal
        faulthandler.enable()
        if hasattr(faulthandler, 'register'):
            faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception as _fh:
        print(f'  (no faulthandler: {_fh})')

    ap = argparse.ArgumentParser(description='Embed face crops (subprocess mode)')
    ap.add_argument('--execution-id', type=int, required=True)
    ap.add_argument('--target-dir', type=str, required=True)
    ap.add_argument('--catalog-id', type=int, required=True)
    a = ap.parse_args()

    catalog_db_path = os.path.join(a.target_dir, 'workflow_results.db')

    cat = Catalog.get_by_id(a.catalog_id)
    if cat is None:
        print(f'ERROR: Catalog {a.catalog_id} not found', file=sys.stderr)
        _db_complete(catalog_db_path, a.execution_id, 'failed',
                     f'Catalog {a.catalog_id} not found')
        sys.exit(1)

    # The first version of this passed None here and wrote no terminal status.
    # The run worked - 22 embeddings a second - and the UI showed 0 of 0 for
    # ever, because NOTHING in the backend writes those columns for a
    # subprocess: a workflow records its own progress and its own ending.
    def subprocess_progress(processed, total=None):
        _db_update_progress(catalog_db_path, a.execution_id, processed,
                            total if total is not None else processed)

    try:
        out = execute(None, a.target_dir, None, subprocess_progress,
                      catalog_id=a.catalog_id, catalog=cat,
                      execution_id=a.execution_id)
        print(out)
        if not (isinstance(out, dict) and out.get('stopped')):
            _db_complete(catalog_db_path, a.execution_id, 'completed',
                         skipped_files=(out.get('skipped', 0)
                                        if isinstance(out, dict) else 0))
            print(f'Subprocess execution {a.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, a.execution_id, 'failed', str(exc))
        sys.exit(1)
