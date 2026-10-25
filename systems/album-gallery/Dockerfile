# ── Stage 1: build the React/Vite frontend ────────────────────────────────────
FROM docker.io/library/node:22-alpine AS builder

WORKDIR /build

# Copy package files first for layer-cached installs
COPY app/package.json app/package-lock.json ./app/
RUN npm ci --prefix ./app

# Copy the rest of the app source and build
COPY app/ ./app/
RUN npm run build --prefix ./app

# ── Stage 2: minimal production image ─────────────────────────────────────────
FROM docker.io/library/node:22-alpine

WORKDIR /app

# The production server has NO runtime dependencies: node builtins only.
# It used to need image-size to answer /api/dimensions, and that route is gone —
# image sizes are baked into each album manifest at publish time, because a
# packed bundle on a static host has no server to ask.
COPY server.mjs ./
RUN echo '{"name":"album-gallery-server","private":true,"type":"module"}' > package.json

# Copy the Vite build output from the builder stage
COPY --from=builder /build/app/dist ./app/dist

# Which gallery this is: the last commit to systems/album-gallery, passed by
# start-podman.sh (airlock_gallery_revision). As a label for `docker image
# inspect`, and in the environment for /health - which is how the Publish to
# server button compares a server's gallery with this machine's
# (deployment-publish-to-server-spec.md section 9).
ARG GALLERY_REVISION=unknown
ENV GALLERY_REVISION=$GALLERY_REVISION
LABEL org.opencontainers.image.revision=$GALLERY_REVISION

# Port the server listens on (matches Vite dev default; override via PORT env var)
EXPOSE 3001

# No ALBUM_ROOT default. Setting one here would silently enable legacy
# single-gallery mode in every container, including ones that only ever serve
# deployments. Both variables are supplied at run time instead:
#   CATALOGS_ROOT         the catalogs root, mounted read-only  (deployments)
#   ALBUM_SESSION_SECRET  signs the session cookie              (passwords)
#   ALBUM_ROOT            legacy single-gallery mode, one release only

CMD ["node", "server.mjs"]
