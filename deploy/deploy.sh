#!/usr/bin/env bash
# Prepare this machine to run airlock, then build the images.
#
#   ./deploy/deploy.sh            # check and prepare this machine
#   ./deploy/deploy.sh --check    # check only, change nothing
#   ./deploy/deploy.sh --start    # prepare, then build and start the stack
#
# Run this once after copying the repository to a new machine, then start the
# stack with ./start-all.sh.
#
# What it does, in order:
#   1. checks podman, its mode, and the tools the stack needs
#   2. writes deploy/airlock.env if it does not exist
#   3. creates the host directories that get bind-mounted
#   4. checks they are owned by you, which is what --userns=keep-id needs
#   5. builds the virtualenvs for the services that will run natively here,
#      and caches the face models - the host half of the stack, which no
#      container image can provide
#
# Building and starting is start-all.sh's job; --start hands over to it.
#
# Step 4 is the one that does not exist on macOS and is mandatory on Linux.
# See deploy/README.md.

{   # read-the-whole-file guard; see start-all.sh for why

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/airlock-env.sh"
# shellcheck disable=SC1091
source "$HERE/../tools/airlock-venv.sh"

CHECK_ONLY=""
NO_BUILD=""
START_AFTER=""
for arg in "$@"; do
  case "$arg" in
    --check)    CHECK_ONLY=1; NO_BUILD=1 ;;
    --no-build) NO_BUILD=1 ;;
    --start)    START_AFTER=1 ;;
    -h|--help)  sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg"; exit 2 ;;
  esac
done

PROBLEMS=0
# Things --check found that a real run would fix. Not problems with the machine,
# but the difference between "ready" and "not run yet" - and reporting "ready"
# while listing seven missing directories is worse than useless, because the
# very next command fails with "statfs: no such file or directory".
PENDING=0
ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; PROBLEMS=$((PROBLEMS+1)); }

OS="$(uname -s)"
ROOTLESS="$(podman info --format '{{.Host.Security.Rootless}}' 2>/dev/null || echo unknown)"

echo "── Host ─────────────────────────────────────────────────────────────────"
echo "  repository      $AIRLOCK_ROOT"
echo "  os              $OS"
echo "  platform        $(airlock_platform)"
# A 32-bit Raspberry Pi OS is a real thing to end up on by accident, and the
# consequence is not slowness: no TensorFlow wheel exists for 32-bit ARM in
# EITHER mode, so face recognition cannot be installed at all - the container
# build fails too. Said here, once, rather than discovered during an image build.
if [[ "$AIRLOCK_ARCH" == "arm32" ]]; then
  warn "32-bit ARM: backend-deepface cannot be installed on this architecture"
  warn "  (no TensorFlow wheels, host or container). Use a 64-bit OS - on a"
  warn "  Raspberry Pi, the arm64 image. Everything else works."
fi
if command -v podman >/dev/null 2>&1; then
  ok "podman $PODMAN_VERSION  (rootless: $ROOTLESS)"
  # 4.7 is where BOTH of the flags these scripts want became available.
  # Ubuntu 22.04 ships 3.4.4, from 2021, and fails a run outright with
  # "unrecognized namespace mode" and "invalid IP address in add-host".
  # The fallbacks in airlock-env.sh keep an older podman working; this says
  # what is being given up.
  if _version_at_least "$PODMAN_VERSION" 4.7; then
    ok "podman is new enough for keep-id options and host-gateway"
  else
    warn "podman $PODMAN_VERSION is old - using fallbacks:"
    warn "  user namespace : ${AIRLOCK_USERNS:-none}"
    warn "  host alias     : $AIRLOCK_HOST_ALIAS  (instead of host-gateway)"
    warn "  A newer podman is worth having:"
    warn "    Ubuntu 22.04 ships 3.4.4; 24.04 ships 4.9.3."
    warn "    Or: sudo apt install -t \$(lsb_release -cs)-backports podman"
  fi
