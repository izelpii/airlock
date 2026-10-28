#!/bin/sh
# The certs service: run the application's make-certs.sh once, then never again.
#
#   AIRLOCK_NAMES      this machine's LAN address(es) and any extra names,
#                      comma- or space-separated. Inside a container the script
#                      can only see the container's own address, so it runs with
#                      --no-auto-ip and is TOLD the machine's - without them,
#                      browsers warn on https://<lan address>/.
#   AIRLOCK_CA_LABEL   what to call this installation's CA (default: airlock)
#   PUID, PGID         who should own ./certs afterwards (default 1000:1000)
#
# Already there (fullchain.pem and privkey.pem): nothing to do - restarting the
# stack must never replace a certificate that devices already trust. To renew
# the leaf and keep the CA:  docker compose run --rm certs --force
set -eu

cd /work
if [ "$#" -eq 0 ] && [ -s certs/fullchain.pem ] && [ -s certs/privkey.pem ]; then
    echo "certs: certificate present - nothing to do"
    exit 0
fi

# shellcheck disable=SC2086  # the names are meant to split
set -- --no-auto-ip --ca-label "${AIRLOCK_CA_LABEL:-airlock}" "$@" $(echo "${AIRLOCK_NAMES:-}" | tr ',' ' ')
if [ -z "${AIRLOCK_NAMES:-}" ]; then
    echo "certs: AIRLOCK_NAMES is empty - the certificate will cover airlock.lan and its"
    echo "       service names only; set this machine's LAN address in .env to use"
    echo "       https://<lan address>/ without a warning, then: docker compose run --rm certs --force"
fi

./make-certs.sh "$@"

chown -R "${PUID:-1000}:${PGID:-1000}" certs
chmod 0700 certs
chmod 0644 certs/airlock-ca.pem certs/fullchain.pem 2>/dev/null || true
echo "certs: install certs/airlock-ca.pem on each device that will open Airlock"
