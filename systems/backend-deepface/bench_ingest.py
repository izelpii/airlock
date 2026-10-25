#!/usr/bin/env python3
"""
Benchmark the face-detection ingestion path, stage by stage.

Run this on the *host*, with the backend-deepface venv active and the service
listening, e.g.

    cd systems/backend-deepface
    ./venv/bin/python bench_ingest.py --images ~/Pictures/SomeAlbum --sample 20

It answers two questions:

  1. Where does the per-image wall clock actually go - image preparation
     (decode + rotate + re-encode), the network hop, or detection itself?
  2. Does shrinking the image before sending it change the faces found?

Every size is measured against DETECTION_MAX_DIM=0 (the original
full-resolution behaviour) as the reference, and boxes are matched by IoU, so
"same faces, 12x faster" is a claim you can check rather than take on faith.
"""

import argparse
import importlib.util
import io
import os
import statistics
import sys
import time
import types

import requests
from PIL import Image

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
WORKFLOW_PATH = os.path.join(
    REPO_ROOT, 'systems', 'backend', 'workflows', 'ai_auto_detect_faces_workflow.py'
)

IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.gif', '.heic', '.heif'}


def load_workflow_module():
    """
    Import the real workflow module so we benchmark the shipping code path
    rather than a copy of it that can drift.

    The module imports Catalog at the top for the workflow entry points; none
    of the functions used here touch it, so a stub keeps the backend's heavier
    dependencies out of this venv.
    """
    if 'catalog' not in sys.modules:
        stub = types.ModuleType('catalog')

        class Catalog:  # noqa: D401 - stub
            @staticmethod
            def get_by_id(*_a, **_kw):
                raise NotImplementedError('stubbed for benchmarking')

        stub.Catalog = Catalog
        sys.modules['catalog'] = stub

    spec = importlib.util.spec_from_file_location('_bench_wf', WORKFLOW_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def collect_images(paths, sample):
    found = []
    for p in paths:
        p = os.path.expanduser(p)
        if os.path.isfile(p):
            found.append(p)
        elif os.path.isdir(p):
            for root, _dirs, files in os.walk(p):
                for name in sorted(files):
                    if os.path.splitext(name)[1].lower() in IMAGE_EXTS:
                        found.append(os.path.join(root, name))
                if len(found) >= sample * 4:
                    break
    found.sort()
    if sample and len(found) > sample:
        step = len(found) / float(sample)
        found = [found[int(i * step)] for i in range(sample)]
    return found


def iou(a, b):
    ax2, ay2 = a['x'] + a['w'], a['y'] + a['h']
    bx2, by2 = b['x'] + b['w'], b['y'] + b['h']
    ix1, iy1 = max(a['x'], b['x']), max(a['y'], b['y'])
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = a['w'] * a['h'] + b['w'] * b['h'] - inter
    return inter / union if union > 0 else 0.0


def normalise(faces, width, height):
    """Boxes as fractions of the image, so sizes can be compared directly."""
    if not width or not height:
        return []
    return [
        {
            'x': f['x'] / width,
            'y': f['y'] / height,
            'w': f['w'] / width,
            'h': f['h'] / height,
        }
        for f in faces
    ]


def compare(reference, candidate, threshold=0.5):
    """Return (matched, missed, extra) counting reference boxes as truth."""
    unused = list(candidate)
    matched = 0
    for ref in reference:
        best_i, best_v = None, 0.0
        for i, cand in enumerate(unused):
            v = iou(ref, cand)
            if v > best_v:
                best_i, best_v = i, v
        if best_i is not None and best_v >= threshold:
            matched += 1
            unused.pop(best_i)
    return matched, len(reference) - matched, len(unused)


def full_resolution_jpeg(path):
    """The original path: full decode, full rotate, full re-encode at q95."""
    from PIL import ImageOps
    img = ImageOps.exif_transpose(Image.open(path))
    if img.mode in ('RGBA', 'LA', 'P'):
        if img.mode == 'P':
            img = img.convert('RGBA')
        flat = Image.new('RGB', img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        img = flat
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=95)
    size = img.size
    img.close()
    return buf.getvalue(), size[0], size[1]


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('--images', nargs='+', required=True,
                    help='Image files or directories to sample from')
    ap.add_argument('--sample', type=int, default=15,
                    help='How many images to test (evenly spread; default 15)')
    ap.add_argument('--sizes', default='0,640,1024,1600',
                    help='DETECTION_MAX_DIM values to compare; 0 = full resolution')
    ap.add_argument('--url', default=os.environ.get('DEEPFACE_URL', 'http://localhost:5001'))
    ap.add_argument('--iou', type=float, default=0.5,
                    help='IoU threshold for calling two boxes the same face')
    args = ap.parse_args()

    sizes = [int(s.strip()) for s in args.sizes.split(',') if s.strip()]
    if 0 not in sizes:
        sizes.insert(0, 0)

    images = collect_images(args.images, args.sample)
    if not images:
        print('No images found.')
        return 1

    try:
        health = requests.get(f'{args.url}/api/health', timeout=10)
        health.raise_for_status()
    except Exception as e:
        print(f'Detector not reachable at {args.url}: {e}')
        return 1

    wf = load_workflow_module()
    upload_url = f'{args.url}/api/upload'

    print(f'{len(images)} image(s), sizes {sizes}, detector {args.url}\n')

    results = {size: {'prep': [], 'http': [], 'bytes': [], 'faces': 0,
                      'matched': 0, 'missed': 0, 'extra': 0} for size in sizes}

    for n, path in enumerate(images, 1):
        try:
            on_disk = os.path.getsize(path)
        except OSError:
            continue
        print(f'[{n}/{len(images)}] {os.path.basename(path)}  ({on_disk / 1e6:.1f} MB on disk)')

        reference = None
        for size in sizes:
            try:
                t0 = time.perf_counter()
                if size == 0:
                    payload, width, height = full_resolution_jpeg(path)
                else:
                    wf.DETECTION_MAX_DIM = size
                    payload, width, height = wf.prepare_detection_jpeg(path)
                prep = time.perf_counter() - t0

                t1 = time.perf_counter()
                resp = requests.post(
                    upload_url,
                    files={'file': (os.path.basename(path) + '.jpg', payload, 'image/jpeg')},
                    timeout=120,
                )
                http = time.perf_counter() - t1
                resp.raise_for_status()
                faces = resp.json().get('faces', [])
            except Exception as e:
                print(f'    size {size or "full":>5}: FAILED - {e}')
                continue

            boxes = normalise(faces, width, height)
            if size == 0:
                reference = boxes
                agree = ''
            else:
                matched, missed, extra = compare(reference or [], boxes, args.iou)
                results[size]['matched'] += matched
                results[size]['missed'] += missed
                results[size]['extra'] += extra
                agree = f'  match {matched} miss {missed} extra {extra}'

            r = results[size]
            r['prep'].append(prep)
            r['http'].append(http)
            r['bytes'].append(len(payload))
            r['faces'] += len(faces)

            print(f'    size {size or "full":>5}: {width}x{height:<5} '
                  f'prep {prep * 1000:7.1f} ms  post+detect {http * 1000:7.1f} ms  '
                  f'{len(payload) / 1e6:5.2f} MB  {len(faces)} face(s){agree}')

    print('\n' + '=' * 78)
    print(f'{"size":>6} {"prep ms":>9} {"detect ms":>10} {"total ms":>9} '
          f'{"payload MB":>11} {"faces":>6} {"agreement":>22}')
    print('-' * 78)

    baseline_total = None
    for size in sizes:
        r = results[size]
        if not r['prep']:
            continue
        prep = statistics.median(r['prep']) * 1000
        http = statistics.median(r['http']) * 1000
        total = prep + http
        if size == 0:
            baseline_total = total
            agree = 'reference'
        else:
            agree = f"{r['matched']} same / {r['missed']} lost / {r['extra']} new"
        speed = f'  ({baseline_total / total:.1f}x)' if baseline_total and size else ''
        print(f'{size or "full":>6} {prep:9.1f} {http:10.1f} {total:9.1f}{speed} '
              f'{statistics.median(r["bytes"]) / 1e6:11.2f} {r["faces"]:6} {agree:>22}')

    print('=' * 78)
    print('\nMedians, single-threaded. Per-image totals fall further with '
          'DETECTION_CONCURRENCY > 1 and multiple gunicorn workers, which this '
          'benchmark deliberately does not exercise - it measures the work '
          'itself, not how much of it runs at once.')
    return 0


if __name__ == '__main__':
    sys.exit(main())
