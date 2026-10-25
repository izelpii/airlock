#!/usr/bin/env bash
# Build and run the backend-pets container with podman.
#
# Usage:
#   ./start-podman.sh          # build (if needed) and run
#   ./start-podman.sh --build  # force rebuild before running

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"

if [[ "${1:-}" == "--build" ]] || ! podman image exists backend-pets 2>/dev/null; then
  echo "Building backend-pets image..."
  airlock_prepare_build
  podman build \
    --ignorefile "$SCRIPT_DIR/.dockerignore" \
    -f "$SCRIPT_DIR/Dockerfile" \
    -t backend-pets \
    "$SCRIPT_DIR"
fi

mkdir -p "$SCRIPT_DIR/weights"

# Which model, how big an input, how many at once. All have defaults in app.py;
# deploy/airlock.env is where a host overrides them, and these are passed
# explicitly so that a `podman play kube` deployment and this script agree -
# test_pod_manifests.py checks that they do.
DETECT_SIZE="${PETS_DETECT_SIZE:-416}"
CONCURRENCY="${PETS_CONCURRENCY:-2}"

echo "Starting backend-pets on http://localhost:5006 (detect at ${DETECT_SIZE}px)"
for m in yolox_tiny.onnx animal_clip_vit_b32.onnx; do
  [[ -s "$SCRIPT_DIR/weights/$m" ]] || \
    echo "  ! weights/$m is missing - its endpoint will answer 503. tools/fetch_pet_weights.sh" >&2
done

podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name backend-pets -p 5006:5006 \
  ${AIRLOCK_USERNS} \
  -v "${SCRIPT_DIR}/weights":/app/weights:z \
  -e PETS_DETECT_SIZE="$DETECT_SIZE" \
  -e PETS_CONCURRENCY="$CONCURRENCY" \
  backend-pets
