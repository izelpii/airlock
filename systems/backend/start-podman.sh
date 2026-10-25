#!/usr/bin/env bash
# Build and run the workflow-backend container with podman.
#
# Usage:
#   ./start-podman.sh          # build (if needed) and run
#   ./start-podman.sh --build  # force rebuild before running
#
# ── Catalog data directory ────────────────────────────────────────────────────
# All catalogs live under a single host directory that is mounted into the
# container at /catalogs.  When creating a catalog in the UI, set its
# Target Directory to /catalogs/<your-catalog-name>.
#
# To move this system to a new machine, set CATALOGS_HOST_DIR in
# deploy/airlock.env - not here. This script no longer names any machine.
#
# ── Input directory ───────────────────────────────────────────────────────────
# Workflows that read source files (e.g. io_picture_import_workflow.py) expect
# an input directory mounted at /input inside the container.  Point
# INPUT_HOST_DIR at the folder on this machine that contains the files to
# import.  When creating a workflow in the UI, set Source Directory to /input
# (or a subdirectory of it).
#
# To move this system to a new machine, set INPUT_HOST_DIR in
# deploy/airlock.env - not here.

# ── Storage root ──────────────────────────────────────────────────────────────
# The one directory tree that storage routes may point inside
# (documentation/specs/local-storage-spec.md). Everything under it is readable
# by the backend, READ-ONLY; nothing outside it can be made into a route.
#
# A bind mount is fixed when the container is created, so a folder named in the
# UI cannot be read unless it is already inside something mounted. Mounting one
# root once is what lets a route created at 3pm work at 3pm.
#
# $HOME by default: on a Linux host the natural arrangement is one tree holding
# everything (/srv/media, /mnt/photos, a mounted array) named by one variable.
# Narrow it whenever you like:  STORAGE_ROOT_HOST=$HOME/Pictures ./start-podman.sh
# (set in deploy/airlock.env; defaults to $HOME)
#
# ── Deepface service URL ──────────────────────────────────────────────────────
# The face detection workflow calls a deepface HTTP backend.
# When the backend runs in a container but deepface runs directly on the host,
# use host.containers.internal (Podman's built-in alias for the host).
# If deepface is ever containerised, change this to its container hostname.
# (set in deploy/airlock.env)
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Every host-specific path and the rootless-podman user namespace come from one
# place, so moving this system to another machine is one file, not one edit per
# script. See deploy/README.md.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"

# ── Build the image ───────────────────────────────────────────────────────────
# Only workflows/ is bind-mounted from the host. app.py, models.py, catalog.py,
# file_manager.py and the shared modules are COPYed into the image, so editing
# them changes nothing until the image is rebuilt.
#
# That asymmetry caused a real and hard-to-see failure: schema changes added to
# models.py were never applied, because the running container held an older
# models.py, while workflow edits beside them took effect immediately. Everything
# looked deployed. So this script now rebuilds automatically whenever a baked-in
# source file is newer than the image, rather than only when asked.
BAKED_SOURCES=(Dockerfile requirements.txt app.py models.py catalog.py
               file_manager.py face_recognition_schema.py migration_utils.py)

# Epoch seconds for a file's mtime. BSD stat (macOS) and GNU stat (Linux) spell
# this differently, and -f means "format" on one and "file system" on the other -
# so a wrong guess prints filesystem details instead of failing cleanly. Validate
# the result rather than trusting either.
mtime_epoch() {
  local v
  v="$(stat -f %m "$1" 2>/dev/null)" || v=""
  [[ "$v" =~ ^[0-9]+$ ]] || v="$(stat -c %Y "$1" 2>/dev/null)" || v=""
  [[ "$v" =~ ^[0-9]+$ ]] || v=0
  printf '%s' "$v"
}

needs_build() {
  [[ "${1:-}" == "--build" ]] && { echo "forced with --build"; return 0; }
  podman image exists workflow-backend 2>/dev/null || { echo "no image yet"; return 0; }

  local built newest raw
  raw="$(podman image inspect workflow-backend --format '{{.Created}}' 2>/dev/null)" \
    || { echo "cannot read image timestamp"; return 0; }
  # Two things matter here, and both were wrong before:
  #   the separator - podman prints "2026-09-04 20:23:06", with a SPACE, so a
  #     format containing a literal T never matched and this whole check fell
  #     through to "cannot parse" and rebuilt unconditionally;
  #   -u           - podman reports Created in UTC, and without it BSD date reads
  #     those digits as LOCAL time, making the image look newer than it is by the
  #     UTC offset (4h on US Eastern) and hiding every edit inside that window.
  # Together they would have skipped rebuilds for recently edited sources.
  built="$(date -j -u -f '%Y-%m-%d %H:%M:%S' "${raw:0:19}" +%s 2>/dev/null)" || built=""
  # GNU date gets the string with its offset, minus podman's trailing zone
  # name: "2026-10-02 14:17:02.39 +0000 UTC" names the zone twice and GNU date
  # rejects it, so on Linux every start fell through to "cannot parse" and
  # rebuilt the image (seen on oldserver, podman 3.4.4).
  [[ "$built" =~ ^[0-9]+$ ]] || built="$(date -u -d "${raw% [A-Z]*}" +%s 2>/dev/null)" || built=""
  # An unreadable timestamp must rebuild, not silently skip: skipping is the
  # failure mode this function exists to prevent.
  [[ "$built" =~ ^[0-9]+$ ]] || { echo "cannot parse image timestamp"; return 0; }

  for f in "${BAKED_SOURCES[@]}"; do
    [[ -f "$SCRIPT_DIR/$f" ]] || continue
    newest="$(mtime_epoch "$SCRIPT_DIR/$f")"
    if (( newest > built )); then
      echo "$f is newer than the image"
      return 0
    fi
  done
  return 1
}

