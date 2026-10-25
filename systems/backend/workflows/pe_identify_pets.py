#!/usr/bin/env python3
"""
Identify Pets: crop each detected animal, measure it, score it against the
pets somebody has already named.

Four phases, each resumable:

  crop    cut the box out of the display copy and write it to cache/pets-ai,
          rescaling from the DETECTION dimensions. Those two columns exist
          because the boxes are in the detection image's space, not the
          original's; documentation/fixes/ has nine files about getting that
          chain wrong.
  embed   send the crops to backend-pets and persist the 512-d vectors. Stored,
          not recomputed: re-embedding on every run would make this pass cost
          what the detection pass costs, and detection is the expensive one.
  score   cosine distance from every unnamed animal to every NAMED one.
          Not filtered by species - the detector mislabels one animal's
          species often enough that gating on it loses real matches; see
          phase_score. Candidates, not decisions.
  assign  write person_id only where the best match is clearly best - see
          PET_MATCH_DISTANCE and PET_MATCH_MARGIN below.

WHY THERE IS NO pe_ingest_manual_pets.py, despite faces having one: a manual
face tag arrives as a region with no crop and no embedding, so
ai_ingest_manual_faces.py exists to build the reference set. Here every
detection is embedded in phase 2 whether it is named or not, so naming one IS
adding it to the reference set. One fewer workflow; do not add it for symmetry.

See documentation/specs/pet-recognition-spec.md section 6.1.
"""

import io
import json
import os
import sys
import sqlite3

_HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _HERE)
sys.path.insert(0, os.path.dirname(_HERE))

import requests  # noqa: E402
from PIL import Image, ImageOps  # noqa: E402

from lib.stopping import stop_requested, record_stopped, stop_message  # noqa: E402
from pet_schema import ensure_pet_schema  # noqa: E402

try:
    import numpy as np
except ImportError:                       # pragma: no cover
    np = None


PETS_URL = os.environ.get('PETS_URL', 'http://pets.airlock.lan')
EMBED_URL = PETS_URL.rstrip('/') + '/embed'
HEALTH_URL = PETS_URL.rstrip('/') + '/health'

CROP_DIR = os.path.join('cache', 'pets-ai')

# Context around the box. A tight crop of a dog's head is harder to match than
# one with some of the animal in it, and the embedder was trained on photographs
# rather than on boxes.
CROP_PAD = float(os.environ.get('PET_CROP_PAD', '0.12'))

# Below this cosine distance, and this much clearer than the runner-up, an
# unnamed animal is named automatically.
#
# BOTH PROVISIONAL AND DELIBERATELY TIGHT. A wrong auto-assignment is worse
# than an unnamed dog: the user has to notice it before they can undo it, where
# an unnamed dog is simply still in the review page. embedding_models.py makes
# the same argument about faces, and the pet equivalent has not been measured
# at all yet.
PET_MATCH_DISTANCE = float(os.environ.get('PET_MATCH_DISTANCE', '0.22'))
PET_MATCH_MARGIN = float(os.environ.get('PET_MATCH_MARGIN', '0.08'))

# ...and it has to be this close to the pet's CENTRE - the mean of every
# photograph of it - not merely to one of them.
#
# The nearest-photograph rule alone is the chaining that grouped 7,378 of
# `family`'s animals as Luna: anything within 0.22 of any ONE of her
# thousands of photographs qualified, and with only one pet the margin is
# always met. After the 2,357 photographs outside her core were unnamed, it
# would have put 834 of them straight back. Measured: every one of her 5,021
# core photographs is within 0.28 of her centre, and none of the 2,357 is.
PET_MATCH_CENTRE_DISTANCE = float(os.environ.get('PET_MATCH_CENTRE_DISTANCE', '0.28'))

EMBED_TIMEOUT = float(os.environ.get('PET_EMBED_TIMEOUT', '60'))


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


# ─── Phase 1: crops ──────────────────────────────────────────────────────────

