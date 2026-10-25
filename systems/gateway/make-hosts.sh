#!/usr/bin/env bash
# Print (or install) the /etc/hosts entries for the airlock service names.
#
#   ./make-hosts.sh              # print them
#   ./make-hosts.sh --check      # report which names already resolve
#   sudo ./make-hosts.sh --install
#
# --install is idempotent: it skips any name already present in /etc/hosts, so
# running it twice does not produce duplicate entries.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ENTRIES="$SCRIPT_DIR/hosts-entries.txt"
HOSTS_FILE="${HOSTS_FILE:-/etc/hosts}"

names() { grep -E '^[0-9]' "$ENTRIES" | awk '{print $2}'; }

case "${1:-}" in
  --check)
    fail=0
    while read -r name; do
      if getent hosts "$name" >/dev/null 2>&1 || ping -c1 -t1 "$name" >/dev/null 2>&1; then
        printf '  %-26s resolves\n' "$name"
      else
        printf '  %-26s NOT RESOLVING\n' "$name"; fail=1
      fi
    done < <(names)
    [[ $fail -eq 0 ]] || {
      echo
      echo "Run: sudo $0 --install"
      exit 1
    }
    ;;

  --install)
    [[ -w "$HOSTS_FILE" ]] || { echo "Cannot write $HOSTS_FILE - run with sudo."; exit 1; }
    added=0
    while read -r name; do
      if grep -qE "^[^#]*[[:space:]]${name//./\\.}([[:space:]]|$)" "$HOSTS_FILE"; then
        printf '  %-26s already present\n' "$name"
      else
        printf '127.0.0.1\t%s\n' "$name" >> "$HOSTS_FILE"
        printf '  %-26s added\n' "$name"
        added=$((added + 1))
      fi
    done < <(names)
    echo
    echo "$added name(s) added to $HOSTS_FILE."
    ;;

  *)
    cat "$ENTRIES"
    echo
    echo "# To install:  sudo $0 --install"
    ;;
esac
