#!/usr/bin/env bash
# Every image that runs as appuser must work for ANY owner, not just uid 1000.
#
#   compose/ci/any-uid-test.sh [--engine docker|podman] [--images PREFIX] [--tag TAG]
#
# Each image is started the way docker-compose.yml starts it, with PUID and PGID
# set to 4242 - a uid nothing in any image knows. It passes when the service
# answers its health URL, the process is running as 4242, and the folders the
# image owns (AIRLOCK_OWNED_DIRS) belong to 4242. Exit status: the number of
# images that failed.
set -uo pipefail

ENGINE="" ; IMAGES="localhost" ; TAG="dev"
while (( $# )); do
  case "$1" in
    --engine) ENGINE="$2"; shift ;;
    --images) IMAGES="$2"; shift ;;
    --tag)    TAG="$2"; shift ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done
if [[ -z "$ENGINE" ]]; then command -v docker >/dev/null 2>&1 && ENGINE=docker || ENGINE=podman; fi

# image | port | health path | extra run arguments
CASES=(
  "backend|5000|/api/version|-e DISABLE_WORKFLOW_SCHEDULER=1 --tmpfs /app/database:mode=1777 --tmpfs /catalogs:mode=1777 --tmpfs /input:mode=1777 --tmpfs /storage:mode=1777 -e STORAGE_ROOT_CONTAINER=/storage"
  "deepface|5001|/api/health|-e DEEPFACE_WORKERS=1 -e DEEPFACE_WARMUP_EMBEDDER=0 -e FACE_LABELS_DB=/app/data/face_labels.db"
  "pets|5006|/health|"
  "vision|5003|/health|"
  "geoloc|5002|/health|"
  "transcode|5004|/health|--tmpfs /catalogs:mode=1777"
)
FAILED=0
for case in "${CASES[@]}"; do
  IFS='|' read -r name port probe extra <<< "$case"
  image="$IMAGES/airlock-$name:$TAG"
  ctr="airlock-anyuid-$name"
  "$ENGINE" rm -f "$ctr" >/dev/null 2>&1
  # shellcheck disable=SC2086  # extra is a list of arguments
  if ! "$ENGINE" run -d --name "$ctr" -e PUID=4242 -e PGID=4242 $extra "$image" >/dev/null; then
    echo "✗ $name: did not start"; FAILED=$((FAILED+1)); continue
  fi
done

for case in "${CASES[@]}"; do
  IFS='|' read -r name port probe extra <<< "$case"
  ctr="airlock-anyuid-$name"
  answer=""
  for _ in $(seq 1 60); do
    answer="$("$ENGINE" exec "$ctr" python3 -c "
import urllib.request as u, urllib.error as e
try: print(u.urlopen('http://127.0.0.1:$port$probe', timeout=5).status)
except e.HTTPError as x: print(x.code)
except Exception: print('')" 2>/dev/null)"
    [[ -n "$answer" ]] && break
    sleep 5
  done
  who="$("$ENGINE" exec "$ctr" sh -c 'ps -o user= -p 1 2>/dev/null || stat -c %u /proc/1' 2>/dev/null | tr -d ' ')"
  uid1="$("$ENGINE" exec "$ctr" sh -c 'awk "/^Uid:/ {print \$2}" /proc/1/status' 2>/dev/null)"
  owned_ok=1
  for d in $("$ENGINE" exec "$ctr" sh -c 'echo $AIRLOCK_OWNED_DIRS' 2>/dev/null); do
    o="$("$ENGINE" exec "$ctr" stat -c %u "$d" 2>/dev/null)"
    [[ "$o" == 4242 ]] || { owned_ok=0; echo "  $name: $d belongs to $o"; }
  done
  if [[ -n "$answer" && "$uid1" == 4242 && $owned_ok == 1 ]]; then
    echo "✓ $name: answers $answer as uid 4242"
  else
    echo "✗ $name: answer='${answer}' uid='${uid1}' (${who})"
    "$ENGINE" logs "$ctr" 2>&1 | tail -15 | sed 's/^/    /'
    FAILED=$((FAILED+1))
  fi
  "$ENGINE" rm -f "$ctr" >/dev/null 2>&1
done
exit "$FAILED"
