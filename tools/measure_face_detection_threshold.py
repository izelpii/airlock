"""
How much would lowering YUNET_SCORE_THRESHOLD help?  READ-ONLY.

Results and method: documentation/features/FACE_DETECTION_THRESHOLD.md.

Run it in a throwaway container from the deepface image, with the catalog
mounted read-only (homeserver paths shown):

    mkdir -p /tmp/yunet && chmod 777 /tmp/yunet
    podman run --rm --user 0 --entrypoint python3 \\
      -e SAMPLE=600 -e RANDOM_TILES=1 -e PER_SHEET=60 \\
      -e BANDS=0.9,0.875,0.85,0.825,0.8,0.75,0.7,0.6 \\
      -v /opt/airlock/catalogs:/catalogs:ro \\
      -v /opt/airlock/systems/backend-deepface/deepface_weights/weights:/weights:ro \\
      -v $PWD/tools/measure_face_detection_threshold.py:/measure.py:ro \\
      -v /tmp/yunet:/out localhost/backend-deepface:latest /measure.py

Settings (environment): CATALOG_DIR (default /catalogs/family), MODEL, SAMPLE,
BANDS, PER_SHEET, RANDOM_TILES=1 (random tiles per band instead of the top
ones), SKIP_MANUAL=1 (only the random sample).

Runs the detector exactly as Find Faces feeds it (the 1200 px derivative, EXIF
orientation applied, re-encoded as JPEG q90, YuNet NMS 0.3 / top-k 5000) but
keeps everything scoring >= 0.3, then:

  A. hand-drawn faces (source='Manual'): the score YuNet gives each one - the
     faces the detector missed, and so what each lower cutoff would recover;
  B. a random sample of scanned photos: the NEW detections (not overlapping
     anything stored) per score band, cropped onto contact sheets so each
     band can be judged by looking.

Writes /out/summary.json and /out/sheet_<band>.jpg. Opens the catalog with
mode=ro&immutable=1 from a read-only mount; writes nothing outside /out.
"""

import io
import json
import os
import random
import sqlite3
import sys
from collections import defaultdict
from multiprocessing import Pool

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageOps

CAT = os.environ.get('CATALOG_DIR', '/catalogs/family')
DB = f'file:{CAT}/workflow_results.db?mode=ro&immutable=1'
MODEL = os.environ.get('MODEL', '/weights/face_detection_yunet_2023mar.onnx')
SAMPLE = int(os.environ.get('SAMPLE', '600'))
FLOOR = 0.3
BANDS = [float(b) for b in os.environ.get('BANDS', '0.9,0.85,0.8,0.75,0.7,0.65,0.6,0.5,0.3').split(',')]
PER_SHEET = int(os.environ.get('PER_SHEET', '120'))
RANDOM_TILES = os.environ.get('RANDOM_TILES') == '1'
SKIP_MANUAL = os.environ.get('SKIP_MANUAL') == '1'
cv2.setNumThreads(1)

_det = None


def detect(rel_path):
    """Scores and boxes, as Find Faces would see them, down to FLOOR."""
    global _det
    path = os.path.join(CAT, rel_path)
    try:
        img = Image.open(path)
        img = ImageOps.exif_transpose(img).convert('RGB')
        buf = io.BytesIO()
        img.save(buf, 'JPEG', quality=90)
        bgr = cv2.imdecode(np.frombuffer(buf.getvalue(), np.uint8), cv2.IMREAD_COLOR)
    except Exception as exc:
        return None, str(exc)
    h, w = bgr.shape[:2]
    if _det is None:
        _det = cv2.FaceDetectorYN_create(MODEL, '', (w, h), FLOOR, 0.3, 5000)
    _det.setInputSize((w, h))
    _det.setScoreThreshold(FLOOR)
    _, faces = _det.detect(bgr)
    out = [] if faces is None else [
        (float(f[14]), float(f[0]), float(f[1]), float(f[2]), float(f[3])) for f in faces]
    return (w, h, out), None


def iou(a, b):
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def work(job):
    kind, file_id, rel, boxes = job
    res, err = detect(rel)
    return kind, file_id, rel, boxes, res, err


