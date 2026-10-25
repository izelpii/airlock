#!/usr/bin/env bash
# Run backend-deepface DIRECTLY ON THE HOST (not in a container).
#
# ── Why this exists ───────────────────────────────────────────────────────────
# Podman on macOS runs containers inside a Linux virtual machine. Apple's GPU
# stack - Metal, and the tensorflow-metal plugin built on it - is a macOS-native
# framework, and there is no Metal passthrough into a Linux guest. A containerised
# backend-deepface is therefore CPU-only no matter how it is configured. Running
# it natively is the only way to reach the GPU on Apple Silicon.
#
# Nothing else has to change: the main backend already calls this service at
# DEEPFACE_URL=http://host.containers.internal:5001, and the service exchanges
# only image BYTES over HTTP - no shared filesystem - so moving it out of a
# container creates no mount problems.
#
# ── What actually gets accelerated ────────────────────────────────────────────
# Only the VGG-Face embedding step runs on TensorFlow. YuNet face detection runs
# through OpenCV's DNN module, which does not use Metal, so the detection pass
# stays CPU-bound either way. Embeddings are also cached after first computation,
# so the win shows up when ingesting a large batch of new photos, not on
# day-to-day re-scoring.
#
# Usage:
#   ./start-host.sh            # start the service
#   ./start-host.sh --setup    # create/refresh the venv, then start
#   ./start-host.sh --check    # report GPU availability and exit

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

VENV="$SCRIPT_DIR/venv"
PORT="${DEEPFACE_PORT:-5001}"
# airlock_native_binds: where a native service listens (accelerated-services-spec.md 5).
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"
# airlock_venv_ensure: the venv, built with a Python that can install this
# requirement set, verified by its entry point rather than by its existence.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../tools/airlock-venv.sh"
# Named so a failure can tell the operator which mode variable to set instead.
AIRLOCK_SERVICE=DEEPFACE

# Keep the local processing DB and model cache beside the code, matching the
# container's mounted volumes so both modes behave the same.
export FACE_LABELS_DB="${FACE_LABELS_DB:-$SCRIPT_DIR/data/face_labels.db}"
mkdir -p "$SCRIPT_DIR/data" "$SCRIPT_DIR/uploads" "$SCRIPT_DIR/database_images"

# ── Model weights ─────────────────────────────────────────────────────────────
# DeepFace looks for weights in $DEEPFACE_HOME/.deepface/weights, defaulting
# DEEPFACE_HOME to the user's home directory. Without this it searches
# ~/.deepface/weights, finds nothing, and re-downloads models that are already
# sitting in this repo - which is what caused the boot crash loop.
#
# The container gets this for free: it mounts ./deepface_weights at
# /home/appuser/.deepface with HOME=/home/appuser. Host mode reproduces the same
# layout with a symlink, so both modes share one weights cache.
if [[ ! -e "$SCRIPT_DIR/.deepface" ]]; then
  ln -s deepface_weights "$SCRIPT_DIR/.deepface"
fi
export DEEPFACE_HOME="${DEEPFACE_HOME:-$SCRIPT_DIR}"
mkdir -p "$SCRIPT_DIR/deepface_weights/weights"

# ── macOS fork safety ─────────────────────────────────────────────────────────
# gunicorn forks its workers. macOS refuses to run an Objective-C class's
# +initialize inside a fork child if it might have been mid-initialization in the
# parent - it aborts the child instead. Anything touching Foundation (an HTTPS
# download, some CoreFoundation-backed calls) can trip this during app import,
# which under gunicorn happens in the worker. The result is a worker that dies on
# boot, forever:
#   objc[...]: +[NSCharacterSet initialize] may have been in progress ...
#   [ERROR] Worker was sent SIGKILL! Perhaps out of memory?   <- misleading
#
# Three defences, all applied:
#   1. this variable, which tells the ObjC runtime not to abort in the child
#   2. DEEPFACE_HOME above + the --setup pre-fetch, so no worker ever downloads
#      weights at boot (the download was what dragged Foundation in)
#   3. NOT using --preload - see the note above the gunicorn line. --preload was
#      once the headline defence here and is now the opposite: importing the app
#      in the master starts TensorFlow's and OpenCV's worker threads, and those
#      do not survive fork().
export OBJC_DISABLE_INITIALIZE_FORK_SAFETY=YES

