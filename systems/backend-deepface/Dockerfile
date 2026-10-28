FROM docker.io/library/python:3.12-slim

# ── System dependencies ────────────────────────────────────────────────────────
# libgl1 / libglib2.0-0 – required for OpenCV (cv2) headless operations
RUN apt-get update && apt-get install -y --no-install-recommends \
        libgl1 \
        libglib2.0-0 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────────
COPY requirements.txt ./requirements.txt
# GPU=1 builds the 'gpu' variant (start-podman.sh with AIRLOCK_GPU=1): TensorFlow
# with its CUDA libraries, for a container that gets the NVIDIA GPU through CDI.
# The default image stays CPU-only and small (accelerated-services-spec.md 3.3).
ARG GPU=0
# Two things pip leaves behind, removed in the SAME layer as the install - a
# later RUN that deletes files only hides them, and the image keeps their size:
#
#  * On x86_64 the `tensorflow` wheel carries CUDA support whether or not there
#    is a GPU: 1.9 GB installed. `tensorflow-cpu` is the same release without
#    it, under the same import name - about a gigabyte less. deepface and
#    retina-face name `tensorflow` as a dependency, so pip always installs that
#    one first; it is swapped afterwards, at the same version, and only when a
#    tensorflow-cpu wheel exists for it. arm64 wheels have no CUDA to drop, and
#    the GPU variant needs it.
#  * OpenCV arrives two or three times - opencv-python-headless (asked for
#    here), opencv-python (the GUI build, from deepface 0.0.100 and earlier) and
#    opencv-contrib-python-headless (deepface 0.0.103 and later). Each installs
#    the same cv2 package over the others. Exactly one is kept: the contrib
#    build when something requires it, since it is a superset, else headless.
#  * tensorflow/include: 280 MB of C++ headers, for compiling custom ops. Never
#    read at runtime.
RUN pip install --no-cache-dir -r ./requirements.txt && \
    if pip show opencv-contrib-python-headless >/dev/null 2>&1; then \
      CVPKG=opencv-contrib-python-headless; else CVPKG=opencv-python-headless; fi && \
    CV="$(pip show "$CVPKG" | awk '/^Version:/{print $2}')" && \
    pip uninstall -y opencv-python opencv-contrib-python opencv-python-headless \
                     opencv-contrib-python-headless && \
    pip install --no-cache-dir --no-deps "${CVPKG}==${CV}" && \
    if [ "$GPU" != "1" ] && [ "$(uname -m)" = "x86_64" ]; then \
      TF="$(pip show tensorflow | awk '/^Version:/{print $2}')"; \
      if pip download --no-deps --no-cache-dir -q -d /tmp/tfcpu "tensorflow-cpu==${TF}" >/dev/null 2>&1; then \
        pip uninstall -y tensorflow && \
        pip install --no-cache-dir --no-deps /tmp/tfcpu/tensorflow_cpu-*.whl; \
      else \
        echo "no tensorflow-cpu ${TF} wheel - keeping tensorflow"; \
      fi; \
      rm -rf /tmp/tfcpu; \
    fi && \
    rm -rf /usr/local/lib/python3.12/site-packages/tensorflow/include && \
    python -c "import tensorflow as tf, cv2; print('tensorflow', tf.__version__, '| opencv', cv2.__version__)"
RUN if [ "$GPU" = "1" ]; then \
      TF="$(pip show tensorflow 2>/dev/null | awk '/^Version:/{print $2}')"; \
      pip install --no-cache-dir "tensorflow[and-cuda]==${TF}"; \
    fi

# ── Application source ─────────────────────────────────────────────────────────
COPY app.py ./

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
# uploads, database_images, data and the DeepFace weights cache are all
# bind-mounted by start-podman.sh.
RUN addgroup --gid 1000 appgroup && \
    adduser --uid 1000 --gid 1000 --disabled-password --gecos "" appuser && \
    mkdir -p /app/uploads /app/database_images /app/data /home/appuser/.deepface && \
    chown -R appuser:appgroup /app /home/appuser && \
    chmod 0775 /app/uploads /app/database_images /app/data /home/appuser/.deepface

EXPOSE 5001

USER appuser

ENV HOME=/home/appuser
ENV PYTHONUNBUFFERED=1

# Processes, not threads: OpenCV's FaceDetectorYN carries mutable per-request
# state (setInputSize / setScoreThreshold) and DeepFace caches one detector per
# process, so threads sharing it would race.
#
# NO --preload: it imports the app in the master, starting TensorFlow's and
# OpenCV's thread pools there, and gunicorn then fork()s without exec(). The
# child inherits mutexes held by threads that no longer exist and dies with
# SIGSEGV on first use. Each worker must fork first and import second.
#
# DEEPFACE_WORKERS is settable from the pod manifest; 2 is a conservative
# default for a podman VM, where memory is capped well below the host's.
ENV DEEPFACE_WORKERS=2
CMD ["sh", "-c", "exec gunicorn --workers=${DEEPFACE_WORKERS} --threads=1 --timeout=180 --graceful-timeout=30 --bind=0.0.0.0:5001 app:app"]
