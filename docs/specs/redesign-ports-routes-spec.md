# Ports to Named Endpoints — Implementation Specification

**Status:** phases 1-3 implemented; the gateway is already in `v4.0.0`, the first commit in
this repository. Phase 4 (LAN DNS) and phase 5 (one pod) still open at `v6.4.4`. See section 9.1
for one deliberate deviation from the plan below.
**Decision:** the browser uses **same-origin paths** under `shoebox.lan`, not a `backend.shoebox.lan`
subdomain. Service subdomains still exist for service-to-service calls and direct access. Settled;
see section 5 for the reasoning.
**Supersedes:** the original draft (preserved as `redesign-ports-routes-spec-orig.md`)
**Related:** `documentation/architecture/CONTAINERIZATION_AUDIT.md`,
`documentation/features/CONTAINERIZATION_TRADEOFFS.md`,
`documentation/features/combining-backends.md`

> **What changed since (as of v6.4.4, 2026-10-25):**
> - **Names.** `shoebox.lan` became `airlock.lan` in v5.6.4 (`name-migration-spec.md`). The old
>   names answered with a 301 until v6.4.5, and are no longer served at all. This spec keeps the
>   old names as written: read `shoebox.lan` as `airlock.lan` throughout.
> - **TLS and sign-in** (out of scope in §11) came in v4.1.0: the gateway serves HTTPS on 443 and
>   redirects :80 to it (`airlock.conf.template:44-77`); sign-in lives in the backend
>   (`user-authentication-spec.md`). The `<service>.airlock.lan` blocks stay plain HTTP on :80.
> - **More services:** `backend-transcode` (:5004, v4.9.0) is *not* routed by the gateway - the
>   backend calls `http://transcode.airlock.lan:5004` directly (`systems/backend/backend-pod.yaml:69`);
>   `backend-pets` (:5006, v5.2.0) is `pets.airlock.lan`; the gateway also serves `/gallery/` and
>   `<catalog>.airlock.lan` album sites (`airlock.conf.template:192,257`).
> - **deepface** is no longer always on the host: since v4.9.0 it runs native or in a container
>   (`accelerated-services-spec.md`).
> - **The gateway reaches the host as `airlock-host`**, a name set only by `--add-host`, not
>   `host.containers.internal` (v5.13.1; `systems/gateway/start-podman.sh:24-30`).
> - The real config file is `systems/gateway/airlock.conf.template`, not `nginx.conf`.

---

## 1. The one thing the draft is missing

> "instead, the backend should point now to backend.shoebox.lan, without any more references to 5000"

**A DNS name resolves to an IP address, not to an IP address and a port.** `http://backend.shoebox.lan/`
means `backend.shoebox.lan:80`. So adding names does not remove ports — it moves every service to
port 80 and creates a collision, because seven services cannot all listen there.

What actually removes the ports is a **reverse proxy**: one process listening on port 80 that reads
the `Host:` header and forwards to the right service. DNS makes every name resolve to the same
machine; the proxy decides which service the request belongs to.

So the new component is not primarily a DNS resolver. It is a **gateway**. DNS is the smaller half
of the problem, and for a fixed set of eight names `/etc/hosts` alone may be enough.

`.lan` is a good choice, incidentally, and worth keeping: `.local` is claimed by mDNS/Bonjour on
macOS and would collide.

```
                     browser / other services
                              │
                    *.shoebox.lan  ->  127.0.0.1        (DNS: /etc/hosts or dnsmasq)
                              │
                              ▼
                  ┌───────────────────────┐
                  │   gateway  :80        │   routes on the Host header
                  └───────────┬───────────┘
        ┌──────────┬──────────┼──────────┬──────────┬──────────┐
        ▼          ▼          ▼          ▼          ▼          ▼
   frontend   backend    deepface     vision     geoloc     albums    gallery
     :3000     :5000      :5001*      :5003      :5002      :5005      :3001

   * deepface runs on the HOST, not in a pod (Metal has no passthrough into
     podman's Linux VM). The gateway must reach it at host.containers.internal.
```

