# Packing a deployed album

`tools/pack_deployment.py` turns a published album deployment into a single
`.tar.gz` that runs on any machine with Docker — a cloud VM, a NAS, a laptop —
with no shoebox, no source checkout and no network path back home.

Specified in
albums-enhancement-spec.md §4.5.

The work is done by `systems/backend/deployment_pack.py`, which the Publish to
server button will share
(deployment-publish-to-server-spec.md);
this script is its command line. With `--stage DIR` it writes the bundle as a
folder instead of a tarball, media hard-linked from the catalog, and a later run
**refreshes** that folder rather than rebuilding it: unchanged files keep their
time, so `rsync` sends only what changed. That is the update path of
[PUBLISH_TO_EC2.md](../guides/PUBLISH_TO_EC2.md).

---

## Quick start

Publish the deployment in the UI first (**Albums → Deployments → Publish**),
then:

```bash
tools/pack_deployment.py \
    --catalog family \
    --deployment album2026 \
    --domain photos.example.com \
    --image ghcr.io/YOU/album-gallery:latest \
    --out ./dist
```

```
  deployment : Family Album  (family/album2026)
  albums     : 1  00009
  password   : yes
  published  : 42 minutes ago

  dereferenced: 428 files, 37.6 MB   (the link farm on disk is only 522 B)
  tarball     : dist/album2026-20260907.tar.gz  (37.5 MB)
```

On the VM:

```bash
tar xzf album2026-20260907.tar.gz && cd album2026
echo "ALBUM_SESSION_SECRET=$(openssl rand -hex 32)" > .env
docker compose up -d
```

Then open `https://photos.example.com/` — the domain root redirects to the
album. Caddy fetches a TLS certificate on the first request.

---

## Why the tool exists: 522 bytes vs 37.6 MB

A published deployment on disk is a farm of **symlinks, two levels deep**:

```
cache/Deployments/album2026/
  00009            -> ../../Albums/00009
    *_alt.jpg      -> ../../Images/2026/08/10/...
    *_proxy.mp4    -> ../../Videos/2026/08/05/...
```

`cp -r`, `scp -r` and `tar` without `-h` copy the **links**. Half a kilobyte
arrives, the album appears to publish, and every single image 404s on a machine
where nobody can debug it.

The packer materialises real bytes, and treats a link that does not resolve as a
**hard error** rather than shipping a hole. That is the one job that makes this a
tool instead of a `tar` command.

It also means any size shown for a deployment must be the *dereferenced* one.
522 B is a true number that answers the wrong question.

---

## The two targets

| | `--target node` (default) | `--target static` |
|---|---|---|
| Needs | Docker + Compose | any web server or S3 bucket |
| Ships | data + `compose.yml` + `Caddyfile` | data + the gallery web build |
| TLS | automatic, via Caddy | whatever the host provides |
| **Password** | **kept and enforced** | **impossible** |
| Videos | yes | yes |

**A static bundle cannot carry a password.** A file host has nothing to verify
one with, and the hash must never be shipped to it. Packing a locked deployment
for `--target static` is refused; `--allow-unlocked-static` overrides it, packs
without protection, prints a warning, and rewrites `requires_password` to `false`
so the viewer is not shown a lock screen it could never satisfy.

---

## What is in a bundle

**Node target** — the exact directory shape the gallery already expects, so the
destination runs the same image with the same `CATALOGS_ROOT` and nothing in the
server knows it was packed:

```
album2026/
├── catalogs/
│   ├── catalogs.json                 {"family": "family"}
│   └── family/cache/Deployments/album2026/
│       ├── albums.json
│       ├── deployment.json
│       ├── .auth.json                mode 0600, node target only
│       └── 00009/                    REAL FILES — no symlinks
│           ├── album.json
│           ├── *_alt.jpg  *_thumb_320.jpg  *_poster.jpg
│           └── *_proxy.mp4
├── compose.yml
├── Caddyfile
└── README.md
```

**Static target** — laid out the way the browser asks for it, with no server:

```
zzpack/
├── index.html   assets/   favicon.svg      the gallery web build
├── albums.json
├── deployment.json
├── albums/00009/…                          REAL FILES
└── README.md
```

### The application is not in the bundle

The gallery image is **pulled from a registry**, named by `--image`. Shipping a
`docker save` would add ~54 MB and go stale on every gallery change; the bundle
carries **data and configuration only**, so it stays small and stays valid across
upgrades. Updating the application at the destination is:

```bash
docker compose pull && docker compose up -d
```

