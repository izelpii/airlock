"""
What a published deployment ships, and the bundle built from it.

The library behind tools/pack_deployment.py (the command line, which makes a
tarball or a staged folder by hand) and behind the Publish to server button
(documentation/specs/deployment-publish-to-server-spec.md, phase 1). One
implementation, so the button cannot ship something the packer would refuse,
or the other way round.

Standard library only, and no import of app.py or the database: the command
line runs it with the host's own python3 (3.10 on homeserver), outside the backend
container.

── Why this reads only files ─────────────────────────────────────────────────
It never opens workflow_results.db. The deployment directory already carries
everything a bundle needs - deployment.json names the albums, albums.json lists
the folders, album.json is the manifest - so the database adds nothing here.

That is also the safe choice. The catalogs directory is shared with the running
backend over a bind mount, and the databases are in WAL mode: opening one from
the host while the container writes has corrupted an import before. A packer
that only reads regular files cannot do that.

The cost is that this cannot republish. Publishing needs the backend, and the
bundle is only as fresh as the last publish - so the age of the manifest is
reported, and max_age turns a stale one into an error.

── The one thing that matters ────────────────────────────────────────────────
A deployment directory is a farm of symlinks two levels deep, and it is 12 KB.
The album it stands for is 39 MB. Every copy here DEREFERENCES: real bytes go
into the bundle, and a link that does not resolve is a hard error rather than a
hole discovered in production.
"""

import json
import os
import shutil
from datetime import datetime, timezone

DEFAULT_IMAGE = 'ghcr.io/OWNER/album-gallery:latest'
PLACEHOLDER_DOMAIN = 'CHANGE-ME.example.com'

# Media a deployment is allowed to contain. Anything else in the directory is a
# mistake worth naming rather than shipping.
EXPECTED_SUFFIXES = {'.jpg', '.jpeg', '.png', '.webp', '.avif', '.gif',
                     '.mp4', '.mov', '.json'}

# The sizes a deployment published before packed_rungs existed ships: the pair
# that reproduces the bundle it would have produced.
DEFAULT_RUNGS = ('1200', 'thumb320')


class PackError(Exception):
    """Something that must stop the pack, phrased for a person."""


# ── helpers ───────────────────────────────────────────────────────────────────

def human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024.0


def read_json(path, what):
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh)
    except FileNotFoundError:
        raise PackError(f'{what} not found at {path}')
    except json.JSONDecodeError as exc:
        raise PackError(f'{what} at {path} is not valid JSON: {exc}')


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    except ValueError:
        return None


def age_of(stamp):
    """(text, seconds) for an ISO timestamp, or (…, None) when unparseable."""
    dt = parse_iso(stamp)
    if dt is None:
        return (str(stamp or 'unknown'), None)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    secs = (datetime.now(timezone.utc) - dt).total_seconds()
    if secs < 90:
        text = 'just now'
    elif secs < 5400:
        text = f'{secs / 60:.0f} minutes ago'
    elif secs < 172800:
        text = f'{secs / 3600:.0f} hours ago'
    else:
        text = f'{secs / 86400:.0f} days ago'
    return (text, secs)


def write(directory, name, text):
    with open(os.path.join(directory, name), 'w', encoding='utf-8') as fh:
        fh.write(text)


def write_if_changed(directory, name, text):
    """write(), but leave the file - and its mtime - alone when it already says
    this. A staged file whose mtime moves is a file rsync sends again."""
    path = os.path.join(directory, name)
    old_mtime = None
    try:
        with open(path, encoding='utf-8') as fh:
            if fh.read() == text:
                return False
        old_mtime = os.stat(path).st_mtime
    except OSError:
        pass
    write(directory, name, text)
    # rsync compares times to the second: a change written within the second
    # of the last one, at the same size, would look unchanged and not be sent.
    if old_mtime is not None and int(os.stat(path).st_mtime) <= int(old_mtime):
        bumped = int(old_mtime) + 1
        os.utime(path, (bumped, bumped))
    return True


