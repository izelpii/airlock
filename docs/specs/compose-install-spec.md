# Installing with Docker Compose and Published Images

**Status:** built (v6.6.0), with the Phase 0 application improvements released
first: geoloc's `/health` (v6.5.6), the gallery's kept session secret (v6.5.7),
and smaller images (v6.5.8).
Everything else is in `compose/`; its guide is `compose/README.md`.

> **As implemented: where it differs from the plan below.**
> - **§7 ownership: an entrypoint, not `user:`.** Docker gives a new named volume the ownership of the image folder it covers (uid 1000). A process started directly as `PUID` therefore couldn't write it. And `/home/appuser` is mode 0700, so no other uid could even reach a volume mounted inside it. So each `appuser` image's layer has `images/common/airlock-anyuid`: it starts as root, re-owns only the image's own folders (`AIRLOCK_OWNED_DIRS`: home, model and work folders, never the user's data), adds a passwd entry, and becomes `PUID:PGID` with `setpriv`. With podman, `docker-compose.podman.yml` uses `keep-id`, and the entrypoint sees a non-root start and stays out of the way. The review found **no application code that fails under another uid**. gunicorn's control socket, which failed under `/app`, follows `HOME`, which the layer now sets for every image. So no application change was needed for it.
> - **§3.2 layers are generated.** `images/render_layer.py` writes each layer's Dockerfile at build time from `images/services.json`. It repeats the application's `CMD`, read from its own Dockerfile, because setting `ENTRYPOINT` resets an inherited `CMD`. No hand copy can drift. Base images are pushed as `airlock-<service>-base` (decision 6), because a multi-architecture layer has to build `FROM` an image in the registry.
> - **§4 the gateway uses `resolve` for every upstream, not only the optional ones** (nginx 1.27.3+). A compose container that restarts gets a new address, so every name is re-resolved. The resolver is read from `resolv.conf` by the nginx image's own `15-local-resolvers.envsh`. Ollama's address is decided at start by `16-airlock-ollama.envsh`: an `/etc/hosts` name such as `host.docker.internal` on Linux is written as its address, since nginx's resolver asks DNS only.
> - **§5.1 no `docker-compose.dev.yml`.** Local images come from `ci/build.sh` (tagged `localhost/airlock-*:dev`), run with `AIRLOCK_IMAGES=localhost` and `AIRLOCK_VERSION=dev`.
> - **§5.2 additions.** The pet detector (YOLOX-Tiny) is baked into the pets layer like YuNet, so pets detects animals out of the box; the identity model is copied in by hand (README, "Pets"). The storage root is mounted **read-write**, as the script install does since move mode, and `STORAGE_ROOT_MODE=ro` forbids moves. Optional tuning goes in an optional `settings.env`: compose would otherwise pass unset settings as empty strings, and some mean something different when empty (an empty `DEEPFACE_WARMUP_MODELS` warms up nothing). That needs compose 2.24+.
> - **§6** `docker compose run --rm certs --force` renews the leaf and keeps the CA.
> - **§10.2** the sizes quoted are from this development machine's podman builds, which are amd64, not arm64 as the table says.
> - **§10.4 every image builds natively, one runner per architecture (v6.6.1).** v6.6.0 built both architectures on one amd64 runner, with arm64 under QEMU: it published the gateway and certs, then spent GitHub's whole six-hour limit on the frontend. Now a `build` job runs `ci/build.sh --push --arch amd64` on `ubuntu-24.04` and `--arch arm64` on `ubuntu-24.04-arm`, in parallel, each pushing `<version>-<arch>` tags. A `publish` job then runs `ci/build.sh --merge`, which joins them into the release tags with `docker buildx imagetools create`, and smoke-tests the result. Build caches are scoped per service and architecture. GitHub sends no push event for more than three tags at once, so a release's tag is pushed on its own, or the workflow is run by hand.
> - **Phase 0 image slimming, as done.** Both changes are in the application's own Dockerfiles, so the script install gains them too.
>   - **deepface, 3.0 → 2.0 GB.** On x86_64, `tensorflow` (1.9 GB, CUDA included) is swapped for `tensorflow-cpu` of the same version, which has the same import name. The swap happens only when that wheel exists, and never for the GPU variant. OpenCV, which arrived as two or three overlapping distributions, is reduced to one headless build. TensorFlow's C++ headers are dropped. VGG-Face and SFace embeddings of the same face, computed with `tensorflow-cpu` and with full `tensorflow` in the same image, were **bit-for-bit identical**. Detection and embedding were also checked through the running service.
>   - **backend, 1.01 GB → 809 MB.** The Mesa GPU drivers (LLVM, gallium, z3) that Debian's ffmpeg pulls in are removed. They are never linked, and this container has no GPU; `backend-transcode`, which does use GPUs, is untouched. The same `RUN` proves ffmpeg still decodes and encodes, and the full backend suite passed in the slimmed image: 995 tests, including the 35 video and image ones.
> - **Not done:** platform testing beyond this Mac's podman (Phase 5: Docker Engine on Linux, Raspberry Pi, Docker Desktop).

**Related:**
- `docs/guides/INSTALLATION.md`: today's install, through `deploy/deploy.sh` and `start-all.sh`.
- `accelerated-services-spec.md`: native, container and GPU modes.
- `redesign-ports-routes-spec.md`: the gateway and the service names.
- `user-authentication-spec.md` §10: TLS and the private CA.
- `deploy/README.md`: rootless podman and file ownership.

