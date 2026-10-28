#!/usr/bin/env bash
# Build the compose install's images: each application image as it is (the
# BASE), then its compose layer on top. compose/README.md, "Building the images".
#
#   compose/ci/build.sh                          # every image, this machine only
#   compose/ci/build.sh backend gateway          # just these
#   compose/ci/build.sh --push --registry ghcr.io/izelpii --version 6.6.0
#                                                # both architectures, pushed
#   compose/ci/build.sh --push --arch arm64 ...  # CI: one architecture, natively
#   compose/ci/build.sh --merge ...              # CI: then join the two
#
# Local builds are tagged <registry>/airlock-<service>:<tag> with registry
# "localhost" and tag "dev" unless told otherwise - what docker-compose.yml runs
# with AIRLOCK_IMAGES=localhost and AIRLOCK_VERSION=dev in .env.
#
# Options
#   --push               build linux/amd64 and linux/arm64 with buildx, and push
#   --arch A             with --push: only linux/A (amd64 | arm64), pushed as
#                        <version>-A, for --merge to join. One machine per
#                        architecture, each building natively: under emulation
#                        the arm64 builds took over six hours.
#   --merge              join the --arch builds into the release tags (no build)
#   --registry R         image prefix (default: localhost; ghcr.io/izelpii in CI)
#   --version V          the release (default: the VERSION file); tags V, and with
#                        --push also MAJOR.MINOR, latest and sha-<commit>
#   --tag T              local tag (default: dev)
#   --engine E           docker | podman (default: docker if present, else podman)
#   --no-cache           pass through to the builds
#
# The BASE is the application's Dockerfile, untouched. Everything this script
# adds - the layer Dockerfiles, the certs image's context - is generated or
# assembled under a temporary folder, never in the source tree.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE="$(dirname "$HERE")"
REPO="$(dirname "$COMPOSE")"
IMAGES="$COMPOSE/images"

PUSH=0
ARCH=""
MERGE=0
REGISTRY="localhost"
VERSION=""
TAG="dev"
ENGINE=""
NO_CACHE=()
ONLY=()
while (( $# )); do
  case "$1" in
    --push)      PUSH=1 ;;
    --arch)      ARCH="$2"; shift ;;
    --merge)     MERGE=1; PUSH=1 ;;
    --registry)  REGISTRY="$2"; shift ;;
    --version)   VERSION="$2"; shift ;;
    --tag)       TAG="$2"; shift ;;
    --engine)    ENGINE="$2"; shift ;;
    --no-cache)  NO_CACHE=(--no-cache) ;;
    -h|--help)   sed -n '2,27p' "$0"; exit 0 ;;
    -*)          echo "unknown option: $1" >&2; exit 2 ;;
    *)           ONLY+=("$1") ;;
  esac
  shift
done

[[ -n "$VERSION" ]] || VERSION="$(tr -d '[:space:]' < "$REPO/VERSION")"
VERSION="${VERSION#v}"
if [[ -z "$ENGINE" ]]; then
  if command -v docker >/dev/null 2>&1; then ENGINE=docker; else ENGINE=podman; fi
fi
if (( PUSH )) && [[ "$ENGINE" != docker ]]; then
  echo "--push needs docker buildx (multi-architecture builds and push)" >&2
  exit 2
fi
case "$ARCH" in
  ''|amd64|arm64) ;;
  *) echo "--arch: amd64 or arm64, not $ARCH" >&2; exit 2 ;;
esac
if [[ -n "$ARCH" ]] && (( ! PUSH || MERGE )); then
  echo "--arch goes with --push (and not with --merge)" >&2
  exit 2
