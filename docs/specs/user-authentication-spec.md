# User Authentication and Catalog Ownership

**Status:** built. Sign-in, accounts and roles arrived in v4.1.0, catalog
sharing in v4.2.0, and account pages in v5.13.0. This document describes the
system as it stands at v6.5. Where a choice was made deliberately, it also
says why. Two things that were designed are not built, and both are listed in
§13: per-catalog Socket.IO rooms, and recording who started a workflow.

**Code:**
- `systems/backend/auth.py`: tables, sessions, roles, throttling, the request hook.
- `systems/backend/app.py`: the `/api/auth/*`, `/api/users/*` and `/access` routes, and the Socket.IO handshake.
- `systems/backend/manage_users.py`: account administration from a shell.
- `systems/frontend/src/views/Login.js` and `App.js`: the gate.
- `systems/frontend/src/services/api.js`: the interceptors.
- `systems/gateway/`: TLS, covered in §10.

**Related:**
- `redesign-ports-routes-spec.md`: the gateway and the service names.
- `album-deployment-spec.md` and `deployment-publish-to-server-spec.md`: albums served outside the application, covered in §9.
- `backup-backrest-spec.md`: who may change backups.
- The original brief, `user-authentication-spec-orig.md`.

---

## 1. Purpose

Airlock holds a family's photographs, and the people, places and dates found
in them. Without authentication, anyone who could reach the machine could
read, change and delete all of it. That includes any device on the home
network, and, through permissive CORS, any web page open in a browser on that
network.

Authentication gives every person an account with a password, makes each
catalog belong to one or more accounts, and lets an owner share a catalog with
other accounts at a chosen role.

### What it guarantees

1. Every `/api/**` request, except the few public auth endpoints in §6.1, returns `401` and no data without a valid session.
2. An account sees only the catalogs it owns or has been given.
3. An owner can grant, change and revoke another account's access to a catalog.
4. Revocation is immediate. Signing out, a password change, deactivation, and an administrator's "sign out everywhere" each end the affected sessions at once.
5. Service-to-service traffic and workflow subprocesses need no credentials (§2).

### What it does not claim

It does **not** make Airlock safe to expose to the internet as it stands. See §3
and §10.7.

---

## 2. The shape it was built on

These properties of the system made authentication a contained change, and
they are worth keeping:

- **One choke point for authorization.** Nearly every route is
  `/api/catalogs/<int:catalog_id>/…`, so one check on that path parameter
  covers most of the surface (§6.1).
- **One HTTP client in the frontend.** `services/api.js` creates a single axios
  instance, so credentials and the 401/403 handling live in one place (§8.4).
- **Nothing calls back in.** Workflows and the backend call *out* to the face,
  vision and album services, and no service calls the main backend's API. So
  there are no service accounts, and adding authentication broke nothing
  between services.
- **No new dependency.** Werkzeug, already installed with Flask, provides scrypt
  password hashing.

---

## 3. Threat model

**In scope:**
- casual access by other people and devices on the home network;
- accidental destructive calls from a stale browser tab or a script;
- keeping one person's catalog from being edited by another;
- a foundation for privileged operations that would otherwise be open to
  anyone, such as publishing to a server and running backups.

**Not in scope:**
- **The frontend bundle is not a secret.** nginx serves `index.html` and the
  compiled JavaScript before any API call, and Create React App inlines
  `REACT_APP_*` at build time. The gate in §8.1 is a usability redirect. The
  enforcement is that the API returns 401. Never compile a secret into the
  bundle.
- **A private CA protects against passive sniffing, not against an attacker
  who can get a device to trust their own CA** (§10).
- **Deployed albums are a separate surface** (§9).
- **Internet exposure** changes the attacker set; see §10.7.

---

## 4. Data model

The tables live in the application database,
`systems/backend/database/catalogs.db`, beside `catalog_meta`. They are not in
a catalog's `workflow_results.db`, because an identity spans catalogs.
`auth.init_schema()` creates them on every start, so a new installation needs
no migration step.

### 4.1 `users`

