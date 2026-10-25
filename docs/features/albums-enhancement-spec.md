# Album Deployments — Implementation Specification

**Status:** implemented, phases 1-4, and phase 6 (packing) as a command-line tool.
Phase 5 (per-deployment users) remains, by design — see section 10.

> **Status as of v6.4.4 (2026-10-25).** Phases 1-4 work as specified below. Packing
> (phase 6) is built as `tools/pack_deployment.py` over `systems/backend/deployment_pack.py`,
> with the `node` and `static` targets of §4.4; the `POST …/pack` HTTP endpoint was not
> built. Beyond this spec: a deployment chooses which image sizes it packs (*What to
> pack*, `packed_rungs`); `--stage` keeps an incremental bundle; and since v6.2.0 a
> deployment can be published to a server from its card (**Publish to AWS**:
> `publish.py`, `deployment-publish-to-server-spec.md`, the guide
> `docs/guides/PUBLISH_TO_EC2.md`), which §4.5.7 and §11 had left out.
> The product and its names are now Airlock (`airlock.lan`; `shoebox.lan` still
> redirects). On the album side: albums are made with tools only (v6.1.9; albums
> already made with words still open in their editor), or from a year template
> (`album_templates.py`); album, deployment and catalog cards carry a band (v6.1.7);
> an album card has **Transfer** and **Mass transfer** (v6.3.0) beside Deploy; and the
> Deployments page moved under **Systems** in the sidebar (§8). Sections 2 and 4.5.1
> record the system as measured when this was written.
**Amended:** section 4.4 — a packed bundle is self-contained in the sense of
"independent of this installation", NOT "static files only". It may ship the
gallery's own Node server, which is what lets a deployed album keep its password
gate and grow features that client-side JavaScript alone cannot provide.
**Supersedes:** the original brief of this file (preserved as `albums-enhancement-spec-orig.md`)
**Related:**
`documentation/specs/album-deployment-spec.md` (the per-album folder this builds on),
`documentation/specs/local-storage-spec.md` §4E (why the backend may not drive podman),
`systems/album-gallery/server.mjs`, `systems/gateway/airlock.conf.template` (was `shoebox.conf.template`),
`systems/backend/app.py` (`_deploy_album`)

---

## 1. Goal

A **deployment**: a named, shareable gallery holding one or more albums.

- create deployments; add and remove albums from them
- adding an album must not rebuild anything
- protect a deployment with a password, with per-deployment users later
- reach it at a stable URL
- **be packable for somewhere else** — the same deployment exported as a self-contained
  bundle and hosted remotely (a small VM, a container, or a plain file host), not only served
  from here. Self-contained means independent of this installation, NOT static-only: the bundle
  may carry the gallery's own Node server (§4.4)

---

## 2. Measured against the current system

### 2.1 There is no per-album container — one generic gallery serves everything

The brief says deployment "creates a deployable version of an album or catalog, and launches
it on its own container", using "a template of a Dockerfile". That is not what happens.

```bash
# systems/album-gallery/start-podman.sh:47
exec podman run --rm --replace --name album-gallery -p 3001:3001 \
  -e ALBUM_ROOT=/catalog-cache/Albums \
  -v "${CATALOG_CACHE_HOST_DIR}:/catalog-cache:ro,z"
```

**One** container, named `album-gallery`, on **one** port, pointed at **one** catalog's cache.
`--replace` means starting a second kills the first. The Dockerfile builds a generic image —
server plus a Vite bundle — with no album baked in and nothing per-album to template.

This is good news. Nothing has to be undone; the concept simply does not exist yet.

### 2.2 The gallery already reads a manifest — nobody writes one

`server.mjs:105`:

```js
if (url === '/albums.json' && method === 'GET') {
  const manifestPath = path.join(ALBUM_ROOT, 'albums.json')
  if (fs.existsSync(manifestPath)) return serveFile(manifestPath, res)
  // Auto-discover numeric subfolders when no manifest file exists
```

Grepping the whole backend for `albums.json` returns **nothing**. So the manifest branch is
dead and every gallery falls through to auto-discovery: it shows *every* numeric folder under
`ALBUM_ROOT`, which is every album ever deployed from that catalog, to anyone who can reach
port 3001.

**That dead branch is the feature.** A deployment is a directory with an `albums.json` in it.
Adding an album is a line in that file and a symlink beside it — which is exactly the shape the
brief asks for:

> *"we want to change the definition of a deployment just by adding a new declaration of the
> folders that should be contained in each deployment — The runtime then adds all of the
> folders to the UI"*

### 2.3 The backend cannot launch containers, and must not be given the ability

`local-storage-spec.md` §4E rejected mounting the podman socket into the backend: a process
holding it can start a privileged container and own the machine, which is not a grant to hand
a web-facing Flask app. That decision stands, and it rules out "create a deployment → a
container appears".

So the design has to make container work unnecessary rather than automate it (§4).

### 2.4 `_deploy_album` already builds the per-album folder

`app.py:6923` creates `cache/Albums/<00001>/`, fills it with relative symlinks to the
browser-friendly alternates, and writes `album-<id>.json`. It is called from five places on
save. **None of this changes.** Deployments compose these folders; they do not replace them.

### 2.5 The URL question is already half-answered

| | Today | For `family.shoebox.lan/summer-2019` | For `shoebox.lan/gallery/family/summer-2019` |
| --- | --- | --- | --- |
| TLS certificate | **`DNS:*.shoebox.lan` already in the SANs** | ✅ nothing to do | ✅ nothing to do |
| nginx | one `server_name gallery.shoebox.lan` block | needs a wildcard server block | needs one `location` |
| name resolution | `/etc/hosts`, one line per name | ⚠️ one line per **catalog** (§7.2), or wildcard DNS | ✅ nothing to do |
| the SPA | fetches `/albums.json` absolute | ✅ works | ❌ must become base-path aware (needed for packing anyway — §4.4) |

`make-certs.sh:61` already emits `DNS:*.shoebox.lan`, so subdomains are a solved problem for
TLS. What blocks them is DNS: `/etc/hosts` cannot wildcard, so every new deployment would mean
editing the hosts file of every machine that views it. That is fine for a lab and hopeless for
"send my mother a link".

**Both are worth having, and the order matters** — §7.

---

## 3. Data model

**Decided: a deployment belongs to one catalog.** Not a limitation to work around — the reason
catalogs exist. A catalog separates images that should not mix (work, family), and the things a
deployment will come to carry are catalog-local: albums, and later the People and face
recognition data baked into it. A deployment that spanned catalogs would be a hole in that
separation, and it would have to reconcile two sets of people who are not the same people.

