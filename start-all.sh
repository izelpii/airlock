#!/usr/bin/env bash
# Start the whole airlock stack.
#
# Replaces the pre-containerization start-all.sh. Order matters: services the
# backend calls during workflows must be up before it starts accepting work.
#
#   gateway            pod, :80 - routes every name
#   backend-deepface   native where that reaches an accelerator the container
#                      cannot, container otherwise (AIRLOCK_DEEPFACE_MODE)
#   backend-transcode  video previews; same modes (AIRLOCK_TRANSCODE_MODE)
#   backend-geoloc     pod
#   backend-pets       pod; only started when a catalog has pets enabled
#   backend-vision     pod  (needs Ollama on the host)
#   backend-albums     pod  (needs Ollama on the host)
#   backend            pod
#   album-gallery      pod
#   frontend           pod
#
# Native ("host") mode is chosen per platform AND per host: Apple Silicon gets
# it for both services, an Intel Mac only for transcode (VideoToolbox exists,
# Metal for TensorFlow does not), Linux uses a GPU container or a plain one. A
# host missing the Python or the ffmpeg a native process needs falls back to the
# container with the reason - see deploy/airlock-env.sh, airlock_service_mode.
#
# Services still listen on their own ports, but nothing in the application knows
# them any more: the gateway is the only thing that maps a name to a port. Health
# checks below go through the gateway on purpose - checking localhost:5000 would
# pass while the thing users actually reach was broken.
#
# Usage:
#   ./start-all.sh                        # start everything, build only what is stale
#   ./start-all.sh --build                # rebuild every image first
#   ./start-all.sh --build=frontend,backend    # rebuild just those two
#   ./start-all.sh --build frontend,backend    # same thing
#   ./start-all.sh --no-deepface          # skip deepface (already running elsewhere)
#
# --build takes an optional comma-separated list of services. Without one it
# means "all", which is what it has always meant. The common case during
# development is the two images that hold compiled or baked-in code:
#
#   ./start-all.sh --build=frontend,backend
#
# frontend is a two-stage image - `npm run build` runs INSIDE it and nothing is
# mounted from the host - so editing src/ changes nothing at all until that
# image is rebuilt. backend bakes in app.py, models.py and the rest, though
# workflows/ is bind-mounted and needs no rebuild.
#
# Each start-podman.sh ALSO rebuilds on its own when its sources are newer than
# its image, so a plain ./start-all.sh is usually enough. --build is for forcing
# it: a changed base image, a dependency bump, or a build you want to be certain
# of rather than infer.

# ── Read-the-whole-file guard ─────────────────────────────────────────────────
# Everything below runs inside one brace group, which bash must parse in full
# before executing any of it.
#
# Without this, bash reads a script incrementally by byte offset. Editing the
# file while it is running shifts every later byte, and execution resumes at a
# now-meaningless position - producing a syntax error on a line that is
# perfectly valid, which is a genuinely confusing way to lose an afternoon.
# During active development of this stack that is not hypothetical.
{

set -uo pipefail

GATEWAY_UP=0
NAMES_RESOLVE=0

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMS="$ROOT/systems"
LOGS="$ROOT/logs"
mkdir -p "$LOGS"

# One place for host paths, the rootless user namespace, and OS detection.
# shellcheck disable=SC1091
source "$ROOT/deploy/airlock-env.sh"

# THE CONTAINERS MUST OUTLIVE THIS SCRIPT.
#
# Each start-podman.sh runs `podman run` in the foreground, which is right when
# a person runs one directly. This script used to background them with `&`,
# which made every container a child of this shell: closing the terminal - or
# the SSH session - killed them, and `--rm` then deleted them. On a desktop that
# looks like it works; on a server you log out of, the stack disappears.
export AIRLOCK_DETACH=1
OS="$(uname -s)"

# Every image this script can build, by the name it is started with - which is
# also its directory under systems/ and its log file. ONE list: the three places
# below that launch a service all ask it whether to pass --build, so a service
# added here is buildable everywhere without touching them.
BUILDABLE="gateway backend-deepface backend-transcode backend-geoloc backend-vision backend-albums backend-pets backend album-gallery frontend"

# What people actually type. 'backend' is deliberately NOT a prefix match:
# matching it loosely would build backend-geoloc, backend-vision and
# backend-albums as well, which is the opposite of asking for one service.
build_alias() {
  case "$1" in
    deepface)  echo backend-deepface ;;
    transcode) echo backend-transcode ;;
    geoloc)    echo backend-geoloc ;;
    pets)      echo backend-pets ;;
    vision)    echo backend-vision ;;
    albums)    echo backend-albums ;;
    gallery)   echo album-gallery ;;
    ui|fe)     echo frontend ;;
    be)        echo backend ;;
    *)         echo "$1" ;;
  esac
}

