#!/usr/bin/env bash
# Shared deployment settings, sourced by every start-podman.sh and by deploy.sh.
#
#   source "$(dirname "$0")/../../deploy/airlock-env.sh"
#
# Everything here is derived or overridable. Nothing in this file names a
# particular machine — that is the whole point: the start scripts used to carry
# absolute paths from the machine they were written on, which is one edit per
# script per host, and one missed edit away from a container mounting nothing.
#
# Order of precedence, lowest first:
#   1. the defaults below, derived from the repository location
#   2. deploy/airlock.env, if it exists (per-machine, not in git)
#   3. environment variables already set when the script is called

# Repository root: two levels up from this file, whatever it is called and
# wherever it was copied.
AIRLOCK_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export AIRLOCK_ROOT

# ── Per-machine overrides ─────────────────────────────────────────────────────
# Sourced before the defaults are applied, so it can set any of them. Not in
# git: it describes one machine.
if [[ -f "$AIRLOCK_ROOT/deploy/airlock.env" ]]; then
  # shellcheck disable=SC1090
  source "$AIRLOCK_ROOT/deploy/airlock.env"
fi

# ── Host paths ────────────────────────────────────────────────────────────────
# Catalogs: every catalog directory, the derivatives, albums, deployments and
# received transfers. The one that matters most — losing it loses the library.
export CATALOGS_HOST_DIR="${CATALOGS_HOST_DIR:-$AIRLOCK_ROOT/catalogs}"

# The ingestion inbox, mounted read-write at /input.
export INPUT_HOST_DIR="${INPUT_HOST_DIR:-$AIRLOCK_ROOT/IN}"

# The single root that storage routes are subpaths of. A bind mount is fixed
# when the container is created, so a folder outside this root cannot be read
# whatever the UI says. Mounted read-write, so a move-mode import can take
# files out of a route (ingest-move-spec.md section 8.1). Nothing writes there
# unless an owner marks that route "imports may move files"; copy and reference
# imports only read. STORAGE_ROOT_READONLY=1 mounts it read-only again, which
# makes every route read-only whatever its flag says.
export STORAGE_ROOT_HOST="${STORAGE_ROOT_HOST:-$HOME}"
export STORAGE_ROOT_CONTAINER="${STORAGE_ROOT_CONTAINER:-/storage}"
export STORAGE_ROOT_READONLY="${STORAGE_ROOT_READONLY:-0}"

# The application database, bind-mounted at /app/database.
export DATABASE_HOST_DIR="${DATABASE_HOST_DIR:-$AIRLOCK_ROOT/systems/backend/database}"

# ── Service addresses ─────────────────────────────────────────────────────────
export DEEPFACE_URL="${DEEPFACE_URL:-http://deepface.airlock.lan}"
export ALBUMS_URL="${ALBUMS_URL:-http://albums.airlock.lan}"
export BACKEND_URL="${BACKEND_URL:-http://backend.airlock.lan}"

# ── Rootless-podman user namespace ────────────────────────────────────────────
#
# THE THING THAT BREAKS ON LINUX AND NOT ON macOS.
#
# The images run as a non-root user, uid 1000. Under rootless podman on Linux,
# the invoking host user maps to uid 0 INSIDE the container, and container uid
# 1000 maps to a subuid the host user does not own. So a bind-mounted directory
# created by you arrives root-owned and unwritable by the process that needs it,
# and the failure surfaces as "unable to open database file" or
# "Permission denied: /home/appuser/.deepface/weights".
#
# --userns=keep-id:uid=1000,gid=1000 maps the invoking user to container uid
# 1000 — which IS that non-root user — so host-owned mounts are owned by the
# process that must write them.
#
# Not applied on macOS: podman there runs a Linux VM whose virtiofs layer
# already presents host paths writably, and keep-id inside the VM would map the
# wrong identity.
#
# ONLY for containers that run as uid 1000: backend, backend-deepface,
# backend-vision, backend-geoloc. The gateway, frontend and album-gallery images
# run as ROOT, and for them container-root already maps to the invoking host
# user - which is exactly what lets them read host-owned mounts. Adding keep-id
# there would map root to something else and TAKE AWAY that access. Check the
# image's USER before adding this to a new service.
#
# Set AIRLOCK_USERNS explicitly to override, or to "" to disable.
# ── What this podman can actually do ──────────────────────────────────────────
#
# Ubuntu 22.04 ships podman 3.4.4, from 2021. Two things the newer scripts
# assumed do not exist there, and both fail the run outright:
#
#   --userns=keep-id:uid=,gid=   podman >= 4.3   "unrecognized namespace mode"
#   --add-host name:host-gateway podman >= 4.7   "invalid IP address in add-host"
#
# Both have workable fallbacks, so an old podman degrades instead of stopping.
_podman_version() {
  podman version --format '{{.Client.Version}}' 2>/dev/null \
    || podman --version 2>/dev/null | awk '{print $3}'
}
# "4.9.3" >= "4.3" without sort -V, which BusyBox and older coreutils lack.
_version_at_least() {  # _version_at_least <have> <want>
  local have="${1%%-*}" want="$2" h w
  IFS=. read -r h w _ <<< "$have"; local hmaj="${h:-0}" hmin="${w:-0}"
  IFS=. read -r h w _ <<< "$want"; local wmaj="${h:-0}" wmin="${w:-0}"
  (( hmaj > wmaj )) && return 0
  (( hmaj < wmaj )) && return 1
  (( hmin >= wmin ))
}