else
  bad "podman not found - install it before continuing"
fi

if [[ "$OS" == "Darwin" ]]; then
  if podman machine list --format '{{.Running}}' 2>/dev/null | grep -qi true; then
    ok "podman machine running"
  else
    bad "podman machine is not running - podman machine start"
  fi
fi

# Rootless podman needs subuid/subgid ranges for the invoking user. Without
# them every container that runs as a non-root user fails in ways that look
# like permission bugs in the application.
if [[ "$OS" == "Linux" && "$ROOTLESS" == "true" ]]; then
  if grep -q "^$(id -un):" /etc/subuid 2>/dev/null && grep -q "^$(id -un):" /etc/subgid 2>/dev/null; then
    ok "subuid/subgid ranges present for $(id -un)"
  else
    bad "no subuid/subgid range for $(id -un) - rootless containers cannot map users"
    warn "  sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 $(id -un)"
    warn "  podman system migrate"
  fi
  if [[ -n "$AIRLOCK_USERNS" ]]; then
    ok "user namespace: $AIRLOCK_USERNS"
  else
    warn "AIRLOCK_USERNS is empty - bind mounts may be unwritable by the containers"
  fi
fi

command -v curl >/dev/null 2>&1 && ok "curl present" || bad "curl not found (health checks need it)"

# Ollama is a host service, not a container: backend-vision and backend-albums
# call it. Missing it is not fatal to a deploy, only to those two features.
if curl -fsS -o /dev/null --max-time 2 http://localhost:11434/api/tags 2>/dev/null; then
  ok "Ollama reachable on the host"
else
  warn "Ollama not reachable at localhost:11434 - vision and album services will fail"
fi

echo
echo "── Configuration ────────────────────────────────────────────────────────"
ENV_FILE="$AIRLOCK_ROOT/deploy/airlock.env"
if [[ -f "$ENV_FILE" ]]; then
  ok "deploy/airlock.env present"
else
  if [[ -n "$CHECK_ONLY" ]]; then
    warn "deploy/airlock.env missing - defaults would be used"
    PENDING=$((PENDING+1))
  else
    cp "$HERE/airlock.env.example" "$ENV_FILE"
    ok "wrote deploy/airlock.env from the example - review it"
  fi
fi
echo "  catalogs        $CATALOGS_HOST_DIR"
echo "  input           $INPUT_HOST_DIR"
echo "  storage root    $STORAGE_ROOT_HOST  (read-only, ingestion sources)"
echo "  database        $DATABASE_HOST_DIR"

echo
echo "── Host directories ─────────────────────────────────────────────────────"
DIRS=(
  "$CATALOGS_HOST_DIR"
  "$INPUT_HOST_DIR"
  "$DATABASE_HOST_DIR"
  "$AIRLOCK_ROOT/logs"
  "$AIRLOCK_ROOT/systems/backend-deepface/uploads"
  "$AIRLOCK_ROOT/systems/backend-deepface/database_images"
  "$AIRLOCK_ROOT/systems/backend-deepface/deepface_weights"
)
for d in "${DIRS[@]}"; do
  if [[ -d "$d" ]]; then
    ok "exists  $d"
  elif [[ -n "$CHECK_ONLY" ]]; then
    warn "missing $d"
    PENDING=$((PENDING+1))
  else
    mkdir -p "$d" && ok "created $d" || bad "could not create $d"
  fi
done

if [[ ! -d "$STORAGE_ROOT_HOST" ]]; then
  bad "storage root does not exist: $STORAGE_ROOT_HOST"
fi

# ── Gateway TLS certificate ──────────────────────────────────────────────────
# The nginx config references a certificate, so `nginx -t` cannot pass without
# one and the gateway exits before it ever binds a port. A private CA for .lan
# names - no public CA can issue for those - generated by openssl with no
# prompts, so there is no reason to make a person run it as a separate step.
echo
echo "── Gateway certificate ──────────────────────────────────────────────────"
CERT_DIR="$AIRLOCK_ROOT/systems/gateway/certs"
if [[ -f "$CERT_DIR/fullchain.pem" && -f "$CERT_DIR/privkey.pem" ]]; then
  ok "certificate present"