---

## 0. The rule: new artifacts in their own folder

**Every artifact that exists only for this install method lives in one new
top-level folder, `compose/`.** That covers the compose files, the thin image
layers, the generated gateway configuration, wrapper scripts, CI scripts,
tests and the guide. The compose install is an *addition* to the package,
beside the script install. It is not woven into it.

**Improvements to the application found along the way go to the
application**, as ordinary changes with their own release, tests and
CHANGELOG entry. The test for which side something belongs on:

> **Would the script install, or the application itself, be better for it
> even if the compose install didn't exist?** Then it is an application
> change. **Is it needed only because the stack runs under Docker Compose
> from published images?** Then it goes in `compose/`.

| Belongs to the application (an improvement in its own right) | Belongs in `compose/` (exists only for this method) |
|---|---|
| A `/health` route for geoloc, the one service without one (§5.3) | `docker-compose*.yml`, `.env.example`, `install.sh` |
| A stable gallery session secret, so a restart doesn't sign everyone out of password-protected galleries (§10.3) | The gateway configuration generated for the compose network (§4) |
| Images and code that work under any user id, wherever that is a fix in the code or the Dockerfile (§7) | The compose image layers: permission tweaks for `user:`, OCI labels, entrypoint wrappers (§7) |
| The YuNet face detector baked into the deepface image instead of downloaded by `deploy.sh`, if that proves better for container mode too (§5.2) | The `certs` init image and its wrapper around `make-certs.sh` (§6) |
| Smaller images, such as a slimmer TensorFlow layer (§10.2) | `compose/ci/*` and `.github/workflows/compose-images.yml` |
| Bugs found while testing on new platforms | `compose/tests/*` and `compose/README.md` |

In practice:

1. **Compose-only artifacts are added in `compose/`, never scattered** into `systems/`, `deploy/` or the root scripts. Where compose needs something the application doesn't provide, `compose/` *wraps* the application: a thin image `FROM` an application image, a script that calls an application script with arguments, or configuration generated from the application's own files.
2. **Application improvements are made in the application, separately.** Each is its own change, released and tested on its own merits, and the script install benefits too. This spec names them as prerequisites or follow-ups, and the compose work then uses them instead of working around them.
3. **The application must keep working, and the script install keep behaving, without `compose/`.** No application change may *depend* on the compose install.
4. **Files outside `compose/` that belong to this method, and must live elsewhere:**
   - **`.github/workflows/compose-images.yml`.** GitHub runs workflows only from that folder. It holds no logic: it calls `compose/ci/build.sh`.
   - **Links**: a row in `docs/guides/INSTALLATION.md`, a row in the README's Installation section, and a line in the docs indexes, all pointing to `compose/README.md`.
   - **The public release kit**: the lines in `tools/public/docs.txt` (and `exclude.txt` if needed) that publish `compose/` and the workflow file.
5. **Removing `compose/` and the workflow file removes the install method completely**, and leaves a working application. A test in `compose/tests/` enforces rule 1: compose-only artifacts outside `compose/` fail it.

**Why.** Keeping the deployment-specific parts in one folder means:
- they can't break the script install;
- a review of a compose change is a review of `compose/`;
- the method can be maintained, or dropped, as one unit.

Routing genuine improvements to the application means the core gets better
for everyone, and `compose/` doesn't accumulate workarounds for problems that
should simply be fixed.

---

## 1. Purpose

Today, installing Airlock means cloning the repository and running two
scripts. `deploy/deploy.sh` prepares the host, and `start-all.sh` builds all
ten images *on that machine* and starts them. This works well, but it asks a
lot of a newcomer:
- a clone of the source;
- 10–60 minutes of image builds, an hour or more on a Raspberry Pi;
- podman specifically;
- several host-side steps.

This spec adds a second way to install, aimed at people who already run
Docker. The images are built once, published to a container registry for both
CPU architectures, and installed with one `docker-compose.yml`:

```bash
mkdir airlock && cd airlock
curl -fsSLO https://raw.githubusercontent.com/izelpii/airlock/main/compose/docker-compose.yml
curl -fsSLO https://raw.githubusercontent.com/izelpii/airlock/main/compose/.env.example
cp .env.example .env && $EDITOR .env        # where the photos are
docker compose up -d
```

### Success criteria

1. A machine with Docker Engine or Docker Desktop, and nothing else, runs the full stack from published images: no clone, no build, no host Python, ffmpeg or openssl.
2. The same `docker-compose.yml` works on linux/amd64 and linux/arm64. That covers PCs and servers, Intel and Apple Silicon Macs through Docker Desktop, and a 64-bit Raspberry Pi 4 or 5.
3. Upgrading is `docker compose pull && docker compose up -d`, and data survives it.
4. Files written into the catalogs belong to the host user who runs the stack, whatever that user's uid.
5. The data layout is identical to a script-based install. Moving between the two ways of installing is a matter of pointing at the same folders.
6. **The compose install's own artifacts are all in `compose/`** (§0). Any change to the application made along the way is an improvement in its own right, released separately, and the script install keeps working throughout.

### Non-goals

