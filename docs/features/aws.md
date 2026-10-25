# Deploying an album gallery to AWS EC2

How to put one published deployment on the internet on a single EC2 instance,
with HTTPS and, if the deployment has one, its password still enforced.

The short version: **pack the deployment with `tools/pack_deployment.py`, run
the bundle with Docker Compose on a small instance, and let Caddy get the
certificate over HTTPS.** Everything below is the detail of doing that once,
then keeping it updated.

The instance only needs **port 443** open to the world. Port 80 stays closed.

The packer itself is described in [PACKING-TOOL.md](PACKING-TOOL.md). This guide
covers the AWS side and does not repeat it.

**Airlock can now do steps 4–6 for you.** Since v6.2.0, a deployment card has a
**Publish to AWS** button (owners): the first time, it adds the instance as a
server; then **Set up** once, and **Publish to AWS** after each change. It packs, sends only what changed, keeps `.env`,
and checks the site. You still do steps 1–3 (the instance, DNS, Docker and the
gallery image) yourself; Airlock never holds AWS credentials. The user guide
[PUBLISH_TO_EC2.md](../guides/PUBLISH_TO_EC2.md) walks through both the button
(§3A) and the same work by hand with `tools/pack_deployment.py --stage` and
`rsync`. The by-hand steps below still work.

---

## Why this shape

