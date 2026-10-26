#!/usr/bin/env bash
# Build and run backend-transcode in a container.
# documentation/specs/accelerated-services-spec.md, section 3.2.
#
#   ./start-podman.sh            # build (if needed) and run, CPU only
#   ./start-podman.sh --build    # force a rebuild first
#   AIRLOCK_GPU=1 ./start-podman.sh   # pass the NVIDIA GPU in (start-all.sh
#                                     # sets this in 'gpu' mode)
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"

# The image carries the SAME video module the import uses, copied in fresh for
# every build - never edited here (see .gitignore).
SHARED="$SCRIPT_DIR/../backend/workflows/lib/video_proxy.py"
if [[ "${1:-}" == "--build" ]] || ! podman image exists backend-transcode 2>/dev/null \
   || ! cmp -s "$SHARED" "$SCRIPT_DIR/video_proxy.py" \
   || [[ "$SCRIPT_DIR/app.py" -nt "$SCRIPT_DIR/video_proxy.py" ]]; then
  cp "$SHARED" "$SCRIPT_DIR/video_proxy.py"
  echo "Building backend-transcode image..."
  airlock_prepare_build
  podman build --ulimit nofile=65536:65536 \
    --ignorefile "$SCRIPT_DIR/.dockerignore" \
    -f "$SCRIPT_DIR/Dockerfile" -t backend-transcode "$SCRIPT_DIR"
fi

GPU_FLAGS=()
MODE=container
if [[ "${AIRLOCK_GPU:-0}" == "1" ]]; then
  GPU_FLAGS=(--device nvidia.com/gpu=all)
  MODE=gpu
fi

# ── Intel/AMD video engines, and the Pi's ────────────────────────────────────
# NVIDIA goes in through CDI above; every other hardware encoder is reached by
# handing the container a DEVICE FILE:
#
#   /dev/dri/renderD*   Intel Quick Sync (h264_qsv) and VA-API (h264_vaapi)
#   /dev/video11        the Raspberry Pi 4's H.264 encoder (h264_v4l2m2m)
#
# Passed only when present, so this is a no-op on a Mac (podman's VM has no
# /dev/dri, and VideoToolbox is why host mode exists there) and on a server
# with no video engine. The service probes what it was given with a real
# one-frame encode, so passing a device that cannot encode costs nothing but
# the probe - which is exactly what happens on a machine running the open
# nouveau driver, where the render node exists and has no VA-API encoder.
#
# --group-add keep-groups: under ROOTLESS podman the container user keeps the
# invoking user's supplementary groups, which is what makes /dev/dri/renderD*
# (group `render`, mode 0660) openable inside. Without it the device is there
# and every open is EACCES. It needs the crun runtime, so it is added only when
# that is what podman uses - and the host user still has to BE in the group:
#   sudo usermod -aG render,video $USER     # then log out and back in
DEVICE_FLAGS=()
if [[ "$AIRLOCK_OS" == "Linux" && "${AIRLOCK_GPU:-0}" != "1" ]]; then
  if compgen -G "/dev/dri/renderD*" > /dev/null; then
    DEVICE_FLAGS+=(--device /dev/dri)
  fi
  # The Pi's encoder node specifically, not every /dev/video*: those are
  # cameras and capture devices, and a container has no business with them.
  if [[ -e /dev/video11 ]]; then
    DEVICE_FLAGS+=(--device /dev/video11)
  fi
  # keep-groups also needs podman 3.2 or newer. Older podman reads it as the
  # NAME of a group, finds none, and refuses to start the container at all
  # ("Unable to find group keep-groups" - podman 3.0.1 on Debian 11, a Pi 4).
  # There the device is still passed: the start-up probe finds whether it can
  # be opened, and encodes in software if not.
  if (( ${#DEVICE_FLAGS[@]} )); then
    if [[ "$(podman info --format '{{.Host.OCIRuntime.Name}}' 2>/dev/null || true)" == "crun" ]] \
       && _version_at_least "${PODMAN_VERSION:-0}" 3.2; then
      DEVICE_FLAGS+=(--group-add keep-groups)
    else
      echo "  note: podman ${PODMAN_VERSION:-?} cannot share this user's video group with the"
      echo "        container (keep-groups needs podman 3.2+ with crun); if the encoder"
      echo "        cannot be opened, video previews are encoded in software instead"
    fi
  fi
fi

echo "Starting backend-transcode on :5004 ($MODE)"
if (( ${#DEVICE_FLAGS[@]} )); then
  echo "  video devices: ${DEVICE_FLAGS[*]}"
fi
# The catalogs read-write: proxies and posters are written under each catalog's
# cache/. Same mount and user namespace as the backend, so files have the same
# owner whichever of the two wrote them.
podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name backend-transcode -p 5004:5004 \
  ${AIRLOCK_USERNS} \
  ${GPU_FLAGS[@]+"${GPU_FLAGS[@]}"} \
  ${DEVICE_FLAGS[@]+"${DEVICE_FLAGS[@]}"} \
  -e "TRANSCODE_MODE=${MODE}" \
  ${TRANSCODE_VAAPI_DEVICE:+-e "TRANSCODE_VAAPI_DEVICE=${TRANSCODE_VAAPI_DEVICE}"} \
  ${TRANSCODE_JOBS:+-e "TRANSCODE_JOBS=${TRANSCODE_JOBS}"} \
  ${TRANSCODE_ENCODER:+-e "TRANSCODE_ENCODER=${TRANSCODE_ENCODER}"} \
  -v "${CATALOGS_HOST_DIR}":/catalogs:z \
  backend-transcode
