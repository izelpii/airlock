#!/usr/bin/env python3
"""
Download the DeepFace models once, in a plain single process.

Run by systems/backend-deepface/start-host.sh after the venv is built, and by
deploy/deploy.sh when it prepares a machine for host mode. Never by a gunicorn
worker: on macOS a weights download inside a forked child trips the
Objective-C runtime's fork-safety check (+[NSCharacterSet initialize] may have
been in progress), which kills the worker on boot, forever. Fetching here means
no worker ever has to.

A failure is not fatal - it is reported, and the models are fetched lazily at
first use instead. The one exception is the YuNet detector, which nothing
downloads lazily; deploy.sh fetches that file by URL.

    systems/backend-deepface/venv/bin/python3 tools/prefetch_weights.py

MUST RUN ON THE DEEPFACE VENV'S INTERPRETER, which is why both callers name it
explicitly: `deepface` and `numpy` are installed there and nowhere else. This
script lives in tools/ but is not a general-purpose one - it configures and
warms exactly one service's model cache.
"""
import os
import sys

# Where DeepFace keeps its cache, defaulted to the service's own weights
# directory - the same path the container mounts at /home/appuser/.deepface, so
# host mode and container mode share one copy.
#
# Defaulted HERE because this file no longer sits in that directory: run by hand
# without it, DeepFace would download into $HOME and the next start would find
# an empty cache and do it again. Both callers set it too; this is the backstop.
REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEEPFACE_DIR = os.path.join(REPO, 'systems', 'backend-deepface')
os.environ.setdefault('DEEPFACE_HOME', DEEPFACE_DIR)

print(f"   DEEPFACE_HOME: {os.environ['DEEPFACE_HOME']}")

# numpy and deepface together, with the fix named. Living in tools/ makes it
# easy to run this with whatever `python3` is on PATH, which is never the right
# one: the only interpreter with these installed is the service's venv.
try:
    import numpy as np
    from deepface import DeepFace
except Exception as exc:                                   # noqa: BLE001
    print(f"   cannot import deepface ({exc}).")
    print(f"   Run it on the service's interpreter, which is the only one that")
    print(f"   has deepface installed:")
    print(f"     {os.path.join(DEEPFACE_DIR, 'venv', 'bin', 'python3')} "
          f"{os.path.relpath(os.path.abspath(__file__), REPO)}")
    print(f"   If that venv does not exist yet: "
          f"systems/backend-deepface/start-host.sh --setup")
    sys.exit(1)

blank = np.zeros((64, 64, 3), dtype=np.uint8)
failures = 0

try:
    DeepFace.extract_faces(img_path=blank, detector_backend="yunet",
                           enforce_detection=False)
    print("   YuNet ready")
except Exception as exc:                                   # noqa: BLE001
    # Expected when the .onnx file is absent: get_yunet() returns None rather
    # than downloading. deploy/deploy.sh is what puts it there.
    print(f"   YuNet not ready: {exc}")
    failures += 1

try:
    DeepFace.represent(img_path=blank, model_name="VGG-Face",
                       enforce_detection=False, detector_backend="skip")
    print("   VGG-Face ready")
except Exception as exc:                                   # noqa: BLE001
    print(f"   VGG-Face pre-fetch skipped: {exc}")
    failures += 1

sys.exit(1 if failures else 0)