PODMAN_VERSION="$(_podman_version)"
export PODMAN_VERSION

# ── The unprivileged port floor ───────────────────────────────────────────────
#
# Rootless podman cannot publish a port below this number, and the gateway wants
# 80 and 443. On macOS the restriction lives inside the podman machine's Linux
# VM - not on the Mac - so it has to be read over ssh; on Linux podman runs
# natively and the sysctl is on this host. Prints nothing when it cannot tell,
# which callers must treat as "unknown", never as "fine".
#
# Every caller runs under `set -euo pipefail`, so every command here is guarded:
# `podman machine ssh` fails on Linux, where there is no machine, and pipefail
# propagated that into an assignment and killed the calling script outright.
_unprivileged_port_floor() {
  local v=""
  if [[ "$(uname -s)" == "Darwin" ]]; then
    v="$(podman machine ssh 'cat /proc/sys/net/ipv4/ip_unprivileged_port_start' 2>/dev/null | tr -d '\r' || true)"
  elif [[ -r /proc/sys/net/ipv4/ip_unprivileged_port_start ]]; then
    v="$(cat /proc/sys/net/ipv4/ip_unprivileged_port_start 2>/dev/null || true)"
  fi
  [[ "$v" =~ ^[0-9]+$ ]] && printf '%s' "$v"
  return 0
}

if [[ -z "${AIRLOCK_USERNS+x}" ]]; then
  AIRLOCK_USERNS=""
  if [[ "$(uname -s)" == "Linux" ]] \
     && podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null | grep -qi true; then
    if _version_at_least "$PODMAN_VERSION" 4.3; then
      AIRLOCK_USERNS="--userns=keep-id:uid=1000,gid=1000"
    elif [[ "$(id -u)" == "1000" ]]; then
      # Plain keep-id maps your uid to the SAME uid inside, and the images run
      # as uid 1000 - so when you are 1000 it is exactly equivalent. It has
      # existed since podman 1.x.
      AIRLOCK_USERNS="--userns=keep-id"
    else
      # keep-id would map you to $(id -u) inside, which is not the uid the
      # images run as. Nothing here can fix that on an old podman; say so
      # rather than emitting a flag that silently does the wrong thing.
      AIRLOCK_USERNS=""
      echo "airlock: podman $PODMAN_VERSION cannot map uid $(id -u) to the" >&2
      echo "         container's uid 1000. Upgrade podman to 4.3+, or run as a" >&2
      echo "         user whose uid is 1000." >&2
    fi
  fi
fi
export AIRLOCK_USERNS

# The value used for `--add-host <name>:<value>`. `host-gateway` is the alias
# podman resolves to the host; before 4.7 it is rejected as an invalid IP, and
# rootless podman reaches the host at the slirp4netns gateway instead.
#
# ASK THE CONTAINER, DO NOT ASSUME. The address of the host as seen from inside
# depends on the network backend: slirp4netns puts it at 10.0.2.2, a CNI bridge
# at 10.88.0.1, and neither is safe to hard-code - guessing 10.0.2.2 produced
# "can't connect to remote host (10.0.2.2): Network unreachable" on a machine
# using the other one.
#
# The container's DEFAULT ROUTE is the host, whichever backend is in use. One
# throwaway container answers it definitively. Cached in deploy/airlock.env so
# this costs one container run per machine, not one per service start.
_container_default_route() {
  local img
  for img in airlock-gateway workflow-backend backend-geoloc alpine; do
    podman image exists "$img" 2>/dev/null || continue
    podman run --rm --entrypoint sh "$img" -c \
      "ip route 2>/dev/null | awk '/^default/ {print \$3; exit}'" 2>/dev/null \
      | tr -d '\r' | head -1
    return
  done
}

# The host's own address on the LAN. Reachable from a container through the
# ordinary outbound path, which is why it works where the slirp gateway does not.
_host_lan_ip() {
  ip -4 route get 1.1.1.1 2>/dev/null | awk '{for(i=1;i<=NF;i++) if($i=="src") print $(i+1)}' | head -1
  # macOS has no iproute2. Only the origins list uses this here (the host alias
  # is host-gateway on macOS), but a wrong answer there is a silent failure.
  if [[ "$(uname -s)" == "Darwin" ]]; then
    local _if
    _if="$(route -n get default 2>/dev/null | awk '/interface:/ {print $2; exit}')"
    [[ -n "$_if" ]] && ipconfig getifaddr "$_if" 2>/dev/null
  fi
}