- **Replacing the scripts.** They remain the *accelerated* install. Only they run faces and video natively on a Mac (Metal, VideoToolbox), which no container on macOS can reach (§9).
- **Changing the application *only* to suit compose.** What compose alone needs stays in `compose/`; what improves the application goes to the application (§0).
- **Full podman support for compose.** It is best-effort (§8).
- **Kubernetes, Helm, or orchestration across several hosts.**
- **A public-internet deployment.** The threat model is unchanged (`user-authentication-spec.md` §3, §10.7).

---

## 2. What exists today, and what carries over

| Already true | Why it matters here |
|---|---|
| All 10 services have Dockerfiles on public bases (`python:3.12-slim`, `node:22-alpine`, `nginx:1.27-alpine`) | Base images build unchanged; `compose/` layers on top |
| The frontend uses relative URLs only, and the public name is set when the gateway starts (`SERVER_DOMAIN`) | One prebuilt frontend image serves any installation |
| Data lives in bind mounts: catalogs, database, inbox, storage root, license | The compose file mounts the same paths |
| Schema is created at startup (`catalog.ensure_schema()`, `auth.init_schema()`) | `up -d` on a new version needs no migration step |
| `workflows/` is copied into the backend image | The bind mount used for development can be dropped |
| The Flask secret key (`database/secret_key`) and catalogs index are generated at runtime | No secrets need baking in |
| The application's Dockerfiles take `AIRLOCK_VERSION`, `GALLERY_REVISION` and `GPU` as build arguments | CI passes them; no Dockerfile change |
| `make-certs.sh` already takes extra names and addresses as arguments, and `--no-auto-ip` | A wrapper can drive it unchanged (§6) |

What the scripts do **on the host**, which `compose/` has to absorb:

| Today | Where | Handled in `compose/` by | § |
|---|---|---|---|
| Services reach each other through the host: `*.airlock.lan` names resolve to the host, then to host ports | `--add-host=…:${AIRLOCK_HOST_ALIAS}`, `UPSTREAM_HOST` in the gateway | a compose gateway image whose nginx configuration is generated from the application's template | 4 |
| TLS certificate generated with openssl on the host | `systems/gateway/make-certs.sh`, run by `deploy.sh` | a `certs` init image that runs the same script, unchanged | 6 |
| Rootless podman maps the invoking user to the containers' uid 1000 | `AIRLOCK_USERNS` | thin image layers, plus `user:` | 7 |
| Ollama is a host service | `OLLAMA_HOST` → gateway → host:11434 | `host.docker.internal`, or an optional profile | 5.4 |
| GPU and video devices passed when present | CDI, `--device` | override files | 5.5 |
| Start order and health checks | `start-all.sh` | `depends_on`, healthchecks | 5.3 |
| The face detector (YuNet) downloaded | `deploy.sh` | baked into the image: in the application's Dockerfile if that helps container mode generally, otherwise in the compose layer | 5.2 |
| The gallery's session secret, ideally stable | `ALBUM_SESSION_SECRET`, set by hand | an application improvement: the gallery keeps the secret it generates (§10.3) | 10.3 |
| Port floor lowered for 80/443 | `enable-port-80.sh`, the sysctl | not needed with rootful Docker | 8 |

---

## 3. Layout and shape

### 3.1 The `compose/` folder

```
compose/
  README.md                     the install guide for this method (§12)
  docker-compose.yml            the stack, published images only
  docker-compose.gpu.yml        NVIDIA override (§5.5)
  docker-compose.podman.yml     podman differences (§8)
  docker-compose.dev.yml        build everything locally from systems/ + compose/images/
  .env.example                  every setting, commented, with defaults (§11)
  install.sh                    optional helper: asks, writes .env, starts (§12)

  images/                       thin layers over the application's images (§5.2, §7)
    common/anyuid.sh            permissions for any user id, shared by the layers
    backend/Dockerfile          FROM airlock-backend-base
    deepface/Dockerfile         FROM airlock-deepface-base, + YuNet, + anyuid
    pets/  vision/  geoloc/  transcode/  albums/   (same pattern)
    gateway/Dockerfile          FROM airlock-gateway-base, + generated config (§4)
    gateway/render-config.py    application template → compose configuration
    certs/Dockerfile            alpine + openssl + the application's make-certs.sh (§6)
    certs/run.sh                --if-missing, names from the environment

  ci/
    build.sh                    base images from systems/*, then compose layers, push (§10)
    smoke-test.sh               start from pushed tags, check health and first-run (§10.4)
    any-uid-test.sh             every image under uid 4242 (§7)

  tests/
    test_compose_layout.py      the §0 rule, aliases, upstreams, mounts parity (§16)
```

Plus one file outside it, because GitHub requires the location:
`.github/workflows/compose-images.yml`, which calls `compose/ci/build.sh`.

### 3.2 Two kinds of image

| Kind | Built from | Published? |
|---|---|---|
| **Base**: `airlock-<service>-base` | `systems/<service>/Dockerfile`, as the application has it, with the usual build arguments; no compose-only edits | Not to users. Built in CI and used as the `FROM` of the layer; may be pushed as an intermediate |
| **Compose**: `airlock-<service>` | `compose/images/<service>/Dockerfile`, `FROM` the base | **Yes.** This is what `docker-compose.yml` pulls |

A service that needs nothing extra (frontend, gallery) has a one-line layer
(`FROM …-base`), so every published image has the same shape and the same
pipeline.

### 3.3 Runtime shape