if reason="$(needs_build "${1:-}")"; then
  echo "Building workflow-backend image ($reason)..."
  airlock_prepare_build
  # The version the image will report. Read here, where the repository and its
  # .git are both reachable, because neither is inside the build context.
  # `git describe` adds how far past the release this build is; without a tag or
  # without git, the VERSION file alone still answers.
  _repo_root="$(cd "$SCRIPT_DIR/../.." && pwd)"
  _version="$(cat "$_repo_root/VERSION" 2>/dev/null | tr -d '[:space:]')"
  _described="$(git -C "$_repo_root" describe --tags --long --dirty 2>/dev/null)"
  if [[ -n "$_described" && "$_described" != *-0-g* ]]; then
    # v5.0.0-12-gabc1234 -> 5.0.0+12.abc1234, matching version.py
    _count="$(echo "$_described" | awk -F- '{print $(NF-1)}')"
    _sha="$(echo "$_described" | awk -F- '{print $NF}' | sed 's/^g//')"
    [[ "$_count" =~ ^[0-9]+$ ]] && _version="${_version}+${_count}.${_sha}"
  fi
  echo "  version: ${_version:-unknown}"

  podman build \
    --build-arg "AIRLOCK_VERSION=${_version:-unknown}" \
    --ignorefile "$SCRIPT_DIR/.dockerignore" \
    -f "$SCRIPT_DIR/Dockerfile" \
    -t workflow-backend \
    "$SCRIPT_DIR"
else
  echo "Image is up to date with the baked-in sources."
fi

# ── Import smoke test ─────────────────────────────────────────────────────────
# Verify the image can actually import the app before handing it to gunicorn.
#
# A missing module is invisible to everything else: the source is correct, the
# tests pass on the host, the image builds cleanly - and then gunicorn boot-loops
# with ModuleNotFoundError buried in a stack trace. That has happened twice.
# Ten seconds here turns it into one legible line.
#
# The scheduler is disabled for the check so importing the app does not start
# executing the user's queue inside a throwaway container.
echo "Checking the image can import the app..."
# AIRLOCK_IMPORT_CHECK stops app.py creating the database and cleaning up
# executions. This check answers "is the source present and importable"; it is
# not a request for writable storage, and conflating the two turned a
# permissions problem on a fresh Linux host into "cannot import app.py".
if ! _smoke="$(podman run --rm --entrypoint python3 \
      -e DISABLE_WORKFLOW_SCHEDULER=1 \
      -e AIRLOCK_IMPORT_CHECK=1 \
      workflow-backend -c 'import app; print("import ok")' 2>&1)"; then
  echo
  echo "The image cannot import app.py:"
  echo "$_smoke" | tail -20
  echo
  echo "If this is ModuleNotFoundError, a file is missing from the image rather"
  echo "than from the source tree. The Dockerfile copies *.py from this"
  echo "directory - check the module is here and not excluded by .dockerignore."
  exit 1
fi
echo "    $(echo "$_smoke" | tail -1)"

echo "Starting workflow-backend on http://localhost:5000"
# ── Input directory preflight ─────────────────────────────────────────────────
# A bind mount resolves once, at container start. If INPUT_HOST_DIR is a symlink,
# resolve it here so the container mounts the real directory rather than depending
# on how the container runtime happens to treat the link.
if [[ -L "$INPUT_HOST_DIR" ]]; then
  RESOLVED="$(cd "$INPUT_HOST_DIR" && pwd -P)"
  echo "Note: INPUT_HOST_DIR is a symlink; mounting its target instead:"
  echo "  $INPUT_HOST_DIR -> $RESOLVED"
  INPUT_HOST_DIR="$RESOLVED"
fi

if [[ ! -d "$INPUT_HOST_DIR" ]]; then
  echo "ERROR: input directory does not exist: $INPUT_HOST_DIR" >&2
  echo "       Update INPUT_HOST_DIR in this script." >&2
  exit 1
fi

if [[ -z "$(ls -A "$INPUT_HOST_DIR" 2>/dev/null)" ]]; then
  echo "WARNING: input directory is empty: $INPUT_HOST_DIR" >&2
fi

