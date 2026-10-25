#!/usr/bin/env bash
# Put the pet models in the cache backend-pets bind-mounts.
#
#   tools/fetch_pet_weights.sh            # list what is present
#   tools/fetch_pet_weights.sh yolox      # the detector
#
# The image does not carry weights: start-podman.sh mounts
# systems/backend-pets/weights into the container, so a model file has to exist
# there or its endpoint answers 503. /health says which are present.
#
# LICENCES ARE THE POINT HERE, not a footnote. Airlock Photos is proprietary
# (license.txt), so an AGPL model cannot ship with it - which is why this
# fetches YOLOX (Apache 2.0) and not YOLO11, the better detector.
# See documentation/specs/commercial-licensing-audit.md.
#
# No associative arrays: macOS ships bash 3.2.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
W="$DIR/systems/backend-pets/weights"
mkdir -p "$W"

KNOWN="yolox yolox-s yolox-nano"

file_for() {
  case "$1" in
    yolox)      echo 'yolox_tiny.onnx' ;;
    yolox-s)    echo 'yolox_s.onnx' ;;
    yolox-nano) echo 'yolox_nano.onnx' ;;
    *)          echo '' ;;
  esac
}

url_for() {
  case "$1" in
    # YOLOX-Tiny, Apache 2.0, from Megvii's own release. ~20 MB, 416 px input.
    #
    # Chosen by measurement, not by size (spec section 12, 2026-10-09, on real
    # photographs from `family`): tiny finds animals in 6.0 % of images at
    # 91 ms, yolox_s finds 5.4 % at 711 ms, and yolox_nano finds only 2.7 % at
    # 41 ms. Nano is fast and misses half the animals, which is not a saving.
    yolox) echo 'https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_tiny.onnx' ;;
    # The other two, for re-measuring on different hardware.
    yolox-s)    echo 'https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_s.onnx' ;;
    yolox-nano) echo 'https://github.com/Megvii-BaseDetection/YOLOX/releases/download/0.1.1rc0/yolox_nano.onnx' ;;
    *)     echo '' ;;
  esac
}

if [[ $# -eq 0 ]]; then
  echo "pet weights: $W"
  found=0
  for f in "$W"/*; do
    [[ -e "$f" ]] || continue
    printf '  %8s  %s\n' "$(du -h "$f" | cut -f1)" "$(basename "$f")"
    found=1
  done
  [[ $found -eq 1 ]] || echo '  (empty)'
  echo
  echo "fetchable: $KNOWN"
  echo
  echo "NOT fetchable here - animal_clip_vit_b32.onnx (the identity model)."
  echo "  AvitoTech/CLIP-ViT-base-for-animal-identification is published as"
  echo "  PyTorch, so it has to be exported to ONNX once on a machine that has"
  echo "  torch installed, and the result copied in. Apache 2.0, 512-d output."
  echo "  Detection works without it; identification does not."
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
