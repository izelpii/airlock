#!/usr/bin/env bash
# Build and run the backend-albums container locally with Podman.
#
# Usage:
#   ./start-podman.sh [--build] [ollama-url]
#
#   --build     force a rebuild before running. start-all.sh passes it first.
#   ollama-url  defaults to http://ollama.airlock.lan, which the gateway routes
#               to Ollama on the host.

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# For AIRLOCK_DETACH_FLAG; this service has no host mounts of its own.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"

BUILD=0
if [[ "${1:-}" == "--build" ]]; then
  BUILD=1
  shift
fi

OLLAMA_URL="${1:-http://ollama.airlock.lan}"

# Build when asked, and whenever there is no image yet. This script used to
# only run: on a machine where the image had never been built by hand, podman
# went looking for "backend-albums" on Docker Hub and failed.
if [[ "$BUILD" == "1" ]] || ! podman image exists backend-albums 2>/dev/null; then
  echo "Building backend-albums image..."
  airlock_prepare_build
  podman build \
    --ignorefile "$SCRIPT_DIR/.dockerignore" \
    -f "$SCRIPT_DIR/Dockerfile" \
    -t backend-albums \
    "$SCRIPT_DIR" || exit 1
fi

podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name backend-albums -p 5005:5005 \
  -e ALBUMS_LLM_MODEL=granite3.3:8b \
  -e OLLAMA_HOST="${OLLAMA_URL}" \
  --add-host=host.containers.internal:${AIRLOCK_HOST_ALIAS} \
  --add-host=airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=backend.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=deepface.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=vision.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=geoloc.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=albums.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=gallery.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=ollama.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  backend-albums
