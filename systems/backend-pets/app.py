#!/usr/bin/env python3
"""
backend-pets: find animals in a photograph, and measure one for identity.

Two models, both ONNX under onnxruntime, no deep-learning framework:

  DETECT  YOLOX (Apache 2.0). COCO's classes already include cat, dog, bird and
          horse. Answers "is there an animal here, and where".

  EMBED   a CLIP-ViT fine-tuned for animal identification (Apache 2.0), 512-d.

The two models want DIFFERENT input and it is easy to cross them:
  detect  BGR, 0-255, unnormalised, letterboxed  (YOLOX / cv2 convention)
  embed   RGB, 0-1, CLIP mean/std, squashed to 224  (torchvision convention)
Getting the first one wrong does not fail - see _letterbox.
          Answers "is this the same animal as that one".

Why not VGG-Face or YuNet: YuNet is a human-face cascade and will not fire on a
dog - that is not a threshold to loosen, it is looking for the wrong thing - and
VGG-Face embeddings were never asked to separate dogs. See
documentation/specs/pet-recognition-spec.md section 3.

Why not YOLO11, which is the better detector: it is AGPL-3.0 and Airlock Photos is
proprietary (license.txt). Exporting to ONNX does not change that, and AGPL's
network clause reaches a service like this one even without distribution. See
documentation/specs/commercial-licensing-audit.md.

THE WEIGHTS ARE BIND-MOUNTED, NOT BAKED, matching backend-deepface. So
"configured" and "usable" are different states, and /health reports both - a
model file that is absent gives a clear 503 from its endpoint rather than a
worker that dies at import, because "recognition needs a model" is a far better
failure than "the pet service never started".
"""

import io
import json
import os
import threading

import numpy as np
from flask import Flask, jsonify, request
from flask_cors import CORS
from PIL import Image

app = Flask(__name__)
CORS(app)


# ── Models ───────────────────────────────────────────────────────────────────

_weights_dir = os.environ.get(
    'PETS_WEIGHTS_DIR', os.path.join(os.path.dirname(os.path.abspath(__file__)), 'weights'))

# yolox_tiny, not _s and not _nano, and both halves of that are measured
# (spec section 12, run 2026-10-09 on 300-500 real photographs from `family`):
#
#   yolox_s    @640  711 ms/image   5.4 % of images had an animal
#   yolox_tiny @416   91 ms/image   6.0 %          <- same rate, an eighth of the cost
#   yolox_nano @416   41 ms/image   2.7 %          <- finds barely half
#
# nano is the one the spec originally recommended, on parameter count. It is
# fast and it misses animals, which is not cheap - a detector that does not see
# the dog costs the whole feature, not 50 ms.
DETECT_MODEL = os.environ.get('PETS_DETECT_MODEL', 'yolox_tiny.onnx')
EMBED_MODEL = os.environ.get('PETS_EMBED_MODEL', 'animal_clip_vit_b32.onnx')

MODEL_FILES = {'detect': DETECT_MODEL, 'embed': EMBED_MODEL}

# The detector's input side.
#
# NOT a free parameter, which the spec assumed it was. The official YOLOX ONNX
# exports have a FIXED input shape - asking yolox_s for 320 px gets
# "Got: 320 Expected: 640" out of onnxruntime, not a faster answer. So this is
# a request, and `model_input_size()` below overrides it with whatever the
# loaded model actually wants.
#
# 416 is yolox_tiny's and yolox_nano's native size; yolox_s is 640.
DETECT_SIZE = int(os.environ.get('PETS_DETECT_SIZE', '416'))

EMBED_SIZE = 224          # the CLIP-ViT-B/32 input
EMBED_DIMS = 512

# COCO class ids, 0-indexed. cat=15, dog=16, bird=14, horse=17.
#
# Only cat and dog are identified: they are what the pet re-identification
# models cover. bird and horse are reported when asked for, because COCO gives
# them away free, but there is no identity model for them worth having - see
# the spec's open items.
SPECIES_BY_CLASS = {15: 'cat', 16: 'dog'}
EXTRA_SPECIES = {14: 'bird', 17: 'horse'}