> **Since v4.9.0 / v5.13.1:** deepface runs native *or* in a container, by `AIRLOCK_DEEPFACE_MODE`
> (`deploy/airlock-env.sh`, `airlock_service_mode`), and either way publishes :5001 on the host;
> the gateway reaches the host as `airlock-host` (`systems/gateway/start-podman.sh:30`).

---

## 2. Current state

> As of the time of writing (2026-09-04). Today there are ten, plus Ollama: see
> `docs/architecture/ARCHITECTURE.md`, "Services".

Eight services, each published on its own host port, in **separate pods with no shared network
namespace and no DNS between them**. That last fact is the structural reason this migration is
worth doing.

| Service | Dir | Port | Runs in |
| --- | --- | --- | --- |
| frontend (nginx + CRA build) | `systems/frontend` | 3000 | pod |
| album-gallery (node) | `systems/album-gallery` | 3001 | pod |
| backend (Flask + Socket.IO) | `systems/backend` | 5000 | pod |
| backend-deepface (Flask) | `systems/backend-deepface` | 5001 | **host** |
| backend-geoloc (Flask) | `systems/backend-geoloc` | 5002 | pod |
| backend-vision (Flask) | `systems/backend-vision` | 5003 | pod |
| backend-albums (Flask) | `systems/backend-albums` | 5005 | pod |
| Ollama (external) | — | 11434 | host |

`backend-albums` was not in the draft's mental model but is a real service started by
`start-all.sh:94`; it must be included.

---

## 3. Name plan

The names split into two groups, and which group a caller uses is a decision, not a preference:

- **`shoebox.lan` alone** is what the *browser* uses — the UI and the API arrive on one origin. See
  section 5.
- **The service subdomains** are for *service-to-service* calls, `curl`, and reaching one backend
  directly. They are never compiled into the frontend bundle.

| Name | Target | Used by | Notes |
| --- | --- | --- | --- |
| `shoebox.lan` | frontend :3000 + backend :5000 | **the browser** | UI, `/api/`, `/socket.io/`, `/vision/`, `/geoloc/` |
| `backend.shoebox.lan` | backend :5000 | services, curl | REST + Socket.IO |
| `deepface.shoebox.lan` | host :5001 | backend, workflows | proxied to `host.containers.internal` |
| `vision.shoebox.lan` | vision :5003 | curl | browser uses `/vision/` instead |
| `geoloc.shoebox.lan` | geoloc :5002 | curl | browser uses `/geoloc/` instead |
| `albums.shoebox.lan` | albums :5005 | backend | |
| `gallery.shoebox.lan` | album-gallery :3001 | the browser, directly | already fully same-origin internally |
| `ollama.shoebox.lan` | host :11434 | vision, albums | optional; lets them stop naming the host |

The single most consequential line in that table is the first one: the browser gets the API from the
same origin as the page, which is what keeps the backend address out of the compiled bundle.

> **As of v6.4.4:** every name is under `airlock.lan` (v5.6.4). Added since: `pets.airlock.lan`
> (:5006), `transcode.airlock.lan` (resolved by the containers, not routed by the gateway), the
> `/gallery/` path and `<catalog>.airlock.lan` for published albums
> (`systems/gateway/hosts-entries.txt`, `airlock.conf.template:29-37`).

---

## 4. The gateway

### 4.1 Where it lives

New directory `systems/gateway/`, following the shape of every other service here: a `Dockerfile`,
a `gateway-pod.yaml`, and a `start-podman.sh` with the rebuild-on-stale guard the other scripts now
have.

Use **nginx**. It is already in the stack (the frontend image is nginx), the team has a working
config to copy, and it needs no new runtime. Caddy would give automatic TLS, which this LAN-only
deployment does not need; Traefik would give container auto-discovery, which seven static services
do not need.

Do **not** extend the frontend's nginx to do this. The frontend image rebuilds whenever `src/`
changes, and routing config should not be coupled to a React build — that is the same coupling that
made schema changes invisible in `CONTAINERIZATION_TRADEOFFS.md`.

