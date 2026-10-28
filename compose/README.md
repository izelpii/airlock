# Installing Airlock Photos with Docker Compose

This is **the easy install**: published images, one `docker-compose.yml`, and
nothing to build. It suits a Linux server or NAS, a Raspberry Pi, or any
machine that already runs Docker.

The other way to install, the [script install](../docs/guides/INSTALLATION.md),
builds the images on your machine with podman. It is **the accelerated
install**: on a Mac only the script install runs faces and video natively,
on the GPU and the hardware video encoder. Choose it on a Mac where speed
matters, or if you use podman.

Both run the same application on the same data layout, so you can move
between them later (see "Moving from the script install").

---

## Prerequisites

| | |
|---|---|
| **Docker** | Docker Engine 24+ with the compose plugin, version 2.24 or newer (`docker compose version`), or Docker Desktop on a Mac or Windows |
| **Machine** | Linux x86_64, Linux arm64 (a Raspberry Pi 4 or 5 with a 64-bit OS), or a Mac (Intel or Apple Silicon) with Docker Desktop |
| **Memory** | 8 GB or more is comfortable. In Docker Desktop, give the VM at least 6 GB (Settings ▸ Resources) |
| **Disk** | About 7 GB for the images, plus your library: in copy mode, roughly the size of the photos you import |
| **Ports** | 80 and 443 free on this machine |
| **Ollama** | Optional: photo descriptions and album suggestions. Everything else works without it |

---

## Install

### 1. Get the files

```bash
mkdir airlock && cd airlock
for f in docker-compose.yml .env.example settings.env.example install.sh \
         docker-compose.gpu.yml docker-compose.podman.yml; do
  curl -fsSLO "https://raw.githubusercontent.com/izelpii/airlock/main/compose/$f"
done
chmod +x install.sh
```

Or clone the repository and work in its `compose/` folder.

### 2. Run the installer

```bash
./install.sh
```

It asks two questions:
- **The folder your photos are under.** Airlock reads from there. Every storage route you add in the app must be inside it.
- **This machine's address on your network.** It goes into the HTTPS certificate, so `https://<address>/` works without a warning. The installer suggests the address it finds.

It then writes `.env`, creates the data folders as you (not as root), and
starts everything. The first start downloads about 6 GB of images, and the
faces service needs a few more minutes to get ready.

**Without the installer:** `cp .env.example .env`, edit `STORAGE_ROOT`,
`PUID`/`PGID` (from `id -u` and `id -g`) and `AIRLOCK_NAMES`, then
`mkdir -p data/catalogs data/database data/IN license certs` and
`docker compose up -d`.

### 3. Trust the certificate

The first start creates a certificate authority for this installation, in
`certs/airlock-ca.pem`. Install it on every device that will open Airlock, or
browsers will warn:
- **Mac:** `sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain certs/airlock-ca.pem`
- **Linux:** `sudo cp certs/airlock-ca.pem /usr/local/share/ca-certificates/airlock-ca.crt && sudo update-ca-certificates`. Firefox and Chrome also need it in their own settings: Certificates ▸ Authorities ▸ Import.
- **iPhone, iPad:** AirDrop the file, install the profile, then turn it on in Settings ▸ General ▸ About ▸ Certificate Trust Settings.
- **Android:** Settings ▸ Security ▸ Encryption & credentials ▸ Install a certificate ▸ CA certificate.

### 4. Open it

Go to **https://&lt;this machine's address&gt;/**. A new installation asks you
to create the administrator account; there is no default password. Then
create a catalog and add a storage route. The
[Introduction](../docs/guides/INTRODUCTION.md) explains what those are.

To use **https://airlock.lan** on this machine as well, add
`127.0.0.1 airlock.lan` to `/etc/hosts`.

---

## Day to day

| To | Run |
|---|---|
| See what is running, and whether it is healthy | `docker compose ps` |
| Read a service's log | `docker compose logs -f backend` |
| Stop everything | `docker compose down` |
| Start again | `docker compose up -d` (also happens on its own after a reboot) |
| Update | `docker compose pull && docker compose up -d` |

**An update restarts the services, which interrupts running workflows** and
ends album-transfer codes that are still waiting. Check the Workflows page
first.

**Pin a version** by setting `AIRLOCK_VERSION=6.6.0` in `.env`, instead of
`latest`. To update, change the number and run the update command.

**Back up** `data/catalogs` and `data/database`, or wherever `.env` points
them. Each catalog's Backup tab runs backups to another disk or machine with
restic, borg or rsync. Keep a copy of `certs/` too, so a restored machine
keeps the certificate authority your devices trust.

---

## Settings

`.env` holds what is specific to this machine; every line is explained in
`.env.example`. Advanced tuning (imports, faces, video previews, pets) goes
in an optional `settings.env`: copy `settings.env.example` and uncomment what
you need. A setting that isn't there keeps the application's own default.

After changing either file: `docker compose up -d`.

### Ollama