# 0.45, lowered from 0.55 after the first threshold was measured WRONG.
#
# The 0.55 came from 35 detections in 2,000 family photographs, sorted by
# confidence: everything from 0.57 up was a real animal, and half of what fell
# between 0.35 and 0.54 was children. That looked clean, and it measured the
# wrong thing. Those 35 crops were the ones the detector had ALREADY PRODUCED
# at 0.35 - so it measured precision, what the detector got wrong, and never
# once looked at what it was missing. The same error as the benchmark in
# section 12 of the spec, which counted detections without looking at one.
#
# What 0.55 was missing, on a photograph nobody would call marginal - a large,
# sharp, well-lit white shepherd filling half the frame:
#
#     cat 0.515      dog 0.450      -> nothing detected at all
#
# Three frames of that same dog, minutes apart, show where the scores go:
#
#     standing, clear              dog 0.924
#     lying down, head turned      cat 0.560   dog 0.352
#     lying down, head turned      cat 0.515   dog 0.450
#
# A white dog on a white sofa is hard for this model, and it hedges towards
# cat. 0.45 catches all of them; the confident dogs at 0.59-0.92 are
# unaffected. It also brings back some of the children, which is the price,
# and the cross-class NMS in detect() is what stops the hedging itself costing
# a second spurious animal per photograph.
#
# PETS_CONF overrides it: LOWER to find more animals and more people with them,
# higher to find only the obvious ones.
CONF_THRESHOLD = float(os.environ.get('PETS_CONF', '0.45'))
NMS_IOU = float(os.environ.get('PETS_NMS_IOU', '0.45'))

PETS_CONCURRENCY = int(os.environ.get('PETS_CONCURRENCY', '2'))

# A slot reservation, NOT a lock held across inference.
#
# backend-geoloc held a module-level lock across a 60 s HTTP call in an
# eight-thread server and the whole backend appeared to be down. The shape that
# fixed it is this one: bound how many run at once, and do the slow work
# outside anything another request waits on.
_slots = threading.BoundedSemaphore(PETS_CONCURRENCY)
QUEUE_WAIT_SECONDS = float(os.environ.get('PETS_QUEUE_WAIT', '20'))

_sessions = {}
_session_lock = threading.Lock()


def model_path(which):
    return os.path.join(_weights_dir, MODEL_FILES[which])


def model_present(which):
    p = model_path(which)
    return os.path.isfile(p) and os.path.getsize(p) > 0


def model_input_size(which='detect'):
    """
    The side length the loaded model actually accepts, or None if it is dynamic.

    Read from the graph rather than configured, because the official YOLOX
    exports are fixed-shape and a mismatch is an onnxruntime error deep in a
    request rather than anything a caller can act on. Asked once per process;
    the session is cached.
    """
    try:
        shape = session(which).get_inputs()[0].shape        # [N, C, H, W]
    except Exception:
        return None
    h = shape[2] if len(shape) > 3 else None
    return h if isinstance(h, int) and h > 0 else None


def session(which):
    """
    The onnxruntime session for a model, built once.

    Built lazily and under a lock, but the LOCK IS ONLY AROUND CONSTRUCTION -
    inference happens outside it. Two requests arriving before the first load
    completes wait for each other once, then never again.
    """
    with _session_lock:
        if which in _sessions:
            return _sessions[which]
        import onnxruntime as ort
        opts = ort.SessionOptions()
        opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        # One thread per session: concurrency is bounded by _slots above, and
        # letting each session spawn as many threads as there are cores turns
        # two concurrent requests into oversubscription.
        opts.intra_op_num_threads = int(os.environ.get('PETS_THREADS', '2'))
        s = ort.InferenceSession(model_path(which), opts,
                                 providers=['CPUExecutionProvider'])
        _sessions[which] = s
        return s


# ── Image preparation ────────────────────────────────────────────────────────

