# Airlock Photos - Startup Guide

This guide explains how to start and stop all services for Airlock Photos.

## Prerequisites

Before running the startup script, ensure you have the following installed:

- **podman 4.7+** - Runs every service (on macOS, a running `podman machine` with ≥ 6 GB)
- **curl** and **openssl** - Health checks and the gateway's certificate
- **Ollama** on the host - For the vision and albums services
- **Python 3.10+** and **ffmpeg** - Only for `backend-deepface` and
  `backend-transcode` when they run natively; otherwise they run in containers

On a new machine, run `./deploy/deploy.sh` once first: it checks these, writes
`deploy/airlock.env`, creates the bind-mounted directories and the gateway's
certificate. See [deploy/README.md](../../deploy/README.md).

## Quick Start

### Starting All Services

To start all services with a single command:

```bash
./start-all.sh
```

This script will:
1. Check the preflight: podman, and Ollama on the host (`localhost:11434`)
2. Start `backend-deepface` and `backend-transcode` natively, in a container or
   in a GPU container, as `AIRLOCK_DEEPFACE_MODE` / `AIRLOCK_TRANSCODE_MODE`
   resolve (`auto | host | container | gpu | off`)
3. Start the gateway, then the containers in dependency order: geoloc, pets
   (only when some catalog has pets on), vision, albums, backend, album-gallery,
   frontend
4. Rebuild any image whose sources are newer than it, and check each service's
   health through the gateway names

`./start-all.sh --build` rebuilds every image first; `--build=frontend,backend`
rebuilds just those; `--no-deepface` skips deepface; `--help` lists the names.

Restarting replaces every container, including the backend: running workflows
are interrupted and album transfer codes still waiting end. Check the Workflows
page and Albums ▸ Transfers first, or restart only the service you changed.

### Stopping All Services

To stop all running services:

```bash
./stop-all.sh
```

`stop-all.sh` does not stop `backend-pets` yet; stop it with
`podman stop backend-pets` if it is running.

## Service Ports

The following services will be started:

Services are addressed by name through the gateway (`systems/gateway`), which listens on :443 for
the browser (:80 redirects to it) and on :80 for the internal service names, routing by Host
header. The ports below still exist, but nothing in the application refers to them — the gateway
config is the only place that maps a name to a port.

| Service | Name | Internal port |
|---|---|---|
| React Frontend | https://airlock.lan | 3000 |
| Main Backend | https://airlock.lan/api · http://backend.airlock.lan | 5000 |
| DeepFace Backend | http://deepface.airlock.lan | 5001 (native or container) |
| GeoLocation Backend | https://airlock.lan/geoloc · http://geoloc.airlock.lan | 5002 |
| Vision Backend | https://airlock.lan/vision · http://vision.airlock.lan | 5003 |
| Transcode Backend | none — the backend calls http://transcode.airlock.lan:5004 directly | 5004 (native or container) |
| Albums Backend | http://albums.airlock.lan | 5005 |
| Pets Backend | http://pets.airlock.lan | 5006 (only when a catalog has pets on) |
| Album Gallery | https://airlock.lan/gallery · https://&lt;catalog&gt;.airlock.lan · http://gallery.airlock.lan | 3001 |
| Ollama (host) | http://ollama.airlock.lan | 11434 |
| Gateway health | http://airlock.lan/gateway-health | 80 |

The names need `/etc/hosts` entries:

```bash
sudo systems/gateway/make-hosts.sh --install
systems/gateway/make-hosts.sh --check
```

See [redesign-ports-routes-spec.md](../specs/redesign-ports-routes-spec.md).

## Service Descriptions

### Main Backend (`systems/backend`)
- REST API, sign-in and per-catalog access
- Catalogs, storage routes, imports and the workflow queue (workflows run as subprocesses)
- Photos, videos, events, locations, people, pets, albums, search, photo editing
- Catalog backups (restic, borg, rsync), publishing deployments to a server,
  album transfers over magic-wormhole

### DeepFace Backend (`systems/backend-deepface`)
- Face detection (YuNet) and embeddings (VGG-Face or SFace, chosen per catalog)
- Uses the DeepFace library

### GeoLocation Backend (`systems/backend-geoloc`)
- Place names near a coordinate (`GET /poi`)
- Uses OpenStreetMap data (Nominatim, OSMnx)

### Vision Backend (`systems/backend-vision`)
- AI-powered image description generation
- Uses Ollama for vision analysis (`OLLAMA_MODEL`)

### Transcode Backend (`systems/backend-transcode`)
- Browser-playable video copies and poster frames
- ffmpeg, with a hardware encoder where there is one

### Albums Backend (`systems/backend-albums`)
- Turns an album's prompt into a SQL filter, using Ollama (`ALBUMS_LLM_MODEL`)

