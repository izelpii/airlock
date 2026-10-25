#!/usr/bin/env bash
# Build and verify the virtualenv a native service runs in.
#
# SOURCED, NOT RUN - the only file in tools/ that is. It defines functions and
# does nothing on its own, so executing it has no effect. Its callers are
# systems/backend-deepface/start-host.sh, systems/backend-transcode/start-host.sh,
# deploy/deploy.sh and deploy/status.sh, and all four need
# deploy/airlock-env.sh sourced FIRST for airlock_python and
# AIRLOCK_PYTHON_MIN:
#
#   source "$AIRLOCK_ROOT/deploy/airlock-env.sh"   # first: airlock_python
#   source "$AIRLOCK_ROOT/tools/airlock-venv.sh"
#   airlock_venv_ensure "$VENV" requirements.txt gunicorn || exit 1
#
# ── Why this is not three lines in each start-host.sh ─────────────────────────
# It used to be, and the three lines were:
#
#   [[ -d "$VENV" ]] || setup_venv
#   source "$VENV/bin/activate"
#   exec gunicorn ...
#
# "The directory exists" is not "the dependencies are installed", and the gap
# between those two statements cost a working deployment. On a new Mac, bare
# `python3` is Apple's 3.9, `python3 -m venv` succeeded with it, `pip install
# -r requirements.txt` then died with ResolutionImpossible (current Flask
# publishes nothing for 3.9), `set -e` took the script down - and left behind a
# venv/ directory holding nothing but pip. Every start after that saw the
# directory, skipped setup, and failed at `exec gunicorn` with
# "gunicorn: not found": a permanent failure whose actual cause had scrolled
# away, in a log that was truncated on each restart.
#
# So readiness is tested against the three things that can each be wrong
# independently:
#
#   1. the interpreter    a venv built with a Python below the floor can never
#                         install this requirement set, and no amount of pip
#                         will fix it - it has to be thrown away
#   2. the entry point    bin/gunicorn, the thing the service actually execs
#   3. the requirements   a stamp of requirements.txt, so editing that file
#                         reinstalls instead of running yesterday's packages
#
# Each failure names its own fix, and none of them leaves behind a venv that
# will be mistaken for a finished one: the stamp is written LAST, only after
# pip has succeeded.

# shellcheck disable=SC2034
AIRLOCK_VENV_SH=1

_sha256_file() {
  if   command -v shasum   >/dev/null 2>&1; then shasum -a 256 "$1" | awk '{print $1}'
  elif command -v sha256sum >/dev/null 2>&1; then sha256sum "$1" | awk '{print $1}'
  else echo "no-sha-tool"
  fi
}

# Empty, not failing, when the venv has no working interpreter: the callers
# below run under `set -e` and read emptiness as "rebuild this".
_venv_python_version() { "$1/bin/python3" -c 'import sys; print("%d.%d" % sys.version_info[:2])' 2>/dev/null || true; }

_venv_stamp_want() {  # _venv_stamp_want <venv> <requirements>
  echo "python=$(_venv_python_version "$1") reqs=$(_sha256_file "$2")"
}

# airlock_venv_state <venv> <requirements> <entry> -> "<state> <detail>"
# ready | missing | wrong-python | incomplete | stale
airlock_venv_state() {
  local venv="$1" reqs="$2" entry="$3" ver
  [[ -d "$venv" ]] || { echo "missing no venv yet"; return; }
  ver="$(_venv_python_version "$venv" || true)"
  if [[ -z "$ver" ]]; then
    echo "wrong-python its interpreter does not run"; return
  fi
  if ! _version_at_least "$ver" "$AIRLOCK_PYTHON_MIN"; then
    echo "wrong-python built with Python $ver, below the $AIRLOCK_PYTHON_MIN floor"; return
  fi
  if ! _python_in_range "$ver"; then
    echo "wrong-python built with Python $ver, above the $AIRLOCK_PYTHON_MAX ceiling"; return
  fi
  if [[ ! -x "$venv/bin/$entry" ]]; then
    echo "incomplete $entry is not installed in it"; return
  fi
  if [[ ! -f "$venv/.airlock-stamp" ]]; then
    echo "stale no record of which requirements were installed"; return
  fi
  if [[ "$(cat "$venv/.airlock-stamp")" != "$(_venv_stamp_want "$venv" "$reqs")" ]]; then
    echo "stale $(basename "$reqs") has changed since it was installed"; return
  fi
  echo "ready Python $ver"
}

