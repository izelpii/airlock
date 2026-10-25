"""
Storage routes: named folders that catalogs draw input files from.

Implements documentation/specs/local-storage-spec.md.

A bind mount is fixed when a container is created, so a folder named in the UI
at 3pm cannot be read by a container started at 9am. The answer (spec option A)
is one broad root mounted at startup, with every route a subpath of it:

    host   /Users/izelpi/Pictures/Import        what the user sees
    root   /Users/izelpi        -> /storage     mounted once
    here   /storage/Pictures/Import            what this process can open

Routes are read-only unless an owner marks one "imports may move files"
(read_only = 0). Only a move-mode import ever writes to a route, and only to one
so marked (ingest-move-spec.md section 8.1); see move_refusal().

Deliberately dependency-free - only os and sqlite3. It is imported from the
backend root AND from workflow subprocesses, which have different sys.path
setups and must not drag Flask into a subprocess. Same reason media_types.py and
workflows/lib/stopping.py are built this way.
"""

import os
import sqlite3

# Set by start-podman.sh / backend-pod.yaml. The one fact the deployment and the
# application have to agree on; nothing here infers it.
STORAGE_ROOT_HOST = os.environ.get('STORAGE_ROOT_HOST') or None
STORAGE_ROOT_CONTAINER = os.environ.get('STORAGE_ROOT_CONTAINER', '/storage')

# Roots too broad to hand to the file API: naming one as a route would make
# everything beneath it readable through the catalog endpoints.
FORBIDDEN_ROOTS = {
    '/', '/home', '/Users', '/Volumes', '/mnt', '/media',
    '/etc', '/var', '/tmp', '/app', '/storage', '/catalogs', '/input',
}

# The application database, when nobody passes one. Workflow subprocesses reach
# it the same way catalog.py does, so this adds no new coupling.
DEFAULT_DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'database', 'catalogs.db')


# ── Is the feature deployed at all? ───────────────────────────────────────────

def routes_enabled():
    """
    True when a storage root is mounted.

    Without one the feature is invisible rather than broken: no routes can be
    created, the Storage page says which variable to set, and everything that
    worked before keeps working.
    """
    return bool(STORAGE_ROOT_HOST) and os.path.isdir(STORAGE_ROOT_CONTAINER)


def root_status():
    """What to tell a user when routes cannot be created. None when they can."""
    if not STORAGE_ROOT_HOST:
        return ('No storage root is configured. Set STORAGE_ROOT_HOST on the backend '
                'container (start-podman.sh and backend-pod.yaml both set it) and '
                'restart it.')
    if not os.path.isdir(STORAGE_ROOT_CONTAINER):
        return (f'STORAGE_ROOT_HOST is set to {STORAGE_ROOT_HOST}, but it is not mounted '
                f'at {STORAGE_ROOT_CONTAINER} inside this container. Add '
                f'-v "{STORAGE_ROOT_HOST}":{STORAGE_ROOT_CONTAINER}:ro and restart.')
    return None


# ── Translation ───────────────────────────────────────────────────────────────
# realpath on both sides, so a symlink cannot smuggle a path out of the root -
# the rule _contain() already applies to catalog directories.

def _under(path, root):
    """The realpath of *path* when it sits inside *root*, else None."""
    if not path or not root:
        return None
    real, real_root = os.path.realpath(path), os.path.realpath(root)
    if real == real_root or real.startswith(real_root + os.sep):
        return real
    return None