```sql
CREATE TABLE users (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    username             TEXT NOT NULL UNIQUE COLLATE NOCASE,
    email                TEXT UNIQUE COLLATE NOCASE,
    display_name         TEXT NOT NULL,
    password_hash        TEXT NOT NULL,
    is_active            INTEGER NOT NULL DEFAULT 1,
    is_admin             INTEGER NOT NULL DEFAULT 0,
    must_change_password INTEGER NOT NULL DEFAULT 0,
    created_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at           TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_login_at        TIMESTAMP,
    person_catalog_id    INTEGER,       -- link to a person: see below
    person_id            INTEGER
);
CREATE UNIQUE INDEX idx_users_person
    ON users(person_catalog_id, person_id) WHERE person_id IS NOT NULL;
```

- **`COLLATE NOCASE`** means `Alex` and `alex` are one account.
- **`is_admin`** gates operations across the installation: managing accounts, and setting another account's password. It is not a catalog role. An administrator still needs a role on a catalog to see that catalog.
- **`must_change_password`** is set on every account created by an administrator, and whenever an administrator sets someone's password. The holder must choose a new password at their next sign-in (§8.2).
- **A link to a person** is optional. An account can be linked to a person in one catalog, so the account's page shows that person's face and details. People live in each catalog's own database, and SQLite cannot express a foreign key across databases, so the link is a soft reference. The *pair* `(person_catalog_id, person_id)` identifies the person, because `people.id` is unique only within a catalog. The partial unique index allows one account per person, and any number of unlinked accounts.

### 4.2 `catalog_access` and roles

```sql
CREATE TABLE catalog_access (
    catalog_id  INTEGER NOT NULL,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    role        TEXT NOT NULL CHECK (role IN ('owner','editor','viewer')),
    granted_by  INTEGER REFERENCES users(id),
    granted_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (catalog_id, user_id)
);
```

One table expresses both ownership and sharing, so a catalog can have several
owners. Creating a catalog makes its creator an owner. `catalog_meta` also
records `owner_user_id`, but only for display: the permission checks read
`catalog_access`.

| | `viewer` | `editor` | `owner` |
|---|---|---|---|
| See photos, people, events, places, albums, backup cards | yes | yes | yes |
| Edit photos, people, events, albums; run workflows; import | – | yes | yes |
| Share the catalog (`/access`) | – | – | yes |
| Rename or delete the catalog (`PUT`/`DELETE /api/catalogs/<id>`) | – | – | yes |
| Set or clear an album deployment's password (`/password`) | – | – | yes |
| Purge removed files for good (`/removed-files/purge`) | – | – | yes |
| Publish to a server: see, add or use servers (`/servers`, `/publish-runs`, reads included) | – | – | yes |
| Create, change, run or restore a backup (any non-read on `/backups`) | – | – | yes |

The owner-only rows share one trait. Each one decides who outside the
application can see the photographs, or it cannot be undone. A server's
address and its key line are not for every viewer, which is why publishing is
owner-only even for reads.

