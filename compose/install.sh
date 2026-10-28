#!/usr/bin/env bash
# Set up and start Airlock Photos with Docker Compose. Optional: docker-compose.yml
# works without it - this only asks the questions and writes .env.
#
#   ./install.sh            ask, write .env, create the folders, start
#   ./install.sh --no-start everything but `docker compose up -d`
#
# Run it in the folder that holds docker-compose.yml and .env.example. An .env
# that already exists is never overwritten.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE"
START=1
[[ "${1:-}" == "--no-start" ]] && START=0

say()  { printf '\n\033[1m%s\033[0m\n' "$1"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$1"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$1"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$1" >&2; exit 1; }

for f in docker-compose.yml .env.example; do
  [[ -f "$f" ]] || die "$f is missing here - download it beside this script (README.md)"
done

say "Checking for Docker"
if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
elif podman compose version >/dev/null 2>&1; then
  COMPOSE=(podman compose -f docker-compose.yml -f docker-compose.podman.yml)
  warn "using podman - best-effort; the script install is the one built for podman"
else
  die "Docker with the compose plugin is needed: https://docs.docker.com/engine/install/"
fi
ok "${COMPOSE[*]}"

lan_address() {
  if command -v ip >/dev/null 2>&1; then
    ip -4 route get 1.1.1.1 2>/dev/null | awk '{for (i = 1; i <= NF; i++) if ($i == "src") print $(i + 1)}' | head -1
  elif command -v ipconfig >/dev/null 2>&1; then
    local ifc; ifc="$(route -n get default 2>/dev/null | awk '/interface:/ {print $2; exit}')"
    [[ -n "$ifc" ]] && ipconfig getifaddr "$ifc" 2>/dev/null
  fi
}

if [[ -f .env ]]; then
  say "Settings"
  ok ".env already exists - keeping it (edit it by hand to change anything)"
else
  say "A few questions"
  read -r -p "  Folder your photos are under (Airlock reads from here): " photos
  photos="${photos/#\~/$HOME}"
  [[ -d "$photos" ]] || die "no such folder: $photos"
  guess="$(lan_address || true)"
  read -r -p "  This machine's address on your network [${guess:-none found}]: " addr
  addr="${addr:-$guess}"

  sed -e "s#^STORAGE_ROOT=.*#STORAGE_ROOT=$photos#" \
      -e "s#^PUID=.*#PUID=$(id -u)#" \
      -e "s#^PGID=.*#PGID=$(id -g)#" \
      -e "s#^AIRLOCK_NAMES=.*#AIRLOCK_NAMES=$addr#" \
      .env.example > .env
  ok "wrote .env (photos: $photos; owner: $(id -u):$(id -g); address: ${addr:-none})"
fi

# Bind-mounted folders are made here, as this user: one Docker creates on its
# own belongs to root.
set -a
# shellcheck disable=SC1091
. ./.env
set +a
for d in "${CATALOGS_DIR:-./data/catalogs}" "${DATABASE_DIR:-./data/database}" \
         "${INPUT_DIR:-./data/IN}" "${LICENSE_DIR:-./license}" "${CERTS_DIR:-./certs}"; do
  mkdir -p "$d"
done
ok "data folders ready"

if (( ! START )); then
  say "Ready. Start with: ${COMPOSE[*]} up -d"
  exit 0
fi

say "Starting (the first start downloads about 6 GB of images)"
"${COMPOSE[@]}" up -d

addr="$(echo "${AIRLOCK_NAMES:-}" | cut -d, -f1)"
say "Airlock is starting"
echo "  Open:        https://${addr:-<this machine's address>}${HTTPS_PORT:+:$HTTPS_PORT}/"
echo "  Certificate: install ${CERTS_DIR:-./certs}/airlock-ca.pem on each device that opens it"
echo "  Status:      ${COMPOSE[*]} ps"
echo "  The faces service needs a few minutes on its first start."
