#!/usr/bin/env bash
# Build and run the backend-vision container with podman.
#
# Usage:
#   ./start-podman.sh          # build (if needed) and run
#   ./start-podman.sh --build  # force rebuild before running
#
# ── Catalog data directory ────────────────────────────────────────────────────
# The vision backend looks up face detections and people names from
# workflow_results.db files stored alongside catalog data.  Mount the same
# catalog root that systems/backend uses so the DB path resolution works
# identically inside the container.
#
# Current host location:
# CATALOGS_HOST_DIR comes from deploy/airlock-env.sh
#
# ── Ollama service URL ────────────────────────────────────────────────────────
# Ollama runs directly on the host (not in a container).
# host.containers.internal is Podman's built-in alias for the host machine
# when running inside a container; from the host itself, use localhost.
OLLAMA_HOST="${OLLAMA_HOST:-http://ollama.airlock.lan}"
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Host paths and the rootless-podman user namespace come from one place,
# so moving to another machine is one file rather than one edit per script.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"


# ── Local (non-container) mode ────────────────────────────────────────────────
# If podman is not available, run the app directly with the local Python venv.
if ! command -v podman &>/dev/null; then
  echo "podman not found – running locally with venv"
  export OLLAMA_HOST="http://localhost:11434"
  exec "${SCRIPT_DIR}/venv/bin/python" "${SCRIPT_DIR}/app.py"
fi

# Build the image (build context is systems/backend-vision/ — all sources live there).
if [[ "${1:-}" == "--build" ]] || ! podman image exists backend-vision 2>/dev/null; then
  echo "Building backend-vision image..."
  airlock_prepare_build
  podman build \
    --ignorefile "$SCRIPT_DIR/.dockerignore" \
    -f "$SCRIPT_DIR/Dockerfile" \
    -t backend-vision \
    "$SCRIPT_DIR"
fi

echo "Starting backend-vision on http://localhost:5003"
podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name backend-vision -p 5003:5003 \
  ${AIRLOCK_USERNS} \
  -e "OLLAMA_HOST=${OLLAMA_HOST}" \
  --add-host=host.containers.internal:${AIRLOCK_HOST_ALIAS} \
  --add-host=airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=backend.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=deepface.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=vision.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=geoloc.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=albums.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=gallery.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=ollama.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  -v "${CATALOGS_HOST_DIR}":/catalogs:z \
  backend-vision
