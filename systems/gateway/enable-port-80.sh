#!/usr/bin/env bash
# Let rootless podman bind the gateway's ports inside the podman machine.
#
# The gateway publishes 80 (redirect) and 443 (TLS, the one the browser uses).
# Both are privileged, and lowering the floor to 80 allows both.
#
# Without this, starting the gateway fails with:
#
#   rootlessport cannot expose privileged port 80, you can add
#   'net.ipv4.ip_unprivileged_port_start=80' to /etc/sysctl.conf (currently 1024),
#   or choose a larger port number (>= 1024)
#
# The restriction lives in the podman machine's Linux VM, not on macOS, so this
# lowers the unprivileged-port floor there. It does not touch your Mac, and it
# affects only that VM.
#
# Why bother rather than just using 8080: the whole point of the migration is
# addressing services by name. http://backend.airlock.lan is a name;
# http://backend.airlock.lan:8080 is a name and a port. If you would rather not
# change a VM sysctl, 8080 is fully supported - see --port-8080 below.
#
#   ./enable-port-80.sh            # enable it (persistent across VM restarts)
#   ./enable-port-80.sh --check    # report the current floor
#   ./enable-port-80.sh --port-8080  # print what to change to use 8080 instead

set -uo pipefail

SYSCTL_FILE=/etc/sysctl.d/99-shoebox-unprivileged-ports.conf
SETTING='net.ipv4.ip_unprivileged_port_start=80'

current() { podman machine ssh 'cat /proc/sys/net/ipv4/ip_unprivileged_port_start' 2>/dev/null | tr -d '\r'; }

case "${1:-}" in
  --check)
    v="$(current)"
    if [[ -z "$v" ]]; then
      echo "Could not reach the podman machine. Is it running?  podman machine start"
      exit 1
    fi
    echo "  unprivileged port floor in the podman machine: $v"
    if (( v <= 80 )); then
      echo "  ports 80 and 443 are available to rootless containers"
    else
      echo "  ports 80 and 443 are NOT available - run: $0"
    fi
    ;;

  --port-8080)
    cat <<'HELP'
To run the gateway on unprivileged ports:

  BOTH of them. 443 is privileged too, and it is the port that matters - :80
  only redirects to it. Moving 80 alone leaves the gateway failing to bind 443,
  which looks like "the stack is fine but the UI cannot reach /api".

  1. systems/gateway/gateway-pod.yaml   -> hostPort: 8080
  2. Start with:                           GATEWAY_PORT=8080 TLS_PORT=8443 ./start-podman.sh
     and open https://airlock.lan:8443/
  3. Every service name then carries the port, so update the URLs that
     services use to reach each other - they are all env values in the pod
     manifests:
        backend/backend-pod.yaml          DEEPFACE_URL, ALBUMS_URL, BACKEND_URL
        backend-vision/backend-vision-pod.yaml   OLLAMA_HOST
        backend-albums/backend-albums-pod.yaml   OLLAMA_HOST
     e.g. http://deepface.airlock.lan:8080
  4. The browser needs no change: config.js uses relative paths, so the UI
     works on whatever origin serves it, including airlock.lan:8443.
HELP
    ;;

  *)
    echo "Lowering the unprivileged port floor in the podman machine..."
    if ! podman machine ssh "echo '$SETTING' | sudo tee $SYSCTL_FILE >/dev/null \
         && sudo sysctl -p $SYSCTL_FILE >/dev/null"; then
      echo
      echo "Failed. Is the podman machine running?  podman machine start"
      echo "If you would rather not change it, run: $0 --port-8080"
      exit 1
    fi
    v="$(current)"
    echo "  floor is now: ${v:-unknown}"
    if [[ -n "$v" ]] && (( v <= 80 )); then
      echo "  ports 80 and 443 are available. Start the gateway:  ./start-podman.sh"
    else
      echo "  the setting did not take effect - run: $0 --port-8080"
      exit 1
    fi
    ;;
esac