**Invariant: every catalog keeps at least one owner.** Demoting or revoking the
last owner is refused (`409`). So is deleting an account that is the only
owner of any catalog.

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
```

**Why server-side sessions, not Flask's signed cookie or a JWT.** Revocation
must be immediate (§1, guarantee 4). A signed cookie and a JWT are both
self-contained: once issued, they stay valid until they expire. So "sign out
everywhere", "disable this account" and "revoke this share" can't take effect
without a server-side denylist, which is this table arrived at the long way
round. A row per session also gives the account page something true to show:
the signed-in devices, with browser, address and last use. The cost is one
primary-key lookup per request.

**Lifetime.** 30 days from sign-in (`expires_at`). An expired session, or one
whose account is deactivated, is deleted when it is next presented. Sessions
nobody presents again are swept at startup.

**`last_seen_at` is refreshed at most every 5 minutes.** It used to be written
on every request. A gallery page fires dozens of thumbnail requests at once,
and each one queued for the database's single write lock. One that waited too
long failed with "database is locked", which lost a thumbnail for a timestamp
nobody reads to the second. The refresh, and the lazy delete, are now
best-effort: a busy database skips them, and they never fail the request.

### 4.4 Installing and upgrading

- **A new installation** has no accounts. The first visit offers first-run setup (§7), which creates the first administrator. That administrator becomes owner of every catalog that has no owner yet.
- **An installation from before accounts existed** is upgraded by `migrate_add_users.py`. It turns the single implicit user into an administrator account. A `--password` can be given, or a password is generated and shown once, and the account must change it at first sign-in. The script also grants that account ownership of the existing catalogs, matched by the old owner name; unmatched catalogs go to the administrator and are reported. It is idempotent, and `--dry-run` shows what it would do.
- **`user_settings`** holds per-account profile details: address, city, country, birthdate, and home coordinates. It is keyed by `user_id`. The `users` row holds only identity and rights.

---

## 5. Authentication

### 5.1 Cookie sessions, not bearer tokens

This choice is forced by images. The views build image URLs that `<img src>`
loads directly:

```js
`/api/catalogs/files?path=…`
`/api/catalogs/${catalogId}/photos/${fileId}/thumbnail?size=320`
`/api/people/photos/${photo_path}`
```

An `<img>` tag can't set an `Authorization` header. With bearer tokens, every
image would have to be fetched by JavaScript into a blob URL, or carry the
token in the query string, where it lands in logs and browser history. A
cookie is sent automatically for `<img>`, `fetch` and Socket.IO alike. The
gateway puts the UI and the API on one origin, so there is no cross-origin
cookie problem in a deployed installation.

### 5.2 Passwords

`werkzeug.security` scrypt hashing, with its default parameters. A hash is
never logged or returned: `auth.user_public()` is the only shape of an account
that leaves the server. A password must be at least **12 characters**,
checked on the server, with no composition rules.

### 5.3 The secret key

The Flask secret key must be stable across restarts. Otherwise every deploy
would sign everyone out, which looks exactly like a broken login. It is read
from `AIRLOCK_SECRET_KEY` when that is set. Otherwise it is generated once and
stored at `database/secret_key`, mode `0600`. `database/` is bind-mounted from
the host (`DATABASE_HOST_DIR`), so the key survives image rebuilds, as
`catalogs.db` does. It must never be baked into an image or a `REACT_APP_*`
variable.

### 5.4 The cookie, and allowed origins

```
Set-Cookie: airlock_session=<id>; HttpOnly; SameSite=Lax; Path=/; Max-Age=2592000[; Secure]
```

- **`HttpOnly`:** JavaScript can't read it, so a script-injection bug can't steal the session.
- **`SameSite=Lax`:** blocks cross-site POSTs, the practical CSRF defence here.
- **`Secure` follows the request's scheme.** It is set when `X-Forwarded-Proto` is `https`, which the gateway sends for every browser request (§10.2). It is not hard-coded: a `Secure` cookie sent over plain HTTP is never returned, which looks like "sign-in succeeds, then every request is a 401".

Signing out deletes both the session row and the cookie.

**Origins.** Credentialed requests can't use a wildcard CORS origin, so the
backend accepts a list. It is `AIRLOCK_ALLOWED_ORIGINS` when set, and
otherwise `https://airlock.lan`, `http://airlock.lan`,
`http://localhost:3000` (the development server) and `http://localhost:5000`.
`deploy/airlock-env.sh` adds the machine's own LAN address and short hostname.

**A request whose `Origin` equals the host it was sent to is always
accepted.** That is same-origin by definition. The Socket.IO library allows it
by default, but giving it an explicit list *replaces* that default. Before
this rule, a UI opened at `https://<lan address>/` had its Socket.IO handshake
refused while every REST call on the same origin worked, because only
Socket.IO checks the origin. The page loaded and worked, but never updated
by itself. Genuinely cross-origin callers, such as the development server on
`:3000`, must still be in the list.

### 5.5 Brute force

`POST /api/auth/login` is throttled per username **and** per source address.
After 10 failures within 15 minutes, further attempts are refused (`429`, with
the wait in the message) for 60 seconds. The lockout doubles with each further
failure, up to 15 minutes. A successful sign-in clears the counters. They live
in memory, which avoids a dependency; losing them on restart is acceptable at
this scale.

The response doesn't distinguish "no such user", "wrong password" and
"deactivated": all three are `401 Invalid username or password`. For an
unknown username the hash check still runs, against a dummy hash, so all
cases take comparable time.

---

## 6. Authorization

### 6.1 Fail closed, at one choke point

A `before_request` hook (`auth.install`) runs on every request:

1. It reads the session cookie, and sets `g.current_user` when the session is live.
2. It lets through the public paths: `/api/auth/login`, `/api/auth/session`, `/api/auth/setup`, `/api/auth/setup-required` and `/gateway-health`.
3. It lets through anything outside `/api/`, which is the static UI and not a secret (§3).
4. With no session, it returns `401`.
5. When the route has a `catalog_id`, it requires the role that §4.2 gives the request, and otherwise returns `403`.