# WHICH ADDRESS, AND WHY THIS ORDER.
#
# The LAN address first. A container reaches it through the ordinary outbound
# path, which works under every rootless network backend. The gateway of the
# container's own network - 10.0.2.2 for slirp4netns, 10.88.0.1 for a CNI
# bridge - looks like the obvious answer and frequently is NOT reachable:
# slirp4netns refuses the host loopback unless started with
# allow_host_loopback=true, so the route exists and the connection fails with
# "Network unreachable".
#
# Verifying by ping was tried and removed: rootless containers usually cannot
# send ICMP at all, so the test rejected addresses that were perfectly good.
#
# AIRLOCK_HOST_IP in deploy/airlock.env overrides all of this and is the right
# answer whenever a machine has several interfaces and picks the wrong one.
if [[ -z "${AIRLOCK_HOST_ALIAS+x}" ]]; then
  if [[ "$(uname -s)" != "Linux" ]] || _version_at_least "$PODMAN_VERSION" 4.7; then
    AIRLOCK_HOST_ALIAS="host-gateway"
  elif [[ -n "${AIRLOCK_HOST_IP:-}" ]]; then
    AIRLOCK_HOST_ALIAS="$AIRLOCK_HOST_IP"
  else
    AIRLOCK_HOST_ALIAS=""
    for _cand in "$(_host_lan_ip || true)" "$(_container_default_route || true)"; do
      if [[ "$_cand" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
        AIRLOCK_HOST_ALIAS="$_cand"
        break
      fi
    done
    if [[ -n "$AIRLOCK_HOST_ALIAS" ]]; then
      if [[ -w "$AIRLOCK_ROOT/deploy" ]] \
         && ! grep -q '^AIRLOCK_HOST_IP=' "$AIRLOCK_ROOT/deploy/airlock.env" 2>/dev/null; then
        echo "AIRLOCK_HOST_IP=$AIRLOCK_HOST_ALIAS" >> "$AIRLOCK_ROOT/deploy/airlock.env" 2>/dev/null || true
      fi
    else
      AIRLOCK_HOST_ALIAS="10.0.2.2"
      echo "airlock: could not determine this machine's address; using 10.0.2.2." >&2
      echo "         Set AIRLOCK_HOST_IP in deploy/airlock.env to the LAN address." >&2
    fi
  fi
fi
export AIRLOCK_HOST_ALIAS

# ── Browser origins the backend will accept ───────────────────────────────────
#
# Flask-CORS and Socket.IO both check the browser's Origin header against an
# explicit list; a wildcard is not allowed once requests carry credentials. The
# list in app.py defaults to airlock.lan and localhost, which is exactly the set
# a developer machine uses - and it silently excludes the case where the UI is
# reached by the host's LAN address instead of by name.
#
# That failure is quiet and misleading. The page loads (the gateway proxies it
# same-origin) and every REST call works, so the system looks healthy; only the
# Socket.IO handshake is rejected, and the single log line saying so
#
#   https://192.168.1.16 is not an accepted origin.
#
# is logged once and thereafter at INFO. The symptom the user sees is a UI that
# never updates by itself, which reads as a websocket or transport problem and
# sends you looking in the wrong place entirely.
#
# So: whatever address this machine is reachable at is an origin it should
# accept, under both schemes - the gateway serves TLS, but a direct :3000 visit
# does not. Set AIRLOCK_ALLOWED_ORIGINS in deploy/airlock.env to override.
#
# NOT LOAD-BEARING ANY MORE, AND DELIBERATELY KEPT.
#
# Everything above describes a real failure, but deriving addresses here was the
# wrong fix for it. app.py now decides same-origin PER REQUEST, from the Host
# the browser actually addressed - see _origin_allowed() - which is what
# engine.io does by default and what passing an explicit list had switched off.
# A deployment reached at a new address needs no change here and no change to
# deploy/airlock.env; it just works.
#
# What survives is the ability to name a GENUINELY cross-origin caller: a
# separate front end, a different hostname serving the UI, the CRA dev server on
# another machine. Those are not same-origin and must still be listed. If you
# are debugging a rejected Origin, the answer is almost certainly in
# _origin_allowed(), not in this block.
if [[ -z "${AIRLOCK_ALLOWED_ORIGINS+x}" ]]; then
  _origins="https://airlock.lan,http://airlock.lan,http://localhost:3000,http://localhost:5000"
  for _addr in "${AIRLOCK_HOST_IP:-}" "$(_host_lan_ip || true)" "$(hostname -s 2>/dev/null || true)"; do
    [[ -n "$_addr" ]] || continue
    case ",$_origins," in *",https://$_addr,"*) continue ;; esac
    _origins="$_origins,https://$_addr,http://$_addr,http://$_addr:3000"
  done
  AIRLOCK_ALLOWED_ORIGINS="$_origins"
  unset _origins _addr
fi
export AIRLOCK_ALLOWED_ORIGINS

# ── SELinux relabelling ───────────────────────────────────────────────────────
# ":z" is required on SELinux hosts (Fedora, RHEL) and is a harmless no-op on
# Ubuntu and macOS, so it stays unconditional in the run scripts.

# ── Detached or foreground ────────────────────────────────────────────────────
#
# These scripts run `podman run` in the FOREGROUND by default, which is right
# when a person runs one directly: Ctrl-C stops the container, and the logs are
# on screen.
#
# It is wrong for start-all.sh, which used to background each script with `&`.
# A backgrounded foreground container is owned by that shell, so it dies with
# it - closing the SSH session takes the whole stack down, and `--rm` then
# removes the containers. It can also be stopped by SIGTTIN the moment anything
# touches the terminal. Both were survivable on a desktop and are not on a
# server you log out of.
#
# AIRLOCK_DETACH=1 makes them pass -d instead, so the containers outlive the
# shell that started them. start-all.sh sets it.
if [[ "${AIRLOCK_DETACH:-}" == "1" ]]; then
  AIRLOCK_DETACH_FLAG="-d"
else
  AIRLOCK_DETACH_FLAG=""
fi
export AIRLOCK_DETACH_FLAG

# ── Old images ────────────────────────────────────────────────────────────────
#
# Every rebuild moves an image's tag to the new build and leaves the old one
# behind, untagged. Nothing removed them: on oldserver, two days of rebuilds left
# 228 images (17 in use) and filled the root disk to 97%, and the backend's
# SQLite - whose temporary files live on that disk, in the container's /tmp -
# began failing with "database or disk is full".
#
# Called just before a build. `image prune` removes only untagged images that no
# container uses, so the image the running container was started from survives
# until the next build after it has been replaced - one leftover per service at
# most. Untagged build-stage images go too, so the first build after a prune may
# redo steps the cache would have skipped.
# ── Where a BUILD puts its scratch ───────────────────────────────────────────
# Not where the image ends up. podman and buildah unpack intermediate layers
# into TMPDIR, and with TMPDIR unset that is /var/tmp - on the ROOT filesystem,
# which is exactly the disk a container store gets moved off when it fills up.
#
# Building backend-deepface left 2.4 GB in /var/tmp/buildah<random> on a root
# disk with 9 GB free (oldserver, 2026-10-06), after the store itself had been
# moved to the big volume. The image landed in the right place and the scratch
# did not, which is the same mistake VACUUM makes with its temporary copy
# (tools/reclaim_catalog_db.py) and has the same fix: put the scratch on the
# filesystem that was sized for this.
#
# Buildah also LEAVES that directory behind when the build finishes, so old
# ones are swept here - only inside the directory this function created, and
# only ones untouched for two hours, which cannot be the build about to start.
# podman unshare to remove them: their contents are owned by subuids.
#
# AIRLOCK_BUILD_TMPDIR overrides the location; AIRLOCK_NO_BUILD_TMPDIR=1 turns
# the whole thing off and leaves TMPDIR alone.
airlock_build_scratch() {
  [[ -n "${AIRLOCK_NO_BUILD_TMPDIR:-}" ]] && return 0
  local root graph d
  root="${AIRLOCK_BUILD_TMPDIR:-}"
  if [[ -z "$root" ]]; then
    graph="$(podman info --format '{{.Store.GraphRoot}}' 2>/dev/null)"
    # Must exist ON THIS MACHINE. On macOS the store lives inside the podman
    # VM, so podman reports a path like /var/home/core/... that the Mac has no
    # business creating - and the build happens in the VM, where none of this
    # applies. An unreadable or absent graphroot means leave TMPDIR alone.
    [[ -n "$graph" && -d "$graph" ]] || return 0
    root="$(dirname "$graph")/build-tmp"
  fi
  mkdir -p "$root" 2>/dev/null || return 0
  export TMPDIR="$root"
  for d in "$root"/buildah*; do
    [[ -d "$d" ]] || continue
    [[ -n "$(find "$d" -maxdepth 0 -mmin +120 2>/dev/null)" ]] || continue
    podman unshare rm -rf "$d" 2>/dev/null || true
  done
  return 0
}

# Everything a build wants doing first. Called by every start-podman.sh in
# place of the prune alone, so the scratch directory is set for the build that
# follows rather than per service.
airlock_prepare_build() {
  airlock_build_scratch
  airlock_prune_old_images
}

# The album gallery's version: the last commit that touched systems/album-gallery,
# with "-dirty" when that folder has uncommitted changes. Not the repository's
# HEAD - most commits do not change the gallery, and a server should only look
# out of date when its gallery actually is. "unknown" without git.
airlock_gallery_revision() {
  local root rev
  root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
  rev="$(git -C "$root" log -1 --format=%h -- systems/album-gallery 2>/dev/null)"
  [[ -n "$rev" ]] || { echo unknown; return; }
  if [[ -n "$(git -C "$root" status --porcelain -- systems/album-gallery 2>/dev/null)" ]]; then
    rev="${rev}-dirty"
  fi
  echo "$rev"
}

airlock_prune_old_images() {
  local removed
  removed="$(podman image prune -f 2>/dev/null | grep -c . || true)"
  if [[ "${removed:-0}" -gt 0 ]]; then
    echo "Removed ${removed} unused old image(s)."
  fi
}

# The uid/gid the containers run as. Used by deploy.sh when it prepares host
# directories, and by nothing else.
export AIRLOCK_UID="${AIRLOCK_UID:-1000}"
export AIRLOCK_GID="${AIRLOCK_GID:-1000}"

# ── What this machine is ──────────────────────────────────────────────────────
# Four targets, and the differences between them decide real things - which
# accelerator exists, which package manager installs a missing tool, and whether
# a native service is worth running at all:
#
#   Darwin arm64   Apple Silicon      Metal, VideoToolbox, ANE
#   Darwin x86_64  Intel Mac          VideoToolbox (Quick Sync); NO Metal for TF
#   Linux x86_64   Ubuntu and kin     NVIDIA through CDI when present
#   Linux aarch64  Raspberry Pi       V4L2 M2M video; no CUDA, no Metal
#
# uname -m is normalised because the same machine answers differently depending
# on which shell started it: arm64/aarch64 for the same silicon, x86_64/amd64.
airlock_arch() {
  case "$(uname -m)" in
    arm64|aarch64) echo "arm64" ;;
    x86_64|amd64)  echo "x86_64" ;;
    armv7l|armv6l) echo "arm32" ;;
    *)             uname -m ;;
  esac
}
# Overridable, and that is deliberate: every platform decision in this file
# reads these two, so setting them is how the whole matrix gets tested on one
# machine - and how a cross-build or an emulated environment can be told what
# it really is.
#   AIRLOCK_OS=Linux AIRLOCK_ARCH=arm64 airlock_service_mode DEEPFACE
AIRLOCK_OS="${AIRLOCK_OS:-$(uname -s)}"
AIRLOCK_ARCH="${AIRLOCK_ARCH:-$(airlock_arch)}"
export AIRLOCK_OS AIRLOCK_ARCH

