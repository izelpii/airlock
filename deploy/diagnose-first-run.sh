#!/usr/bin/env bash
# Why is a new deployment asking me to sign in instead of offering first-run setup?
#
#   ./deploy/diagnose-first-run.sh                  # run ON the new machine
#   ./deploy/diagnose-first-run.sh https://airlock.lan
#                                                   # also test the address a
#                                                   # browser is typing, from
#                                                   # whichever machine is typing it
#
# Changes nothing. Every check is a read.
#
# The sign-in form appears instead of "Welcome to Airlock Photos" for exactly four
# reasons, and they look identical in the browser:
#
#   1. the browser is not talking to this machine at all - airlock.lan is
#      127.0.0.1 in /etc/hosts on EVERY installation, so typing the name from
#      another computer reaches that computer's own stack, or nothing;
#   2. an account already exists here, because the database directory was
#      carried over rather than created fresh;
#   3. the backend cannot be reached from where the page was loaded - a service
#      port such as :3000 serves the SPA but not /api, so the setup probe gets
#      index.html with status 200 and no answer in it;
#   4. the backend is reachable but erroring, so the probe cannot get an answer.
#
# The UI does say so - the sign-in form carries a warning banner for 3 and 4 -
# but the banner cannot distinguish them, and case 1 has no banner at all
# because that stack is answering perfectly well. This script names which one.

{   # read-the-whole-file guard; see start-all.sh

set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
source "$HERE/airlock-env.sh"

BROWSER_URL="${1:-}"

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; }
info() { printf "    %s\n" "$1"; }

VERDICT=""
verdict() { [[ -n "$VERDICT" ]] || VERDICT="$1"; }

# Ask an endpoint and report what came back, classifying the answer rather than
# printing a body: the failure that matters most (case 3) is a 200 whose body is
# HTML, which looks like success to everything that only checks the status.
#
# Sets: PROBE_STATUS, PROBE_KIND (setup|login|html|error|unreachable), PROBE_NOTE
probe() {
    local url="$1" body status
    body="$(curl -sk --max-time 5 -w $'\n%{http_code}' "$url" 2>/dev/null)" || {
        PROBE_STATUS=""; PROBE_KIND="unreachable"
        PROBE_NOTE="no answer (connection refused, timed out, or DNS)"
        return
    }
    status="${body##*$'\n'}"
    body="${body%$'\n'*}"
    PROBE_STATUS="$status"
    case "$body" in
        *'"setup_required"'*'true'*)
            PROBE_KIND="setup";  PROBE_NOTE="setup_required: true - offers first-run setup" ;;
        *'"setup_required"'*'false'*)
            PROBE_KIND="login";  PROBE_NOTE="setup_required: false - an account already exists" ;;
        *'<!DOCTYPE'*|*'<html'*|*'<!doctype'*)
            PROBE_KIND="html"
            PROBE_NOTE="HTTP $status but the body is HTML - this address serves the UI, not /api" ;;
        *)
            PROBE_KIND="error"
            PROBE_NOTE="HTTP $status: $(printf '%s' "$body" | tr -d '\n' | cut -c1-160)" ;;
    esac
}

# Why is the gateway down? Three causes account for essentially all of it on a
# machine that has never run the stack, and each has a different fix.
gateway_why() {
    echo
    echo "  Why the gateway is down:"

    if ! command -v podman >/dev/null 2>&1; then
        bad "  podman is not installed"
        return
    fi
    if podman container exists airlock-gateway 2>/dev/null; then
        warn "  the airlock-gateway container EXISTS but is not answering"
        info "  podman logs --tail 30 airlock-gateway"
        return
    fi
    info "  the airlock-gateway container is not running."

    # 1. The port floor. The gateway publishes 80 and 443, and rootless podman
    #    refuses both when the floor is 1024 - the default in a fresh podman
    #    machine. This is the one that catches a new Mac.
    local floor
    floor="$(_unprivileged_port_floor)"
    if [[ -z "$floor" ]]; then
        warn "  cannot read the unprivileged port floor (is the podman machine running?)"
    elif (( floor > 80 )); then
        bad "  the unprivileged port floor is $floor, so 80 and 443 cannot be bound"
        info "  THIS IS THE CAUSE. Fix it, then start the stack again:"
        if [[ "$(uname -s)" == "Darwin" ]]; then
            info "      systems/gateway/enable-port-80.sh     # VM only, persistent"
        else
            info "      sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80"
        fi
        info "      ./start-all.sh"
        VERDICT="rootless podman cannot bind 80/443 - the port floor is $floor"
        return
    else
        ok "  port floor is $floor - 80 and 443 are bindable"
    fi

    # 2. The certificate. certs/ is not in git, so a fresh clone has none and
    #    nginx -t cannot pass; the gateway exits before binding anything.
    if [[ ! -f "$AIRLOCK_ROOT/systems/gateway/certs/fullchain.pem" ]]; then
        bad "  no TLS certificate in systems/gateway/certs (not in git)"
        info "  Fix:  ./deploy/deploy.sh    (or: cd systems/gateway && ./make-certs.sh)"
        VERDICT="the gateway has no TLS certificate"
        return
    fi
    ok "  a TLS certificate is present"

    # 3. Whatever the log says.
    local log="$AIRLOCK_ROOT/logs/gateway.log"
    if [[ -f "$log" ]]; then
        info "  last lines of logs/gateway.log:"
        tail -12 "$log" | sed 's/^/      /'
    else
        info "  no logs/gateway.log yet - has ./start-all.sh been run?"
    fi
}