The role comes from the request's **method and URL rule**, not from a
decorator on each route. `GET`, `HEAD` and `OPTIONS` need `viewer`, and other
methods need `editor`. The owner-only rules of §4.2 are matched by URL
fragment (`auth._OWNER_RULE_FRAGMENTS`). So a new catalog route is protected
the moment it is added, and a missed decision shows as an unexpected `403`,
not as an open endpoint. Routes without a `catalog_id` that need more than a
session use `@auth.admin_required` or check the actor themselves, as the
`/api/users/<id>` routes do.

A `403` reads the same whether the catalog doesn't exist or belongs to someone
else, so the API can't be used to list other people's catalogs.

`GET /api/catalogs` returns only the catalogs the account has a role on. The
whole UI is driven from that list, which is what makes it appear correctly
scoped.

`AIRLOCK_AUTH_REQUIRED=0` turns the hook's enforcement off, while sign-in still
works. It exists for development; the default is `1`.

### 6.2 Roles

See the table in §4.2. Note that `is_admin` doesn't bypass catalog roles. An
administrator manages accounts, not other people's photographs.

### 6.3 Paths in URLs are contained

Two endpoints take a file path rather than an id:

```
GET /api/catalogs/files?path=<path>
GET /api/people/photos/<path>
```

Before authentication, both served whatever was at an absolute path, and
searched every catalog for a relative one. That was an arbitrary file read,
and with several accounts it would be a read across accounts, which the
catalog check never sees, because there is no `catalog_id` in the URL. Both
now go through `_resolve_within_catalogs()`:

- The candidate is passed through `realpath` and must lie under the real path of a catalog **this account can read**. `realpath` matters because album deployments are built from symlinks, and a symlink is otherwise a way out.
- A relative path is tried only against those catalogs.
- "Outside your catalogs" and "doesn't exist" give the same `404`, so the endpoint can't be used to probe the filesystem.

`/api/people/photos/` also allows one extra root, the backend's own `people/`
folder, for photos uploaded before catalogs held them, and that root is
contained the same way. New code should use the id-based routes, such as
`/api/catalogs/<id>/photos/<file_id>/thumbnail`, which are scoped by
construction.

### 6.4 Socket.IO

The handshake is authenticated with the same session cookie. A connection
without a live session is refused. Origins are checked as in §5.4.

Progress events (`workflow_progress` and the others) are emitted to **every
signed-in connection**, not only to those that can read the catalog. An
account can therefore see that a workflow is running in a catalog it has no
access to, and its progress counts, though none of its photos or data. This
is acceptable within one household. Per-catalog rooms are the fix, listed
in §13.

### 6.5 Workflow subprocesses

Workflows run as detached subprocesses that write to the catalog database
directly. They never call the API, so they need no credentials. Who started a
workflow is not recorded (§13). Publish runs do record who started them.

---

## 7. API

| Method | Path | Who | Purpose |
|---|---|---|---|
| `GET` | `/api/auth/setup-required` | public | `{setup_required}`: true only while no account exists |
| `POST` | `/api/auth/setup` | public, first run only | Creates the first administrator and signs it in; `409` once any account exists |
| `POST` | `/api/auth/login` | public | `{username, password}`: sets the cookie, returns the account |
| `GET` | `/api/auth/session` | public | The current account, or `401`: the UI's boot probe |
| `POST` | `/api/auth/logout` | session | Ends this session |
| `POST` | `/api/auth/logout-all` | session | Ends every session of this account, this one included |
| `GET` | `/api/auth/sessions` | session | This account's signed-in devices |
| `POST` | `/api/auth/password` | session | `{current, new}`: changes your password and ends your other sessions |
| `GET` | `/api/users` | session | Every account. Administrators get full detail; others get id, username and name, enough to share a catalog |
| `POST` | `/api/users` | admin | Creates an account with a temporary password (`must_change_password`) |
| `PATCH` | `/api/users/<id>` | admin, or self | Name, email, person link; only an administrator sets `is_admin`/`is_active` |
| `DELETE` | `/api/users/<id>` | admin | Refused for yourself, and for the only owner of a catalog |
| `GET`, `PUT` | `/api/users/<id>/profile` | admin, or self | Profile details (`user_settings`) |
| `POST` | `/api/users/<id>/password` | admin | Sets another account's temporary password, and signs it out everywhere |
| `GET`, `DELETE` | `/api/users/<id>/sessions` | admin, or self | That account's devices; sign it out everywhere |
| `GET` | `/api/users/<id>/person`, `/avatar` | session | The linked person, and their face |
| `GET` | `/api/catalogs/<id>/access` | owner | Who can reach this catalog, at what role |
| `POST` | `/api/catalogs/<id>/access` | owner | `{user_id, role}`: grant or change |
| `DELETE` | `/api/catalogs/<id>/access/<user_id>` | owner | Revoke |

