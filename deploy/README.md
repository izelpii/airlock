# Deploying airlock on another machine

```bash
git clone <repo> airlock && cd airlock
./deploy/deploy.sh          # check this machine and prepare it
cp deploy/airlock.env.example deploy/airlock.env   # written for you if absent
$EDITOR deploy/airlock.env  # point it at your photo library
./start-all.sh --build      # build every image and start the stack
```

`./deploy/deploy.sh --start` does the last two steps in one go.
`./deploy/deploy.sh --check` changes nothing and just reports.

---

## The first run

There is no default account and no seeded password. The first time the UI is
opened it asks whether one exists, and offers **"Welcome to Airlock Photos — create the
administrator account"** when none does. That screen is the only way to create
the first user; everything after it is administered from the Users page or
`systems/backend/manage_users.py`.

**Open it at the gateway's address.** `https://airlock.lan/` works only on the
machine running the stack: `airlock.lan` is `127.0.0.1` in `/etc/hosts` on every
installation, so the name means *this computer* wherever it is typed. From
another machine's browser, use the deployment's LAN address — `https://<lan-address>/` —
and not a service port. `:3000` serves the UI but has no `/api`, so the setup
probe comes back as `index.html` with status 200 and no answer in it.

**A sign-in form on a new deployment means one of four things**, and they look
alike in the browser:

| what you see | what happened |
| --- | --- |
| sign-in form, no banner | the address reached a *different* deployment — usually the original one, via `airlock.lan` |
| sign-in form, no banner | an account already exists here, because `DATABASE_HOST_DIR` was carried over rather than created fresh |
| sign-in form with a warning banner | the page loaded from somewhere that does not serve `/api` — a service port |
| sign-in form with a warning banner | the backend is reachable but erroring, so the probe got no answer |

On a machine that has never run the stack, the usual way into the third row is
that **the gateway never started**, so `:3000` was the only thing answering. It
publishes 80 and 443, both privileged, and rootless podman refuses them while
the unprivileged port floor is 1024 — the default in a fresh podman machine.
`deploy.sh` now checks that floor and lowers it for you on macOS, where it is a
setting inside podman's VM and touches nothing on the Mac.

`./deploy/diagnose-first-run.sh` names which one. Run it on the new machine; add
the address the browser has in its bar to test that too, from whichever machine
is typing it:

```bash
./deploy/diagnose-first-run.sh                    # on the new machine
./deploy/diagnose-first-run.sh https://<lan-address>/ # from the machine browsing it
```

---

## What is machine-specific

One file: **`deploy/airlock.env`**. It is not in git, because it describes one
machine. Everything in it has a default derived from where the repository sits,
so an unedited file is a valid file.

| setting | what it is | default |
| --- | --- | --- |
| `CATALOGS_HOST_DIR` | every catalog: originals, derivatives, albums, deployments, received transfers. **Back this up** | `<repo>/catalogs` |
| `INPUT_HOST_DIR` | the ingestion inbox, mounted read-write at `/input` | `<repo>/IN` |
| `STORAGE_ROOT_HOST` | the one root storage routes are subpaths of, mounted **read-only** | `$HOME` |
| `DATABASE_HOST_DIR` | the application database — catalogs, users, routes | `<repo>/systems/backend/database` |

`STORAGE_ROOT_HOST` deserves a thought before you set it. A bind mount is fixed
when the container starts, so a folder outside that root **cannot** be read no
matter what the UI says. Make it wide enough to cover every library you will
point a storage route at.

---

## Putting a catalog on more than one kind of storage

A catalog is one folder, and every folder it uses is created with it
(`systems/backend/catalog_layout.py`):

```
<catalog>/
├── Images/  Videos/  Others/    originals: large, written once, read rarely
├── cache/                       display sizes, thumbnails, albums, deployments
├── transfers/  tmp/faces/       received album transfers, tagged faces
└── workflow_results.db          the catalog database: small, random access
```

So a catalog can be split by **bind-mounting** parts of it before the stack
starts. For example, the catalog (database and cache) on an SSD and the
originals on a spinning disk:

```bash
# /etc/fstab
/mnt/hdd/airlock/family/Images  /mnt/ssd/catalogs/family/Images  none  bind  0 0
/mnt/hdd/airlock/family/Videos  /mnt/ssd/catalogs/family/Videos  none  bind  0 0
```

with `CATALOGS_HOST_DIR=/mnt/ssd/catalogs`. Then check what the containers see:

```bash
podman exec workflow-backend df -h /catalogs/family /catalogs/family/Images
```

- **Bind mounts, not symlinks.** A container sees only what is mounted into
  it, so a symlink to elsewhere on the host does not resolve inside it, and
  the gallery refuses files that resolve outside their catalog.
- **Imports stay fast across the split.** An import stages each file under
  the originals folder it is going to (`Images/.import-staging/`, …), so moving
  it into place is still a rename on the same mount.
- **Mount the originals before the first import.** Whatever is imported
  before goes onto the disk under the mount point.
- **Back up the database.** The cache can be regenerated; `workflow_results.db`
  cannot.
- A catalog created before this layout existed:
  `python systems/backend/catalog_layout.py <catalog folder>` creates its
  missing folders. It is deliberately not run at startup: on a drive that is
  not mounted, it would create the tree on the disk beneath it.

## Why Linux needs a step macOS does not

Four images run as a non-root user, uid 1000: `backend`, `backend-deepface`,
`backend-vision`, `backend-geoloc`. Under **rootless podman on Linux**:

- the user invoking podman maps to **uid 0 inside** the container;
- container uid 1000 maps to a *subuid* that user does not own.

