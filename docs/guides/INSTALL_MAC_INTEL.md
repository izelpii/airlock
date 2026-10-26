# Installing on a Mac with an Intel processor

This page covers an Intel-based Mac. The general guide,
[Installing Airlock Photos](INSTALLATION.md), explains each step. This page
gives the Mac commands, and what is particular to an Intel Mac.

On an Intel Mac:
- **Video previews** run natively, on the hardware encoder (VideoToolbox).
- **Faces** run in a container. This is not a choice: TensorFlow, which the
  face service needs, published its last macOS Intel version at 2.16, and the
  service needs 2.20 or newer. The container is Linux, where current
  TensorFlow exists, so faces work, on the CPU.

Everything else runs in podman's Linux VM.

---

## Prerequisites

| | Notes |
|---|---|
| macOS | 13 Ventura or newer |
| Memory | 16 GB recommended. The face service runs inside the podman machine, so the VM needs 6 GB of its own |
| [Homebrew](https://brew.sh) | To install the tools |
| podman | Installed with Homebrew; it runs a small Linux VM, the *podman machine* |
| Python 3.12 | For the native video service. 3.10 to 3.13 work; **not** Apple's built-in 3.9, and not 3.14 |
| ffmpeg | For native video previews |
| AirPlay Receiver **off** | It holds port 5000, which the backend needs (macOS 12 and later) |

---

## Installation steps

### 1. Install the tools

```bash
brew install podman git python@3.12 ffmpeg
```

### 2. Create the podman machine, with room for faces

```bash
podman machine init --cpus 4 --memory 6144 --disk-size 60
podman machine start
```

The face service runs in this VM and holds about 600 MB per worker. The
scripts size it to the memory the VM has. Below 3 GB they skip loading the
model at start-up, so the service starts but recognition is slow. Give it
6 GB, or 8 GB if the Mac can spare it.

After a Mac restart, start the VM again with `podman machine start` before
`./start-all.sh`.

### 3. Turn off the AirPlay Receiver

**System Settings ▸ General ▸ AirDrop & Handoff ▸ AirPlay Receiver: off.**
(On macOS 12: System Preferences ▸ Sharing ▸ AirPlay Receiver.)

### 4. Ollama (optional)

Download it from [ollama.com](https://ollama.com), or `brew install ollama`.
Start it, then:

```bash
ollama pull gemma4:latest
ollama pull granite3.3:8b
```

On an Intel Mac, Ollama runs on the CPU, so descriptions are slow. You can
skip it; everything else works without it.

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

`deploy.sh` lets the podman machine open ports 80 and 443, and builds the
Python environment for the video service. At the end, `start-all.sh` should
report:
- **faces: `container`**, with the TensorFlow reason above;
- **transcode: `host`**.

### 6. Trust the certificate

```bash
sudo security add-trusted-cert -d -r trustRoot \
  -k /Library/Keychains/System.keychain systems/gateway/certs/airlock-ca.pem
```

Firefox keeps its own list: Settings ▸ Privacy & Security ▸ Certificates ▸
View Certificates ▸ Authorities ▸ Import. For phones and other computers, see
step 8 of the [general guide](INSTALLATION.md#8-trust-the-certificate).

### 7. Open it

**https://airlock.lan**. Create the owner account, then a catalog.

---

## Troubleshooting

See also the [general troubleshooting](INSTALLATION.md#troubleshooting) and,
for problems common to every Mac (AirPlay, the podman machine, external
disks), the [Apple Silicon page](INSTALL_MAC_ARM.md#troubleshooting).

### Faces run in a container, not natively

This is expected on an Intel Mac and can't be changed (see the top of this
page). Setting `AIRLOCK_DEEPFACE_MODE=host` doesn't help: the scripts see
that a native install can't succeed and run the container anyway, saying why.

### Face detection is slow, or the face service restarts

The service runs on the CPU in the VM:
- Give the VM more memory and CPUs: `podman machine stop && podman machine set --cpus 6 --memory 8192 && podman machine start`.
- Set `DEEPFACE_WORKERS=1` in `deploy/airlock.env` if memory is tight.
- Start large imports in the evening: the faces step is queued after each import and runs on its own.

### Video reports `container` instead of `host`

`start-all.sh` prints the reason. Usually Python 3.12 or ffmpeg is missing:
run `brew install python@3.12 ffmpeg`, then `./deploy/deploy.sh`. Previews
still work in a container, without the hardware encoder.
