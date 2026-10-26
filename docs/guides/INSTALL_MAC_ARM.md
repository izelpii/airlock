# Installing on a Mac with Apple Silicon

This page covers a Mac with an M1, M2, M3 or later chip. The general guide,
[Installing Airlock Photos](INSTALLATION.md), explains each step. This page
gives the Mac commands, and what is particular to the Mac.

On Apple Silicon, two services run natively on the Mac rather than in a
container:
- **faces**, on the GPU (Metal);
- **video previews**, on the hardware encoder (VideoToolbox).

Everything else runs in podman's Linux VM.

---

## Prerequisites

| | Notes |
|---|---|
| macOS | 13 Ventura or newer |
| Memory | 16 GB is comfortable; 8 GB works, with Ollama's vision model the tightest fit |
| [Homebrew](https://brew.sh) | To install the tools |
| podman | Installed with Homebrew; it runs a small Linux VM, the *podman machine* |
| Python 3.12 | For the native face and video services. 3.10 to 3.13 work; **not** Apple's built-in 3.9, and not 3.14 |
| ffmpeg | For native video previews |
| AirPlay Receiver **off** | It holds port 5000, which the backend needs |

---

## Installation steps

### 1. Install the tools

```bash
brew install podman git python@3.12 ffmpeg
```

`curl` and `openssl` come with macOS.

### 2. Create the podman machine

```bash
podman machine init --cpus 4 --memory 6144 --disk-size 60
podman machine start
```

All containers share this VM's memory, not the Mac's. The default 2 GB is too
little, and 6 GB is comfortable. You can change it later with
`podman machine stop && podman machine set --memory 8192 && podman machine start`.

After a Mac restart, start the VM again with `podman machine start` before
`./start-all.sh`.

### 3. Turn off the AirPlay Receiver

**System Settings ▸ General ▸ AirDrop & Handoff ▸ AirPlay Receiver: off.**

Since macOS Monterey it listens on port 5000, the backend's port. With it on,
the backend can't start, and `deploy.sh` and `start-all.sh` stop with this
message.

### 4. Ollama (optional)

Download it from [ollama.com](https://ollama.com), or `brew install ollama`.
Start it, then:

```bash
ollama pull gemma4:latest
ollama pull granite3.3:8b
```

Ollama runs natively and uses the GPU.

### 5. Get the code, prepare, and start

```bash
git clone https://github.com/izelpi/airlock.git
cd airlock
./deploy/deploy.sh --check
./deploy/deploy.sh
open -e deploy/airlock.env          # set STORAGE_ROOT_HOST if your photos are on another disk
./start-all.sh --build
sudo systems/gateway/make-hosts.sh --install
```

`deploy.sh` also lets the podman machine open ports 80 and 443. This changes
a setting inside the VM only, not the Mac. Then it builds the Python
environments for the face and video services and downloads the face models;
this takes several minutes the first time.

If your photos are on an external disk, a typical `deploy/airlock.env` is:

```bash
STORAGE_ROOT_HOST=/Volumes/Photos
```

At the end, `start-all.sh` should report `host` for both faces and transcode.

### 6. Trust the certificate

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain systems/gateway/certs/airlock-ca.pem
```

Safari and Chrome use this. Firefox keeps its own list: Settings ▸ Privacy &
Security ▸ Certificates ▸ View Certificates ▸ Authorities ▸ Import. For phones
and other computers, see step 8 of the
[general guide](INSTALLATION.md#8-trust-the-certificate).

### 7. Open it

**https://airlock.lan**. Create the owner account, then a catalog. From other
devices, use the Mac's address: System Settings ▸ Network shows it.

---

## Troubleshooting

See also the [general troubleshooting](INSTALLATION.md#troubleshooting).

### "port 5000 … AirPlay Receiver"

Turn the receiver off (step 3), then `./start-all.sh` again. If another
program holds port 5000, the message names it.

### "podman machine is not running"

Run `podman machine start`. If it won't start, `podman machine list` shows its
state, and `podman machine stop` then `podman machine start` usually clears a
stuck one.

### "Cannot connect to Podman" or a build stops right at the start

The VM is up but podman can't reach it, which can happen after the Mac sleeps.
Run `podman machine stop && podman machine start`, then run the script again.

### Faces or video report `container` instead of `host`

`start-all.sh` prints the reason. The usual reasons:
- **"no python from 3.10 to 3.13 found":** run `brew install python@3.12`, then `./deploy/deploy.sh`.
- **A pip "ResolutionImpossible" during `deploy.sh`:** an environment was built with Python 3.14, which TensorFlow doesn't support yet. Install 3.12 and run `./deploy/deploy.sh` again; it rebuilds that environment. To force a particular Python, set `AIRLOCK_PYTHON=/opt/homebrew/bin/python3.12` in `deploy/airlock.env`.
- **ffmpeg missing** (video only): run `brew install ffmpeg`.

The service still works in a container, only slower.

### The face service keeps restarting

The podman machine is short of memory (step 2). This matters mostly while the
face service runs in a container; native faces use the Mac's own memory.

### External disk not visible

Containers see only the Mac folders that the podman machine shares with its
VM, and your home folder is always one of them. Run
`podman machine inspect | grep -A10 Mounts` to see the list. If the disk under
`/Volumes` isn't in it, create the machine again with that folder shared too:

```bash
podman machine stop && podman machine rm
podman machine init --cpus 4 --memory 6144 --disk-size 60 \
  -v "$HOME:$HOME" -v /Volumes:/Volumes
podman machine start
./start-all.sh --build        # the images went with the old machine
```

The disk must also be mounted before `./start-all.sh`, because a container's
folders are fixed when it starts.
