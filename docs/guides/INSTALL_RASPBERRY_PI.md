# Installing on a Raspberry Pi

This page covers a Raspberry Pi 4 or 5 running a **64-bit** operating system.
The general guide, [Installing Airlock Photos](INSTALLATION.md), explains each
step. This page gives the Pi commands, and what is particular to the Pi.

On a Pi, every service runs in a container, on the CPU. Airlock runs well on
a Pi as a small home library. Imports, faces and video previews are slower
than on a PC, and photo descriptions with Ollama are very slow.

---

## Prerequisites

| | Notes |
|---|---|
| Model | Raspberry Pi 4 or 5. **8 GB of memory recommended**; 4 GB works without Ollama |
| Operating system | **Raspberry Pi OS (64-bit)**, Bookworm or newer. A 32-bit OS will not work: the face service needs TensorFlow, which has no 32-bit ARM version, in or out of a container |
| Storage | A USB SSD for the catalogs, not the SD card. A catalog does many small writes, which are slow on an SD card and wear it out |
| podman | From the OS: Bookworm ships 4.3. Bullseye (Debian 11) ships 3.0, which works with fallbacks |
| Cooling | A heatsink or fan. Imports keep every core busy for a long time |
| Python, ffmpeg | Not needed: these services run in containers |

**Hardware video encoding:**
- **Pi 4:** has an H.264 encoder, `/dev/video11`, and Airlock uses it when it can.
- **Pi 5:** has no H.264 encoder, so video previews are encoded in software.

Check the model and the OS:

```bash
tr -d '\0' < /proc/device-tree/model; echo
uname -m                 # must print aarch64
grep PRETTY /etc/os-release
```

---

## Installation steps

### 1. Install the tools

```bash
sudo apt update
sudo apt install podman git curl openssl uidmap
```

Log in as the user created when the Pi was set up, normally uid 1000; check
with `id -u`. Then check that the user has subordinate IDs for rootless
podman:

```bash
grep "^$USER:" /etc/subuid /etc/subgid
```

If either file has no line for you:

```bash
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"
podman system migrate
```

### 2. Let podman use ports 80 and 443, and the video encoder

```bash
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/99-airlock.conf
sudo usermod -aG render,video "$USER"
```

Log out and back in, so the group change applies.

### 3. Put the catalogs on the SSD

Mount the SSD, for example at `/mnt/ssd`, so it mounts at every boot (add it
to `/etc/fstab`). Then make a folder on it that you own:

```bash
sudo mkdir -p /mnt/ssd/airlock
sudo chown "$USER": /mnt/ssd/airlock
```

### 4. Ollama (optional, and slow)

On a Pi, Ollama runs on the CPU. The default vision model takes minutes per
photo and needs most of an 8 GB Pi's memory. You can skip it; everything else
works without it. To try it anyway:

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma4:latest
ollama pull granite3.3:8b
```

To use a smaller model, set `OLLAMA_MODEL` and `ALBUMS_LLM_MODEL` in
`deploy/airlock.env`.

### 5. Get the code, prepare, and start

```bash
git clone https://github.com/izelpi/airlock.git
cd airlock
./deploy/deploy.sh --check
./deploy/deploy.sh
nano deploy/airlock.env
```

Point the catalogs at the SSD, and the storage root at where your photos are:

```bash
CATALOGS_HOST_DIR=/mnt/ssd/airlock/catalogs
STORAGE_ROOT_HOST=/mnt/ssd/photos
```

Run `./deploy/deploy.sh` once more, so it creates those folders, then build
and start:

```bash
./start-all.sh --build
sudo systems/gateway/make-hosts.sh --install
```

The first build on a Pi takes an hour or more: the images are built on the Pi
itself. Later starts take a minute or two.

### 6. Trust the certificate

Copy `systems/gateway/certs/airlock-ca.pem` to every device that will open
Airlock, and install it there, following step 8 of the
[general guide](INSTALLATION.md#8-trust-the-certificate) or the
[Mac](INSTALL_MAC_ARM.md#6-trust-the-certificate) and
[Linux](INSTALL_LINUX_X64.md#5-trust-the-certificate) pages. For example,
from a Mac:

```bash
scp pi@<pi address>:airlock/systems/gateway/certs/airlock-ca.pem ~/Downloads/
```

### 7. Open it

From another device: **https://&lt;the Pi's address&gt;/**. `hostname -I` on
the Pi prints the address. Create the owner account, then a catalog.

---

## Troubleshooting

See also the [general troubleshooting](INSTALLATION.md#troubleshooting).
Linux problems that aren't specific to the Pi (folder ownership, SSH logout,
ports) are on the [Linux page](INSTALL_LINUX_X64.md#troubleshooting).

### "32-bit ARM: backend-deepface cannot be installed"

The OS is 32-bit (`uname -m` prints `armv7l`). Reinstall with Raspberry Pi OS
(64-bit). Nothing else can make face recognition work on a 32-bit OS.

### "podman 3.0.1 is old - using fallbacks" (Raspberry Pi OS Bullseye)

It works, with limits:
- Your user must be uid 1000.
- Containers can't share your video group, so on a Pi 4 the video service may not be able to open the encoder. It then encodes in software, and the start says so.

Raspberry Pi OS Bookworm brings podman 4.3 and removes these limits.

### The gateway says port 80 is already in use

First check that nothing else really holds it: `sudo ss -ltnp | grep -E ':(80|443) '`.
If nothing does, the old gateway container was still shutting down when the
new one started. Start it again, in the background:

```bash
cd systems/gateway && AIRLOCK_DETACH=1 ./start-podman.sh
```

### Video previews are slow

This is expected on a Pi 5, which has no hardware encoder. On a Pi 4,
`start-all.sh` and `logs/backend-transcode.log` say whether the encoder at
`/dev/video11` was usable. If it wasn't, check that your user is in the
`video` group (`id`), and log in again after adding it. To spread the work
out, set `IMPORT_DEFER_VIDEO_RENDER=1` in `deploy/airlock.env`: video
previews are then made after every file is in.

### Everything is slow, or the Pi stops responding during an import

- Check memory with `free -h`. Set `DEEPFACE_WORKERS=1`, and skip Ollama on a 4 GB Pi.
- Check the temperature with `vcgencmd measure_temp`. Above about 80 °C the Pi slows itself down.
- Keep the catalogs on the SSD, not the SD card.
