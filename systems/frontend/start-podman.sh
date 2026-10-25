#!/usr/bin/env bash
# Build and run the workflow-frontend container with podman.
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


# ── Build the image ───────────────────────────────────────────────────────────
# This is a two-stage build: `npm run build` runs INSIDE the image, and nginx
# serves the compiled bundle. Nothing is mounted from the host, so editing src/
# changes nothing at all until the image is rebuilt - and because Create React
# App inlines REACT_APP_* at build time, there is no runtime configuration to
# fall back on either.
#
# Building only when the image was missing meant a months-old bundle could be
# served indefinitely while the source said otherwise. So: rebuild whenever any
# source file is newer than the image.
SOURCE_PATHS=(src public package.json package-lock.json Dockerfile)

# Newest mtime, in epoch seconds, across a set of files and directory trees.
# BSD stat (macOS) and GNU stat spell this differently, and -f means "format" on
# one and "file system" on the other - so validate rather than trust either.
newest_mtime() {
  local newest=0 v
  while IFS= read -r f; do
    v="$(stat -f %m "$f" 2>/dev/null)" || v=""
    [[ "$v" =~ ^[0-9]+$ ]] || v="$(stat -c %Y "$f" 2>/dev/null)" || v=""
    [[ "$v" =~ ^[0-9]+$ ]] || continue
    (( v > newest )) && newest="$v"
  done < <(find "$@" -type f -not -path '*/node_modules/*' 2>/dev/null)
  printf '%s' "$newest"
}

needs_build() {
  [[ "${1:-}" == "--build" ]] && { echo "forced with --build"; return 0; }
  podman image exists workflow-frontend 2>/dev/null || { echo "no image yet"; return 0; }

  local raw built newest
  raw="$(podman image inspect workflow-frontend --format '{{.Created}}' 2>/dev/null)" \
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
  # An unreadable timestamp must rebuild, not skip: serving a stale bundle is the
  # exact failure this guard exists to prevent.
  [[ "$built" =~ ^[0-9]+$ ]] || { echo "cannot parse image timestamp"; return 0; }

  local paths=()
  for p in "${SOURCE_PATHS[@]}"; do [[ -e "$SCRIPT_DIR/$p" ]] && paths+=("$SCRIPT_DIR/$p"); done
  newest="$(newest_mtime "${paths[@]}")"
  if (( newest > built )); then
    echo "sources are newer than the image"
    return 0
  fi
  return 1
}

# The webpack build runs inside the podman machine's VM and is bounded by that
# VM's memory, not the Mac's. Report it up front so an out-of-memory failure is
# immediately legible rather than a mystery.
report_machine_memory() {
  local mem
  mem="$(podman machine inspect --format '{{.Resources.Memory}}' 2>/dev/null | head -1)" || return 0
  [[ "$mem" =~ ^[0-9]+$ ]] || return 0
  echo "    podman machine memory: ${mem} MB"
  if (( mem < 4096 )); then
    echo "    NOTE: under 4 GB. If the build is killed, raise it (see below)."
  fi
}

build_failed_help() {
  cat <<'HELP'

The image build failed. If the message mentioned "the process exited too early"
or running out of memory, webpack was killed by the podman VM's memory limit.

In order of preference:

  1. Give the VM more memory (it keeps the setting):
       podman machine stop
       podman machine set --memory 8192 --cpus 4
       podman machine start

  2. Lower the heap cap so V8 collects earlier in a small VM:
       podman build --build-arg NODE_BUILD_HEAP_MB=2048 \
         -f Dockerfile -t workflow-frontend .

  3. Build on the Mac instead, where there is real RAM, then serve that output:
       npm ci && npm run build
     and tell me - the Dockerfile can copy build/ rather than compiling in the VM.
HELP
}

if reason="$(needs_build "${1:-}")"; then
  echo "Building workflow-frontend image ($reason)..."
  airlock_prepare_build
  report_machine_memory
  # --ulimit: a build step inherits the shell's soft open-file limit (1024 on
  # oldserver), and npm ci runs out of it - "EMFILE: too many open files".
  if ! podman build \
      --ulimit nofile=65536:65536 \
      --ignorefile "$SCRIPT_DIR/.dockerignore" \
      -f "$SCRIPT_DIR/Dockerfile" \
      -t workflow-frontend \
      "$SCRIPT_DIR"; then
    build_failed_help
    exit 1
  fi
else
  echo "Image is up to date with src/."
fi

echo "Starting workflow-frontend on http://localhost:3000"
podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name workflow-frontend -p 3000:3000 workflow-frontend