def to_container_path(host_path):
    """
    The path THIS process can open, or None when it is outside the mounted root.

    None is the whole validation. A route that cannot be translated cannot be
    created, so the failure lands when the user types the folder rather than
    when a workflow later finds nothing in it.

    Note the asymmetry: the host side is checked with realpath against
    STORAGE_ROOT_HOST, but the *result* is built by string substitution, because
    the host path does not exist inside this container and cannot be resolved
    here. That is exactly why the translation is stored (spec 6.1) rather than
    recomputed.
    """
    if not host_path or not STORAGE_ROOT_HOST:
        return None
    real = _under(host_path, STORAGE_ROOT_HOST)
    if real is None:
        # realpath needs the path to exist on THIS side to follow links; when it
        # does not, fall back to a lexical check so a host path can still be
        # validated from inside the container.
        norm = os.path.normpath(host_path)
        root = os.path.normpath(STORAGE_ROOT_HOST)
        if norm != root and not norm.startswith(root + os.sep):
            return None
        real = norm
    rel = os.path.relpath(real, os.path.normpath(STORAGE_ROOT_HOST))
    if rel == os.curdir:
        return STORAGE_ROOT_CONTAINER
    return os.path.normpath(os.path.join(STORAGE_ROOT_CONTAINER, rel))


def to_host_path(container_path):
    """The inverse, for showing a user a path they recognise."""
    if not container_path or not STORAGE_ROOT_HOST:
        return None
    norm = os.path.normpath(container_path)
    root = os.path.normpath(STORAGE_ROOT_CONTAINER)
    if norm == root:
        return STORAGE_ROOT_HOST
    if not norm.startswith(root + os.sep):
        return None
    return os.path.normpath(os.path.join(STORAGE_ROOT_HOST,
                                         os.path.relpath(norm, root)))


def validate_host_path(host_path):
    """
    Why this folder cannot be a storage route, or None when it can.

    Ordered so the message names the first thing that is wrong, and so the
    cheapest checks run first.
    """
    if not host_path:
        return 'A folder is required.'
    if not os.path.isabs(host_path):
        return 'The folder must be an absolute path.'

    norm = os.path.normpath(host_path).rstrip(os.sep) or os.sep
    if norm in FORBIDDEN_ROOTS:
        return (f'{host_path} is too broad to use as a storage route. Choose the folder '
                f'that holds the files, not the drive or home directory above it.')
    if STORAGE_ROOT_HOST and norm == os.path.normpath(STORAGE_ROOT_HOST).rstrip(os.sep):
        return (f'{host_path} is the storage root itself. Choose a folder inside it, '
                f'so a route names something narrower than everything.')

    container = to_container_path(host_path)
    if container is None:
        return (f'{host_path} is outside the storage root ({STORAGE_ROOT_HOST}). '
                f'Only folders inside it can be reached by this server.')
    if not os.path.isdir(container):
        return (f'{host_path} is not a folder this server can see. If it is on an '
                f'external drive, check that it is mounted; on macOS, folders under '
                f'Desktop, Documents and Downloads also need Full Disk Access for '
                f'the container runtime, or they appear empty.')
    return None


# ── Reading routes ────────────────────────────────────────────────────────────

