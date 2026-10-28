/**
 * Production server for album-gallery.
 *
 * Serves the Vite build output (app/dist) and the DEPLOYMENTS published by the
 * main backend. See documentation/specs/albums-enhancement-spec.md.
 *
 * ── What a deployment is ─────────────────────────────────────────────────────
 * A directory the backend writes at
 *
 *   <CATALOGS_ROOT>/<catalog-dir>/cache/Deployments/<slug>/
 *     albums.json       the manifest: which album folders this deployment shows
 *     deployment.json   name, description, whether a password is required
 *     .auth.json        the scrypt hash, mode 0600 — NEVER served (see below)
 *     00001 -> ../../Albums/00001      relative symlinks into the catalog cache
 *
 * Creating one touches no container: this process is started once and simply
 * finds a new directory on the next request. That is the whole reason the
 * design works without letting the backend drive podman.
 *
 * ── Routes ───────────────────────────────────────────────────────────────────
 *   GET  /d/<catalog>/<slug>/                     the SPA
 *   GET  /d/<catalog>/<slug>/albums.json          the manifest        [gated]
 *   GET  /d/<catalog>/<slug>/deployment.json      name + lock state   [partly]
 *   GET  /d/<catalog>/<slug>/albums/<id>/…        images and album.json [gated]
 *   POST /d/<catalog>/<slug>/login                { password }
 *   POST /d/<catalog>/<slug>/logout
 *   GET  /health                                  liveness + readiness [open]
 *
 * The CATALOG is in the URL, and it comes first. That is what makes deployment
 * slugs unique per catalog with nothing to scan, and it is what lets this
 * server resolve a request by substitution instead of by building and
 * invalidating an index of every deployment on the machine.
 *
 * ── Environment ──────────────────────────────────────────────────────────────
 *   CATALOGS_ROOT          the catalogs root, mounted read-only. Required for
 *                          deployments.
 *   ALBUM_ROOT             legacy: one catalog's cache/Albums, served at the
 *                          server root. Honoured for one release; CATALOGS_ROOT
 *                          wins when both are set.
 *   ALBUM_SESSION_SECRET   HMAC key for password sessions. Wins when set.
 *   ALBUM_STATE_DIR        a small writable folder for what must outlive the
 *                          process. With the secret unset, one is generated
 *                          there ONCE (session-secret, mode 0600) and reused at
 *                          every start, so a restart no longer signs everyone
 *                          out of password-protected deployments. With neither,
 *                          a key is generated per process, loudly.
 *   PORT                   default 3001
 */

import crypto from 'node:crypto'
import fs from 'node:fs'
import http from 'node:http'
import path from 'node:path'
import { fileURLToPath } from 'node:url'

const __dirname = path.dirname(fileURLToPath(import.meta.url))

const DIST_DIR = path.resolve(__dirname, 'app/dist')
const PORT = Number(process.env.PORT ?? 3001)

const CATALOGS_ROOT = process.env.CATALOGS_ROOT
  ? path.resolve(process.env.CATALOGS_ROOT)
  : null

// The pre-deployment world: one catalog's cache/Albums served at the root.
// Kept working for one release so an existing install does not go dark on
// upgrade. CATALOGS_ROOT wins when both are set.
const ALBUM_ROOT = process.env.ALBUM_ROOT ? path.resolve(process.env.ALBUM_ROOT) : null

// A slug is a DNS label as well as a URL segment, because the subdomain form
// puts the catalog in the hostname. This is the same expression the backend
// validates with and the same one the nginx wildcard server_name uses, so a
// name accepted anywhere is routable everywhere.
const SLUG = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/

const SESSION_TTL_MS = 30 * 24 * 60 * 60 * 1000   // 30 days

// Which gallery this is: the last commit to systems/album-gallery, baked in at
// build time (Dockerfile ARG). /health reports it, so the Publish to server
// button can tell a server still running an older gallery from this machine's
// (deployment-publish-to-server-spec.md section 9).
const GALLERY_REVISION = process.env.GALLERY_REVISION || 'unknown'

const SECRET_FILE = 'session-secret'

/**
 * The key that signs password sessions, and where it came from.
 *
 * It used to be generated per process whenever ALBUM_SESSION_SECRET was unset -
 * which was every installation that had not set it by hand - so each restart of
 * this server (every deploy, every reboot) signed every visitor out of every
 * password-protected deployment. Kept in a file instead, it is generated once
 * per installation. The variable still wins, so a value set on purpose is never
 * replaced by the file.
 */