fi
if [[ "$VERSION" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] && (( PUSH )); then
  # Four-part releases change documentation only (CHANGELOG.md, "What moves which
  # number"): nothing in an image differs, so nothing is published.
  echo "v$VERSION is a documentation-only release - no images to publish"
  exit 0
fi

REVISION="$(git -C "$REPO" rev-parse --short HEAD 2>/dev/null || echo unknown)"
GALLERY_REVISION="$(git -C "$REPO" log -1 --format=%h -- systems/album-gallery 2>/dev/null || true)"
[[ -n "$GALLERY_REVISION" ]] || GALLERY_REVISION=unknown

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

# Service names in the order they are built, and each one's settings, from
# services.json (python3: present on every CI runner and on the Macs and Linux
# machines this is built on).
svc_field() {  # svc_field <service> <field> -> the value, JSON for lists/objects
  python3 - "$IMAGES/services.json" "$1" "$2" <<'PY'
import json, sys
svc = json.load(open(sys.argv[1]))['services'][sys.argv[2]]
v = svc.get(sys.argv[3])
print('' if v is None else (json.dumps(v) if isinstance(v, (list, dict)) else v))
PY
}
ALL=($(python3 -c "import json; print(' '.join(json.load(open('$IMAGES/services.json'))['services']))"))
if (( ${#ONLY[@]} )); then
  for s in "${ONLY[@]}"; do
    [[ " ${ALL[*]} " == *" $s "* ]] || { echo "unknown service: $s (known: ${ALL[*]})" >&2; exit 2; }
  done
  SERVICES=("${ONLY[@]}")
else
  SERVICES=("${ALL[@]}")
fi

release_tags() {  # the tags a release publishes, for every architecture
  echo "$VERSION" "${VERSION%.*}" latest "sha-$REVISION"
}

tags_for() {  # tags_for <image name> <suffix> -> -t arguments
  local name="$1" suffix="$2" t
  if [[ -n "$ARCH" ]]; then
    echo "-t" "$REGISTRY/$name:$VERSION$suffix-$ARCH"
  elif (( PUSH )); then
    for t in $(release_tags); do
      echo "-t" "$REGISTRY/$name:$t$suffix"
    done
  else
    echo "-t" "$REGISTRY/$name:$TAG$suffix"
  fi
}

build() {  # build <cache scope> <platforms> <context> <dockerfile> <tag and build args...>
  local scope="$1${ARCH:+-$ARCH}" platforms="$2" context="$3" dockerfile="$4"; shift 4
  if (( PUSH )); then
    docker buildx build --platform "$platforms" --push \
      --cache-from "type=gha,scope=$scope" --cache-to "type=gha,mode=max,scope=$scope" \
      ${NO_CACHE[@]+"${NO_CACHE[@]}"} -f "$dockerfile" "$@" "$context"
  else
    "$ENGINE" build ${NO_CACHE[@]+"${NO_CACHE[@]}"} -f "$dockerfile" "$@" "$context"
  fi
}

for svc in "${SERVICES[@]}"; do
  source="$(svc_field "$svc" source)"
  layer="$(svc_field "$svc" layer)"
  publish="$(svc_field "$svc" publish_as)"; publish="${publish:-$svc}"
  suffix="$(svc_field "$svc" tag_suffix)"
  platforms="$(svc_field "$svc" platforms | python3 -c "import json,sys; t=sys.stdin.read().strip(); print(','.join(json.loads(t)) if t else 'linux/amd64,linux/arm64')")"
  if [[ -n "$ARCH" ]]; then
    [[ ",$platforms," == *",linux/$ARCH,"* ]] || { echo; echo "── $svc: not built for $ARCH"; continue; }
    platforms="linux/$ARCH"
  fi
  echo
  echo "── $svc ($layer) ─────────────────────────────────────────────"

  if (( MERGE )); then
    # One manifest list per release tag, from the architectures this service
    # is built for. imagetools copies nothing: the layers are already pushed.
    sources=()
    for p in ${platforms//,/ }; do
      sources+=("$REGISTRY/airlock-$publish:$VERSION$suffix-${p#linux/}")
    done
    for t in $(release_tags); do
      docker buildx imagetools create -t "$REGISTRY/airlock-$publish:$t$suffix" "${sources[@]}"
    done
    continue
  fi

  # 1. The base: the application's Dockerfile as it is.
  base_args=(--build-arg "AIRLOCK_VERSION=$VERSION")
  build_args_json="$(svc_field "$svc" build_args)"
  if [[ -n "$build_args_json" ]]; then
    while IFS= read -r kv; do
      kv="${kv//\{version\}/$VERSION}"
      kv="${kv//\{gallery_revision\}/$GALLERY_REVISION}"
      base_args+=(--build-arg "$kv")
    done < <(python3 -c "import json,sys; [print(f'{k}={v}') for k,v in json.loads(sys.argv[1]).items()]" "$build_args_json")
  fi
  base_ref=""
  if [[ -n "$source" ]]; then
    if (( PUSH )); then
      base_ref="$REGISTRY/airlock-$publish-base:$VERSION$suffix${ARCH:+-$ARCH}"
    else
      base_ref="$REGISTRY/airlock-$publish-base:$TAG$suffix"
    fi
    build "$svc-base" "$platforms" "$REPO/$source" "$REPO/$source/Dockerfile" \
      -t "$base_ref" "${base_args[@]}"
  fi

  # 2. The compose layer, from a generated Dockerfile.
  context="$IMAGES"
  if [[ "$layer" == certs ]]; then
    context="$WORK/certs-context"
    mkdir -p "$context"
    cp "$IMAGES/certs/run.sh" "$context/run.sh"
    cp "$REPO/systems/gateway/make-certs.sh" "$context/make-certs.sh"   # unchanged, same commit
  fi
  dockerfile="$WORK/Dockerfile.$svc"
  python3 "$IMAGES/render_layer.py" "$svc" > "$dockerfile"
  # shellcheck disable=SC2046  # tags_for prints separate words on purpose
  build "$svc" "$platforms" "$context" "$dockerfile" $(tags_for "airlock-$publish" "$suffix") \
    ${base_ref:+--build-arg "BASE_IMAGE=$base_ref"} \
    --build-arg "AIRLOCK_VERSION=$VERSION" --build-arg "AIRLOCK_REVISION=$REVISION"
done

echo
if (( MERGE )); then
  echo "Published v$VERSION ($(release_tags)) to $REGISTRY for: ${SERVICES[*]}"
elif [[ -n "$ARCH" ]]; then
  echo "Pushed v$VERSION-$ARCH to $REGISTRY for: ${SERVICES[*]} - --merge publishes it"
elif (( PUSH )); then
  echo "Pushed v$VERSION to $REGISTRY for: ${SERVICES[*]}"
else
  echo "Built $REGISTRY/airlock-*:$TAG for: ${SERVICES[*]}"
  echo "Run them with AIRLOCK_IMAGES=$REGISTRY and AIRLOCK_VERSION=$TAG in compose/.env"
fi