def dir_size(path, follow=True):
    total = 0
    for root, _dirs, files in os.walk(path, followlinks=follow):
        for f in files:
            p = os.path.join(root, f)
            try:
                total += os.path.getsize(p) if follow else os.lstat(p).st_size
            except OSError:
                pass
    return total


# ── the copy that matters ─────────────────────────────────────────────────────

def _place(real, dst, link):
    """
    Put `real` at `dst`: a hard link when asked and possible, else a copy.

    A link costs no space and no time, and keeps the file's own mtime, which is
    what lets rsync skip an unchanged photo by size and time instead of reading
    it on both ends. Across filesystems (a link cannot) it is a copy that keeps
    the mtime too. Never a link for a file the packer goes on to rewrite - see
    the caller.
    """
    if link:
        try:
            if os.path.lexists(dst):
                os.remove(dst)
            os.link(real, dst)
            return
        except OSError:
            pass
        shutil.copy2(real, dst)
    else:
        shutil.copyfile(real, dst)


def _up_to_date(real, dst):
    """True when `dst` already holds `real`: the same file (a hard link), or a
    copy with the same size and modification time (what copy2 preserves)."""
    try:
        a, b = os.stat(real), os.stat(dst)
    except OSError:
        return False
    if (a.st_dev, a.st_ino) == (b.st_dev, b.st_ino):
        return True
    return a.st_size == b.st_size and int(a.st_mtime) == int(b.st_mtime)


def copy_dereferenced(src_dir, dst_dir, boundary, keep=None, link=False, sync=None):
    """
    Copy a tree, replacing every symlink with the bytes it points at.

    `boundary` is the directory a resolved path must stay inside - the same rule
    the gallery applies before it will serve a file. A link that escapes it, or
    that resolves to nothing, is a hard error: shipping either would produce a
    bundle whose images 404 on a machine where nobody can debug it.

    `keep` is an optional (rel_dir, filename) -> bool. Files it rejects are not
    copied and not counted, but they ARE still validated first: a dangling link
    is a broken catalog whether or not that particular file was going to ship,
    and a bundle is not the place to discover it.

    `link` hard-links media instead of copying it (staging). Manifests and
    dotfiles are always copied: prune_manifests rewrites the JSON in place and
    .auth.json is chmod-ed, and through a link either would change the
    catalog's own file.

    `sync`, a dict, makes it incremental over an existing `dst_dir`: media
    already there and up to date is left alone, and sync collects 'placed',
    'kept' and 'written' (every path this pass wrote or kept, for refresh_stage() to
    remove the rest) - plus 'manifests', each rewritten manifest's previous
    bytes and times, so an unchanged one can get its mtime back.

    Returns (files_copied, bytes_copied, skipped_files, skipped_bytes,
             [warning, ...]).
    """
    files = 0
    total = 0
    skipped = 0
    skipped_bytes = 0
    warnings = []
    real_boundary = os.path.realpath(boundary)

    for root, dirnames, filenames in os.walk(src_dir, followlinks=True):
        dirnames.sort()
        rel_root = os.path.relpath(root, src_dir)
        out_root = dst_dir if rel_root == '.' else os.path.join(dst_dir, rel_root)
        os.makedirs(out_root, exist_ok=True)

        for name in sorted(filenames):
            src = os.path.join(root, name)
            try:
                real = os.path.realpath(src, strict=True)
            except OSError:
                raise PackError(
                    f'dangling link or unreadable file: {src}\n'
                    f'  Re-publish the deployment and try again; a bundle with a '
                    f'missing file is a 404 nobody can debug at the destination.')

            if os.path.commonpath([real_boundary, real]) != real_boundary:
                raise PackError(
                    f'{src} resolves outside the catalog cache:\n'
                    f'  -> {real}\n'
                    f'  The gallery would refuse to serve this file, so packing '
                    f'it would ship something that cannot work.')

            suffix = os.path.splitext(name)[1].lower()
            if suffix not in EXPECTED_SUFFIXES and not name.startswith('.'):
                warnings.append(f'unexpected file type in the deployment: '
                                f'{os.path.join(rel_root, name)}')

            if keep is not None and not keep(rel_root, name):
                skipped += 1
                skipped_bytes += os.path.getsize(real)
                continue

            dst = os.path.join(out_root, name)
            rewritten = name.endswith('.json') or name.startswith('.')
            if sync is not None:
                sync.setdefault('written', set()).add(os.path.abspath(dst))
                if rewritten:
                    try:
                        with open(dst, 'rb') as fh:
                            old = fh.read()
                        st = os.stat(dst)
                        sync.setdefault('manifests', {})[dst] = (old, st.st_atime_ns, st.st_mtime_ns)
                    except OSError:
                        pass
                elif _up_to_date(real, dst):
                    sync['kept'] = sync.get('kept', 0) + 1
                    files += 1
                    total += os.path.getsize(real)
                    continue
                else:
                    sync['placed'] = sync.get('placed', 0) + 1
            _place(real, dst, link and not rewritten)
            files += 1
            total += os.path.getsize(real)

    return files, total, skipped, skipped_bytes, warnings


