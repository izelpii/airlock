#!/usr/bin/env bash
# Is airlock up, and what is it doing?
#
#   ./deploy/status.sh            # run on the machine that hosts the stack
#
# Changes nothing. Every check is a read. Exits 1 when a service is down or a
# catalog folder is unusable, so it can also be used from cron or a script.
#
# Four parts:
#   1. Services   - each container: running, for how long, and whether it
#                   ANSWERS. Running is not enough: a container can be up while
#                   the app inside it crashed or is still loading.
#   2. Detached   - a container started in the foreground of a terminal dies
#                   with that terminal (and --rm then deletes it). That is how
#                   oldserver lost its backend mid-import on 2026-10-02.
#   3. Storage    - free space, and catalog folders whose mount points at a
#                   deleted directory (they list as empty and refuse writes).
#   4. Imports    - what the backend is running right now, and how far it got.

{   # read-the-whole-file guard; see start-all.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/airlock-env.sh" >/dev/null 2>&1 || true
# shellcheck disable=SC1091
source "$HERE/../tools/airlock-venv.sh" >/dev/null 2>&1 || true

TLS_PORT="${TLS_PORT:-443}"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; FAILED=1; }
info() { printf "    %s\n" "$1"; }
FAILED=0

if ! command -v podman >/dev/null 2>&1; then
    echo "podman is not installed here - run this on the machine that hosts airlock."
    exit 1
fi

# ── 1. Services ──────────────────────────────────────────────────────────────
# name | URL that proves it answers | what it is | accelerated service key
# (accelerated-services-spec.md: such a service may run natively, in a
# container, or in a container with the GPU - AIRLOCK_<KEY>_MODE decides)
# Any HTTP status counts as "answering" (the backend says 401 to a stranger,
# geoloc 400 to a request without coordinates); only no answer at all is down.
SERVICES=(
    "airlock-gateway|https://localhost:${TLS_PORT}/gateway-health|gateway (HTTPS entry point)"
    "workflow-backend|http://localhost:5000/api/auth/setup-required|backend (API, workflows, imports)"
    "workflow-frontend|http://localhost:3000/|frontend (web app)"
    "backend-deepface|http://localhost:5001/api/health|faces|DEEPFACE"
    "backend-transcode|http://localhost:5004/health|video previews|TRANSCODE"
    "backend-geoloc|http://localhost:5002/poi|places"
    "backend-vision|http://localhost:5003/health|vision"
    "backend-albums|http://localhost:5005/health|albums"
    "album-gallery|http://localhost:3001/health|published galleries"
)