def _display_path(conn, file_id, target_dir):
    """
    The image to crop from: the web-friendly derivative where one exists.

    Never the original if it can be helped. The originals include HEIC and RAW,
    and the derivative is already a plain JPEG of known orientation - so this
    is both faster and avoids a RAW decode per animal.
    """
    row = conn.execute('''
        SELECT fc.file_path, av.alternative_file_path
          FROM file_contents fc
          LEFT JOIN alternative_versions av
                 ON av.original_file_id = fc.id AND av.rung IN ('1200','2048')
         WHERE fc.id = ?
         ORDER BY av.width DESC
         LIMIT 1
    ''', (file_id,)).fetchone()
    if not row:
        return None
    rel = row['alternative_file_path'] or row['file_path']
    if not rel:
        return None
    return rel if os.path.isabs(rel) else os.path.join(target_dir, rel)


def crop_one(conn, det, target_dir):
    """
    One crop on disk, or None.

    The box is in the DETECTION image's coordinate space, which is why
    image_width/image_height are stored beside it: the display copy is a
    different size again, so the box is scaled by the ratio between them. This
    is the step that silently produces offset crops when it is wrong.
    """
    src = _display_path(conn, det['file_id'], target_dir)
    if not src or not os.path.isfile(src):
        return None

    img = Image.open(src)
    img = ImageOps.exif_transpose(img)
    if not os.path.realpath(src).startswith(
            os.path.join(os.path.realpath(target_dir), 'cache') + os.sep):
        # The original, so the Basic Editor's turn is not in its pixels yet
        # (basic-photo-editing.md 5.1); a display copy already carries it.
        from lib.orientation import apply_user, user_orientation
        img = apply_user(img, user_orientation(conn, det['file_id']))
    if img.mode != 'RGB':
        img = img.convert('RGB')

    sx = img.width / float(det['image_width'] or img.width)
    sy = img.height / float(det['image_height'] or img.height)
    x, y = det['x'] * sx, det['y'] * sy
    w, h = det['w'] * sx, det['h'] * sy
    pad = CROP_PAD * max(w, h)
    box = (max(0, int(x - pad)), max(0, int(y - pad)),
           min(img.width, int(x + w + pad)), min(img.height, int(y + h + pad)))
    if box[2] - box[0] < 8 or box[3] - box[1] < 8:
        img.close()
        return None

    crop = img.crop(box)
    img.close()

    out_dir = os.path.join(target_dir, CROP_DIR)
    os.makedirs(out_dir, exist_ok=True)
    name = f'pet_{det["id"]:09d}.jpg'
    abs_path = os.path.join(out_dir, name)
    crop.save(abs_path, 'JPEG', quality=92)
    crop.close()

    # Relative, because catalog_meta.target_directory is the only absolute path
    # the system keeps - which is what let both catalogs move machines today
    # without rewriting a single row.
    return os.path.join(CROP_DIR, name), abs_path


def phase_crop(conn, target_dir, progress=None):
    dets = conn.execute('''
        SELECT pd.* FROM pet_detections pd
         WHERE NOT EXISTS (SELECT 1 FROM ai_pet_contents apc
                            WHERE apc.pet_detection_id = pd.id)
         ORDER BY pd.id
    ''').fetchall()
    made = 0
    for i, det in enumerate(dets, start=1):
        try:
            result = crop_one(conn, det, target_dir)
        except Exception as e:
            print(f'  ! crop failed for detection {det["id"]}: {e}')
            continue
        if not result:
            continue
        rel, abs_path = result
        conn.execute('''INSERT OR IGNORE INTO ai_pet_contents
                          (file_path, file_name, file_size, pet_detection_id,
                           source_file_id)
                        VALUES (?,?,?,?,?)''',
                     (rel, os.path.basename(rel), os.path.getsize(abs_path),
                      det['id'], det['file_id']))
        made += 1
        if i % 50 == 0:
            # COMMIT FIRST. progress() writes workflow_executions on a
            # connection of its own, and while this one holds an open write
            # transaction that write waits out its whole busy timeout - 30 s
            # every 50 crops, progress never recorded - and every other
            # workflow on the catalog is locked out for the length of the
            # phase. phase_embed always did it in this order.
            conn.commit()
            if progress:
                progress(i, len(dets))
    conn.commit()
    return made, len(dets)


# ─── Phase 2: embeddings ─────────────────────────────────────────────────────