```
                      browser ── https://airlock.lan / https://<lan address>
                                     │  :80 → :443
                 ┌───────────────────▼────────────────────┐   network: airlock
                 │ gateway  (aliases: airlock.lan,        │
                 │   backend.airlock.lan, deepface.…,     │
                 │   vision.…, geoloc.…, albums.…, …)     │
                 └──┬──────┬──────┬──────┬──────┬──────┬──┘
                frontend backend deepface pets  vision  albums  geoloc  gallery  transcode
                          │  └── calls deepface/albums by their *.airlock.lan names,
                          │      which resolve to the gateway on this network
                          └── ollama (profile "ollama")  or  the host's Ollama
```

- **One user-defined bridge network, `airlock`.** Every service joins it.
- **Only the gateway publishes ports to the host:** 80 and 443.
- **The `*.airlock.lan` names keep their meaning.** Inside the network they are aliases of the gateway, which routes by `Host` header exactly as the application's configuration already does. `DEEPFACE_URL`, `ALBUMS_URL` and `BACKEND_URL` keep their defaults, so the application sees nothing different.

---

## 4. Addressing: the compose gateway

The application's `airlock.conf.template` uses one host for every upstream:

```nginx
upstream airlock_backend  { server ${UPSTREAM_HOST}:5000; }
upstream airlock_deepface { server ${UPSTREAM_HOST}:5001; }
...
```

On a compose network each service has its own name, so this can't be used
as it is. Per-service upstreams help only compose, so they are a compose
artifact (§0): the template is left as it is, and
`compose/images/gateway/render-config.py` *generates* the compose version
from it while the compose gateway image is built:

1. It reads the application's template from the base image (`/etc/nginx/templates/…`).
2. It rewrites each `upstream airlock_<name> { server ${UPSTREAM_HOST}:<port>; }` into the compose service name: `server backend:5000;`, `server deepface:5001;` and so on, from a fixed map in the script.
3. For optional upstreams (Ollama; pets on small hosts), it writes Docker's resolver (`resolver 127.0.0.11 valid=10s;`) and a variable-based `proxy_pass`, so a missing service gives `502` on that path instead of stopping nginx at start.
4. It writes the result over the template in the compose image. Everything else in the configuration (server blocks, TLS, ACME path, per-catalog gallery hosts) is the application's, byte for byte.
5. **It fails the build** if it finds an `upstream` it has no mapping for, or an `${UPSTREAM_HOST}` it didn't replace. A new service added to the application then breaks the compose build loudly, rather than routing nowhere.

The compose gateway joins the network with the service names as aliases:

```yaml
gateway:
  networks:
    airlock:
      aliases: [airlock.lan, backend.airlock.lan, deepface.airlock.lan, vision.airlock.lan,
                geoloc.airlock.lan, albums.airlock.lan, gallery.airlock.lan,
                ollama.airlock.lan, pets.airlock.lan]
```

`systems/gateway/hosts-entries.txt` remains the list of record.
`compose/tests/test_compose_layout.py` asserts that the aliases match it.

**Transcode** is reached directly, because the application's gateway has no
`transcode` server block. Compose sets `TRANSCODE_URL=http://transcode:5004`
for the backend, which is an environment value the backend already reads.
Empty keeps today's meaning: the import makes its own previews.

---

## 5. The compose file

### 5.1 Files

As in §3.1. The main file references published images only. `docker-compose.dev.yml`
replaces each `image:` with a `build:` that builds the base from `systems/<service>`
and the layer from `compose/images/<service>`, for anyone working on `compose/` itself.

### 5.2 Services

| Service | Image | Mounts | Notes |
|---|---|---|---|
| `certs` | `airlock-certs` | `./certs` | One-shot init (§6) |
| `gateway` | `airlock-gateway` | `./certs:ro` | Publishes `${HTTP_PORT:-80}:80`, `${HTTPS_PORT:-443}:443`; generated configuration (§4) |
| `frontend` | `airlock-frontend` | – | One-line layer |
| `backend` | `airlock-backend` | `${CATALOGS_DIR}:/catalogs`, `${DATABASE_DIR}:/app/database`, `${INPUT_DIR}:/input`, `${STORAGE_ROOT}:/storage:ro`, `${LICENSE_DIR:-./license}:/license:ro` | `user:` (§7) |
| `deepface` | `airlock-deepface` | `deepface-weights:/home/appuser/.deepface`, `deepface-data:/app/database_images`, `deepface-uploads:/app/uploads` | YuNet (about 300 KB) in the image, not downloaded at first start (§0 table) |
| `pets` | `airlock-pets` | `pets-weights:/app/weights` | |
| `vision` | `airlock-vision` | `${CATALOGS_DIR}:/catalogs` | `OLLAMA_HOST=http://ollama.airlock.lan` |
| `albums` | `airlock-albums` | – | `OLLAMA_HOST` likewise |
| `geoloc` | `airlock-geoloc` | – | Health check needs the new `/health` route (§5.3) |
| `gallery` | `airlock-gallery` | `${CATALOGS_DIR}:/catalogs:ro`, plus a small writable volume for its kept secret | §10.3 |
| `transcode` | `airlock-transcode` | `${CATALOGS_DIR}:/catalogs` | Optional devices (§5.5) |
| `ollama` | `ollama/ollama` | `ollama:/root/.ollama` | `profiles: [ollama]` (§5.4) |

Named volumes hold what is machine state but not the user's data: model
weights, deepface's working folders, Ollama's models. The user's data lives
in bind mounts, so it can be seen, backed up and moved without Docker.