# airlock_venv_ensure <venv> <requirements> <entry> [--setup]
# Returns 0 when the venv is usable, 1 with a reason on stdout when it is not.
# --setup forces a reinstall even when the state is ready.
airlock_venv_ensure() {
  local venv="$1" reqs="$2" entry="$3" force="${4:-}" state detail py ver

  state="$(airlock_venv_state "$venv" "$reqs" "$entry")"
  detail="${state#* }"; state="${state%% *}"
  if [[ "$state" == "ready" && "$force" != "--setup" ]]; then
    echo "==> venv ready ($detail)"
    return 0
  fi
  echo "==> venv needs work: $detail"

  py="$(airlock_python)"
  if [[ -z "$py" ]]; then
    echo "!! $(airlock_python_status)" >&2
    echo "   The containers are built FROM python:3.12-slim, so that is the" >&2
    echo "   version this requirement set is tested against." >&2
    return 1
  fi
  ver="$("$py" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"

  # A venv on the wrong interpreter is REPLACED, not repaired. Nothing pip can
  # do makes a 3.9 venv able to install a package that publishes no 3.9 wheel,
  # and leaving it in place is what made the original failure permanent.
  if [[ "$state" == "wrong-python" ]]; then
    echo "==> replacing the venv at $venv ($detail)"
    rm -rf "$venv"
  fi

  if [[ ! -d "$venv" ]]; then
    echo "==> creating the venv with $py ($ver)"
    if ! "$py" -m venv "$venv"; then
      # Debian and Ubuntu split venv out of the interpreter package, and the
      # error python prints for that ("ensurepip is not available") names
      # neither the package nor the distribution that needs it.
      echo "!! could not create a venv with $py" >&2
      echo "   On Debian, Ubuntu and Raspberry Pi OS the venv module ships separately:" >&2
      echo "     sudo apt install python${ver}-venv" >&2
      rm -rf "$venv"
      return 1
    fi
  fi

  echo "==> installing $reqs"
  # Not `set -e`: a pip failure here has to be REPORTED, with the fix, and
  # without a stamp - so the next start tries again instead of exec'ing a
  # binary that was never installed.
  if ! "$venv/bin/python3" -m pip install --upgrade pip >/dev/null 2>&1; then
    echo "!! could not upgrade pip in $venv (continuing)" >&2
  fi
  if ! "$venv/bin/python3" -m pip install -r "$reqs"; then
    echo >&2
    echo "!! installing $reqs failed, so this service cannot start natively." >&2
    echo "   Nothing was stamped, so the next start will try again." >&2
    echo "   Interpreter: $py ($ver), allowed $AIRLOCK_PYTHON_MIN to $AIRLOCK_PYTHON_MAX" >&2
    echo "   To run this service in a container instead - which carries its own" >&2
    echo "   Python and needs nothing from this host - set in deploy/airlock.env:" >&2
    echo "     AIRLOCK_${AIRLOCK_SERVICE:-SERVICE}_MODE=container" >&2
    return 1
  fi
  if [[ ! -x "$venv/bin/$entry" ]]; then
    echo "!! $reqs installed but $entry is still missing from $venv/bin" >&2
    echo "   Add it to $reqs - the service execs it by name." >&2
    return 1
  fi

  # LAST, and only now: everything above succeeded, so this venv can be
  # trusted on the next start.
  _venv_stamp_want "$venv" "$reqs" > "$venv/.airlock-stamp"
  echo "==> venv ready (Python $(_venv_python_version "$venv"))"
  return 0
}