def section_photos(album_dir, section):
    """
    A section's photos, and the file they came from.

    Format 1 manifests carry them inline (`photos`, file None). Format 2 moved
    each section's photos into section-<id>.json beside album.json and names it
    in `photos_file`. Raises PackError when that file cannot be read - the index
    would then point the gallery at something the bundle does not have.
    """
    if 'photos_file' not in section:
        return section.get('photos') or [], None
    path = os.path.join(album_dir, os.path.basename(section['photos_file']))
    try:
        with open(path, encoding='utf-8') as fh:
            return json.load(fh).get('photos') or [], path
    except (OSError, ValueError) as exc:
        raise PackError(
            f'cannot read {path}: {exc}\n'
            f'  Publish the deployment again; album.json names this file for '
            f'the photos of section {section.get("id")}.')


def manifest_shipping_list(dep_dir, folders, allowed_rungs):
    """
    What the bundle is allowed to contain, per album folder.

    THE MANIFEST IS THE SHIPPING LIST. A bundle carries exactly what its
    album.json files describe, and nothing else - so a file cannot reach a
    destination where nothing can find it, and a manifest cannot name a file
    that is not there. Both directions matter; only the second used to hold.

    `allowed_rungs` narrows the optional arrays: a `sources` or `thumbnails`
    entry ships only when its rung was selected for this deployment. alt_path,
    poster_path, video_path and section covers always ship - they are what the
    gallery falls back to, and a bundle without them shows nothing.

    Returns {folder: set(basenames)}. Raises PackError when a manifest cannot be
    read: packing a folder whose contents nothing describes is exactly how a
    bundle full of unreachable files gets shipped.
    """
    wanted = {}
    for folder in folders:
        manifest_path = os.path.join(dep_dir, folder, 'album.json')
        try:
            with open(manifest_path, encoding='utf-8') as fh:
                manifest = json.load(fh)
        except (OSError, ValueError) as exc:
            raise PackError(
                f'cannot read the manifest for album "{folder}": {exc}\n'
                f'  Publish the deployment again. Without a manifest there is '
                f'no way to know which files the album actually uses, and a '
                f'bundle guessed from a directory listing is how unreachable '
                f'files get shipped.')

        names = set()

        def add(value):
            if value:
                names.add(os.path.basename(value))

        for section in manifest.get('sections') or []:
            cover = section.get('cover') or {}
            add(cover.get('alt_path'))
            # Format 2's card thumbnail: a square crop, so it ships only when
            # its rung does - the gallery falls back to alt_path without it.
            if 'thumb320' in allowed_rungs:
                add(cover.get('thumb_path'))
            photos, _ = section_photos(os.path.join(dep_dir, folder), section)
            for photo in photos:
                add(photo.get('alt_path'))
                add(photo.get('video_path'))
                add(photo.get('poster_path'))
                for entry in (photo.get('sources') or []):
                    if entry.get('rung') in allowed_rungs:
                        add(entry.get('path'))
                for entry in (photo.get('thumbnails') or []):
                    if entry.get('rung') in allowed_rungs:
                        add(entry.get('path'))
        wanted[folder] = names
    return wanted