BUILD_ALL=""
BUILD_ONLY=""      # space-delimited, resolved service names
SKIP_DEEPFACE=""
BAD_ARGS=""

add_build_targets() { # add_build_targets <comma-separated list>
  local item name
  # Commas or spaces, so --build="frontend backend" works too.
  for item in ${1//,/ }; do
    [[ -z "$item" ]] && continue
    if [[ "$item" == "all" ]]; then BUILD_ALL="1"; continue; fi
    name="$(build_alias "$item")"
    # Validated against BUILDABLE rather than passed through. A typo that
    # silently built nothing would be the worst outcome here: the script would
    # report success and serve the old bundle, which is exactly the failure the
    # frontend's own staleness check exists to prevent.
    if [[ " $BUILDABLE " == *" $name "* ]]; then
      [[ " $BUILD_ONLY " == *" $name "* ]] || BUILD_ONLY="$BUILD_ONLY $name"
    else
      BAD_ARGS="$BAD_ARGS $item"
    fi
  done
}

# An index, because --build may consume the argument after it.
args=("$@")
i=0
while (( i < ${#args[@]} )); do
  arg="${args[$i]}"
  case "$arg" in
    --build=*)
      add_build_targets "${arg#--build=}"
      ;;
    --build)
      # "--build frontend,backend" as well as "--build=frontend,backend". The
      # next argument is only consumed when it is not itself a flag, so a bare
      # --build followed by --no-deepface still means "build everything".
      if (( i + 1 < ${#args[@]} )) && [[ "${args[$((i+1))]}" != --* ]]; then
        add_build_targets "${args[$((i+1))]}"
        i=$((i+1))
      else
        BUILD_ALL="1"
      fi
      ;;
    --no-deepface) SKIP_DEEPFACE="1" ;;
    -h|--help)
      # The usage block at the top of this file is the only copy of it.
      #
      # Two -e expressions rather than one \s: BSD sed (macOS) has no \s, so a
      # single 's/^#\s\?//' silently matched nothing and printed the comment
      # markers verbatim. These are plain BRE and behave the same on both.
      sed -n '/^# Usage:/,/^$/p' "${BASH_SOURCE[0]}" | sed -e 's/^# //' -e 's/^#$//'
      # Generated from BUILDABLE, not typed again: a service added to that list
      # is one --help already knows about.
      echo "Services --build accepts:"
      for svc in $BUILDABLE; do echo "  $svc"; done
      echo
      echo "Short names: deepface transcode geoloc vision albums gallery ui|fe be all"
      exit 0
      ;;
    *)
      BAD_ARGS="$BAD_ARGS $arg"
      ;;
  esac
  i=$((i+1))
done

if [[ -n "$BAD_ARGS" ]]; then
  # Refused rather than ignored. This script used to drop anything it did not
  # recognise, so `--buidl` started the stack without building and said nothing
  # - and the next hour went into wondering why a change had not appeared.
  printf "  \033[31m✗\033[0m unknown argument(s):%s\n" "$BAD_ARGS" >&2
  echo >&2
  echo "  Services that can be built:" >&2
  for svc in $BUILDABLE; do echo "    $svc" >&2; done
  echo >&2
  echo "  Short names: deepface transcode geoloc vision albums gallery ui|fe be all" >&2
  echo "  Example:     ./start-all.sh --build=frontend,backend" >&2
  exit 2
fi

# --build with no list keeps its old meaning: everything.
if [[ -n "$BUILD_ALL" ]]; then
  BUILD_ONLY=""
fi

# Does this service get --build passed to its start script?
#
# Returns the flag itself so the callers stay a single word at the point of use,
# as `$BUILD` was. Unquoted expansion is intentional there: an empty result must
# disappear rather than become an empty argument.
build_flag() { # build_flag <service-name>
  if [[ -n "$BUILD_ALL" ]]; then echo "--build"; return; fi
  [[ " $BUILD_ONLY " == *" $1 "* ]] && echo "--build"
  return 0
}

# Which images actually got built this run, for the summary at the end.
REBUILT=""

# Services that were asked for and deliberately not built, with the reason.
# A plain list plus a generic guess was actively misleading: backend-pets was
# reported as "a service running natively here has no image to build", which
# is true of deepface and transcode on a Mac and nonsense for a pod that was
# simply never started. bash 3.2 has no associative arrays, so this is one
# pre-formatted line per service.
NOT_BUILT=""
not_built() { # not_built <service> <reason>
  NOT_BUILT="$NOT_BUILT
      $1: $2"
}

# Said BEFORE the start script runs.
#
# Every start-podman.sh writes to a log file, so from out here a service that
# is compiling a React bundle for four minutes and a service that is starting a
# container it already has look identical: the same line, then silence. Naming
# it, and naming the log to watch, is the difference between waiting and
# wondering whether anything is happening.
announce_build() { # announce_build <service-name>
  [[ -n "$(build_flag "$1")" ]] || return 0
  printf "  \033[36m⟳\033[0m %s: rebuilding its image - this can take a few minutes\n" "$1"
  # Relative to the repository root, which is where this script is run from, so
  # the line can be copied straight into another terminal.
  printf "      follow it with:  tail -f logs/%s.log\n" "$1"
  return 0
}

# Said AFTER, from what the start script decided rather than from what was asked
# of it.
#
# Each start-podman.sh rebuilds on its own when its sources are newer than its
# image, so a build can happen with no --build anywhere - and that pause is
# exactly as long and exactly as unexplained. Reading it back from the log is
# the only way to report what really happened. A line beginning "Building " is
# the one thing all nine of those scripts have in common; the rest of their
# wording differs.
report_build() { # report_build <service-name> <elapsed-seconds>
  if grep -q '^Building ' "$LOGS/$1.log" 2>/dev/null; then
    ok "$1: image rebuilt in ${2}s"
    REBUILT="$REBUILT $1"
  fi
  return 0
}

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }

wait_for() { # wait_for <name> <url> <seconds> [pid-to-watch]
  # ANY HTTP RESPONSE MEANS THE SERVICE IS UP.
  #
  # This used `curl -f`, which fails on any status >= 400 - including the 401
  # that /api/catalogs correctly returns when nobody is signed in. So the
  # backend health check could never pass once authentication was switched on,
  # and every start reported a failure for a service that was running fine.
  # What is being tested is "did something answer on this port", and a 401 is a
  # perfectly good answer.
  #
  # WITH A PID, THE PROCESS IS WATCHED TOO. A native service that exits during
  # boot - a missing dependency, a port already taken - used to cost the full
  # timeout in one-second polls of a port nothing would ever answer on, and then
  # reported "did not answer", which describes the symptom and not the cause.
  # Watching the pid turns 90 seconds of silence into an immediate "it exited",
  # and the caller then prints the end of its log, where the real error is.
  local name="$1" url="$2" timeout="${3:-60}" pid="${4:-}" i=0 code
  while (( i < timeout )); do
    code="$(curl -sS -o /dev/null -w '%{http_code}' --max-time 2 "$url" 2>/dev/null)"
    if [[ -n "$code" && "$code" != "000" ]]; then
      if [[ "$code" =~ ^[45] ]]; then
        ok "$name is up (HTTP $code)"
      else
        ok "$name is up"
      fi
      return 0
    fi
    if [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null; then
      die "$name exited after ${i}s without answering at $url"
      return 2
    fi
    sleep 1; ((i++))
  done
  warn "$name did not answer at $url after ${timeout}s (continuing)"
  return 1
}

# The last few lines of a service's log, indented, when it failed to come up.
# THE WHOLE POINT: the explanation is already written down - a missing ffmpeg, a
# pip resolution failure, a port in use - and making someone go and find the
# file is what turned a five-second diagnosis into an afternoon.
log_tail() { # log_tail <log-file> [lines]
  local file="$1" lines="${2:-12}"
  [[ -s "$file" ]] || { warn "  $file is empty"; return; }
  warn "  last ${lines} lines of ${file#$ROOT/}:"
  sed -e 's/^/      /' <(tail -n "$lines" "$file")
}

# Keep ONE previous log. Each start used to truncate, which is how the error
# that mattered got lost: the first run recorded "pip install failed", the
# second overwrote it with the consequence ("gunicorn: not found"), and the
# cause was gone before anyone looked.
rotate_log() { # rotate_log <log-file>
  [[ -s "$1" ]] && mv -f "$1" "$1.1" 2>/dev/null
  return 0
}

echo "── Preflight ────────────────────────────────────────────────────────────"

# IS THE WORKING DIRECTORY STILL THERE?
#
# Deleting and re-cloning the repository while a shell sits inside it leaves
# that shell holding the old, deleted inode. The prompt still prints the path
# because it is a cached string, but getcwd() fails and every subprocess that
# needs a working directory falls over. podman's output for that is five lines
# of "shell-init: error retrieving current directory", which names neither the
# cause nor the fix.
if ! /bin/pwd >/dev/null 2>&1; then
  die "this shell's working directory no longer exists"
  echo "     The repository was probably deleted and re-created while you were"
  echo "     inside it. Your shell still points at the old one."
  echo
  echo "     Fix:  cd $ROOT"
  echo "           (a fresh cd to the absolute path - 'cd .' will not do it)"
  exit 1
fi

command -v podman >/dev/null || { die "podman not found"; exit 1; }
# Only macOS has one: podman runs natively on Linux, where this check warned
# about a component that does not exist.
if [[ "$OS" == "Darwin" ]]; then
  podman machine list --format '{{.Running}}' 2>/dev/null | grep -q true \
    && ok "podman machine running" \
    || warn "podman machine may not be running — try: podman machine start"
fi

# Ollama powers backend-vision (descriptions) and backend-albums (prompt → SQL).
# Both reach it as ollama.airlock.lan, which the gateway forwards to the host,
# so it must listen on the host.
if curl -fsS -o /dev/null --max-time 2 http://localhost:11434/api/tags 2>/dev/null; then
  ok "Ollama reachable on the host"
else
  warn "Ollama not reachable at localhost:11434 — vision and album services will fail"
  warn "  start it with:  ollama serve"
fi

# ── Host tools the NATIVE services need ──────────────────────────────────────
# Reported here, before any service is started, because this is what decides
# whether deepface and transcode can run in host mode at all - and because a
# missing tool is a two-word fix that nobody can apply while it is buried in a
# service log. Not fatal: airlock_service_mode falls back to the container,
# which carries its own Python and its own ffmpeg.
echo "  platform        $(airlock_platform)"
_py_status="$(airlock_python_status)"
if [[ "$_py_status" == ok* ]]; then
  ok "python ${_py_status#ok }  (native services need $AIRLOCK_PYTHON_MIN to $AIRLOCK_PYTHON_MAX)"
else
  warn "${_py_status#no: }"
  warn "  native services will run in containers instead"
fi
_ff_status="$(airlock_ffmpeg_status)"
if [[ "$_ff_status" == ok* ]]; then
  ok "ffmpeg ${_ff_status#ok }"
else
  warn "${_ff_status#no: }"
  warn "  backend-transcode will run in a container (no VideoToolbox/NVENC)"
fi

# The backend's port. Fatal, and checked before anything starts: a taken :5000
# leaves the backend down while :5000 still answers - with AirPlay's 403 on a
# new Mac - which no later message would explain (airlock_backend_port_status).
_port_problem="$(airlock_backend_port_problem 5000)"
if [[ -n "$_port_problem" ]]; then
  while IFS= read -r _line; do printf "  \033[31m✗\033[0m %s\n" "$_line"; done <<< "$_port_problem"
  echo
  echo "Fix that, then run ./start-all.sh again."
  exit 1
fi

# How much memory a CONTAINER can have - on macOS the podman VM's allocation,
# not the Mac's. The one service this constrains is backend-deepface, whose
# every worker holds its own copy of VGG-Face; too little and its workers are
# OOM-killed in a loop that never mentions memory.
_mem_status="$(airlock_container_memory_status)"
if [[ "$_mem_status" == ok* ]]; then
  ok "container memory ${_mem_status#ok }"
else
  warn "${_mem_status#no: }"
fi

# Said out loud, because a build that was asked for and did not happen is
# indistinguishable from one that did until the page fails to change.
if [[ -n "$BUILD_ALL" ]]; then
  ok "rebuilding every image"
elif [[ -n "$BUILD_ONLY" ]]; then
  ok "rebuilding:${BUILD_ONLY}"
  echo "    the rest start from their current image, unless its sources are newer"
fi

echo
# ── Accelerated services ──────────────────────────────────────────────────────
# documentation/specs/accelerated-services-spec.md. Each runs natively, in a
# container, or in a container with the GPU, per AIRLOCK_<SERVICE>_MODE
# (default auto: native on macOS, GPU container on Linux with a usable NVIDIA
# card, plain container otherwise). Started before the backend, which calls
# them. Checked on their own ports, not through the gateway: a native service
# must be up before the gateway is asked to forward to it.
start_accelerated() { # start_accelerated <SERVICE> <dir> <port> <health-path>
  local svc="$1" dir="$SYSTEMS/$2" name="$2" port="$3" path="$4"
  local decision mode reason pid="" rc=0 _forced_build=""
  # The mode is a DECISION ABOUT THIS MACHINE, made in one place
  # (airlock_service_mode) and printed with its reason. host is chosen only when
  # the host has what a native process needs; otherwise it falls back to the
  # container, which carries its own Python and ffmpeg - so a machine missing a
  # host tool loses the accelerator, not the service.
  decision="$(airlock_service_mode "$svc")"
  mode="${decision%% *}"; reason="${decision#* }"
  echo "  mode $mode ($reason)"
  # A healthy service is NOT a reason to skip a rebuild somebody asked for.
  #
  # This shortcut used to come first unconditionally, so `--build=deepface`
  # against a running deepface printed "already running", returned, and never
  # reached the build - while the summary dutifully reported that it had been
  # asked to rebuild and had not. The old image kept serving, which is a bad
  # way to learn your change is not deployed. Only the two accelerated services
  # go through this function, which is why gateway rebuilt in the same run.
  #
  # Restricted to modes that HAVE an image: in host mode there is nothing to
  # build, and falling through would nohup a second native process that then
  # fails to bind the port. start-podman.sh uses --replace, so recreating a
  # live container is the normal path and needs no stop first.
  _forced_build="$(build_flag "$name")"
  if [[ -n "$_forced_build" && "$mode" != "host" && "$mode" != "off" ]]; then
    echo "  --build was asked for, so rebuilding rather than leaving it running"
  elif curl -sS -o /dev/null --max-time 2 "http://localhost:${port}${path}" 2>/dev/null \
     && [[ "$mode" != "off" ]]; then
    ok "already running"; return
  fi
  rotate_log "$LOGS/$name.log"
  case "$mode" in
    off) warn "$name not started (off)"; return ;;
    host)
      [[ -x "$dir/start-host.sh" ]] || { warn "$name has no start-host.sh"; return; }
      nohup "$dir/start-host.sh" > "$LOGS/$name.log" 2>&1 &
      pid="$!"
      echo "$pid" > "$LOGS/$name.pid"
      ;;
    container|gpu)
      announce_build "$name"
      local t0=$SECONDS
      ( cd "$dir" && AIRLOCK_GPU=$([[ "$mode" == gpu ]] && echo 1 || echo 0) \
          ./start-podman.sh $(build_flag "$name") > "$LOGS/$name.log" 2>&1 ) \
        || { warn "$name failed to start"; log_tail "$LOGS/$name.log"; return; }
      report_build "$name" $(( SECONDS - t0 ))
      ;;
  esac
  # The pid is passed for host mode only: a container's start script has already
  # returned by now, so there is no process of ours to watch.
  wait_for "$name" "http://localhost:${port}${path}" 90 "$pid" || rc=$?
  if (( rc != 0 )); then
    log_tail "$LOGS/$name.log"
    if [[ "$mode" == "host" ]]; then
      warn "  to run it in a container instead: AIRLOCK_${svc}_MODE=container in deploy/airlock.env"
    fi
    # A native service that died leaves a pid file naming a process that is
    # gone, which is worse than no pid file: stop-all.sh would read it and
    # report that it stopped something.
    [[ -n "$pid" ]] && ! kill -0 "$pid" 2>/dev/null && rm -f "$LOGS/$name.pid"
  fi
  return 0
}

