# Installing Airlock Photos

This page explains how to install Airlock Photos on a computer of your own:
what the machine needs, the steps in order, and what to do when a step fails.
The steps are the same everywhere. What changes per platform is how you get the
tools, and which services can use the machine's hardware. Each platform has
its own page with the exact commands:

| Your machine | Guide | What it gets |
|---|---|---|
| A Linux PC or server (x86_64) | [Linux x64](INSTALL_LINUX_X64.md) | Everything in containers. An NVIDIA, Intel or AMD GPU is used for faces or video when present |
| A Mac with Apple Silicon (M1 and later) | [Mac (Apple Silicon)](INSTALL_MAC_ARM.md) | Faces and video run natively, on the Mac's GPU and video encoder |
| A Mac with an Intel processor | [Mac (Intel)](INSTALL_MAC_INTEL.md) | Video runs natively; faces run in a container |
| A Raspberry Pi 4 or 5, 64-bit OS | [Raspberry Pi](INSTALL_RASPBERRY_PI.md) | Everything in containers; the Pi 4's video encoder is used when it can be |
| **Any machine with Docker** | [Docker Compose](../../compose/README.md) | The easy install: published images, nothing to build. Faces and video on the CPU on a Mac |

Not sure which Mac you have? Apple menu ▸ **About This Mac**: "Chip: Apple M…"
is Apple Silicon, and "Processor: … Intel" is Intel. On Linux, `uname -m` prints
`x86_64` for a PC and `aarch64` for a 64-bit Raspberry Pi.

---

## How it fits together

Airlock is a set of small services, each in its own podman container:
- the web interface;
- the backend, which holds your catalogs and runs the import and the other workflows;
- services for faces, pets, video previews, places and albums;
- a gateway that serves all of them over HTTPS at one address.

You install a handful of tools on the machine itself. Then two scripts do the
rest: `deploy/deploy.sh` prepares the machine, and `start-all.sh` builds the
container images and starts everything.

Two services, faces (`backend-deepface`) and video previews
(`backend-transcode`), can run either in a container or natively on the
machine, where they can use its GPU or video encoder. The scripts choose for
you, and say which they chose and why. If a native run is not possible, the
service runs in a container instead: you may lose some speed, but never the
service.

---

## Prerequisites

### Hardware

| | Minimum | Recommended |
|---|---|---|
| Memory | 4 GB | 8 GB or more. Face recognition holds about 600 MB per worker, and Ollama's vision model needs several GB more |
| Free disk for the software | 15 GB | 25 GB: the container images, face models, and Ollama's models |
| Disk for the library | As big as your photos | In copy mode a catalog holds a copy of every original, plus display sizes and thumbnails. Plan on about 1.2× the size of the photos you import |
| Network | A fixed LAN address, if other devices will use it | Give the machine a reserved address in your router |

### Software on the machine

Almost everything runs in a container and brings its own dependencies. The
machine itself needs only:

| Tool | Version | Needed for |
|---|---|---|
| **podman** | 4.7 or newer is best; older versions work with fallbacks | Runs every service. On a Mac it runs inside a small Linux VM, the *podman machine* |
| **git** | any | Getting the code, and updating it |
| **curl** and **openssl** | any | Health checks, and the gateway's certificate |
| **Ollama** | any | Optional. Photo descriptions and album suggestions. Everything else works without it |
| **Python 3.10 to 3.13** (3.12 preferred) | | Only for the services that run natively, which is on a Mac. Linux does not need it |
| **ffmpeg** | any | Only for native video previews, again on a Mac |

You do **not** need Node.js, ExifTool or a database server: these are inside
the images.

### Ports

The gateway uses ports **80** and **443** (80 only redirects to 443). The
backend uses **5000**, and the other services use 3000, 3001 and 5001–5006 on the machine
itself. Nothing else may be listening on those ports.

---

## Installation steps

### 1. Install the tools

Follow your platform's page for the exact commands: podman, git, curl, openssl
and, optionally, Ollama. On a Mac, also create and start the podman machine.

### 2. Get the code

Put it anywhere you like; the scripts work out everything from where the
folder is:

```bash
git clone https://github.com/izelpii/airlock.git
cd airlock
```

### 3. Check the machine

```bash
./deploy/deploy.sh --check
```

This changes nothing. It lists each requirement with ✓ (ready), ! (worth
knowing, or still to do) or ✗ (must be fixed). On a fresh machine, missing
directories and a missing certificate show as "still to do". The next step
creates them.

### 4. Prepare the machine

```bash
./deploy/deploy.sh
```

This:
1. checks podman and the tools above;
2. writes `deploy/airlock.env`, the one file of settings for this machine;
3. creates the folders the containers use, and checks you own them;
4. generates the gateway's HTTPS certificate, from a private certificate authority made for this machine;
5. on macOS, lets podman use ports 80 and 443. On Linux it prints the one `sudo` command that does this, and you run it yourself;
6. downloads the face detector, and builds the Python environments for any service that will run natively.

You can run it again at any time; it only changes what isn't ready.

### 5. Say where your photos are

Open `deploy/airlock.env`. Every setting has a default that works, so change
only what is different on your machine:

| Setting | What it is | Default |
|---|---|---|
| `CATALOGS_HOST_DIR` | Where catalogs are kept: originals (in copy mode), thumbnails, albums. **Back this folder up** | `<airlock folder>/catalogs` |
| `STORAGE_ROOT_HOST` | The one folder Airlock may read photos from, read-only. Every import source must be inside it | Your home folder |
| `INPUT_HOST_DIR` | An inbox folder for imports | `<airlock folder>/IN` |
| `DATABASE_HOST_DIR` | The application database: users, catalogs, settings | `<airlock folder>/systems/backend/database` |

If your photos are on an external or network disk, set `STORAGE_ROOT_HOST` to
a folder that contains them, such as `/Volumes/Photos` or `/mnt/photos`. A
folder outside it cannot be read, whatever the interface allows you to type.

### 6. Build and start

```bash
./start-all.sh --build
```

The first build takes a while: 10–30 minutes on a fast machine, and longer on
a Raspberry Pi. Each service is built, started, and checked through the
gateway. At the end, the script prints which mode each accelerated service
chose, and any names or settings still missing.

### 7. Let this computer find Airlock by name

```bash
sudo systems/gateway/make-hosts.sh --install
```

This adds `airlock.lan` and the service names to `/etc/hosts`, pointing at this
machine. `start-all.sh` tells you when they are missing.

### 8. Trust the certificate

The gateway's certificate comes from a certificate authority made for this
installation, so browsers warn about it until they trust that authority. Its
file is `systems/gateway/certs/airlock-ca.pem`. Install it once on every
device that will open Airlock: this machine, other computers, and phones. Your
platform's page has the command for that platform:
- **iPhone or iPad:** AirDrop the file, install the profile, then turn it on in Settings ▸ General ▸ About ▸ Certificate Trust Settings.
- **Android:** Settings ▸ Security ▸ Encryption & credentials ▸ Install a certificate ▸ CA certificate.

### 9. Create the owner account

Open **https://airlock.lan** on this machine. From another device, open
**https://&lt;this machine's LAN address&gt;/**, not `airlock.lan`: that
name only means "this computer". A new installation has no account and no
default password. It offers **"Welcome to Airlock Photos — create the
administrator account"**, which creates the owner. You can add other people
later on the Users page.

### 10. Import your first photos

1. **Admin ▸ Catalogs ▸ Create Catalog**. Give it a name, and choose how it treats your files: copy them in, leave them where they are and index them, or move them in.
2. In the catalog's **Storage routes** tab, choose **Add a storage route**, and give it a folder inside `STORAGE_ROOT_HOST`. This also creates an **Import** workflow for that folder.
3. On **Workflows**, press ▶ on the import. Photos appear while it runs. When it finishes, the next steps are queued after it: events, faces, places, and descriptions.

### Day to day

