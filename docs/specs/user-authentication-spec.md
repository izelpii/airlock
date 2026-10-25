# User Authentication and Catalog Ownership — Implementation Specification

**Status:** implemented in v4.1.0 (2026-09-04, `systems/backend/auth.py`, `manage_users.py`,
`migrate_add_users.py`, TLS in `systems/gateway/`); the sharing UI (`CatalogSharePanel.js`) is in
v4.2.0. Phases 0-4 of §11 are done; phase 5 (albums) took the form of optional per-deployment
passwords (§9 note); phase 6 (public domain) has not happened. Not built: per-catalog Socket.IO
rooms (§6.4) and `started_by_user_id` (§6.5).
**Supersedes:** the original brief of this file (preserved as `user-authentication-spec-orig.md`)
**Related:**
`documentation/features/SYSTEMS_CONTROL_PANEL_ANALYSIS.md`,
`docs/specs/redesign-ports-routes-spec.md`,
`documentation/specs/album-deployment-spec.md`,
`documentation/features/CONTAINERIZATION_TRADEOFFS.md`

> **What changed afterwards (as of v6.4.4, 2026-10-25):**
> - **Names** (v5.6.x rename): the cookie is `airlock_session` (the old `shoebox_session` was
>   accepted and swapped until v6.4.5, and no longer is), the settings are `AIRLOCK_SECRET_KEY` and
>   `AIRLOCK_AUTH_REQUIRED` (default on, `app.py:136`), the domain is `airlock.lan`, the gateway
>   config is `airlock.conf.template`. This spec keeps the names it was written with.
> - **More owner-only operations** than §4.2 lists (`auth.py:555-586`): deployment passwords,
>   purging removed files, publishing to a server (`/servers`, `/publish-runs`, reads included),
>   and changing, running or restoring backups (viewers see backup cards).
> - **Each account has its own page** (v5.13.0), `/users/<id>`, in place of §8.3's tabs; an
>   administrator can set another account's password and see or sign out its devices
>   (`app.py:17622-17790`).

---

## 1. Purpose

Shoebox currently has no authentication of any kind. `app.py` registers 115 routes; none of them
check who is calling. The app binds `0.0.0.0`, `CORS(app)` is registered with no origin restriction,
and the gateway publishes everything on port 80 under `shoebox.lan`. Anyone who can reach the host —
any device on the LAN, and any web page open in a browser on that LAN via CORS — can read, modify
and delete the entire photo library.

This specification adds password authentication, makes a catalog belong to a user, and lets an owner
share a catalog with other users.

It also fixes two authorization holes that authentication alone would not close (§6.3), because
adding users without fixing them produces a system that *looks* multi-user but is not.

### Success criteria

1. An unauthenticated request to any `/api/**` route except the auth endpoints returns `401` and no
   data.
2. A user sees only the catalogs they own or have been granted access to.
3. An owner can grant and revoke another user's access to a catalog, at a chosen role.
4. A user can change their own password, and revoking a session takes effect immediately.
5. Existing data keeps working: the current catalog and its 902-photo library remain accessible to
   the migrated account, with no re-import.
6. Service-to-service traffic and workflow subprocesses continue to function unchanged.

### Explicit non-goal

This does **not** make Shoebox safe to expose to the internet. See §3.

---

## 2. What exists today

The system is unusually well shaped for this change. These are the seams to build on rather than
around.

**Ownership already exists as a string.** `catalog_meta.owner` is `TEXT NOT NULL`, currently
`'Izelpi'` for both catalogs. `catalog.py` already has an unused-but-correct accessor:

```python
Catalog.get_by_owner(owner)   # SELECT * FROM catalog_meta WHERE owner = ?
```

**There is already an implicit user.** `user_settings` holds exactly one row — `Izelpi`,
`owner@example.com`, their home town plus a home lat/long used for geolocation defaults. Its
`name` matches `catalog_meta.owner` exactly. That row is the account to migrate.

**Authorization has one natural choke point.** Of 115 routes, **97 are `/api/catalogs/<int:catalog_id>/…`**.
A single decorator on that path parameter covers 84% of the surface. The remaining 18:

```
/api/catalogs                      /api/catalogs/files
/api/people/photos/<path>          /api/user/settings        /api/user/location
/api/workflows/scripts             /api/systems/
```

**The frontend has one HTTP client.** `services/api.js` creates a single axios instance, so request
and response interceptors are single-point changes.

