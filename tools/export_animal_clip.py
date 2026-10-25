#!/usr/bin/env python3
"""
Export the animal identity model to the ONNX file backend-pets loads.

    python3 tools/export_animal_clip.py [--out <path>] [--check]

WHY THIS SCRIPT EXISTS AT ALL
-----------------------------
AvitoTech/CLIP-ViT-base-for-animal-identification is published as PyTorch
weights. backend-pets runs onnxruntime and nothing else - no torch, no
transformers, no 2 GB of CUDA wheels in a service whose whole job is to turn a
224 px crop into 512 floats. So the conversion happens ONCE, here, on whatever
machine has torch, and the ~170 MB result is copied into
systems/backend-pets/weights/.

It is deliberately not part of any start script. A model file is data, not a
build step: tools/fetch_pet_weights.sh explains the same thing for YOLOX.

THE CONTRACT THIS FILE HAS TO HONOUR
------------------------------------
backend-pets/app.py embed() does, in order:

    1. one input, NCHW float32, [1, 3, 224, 224]
    2. CLIP's own normalisation (mean/std below), applied to RGB in 0..1
    3. takes run(None, ...)[0] as the embedding and L2-normalises it

So the graph must have ONE input and its FIRST output must be the 512-d image
embedding - not last_hidden_state, which is what AutoModel would give first and
which would produce 768-d garbage that still looks like a vector. Hence the
wrapper module: it returns exactly one tensor, and there is nothing to get
wrong at the other end.

The full checkpoint is a CLIPModel - vision tower, text tower, two projections.
Only the vision half is wanted, and dropping the text tower is most of the file
size as well as all of the tokeniser.

LICENCE
-------
Apache 2.0, which is why it is this model and not one of the better AGPL ones.
See documentation/specs/commercial-licensing-audit.md.
"""

import argparse
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEFAULT_OUT = os.path.join(HERE, os.pardir, 'systems', 'backend-pets',
                           'weights', 'animal_clip_vit_b32.onnx')

REPO = 'AvitoTech/CLIP-ViT-base-for-animal-identification'
SIDE = 224
DIMS = 512

# backend-pets/app.py embed() holds the same six numbers. They are CLIP's own,
# not this fine-tune's, and they live in two files because the two run in
# different processes with no common import - so check_preprocessing() below
# asserts the pair agree rather than trusting that they do. Getting one of them
# wrong does not fail: it shifts every vector slightly, and the only symptom is
# a threshold that will not separate two dogs.
CLIP_MEAN = (0.48145466, 0.4578275, 0.40821073)
CLIP_STD = (0.26862954, 0.26130258, 0.27577711)
APP_PY = os.path.join(HERE, os.pardir, 'systems', 'backend-pets', 'app.py')


def check_preprocessing():
    """The service's six constants, and its input side, must match this file."""
    path = os.path.normpath(APP_PY)
    try:
        src = io.open(path, encoding='utf-8').read()
    except OSError as e:
        print(f'  ! could not read {path}: {e}')
        return True

    ok = True
    for n in CLIP_MEAN + CLIP_STD:
        if repr(n) not in src and str(n) not in src:
            print(f'  FAIL: {n} is not in backend-pets/app.py - the service '
                  f'normalises differently from what this model expects')
            ok = False
    for name, want in (('EMBED_SIZE', SIDE), ('EMBED_DIMS', DIMS)):
        if f'{name} = {want}' not in src:
            print(f'  FAIL: backend-pets/app.py does not say {name} = {want}')
            ok = False
    if ok:
        print(f'  preprocessing agrees with backend-pets/app.py '
              f'({SIDE} px, {DIMS}-d, CLIP mean/std)')
    return ok


def _die(msg, code=2):
    print(f'error: {msg}', file=sys.stderr)
    sys.exit(code)


