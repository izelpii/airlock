FROM docker.io/library/python:3.12-slim

# ── System dependencies ────────────────────────────────────────────────────────
# libjpeg / zlib – required by Pillow
RUN apt-get update && apt-get install -y --no-install-recommends \
        libjpeg62-turbo \
        zlib1g \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────────
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r ./requirements.txt

# ── Application source ─────────────────────────────────────────────────────────
COPY app.py ./

# ── Runtime configuration ──────────────────────────────────────────────────────
# The app resolves workflow_results.db by walking up from the image path.
# Mount catalog directories at the same paths they have on the host so that
# the database look-up resolves correctly inside the container.
EXPOSE 5003

# Run as non-root
RUN addgroup --gid 1000 appgroup && \
    adduser --uid 1000 --gid 1000 --disabled-password --gecos "" appuser && \
    chown -R appuser:appgroup /app

USER appuser

# Production server rather than Flask's development server.
# Generous timeout: a vision model describing an image can take a while, and the
# default 30s would cut long generations off mid-flight.
CMD ["gunicorn", "--workers=1", "--threads=2", "--timeout=600", \
     "--bind=0.0.0.0:5003", "app:app"]