def prune_manifests(stage_root, folders, allowed_rungs):
    """
    Drop the sources/thumbnails entries a bundle did not ship, in the STAGED
    copy only. The catalog's own manifest is never modified.

    Not optional. A manifest listing a file the bundle does not contain puts a
    404 inside a srcset - which browsers handle inconsistently and which nobody
    at the destination can debug.
    """
    for folder in folders:
        path = os.path.join(stage_root, folder, 'album.json')
        if not os.path.exists(path):
            continue
        with open(path, encoding='utf-8') as fh:
            manifest = json.load(fh)
        album_dir = os.path.join(stage_root, folder)

        def prune(photos):
            for photo in photos:
                for key in ('sources', 'thumbnails'):
                    entries = photo.get(key)
                    if not entries:
                        continue
                    kept = [e for e in entries if e.get('rung') in allowed_rungs]
                    if kept:
                        photo[key] = kept
                    else:
                        photo.pop(key, None)

        for section in manifest.get('sections') or []:
            cover = section.get('cover') or {}
            if 'thumb320' not in allowed_rungs:
                cover.pop('thumb_path', None)
            photos, section_path = section_photos(album_dir, section)
            prune(photos)
            if section_path:
                with open(section_path, encoding='utf-8') as fh:
                    data = json.load(fh)
                data['photos'] = photos
                with open(section_path, 'w', encoding='utf-8') as fh:
                    json.dump(data, fh, ensure_ascii=False, separators=(',', ':'))
        with open(path, 'w', encoding='utf-8') as fh:
            json.dump(manifest, fh, ensure_ascii=False, indent=2)


# ── rendered files ────────────────────────────────────────────────────────────

def render_compose(image, catalog_slug, slug):
    return f'''# Generated by tools/pack_deployment.py for "{slug}".
#
# The gallery image is PULLED, not shipped: the bundle carries data and
# configuration only, so it stays small and stays valid across gallery upgrades.
# Updating the application is `docker compose pull && docker compose up -d`.
services:
  gallery:
    image: {image}
    environment:
      CATALOGS_ROOT: /catalogs
      # No default on purpose. Unset, the server generates a key per process and
      # every visitor to a password-protected album is signed out on restart;
      # ":?" makes the stack refuse to start rather than start subtly broken.
      ALBUM_SESSION_SECRET: ${{ALBUM_SESSION_SECRET:?set ALBUM_SESSION_SECRET in .env}}
    volumes:
      # Read-only: the gallery never writes to a catalog.
      - ./catalogs:/catalogs:ro
    restart: unless-stopped

  caddy:
    image: caddy:2
    ports:
      - "80:80"
      - "443:443"
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro
      # Certificates live here. Losing this volume means re-issuing them, and
      # Let's Encrypt rate-limits that.
      - caddy_data:/data
      - caddy_config:/config
    depends_on:
      - gallery
    restart: unless-stopped

volumes:
  caddy_data:
  caddy_config:
'''


def render_caddyfile(domain, catalog_slug, slug):
    placeholder = domain == PLACEHOLDER_DOMAIN
    header = ''
    if placeholder:
        header = ('# NO --domain WAS GIVEN. Replace the hostname below with your own\n'
                  '# before starting the stack, or Caddy cannot get a certificate.\n')
    return f'''{header}# TLS is automatic: Caddy obtains and renews a certificate for this name.
# The gallery itself speaks plain HTTP on 3001 and should stay that way - at
# home the airlock gateway terminates TLS, and here Caddy does.
{domain} {{
    # The album lives at its full path so nothing has to rewrite asset URLs.
    # The SPA builds every request from document.baseURI, so this simply works.
    redir / /d/{catalog_slug}/{slug}/ 302
    reverse_proxy gallery:3001
}}
'''