**Nothing calls back in.** Workflows call *out* to `deepface`/`vision`/`albums`; no service calls the
main backend's API. Adding auth therefore cannot break service-to-service traffic — a rare piece of
good luck worth stating explicitly, because it removes the usual need for service accounts.

**Password hashing needs no new dependency.** Werkzeug 3.0.1 is already installed with Flask and
defaults to scrypt:

```
scrypt:32768:8:1$LPGa8UGw8wHMe6mi$321b03…
```

---

## 3. Threat model

State plainly what this does and does not buy, because the deployment is plaintext HTTP on a LAN.

**In scope.** Casual access by other people and devices on the home network; accidental destructive
calls from a stale browser tab or a script; separating two family members' catalogs so neither can
edit the other's; and — the reason this is now urgent — making it possible to *later* add privileged
operations such as the Systems control panel (`SYSTEMS_CONTROL_PANEL_ANALYSIS.md`), which would
otherwise hand container control to any unauthenticated caller.

**Not in scope, and must not be claimed.**

- **Plaintext until §10 ships.** The gateway listens on `:80` today, so passwords and session
  cookies cross the LAN in cleartext until TLS is in place. TLS is specified in §10 and is a
  prerequisite for the `Secure` cookie (§5.4); order the phases so it lands with or before
  enforcement. Until then, a privately signed certificate protects against passive sniffing on the
  LAN but not against an attacker who can make devices trust their own CA.
- **The frontend bundle is not a secret.** The brief says "nothing in the UI will be displayed until
  the user can authenticate". That is achievable for *data*, not for *code*: nginx serves
  `index.html` and the compiled JavaScript before any API call happens, and CRA inlines
  `REACT_APP_*` at build time. The gate is a client-side redirect for usability; the enforcement is
  the API returning 401. Do not treat the bundle as protected, and never compile a secret into it.
- **Deployed albums remain public.** See §9.
- **Arbitrary file read.** See §6.3 — a pre-existing issue this spec closes as part of the work.

---

## 4. Data model

All three tables live in the application database (`systems/backend/database/catalogs.db`), beside
`catalog_meta` — not in a per-catalog `workflow_results.db`, since identity spans catalogs.

### 4.1 `users`

```sql
CREATE TABLE users (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    username       TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email          TEXT UNIQUE COLLATE NOCASE,
    display_name   TEXT NOT NULL,
    password_hash  TEXT NOT NULL,
    is_active      INTEGER NOT NULL DEFAULT 1,
    is_admin       INTEGER NOT NULL DEFAULT 0,
    created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at  TIMESTAMP
);
CREATE INDEX idx_users_username ON users(username);
```

`COLLATE NOCASE` on `username` and `email` prevents `izelpi` and `Izelpi` becoming two accounts.
`is_admin` gates installation-wide operations (user management, and later the Systems panel);
it is not a catalog role.

### 4.2 `catalog_access`

```sql
CREATE TABLE catalog_access (
    catalog_id  INTEGER NOT NULL REFERENCES catalog_meta(id) ON DELETE CASCADE,
    user_id     INTEGER NOT NULL REFERENCES users(id)        ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('owner','editor','viewer')),
    granted_by  INTEGER REFERENCES users(id),
    granted_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (catalog_id, user_id)
);
CREATE INDEX idx_catalog_access_user ON catalog_access(user_id);
```