echo "── backend-deepface ─────────────────────────────────────────────────────"
# --no-deepface is kept for anyone who already uses it; it means mode off.
if [[ -n "$SKIP_DEEPFACE" ]]; then
  AIRLOCK_DEEPFACE_MODE=off
fi
start_accelerated DEEPFACE backend-deepface 5001 /api/health

echo
echo "── backend-transcode ────────────────────────────────────────────────────"
start_accelerated TRANSCODE backend-transcode 5004 /health

echo
echo "── Gateway ──────────────────────────────────────────────────────────────"
# First: every health check below addresses services through it. It is also the
# only pod that publishes a port.
if [[ -x "$SYSTEMS/gateway/start-podman.sh" ]]; then
  # Synchronous: with AIRLOCK_DETACH=1 the script starts the container and
  # returns, so there is nothing to background and a failure is visible here
  # rather than discovered later as a missing service.
  announce_build gateway
  _gw_t0=$SECONDS
  ( cd "$SYSTEMS/gateway" && ./start-podman.sh $(build_flag gateway) > "$LOGS/gateway.log" 2>&1 ) \
    || warn "gateway script exited non-zero - see $LOGS/gateway.log"
  report_build gateway $(( SECONDS - _gw_t0 ))
  if wait_for "gateway" "http://localhost/gateway-health" 60; then
    GATEWAY_UP=1
  else
    warn "gateway not answering"
    # Name the most likely cause rather than making the user read the log to
    # find it: rootless podman refuses privileged ports by default.
    # Podman words this differently across versions - 3.4 says "rootlessport"
    # and a bare "permission denied", 4.x says "cannot expose privileged port".
    # Matching only the newest wording meant the most common cause of a failed
    # gateway on Linux was reported as "see the log".
    # The certificate first: it is the failure a freshly cloned repository
    # hits every time, because certs/ is not in git. The gateway exits before
    # binding anything, so it looks identical to a port problem from out here.
    if grep -qi "No TLS certificate" "$LOGS/gateway.log" 2>/dev/null; then
      warn "  the gateway has no TLS certificate (certs/ is not in git)"
      warn "  fix with:  ./deploy/deploy.sh"
      warn "  or:        cd systems/gateway && ./make-certs.sh"
    elif grep -qiE "privileged port|rootlessport|permission denied|bind: address" \
            "$LOGS/gateway.log" 2>/dev/null; then
      warn "  rootless podman cannot bind port 80"
      warn "  fix with:  systems/gateway/enable-port-80.sh"
      warn "  or use 8080:  systems/gateway/enable-port-80.sh --port-8080"
    else
      warn "  see $LOGS/gateway.log"
    fi
  fi
  if [[ -x "$SYSTEMS/gateway/make-hosts.sh" ]] \
     && "$SYSTEMS/gateway/make-hosts.sh" --check >/dev/null 2>&1; then
    NAMES_RESOLVE=1
  else
    warn "some *.airlock.lan names do not resolve"
    warn "  fix with:  sudo systems/gateway/make-hosts.sh --install"
  fi