| To | Run |
|---|---|
| Start after a reboot | `./start-all.sh` (nothing starts the stack at boot on its own) |
| Stop everything | `./stop-all.sh` |
| See what is running, and whether it answers | `./deploy/status.sh` |
| Update to a new version | `git pull`, then `./start-all.sh`. Each service rebuilds itself when its code changed |
| Rebuild particular services | `./start-all.sh --build=frontend,backend` |

Restarting interrupts running workflows and ends album-transfer codes that are
still waiting. Check the Workflows page before you restart.

---

## Troubleshooting

Start with these three commands. They change nothing, and between them they
name most problems:

```bash
./deploy/deploy.sh --check         # what this machine is missing
./deploy/status.sh                 # which services are up, and whether they answer
./deploy/diagnose-first-run.sh     # why the first page asks you to sign in
```

Each service writes a log in `logs/`, for example `logs/backend.log` and
`logs/gateway.log`.

### The page does not load at all

- **Is the gateway running?** `podman ps` should list `airlock-gateway`. If it isn't listed, read `logs/gateway.log`.
- **Ports 80 and 443 refused.** Rootless podman may not open ports below a limit, which is 1024 by default. `deploy.sh` lowers it on a Mac. On Linux, run the command it printed: `sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80`, plus the line that makes it permanent. Or run the gateway on 8080 and 8443 instead: `systems/gateway/enable-port-80.sh --port-8080`.
- **The name doesn't resolve.** `systems/gateway/make-hosts.sh --check`; fix with `sudo systems/gateway/make-hosts.sh --install`. From another device, use the LAN address, not `airlock.lan`.

### The browser warns that the connection is not private

The certificate authority isn't trusted on that device yet (step 8). Each
installation has its **own** authority. A device that uses two Airlock
machines needs both installed. If you added a new address to the machine,
regenerate the certificate so it covers that address: `cd systems/gateway &&
./make-certs.sh --force && ./start-podman.sh`. This keeps the same authority,
so devices don't need the file again.

### A new installation shows a sign-in form, not "create the administrator account"

This has four possible causes. `./deploy/diagnose-first-run.sh` names which
one:
- the address reached a different Airlock, often another machine's `airlock.lan`;
- an account already exists, because `DATABASE_HOST_DIR` was copied from an earlier installation;
- the page came from a service port such as `:3000`, which has no `/api`;
- the backend is up but failing; read `logs/backend.log`.

### A service does not start

- Read its log: `logs/<service>.log`.
- **"port … already in use"** or **"proxy already running"**: an old container still holds the port. `podman ps -a` shows it; `podman stop <name>`, then start again.
- **The backend never comes up on a Mac:** the AirPlay Receiver holds port 5000. See the Mac pages.
- Rebuild just that service: `./start-all.sh --build=<service>`. `./start-all.sh --help` lists the names.

### The containers cannot write their folders (Linux)

The folders must belong to you, and `deploy.sh` checks this. Run it again: it
gives back any folder that an earlier `chown` handed to a podman-internal
user. Rootless podman also needs a subuid/subgid range for your user, and
`deploy.sh` prints the command if it is missing.

### Faces are slow, or the face service restarts

Each face worker holds about 600 MB. On a Mac, every container shares the
podman machine's memory, 2 GB by default, not the Mac's. Give it 6 GB:
`podman machine stop && podman machine set --memory 6144 && podman machine start`.
Elsewhere, close other heavy programs, or set `DEEPFACE_WORKERS=1` in
`deploy/airlock.env`.

### Photo descriptions or album suggestions fail

These need Ollama on the machine itself, listening on port 11434, with its
models pulled: `ollama pull gemma4:latest` and `ollama pull granite3.3:8b`.
`deploy.sh --check` reports whether it answers. Everything else in Airlock
works without it.

### A pip error during `deploy.sh` ("ResolutionImpossible")

A native service was being built with a Python outside 3.10–3.13. Install
3.12, or point at one in `deploy/airlock.env`:
`AIRLOCK_PYTHON=/path/to/python3.12`. Then run `./deploy/deploy.sh` again.
It replaces an environment that was built with the wrong Python.

More detail on all of this is in the [deploy README](../../deploy/README.md)
and the [startup guide](STARTUP_GUIDE.md).