So the tables live in the **catalog's own database** (`workflow_results.db`), beside `albums`,
created idempotently in `models.py`'s `CatalogDatabase` schema:

```sql
CREATE TABLE IF NOT EXISTS album_deployments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    slug          TEXT NOT NULL UNIQUE,   -- URL segment and DNS label: 'family-2019'
    name          TEXT NOT NULL,          -- shown in the gallery: 'Family 2019'
    description   TEXT,
    -- scrypt, exactly as auth.py hashes user passwords. NULL means public.
    password_hash TEXT,
    enabled       INTEGER NOT NULL DEFAULT 1,
    created_by    INTEGER,                -- users.id, for the record only
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS album_deployment_albums (
    deployment_id INTEGER NOT NULL,
    album_id      INTEGER NOT NULL,
    position      INTEGER NOT NULL DEFAULT 0,
    added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (deployment_id, album_id)
);
```

**Two things fall out of living in the catalog, and both are worth having:**

- **No cross-database reference.** `albums` is in the same file, so `album_id` alone identifies
  an album and every check is one query. Compare `storage_routes`, where the equivalent
  reference spans two files and forces a scan of every catalog
  (`local-storage-spec.md` §6.4). None of that applies here.
- **The catalog directory stays self-contained.** Deployments live under it (§4.1), so a
  catalog folder carried to another machine brings its deployments with it — the property
  `file-ingestion-spec.md` §3.0 establishes and this must not break.

`created_by` is a `users.id` from the *application* database. It is a record of who made it,
never read to make a decision, so the split costs nothing.

### 3.1 Slugs

**Decided: the catalog is part of the URL.** A deployment is identified by the pair
`<catalog-slug>/<deployment-slug>`, never by the deployment slug alone.

That is what makes the per-catalog design free. Catalog slugs are unique because
`catalog_meta` is one table in one database; deployment slugs are unique within a catalog
because `album_deployments` is one table in that catalog's database. **The pair is therefore
unique by two ordinary `UNIQUE` constraints, with nothing to scan** — where a bare deployment
slug would have needed a check across every catalog on every create, precisely the kind of
cross-file bookkeeping this design otherwise avoids.

It also reads as what it is. `family/2019-summer` says which library a link belongs to, and two
catalogs may both hold a "Summer" album without the URLs fighting.

#### `catalog_meta.slug`

Catalogs have no slug today — `catalog_meta` holds `name` and nothing URL-safe. Add one:

```sql
ALTER TABLE catalog_meta ADD COLUMN slug TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_slug ON catalog_meta(slug);
```

Guarded `ALTER` in `Catalog.init_db()`, backfilled from `name` for existing rows, and derived
from the name on create. Nullable only so the migration can run before the backfill; a catalog
without one simply cannot host a deployment, which is a clear message rather than a crash.

#### Derivation, both kinds

```
'Family Pictures'  →  'family'          (catalog, from its name)
'Summer 2019'      →  'summer-2019'     (deployment, from its name)
```

Lowercase, spaces and punctuation collapsed to single hyphens, trimmed, truncated to 40,
deduplicated with a numeric suffix (`summer-2019-2`) inside its own scope. Overridable in both
cases. The rule is `^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$` — a URL segment and a DNS label, and DNS
labels are the stricter of the two.

**Changing a slug breaks every link already shared with it**, so both are editable but warn:
the deployment on its own card (§8), and the catalog in its edit form, which is the more
dangerous of the two because it moves every deployment under it at once.

---

## 4. How a deployment is published

### 4.1 A directory, not an image

```
<catalog.target_directory>/cache/
  Albums/                            unchanged - what _deploy_album already writes
    00001/  00007/
  Deployments/                       new, beside it
    family-2019/                     one directory per deployment, named by slug
      albums.json                    the manifest the gallery already looks for (§2.2)
      deployment.json                name, description, whether a password is required
      00001 -> ../../Albums/00001    relative symlink, two levels up and back down
      00007 -> ../../Albums/00007
    wedding/
      …
```

**Inside the catalog, deliberately.** A deployment draws only on that catalog's albums (§3), and
keeping it there means a catalog folder carried to another machine brings its deployments with
it. It also makes the symlinks short and wholly internal — `../../Albums/00001` never leaves
the catalog cache, so there is no path that resolves on the host and dangles in a container.

**Adding an album writes one symlink and rewrites `albums.json`.** No image build, no
container restart, no downtime — which is the brief's own requirement, and the reason §2.3's
constraint costs nothing.

Symlinks are already the established mechanism: `_deploy_album` fills each album folder with
relative links into the catalog's alternates, so no bytes are duplicated per album and none are
duplicated per deployment either.

> **Relative links only.** `_deploy_album` learned this the hard way — an absolute link resolves
> on the host and dangles inside the container. Deployment links must be relative to
> the deployment directory. Living inside the catalog cache (§4.1) makes that easy — the links
> never leave it — but the gallery must still mount the catalogs root such that
> `Deployments/<slug>/00001 -> ../../Albums/00001` resolves inside the container. This is the
> single most likely way the feature ships broken.

### 4.2 One container, many deployments

The gallery gains a deployment dimension instead of being launched once per deployment:

| | now | after |
| --- | --- | --- |
| `ALBUM_ROOT` | a catalog's `cache/Albums` | — |
| `CATALOGS_ROOT` | — | the catalogs root, read-only: deployments live at `<catalog>/cache/Deployments/<slug>` |
| `GET /albums.json` | manifest or auto-discovery at the root | `GET /d/<catalog>/<slug>/albums.json` |
| `GET /albums/<id>/…` | assets under `ALBUM_ROOT` | `GET /d/<catalog>/<slug>/albums/<id>/…` |

One long-lived container, started once. Creating a deployment touches no container at all —
the next request simply finds a new directory. That is what makes §2.3 a non-issue rather than
a blocker.

**The gallery now mounts the catalogs root**, not one catalog's cache, because deployments live
inside their catalogs (§4.1) and there is more than one catalog. Read-only, and the same
directory the backend already mounts:

```bash
-e CATALOGS_ROOT=/catalogs \
-v "${CATALOGS_HOST_DIR}:/catalogs:ro,z"
```

**Resolution is direct, with no index and no scan.** The URL names the catalog, so
`/d/family/summer-2019/…` maps to
`<CATALOGS_ROOT>/family/cache/Deployments/summer-2019/…` by substitution — the same trick
storage routes use, for the same reason. That is the second dividend of putting the catalog in
the URL: a gallery that answers from the path rather than from a directory index it has to
build, invalidate and keep warm.

The catalog segment is a *slug*, not a directory name, so the gallery needs the mapping. It
reads `<CATALOGS_ROOT>/catalogs.json` — a small file the backend writes on catalog create,
rename and delete, listing `{slug: directory}`. One file, rewritten rarely, read at boot and on
a miss.