else
  warn "systems/gateway/start-podman.sh not found - services will only be reachable by port"
fi

# ── Which addresses do the health checks use? ────────────────────────────────
# By name through the gateway when that works, because it is the path a user
# actually takes and checking localhost:5000 would pass while the real one was
# broken.
#
# But ONLY when it works. Health-checking a name that does not resolve does not
# fail fast - each check sits out its full 90 s timeout, so six services turn
# into seven minutes of silence that looks exactly like a hang. The ports still
# work regardless; this is a diagnostic fallback, not a second addressing scheme.
if [[ "$GATEWAY_UP" == "1" && "$NAMES_RESOLVE" == "1" ]]; then
  ok "health checks will use the service names"
  URL_GEOLOC="http://geoloc.airlock.lan/health"
  URL_PETS="http://pets.airlock.lan/health"
  URL_VISION="http://vision.airlock.lan/health"
  URL_BACKEND="http://backend.airlock.lan/api/catalogs"
  URL_GALLERY="http://gallery.airlock.lan/albums.json"
  URL_FRONTEND="http://airlock.lan/"
else
  warn "falling back to port-based health checks for this run"
  warn "  (the services themselves are unaffected - only these checks)"
  URL_GEOLOC="http://localhost:5002/health"
  URL_PETS="http://localhost:5006/health"
  URL_VISION="http://localhost:5003/health"
  URL_BACKEND="http://localhost:5000/api/catalogs"
  URL_GALLERY="http://localhost:3001/albums.json"
  URL_FRONTEND="http://localhost:3000/"