# Why a native service is not answering, from what can be read on this machine:
# a dead pid file, a venv that was never finished, a missing host tool - then
# the end of its log, which is where anything else will be.
native_diagnosis() {  # native_diagnosis <name> <SERVICE-KEY>
    # Defaulted, because sourcing airlock-env.sh above is allowed to fail and
    # `set -u` would otherwise turn a missing variable into a crash in the one
    # script whose job is to report problems.
    local name="$1" key="$2" root="${AIRLOCK_ROOT:-$HERE/..}"
    local pidfile="$root/logs/$name.pid"
    local log="$root/logs/$name.log" dir="$root/systems/$name"
    local ready state pid

    if [[ -f "$pidfile" ]]; then
        pid="$(cat "$pidfile" 2>/dev/null)"
        if [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null; then
            info "pid $pid is alive but not answering yet - it may still be loading models"
        else
            info "pid ${pid:-?} in logs/$name.pid is gone: it started and exited"
        fi
    else
        info "no logs/$name.pid - it has not been started since the last boot"
    fi

    # The host half. declare -F, because status.sh must keep working even when
    # sourcing airlock-env.sh failed (an old checkout, a missing file).
    if declare -F airlock_host_ready >/dev/null 2>&1 && [[ -n "$key" ]]; then
        ready="$(airlock_host_ready "$key")"
        if [[ "$ready" != ok* ]]; then
            info "host cannot run it natively: ${ready#no: }"
            info "or run it in a container: AIRLOCK_${key}_MODE=container in deploy/airlock.env"
        fi
    fi
    if declare -F airlock_venv_state >/dev/null 2>&1 && [[ -f "$dir/requirements.txt" ]]; then
        state="$(airlock_venv_state "$dir/venv" "$dir/requirements.txt" gunicorn)"
        [[ "$state" == ready* ]] || info "venv: ${state#* } - fix with ./deploy/deploy.sh"
    fi

    if [[ -s "$log" ]]; then
        info "last 3 lines of logs/$name.log:"
        sed -e 's/^/      /' <(tail -n 3 "$log")
    fi
}

echo
echo "Services"
DOWN=()
FOREGROUND=()
for entry in "${SERVICES[@]}"; do
    IFS='|' read -r name url what key <<<"$entry"
    if [[ -n "${key:-}" ]]; then
        decision="$(airlock_service_mode "$key" 2>/dev/null)"
        mode="${decision%% *}"
        detail=""
        if [[ "$name" == "backend-transcode" ]]; then
            enc="$(curl -s --max-time 3 "$url" 2>/dev/null | python3 -c \
                'import json,sys; print(", ".join(json.load(sys.stdin).get("encoders", [])))' 2>/dev/null)"
            [[ -n "$enc" ]] && detail=": $enc"
        fi
        if [[ "$mode" == "off" ]]; then
            info "$name - off (AIRLOCK_${key}_MODE=off) (${what})"
            continue
        fi
        if [[ "$mode" == "host" ]]; then
            code="$(curl -sk -o /dev/null --max-time 5 -w '%{http_code}' "$url" 2>/dev/null)"
            if [[ -z "$code" || "$code" == "000" ]]; then
                bad "$name - native, not answering on ${url} (${what})"
                # NOT "start with ./start-all.sh". That was the advice before,
                # and it was the one thing that could not help: the service had
                # an unmet host prerequisite, so starting it again produced the
                # same failure and overwrote the log that explained the first
                # one. What follows says WHY it cannot start, when that is
                # knowable from here.
                native_diagnosis "$name" "$key"
                DOWN+=("$name")
            else
                ok "$name - up, native${detail} (${what})"
            fi
            continue
        fi
        what="${what}, ${mode}${detail}"
    fi
    state="$(podman inspect --format '{{.State.Status}}|{{.State.StartedAt}}' "$name" 2>/dev/null)"
    if [[ -z "$state" ]]; then
        bad "$name - not there (${what})"
        DOWN+=("$name")
        continue
    fi
    status="${state%%|*}"
    if [[ "$status" != "running" ]]; then
        bad "$name - ${status} (${what})"
        DOWN+=("$name")
        continue
    fi
    since="$(podman ps --filter "name=^${name}\$" --format '{{.RunningFor}}' 2>/dev/null)"
    code="$(curl -sk -o /dev/null --max-time 5 -w '%{http_code}' "$url" 2>/dev/null)"
    if [[ -z "$code" || "$code" == "000" ]]; then
        bad "$name - running ${since:-}, but not answering on ${url} (${what})"
        info "podman logs --tail 50 $name"
        DOWN+=("$name")
    elif [[ "$code" =~ ^5 ]]; then
        warn "$name - running ${since:-}, answering with HTTP ${code} (${what})"
    else
        ok "$name - up ${since:-} (${what})"
    fi
    # The container's conmon is a child of whatever started it. Detached, that
    # is systemd (or init); in the foreground, it is a shell - and when that
    # shell's terminal closes, the container goes with it.
    pid="$(podman inspect --format '{{.State.ConmonPid}}' "$name" 2>/dev/null)"
    if [[ -n "$pid" && "$pid" != "0" ]]; then
        tty="$(ps -o tty= -p "$(ps -o ppid= -p "$pid" 2>/dev/null | tr -d ' ')" 2>/dev/null | tr -d ' ')"
        if [[ -n "$tty" && "$tty" != "?" && "$tty" != "??" ]]; then
            FOREGROUND+=("$name ($tty)")
        fi
    fi
done

if ((${#FOREGROUND[@]})); then
    echo
    echo "Tied to a terminal"
    for f in "${FOREGROUND[@]}"; do
        warn "$f - stops when that terminal closes, unless it is inside screen/tmux"
    done
    info "Restart detached: AIRLOCK_DETACH=1 ./start-podman.sh, or ./start-all.sh"
fi

if ((${#DOWN[@]})); then
    info ""
    info "Start everything:   cd ${AIRLOCK_ROOT:-/opt/airlock} && ./start-all.sh"
    info "Or one service:     cd systems/<service> && AIRLOCK_DETACH=1 ./start-podman.sh"
fi

# ── 3. Storage ───────────────────────────────────────────────────────────────
echo
echo "Storage"
CATALOGS="${CATALOGS_HOST_DIR:-${AIRLOCK_ROOT:-/opt/airlock}/catalogs}"
show_df() {
    local label="$1" path="$2" line pct
    [[ -e "$path" ]] || return
    line="$(df -h "$path" 2>/dev/null | tail -1)"
    pct="$(awk '{print $5}' <<<"$line" | tr -d '%')"
    msg="$(awk '{printf "%s free of %s (%s used)", $4, $2, $5}' <<<"$line")"
    if [[ "$pct" =~ ^[0-9]+$ ]] && ((pct >= 95)); then
        bad "$label: $msg - $path"
    elif [[ "$pct" =~ ^[0-9]+$ ]] && ((pct >= 85)); then
        warn "$label: $msg - $path"
    else
        ok "$label: $msg - $path"
    fi
}
show_df "system disk" /
show_df "catalogs" "$CATALOGS"
[[ -n "${STORAGE_ROOT_HOST:-}" ]] && show_df "storage root" "$STORAGE_ROOT_HOST"
seen=" $(df -P "$CATALOGS" 2>/dev/null | awk 'NR==2{print $1}') "
for d in "$CATALOGS"/*/{Images,Videos,Others}; do
    [[ -d "$d" ]] || continue
    dev="$(df -P "$d" 2>/dev/null | awk 'NR==2{print $1}')"
    [[ "$seen" == *" $dev "* ]] && continue
    seen+="$dev "
    show_df "originals" "$d"
done

# A bind mount whose source directory was deleted still looks like a folder,
# but every write into it fails with "No such file or directory".
if command -v findmnt >/dev/null 2>&1; then
    stale="$(findmnt -rn -o TARGET,SOURCE | awk -v c="$CATALOGS" 'index($0, c) && /\/\/deleted/ {print $1}')"
    if [[ -n "$stale" ]]; then
        while read -r m; do
            bad "$m is mounted from a DELETED directory - nothing can be written there"
        done <<<"$stale"
        info "Fix: sudo umount <folder>; recreate its source; sudo mount -a; restart the backend"
    else
        ok "catalog folders are mounted from directories that exist"
    fi
fi

# ── 4. Imports and workflows ─────────────────────────────────────────────────
# Read inside the container, never from the host: host and container sharing a
# SQLite file can corrupt reads (see the memory note on virtiofs/WAL).
echo
echo "Running workflows"
if [[ " ${DOWN[*]-} " == *" workflow-backend "* ]]; then
    warn "backend is down - nothing can be running"
else
    podman exec -i workflow-backend python3 - <<'PY' 2>/dev/null || warn "could not read the catalogs"
import glob, os, sqlite3
from datetime import datetime, timezone

def ago(ts):
    try:
        t = datetime.fromisoformat(str(ts).replace('Z', '')).replace(tzinfo=timezone.utc)
        s = int((datetime.now(timezone.utc) - t).total_seconds())
        return f'{s // 3600}h {s % 3600 // 60:02d}m' if s >= 3600 else f'{s // 60}m'
    except Exception:
        return '?'

found = False
for db in sorted(glob.glob('/catalogs/*/workflow_results.db')):
    cat = os.path.basename(os.path.dirname(db))
    try:
        c = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=10)
        c.row_factory = sqlite3.Row
        runs = c.execute(
            "SELECT e.id, e.started_at, w.name FROM workflow_executions e "
            "LEFT JOIN workflows w ON w.id = e.workflow_id "
            "WHERE e.status = 'running' ORDER BY e.id").fetchall()
        queued = c.execute("SELECT COUNT(*) FROM workflow_queue "
                           "WHERE status IN ('queued', 'blocked')").fetchone()[0]
    except sqlite3.Error as exc:
        print(f'  \033[33m!\033[0m {cat}: could not read ({exc})')
        continue
    for r in runs:
        found = True
        line = f"{cat}: {r['name'] or 'workflow'} (execution {r['id']}), running {ago(r['started_at'])}"
        try:
            ph = {p['phase']: p for p in c.execute(
                'SELECT phase, status, total, done, skipped, failed FROM import_phases '
                'WHERE execution_id = ?', (r['id'],))}
        except sqlite3.Error:
            ph = {}
        print(f'  \033[32m▶\033[0m {line}')
        if ph:
            place, scan = ph.get('place'), ph.get('scan')
            total = (place or scan or {})['total'] if (place or scan) else None
            done = place['done'] if place else 0
            failed = sum((p['failed'] or 0) for p in ph.values())
            if total:
                print(f'    {done:,} of {total:,} files placed ({100 * done / total:.1f}%)'
                      + (f', \033[31m{failed:,} failed\033[0m' if failed else ', none failed'))
            for p in ('scan', 'probe', 'resolve', 'place', 'render'):
                if p in ph and ph[p]['status'] == 'running':
                    q = ph[p]
                    print(f"    {p}: {q['done'] or 0:,}/{q['total'] or 0:,}")
    if queued:
        found = True
        print(f'    {cat}: {queued} workflow(s) waiting in the queue')
    c.close()
if not found:
    print('  \033[32m✓\033[0m nothing running or queued')
PY
fi

echo
if ((FAILED)); then
    echo "Something needs attention (see ✗ above)."
    exit 1
fi
echo "All services are up."
exit 0
}   # end read-the-whole-file guard
