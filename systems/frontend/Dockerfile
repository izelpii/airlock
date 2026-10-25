# ── Stage 1a: dependencies only ───────────────────────────────────────────────
# Its own stage so something can stop here. tools/lint-frontend.sh does: it
# needs node_modules and the host's src/ mounted over it, and nothing else - and
# `--target builder` would have run the whole webpack build to get there, which
# is the thing a pre-build lint exists to avoid.
#
# No duplicated install: the builder below continues FROM this, so `npm ci` runs
# once and both paths share the layer.
FROM docker.io/library/node:22-alpine AS deps

WORKDIR /build

# Upgrade npm to match the version used to generate package-lock.json
RUN npm install -g npm@latest

# Copy package files first for layer-cached installs
COPY package.json package-lock.json ./
RUN npm ci

# ── Stage 1b: build the React app ─────────────────────────────────────────────
FROM deps AS builder

# ── Service URLs ──────────────────────────────────────────────────────────────
# Empty on purpose. src/config.js now defaults to RELATIVE paths, so the bundle
# addresses the API as /api/... on whatever origin served the page, and the
# gateway (systems/gateway) routes it. That means the backend's address is no
# longer compiled in, and moving the stack no longer needs a frontend rebuild.
#
# Create React App still inlines REACT_APP_* at build time, so setting any of
# these to an absolute URL reintroduces exactly that coupling. They remain as an
# escape hatch for a dev build pointed at hand-started services:
#   podman build --build-arg REACT_APP_API_HOST=http://localhost:5000 ...
# Prefer leaving them empty and letting the gateway decide.
ARG REACT_APP_API_HOST=
ARG REACT_APP_VISION_HOST=
ARG REACT_APP_GEOLOC_HOST=
ENV REACT_APP_API_HOST=$REACT_APP_API_HOST \
    REACT_APP_VISION_HOST=$REACT_APP_VISION_HOST \
    REACT_APP_GEOLOC_HOST=$REACT_APP_GEOLOC_HOST

# ── Build memory ──────────────────────────────────────────────────────────────
# webpack's production build is the most memory-hungry thing in this repo, and it
# runs inside the podman machine's VM, not on the Mac - so it gets whatever RAM
# that VM was created with, which is usually far less than the host has. When it
# exceeds that, the kernel SIGKILLs the child and CRA reports:
#
#   The build failed because the process exited too early. This probably means
#   the system ran out of memory or someone called `kill -9` on the process.
#
# Two defences:
#
# 1. GENERATE_SOURCEMAP=false. Source maps are the single largest consumer here -
#    building them means holding the full module graph plus mappings in memory.
#    They are also useless in this image: nginx serves the bundle to a browser,
#    and nobody debugs minified production output through it.
#
# 2. A hard V8 heap cap BELOW the VM's memory. This is deliberately a cap, not a
#    raise: unbounded, V8 keeps growing until the cgroup kills it outright, and a
#    killed process cannot garbage-collect. Capped, it collects and finishes.
#    Raise NODE_BUILD_HEAP_MB only if the podman machine has the RAM to back it.
ARG NODE_BUILD_HEAP_MB=3072
ENV GENERATE_SOURCEMAP=false \
    NODE_OPTIONS=--max-old-space-size=$NODE_BUILD_HEAP_MB

# Copy the rest of the source and build. REACT_APP_BUILD_ID stamps this build:
# public/'s images (card decorators, logo) are asked for as <file>?v=<stamp>,
# which nginx lets browsers keep for a year - a rebuild is a new stamp, so a
# replaced image is a new URL rather than a stale cache entry (config.js
# assetUrl).
COPY . .
RUN REACT_APP_BUILD_ID=$(date -u +%Y%m%d%H%M%S) npm run build

# ── Stage 2: serve the static build with nginx ────────────────────────────────
FROM docker.io/library/nginx:1.27-alpine

# Replace the default nginx config with a minimal SPA-friendly one.
#
# Cache headers, because without any a browser guesses (heuristic freshness: a
# fraction of the time since Last-Modified) and can keep running the PREVIOUS
# build for hours after a redeploy - which is exactly what happened on oldserver:
# a fixed Videos page was deployed, and the browser kept serving the old one.
#
#   index.html  no-cache: revalidated on every load. It is 500 bytes, and it is
#               the file that names the current bundle. Also covers every SPA
#               route, which try_files serves as /index.html.
#   /static/    content-hashed names (main.<hash>.js): a new build is a new
#               URL, so these never need revalidating.
#   the rest    public/'s own files keep their names when replaced, so no-cache:
#               the browser asks each time and gets a 304 while unchanged.
#               Without it, new decorators stayed invisible for days after a
#               deploy (2026-10-21).
#   ?v=<build>  the same files asked for with the build's stamp (assetUrl in
#               config.js: the decorators and the logo) are kept for a year and
#               not asked for again - a new build is a new URL. The map makes
#               this depend on the stamp: without one, still no-cache.
RUN printf 'map $arg_v $public_cache {\n\
    ""      "no-cache";\n\
    default "public, max-age=31536000, immutable";\n\
}\n\
server {\n\
    listen 3000;\n\
    root /usr/share/nginx/html;\n\
    index index.html;\n\
    location / {\n\
        add_header Cache-Control $public_cache;\n\
        try_files $uri $uri/ /index.html;\n\
    }\n\
    location = /index.html {\n\
        add_header Cache-Control "no-cache";\n\
    }\n\
    location /static/ {\n\
        add_header Cache-Control "public, max-age=31536000, immutable";\n\
    }\n\
}\n' > /etc/nginx/conf.d/default.conf

# Copy the React build output from the builder stage
COPY --from=builder /build/build /usr/share/nginx/html

EXPOSE 3000

CMD ["nginx", "-g", "daemon off;"]