echo "── This machine ─────────────────────────────────────────────────────────"
echo "  repository      $AIRLOCK_ROOT"
echo "  database        $DATABASE_HOST_DIR"
IPS="$(ipconfig getifaddr en0 2>/dev/null; ipconfig getifaddr en1 2>/dev/null;
       hostname -I 2>/dev/null)"
IPS="$(printf '%s\n' $IPS | sort -u | tr '\n' ' ')"
echo "  lan address(es) ${IPS:-unknown}"
echo "  hostname        $(hostname)"

# ── 1. The database that is already here ──────────────────────────────────────
#
# A fresh clone has an empty database directory. Anything in it was carried from
# another machine, and if it holds a users table with rows then this deployment
# is not new as far as the backend is concerned - it is the old one, moved. That
# is a legitimate thing to do; it is only a surprise when it was not intended.
echo
echo "── Existing database ────────────────────────────────────────────────────"
DB="$DATABASE_HOST_DIR/catalogs.db"
if [[ -f "$DB" ]]; then
    warn "catalogs.db exists  ($(wc -c < "$DB" | tr -d ' ') bytes, modified $(
        date -r "$DB" 2>/dev/null || stat -c %y "$DB" 2>/dev/null))"
    info "A fresh install creates this on first start, so its mere presence is"
    info "normal once the stack has run. The user count below is the real answer."
else
    ok "no catalogs.db yet - nothing has been carried over"
fi

# NEVER open this database from the host. The container holds it in WAL mode
# over virtiofs, and a second reader from the other side of that mount reports
# "disk I/O error" - a failure that looks like corruption and is not. Ask the
# container, which is the only process that should have the file open.
echo
echo "── Accounts ─────────────────────────────────────────────────────────────"
if ! command -v podman >/dev/null 2>&1; then
    bad "podman not found - cannot ask the backend how many accounts exist"
elif ! podman container exists workflow-backend 2>/dev/null; then
    bad "the workflow-backend container is not running"
    if [[ "$(airlock_backend_port_status 5000)" == airplay ]]; then
        # The usual reason on a new Mac: the container cannot bind :5000, while
        # :5000 still answers - with AirPlay's 403, which the checks below show.
        info "Port 5000 is held by macOS's AirPlay Receiver, so it cannot start."
        info "Turn it off: $AIRLOCK_AIRPLAY_FIX"
        info "Then:        ./start-all.sh"
        verdict "macOS's AirPlay Receiver holds port 5000, so the backend cannot start"
    else
        info "Nothing serves /api, so the UI cannot be told that setup is needed."
        info "Start it:  ./start-all.sh        (or: cd systems/backend && ./start-podman.sh)"
        info "Then:      tail -50 logs/backend.log"
        verdict "the backend is not running"
    fi
else
    USERS="$(podman exec workflow-backend python3 -c '
import sqlite3, sys
try:
    c = sqlite3.connect("/app/database/catalogs.db")
    if not c.execute("SELECT 1 FROM sqlite_master WHERE type=\"table\" AND name=\"users\"").fetchone():
        print("NOTABLE"); sys.exit(0)
    rows = c.execute("SELECT username, is_admin FROM users ORDER BY id").fetchall()
    print(len(rows))
    for u, a in rows:
        print("  " + u + (" (admin)" if a else ""))
except Exception as e:
    print(f"ERROR {e}")
' 2>&1)"
    case "$USERS" in
        NOTABLE)
            bad "the users table does not exist"
            info "auth.install() creates it on every start, so the backend either"
            info "failed before that or is writing a different database file."
            info "Check:  tail -50 logs/backend.log"
            verdict "the backend never created its auth tables" ;;
        ERROR*)
            bad "could not read the database: ${USERS#ERROR }"
            verdict "the backend cannot read its own database" ;;
        0)
            ok "no accounts exist - this deployment SHOULD be offering first-run setup"
            info "So the sign-in form is not coming from this machine's data."
            info "The address the browser is using is the next thing to check." ;;
        *)
            n="$(printf '%s' "$USERS" | head -1)"
            warn "$n account(s) already exist here:"
            printf '%s\n' "$USERS" | tail -n +2 | sed 's/^/  /'
            info "This is why the sign-in form appears: setup is complete."
            info "It is not new data - a database directory came with the deployment."
            info "To start over, stop the stack and remove $DB"
            info "(that discards the accounts, not the catalogs)."
            verdict "an account already exists in $DATABASE_HOST_DIR" ;;
    esac