### 4.2 Config shape

Write a real `nginx.conf` file and `COPY` it. Do not use the `printf`-into-a-Dockerfile-RUN trick
the frontend uses; this config is too large to stay readable that way.

```nginx
# systems/gateway/nginx.conf  (sketch — see §4.3 for the Socket.IO block)

upstream shoebox_frontend { server host.containers.internal:3000; }
upstream shoebox_backend  { server host.containers.internal:5000; }
upstream shoebox_deepface { server host.containers.internal:5001; }
upstream shoebox_vision   { server host.containers.internal:5003; }
upstream shoebox_geoloc   { server host.containers.internal:5002; }
upstream shoebox_albums   { server host.containers.internal:5005; }
upstream shoebox_gallery  { server host.containers.internal:3001; }

server {                                    # bare name: the UI, plus same-origin API
    listen 80;
    server_name shoebox.lan;

    location /api/ { proxy_pass http://shoebox_backend;  include /etc/nginx/proxy.conf; }
    location /socket.io/ { ... see 4.3 ... }
    location /vision/ { rewrite ^/vision/(.*) /$1 break;
                        proxy_pass http://shoebox_vision;  include /etc/nginx/proxy.conf; }
    location /geoloc/ { rewrite ^/geoloc/(.*) /$1 break;
                        proxy_pass http://shoebox_geoloc;  include /etc/nginx/proxy.conf; }
    location / { proxy_pass http://shoebox_frontend; include /etc/nginx/proxy.conf; }
}

server { listen 80; server_name backend.shoebox.lan;
         location / { proxy_pass http://shoebox_backend; include /etc/nginx/proxy.conf; } }
# ... one server block per name in the table above ...
```

`proxy.conf` carries the headers every block needs:

```nginx
proxy_set_header Host              $host;
proxy_set_header X-Real-IP         $remote_addr;
proxy_set_header X-Forwarded-For   $proxy_add_x_forwarded_for;
proxy_set_header X-Forwarded-Proto $scheme;
proxy_http_version 1.1;
client_max_body_size 100M;      # /api/upload sends full-resolution images
proxy_read_timeout 600s;        # backend-vision has a 600s gunicorn timeout
```

Both of those last two are not optional. `client_max_body_size` defaults to 1 MB, which would
reject every image upload; `proxy_read_timeout` defaults to 60 s, which would cut off vision
descriptions and long workflow requests mid-flight. A migration that gets the names right and these
wrong looks like the services became flaky.

### 4.3 Socket.IO

`systems/backend/Dockerfile:72-79` documents two constraints that the gateway must respect:

- The backend runs `--worker-class gthread` with `async_mode='threading'`, so Socket.IO negotiates
  **HTTP long-polling, not websockets**. The client (`services/socket.js:12`) still asks for
  `websocket` first and falls back.
- There is **exactly one gunicorn worker**, because workflow execution state and Socket.IO sessions
  live in process memory.

So the gateway must handle a failed upgrade cleanly and must not buffer the polling stream:

```nginx
location /socket.io/ {
    proxy_pass http://shoebox_backend;
    include /etc/nginx/proxy.conf;
    proxy_set_header Upgrade    $http_upgrade;
    proxy_set_header Connection $connection_upgrade;   # map, see below
    proxy_buffering off;          # long-polling must not be buffered
    proxy_read_timeout 3600s;     # a poll holds the connection open
}
```

with, at `http` level:

```nginx
map $http_upgrade $connection_upgrade { default upgrade; '' close; }
```

**Acceptance for this section is behavioural, not structural:** start a long workflow and watch the
queue panel's progress bar advance through the gateway. A proxy that mishandles this breaks live
progress *silently* — the app still works, the bars just never move.

### 4.4 Port 80 and the podman VM

**Confirmed on the target machine: rootless podman refuses it by default.**

```
rootlessport cannot expose privileged port 80, you can add
'net.ipv4.ip_unprivileged_port_start=80' to /etc/sysctl.conf (currently 1024),
or choose a larger port number (>= 1024)
```