# A one-line name for this platform, for logs and status output.
airlock_platform() {
  case "${AIRLOCK_OS}/${AIRLOCK_ARCH}" in
    Darwin/arm64)  echo "macOS on Apple Silicon" ;;
    Darwin/x86_64) echo "macOS on Intel" ;;
    Linux/arm64)   echo "Linux on arm64" ;;
    Linux/x86_64)  echo "Linux on x86_64" ;;
    *)             echo "${AIRLOCK_OS} on ${AIRLOCK_ARCH}" ;;
  esac
}

# ── Accelerated services: where each one runs ─────────────────────────────────
#
# documentation/specs/accelerated-services-spec.md. GPU work lives in services
# with a network address; each machine decides, per service, whether that
# service runs natively or in a container. Set in deploy/airlock.env:
#
#   AIRLOCK_DEEPFACE_MODE=auto     auto | host | container | gpu | off
#   AIRLOCK_TRANSCODE_MODE=auto
#
#   host       native process (start-host.sh) - the only way to Metal and
#              VideoToolbox on a Mac, where containers run in a VM
#   container  container, CPU only
#   gpu        container with the NVIDIA GPU passed in (CDI)
#   off        not started; callers fall back or report it unavailable
#   auto       the default. Per platform and per service:
#
#                                  DEEPFACE            TRANSCODE
#                 Darwin arm64     host (Metal)        host (VideoToolbox)
#                 Darwin x86_64    container           host (VideoToolbox)
#                 Linux x86_64     gpu or container    gpu or container
#                 Linux arm64      container           container
#
#              An Intel Mac gets the container for deepface for two reasons,
#              either of which would be enough: tensorflow-metal is
#              Apple-Silicon-only, AND TensorFlow has published no macOS x86_64
#              wheel since 2.16 while this service needs >= 2.20 - so natively
#              it cannot even be installed. See airlock_host_supported.
#              A Pi's V4L2 encoder is a device a container can be given, so
#              host mode stays opt-in there.
#
# host, and auto's choice of it, are conditional on the HOST ACTUALLY HAVING
# what a native process needs - a Python >= 3.10 and, for transcode, ffmpeg.
# When it does not, the mode falls back to container with the reason, exactly
# as gpu falls back when there is no usable GPU. See airlock_host_ready.
#
# The backend never runs anything on the host itself: it calls these services
# by URL, and where they run is invisible to it.

