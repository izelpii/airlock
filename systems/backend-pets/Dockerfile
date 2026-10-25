FROM docker.io/library/python:3.12-slim

WORKDIR /app

# ── Python dependencies ───────────────────────────────────────────────────────
# No TensorFlow and no torch: both models are ONNX and run under onnxruntime.
# That is why this image is ~400 MB where backend-deepface is 3 GB, and why it
# builds in a minute. See the spec's section 4.1.
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r ./requirements.txt

# ── Application source ────────────────────────────────────────────────────────
COPY app.py ./

EXPOSE 5006

# The weights are NOT baked in. start-podman.sh bind-mounts them, matching
# backend-deepface, so a 37 MB model is not in every image layer and can be
# replaced without a rebuild. The directory has to exist and be writable by the
# non-root user for the mount to land on it.
RUN addgroup --gid 1000 appgroup && \
    adduser --uid 1000 --gid 1000 --disabled-password --gecos "" appuser && \
    mkdir -p /app/weights && \
    chown -R appuser:appgroup /app

USER appuser

ENV PYTHONUNBUFFERED=1
ENV PETS_WEIGHTS_DIR=/app/weights

# Threads, not workers. Each worker would hold its own copy of both ONNX
# sessions, and PETS_CONCURRENCY already bounds how many inferences run at
# once - so extra workers buy memory pressure rather than throughput.
CMD ["sh", "-c", "exec gunicorn --workers=1 --threads=${PETS_THREADS_HTTP:-4} \
     --timeout=180 --graceful-timeout=30 --bind=0.0.0.0:5006 app:app"]