fi

echo
echo "── Pods ─────────────────────────────────────────────────────────────────"
# Has any catalog switched pets on?
#
# The property lives in each catalog's own workflow_results.db, so this asks
# every one of them rather than a single global setting - the switch is
# per-catalog because the answer differs per household, and one deployment can
# hold a catalog with pets and one without.
#
# Uses python3 rather than sqlite3: the latter is not installed on every host
# (homeserver has no sqlite3 CLI), while python3 is a hard requirement of the
# backend anyway. A catalog with no property, or no database yet, is "off".
airlock_pets_enabled() {
  # $ROOT is this script's own; SCRIPT_DIR does not exist here, and under
  # `set -u` a fallback naming it would be a trap waiting for the day
  # AIRLOCK_ROOT is empty.
  local root="${AIRLOCK_ROOT:-$ROOT}/catalogs"
  [[ -d "$root" ]] || return 1
  "${AIRLOCK_PYTHON:-python3}" - "$root" <<'PY' 2>/dev/null
import glob, os, sqlite3, sys
root = sys.argv[1]
for db in glob.glob(os.path.join(root, '*', 'workflow_results.db')):
    try:
        c = sqlite3.connect(f'file:{db}?mode=ro', uri=True)
        row = c.execute("SELECT value FROM catalog_properties "
                        " WHERE key = 'pets_enabled'").fetchone()
        c.close()
        if row and str(row[0]) == '1':
            sys.exit(0)
    except sqlite3.Error:
        continue
sys.exit(1)
PY
}

