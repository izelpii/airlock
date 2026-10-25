#!/usr/bin/env bash
# Move an installation from /opt/shoebox to /opt/airlock - phase 7 of
# documentation/specs/name-migration-spec.md.
#
#   deploy/move-to-opt-airlock.sh            # check, and print what it would do
#   deploy/move-to-opt-airlock.sh --apply    # do it (asks for sudo)
#
# What it does, in order:
#   1. refuses unless /opt/shoebox is a real directory, /opt/airlock does not
#      exist, both are on the same filesystem (so the move is a rename, not a
#      copy of a terabyte), and no workflow is running in any catalog
#   2. ./stop-all.sh
#   3. unmounts every bind mount under /opt/shoebox
#   4. mv /opt/shoebox /opt/airlock, and leaves /opt/shoebox as a symlink to it,
#      which catches anything still naming the old path
#   5. rewrites the mount targets in /etc/fstab (a copy is kept beside it),
#      reloads systemd, mounts everything again
#   6. STOPS unless every mount that was there before is there again, at its new
#      path, and not empty - the fstab lines say `nofail`, so a wrong path
#      mounts nothing and says nothing, and every photo would show as missing
#   7. ./start-all.sh from /opt/airlock
#
# /media/usb1/shoebox-storage, the source side of the mounts, keeps its name
# (decided 2026-10-11). Only the targets under /opt move.

set -uo pipefail

OLD=/opt/shoebox
NEW=/opt/airlock
APPLY=0
[[ "${1:-}" == "--apply" ]] && APPLY=1

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
bad()  { printf "  \033[31m✗\033[0m %s\n" "$1"; FAIL=1; }
step() { printf "\n── %s\n" "$1"; }
run()  {
  echo "  \$ $*"
  if [[ $APPLY -eq 1 ]]; then "$@" || { bad "failed: $*"; exit 1; }; fi
}

FAIL=0
step "Checks"
[[ "$(uname -s)" == "Linux" ]] && ok "Linux" || bad "this is for the Linux host (homeserver), not $(uname -s)"
if [[ -L "$OLD" ]]; then
  bad "$OLD is already a symlink (-> $(readlink "$OLD")) - nothing to move"
elif [[ -d "$OLD" ]]; then
  ok "$OLD is a directory"
else
  bad "$OLD does not exist"
fi
[[ -e "$NEW" ]] && bad "$NEW already exists - refusing to merge into it" || ok "$NEW is free"
if [[ -d "$OLD" && ! -L "$OLD" ]]; then
  if [[ "$(stat -c %d "$OLD")" == "$(stat -c %d "$(dirname "$NEW")")" ]]; then
    ok "same filesystem: the move is a rename"
  else
    bad "$OLD and $(dirname "$NEW") are on different filesystems - the move would copy everything"
  fi
fi

# Running workflows would be killed by stop-all.sh. Asked of each catalog's
# database directly; read-only, and python3 because sqlite3 is not installed.
RUNNING="$(python3 - "$OLD" <<'PY' 2>/dev/null
import glob, sqlite3, sys
out = []
for db in glob.glob(sys.argv[1] + '/catalogs/*/workflow_results.db'):
    try:
        c = sqlite3.connect(f'file:{db}?mode=ro', uri=True, timeout=5)
        for (name,) in c.execute("SELECT workflow_name FROM workflow_executions WHERE status = 'running'"):
            out.append(f'{db.split("/")[-2]}: {name}')
    except sqlite3.Error as e:
        out.append(f'{db}: could not check ({e})')
print('\n'.join(out))
PY
)"
if [[ -n "$RUNNING" ]]; then
  bad "workflows are running - stop-all.sh would interrupt them:"
  printf '      %s\n' "$RUNNING"
else
  ok "no workflow running"
fi

MOUNTS="$(findmnt -rn -o TARGET | grep "^$OLD/" || true)"
COUNT="$(printf '%s' "$MOUNTS" | grep -c . || true)"
ok "$COUNT bind mount(s) under $OLD"
printf '      %s\n' $MOUNTS
FSTAB_LINES="$(grep -c "[[:space:]]$OLD/" /etc/fstab || true)"
ok "$FSTAB_LINES /etc/fstab line(s) mount into $OLD"

if [[ $FAIL -ne 0 ]]; then
  echo; echo "Not moving: fix the above first."; exit 1
fi
if [[ $APPLY -eq 0 ]]; then
  echo; echo "Checks pass. This is what --apply would do:"
fi

step "Stop the stack"
run bash -c "cd '$OLD' && ./stop-all.sh"

step "Unmount"
for m in $MOUNTS; do run sudo umount "$m"; done

step "Move, and leave the old path pointing at the new"
run sudo mv "$OLD" "$NEW"
run sudo ln -s "$NEW" "$OLD"

step "Rewrite /etc/fstab targets"
run sudo cp -p /etc/fstab "/etc/fstab.before-airlock"
run sudo sed -i "s#\([[:space:]]\)$OLD/#\1$NEW/#" /etc/fstab
run sudo systemctl daemon-reload
run sudo mount -a

step "Every mount back, at its new path, and not empty"
if [[ $APPLY -eq 1 ]]; then
  for m in $MOUNTS; do
    n="$NEW${m#$OLD}"
    if mountpoint -q "$n" && [[ -n "$(ls -A "$n" 2>/dev/null | head -1)" ]]; then
      ok "$n"
    else
      bad "$n is not mounted, or is empty"
    fi
  done
  if [[ $FAIL -ne 0 ]]; then
    echo
    echo "STOPPED before starting anything. /etc/fstab.before-airlock is the old file;"
    echo "the data is at $NEW and $OLD points to it. Fix the mounts, then ./start-all.sh."
    exit 1
  fi
else
  echo "  (checks each of the $COUNT mounts with mountpoint -q and ls)"
fi

step "Start the stack"
run bash -c "cd '$NEW' && ./start-all.sh"

echo
if [[ $APPLY -eq 1 ]]; then
  echo "Done. On each Mac that pushes here:"
else
  echo "Afterwards, on each Mac that pushes here:"
fi
echo "  git remote set-url homeserver ssh://homeserver$NEW"