def render_readme(domain, catalog_slug, slug, target, name, stats, locked):
    url = f'https://{domain}/d/{catalog_slug}/{slug}/'
    if target == 'static':
        return f'''# {name} — static bundle

Everything here is plain files. Serve this directory with any web server or
sync it to a bucket; there is no application to run.

    aws s3 sync . s3://YOUR-BUCKET/ --delete

Contents: {stats['files']} files, {human(stats['bytes'])}.

## No password

A file host has nothing to check a password with, so this bundle is **not**
protected{' (the original deployment IS password-protected — this copy is not)' if locked else ''}.
Anyone with the URL can see every photo. Use the Node bundle if that matters.
'''

    return f'''# {name}

A self-contained copy of the "{slug}" album deployment.
{stats['files']} files, {human(stats['bytes'])}.

## Run it

Requires Docker with the Compose plugin, and a DNS record for
**{domain}** pointing at this machine.

    echo "ALBUM_SESSION_SECRET=$(openssl rand -hex 32)" > .env
    docker compose up -d

Then open {url}
(the domain root redirects there).

Watch the first start with `docker compose logs -f` — Caddy fetches a
certificate on the first request, which takes a few seconds.

## The two things that go wrong

**Port 443 must be reachable from the internet.** Caddy obtains the certificate
over 443 itself (the TLS-ALPN challenge), so port 80 can stay closed: on AWS, a
security group allowing 443 is enough. Caddy may log an error about the "HTTP
challenge" first; with port 80 closed that is expected, and it then succeeds
over 443.

**Keep `.env`.** Losing `ALBUM_SESSION_SECRET` signs everyone out; changing it
does the same. It is not the album password{'' if locked else ' (this deployment has none)'}.

## Updating

This bundle is a snapshot; nothing syncs. To refresh it: publish the deployment
again in airlock, re-run the packer, and copy the new tarball over. For a large
album, `rsync -a` over the unpacked `catalogs/` tree is the incremental version.

To update the gallery application without repacking:

    docker compose pull && docker compose up -d
'''


# ── a deployment, read ────────────────────────────────────────────────────────

def inspect(catalogs_root, catalog, deployment):
    """
    Everything the bundle is made from, read from the published folder:
    {catalog_dir_name, catalog_dir, dep_dir, meta, folders, locked, name,
     published, published_secs, allowed_rungs, wanted, keep}.

    `keep(rel_dir, name)` is the shipping list as a filter: manifests and
    dotfiles always, album files only when a manifest names them.
    """
    root = os.path.abspath(catalogs_root)
    index_path = os.path.join(root, 'catalogs.json')
    index = read_json(index_path, 'catalogs.json')
    if catalog not in index:
        raise PackError(
            f'catalog "{catalog}" is not in {index_path}.\n'
            f'  Known: {", ".join(sorted(index)) or "(none)"}')

    catalog_dir_name = index[catalog]
    catalog_dir = os.path.join(root, catalog_dir_name)
    dep_dir = os.path.join(catalog_dir, 'cache', 'Deployments', deployment)
    if not os.path.isdir(dep_dir):
        available = []
        deployments_root = os.path.join(catalog_dir, 'cache', 'Deployments')
        if os.path.isdir(deployments_root):
            available = sorted(os.listdir(deployments_root))
        raise PackError(
            f'no published deployment "{deployment}" in catalog '
            f'"{catalog}".\n'
            f'  Found: {", ".join(available) or "(none published)"}\n'
            f'  Publish it from the Deployments page first.')

    meta = read_json(os.path.join(dep_dir, 'deployment.json'), 'deployment.json')
    folders = read_json(os.path.join(dep_dir, 'albums.json'), 'albums.json')
    published, published_secs = age_of(meta.get('generated_at'))

    # What this deployment is allowed to pack. It comes from deployment.json,
    # written by the backend at publish time, so this still never opens a
    # database.
    allowed_rungs = set(meta.get('packed_rungs') or DEFAULT_RUNGS)
    wanted = manifest_shipping_list(dep_dir, folders, allowed_rungs)

    def keep(rel_dir, name):
        # Always keep the manifests, and always keep dotfiles: .auth.json is in
        # no manifest and is what makes a node bundle's password gate work.
        if name.endswith('.json') or name.startswith('.'):
            return True
        # rel_dir is '.' at the deployment root and '<folder>' inside an album.
        allowed = wanted.get(os.path.basename(rel_dir))
        if allowed is None:
            return True          # not an album folder; nothing to filter by
        return name in allowed

    return {
        'catalog': catalog, 'deployment': deployment,
        'catalog_dir_name': catalog_dir_name, 'catalog_dir': catalog_dir,
        'dep_dir': dep_dir, 'meta': meta, 'folders': folders,
        'locked': bool(meta.get('requires_password')),
        'name': meta.get('name') or deployment,
        'published': published, 'published_secs': published_secs,
        'allowed_rungs': allowed_rungs, 'wanted': wanted, 'keep': keep,
    }


