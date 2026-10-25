#!/usr/bin/env bash
# Make card decorators from one image: each one a random 800x100 piece of the
# image with a random hue - or one hue for all of them - plus its black-and-
# white version.
#
#   tools/make_card_decorators.sh IMAGE DIRECTORY COUNT [HUE]
#
# For i = 1 .. COUNT, DIRECTORY gets:
#   cardDeco<i>.png     a random 800x100 piece, its hue shifted by HUE degrees
#                       (0-359) when given, else by a random 0-359 for each
#   cardDeco<i>bw.png   that decorator in black and white
# e.g. COUNT 3 -> cardDeco1.png cardDeco1bw.png ... cardDeco3.png cardDeco3bw.png
# Each line printed says where the piece came from and which hue it got.
# Uses tools/image_tool.py for every step.
#
# Python: the first of $PYTHON, pyenv's python3, python3 that can import
# Pillow. Set PYTHON=/path/to/python to choose one.

set -euo pipefail

usage() {
  echo "usage: $(basename "$0") IMAGE DIRECTORY COUNT [HUE]" >&2
  echo "  Makes COUNT card decorators from IMAGE in DIRECTORY: each a random" >&2
  echo "  800x100 piece with a random hue (cardDeco<i>.png), plus its black-and-" >&2
  echo "  white version (cardDeco<i>bw.png)." >&2
  echo "  HUE (0-359): shift every piece by these degrees instead of at random." >&2
  exit 2
}

[[ $# -eq 3 || $# -eq 4 ]] || usage

IMAGE="$1"
OUT="$2"
COUNT="$3"
HUE="${4:-}"
SIZE=800x100

if ! [[ "$COUNT" =~ ^[1-9][0-9]*$ ]]; then
  echo "error: COUNT must be a whole number of at least 1, got '$COUNT'" >&2
  usage
fi
if [[ -n "$HUE" ]] && ! [[ "$HUE" =~ ^[0-9]+$ && "$HUE" -le 359 ]]; then
  echo "error: HUE must be a whole number from 0 to 359, got '$HUE'" >&2
  usage
fi

TOOL="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/image_tool.py"

[[ -f "$IMAGE" ]] || { echo "error: $IMAGE is not a file" >&2; exit 1; }
[[ -f "$TOOL" ]]  || { echo "error: $TOOL not found" >&2; exit 1; }

# A python3 that has Pillow. The one first on PATH often does not (Homebrew's
# or the system one instead of pyenv's), and image_tool.py would stop on it.
PY=""
for candidate in "${PYTHON:-}" "$HOME/.pyenv/shims/python3" python3; do
  [[ -n "$candidate" ]] || continue
  if command -v "$candidate" >/dev/null 2>&1 \
      && "$candidate" -c 'import PIL' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [[ -z "$PY" ]]; then
  echo "error: no python3 with Pillow found." >&2
  echo "  Install it (python3 -m pip install Pillow) or set PYTHON=/path/to/python" >&2
  exit 1
fi

mkdir -p "$OUT"

# The random piece is cut to a scratch file; only the decorators stay.
piece="$(mktemp "${TMPDIR:-/tmp}/card-piece.XXXXXX")"
mv "$piece" "$piece.png"
piece="$piece.png"
trap 'rm -f "$piece"' EXIT

for i in $(seq 1 "$COUNT"); do
  deco="$OUT/cardDeco$i.png"

  # The random piece. image_tool.py prints where it came from.
  where="$("$PY" "$TOOL" crop "$IMAGE" --size "$SIZE" --random -o "$piece" \
            | sed -n 's/^random region: //p')"

  degrees="${HUE:-$((RANDOM % 360))}"
  "$PY" "$TOOL" hue "$piece" --degrees "$degrees" -o "$deco" >/dev/null

  # Always, and of the decorator itself.
  "$PY" "$TOOL" bw "$deco" -o "$OUT/cardDeco${i}bw.png" >/dev/null

  echo "cardDeco$i: $where, hue $degrees  (+ cardDeco${i}bw)"
done

echo "Done: $COUNT decorator(s) and their black-and-white versions in $OUT"