### 5.3 Start order and health

Each service gets a `healthcheck` **in `docker-compose.yml`**. Health checks
are a compose concern, so they don't go in the application's Dockerfiles. Any HTTP response
counts as healthy, because `/api/*` correctly answers 401 when no one is
signed in (`deploy/README.md`, "A 401 means the service is up").

| Service | Check |
|---|---|
| backend | `GET /api/version`, any status |
| deepface | `GET /api/health` |
| transcode, pets, vision, albums, gallery | `GET /health` |
| geoloc | `GET /health`, once the application gains the route: geoloc is the only service without one, and `start-all.sh` and `deploy/status.sh` would use it too (§0: an application improvement). Until then, a TCP connect to 5002. |
| frontend | `GET /` |
| gateway | `GET /gateway-health` |

The checks use the runtimes the images already have: `python3 -c "import
urllib.request…"` (or `socket` for geoloc) in the Python images, and `wget
-q` in the Node and nginx images.

The backend `depends_on` deepface, albums and transcode
(`condition: service_started`; deepface's first start downloads models and
may take minutes, and the backend copes with it being unavailable). The
gateway depends on `certs` (`service_completed_successfully`), frontend and
backend.

`restart: unless-stopped` on every long-running service, so the stack comes
back after a reboot, which `start-all.sh` doesn't do.

### 5.4 Ollama

Two supported arrangements, chosen in `.env`:

- **On the host (default).** `OLLAMA_UPSTREAM=host.docker.internal`. Docker Desktop provides that name. On Linux the gateway gets `extra_hosts: ["host.docker.internal:host-gateway"]`. Ollama must listen beyond loopback on Linux (`OLLAMA_HOST=0.0.0.0`). This is the right choice on a Mac, where Ollama on the host uses Metal and a container would be CPU-only.
- **In the stack.** `docker compose --profile ollama up -d`, with `OLLAMA_UPSTREAM=ollama`. A one-shot `ollama-init` service in the same profile pulls the default models.

`OLLAMA_UPSTREAM` is consumed by the compose gateway's generated
configuration (§4), not by the application's.

### 5.5 Hardware acceleration

**NVIDIA (Linux):** `docker-compose.gpu.yml` adds device reservations to
deepface, transcode and ollama, and sets `TRANSCODE_MODE=gpu`, which the
transcode service already reads. The deepface image used with it is the GPU
variant: the base built with the application's existing `GPU=1` build
argument, plus the same compose layer, tagged `-gpu`, amd64 only. This needs
the NVIDIA Container Toolkit.

**Intel/AMD VA-API and Quick Sync, and the Raspberry Pi 4's encoder:** commented
lines in the main file:

```yaml
transcode:
  devices: ["/dev/dri:/dev/dri"]          # Intel/AMD
  # devices: ["/dev/video11:/dev/video11"] # Raspberry Pi 4
  group_add: ["${RENDER_GID:-render}", "${VIDEO_GID:-video}"]
```

The transcode service already proves each encoder with a one-frame encode
and falls back to software.

---

## 6. Certificates

A one-shot `certs` service runs before the gateway. Its image,
`compose/images/certs/`, is `alpine` with `openssl` and `bash`, plus a
**copy of the application's `systems/gateway/make-certs.sh`, unchanged**,
taken at build time. `compose/images/certs/run.sh` drives it:

```sh
# compose/images/certs/run.sh  (sketch)
[ -f /work/certs/fullchain.pem ] && [ -f /work/certs/privkey.pem ] && exit 0   # --if-missing
cd /work
exec ./make-certs.sh --no-auto-ip --ca-label "${CA_LABEL:-airlock}" $(echo "$AIRLOCK_NAMES" | tr ',' ' ')
```

```yaml
certs:
  image: ghcr.io/izelpii/airlock-certs:${AIRLOCK_VERSION:-latest}
  environment:
    AIRLOCK_NAMES: ${AIRLOCK_NAMES:-}     # this machine's LAN address(es), comma-separated
    CA_LABEL: ${AIRLOCK_CA_LABEL:-airlock}
  volumes: ["./certs:/work/certs"]        # make-certs.sh writes to <its folder>/certs
  restart: "no"
```

The details that matter:
- **`make-certs.sh` writes to `certs/` beside itself.** Placing it at `/work/make-certs.sh`, and mounting `./certs` at `/work/certs`, gives it the folder it expects with no change.
- **`--no-auto-ip`**, because inside a container the script would only see the container's own address. The machine's LAN address comes from `AIRLOCK_NAMES` instead. The script already turns an IPv4 argument into an IP SAN (`user-authentication-spec.md` §10.3, "its own IP is a name").
- **The certs image carries openssl,** which the running gateway doesn't need.
- **`./certs/airlock-ca.pem`** lands next to the compose file, to install on each device, with the same per-platform instructions as today.
- **A test** compares the certs image's copy of `make-certs.sh` with `systems/gateway/make-certs.sh` at build time. It is copied in CI from the same commit, so they can't drift.

---

## 7. File ownership

The riskiest part.

**Today**, rootless podman with `--userns=keep-id:uid=1000,gid=1000` maps the
invoking user to uid 1000 inside the container, so files the backend writes
belong on the host to that user, whatever their uid.