**Error contract.**
- **`401`** means not signed in, and the UI shows the sign-in view.
- **`403`** means signed in but not allowed. The UI shows the message and does **not** redirect: sending a signed-in user to a sign-in page they already passed is a confusing loop.
- Bodies are `{"error": "...", "code": "unauthenticated" | "forbidden"}`, and a throttled sign-in has `"code": "rate_limited"`.

**First run.** `/api/auth/setup` checks that no account exists, and inserts,
inside one `BEGIN IMMEDIATE` transaction, so two simultaneous requests can't
create two administrators.

**Guard rails that keep a way back in:**
- You can't remove your own administrator rights.
- You can't deactivate or delete your own account.
- You can't remove the last administrator.
- You can't set your own password without the current one.
- Whatever the UI allows, `manage_users.py` always works from a shell (§12).

Deactivating an account ends all its sessions.

---

## 8. UI

### 8.1 The gate

`App.js` asks `GET /api/auth/session` once, before anything else renders:
- **while waiting:** a full-page spinner;
- **`401`:** the sign-in view, or first-run setup while `setup_required` is true;
- **a session that must change its password:** the change-password form, and nothing else;
- **otherwise:** the application.

This keeps data off the screen until sign-in, as far as a browser application
can. The bundle itself is still served (§3).

### 8.2 Sign-in, first run, and a forced password change

All three are in `views/Login.js`:
- **Sign-in:** username and password, with one error that doesn't say which was wrong. There is no "register" link: accounts are created by an administrator, or, for the first one, by first-run setup.
- **First run:** "Welcome to Airlock Photos — create the administrator account", with username, name, email and password.
- **Forced change:** when `must_change_password` is set, "Choose a new password" replaces the application until a new password is saved.

If the page was loaded from somewhere that doesn't serve `/api`, such as a
service port, the setup probe gets no answer. The sign-in view then shows a
warning banner rather than pretending an account exists.
`deploy/diagnose-first-run.sh` tells these cases apart.

### 8.3 Accounts and sharing

- **Each account has its own page, `/users/<id>`.** It is opened from your name in the top bar, or by an administrator from a card in **Admin ▸ Users**. The page has four sections: User settings (profile), Linked person, Password, and Signed-in devices (with "sign out everywhere"). An administrator sees the same page for any account, and can set its password there.
- **Admin ▸ Users** lists the accounts. Creating an account, and the Administrator and Active switches, are on the cards. The switches on your own card are disabled, matching the server's guard rails (§7).
- **Sharing a catalog** is on the Catalogs page (`CatalogSharePanel.js`): who has access, at what role, with grant, change and revoke, for owners only.
- **The gear** opens Site settings, `/settings`, which covers the installation, not an account.

### 8.4 Interceptors

On the single axios instance (`services/api.js`):
- every request is sent with `withCredentials: true`;
- a `401` clears the session and shows the sign-in view, **except** for the boot probe, which expects `401` and would otherwise loop;
- a `403` shows its message and keeps the user signed in.

### 8.5 Images

Because the session is a cookie (§5.1), `<img src>` works without any
change. A `401` on an image shows as a broken image, not a redirect, which is
why the gate (§8.1) runs before any view mounts.

---

## 9. Album deployments are a separate surface

A deployed album is served by `album-gallery` (`server.mjs`) at
`https://<catalog>.airlock.lan/<album>` (internally `/d/<catalog>/<slug>/`). It
reads files from a read-only mount of the catalog cache and **never talks to
the backend**, so none of the account checks above apply to it.