fi

# ── 2. What the backend itself answers ────────────────────────────────────────
echo
echo "── The API, asked directly ──────────────────────────────────────────────"
probe "http://localhost:5000/api/auth/setup-required"
case "$PROBE_KIND" in
    setup)       ok   "backend on :5000 - $PROBE_NOTE" ;;
    login)       warn "backend on :5000 - $PROBE_NOTE" ;;
    unreachable) bad  "backend on :5000 - $PROBE_NOTE"
                 verdict "the backend is not answering on port 5000" ;;
    *)           bad  "backend on :5000 - $PROBE_NOTE" ;;
esac

# ── 3. What the gateway answers, which is what a browser actually gets ────────
echo
echo "── The API, through the gateway ─────────────────────────────────────────"
probe "https://localhost/api/auth/setup-required"
case "$PROBE_KIND" in
    setup) ok   "gateway :443 - $PROBE_NOTE" ;;
    login) warn "gateway :443 - $PROBE_NOTE" ;;
    unreachable)
        bad "gateway :443 - $PROBE_NOTE"
        info "The gateway is what puts the UI and /api on one origin. Without it"
        info "the only way in is a service port, and a service port cannot serve"
        info "the API - which is exactly the case that shows a sign-in form."
        verdict "the gateway is not answering on :443"
        gateway_why ;;
    *) bad "gateway :443 - $PROBE_NOTE" ;;
esac

# The trap this whole screen is famous for: :3000 answers, and answers wrongly.
probe "http://localhost:3000/api/auth/setup-required"
if [[ "$PROBE_KIND" == "html" ]]; then
    warn ":3000 answers /api with the UI's index.html, as it must"
    info "If the browser is pointed at :3000, THAT is the bug: the page loads,"
    info "the probe gets HTTP 200 with no answer in it, and the app falls back"
    info "to the sign-in form. Use the gateway address instead."
fi

# ── 4. Where the name points ──────────────────────────────────────────────────
#
# airlock.lan is 127.0.0.1 on every installation, which is correct and is also
# the trap: the name is not a network address for this machine, it is "me". A
# browser on another computer that types it reaches that computer.
echo
echo "── Addressing ───────────────────────────────────────────────────────────"
RESOLVED="$( (getent hosts airlock.lan 2>/dev/null || dscacheutil -q host -a name airlock.lan 2>/dev/null | awk '/ip_address/{print $2}') | awk '{print $1}' | head -1)"
if [[ -z "$RESOLVED" ]]; then
    warn "airlock.lan does not resolve on this machine"
    info "sudo systems/gateway/make-hosts.sh --install"
elif [[ "$RESOLVED" == "127.0.0.1" || "$RESOLVED" == "::1" ]]; then
    ok "airlock.lan -> $RESOLVED (this machine, as designed)"
    info "That name means \"me\" on whatever machine types it. From another"
    info "computer's browser it reaches THAT computer, or nothing - never here."
    info "From elsewhere, use:  https://${IPS%% *}/"
else
    warn "airlock.lan -> $RESOLVED, which is not this machine"
    verdict "airlock.lan points at $RESOLVED, a different machine"
fi

# ── 5. The address a browser is actually using ────────────────────────────────
if [[ -n "$BROWSER_URL" ]]; then
    echo
    echo "── The address you gave: $BROWSER_URL"
    probe "${BROWSER_URL%/}/api/auth/setup-required"
    case "$PROBE_KIND" in
        setup) ok "$PROBE_NOTE"
               info "This address would show the first-run screen." ;;
        login) bad "$PROBE_NOTE"
               info "Whatever is answering here has an account already. If this"
               info "machine reported 0 accounts above, then this address is"
               info "reaching a DIFFERENT deployment - the original one."
               verdict "$BROWSER_URL is served by a deployment that already has an account" ;;
        html)  bad "$PROBE_NOTE"
               info "The page will load and then show a sign-in form with a"
               info "warning banner. Use the gateway address (https, no port)."
               verdict "$BROWSER_URL does not serve /api" ;;
        unreachable) bad "$PROBE_NOTE"
               verdict "$BROWSER_URL cannot be reached from here" ;;
        *)     bad "$PROBE_NOTE" ;;
    esac
fi

echo
echo "─────────────────────────────────────────────────────────────────────────"
if [[ -n "$VERDICT" ]]; then
    echo "  Most likely: $VERDICT"
else
    echo "  Nothing here explains a sign-in form. Run this again from the machine"
    echo "  whose browser shows it, passing the exact address it has in the bar:"
    echo "      ./deploy/diagnose-first-run.sh https://that-address/"
fi
echo

exit 0
}