start_pod() { # start_pod <dir> <name> <health-url>
  local dir="$SYSTEMS/$1" name="$2" url="${3:-}"
  if [[ ! -x "$dir/start-podman.sh" ]]; then warn "$name: no start-podman.sh, skipped"; return; fi
  announce_build "$name"
  local t0=$SECONDS
  if ! ( cd "$dir" && ./start-podman.sh $(build_flag "$name") > "$LOGS/$name.log" 2>&1 ); then
    warn "$name: start-podman.sh exited non-zero - see $LOGS/$name.log"
    # Named here rather than left in the log: a build failure and a container
    # that will not start look identical from the outside.
    tail -3 "$LOGS/$name.log" 2>/dev/null | sed 's/^/      /'
    return
  fi
  report_build "$name" $(( SECONDS - t0 ))
  if [[ -n "$url" ]]; then wait_for "$name" "$url" 90 || true; else ok "$name launched"; fi
}

start_pod backend-geoloc  backend-geoloc  "$URL_GEOLOC"
# Only when some catalog has pets switched on. The models are optional by
# design: a household with no pets should not run them, and "off" has to cost
# nothing rather than nearly nothing - no model loaded, no service contacted,
# no container. See documentation/specs/pet-recognition-spec.md section 10.
if airlock_pets_enabled; then
  start_pod backend-pets  backend-pets  "$URL_PETS"