- **A deployment can have a password.** Only an owner can set it (§4.2). The gallery checks it with a form and its own signed cookie, per deployment; it is not an Airlock account. It verifies the scrypt hash that the backend wrote, so there is one hashing scheme in the system.
- **A deployment without a password is public** to anyone who can reach the gateway. That is the point of sharing an album with family, and it is a decision the owner makes per deployment.
- **Revoking someone's access to a catalog doesn't revoke an album link they already have.** Change or set the deployment's password to do that.
- **Publishing to a server outside the LAN** (`deployment-publish-to-server-spec.md`) puts a deployment on a public host. It is owner-only for the same reason.

---

## 10. TLS and certificates

TLS is what makes the `Secure` cookie (§5.4) possible, and it keeps passwords
off the network in clear text. It is set up so that the same configuration
serves the private LAN name today and a public domain later; only the
certificate source and one variable change.

### 10.1 Two name planes

| Plane | Names | Resolved by | Certificate |
|---|---|---|---|
| **Browser-facing** | `airlock.lan`, `<catalog>.airlock.lan`, the machine's LAN address; later, a public domain | browsers, `/etc/hosts`, DNS | **yes** |
| **Internal services** | `backend.airlock.lan`, `deepface.airlock.lan`, `albums.airlock.lan`, … | containers' host aliases | no: plain HTTP behind the gateway |

The internal names are plumbing. The backend reaches the face and album
services through them (`DEEPFACE_URL`, `ALBUMS_URL`), and no browser sees
them. They must **not** be renamed when a public domain arrives.

**The frontend is domain-agnostic.** Every URL in `systems/frontend/src` is
relative, so changing the public name needs no frontend rebuild. Never
introduce an absolute `REACT_APP_*` host.

### 10.2 Termination

TLS ends at the gateway, the one component that knows the ports. Every hop
behind it is HTTP. `proxy.conf` sends `X-Forwarded-Proto $scheme`, which is
`https` for browser requests, and that is what the backend reads to set
`Secure` (§5.4). There is one certificate, in one place to renew.

### 10.3 Certificate providers are pluggable

nginx reads the certificate from fixed paths, mounted read-only from
`systems/gateway/certs/`:

```
/etc/nginx/certs/fullchain.pem
/etc/nginx/certs/privkey.pem
```

and doesn't know how they got there:

| | Provider A: private CA (today) | Provider B: ACME (a public domain) |
|---|---|---|
| Tool | `make-certs.sh` (openssl) | certbot, lego or similar |
| Covers | `airlock.lan`, `*.airlock.lan`, the machine's own IPv4 addresses, any names given | the public domain |
| Trust | the root installed once per device | every browser already |
| Renewal | by hand; the leaf lasts 825 days (about 27 months, the most some clients accept), the root 10 years | automatic |

`make-certs.sh` creates a root CA (`airlock-ca.pem` and its key) and a leaf
for the names above. `deploy/deploy.sh` runs it on a new machine. **Each
installation has its own CA**, named after the machine. A device that uses
two installations trusts both roots. `--force` renews the leaf and keeps the
CA, so devices need nothing new. `--new-ca` starts over, and every device must
trust the new root. An address given on the command line becomes an IP SAN,
because browsers reject an IP address carried as a DNS name.

The CA key is the sensitive file: mode `0600`, never in an image, and
`systems/gateway/certs/` is in `.gitignore`. Certificates are machine state,
like `catalogs.db`, and are not committed.

Installing the root is covered per platform in the installation guides
(`docs/guides/INSTALLATION.md`): macOS Keychain, the Linux system
store and Firefox's own list, an iOS profile, and Android's CA store.

### 10.4 The gateway's configuration

`airlock.conf.template`:
- **`:80`** serves `/.well-known/acme-challenge/` (§10.5) and redirects everything else to HTTPS. It also carries the internal service names, which stay on plain `:80` (§10.1).
- **`:443`** serves `${SERVER_DOMAIN}` and `localhost`, plus a block per `<catalog>.airlock.lan` for album deployments. It uses TLS 1.2 and 1.3, the fixed certificate paths, and HTTP/2.

`SERVER_DOMAIN` (default `airlock.lan`) is substituted when the container
starts, so a public name is one environment variable, not an image rebuild.
Rootless podman needs the port floor lowered to 80 to publish 80 and 443;
`deploy.sh` does this on macOS and prints the command on Linux. Otherwise,
`GATEWAY_PORT` and `TLS_PORT` move the gateway to 8080 and 8443.