setup_venv() {  # setup_venv [--setup]
  airlock_venv_ensure "$VENV" "$SCRIPT_DIR/requirements.txt" gunicorn "${1:-}" || return 1

  # tensorflow-metal is deliberately NOT installed here.
  #
  # Apple's plugin stopped at v1.2.0 (January 2025), which supports TensorFlow
  # up to 2.18. requirements.txt pulls a much newer TensorFlow, so the plugin
  # would either refuse to register its PluggableDevice or register a broken
  # one - and an incompatible Metal device does not fall back to CPU quietly,
  # it crashes or returns wrong tensors.
  #
  # It would also barely matter. Metal only touches the VGG-Face embedding
  # step; YuNet detection runs through OpenCV's DNN module, which has no Metal
  # backend, and embeddings are cached per face after first computation. The
  # ingestion cost is decode/encode/transfer, not matrix multiplication - see
  # documentation/features/INGESTION_PERFORMANCE.md.
  #
  # If Apple ships a plugin for your TensorFlow version, install it by hand:
  #   https://developer.apple.com/metal/tensorflow-plugin/
  if [[ "$AIRLOCK_OS" == "Darwin" && "$AIRLOCK_ARCH" == "arm64" ]]; then
    _tf_ver="$("$VENV/bin/python3" -m pip show tensorflow 2>/dev/null | awk '/^Version:/{print $2}')"
    echo "==> Apple Silicon, tensorflow ${_tf_ver:-unknown}"
    echo "    tensorflow-metal supports TensorFlow <= 2.18 and is not installed."
    echo "    Detection does not use it. See INGESTION_PERFORMANCE.md."
  fi

  echo "==> Pre-fetching model weights (so no worker ever downloads at boot)"
  # Deliberately done here, in a plain single process. A download inside a forked
  # gunicorn worker is what triggers the macOS ObjC fork-safety abort.
  DEEPFACE_HOME="$DEEPFACE_HOME" "$VENV/bin/python3" \
    "$SCRIPT_DIR/../../tools/prefetch_weights.py" \
    || echo "!! weight pre-fetch failed; it will be retried at first use"
}

check_gpu() {
  # The venv's own interpreter, not an activated `python3`: this also runs from
  # --check, where sourcing activate would mask a venv that is not usable.
  "$VENV/bin/python3" - <<'PY'
import sys
try:
    import tensorflow as tf
except Exception as e:
    print(f"  tensorflow: NOT IMPORTABLE ({e})"); sys.exit(1)
print(f"  tensorflow        : {tf.__version__}")
gpus = tf.config.list_physical_devices('GPU')
print(f"  GPU devices       : {gpus if gpus else 'none'}")
try:
    import tensorflow_metal  # noqa: F401
    print("  tensorflow-metal  : installed")
except Exception:
    print("  tensorflow-metal  : not installed (supports TF <= 2.18; expected)")
print()
if gpus:
    print("  GPU acceleration ACTIVE for VGG-Face embeddings.")
else:
    print("  Running on CPU. This is the expected state and is not the")
    print("  bottleneck: YuNet detection uses OpenCV's DNN module, which has no")
    print("  Metal backend, and embeddings are cached per face. Ingestion speed")
    print("  comes from DETECTION_MAX_DIM / DETECTION_CONCURRENCY instead - see")
    print("  documentation/features/INGESTION_PERFORMANCE.md.")
PY
}

case "${1:-}" in
  --setup) setup_venv --setup || exit 1 ;;
  --check)
    echo "  platform        : $(airlock_platform)"
    echo "  venv            : $(airlock_venv_state "$VENV" "$SCRIPT_DIR/requirements.txt" gunicorn)"
    echo "  host python     : $(airlock_python_status)"
    [[ -x "$VENV/bin/gunicorn" ]] || { echo "  Run ./start-host.sh --setup first."; exit 1; }
    check_gpu; exit 0 ;;