`ALBUM_ROOT` stays supported for one release so the existing single-gallery setup keeps
working; when `CATALOGS_ROOT` is set it wins.

### 4.3 The manifest

`albums.json` — deliberately the shape the gallery already expects, a list of folder names, so
the dead branch in §2.2 lights up with no client change:

```json
["00001", "00007"]
```

`deployment.json` carries what the gallery needs to render and gate the deployment:

```json
{
  "slug": "family-2019",
  "name": "Family 2019",
  "description": "…",
  "requires_password": true,
  "albums": [
    {"folder": "00001", "title": "Summer", "photo_count": 84},
    {"folder": "00007", "title": "Christmas", "photo_count": 120}
  ],
  "generated_at": "2026-09-06T18:00:00Z"
}
```

**`requires_password` is a boolean, never the hash.** This file is served to unauthenticated
browsers; it says whether to show a password form, and nothing more.

Each album's own `album-<id>.json` (written by `_deploy_album`) gains **per-photo `width` and
`height`**, read from `alternative_versions` at publish time. *(Now: the album manifest is
`album.json`, with each section's photos in a `section-<id>.json` beside it; and
`deployment.json` also carries `packed_rungs` and `enabled`.)* Nothing computes them at runtime —
see §4.4, where that is what lets a bundle run without a server.

### 4.4 Portability: publish here, pack for elsewhere

A deployment must also be exportable — a bundle that runs somewhere else entirely: S3, a static
host, a small VM. That is a second *output* of the same definition, not a second feature.

**What "self-contained" means here.** *Corrected during implementation — an earlier draft of
this section used "self-contained" and "static" as synonyms, and they are not.*

> A packed bundle is independent of **this machine, this backend and this catalog**. It carries
> its own assets and its own manifest, and needs nothing from the shoebox installation that
> produced it.
>
> It does **not** mean "static files only". A bundle may ship the gallery's own Node server
> (`server.mjs`) and run it at the destination. That server is *part of* the bundle, so a
> deployment that runs it is still self-contained.

The distinction matters because deployed albums are going to grow functionality that HTML and
client-side JavaScript cannot provide on their own — password checks and sessions today
(§5), and whatever per-deployment users (§10, Phase 5) turn into. Freezing the bundle at
"static files" would have capped what a deployed album can ever do.

**Two targets, one bundle.** The packer emits the same assets and the same manifest either way;
the target decides whether the server goes with it.

| Target | Ships | Can do | Cannot do |
| --- | --- | --- | --- |
| **Node** (a small VM, a container, anything that runs `node`) | assets + manifest + `server.mjs` + the SPA build | everything the local gallery does, including the password gate and sessions | — |
| **Static** (S3, GitHub Pages, any file host) | assets + manifest + the SPA build | browse the whole deployment | anything needing a server: the password gate, and later per-deployment users |

The static target is the degraded one, and it is worth keeping precisely because it is the
cheapest thing to host. Everything in the decision table below exists to keep it possible; none
of it costs the Node target anything.

```
        album_deployments (the definition)
                 │
      ┌──────────┴──────────┐
   publish                pack
   symlinks, instant,     real bytes, portable,
   served by the gallery  served anywhere - with or
                          without server.mjs beside it
```

Both write the **same manifest**. Only the asset layer differs — links here, copies there —
which is what stops the two drifting into different products.

Three things in the current design would make a bundle arrive broken. **All three are now
resolved** — see §4.5, which turns the deferred packer into work that can start.

**1. Symlinks do not travel.** `tar` a deployment today and you get dangling links; `aws s3
sync` follows them and silently uploads whatever they happen to point at. Packing must
materialise real files. Nothing to change now — but §6.2 must keep every path relative and the
manifest complete, so the packer only has to swap links for bytes.

**2. `/api/dimensions` was a needless server round trip. — DONE.** The SPA POSTed image paths
and got back sizes for data the backend already had: `alternative_versions.width` and `.height`
are populated for **every** row (1231 of 1231 measured). Width and height are baked into the
manifest at publish time and the runtime call is gone.

Note the reasoning is *not* "a bundle can have no server" — it can. It is that the data was
already known at publish time, so asking for it later was pure latency; removing it made the
local gallery faster too, and it happens to be what lets the static target work at all.

**3. Absolute fetch paths. — DONE.** Every fetch and every image `src` now resolves through
`asset()`, which is `new URL(relative, document.baseURI)`. The same build serves
`/d/<catalog>/<slug>/`, `/gallery/<catalog>/<slug>/` behind nginx, and a bucket prefix, with no
build-time base path.

**The password gate travels with `server.mjs`, and only with it** (§5). Packing for the **Node**
target carries `.auth.json` and the verification code, so a locked deployment stays locked at
the destination — same scrypt hash, same signed cookie, same throttling.

Packing for the **static** target cannot: a file host has nothing to check a password with, and
the hash must never be shipped to one (§4.3). So the packer must ask which target, and a locked
deployment packed for a static host either refuses or packs *unlocked* with a plain warning.
What it must never do is imply the lock came along. A "password-protected static bundle" would
be a lie; a "password-protected Node bundle" is simply true.

**Decided now, so packing is later work rather than rework:**

| Decision | Why it has to be now |
| --- | --- |
| Dimensions in the manifest; drop `/api/dimensions` | the data is known at publish time, so the round trip was pure latency — and its absence is what makes the static target possible |
| Every path in the manifest relative to the deployment root | a bundle has no idea where it will be mounted |
| The manifest is authoritative — never directory auto-discovery | S3 has no directories to discover |
| No absolute URLs, no hostnames, anywhere in the bundle | it will be served from a different origin |
| `deployment.json` states `requires_password`, not the hash (§4.3) | the hash must never reach a static host |

**No longer deferred:** §4.5 specifies the packer and the destination runbook, with the sizes
measured rather than guessed. Everything above is what made that section short.

**Not decided here:** whether a packed bundle may reach *back* to the originating shoebox for
full-size originals it did not ship — an authenticated media endpoint. That is a different
thing from packing and trades away the independence this section defines: the bundle would stop
working whenever the home server is asleep or unreachable. If it is ever built, the two rules
that matter are that any credential reaching a browser is public, and that it must therefore be
scoped to ONE deployment and short-lived, never a catalog-wide key.

---

### 4.5 Packing for a VM — measured, and the shortest path to running

Written for a first real deployment on a paid AWS account, but nothing here is
AWS-specific: it is "a VM with a DNS name", and the same steps fit any host that
runs containers.

The design goal is one command to produce the bundle and one command to bring it
up. Everything below exists to keep it to those two.