**With Docker Engine (rootful)** there is no mapping: uid 1000 inside is uid
1000 on the host. On a host whose user is 1000, which covers most single-user
Linux machines and the Pi's first user, everything works. Otherwise, files
end up owned by another account, or the backend can't write to folders the
user created.

**Design, all in `compose/`:**
1. **`compose/images/common/anyuid.sh`**, run in each layer for the images that use `appuser` (backend, deepface, pets, vision, geoloc, transcode):
   - it makes the paths the process writes group-owned by gid 0 and group-writable, the OpenShift convention: `chgrp -R 0 … && chmod -R g=u …`;
   - it sets `ENV HOME` to a writable folder, so libraries that cache under `~` work under an unknown uid;
   - it adds an `/etc/passwd` entry at container start (a tiny entrypoint wrapper that execs the image's original command), for code that looks up the user's name.
2. **Compose sets `user: "${PUID:-1000}:${PGID:-1000}"`** on those services. `install.sh` writes `PUID=$(id -u)` and `PGID=$(id -g)` into `.env`.
3. **Docker Desktop (macOS, Windows)** maps ownership to the host user through its file sharing anyway, so `PUID` is harmless there.
4. **The gateway, frontend and gallery run as root inside**, as today. They write no user data, and the gallery's mount is read-only.

**`compose/ci/any-uid-test.sh`** starts every published image with
`--user 4242:4242`. Each one must reach healthy and write its working
folders.

**When an image fails under an arbitrary uid** because of its code or its
Dockerfile, for example a path written outside the mounts or a hard-coded
home, that is fixed **in the application**. It is a robustness improvement,
and the script install's rootless mapping benefits from it too (§0, rule 2).
The compose layer keeps only what is specific to running with `user:` under
Docker.

---

## 8. Container engines and platforms

| Host | Engine | Support | Notes |
|---|---|---|---|
| Linux x86_64 | Docker Engine 24+ with the compose plugin | **Primary** | Ports 80/443 need no setting (rootful) |
| Linux arm64 (Pi 4/5, 64-bit OS) | Docker Engine | **Primary** | 8 GB Pi recommended; the first pull is about 6 GB |
| macOS (Intel, Apple Silicon) | Docker Desktop, or OrbStack | **Supported** | Faces and video on the CPU (§9); give the VM 6 GB+ |
| Linux, rootless Docker | Docker rootless | Best-effort | Port floor as for rootless podman |
| Linux or macOS | podman 4.7+ with `podman compose` | Best-effort | `docker-compose.podman.yml` |
| Windows | Docker Desktop with WSL2 | Untested | Bind mounts should live inside WSL2 |

**Podman.** Network aliases need netavark (podman 4+). User mapping uses
`userns_mode: "keep-id:uid=1000,gid=1000"` instead of `user:`, and GPUs go
through CDI (`devices: ["nvidia.com/gpu=all"]`). These go in
`docker-compose.podman.yml`. Podman users are better served by the existing
scripts, and `compose/README.md` says so.

---

## 9. What compose cannot do that the scripts can

| Capability | Scripts | Compose |
|---|---|---|
| Faces on Apple Silicon's GPU (Metal) | native, `host` mode | CPU in the Linux VM |
| Video previews with VideoToolbox (macOS) | native | CPU, libx264 |
| Ollama on a Mac's GPU | host install | host install still recommended (§5.4) |
| Choosing per service: native, container, GPU, off | `AIRLOCK_*_MODE` | container or GPU only |
| Editing `workflows/` without a rebuild | bind mount | `docker-compose.dev.yml` only |

So the guides present compose as **"the easy install"**, and the scripts as
**"the accelerated install, and the one for podman"**.

---

## 10. Images and the registry

### 10.1 Registry

**GitHub Container Registry**, `ghcr.io/izelpii/airlock-<service>`:
- free for public images;
- next to the source;
- authenticated in CI with the workflow's own `GITHUB_TOKEN`.

Docker Hub's unauthenticated pull limits are per IP, and a 10-image stack can
hit them on a shared address, so Docker Hub would be a mirror at most.
**Decision for the maintainers** (§14).

### 10.2 Names, tags, sizes

| Published image | Base from | Layer from | Approx. size |
|---|---|---|---|
| `airlock-gateway` | `systems/gateway` | `compose/images/gateway` | 50 MB |
| `airlock-certs` | – (alpine) | `compose/images/certs` | 10 MB |
| `airlock-frontend` | `systems/frontend` | one line | 60 MB |
| `airlock-backend` | `systems/backend` | `compose/images/backend` | 1.0 GB |
| `airlock-deepface` | `systems/backend-deepface` | `compose/images/deepface` | 3.0 GB |
| `airlock-deepface` `-gpu` | same, `GPU=1`, amd64 only | same | larger |
| `airlock-pets` | `systems/backend-pets` | `compose/images/pets` | 310 MB |
| `airlock-vision` | `systems/backend-vision` | `compose/images/vision` | 175 MB |
| `airlock-albums` | `systems/backend-albums` | `compose/images/albums` | 160 MB |
| `airlock-geoloc` | `systems/backend-geoloc` | `compose/images/geoloc` | 470 MB |
| `airlock-gallery` | `systems/album-gallery` | one line | 170 MB |
| `airlock-transcode` | `systems/backend-transcode` | `compose/images/transcode` | 600 MB |

Sizes are this machine's arm64 builds of the bases; the layers add little.
About **6.3 GB** in total. Making images smaller, for example a slimmer
TensorFlow in deepface or fewer build tools in the backend, helps the script
install's builds too, so it is an application improvement (§0).

**Tags** for release `vX.Y.Z`: `X.Y.Z`, `X.Y` and `latest`, each a
multi-arch manifest list (linux/amd64, linux/arm64), plus `sha-<short>`.

**Four-part releases** (`vX.Y.Z.N`, documentation only) **build no images**.
`.env.example` pins a three-part version.

### 10.3 Build arguments and runtime values

- **`AIRLOCK_VERSION`, `GALLERY_REVISION`, `GPU`:** passed by `compose/ci/build.sh` to the application's Dockerfiles, which already declare them.
  - `GALLERY_REVISION` is the last commit that touched `systems/album-gallery`, the same rule as `airlock_gallery_revision`. It is reported by the gallery's `/health`, and that is how a published server is seen to run an older gallery.
  - The public repository has its own commit hashes, so CI computes it from the public tree. An image-based installation compares like with like: public images only.
- **`ALBUM_SESSION_SECRET`:** the gallery generates one at each start when it is unset, which signs everyone out of password-protected galleries on every restart. That is a flaw in the script install too, so the fix belongs to the application (§0): when the variable is unset, the gallery generates the secret once and keeps it in a small file it can write, reusing it at each start. Compose then needs nothing beyond a volume for that file. Setting the variable still overrides it.
- **`REACT_APP_*`:** stays empty, so URLs stay relative.
- **OCI labels** (`org.opencontainers.image.source`, `.version`, `.revision`, `.licenses`) are added in the compose layers. `.licenses` points at the license file, since it isn't an SPDX license.

### 10.4 The build pipeline

`.github/workflows/compose-images.yml`, in the **public** repository. It
holds no logic of its own (§0, rule 4):
- **trigger:** a push of a tag matching `v[0-9]+.[0-9]+.[0-9]+` (three parts only);
- **steps:** checkout, `docker/setup-buildx-action`, `docker/login-action` (ghcr, `GITHUB_TOKEN`), then `compose/ci/build.sh --push --arch <arch>` on a native runner per architecture, then `compose/ci/build.sh --merge` (as implemented, above).

`compose/ci/build.sh`, for each service:
1. Build the base from `systems/<service>` with `docker buildx build --platform linux/amd64,linux/arm64`, with the build arguments above, cached with `type=gha`.
2. Build the compose layer from `compose/images/<service>`, `FROM` that base.
3. Push the layer under the published name and tags.

Deepface and backend go on native arm64 runners where the plan provides them,
because emulation is slow for TensorFlow and the backend's C extensions.

`compose/ci/smoke-test.sh` then:
- starts the stack from the just-pushed tags on amd64;
- waits for every health check;
- expects `{"setup_required": true}` from `https://localhost/api/auth/setup-required`;
- runs `any-uid-test.sh`.

---

## 11. Configuration: `compose/.env.example`

The compose counterpart of `deploy/airlock.env`, with the same names where a
setting means the same thing:

```bash
# ── Where your photos are ────────────────────────────────────────────────
CATALOGS_DIR=./data/catalogs        # every catalog - BACK THIS UP
DATABASE_DIR=./data/database        # accounts, catalogs list, secret key
INPUT_DIR=./data/IN                 # import inbox
STORAGE_ROOT=/mnt/photos            # read-only; every storage route lives inside it
#LICENSE_DIR=./license

# ── This machine ─────────────────────────────────────────────────────────
PUID=1000                           # `id -u`; files in the catalogs belong to this user
PGID=1000                           # `id -g`
AIRLOCK_NAMES=192.168.1.20          # this machine's LAN address(es), for the certificate
HTTP_PORT=80
HTTPS_PORT=443
#ALBUM_SESSION_SECRET=              # optional; the gallery keeps its own otherwise

# ── Version ──────────────────────────────────────────────────────────────
AIRLOCK_VERSION=6.6.0               # an image tag; "latest" follows every release

# ── Ollama ───────────────────────────────────────────────────────────────
OLLAMA_UPSTREAM=host.docker.internal   # or "ollama" with --profile ollama
#OLLAMA_MODEL=gemma4:latest
#ALBUMS_LLM_MODEL=granite3.3:8b

# ── Optional, as in deploy/airlock.env ───────────────────────────────────
#AIRLOCK_ALLOWED_ORIGINS=https://photos.example.com
#DEEPFACE_WORKERS=2
#TRANSCODE_JOBS=2
#IMPORT_READERS=1
```

`STORAGE_ROOT` is mounted at `/storage`, and the backend is given
`STORAGE_ROOT_HOST=${STORAGE_ROOT}` and `STORAGE_ROOT_CONTAINER=/storage`,
both of which it already reads, so it shows host paths as it does today.

---

## 12. First run, upgrades, backups, documentation

**First run:**
1. `docker compose up -d`. The images are pulled, `certs` runs once, and everything starts.
2. Install `./certs/airlock-ca.pem` on each device.
3. Open `https://<LAN address>/` and create the administrator account.

**`compose/install.sh`** does the questions for the user:
- it checks for Docker and the compose plugin;
- it asks for the photo folder and the LAN address, proposing what `ip route` or `ipconfig` reports;
- it writes `.env` with `PUID` and `PGID`;
- it runs `up -d`, and prints the address and the CA's path.

It is optional: the compose file works without it.

**Upgrade:** `docker compose pull && docker compose up -d`. Restarting
interrupts running workflows, as today; the guide repeats the advice to check
the Workflows page first.

**Backups:** unchanged. The catalog's Backup tab runs restic, borg or rsync
from the backend container, with its keys under `DATABASE_DIR`.

**Moving from the scripts to compose:**
1. Point `CATALOGS_DIR`, `DATABASE_DIR` and `STORAGE_ROOT` at the existing folders.
2. Copy `systems/gateway/certs/*` into `./certs`, so devices keep trusting the same CA.
3. Stop the podman stack, and start the compose one.

Moving back is the reverse.

**Documentation:**
- **The guide for this method is `compose/README.md`**, inside the folder (§0).
- **The existing documentation only gains links:** one row in `docs/guides/INSTALLATION.md`'s platform table ("Any machine with Docker → compose/README.md"), one row in the README's Installation section, and a line in the docs indexes.
- **The public kit publishes `compose/README.md` with the folder.**

---

## 13. Security notes

- **Only the gateway publishes ports.** Today, services listen on host ports 3000–5006, where anyone on the LAN can reach them directly: bypassing TLS, though not authentication. In compose they are unreachable from outside the network. `compose/README.md` states the difference.
- **The CA private key lives in `./certs`.** Keep it at mode `0700`, out of anything synced to the cloud.
- **The Docker socket is mounted into nothing.**
- **Published images contain the application source,** as the GitHub repository already does. The license applies to both.

---

## 14. Decisions for the maintainers

1. **Registry:** ghcr.io only (recommended), or with a Docker Hub mirror.
2. **Namespace:** `ghcr.io/izelpii/airlock-*`, or an organisation for the project.
3. **GPU images:** publish `airlock-deepface:*-gpu`, or document a local build for NVIDIA users.
4. **Ollama default:** host (recommended), or the in-stack profile.
5. **`install.sh`:** ship it, or document `.env` by hand only.
6. **Base images:** push them as intermediates (faster rebuilds of layers), or keep them CI-only.

---

## 15. Phases

Compose artifacts go in `compose/` (§0). Application improvements are
released on their own, and the script install keeps working throughout.

**Phase 0: application improvements** (each its own release, useful
without compose; §0 table):
- a `/health` route in geoloc;
- a gallery session secret that survives restarts;
- any-uid robustness where it is a code or Dockerfile fix;
- YuNet in the deepface image, if it suits container mode;
- image slimming.

They can land in any order, before or alongside the phases below.

**Phase 1: skeleton and the rule.**
- `compose/` with its README.
- `compose/tests/test_compose_layout.py`, which enforces §0 rule 1: no compose-only artifact (compose files, layers, CI scripts) outside `compose/` other than the files of rule 4.
- `render-config.py` (§4), with its tests against the application's current template.

**Phase 2: images.**
- The `compose/images/*` layers: anyuid, YuNet, OCI labels, the generated gateway configuration, the certs image.
- `compose/ci/build.sh`, building the bases from `systems/*` as the application has them.
- `any-uid-test.sh`.
- Application fixes found here go to the application (§0, rule 2).

**Phase 3: compose.** `docker-compose.yml`, the gpu, podman and dev
overrides, `.env.example` and `install.sh`. Tested first on Linux x86_64 with
Docker Engine, built locally with the dev override.

**Phase 4: publishing.** The workflow file, ghcr.io, multi-arch, the smoke
test, and the kit lines so `compose/` and the workflow publish.

**Phase 5: platforms.** Raspberry Pi 4 and 5 (arm64), Docker Desktop on Apple
Silicon and Intel, podman best-effort.

**Phase 6: documentation.** `compose/README.md` in full, including moving
between the two installs. Links added to `INSTALLATION.md`, the README and the
indexes.

**Rough effort** (one developer who knows the codebase):
- phase 0: 1–2 days, mostly the any-uid review;
- phases 1–3: 4–6 days;
- phase 4: 1–2 days;
- phases 5–6: 2–4 days.

---

## 16. Risks

| Risk | Mitigation |
|---|---|
| **Drift**: the application changes (a new service, upstream, mount, environment variable or device) and `compose/` silently falls behind | `render-config.py` fails the build on an unknown upstream (§4). `test_compose_layout.py` compares each service's mounts, environment and devices in `systems/*/start-podman.sh` with `docker-compose.yml`, and the aliases with `hosts-entries.txt`. The certs image's script is copied from the same commit (§6). All of it runs in CI on every release tag |
| **Compose-only artifacts leaking into the application**, or application fixes hidden in `compose/` as workarounds | The §0 test for the first. For the second, review: a workaround in `compose/` that would help the script install too is moved to the application (§0 table) |
| A non-1000 host uid writes files the user can't touch | §7: application fixes where the code is the cause, compose layers for the rest, `PUID`/`PGID`, `any-uid-test.sh` |
| nginx refuses to start when an optional service is absent | Resolver-based upstreams for optional services in the generated configuration (§4) |
| The certificate lacks the LAN address | `AIRLOCK_NAMES`, asked by `install.sh`, explained in the guide |
| The arm64 deepface build is too slow or fails under emulation | Native arm64 runners; layer cache |
| A 6 GB first pull on a Pi | Stated in the guide; recommend an SSD |
| Docker Hub limits, if used | ghcr.io as primary |
| People expect Mac speed from compose | §9 stated in the guide, with the script install recommended for Macs |
