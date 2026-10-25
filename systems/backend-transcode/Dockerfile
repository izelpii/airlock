# backend-transcode - video previews as a service.
# documentation/specs/accelerated-services-spec.md, section 3.2.
#
# Debian's ffmpeg is built with NVENC/NVDEC support (it loads the NVIDIA driver
# libraries at run time), so this one image serves both modes: CPU-only, and
# with the GPU passed in (podman run --device nvidia.com/gpu=all), where the
# CDI spec injects the driver. Without a GPU, h264_nvenc is listed but fails its
# one-frame probe, and the service uses libx264.
FROM docker.io/library/python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# video_proxy.py is vendored from backend/workflows/lib by start-podman.sh.
COPY app.py video_proxy.py ./

RUN addgroup --gid 1000 appgroup && \
    adduser --uid 1000 --gid 1000 --disabled-password --gecos "" appuser && \
    chown -R appuser:appgroup /app
USER appuser

ENV CATALOGS_ROOT=/catalogs PORT=5004
EXPOSE 5004
# Threads, not processes: each request mostly waits on its ffmpeg child, and
# the job limit (TRANSCODE_JOBS) is a semaphore inside one process. The long
# timeout is for a long video encoded in software.
CMD ["sh", "-c", "exec gunicorn --worker-class gthread --workers=1 --threads=8 --timeout=7200 --bind=0.0.0.0:${PORT} app:app"]