else
  echo "── backend-pets ─────────────────────────────────────────────────────────"
  echo "  not started: no catalog has pets enabled"
  not_built backend-pets "not started, so not built - no catalog has pets
        enabled. Turn Pets on for a catalog (it is on by default for a new one)
        and run this again."
fi
start_pod backend-vision  backend-vision  "$URL_VISION"
start_pod backend-albums  backend-albums  ""
start_pod backend         backend         "$URL_BACKEND"
start_pod album-gallery   album-gallery   "$URL_GALLERY"
start_pod frontend        frontend        "$URL_FRONTEND"

echo
echo "── Ready ────────────────────────────────────────────────────────────────"
# Repeated here because the per-service lines are thirty lines up by now, behind
# health checks and mode decisions. After a --build the question a person
# actually has is "did the thing I changed get rebuilt", and scrolling back to
# find out is a poor way to answer it.
if [[ -n "$REBUILT" ]]; then
  ok "images rebuilt this run:${REBUILT}"
fi
# Anything asked for that did NOT build. Not "already up to date" - --build
# forces a build, so that is not the reason. The real ones are that the service
# runs natively on this machine and has no image (deepface and transcode on a
# Mac), that it was skipped, or that it failed. Named rather than left silent,
# because "I passed --build and my change is not there" needs an answer.
_asked="$BUILD_ONLY"
[[ -n "$BUILD_ALL" ]] && _asked="$BUILDABLE"
_skipped=""
for _svc in $_asked; do
  [[ " $REBUILT " == *" $_svc "* ]] || _skipped="$_skipped $_svc"
