# Workflow Manager - Startup Guide

This guide explains how to start and stop all services for the Workflow Manager application.

## Prerequisites

Before running the startup script, ensure you have the following installed:

- **Python 3.8+** - For backend services
- **Node.js 16+** and **npm** - For the frontend
- **pip** - Python package manager

## Quick Start

### Starting All Services

To start all backend services and the frontend with a single command:

```bash
./start-all.sh
```

This script will:
1. Create virtual environments for each Python backend (if they don't exist)
2. Install/update Python dependencies from `requirements.txt` files
3. Start all backend services on their respective ports
4. Install npm dependencies for the frontend (if needed)
5. Start the React frontend

### Stopping All Services

To stop all running services:

```bash
./stop-all.sh
```

## Service Ports

The following services will be started:

Services are addressed by name through the gateway (`systems/gateway`), which listens on :80 and
routes by Host header. The ports below still exist, but nothing in the application refers to them —
the gateway config is the only place that maps a name to a port.

| Service | Name | Internal port |
|---|---|---|
| React Frontend | http://airlock.lan | 3000 |
| Main Backend | http://airlock.lan/api · http://backend.airlock.lan | 5000 |
| DeepFace Backend | http://deepface.airlock.lan | 5001 (on the host) |
| GeoLocation Backend | http://airlock.lan/geoloc · http://geoloc.airlock.lan | 5002 |
| Vision Backend | http://airlock.lan/vision · http://vision.airlock.lan | 5003 |
| Albums Backend | http://albums.airlock.lan | 5005 |
| Album Gallery | http://gallery.airlock.lan | 3001 |
| Gateway health | http://airlock.lan/gateway-health | 80 |

The names need `/etc/hosts` entries:

```bash
sudo systems/gateway/make-hosts.sh --install
systems/gateway/make-hosts.sh --check
```

See `docs/specs/redesign-ports-routes-spec.md`.

## Service Descriptions

### Main Backend (`./backend`)
- Core workflow management API
- File operations and catalog management
- EXIF data extraction
- Photo and event management

### DeepFace Backend (`./backend-deepface`)
- Face detection and recognition
- Uses DeepFace library for AI-powered face analysis

### GeoLocation Backend (`./backend-geoloc`)
- Reverse geocoding services
- Location name resolution from GPS coordinates
- Uses OpenStreetMap data

### Vision Backend (`./backend-vision`)
- AI-powered image description generation
- Uses Ollama for vision analysis

### Frontend (`./frontend`)
- React-based web interface
- Provides UI for all workflow management features

## Logs

All service logs are stored in the `./logs` directory with timestamps:
- `Main Backend_YYYYMMDD_HHMMSS.log`
- `DeepFace Backend_YYYYMMDD_HHMMSS.log`
- `GeoLocation Backend_YYYYMMDD_HHMMSS.log`
- `Vision Backend_YYYYMMDD_HHMMSS.log`
- `frontend_YYYYMMDD_HHMMSS.log`

PID files are also stored in `./logs` for process management.

## Troubleshooting

### Port Already in Use

If a port is already in use, the script will skip starting that service. To resolve:

1. Check what's using the port:
   ```bash
   lsof -i :PORT_NUMBER
   ```

2. Stop the conflicting process or use `./stop-all.sh` if it's a previous instance

### Service Fails to Start

1. Check the log file in `./logs` directory for error messages
2. Ensure all dependencies are installed
3. Verify Python and Node.js versions meet requirements

### Manual Service Start

If you need to start a service manually:

**Python Backend:**
```bash
cd backend  # or backend-deepface, backend-geoloc, backend-vision
source venv/bin/activate
python app.py
```

**Frontend:**
```bash
cd frontend
npm start
```

## First Time Setup

On first run, the script will:
- Create Python virtual environments in each backend directory
- Install all Python dependencies
- Install all npm dependencies for the frontend

This may take several minutes depending on your internet connection.

## Development Mode

All services run in development mode by default:
- Python backends use Flask's development server
- Frontend uses React's development server with hot reload

## Production Deployment

For production deployment, consider:
- Using production WSGI servers (gunicorn, uWSGI) for Python backends
- Building the React frontend (`npm run build`) and serving with nginx
- Setting up proper environment variables
- Implementing proper logging and monitoring

## Additional Commands

### Check Running Services
```bash
ps aux | grep -E "python app.py|npm"
```

### View Real-time Logs
```bash
tail -f logs/Main\ Backend_*.log
```

### Clean Up Old Logs
```bash
rm -rf logs/*
```

## Support

For issues or questions, refer to the main project documentation or check individual service README files in their respective directories.

---

## Catalog paths and the container boundary

A catalog's **Target Directory** is stored in `catalog_meta.target_directory` and is an absolute
path *inside the backend container* — for the Family catalog, `/catalogs/family`.

That works because `backend-pod.yaml` mounts the host's `shoebox/catalogs` directory at `/catalogs`.
It also means **the value is environment-specific**: running the backend natively (outside a
container) requires changing it to the host path first, e.g.
`/Users/izelpi/Personal/Development/shoebox/catalogs/family`.

Everything else is portable. Every path stored *inside* `workflow_results.db` is relative to the
catalog root (`Images/2026/...`, `cache/faces-ai/1.jpg`), so moving a catalog to another machine or
mount point needs exactly one change: this single value.

Services that must agree on the `/catalogs` path, because they exchange absolute paths with the
backend:

| Service | Needs the mount | Why |
|---|---|---|
| `backend` | yes, read/write | owns the catalog |
| `backend-vision` | yes, read-only | `/describe` is called with an `image_path` the backend produced |
| `album-gallery` | `<catalog>/cache` read-only | serves the deployed albums and the browser-friendly derivatives |
| `backend-deepface` | **no** | receives image bytes over HTTP; shares no filesystem |
| `backend-geoloc` | no | coordinates in, place names out |
| `backend-albums` | no | prompt in, SQL out |

`backend-deepface` needing no filesystem is what makes it straightforward to run on the host for
Apple GPU acceleration — see `systems/backend-deepface/start-host.sh`.