def _letterbox(img, size):
    """
    Resize to fit `size` x `size` keeping aspect, padding the remainder.

    Returns (chw_float32, scale). The scale is what maps a box back to the
    image that was posted - the caller then maps that to the original, using
    the detection dimensions this service reports. Getting that chain wrong is
    the bug documentation/fixes/ has nine files about, which is why `width` and
    `height` are in every response.

    ── BGR, AND IT IS NOT A DETAIL ──────────────────────────────────────────
    YOLOX's own pipeline reads images with cv2.imread, which returns BGR, and
    passes them through unnormalised - no /255, no mean, no std. So the model
    was trained on BGR 0-255 and that is what it has to be given.

    This shipped as RGB, and the symptom was not a crash or obvious nonsense.
    Boxes landed EXACTLY on the right object - box regression reads shape and
    survives a colour swap - while the class head, which does not, produced
    confident rubbish. Measured on six real photographs from the family
    catalog, every one of which contains people and no animals:

        a child's face           RGB: dog 0.84    BGR: person 0.84
        a boy asleep on a couch  RGB: cat 0.52    BGR: person 0.89
        a man holding a toddler  RGB: person 0.58 BGR: person 0.85

    Seven detections, seven people, nothing else - a feature that would have
    filled the Pets page with the family's own children. And it invalidated
    the benchmark in section 12 of the spec, which compared three YOLOX sizes
    on how OFTEN each found an animal without ever checking whether what it
    found was one. "yolox_tiny finds animals in 6.0 % of images" was really
    "hallucinates one in 6.0 % of images".
    """
    w, h = img.size
    scale = min(size / w, size / h)
    nw, nh = max(1, int(round(w * scale))), max(1, int(round(h * scale)))
    resized = img.resize((nw, nh), Image.BILINEAR)
    canvas = Image.new('RGB', (size, size), (114, 114, 114))
    canvas.paste(resized, (0, 0))
    arr = np.asarray(canvas, dtype=np.float32)      # HWC, 0-255
    arr = arr[:, :, ::-1]                           # -> BGR, as trained
    return np.ascontiguousarray(
        np.transpose(arr, (2, 0, 1))[None, ...]), scale


def _nms(boxes, scores, iou_threshold):
    """Plain greedy NMS. boxes are x1,y1,x2,y2."""
    if len(boxes) == 0:
        return []
    x1, y1, x2, y2 = boxes[:, 0], boxes[:, 1], boxes[:, 2], boxes[:, 3]
    areas = np.maximum(0.0, x2 - x1) * np.maximum(0.0, y2 - y1)
    order = scores.argsort()[::-1]
    keep = []
    while order.size > 0:
        i = order[0]
        keep.append(int(i))
        if order.size == 1:
            break
        xx1 = np.maximum(x1[i], x1[order[1:]])
        yy1 = np.maximum(y1[i], y1[order[1:]])
        xx2 = np.minimum(x2[i], x2[order[1:]])
        yy2 = np.minimum(y2[i], y2[order[1:]])
        inter = np.maximum(0.0, xx2 - xx1) * np.maximum(0.0, yy2 - yy1)
        iou = inter / np.maximum(1e-9, areas[i] + areas[order[1:]] - inter)
        order = order[1:][iou <= iou_threshold]
    return keep


def _yolox_decode(raw, size):
    """
    YOLOX's grid decode, for an export that did NOT fold it in.

    An export made with --decode_in_inference already returns boxes in input
    pixels, in which case `raw` needs no grids and this is a no-op. Both shapes
    exist in the wild, so detect which one arrived rather than assuming:
    decoded output has values far larger than 1, raw output is in grid units.

    This is the part of the service most likely to be wrong against a specific
    model file, and section 12's benchmark is where it gets checked against
    real photographs rather than against my reading of the exporter.
    """
    if raw.ndim == 3:
        raw = raw[0]
    # already-decoded exports: xywh in input pixels
    if raw[:, :4].max() > size * 1.5 or raw.shape[0] in (0,):
        return raw
    strides = [8, 16, 32]
    grids, expanded = [], []
    for stride in strides:
        hsize = wsize = size // stride
        yv, xv = np.meshgrid(np.arange(hsize), np.arange(wsize), indexing='ij')
        grid = np.stack((xv, yv), 2).reshape(1, -1, 2)
        grids.append(grid)
        expanded.append(np.full((1, grid.shape[1], 1), stride, dtype=np.float32))
    grids = np.concatenate(grids, 1)[0]
    expanded = np.concatenate(expanded, 1)[0]
    if grids.shape[0] != raw.shape[0]:
        # Shapes disagree, so this export is not the grid kind after all.
        return raw
    out = raw.copy()
    out[:, 0:2] = (raw[:, 0:2] + grids) * expanded
    out[:, 2:4] = np.exp(raw[:, 2:4]) * expanded
    return out


