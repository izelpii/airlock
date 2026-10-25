FROM docker.io/library/python:3.12-slim

# ── System dependencies ────────────────────────────────────────────────────────
# libspatialindex – required by osmnx/rtree for spatial indexing
RUN apt-get update && apt-get install -y --no-install-recommends \
        libspatialindex-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# ── Python dependencies ────────────────────────────────────────────────────────
COPY requirements.txt ./requirements.txt
RUN pip install --no-cache-dir -r ./requirements.txt

# ── Application source ─────────────────────────────────────────────────────────
COPY app.py ./

EXPOSE 5002

# Run as non-root
RUN addgroup --gid 1000 appgroup && \
    adduser --uid 1000 --gid 1000 --disabled-password --gecos "" appuser && \
    chown -R appuser:appgroup /app

USER appuser

# Production server rather than Flask's development server.
# Threads (not extra workers) because the work is network-bound on Nominatim and
# the app sleeps 1s between requests to respect its rate limit; a single process
# also keeps the OSMnx cache coherent.
CMD ["gunicorn", "--workers=1", "--threads=4", "--timeout=120", \
     "--bind=0.0.0.0:5002", "app:app"]