One table expresses both ownership and sharing, which is what the brief asks for ("multiple users can
own a Catalog"). Roles:

| Role | Read | Edit photos, people, events, albums | Run workflows | Share / rename / delete catalog |
| --- | --- | --- | --- | --- |
| `viewer` | yes | no | no | no |
| `editor` | yes | yes | yes | no |
| `owner` | yes | yes | yes | yes |

Invariant: **every catalog has at least one `owner`.** Revoking or deleting the last owner must be
refused, or the catalog becomes unadministrable.

Workflows are `editor` because they mutate catalog data. `viewer` is what a shared family member
gets.

> **As implemented** (`systems/backend/auth.py:555-586`): the role is derived from the URL rule and
> method, not from a decorator per route. `owner` is needed for `/access`, a deployment's
> `/password`, `/removed-files/purge`, `/servers` and `/publish-runs` (all methods), for any
> non-read on `/backups`, and for `PUT`/`DELETE` on the catalog itself. Other reads are `viewer`,
> other writes `editor`.

### 4.3 `sessions`

```sql
CREATE TABLE sessions (
    id            TEXT PRIMARY KEY,            -- secrets.token_urlsafe(32)
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_seen_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    expires_at    TIMESTAMP NOT NULL,
    user_agent    TEXT,
    ip            TEXT
);
CREATE INDEX idx_sessions_user ON sessions(user_id);
```

**Why server-side sessions rather than Flask's signed-cookie session or a JWT.** Success criterion 4
requires that revoking access takes effect immediately. A signed cookie and a JWT are both
self-contained: once issued they remain valid until expiry, so "log out everywhere", "disable this
account" and "revoke this share" cannot be honoured without a server-side denylist — which is the
table above, arrived at the long way round. A row per session also gives the settings page something
truthful to show ("3 active sessions") and costs one indexed primary-key lookup per request against
a SQLite database the process already has open.

Lifetime: 30 days absolute (`expires_at`), refreshed on use by updating `last_seen_at`. Expired rows
are deleted lazily on lookup and by a periodic sweep in the existing scheduler thread.

### 4.4 Migration

A single migration script, `migrate_add_users.py`, following the conventions in
`systems/backend/migration_utils.py`:

1. Create the three tables.
2. Create one user from the existing `user_settings` row: `display_name` and `email` from that row,
   `username` derived from the email local part (`owner` → prompt or `--username`),
   `is_admin = 1`.
3. Password: no plaintext exists to migrate. Take `--password` on the command line, or generate one
   and print it once, and set a `must_change_password` flag (add to `users`) so the first login
   forces a change.
4. For every distinct `catalog_meta.owner`, match to a user by `display_name`; insert
   `catalog_access(catalog_id, user_id, 'owner')`. Unmatched owner strings are reported and assigned
   to the admin, never silently dropped.
5. Leave `catalog_meta.owner` in place. It becomes a display fallback, and keeping it makes the
   migration reversible. Mark it deprecated in `catalog.py`; remove it in a later change once
   `catalog_access` is proven.

The script must be idempotent and must report what it did per catalog — the failure mode documented
in `migration_utils.py` (a migration that appears to succeed and did nothing) is the one to avoid.

**`user_settings` is not the users table.** It currently holds *installation* settings that happen to
be named after a person — notably the home lat/long used for geolocation defaults. Fold the identity
fields (`name`, `email`) into `users` and keep the rest as per-user preferences by adding
`user_id INTEGER REFERENCES users(id)`, backfilled to the migrated account. Do not delete the table;
`/api/user/settings` and `/api/user/location` are live endpoints and the location fields are used by
geolocation code.

---

## 5. Authentication

### 5.1 Cookie sessions, not bearer tokens

**This is the decisive design choice and it is forced by the image loading.** Nine view files build
raw image URLs consumed by `<img src>`:

```js
`${API_HOST}/api/catalogs/files?path=…`
`${API_HOST}/api/catalogs/${catalogId}/photos/${fileId}/thumbnail?size=320`
`${PEOPLE_PHOTOS_BASE_URL}${photo_path}`
```

An `<img>` tag cannot set an `Authorization` header. With bearer tokens every image would need to be
fetched by JavaScript into a blob URL, or have a token appended to the query string where it lands in
logs and browser history. A cookie is sent automatically by the browser for `<img>`, `fetch` and
Socket.IO alike, and the gateway already puts UI and API on one origin
(`redesign-ports-routes-spec.md` §5), so no cross-origin cookie problem exists in the deployed
configuration.

### 5.2 Password hashing

`werkzeug.security.generate_password_hash` / `check_password_hash`, default scrypt parameters
(`scrypt:32768:8:1`). No new dependency. Never log or return a hash. Minimum length 12 characters,
checked server-side; no composition rules.

### 5.3 `SECRET_KEY`

**There is currently no `SECRET_KEY` anywhere in `app.py`.** One is required, and it must be stable
across restarts or every session is invalidated on each deploy — which, given how often the backend
image is rebuilt, would be indistinguishable from a broken login.

Resolution order: `SHOEBOX_SECRET_KEY` from the environment; otherwise a key generated once and
stored at `database/secret_key` with mode `0600`.
*(Since v5.6.3 the variable is `AIRLOCK_SECRET_KEY`, `auth.py:76`.)* That directory is already a persistent volume
(`VOLUME ["/app/database"]`), so the key survives image rebuilds — the same property that makes
`catalogs.db` survive them.

The key is used for CSRF tokens and cookie signing. It must never be baked into an image or a
`REACT_APP_*` variable.

### 5.4 Cookie attributes

```
Set-Cookie: shoebox_session=<id>; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000
```

> **Since v5.6.5** the cookie is `airlock_session` (`systems/backend/auth.py`). The old
> `shoebox_session` was accepted and swapped for the new name until v6.4.5; it no longer signs in.

- `HttpOnly` — not readable by JavaScript, so an XSS bug cannot exfiltrate the session.
- `SameSite=Lax` — blocks cross-site POSTs, which is the practical CSRF defence here.
- **`Secure` follows TLS (§10), and only then.** Set it from `X-Forwarded-Proto`, which the gateway
  already sends and which becomes `https` automatically once nginx terminates TLS (§10.2). Do not
  set it while serving over HTTP: a `Secure` cookie is simply never sent back, which presents as
  "login succeeds, then every request 401s".

`CORS(app)` must become explicit. Wildcard origins are incompatible with credentialed requests, so:

```python
CORS(app, supports_credentials=True, origins=[
    "http://shoebox.lan", "http://localhost:3000",   # dev server
])
```

The dev-server origin matters: `npm start` runs the UI on `:3000` against the backend on `:5000`,
which is cross-origin, and axios needs `withCredentials: true` for the cookie to travel.

> **As implemented:** the list is `AIRLOCK_ALLOWED_ORIGINS` (default `https://airlock.lan`, the old
> `shoebox.lan` origins, `localhost:3000` and `:5000`), and a request whose `Origin` matches the
> host it arrived on is accepted too; Socket.IO uses the same check
> (`systems/backend/app.py:69-118`).

### 5.5 Brute force

Rate-limit `POST /api/auth/login` per username and per source IP: 10 attempts per 15 minutes, then a
60-second lockout that doubles up to 15 minutes. Counters live in memory (a dict guarded by a lock);
losing them on restart is acceptable at this scale and avoids a new dependency. Login responses must
not distinguish "no such user" from "wrong password", and must take comparable time in both cases —
run the hash check against a dummy hash when the user does not exist.

---

## 6. Authorization

### 6.1 The catalog choke point

Two decorators, applied to every route:

```python
@login_required                      # 401 when no valid session
@catalog_access(minimum='editor')    # 403 when the user's role is insufficient
def some_route(catalog_id): ...
```

`@catalog_access` reads the `catalog_id` view argument, looks up `catalog_access` for
`g.current_user.id`, and aborts before the handler runs. Because 97 routes already carry
`catalog_id`, this is close to mechanical.

**Fail closed.** The default for any route not explicitly marked public must be "authentication
required". Implement with a `before_request` hook that rejects anything not on an explicit allowlist
(`/api/auth/login`, `/api/auth/session`, the first-run setup endpoints, and `/gateway-health`), rather
than by remembering to decorate 115 handlers. A missed decorator then fails as an unexpected 401,
not as a silent hole.

`GET /api/catalogs` must return only catalogs the user can reach — a join against `catalog_access`,
replacing today's `Catalog.get_all()`. This is what makes the UI appear correctly scoped, since
`CatalogContext` drives the whole app from that list.

### 6.2 Roles

Enforce with `minimum=` on the decorator per the table in §4.2. Mutating routes take
`minimum='editor'`; catalog rename/delete/share take `minimum='owner'`; reads take `'viewer'`.

### 6.3 The two path-based endpoints — must be fixed

These are the holes that authentication alone does not close, and they are the reason this section
exists.

```
GET /api/catalogs/files?path=<any path>
GET /api/people/photos/<any path>
```

Both accept an **absolute path** and serve whatever is there; when the path is relative they search
**every catalog directory** until one matches. Neither performs any containment check. Today, with
one user, this is a latent arbitrary-file-read against the backend container's filesystem. The moment
catalogs belong to different people it also becomes a cross-tenant read: user B can fetch user A's
photos by guessing or enumerating paths, and `catalog_access` never sees the request because there is
no `catalog_id` in it.

Required changes:

1. **Resolve and contain.** `os.path.realpath()` the candidate, and require it to be under the
   `realpath` of a catalog directory the current user may read. Reject absolute paths outside every
   permitted root. `realpath` matters because the deployed albums are built from symlinks
   (`_deploy_album`) and a symlink is otherwise a containment bypass.
2. **Stop searching all catalogs.** Resolve relative paths only against catalogs the user can reach.
3. **Prefer identifiers over paths.** New UI code should use
   `/api/catalogs/<catalog_id>/photos/<file_id>/thumbnail`, which is naturally scoped. Treat the
   path-based endpoints as legacy and migrate remaining callers; the recent gallery work already
   moved three views onto the id-based thumbnail route.

### 6.4 Socket.IO

`socketio` is registered with `cors_allowed_origins="*"`. Workflow progress events currently broadcast
to everyone connected. With multiple users this leaks activity across accounts.

- Authenticate the handshake in `@socketio.on('connect')` by reading the same session cookie; reject
  the connection when it is absent or invalid.
- Restrict origins to the same list as §5.4.
- Emit progress into a per-catalog room (`catalog:<id>`) and join a socket only to rooms its user may
  read, replacing today's global broadcast.

> **As of v6.4.4:** the handshake is authenticated (`app.py:18202-18222`) and origins are
> restricted, but per-catalog rooms were **not** built: `workflow_progress` is still emitted to
> every signed-in connection.

### 6.5 Workflow subprocesses

Workflows run detached via `subprocess.Popen` and write to the catalog database directly; they do not
call the API, so they need no credentials. Record provenance instead: add `started_by_user_id` to the
workflow execution row so the UI can show who ran what. A scheduled workflow with no interactive user
records `NULL` and runs as the system.

> **Not built as of v6.4.4:** workflow executions have no `started_by_user_id`. Only publish runs
> record who started them (`systems/backend/publish.py:94`).

---

## 7. API

| Method | Path | Auth | Purpose |
| --- | --- | --- | --- |
| `POST` | `/api/auth/login` | public | `{username, password}` → sets cookie, returns user |
| `POST` | `/api/auth/logout` | session | Deletes the current session row |
| `POST` | `/api/auth/logout-all` | session | Deletes all sessions for the user |
| `GET` | `/api/auth/session` | public | Current user, or `401` — the UI's boot probe |
| `POST` | `/api/auth/password` | session | `{current, new}`, rotates all other sessions |
| `GET` | `/api/auth/setup-required` | public | `true` only while `users` is empty |
| `POST` | `/api/auth/setup` | first-run only | Creates the first admin; refuses once any user exists |
| `GET` | `/api/users` | admin | List accounts |
| `POST` | `/api/users` | admin | Create account |
| `PATCH` | `/api/users/<id>` | admin or self | Update profile; only admin may set `is_admin`/`is_active` |
| `DELETE` | `/api/users/<id>` | admin | Refused if it would orphan a catalog (§4.2) |
| `GET` | `/api/catalogs/<id>/access` | owner | Who can reach this catalog |
| `POST` | `/api/catalogs/<id>/access` | owner | `{user_id, role}` — grant or change |
| `DELETE` | `/api/catalogs/<id>/access/<user_id>` | owner | Revoke; refused for the last owner |

**Error contract.** `401` means *not authenticated* — the UI redirects to login. `403` means
*authenticated but not permitted* — the UI shows a message and does **not** redirect, since bouncing
an authenticated user to a login page they have already passed is a confusing loop. Bodies are
`{"error": "...", "code": "unauthenticated" | "forbidden"}`.

**First-run.** `/api/auth/setup` must hard-refuse once any user row exists, checked inside the same
transaction that inserts, so it cannot be raced into creating a second admin.

> **Added since:** `GET /api/auth/sessions`; and, for v5.13.0's account pages,
> `/api/users/<id>/profile` (GET, PUT), `/api/users/<id>/password` (POST: an administrator sets a
> temporary password, which signs that account out everywhere), `/api/users/<id>/sessions` (GET,
> DELETE), `/api/users/<id>/person` and `/api/users/<id>/avatar` (`systems/backend/app.py:17378,
> 17622-17790, 18084, 18113`).

---

## 8. UI

### 8.1 The gate

`App.js` currently renders the sidebar, top menu and routes unconditionally. Wrap it:

```
<AuthProvider>          // calls GET /api/auth/session once on mount
  loading  → full-page spinner (render nothing else)
  401      → <Login/>            (or <FirstRunSetup/> when setup-required)
  ok       → the existing app shell
</AuthProvider>
```

This satisfies "nothing in the UI will be displayed until the user can authenticate" as far as a
browser application can — with the §3 caveat that the bundle itself is still served.

### 8.2 Login page

Username, password, submit, one inline error that does not distinguish unknown user from bad
password. No registration link: accounts are created by an admin. Forced password change on first
login when `must_change_password` is set.

### 8.3 Settings page

`/settings` already routes to `UserSettings.js`, which is where the brief asks for this. Add tabs:

- **Profile** — display name, email, and the existing location fields.
- **Password** — current, new, confirm.
- **Sessions** — active sessions with user agent, IP and last seen; "sign out everywhere".
- **Sharing** — for each catalog the user owns, who has access and at what role; grant and revoke.
- **Users** — admin only: create, deactivate, promote.

> **Since v5.13.0** there are no tabs: each account has its own page, `/users/<id>` (User
> Settings, Linked person, Password, Signed-in devices), opened from your name in the top bar or
> from the pencil on a card in Admin → Users; the Administrator and Active switches stay on the
> card. Sharing a catalog is on the Catalogs page (`CatalogSharePanel.js` in `CatalogList.js`). The
> gear is Site settings, `/settings` (`systems/frontend/src/App.js:169-177`).

### 8.4 Interceptors

On the single axios instance:

- Request: `withCredentials = true`.
- Response: on `401`, clear auth state and show the login view — **except** for the boot probe, which
  expects `401` and must not trigger a redirect loop.
- Response: on `403`, surface the message; do not log the user out.

### 8.5 Images

Because auth is a cookie (§5.1), every existing `<img src>` keeps working with no change. A `401` on
an image renders as a broken image rather than a redirect, so the gate at §8.1 must run before any
view mounts.

---

## 9. Albums are a separate, public surface

`album-gallery` runs its own HTTP server (`server.mjs`, `http.createServer`) on port 3001, published
as `gallery.shoebox.lan`, serving files straight from a read-only mount of the catalog cache. **It
never talks to the backend.** Nothing in this specification protects it: after login is added, a
deployed album remains readable by anyone who can reach the gateway.

That may well be intended — sharing an album with family is the point of deploying one — but it must
be a decision, not an accident, and it interacts with catalog sharing: revoking a user's
`catalog_access` does **not** revoke their access to an album deployed from that catalog.

Options, to be settled before or alongside implementation:

1. **Public by design.** Document it; treat album URLs as unguessable capabilities and pad the album
   directory names accordingly (`00001` is trivially enumerable today).
2. **Per-album secret link.** A token in the path, checked by `server.mjs`. Cheap, no session
   plumbing, survives the separate-origin problem.
3. **Route albums through the backend.** Full authorization, at the cost of proxying every image
   through Flask — the re-encode cost measured in the gallery-thumbnail work makes this the expensive
   option.

Recommended: (2) for shared albums, with (1) documented as the current state until then.

> **As of v6.4.4:** a deployment can carry a password, set by an owner only and checked by the
> gallery with its own session (`systems/album-gallery/server.mjs:25,393-460`); a deployment
> without one is public to anyone who reaches the gateway. Published albums are served at
> `https://<catalog>.airlock.lan/<album>`, and since v6.2.0 an owner can also publish a deployment
> to a server outside the LAN (`deployment-publish-to-server-spec.md`).

---

## 10. TLS and certificates

TLS is **in scope** and is a prerequisite for §5.4's `Secure` cookie. It is specified so that the
same configuration serves the current private LAN name and a future public domain
(`lozaprada.org`) without redesign — only the certificate source and one variable change.

### 10.1 Two name planes

The single most important structural fact, and what makes the later move cheap: Shoebox already has
two distinct sets of names, and **only one of them faces a browser**.

| Plane | Names | Who resolves them | Needs a public certificate |
| --- | --- | --- | --- |
| **External origin** | `shoebox.lan` today, `lozaprada.org` later | browsers, phones, `/etc/hosts` or real DNS | **yes** |
| **Internal services** | `backend.shoebox.lan`, `deepface.shoebox.lan`, `albums.shoebox.lan`, … | pod `hostAliases`, service-to-service HTTP | no |

The internal plane is plumbing behind the gateway: `app.py` reaches deepface and albums through it
(`DEEPFACE_URL`, `ALBUMS_URL`, both env-overridable), and nothing in a browser ever sees those names.
It stays plaintext HTTP inside the trust boundary and **must not** be renamed when the public domain
arrives. Only the external origin gets a certificate.

**The frontend is already domain-agnostic.** `systems/frontend/src` contains no functional reference
to `shoebox.lan` — only a comment in `config.js`. Every URL is relative by the rule that file
documents, so changing the public domain requires **no frontend rebuild**. This is the payoff from
`redesign-ports-routes-spec.md` §5 and it should not be regressed: never introduce an absolute
`REACT_APP_*` host.

### 10.2 Termination

TLS terminates at the gateway, which is already the only component that knows port numbers. Upstream
hops stay HTTP. `proxy.conf` already sends `X-Forwarded-Proto $scheme`, which becomes `https`
automatically once nginx listens on 443 — so the backend can trust that header to decide whether to
set `Secure` on the session cookie, with no other change.

No other service gains a certificate. One cert, one place to renew.

### 10.3 Certificate providers are pluggable

The gateway reads its certificate from **fixed paths**, and nothing in the nginx configuration knows
how the files got there:

```
/etc/nginx/certs/fullchain.pem
/etc/nginx/certs/privkey.pem
```

mounted from `systems/gateway/certs/` on the host. Two providers write to that directory:

| | Provider A — private CA (now) | Provider B — ACME (public domain) |
| --- | --- | --- |
| Tool | `openssl` (already installed) | certbot / lego / Caddy |
| Covers | `shoebox.lan`, `*.shoebox.lan` | `lozaprada.org` (+ wildcard via DNS-01) |
| Trust | root CA installed per device | trusted by every browser already |
| Renewal | manual, long expiry | automatic |
| Cost | none | a domain, and reachability for validation |

Switching providers replaces the files and reloads nginx. No config change, which is the point.

`make-certs.sh` implements Provider A: a root CA (`shoebox-ca.pem`, 10-year) plus a SAN leaf
(2-year) covering `shoebox.lan` and every service name. The root is installed once per device —
System Settings on macOS, a profile on iOS, Credential Storage on Android — after which browsers show
no warning. The CA private key is the sensitive artifact: `0600`, never in the image, and
`systems/gateway/certs/` must be in `.gitignore`.

**Certificates are not committed to the repository.** They are environment state, like `catalogs.db`.

> **Since v6.4.5** the leaf covers `airlock.lan` and `*.airlock.lan` (no longer `shoebox.lan`), and
> the CA's files are `airlock-ca.*`. A CA still stored under its pre-rename name `shoebox-ca.*` is
> renamed, not replaced: `make-certs.sh` stops and prints the command
> (`systems/gateway/make-certs.sh`).

### 10.4 nginx changes

`shoebox.conf.template` gains a redirect server and TLS on the browser-facing block:

```nginx
server {
    listen 80 default_server;
    server_name _;

    # Must stay HTTP and must not redirect — see §10.5
    location /.well-known/acme-challenge/ { root /var/www/acme; }

    location / { return 301 https://$host$request_uri; }
}

server {
    listen 443 ssl default_server;
    http2 on;
    server_name ${SERVER_DOMAIN} localhost;

    ssl_certificate     /etc/nginx/certs/fullchain.pem;
    ssl_certificate_key /etc/nginx/certs/privkey.pem;
    ssl_protocols       TLSv1.2 TLSv1.3;
    ssl_session_cache   shared:SSL:10m;
    ...existing /api/, /socket.io/, /vision/, /geoloc/, / locations unchanged...
}
```

`SERVER_DOMAIN` joins `UPSTREAM_HOST` in `NGINX_ENVSUBST_FILTER`, so the public domain is one
environment variable at container start rather than an image rebuild. Default `shoebox.lan`.
*(Default `airlock.lan` since v5.6.4, `systems/gateway/start-podman.sh:33`.)*

The internal service-name blocks (`backend.`, `vision.`, `geoloc.`, …) stay on plain `listen 80` per
§10.1.

**Port 443 needs no privilege change.** The podman machine's unprivileged floor is already 80
(`net.ipv4.ip_unprivileged_port_start = 80`), set by `enable-port-80.sh`, and 443 is above it.
`start-podman.sh` adds `-p 443:443` and a read-only mount of the certs directory.

### 10.5 The ACME exception must exist from day one

The `/.well-known/acme-challenge/` location above serves no purpose today and must be added anyway.
When the public domain arrives, HTTP-01 validation requires an unredirected plaintext path on port
80; a blanket `return 301` breaks issuance and renewal in a way that is confusing to diagnose months
later. It costs three lines now.

Wildcard certificates (`*.lozaprada.org`) require DNS-01 instead, which needs API access to the DNS
provider. If the public deployment only ever exposes the apex name — the recommended shape, since the
service names are internal (§10.1) — HTTP-01 suffices and no DNS credentials are needed.

### 10.6 HSTS: not yet

Do **not** send `Strict-Transport-Security` while the certificate is privately signed. HSTS instructs
browsers to refuse plaintext for the domain for the stated duration, and it is not revocable by
changing the server — a mistake pins every device until the max-age expires. Enable it only after the
public domain has a publicly trusted certificate, starting at `max-age=300` and raising it once
renewal is proven.

### 10.7 Moving to the public domain

The migration this design is built for, in full:

1. Point DNS at the host; open 80 and 443.
2. Obtain a certificate with Provider B into `systems/gateway/certs/`.
3. Set `SERVER_DOMAIN=lozaprada.org` and restart the gateway.
4. Add the new origin to the backend's CORS list (§5.4).
5. Enable `Secure` on the session cookie (§5.4) and raise HSTS (§10.6).

No frontend rebuild, no backend image change, no internal service renaming. Everything else in this
specification — sessions, roles, sharing — is unaffected.

The one thing that genuinely changes is the threat model: on a public domain the attacker set is no
longer "people on the home LAN". Login rate limiting (§5.5) becomes load-bearing rather than
prudent, the arbitrary-file-read fix (§6.3) becomes urgent rather than important, and the public
album surface (§9) becomes a deliberate exposure decision rather than a LAN convenience.

---

## 11. Phases

Each phase leaves the system working.

**Phase 0 — TLS (§10).** Independent of everything else and deliverable on its own: gateway on 443,
80 redirecting, private CA, ACME path reserved. Doing it first means the login form in Phase 1 never
posts a password in cleartext, and it is the phase most likely to be wanted regardless of when
authentication lands.

**Phase 1 — Identity, no enforcement.** Tables, migration, `SECRET_KEY`, hashing, login/logout/session
endpoints, `AuthProvider`, login page. Enforcement off behind `SHOEBOX_AUTH_REQUIRED=0`. Verifiable:
log in, see the session, log out — while everything else behaves as today.

**Phase 2 — Enforcement.** `before_request` allowlist, `@login_required`, scoped `GET /api/catalogs`,
axios interceptors, Socket.IO handshake auth. Flip the flag. This is the point of no return: test the
migrated login before deploying.

**Phase 3 — Authorization.** `catalog_access` enforcement with roles, the sharing UI, admin user
management.

**Phase 4 — Close the path holes (§6.3).** Independent of the others and safe to bring forward; worth
doing early because it is a real vulnerability today, before any multi-user concern.

**Phase 5 — Albums decision (§9).**

**Phase 6 — Public domain (§10.7)**, if and when the server moves. Nothing above needs redesign for
it; the checklist in §10.7 is the whole change.

---

## 12. Risks

**Locking yourself out.** The single largest risk, and it is operational rather than technical. The
migration generates a password for an account with `must_change_password`; if that output is lost the
only recovery is a CLI reset. Ship `manage_users.py --reset-password` **in Phase 1**, before
enforcement exists, not after it is needed.

**A missed route.** Mitigated by the allowlist design in §6.1 — fail closed, so an oversight is a
visible 401 rather than an open endpoint. Add a test that asserts every registered rule is either on
the allowlist or decorated.

**Sessions dropped on every rebuild.** Caused by an unstable `SECRET_KEY` or a sessions table in a
non-persistent location. Both live under `database/`, which is a volume; verify after the first
container rebuild, since image/source drift has bitten this project before
(`CONTAINERIZATION_TRADEOFFS.md`).

**Dev-mode CORS.** `npm start` is cross-origin and will fail credentialed requests until §5.4 is
configured. Expect this during Phase 1 and configure it up front.

**A private CA is only as good as its trust distribution.** Until the root is installed on a device,
that device sees a certificate warning — and users trained to click through warnings are worse off
than users on plain HTTP, because the warning stops being informative. Install the root on every
device that will use Shoebox, and keep the CA key at `0600` outside the repository and outside every
image.

**HSTS is not revocable.** Enabling it before a publicly trusted certificate pins every device to
HTTPS for the max-age, with no server-side undo (§10.6).

**Album access outliving catalog access.** §9. A revoked user retains any album URL they already have.

---

## 13. Out of scope

OAuth/SSO; multi-factor; password reset by email (no mail path exists); per-person or per-album
ACLs finer than the catalog; audit logging beyond `started_by_user_id`; rate limiting outside the
login route; and authentication for the Systems control panel, which depends on this work landing
first (`SYSTEMS_CONTROL_PANEL_ANALYSIS.md` §"The security question").