esac

# Every start, not only the first: the venv is checked against its entry point
# and a stamp of requirements.txt, so a half-built one is rebuilt rather than
# mistaken for a finished one, and an edited requirements.txt is installed.
setup_venv || exit 1

# shellcheck disable=SC1091
source "$VENV/bin/activate"

echo "==> backend-deepface (host mode)"
echo "    port            : $PORT"
echo "    FACE_LABELS_DB  : $FACE_LABELS_DB"
echo "    DEEPFACE_HOME   : $DEEPFACE_HOME"
echo "    weights         : $(ls "$SCRIPT_DIR/deepface_weights/weights" 2>/dev/null | wc -l | tr -d ' ') cached"
check_gpu || true
echo

# Concurrency.
#
# Processes, not threads: OpenCV's FaceDetectorYN carries mutable state
# (setInputSize / setScoreThreshold are called per request), and DeepFace hands
# out one cached detector per process - so two threads sharing it would race.
# Separate workers each get their own copy and cannot interfere.
#
# NO --preload. This is deliberate and load-bearing.
#
# --preload imports the app in the gunicorn master, which runs the model
# warm-ups there, which starts TensorFlow's Eigen/OpenMP thread pools and
# OpenCV's parallel_for pool in the master. gunicorn then fork()s without
# exec(). POSIX only carries the calling thread into the child: every other
# thread vanishes mid-stride, and any mutex it held stays locked forever. The
# child's first touch of that runtime then dies on a lock held by a thread that
# no longer exists:
#   [WARNING] Worker (pid:NNNNN) was sent SIGSEGV!
# ...on every worker, immediately, forever.
#
# Without --preload the master stays a plain Python process that never imports
# TensorFlow or OpenCV. Each worker forks first and imports second, so every
# model is built in a process that has never been forked out of. Costs one model
# load per worker (~600 MB resident, ~15 s boot) and buys a service that starts.
#
# The original reason --preload was added - a weights *download* aborting inside
# a fork child - is handled independently by DEEPFACE_HOME, the --setup
# pre-fetch, and OBJC_DISABLE_INITIALIZE_FORK_SAFETY above.
#
# Default: half the cores, capped at 4 - the workflow only keeps
# DETECTION_CONCURRENCY images in flight. Without --preload nothing is shared
# copy-on-write, so each worker costs ~600 MB resident once VGG-Face is built.
# Override with DEEPFACE_WORKERS; set DEEPFACE_WARMUP_EMBEDDER=0 to skip
# building VGG-Face at boot if this host only runs detection.
if [[ -z "${DEEPFACE_WORKERS:-}" ]]; then
  _cores="$(sysctl -n hw.ncpu 2>/dev/null || echo 4)"
  DEEPFACE_WORKERS=$(( _cores / 2 ))
  (( DEEPFACE_WORKERS < 1 )) && DEEPFACE_WORKERS=1
  (( DEEPFACE_WORKERS > 4 )) && DEEPFACE_WORKERS=4
fi

echo "    workers         : $DEEPFACE_WORKERS"
echo

# Not 0.0.0.0: the gateway is the way in from outside (accelerated-services-spec.md
# section 5). On macOS podman's VM reaches the host's 127.0.0.1 through
# host.containers.internal, so loopback is enough (verified 2026-10-02); on
# Linux the address containers use for the host is added.
BINDS=()
for addr in $(airlock_native_binds); do BINDS+=(--bind "${addr}:${PORT}"); done
# deploy/airlock.env is sourced, not exported: the detector's cutoff has to be
# handed on explicitly, as start-podman.sh does with -e.
export YUNET_SCORE_THRESHOLD="${YUNET_SCORE_THRESHOLD:-0.9}"
exec gunicorn --workers="$DEEPFACE_WORKERS" --threads=1 --timeout=180 \
     --graceful-timeout=30 \
     "${BINDS[@]}" app:app
