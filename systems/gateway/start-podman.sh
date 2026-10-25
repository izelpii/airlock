#!/usr/bin/env bash
# Build and run the airlock gateway with podman.
#
# Usage:
#   ./start-podman.sh          # build if stale, then run
#   ./start-podman.sh --build  # force a rebuild first
#
# The gateway is the only service that publishes a port. Everything else keeps
# its own hostPort so the gateway can reach it, but nothing in the application
# refers to those ports - see documentation/specs/redesign-ports-routes-spec.md.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Host paths and the rootless-podman user namespace come from one place,
# so moving to another machine is one file rather than one edit per script.
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"

IMAGE=airlock-gateway
GATEWAY_PORT="${GATEWAY_PORT:-80}"
TLS_PORT="${TLS_PORT:-443}"
# The host, as the gateway reaches the services. A name of our own, set only by
# --add-host below, so it has exactly one address. host.containers.internal
# does not: podman 3.4 (homeserver) also writes its own entry for it, 10.0.2.2,
# which is unreachable from this container - nginx round-robined across both,
# and every request sent to 10.0.2.2 failed with "Network unreachable" before
# being retried on the right one (246 in a day on homeserver, 2026-10-19).
UPSTREAM_HOST="${UPSTREAM_HOST:-airlock-host}"
# The browser-facing name. Set SERVER_DOMAIN=lozaprada.org when this server
# moves to a public domain - see user-authentication-spec.md section 10.7.
SERVER_DOMAIN="${SERVER_DOMAIN:-airlock.lan}"
CERT_DIR="$SCRIPT_DIR/certs"

BAKED_SOURCES=(Dockerfile airlock.conf.template proxy.conf)

# ── Is the image current? ─────────────────────────────────────────────────────
# By the CONTENT of the baked sources, recorded on the image as a label.
#
# This compared mtimes against the image's Created date, and that can never
# converge. Two reasons, and either alone is enough:
#
#   git sets mtime to the time of the CHECKOUT, not of the commit. After any
#   pull every file it touched looks newer than every image, however old its
#   actual change. This Dockerfile last changed on 2026-09-26 and carried an
#   mtime of 2026-10-05, the moment of a pull.
#
#   A build whose layers all hit the cache returns the SAME image, with its
#   original Created date. So the timestamp being compared against never
#   advances, and the next run decides to build again.
#
# Together: the gateway rebuilt on every single start for 24 days, its image
# stuck at 2026-09-13, while the script reported "Dockerfile is newer than the
# image" and was right every time. A content digest has neither problem - it
# changes when and only when the baked config changes.
sources_digest() {
  local f out
  for f in "${BAKED_SOURCES[@]}"; do
    [[ -f "$SCRIPT_DIR/$f" ]] && cat "$SCRIPT_DIR/$f"
  done | {
    # shasum on macOS, sha256sum on most Linux. Neither is everywhere.
    out="$(shasum -a 256 2>/dev/null)" || out="$(sha256sum 2>/dev/null)" || out=""
    printf '%s' "${out%% *}"
  }
}

SOURCES_LABEL=airlock.sources

