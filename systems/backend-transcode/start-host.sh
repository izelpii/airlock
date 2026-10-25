#!/usr/bin/env bash
# Run backend-transcode DIRECTLY ON THE HOST (not in a container).
# documentation/specs/accelerated-services-spec.md, section 3.1.
#
# On a Mac this is the only way to VideoToolbox: podman runs containers in a
# Linux VM with no access to Apple's media engines. The service then reads and
# writes the catalogs at CATALOGS_HOST_DIR, the same folders the containers
# mount at /catalogs - including any originals folder that is a bind mount from
# another disk, which the host sees too.
#
#   ./start-host.sh            # start (creates the venv on first run)
#   ./start-host.sh --setup    # create/refresh the venv, then start
#   ./start-host.sh --check    # report ffmpeg and the usable encoders, then exit
set -euo pipefail
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../deploy/airlock-env.sh"
# shellcheck disable=SC1091
source "$SCRIPT_DIR/../../tools/airlock-venv.sh"
VENV="$SCRIPT_DIR/venv"
PORT="${PORT:-5004}"
# Named so a failure can tell the operator which mode variable to set instead.
AIRLOCK_SERVICE=TRANSCODE

# ffmpeg is the whole service - it is not a Python dependency, it is a host
# program this shells out to, and the container image apt-installs its own.
# Checked here AND in airlock_service_mode, which is what now stops auto from
# choosing host mode on a machine that does not have it: the mode falls back to
# container, and this message is for someone who asked for host explicitly.
if ! command -v ffmpeg >/dev/null || ! command -v ffprobe >/dev/null; then
  _ff="$(airlock_ffmpeg_status)"
  echo "!! ${_ff#no: }" >&2
  echo "   Or run this service in a container, which carries its own ffmpeg," >&2
  echo "   by setting in deploy/airlock.env:" >&2
  echo "     AIRLOCK_TRANSCODE_MODE=container" >&2
  exit 1
fi

setup_venv() {  # setup_venv [--setup]
  airlock_venv_ensure "$VENV" "$SCRIPT_DIR/requirements.txt" gunicorn "${1:-}"
}

check() {
  # The venv's interpreter: video_proxy imports nothing outside the standard
  # library, but using the venv keeps --check honest about what will run.
  "${VENV}/bin/python3" - <<'PY'
import sys, os, glob
sys.path.insert(0, os.path.join(os.getcwd(), '..', 'backend', 'workflows', 'lib'))
import video_proxy as vp
print('  ffmpeg + ffprobe :', 'found' if vp.available() else 'MISSING')
hw = vp.usable_encoders()
print('  hardware encoders:', ', '.join(hw) if hw else 'none (software only)')
# Named, because "none" has several very different causes: no device, a device
# the driver cannot encode with (nouveau), or no permission to open it.
if 'h264_vaapi' in hw:
    print('  vaapi device     :', vp.vaapi_device())
elif glob.glob('/dev/dri/renderD*'):
    print('  render nodes     :', ', '.join(glob.glob('/dev/dri/renderD*')),
          '- present but no usable VA-API encoder (open driver, or no permission)')
PY
}

case "${1:-}" in
  --setup) setup_venv --setup || exit 1 ;;
  --check)
    echo "  platform         : $(airlock_platform)"
    echo "  venv             : $(airlock_venv_state "$VENV" "$SCRIPT_DIR/requirements.txt" gunicorn)"
    echo "  host python      : $(airlock_python_status)"
    [[ -x "$VENV/bin/python3" ]] || { echo "  Run ./start-host.sh --setup first."; exit 1; }
    check; exit 0 ;;
esac

# Every start, not only the first: a venv is ready when its entry point is
# installed and requirements.txt has not changed since, never merely because a
# directory of that name exists.
setup_venv || exit 1

# shellcheck disable=SC1091
source "$VENV/bin/activate"

export CATALOGS_ROOT="$CATALOGS_HOST_DIR"
export TRANSCODE_MODE=host
BINDS=()
for addr in $(airlock_native_binds); do BINDS+=(--bind "${addr}:${PORT}"); done

echo "==> backend-transcode (host mode)"
echo "    catalogs : $CATALOGS_ROOT"
echo "    listening: $(airlock_native_binds) port $PORT"
check
exec gunicorn --worker-class gthread --workers=1 --threads=8 --timeout=7200 \
     "${BINDS[@]}" app:app