#### 4.5.1 What a bundle actually weighs

Measured on the live `album2026` deployment — 2 events, 151 items:

| | |
| --- | --- |
| Deployment directory as it sits on disk | **12 KB** |
| The same directory with symlinks **dereferenced** | **39 MB** (403 jpg + 21 mp4) |
| The gallery container image, gzipped | **54 MB** |
| The whole catalog, for contrast | 3.9 GB |

Two things follow. The payload is **~93 MB total**, which is a two-minute
upload and a $0-a-month storage question — this is not a data-movement problem.
And the 12 KB figure is the trap: see §4.5.2.

The 39 MB is already the *derivative* set, not the originals. A deployment never
carries originals (§4.1), so packing inherits that: a 3.9 GB catalog produces a
39 MB album.

#### 4.5.2 The one thing that makes a bundle arrive broken

A deployment directory is a farm of symlinks, **two levels deep**:

```
cache/Deployments/album2026/
  00009            -> ../../Albums/00009
    *_alt.jpg      -> ../../Images/2026/08/10/...
    *_proxy.mp4    -> ../../Videos/2026/08/05/...
```

`cp -r`, `scp -r` and `tar` without `-h` all copy the **links**: 12 KB arrives,
every image 404s, and the album looks published but empty. `aws s3 sync` follows
them and uploads real bytes, which is right by accident and wrong the moment a
link points somewhere the packer did not intend.

**The packer materialises files. It never ships a link.** In shell terms this is
`cp -RL` / `rsync -L` / `tar -h`, but the packer does it explicitly so it can
also fail loudly on a link that does not resolve, rather than shipping a hole.

This is why the "size of the deployment directory" shown anywhere in the UI must
be the **dereferenced** size. 12 KB is a true number that answers the wrong
question.

#### 4.5.3 The bundle

One `.tar.gz`, and its contents are the entire contract:

```
<slug>-<YYYYMMDD>.tar.gz
  catalogs/
    catalogs.json                     {"<catalog>": "<catalog-dir>"}
    <catalog-dir>/cache/Deployments/<slug>/
      albums.json
      deployment.json
      .auth.json                      Node target only, mode 0600
      <album-folder>/                 REAL FILES, no links
        album.json  *.jpg  *.mp4
  compose.yml
  Caddyfile
  README.md
```

The `catalogs/` subtree is byte-for-byte the shape `server.mjs` already expects,
which is the point: **the destination runs the same image with the same
`CATALOGS_ROOT`, and nothing in the server has to know it was packed.**

The containment check is what makes a dereferenced copy legal. The asset route
resolves `realpath` and requires the result to sit inside `<catalog-dir>/cache/`
(`server.mjs`, `containedReal`). Links resolve out to `cache/Images/…`, which is
inside that boundary; real files copied into `cache/Deployments/<slug>/<folder>/`
are inside it too. Both pass, so the same code serves both.

**`catalogs.json` must be in the bundle.** It is the one mapping the server
cannot derive — `{slug: directory-name}` — and it is read at boot. A bundle
without it serves 404 for everything and gives no clue why.

#### 4.5.4 The image, and why the bundle does not contain one

Three ways to get the gallery onto the VM:

| | Cost | Verdict |
| --- | --- | --- |
| `docker save` into the bundle | +54 MB, and the bundle goes stale on every gallery change | No |
| Build on the VM from the repo | needs the source and a build toolchain there | No |
| **Pull from a registry** (ECR, GHCR, Docker Hub) | one line in `compose.yml` | **Yes** |

The bundle carries **data and configuration**, never the application. That keeps
the bundle small, keeps it valid across gallery upgrades, and makes updating the
gallery a `docker compose pull` rather than a repack.

For the very first test, before any registry exists, `docker save | gzip` and
`scp` is a fine one-off — it is just not what the packer emits.

#### 4.5.5 Bring-up: TLS, and the two settings that matter

```yaml
# compose.yml
services:
  gallery:
    image: ghcr.io/<owner>/album-gallery:<tag>
    environment:
      CATALOGS_ROOT: /catalogs
      ALBUM_SESSION_SECRET: ${ALBUM_SESSION_SECRET:?set this}
    volumes: [ "./catalogs:/catalogs:ro" ]
    restart: unless-stopped
  caddy:
    image: caddy:2
    ports: [ "80:80", "443:443" ]
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
    restart: unless-stopped
volumes: { caddy_data: {} }
```

```
# Caddyfile
<your-domain> {
    redir / /d/<catalog>/<slug>/ 302
    reverse_proxy gallery:3001
}
```

**TLS is Caddy's job and it is automatic.** `server.mjs` speaks plain HTTP on
3001 and should stay that way; at home the gateway terminates TLS, and here Caddy
does, from the DNS name the user already has. Certificates renew themselves and
there is nothing to configure beyond the domain.

**`ALBUM_SESSION_SECRET` is not optional here.** Unset, `server.mjs` generates one
per process and says so loudly — every visitor to a password-protected deployment
is signed out on each restart. `compose.yml` uses `${VAR:?}` so the stack refuses
to start without it rather than starting subtly broken.

**The URL keeps its prefix.** The bundle is served at
`https://<domain>/d/<catalog>/<slug>/`, with a redirect from `/`. Rewriting the
prefix away is possible — the SPA is built on `document.baseURI` and does not
care — but it means Caddy rewriting every asset path, which is one more thing to
get wrong on a first deployment for a cosmetic gain. Serve it at the prefix
first; shorten it later if it grates.

The whole bring-up is then:

```
scp <slug>-<date>.tar.gz vm:~ && ssh vm
tar xzf <slug>-<date>.tar.gz && cd <slug>
echo "ALBUM_SESSION_SECRET=$(openssl rand -hex 32)" > .env
docker compose up -d
```

#### 4.5.6 The packer

```
POST /api/catalogs/<cid>/deployments/<id>/pack?target=node|static
  -> 200  application/gzip   <slug>-<YYYYMMDD>.tar.gz
```

and the same thing as a script, because a first deployment should not require
the web UI to be reachable:

```
tools/pack_deployment.py --catalog <slug> --deployment <slug> \
    --target node --domain photos.example.com \
    --image ghcr.io/<owner>/album-gallery:<tag> --out ./dist
```

> **As built:** only the script exists; there is no `/pack` HTTP endpoint. Its options are
> `--catalog`, `--deployment`, `--target node|static` (default `node`), `--domain`,
> `--image`, `--out`, `--stage DIR`, `--max-age MINUTES`, `--allow-unlocked-static`,
> `--catalogs-root` and `--spa-dist`. It packs only the image sizes the deployment's
> *What to pack* allows (`packed_rungs` in `deployment.json`). In-app, the same code
> (`deployment_pack.py`) is what **Publish to AWS** uses.

