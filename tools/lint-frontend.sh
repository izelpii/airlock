#!/usr/bin/env bash
# Lint the React sources without building them.
#
#   tools/lint-frontend.sh                 # the whole of systems/frontend/src
#   tools/lint-frontend.sh src/views/Locations.js ...
#
# WHY THIS EXISTS. react-scripts runs ESLint as part of `npm run build`, and a
# lint error there fails the image build - but only after `npm ci` and a couple
# of minutes of webpack, and only inside the podman VM. There is no node on the
# development Macs outside that build, so a one-character mistake in a JSX file
# cost a full build cycle to discover. Twice: a rename that left two references
# behind compiled fine by every check available here and failed with
# "'locations' is not defined" after four minutes.
#
# This runs the SAME ESLint, from the frontend image's own `deps` stage - the
# one that stops after `npm ci` - with the host's src/ mounted in. `deps` exists
# for this: targeting `builder` instead runs the whole webpack build to get at
# node_modules, which is the thing a pre-build lint exists to avoid. Seconds
# after the first run, and it shares the build's own layer cache.
#
# It does two things: compiles every file with the project's real Babel preset,
# and checks ONE lint rule, no-undef. The compile is the stronger of the two and
# was added after a JSX comment passed ESLint and would have failed the build.
#
# no-undef is the rule that fails a build, and it is
# also the only one that can be trusted from here. no-unused-vars was tried and
# removed: without eslint-plugin-react, ESLint cannot see that JSX uses the
# names it imports, so `React`, `CButton` and every component came back as
# unused - 989 warnings, none of them real. A lint that is mostly wrong gets
# ignored, and then so does the one line in it that was right.

set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
FRONTEND="$ROOT/systems/frontend"
IMAGE=airlock-frontend-lint

ok()   { printf "  \033[32m✓\033[0m %s\n" "$1"; }
warn() { printf "  \033[33m!\033[0m %s\n" "$1"; }
die()  { printf "  \033[31m✗\033[0m %s\n" "$1"; exit 1; }

[[ -d "$FRONTEND/src" ]] || die "no $FRONTEND/src"

# Only the dependency layers are needed, so this stops at `builder` and reuses
# the same cache the real build populates. Quiet unless it actually has to work.
if ! podman image exists "$IMAGE" 2>/dev/null; then
  echo "Preparing the lint image (first run only; reuses the build cache)..."
fi
if ! podman build --target deps -t "$IMAGE" \
      --ignorefile "$FRONTEND/.dockerignore" \
      -f "$FRONTEND/Dockerfile" "$FRONTEND" >/dev/null 2>&1; then
  # Named separately from a lint failure: "the image would not build" and "your
  # code has an error" are different problems with different fixes, and
  # conflating them sent one real lint error here as an image failure.
  die "could not prepare the lint image (npm ci failed?) - run it verbosely:
      podman build --target deps -f $FRONTEND/Dockerfile $FRONTEND"
fi

TARGETS=("$@")
if (( ${#TARGETS[@]} == 0 )); then
  TARGETS=(src/)
fi

# --no-eslintrc with an explicit config, rather than the project's: CRA's config
# lives inside react-scripts and pulls in plugins (react-hooks) whose rules the
# inline eslint-disable comments in this codebase reference. Loading a subset of
# it makes those comments themselves errors, which is why --no-inline-config is
# set too.
#
# Test files get the jest globals; they are linted too, since a broken test file
# is a broken file.
# ── 1. Does it COMPILE? ───────────────────────────────────────────────────────
# With the project's real Babel preset, which is the same thing `npm run build`
# does first. ESLint parses with espree and is more forgiving: a JSX comment
# placed somewhere espree accepts and Babel does not would pass the lint and
# fail the build, which is the one outcome this script exists to prevent.
podman run --rm --entrypoint sh -e NODE_ENV=production \
  -v "$FRONTEND/src:/build/src:ro,z" \
  "$IMAGE" -c 'cd /build && node -e "
const babel = require(\"@babel/core\"), fs = require(\"fs\"), path = require(\"path\");
function walk(d){return fs.readdirSync(d,{withFileTypes:true}).flatMap(e=>
  e.isDirectory()?walk(path.join(d,e.name)):(e.name.endsWith(\".js\")?[path.join(d,e.name)]:[]));}
let bad = 0, all = walk(\"src\");
for (const f of all) {
  try { babel.transformSync(fs.readFileSync(f,\"utf8\"),
          {filename:f, presets:[require.resolve(\"babel-preset-react-app\")]}); }
  catch (e) { bad++; console.log(\"FAILED  \"+f); console.log(\"   \"+String(e.message).split(\"\\n\")[0]); }
}
console.log(bad ? bad+\" of \"+all.length+\" file(s) will not compile\" : all.length+\" files compile\");
process.exit(bad?1:0);
"'
compile_rc=$?

# ── 2. Does it REFERENCE anything that does not exist? ───────────────────────
podman run --rm --entrypoint sh \
  -v "$FRONTEND/src:/build/src:ro,z" \
  "$IMAGE" -c "cd /build && ./node_modules/.bin/eslint --ext .js --no-eslintrc \
    --parser-options=ecmaVersion:2021,sourceType:module,ecmaFeatures:{jsx:true} \
    --env browser,es2021,jest,node --no-inline-config \
    --rule '{\"no-undef\":\"error\"}' \
    ${TARGETS[*]}"
lint_rc=$?

if (( compile_rc == 0 && lint_rc == 0 )); then
  ok "compiles, and no undefined identifiers in ${TARGETS[*]}"
else
  warn "fix the errors above before building - they will fail the image build"
fi
(( compile_rc || lint_rc )) && exit 1
exit 0
