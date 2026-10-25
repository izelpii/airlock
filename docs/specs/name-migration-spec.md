# Renaming Shoebox to Airlock Photos

**Status: phases 1–6 released in v5.6.0–v5.6.7 (2026-10-11); phase 7 run on
homeserver since; phase 8's fallback removal, due in v5.7, has not happened (still
pending at v6.4.4).** Written 2026-10-11.
Measured against `v5.5.3` and the live deployment on homeserver (`192.168.1.18`) the
same day.

| phase | release | state |
|---|---|---|
| 1 What people see | v5.6.0 | done |
| 2 Internal identifiers | v5.6.1 | done; `deploy/shoebox-env.sh` shim removed before v5.6.9, `tools/shoebox-venv.sh` shim removed in v6.4.5 |
| 3 Environment variables | v5.6.2 (auth key), v5.6.3 | done; fallbacks removed in v6.4.5 |
| 4 Hostnames | v5.6.4 | done in code; on homeserver: `make-certs.sh --force`, hosts lines |
| 5 Session cookie | v5.6.5 | done; fallback removed in v6.4.5 |
| 6 Gateway container | v5.6.6 | done |
| 7 homeserver's paths | v5.6.7 | done: homeserver runs from `/opt/airlock` |
| 8 Docs, repository, fallbacks out | v5.6.7 (docs) | living docs done; Gitea repository renamed (`izelpi/airlock`); fallbacks removed in v6.4.5 |

> **Completed in v6.4.5 (2026-10-25).** v5.7.0 shipped the Dashboard instead, so
> the fallbacks planned to go "in v5.7" stayed until v6.4.5, which removed them
> all: `SHOEBOX_*` settings are no longer copied to `AIRLOCK_*` (`env_compat.py`
> is gone), `deploy/shoebox.env` is not read, a `shoebox_session` cookie no
> longer signs in, the `shoebox.lan` CORS origins are out of the defaults, and
> the `tools/shoebox-venv.sh` shim and `deploy/move-to-opt-airlock.sh` are
> deleted. **Beyond the plan in §3.8,** the gateway's `shoebox.lan` redirects and
> the `shoebox.lan` names on the certificate went too: old `shoebox.lan` links
> no longer open. A CA still stored as `shoebox-ca.*` must be renamed to
> `airlock-ca.*` - `make-certs.sh` stops and prints the command rather than make
> a new CA; the certificate, and every device's trust in it, is unchanged.

Kept on purpose, beyond what §3 lists: homeserver's
`/etc/sysctl.d/99-shoebox-unprivileged-ports.conf`
(`systems/gateway/enable-port-80.sh:28`), and real paths on the Macs
(`~/Personal/Development/shoebox`) in pod manifests. The magic-wormhole APPID
and subprotocol were kept as `shoebox.app/album-transfer` and
`shoebox-album-transfer` through the rename, because both ends of a transfer
must agree.

> **Since v6.3.1:** they are `airlock.app/album-transfer` and
> `airlock-album-transfer` (`systems/backend/wormhole_runner.py:47,70`), a clean
> break: installs before and after v6.3.1 cannot exchange albums.

The application is called **Shoebox** everywhere: on the login screen, in the
version badge, in hostnames, environment variables, container and file names,
the TLS certificates, host paths and the licence. This spec covers renaming it
to **Airlock Photos**, short name `airlock`: how much has to change, what can go
wrong, and an order of work in which every step can ship and be reverted on
its own.

**Bottom line.** About 1,100 lines in 137 files mention the name. But the
catalogs need no data migration (§2.3), and most of the rest is mechanical.
The risk is in a small number of things *outside* the repository: shared album
links, device trust in the local CA, `/etc/hosts` and `/etc/fstab` on homeserver,
and the env file. Each of those has a compatibility path, so nothing has to
break on a single day.

---

## 1. Names