The restriction is inside the podman machine's Linux VM, not on macOS. Two supported answers:

1. **Lower the floor in the VM** — `systems/gateway/enable-port-80.sh`. Writes
   `/etc/sysctl.d/99-shoebox-unprivileged-ports.conf` in the VM and applies it, so it survives VM
   restarts. Touches nothing on the Mac. This is the default, because it is what keeps the names
   portless: `http://backend.shoebox.lan` is a name, `http://backend.shoebox.lan:8080` is a name and
   a port, and the second is most of what this migration set out to remove.

2. **Run on 8080** — `enable-port-80.sh --port-8080` prints exactly what to change. The browser needs
   no change at all, because `config.js` is relative and follows whatever origin served the page;
   only the service-to-service URLs (all env values in pod manifests) gain `:8080`.

`start-podman.sh` checks the VM's floor before calling `podman run`, so the failure arrives as the
remedy rather than as a wall of rootlessport output. `start-all.sh` greps the log for this specific
error and prints the fix.

> **Since v4.1.0:** the gateway publishes 443 as well as 80 (`systems/gateway/start-podman.sh:195-196`),
> and both must be allowed; the 8080 escape hatch pairs with `TLS_PORT=8443`
> (`start-podman.sh:150-179`). The sysctl file keeps its old name,
> `99-shoebox-unprivileged-ports.conf` (`enable-port-80.sh:28`).

---

## 5. The frontend: same-origin paths

**Decided.** The browser is served the UI and the API from one origin, `shoebox.lan`. The backend
address is never compiled into the JavaScript bundle.

The reasoning, which is worth keeping because it is the part that is easy to reverse by accident:

`systems/frontend/src/config.js:11-14` says it plainly:

> Create React App inlines any REACT_APP_* variable at BUILD time, so these must be set when the
> image is built, not when it runs.

If the browser is told to call `http://backend.shoebox.lan`, that string is **baked into the
JavaScript bundle at image build time**. Changing it later means a frontend rebuild. This has
already gone wrong repeatedly in this project — a stale bundle is invisible, because the source is
correct and the build succeeds.

`config.js:19-22` already describes the exit, and this spec takes it:

> A cleaner long-term option is to serve the API through the same nginx that serves this bundle, and
> use same-origin relative paths ('/api/...'), which removes build-time configuration altogether.
> Set REACT_APP_API_HOST='' to get that behaviour.

With the gateway serving both the bundle and `/api/` under `shoebox.lan`:

```js
export const API_HOST    = host('REACT_APP_API_HOST', '');     // same origin
export const VISION_HOST = host('REACT_APP_VISION_HOST', '/vision');
export const GEOLOC_HOST = host('REACT_APP_GEOLOC_HOST', '/geoloc');
```

Everything downstream — `API_BASE_URL`, `FILES_BASE_URL`, `SOCKET_URL`, and all ~40 call sites —
keeps working unchanged, because they are all built by concatenation from these three.

Why this, and not `backend.shoebox.lan` for the browser:

| | subdomain for the browser (rejected) | same-origin paths (chosen) |
| --- | --- | --- |
| Change the address later | rebuild the frontend image | edit gateway config, reload |
| CORS | still needed (cross-origin) | **eliminated** — same origin |
| Socket.IO origin | cross-origin, needs `cors_allowed_origins` | same origin |
| Opening the UI from a phone | needs a rebuild per address | works via any name that reaches the gateway |

The service subdomains from section 3 still exist and still work — for `curl`, for service-to-service
calls, and for reaching one backend directly. They are simply not what the bundle is compiled
against.

**The rule to keep:** if a URL ends up inside the frontend bundle, it must be relative. A future
change that reintroduces an absolute service URL in `src/` reintroduces the rebuild-to-readdress
problem with it, and the failure is silent — the source looks right and the build succeeds.

---

## 6. DNS

Three different resolvers need to answer for `*.shoebox.lan`, and they are easy to conflate.

