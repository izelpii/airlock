# System Architecture

> **Updated for v6.4.1 (2026-10-25).** Previously revised at v5.6.8
> (2026-10-12). Since then the backend gained catalog backups, search, photo
> editing, publishing to a server, album transfers to several people at once,
> overnight scheduling and a license notice; the UI gained the Dashboard, a
> calendar and account pages. The [version table](#version-change) at the end
> lists where each shows up here; `CHANGELOG.md` has the detail and the reasons.

## Overview

**Airlock Photos** (short name `airlock`, formerly Shoebox) is a self-hosted
photo and video library. It runs as a set of podman containers behind one nginx
gateway. A React UI talks to a central Flask backend, which owns the databases
and runs workflows; the workflows call small single-purpose services for faces,
pets, video and geocoding. Ollama runs on the host and supplies the language
and vision models.

The backend also reaches **outside** the machine, always on the owner's say-so:
to another machine on the LAN for catalog backups (restic, borg or rsync over
SSH), to a public server to publish an album gallery (rsync over SSH), and to
the public magic-wormhole relays to send an album to another Airlock.

Everything is addressed **by name through the gateway** (`airlock.lan` and
`<service>.airlock.lan`). The gateway config is the only place that maps a name
to a port, so nothing else in the application refers to a port number.

---

## High-Level Architecture

```
                           Browser (any device on the LAN)
                                      │  https://airlock.lan
                                      │  https://<catalog>.airlock.lan/<album>
                                      ▼
╔═══════════════════════════════════════════════════════════════════════════╗
║  airlock-gateway  (nginx, :80 → 301 to :443, TLS from a private CA)       ║
║                                                                           ║
║   /              → frontend        :3000                                  ║
║   /api/ /socket.io/ → backend      :5000                                  ║
║   /gallery/, <catalog>.airlock.lan → album-gallery :3001                  ║
║   /vision/ /geoloc/ → vision :5003, geoloc :5002                          ║
║   <svc>.airlock.lan (plain HTTP) → each service, for container-to-        ║
║                                    container calls                        ║
╚════════╤══════════════════════════════════╤═══════════════════════════════╝
         │                                  │
         ▼                                  ▼
┌──────────────────────┐        ┌───────────────────────────────────────────┐
│ frontend  :3000      │  REST  │ backend (container workflow-backend) :5000│
│ React 18 SPA (CRA),  │──────► │ Flask + Flask-SocketIO, gunicorn gthread  │
│ served by nginx      │ Socket │ 1 worker × 8 threads                      │
└──────────────────────┘  .IO   │                                           │
                                │ • REST API (~270 routes), auth, sessions  │
┌──────────────────────┐        │ • workflow scheduler + queue              │
│ album-gallery :3001  │        │ • workflows run as subprocesses           │
│ Node 22 server.mjs + │        │ • backup scheduler (restic/borg/rsync)    │
│ Vite/React 19 SPA    │        │ • publishes deployments, here and remote  │
│ serves published     │        │ • album transfers (wormhole subprocesses) │
│ deployments          │        │ • owns catalogs.db + every catalog's DB   │
└──────────────────────┘        └──┬──────────┬──────────┬─────────┬────────┘
                                   │          │          │         │
                                   ▼          ▼          ▼         ▼
                         ┌────────────┐ ┌──────────┐ ┌────────┐ ┌──────────┐
                         │ deepface   │ │ pets     │ │transcode│ │ geoloc   │
                         │ :5001      │ │ :5006    │ │ :5004   │ │ :5002    │
                         │ YuNet      │ │ YOLOX-   │ │ ffmpeg, │ │ Nominatim│
                         │ detection, │ │ Tiny +   │ │ hardware│ │ / OSMnx  │
                         │ VGG-Face / │ │ animal   │ │ encoder │ │ place    │
                         │ SFace      │ │ CLIP     │ │ if any  │ │ names    │
                         │ embeddings │ │ (ONNX)   │ │         │ │          │
                         └────────────┘ └──────────┘ └────────┘ └──────────┘
                         ┌────────────┐ ┌──────────┐      ┌──────────────────┐
                         │ vision     │ │ albums   │ ───► │ Ollama (host)    │
                         │ :5003      │ │ :5005    │      │ :11434           │
                         │ /describe  │ │ prompt → │      │ ollama.airlock.lan│
                         │ (from UI)  │ │ SQL WHERE│      └──────────────────┘
                         └────────────┘ └──────────┘

  Outside this machine, reached by the backend only:
   ┌─────────────────────────┐ ┌──────────────────────────┐ ┌───────────────────────┐
   │ backup host (backup-host)     │ │ publish server (e.g. EC2)│ │ magic-wormhole relays │
   │ restic over SFTP, borg, │ │ rsync + docker compose   │ │ public rendezvous and │
   │ rsync - a locked-down   │ │ over SSH; Caddy + the    │ │ transit, for album    │
   │ SSH key per backup type │ │ gallery image serve it   │ │ transfers between     │
   └─────────────────────────┘ └──────────────────────────┘ │ Airlock installs      │
                                                            └───────────────────────┘
```

`backend-transcode` is the one service the gateway does not route: the backend
calls `http://transcode.airlock.lan:5004` directly. `backend-pets` is started
only when some catalog has pets turned on.

---

## Services

| Service (`systems/…`) | Port | Gateway name | Runs as | Purpose |
|---|---|---|---|---|
| `gateway` | 80, 443 | `airlock.lan` | container `airlock-gateway` | TLS, routing |
| `frontend` | 3000 | `/` on `airlock.lan` | container `workflow-frontend` | Main UI |
| `backend` | 5000 | `/api/`, `backend.airlock.lan` | container `workflow-backend` | API, auth, workflows, data |
| `backend-deepface` | 5001 | `deepface.airlock.lan` | native or container (see below) | Face detection and embeddings |
| `backend-geoloc` | 5002 | `/geoloc/`, `geoloc.airlock.lan` | container | Place names near a coordinate |
| `backend-vision` | 5003 | `/vision/`, `vision.airlock.lan` | container | Photo descriptions via Ollama |
| `backend-transcode` | 5004 | none (direct) | native or container | Browser copies and poster frames for video |
| `backend-albums` | 5005 | `albums.airlock.lan` | container | Turns an album prompt into a SQL filter via Ollama |
| `backend-pets` | 5006 | `pets.airlock.lan` | container, only if a catalog uses pets | Animal detection and identity embeddings |
| `album-gallery` | 3001 | `/gallery/`, `gallery.airlock.lan`, `<catalog>.airlock.lan` | container | Serves published album deployments |
| Ollama | 11434 | `ollama.airlock.lan` | host process | LLM / vision models |

Every service has a `start-podman.sh` (which runs `podman run --rm --replace`)
and a `*-pod.yaml`. `start-all.sh` starts them in dependency order — deepface,
transcode, gateway, geoloc, pets, vision, albums, backend, album-gallery,
frontend — and checks health through the gateway names. Each image rebuilds
itself when its sources are newer than it; `--build[=list]` forces it.

### Native, container or GPU (accelerated services)

`backend-deepface` and `backend-transcode` can run outside podman's VM, which is
the only way to reach the host's hardware. `AIRLOCK_DEEPFACE_MODE` and
`AIRLOCK_TRANSCODE_MODE` take `auto | host | container | gpu | off`;
`deploy/airlock-env.sh` (`airlock_service_mode`) resolves `auto`:

| Platform | deepface | transcode |
|---|---|---|
| macOS, Apple Silicon | native (CPU¹) | native (VideoToolbox) |
| macOS, Intel | container — no TensorFlow ≥ 2.20 wheels for macOS x86_64 | native (VideoToolbox) |
| Linux x86_64 | GPU container if an NVIDIA CDI spec exists, else container | same |
| Linux arm64 (Pi) | container | container |

`host` falls back to `container` when the host lacks Python ≥ 3.10 or ffmpeg.
Native services are started with `nohup` from `start-host.sh` and tracked by
pid files in `logs/`.

¹ `tensorflow-metal` supports TensorFlow ≤ 2.18 only, so it is deliberately not
installed; detection (YuNet, OpenCV DNN) never used Metal anyway. See
`documentation/features/INGESTION_PERFORMANCE.md`.

Transcode tries `h264_videotoolbox`, `h264_nvenc`, `h264_qsv`, `h264_vaapi`,
`h264_v4l2m2m` and then `libx264`. With `AIRLOCK_TRANSCODE_MODE=off` the import
makes video previews itself.

---

## Component Breakdown

### Frontend (`systems/frontend`)

React 18 with CoreUI 5, React Router 6, Axios, Socket.IO client, Leaflet,
Recharts 3 and react-pro-sidebar, built with Create React App **inside a
two-stage image** and served by nginx. Nothing is mounted from the host, so a
change to `src/` needs `./start-all.sh --build=frontend`. All service addresses
come from `src/config.js` and are relative paths through the gateway.

Public images (card bands, logos) are requested with the build's id as `?v=`
(`assetUrl()`); nginx serves a versioned request as cacheable for good and an
unversioned one as `no-cache`, so a new build's images show without a hard
refresh (v6.2.1).

Main areas (routes in `App.js`):

| Area | Routes |
|---|---|
| Home | `/` — the Dashboard: totals, the last twelve months, the library over time, what needs attention, activity, coverage, "on this day", storage and backups |
| Library | `/photos`, `/videos`, `/events`, `/locations`, `/locations-map`, `/calendar-view`, `/catalog-files`, `/content-report`, `/removed-files` |
| Search | `/search`, `/saved-searches`, `/saved-searches/:id` |
| People and pets | `/people`, `/identify-people`, `/identify-groups`, `/pets`, `/identify-pets` |
| Albums | `/albums`, `/albums/deployments`, `/albums/transfers`, `/albums/transfers/:id`, `/albums/transfers/batches/:id`, `/albums/systems` |
| Operations | `/workflows` (queue and pipeline diagram, with the catalog's backups), `/workflow-executions`, `/import-progress`, `/operations` (catalog report), `/catalogs` (with the Backup tab), `/alerts` |
| Accounts | `/users`, `/users/:id` (each account's own page), `/admin`, `/settings` |

In the sidebar, **Deployments** and **Workflows** sit under *Systems*; Albums
holds the albums themselves and Transfers.

The version badge beside the logo reads `/api/version`; without a valid license
it is followed by a red *Unlicensed - No commercial use*, and with one the left
panel ends with *Licensed to: …* ([License notice](#license-notice)).

Large galleries page on the server: Photos loads 200 items around what is on
screen, Identify people loads counts and then one person's page, locations
and pets page with real totals (v5.3–v5.5).

### Main Backend (`systems/backend`)

Flask 3 under gunicorn (one worker, eight threads), with Flask-SocketIO in
threading mode for live progress. `app.py` holds the routes (266); the rest of
the directory holds the domain modules and the `migrate_*.py` scripts (42):

| Concern | Modules |
|---|---|
| Accounts, catalogs, storage | `auth.py`, `manage_users.py`, `catalog.py`, `catalog_layout.py`, `storage_routes.py`, `storage_alerts.py`, `slugs.py`, `env_compat.py` |
| Faces, pets, places | `embedding_models.py`, `face_recognition_schema.py`, `face_best_match.py`, `rescan_faces.py`, `pet_schema.py`, `location_schema.py` |
| Workflows | `workflow_scheduler.py`, `workflow_queue_schema.py`, `workflow_triggers.py`, `workflow_defaults.py`, `import_golden.py`, `file_manager.py`, `media_types.py` |
| Reports and views | `dashboard.py`, `content_report.py`, `calendar_counts.py`, `card_decorators.py` |
| Search | `search.py`, `search_index.py`, `saved_searches.py` |
| Photo editing | `photo_edits.py` |
| Albums and publishing | `album_templates.py`, `deployment_pack.py`, `publish.py`, `publish_ssh.py` |
| Album transfers | `wormhole_transfer.py`, `wormhole_runner.py` (the subprocess) |
| Backups | `backups.py`, `backup_restic.py`, `backup_borg.py`, `backup_rsync.py`, `backup_ssh.py`, `backup_secrets.py` |
| Version and license | `version.py`, `airlock_license.py` |

Responsibilities:

1. **Accounts and access** — users, per-catalog roles, sessions (below).
2. **Catalogs and storage routes** — where files come from and where they live.
3. **Workflows** — queueing, scheduling and running them; progress over Socket.IO.
4. **Library API** — files, photos, videos, events, locations, people, pets,
   faces, tags, albums, deployments, transfers, alerts, version.
5. **Serving media** — originals and cached derivatives, by catalog-relative path.
6. **Publishing** — writing album deployments that `album-gallery` serves, and
   sending them to a server of the owner's.
7. **Backups** — each catalog's database and files to another machine, on a
   schedule.
8. **Album transfers** — sending and receiving albums over magic-wormhole.

Besides the request threads, the process runs background threads started at
import: the workflow scheduler, `backup-scheduler`, `saved-search-sweep`,
`storage-monitor`, and three start-up passes — `photo-edit-resume` (finishes
rebuilds a restart cut short), `publish-interrupted` and `wormhole-orphans`
(mark runs and transfers the previous process left as interrupted, and stop
their leftover subprocesses).

Opening a catalog's database no longer runs schema setup on every request:
`init_db()` is guarded by `PRAGMA schema_version`, and WAL is set once per
process (v5.4.1). Expensive indexes are built by migration scripts with the
backend stopped, never inside a request (v5.4.4).

### Authentication

Sign-in is on by default (`AIRLOCK_AUTH_REQUIRED=1`; read only through
`auth.enabled_for(app)`).

- Users, sessions and per-catalog access live in `catalogs.db`. Passwords are
  scrypt hashes (minimum 12 characters); sessions last 30 days.
- Roles are **viewer, editor, owner**, granted per catalog.
- The cookie is `airlock_session` (HttpOnly, SameSite=Lax, Secure behind
  HTTPS).
- Only login, session, first-run setup and `/gateway-health` are public. With
  no users yet, the UI offers first-run setup.
- `systems/backend/manage_users.py` manages users from the command line.

### Workflow engine

Workflow scripts live in `systems/backend/workflows/` (bind-mounted, so editing
one needs no rebuild); each catalog's set is seeded from `defaults.json`.

- `workflow_scheduler.py` is a thread in the backend that drains each catalog's
  `workflow_queue`: at most `MAX_CONCURRENT_WORKFLOWS` (2) at once, never two
  whose write sets overlap, guarded by a lease in `catalogs.db`.
- `DEPENDS_ON` in `workflow_triggers.py` chains workflows (one level deep).
- Each run is a **subprocess** (`python -u <script> --execution-id …
  --target-dir … --catalog-id …`) so it can be stopped; its pid is recorded.
- Runs are recorded in `workflow_executions` in the catalog's database and
  reported over Socket.IO as `workflow_progress`.
- A queue row can wait: `not_before` delays it, and `workflow_triggers.NIGHTLY`
  sends some triggers to the next night instead. Tagging a face queues Match
  Faces by Appearance for 03:00 (`NIGHTLY_LEARNING_AT`, `NIGHTLY_LEARNING_TZ`);
  the other face workflows it triggers wait five quiet minutes. Run on the
  Workflows page, or an import finishing, still starts it at once (v6.2.3).
- Restarting the backend container interrupts running workflows **and ends album
  transfer codes still waiting**; check both before a deploy.

The Workflows page draws the pipeline from `DEPENDS_ON` (`/workflow-graph`), and
the catalog's backups beside it in a row of their own, connected to nothing:
backups run on their own schedule, never through the queue (v6.3.2).

| Order | Script | Name |
|---|---|---|
| 11 | `io_picture_import_v2_workflow.py` | Import Files (default for new storage routes) |
| 10 | `io_picture_import_workflow.py` | Legacy Two-Stage Import |
| 20 | `ev_extract_events.py` | Create Events |
| 25 | `ge_extract_locations.py` | Extract Locations |
| 26 | `ge_resolve_locations.py` | Resolve Locations |
| 30 | `ai_auto_detect_faces_workflow.py` | Find Faces (pet detection rides along) |
| 35 | `ai_embed_faces.py` | Measure Faces |
| 40 | `ai_ingest_manual_faces.py` | Learn Tagged Faces |
| 50 | `ai_face_recognition_workflow.py` | Match Faces by Appearance |
| 70 | `ai_contextual_recognition_workflow.py` | Re-rank Matches by Context |
| 80 | `pe_identify_pets.py` | Measure and Match Pets (pets only) |
| 90 | `pe_cluster_pets.py` | Group Unnamed Pets (pets only) |

Import workflows are created per storage route rather than seeded, and every
catalog also always has **Import transfers**: an Import workflow reading its
own `transfers/`, started when an album transfer completes. It copies whatever
the catalog's mode, and moves each imported album to `transfers/consumed/`
(`documentation/features/transfers-import.md`).
`ai_ml_faces_recognition_workflow.py` (Match Faces Within Events) is retired and
removed from catalogs automatically (v5.5.3); `workflows/legacy/` holds older
scripts.

### AI and media services

- **backend-deepface** — YuNet detection (OpenCV DNN) and embeddings via
  DeepFace with detection skipped (`/api/upload`, `/api/embed`,
  `/api/compare-multiple`). The embedding model is chosen **per catalog** from
  the registry in `systems/backend/embedding_models.py`: VGG-Face (id 1,
  4096-d, non-commercial weights) or SFace (id 2, 128-d, Apache 2.0). Every
  stored embedding records its model, and comparisons between models are
  refused. `tools/migrate_embedding_model.py` switches a catalog. Workers are
  sized to the memory available.
- **backend-pets** — YOLOX-Tiny finds animals; a CLIP-ViT fine-tuned for animal
  identity (512-d, ONNX) tells them apart. Chosen per catalog — on by default
  for a new one — and a catalog with pets off costs nothing but empty tables:
  no calls, and no container unless some other catalog has pets on.
- **backend-transcode** — browser-playable video copies and poster frames on
  catalog-relative paths.
- **backend-geoloc** — `GET /poi` returns named places near a coordinate
  (Nominatim with offset probes, OSMnx), cached on disk.
- **backend-vision** — `/describe` returns a description of a photo from an
  Ollama vision model (`OLLAMA_MODEL`, default `gemma4:latest`). Called from the
  Photos and Event pages.
- **backend-albums** — `/translate` turns an album's natural-language prompt
  into a SQL `WHERE` clause using `ALBUMS_LLM_MODEL` (default `granite3.3:8b`).

### Album gallery and deployments

Publishing an album writes a **deployment** under the catalog's
`cache/Deployments/<slug>/` (symlinks into `cache/Albums`, optional password in
`.auth.json`). `album-gallery` (Node 22 `server.mjs` plus a Vite/React 19 SPA)
serves it at `https://<catalog>.airlock.lan/<slug>` or `/gallery/…`.
`tools/pack_deployment.py` turns a deployment into a self-contained tarball
(compose file and Caddyfile) that runs anywhere Docker does. Albums move between
installations with magic-wormhole (`wormhole_transfer.py`).

### Backups

A **backup card** belongs to one catalog and copies its database and files to
another machine on a schedule (`backups.py`; `docs/features/backups.md`).
The backend runs the tool itself, from inside its container — restic, borg and
rsync are installed in the backend image — so the catalogs are read where they
live and nothing else needs installing on this host.

| Type | Module | On the far side |
|---|---|---|
| restic | `backup_restic.py` | A repository over SFTP (backup-host: `/media/usb1/backup`); encrypted, deduplicated, versioned |
| borg | `backup_borg.py` | `borg serve` over SSH (backup-host: `/media/usb1/borg`); same major version on both ends |
| rsync | `backup_rsync.py` | A plain copy, newest only (backup-host: `/media/usb1/rsync`, an `rrsync`-locked key) |

- The live `workflow_results.db` is copied first, as
  `cache/backup/workflow_results.snapshot.db` (with `catalogs.snapshot.db`), so
  the backup holds a consistent database rather than one mid-write.
- `backup-scheduler` starts due backups; **Back up now** and **Verify** run the
  same code. Progress is reported live; a stale restic lock is cleared before a
  run, and exFAT sources use `--ignore-inode` so unchanged files are skipped.
- The installation has **one SSH key per backup type** — restic, borg, rsync —
  in `database/backup-ssh/` (`backup_ssh.py`). Each is installed on the backup
  host restricted to its one command (`sftp-server`, `borg serve`, `rrsync`),
  which is why they are three: an `authorized_keys` line forces one command. A
  backup host's own key is pinned the first time it is tested. Repository passwords and cloud keys
  are encrypted at rest with a key derived from `database/secret_key`
  (`backup_secrets.py`) and are never returned to the browser.
- Backrest is not involved any more (v5.9.0): it runs on backup-host only, as an
  independent second view of the restic repository.

### Search

`/search` is one box plus filters (`search.py`;
`documentation/architecture/search-spec.md`): words and `key:value` keywords in
the box, filter parameters beside it, one list of results, keyset-paged with a
capped count. A result is a catalog **file** — what Photos and Videos list, one
per RAW+JPG pair — or a **duplicate**: a copy an import turned away
(`file_operations`), shown as a hit pointing to the catalog file it copies.
Names are matched through an FTS5 index (`search_names`, `search_state`,
`search_index.py`). A search can be **saved** (`saved_searches`,
`saved_search_files`), shared within the catalog like Events and run again;
`saved-search-sweep` deletes idle ones every six hours.

### Photo editing

A photo can be turned or flipped (`photo_edits.py`). The **original is never
written to**: the edit is stored in `photo_edits` (history in
`photo_edit_history`), the face and pet boxes are moved in the same
transaction, and the photo's derivatives — every display rung, both thumbnails
and its face crops — are rebuilt from the original with the edit applied.
`photo-edit-resume` finishes rebuilds a restart interrupted.

### Publishing to a server

Besides being served at home by `album-gallery`, a deployment can be published
to a server the owner runs — an EC2 instance, say
(`docs/guides/PUBLISH_TO_EC2.md`; `publish.py`):

- The deployment is packed into a staged **bundle** (`deployment_pack.py`, the
  same code as `tools/pack_deployment.py`): the album derivatives with their
  links resolved, `compose.yml`, a `Caddyfile` for the server's domain, and a
  README. The gallery image is not in the bundle: it is built here and loaded on
  the server once (`docker load`, EC2 guide step 3), and updated separately.
- **Preview** is an `rsync --dry-run`: files to send, to change and to delete,
  and the bytes. **Publish** sends in three passes — media, then album data,
  then deletions — so a visitor never sees a manifest naming a missing file,
  then runs `docker compose up -d` if `compose.yml` or the `Caddyfile` changed,
  and checks the gallery answers. `.env` on the server is never touched.
- Servers (`deployment_servers`) and runs (`deployment_publish_runs`) live in
  the catalog's database; each server has its own SSH key
  (`database/publish-ssh/`) and a pinned host key. Owner only.

### Album transfers

An album's **originals** go to another Airlock over magic-wormhole
(`wormhole_transfer.py`; `documentation/specs/wormhole-album-transfer-spec.md`).
Each transfer is a subprocess, `wormhole_runner.py`, with its own Twisted
reactor, that writes JSON events on stdout and touches no database; a watcher
thread in the backend keeps the rows and the browser current. One code opens a
dilated session with a subchannel per file; resume is by content (SHA-256), and
a received album appears in `<catalog>/transfers/<id>` only once every file has
arrived and been checked.

- App id `airlock.app/album-transfer`, subprotocol `airlock-album-transfer`
  (both `shoebox-…` until v6.3.1): both ends must be Airlock at v6.3.1 or later.
  The standard `wormhole receive` client cannot receive these.
- **Mass transfer** (v6.3.0; `album-mass-transfer-spec.md`): one album to 2–20
  people, each with their own code and an optional label, all ordinary
  transfers in one `wormhole_transfer_batches` group with one deadline of at
  most three days. The runner ends a code nobody has used by the deadline,
  re-reading a per-group file so a moved deadline is honoured; a transfer
  already under way is never cut off.

### License notice

A copy can carry a signed license naming who it is licensed to
(`airlock_license.py`): a JSON file with an Ed25519 signature, in the
repository's `license/` folder, mounted read-only at `/license`. The backend
holds only the public key, re-reads the file when it changes, and reports the
result in `/api/version`. It is a notice, never a lock — nothing is disabled
with or without one. Licenses are issued with a tool kept outside the
repository; license files are git-ignored.

---

## Data Architecture

All data is SQLite (WAL mode). There are two kinds of database.

### Application database — `systems/backend/database/catalogs.db`

Mounted at `/app/database`, beside the `secret_key` and `instance_id` files.

| Table | Contents |
|---|---|
| `catalog_meta` | Catalogs: directory, `ingest_mode` (copy / reference / move), `slug`, image rungs, similarity metric, owner |
| `storage_routes` | Where a catalog imports from, and whether imports may move files |
| `users`, `sessions`, `catalog_access` | Accounts, sign-in, per-catalog roles |
| `alerts` | Storage alerts |
| `user_settings` | Per-user profile and home location |
| `scheduler_lease` | Which process runs the workflow scheduler |
| `backup_targets`, `backup_runs` | Backup cards (one catalog each) and their runs |

Beside it in `database/`: `secret_key` (sessions, and the key backup secrets
are encrypted with), `instance_id`, and the SSH keys Airlock made —
`backup-ssh/` (one per backup type) and `publish-ssh/` (one per publish
server), each 0600 in a 0700 folder.

The v4 `backend/workflows.db` and `backend/user_settings.db` are gone:
workflow definitions moved into each catalog's database, and settings into
`catalogs.db`.

### Catalog database — `catalogs/<catalog>/workflow_results.db`

One per catalog, holding everything about its files. Paths stored inside are
**relative to the catalog**, so a catalog can be moved or mounted anywhere.

```
file_contents            master_datetime (the date, never missing), sha256, geohash, …
 ├── exif_data           promoted EXIF columns (camera, lens, ISO, GPS, …)
 │    └── exif_raw       the raw EXIF JSON blob, split out in v5.4.5
 ├── alternative_versions  image "rungs" (320, 1200, …), video proxies and posters
 ├── face_detections     boxes, source (Automatic/Manual), person_id
 │    ├── ai_face_contents / face_contents   crops + embeddings (+ embedding_model_id)
 │    ├── face_similarity_scores             face × person distances
 │    └── face_best_match                    best match per face, kept by triggers (v5.5.6)
 ├── pet_detections …    always present, empty unless pets are on (pet_schema.py)
 ├── event_files         → events
 ├── file_locations      → locations, location_clusters (where coordinates and place came from)
 └── tags
people                   people *and* pets (kind column), people categories
albums, album sections/filters/categories, album_deployments
deployment_servers, deployment_publish_runs     publishing to a server
wormhole_transfers, wormhole_transfer_files, wormhole_transfer_batches
saved_searches, saved_search_files; search_names, search_state (FTS5)
photo_edits, photo_edit_history
workflows, workflow_queue, workflow_executions, imports
catalog_properties       per-catalog settings: pets_enabled, embedding_model, …
views: face_embeddings (resolves model id), subject_appearances (faces ∪ pets)
```

Migrations are explicit scripts (`systems/backend/migrate_*.py`). The large
ones (`migrate_split_raw_exif.py`, `migrate_add_file_id_indexes.py`) run in
phases, with the backend stopped, and refuse anything irreversible until they
have verified it.

### Files on disk

```
catalogs/
├── catalogs.json                 slug → directory, read by album-gallery
└── <catalog>/
    ├── workflow_results.db
    ├── Images/ Videos/ Others/   originals (absent in ingest_mode=reference)
    ├── cache/
    │   ├── Images/ Videos/       derivatives: rungs, video proxies, posters
    │   ├── faces/ avatars/
    │   ├── Albums/ Deployments/  album folders, and the link farms album-gallery serves
    │   ├── Transfers/            send manifests, receive staging, group deadline files
    │   └── backup/               database snapshots taken before each backup
    ├── transfers/<id>[-label]/   received albums, published only when complete
    └── tmp/faces/
IN/                               default import drop folder (mounted at /input)
license/                          this copy's *.license file (mounted at /license, read-only)
```

Storage routes are subpaths of one host root (`STORAGE_ROOT_HOST`, default
`$HOME`), mounted at `/storage`. In **move** mode new files are renamed into the
catalog where the disk allows it, otherwise copied, verified by SHA-256 and
deleted from the source — only after the database commit. Duplicates go to
`<source>/DUPS/`.

---

## Security & Deployment Notes

- HTTPS everywhere through the gateway, with a private CA
  (`systems/gateway/make-certs.sh`; install the CA once per device).
  Certificates are mounted, not baked into the image, and not in git.
- Sign-in required by default; CORS restricted to `AIRLOCK_ALLOWED_ORIGINS`.
- Python services run under gunicorn, not Flask's development server.
- Per-machine settings live in `deploy/airlock.env` (template
  `deploy/airlock.env.example`). `deploy/deploy.sh` prepares a host,
  `deploy/status.sh` reports on one. The names from before the rename
  (`SHOEBOX_*`, `deploy/shoebox.env`, the `shoebox_session` cookie, `shoebox.lan`)
  are no longer accepted, since v6.4.5.
- The reference deployment runs on Linux (`homeserver`) under `/opt/airlock`.
- Anything that leaves the machine is the owner's to start: backups, publishing
  and transfers. Each uses a key of Airlock's own — one per backup type, one per
  publish server, with host keys pinned — never the host's own SSH keys. Publishing is
  owner only, reads included; a backup card can be seen by anyone with the
  catalog, but only an owner creates, changes, runs or restores one (`auth.py`).
- An album transfer code is single-use and SPAKE2-protected: safe to read out,
  not to publish. A received file is written only inside
  `<catalog>/transfers/<id>`, under a sanitised name.

---

## Version change

| Version | Change | Where it shows up here |
|---|---|---|
| v1 | Script only: Perl (2005), then bash/Perl (2007), then bash/Python (around 2010) | copy files, deduplicate, organize by date. First script 2005 |
| v2 | Complete redesign, barebones UI | UI monitoring copy, organization 2011 |
| v3 | UI redesign, workflows working on UI, based on AdminLTE  | Scripting, UI and worfklows tied together 2017|
| v4.0 | UI redesign using react, adopting version control with github | Script control, UI, minor deduplication enhancements |
| v4.1 | Sign-in, users, SSL gateway, migration to gitea | Gateway, Authentication |
| v4.2 | Storage routes | Files on disk |
| v4.3 | Video, transcoded for the browser | `backend-transcode` |
| v4.4 | Deployments: pack, publish, transfer | Album gallery and deployments |
| v4.5–v4.6 | Live and streaming import | Workflow engine |
| v4.7 | Removing files, Removed Files report | `/removed-files` |
| v4.8 | Move-mode ingestion | Files on disk |
| v4.9 | Native / container / GPU per service | Accelerated services |
| v4.10–v4.11 | Mass date edits; Live Photos as one item | Library API |
| v4.12 | Storage alerts | `alerts`, `/alerts` |
| v4.13 | Location clustering and the map | `location_clusters`, `file_locations` |
| v5.0 | Version tracking (`VERSION`, `/api/version`, tags) | — |
| v5.1 | Embedding provenance; SFace selectable | AI and media services |
| v5.2 | Pets | `backend-pets`, pet tables |
| v5.3–v5.5 | Server-side paging; EXIF blob split; `face_best_match`; faster face workflows | Frontend, Data Architecture |
| v5.6 | Renamed Shoebox → Airlock Photos (`airlock.lan`, `AIRLOCK_*`, `airlock_session`, `airlock-gateway`) | Throughout; see `docs/specs/name-migration-spec.md` |
| v5.7 | The Dashboard; the Catalogs page about the open catalog | Frontend |
| v5.8–v5.11 | Catalog backups: restic (run by Airlock itself from v5.9), borg, rsync | Backups |
| v5.12 | Calendar view; Dashboard coverage and storage bars | Frontend |
| v5.13 | Each account has its own page | Frontend, Authentication |
| v6.0 | Search and saved searches | Search |
| v6.1 | Basic photo editing; card bands; backups show progress | Photo editing |
| v6.2 | Publishing a deployment to a server; build-stamped asset caching; overnight Match Faces | Publishing to a server, Frontend, Workflow engine |
| v6.3 | Mass transfer; transfers register as `airlock.app`; backups drawn on the workflow diagram | Album transfers, Workflow engine |
| v6.4 | License notice; the pre-rename Shoebox names no longer accepted (v6.4.5) | License notice, Security |