It reads **only files** — never `workflow_results.db`. The deployment directory
already carries everything a bundle needs, and the catalogs tree is shared with
the running backend over a bind mount where opening a WAL database from the host
has corrupted an import before. The cost is that the script cannot publish: it
prints how long ago the deployment was published, and `--max-age MINUTES` turns a
stale bundle into an error rather than a surprise.

What it does, in order:

1. Check freshness. Publishing needs the backend, so the packer reports the
   deployment's age and `--max-age` makes staleness fatal.
2. Copy the deployment directory **dereferencing every link**, failing loudly on
   one that does not resolve — a missing file is a bug to fix before shipping,
   not a hole to discover in production.
3. Write `catalogs.json` with the single `{slug: dir}` entry this bundle needs.
4. Node target: include `.auth.json` at mode 0600. Static target: **never**, and
   see §4.4 — a locked deployment either refuses or packs unlocked and says so.
5. Render `compose.yml`, `Caddyfile` and `README.md` from the deployment's own
   slug and catalog, so the commands in the README are the ones to actually run.
6. Report the **dereferenced** size and the file count (§4.5.2).

*Acceptance:* on a machine with only Docker and the tarball — no shoebox, no
source, no network path home — `docker compose up -d` serves the deployment over
HTTPS at the DNS name, images and videos load, and a locked deployment returns
401 before login and 200 after.

#### 4.5.7 Updating, and what a bundle is not

A bundle is a **snapshot**. Nothing syncs. Adding photos means republish, repack,
re-upload — which at 39 MB is fine, and `rsync` over the unpacked `catalogs/`
tree makes it incremental if it stops being fine.

Deliberately not solved here: **incremental sync**, and **a bundle reaching back
to the origin** for full-size originals. §4.4 already records why the second one
trades away the independence that makes a bundle worth having.

> **Since v6.1.15 / v6.2.0:** incremental sync exists. `pack_deployment.py --stage DIR`
> keeps the bundle as a folder (media hard-linked from the catalog) that later runs
> refresh in place, so `rsync` sends only what changed; and the deployment card's
> **Publish to AWS** does the same over SSH (preview, send, delete, never `.env`). See
> `docs/guides/PUBLISH_TO_EC2.md`. Reaching back to the origin is still not built.

---

## 5. Password protection

### 5.1 In the gallery server, not in nginx

nginx basic auth is one `htpasswd` line and would work. It is still the wrong answer:

- the browser dialog cannot be styled, explained, or branded, and has no sign-out;
- it sends the password on every request, forever, with no session to expire;
- **it does not extend.** The brief says per-deployment *users* come next, and basic auth is a
  dead end for that — a session is the thing that grows into it.

So: a form in the gallery, a session cookie, and the check in `server.mjs`.

### 5.2 Mechanism

- **Hashing:** scrypt, the same parameters `auth.py` already uses for user passwords. The
  backend hashes; the gallery only ever verifies. One implementation of "how this system
  hashes a password" (Node's `crypto.scrypt` matching the same N/r/p and salt encoding).
- **Where the hash lives:** `<catalog>/cache/Deployments/<slug>/.auth.json`, mode `0600`, written by the
  backend. Not in `deployment.json`, which is public. The leading dot and the explicit
  exclusion in the asset route (§5.4) keep it unserveable.
- **Session:** `POST /d/<catalog>/<slug>/login` → on success set `sb_album_<catalog>_<slug>`, an HMAC-signed cookie
  (`catalog|slug|expiry|sig`) with `HttpOnly`, `SameSite=Lax`, `Secure` when the request arrived over
  TLS, 30-day expiry. The signing key comes from `ALBUM_SESSION_SECRET`; when it is unset the
  server generates one at boot and logs that sessions will not survive a restart.
- **Cookie scoped per deployment**, keyed on the catalog AND the slug, so unlocking one grants
  nothing anywhere else — including a same-named deployment in another catalog.
- **Throttling:** the same shape `auth.py` uses for logins — a short lockout after repeated
  failures, per deployment and client address. A gallery link is the part of this system most likely
  to be public.

### 5.3 What a locked deployment reveals

Its `name`, and that it needs a password. Not the album list, not the photo count, and no
image. The check therefore has to sit in front of `albums.json`, the asset route **and**
`deployment.json`'s album array — not only in front of the HTML, which is easy to get wrong
and produces a gallery that "looks" locked while its images are one URL guess away.

### 5.4 Never serve dotfiles

The asset route must refuse any path component starting with `.`, and resolve with `realpath`
under the deployment directory before opening anything — the containment rule already applied
to catalog files. `.auth.json` is the immediate reason; the rule is worth having regardless.

---

## 6. Backend

### 6.1 API

Catalog-scoped, like albums — which also means the existing role check applies with no new
code: `_minimum_role_for_request()` derives the role from the method and the URL rule, so a
`GET` needs `viewer` and every mutation needs `editor` on that catalog.

| Method | Path (all under `/api/catalogs/<cid>`) | Purpose |
| --- | --- | --- |
| `GET` | `/deployments` | list, with album counts and publish state |
| `POST` | `/deployments` | `{name, description, slug?}` — slug derived when absent (§3.1) |
| `PUT` | `/deployments/<id>` | rename, enable/disable |
| `DELETE` | `/deployments/<id>` | removes the directory; albums untouched |
| `PUT` | `/deployments/<id>/password` | `{password}` or `null` to make public |
| `GET` | `/deployments/<id>/albums` | albums in it |
| `POST` | `/deployments/<id>/albums` | `{album_id}` — add |
| `DELETE` | `/deployments/<id>/albums/<album_id>` | remove |
| `POST` | `/deployments/<id>/publish` | rewrite the directory from the database |

Setting a password is `owner`, not `editor`: it decides who can see the deployment from outside
the application, which is the same kind of decision as sharing the catalog.

Every mutation calls the publisher (§6.2) so the directory is never out of step. `publish` is
the explicit repair for a directory edited or lost by hand.

### 6.2 The publisher

`_publish_deployment(catalog, catalog_db, deployment_id)` — the single place that writes the
directory, and deliberately the same signature shape as `_deploy_album` beside it:

1. `mkdir -p <catalog.target_directory>/cache/Deployments/<slug>`
2. For each album: resolve its `cache/Albums/<00001>` folder, verify it exists, and create a
   **relative** symlink (§4.1). An album whose folder is missing — never deployed, or the
   catalog was detached — is **skipped and reported**, not fatal: one broken album must not
   take the deployment down.
3. Write `albums.json` and `deployment.json`.
4. Remove symlinks for albums no longer in the deployment. Leave everything else alone.