def detect(jpeg_bytes, size=None, include_extra=False):
    """
    Boxes for the animals in one image, in THAT image's pixel space.

    Returns (detections, width, height). The caller persists width and height
    alongside the boxes, because they are the space the boxes are in.
    """
    size = size or DETECT_SIZE
    # The model's own shape wins. A fixed-shape export cannot be asked for a
    # different size, and discovering that as an InvalidArgument mid-request is
    # no use to anybody.
    fixed = model_input_size('detect')
    if fixed:
        size = fixed
    img = Image.open(io.BytesIO(jpeg_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    width, height = img.size
    blob, scale = _letterbox(img, size)
    img.close()

    s = session('detect')
    acquired = _slots.acquire(timeout=QUEUE_WAIT_SECONDS)
    if not acquired:
        raise TimeoutError('pet detector is busy')
    try:
        raw = s.run(None, {s.get_inputs()[0].name: blob})[0]
    finally:
        _slots.release()

    pred = _yolox_decode(np.asarray(raw, dtype=np.float32), size)
    if pred.size == 0:
        return [], width, height

    # [cx, cy, w, h, obj, cls...]
    obj = pred[:, 4]
    cls_scores = pred[:, 5:]
    cls_ids = cls_scores.argmax(axis=1)
    conf = obj * cls_scores[np.arange(len(cls_ids)), cls_ids]

    wanted = dict(SPECIES_BY_CLASS)
    if include_extra:
        wanted.update(EXTRA_SPECIES)

    mask = (conf >= CONF_THRESHOLD) & np.isin(cls_ids, list(wanted))
    if not mask.any():
        return [], width, height

    cx, cy, bw, bh = pred[mask, 0], pred[mask, 1], pred[mask, 2], pred[mask, 3]
    boxes = np.stack([cx - bw / 2, cy - bh / 2, cx + bw / 2, cy + bh / 2], axis=1)
    boxes /= scale                       # back to the posted image's pixels
    kept_conf = conf[mask]
    kept_cls = cls_ids[mask]

    # ONE pass across every species, not one pass per species.
    #
    # Standard detection practice is per-class NMS, because a person standing
    # in front of a car is genuinely two objects and neither should suppress
    # the other. Here the classes are cat and dog, and ONE ANIMAL IS NOT BOTH -
    # so the usual reasoning inverts, and the usual code produced this, on a
    # photograph of one white shepherd lying on a sofa:
    #
    #     cat 0.515  box (145, 251) 981 x 546
    #     dog 0.450  box (150, 279) 969 x 521      <- 5 px apart
    #
    # Two detections, two crops, two embeddings, and two candidate animals for
    # the clustering pass to decide were different pets. The model is hedging
    # between two labels for one thing it can see perfectly well, and the right
    # answer is to let them compete: highest confidence takes the box.
    #
    # The cost is a real cat and a real dog overlapping by more than NMS_IOU
    # (0.45), which needs them almost on top of each other in frame. Losing one
    # of those occasionally is a better trade than splitting every ambiguous
    # animal in two.
    out = []
    for i in _nms(boxes, kept_conf, NMS_IOU):
        x1, y1, x2, y2 = boxes[i]
        x1 = max(0, min(width - 1, float(x1)))
        y1 = max(0, min(height - 1, float(y1)))
        x2 = max(0, min(width, float(x2)))
        y2 = max(0, min(height, float(y2)))
        if x2 - x1 < 2 or y2 - y1 < 2:
            continue
        out.append({
            'x': int(round(x1)), 'y': int(round(y1)),
            'w': int(round(x2 - x1)), 'h': int(round(y2 - y1)),
            'confidence': round(float(kept_conf[i]), 4),
            'species': wanted[int(kept_cls[i])],
        })
    out.sort(key=lambda d: -d['confidence'])
    return out, width, height


def embed(jpeg_bytes):
    """A 512-d vector for one pre-cropped animal."""
    img = Image.open(io.BytesIO(jpeg_bytes))
    if img.mode != 'RGB':
        img = img.convert('RGB')
    img = img.resize((EMBED_SIZE, EMBED_SIZE), Image.BICUBIC)
    arr = np.asarray(img, dtype=np.float32) / 255.0
    img.close()
    # CLIP's normalisation
    mean = np.array([0.48145466, 0.4578275, 0.40821073], dtype=np.float32)
    std = np.array([0.26862954, 0.26130258, 0.27577711], dtype=np.float32)
    arr = (arr - mean) / std
    blob = np.transpose(arr, (2, 0, 1))[None, ...]

    s = session('embed')
    acquired = _slots.acquire(timeout=QUEUE_WAIT_SECONDS)
    if not acquired:
        raise TimeoutError('pet embedder is busy')
    try:
        vec = s.run(None, {s.get_inputs()[0].name: blob})[0]
    finally:
        _slots.release()

    vec = np.asarray(vec, dtype=np.float32).reshape(-1)
    norm = float(np.linalg.norm(vec))
    if norm > 0:
        vec = vec / norm            # L2-normalised, so cosine is a dot product
    return [round(float(v), 6) for v in vec]


# ── Endpoints ────────────────────────────────────────────────────────────────

def _missing(which):
    return jsonify({
        'error': f'{which} model not available',
        'expected': model_path(which),
        'hint': 'tools/fetch_pet_weights.sh puts it there',
    }), 503


@app.route('/health', methods=['GET'])
def health():
    """
    Liveness, plus whether each model is actually on disk.

    The weights are bind-mounted rather than baked, so a model can be
    configured and still be missing. Reporting it here is what stops that
    becoming a confusing 503 in the middle of a workflow run.
    """
    return jsonify({
        'status': 'healthy',
        'weights_dir': _weights_dir,
        'models': MODEL_FILES,
        'present': {k: model_present(k) for k in MODEL_FILES},
        'loaded': sorted(_sessions),
        'detect_size_requested': DETECT_SIZE,
        'detect_size_actual': model_input_size('detect') if model_present('detect') else None,
        'embed_dims': EMBED_DIMS,
        'concurrency': PETS_CONCURRENCY,
    })


@app.route('/detect', methods=['POST'])
def detect_endpoint():
    """
    Animals in one image.

    Request (multipart/form-data):
        image       the JPEG. ALREADY PREPARED by the caller - this service
                    never opens an original. See the spec's section 5.1: the
                    decode is ~98 % of the per-image cost and is shared with
                    the face pass.
        max_dim     optional, overrides PETS_DETECT_SIZE
        include_extra  optional '1' to also report bird and horse

    Response:
        { detections: [{x,y,w,h,confidence,species}], width, height, size }

    `width`/`height` are the posted image's, which is the space the boxes are
    in. The caller stores them next to the boxes; without them a crop is
    silently offset.
    """
    if not model_present('detect'):
        return _missing('detect')
    if 'image' not in request.files:
        return jsonify({'error': 'image file is required'}), 400
    try:
        size = int(request.form.get('max_dim') or DETECT_SIZE)
    except (TypeError, ValueError):
        size = DETECT_SIZE
    include_extra = str(request.form.get('include_extra', '')).lower() in ('1', 'true', 'yes')
    try:
        dets, w, h = detect(request.files['image'].read(), size, include_extra)
    except TimeoutError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'detections': dets, 'width': w, 'height': h, 'size': size})