function loadSessionSecret(env = process.env) {
  if (env.ALBUM_SESSION_SECRET) return { secret: env.ALBUM_SESSION_SECRET, source: 'environment' }
  const dir = env.ALBUM_STATE_DIR
  if (dir) {
    const file = path.join(dir, SECRET_FILE)
    try {
      const kept = fs.readFileSync(file, 'utf8').trim()
      if (kept.length >= 32) return { secret: kept, source: 'kept' }
    } catch (err) {
      if (err.code !== 'ENOENT') console.warn(`album-gallery: cannot read ${file}: ${err.message}`)
    }
    const fresh = crypto.randomBytes(32).toString('hex')
    try {
      fs.mkdirSync(dir, { recursive: true })
      // 'w' rather than 'wx': a file that exists but was unusable (empty, too
      // short) is replaced, not kept forever.
      fs.writeFileSync(file, fresh + '\n', { mode: 0o600 })
      return { secret: fresh, source: 'kept' }
    } catch (err) {
      console.warn(`album-gallery: cannot keep the session secret in ${file}: ${err.message}`)
    }
  }
  console.warn(
    'album-gallery: no ALBUM_SESSION_SECRET and no writable ALBUM_STATE_DIR. A key\n' +
    '  was generated for this process, so everyone signed in to a password-protected\n' +
    '  deployment will be signed out when this server restarts.',
  )
  return { secret: crypto.randomBytes(32).toString('hex'), source: 'process' }
}

const { secret: SESSION_SECRET, source: SESSION_SECRET_SOURCE } = loadSessionSecret()

const MIME = {
  '.html': 'text/html',
  '.js': 'application/javascript',
  '.mjs': 'application/javascript',
  '.css': 'text/css',
  '.json': 'application/json',
  '.jpg': 'image/jpeg',
  '.jpeg': 'image/jpeg',
  '.png': 'image/png',
  '.gif': 'image/gif',
  '.webp': 'image/webp',
  '.avif': 'image/avif',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon',
  '.mp4': 'video/mp4',
  '.mov': 'video/quicktime',
  '.woff': 'font/woff',
  '.woff2': 'font/woff2',
  '.txt': 'text/plain',
}

function mime(filePath) {
  return MIME[path.extname(filePath).toLowerCase()] ?? 'application/octet-stream'
}

/**
 * Serve a file, honouring HTTP range requests.
 *
 * Ranges matter here because deployed albums now carry video. A browser seeking
 * in a <video> asks for a byte range; a server that answers 200 with the whole
 * file cannot be seeked, and Safari - which probes with `Range: bytes=0-1`
 * before it will play anything - may refuse the video outright. Streaming the
 * whole body was fine while every asset was a JPEG and is not any more.
 *
 * `req` is optional so the static-asset callers that never need a range can
 * keep calling this with two arguments.
 */
function serveFile(filePath, res, extraHeaders = {}, req = null) {
  let stat
  try {
    stat = fs.statSync(filePath)
  } catch {
    return send(res, 404, 'Not found')
  }
  const size = stat.size

  res.setHeader('Content-Type', mime(filePath))
  res.setHeader('Accept-Ranges', 'bytes')
  // Validators, so a browser holding a copy can ask "still the same?" and get a
  // bodyless 304 instead of the whole photo again. Size + mtime, the same
  // weak validator nginx uses: a re-rendered file changes at least one.
  const etag = `W/"${size.toString(16)}-${Math.floor(stat.mtimeMs).toString(16)}"`
  res.setHeader('ETag', etag)
  res.setHeader('Last-Modified', stat.mtime.toUTCString())
  for (const [k, v] of Object.entries(extraHeaders)) res.setHeader(k, v)

  if (notModified(req, etag, stat.mtime)) {
    res.statusCode = 304
    res.removeHeader('Content-Type')
    return res.end()
  }

  const rangeHeader = req?.headers?.range
  const match = rangeHeader ? /^bytes=(\d*)-(\d*)$/.exec(rangeHeader.trim()) : null
  if (match) {
    const [, rawStart, rawEnd] = match
    let start
    let end
    if (rawStart === '') {
      // A suffix range: the LAST n bytes.
      const suffix = Number(rawEnd)
      if (!Number.isFinite(suffix) || suffix <= 0) {
        return send(res, 416, 'Range not satisfiable', 'text/plain',
          { 'Content-Range': `bytes */${size}` })
      }
      start = Math.max(0, size - suffix)
      end = size - 1
    } else {
      start = Number(rawStart)
      end = rawEnd === '' ? size - 1 : Number(rawEnd)
    }
    if (!Number.isFinite(start) || !Number.isFinite(end) || start > end || start >= size) {
      return send(res, 416, 'Range not satisfiable', 'text/plain',
        { 'Content-Range': `bytes */${size}` })
    }
    end = Math.min(end, size - 1)
    res.statusCode = 206
    res.setHeader('Content-Range', `bytes ${start}-${end}/${size}`)
    res.setHeader('Content-Length', String(end - start + 1))
    return fs.createReadStream(filePath, { start, end }).pipe(res)
  }

  res.setHeader('Content-Length', String(size))
  fs.createReadStream(filePath).pipe(res)
}