### 6.1 The browser (macOS)

Simplest: `/etc/hosts`. Eight names, no wildcards, no daemon:

```
127.0.0.1   shoebox.lan
127.0.0.1   backend.shoebox.lan
127.0.0.1   deepface.shoebox.lan
127.0.0.1   vision.shoebox.lan
127.0.0.1   geoloc.shoebox.lan
127.0.0.1   albums.shoebox.lan
127.0.0.1   gallery.shoebox.lan
127.0.0.1   ollama.shoebox.lan
```

Ship this as `systems/gateway/hosts-entries.txt` plus a `make-hosts.sh` that prints them, so it is
copy-pasteable and version-controlled rather than folklore.

`/etc/hosts` has no wildcard support, so a new service means a new line. With eight services that
is acceptable; if the count grows, move to the dnsmasq option below.

> **As of v6.4.4:** `hosts-entries.txt` lists ten `*.airlock.lan` names, `transcode` and `pets`
> included.

### 6.2 Other devices on the LAN (phones, laptops)

`/etc/hosts` does not help them. This is where the draft's DNS container earns its place:

- Add **dnsmasq** to `systems/gateway/` (a second container in the same pod) with
  `address=/shoebox.lan/<mac's LAN IP>` — a single wildcard rule covering every present and future
  subdomain.
- Point the router's DHCP at it, or set the DNS server manually per device.
- The gateway's `server_name` blocks work unchanged; only the A record changes from `127.0.0.1` to
  the LAN IP.

Treat this as **Phase 4**, optional. It is genuinely useful (it is what makes the UI reachable from
a phone without rebuilding anything) but it is not needed to remove the ports.

### 6.3 Containers, inside the podman VM

Containers do **not** read the Mac's `/etc/hosts`. If a container needs to call
`backend.shoebox.lan`, give it the mapping explicitly in its pod manifest — the pattern
`backend-albums-pod.yaml:22-25` already uses:

```yaml
  hostAliases:
    - ip: "host-gateway"
      hostnames:
        - "shoebox.lan"
        - "backend.shoebox.lan"
        - "deepface.shoebox.lan"
```

`host-gateway` resolves to the Mac, where the gateway is published. So a container calling
`http://deepface.shoebox.lan/api/embed` goes container → host:80 → gateway → host:5001. One extra
hop versus today's direct `host.containers.internal:5001`, on a local socket, for calls that already
carry image payloads — measure it, but expect it to be lost in the noise.

### 6.4 The deepface wrinkle

`backend-deepface` runs **on the host**, not in a pod, because Metal has no passthrough into the
podman VM (`CONTAINERIZATION_AUDIT.md`, "Why containerizing it can never use the GPU"). So:

- `deepface.shoebox.lan` must proxy to `host.containers.internal:5001`, not to a container.
- `start-host.sh` binds `0.0.0.0` specifically so containers can reach it; that must not change.
- The host process itself is not behind the gateway, so a direct `localhost:5001` still works and
  should keep working — it is what `start-host.sh --check` and `bench_ingest.py` use.

> **Since v4.9.0:** this applies only when deepface runs native (macOS on Apple Silicon by
> default). On Linux and Intel Macs it runs in a container that publishes :5001, and the gateway
> reaches it the same way, through the host (`airlock-host:5001`).

---

## 7. Change inventory

Every site below was located by inspection; file and line are as of this writing.

### 7.1 Service-to-service defaults (Python)

Change the fallback, keep the env var. The env var is how the gateway address gets injected in a
container; the fallback is what a host-side developer run uses.

