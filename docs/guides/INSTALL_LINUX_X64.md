# Installing on Linux (x86_64)

This page covers a PC or server running 64-bit Linux on an Intel or AMD
processor. The commands are for Ubuntu and Debian; on Fedora and its relatives,
use `dnf` in place of `apt`. The general guide,
[Installing Airlock Photos](INSTALLATION.md), explains each step. This page
gives the Linux commands, and what is particular to Linux.

On Linux, every service runs in a container. When the machine has a supported
GPU, the face and video services use it.

---

## Prerequisites

| | Notes |
|---|---|
| Distribution | Ubuntu 24.04 or Debian 12 or newer is best. Ubuntu 22.04 works: its podman 3.4 is old, and the scripts use fallbacks for what it lacks |
| podman | 4.7 or newer is best. Ubuntu 24.04 ships 4.9.3 |
| A normal user account | Run everything as yourself, **not** root. Podman runs *rootless*, and the files it writes stay yours |
| Memory | 8 GB or more is comfortable; 4 GB works without Ollama |
| Python, ffmpeg | Not needed: on Linux these services run in containers |

**Optional, for speed:**
- **NVIDIA GPU:** the NVIDIA driver, plus the NVIDIA Container Toolkit with a CDI spec. Podman 4.1 or newer is needed to pass the GPU in. Faces and video previews then run on the GPU.
- **Intel or AMD graphics:** `/dev/dri` present, with your user in the `render` and `video` groups. Video previews then use Quick Sync or VA-API.

---

## Installation steps

### 1. Install the tools

```bash
sudo apt update
sudo apt install podman git curl openssl uidmap
```

Rootless podman needs a range of subordinate user IDs for your account. Most
distributions create one when the user is added. Check:

```bash
grep "^$USER:" /etc/subuid /etc/subgid
```

If either file has no line for you:

```bash
sudo usermod --add-subuids 100000-165535 --add-subgids 100000-165535 "$USER"
podman system migrate
```

**Ollama (optional):**

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull gemma4:latest
ollama pull granite3.3:8b
```

The installer sets Ollama up as a system service, so it is already running.

### 2. Let podman use ports 80 and 443

This is a kernel setting on the machine itself, so `deploy.sh` doesn't change
it for you:

```bash
sudo sysctl -w net.ipv4.ip_unprivileged_port_start=80
echo 'net.ipv4.ip_unprivileged_port_start=80' | sudo tee /etc/sysctl.d/99-airlock.conf
```

The first line takes effect now; the second keeps it after a reboot. If you'd
rather not, see "Run the gateway on 8080" under Troubleshooting.

### 3. Optional: the GPU

**NVIDIA:** install the driver and the NVIDIA Container Toolkit, following
NVIDIA's instructions for your distribution, then generate the CDI spec:

```bash
sudo nvidia-ctk cdi generate --output=/etc/cdi/nvidia.yaml
nvidia-ctk cdi list            # should list nvidia.com/gpu=all
```

**Intel or AMD:**

```bash
sudo usermod -aG render,video "$USER"
```

Then log out and back in, so the new groups apply.

### 4. Get the code, prepare, and start

```bash
git clone https://github.com/izelpi/airlock.git
cd airlock
./deploy/deploy.sh --check
./deploy/deploy.sh
$EDITOR deploy/airlock.env           # CATALOGS_HOST_DIR, STORAGE_ROOT_HOST - see below
./start-all.sh --build
sudo systems/gateway/make-hosts.sh --install
```

For a server, a typical `deploy/airlock.env`:

```bash
CATALOGS_HOST_DIR=/srv/airlock/catalogs
STORAGE_ROOT_HOST=/mnt/photos
```

Create those folders **as yourself** before running `deploy.sh`, or run
`sudo chown "$USER": /srv/airlock` first. The containers run as you, so they
can write only to folders you own.

At the end, `start-all.sh` prints a line for faces and one for transcode:
`gpu` when the NVIDIA card was found, `container` otherwise, each with the
reason.

### 5. Trust the certificate

On the server itself, so that `curl` and the system trust it:

```bash
sudo cp systems/gateway/certs/airlock-ca.pem /usr/local/share/ca-certificates/airlock-ca.crt
sudo update-ca-certificates
```

Desktop browsers on Linux keep their own list, so import the same file there:
- **Firefox:** Settings ▸ Privacy & Security ▸ Certificates ▸ View Certificates ▸ Authorities ▸ Import, and tick "Trust this CA to identify websites".
- **Chrome and Chromium:** Settings ▸ Privacy and security ▸ Security ▸ Manage certificates ▸ Authorities ▸ Import.

Copy the file to your other computers and phones too; see step 8 of the
[general guide](INSTALLATION.md#8-trust-the-certificate).

### 6. Open it

From the server: **https://airlock.lan**. From any other device:
**https://&lt;server's LAN address&gt;/** (`hostname -I` prints it). Create the
owner account, then a catalog.

---

## Troubleshooting

See also the [general troubleshooting](INSTALLATION.md#troubleshooting).

### "podman … is old - using fallbacks"

This is common on Ubuntu 22.04 (podman 3.4.4). It works, with two limits:
- your user should have uid 1000, the uid the images run as (`id -u`);
- the address containers use to reach the machine is found by testing, not from podman.

Ubuntu 24.04 brings podman 4.9 and removes both limits. On 22.04,
`sudo apt install -t $(lsb_release -cs)-backports podman` may offer a newer
podman.

### "owned by uid 100999, not you"

An earlier `podman unshare chown` gave the folder to a podman-internal user.
Run `./deploy/deploy.sh` again: it hands the folder back to you. Don't `chown`
the folders by hand. The containers run as *you*, and a folder you own is
already right.

### Containers stop when you log out of SSH

`start-all.sh` runs every container detached, so closing the terminal doesn't
stop them. If they still stop at logout, systemd is ending your user's
processes. Allow them to keep running:

```bash
sudo loginctl enable-linger "$USER"
```

### Run the gateway on 8080

If you can't or won't lower the port limit:

```bash
systems/gateway/enable-port-80.sh --port-8080    # prints what to set
```

Then set `GATEWAY_PORT=8080` and `TLS_PORT=8443` in `deploy/airlock.env`, and
open `https://<address>:8443/`.

### The GPU is not used

`start-all.sh` prints why for each service. The usual reasons:
- **NVIDIA:** no CDI spec, so run `nvidia-ctk cdi list`; or podman older than 4.1.
- **Intel or AMD:** your user isn't in `render` and `video`, or you didn't log in again after adding the groups. The video service tests the encoder with a real one-frame encode before using it, so a device that can't encode falls back to software and says so.

To choose yourself, set `AIRLOCK_DEEPFACE_MODE` or `AIRLOCK_TRANSCODE_MODE`
(`auto`, `container`, `gpu` or `off`) in `deploy/airlock.env`.

### Photo descriptions fail although Ollama is running

Check that the machine answers: `curl http://localhost:11434/api/tags`. The
containers reach Ollama through the machine's own address. If Ollama listens
only on 127.0.0.1 and descriptions still fail, have it listen on all
addresses:
1. `sudo systemctl edit ollama`, and add `Environment="OLLAMA_HOST=0.0.0.0"` under `[Service]`.
2. `sudo systemctl restart ollama`.

Keep port 11434 closed to the internet in your firewall.
