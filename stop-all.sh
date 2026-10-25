#!/usr/bin/env bash
# Stop the whole airlock stack: every container, plus any service running natively
# (deepface, transcode - see accelerated-services-spec.md).

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
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
SYSTEMS="$ROOT/systems"
LOGS="$ROOT/logs"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }

echo "── Pods ─────────────────────────────────────────────────────────────────"
for s in frontend album-gallery backend backend-albums backend-vision backend-pets \
         backend-geoloc backend-deepface backend-transcode gateway; do
  yaml="$SYSTEMS/$s/${s}-pod.yaml"
  # The start-podman.sh scripts use `podman run`, not `podman play kube`, so the
  # manifest teardown usually has nothing to do and still reports success. Try
  # both, and report on what actually stopped.
  stopped=""
  if [[ -f "$yaml" ]]; then
    ( cd "$SYSTEMS/$s" && podman play kube --down "${s}-pod.yaml" >/dev/null 2>&1 ) && stopped="pod"
  fi
  # The gateway's container is airlock-gateway; the backend's, workflow-backend.
  for c in "$s" "workflow-$s" "airlock-$s"; do
    podman rm -f "$c" >/dev/null 2>&1 && stopped="${stopped:+$stopped, }container $c"
  done
  [[ -n "$stopped" ]] && ok "$s stopped ($stopped)" || warn "$s was not running"
done

echo
echo "── Native services (accelerated-services-spec.md) ───────────────────────"
# Started by start-all.sh in 'host' mode: a pid file per service. The scripts
# exec gunicorn, which forks workers - so the children go first.
stop_native() { # stop_native <name> <fallback pkill pattern>
  local name="$1" pattern="$2" pidfile="$LOGS/$1.pid"
  if [[ -f "$pidfile" ]] && kill -0 "$(cat "$pidfile")" 2>/dev/null; then
    pkill -TERM -P "$(cat "$pidfile")" 2>/dev/null
    kill -TERM "$(cat "$pidfile")" 2>/dev/null
    rm -f "$pidfile"
    ok "$name stopped"
  elif pkill -f "$pattern" 2>/dev/null; then
    rm -f "$pidfile"
    ok "$name stopped (by command-line match)"
  else
    rm -f "$pidfile"
    echo "  - $name was not running natively"
  fi
}
# deepface: matched by its gunicorn bind to port 5001 - there is no name in the
# process's command line to match on. transcode: by its app directory.
stop_native backend-deepface "gunicorn.*:5001.*app:app|gunicorn.*app:app.*5001"
stop_native backend-transcode "gunicorn.*backend-transcode|backend-transcode/venv/bin/python.*gunicorn|gunicorn.*:5004.*app:app"
echo
ok "All stopped."

# Explicit exit inside the group: once the brace group finishes, bash would
# otherwise carry on reading the file from its old byte offset, which after an
# edit lands mid-line and prints one last spurious error. Exiting here means it
# never looks.
exit 0

}   # end read-the-whole-file guard