@app.route('/embed', methods=['POST'])
def embed_endpoint():
    """One crop in, one 512-d vector out."""
    if not model_present('embed'):
        return _missing('embed')
    if 'image' not in request.files:
        return jsonify({'error': 'image file is required'}), 400
    try:
        vec = embed(request.files['image'].read())
    except TimeoutError as e:
        return jsonify({'error': str(e)}), 503
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    return jsonify({'embedding': vec, 'dims': len(vec), 'model': EMBED_MODEL})


@app.route('/embed-batch', methods=['POST'])
def embed_batch_endpoint():
    """
    Several crops in one request.

    Exists because the identify pass embeds thousands of 224 px crops in a row,
    and a round trip per crop is almost entirely overhead. Each result carries
    its index so a failure in one does not discard the rest.
    """
    if not model_present('embed'):
        return _missing('embed')
    files = request.files.getlist('images')
    if not files:
        return jsonify({'error': 'images files are required'}), 400
    out = []
    for i, f in enumerate(files):
        try:
            out.append({'index': i, 'embedding': embed(f.read())})
        except Exception as e:
            out.append({'index': i, 'error': str(e)})
    return jsonify({'results': out, 'dims': EMBED_DIMS, 'model': EMBED_MODEL})


if __name__ == '__main__':
    print(f'backend-pets weights dir: {_weights_dir}')
    for k, v in MODEL_FILES.items():
        print(f'  {k:<7} {v:<34} {"present" if model_present(k) else "MISSING"}')
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', '5006')), debug=False)