Returns `{linked, skipped: [...], removed}`. Idempotent, so `publish` is always safe.

**There is no environment variable to set on the backend.** The deployment root is derived from
`catalog.target_directory`, which the backend already has, so the class of failure storage
routes needed `routes_enabled()` for cannot arise here. The gallery still needs
`CATALOGS_ROOT` (§4.2), and that is the one place a launcher can forget — which
`systems/test_pod_manifests.py` now catches.

### 6.3 Deleting an album

**Decided: refuse.** Deleting an album that a deployment publishes returns 409 and names the
deployments, the way deleting a storage route in use already does. Letting it through and
rendering the album as "missing" would break a link someone has already shared, silently and
later.

One query, because the deployment tables are in the same database as `albums` (§3):

```sql
SELECT d.name FROM album_deployments d
  JOIN album_deployment_albums a ON a.deployment_id = d.id
 WHERE a.album_id = ?
```

Deleting a *catalog* needs no check at all: it is a detach, the directory survives with its
deployments inside it, and re-adding the folder brings them back
(`file-ingestion-spec.md` §3.0). The gallery stops serving those slugs while the catalog is
unregistered, which is the correct outcome.

---

## 7. URLs

> **Since v5.6.4:** the local domain is `airlock.lan` (`https://airlock.lan/gallery/…`,
> `https://<catalog>.airlock.lan/…`), the nginx upstream is `airlock_gallery` and the
> config is `systems/gateway/airlock.conf.template`. `shoebox.lan` and
> `<catalog>.shoebox.lan` answer with a permanent redirect to the airlock names. The
> examples below keep the names of the time.

**Decided: both forms, and the catalog appears in both.** Path-based works today; the subdomain
form is written at the same time and starts working when wildcard DNS exists.

```
  https://shoebox.lan/gallery/family/summer-2019      path      — works today
  https://family.shoebox.lan/summer-2019             subdomain — needs wildcard DNS
```

Catalog first, deployment second, in both. One shape to learn, and both land on the gallery's
`/d/<catalog>/<slug>/`.

### 7.1 Path-based — works today

**The prefix is `/gallery/`, not `/albums/`.** *Corrected during implementation.*
The application already owns `/albums` for its own client-side routes — `/albums`,
`/albums/deployments`, `/albums/words/<id>`, `/albums/tools/<id>` — and a hard
refresh or a pasted link on any of them is a real request to nginx. Proxying
`/albums/` to the gallery therefore hijacks them: verified against real nginx,
`/albums/deployments` and `/albums/words/1` both returned the *gallery's* shell
instead of the application. The failure appears only on refresh, never while
clicking around the SPA, which is exactly the kind that ships unnoticed.

`/gallery/` collides with nothing and matches the existing `gallery.shoebox.lan`
service name. The subdomain form (§7.2) was never affected — it has no path
prefix to collide with.

```nginx
location /gallery/ {
    proxy_pass http://shoebox_gallery/d/;
    include /etc/nginx/proxy.conf;
}
```

`/gallery/family/summer-2019` → `/d/family/summer-2019`. One nginx block, no DNS work, no
certificate work, reachable from any machine that can already reach `shoebox.lan`.