# "ok <name>" when containers can use an NVIDIA GPU here, else "no: <why>".
# Usable means a working driver AND a CDI spec AND a podman that reads CDI -
# anything less is reported, never half-used.
airlock_nvidia_status() {
  # AIRLOCK_OS, not uname: every platform decision in this file reads the same
  # two normalised variables, which also makes the whole matrix testable by
  # setting them (AIRLOCK_OS=Linux AIRLOCK_ARCH=arm64 airlock_service_mode ...).
  if [[ "$AIRLOCK_OS" != "Linux" ]]; then echo "no: not Linux"; return; fi
  if ! command -v nvidia-smi >/dev/null 2>&1; then echo "no: nvidia-smi not found"; return; fi
  local name
  name="$(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | head -1)"
  [[ -n "$name" ]] || { echo "no: nvidia-smi found no working GPU"; return; }
  if ! _version_at_least "$PODMAN_VERSION" 4.1; then
    echo "no: podman $PODMAN_VERSION cannot pass a GPU in (CDI needs 4.1+)"; return
  fi
  if ! grep -qs 'nvidia.com/gpu' /etc/cdi/*.yaml /etc/cdi/*.json /var/run/cdi/*.yaml \
       /var/run/cdi/*.json 2>/dev/null; then
    echo "no: $name found, but no CDI spec - run: sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml"
    return
  fi
  echo "ok $name"
}

# ── The Python a native service runs on ───────────────────────────────────────
# NEVER bare `python3`. On macOS that is Apple's CommandLineTools build - 3.9.6
# on Ventura - and `python3 -m venv` with it produces a venv that cannot install
# this stack: current Flask and flask-cors publish no 3.9 distributions, so pip
# backtracks through every deepface release and ends in ResolutionImpossible.
# Homebrew deliberately does not create an unversioned `python3` link, so a
# machine can have 3.11 installed and still hand `python3` to 3.9.
#
# The containers are built FROM python:3.12-slim, so 3.12 is the version this
# requirement set is actually tested against. 3.10 is the floor.
AIRLOCK_PYTHON_MIN="${AIRLOCK_PYTHON_MIN:-3.10}"

_python_version() { "$1" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true; }

# Prints the path of an interpreter at least AIRLOCK_PYTHON_MIN, or nothing.
# Newest first: a 3.9 that happens to be called python3 must not win over a
# 3.12 that is called python3.12.
# Prints nothing and RETURNS 0 when there is none: every caller assigns this in
# a command substitution, and under `set -e` a non-zero return there would kill
# the script before it could report the problem with its own fix attached.
airlock_python() {
  local cand ver
  # An explicit choice is honoured even if it is older - the operator may know
  # something this check does not - but it still has to exist.
  if [[ -n "${AIRLOCK_PYTHON:-}" ]]; then
    command -v "$AIRLOCK_PYTHON" 2>/dev/null || true
    return 0
  fi
  for cand in python3.14 python3.13 python3.12 python3.11 python3.10 python3 python; do
    command -v "$cand" >/dev/null 2>&1 || continue
    ver="$(_python_version "$cand" || true)"
    [[ -n "$ver" ]] || continue
    if _version_at_least "$ver" "$AIRLOCK_PYTHON_MIN"; then
      command -v "$cand"; return 0
    fi
  done
  return 0
}

# "ok <path> (<version>)" or "no: <why>", with the fix for THIS platform - the
# command differs per target and a wrong one is worse than none.
airlock_python_status() {
  local py ver
  py="$(airlock_python)"
  if [[ -n "$py" ]]; then
    echo "ok $py ($(_python_version "$py" || true))"; return 0
  fi
  ver="$(_python_version python3 || true)"
  echo "no: no python >= $AIRLOCK_PYTHON_MIN found${ver:+ (python3 is $ver)}; install it with: $(airlock_install_hint python)"
}

# ffmpeg and ffprobe, which host-mode transcode shells out to. The container
# image installs them itself, so this is a host-mode question only.
airlock_ffmpeg_status() {
  if command -v ffmpeg >/dev/null 2>&1 && command -v ffprobe >/dev/null 2>&1; then
    echo "ok $(ffmpeg -version 2>/dev/null | head -1 | awk '{print $3}' || true)"
  else
    echo "no: ffmpeg/ffprobe not on PATH; install them with: $(airlock_install_hint ffmpeg)"
  fi
}

# The command that installs a missing host tool here. One place, because every
# script that reports a missing tool wants to name the fix, and naming the wrong
# package manager sends people down a dead end.
airlock_install_hint() {  # airlock_install_hint <python|ffmpeg>
  local what="$1"
  if [[ "$AIRLOCK_OS" == "Darwin" ]]; then
    case "$what" in
      python) echo "brew install python@3.12" ;;
      ffmpeg) echo "brew install ffmpeg" ;;
    esac
  else
    case "$what" in
      python) echo "sudo apt install python3.12 python3.12-venv" ;;
      ffmpeg) echo "sudo apt install ffmpeg" ;;
    esac
  fi
}

# ── How much memory a container can actually have ─────────────────────────────
# ON macOS THIS IS NOT THE MAC'S MEMORY. Containers run inside the podman
# machine, a Linux VM created with a fixed allocation - 2048 MB by default,
# whatever the Mac has - and every container shares it. The host's 32 GB is
# invisible in there.
#
# This matters most for backend-deepface, which is the one service whose
# container is memory-hungry rather than idle: each gunicorn worker builds its
# own copy of VGG-Face, about 600 MB resident, because the models cannot be
# shared across a fork (see start-host.sh on --preload). Two workers plus
# TensorFlow in a 2 GB VM that is also running eight other containers produces
#
#   [ERROR] Worker (pid:NNN) was sent SIGKILL! Perhaps out of memory?
#
# forever, with no mention of memory being the cause anywhere a user would look.
#
# Prints a number in MB, or nothing when it cannot be determined.
airlock_container_memory_mb() {
  if [[ "$AIRLOCK_OS" == "Darwin" ]]; then
    # The running machine's allocation. Reported in bytes by some podman
    # versions and in MB by others, so a value that looks like bytes is scaled.
    local mem
    mem="$(podman machine inspect --format '{{.Resources.Memory}}' 2>/dev/null | head -1)"
    [[ "$mem" =~ ^[0-9]+$ ]] || return 0
    (( mem > 1048576 )) && mem=$(( mem / 1048576 ))
    echo "$mem"
  elif [[ -r /proc/meminfo ]]; then
    awk '/^MemTotal:/ {print int($2/1024)}' /proc/meminfo
  fi
  return 0
}

# How many deepface workers fit. Each one is a full copy of the model, so this
# is a memory division, not a core division - 4 cores do not help if the third
# worker is killed while loading.
#
# ~1.1 GB per worker. One worker always, because a service with no workers is
# not a service: if even that does not fit, the OOM is at least reported against
# a configuration that was honest about being too small.
AIRLOCK_DEEPFACE_MB_PER_WORKER="${AIRLOCK_DEEPFACE_MB_PER_WORKER:-1100}"

# What to leave for everything else, and why the two platforms differ.
#
# On LINUX the containers share the host's real memory with everything else on
# the machine, and an image build gets that memory too. 700 MB of slack is
# enough.
#
# On macOS they share ONE FIXED VM - and so does every `podman build`. With
# 6144 MB and a 700 MB reserve this picked 4 workers, which grew to 4.99 GB of
# a 6.2 GB VM; the next frontend build then asked for a 3 GB V8 heap, got about
# 0.9 GB, and was killed with "the process exited too early" (2026-10-06). The
# worker count was right by its own arithmetic and wrong about the machine.
#
# So on macOS the reserve covers a webpack build as well: NODE_BUILD_HEAP_MB
# (3072) plus room for node itself. A dev Mac builds often and detects faces
# rarely, which is the trade this makes - and DEEPFACE_WORKERS overrides it when
# a particular run is worth the whole VM.
if [[ "$AIRLOCK_OS" == "Darwin" ]]; then
  AIRLOCK_DEEPFACE_RESERVE_MB="${AIRLOCK_DEEPFACE_RESERVE_MB:-3600}"
else
  AIRLOCK_DEEPFACE_RESERVE_MB="${AIRLOCK_DEEPFACE_RESERVE_MB:-700}"
fi

airlock_deepface_workers() {
  local mem avail n
  if [[ -n "${DEEPFACE_WORKERS:-}" ]]; then echo "$DEEPFACE_WORKERS"; return 0; fi
  mem="$(airlock_container_memory_mb)"
  if [[ -z "$mem" ]]; then echo 2; return 0; fi      # unknown: the old default
  avail=$(( mem - AIRLOCK_DEEPFACE_RESERVE_MB ))
  n=$(( avail / AIRLOCK_DEEPFACE_MB_PER_WORKER ))
  (( n < 1 )) && n=1
  (( n > 4 )) && n=4
  echo "$n"
}

# "ok <mb>" when there is room for deepface's container, else "no: <why>" with
# the command that fixes it. Not fatal anywhere: a cramped VM still runs the
# service, it just kills and restarts workers under load.
AIRLOCK_DEEPFACE_MIN_MB="${AIRLOCK_DEEPFACE_MIN_MB:-3072}"
airlock_container_memory_status() {
  local mem
  mem="$(airlock_container_memory_mb)"
  [[ -n "$mem" ]] || { echo "ok unknown"; return 0; }
  if (( mem >= AIRLOCK_DEEPFACE_MIN_MB )); then
    echo "ok ${mem} MB"
    return 0
  fi
  if [[ "$AIRLOCK_OS" == "Darwin" ]]; then
    echo "no: the podman machine has only ${mem} MB, and backend-deepface wants"\
         "~1.1 GB per worker; give the VM more with:"\
         "podman machine stop && podman machine set --memory ${AIRLOCK_DEEPFACE_MIN_MB} && podman machine start"
  else
    echo "no: this machine has only ${mem} MB of RAM, and backend-deepface wants ~1.1 GB per worker"
  fi
}

# ── Can this service be INSTALLED natively on this platform at all? ───────────
# Not a question about tools, about WHEELS. No amount of Python or ffmpeg makes
# a package installable on a platform nobody builds it for, and the failure that
# teaches you this is a pip ResolutionImpossible several minutes into a deploy.
#
# The decisive case is deepface on an INTEL MAC. requirements.txt asks for
# tf-keras >= 2.20, which needs tensorflow >= 2.20, and TensorFlow 2.20
# publishes exactly these wheels:
#
#   macosx_12_0_arm64                 Apple Silicon        yes
#   manylinux2014_x86_64              Ubuntu and kin       yes
#   manylinux2014_aarch64             Raspberry Pi 64-bit  yes
#   (nothing for macosx x86_64)       Intel Mac            NO
#
# The newest TensorFlow for an Intel Mac is 2.16.2 - the line was dropped, not
# delayed. So native deepface there is not "slow" or "unaccelerated", it is
# uninstallable, and the container is the answer: it is a linux/amd64 image,
# which is one of the platforms that does have wheels.
#
# "ok <detail>" or "no: <why>".
airlock_host_supported() {  # airlock_host_supported <DEEPFACE|TRANSCODE>
  case "$1/${AIRLOCK_OS}/${AIRLOCK_ARCH}" in
    DEEPFACE/Darwin/x86_64)
      echo "no: TensorFlow publishes no macOS x86_64 wheels after 2.16 and this"\
           "service needs >= 2.20, so it cannot be installed natively on an"\
           "Intel Mac; the container image is linux/amd64, where those wheels exist"
      ;;
    # 32-bit ARM has no TensorFlow wheels either, and a Pi running a 32-bit OS
    # is a real possibility - Raspberry Pi OS still ships one.
    DEEPFACE/Linux/arm32)
      echo "no: TensorFlow publishes no 32-bit ARM wheels; use a 64-bit OS"
      ;;
    *) echo "ok $(airlock_platform)" ;;
  esac
}

# ── Can this host run this service natively? ──────────────────────────────────
# "ok <detail>" or "no: <why>". Asked BEFORE host mode is chosen, because the
# alternative is not "no service" - it is the container, which carries its own
# Python and its own ffmpeg and therefore always works. The only thing a
# container gives up is the accelerator.
airlock_host_ready() {  # airlock_host_ready <DEEPFACE|TRANSCODE>
  local py ff plat
  plat="$(airlock_host_supported "$1")"
  [[ "$plat" == ok* ]] || { echo "$plat"; return 0; }
  case "$1" in
    DEEPFACE)
      py="$(airlock_python_status)"
      [[ "$py" == ok* ]] || { echo "$py"; return; }
      echo "ok python ${py#ok }"
      ;;
    TRANSCODE)
      ff="$(airlock_ffmpeg_status)"
      [[ "$ff" == ok* ]] || { echo "$ff"; return; }
      py="$(airlock_python_status)"
      [[ "$py" == ok* ]] || { echo "$py"; return; }
      echo "ok ffmpeg ${ff#ok }, python ${py#ok }"
      ;;
    *) echo "no: unknown service $1" ;;
  esac
}

# What host mode would actually BUY on this platform, for a service, or nothing
# when the answer is "no accelerator, so it is just a process outside a
# container". This is what keeps auto from choosing host on an Intel Mac for
# deepface: tensorflow-metal is Apple-Silicon-only, so a native deepface there
# runs the same CPU code as the container with none of the isolation.
airlock_host_gains() {  # airlock_host_gains <DEEPFACE|TRANSCODE>
  case "$1/${AIRLOCK_OS}/${AIRLOCK_ARCH}" in
    DEEPFACE/Darwin/arm64)  echo "Metal is reachable only natively" ;;
    TRANSCODE/Darwin/*)     echo "VideoToolbox is reachable only natively" ;;
    # A Pi's hardware encoder is a /dev/video* device. A container can be given
    # it, but host mode needs no device plumbing, so it stays available by
    # explicit AIRLOCK_TRANSCODE_MODE=host.
    TRANSCODE/Linux/arm64)  echo "" ;;
    *)                      echo "" ;;
  esac
}

# airlock_service_mode <SERVICE>  ->  "<mode> <reason>"
# SERVICE is DEEPFACE or TRANSCODE.
#
# Two fallbacks, both of the same shape - ASK WHETHER THE MACHINE CAN DO IT
# BEFORE CHOOSING IT, and say what was chosen instead:
#
#   gpu  -> container  when there is no usable NVIDIA GPU
#   host -> container  when the host lacks the Python or the ffmpeg that a
#                      native process needs
#
# The second one is why a new Mac used to end up with two dead services: auto
# chose host on any Darwin, and host mode then needed a Python 3.10+ and an
# ffmpeg that nothing had checked for or installed. An explicit mode gets the
# same treatment as gpu already did: honoured if the machine can do it, swapped
# with a reason if it cannot, never started to fail.
airlock_service_mode() {
  local svc="$1" var="AIRLOCK_${1}_MODE" want nvidia ready gains
  want="${!var:-auto}"
  case "$want" in
    off)       echo "off set by $var"; return ;;
    container) echo "container set by $var"; return ;;
    host|gpu|auto) ;;
    *)         echo "container unknown $var=$want, using container"; return ;;
  esac

  # Host mode, asked for explicitly or reached by auto below.
  if [[ "$want" == "host" ]]; then
    ready="$(airlock_host_ready "$svc")"
    if [[ "$ready" == ok* ]]; then
      echo "host set by $var (${ready#ok })"
    else
      echo "container $var=host, but ${ready#no: }"
    fi
    return
  fi

  if [[ "$want" == "auto" ]]; then
    gains="$(airlock_host_gains "$svc")"
    if [[ -n "$gains" ]]; then
      ready="$(airlock_host_ready "$svc")"
      if [[ "$ready" == ok* ]]; then
        echo "host $(airlock_platform): $gains"; return
      fi
      # The accelerator is there and the host is not ready for it. Still the
      # container, because a working CPU service beats an absent one - but the
      # reason names what it costs, so it reads as a thing to fix.
      echo "container $(airlock_platform) could use host mode, but ${ready#no: }"
      return
    fi
    # No accelerator to reach natively. On Darwin that is the Intel-Mac
    # deepface case; on Linux the GPU question below is the whole decision.
    if [[ "$AIRLOCK_OS" == "Darwin" ]]; then
      ready="$(airlock_host_supported "$svc")"
      if [[ "$ready" == ok* ]]; then
        echo "container $(airlock_platform): nothing for host mode to accelerate here"
      else
        echo "container ${ready#no: }"
      fi
      return
    fi
  fi

  nvidia="$(airlock_nvidia_status)"
  if [[ "$nvidia" == ok* ]]; then
    echo "gpu ${nvidia#ok }"
  elif [[ "$want" == "gpu" ]]; then
    echo "container $var=gpu, but ${nvidia#no: }"
  else
    echo "container no usable GPU (${nvidia#no: })"
  fi
}

# The address native services listen on. Never 0.0.0.0 on a machine with a LAN
# address: the gateway is the way in from outside.
#   macOS: 127.0.0.1 - podman's VM reaches the host's loopback through
#          host.containers.internal (verified 2026-10-02).
#   Linux: the address containers use for the host (AIRLOCK_HOST_ALIAS) when it
#          is an IP, plus 127.0.0.1 for local checks.
airlock_native_binds() {
  if [[ "$(uname -s)" == "Darwin" ]]; then
    echo "127.0.0.1"
  elif [[ "${AIRLOCK_HOST_ALIAS:-}" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]]; then
    echo "127.0.0.1 $AIRLOCK_HOST_ALIAS"
  else
    echo "0.0.0.0"
  fi
}

# How the backend reaches the transcode service: by a name it resolves to the
# host (--add-host), on the service's own port - not through the gateway, whose
# timeouts are sized for web requests, not for a long video. Empty when the
# service is off, and the import then makes previews itself.
if [[ -z "${TRANSCODE_URL+x}" ]]; then
  if [[ "${AIRLOCK_TRANSCODE_MODE:-auto}" == "off" ]]; then
    TRANSCODE_URL=""
  else
    TRANSCODE_URL="http://transcode.airlock.lan:5004"
  fi
fi
export TRANSCODE_URL