needs_build() {
  [[ "${1:-}" == "--build" ]] && { echo "forced with --build"; return 0; }
  podman image exists "$IMAGE" 2>/dev/null || { echo "no image yet"; return 0; }

  local want have
  want="$(sources_digest)"
  # No digest means no basis for a decision. Rebuilding is the safe answer:
  # serving stale routing config is the failure this guard exists to prevent.
  [[ -n "$want" ]] || { echo "cannot hash the baked sources"; return 0; }

  have="$(podman image inspect "$IMAGE" \
            --format "{{index .Labels \"$SOURCES_LABEL\"}}" 2>/dev/null)" || have=""
  [[ "$have" == "<no value>" ]] && have=""

  if [[ -z "$have" ]]; then
    # Built before this label existed, so what is in it cannot be known. One
    # rebuild settles it and every start after this one is a no-op.
    echo "image predates the content check"
    return 0
  fi
  if [[ "$have" != "$want" ]]; then
    echo "the baked routing config changed"
    return 0
  fi
  return 1
}

if reason="$(needs_build "${1:-}")"; then
  echo "Building $IMAGE ($reason)..."
  airlock_prepare_build
  # The label is what the next start reads. Written from the same function that
  # checks it, so the two cannot disagree about what was baked in.
  podman build \
    --label "$SOURCES_LABEL=$(sources_digest)" \
    -f "$SCRIPT_DIR/Dockerfile" -t "$IMAGE" "$SCRIPT_DIR"
else
  echo "Image is up to date with the routing config."
fi

# The config now references a certificate, so nginx -t cannot pass without one.
# Check here rather than letting nginx fail: its message names a path inside the
# container, which is not where the fix is.
if [[ ! -f "$CERT_DIR/fullchain.pem" || ! -f "$CERT_DIR/privkey.pem" ]]; then
  echo
  echo "No TLS certificate in $CERT_DIR" >&2
  echo "Generate one first:  ./make-certs.sh" >&2
  exit 1
fi

# Catch a bad nginx config before it becomes a restart loop rather than an error.
# The certs must be mounted for this too: nginx -t loads them, so validating
# without the mount fails on a config that is actually fine.
echo "Validating the nginx configuration..."
if ! _check="$(podman run --rm \
      --add-host=airlock-host:${AIRLOCK_HOST_ALIAS} \
      -e "UPSTREAM_HOST=$UPSTREAM_HOST" \
      -e "SERVER_DOMAIN=$SERVER_DOMAIN" \
      -v "${CERT_DIR}:/etc/nginx/certs:ro,z" \
      --entrypoint sh "$IMAGE" -c \
      '/docker-entrypoint.sh nginx -t 2>&1' 2>&1)"; then
  echo
  echo "The gateway configuration is not valid:"
  echo "$_check" | tail -20
  exit 1
fi
echo "    $(echo "$_check" | grep -E 'syntax is ok|test is successful' | tail -1)"

# ── Can rootless podman bind these ports? ─────────────────────────────────────
# Ports below the VM's ip_unprivileged_port_start are refused, and the failure
# arrives as a wall of text from rootlessport at the moment of `podman run`.
# Check first so the remedy is the thing you read, not the symptom.
#
# BOTH published ports, not just GATEWAY_PORT. 443 is the one the browser
# actually uses - :80 only redirects to it - and it is privileged too, so
# checking 80 alone passed happily on the 8080 escape hatch and then failed on
# 443 anyway. The symptom is the worst kind: the stack comes up, every service
# is healthy, and the UI is reachable only on a service port that cannot serve
# /api, which presents as a sign-in form on a brand-new installation.
_LOWEST_PORT="$GATEWAY_PORT"
(( TLS_PORT < _LOWEST_PORT )) && _LOWEST_PORT="$TLS_PORT"
if (( _LOWEST_PORT < 1024 )); then
  _floor="$(_unprivileged_port_floor)"
  if [[ "$_floor" =~ ^[0-9]+$ ]] && (( _floor > _LOWEST_PORT )); then
    echo
    echo "Rootless podman cannot bind the gateway's ports: the unprivileged"
    echo "port floor is ${_floor}, and this needs ${GATEWAY_PORT} (redirect) and"
    echo "${TLS_PORT} (TLS, the one the browser uses)."
    echo
    if [[ "$(uname -s)" == "Darwin" ]]; then
      echo "Either allow them (one command, persistent, affects only the VM):"
      echo "    ./enable-port-80.sh"
    else
      echo "Either lower the floor on this host:"
      echo "    sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80"
      echo "    # persist it:"
      echo "    echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/99-airlock.conf"
    fi
    echo
    echo "Or run the gateway on unprivileged ports instead - BOTH of them,"
    echo "since 443 is privileged as well:"
    echo "    ./enable-port-80.sh --port-8080     # what to change"
    echo "    GATEWAY_PORT=8080 TLS_PORT=8443 ./start-podman.sh"
    exit 1
  fi
fi

echo "Starting $IMAGE on https://${SERVER_DOMAIN} (port ${TLS_PORT}); port ${GATEWAY_PORT} redirects"
echo "    names: airlock.lan, backend/deepface/vision/geoloc/albums/gallery/ollama.airlock.lan"
echo "    (run ./make-hosts.sh --check to confirm they resolve)"
podman run ${AIRLOCK_DETACH_FLAG} --rm --replace --name airlock-gateway \
  -p "${GATEWAY_PORT}:80" \
  -p "${TLS_PORT}:443" \
  -e "UPSTREAM_HOST=${UPSTREAM_HOST}" \
  -e "SERVER_DOMAIN=${SERVER_DOMAIN}" \
  -v "${CERT_DIR}:/etc/nginx/certs:ro,z" \
  --add-host=airlock-host:${AIRLOCK_HOST_ALIAS} \
  --add-host=host.containers.internal:${AIRLOCK_HOST_ALIAS} \
  "$IMAGE"
