FROM docker.io/library/python:3.12-slim

# ── System dependencies ────────────────────────────────────────────────────────
# exiftool  – required by PyExifTool
# libraw-dev / libraw23 – required by rawpy
# libheif-dev – required by pillow-heif
# libjpeg-dev, zlib1g-dev – required by Pillow
# ffmpeg/ffprobe: video proxies and poster frames for the galleries
# (documentation/specs/video-spec.md). Workflows run in this container,
# so this is the only place the binaries are needed.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libimage-exiftool-perl \
        libraw23 \
        libheif1 \
        libjpeg62-turbo \
        zlib1g \
        ffmpeg \
        openssh-client \
        rsync \
        bzip2 \
        curl \
    && rm -rf /var/lib/apt/lists/*

# ── restic: catalog backups (documentation/specs/backup-backrest-spec.md) ──────
# Airlock runs restic itself, here, where the catalogs are; the repository lives
# on another machine (backup-host), reached over SFTP - hence openssh-client above.
# Pinned to the version backup-host's Backrest uses, and checked against the release's
# published SHA256SUMS. BSD-2-Clause: its notice ships with the image.
ARG RESTIC_VERSION=0.19.1
ARG TARGETARCH
RUN set -e; arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) sum=f415415624dcc452f2a02b8c33641791a8c6d6d3b65bbb3543fcf9a25151585c ;; \
      arm64) sum=a5f64aaab53d51e311fa3829124c5b703f2d14cf187d8640b6be3b2b49376465 ;; \
      *) echo "no restic checksum for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /tmp/restic.bz2 \
      "https://github.com/restic/restic/releases/download/v${RESTIC_VERSION}/restic_${RESTIC_VERSION}_linux_${arch}.bz2"; \
    echo "${sum}  /tmp/restic.bz2" | sha256sum -c -; \
    bunzip2 -c /tmp/restic.bz2 > /usr/local/bin/restic; rm /tmp/restic.bz2; \
    chmod 755 /usr/local/bin/restic; restic version

# rsync (apt, above) is the third backup type: a plain copy of the catalog on
# another machine (backup_rsync.py, documentation/features/backups.md).

# ── borg: catalog backups, the second type (documentation/features/backups.md) ─
# The official standalone binary, the same 1.4.x the backup host runs
# (documentation/guides/BORG_INSTALL.md): client and server must share a major
# version. Checked against the release's published sha256.
ARG BORG_VERSION=1.4.5
RUN set -e; arch="${TARGETARCH:-$(dpkg --print-architecture)}"; \
    case "$arch" in \
      amd64) asset=borg-linux-glibc231-x86_64; sum=c8457f70660064d0f45b38283ab4cc65b342970012013201d3aec713a75898fb ;; \
      arm64) asset=borg-linux-glibc235-arm64-gh; sum=aae37546320810969d2f3cb41a23ec6b86f7fa1c5f868e18f10285978bc977e7 ;; \
      *) echo "no borg checksum for $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL -o /usr/local/bin/borg \
      "https://github.com/borgbackup/borg/releases/download/${BORG_VERSION}/${asset}"; \
    echo "${sum}  /usr/local/bin/borg" | sha256sum -c -; \
    chmod 755 /usr/local/bin/borg; borg --version

WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────────
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r ./requirements.txt

# ── Application source ─────────────────────────────────────────────────────────
# Copy every top-level module, not a hand-maintained list.
#
# This was an explicit allowlist, and it failed twice in the same way: a new
# module was added beside app.py, the list was not updated, and the container
# died at import with ModuleNotFoundError. An allowlist that must be edited in
# lockstep with every new file is a rebuild-time landmine, and neither the
# rebuild-on-stale check nor a passing test suite on the host can catch it -
# the code is correct, the image is just missing a file.
#
# Everything a workflow imports must be here, because workflows/ is bind-mounted
# live from the host while /app is not: a workflow picks up its new import
# immediately and then cannot find the module inside the container.
#
# This also puts the migrate_*.py scripts in the image for the first time, which
# is where they should be run from - catalog_meta.target_directory holds
# container paths (/catalogs/<name>), so a migration run inside the container
# resolves them directly instead of relying on the host-path fallback in
# migration_utils.py.
#
# .dockerignore keeps out __pycache__, venv, database/ and the stray '=*' files.
# ── Version ───────────────────────────────────────────────────────────────────
# Passed in rather than read from a file: the build context is systems/backend,
# so the repository's VERSION file is outside it and cannot be COPYed, and the
# image carries no .git to ask instead. start-podman.sh reads both and hands
# the answer over. See systems/backend/version.py and CHANGELOG.md.
ARG AIRLOCK_VERSION=unknown
ENV AIRLOCK_VERSION=$AIRLOCK_VERSION

COPY *.py ./
COPY workflows/ ./workflows/

# ── Runtime user, created BEFORE any VOLUME ───────────────────────────────────
# Order matters, and getting it wrong is invisible on one machine and fatal on
# the next. Per OCI semantics, changes to a path made AFTER it is declared as a
# VOLUME are discarded - so a `chown` that follows the declaration may simply
# not be in the image. This built fine on macOS and produced an unwritable
# directory on Ubuntu.
#
# There is no VOLUME declaration any more either. Every run bind-mounts these
# paths, so the declaration bought nothing and cost this: `podman run` with no
# -v creates an ANONYMOUS volume over the path, and under rootless podman that
# volume is owned by the host user, which maps to uid 0 inside the container -
# unwritable by the non-root user this image runs as.
#
# The backend stores its main database under /app/database/catalogs.db, which
# start-podman.sh bind-mounts from <project_root>/database so it survives
# restarts. Catalog target directories are user-defined and mounted at the same
# paths inside the container.
RUN addgroup --gid 1000 appgroup && \
    adduser --uid 1000 --gid 1000 --disabled-password --gecos "" appuser && \
    mkdir -p /app/database && \
    chown -R appuser:appgroup /app && \
    chmod 0775 /app/database

EXPOSE 5000

USER appuser

# Production server. Flask's built-in server (what `python app.py` starts, via
# socketio.run(..., allow_unsafe_werkzeug=True)) is explicitly not for production.
#
# Worker class: gthread, NOT the gevent-websocket worker.
#   app.py creates SocketIO(async_mode='threading'), and that mode is
#   incompatible with the gevent worker. It also spawns real threading.Thread
#   watchers and subprocess.Popen children for workflows, which gevent's
#   monkey-patching would alter in ways that need testing against running
#   services. gthread keeps the existing concurrency model exactly as it is.
#
#   Consequence: Socket.IO negotiates HTTP long-polling rather than websockets.
#   Live workflow progress still works - it is just less efficient than a true
#   websocket. To upgrade later, switch async_mode to 'gevent' AND this worker to
#   geventwebsocket.gunicorn.workers.GeventWebSocketWorker together, then verify
#   workflow subprocesses and progress events still behave.
#
# Exactly ONE worker: workflow execution state and Socket.IO sessions live in
# process memory, so a second worker would see neither.
#
# Long timeout: requests can kick off heavy catalog work before responding.
CMD ["gunicorn", "--worker-class", "gthread", \
     "--workers=1", "--threads=8", "--timeout=300", \
     "--bind=0.0.0.0:5000", "app:app"]