**The one real cost:** the SPA fetches `/albums.json` and `/api/dimensions` at absolute paths
(`app/src/App.tsx:95,109`). Under a base path those resolve to the wrong place. Derive the base
from `document.baseURI` (or `import.meta.env.BASE_URL` with Vite's `base`) and make both
fetches relative — the same fix §4.4 needs for packing, so it is paid once.

### 7.2 Subdomain — the catalog is the label

```nginx
# The label is the CATALOG; the deployment stays in the path. One label, so the
# existing wildcard certificate covers it — see below for why that matters.
server {
    listen 80;
    listen 443 ssl;
    server_name ~^(?<catalog>[a-z0-9][a-z0-9-]{1,38}[a-z0-9])\.shoebox\.lan$;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;

    location / {
        proxy_pass http://shoebox_gallery/d/$catalog/;
        include /etc/nginx/proxy.conf;
    }
}
```

`https://family.shoebox.lan/summer-2019`.

**Why the catalog and not the deployment is the label.** A TLS wildcard matches **exactly one
label**, so `*.shoebox.lan` covers `family.shoebox.lan` and does *not* cover
`summer-2019.family.shoebox.lan`. Verified against this installation's own certificate:

```
  https://one.shoebox.lan          matched cert's "*.shoebox.lan"   → HTTP 200
  https://two.levels.shoebox.lan   subjectAltName does not match    → connection fails
```

So the two-label form `<deployment>.<catalog>.shoebox.lan` would need a SAN per catalog —
`make-certs.sh` can add them (`SANS+=("DNS:$d" "DNS:*.$d")`, line 66) but the certificate would
have to be regenerated and re-trusted on every device each time a catalog is created. Putting
the *catalog* in the label needs no certificate change, ever.

It also matches how the system is organised: a catalog is a site, its deployments are pages
within it.

**`$catalog` must not be trusted as a path.** It arrives in the Host header. The regex
constrains it to a DNS label with no dots or slashes, the gallery resolves it through
`catalogs.json` rather than by joining it to a path, and the result is `realpath`-contained
(§5.4). Three checks, because this route is reachable from outside.

**What is still missing is only DNS.** `/etc/hosts` has no wildcards, so today each catalog
needs a line — note that is per *catalog*, not per deployment, so the manual workaround is
actually usable: one line lets every deployment in that catalog resolve. Two things remove it
entirely, both outside this change:

- **dnsmasq** on the host: `address=/shoebox.lan/127.0.0.1`.
- **A public domain** already contemplated for TLS: a wildcard `A` record for
  `*.lozaprada.org`, plus `./make-certs.sh lozaprada.org`.

### 7.3 One pair, two routes

The gallery is reached at `/d/<catalog>/<slug>/…` either way. So:

- **the SPA needs no notion of subdomains** — it reads catalog and slug from its own base path,
  which the §7.1 fix already gives it;
- **`deployment.json` carries no URL.** A deployment does not know how it was reached, and a
  baked-in URL would be wrong under the other route and wrong again in a packed bundle (§4.4);
- **the UI shows both links** (§8), with the subdomain marked as needing DNS until it resolves.

---

## 8. UI

> **As of v6.4.4:** the Deployments page (`/albums/deployments`) is listed under **Systems**
> in the sidebar, beside Workflows; the Albums group holds Albums and Transfers. Each card
> has the deployment band (v6.1.7); albums are ticked in and out on the card itself and
> republish at once; **Edit** opens a dialog (name, address/slug with the broken-link
> warning, description, albums, password) rather than flipping the card; the card's flip side is
> *What to pack*; and, for owners, the foot of the card has **Publish to AWS**
> (v6.2.0/v6.2.2). An album card also offers **Deploy** into a deployment.

**Under Albums, not Admin.** Storage routes are installation-wide, so they sit in Admin;
a deployment belongs to a catalog (§3), and the sidebar's Albums group is already gated on
`activeCatalog`. A second item beside "My albums":

```
Albums
  My albums
  Deployments        ← new, catalog-scoped like everything else here
```

Page shape follows Storage: a plain heading, an action bar with **New Deployment**, and cards
on the background.

Each card carries name and slug, both URLs as copyable links (§7.3) — shown in full, catalog
segment included, because that is what a recipient will paste — a lock icon when a password is
set, the album count, enabled/disabled, and Edit / Delete. Edit flips the card to a
back face with name, description, password (set / change / remove) and the album picker.

**The picker is a plain checklist of this catalog's albums** — no catalog column, because there
is only one catalog in play. That is the visible dividend of §3: the thing that would have been
the fiddliest control on the page is now a list.

The slug is shown, derived live from the name as it is typed (§3.1), and editable. Once a
deployment has been shared, changing it breaks that link — so an existing deployment warns
before accepting a new slug rather than silently rewriting the URL.

**The catalog's own slug is edited on the Catalogs page**, not here, and warns harder: it moves
every deployment in that catalog at once.

---

## 9. Risks

| Risk | Mitigation |
| --- | --- |
| **Symlinks resolve on the host but dangle in the container** — the failure `_deploy_album` already hit | §4.1 — relative links only, and an acceptance test that reads an image *through the gallery*, not off the host filesystem |
| **A locked deployment leaks its images** because only the HTML was gated | §5.3 — the check sits in front of `albums.json`, the asset route and the album list |
| **`.auth.json` served as a static asset** | §5.4 — refuse dotfiles, `realpath`-contain every asset path |
| **Two deployments collide** | Cannot: the URL is `<catalog>/<deployment>`, and each half is `UNIQUE` in its own table. No scan, by construction (§3.1) |
| **A two-label subdomain is assumed to work** — `summer-2019.family.shoebox.lan` | §7.2 — a TLS wildcard matches ONE label; verified against this installation's certificate. The catalog is the label, the deployment stays in the path |
| **A catalog has no slug** (created before this change, or the backfill failed) | §3.1 — it cannot host a deployment, and says so; the `ALTER` is guarded and the backfill derives from the name |
| **A shared link breaks when a slug is edited** | §8 — an existing deployment warns; the catalog slug warns harder, since it moves every deployment under it |
| **A deployment references a deleted album** | §6.3 refuses the delete and names the deployments; §6.2 still skips-and-reports for an album whose folder was never deployed |
| **Directory and database drift** (hand-edited, restored from backup) | `POST /publish` rewrites from the database; every mutation calls it anyway |
| **`CATALOGS_ROOT` set in one gallery launcher only** | `systems/test_pod_manifests.py` fails when a launcher and its pod manifest disagree — the check that caught gateway-pod.yaml shipping without TLS |
| **The gallery scans every catalog on every request** | §4.2 — index at boot, refresh on a miss |
| **Sessions lost on restart** | `ALBUM_SESSION_SECRET`; logged loudly when generated at boot |
| **A public gallery is brute-forced** | §5.2 throttling, per deployment and address |
| **A packed bundle ships dangling symlinks**, or `aws s3 sync` follows them and uploads whatever they point at | §4.4 — packing materialises real bytes; publishing and packing share the manifest but not the asset layer |
| **Someone assumes a STATIC bundle kept its password** | §4.4 — packing asks for a target. The Node target keeps the lock; the static target refuses or packs unlocked with a plain warning. No "password-protected static bundle" is ever offered |
| **"Self-contained" is read as "static files only"** and the deployed album is capped at what client-side JavaScript can do | §4.4 — self-contained means independent of THIS installation; `server.mjs` may ship inside the bundle |
| **An absolute path or hostname leaks into the bundle** and breaks it on another origin | §4.4 — everything relative, decided now rather than discovered at pack time |

---

## 10. Phases

**Phase 1 — deployments exist.** `catalog_meta.slug` with its backfill, tables in the catalog
database, slug derivation, publisher, CRUD, add/remove albums, the Albums → Deployments page.
*Acceptance:* create a deployment named "Family 2019", get the slug `family-2019` without
typing one, add two albums, and see
`<catalog>/cache/Deployments/summer-2019/albums.json` list exactly those two folders — with no
container touched. A second catalog may hold a `summer-2019` of its own without conflict.
Every photo in the album manifests carries `width` and `height` (§4.4).

**Phase 2 — the gallery serves them.** `CATALOGS_ROOT`, `catalogs.json`, and the
`/d/<catalog>/<slug>/…` routes in `server.mjs`; the SPA reads catalog and slug from its path.
*Acceptance:* two deployments in one container show different albums, and an image loads
**through the gallery**, proving the symlinks resolve inside the container (§9).

**Phase 3 — the URLs.** The nginx `location`, the wildcard subdomain server block, and the
SPA's base-path fix (§7.1).
*Acceptance:* `https://shoebox.lan/gallery/<catalog>/<slug>` works from another machine on the
network; and with a single `/etc/hosts` line for the **catalog**,
`https://<catalog>.shoebox.lan/<slug>` serves the same deployment — proving the subdomain route
works, that the existing certificate covers it, and that only DNS is outstanding.

**Phase 4 — passwords.** Hash on the backend, `.auth.json`, the login form, the signed cookie,
throttling.
*Acceptance:* a locked deployment returns 401 for `albums.json` **and** for a direct image URL
before login, and both succeed after.

**Phase 5 — per-deployment users.** Deferred by design: §5's session is the seam. A
`deployment_users` table and a username field on the same form, with the same cookie.

**Phase 6 — packing.** *Built as the command-line packer (§4.5.6 "As built"); the HTTP
endpoint was not, and the Node target runs the gallery image under Docker Compose rather
than shipping `server.mjs` for bare `node` (§4.5.4).* Specified in detail in §4.5, and unblocked: all three prerequisites in
§4.4 are met (dimensions are in the manifest, every path is relative, the manifest is
authoritative). `POST /api/catalogs/<cid>/deployments/<id>/pack?target=node|static` plus a
standalone script: materialise the symlinks into real files, write `catalogs.json`, add
`.auth.json` for the Node target, render `compose.yml` / `Caddyfile` / `README.md`, and hand
back a `.tar.gz`.

*Acceptance, Node target on a real VM (§4.5.5):* with only Docker and the tarball on the
machine — no shoebox, no source, no path home — `docker compose up -d` serves the deployment
over HTTPS at a DNS name, images and videos load, and a locked deployment returns 401 before
login and 200 after.

*Acceptance, both targets:*
- **Node** — unpack on another machine with nothing but `node` installed, run `server.mjs`, and
  browse the whole deployment. A locked deployment is still locked: `albums.json` and a direct
  image URL both return 401 before login and 200 after, with no shoebox anywhere in reach.
- **Static** — unpack into a plain file server, no Node at all, and browse the whole
  deployment. A locked deployment either refuses to pack for this target or packs unlocked with
  an explicit warning, and `.auth.json` is never in the bundle (§4.4).

---

## 11. Out of scope

- **Building an image per deployment.** §4.1 — the whole design exists to avoid it.
- **The backend starting or stopping containers** (§2.3).
- **Wildcard DNS itself.** The subdomain route is built (§7.2); making the names resolve is a
  dnsmasq config or a DNS record, not application work.
- **Per-deployment users** (§10, Phase 5).
- **The packer itself** (§10, Phase 6). §4.4 is the architecture it needs; the tar is not part
  of this change. *(Since built: `tools/pack_deployment.py`.)*
- **Uploading a bundle anywhere.** Packing produces a bundle; where it goes is the user's
  business, and an S3 client in this backend is a credential problem nobody asked for.
  *(Since v6.2.0 Airlock publishes to a server the owner set up, over SSH with a key per
  server; it still holds no AWS or other cloud credentials.)*
- **An authenticated media endpoint a deployed bundle can call back to** for originals it did
  not ship (§4.4, "Not decided here"). Considered and deliberately left open: it would make a
  remote album depend on this machine being awake, which is the opposite of what packing is
  for. If it is built, the credential must be scoped to one deployment and short-lived —
  anything a browser holds is public.
- **Editing album contents from the deployment page.** A deployment selects albums; albums are
  edited where they are made.
- **Deployments spanning catalogs** (§3). Decided against, not deferred: a catalog is the
  boundary that keeps work and family apart, and the People and face data a deployment will
  come to carry are catalog-local. Putting several albums in one deployment means putting them
  in one catalog.

---

## 12. Implementation checklist

```
Phase 1 — deployments  (endpoints under /api/catalogs/<cid>/deployments)
[x] album_deployments + album_deployment_albums in models.py's CatalogDatabase
    — the CATALOG database, beside albums, not the application one              (§3)
[x] catalog_meta.slug: guarded ALTER, UNIQUE index, backfilled from name        (§3.1)
[x] Both slugs derived from their name, overridable,
    ^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$                                           (§3.1)
[x] Uniqueness is by construction — <catalog>/<deployment>, each UNIQUE in its
    own table. NO cross-catalog scan                                            (§3.1)
[x] A catalog with no slug cannot host a deployment, and says so                (§3.1)
[x] Changing either slug warns that shared links will break; the catalog slug
    warns harder — it moves every deployment under it                           (§8)
[x] _publish_deployment(): relative symlinks, albums.json, deployment.json      (§6.2)
[x] A missing album folder is skipped and reported, never fatal                 (§6.2)
[x] Every mutation republishes; POST /publish repairs a hand-edited directory   (§6.1)
[x] Deployments live at <catalog>/cache/Deployments/<slug>, links ../../Albums/  (§4.1)
[x] Deleting an album a deployment publishes -> 409 naming the deployments      (§6.3)
[x] Deleting a catalog needs no check — it is a detach, deployments travel      (§6.3)
[x] Albums > Deployments page (NOT Admin): cards, edit face, plain album
    checklist — one catalog, so no catalog column                               (§8)
[x] album-<id>.json gains per-photo width/height from alternative_versions      (§4.4)
[x] Manifest is authoritative; no directory auto-discovery anywhere             (§4.4)
[x] No absolute path, URL or hostname in anything written to the deployment dir (§4.4)

Phase 2 — the gallery
[x] server.mjs: CATALOGS_ROOT, /d/<catalog>/<slug>/albums.json and /albums/<id>/(§4.2)
[x] catalogs.json {slug: directory}, written by the backend on catalog
    create/rename/delete; the gallery resolves from it, never by scanning       (§4.2)
[x] CATALOGS_ROOT in the gallery's start-podman.sh AND its pod manifest         (§9)
[x] ALBUM_ROOT still honoured for one release                                   (§4.2)
[x] Asset route refuses dotfiles and realpath-contains every path               (§5.4)
[x] Verify an image loads THROUGH the gallery, not off the host                 (§9)

Phase 3 — URL
[x] nginx location /gallery/ -> gallery /d/  (so /gallery/<cat>/<slug>)         (§7.1)
[x] nginx wildcard server_name -> gallery /d/$catalog/ — the CATALOG is the
    label, because a TLS wildcard matches exactly one                           (§7.2)
[x] $catalog is never joined to a path: resolved through catalogs.json, then
    realpath-contained                                                          (§7.2)
[x] deployment.json carries NO url — it does not know how it was reached        (§7.3)
[x] Card offers both links, subdomain marked as needing DNS until it resolves   (§7.3)
[x] SPA fetches become base-relative (App.tsx:95,109)                           (§7.1)
[x] /api/dimensions deleted from the SPA and the server — the manifest has it   (§4.4)

Phase 4 — password
[x] scrypt hash written by the backend to .auth.json, mode 0600                 (§5.2)
[x] deployment.json carries requires_password, never the hash                    (§4.3)
[x] POST /d/<cat>/<slug>/login, HMAC cookie keyed on catalog AND slug          (§5.2)
[x] ALBUM_SESSION_SECRET, with a loud warning when generated at boot            (§5.2)
[x] Throttled per deployment and address                                        (§5.2)
[x] A locked deployment gates albums.json, assets AND the album list            (§5.3)
[x] Verify: direct image URL is 401 before login, 200 after

Phase 6 — pack  (tools/pack_deployment.py; no HTTP endpoint)
[x] Materialise symlinks, SPA build (static), .tar.gz, --target node|static     (§4.4)
[x] Node target ships .auth.json (0600) and compose.yml naming the gallery
    image; the password gate still works                                        (§4.4)
[x] Static target ships no .auth.json; a locked deployment refuses unless
    --allow-unlocked-static, and then warns                                     (§4.4)

LATER
[ ] Phase 5 — per-deployment users, on the same session                         (§10)
[ ] Verify a Node bundle runs with only `node` installed, no Airlock in reach   (§10)
[ ] Verify a static bundle browses on a plain file server, no Node at all       (§10)
[ ] Wildcard DNS (dnsmasq, or *.lozaprada.org) — infrastructure, not code       (§7.2)
```
