#!/usr/bin/env bash
# Build and run the album-gallery container with podman.
#
# Usage:
#   ./start-podman.sh          # build (if needed) and run
#   ./start-podman.sh --build  # force rebuild before running
#
# ── What this serves ──────────────────────────────────────────────────────────
# DEPLOYMENTS: named, shareable galleries the main backend publishes to
#   <catalog>/cache/Deployments/<slug>/
# reachable at /d/<catalog-slug>/<deployment-slug>/. Each is an albums.json plus
# one relative symlink per album into that catalog's cache/Albums, which in turn
# link into cache/Images. Nothing is copied at any level, and creating a
# deployment touches no container: this one is started once and finds the new
# directory on the next request.
#
# ── Why the CATALOGS root and not one catalog's cache ─────────────────────────
# Deployments live inside their catalogs, and there is more than one catalog. The
# URL names the catalog, so a request resolves by substitution:
#   /d/family/summer-2019/  ->  $CATALOGS_HOST_DIR/family/cache/Deployments/summer-2019/
# The slug-to-directory mapping comes from catalogs.json, which the backend
# writes into this same root on catalog create, rename and delete.
#
# Mounted READ-ONLY. A gallery that may be exposed to viewers has no business
# writing anything, and the server never tries.
#
# NOTE: this mounts the whole catalogs root, which includes the originals
# (RAW/HEIC), workflow_results.db and the face crops. The server does not serve
# them: every asset path is realpath-resolved and must land under that catalog's
# cache/, the album folder must appear in the deployment's manifest, and no path
# component may begin with a dot. Read-only plus those three checks is what
# replaces the old "mount only cache/" boundary, which cannot express more than
# one catalog.
#
# To move this system to a new machine, update CATALOGS_HOST_DIR below (and the
# matching hostPath in album-gallery-pod.yaml).

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Host paths and the rootless-podman user namespace come from one place,
# so moving to another machine is one file rather than one edit per script.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"


# Current host location of the catalogs root — the SAME directory the backend
# mounts at /catalogs. Both must point at the same place or a deployment
# resolves in one and not the other.
# CATALOGS_HOST_DIR comes from deploy/airlock-env.sh

# Signing key for password-protected deployments. Without it the server
# generates one at boot and everyone is signed out on every restart, which it
# says loudly. Set it here, or in the environment, to keep sessions.
ALBUM_SESSION_SECRET="${ALBUM_SESSION_SECRET:-}"

if [[ "${1:-}" == "--build" ]] || ! podman image exists album-gallery 2>/dev/null; then
  airlock_prepare_build
  # --ulimit: a build step inherits the shell's soft open-file limit (1024 on
  # oldserver), and npm ci runs out of it - "EMFILE: too many open files".
  _revision="$(airlock_gallery_revision)"
  echo "  gallery revision: $_revision"
  podman build \
    --ulimit nofile=65536:65536 \
    --build-arg "GALLERY_REVISION=$_revision" \
    --ignorefile "$SCRIPT_DIR/.dockerignore" \
    -f "$SCRIPT_DIR/Dockerfile" \
    -t album-gallery \
    "$SCRIPT_DIR"
fi

if [[ ! -d "$CATALOGS_HOST_DIR" ]]; then
  echo "Catalogs root not found: $CATALOGS_HOST_DIR" >&2
  echo "Update CATALOGS_HOST_DIR in this script." >&2
  exit 1
fi

if [[ ! -f "$CATALOGS_HOST_DIR/catalogs.json" ]]; then
  echo "Note: $CATALOGS_HOST_DIR/catalogs.json does not exist yet." >&2
  echo "  The backend writes it at startup and whenever a catalog changes." >&2
  echo "  Until it does, no deployment URL will resolve." >&2
fi

podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name album-gallery -p 3001:3001 \
  -e CATALOGS_ROOT=/catalogs \
  -e ALBUM_SESSION_SECRET="${ALBUM_SESSION_SECRET}" \
  -v "${CATALOGS_HOST_DIR}":/catalogs:ro,z \
  album-gallery