# macOS keeps ~/Desktop, ~/Documents and ~/Downloads behind its privacy controls.
# The podman machine's file-sharing helper needs permission to read them, or the
# directory mounts but appears EMPTY inside the container - which looks exactly
# like a broken import.
case "$INPUT_HOST_DIR" in
  "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*)
    echo "Note: input lives under a macOS privacy-protected folder."
    echo "      If the container sees it as empty, grant Full Disk Access to podman"
    echo "      in System Settings > Privacy & Security, or move the source"
    echo "      somewhere outside Desktop/Documents/Downloads."
    ;;
esac

if [[ ! -d "$STORAGE_ROOT_HOST" ]]; then
  echo "ERROR: storage root does not exist: $STORAGE_ROOT_HOST" >&2
  echo "       Set STORAGE_ROOT_HOST to a directory that does." >&2
  exit 1
fi

# The same macOS privacy trap as the input directory, and it now applies to any
# route a user creates under these folders: they mount EMPTY rather than failing.
case "$STORAGE_ROOT_HOST" in
  "$HOME"/Desktop/*|"$HOME"/Documents/*|"$HOME"/Downloads/*|"$HOME")
    echo "Note: storage routes under ~/Desktop, ~/Documents or ~/Downloads need"
    echo "      Full Disk Access for the podman helper, or they appear EMPTY"
    echo "      inside the container. System Settings > Privacy & Security."
    ;;
esac

# Import tuning, passed through only when set - in the environment or in
# deploy/airlock.env: the IMPORT_* settings (IMPORT_READERS,
# IMPORT_PROBE_CONCURRENCY, ...; see parallelism.md 6.2 and
# ingestion-architecture.md) and AIRLOCK_INGEST_SCRIPT, the import new storage
# routes get. compgen -v also sees variables airlock.env set without exporting.
# The workflows read them in the container; nothing else does.
# Expanded below as ${_IMPORT_FLAGS[@]+"${_IMPORT_FLAGS[@]}"}: macOS ships bash
# 3.2, where "${arr[@]}" of an EMPTY array is an "unbound variable" under set -u -
# so on a Mac with no IMPORT_* set the backend never started.
_IMPORT_FLAGS=()
for _name in $(compgen -v | grep -E '^(IMPORT_[A-Z0-9_]+|AIRLOCK_INGEST_SCRIPT)$'); do
  _IMPORT_FLAGS+=(-e "${_name}=${!_name}")
  echo "Passing ${_name}=${!_name} to the backend"
done

# The storage root is read-write so a move-mode import can take files out of a
# route the owner marked "imports may move files" (ingest-move-spec.md 8.1).
# STORAGE_ROOT_READONLY=1 restores the read-only mount: every route is then
# read-only to the kernel, whatever its flag says.
if [ "${STORAGE_ROOT_READONLY:-0}" = "1" ]; then
  _STORAGE_MOUNT_OPTS="ro,z"
else
  _STORAGE_MOUNT_OPTS="z"
fi

# AIRLOCK_ALLOWED_ORIGINS only adds GENUINELY cross-origin callers. Same-origin
# is decided per request inside app.py (_origin_allowed), so a machine reached at
# a new address needs nothing set here. See deploy/airlock-env.sh.
# The license folder (airlock_license.py): read-only, and made if missing - a
# bind mount of a folder that does not exist stops the container from starting.
mkdir -p "${AIRLOCK_ROOT}/license"
podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name workflow-backend -p 5000:5000 \
  ${AIRLOCK_USERNS} \
  ${_IMPORT_FLAGS[@]+"${_IMPORT_FLAGS[@]}"} \
  -e "DEEPFACE_URL=${DEEPFACE_URL}" \
  -e "TRANSCODE_URL=${TRANSCODE_URL}" \
  -e "STORAGE_ROOT_HOST=${STORAGE_ROOT_HOST}" \
  -e "STORAGE_ROOT_CONTAINER=${STORAGE_ROOT_CONTAINER}" \
  -e "ALBUMS_URL=${ALBUMS_URL}" \
  -e "BACKEND_URL=${BACKEND_URL}" \
  -e "AIRLOCK_ALLOWED_ORIGINS=${AIRLOCK_ALLOWED_ORIGINS}" \
  -e "WORMHOLE_TRANSIT_RELAY=${WORMHOLE_TRANSIT_RELAY:-}" \
  --add-host=host.containers.internal:${AIRLOCK_HOST_ALIAS} \
  --add-host=airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=backend.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=deepface.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=vision.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=geoloc.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=albums.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=gallery.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=ollama.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=transcode.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  --add-host=pets.airlock.lan:${AIRLOCK_HOST_ALIAS} \
  -v "${DATABASE_HOST_DIR}":/app/database:z \
  -v "${AIRLOCK_ROOT}/license":/license:ro,z \
  -v "${SCRIPT_DIR}/workflows":/app/workflows:z \
  -v "${CATALOGS_HOST_DIR}":/catalogs:z \
  -v "${INPUT_HOST_DIR}":/input:z \
  -v "${STORAGE_ROOT_HOST}":"${STORAGE_ROOT_CONTAINER}":${_STORAGE_MOUNT_OPTS} \
  workflow-backend