done
if [[ -n "$_skipped" ]]; then
  warn "asked to rebuild, did not build:${_skipped}"
  # The reasons that are KNOWN, first and by name.
  if [[ -n "$NOT_BUILT" ]]; then
    printf '%s\n' "$NOT_BUILT"
  fi
  # ...and the guess only for those nothing recorded a reason for.
  _unexplained=""
  for _svc in $_skipped; do
    case "$NOT_BUILT" in
      *"      $_svc: "*) ;;
      *) _unexplained="$_unexplained $_svc" ;;
    esac
  done
  if [[ -n "$_unexplained" ]]; then
    echo "     ${_unexplained# }: a service running natively here has no image to"
    echo "        build; otherwise see logs/<service>.log"
  fi
fi
if [[ "$GATEWAY_UP" == "1" && "$NAMES_RESOLVE" == "1" ]]; then
  echo "  UI              http://airlock.lan"
  echo "  Album gallery   http://gallery.airlock.lan"
  echo "  Backend API     http://airlock.lan/api   (also http://backend.airlock.lan/api)"
  echo "  Gateway health  http://airlock.lan/gateway-health"
else
  echo "  Backend API     http://localhost:5000/api"
  echo "  Album gallery   http://localhost:3001"
  echo
  # Said plainly because the opposite was implied here for a long time: the
  # frontend's nginx serves the SPA and nothing else, and the browser asks the
  # SAME ORIGIN for /api. On :3000 that origin is nginx, which answers with
  # index.html instead of JSON - so the UI loads and then behaves as though
  # nobody is signed in. The gateway is what puts the UI and the API on one
  # origin; without it the UI is not usable, however healthy the services are.
  echo "  The UI needs the gateway. http://localhost:3000 loads the page but"
  echo "  cannot reach /api, so it will show a sign-in form that cannot work."
  echo
  echo "  To use the names:"
  [[ "$NAMES_RESOLVE" == "1" ]] || echo "    sudo systems/gateway/make-hosts.sh --install"
  [[ "$GATEWAY_UP"    == "1" ]] || echo "    check $LOGS/gateway.log, then: cd systems/gateway && ./start-podman.sh"
fi
echo "  Logs            $LOGS/"
echo
echo "  Stop everything with ./stop-all.sh"

# Explicit exit inside the group: once the brace group finishes, bash would
# otherwise carry on reading the file from its old byte offset, which after an
# edit lands mid-line and prints one last spurious error. Exiting here means it
# never looks.
exit 0

}   # end read-the-whole-file guard