def check_age(info, max_age_minutes):
    """PackError when the deployment was published longer ago than allowed."""
    secs = info['published_secs']
    if max_age_minutes is not None and secs is not None and secs > max_age_minutes * 60:
        raise PackError(
            f'the deployment was published {info["published"]}, over the '
            f'--max-age of {max_age_minutes} minutes.\n'
            f'  Publish it again so the bundle matches the album.')


# ── the bundle, built ─────────────────────────────────────────────────────────

def _album_dest(stage, info):
    return os.path.join(stage, 'catalogs', info['catalog_dir_name'],
                        'cache', 'Deployments', info['deployment'])


def build_node(info, stage, image, domain, link=False, sync=None):
    """
    The node bundle's tree in `stage`: the deployment (dereferenced, filtered,
    pruned), this catalog's index, compose.yml, Caddyfile and README.md.
    Returns {'files', 'bytes', 'skipped', 'skipped_bytes', 'warnings'}.
    """
    dest = _album_dest(stage, info)
    os.makedirs(dest, exist_ok=True)
    boundary = os.path.join(info['catalog_dir'], 'cache')
    files, size, skipped, skipped_bytes, warnings = copy_dereferenced(
        info['dep_dir'], dest, boundary, keep=info['keep'], link=link, sync=sync)
    prune_manifests(dest, info['folders'], info['allowed_rungs'])
    # Only this catalog. The index is the one mapping the server cannot derive,
    # and a bundle without it 404s everything with no clue why.
    put = write_if_changed if sync is not None else write
    put(os.path.join(stage, 'catalogs'), 'catalogs.json',
        json.dumps({info['catalog']: info['catalog_dir_name']}, indent=2))
    auth = os.path.join(dest, '.auth.json')
    if os.path.exists(auth):
        os.chmod(auth, 0o600)
    put(stage, 'compose.yml', render_compose(image, info['catalog'], info['deployment']))
    put(stage, 'Caddyfile', render_caddyfile(domain, info['catalog'], info['deployment']))
    put(stage, 'README.md', render_readme(domain, info['catalog'], info['deployment'],
                                          'node', info['name'],
                                          {'files': files, 'bytes': size}, info['locked']))
    return {'files': files, 'bytes': size, 'skipped': skipped,
            'skipped_bytes': skipped_bytes, 'warnings': warnings}


def build_static(info, stage, domain, spa_dist):
    """The static bundle's tree in `stage` (no server, no password)."""
    if not os.path.isdir(spa_dist):
        raise PackError(
            f'the static target needs the gallery build, and there is '
            f'none at {spa_dist}.\n'
            f'  Run `npm run build` in systems/album-gallery/app first.')
    boundary = os.path.join(info['catalog_dir'], 'cache')
    data = os.path.join(stage, '_data')
    files, size, skipped, skipped_bytes, warnings = copy_dereferenced(
        info['dep_dir'], data, boundary, keep=info['keep'])
    prune_manifests(data, info['folders'], info['allowed_rungs'])
    shutil.copytree(spa_dist, stage, dirs_exist_ok=True)
    os.makedirs(os.path.join(stage, 'albums'), exist_ok=True)
    for folder in info['folders']:
        shutil.move(os.path.join(data, folder), os.path.join(stage, 'albums', folder))
    shutil.move(os.path.join(data, 'albums.json'), os.path.join(stage, 'albums.json'))
    # Never ship the hash to a file host, and never claim a lock the bundle
    # cannot enforce.
    meta_out = dict(info['meta'])
    meta_out['requires_password'] = False
    meta_out['locked'] = False
    with open(os.path.join(stage, 'deployment.json'), 'w', encoding='utf-8') as fh:
        json.dump(meta_out, fh, indent=2)
    shutil.rmtree(data)
    write(stage, 'README.md', render_readme(domain, info['catalog'], info['deployment'],
                                            'static', info['name'],
                                            {'files': files, 'bytes': size}, info['locked']))
    return {'files': files, 'bytes': size, 'skipped': skipped,
            'skipped_bytes': skipped_bytes, 'warnings': warnings}