### 10.5 The ACME path exists already

`/.well-known/acme-challenge/` serves nothing today. It is there because
HTTP-01 validation needs an unredirected plain-HTTP path on port 80, and a
blanket redirect breaks issuance and renewal in a way that is confusing to
diagnose months later. A wildcard public certificate would need DNS-01
instead. If only the main name is exposed, HTTP-01 is enough; the service
names are internal (§10.1).

### 10.6 No HSTS while the CA is private

The gateway sends no `Strict-Transport-Security`. HSTS tells browsers to
refuse plain HTTP for the domain for the stated time, and changing the server
can't undo it. Enable it only on a public domain with a publicly trusted
certificate. Start at `max-age=300`, and raise it once renewal is proven.

### 10.7 Moving to a public domain

1. Point DNS at the host, and open 80 and 443.
2. Put a Provider B certificate in `systems/gateway/certs/`.
3. Set `SERVER_DOMAIN=<the domain>`, and restart the gateway.
4. Add `https://<the domain>` to `AIRLOCK_ALLOWED_ORIGINS` (§5.4).
5. Once renewal works, add HSTS (§10.6).

There is no frontend rebuild, no backend image change, and no renaming of
internal services. What changes is the threat model. The attacker set is no
longer "people on the home network":
- login throttling (§5.5) becomes load-bearing;
- album deployments without a password (§9) become public on the internet;
- the progress broadcast (§6.4) and the other limits in §13 deserve a second look.

To share albums outside the home, publishing a deployment to a server of its
own (§9) is the safer route. It exposes the album, not the library.

---

## 11. History

| Version | What arrived |
|---|---|
| v4.1.0 | Accounts, sessions, roles, the fail-closed hook, contained file paths, TLS at the gateway, `manage_users.py`, `migrate_add_users.py` |
| v4.2.0 | Sharing a catalog from the Catalogs page |
| v5.6.x | The rename: cookie `airlock_session`, `AIRLOCK_*` settings, `airlock.lan`. The old names were accepted until v6.4.5 |
| v5.13.0 | An account page per account; administrators set passwords and sign accounts out |
| v6.x | More owner-only operations: deployment passwords, purging removed files, publishing to a server, backups. Origins accepted by same-host as well as by list |

---

## 12. Risks, and the way back in

**Locking yourself out** is the largest risk, and it is operational, not
technical. The way back in is `systems/backend/manage_users.py`, run where the
database is:

```bash
podman exec -it workflow-backend python3 manage_users.py list
podman exec -it workflow-backend python3 manage_users.py reset-password <username>
podman exec -it workflow-backend python3 manage_users.py set-admin <username> --on
podman exec -it workflow-backend python3 manage_users.py activate <username> --on
podman exec -it workflow-backend python3 manage_users.py access <catalog_id> --grant <username> --role owner
```

`reset-password` without `--password` generates one and prints it, which keeps
it out of shell history. The other commands are `create`, `rename` and
`sessions [--revoke]`.

**Sessions dropped on every rebuild** would mean an unstable secret key, or a
`database/` folder that isn't the mounted one. Both live under `database/`
(§5.3). Check after the first rebuild on a new machine.

**A private CA is only as good as its distribution.** A device without the
root shows a warning. People trained to click through warnings are worse off
than people on plain HTTP, because the warning stops meaning anything.
Install the root on every device that uses Airlock.

**HSTS can't be revoked** (§10.6).

**Album links outlive catalog access** (§9).

**A forgotten owner check.** A new owner-only operation must be added to
`_OWNER_RULE_FRAGMENTS` in `auth.py`. Otherwise it gets the default, which is
`editor` for writes and `viewer` for reads. Default-protected, but not
owner-only.

---

## 13. Not built, and out of scope

**Designed, not built:**
- **Per-catalog Socket.IO rooms.** Progress events would go to a room `catalog:<id>`, joined only by connections that can read that catalog, instead of to every signed-in connection (§6.4).
- **`started_by_user_id` on workflow executions**, so the UI can show who ran what, with `NULL` for scheduled runs (§6.5).

**Out of scope:**
- OAuth and single sign-on;
- multi-factor authentication;
- password reset by email (there is no mail path);
- permissions finer than a catalog, such as per person or per album, other than deployment passwords;
- audit logging;
- rate limiting beyond the sign-in route.