def embedder_available():
    try:
        r = requests.get(HEALTH_URL, timeout=10)
        if r.status_code != 200:
            return False, f'{HEALTH_URL} answered {r.status_code}'
        present = (r.json() or {}).get('present') or {}
        if not present.get('embed'):
            return False, ('backend-pets has no identity model. '
                           'animal_clip_vit_b32.onnx needs a one-off ONNX export '
                           'on a machine with torch - see '
                           'tools/fetch_pet_weights.sh')
        return True, None
    except Exception as e:
        return False, f'{HEALTH_URL} unreachable: {e}'


def phase_embed(conn, target_dir, progress=None):
    rows = conn.execute('''
        SELECT apc.id, apc.file_path, apc.pet_detection_id
          FROM ai_pet_contents apc
         WHERE apc.embedding IS NULL
         ORDER BY apc.id
    ''').fetchall()
    if not rows:
        return 0, 0

    done = 0
    for i, row in enumerate(rows, start=1):
        path = row['file_path']
        if not os.path.isabs(path):
            path = os.path.join(target_dir, path)
        if not os.path.isfile(path):
            continue
        try:
            with open(path, 'rb') as fh:
                r = requests.post(EMBED_URL, files={'image': ('crop.jpg', fh,
                                                              'image/jpeg')},
                                  timeout=EMBED_TIMEOUT)
            if r.status_code != 200:
                print(f'  ! embed {row["id"]}: HTTP {r.status_code}')
                continue
            vec = (r.json() or {}).get('embedding')
            if not vec:
                continue
        except Exception as e:
            print(f'  ! embed {row["id"]}: {e}')
            continue
        conn.execute('UPDATE ai_pet_contents SET embedding = ? WHERE id = ?',
                     (json.dumps(vec), row['id']))
        done += 1
        if i % 25 == 0:
            conn.commit()
            if progress:
                progress(i, len(rows))
    conn.commit()
    return done, len(rows)


# ─── Phases 3 and 4: scoring, and the few assignments it earns ───────────────