| | old | new |
|---|---|---|
| **Product name**: what people read (login screen, version badge, browser title, README, licence) | Shoebox — Photo Catalog & Workflow Manager | **Airlock Photos** |
| Wordmark | — | `airlock photos` (already in `systems/frontend/public/logo.png`, uncommitted on 2026-10-11) |
| **Short internal name**: the software, URLs, folders, files, containers, repository | `shoebox` | **`airlock`** |
| Code identifiers (shell functions, nginx upstreams, Python names) | `shoebox_` | `airlock_` |
| Environment variable prefix | `SHOEBOX_` | `AIRLOCK_` |
| Local domain | `shoebox.lan`, `*.shoebox.lan` | `airlock.lan`, `*.airlock.lan` |
| Install root | `/opt/shoebox` | `/opt/airlock` |
| Session cookie | `shoebox_session` | `airlock_session` |

**The rule.** "Airlock Photos" appears only where a person reads the product's
name: about five places (§2.2, §3.1), plus the licence. Everything else uses
`airlock`: technical prose ("restart airlock's backend"), hostnames, paths,
files, the repository and identifiers. `airlock` contains no hyphen, so the
same word works as a file name, a hostname, a bash function prefix and a
Python name.

**Is the name free?** A web search on 2026-10-11 found no product, app or site
called "Airlock Photos" or "airlock-photos". "Airlock" alone is taken in other
fields: Airlock Digital (application allowlisting, with US trademark filings
for security software), an encrypted messaging app (airlock-app.com), and
Airlock.Pro (smart locks). None of them is photo software. The short name
`airlock` only appears on the household's own network (`airlock.lan`) and in
the repository, so the product name is what would matter if the software were
ever published. A web search is not a trademark clearance (R10).

---

## 2. Inventory

### 2.1 In the repository

Counted with `git grep -ic shoebox` at `v5.5.3`:

| area | lines | files | what it is |
|---|---:|---:|---|
| `documentation/` | 340 | 36 | prose; partly historical (§3.8) |
| `deploy/` | 257 | 6 | `shoebox-env.sh` (shell functions, `SHOEBOX_*` vars), `deploy.sh`, `shoebox.env.example` |
| `systems/backend` | 186 | 40 | env vars, `--add-host` names, cookie name, `import version as shoebox_version`, test fixtures |
| `systems/gateway` | 105 | 10 | `shoebox.conf.template` (nginx `server_name`, `upstream shoebox_*`), `make-certs.sh`, hosts entries, `shoebox-gateway` image |
| `start-all.sh`, `stop-all.sh` | 38 | 2 | sourcing `shoebox-env.sh`, `SHOEBOX_*` |
| other `systems/*` services | ~140 | ~25 | `start-podman.sh`, pod manifests (`hostAliases: *.shoebox.lan`) |
| `systems/test_service_names.py`, `test_pod_manifests.py` | 24 | 2 | tests that *enforce* the `*.shoebox.lan` names |
| `systems/frontend`, `album-gallery` | 22 | 13 | user-visible strings, a CSS class, a drag MIME type |
| `tools/` | 27 | 5 | `shoebox-venv.sh` |
| `README.md`, `CHANGELOG.md`, `license.txt`, `.gitignore` | 24 | 4 | |

Files whose **names** contain it: `deploy/shoebox-env.sh`,
`deploy/shoebox.env.example`, `systems/gateway/shoebox.conf.template`,
`tools/shoebox-venv.sh`.

By form, the most common are `shoebox.lan` and its subdomains (~250),
`SHOEBOX_*` variables (~30 distinct, ~250 uses), `shoebox_*` shell functions
(~25 distinct), and the bare word in prose (~130).

### 2.2 User-visible strings

Only a handful:

- `systems/frontend/src/views/Login.js`: "Welcome to Shoebox", title "Shoebox"
- `systems/frontend/src/components/VersionBadge.js`: "Shoebox 5.5.3"
- `systems/frontend/src/components/CatalogForm.js`: example `family.shoebox.lan`
- `systems/frontend/src/context/AuthContext.js`: error text naming `https://shoebox.lan`
- Backend alert text: "Used by: Shoebox (application database)"

Browser `localStorage` keys don't contain the name (`activeCatalogId`,
`album-gallery.*`), so no stored browser state is lost.

### 2.3 In the catalogs: nothing to migrate

Scanned read-only on homeserver:

- **File paths are relative** (`Images/2021/03/25/…`). None of the 373,020
  `family` rows starts with `/`.