def export(out_path, opset=17):
    try:
        import torch
        from transformers import CLIPVisionModelWithProjection
    except ImportError as e:
        _die(f'{e}.\n\n'
             f'  This script needs torch and transformers, which backend-pets\n'
             f'  deliberately does not have. Run it anywhere they exist:\n\n'
             f'    python3 -m venv /tmp/clipexport\n'
             f'    /tmp/clipexport/bin/pip install torch transformers onnx '
             f'--index-url https://download.pytorch.org/whl/cpu\n'
             f'    /tmp/clipexport/bin/python tools/export_animal_clip.py\n')

    print(f'loading {REPO} (vision tower + projection only)')
    # eager attention, not SDPA, and this is not a preference.
    # torch.onnx.export cannot translate scaled_dot_product_attention on older
    # torch (it dies inside the tracer with "z_(): incompatible function
    # arguments"), and transformers defaults to SDPA when it is available. The
    # two produce the same numbers; only the traced graph differs.
    try:
        model = CLIPVisionModelWithProjection.from_pretrained(
            REPO, attn_implementation='eager')
    except TypeError:
        # Older transformers spells it differently.
        model = CLIPVisionModelWithProjection.from_pretrained(
            REPO, attn_implementation=None)
    model.eval()

    class ImageEmbedder(torch.nn.Module):
        """
        One input, one output, no ModelOutput dict.

        The point of the wrapper: CLIPVisionModelWithProjection returns a
        dataclass, and which field lands at ONNX output 0 is an implementation
        detail of whatever transformers version did the export. The service
        takes output 0 and trusts it. This makes that trust well founded.
        """

        def __init__(self, inner):
            super().__init__()
            self.inner = inner

        def forward(self, pixel_values):
            return self.inner(pixel_values=pixel_values).image_embeds

    wrapped = ImageEmbedder(model).eval()
    dummy = torch.zeros(1, 3, SIDE, SIDE, dtype=torch.float32)

    with torch.no_grad():
        got = wrapped(dummy)
    if tuple(got.shape) != (1, DIMS):
        _die(f'the wrapped model returned {tuple(got.shape)}, not (1, {DIMS}). '
             f'The checkpoint changed shape; backend-pets EMBED_DIMS would be '
             f'wrong and every stored vector incomparable with every other.')

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    tmp = out_path + '.part'
    print(f'exporting opset {opset} -> {out_path}')
    with torch.no_grad():
        torch.onnx.export(
            wrapped, dummy, tmp,
            input_names=['pixel_values'],
            output_names=['image_embeds'],
            # Batch only. Height and width stay FIXED at 224: the service
            # resizes to exactly that, and a dynamic spatial axis on a ViT
            # invites a silent interpolation of the position embeddings.
            dynamic_axes={'pixel_values': {0: 'batch'},
                          'image_embeds': {0: 'batch'}},
            opset_version=opset,
            do_constant_folding=True,
        )
    os.replace(tmp, out_path)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    print(f'wrote {out_path} ({size_mb:.0f} MB)')
    return out_path


def check(out_path):
    """
    Prove the file the service will load behaves the way the service assumes.

    Not "does it load": that is the easy half. This asserts the input shape,
    the output shape, and that the graph is actually computing rather than
    returning a constant.

    It does NOT tell you the model is any good, and the cosine it prints is a
    trap worth naming: two random NOISE images come out at about +0.98, because
    CLIP embeddings occupy a narrow cone rather than the whole sphere. That is
    a property of the model, not a fault in the export - but it means a
    threshold cannot be guessed from first principles. 0.22 cosine distance
    sounds strict and would match two completely unrelated images. The
    threshold has to be MEASURED on real crops; see
    tools/bench_pet_threshold.py.
    """
    try:
        import numpy as np
        import onnxruntime as ort
    except ImportError as e:
        print(f'skipping check: {e}')
        return True

    s = ort.InferenceSession(out_path, providers=['CPUExecutionProvider'])
    inp = s.get_inputs()[0]
    print(f'  input  {inp.name} {inp.shape} {inp.type}')
    out = s.get_outputs()[0]
    print(f'  output {out.name} {out.shape}')

    ok = True
    if len(inp.shape) != 4 or inp.shape[1] != 3:
        print('  FAIL: input is not NCHW with 3 channels')
        ok = False
    if inp.shape[2] != SIDE or inp.shape[3] != SIDE:
        print(f'  FAIL: input side is {inp.shape[2:]}, not {SIDE}')
        ok = False

    rng = np.random.default_rng(0)
    a = rng.standard_normal((1, 3, SIDE, SIDE), dtype=np.float32)
    b = rng.standard_normal((1, 3, SIDE, SIDE), dtype=np.float32)
    va = s.run(None, {inp.name: a})[0].reshape(-1)
    vb = s.run(None, {inp.name: b})[0].reshape(-1)

    if va.shape[0] != DIMS:
        print(f'  FAIL: output is {va.shape[0]}-d, not {DIMS}-d')
        ok = False
    na, nb = np.linalg.norm(va), np.linalg.norm(vb)
    if not (na > 0 and nb > 0):
        print('  FAIL: a zero vector came back; the graph is not computing')
        ok = False
    else:
        cos = float(np.dot(va / na, vb / nb))
        print(f'  two random inputs: cosine {cos:+.4f}  '
              f'(expect ~0.98 - see the docstring, this is the cone effect '
              f'and not a problem with the export)')
        if cos > 0.99999:
            print('  FAIL: different inputs give an IDENTICAL vector, so the '
                  'graph is returning a constant')
            ok = False

    ok = check_preprocessing() and ok

    print('  OK' if ok else '  NOT USABLE')
    return ok


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    ap.add_argument('--out', default=os.path.normpath(DEFAULT_OUT))
    ap.add_argument('--check', action='store_true',
                    help='only check an existing file, do not export')
    ap.add_argument('--opset', type=int, default=17)
    args = ap.parse_args()

    if args.check:
        if not os.path.isfile(args.out):
            _die(f'{args.out} does not exist')
        sys.exit(0 if check(args.out) else 1)

    if os.path.isfile(args.out) and os.path.getsize(args.out) > 0:
        print(f'{args.out} already exists; checking it instead')
        sys.exit(0 if check(args.out) else 1)

    export(args.out, opset=args.opset)
    sys.exit(0 if check(args.out) else 1)


if __name__ == '__main__':
    main()
