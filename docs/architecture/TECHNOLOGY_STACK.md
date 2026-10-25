# Technology Stack

> **Updated for v5.6.8 (2026-10-12).** Previously revised at v4.0, when there
> were four Python services run from venvs on Python 3.8. Everything now runs in
> podman containers behind an nginx gateway, on Python 3.12. Versions below come
> from each service's `requirements.txt` / `package.json` / `Dockerfile`.

## Overview

Airlock Photos is a set of containerised services: an nginx gateway, a React
UI, a central Flask backend, seven small single-purpose services (six Python,
one Node) and Ollama on the host. See [ARCHITECTURE.md](ARCHITECTURE.md) for how
they fit together.

---

## Runtime and orchestration

| Technology | Version | Purpose |
|---|---|---|
| **podman** | 4.7+ recommended (4.1+ for GPU via CDI) | Runs every service; on macOS inside a podman machine VM (give it ≥ 6 GB) |
| **nginx** | 1.27 (alpine) | Gateway (`airlock-gateway`): TLS, name-based routing; also serves the built frontend |
| **gunicorn** | gthread workers | WSGI server for every Python service |
| **Python** | 3.12 in containers; ≥ 3.10 for native services | All backend services |
| **Node.js** | 22 | Frontend build stage; `album-gallery` server |
| **Bash** | — | `start-all.sh`, `stop-all.sh`, `deploy/*.sh`, per-service `start-podman.sh` / `start-host.sh` |
| **OpenSSL** | — | Private CA and leaf certificate (`systems/gateway/make-certs.sh`) |
| **SQLite** | from the image's Python | Every database, WAL mode |

---

## Frontend (`systems/frontend` — :3000)

| Technology | Version | Purpose |
|---|---|---|
| **React** | 18.2 | UI framework |
| **Create React App** (`react-scripts`) | 5.0.1 | Build, run inside the image |
| **CoreUI React** | 5 | Components and layout |
| **React Router** | 6.20 | Client-side routing |
| **Axios** | 1.x | API client |
| **Socket.IO client** | 4.x | Live workflow progress |
| **Leaflet / react-leaflet** | 4 | Maps on location and event pages |
| **Recharts** | 3 | Charts (content report, import progress, operations) |

## Album gallery (`systems/album-gallery` — :3001)

| Technology | Version | Purpose |
|---|---|---|
| **Node.js** (`server.mjs`) | 22 | Serves published deployments, password checks |
| **Vite** | 8 | Build of the gallery SPA (`app/`) |
| **React** | 19.2 | Gallery UI |
| **TypeScript** | ~6.0 | Gallery SPA language |
| **PhotoSwipe, react-photo-album** | — | Lightbox and justified layout |

---

## Main backend (`systems/backend` — :5000)

| Technology | Version | Purpose |
|---|---|---|
| **Flask** | 3.0 | REST API |
| **Flask-SocketIO** / python-socketio | 5.3.5 / 5.10 | Live progress (threading async mode) |
| **Flask-CORS** | 4.0 | Origins limited to `AIRLOCK_ALLOWED_ORIGINS` |
| **Werkzeug** (scrypt) | — | Password hashing |
| **Pillow**, **pillow-heif**, **rawpy** | 10.1 / — / — | Images, HEIC, Canon CR3 |
| **PyExifTool** + ExifTool | 0.5.6 | EXIF extraction |
| **pygeohash** | — | Geohash at import, location clustering |
| **NumPy** | < 2 | Face matching as matrix products, clustering |
| **magic-wormhole** | — | Album transfers between installations |
| **requests** | — | Calls to the other services |

## Services

| Service | Port | Main libraries |
|---|---|---|
| `backend-deepface` | 5001 | DeepFace ≥ 0.0.79, tf-keras ≥ 2.20 (TensorFlow ≥ 2.20; `tensorflow[and-cuda]` in the GPU image), OpenCV-headless ≥ 4.8 (YuNet detector). Models: VGG-Face or SFace, chosen per catalog |
| `backend-geoloc` | 5002 | geopy (Nominatim), OSMnx, pandas |
| `backend-vision` | 5003 | ollama client 0.1.6; model `OLLAMA_MODEL` (default `gemma4:latest`) |
| `backend-transcode` | 5004 | Flask 3.0.3, gunicorn 23, ffmpeg/ffprobe (VideoToolbox, NVENC, QSV, VAAPI, V4L2 M2M or libx264) |
| `backend-albums` | 5005 | ollama client; model `ALBUMS_LLM_MODEL` (default `granite3.3:8b`) |
| `backend-pets` | 5006 | onnxruntime ≥ 1.17; YOLOX-Tiny and an animal-identity CLIP-ViT-B/32 (both Apache 2.0) |

---

## Databases

All SQLite, WAL mode, no database server.

| Database | Location | Contents |
|---|---|---|
| `catalogs.db` | `systems/backend/database/` | Catalogs, storage routes, users, sessions, access, alerts, user settings, scheduler lease |
| `workflow_results.db` | `catalogs/<catalog>/` | Everything about one catalog's files: EXIF, derivatives, faces, pets, people, events, locations, albums, workflows and their runs |
| `face_labels.db` | `systems/backend-deepface/` | DeepFace's own label index |

The v4 databases `backend/workflows.db` and `backend/user_settings.db` are no
longer used.

---

## External tools and services

| Tool / service | Used for |
|---|---|
| **Ollama** (host, :11434) | Photo descriptions and album prompts |
| **ExifTool** | EXIF metadata, including HEIC and CR3 |
| **ffmpeg / ffprobe** | Video proxies and posters |
| **OpenStreetMap / Nominatim** | Place names for coordinates |

---

## Platforms

| Platform | Notes |
|---|---|
| macOS, Apple Silicon | deepface and transcode run natively (VideoToolbox for video) |
| macOS, Intel | transcode native; deepface in a container (no TensorFlow ≥ 2.20 for macOS x86_64) |
| Linux x86_64 | Containers; NVIDIA GPU used when a driver and CDI spec are present. Runs the reference deployment (`homeserver`) |
| Linux arm64 (64-bit Raspberry Pi OS) | Containers throughout |
