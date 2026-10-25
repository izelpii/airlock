#!/usr/bin/env bash
# Build and run the backend-geoloc container with podman.
#
# Usage:
#   ./start-podman.sh          # build (if needed) and run
#   ./start-podman.sh --build  # force rebuild before running

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Host paths and the rootless-podman user namespace come from one place,
# so moving to another machine is one file rather than one edit per script.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"


# Build the image (build context is systems/backend-geoloc/ — all sources live there).
if [[ "${1:-}" == "--build" ]] || ! podman image exists backend-geoloc 2>/dev/null; then
  echo "Building backend-geoloc image..."
  airlock_prepare_build
  podman build \
    --ignorefile "$SCRIPT_DIR/.dockerignore" \
    -f "$SCRIPT_DIR/Dockerfile" \
    -t backend-geoloc \
    "$SCRIPT_DIR"
fi

echo "Starting backend-geoloc on http://localhost:5002"
podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name backend-geoloc -p 5002:5002 \
  ${AIRLOCK_USERNS} \
  backend-geoloc