### Pets Backend (`systems/backend-pets`)
- Animal detection and identity embeddings (ONNX), for catalogs with pets on

### Album Gallery (`systems/album-gallery`)
- Serves published album deployments

### Frontend (`systems/frontend`)
- React-based web interface, built inside its image and served by nginx

## Logs

All service logs are stored in the `./logs` directory, one file per service,
named after it:
- `backend.log`
- `backend-deepface.log`
- `backend-geoloc.log`
- `backend-vision.log`
- `gateway.log`, `frontend.log`, and so on

`backend-deepface` and `backend-transcode` keep their previous log as
`<name>.log.1`. PID files are also stored in `./logs` for the services running
natively.

## Troubleshooting

### Port Already in Use

If a native service's port already answers, the script leaves it running
rather than starting another. The gateway publishes ports 80 and 443, which
rootless podman refuses while the unprivileged port floor is above 80;
`./deploy/deploy.sh` checks it and says how to lower it. To see what is using
a port:

1. Check what's using the port:
   ```bash
   lsof -i :PORT_NUMBER
   ```

2. Stop the conflicting process or use `./stop-all.sh` if it's a previous instance

### Service Fails to Start

1. Check the log file in `./logs` directory for error messages
2. Run `./deploy/deploy.sh --check` to see what this machine is missing
3. Run `./deploy/status.sh` to see which services are up and answering

### Manual Service Start

If you need to start a service manually:

**A container:**
```bash
cd systems/backend  # or backend-geoloc, backend-vision, frontend, ...
./start-podman.sh   # --build rebuilds the image
```

**Natively (deepface and transcode only):**
```bash
cd systems/backend-deepface  # or backend-transcode
./start-host.sh
```

## First Time Setup

On first run:
- `./deploy/deploy.sh` builds the Python virtual environments for the services
  that will run natively, and caches the face models
- `./start-all.sh --build` builds every image

This may take several minutes depending on your internet connection. Then open
https://airlock.lan and create the administrator account.

## Development Mode

All services run with production servers by default:
- Python services run under gunicorn
- The frontend is a production build served by nginx; a change to
  `systems/frontend/src` needs `./start-all.sh --build=frontend`
- `systems/backend/workflows/` is bind-mounted, so editing a workflow needs no rebuild

For a frontend development server outside the container, `cd systems/frontend`,
`npm install`, `npm start`.

## Production Deployment

For production deployment:
- Run `./deploy/deploy.sh` on the machine, then `./start-all.sh --build`
- Keep per-machine settings in `deploy/airlock.env` (template `deploy/airlock.env.example`)
- Install the gateway's CA certificate on each device that will use it
- Set up catalog backups (the catalog's Backup tab)

## Additional Commands

### Check Running Services
```bash
./deploy/status.sh
podman ps
```

### View Real-time Logs
```bash
tail -f logs/backend.log
```

### Clean Up Old Logs
```bash
rm -f logs/*.log.1
```

## Support

For issues or questions, refer to the main [README](../../README.md) and the documents beside this one, or run `./deploy/status.sh` and check the service's log in `./logs`.

---

## Catalog paths and the container boundary

A catalog's **Target Directory** is stored in `catalog_meta.target_directory` and is an absolute
path *inside the backend container* — for the Family catalog, `/catalogs/family`.

That works because the backend's `start-podman.sh` mounts the host's catalogs directory
(`CATALOGS_HOST_DIR` in `deploy/airlock.env`, default `<repository>/catalogs`) at `/catalogs`.
It also means **the value is environment-specific**: running the backend natively (outside a
container) requires changing it to the host path first, e.g. `<CATALOGS_HOST_DIR>/family`.

Everything else is portable. Every path stored *inside* `workflow_results.db` is relative to the
catalog root (`Images/2026/...`, `cache/faces-ai/1.jpg`), so moving a catalog to another machine or
mount point needs exactly one change: this single value.

Services that must agree on the `/catalogs` path, because they exchange absolute paths with the
backend:

| Service | Needs the mount | Why |
|---|---|---|
| `backend` | yes, read/write | owns the catalog |
| `backend-vision` | yes, read-write | `/describe` is called with an `image_path` the backend produced |
| `backend-transcode` | yes, read/write | reads a video by catalog-relative path and writes its proxy and poster under the catalog's `cache/` |
| `album-gallery` | yes, read-only | serves the deployed albums and the browser-friendly derivatives from `<catalog>/cache` |
| `backend-deepface` | **no** | receives image bytes over HTTP; shares no filesystem |
| `backend-pets` | no | receives image bytes over HTTP |
| `backend-geoloc` | no | coordinates in, place names out |
| `backend-albums` | no | prompt in, SQL out |

`backend-deepface` needing no filesystem is what makes it straightforward to run on the host for
Apple GPU acceleration — see `systems/backend-deepface/start-host.sh`.