| File | Line | Now | After |
| --- | --- | --- | --- |
| `backend/app.py` | 3175 | `DEEPFACE_URL`, default `http://localhost:5001` | default `http://deepface.shoebox.lan` |
| `backend/app.py` | 4078 | same | same |
| `backend/app.py` | **5523** | `ALBUMS_BACKEND_URL = 'http://localhost:5005'` | **add `ALBUMS_URL` env**, default `http://albums.shoebox.lan` |
| `backend/workflows/ai_face_recognition_workflow.py` | 19 | `DEEPFACE_URL` default | new default |
| `backend/workflows/ai_ml_faces_recognition_workflow.py` | 28 | same | same |
| `backend/workflows/ai_auto_detect_faces_workflow.py` | 666, 834 | same | same |
| `backend/workflows/ai_auto_detect_faces_workflow.py` | **848** | error text naming `http://host.containers.internal:5001` | update the message |
| `backend/workflows/ai_ingest_manual_faces.py` | 13 | same | same |
| `backend/workflows/ev_extract_events.py` | 144 | `BACKEND_URL`, default `http://localhost:5000` | default `http://backend.shoebox.lan` |
| `backend/migrate_add_face_embedding.py` | **29** | hardcoded `http://localhost:5001/api/embed` | **add env override** |
| `backend-vision/app.py` | 28 | `OLLAMA_HOST` default | `http://ollama.shoebox.lan` (or leave; see §3) |
| `backend-albums/app.py` | — | implicit `OLLAMA_HOST` via the ollama library | set it explicitly, as vision does |

The three in bold have **no environment override today** and will be silently missed by a
config-only migration.

`BACKEND_URL` (`ev_extract_events.py:144`) is read but never set anywhere — the backend calls itself
over HTTP for `/api/user/location`. Worth noting: that call does not need the network at all and
could be an in-process function call, which would delete the dependency rather than rename it.

### 7.2 Frontend

- `src/config.js:30,33,36` — defaults become `''`, `/vision`, `/geoloc` (§5).
- `src/views/UserSettings.js:46` — `process.env.REACT_APP_API_URL || SOCKET_URL`. This invents a
  **fourth** addressing variable that exists nowhere else — not in `config.js`, not in the
  Dockerfile. Delete it and import from `config.js` like every other view.
- `src/views/Photos.js:1031` and `src/views/EventDetail.js:1167` — user-facing alert text says
  "running on port 5003". Update to name the service, not the port.
- `src/config.js:45` — `PEOPLE_PHOTOS_BASE_URL` is exported and never imported; five call sites
  rebuild it inline. Either use it or delete it, while the file is open.
- `frontend/Dockerfile:22-25` — the three `REACT_APP_*` build args. With §5 they become
  vestigial; keep them as an escape hatch, but change the defaults.

No other file under `src/` contains a literal host or port — the `config.js` work already done
holds up, and that is why this migration is tractable.

### 7.3 Manifests and scripts

- Seven `*-pod.yaml`: keep `containerPort`, **drop `hostPort`** for everything except the gateway.
  Services stop being individually reachable from the host, which is the point. Keep `hostPort` on
  the frontend during Phase 1 so there is a way back.
- Seven `start-podman.sh`: drop the `-p <port>:<port>` publish flags to match.
- `backend/backend-pod.yaml:45-46` and `backend/start-podman.sh:37` — `DEEPFACE_URL` becomes
  `http://deepface.shoebox.lan`.
- `backend-vision-pod.yaml:48-49`, `backend-albums-pod.yaml:41-42` — `OLLAMA_HOST` likewise.
- Every pod that calls another service gains a `hostAliases` block (§6.3).
- `start-all.sh` — the health gates at lines 58, 69, 78, 92, 93, 95, 96, 97 and the summary at
  101-103 all use `localhost:PORT`. This script is effectively the port-addressing map of the
  system; it must be rewritten against names, and it should gate on the **gateway** being up first.
- `stop-all.sh:39` — `pkill -f "gunicorn.*app:app.*5001"` matches deepface by its bind string. It
  still works (deepface keeps its port behind the gateway) but leave a comment saying why, or the
  next person "cleans it up" and breaks shutdown.

### 7.4 Tests and benches

`backend/test_workflow_execution.py:89,104`, `documentation/tests/test_face_comparison.py:18`,
`documentation/tests/test_year_filter.py:33`, `backend-deepface/bench_ingest.py:159`. All should
take a `--url` or read an env var rather than hardcoding.