- **Catalog roots are container paths** (`/catalogs/family`,
  `/catalogs/second-catalog` in `catalog_meta.target_directory`), not host
  paths. `storage_routes` doesn't contain the name.
- The only matches in any table under 20,000 rows are **historical messages**:
  one alert and one failed run's `error_message` naming `deepface.shoebox.lan`.
  They stay as written.

So the 25.7 GB `family` database and the derived `cache/` don't change. That
removes the largest risk such a rename usually carries.

### 2.4 Outside the repository

| where | what | consequence of a rename |
|---|---|---|
| homeserver `/opt/shoebox` | the install, git work tree, catalogs, certs | path is in the env file, fstab, the Mac's git remote, memory notes |
| homeserver `/etc/fstab` | 6 bind mounts `/media/usb1/shoebox-storage/<cat>/* → /opt/shoebox/catalogs/<cat>/*` | catalogs lose their photos if paths diverge; `nofail` means it **fails silently** |
| homeserver `/etc/hosts` | 3 lines of `*.shoebox.lan → 127.0.0.1` | gateway names stop resolving on homeserver |
| homeserver `deploy/shoebox.env` | not in git: `SHOEBOX_HOST_IP`, `STORAGE_ROOT_HOST`, `DEEPFACE_MODEL`, `DEEPFACE_WARMUP_MODELS` | a renamed variable read without fallback reverts to its default |
| homeserver podman | `localhost/shoebox-gateway` image, `shoebox-gateway` container | a new container name **does not replace the old one** (§4, R3) |
| `systems/gateway/certs/` | CA `CN=Shoebox Local CA (oldserver), O=Shoebox`; leaf `CN=shoebox.lan`, SAN `*.shoebox.lan` | new hostnames need a new leaf; a new CA needs re-trust on every device |
| every device | CA installed as trusted; `/etc/hosts` entries on both Macs | |
| people | shared album links like `https://family.shoebox.lan/summer-2019` | links break unless the old names keep answering |
| `git.example.lan:3000/izelpi/shoebox` | the origin repository | Gitea redirects a renamed repo's old URL, but check it on this version |
| Macs | `~/Development/shoebox`, `~/Personal/Development/shoebox` (in `album-gallery-pod.yaml`) | Claude Code's memory folder is keyed by the project path (§3.9) |
| oldserver `192.168.1.17` | retired, read-only | out of scope |

---

## 3. Plan

Eight phases. Each is one or more ordinary releases, can be deployed alone, and
leaves the system working. Phases 1–3 touch only the repository. Phases 4–7
touch homeserver and devices and each has a fallback.

### 3.1 Phase 1: what people see

Change the strings in §2.2, the browser tab title (`systems/frontend/public/index.html`,
currently "Workflow Manager"),
`README.md`'s heading and introduction, and commit the new logo. The version
badge reads "Airlock Photos 5.6.0".

- Risk: none. No identifier, URL or name changes.
- Effort: under an hour.
- Release: this is the **minor** that names the product (`v5.6.0`); everything
  after it is patches.

### 3.2 Phase 2: internal identifiers

Mechanical renames that never leave a process:

- shell functions `shoebox_*` → `airlock_*` in `deploy/shoebox-env.sh`,
  `tools/shoebox-venv.sh` and their callers
- nginx `upstream shoebox_frontend|backend|vision|gallery|…` → `airlock_*`
- `import version as shoebox_version` → `airlock_version`
- CSS class `shoebox-count`, drag type `application/x-shoebox-section`
- the files themselves: `deploy/airlock-env.sh`, `tools/airlock-venv.sh`,
  `systems/gateway/airlock.conf.template`, with a one-line **shim** left at
  each old path (`source "$(dirname "$0")/airlock-env.sh"`) for one minor
  version, so a script or habit that sources the old name still works

Not here: container and image names (Phase 6), env vars (Phase 3), hostnames
(Phase 4).

- Risk: low. The test suite covers the shell environment and the gateway
  config (`check-config.py`). A missed caller fails loudly at startup, not
  silently.
- Effort: half a day.

### 3.3 Phase 3: environment variables

`SHOEBOX_*` → `AIRLOCK_*`, **with fallback**. At the top of
`airlock-env.sh`, before anything reads them:

```bash
# Accept the old prefix for one minor version, and say so.
for _old in $(compgen -v SHOEBOX_); do
  _new="AIRLOCK_${_old#SHOEBOX_}"
  if [[ -z "${!_new+x}" ]]; then
    export "$_new=${!_old}"
    echo "note: $_old is now $_new (deploy/airlock.env)" >&2
  fi
done
```

The Python services do the same in one helper, `env("AUTH_ENABLED")`, which
reads `AIRLOCK_X` and falls back to `SHOEBOX_X`. The env file:
`deploy/airlock.env` is read first, then `deploy/shoebox.env`, so homeserver's
existing file keeps working until someone renames it.

- Risk: a name missed by the rename silently takes its default. Environment
  variables mostly fail safe: `SHOEBOX_AUTH_REQUIRED` defaults to on.
  **The Flask config key `SHOEBOX_AUTH_ENABLED` does not.** `auth.py` sets it
  once, and eleven places in `app.py` and `auth.py` read it with
  `app.config.get(...)`. If the setter is renamed and one reader isn't, that
  reader gets `None` and **skips the login check**. Rename this key to a
  name-free `AUTH_ENABLED` in a commit of its own, and have every reader go
  through one function.
  **Mitigation:** a test that fails if any non-shim file outside `CHANGELOG.md`
  and `documentation/fixes/` still contains `SHOEBOX_`.
- Effort: half a day.

### 3.4 Phase 4: hostnames, both names at once

The local domain moves to `airlock.lan`, and `shoebox.lan` **keeps answering**:

1. **Certificates.** `make-certs.sh` already takes extra domains. Issue a new
   *leaf* with `airlock.lan`, `*.airlock.lan`, `shoebox.lan`, `*.shoebox.lan`,
   signed by the **existing CA**. Devices that trust the CA trust the new leaf
   with no action (§3.7).
2. **Gateway.** Every `server_name` lists both domains. Requests to the old
   names get a **301 to the same path on the new name**. That includes catalog
   subdomains (`family.shoebox.lan/summer-2019` →
   `family.airlock.lan/summer-2019`), so shared album links keep working.
   This redirect has no end date; it costs one server block.
3. **Internal names.** The `--add-host` / `hostAliases` names containers use
   to reach each other (`backend.`, `deepface.`, `vision.`, `geoloc.`,
   `albums.`, `gallery.`, `ollama.`, `pets.`, `transcode.`) switch to
   `*.airlock.lan` in one release. They're internal, so nothing outside needs
   both. `systems/test_service_names.py` and `test_pod_manifests.py` change
   with them and keep enforcing that every name is declared everywhere.
4. **CORS.** `AIRLOCK_ALLOWED_ORIGINS` includes both origins until Phase 8.
5. **Hosts files.** Append the `airlock.lan` lines from the new
   `hosts-entries.txt` on homeserver and both Macs. Remove the old lines only in
   Phase 8.

- Risk: medium. It's the phase people see break if done wrong. A browser on a
  device without the new hosts lines can't reach `airlock.lan`, but the old
  name still works there, so nothing is lost.
- Effort: half a day, plus a minute per device.

### 3.5 Phase 5: session cookie

Read `airlock_session` and fall back to `shoebox_session`. Always write
`airlock_session`, and clear the old one on the next response. Nobody is
logged out. Dropping the fallback later logs out anyone who hasn't visited
since, which is harmless.

Cookies are per host. The 301 from `shoebox.lan` to `airlock.lan` lands
without a cookie on the new host, so **everyone signs in once** after Phase 4,
whatever the cookie is called. Accept that, and say so in the release notes.

- Effort: an hour.

### 3.6 Phase 6: container and image names

Only `shoebox-gateway` carries the name (the others are `workflow-backend`,
`backend-deepface`, …). Rename it to `airlock-gateway`.

- **Risk (R3):** `podman run --replace --name airlock-gateway` replaces a
  container *of that name*. The old `shoebox-gateway` keeps running and keeps
  ports 80 and 443, so the new one fails to bind. The start script has to
  `podman rm -f shoebox-gateway` first, once.
- Restarting the gateway doesn't interrupt workflows. Deploy only that service
  (see memory note "deploys restart running workflows").