def main():
    os.makedirs('/out', exist_ok=True)
    c = sqlite3.connect(DB, uri=True)
    c.row_factory = sqlite3.Row
    deriv = {r['original_file_id']: r['alternative_file_path'] for r in c.execute(
        "SELECT original_file_id, alternative_file_path FROM alternative_versions "
        "WHERE rung='1200'")}
    stored = defaultdict(list)
    for r in c.execute('SELECT file_id, x, y, w, h, source, image_width, image_height, '
                       'confidence FROM face_detections'):
        stored[r['file_id']].append(dict(r))

    manual_files = sorted({fid for fid, ds in stored.items()
                           if any(d['source'] == 'Manual' for d in ds) and fid in deriv})
    scanned = [r[0] for r in c.execute(
        'SELECT id FROM file_contents WHERE face_detection_scanned_at IS NOT NULL '
        'AND removed_at IS NULL')]
    scanned = [f for f in scanned if f in deriv]
    sample = random.Random(42).sample(scanned, min(SAMPLE, len(scanned)))
    c.close()

    jobs = [] if SKIP_MANUAL else [('manual', f, deriv[f], stored[f]) for f in manual_files]
    jobs += [('sample', f, deriv[f], stored[f]) for f in sample]
    print(f'{len(manual_files)} files with manual faces, {len(sample)} sampled files', flush=True)

    manual_scores = []            # (score or 0, box width) per manual face
    manual_dim_mismatch = 0
    reproduced = [0, 0]           # stored Automatic boxes: matched >= 0.9, total
    new_dets = []                 # (score, file_id, rel, box) not overlapping stored
    sample_faces_stored = 0
    errors = 0
    with Pool(4) as pool:
        for i, (kind, fid, rel, boxes, res, err) in enumerate(
                pool.imap_unordered(work, jobs, chunksize=8), 1):
            if i % 500 == 0:
                print(f'  {i} / {len(jobs)}', flush=True)
            if res is None:
                errors += 1
                continue
            w, h, dets = res
            if kind == 'manual':
                for d in boxes:
                    if d['source'] != 'Manual':
                        continue
                    if (d['image_width'], d['image_height']) not in ((w, h), (None, None)):
                        manual_dim_mismatch += 1
                        continue
                    box = (d['x'], d['y'], d['w'], d['h'])
                    best = max((s for s, *b in dets if iou(box, b) >= 0.3), default=0.0)
                    manual_scores.append((best, d['w']))
            else:
                sample_faces_stored += len(boxes)
                for d in boxes:
                    if d['source'] == 'Automatic':
                        reproduced[1] += 1
                        box = (d['x'], d['y'], d['w'], d['h'])
                        if any(s >= 0.9 and iou(box, b) >= 0.5 for s, *b in dets):
                            reproduced[0] += 1
                for s, *b in dets:
                    if s >= 0.9:
                        continue
                    if any(iou((d['x'], d['y'], d['w'], d['h']), b) >= 0.3 for d in boxes):
                        continue
                    new_dets.append((s, fid, rel, b))

    # A: what each cutoff would have recovered of the hand-drawn faces
    found_auto = sum(1 for s, _ in manual_scores if s >= 0.9)
    missed = [(s, bw) for s, bw in manual_scores if s < 0.9]
    recover = {t: sum(1 for s, _ in missed if s >= t) for t in BANDS[1:]}
    never = sum(1 for s, _ in missed if s < FLOOR)
    widths = sorted(bw for _, bw in missed)

    # B: new detections per band in the random sample
    band_counts = {}
    for hi, lo in zip(BANDS, BANDS[1:]):
        band_counts[f'{lo:.2f}-{hi:.2f}'] = sum(1 for s, *_ in new_dets if lo <= s < hi)

    # Contact sheets: every new detection per band (capped), with context.
    sheet_index = []
    for hi, lo in zip(BANDS, BANDS[1:]):
        items = [d for d in new_dets if lo <= d[0] < hi]
        if RANDOM_TILES:
            items = random.Random(7).sample(items, min(PER_SHEET, len(items)))
            items.sort(key=lambda d: -d[0])
        else:
            items = sorted(items, key=lambda d: -d[0])[:PER_SHEET]
        band_files = len({d[1] for d in new_dets if lo <= d[0] < hi})
        band_widths = sorted(d[3][2] for d in new_dets if lo <= d[0] < hi)
        sheet_index.append({'band': f'{lo:.3f}-{hi:.3f}', 'detections': len([d for d in new_dets if lo <= d[0] < hi]),
                            'photos': band_files, 'median_width_px': band_widths[len(band_widths) // 2],
                            'tiles': [(n, round(d[0], 3), d[1]) for n, d in enumerate(items, 1)]})
        if not items:
            continue
        tiles = []
        for s, fid, rel, (x, y, bw, bh) in items:
            try:
                im = ImageOps.exif_transpose(Image.open(os.path.join(CAT, rel))).convert('RGB')
            except Exception:
                continue
            pad = 0.6
            box = (int(x - bw * pad), int(y - bh * pad), int(x + bw * (1 + pad)),
                   int(y + bh * (1 + pad)))
            t = im.crop(box).resize((140, 140))
            dr = ImageDraw.Draw(t)
            # the detection itself, inside its context
            kx = 140 / (bw * (1 + 2 * pad))
            ky = 140 / (bh * (1 + 2 * pad))
            dr.rectangle([bw * pad * kx, bh * pad * ky,
                          bw * (1 + pad) * kx, bh * (1 + pad) * ky],
                         outline=(255, 0, 0), width=2)
            dr.rectangle([0, 0, 140, 16], fill=(0, 0, 0))
            dr.text((3, 2), f'{len(tiles) + 1}: {s:.3f}', fill=(255, 255, 0))
            tiles.append(t)
        cols = 6
        rows = (len(tiles) + cols - 1) // cols
        sheet = Image.new('RGB', (cols * 142, rows * 142), (40, 40, 40))
        for i, t in enumerate(tiles):
            sheet.paste(t, ((i % cols) * 142, (i // cols) * 142))
        sheet = sheet.resize((sheet.width * 13 // 10, sheet.height * 13 // 10))
        sheet.save(f'/out/sheet_{lo:.3f}-{hi:.3f}.jpg', quality=85)

    summary = {
        'manual_faces_measured': len(manual_scores),
        'manual_dim_mismatch_skipped': manual_dim_mismatch,
        'manual_already_found_at_0.9': found_auto,
        'manual_missed_at_0.9': len(missed),
        'manual_missed_recovered_at': recover,
        'manual_missed_below_0.3': never,
        'manual_missed_box_width_px_quartiles': [widths[len(widths) * q // 4] for q in (1, 2, 3)] if widths else None,
        'sample_files': len(sample),
        'sample_stored_faces': sample_faces_stored,
        'sample_auto_reproduced_at_0.9': reproduced,
        'sample_new_detections_by_band': band_counts,
        'errors': errors,
        'sheets': sheet_index,
    }
    json.dump(summary, open('/out/summary.json', 'w'), indent=1)
    print(json.dumps(summary, indent=1))


if __name__ == '__main__':
    sys.exit(main())