elif [[ -n "$CHECK_ONLY" ]]; then
  warn "no certificate in $CERT_DIR - the gateway will not start"
  PENDING=$((PENDING+1))
elif ! command -v openssl >/dev/null 2>&1; then
  bad "openssl not found - needed to generate the gateway certificate"
  warn "  sudo apt install openssl, then: systems/gateway/make-certs.sh"
elif [[ -x "$AIRLOCK_ROOT/systems/gateway/make-certs.sh" ]]; then
  if ( cd "$AIRLOCK_ROOT/systems/gateway" && ./make-certs.sh \
        > "$AIRLOCK_ROOT/logs/make-certs.log" 2>&1 ); then
    ok "generated a certificate for the .lan names and this machine's IPs"
    warn "  Install THIS MACHINE'S systems/gateway/certs/airlock-ca.pem on each"
    warn "  device that will use it, or browsers will warn. Every deployment has"
    warn "  its own CA, named after the machine; a device that uses two"
    warn "  deployments needs both roots installed."
  else
    bad "make-certs.sh failed - see logs/make-certs.log"
  fi
else
  bad "systems/gateway/make-certs.sh is missing or not executable"
fi

# ── Privileged ports ─────────────────────────────────────────────────────────
#
# The gateway publishes 80 and 443, and rootless podman refuses any port below
# ip_unprivileged_port_start. This was the one preparation step that lived
# nowhere: it was mentioned in a Linux-only footnote AFTER "this machine is
# ready", while macOS - where a fresh podman machine always starts at 1024 - was
# told nothing at all.
#
# It fails late and quietly. deploy.sh reports ready, start-all.sh builds
# everything, and only the gateway dies; every other service is healthy. The UI
# is then reachable only on :3000, which serves the page but not /api, so a
# brand-new installation greets you with a sign-in form for an account that
# does not exist. See deploy/README.md, "The first run".
echo
echo "── Privileged ports ─────────────────────────────────────────────────────"
FLOOR="$(_unprivileged_port_floor)"
if [[ -z "$FLOOR" ]]; then
  if [[ "$OS" == "Darwin" ]]; then
    warn "cannot read the podman machine's port floor - is the machine running?"
  else
    warn "cannot read net.ipv4.ip_unprivileged_port_start on this host"
  fi
  warn "  the gateway needs to bind 80 and 443; if it will not start, see:"
  warn "  systems/gateway/enable-port-80.sh --check"
elif (( FLOOR <= 80 )); then
  ok "unprivileged port floor is $FLOOR - the gateway can bind 80 and 443"
elif [[ -n "$CHECK_ONLY" ]]; then
  warn "unprivileged port floor is $FLOOR - the gateway cannot bind 80 or 443"
  warn "  a real run fixes this on macOS; on Linux it needs one sudo command"
  PENDING=$((PENDING+1))
elif [[ "$OS" == "Darwin" ]]; then
  # Lowering it inside the podman machine changes nothing on the Mac itself,
  # which is why this is safe to do unasked - the same standing as generating
  # the certificate above.
  if "$AIRLOCK_ROOT/systems/gateway/enable-port-80.sh" >/dev/null 2>&1      && FLOOR="$(_unprivileged_port_floor)" && [[ -n "$FLOOR" ]] && (( FLOOR <= 80 )); then
    ok "lowered the podman machine's port floor to $FLOOR (persistent, VM only)"
  else
    bad "could not lower the podman machine's port floor (currently $FLOOR)"
    warn "  run it directly to see why:  systems/gateway/enable-port-80.sh"
    warn "  or run the gateway high:     systems/gateway/enable-port-80.sh --port-8080"
  fi
