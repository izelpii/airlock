#!/usr/bin/env bash
# Build and run the backend-deepface container with podman.
#
# Usage (run directly from systems/backend-deepface/):
#   ./start-podman.sh          # build (if needed) and run
#   ./start-podman.sh --build  # force rebuild before running

set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")"
# The script's own directory, not $(pwd): start-all.sh invokes this from the
# repository root, and $(pwd) then pointed the build and every mount at the
# wrong place.
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Host paths and the rootless-podman user namespace come from one place.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"

# 'gpu' mode (start-all.sh sets AIRLOCK_GPU=1; accelerated-services-spec.md 3.3):
# the CUDA build of the image, and the NVIDIA GPU passed in through CDI.
IMAGE=backend-deepface
GPU_FLAGS=()
BUILD_ARGS=()
if [[ "${AIRLOCK_GPU:-0}" == "1" ]]; then
  IMAGE=backend-deepface-gpu
  GPU_FLAGS=(--device nvidia.com/gpu=all)
  BUILD_ARGS=(--build-arg GPU=1)
fi

# Build the image (build context is systems/backend-deepface/ — all sources live there).
if [[ "${1:-}" == "--build" ]] || ! podman image exists "$IMAGE" 2>/dev/null; then
  echo "Building $IMAGE image..."
  airlock_prepare_build
  podman build \
    ${BUILD_ARGS[@]+"${BUILD_ARGS[@]}"} \
    --ignorefile "$SCRIPT_DIR/.dockerignore" \
    -f "$SCRIPT_DIR/Dockerfile" \
    -t "$IMAGE" \
    "$SCRIPT_DIR"
fi

# Ensure host volume directories exist before mounting
mkdir -p "${SCRIPT_DIR}/uploads" "${SCRIPT_DIR}/database_images" "${SCRIPT_DIR}/deepface_weights"

# Workers sized to the memory the container can actually have, not to the cores.
# Each worker builds its own VGG-Face (~600 MB resident; the models cannot
# survive a fork, see start-host.sh), so on macOS - where every container shares
# one 2 GB-by-default podman VM - a fixed 2 workers is what produced an endless
# "Worker was sent SIGKILL! Perhaps out of memory?" with nothing naming memory.
WORKERS="$(airlock_deepface_workers)"
MEM_STATUS="$(airlock_container_memory_status)"

# Below the floor, DON'T BUILD VGG-FACE AT BOOT. The embedding model is the
# single largest allocation in the process, and a worker killed while building
# it never reaches the point of answering anything - so a cramped machine gets
# a service that is down rather than one that is merely slower.
#
# With the warm-up off, the worker boots, /api/health answers, and YuNet
# detection (a few MB through OpenCV's DNN module) works. The embedder is then
# built on the first /api/embed request, which may still fail here - but
# "recognition needs more memory" is a far better failure than "the face
# service never started".
# Which model this host embeds with, and which to pre-load.
#
# app.py has read DEEPFACE_MODEL and DEEPFACE_WARMUP_MODELS since embedding
# provenance landed, but nothing passed them in - so the default was the only
# reachable setting. That matters more than a missing knob: the warm-up builds
# the DEFAULT model, and VGG-Face's weights are a 560 MB download, which a host
# that only ever wanted SFace should not be made to fetch. Set DEEPFACE_MODEL
# in deploy/airlock.env.
#
# See documentation/specs/face-recognition-migration-spec.md.
MODEL="${DEEPFACE_MODEL:-VGG-Face}"
WARMUP_MODELS="${DEEPFACE_WARMUP_MODELS:-$MODEL}"

WARMUP="${DEEPFACE_WARMUP_EMBEDDER:-1}"
if [[ "$MEM_STATUS" != ok* ]]; then
  echo "!! ${MEM_STATUS#no: }" >&2
  if [[ -z "${DEEPFACE_WARMUP_EMBEDDER:-}" ]]; then
    WARMUP=0
    echo "   Starting $WORKERS worker(s) with the $MODEL warm-up OFF so it can" >&2
    echo "   boot at all: detection will work, embeddings are built on first use." >&2
  else
    echo "   Starting $WORKERS worker(s); expect restarts under load." >&2
  fi
fi

echo "Starting backend-deepface on http://localhost:5001 ($WORKERS worker(s), $MODEL)"
podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name backend-deepface -p 5001:5001 \
  ${AIRLOCK_USERNS} \
  ${GPU_FLAGS[@]+"${GPU_FLAGS[@]}"} \
  -v "${SCRIPT_DIR}/uploads":/app/uploads:z \
  -v "${SCRIPT_DIR}/database_images":/app/database_images:z \
  -v "${SCRIPT_DIR}/deepface_weights":/home/appuser/.deepface:z \
  -e DEEPFACE_WORKERS="$WORKERS" \
  -e DEEPFACE_WARMUP_EMBEDDER="$WARMUP" \
  -e DEEPFACE_MODEL="$MODEL" \
  -e DEEPFACE_WARMUP_MODELS="$WARMUP_MODELS" \
  -e YUNET_SCORE_THRESHOLD="${YUNET_SCORE_THRESHOLD:-0.9}" \
  "$IMAGE"