- Effort: an hour.

### 3.7 Phase 7: homeserver's paths, and the CA

**`/opt/shoebox` → `/opt/airlock`.** `deploy/move-to-opt-airlock.sh` does all of
this: run without arguments it checks and prints the plan, with `--apply` it
acts, and it will not start the stack unless every mount is back. By hand, in a
window with no workflow running:

1. `./stop-all.sh`
2. `sudo umount` the six bind mounts
3. `sudo mv /opt/shoebox /opt/airlock`, then `sudo ln -s /opt/airlock /opt/shoebox`
   (the symlink catches anything missed: scripts, notes, the Mac's remote)
4. edit the six `/etc/fstab` lines to `/opt/airlock/catalogs/…`, then
   `sudo systemctl daemon-reload && sudo mount -a`
5. **check before starting:** `ls /opt/airlock/catalogs/family/Images | head`
   is non-empty for all six mounts. `nofail` means a wrong path mounts nothing
   and raises no error, and the catalog then shows every photo as missing.
6. `./start-all.sh`, then update the Mac's remote:
   `git remote set-url homeserver ssh://homeserver/opt/airlock`

`/media/usb1/shoebox-storage` stays as it is (decided 2026-10-11). Nobody sees
it, and renaming a directory on the exfat easystore is a second fstab edit for
no visible gain.

**The CA: keep it.** It's named `Shoebox Local CA (oldserver)` and is trusted on
every device. The memory notes record that the misnomer is kept on purpose so
that trust holds. A new CA means reinstalling and re-trusting it on every
phone, tablet and computer. Phase 4's new leaf is signed by the existing CA,
so no device needs touching. Only a **fresh install** gets
`CN=Airlock Local CA (<host>)` from the renamed `make-certs.sh`. The CA's file
names (`shoebox-ca.pem`/`.key`) can be renamed on homeserver, but `make-certs.sh`
must look for both, or it will generate a new CA.

- Risk: medium (fstab). Effort: about an hour, with a restart.

### 3.8 Phase 8: documentation, repository, and removing the fallbacks

- **Living documents** (`README.md`, `documentation/guides/`, `architecture/`,
  current specs, `deploy/README.md`) say `airlock` in technical text and
  "Airlock Photos" where they name the product.
- **Historical documents** keep "Shoebox": `CHANGELOG.md` entries before
  `v5.6.0`, `documentation/fixes/`, and dated specs. They describe what
  happened under that name, and rewriting them would make the history
  disagree with the commits. `CHANGELOG.md` gets one line: "Shoebox was
  renamed Airlock Photos in v5.6.0."
- **Licence: done in `v5.5.4`.** `license.txt` now names "Airlock Photos" as
  "the Software". The product has never been released under the old name, so
  the agreement needs no "formerly known as" clause.
- **Repository.** Rename `git.example.lan:3000/izelpi/shoebox` to `airlock`. Check
  that the old URL redirects, then `git remote set-url origin` on each clone.
  Renaming local folders is optional (§3.9).
- **Fallbacks out.** One minor version after Phase 3, remove the `SHOEBOX_`
  fallback, the env-file fallback, the file shims, the cookie fallback, the
  `shoebox.lan` CORS origin and the old hosts lines. **Keep** the gateway's
  `shoebox.lan` 301 redirect, the `shoebox.lan` SANs on the leaf, and the
  `/opt/shoebox` symlink. They cost nothing and protect old links.

> **As of v6.4.5:** the repository is renamed (`git.example.lan:3000/izelpi/airlock`) and
> the fallbacks are out. Unlike this plan, the 301 redirects and the
> `shoebox.lan` names on the certificate were removed as well (see the status
> note at the top).

### 3.9 The development machine

Claude Code stores this project's memory under
`~/.claude/projects/-Users-izelpi-Development-shoebox/`. That folder is keyed by
the working directory's path. If `~/Development/shoebox` becomes
`~/Development/airlock`, a new session starts with **no memory**: the notes
that Shoebox runs on homeserver, that homeserver must stay on mains, and that deploys
restart workflows. Copy the folder to
`-Users-izelpi-Development-airlock/` when the directory moves, or don't move
the directory.

> **Since 2026-10:** the project folder is `~/Development/airlock`, and the
> memory folder moved with it.

---

## 4. Risks

| # | risk | likelihood | impact | mitigation |
|---|---|---|---|---|
| R1 | Shared album links (`family.shoebox.lan/…`) break | certain without mitigation | people outside the household lose access | permanent 301 from every old name (§3.4) |
| R2 | A missed `SHOEBOX_` name silently takes its default. Worst case: a reader of the `SHOEBOX_AUTH_ENABLED` config key gets `None` and skips the login check | medium | security or wrong behaviour, unnoticed | rename that key alone, behind one accessor; prefix fallback with a warning; a test against leftovers (§3.3) |
| R3 | `airlock-gateway` can't bind 80/443 because `shoebox-gateway` still holds them | certain on first deploy | site down until noticed | `podman rm -f shoebox-gateway` once in the start script (§3.6) |
| R4 | fstab edited wrongly; `nofail` hides it; catalogs show every photo missing | low | alarming, though no data is lost | check each mount before starting (§3.7 step 5); symlink |
| R5 | Generating a new CA, deliberately or because `make-certs.sh` can't find `shoebox-ca.*` | low | every device shows certificate errors until it re-trusts | keep the CA; script looks for both file names (§3.7) |
| R6 | Everyone has to sign in again | certain after Phase 4 | minor | cookies are per host; say so in the release notes |
| R7 | A deploy restarts the backend mid-workflow | depends on timing | hours of work interrupted | check `workflow_executions` first; deploy only changed services |
| R8 | Claude Code memory orphaned by moving the project folder | certain if moved | lost operational knowledge | copy the memory folder (§3.9) |
| R9 | Over-eager search and replace rewrites history (changelog, fix notes) | medium | misleading records | historical files excluded (§3.8) |
| R10 | Name collision. No "Airlock Photos" was found (§1), but "Airlock" alone has registered marks for software (Airlock Digital; Airlock, Inc.) | low while private | matters only if the app is ever published or sold | a proper trademark search (USPTO, and wherever it would be sold) before any commercial use; see `commercial-licensing-audit.md` |

---

## 5. Verification

After each phase, on homeserver:

- `https://192.168.1.18/` loads, signs in, and the badge shows the new version
- `https://airlock.lan/` and `https://shoebox.lan/` both work (from Phase 4),
  the second redirecting to the first
- an album link on an old catalog subdomain redirects and plays
- Find Faces → Measure Faces → Match Faces runs a short chain to completion,
  which exercises the backend → deepface internal hostnames
- `git grep -n SHOEBOX_` outside the allowed historical files and shims
  returns nothing (from Phase 3; enforced by a test)
- `podman ps` shows no `shoebox-gateway` (from Phase 6)
- each of the six bind mounts lists files (from Phase 7)

---

## 6. Effort

| phase | repository work | homeserver / devices | restart |
|---|---|---|---|
| 1 Branding | < 1 h | — | frontend |
| 2 Internal identifiers | ½ day | — | all (one window) |
| 3 Env vars | ½ day | — | all (same window) |
| 4 Hostnames | ½ day | hosts lines on 3 machines | gateway + all |
| 5 Cookie | 1 h | — | backend |
| 6 Gateway container | 1 h | — | gateway |
| 7 Paths | — | ~1 h, careful | all |
| 8 Docs, repo, fallbacks out | ½ day | git remotes | — |

About **three working days** in total. Phases 2–5 can share one deploy window,
so homeserver restarts the full stack about three times: once for 2–5, once for
Phase 7, and once to remove the fallbacks.

---

## 7. Decisions needed

1. ~~The names~~: decided 2026-10-11. **Airlock Photos** is the product name,
   **`airlock`** the short internal name (§1).
2. ~~Renaming `/media/usb1/shoebox-storage`~~: decided 2026-10-11, **no** (§3.7).
3. ~~Whether to rename the local project folder on the Macs~~: done for
   `~/Development/airlock`, with the memory folder (§3.9). The pod manifests
   still name `~/Personal/Development/shoebox`.
4. ~~The licence wording~~: done in `v5.5.4` (§3.8).
