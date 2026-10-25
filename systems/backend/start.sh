#!/usr/bin/env bash
# Start the backend locally (no container) from the project root.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

cd "$PROJECT_ROOT"

if [ ! -d "systems/backend/venv" ]; then
  echo "Creating virtual environment..."
  python3 -m venv systems/backend/venv
fi

# shellcheck disable=SC1091
source systems/backend/venv/bin/activate

pip install --quiet -r systems/backend/requirements.txt

echo "Starting backend on http://localhost:5000"
exec python systems/backend/app.py
