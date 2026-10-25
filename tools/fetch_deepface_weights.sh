#!/usr/bin/env bash
# Put a face model's weights in the cache backend-deepface bind-mounts.
#
# The image does not carry weights: start-podman.sh mounts
# systems/backend-deepface/deepface_weights into the container, so a model's
# file has to exist there - or DeepFace downloads it on first use, inside a
# gunicorn worker, which is a bad moment to discover a slow line.
#
#   tools/fetch_deepface_weights.sh            # list what is present
#   tools/fetch_deepface_weights.sh sface      # fetch SFace
#
# SFace is the Apache-2.0 model the face-recognition migration moves to; see
# documentation/specs/face-recognition-migration-spec.md.
#
# No associative arrays: macOS ships bash 3.2, which does not have them. The
# backend start script already learned that one.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
W="$DIR/systems/backend-deepface/deepface_weights/weights"
mkdir -p "$W"

KNOWN="sface yunet"

file_for() {
  case "$1" in
    sface) echo 'face_recognition_sface_2021dec.onnx' ;;
    yunet) echo 'face_detection_yunet_2023mar.onnx' ;;
    *)     echo '' ;;
  esac
}

url_for() {
  case "$1" in
    sface) echo 'https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx' ;;
    yunet) echo 'https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx' ;;
    *)     echo '' ;;
  esac
}

if [[ $# -eq 0 ]]; then
  echo "weights cache: $W"
  for f in "$W"/*; do
    [[ -e "$f" ]] && printf '  %8s  %s\n' "$(du -h "$f" | cut -f1)" "$(basename "$f")"
  done
  echo
  echo "fetchable: $KNOWN"
  echo "note: vgg_face_weights.h5 is not fetched here - VGG-Face is licensed for"
  echo "      non-commercial research only. See"
  echo "      documentation/specs/commercial-licensing-audit.md"
  exit 0
fi

for name in "$@"; do
  file="$(file_for "$name")"
  if [[ -z "$file" ]]; then
    echo "unknown model: $name (fetchable: $KNOWN)" >&2
    exit 2
  fi
  dest="$W/$file"
  if [[ -s "$dest" ]]; then
    echo "$name: already present ($dest)"
    continue
  fi
  echo "$name: fetching $(url_for "$name")"
  curl -fL --progress-bar -o "$dest.part" "$(url_for "$name")"
  mv "$dest.part" "$dest"
  echo "$name: $(du -h "$dest" | cut -f1) -> $dest"
done