def _connect(db_path=None):
    conn = sqlite3.connect(db_path or DEFAULT_DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def get_route(route_id, db_path=None):
    """
    One route as a dict, or None.

    Never raises: this is called from workflow subprocesses while deciding
    whether to run, and a momentarily locked database must not take an import
    down with it.
    """
    if not route_id:
        return None
    try:
        conn = _connect(db_path)
        try:
            row = conn.execute('SELECT * FROM storage_routes WHERE id = ?',
                               (route_id,)).fetchone()
            return dict(row) if row else None
        finally:
            conn.close()
    except sqlite3.Error:
        return None


def list_routes(db_path=None, catalog_id=None):
    """
    Routes in name order, each with a live `available` flag.

    `catalog_id` restricts to the routes that catalog owns. A route belongs to
    exactly one catalog, so this is ownership rather than a filter over some
    wider set (local-storage-spec.md; see catalog.py _migrate_routes_to_owner).
    """
    try:
        conn = _connect(db_path)
        try:
            if catalog_id is None:
                rows = [dict(r) for r in conn.execute(
                    'SELECT * FROM storage_routes ORDER BY name')]
            else:
                rows = [dict(r) for r in conn.execute(
                    'SELECT * FROM storage_routes WHERE catalog_id = ? ORDER BY name',
                    (catalog_id,))]
        finally:
            conn.close()
    except sqlite3.Error:
        return []
    for r in rows:
        r['available'] = route_available(r)
    return rows


def route_available(route):
    """True when the route's folder is a readable directory RIGHT NOW."""
    if not route:
        return False
    path = route.get('container_path')
    return bool(path) and os.path.isdir(path) and os.access(path, os.R_OK)


def mount_writable(route):
    """True when this process could write to the route's folder at all.

    False when the storage root is mounted read-only (STORAGE_ROOT_READONLY=1),
    or the folder belongs to someone else. Reported beside the flag, so a route
    marked writable on a read-only mount says why a move import refuses it.
    """
    path = (route or {}).get('container_path')
    return bool(path) and os.path.isdir(path) and os.access(path, os.W_OK)


def move_refusal(route):
    """
    Why a move-mode import may not take files out of this route, or None.

    Two conditions, both required: the owner's flag (read_only = 0), and a
    folder this process can actually write. The flag is the decision; the mount
    is the fact.
    """
    if not route:
        return ('A catalog that moves files can only import from a storage route '
                'marked "imports may move files".')
    name = route.get('name') or f'route {route.get("id")}'
    if route.get('read_only', 1):
        return (f'Storage route "{name}" is read-only. This catalog moves files out of '
                f'the folders it imports from, so an owner must mark the route '
                f'"imports may move files" first.')
    if not mount_writable(route):
        return (f'Storage route "{name}" is marked "imports may move files", but this '
                f'server cannot write to {route.get("host_path") or route.get("container_path")}. '
                f'Is the storage root mounted read-only (STORAGE_ROOT_READONLY)?')
    return None


def count_files(route, limit=100000, seconds=2.0):
    """
    Files under a route, for the UI: (count, complete). Never raises.

    Bounded by a file ceiling AND a time budget. The ceiling alone was not
    enough: on oldserver a route on a USB hard drive (/storage/usb1/photos, ~600k
    files) took minutes to reach 100,000, and GET /api/storage-routes - which
    the Catalogs page waits for - took 346 s. `complete` is False when either
    bound stopped the walk; the count is then a floor ("N+ files").
    """
    import time
    if not route_available(route):
        return None, False
    deadline = time.monotonic() + seconds
    n = 0
    try:
        for _root, _dirs, files in os.walk(route['container_path']):
            n += len(files)
            if n >= limit or time.monotonic() > deadline:
                return n, False
    except OSError:
        return None, False
    return n, True


# ── The one resolution entry point ────────────────────────────────────────────

def resolve_source(route_id, subpath='', db_path=None):
    """
    (absolute container path, error). Exactly one of the two is set.

    Used by BOTH the launcher and the workflow scripts, so an unavailable route
    produces the same sentence wherever it is noticed - at enqueue time, or
    minutes later when the queue finally reaches it.
    """
    route = get_route(route_id, db_path)
    if route is None:
        return None, (f'Storage route {route_id} no longer exists. Point this workflow '
                      f'at a route that does.')

    name = route.get('name') or f'route {route_id}'
    if not route_available(route):
        return None, (f'Storage route "{name}" is not available: '
                      f'{route.get("host_path") or route.get("container_path")} cannot be '
                      f'read right now. If it is on an external drive, check that it is '
                      f'mounted and that the folder is mounted into this container.')

    base = route['container_path']
    if not subpath:
        return base, None

    # A subpath is user input and must not escape its route.
    candidate = os.path.normpath(os.path.join(base, subpath.lstrip('/')))
    contained = _under(candidate, base)
    if contained is None:
        return None, (f'"{subpath}" is outside storage route "{name}".')
    if not os.path.isdir(contained):
        return None, (f'"{subpath}" is not a folder inside storage route "{name}".')
    return contained, None
