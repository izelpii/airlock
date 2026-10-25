#!/usr/bin/env bash
# Run the album-gallery in Vite's dev server (hot reload), not the production
# server. See server.mjs for what actually gets deployed.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="$SCRIPT_DIR/app"

if [ ! -d "$APP_DIR/node_modules" ]; then
  echo "Installing dependencies..."
  npm install --prefix "$APP_DIR"
fi

# The catalogs root. Deployments live at
#   $CATALOGS_ROOT/<catalog-dir>/cache/Deployments/<slug>/
# and the catalog slug is mapped to its directory by $CATALOGS_ROOT/catalogs.json,
# which the backend writes. Override for any other location.
export CATALOGS_ROOT="${CATALOGS_ROOT:-$SCRIPT_DIR/../../catalogs}"

echo "Starting album-gallery on http://localhost:3001"
echo "  CATALOGS_ROOT=$CATALOGS_ROOT"
if [ -f "$CATALOGS_ROOT/catalogs.json" ]; then
  echo "  deployments:"
  # Listed so the developer can paste a URL instead of guessing one.
  for dir in "$CATALOGS_ROOT"/*/cache/Deployments/*/; do
    [ -d "$dir" ] || continue
    slug="$(basename "$dir")"
    catalog="$(basename "$(dirname "$(dirname "$(dirname "$dir")")")")"
    echo "    http://localhost:3001/d/<slug of $catalog>/$slug/"
  done
else
  echo "  (no catalogs.json yet — start the backend once and it writes one)"
fi

# NOTE: the dev server does NOT enforce deployment passwords. server.mjs does.
exec npm run dev --prefix "$APP_DIR"