else
  # Not run unasked: this is a sysctl on the host itself, not inside a VM.
  bad "unprivileged port floor is $FLOOR - the gateway cannot bind 80 or 443"
  warn "  sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80"
  warn "  echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/99-airlock.conf"
  warn "  or run the gateway high:  systems/gateway/enable-port-80.sh --port-8080"
fi

# ── Ownership ────────────────────────────────────────────────────────────────
#
# There is deliberately NO chown here, and that is the whole point.
#
# Two mechanisms can make a bind mount writable by a container that runs as uid
# 1000, and they are MUTUALLY EXCLUSIVE:
#
#   a) leave the default rootless mapping and chown the host directory to the
#      subuid that container-1000 maps to (100999 or similar), via
#      `podman unshare chown -R 1000:1000`;
#   b) run with --userns=keep-id:uid=1000,gid=1000, which maps the INVOKING
#      USER to container uid 1000, so a directory you own is already owned by
#      the process that writes it.
#
# This deployment uses (b). Doing both is not belt-and-braces, it is a
# contradiction: the chown hands the directory to 100999, keep-id then runs the
# process as you, and the container cannot write to its own data directory. The
# symptom is an empty database directory owned by a five-digit uid, and a
# backend that quietly never creates catalogs.db.
#
# (b) is also the better half on its own terms: the files stay owned by you, so
# backing up or inspecting the catalog tree needs no podman at all.
#
# If you ever need to undo an old chown from (a):
#     podman unshare chown -R 0:0 <dir>     # 0 inside the namespace is you
if [[ "$OS" == "Linux" && "$ROOTLESS" == "true" ]]; then
  MISMATCHED=()
  CHECKED_ANY=""
  for d in "$CATALOGS_HOST_DIR" "$INPUT_HOST_DIR" "$DATABASE_HOST_DIR" \
           "$AIRLOCK_ROOT/systems/backend-deepface/uploads" \
           "$AIRLOCK_ROOT/systems/backend-deepface/database_images" \
           "$AIRLOCK_ROOT/systems/backend-deepface/deepface_weights"; do
    [[ -d "$d" ]] || continue
    if [[ -z "$CHECKED_ANY" ]]; then
      # Only now that there is something to say.
      echo
      echo "── Ownership ────────────────────────────────────────────────────────────"
      CHECKED_ANY=1
    fi
    owner="$(stat -c '%u' "$d" 2>/dev/null || echo '?')"
    if [[ "$owner" == "$(id -u)" ]]; then
      ok "owned by you  $d"
    else
      MISMATCHED+=("$d")
      bad "owned by uid $owner, not you ($(id -u)):  $d"
    fi
  done
  if (( ${#MISMATCHED[@]} )); then
    warn "keep-id runs the containers as YOU, so these are unwritable by them."
    warn "Give them back with:"
    for d in "${MISMATCHED[@]}"; do
      warn "  podman unshare chown -R 0:0 $d"
    done
    if [[ -z "$CHECK_ONLY" ]]; then
      echo
      for d in "${MISMATCHED[@]}"; do
        if podman unshare chown -R 0:0 "$d" 2>/dev/null; then
          ok "restored $d"
          PROBLEMS=$((PROBLEMS-1))
        fi
      done
    fi
  fi
  # STORAGE_ROOT_HOST is never touched: it is mounted read-only and holds the
  # user's own photo library.
fi

# ── Face detector weights ────────────────────────────────────────────────────
# YuNet is loaded straight from a path with cv2.FaceDetectorYN_create, and
# NOTHING downloads it - get_yunet() just returns None when the file is absent,
# and face detection then fails with "YuNet weights not found" on every request.
# The startup log says "it will be downloaded", which is not true: DeepFace
# fetches its own RECOGNITION models lazily, but this detector is not one of
# them.
#
# 227 KB, from the OpenCV Zoo. Fetched here rather than at runtime because the
# app's own comments warn that downloading inside a forked gunicorn worker
# aborts the worker on macOS.
echo
echo "── Face detector weights ────────────────────────────────────────────────"
YUNET_DIR="$AIRLOCK_ROOT/systems/backend-deepface/deepface_weights/weights"
YUNET="$YUNET_DIR/face_detection_yunet_2023mar.onnx"
YUNET_URL="https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx"
YUNET_SHA="8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"

_sha256() {
  if command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  elif command -v shasum   >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  fi
}

if [[ -f "$YUNET" ]]; then
  ok "YuNet detector present"
elif [[ -n "$CHECK_ONLY" ]]; then
  warn "YuNet detector missing - face detection would fail"
  PENDING=$((PENDING+1))
else
  mkdir -p "$YUNET_DIR"
  if curl -fsSL "$YUNET_URL" -o "$YUNET.part" 2>/dev/null; then
    got="$(_sha256 "$YUNET.part")"
    if [[ -z "$got" || "$got" == "$YUNET_SHA" ]]; then
      mv "$YUNET.part" "$YUNET"
      ok "downloaded the YuNet detector ($(wc -c < "$YUNET" | tr -d ' ') bytes)"
    else
      rm -f "$YUNET.part"
      bad "YuNet download did not match the expected checksum"
      warn "  expected $YUNET_SHA"
      warn "  got      $got"
    fi
  else
    rm -f "$YUNET.part"
    bad "could not download the YuNet detector"
    warn "  fetch it by hand into $YUNET_DIR/"
    warn "  $YUNET_URL"
  fi
fi

echo
echo "── Native services ──────────────────────────────────────────────────────"
# THE SECTION THE README ALREADY PROMISED AND NOTHING DID.
#
# deepface and backend-transcode can each run as a native process, which is the
# only way to reach Metal or VideoToolbox - podman on a Mac runs containers in a
# Linux VM, and neither framework crosses into it. A native process needs two
# things from the HOST that a container image provides for itself: a Python new
# enough for the requirements, and (for transcode) ffmpeg.
#
# This is where those are checked and where the virtualenvs are built, so the
# first failure happens HERE, in a script someone is watching, with the fix on
# the next line - rather than inside a backgrounded start script whose log is
# the only record.
#
# A machine that cannot do host mode is not broken: airlock_service_mode falls
# back to the container. That is why nothing in this section calls bad().
NATIVE_PENDING=0

PY_STATUS="$(airlock_python_status)"
if [[ "$PY_STATUS" == ok* ]]; then
  ok "python ${PY_STATUS#ok }  (floor $AIRLOCK_PYTHON_MIN, containers use 3.12)"
else
  warn "${PY_STATUS#no: }"
fi

FF_STATUS="$(airlock_ffmpeg_status)"
if [[ "$FF_STATUS" == ok* ]]; then
  ok "ffmpeg ${FF_STATUS#ok }"
else
  warn "${FF_STATUS#no: }"
fi

# The memory a container can have. On macOS that is the podman VM's fixed
# allocation and nothing to do with the Mac's RAM, which is why it has to be
# reported rather than assumed: every container shares it, and deepface's
# workers each hold their own copy of VGG-Face.
MEM_STATUS="$(airlock_container_memory_status)"
if [[ "$MEM_STATUS" == ok* ]]; then
  ok "container memory ${MEM_STATUS#ok } (deepface sizes its workers to this: $(airlock_deepface_workers))"
else
  warn "${MEM_STATUS#no: }"
  warn "  deepface will run $(airlock_deepface_workers) worker(s) and may restart them under load"
fi

# What each service will actually do on this machine, by the same function
# start-all.sh and status.sh use - so all three agree by construction.
for pair in "DEEPFACE backend-deepface" "TRANSCODE backend-transcode"; do
  read -r SVC DIR <<<"$pair"
  DECISION="$(airlock_service_mode "$SVC")"
  MODE="${DECISION%% *}"
  echo "  $DIR: $MODE (${DECISION#* })"

  [[ "$MODE" == "host" ]] || continue

  SVC_DIR="$AIRLOCK_ROOT/systems/$DIR"
  STATE="$(airlock_venv_state "$SVC_DIR/venv" "$SVC_DIR/requirements.txt" gunicorn)"
  if [[ "$STATE" == ready* ]]; then
    ok "  venv ready (${STATE#ready })"
    continue
  fi
  if [[ -n "$CHECK_ONLY" ]]; then
    warn "  venv ${STATE#* } - a real run would build it"
    PENDING=$((PENDING+1))
    continue
  fi
  echo "  building the venv (${STATE#* })"
  # PIPESTATUS, not the pipeline's status: the output is indented through sed,
  # and sed succeeds whatever pip did.
  (
    cd "$SVC_DIR" || exit 1
    export AIRLOCK_SERVICE="$SVC"
    airlock_venv_ensure "$SVC_DIR/venv" "$SVC_DIR/requirements.txt" gunicorn
  ) 2>&1 | sed -e 's/^/    /'
  VENV_RC="${PIPESTATUS[0]}"
  if (( VENV_RC == 0 )); then
    ok "  venv built"
    # DeepFace's models, fetched in a plain process. A download inside a forked
    # gunicorn worker aborts the worker on macOS, so it must not be the first
    # request that triggers it.
    if [[ "$SVC" == "DEEPFACE" && -x "$SVC_DIR/venv/bin/python3" ]]; then
      if DEEPFACE_HOME="$SVC_DIR" "$SVC_DIR/venv/bin/python3" \
           "$AIRLOCK_ROOT/tools/prefetch_weights.py" 2>&1 | sed -e 's/^/    /'; then
        ok "  models cached"
      else
        warn "  model pre-fetch incomplete - they will be fetched at first use"
      fi
    fi
  else
    warn "  venv could not be built - this service will need AIRLOCK_${SVC}_MODE=container"
    NATIVE_PENDING=$((NATIVE_PENDING+1))
  fi
done

if (( NATIVE_PENDING > 0 )); then
  warn "$NATIVE_PENDING native service(s) are not ready; they can run in containers:"
  warn "  set AIRLOCK_DEEPFACE_MODE / AIRLOCK_TRANSCODE_MODE to container in deploy/airlock.env"
fi

echo
if [[ -z "$NO_BUILD" ]]; then
  echo "── Building images ──────────────────────────────────────────────────────"
  # Delegated to each service's own start-podman.sh rather than reimplemented
  # here. Those scripts know their image tag, their .dockerignore and their
  # build arguments, and the tags are NOT the directory names - backend builds
  # "workflow-backend", frontend builds "workflow-frontend". A second copy of
  # that knowledge here would be wrong the first time one of them changed.
  echo "  Skipped: run ./start-all.sh --build, which builds and starts in the"
  echo "  right order. Use --start below to do it now."
fi

echo
echo "── Result ───────────────────────────────────────────────────────────────"
if (( PROBLEMS == 0 && PENDING > 0 )); then
  warn "$PENDING thing(s) still to set up - nothing is wrong, this was only a check"
  echo
  echo "  Run:   ./deploy/deploy.sh          (creates the directories and the config)"
  echo "         ./start-all.sh --build"
  exit 0
fi
if (( PROBLEMS == 0 )); then
  ok "this machine is ready"
  if [[ -n "$START_AFTER" ]]; then
    echo
    exec "$AIRLOCK_ROOT/start-all.sh" --build
  fi
  echo
  echo "  Next:  ./start-all.sh --build      (first run on this machine)"
  echo "         ./start-all.sh              (afterwards)"
  echo
  echo "  Then open the gateway's address and create the administrator account."
  echo "  From another machine that is https://<this machine's LAN address>/ -"
  echo "  airlock.lan means \"me\" wherever it is typed, and a service port such"
  echo "  as :3000 serves the page but not /api. See deploy/README.md."
else
  bad "$PROBLEMS problem(s) above need attention"
  exit 1
fi

exit 0

}   # end read-the-whole-file guard