# ── the stage: kept between publishes ─────────────────────────────────────────

# Where the Publish button keeps a deployment's stage: on the catalog's own
# storage, never /tmp - at 7.2 GB, staging in /tmp filled oldserver's root disk, and
# inside the container /tmp is the overlay (spec 2.2.1, 6.2).
def stage_dir_for(catalog_dir, deployment):
    return os.path.join(catalog_dir, 'cache', 'Publish', deployment, 'stage')


def _links_work(src_file, directory):
    """Whether a hard link from the catalog into `directory` is possible."""
    probe = os.path.join(directory, '.link-probe')
    try:
        if os.path.lexists(probe):
            os.remove(probe)
        os.link(src_file, probe)
        os.remove(probe)
        return True
    except OSError:
        return False


def _first_media(dep_dir):
    for root, _dirs, files in os.walk(dep_dir, followlinks=True):
        for name in files:
            if not (name.endswith('.json') or name.startswith('.')):
                try:
                    return os.path.realpath(os.path.join(root, name), strict=True)
                except OSError:
                    continue
    return None


def refresh_stage(info, stage, image, domain):
    """
    Build or refresh the node bundle in `stage`, incrementally (spec 6.2).

    Media already there and unchanged stays as it is - same file, same mtime -
    so rsync skips it; new or changed media is hard-linked from the catalog (a
    copy keeping its mtime where a link is impossible); files the deployment no
    longer ships leave the stage; a manifest or config file whose content did
    not change keeps its old mtime. The first stage costs one pass; later ones
    touch only what changed.

    Refuses, before writing anything, when links are impossible and the disk
    cannot hold the copies the stage still needs.

    Returns the build_node() counts plus 'placed', 'kept', 'removed' and
    'linked' (True when media is hard-linked).
    """
    os.makedirs(stage, exist_ok=True)
    first = _first_media(info['dep_dir'])
    linked = bool(first) and _links_work(first, stage)
    if not linked:
        need = 0
        for root, _dirs, names in os.walk(info['dep_dir'], followlinks=True):
            rel = os.path.relpath(root, info['dep_dir'])
            out = os.path.join(_album_dest(stage, info), rel) if rel != '.' \
                else _album_dest(stage, info)
            for name in names:
                if not info['keep'](rel, name):
                    continue
                try:
                    real = os.path.realpath(os.path.join(root, name), strict=True)
                except OSError:
                    continue        # copy_dereferenced reports it
                if not _up_to_date(real, os.path.join(out, name)):
                    need += os.path.getsize(real)
        free = shutil.disk_usage(stage).free
        if need * 1.1 > free:
            raise PackError(
                f'the stage needs {human(need)} more and {human(free)} is free at {stage}.\n'
                f'  It cannot hard-link from the catalog here (another filesystem), so it '
                f'copies; free some space or stage on the catalog\'s own disk.')

    sync = {}
    counts = build_node(info, stage, image, domain, link=True, sync=sync)

    # Manifests rewritten with what they already said get their time back.
    for path, (old, atime, mtime) in (sync.get('manifests') or {}).items():
        try:
            with open(path, 'rb') as fh:
                if fh.read() == old:
                    os.utime(path, ns=(atime, mtime))
        except OSError:
            pass

    # What the deployment no longer ships leaves the stage.
    written = sync.get('written', set())
    dest = _album_dest(stage, info)
    removed = 0
    for root, dirs, names in os.walk(dest, topdown=False):
        for name in names:
            path = os.path.abspath(os.path.join(root, name))
            if path not in written:
                os.remove(path)
                removed += 1
        for d in dirs:
            p = os.path.join(root, d)
            if not os.listdir(p):
                os.rmdir(p)

    counts.update({'placed': sync.get('placed', 0), 'kept': sync.get('kept', 0),
                   'removed': removed, 'linked': linked})
    return counts