### 7.5 Documentation

~40 stale `localhost:PORT` references, notably `documentation/README.md:92-96`,
`guides/STARTUP_GUIDE.md:44-48`, `guides/QUICKSTART.md:24-25,96-101`. Update in the same phase as
the code, or they become the most confidently wrong thing in the repo.

### 7.6 Nothing in the database

Verified by scanning every `.db` under `systems/` and `catalogs/`: **no service address is persisted
anywhere.** `catalog_meta` has `scp_host/scp_port/ftp_host/ftp_port`, but those are file-transfer
endpoints for a feature unrelated to service addressing — out of scope. The only stored string
containing a service URL is a historical `workflow_executions.error_message`.

This is the good news of the whole migration: it is entirely code and config, with no data to
migrate and no rollback risk in the data layer.

---

## 8. CORS

Every Flask backend currently uses blanket `CORS(app)` — wildcard, no allowlist — and
`socketio = SocketIO(app, cors_allowed_origins="*")`.

Under §5 the browser talks to a single origin, so CORS stops being load-bearing for the app. Two
consequences:

1. **Do not tighten CORS in the same phase as the move.** If something breaks, you want one variable
   changed, not two. Tighten it afterwards, as its own change, once same-origin is confirmed working.
2. When you do tighten it, `cors_allowed_origins` should list `http://shoebox.lan` and whatever LAN
   address is in use — not `*`.