| Choice | Why |
|---|---|
| **A packed bundle**, not the Airlock stack | The instance gets the album's derivatives and nothing else: no originals, no catalog database, no backend, and no network path back home. A 3.9 GB catalog packs to ~38 MB. |
| **Docker Compose + Caddy** (`--target node`) | It is what the packer emits. Caddy obtains and renews the TLS certificate on its own, and the gallery server enforces the deployment's password. |
| **One EC2 instance** | A gallery is static files plus a small Node server. There is nothing here that needs a load balancer, ECS or Kubernetes. |
| **HTTPS only** | Caddy proves domain ownership to Let's Encrypt over port 443 itself (the TLS-ALPN challenge), so port 80 is never opened. |
| **Intel or Graviton** | Either works. What matters is building the image for the instance's architecture, not the Mac's. See [step 3](#3-get-the-gallery-image-to-the-instance). |

**When not to use EC2.** If the deployment has **no password**, the
`--target static` bundle on S3 + CloudFront is cheaper and has no server to
patch. A static host cannot check a password, so a locked deployment needs the
approach in this guide. See [Alternatives](#alternatives).

---

## What you need

- An AWS account, and the AWS CLI configured on this Mac (`aws configure`).
- A domain name you can add DNS records to, e.g. `photos.example.com`.
- The deployment **published** in Airlock (**Systems → Deployments**). Check
  *What to pack* on the back of its card first: every ticked size ends up in
  the bundle, and *Full resolution JPG* makes it several times larger.

The examples use catalog `family`, deployment `album2026`, domain
`photos.example.com`, region `us-east-1`. Replace them with your own.

---

## 1. Launch the instance

In the EC2 console, **Launch instance**:

| Setting | Value |
|---|---|
| AMI | **Ubuntu Server LTS** (24.04 or 26.04), 64-bit, **x86** or **Arm**. Note which one: the image is built to match (step 3). |
| Instance type | A micro or small: `t3.micro`/`t3.small` (Intel) or `t4g.micro`/`t4g.small` (Graviton, a little cheaper). A 1 GB instance is enough for a family album with a few viewers at a time, with swap added in step 2. |
| Key pair | Create one, or use an existing one. You need it for `ssh`/`scp`. |
| Storage | gp3, **20 GB** plus the bundle size. See [Disk](#disk) before accepting the 8 GB default. |

Check an existing instance's architecture with `uname -m`: `x86_64` is Intel,
`aarch64` is Arm.

### Security group

Exactly these inbound rules:

| Port | Source | Why |
|---|---|---|
| 22 (SSH) | **My IP** only | Administration. Never `0.0.0.0/0`. |
| 443 (HTTPS) | `0.0.0.0/0`, `::/0` | The gallery, and the certificate check. |

**Port 80 is not opened.** Caddy gets its certificate over 443, so HTTP is
never needed. The one visible effect is that nothing redirects `http://` to
`https://`: viewers must use the `https://` link, which is the link you send
them anyway.

Port 3001 (the gallery server) is **not** opened either. Only Caddy talks to
it, over the Compose network.

Check the rule from the Mac once the stack is running (step 5):

```bash
curl -sI -m 10 https://photos.example.com/ | head -1
```

A **timeout** means the security group (or a network ACL) is still blocking
443. The instance's own firewall (`ufw`) is inactive on Ubuntu's AWS images.

### A stable address

The public IP of an instance changes when it is stopped and started. Allocate
an **Elastic IP** (EC2 → Elastic IPs → Allocate) and associate it with the
instance.

### DNS

At your DNS provider (or Route 53), create an **A record**:

```
photos.example.com.   A   <the Elastic IP>
```

Do this **before** starting the stack. Caddy asks for the certificate on its
first start, and fails if the name does not resolve to this machine yet.

Check it from the Mac:

```bash
dig +short photos.example.com     # must print the Elastic IP
```

### No domain yet? Use sslip.io for a test

`203-0-113-10.sslip.io` resolves to `203.0.113.10`, for any IP written that way,
with no setup. Let's Encrypt issues certificates for these names, so a test
gallery gets real HTTPS before you own a domain:

```bash
--domain 203-0-113-10.sslip.io      # in step 4, using your instance's IP
```

It is for testing only: the name is tied to the IP, and it changes if the IP
does. Moving to your own domain later is one line in the bundle's `Caddyfile`
followed by `docker compose restart caddy`.

---

## 2. Install Docker on the instance

```bash
ssh -i ~/.ssh/YOUR-KEY.pem ubuntu@photos.example.com

sudo apt update && sudo apt -y upgrade
sudo apt -y install docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit        # log out and back in so the group change takes effect
```

Check it after logging back in:

```bash
docker compose version
```

### Swap, on a 1 GB instance

A micro instance has about 900 MB of usable memory. The gallery and Caddy fit,
but swap keeps an unusual spike (a burst of viewers, an `apt upgrade` running
alongside) from killing a container:

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

The 2 GB comes out of the disk; see [Disk](#disk).

### Disk

Check it before packing a real album:

```bash
df -h /
```

What a gallery uses:

| Item | Size |
|---|---|
| Ubuntu + Docker | ~2.5 GB |
| Swap file | 2 GB |
| Gallery image + Caddy image | ~450 MB |
| The bundle | its `dereferenced` size from step 4 |
| An update | room for a second copy of the bundle while it uploads |

AWS's default 8 GB root volume leaves about 1.5 GB after the first three. That
is enough for a test album of ~100 MB, not for a real one. Grow the volume
first: EC2 → Volumes → the instance's volume → **Modify volume** → 30 GB.
Then, on the instance:

```bash
sudo growpart /dev/nvme0n1 1 && sudo resize2fs /dev/root    # or /dev/xvda on older types
df -h /                                                     # confirm the new size
```

No reboot is needed.

Ubuntu applies security updates automatically (`unattended-upgrades` is on by
default). Leave it on.

---

## 3. Get the gallery image to the instance

The bundle carries **data only**. `compose.yml` names the gallery image, and the
instance has to be able to get it.

Build it from the current code **for the instance's architecture**, which is
not necessarily the Mac's. This Mac is Arm; an image built for it without
`--platform` fails on an Intel instance with `exec format error`.

| Instance `uname -m` | `--platform` |
|---|---|
| `x86_64` (Intel/AMD, e.g. `t3`) | `linux/amd64` |
| `aarch64` (Graviton, e.g. `t4g`) | `linux/arm64` |

```bash
cd systems/album-gallery
podman build --platform linux/amd64 \
  --ignorefile .dockerignore -f Dockerfile \
  --build-arg GALLERY_REVISION=$(git log -1 --format=%h -- .) \
  -t album-gallery:amd64 .
```

`GALLERY_REVISION` is optional. The gallery reports it at `/health`, so you can
tell which version a server runs, and the **Publish to AWS** button uses it to
point out a server running an older gallery.

Building for Intel on this Mac runs under emulation. It takes a few minutes
rather than seconds, and it works.

Then use one of these two options.

### Option A: Amazon ECR (for a gallery you update often)

A private registry in the same region. The instance pulls from it with an IAM
role, so there is no password to store on the machine, and updating the
gallery later is `docker compose pull`.

**Once, on the Mac:**

```bash
REGION=us-east-1
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGISTRY=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com

aws ecr create-repository --repository-name album-gallery --region $REGION
```

**Each time the gallery changes, on the Mac:**

```bash
aws ecr get-login-password --region $REGION \
  | podman login --username AWS --password-stdin $REGISTRY

podman tag album-gallery:amd64 $REGISTRY/album-gallery:latest
podman push $REGISTRY/album-gallery:latest
```

**Once, for the instance:**

1. IAM → Roles → **Create role** → trusted entity *EC2* → attach
   **AmazonEC2ContainerRegistryReadOnly** → name it `album-gallery-ec2`.
2. EC2 → the instance → Actions → Security → **Modify IAM role** → pick
   `album-gallery-ec2`.
3. On the instance, install the credential helper so Docker logs in to ECR on
   its own:

   ```bash
   sudo apt -y install amazon-ecr-credential-helper
   mkdir -p ~/.docker
   echo '{"credsStore": "ecr-login"}' > ~/.docker/config.json
   ```

Pack with `--image $REGISTRY/album-gallery:latest` (step 4).

### Option B: copy the image over (simplest)

No AWS registry to set up. Good for a test, and fine for a gallery that is
rarely updated. The image compresses to ~56 MB.

```bash
# on the Mac
podman save localhost/album-gallery:amd64 | gzip > dist/album-gallery-amd64.tar.gz
scp -i ~/.ssh/YOUR-KEY.pem dist/album-gallery-amd64.tar.gz ubuntu@photos.example.com:

# on the instance
gunzip -c album-gallery-amd64.tar.gz | docker load     # prints the loaded name
docker pull caddy:2
rm album-gallery-amd64.tar.gz
```

Pack with `--image` set to the exact name `docker load` printed:
`localhost/album-gallery:amd64`. Compose uses the local image and never tries
to pull it. To update the gallery app, you repeat these steps.

---

## 4. Pack the deployment

On the Mac, from the repository root, right after publishing:

```bash
tools/pack_deployment.py \
  --catalog family \
  --deployment album2026 \
  --domain photos.example.com \
  --image localhost/album-gallery:amd64 \
  --max-age 30 \
  --out ./dist
```

`--image` is the name from step 3: `localhost/album-gallery:amd64` with
Option B, or `$REGISTRY/album-gallery:latest` with ECR.

`--max-age 30` refuses to pack a deployment published more than 30 minutes
ago, so the bundle cannot quietly miss recent photos. Leave it out for a test,
where an older publish does not matter. The packer only reads files; it never
touches the catalog database.

Read the summary it prints. **`dereferenced`** is the real size of what you are
about to upload.

`--target node` (Compose + Caddy) is the default. To keep the bundle as a folder
that later runs refresh in place, instead of a tarball, add `--stage ./dist/stage`
(see [Updating](#the-album-changed)).

---

## 5. Start it on the instance

Each gallery gets its own folder under `~/galleries/`, named after the
deployment. A test gallery and the real one can sit side by side, though only
one of them can hold port 443 at a time.

```bash
# on the Mac
scp -i ~/.ssh/YOUR-KEY.pem dist/album2026-*.tar.gz ubuntu@photos.example.com:

# on the instance
mkdir -p ~/galleries
tar xzf album2026-*.tar.gz -C ~/galleries && rm album2026-*.tar.gz
cd ~/galleries/album2026
echo "ALBUM_SESSION_SECRET=$(openssl rand -hex 32)" > .env
chmod 600 .env
docker compose up -d
docker compose logs -f caddy     # Ctrl-C to stop following
```

Caddy logs that it is *obtaining* a certificate, then that it *obtained* one
for `photos.example.com`. You may also see an error about the HTTP challenge
on the way: Caddy can try that method first, it fails because 80 is closed,
and Caddy retries with the TLS-ALPN challenge over 443. That is normal.
`docker compose logs gallery` should show
`album-gallery listening on http://0.0.0.0:3001`.

**Replacing a test gallery with the real one.** Stop the test first, so port
443 is free: `cd ~/galleries/<test> && docker compose down`. Then start the
real one as above. Delete the test's folder when you no longer need it.

**Keep `.env`.** `ALBUM_SESSION_SECRET` signs the login cookie. If it changes,
everyone signed in to a password-protected gallery is signed out.

The containers restart on their own after a reboot (`restart: unless-stopped`).

### Check it

```bash
curl -sI -m 10 https://photos.example.com/ | head -3
# HTTP/2 302
# location: /d/family/album2026/
```

A timeout here, with the containers running, means port 443 is still blocked
in the security group.

Then open `https://photos.example.com/` in a browser. The root redirects to
the album.

---

## 6. Updating

### The album changed

A bundle is a snapshot; nothing syncs by itself.

1. Change the album in Airlock, and **publish** the deployment.
2. Re-run the packer (step 4).
3. Send only the data, deleting files the album no longer has:

   ```bash
   # on the Mac
   mkdir -p /tmp/album2026-new && tar xzf dist/album2026-*.tar.gz -C /tmp/album2026-new
   rsync -az --delete -e "ssh -i ~/.ssh/YOUR-KEY.pem" \
     /tmp/album2026-new/album2026/catalogs/ \
     ubuntu@photos.example.com:galleries/album2026/catalogs/
   ```

No restart is needed: the gallery reads the album's files on each request.

`rsync --delete` matters: extracting a new tarball on top of the old one
would leave photos you removed still reachable. `.env`, `compose.yml` and the
Caddyfile are not touched, so the certificate and viewers' sessions survive.

A tarball's files carry the packing time, so this sends every photo again. For
an update that sends only what changed, pack with `--stage` and rsync the staged
folder with `--delete --exclude=/.env`, as in
[PUBLISH_TO_EC2.md §7](../guides/PUBLISH_TO_EC2.md#7-updating-the-gallery), or use
the **Publish** button, which does the same.

### The gallery app changed

With ECR (Option A): rebuild and push (step 3), then on the instance:

```bash
cd ~/galleries/album2026 && docker compose pull && docker compose up -d
```

With Option B: repeat the build, save, copy and load (step 3), then
`cd ~/galleries/album2026 && docker compose up -d`. Compose sees that the
image changed and recreates the gallery container. `docker image prune`
removes the old one afterwards.

---

## Cost

A rough monthly figure for `us-east-1`. Check current AWS pricing, because it
changes.

| Item | Approximate |
|---|---|
| A small instance, on demand | ~$15 for `t3.small`, ~$12 for `t4g.small` |
| A micro instance, on demand | ~$7.50 for `t3.micro`, ~$6 for `t4g.micro` |
| 20–30 GB gp3 | ~$2 |
| Public IPv4 address (the Elastic IP) | ~$3.60 |
| Data transfer out | first 100 GB/month free, then ~$0.09/GB |
| ECR storage | a few cents |

**Data transfer is the one that grows with use.** Every photo a viewer opens
is downloaded from the instance. Packing *Full resolution JPG* multiplies that
per photo.

To stop paying for compute while keeping everything, **stop** the instance
rather than terminating it. The Elastic IP keeps the DNS valid, and storage and
the address are still billed.

---

## Security checklist

- SSH (22) open to **your IP only**; key-based login only (Ubuntu's default).
- Only 443 open to the world. 80 and 3001 are never opened.
- The gallery mounts the bundle **read-only** and cannot modify it.
- The bundle holds **no originals and no catalog database**, only what the
  deployment publishes. If the instance were compromised, that is all there
  is to lose.
- A password-protected deployment ships `.auth.json` (the password hash, mode
  0600). Keep the bundle directory private to the `ubuntu` user.
- `.env` is the only secret created on the machine. Back it up somewhere safe
  if re-signing everyone in would be a problem. Nothing else needs a backup,
  because the bundle can always be packed again.

---

## Troubleshooting

The packer-side problems (missing images, a 404 for everything, the stack
refusing to start) are in
[PACKING-TOOL.md → Troubleshooting](PACKING-TOOL.md#troubleshooting). On EC2
specifically:

| Symptom | Cause and fix |
|---|---|
| `exec format error` in `docker compose logs gallery` | The image's architecture does not match the instance: an image built on this (Arm) Mac without `--platform`, running on an Intel (`x86_64`) instance, or the reverse. Rebuild with the `--platform` from step 3. |
| `curl` to `https://` times out | Port 443 is blocked by the security group or a network ACL. The instance's own firewall is off by default. |
| Caddy: HTTP challenge error, then the certificate is obtained | Normal: port 80 is closed on purpose, and the TLS-ALPN challenge over 443 succeeds. |
| Caddy: every challenge failed / no certificate | DNS does not point at this instance yet, or 443 is not reachable from the internet. Fix it, then `docker compose restart caddy`. |
| `http://` does not load | Expected: only HTTPS is served. Use the `https://` link. |
| Too many failed certificate attempts | Let's Encrypt rate limits. Wait an hour; do not delete the `caddy_data` volume while testing. |
| `permission denied ... docker.sock` | You have not logged out and back in since `usermod -aG docker`. |
| `pull access denied` / `no basic auth credentials` for ECR | The instance has no IAM role, or the credential helper is missing. See Option A. |
| The site stops after the instance is stopped and started | Without an Elastic IP, the public IP changed and DNS points at the old one. |
| Disk full | `docker image prune` removes old gallery images left behind by updates. Otherwise grow the volume; see [Disk](#disk). |
| Containers killed, `Out of memory` in `dmesg` | A 1 GB instance without swap. Add it; see [Swap](#swap-on-a-1-gb-instance). |

---

## Alternatives

| Option | When it fits |
|---|---|
| **S3 + CloudFront** (`--target static`) | The deployment has **no password**. No server to patch, and it costs almost nothing at family scale. `aws s3 sync` the static bundle into a private bucket behind CloudFront with an ACM certificate. |
| **Lightsail** | The same steps as this guide, on a fixed-price instance with transfer included in the plan. Simpler billing; fewer knobs. |
| **ECS / Fargate** | Only if several galleries need to be managed as a fleet. For one album it is much more setup than one instance. |