def _vectors(conn, named):
    """(detection_ids, matrix, species) for named or unnamed animals."""
    sql = '''
        SELECT apc.pet_detection_id AS id, apc.embedding, pd.species,
               pd.person_id
          FROM ai_pet_contents apc
          JOIN pet_detections pd ON pd.id = apc.pet_detection_id
         WHERE apc.embedding IS NOT NULL
           AND pd.person_id IS %s NULL
    ''' % ('NOT' if named else '')
    ids, vecs, species, people = [], [], [], []
    expected = None
    for r in conn.execute(sql):
        try:
            v = json.loads(r['embedding'])
        except (TypeError, ValueError):
            continue
        if expected is None:
            expected = len(v)
        if len(v) != expected:
            continue
        ids.append(r['id'])
        vecs.append(v)
        species.append(r['species'])
        people.append(r['person_id'])
    if not ids:
        return [], None, [], []
    m = np.asarray(vecs, dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    n[n == 0] = 1.0
    return ids, m / n, species, people


def phase_score(conn, execution_id, progress=None):
    """
    Distance from every unnamed animal to every named one.

    NOT restricted by species, and it used to be. That restriction read well -
    "scoring a cat crop against dog references is wasted work and a source of
    avoidable error" - and it was wrong twice over.

    Wrong on the saving: `sim = q_m @ ref_m.T` below computes EVERY pair
    anyway. The filter only chose which columns to look at afterwards, so it
    never saved an operation.

    Wrong on the error, which is the part that mattered. Measured on eight
    photographs of one white shepherd taken minutes apart, yolox_tiny calls it
    `dog` six times and `cat` twice - it hedges when the dog is lying down with
    its head turned. Gating on that label means the two `cat` crops are never
    compared against the dog the other six named, so they stay unidentified
    for ever: the user sees Rex found in six photographs and inexplicably
    absent from two near-identical ones, with nothing anywhere saying why.

    Species is a HINT recorded beside the box (§3.2), with a measured error
    rate of 25 % on one animal. The embedding is the evidence; the label is
    not, and must not gate anything.
    """
    ref_ids, ref_m, _ref_species, ref_people = _vectors(conn, named=True)
    if not ref_ids:
        print('  no named animals yet - nothing to score against')
        return 0, 0
    q_ids, q_m, _q_species, _ = _vectors(conn, named=False)
    if not q_ids:
        return 0, 0

    print(f'  scoring {len(q_ids)} unnamed against {len(ref_ids)} reference crop(s)')
    sim = q_m @ ref_m.T
    dist = 1.0 - sim

    # Each pet's centre, for the second test an automatic name has to pass.
    pets = sorted(set(ref_people))
    people_arr = np.asarray(ref_people)
    centres = np.stack([ref_m[people_arr == pid].mean(axis=0) for pid in pets])
    centres /= np.maximum(np.linalg.norm(centres, axis=1, keepdims=True), 1e-12)
    to_centre = 1.0 - q_m @ centres.T            # unnamed x pets
    pet_col = {pid: k for k, pid in enumerate(pets)}

    scored = assigned = 0
    for qi, q_id in enumerate(q_ids):
        # Best distance per candidate PET, not per reference crop: a pet with
        # forty reference photographs should not beat one with two just by
        # having more chances.
        best = {}
        for ri in range(len(ref_ids)):
            pid = ref_people[ri]
            d = float(dist[qi, ri])
            if pid not in best or d < best[pid]:
                best[pid] = d

        for pid, d in best.items():
            conn.execute('''INSERT INTO pet_similarity_scores
                              (pet_detection_id, person_id, cosine_distance,
                               execution_id)
                            VALUES (?,?,?,?)
                            ON CONFLICT(pet_detection_id, person_id)
                            DO UPDATE SET cosine_distance = excluded.cosine_distance,
                                          execution_id = excluded.execution_id''',
                         (q_id, pid, d, execution_id))
            scored += 1

        ranked = sorted(best.items(), key=lambda kv: kv[1])
        if (ranked and ranked[0][1] <= PET_MATCH_DISTANCE
                and to_centre[qi, pet_col[ranked[0][0]]] <= PET_MATCH_CENTRE_DISTANCE):
            runner_up = ranked[1][1] if len(ranked) > 1 else 10.0
            if runner_up - ranked[0][1] >= PET_MATCH_MARGIN:
                conn.execute('UPDATE pet_detections SET person_id = ? WHERE id = ?',
                             (ranked[0][0], q_id))
                assigned += 1
        if qi % 100 == 0:
            conn.commit()                    # first - see phase_crop
            if progress:
                progress(qi, len(q_ids))
    conn.commit()
    return scored, assigned


def execute(source_dir, target_dir, files, progress_callback,
            catalog_id=None, catalog=None, execution_id=None):
    """Identify Pets workflow. Returns statistics about the run."""
    catalog_db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(catalog_db_path):
        raise FileNotFoundError(f'no catalog database at {catalog_db_path}')

    ensure_pet_schema(catalog_db_path)
    if np is None:
        raise RuntimeError('numpy is required to score pets and is not installed')

    conn = sqlite3.connect(catalog_db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    _wal_pragmas(conn)
    try:
        print('Phase 1: crops')
        made, candidates = phase_crop(conn, target_dir, progress_callback)
        print(f'  {made} new crop(s) of {candidates} detection(s) without one')

        if execution_id and stop_requested(catalog_db_path, execution_id):
            record_stopped(catalog_db_path, execution_id)
            return {'stopped': True, 'message': stop_message()}

        print('Phase 2: embeddings')
        ok, why = embedder_available()
        if not ok:
            # Not a failure: crops are useful on their own, the groups page can
            # wait, and a missing model is a deployment state rather than a bug.
            print(f'  skipped - {why}')
            return {'crops': made, 'embedded': 0, 'scored': 0, 'assigned': 0,
                    'embedder': why}
        embedded, pending = phase_embed(conn, target_dir, progress_callback)
        print(f'  {embedded} of {pending} crop(s) measured')

        if execution_id and stop_requested(catalog_db_path, execution_id):
            record_stopped(catalog_db_path, execution_id)
            return {'stopped': True, 'message': stop_message()}

        print('Phase 3: scoring')
        scored, assigned = phase_score(conn, execution_id, progress_callback)
        print(f'  {scored} score(s) written, {assigned} animal(s) named '
              f'automatically at distance <= {PET_MATCH_DISTANCE} '
              f'with a margin of {PET_MATCH_MARGIN}')

        return {'crops': made, 'embedded': embedded, 'scored': scored,
                'assigned': assigned}
    finally:
        conn.close()


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(
        description='Identify Pets workflow (subprocess mode)')
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