> **Done for the backend:** it accepts only `AIRLOCK_ALLOWED_ORIGINS` (default `airlock.lan`,
> the `shoebox.lan` origins until the rename's fallbacks go, and `localhost:3000/5000`), for both
> Flask-CORS and Socket.IO (`systems/backend/app.py:69-76,117`); `deploy/airlock-env.sh` adds the
> machine's own address. The other services still use blanket `CORS(app)`.

---

## 9. Phases

### Phase 1 — Gateway alongside the existing ports
- [x] `systems/gateway/` — `Dockerfile`, `nginx.conf`, `proxy.conf`, `gateway-pod.yaml`,
      `start-podman.sh` (with the rebuild-on-stale guard)
- [x] Server blocks for all eight names, plus the `shoebox.lan` path routes
- [x] `hosts-entries.txt` + `make-hosts.sh`
- [x] Ports stay published; nothing else changes

**Acceptance:** `curl -H 'Host: backend.shoebox.lan' http://localhost/api/catalogs` returns the same
JSON as `curl http://localhost:5000/api/catalogs`. Both paths work simultaneously.

### Phase 2 — Frontend to same-origin
This is the decided design (section 5), not one of two options.
- [x] `config.js` defaults → `''`, `/vision`, `/geoloc`
- [x] `UserSettings.js:46` uses `config.js`
- [x] Port numbers removed from the two alert strings
- [x] Frontend rebuilt; UI opened at `http://shoebox.lan`

**Acceptance:** every page loads, images render, and — the one that fails silently — a running
workflow's progress bar advances in the queue panel.

### Phase 3 — Service-to-service, then close the ports
- [x] All defaults and env values in §7.1 point at names
- [x] `hostAliases` in every pod that calls another service
- [x] `ALBUMS_URL` and the deepface embed migration script gain env overrides
- [x] `start-all.sh` / `stop-all.sh` rewritten against names
- [ ] ~~Drop `hostPort` and `-p` flags for everything but the gateway~~ — **not done, see 9.1**
- [x] Docs updated

**Acceptance:** with only port 80 published, the full pipeline runs end to end — import, detect,
recognise, contextual — and `podman ps` shows no other published port.

### 9.1 Deviation: the service hostPorts stayed

The plan above said to drop `hostPort` from every pod but the gateway. **That was wrong, and doing
it would have broken the gateway.**

The gateway runs in its own pod. It reaches the other services through
`host.containers.internal:<port>` — which only works because those ports are published on the host.
Remove the publishing and the gateway has no route to anything: separate pods share no network
namespace, and there is no container DNS between them.

So every service still publishes its port, and what actually changed is who knows about it:

| | before | after |
| --- | --- | --- |
| Frontend bundle | 3 absolute `localhost:PORT` URLs compiled in | relative paths only |
| Backend + workflows | 9 `localhost:5001` defaults, 3 unoverridable literals | names, all env-overridable |
| Pod manifests | each service's own port, plus cross-service ports | each service's own port only |
| Gateway config | — | the single place a name maps to a port |
| `start-all.sh` | 8 `localhost:PORT` health checks | names, through the gateway |

That is the outcome that matters: **no application code refers to a service port.** The ports are an
implementation detail of the deployment, described in exactly one file.

Closing them entirely is a real improvement and a real project: it needs every service in **one pod**
with a shared network namespace, so the gateway can reach them at `localhost:<port>` while only :80
is published. That is a restructure of all seven manifests plus the volume and start-script layout,
with its own failure modes. It is worth doing deliberately rather than as a footnote to this
migration.

### Phase 4 — LAN DNS (optional)
- [ ] dnsmasq container in the gateway pod, `address=/shoebox.lan/<LAN IP>`
- [ ] Router or per-device DNS pointed at it

**Acceptance:** the UI loads on a phone at `http://shoebox.lan` with no rebuild and no `/etc/hosts`
edit on that device.

> **Still open at v6.4.4:** there is no dnsmasq in the repository.

### Phase 5 — One pod, ports closed (not started)
- [ ] Merge the seven pod manifests into one with a shared network namespace
- [ ] Gateway upstreams become `localhost:<port>`
- [ ] Drop every `hostPort` and `-p` flag except the gateway's :80
- [ ] Rework `start-all.sh` / `stop-all.sh` around a single pod lifecycle

**Acceptance:** `podman ps` shows exactly one published port, and the full pipeline still runs.

> **Still not started at v6.4.4:** each service still has its own `*-pod.yaml` with a `hostPort`.

---

## 10. Risks

| Risk | Mitigation |
| --- | --- |
| **Socket.IO breaks silently** — progress bars stop moving, nothing errors | §4.3 config; acceptance test is explicitly behavioural |
| **1 MB upload limit** — nginx default rejects image uploads | `client_max_body_size 100M` |
| **60 s proxy timeout** — vision (600 s) and long workflow calls cut off | `proxy_read_timeout 600s` |
| **Port 80 unavailable** to rootless podman on macOS | Verify first; fall back to 8080 uniformly |
| **Stale frontend bundle** — the address is compiled in | §5 removes the address from the bundle entirely |
| **Containers cannot resolve the names** — they do not read the Mac's `/etc/hosts` | `hostAliases` per pod (§6.3) |
| **deepface is on the host** | Gateway proxies to `host.containers.internal:5001`; direct access preserved |
| **`/etc/hosts` drift across machines** | Version-controlled `hosts-entries.txt`; dnsmasq in Phase 4 |
| **Extra hop on image-carrying calls** | Local socket; measure with `bench_ingest.py` before and after |
| **Closing ports removes the debugging path** | Keep every service's `containerPort` and document `podman exec` / temporary `-p` for diagnosis |

---

## 11. Out of scope

- **TLS.** LAN-only, no certificate authority, and `http://` keeps the browser from complaining
  about a self-signed cert. Add later if the deployment ever leaves the LAN.
  *Since v4.1.0: the gateway terminates TLS with a certificate from a private CA
  (`systems/gateway/make-certs.sh`); see `user-authentication-spec.md`.*
- **Authentication at the gateway.** Nothing authenticates today; adding it here would be a
  different project. *Since v4.1.0 the backend authenticates (`systems/backend/auth.py`); the
  gateway itself still does not.*
- **Collapsing services.** `documentation/features/combining-backends.md` proposes merging backend
  and deepface. That would reduce the number of names, but it is a separate decision and this
  migration does not depend on it either way.
- **`scp_host` / `ftp_host` in `catalog_meta`.** File-transfer destinations, not service addressing.
