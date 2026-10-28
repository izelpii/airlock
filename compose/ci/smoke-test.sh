#!/usr/bin/env bash
# Start the whole stack from built images and prove a new installation works:
# every service healthy, HTTPS answering, first-run setup offered.
#
#   compose/ci/smoke-test.sh [--images PREFIX] [--tag TAG] [--compose "docker compose"]
#
# Runs in a throwaway folder with its own .env, ports 18080/18443 and project
# name, so it never touches a real installation on the same machine.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_DIR="$(dirname "$HERE")"
IMAGES="localhost" ; TAG="dev" ; COMPOSE_CMD=""
while (( $# )); do
  case "$1" in
    --images)  IMAGES="$2"; shift ;;
    --tag)     TAG="$2"; shift ;;
    --compose) COMPOSE_CMD="$2"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ -z "$COMPOSE_CMD" ]]; then
  if docker compose version >/dev/null 2>&1; then COMPOSE_CMD="docker compose"
  else COMPOSE_CMD="docker-compose"; fi
fi

WORK="$(mktemp -d)"
mkdir -p "$WORK/photos" "$WORK/data/catalogs" "$WORK/data/database" "$WORK/data/IN" "$WORK/license" "$WORK/certs"
cp "$COMPOSE_DIR/docker-compose.yml" "$WORK/"
cat > "$WORK/.env" <<EOF
STORAGE_ROOT=$WORK/photos
PUID=$(id -u)
PGID=$(id -g)
AIRLOCK_NAMES=127.0.0.1
HTTP_PORT=18080
HTTPS_PORT=18443
AIRLOCK_IMAGES=$IMAGES
AIRLOCK_VERSION=$TAG
DEEPFACE_WORKERS=1
DEEPFACE_WARMUP_EMBEDDER=0
EOF
# shellcheck disable=SC2086  # COMPOSE_CMD is a command and its arguments
dc() { (cd "$WORK" && $COMPOSE_CMD -p airlock-smoke "$@"); }
cleanup() { dc down -v >/dev/null 2>&1 || true; rm -rf "$WORK"; }
trap cleanup EXIT

dc up -d
echo "waiting for the services to be healthy..."
for _ in $(seq 1 90); do
  states="$(dc ps --format '{{.Service}} {{.Health}}' 2>/dev/null || true)"
  if [[ -n "$states" ]] && ! grep -qvE ' healthy$|^certs ' <<< "$states"; then break; fi
  sleep 10
done
echo "$states"
if grep -qvE ' healthy$|^certs ' <<< "$states"; then
  echo "✗ not every service became healthy"
  dc logs --tail 30
  exit 1
fi

fail=0
check() {  # check <what> <expected substring> <curl args...>
  local what="$1" want="$2"; shift 2
  local got; got="$(curl -sk --max-time 10 "$@" || true)"
  if [[ "$got" == *"$want"* ]]; then echo "✓ $what"; else echo "✗ $what: got '${got:0:200}'"; fail=1; fi
}
check "HTTP redirects to HTTPS" "301" -o /dev/null -w '%{http_code}' http://127.0.0.1:18080/
check "the gateway answers over HTTPS" "gateway ok" https://127.0.0.1:18443/gateway-health
check "first-run setup is offered" '"setup_required":true' https://127.0.0.1:18443/api/auth/setup-required
check "the UI is served" "<div id=\"root\">" https://127.0.0.1:18443/
sans="$(openssl s_client -connect 127.0.0.1:18443 -servername airlock.lan </dev/null 2>/dev/null \
        | openssl x509 -noout -text 2>/dev/null | grep -A1 'Subject Alternative Name' | tail -1)"
if [[ "$sans" == *"IP Address:127.0.0.1"* && "$sans" == *"DNS:airlock.lan"* ]]; then
  echo "✓ the certificate covers airlock.lan and the address given"
else
  echo "✗ certificate names: ${sans:-none}"; fail=1
fi
if grep -q "BEGIN CERTIFICATE" "$WORK/certs/airlock-ca.pem" 2>/dev/null && [[ -O "$WORK/certs/airlock-ca.pem" ]]; then
  echo "✓ the CA is in certs/, owned by this user"
else
  echo "✗ certs/airlock-ca.pem is missing or not owned by $(id -un)"; fail=1
fi
check "faces answer through the gateway" '"status"' -H 'Host: deepface.airlock.lan' http://127.0.0.1:18080/api/health
exit "$fail"