/**
 * True when the request's conditional headers say the browser's copy is current.
 * If-None-Match wins over If-Modified-Since when both are sent (RFC 9110 13.2.2).
 */
function notModified(req, etag, mtime) {
  if (!req || (req.method && req.method !== 'GET' && req.method !== 'HEAD')) return false
  const inm = req.headers?.['if-none-match']
  if (inm) {
    return inm.split(',').map((t) => t.trim().replace(/^W\//, ''))
      .some((t) => t === '*' || t === etag.replace(/^W\//, ''))
  }
  const ims = req.headers?.['if-modified-since']
  if (ims) {
    const since = Date.parse(ims)
    // HTTP dates have whole seconds; compare at that resolution.
    return Number.isFinite(since) && Math.floor(mtime.getTime() / 1000) * 1000 <= since
  }
  return false
}

// ── Browser caching ───────────────────────────────────────────────────────────
// What each kind of file may be reused for without asking again.
//
//   photos, videos, thumbnails   7 days fresh, then up to 30 more served from
//                                the cache while it checks in the background.
//                                Their names carry the original's fingerprint,
//                                so a URL nearly always means the same bytes;
//                                the rare re-render (a new video proxy) reaches
//                                a returning visitor within a week.
//   album/section manifests      always checked (no-cache): a republish must
//                                show at once. The check is a 304 when nothing
//                                changed, so it costs a round trip, not a body.
//   app code (assets/*-HASH.*)   a year, immutable: a new build has new names.
//   the page itself              always checked, for the same reason.
//
// "private" for a password-protected gallery: only the visitor's own browser
// may keep it, never a shared cache between them and this server.
const MEDIA_MAX_AGE = 7 * 24 * 3600
const MEDIA_STALE = 30 * 24 * 3600

function mediaCacheControl(filePath, locked) {
  if (path.extname(filePath).toLowerCase() === '.json') return 'no-cache'
  return `${locked ? 'private' : 'public'}, max-age=${MEDIA_MAX_AGE}, `
    + `stale-while-revalidate=${MEDIA_STALE}`
}

function appCacheControl(rel) {
  // Vite names every built asset <name>-<hash>.<ext> under assets/.
  if (/^assets\/.+-[A-Za-z0-9_-]{6,}\.[a-z0-9]+$/.test(rel)) {
    return 'public, max-age=31536000, immutable'
  }
  return 'public, max-age=86400'
}

function send(res, status, body, type = 'text/plain', headers = {}) {
  res.statusCode = status
  res.setHeader('Content-Type', type)
  for (const [k, v] of Object.entries(headers)) res.setHeader(k, v)
  res.end(body)
}

function sendJson(res, status, value, headers = {}) {
  send(res, status, JSON.stringify(value), 'application/json', headers)
}

// ── Catalog slug → directory ──────────────────────────────────────────────────
//
// The URL names a catalog by SLUG; the filesystem names it by directory. The
// backend writes the one mapping that cannot be derived — <CATALOGS_ROOT>/
// catalogs.json, {slug: directory-name} — on catalog create, rename and delete.
//
// Read at boot and refreshed on a miss, rate-limited so an unknown slug cannot
// turn into a disk read per request. Directory NAMES, never absolute paths:
// this container's CATALOGS_ROOT is not the backend's.

let catalogIndex = {}
let catalogIndexReadAt = 0
const CATALOG_INDEX_MIN_INTERVAL_MS = 1000

function readCatalogIndex() {
  catalogIndexReadAt = Date.now()
  if (!CATALOGS_ROOT) return
  try {
    const raw = fs.readFileSync(path.join(CATALOGS_ROOT, 'catalogs.json'), 'utf8')
    const parsed = JSON.parse(raw)
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      catalogIndex = parsed
    }
  } catch {
    // Absent on a machine with no catalogs yet, and unreadable during the
    // instant the backend replaces it. Neither is worth a stack trace; the
    // previous mapping stays in effect.
  }
}

/**
 * The absolute catalog directory for a URL slug, or null.
 *
 * The slug arrives from the path or, under the subdomain form, from the Host
 * header — so it is never joined to a path directly. It is matched against the
 * label grammar, looked up in the index, and only the DIRECTORY NAME that comes
 * back is joined. Three steps, because this route is reachable from outside.
 */
function catalogDirFor(slug) {
  if (!CATALOGS_ROOT || !SLUG.test(slug)) return null
  let dir = catalogIndex[slug]
  if (dir === undefined && Date.now() - catalogIndexReadAt > CATALOG_INDEX_MIN_INTERVAL_MS) {
    readCatalogIndex()
    dir = catalogIndex[slug]
  }
  if (typeof dir !== 'string' || !dir || dir.includes('/') || dir.includes('\\')
      || dir === '.' || dir === '..') {
    return null
  }
  const resolved = path.join(CATALOGS_ROOT, dir)
  return contained(CATALOGS_ROOT, resolved) ? resolved : null
}

/** True when *child* is *parent* or sits underneath it. */
function contained(parent, child) {
  const rel = path.relative(parent, child)
  return rel === '' || (!rel.startsWith('..') && !path.isAbsolute(rel))
}

/**
 * contained(), with BOTH sides fully resolved first.
 *
 * Use this whenever the child has been through realpath, because then the
 * parent must be too. A mount point is very often itself a symlink - on macOS
 * /var is /private/var, and the catalogs root under a container bind mount can
 * be one anywhere - so comparing a resolved child against an unresolved parent
 * rejects perfectly legitimate files. That failure is silent and looks exactly
 * like "the album is empty".
 */
function containedReal(parent, realChild) {
  let realParent
  try {
    realParent = fs.realpathSync(parent)
  } catch {
    return false
  }
  return contained(realParent, realChild)
}

// ── Deployments ───────────────────────────────────────────────────────────────

function deploymentDir(catalogSlug, slug) {
  if (!SLUG.test(slug)) return null
  const catalogDir = catalogDirFor(catalogSlug)
  if (!catalogDir) return null
  const dir = path.join(catalogDir, 'cache', 'Deployments', slug)
  return fs.existsSync(dir) ? dir : null
}

function readJson(file) {
  try {
    return JSON.parse(fs.readFileSync(file, 'utf8'))
  } catch {
    return null
  }
}

/**
 * Resolve one asset request inside a deployment, or null.
 *
 * Three independent checks, because a link to a deployment is the part of this
 * system most likely to be public:
 *
 *  1. No path component may start with '.', and none may be '..'. That refuses
 *     .auth.json by name and refuses textual escape from the deployment.
 *  2. The album folder must appear in albums.json. The MANIFEST is what decides
 *     what exists — never the directory listing — so a folder left behind by an
 *     older deployment is not reachable, and a packed bundle behaves the same
 *     way on a host that has no directories to list.
 *  3. The real path, after every symlink is followed, must land inside the
 *     catalog's cache/ directory. The album links point OUT of the deployment
 *     (../../Albums/00001) and on again into cache/Images, so cache/ is the real
 *     boundary — and it is the same one that withholds the originals,
 *     workflow_results.db and the face crops.
 */
function resolveAsset(catalogSlug, slug, relPath) {
  const dir = deploymentDir(catalogSlug, slug)
  if (!dir) return null

  const parts = relPath.split('/').filter((s) => s.length > 0)
  if (parts.length === 0) return null
  if (parts.some((s) => s.startsWith('.'))) return null

  const [folder, ...rest] = parts
  if (!/^\d+$/.test(folder) || rest.length === 0) return null

  const manifest = readJson(path.join(dir, 'albums.json'))
  if (!Array.isArray(manifest) || !manifest.includes(folder)) return null

  const candidate = path.join(dir, folder, ...rest)
  let real
  try {
    real = fs.realpathSync(candidate)
  } catch {
    return null
  }
  const catalogCache = path.join(catalogDirFor(catalogSlug), 'cache')
  if (!containedReal(catalogCache, real)) return null
  if (!fs.statSync(real).isFile()) return null
  return real
}

// ── Password sessions ─────────────────────────────────────────────────────────
//
// A form and a signed cookie, not nginx basic auth. Basic auth would have been
// one htpasswd line, but its dialog cannot be styled or explained, it has no
// sign-out, and it does not grow into the per-deployment users this is meant to
// become. A session is the thing that grows.

function cookieName(catalogSlug, slug) {
  // Keyed on BOTH halves: unlocking family/summer-2019 must grant nothing on
  // work/summer-2019, which is a real pair of URLs under this scheme.
  return `sb_album_${catalogSlug}_${slug}`.replace(/-/g, '_')
}

function sign(payload) {
  return crypto.createHmac('sha256', SESSION_SECRET).update(payload).digest('hex')
}

function makeSessionValue(catalogSlug, slug) {
  const expiry = Date.now() + SESSION_TTL_MS
  const payload = `${catalogSlug}|${slug}|${expiry}`
  return `${payload}|${sign(payload)}`
}

function sessionValid(value, catalogSlug, slug) {
  if (typeof value !== 'string') return false
  const parts = value.split('|')
  if (parts.length !== 4) return false
  const [cat, dep, expiry, sig] = parts
  if (cat !== catalogSlug || dep !== slug) return false
  if (!/^\d+$/.test(expiry) || Number(expiry) < Date.now()) return false
  const expected = Buffer.from(sign(`${cat}|${dep}|${expiry}`), 'utf8')
  const given = Buffer.from(sig, 'utf8')
  return expected.length === given.length && crypto.timingSafeEqual(expected, given)
}

function parseCookies(req) {
  const header = req.headers.cookie
  if (!header) return {}
  const out = {}
  for (const piece of header.split(';')) {
    const idx = piece.indexOf('=')
    if (idx < 0) continue
    out[piece.slice(0, idx).trim()] = piece.slice(idx + 1).trim()
  }
  return out
}

/** True when the request may see this deployment's contents. */
function authorised(req, catalogSlug, slug) {
  const dir = deploymentDir(catalogSlug, slug)
  if (!dir) return false
  if (!fs.existsSync(path.join(dir, '.auth.json'))) return true   // public
  return sessionValid(parseCookies(req)[cookieName(catalogSlug, slug)], catalogSlug, slug)
}

/**
 * Verify a password against the hash the BACKEND wrote.
 *
 * The format is Werkzeug's — `scrypt:N:r:p$salt$hex` — because auth.py hashes
 * user passwords with exactly that, and one answer to "how does this system
 * hash a password" is worth more than a second implementation here. This side
 * only ever verifies; it never writes a hash.
 */
function verifyPassword(stored, password) {
  try {
    const [method, salt, want] = String(stored).split('$')
    const [scheme, N, r, p] = method.split(':')
    if (scheme !== 'scrypt') return false
    const n = Number(N), rr = Number(r), pp = Number(p)
    const got = crypto.scryptSync(password, salt, want.length / 2, {
      N: n, r: rr, p: pp, maxmem: 132 * n * rr * pp + 1024,
    })
    const expected = Buffer.from(want, 'hex')
    return got.length === expected.length && crypto.timingSafeEqual(got, expected)
  } catch {
    return false
  }
}

// Login throttling, the same shape auth.py uses and for a stronger reason: a
// gallery link is the part of this system most likely to be public. Keyed on
// the deployment AND the client address, so neither one deployment nor one
// client can be hammered. In memory on purpose — losing the counters on restart
// is acceptable and avoids a dependency.
const ATTEMPTS = new Map()
const MAX_ATTEMPTS = 10
const WINDOW_MS = 15 * 60 * 1000
const MAX_LOCKOUT_MS = 15 * 60 * 1000

function throttleKeys(catalogSlug, slug, req) {
  const address = (req.headers['x-forwarded-for'] || '').split(',')[0].trim()
    || req.socket.remoteAddress || 'unknown'
  return [`d:${catalogSlug}/${slug}`, `a:${address}|${catalogSlug}/${slug}`]
}

function retryAfter(keys) {
  const now = Date.now()
  let wait = 0
  for (const key of keys) {
    const state = ATTEMPTS.get(key)
    if (state && state.until > now) wait = Math.max(wait, Math.ceil((state.until - now) / 1000))
  }
  return wait
}

function recordFailure(keys) {
  const now = Date.now()
  for (const key of keys) {
    const state = ATTEMPTS.get(key) ?? { count: 0, first: now, until: 0 }
    if (now - state.first > WINDOW_MS) { state.count = 0; state.first = now }
    state.count += 1
    if (state.count >= MAX_ATTEMPTS) {
      const over = state.count - MAX_ATTEMPTS
      state.until = now + Math.min(60_000 * 2 ** over, MAX_LOCKOUT_MS)
    }
    ATTEMPTS.set(key, state)
  }
}

function recordSuccess(keys) {
  for (const key of keys) ATTEMPTS.delete(key)
}

function readBody(req) {
  return new Promise((resolve) => {
    let body = ''
    req.on('data', (chunk) => {
      body += chunk
      if (body.length > 8192) { body = body.slice(0, 8192); req.destroy() }
    })
    req.on('end', () => resolve(body))
    req.on('error', () => resolve(''))
  })
}

// ── The SPA ───────────────────────────────────────────────────────────────────

/**
 * Liveness, and as much of readiness as this server can honestly report.
 *
 * Two different questions, answered in one response on purpose:
 *
 *   the STATUS CODE is liveness   - 200 means this process answered
 *   `serving` is readiness        - false when no deployment could resolve
 *
 * It never returns 503, and that is deliberate. The only way to be unready here
 * is a CATALOGS_ROOT that is unset or not mounted, and no restart re-creates a
 * bind mount: a probe that failed on it would restart this container forever
 * while the real fix is one directory on the host. So the condition is reported
 * in a field a dashboard can colour, and the process stays up - still serving
 * the legacy ALBUM_ROOT if one is set.
 *
 * It names no catalog and no deployment. This route is NOT behind the password
 * gate, and "which galleries exist here" is exactly what section 5.3 withholds
 * from an unauthenticated caller. A COUNT says the mount is working without
 * handing anyone a list of slugs to try.
 */
function healthPayload() {
  const rootState = !CATALOGS_ROOT
    ? 'unset'
    : fs.existsSync(CATALOGS_ROOT) ? 'ok' : 'missing'

  // Same staleness rule catalogDirFor() uses, so a polled health check cannot
  // turn into a stat() per second on a file the backend rewrites rarely.
  if (rootState === 'ok' && Date.now() - catalogIndexReadAt > CATALOG_INDEX_MIN_INTERVAL_MS) {
    readCatalogIndex()
  }

  const catalogs = rootState === 'ok' ? Object.keys(catalogIndex).length : 0
  const serving = rootState === 'ok' || Boolean(ALBUM_ROOT)

  return {
    status: serving ? 'ok' : 'degraded',
    serving,
    catalogs_root: rootState,
    // How many catalogs catalogs.json maps. Zero with a mounted root means the
    // backend has not written the index yet - which looks exactly like "every
    // deployment 404s" from outside, and is worth being able to tell apart.
    catalogs,
    legacy_album_root: Boolean(ALBUM_ROOT),
    revision: GALLERY_REVISION,
    // environment | kept | process. 'process' means a restart signs everyone out.
    session_secret: SESSION_SECRET_SOURCE,
    uptime_seconds: Math.round(process.uptime()),
    detail:
      rootState === 'missing'
        ? 'CATALOGS_ROOT is set but is not mounted, so no deployment can resolve.'
        : rootState === 'unset' && !ALBUM_ROOT
          ? 'CATALOGS_ROOT is not set, so this server has nothing to serve.'
          : null,
  }
}

/**
 * Serve a file from the Vite build, falling back to index.html.
 *
 * The build uses a RELATIVE base (vite.config.ts), so index.html asks for
 * ./assets/… and the browser resolves that against whatever prefix the
 * deployment is mounted at — /d/family/summer-2019/, /albums/family/summer-2019/
 * behind nginx, or a bucket prefix in a packed bundle. That one setting is what
 * lets the same build serve all three.
 */
function serveApp(urlPath, res, req = null) {
  const rel = urlPath.replace(/^\/+/, '')
  if (rel && !rel.split('/').some((s) => s.startsWith('.'))) {
    const candidate = path.join(DIST_DIR, rel)
    if (contained(DIST_DIR, candidate) && fs.existsSync(candidate)
        && fs.statSync(candidate).isFile()) {
      return serveFile(candidate, res, { 'Cache-Control': appCacheControl(rel) }, req)
    }
  }
  const index = path.join(DIST_DIR, 'index.html')
  if (!fs.existsSync(index)) return send(res, 404, 'Not found')
  // The shell is always checked: it is the one file whose staleness would pin a
  // browser to an old asset manifest. no-cache rather than no-store, so the
  // check is a 304 when nothing changed. The hashed assets are immutable.
  return serveFile(index, res, { 'Cache-Control': 'no-cache' }, req)
}

// ── Request handling ──────────────────────────────────────────────────────────

const server = http.createServer(async (req, res) => {
  const rawUrl = req.url ?? '/'
  const method = req.method ?? 'GET'
  let urlPath
  try {
    urlPath = decodeURIComponent(rawUrl.split('?')[0])
  } catch {
    return send(res, 400, 'Bad request')
  }
  if (urlPath.includes('\0')) return send(res, 400, 'Bad request')

  // ── /health ────────────────────────────────────────────────────────────────
  // Before everything else, because serveApp() would otherwise answer /health
  // with index.html and a 200 - a check that can never fail, which is worse
  // than no check at all.
  //
  // Reached three ways, all of them the same route: :3001/health directly (a
  // container healthcheck), /gallery-health through the gateway (the UI, see
  // systems/gateway/airlock.conf.template), and inside a packed node bundle,
  // where it is the one route that needs no deployment to exist.
  if (urlPath === '/health') {
    if (method !== 'GET') return send(res, 405, 'Method not allowed')
    return sendJson(res, 200, healthPayload(), { 'Cache-Control': 'no-store' })
  }

  // ── /d/<catalog>/<slug>/… ──────────────────────────────────────────────────
  const dep = urlPath.match(/^\/d\/([^/]+)\/([^/]+)(\/.*)?$/)
  if (dep) {
    const [, catalogSlug, slug, tailRaw] = dep
    const tail = tailRaw ?? ''

    if (!SLUG.test(catalogSlug) || !SLUG.test(slug)) {
      return send(res, 404, 'Not found')
    }

    // A bare /d/<catalog>/<slug> has to become /d/<catalog>/<slug>/ before the
    // page loads, or every relative fetch resolves one level too high. The
    // Location is RELATIVE on purpose: behind nginx the browser is at
    // /albums/<catalog>/<slug> and an absolute /d/… would send it somewhere
    // that does not exist from outside.
    if (tail === '') {
      return send(res, 301, '', 'text/plain', { Location: `${slug}/` })
    }

    // No dot-prefixed component anywhere under a deployment, and no '..'.
    // Checked ONCE, here, before anything decides what kind of request this is
    // - so .auth.json cannot be reached by the asset route, by the SPA
    // fallback, or by a route added later that forgets. A flat 404, not the
    // application shell: a dotfile is not a page.
    if (tail.split('/').some((segment) => segment.startsWith('.'))) {
      return send(res, 404, 'Not found')
    }

    const dir = deploymentDir(catalogSlug, slug)
    if (!dir) {
      // Identical answer for "no such catalog", "no such deployment" and
      // "CATALOGS_ROOT is not configured", so the URL space cannot be walked.
      return send(res, 404, 'Not found')
    }

    const meta = readJson(path.join(dir, 'deployment.json')) ?? {}
    if (meta.enabled === false) return send(res, 404, 'Not found')

    const locked = fs.existsSync(path.join(dir, '.auth.json'))
    const ok = authorised(req, catalogSlug, slug)

    // POST /login
    if (tail === '/login' && method === 'POST') {
      const keys = throttleKeys(catalogSlug, slug, req)
      const wait = retryAfter(keys)
      if (wait > 0) {
        return sendJson(res, 429, { error: 'Too many attempts. Try again shortly.' },
                        { 'Retry-After': String(wait) })
      }
      let password = ''
      try { password = JSON.parse(await readBody(req)).password ?? '' } catch { /* empty */ }
      const stored = readJson(path.join(dir, '.auth.json'))
      if (!stored?.password_hash) {
        return sendJson(res, 200, { ok: true })   // public; nothing to unlock
      }
      if (!verifyPassword(stored.password_hash, password)) {
        recordFailure(keys)
        return sendJson(res, 401, { error: 'That password is not right.' })
      }
      recordSuccess(keys)
      const secure = (req.headers['x-forwarded-proto'] === 'https') ? '; Secure' : ''
      return sendJson(res, 200, { ok: true }, {
        'Set-Cookie': `${cookieName(catalogSlug, slug)}=${makeSessionValue(catalogSlug, slug)}; `
          + `Path=/; Max-Age=${SESSION_TTL_MS / 1000}; HttpOnly; SameSite=Lax${secure}`,
      })
    }

    if (tail === '/logout' && method === 'POST') {
      return sendJson(res, 200, { ok: true }, {
        'Set-Cookie': `${cookieName(catalogSlug, slug)}=; Path=/; Max-Age=0; HttpOnly; SameSite=Lax`,
      })
    }

    if (method !== 'GET') return send(res, 405, 'Method not allowed')

    // deployment.json — the ONE thing a locked deployment answers, and it
    // answers with its name and the fact that it is locked. Not the album list,
    // not a photo count, not a folder name: those are the album list by another
    // route (spec section 5.3).
    if (tail === '/deployment.json') {
      if (locked && !ok) {
        return sendJson(res, 200, {
          slug: meta.slug ?? slug,
          name: meta.name ?? slug,
          requires_password: true,
          locked: true,
        })
      }
      return sendJson(res, 200, { ...meta, requires_password: locked, locked: false })
    }

    if (locked && !ok
        && (tail === '/albums.json' || tail.startsWith('/albums/'))) {
      // In front of the manifest AND the assets, not only the HTML. A gallery
      // that merely looks locked has its images one URL guess away.
      return sendJson(res, 401, { error: 'This gallery needs a password.' })
    }

    if (tail === '/albums.json') {
      const manifest = path.join(dir, 'albums.json')
      if (!fs.existsSync(manifest)) return sendJson(res, 200, [])
      return serveFile(manifest, res, { 'Cache-Control': 'no-cache' }, req)
    }

    if (tail.startsWith('/albums/')) {
      const real = resolveAsset(catalogSlug, slug, tail.slice('/albums/'.length))
      if (!real) return send(res, 404, 'Not found')
      return serveFile(real, res, { 'Cache-Control': mediaCacheControl(real, locked) }, req)
    }

    // Anything else under the deployment is the SPA: its shell, or one of the
    // hashed assets the shell asks for relative to this same prefix.
    return serveApp(tail, res, req)
  }

  // ── Legacy single-gallery mode ─────────────────────────────────────────────
  // ALBUM_ROOT served at the server root, exactly as before deployments existed.
  // Honoured for one release so an existing install does not go dark on upgrade.
  if (ALBUM_ROOT && method === 'GET') {
    if (urlPath === '/albums.json') {
      const manifest = path.join(ALBUM_ROOT, 'albums.json')
      if (fs.existsSync(manifest)) return serveFile(manifest, res)
      try {
        const folders = fs.readdirSync(ALBUM_ROOT, { withFileTypes: true })
          .filter((e) => e.isDirectory() && /^\d+$/.test(e.name))
          .map((e) => e.name).sort()
        return sendJson(res, 200, folders)
      } catch {
        return sendJson(res, 200, [])
      }
    }
    const legacy = urlPath.match(/^\/albums\/(\d+)\/(.+)$/)
    if (legacy) {
      const [, folder, rest] = legacy
      if (rest.split('/').some((s) => s.startsWith('.') )) return send(res, 404, 'Not found')
      const candidate = path.join(ALBUM_ROOT, folder, rest)
      try {
        const real = fs.realpathSync(candidate)
        // ALBUM_ROOT is <catalog>/cache/Albums and its links point up into
        // cache/Images, so cache/ is the containment boundary here too.
        if (containedReal(path.dirname(ALBUM_ROOT), real) && fs.statSync(real).isFile()) {
          return serveFile(real, res, {}, req)
        }
      } catch { /* falls through to 404 */ }
      return send(res, 404, 'Not found')
    }
  }

  if (method !== 'GET') return send(res, 405, 'Method not allowed')
  return serveApp(urlPath, res, req)
})

readCatalogIndex()

server.listen(PORT, () => {
  console.log(`album-gallery listening on http://0.0.0.0:${PORT}`)
  if (CATALOGS_ROOT) {
    console.log(`CATALOGS_ROOT: ${CATALOGS_ROOT}`)
    console.log(`  catalogs known: ${Object.keys(catalogIndex).join(', ') || '(none yet)'}`)
    if (!fs.existsSync(CATALOGS_ROOT)) {
      console.warn(`  WARNING: ${CATALOGS_ROOT} does not exist. No deployment will resolve.`)
    }
  } else {
    console.warn(
      'album-gallery: CATALOGS_ROOT is not set, so /d/<catalog>/<slug>/ serves\n' +
      '  nothing. Set it to the catalogs root, mounted read-only.',
    )
  }
  if (ALBUM_ROOT) console.log(`ALBUM_ROOT (legacy): ${ALBUM_ROOT}`)
})