- **On this machine (the default):** install it from [ollama.com](https://ollama.com), then `ollama pull gemma4:latest` and `ollama pull granite3.3:8b`.
  - **On a Mac** this is the right choice: Ollama uses the GPU there, and in a container it couldn't.
  - **On Linux** Ollama must listen on all addresses: run `sudo systemctl edit ollama`, add `Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]`, then restart it. Keep port 11434 closed to the internet.
- **In the stack:** set `OLLAMA_UPSTREAM=ollama` in `.env`, then run `docker compose --profile ollama up -d`. The models download on the first start.

### NVIDIA

Install the NVIDIA driver and the NVIDIA Container Toolkit. Set
`DEEPFACE_VARIANT=-gpu` in `.env`, then start with both files:

```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d
```

Faces, video previews and an in-stack Ollama then use the GPU.

### Hardware video (Intel, AMD, Raspberry Pi 4)

1. In `docker-compose.yml`, uncomment the `devices:` lines of the `transcode` service: `/dev/dri` for Intel and AMD, `/dev/video11` for a Pi 4.
2. Set `VIDEO_GIDS` in `.env` to the device's groups, for example `VIDEO_GIDS=$(getent group render | cut -d: -f3),$(getent group video | cut -d: -f3)`.

The video service tries the encoder with a one-frame encode and falls back to
software if it can't use it. A Pi 5 has no hardware encoder.

### Pets

The pet detector comes with the image. The pet *identity* model can't be
downloaded: it has to be exported once from PyTorch (see
`tools/fetch_pet_weights.sh` in the repository). Copy it in with:

```bash
docker compose cp animal_clip_vit_b32.onnx pets:/app/weights/
docker compose restart pets
```

### Podman

Podman works, best-effort:

```bash
podman compose -f docker-compose.yml -f docker-compose.podman.yml up -d
```

Rootless podman can't open ports 80 and 443 until the port floor is lowered:
`sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80`, or set
`HTTP_PORT=8080` and `HTTPS_PORT=8443`. For podman, the script install is
the better-supported choice.

---

## Who owns the files

Airlock writes its catalogs and database as the user in `PUID` and `PGID`,
so they belong to you on this machine.

Each service starts as root only long enough to hand its *own* working
folders (model caches, its home folder) to that user. It then runs as that
user. It never changes the ownership of your photos, catalogs or database.
The installer creates those folders as you, before Docker can create them as
root.

If a log says *Permission denied* on `/catalogs` or `/app/database`, the
folder belongs to someone else. Fix it with `sudo chown -R $(id -u):$(id -g) data`.

---

## Moving from the script install

1. In `.env`, point `CATALOGS_DIR`, `DATABASE_DIR` and `INPUT_DIR` at the folders the script install used (its `deploy/airlock.env`), and `STORAGE_ROOT` at its `STORAGE_ROOT_HOST`.
2. Copy `systems/gateway/certs/*` into `./certs`, so your devices keep trusting the same authority.
3. Stop the script install (`./stop-all.sh`), then run `docker compose up -d`.

To move back, stop compose and start the script install. The data is the
same.

---

## Troubleshooting

| What you see | What to do |
|---|---|
| `set STORAGE_ROOT in .env` | `.env` is missing or has no `STORAGE_ROOT`. Run `./install.sh`, or edit `.env` |
| A service stays `unhealthy` | `docker compose logs <service>`. Faces take several minutes on the first start, while models download |
| The browser warns about the certificate | Install `certs/airlock-ca.pem` on that device. If you opened it by an address not in `AIRLOCK_NAMES`, add it, then `docker compose run --rm certs --force && docker compose restart gateway`. That renews the certificate but keeps the authority, so devices need nothing new |
| `port is already allocated` | Something else uses 80 or 443. Stop it, or set `HTTP_PORT`/`HTTPS_PORT` in `.env` |
| A sign-in form on a new installation | You reached another Airlock (`airlock.lan` means *this* computer), or `DATABASE_DIR` holds an earlier installation's accounts |
| Descriptions fail | Ollama isn't reachable: see "Ollama" above. Run `docker compose logs gateway` to see which address it uses |
| Faces restart, "out of memory" | Set `DEEPFACE_WORKERS=1` in `.env`, or give Docker Desktop's VM more memory |

---

## Building the images

The images are built from the application unchanged, plus a thin layer
from `compose/images/`:

```bash
compose/ci/build.sh                      # everything, for this machine, tagged localhost/...:dev
compose/ci/build.sh backend gateway      # just these
compose/ci/smoke-test.sh                 # start them on spare ports and check a first run
compose/ci/any-uid-test.sh               # every image, as an unrelated user
python3 -m pytest compose/tests          # the compose install stays in step with the application
```

Run local builds with `AIRLOCK_IMAGES=localhost` and `AIRLOCK_VERSION=dev`
in `.env`. Releases are published to `ghcr.io` by
`.github/workflows/compose-images.yml`. It runs `compose/ci/build.sh --push
--arch amd64` and `--arch arm64`, each on a native machine, then
`compose/ci/build.sh --merge` to join them into the release tags.

Everything this install method needs lives in this folder. The one exception
is that workflow file, which GitHub requires in `.github/`. The design is in
the [compose install spec](../docs/specs/compose-install-spec.md).
