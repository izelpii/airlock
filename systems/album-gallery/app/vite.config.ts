import react from '@vitejs/plugin-react'
import fs from 'node:fs'
import path from 'node:path'
import { defineConfig } from 'vite'

// ── Development server ────────────────────────────────────────────────────────
//
// Mirrors the /d/<catalog>/<slug>/… routes that server.mjs serves in production,
// so `npm run dev` shows a real deployment rather than a stand-in.
//
// It does NOT implement the password gate. This binds to localhost, and adding a
// second implementation of the session check is exactly how the two drift into
// disagreeing about who may see what — server.mjs is the one that is deployed
// and the one that is authoritative. A locked deployment therefore opens freely
// here; check the lock against the built server.
//
// There is no /api/dimensions middleware any more. Image sizes are baked into
// album.json at publish time, because a packed bundle on a static host has no
// server to ask (albums-enhancement-spec.md section 4.4).

const CATALOGS_ROOT = process.env.CATALOGS_ROOT
  ? path.resolve(process.env.CATALOGS_ROOT)
  : null

const SLUG = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/

function catalogDir(slug: string): string | null {
  if (!CATALOGS_ROOT || !SLUG.test(slug)) return null
  try {
    const index = JSON.parse(
      fs.readFileSync(path.join(CATALOGS_ROOT, 'catalogs.json'), 'utf8'),
    ) as Record<string, string>
    const dir = index[slug]
    if (typeof dir !== 'string' || dir.includes('/')) return null
    return path.join(CATALOGS_ROOT, dir)
  } catch {
    return null
  }
}

function contentType(filePath: string): string {
  const ext = path.extname(filePath).toLowerCase()
  return ext === '.json' ? 'application/json'
    : ext === '.jpg' || ext === '.jpeg' ? 'image/jpeg'
      : ext === '.png' ? 'image/png'
        : ext === '.webp' ? 'image/webp'
          : 'application/octet-stream'
}

function deploymentAssetsPlugin() {
  return {
    name: 'deployment-assets',
    configureServer(server: import('vite').ViteDevServer) {
      server.middlewares.use((req, res, next) => {
        const url = (req.url ?? '/').split('?')[0]
        const match = url.match(/^\/d\/([^/]+)\/([^/]+)\/(.+)$/)
        if (!match) return next()

        const [, catalogSlug, slug, tail] = match
        const dir = catalogDir(catalogSlug)
        if (!dir || !SLUG.test(slug)) return next()

        const deploymentDir = path.join(dir, 'cache', 'Deployments', slug)
        // Same two rules the production server applies: no dotfile component
        // (.auth.json is the immediate reason) and no escaping the deployment.
        const parts = tail.split('/').filter(Boolean)
        if (parts.some((s) => s.startsWith('.'))) return next()

        const filePath = path.join(deploymentDir, ...parts)
        if (!filePath.startsWith(deploymentDir + path.sep)) return next()
        if (!fs.existsSync(filePath) || !fs.statSync(filePath).isFile()) return next()

        res.setHeader('Content-Type', contentType(filePath))
        fs.createReadStream(filePath).pipe(res)
      })
    },
  }
}

export default defineConfig({
  // RELATIVE asset URLs. The same build is served from /d/family/summer-2019/,
  // from /albums/family/summer-2019/ behind the gateway, and from a bucket
  // prefix in a packed bundle; only relative paths are correct in all three.
  base: './',
  plugins: [react(), deploymentAssetsPlugin()],
  server: {
    port: 3001,
  },
})
