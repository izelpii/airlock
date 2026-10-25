# Publish an album to the internet on Amazon EC2

This guide puts one of your Airlock **deployments** on the internet: a gallery at your
own address (`https://photos.example.com`), with HTTPS, and with the deployment's
password still required if it has one. It runs on one small Amazon EC2 server that
holds only the album's pictures. It has no originals, no catalog database and no way
back into your Airlock.

You do the AWS part once (§1–§3). Then Airlock does the rest: on the deployment's card,
**Add a server**, **Set up** once, and **Publish** after each change (§3A). Sections 4–7
are the same work by hand, for when you would rather, or when the button cannot reach
the server.

**Time:** about an hour the first time, most of it waiting for AWS and DNS.
**Cost:** roughly US $10–20 a month for a small server (details in
[aws.md → Cost](../features/aws.md#cost)).

---

## What you need

- **An AWS account**, and permission to create EC2 instances in it.
- **A domain name** whose DNS you can change, for example `photos.example.com`.
  No domain yet? You can test with a free `sslip.io` name (§2.4).
- **A published deployment in Airlock.** In Airlock, open **Albums → Deployments**.
  Your deployment should list its albums. Flip the card (the image icon,
  *What to pack*) and check the sizes it ships. *Full resolution JPG* makes the
  gallery several times larger.
- **A terminal on the Airlock machine**, the computer whose `catalogs/` folder
  holds your photos. The commands in §3 and §5–§7 run there, from the Airlock folder
  (`/opt/airlock` on a standard install).

The examples use catalog `family`, deployment `album2026`, domain
`photos.example.com` and region `us-east-1`. Replace them with yours.
The catalog and deployment names are the short names in the gallery's address
(`/d/<catalog>/<deployment>/`).

---

## 1. Create the server

In the AWS console, open **EC2 → Launch instance**:

| Setting | Choose |
|---|---|
| Name | e.g. `airlock-gallery` |
| Image (AMI) | **Ubuntu Server 24.04 LTS**, 64-bit **x86**. Use the same kind of processor as your Airlock machine. `uname -m` there prints `x86_64` for x86, and then the gallery image needs no conversion (§3). |
| Instance type | `t3.small` (2 GB). `t3.micro` (1 GB) works for a family album with swap added (§2.2). |
| Key pair | **Create new key pair**, type ED25519, `.pem`. Your browser downloads it: keep it. This is the key **you** log in with. |
| Storage | **30 GB gp3**, or 20 GB plus the size of your album. The default 8 GB is too small for a real album. |

### 1.1 Network: only HTTPS and your own SSH

In **Network settings → Edit**, set exactly these inbound rules:

| Type | Port | Source |
|---|---|---|
| SSH | 22 | **My IP** (never "Anywhere") |
| HTTPS | 443 | Anywhere (`0.0.0.0/0` and `::/0`) |

Leave port 80 closed. The certificate is obtained over 443, and visitors use the
`https://` link you send them.

Launch the instance.

### 1.2 A fixed address

A server's public address changes when it's stopped and started. Give it a fixed
one: **EC2 → Elastic IPs → Allocate**, then **Actions → Associate** with your
instance. Note the address, e.g. `203.0.113.10`.

### 1.3 Point your domain at it

At your DNS provider (or Route 53), add an **A record**:

```
photos.example.com.   A   203.0.113.10
```

Check it from any computer. It must print the Elastic IP before you continue to §6:

```bash
dig +short photos.example.com
```

---

## 2. Prepare the server

Log in with the key from §1 (the file name is what your browser saved):

```bash
chmod 600 ~/Downloads/airlock-gallery.pem
ssh -i ~/Downloads/airlock-gallery.pem ubuntu@photos.example.com
```

### 2.1 Docker

```bash
sudo apt update && sudo apt -y upgrade
sudo apt -y install docker.io docker-compose-v2
sudo systemctl enable --now docker
sudo usermod -aG docker ubuntu
exit
```

Log in again, so the `docker` group applies, and check:

```bash
docker compose version
```

### 2.2 Swap (on a 1 GB server)

```bash
sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
sudo mkswap /swapfile && sudo swapon /swapfile
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

### 2.3 Disk

```bash
df -h /
```

You need room for the album (§5 prints its size) plus about 5 GB. If the disk is
smaller, grow it in **EC2 → Volumes → Modify volume**, then run
`sudo growpart /dev/nvme0n1 1 && sudo resize2fs /dev/root` on the server. No
reboot is needed.

### 2.4 No domain yet?

Use `203-0-113-10.sslip.io`, your Elastic IP written with dashes, wherever this guide
says `photos.example.com`. It resolves to that address with no setup, and it gets a
real certificate. Use it for testing only: switching to your own domain later is a
one-line change in the gallery's `Caddyfile`.

---

## 3. Put the gallery program on the server

The server needs the Airlock **album gallery**, a small web server. Your album's
pictures go to the server separately (§5–§6). On the **Airlock machine**, from the
Airlock folder:

```bash
cd systems/album-gallery
podman build --platform linux/amd64 --ignorefile .dockerignore -f Dockerfile \
    --build-arg GALLERY_REVISION=$(git log -1 --format=%h -- .) \
    -t album-gallery:amd64 .
cd ../..
podman save localhost/album-gallery:amd64 | gzip > /tmp/album-gallery.tar.gz
scp -i ~/airlock-gallery.pem /tmp/album-gallery.tar.gz ubuntu@photos.example.com:
```

Copy the `.pem` from §1 to the Airlock machine first if it's only on your laptop.
If you chose an Arm server (`t4g`) in §1, build with `--platform linux/arm64` instead.

On the **server**:

```bash
gunzip -c album-gallery.tar.gz | docker load      # prints: localhost/album-gallery:amd64
docker pull caddy:2
rm album-gallery.tar.gz
```

Repeat this section only when Airlock's gallery program itself changes, not when
your album does. `curl -s https://photos.example.com/health` shows which version a
server runs (`revision`), to compare with `git log -1 --format=%h -- systems/album-gallery`
on the Airlock machine. To have the server download updates itself instead, see
[aws.md → Option A: Amazon ECR](../features/aws.md#option-a-amazon-ecr-for-a-gallery-you-update-often).

---

## 3A. From here, in Airlock

Owners of the catalog see this on each deployment card. Everything happens in the
**Servers** window it opens.

1. **Albums → Deployments**, on your deployment's card: **Add a server to publish to**.
2. Fill in the server: a name, its **host** (the Elastic IP from §1.2), **SSH user**
   `ubuntu`, the **folder** (Airlock suggests `galleries/<deployment>`), the **public
   address** (`https://photos.example.com/`) and the **gallery image** you loaded in §3
   (`localhost/album-gallery:amd64`).
3. Airlock makes a key for this server and shows **one line**. Copy it, then add it on
   the server with your own key from §1:

   ```bash
   ssh -i ~/Downloads/airlock-gallery.pem ubuntu@photos.example.com 'cat >> ~/.ssh/authorized_keys'
   ```

   Paste the line, press Enter, then Ctrl-D.
4. **Connect.** Airlock shows the server's fingerprint (`SHA256:…`). To be sure it's
   your server, compare it with what the server itself says:
   `ssh -i … ubuntu@photos.example.com ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub`.
   If they match, **It matches - confirm**.
5. **Preview.** On a new server it answers *needs set up*. Press **Set up**. It checks
   Docker, the ports and the space, sends the first copy, creates the session secret on
   the server, starts the gallery and checks the site. The first certificate can take a
   minute; Set up waits for it.
6. From then on, after you change the album: **Publish** on the card. It republishes,
   sends only what changed, removes what the album no longer has, never touches
   `.env`, and checks the site. If it would delete many files, it asks first.

Airlock never asks for, or keeps, AWS credentials: the console part above stays yours.

## 4. A key for publishing

Your `.pem` is your personal key. Give the Airlock machine a key of its own, used
only to publish galleries, so you can revoke it without touching yours. On the
**Airlock machine**:

```bash
ssh-keygen -t ed25519 -N '' -C 'airlock-publish album2026' -f ~/.ssh/airlock_publish
ssh -i ~/airlock-gallery.pem ubuntu@photos.example.com \
    'cat >> ~/.ssh/authorized_keys' < ~/.ssh/airlock_publish.pub
ssh -i ~/.ssh/airlock_publish ubuntu@photos.example.com 'echo connected'
```

The last line must print `connected`. It also asks you to confirm the server's
identity the first time: answer `yes`.

---

## 5. Prepare the album

In **Airlock**, make sure the gallery will show what you expect:

1. **Albums:** on each album whose photos changed, press the green **rebuild** button
   in the card's header ("Rebuild this album and refresh what publishes it").
   Turned photos, re-dated photos and removed photos all need this. Photos added to an
   event reach an album only when you rebuild it.
2. **Deployments:** on your deployment's card, press the **refresh** button ("Rewrite
   the published folder from the database").

Then, on the **Airlock machine**, from the Airlock folder, build the folder that will
be sent:

```bash
python3 tools/pack_deployment.py \
    --catalog family --deployment album2026 \
    --domain photos.example.com \
    --image localhost/album-gallery:amd64 \
    --stage ./dist/stage \
    --max-age 60
```

- `--stage` writes a folder, `dist/stage/album2026/`, rather than a download file. The
  photos in it are hard links to Airlock's own copies, so it takes almost no space, and
  each update sends only what changed. Keep `dist/` on the same disk as `catalogs/`
  (the Airlock folder already is). On another disk the photos are copied instead,
  which still works but needs the album's full size in space.
- `--max-age 60` refuses to pack if you published more than an hour ago, so nothing
  stale goes out.
- The **dereferenced** line in its output is the real size of the album. Check the
  server has room for it (§2.3).

---

## 6. The first publish

From the **Airlock machine**:

```bash
rsync -a --info=progress2 -e "ssh -i ~/.ssh/airlock_publish" \
    ./dist/stage/album2026/ ubuntu@photos.example.com:galleries/album2026/
```

Then on the **server**:

```bash
cd ~/galleries/album2026
echo "ALBUM_SESSION_SECRET=$(openssl rand -hex 32)" > .env && chmod 600 .env
docker compose up -d
docker compose logs -f caddy          # Ctrl-C once it says the certificate was obtained
```

Caddy may log an error about the "HTTP challenge" first. That's expected with port 80
closed: it then succeeds over 443.

**Keep `.env`.** It signs visitors' logins. If it's replaced, everyone using a
password-protected gallery is signed out. The update command in §7 never touches it.

### Check it

From any computer:

```bash
curl -sI https://photos.example.com/ | head -2
# HTTP/2 302
# location: /d/family/album2026/
```

Then open `https://photos.example.com` in a browser. A timeout means port 443 isn't
open (§1.1); a certificate error means DNS doesn't point at the server yet (§1.3).

---

## 7. Updating the gallery

Each time the album changes:

1. In **Airlock**, rebuild the changed albums and refresh the deployment (§5, steps 1–2).
2. On the **Airlock machine**, build the folder again (the same command as §5).
3. **Preview** what will change. Nothing is sent yet:

   ```bash
   rsync -a -n --itemize-changes --delete --exclude=/.env \
       -e "ssh -i ~/.ssh/airlock_publish" \
       ./dist/stage/album2026/ ubuntu@photos.example.com:galleries/album2026/
   ```

   Lines starting `>f` are files to send; `*deleting` are files the album no longer
   has. **Read the deletions.** Hundreds of them usually mean the deployment was
   emptied by mistake: stop and check it in Airlock.
4. **Send it:** the same command without `-n`:

   ```bash
   rsync -a --delete --exclude=/.env --info=progress2 \
       -e "ssh -i ~/.ssh/airlock_publish" \
       ./dist/stage/album2026/ ubuntu@photos.example.com:galleries/album2026/
   ```

The gallery serves the new files straight away; no restart is needed. If the
password or the domain changed, run `docker compose up -d` in `~/galleries/album2026`
on the server afterwards.

Two rules matter here:

- **Always `--exclude=/.env`** with `--delete`, or the session secret is deleted (§6).
- **No `--checksum`** in normal use. The folder keeps each photo's own date, so rsync
  skips unchanged photos without reading them. Use `--checksum` once if this server
  was filled some other way before, for example from a `.tar.gz` bundle.

---

## 8. When something goes wrong

| Symptom | What to do |
|---|---|
| `Permission denied (publickey)` | The publishing key isn't on the server: repeat §4. |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | The server was replaced, or something is wrong. If you replaced it, remove the old entry with `ssh-keygen -R photos.example.com` and connect again. Otherwise stop and find out why. |
| `exec format error` in `docker compose logs gallery` | The gallery was built for the wrong processor: rebuild with the right `--platform` (§3). |
| The page times out | Port 443 isn't open to everyone (§1.1). |
| No certificate | DNS doesn't point at the server yet (§1.3). Fix it, then `docker compose restart caddy`. Don't delete Caddy's volume while testing: Let's Encrypt limits retries. |
| `No space left on device` | Grow the disk (§2.3); `docker image prune` removes old gallery versions. |
| The packer says the deployment is too old | Refresh it in Airlock (§5, step 2), or raise `--max-age`. |
| A photo you removed is still visible | You skipped `--delete`, or the album wasn't rebuilt (§5, step 1). |

More cases, costs and alternatives (such as S3 for galleries without a password) are
in [aws.md](../features/aws.md).

---

## 9. Keeping it safe

- **Only what the deployment publishes leaves Airlock**: the album sizes you
  ticked. Never originals, faces or the database.
- **SSH is open to your address only.** If your address changes, update the rule in
  the server's security group.
- **The publishing key** (`~/.ssh/airlock_publish`) can be revoked at any time by
  deleting its line from `~/.ssh/authorized_keys` on the server.
- **Ubuntu installs its own security updates.** Log in now and then to run
  `sudo apt upgrade` and reboot when it asks.
- **To pause the gallery** without losing anything, stop the instance in EC2. The
  Elastic IP keeps your address, and storage and the address are still billed.