For a first test, before any registry exists, `docker save album-gallery | gzip`
and `scp` is a fine one-off — it is simply not what the packer emits.

### `catalogs.json` is not optional

It is the one mapping the server cannot derive — `{slug: directory-name}` — and
it is read at boot. A bundle without it returns 404 for everything and gives no
hint why. The packer writes it with only the catalog being packed.

---

## Options

| Option | Meaning |
|---|---|
| `--catalog` | catalog slug, e.g. `family` (**required**) |
| `--deployment` | deployment slug, e.g. `album2026` (**required**) |
| `--target node\|static` | default `node` |
| `--domain` | DNS name for the `Caddyfile`; without it the file carries a placeholder and a warning |
| `--image` | gallery image to pull; without it `compose.yml` points at a placeholder |
| `--out` | where to write the tarball (default `./dist`) |
| `--max-age MINUTES` | fail if the deployment was published longer ago than this |
| `--catalogs-root` | defaults to `./catalogs`, or `$CATALOGS_HOST_DIR` |
| `--spa-dist` | gallery web build, for `--target static` |
| `--allow-unlocked-static` | pack a locked deployment for a static host **without** its password |

---

## Freshness: the packer cannot publish

The script reads **only files** — it never opens `workflow_results.db`. The
deployment directory already carries everything a bundle needs, and the catalogs
tree is a bind mount shared with the running backend whose databases are in WAL
mode; opening one from the host while the container writes has corrupted an
import before. A packer that only reads regular files cannot do that.

The cost is that it cannot re-publish for you. So it prints how long ago the
deployment was published:

```
  published  : 42 minutes ago
```

and `--max-age` turns staleness into an error instead of a surprise:

```bash
tools/pack_deployment.py … --max-age 10
# error: the deployment was published 45 minutes ago, over the --max-age of 10 minutes.
#   Publish it again so the bundle matches the album.
```

**Publish first, then pack.** If you added photos since the last publish, the
bundle will not contain them.

---

## Running it on a VM

Three things beyond `docker compose up -d`:

**DNS must already point at the machine.** Caddy proves domain ownership to
Let's Encrypt over HTTP, so the name has to resolve before the first start.

**Ports 80 and 443 must both be open.** Not just 443 — the certificate challenge
uses 80. On AWS that is a security-group rule.

**Keep `.env`.** `ALBUM_SESSION_SECRET` signs the login cookie. Losing or
changing it signs every viewer out. `compose.yml` uses `${ALBUM_SESSION_SECRET:?}`
so the stack refuses to start without it rather than starting subtly broken —
which is what would otherwise happen, with the server generating a fresh key on
every restart.

The album is served at `https://<domain>/d/<catalog>/<deployment>/`, with a
redirect from the domain root. The prefix is kept deliberately: the web app
builds every request from `document.baseURI`, so this needs no path rewriting,
which is one fewer thing to get wrong on a first deployment.

---

## Updating

A bundle is a **snapshot**. Nothing syncs.

1. Change the album in shoebox
2. Publish the deployment
3. Re-run the packer
4. Copy the new tarball over and `docker compose up -d`

At ~38 MB that is fine. If an album grows past the point where re-uploading is
comfortable, `rsync -a` over the unpacked `catalogs/` tree is the incremental
version and needs no change to the tool.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| Every image 404s, the bundle is a few KB | The tarball was made with `cp -r`/`tar` instead of this tool — the symlinks were copied, not the files |
| 404 for everything, including `albums.json` | `catalogs/catalogs.json` is missing, or `--catalog` does not match the directory name |
| The stack will not start | `ALBUM_SESSION_SECRET` is not set — create `.env` |
| Caddy cannot get a certificate | DNS is not pointing here yet, or port 80 is closed |
| Viewers signed out on every restart | `ALBUM_SESSION_SECRET` is changing between starts |
| Photos are missing from the bundle | The deployment was published before they were added — publish, then re-pack |
| `error: dangling link or unreadable file` | A derivative referenced by the album is gone; re-publish the deployment |

---

## What it deliberately does not do

- **Publish.** See *Freshness* above.
- **Sync incrementally.** A bundle is a snapshot; `rsync` covers the rest.
- **Ship originals.** A deployment carries the 1200px derivatives, video proxies
  and posters — never the source files. A 3.9 GB catalog produces a ~38 MB album.
- **Reach back to the origin** for full-size images. That would trade away the
  independence the bundle exists for; the reasoning is in
  §4.4 of the spec.
- **Build or push the gallery image.** That is a registry workflow, named by
  `--image`.