So a directory you created arrives **root-owned and unwritable** by the process
that has to write it. It does not announce itself as an ownership problem:

```
sqlite3.OperationalError: unable to open database file
PermissionError: [Errno 13] Permission denied: '/home/appuser/.deepface/weights'
```

Two things fix it, and `deploy.sh` does both:

1. **`--userns=keep-id:uid=1000,gid=1000`** on those four services, which maps
   you to container uid 1000 — so host-owned mounts are owned by the process
   that needs them. Set automatically by `deploy/airlock-env.sh`, and empty on
   macOS, where podman's VM already presents host paths writably.

2. **`podman unshare chown -R 1000:1000`** on the writable host directories.
   `podman unshare` runs the chown *inside* the namespace, so `1000:1000` means
   the container's user rather than yours.

`STORAGE_ROOT_HOST` is deliberately **not** chowned: it is mounted read-only and
holds your own photo library. Taking ownership of a home directory to make a
container happy is a poor trade.

> **Do not add `keep-id` to the gateway, frontend or album-gallery.** Those
> images run as root, where container-root already maps to you — which is
> exactly what lets them read host-owned mounts. `keep-id` would take that
> away.

### Rootless prerequisites

Rootless podman needs subuid/subgid ranges for your user. Without them, every
container that runs as a non-root user fails in ways that look like application
bugs. `deploy.sh` checks; if it complains:

```bash
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"
podman system migrate
```

### Port 80 — and 443

Rootless podman will not bind privileged ports by default. Either run
`systems/gateway/enable-port-80.sh`, or use its `--port-8080` mode. `deploy.sh`
reports the floor, and lowers it automatically on macOS; on Linux the sysctl is
on the host itself, so it prints the `sudo` command rather than running it.

Both ports matter. 443 is the one the browser uses — `:80` only redirects to it
— so the high-port escape hatch has to move both: `GATEWAY_PORT=8080
TLS_PORT=8443`.

---

## What `start-all.sh` does differently on Linux

**Containers run detached.** Each `start-podman.sh` runs `podman run` in the
foreground, which is right when you run one by hand — Ctrl-C stops it and the
logs are on screen. `start-all.sh` sets `AIRLOCK_DETACH=1` so they pass `-d`
instead. It used to background them with `&`, which made every container a child
of that shell: closing the terminal — or the **SSH session** — killed them, and
`--rm` then deleted them. Survivable on a desktop, not on a server you log out
of.

**backend-deepface runs as a container.** On macOS it runs natively, because
podman there uses a Linux VM and Apple's Metal stack has no passthrough into it,
so a containerised deepface would be CPU-only. On Linux there is no VM and no
reason to build a virtualenv on the host — `start-all.sh` picks by `uname`.

**A 401 means the service is up.** The health check used `curl -f`, which fails
on any status ≥ 400 — including the 401 that `/api/catalogs` correctly returns
when nobody is signed in. Every start reported the backend as failed while it
was running perfectly. The check now accepts any HTTP response, because what it
is testing is whether something answered.

## If a service will not start

`--rm --replace --name X` only replaces a container **named** `X`. A stale
container with a generated name (`upbeat_jemison`) still holds the port, and the
failure reads:

```
Error: something went wrong with the request: "proxy already running"
```

`podman ps` will show the culprit; stop it and start again.

## Two design rules this deployment depends on

Both were learned by breaking them, and both are easy to undo by accident.

**No `VOLUME` in a Dockerfile.** Per OCI semantics, changes to a path made
*after* it is declared as a `VOLUME` are discarded — so a `chown` that follows
the declaration may simply not be in the image. It built cleanly on macOS and
produced an unwritable directory on Ubuntu. Separately, `podman run` with no
`-v` creates an *anonymous* volume over a declared path, and under rootless
podman that volume is root-owned inside. Every path these images care about is
bind-mounted by its start script, so the declaration bought nothing.

**Importing a module must not create a database.** `catalog.py` used to call
`Catalog.init_db()` at import time. That made the image's own smoke test —
`python3 -c "import app"`, whose job is to prove the source is present and
importable — depend on writable storage, so a permissions problem arrived as
*"cannot import app.py"* with a hint about `ModuleNotFoundError`. Schema
creation is now `catalog.ensure_schema()`, called explicitly at startup, and the
smoke test sets `AIRLOCK_IMPORT_CHECK=1` to skip it.

---

## Diagnosing a container that will not start

```bash
# Who am I inside, and can I write where I must?
podman run --rm --entrypoint sh workflow-backend -c 'id; ls -ldn /app /app/database'
podman run --rm --entrypoint sh workflow-backend -c 'touch /app/database/x && echo WRITABLE || echo NOT'

# Does the source import at all, without touching storage?
podman run --rm --entrypoint python3 -e AIRLOCK_IMPORT_CHECK=1 \
  workflow-backend -c 'import app; print("import ok")'

# Rootless or not, and which podman?
podman info --format '{{.Host.Security.Rootless}} {{.Version.Version}}'

# Leftover anonymous volumes from an older image
podman volume ls
```

If `/app` shows uid 1000 but `/app/database` shows uid 0, something is mounted
over it that you do not own — an anonymous volume, or a bind mount that needs
the `chown` above.

---

## Layout

```
deploy/
  deploy.sh             check and prepare a machine
  diagnose-first-run.sh why the UI asks to sign in instead of offering setup
  airlock-env.sh        the settings every start script sources
  airlock.env.example   copy to airlock.env and edit
  README.md             this file
start-all.sh            build and start the stack
stop-all.sh             stop it
```
