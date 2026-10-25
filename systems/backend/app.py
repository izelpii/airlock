# First, before anything reads a setting: SHOEBOX_* variables still count
# under their AIRLOCK_* names until v5.7. See env_compat.py.
import env_compat  # noqa: F401,E402
from flask import Flask, request, jsonify, g
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import os
import sys
import threading
import importlib.util
import subprocess
import time
import signal
import sqlite3
import hashlib
import logging as _logging
import json as _json
from datetime import datetime as _datetime

from catalog import Catalog
import models
from models import Database, WorkflowModel, ExecutionModel, CatalogDatabase, PeopleModel, PeopleCategoryModel, FaceDetectionModel, EventModel, TagModel, LocationModel, UserSettingsModel  # noqa: F401
import auth
from media_types import (is_video_file, original_kind as _original_kind,
                         VIDEO_EXTENSIONS)
import content_report
import version as airlock_version
import embedding_models as airlock_embedding_models
# The rung registry lives with the code that renders derivatives, so the
# generator and the validator can never disagree about what a rung is.
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workflows', 'lib'))
from utilities import IMAGE_RUNGS, MANDATORY_RUNGS  # noqa: E402
# One way to ask backend-geoloc what is near a coordinate, shared with the
# clustering workflow. See workflows/lib/geocode.py for why this is a module
# and not an HTTP call between the two.
from geocode import nearby as geocode_nearby, GeocodeUnavailable  # noqa: E402
import workflow_defaults
import face_best_match
import workflow_queue_schema
import workflow_scheduler
import workflow_triggers
import storage_routes as storage
import album_templates
import card_decorators
import catalog_layout
import wormhole_transfer
import airlock_license
import slugs

app = Flask(__name__)

# Signing key for cookies and CSRF. Persisted under database/, which is a mounted
# volume, so sessions survive an image rebuild - an unstable key would sign every
# user out on each deploy and read as a broken login (spec 5.3).
app.secret_key = auth.load_secret_key()

# Credentialed requests cannot use a wildcard origin, so the origins are now
# explicit. localhost:3000 is the CRA dev server, which is cross-origin against
# the backend and needs withCredentials to work (spec 5.4).
#
# deploy/airlock-env.sh sets AIRLOCK_ALLOWED_ORIGINS to this list plus the
# address this machine is actually reachable at, so a UI opened by LAN address
# rather than by name is accepted too. An origin that is missing here does NOT
# break the page - the gateway proxies it same-origin and every REST call still
# works - it breaks only the Socket.IO handshake, so the UI stops updating by
# itself and looks like a transport bug. An EMPTY value falls back to the
# default rather than being read as "accept nothing", which would present as
# exactly that same silent failure on every address at once.
# shoebox.lan is the name before the rename (v5.6.4); its origins stay until v5.7.
_DEFAULT_ORIGINS = ('https://airlock.lan,http://airlock.lan,'
                    'https://shoebox.lan,http://shoebox.lan,'
                    'http://localhost:3000,http://localhost:5000')
_ALLOWED_ORIGINS = [o.strip() for o in (
    os.environ.get('AIRLOCK_ALLOWED_ORIGINS', '').strip() or _DEFAULT_ORIGINS
).split(',') if o.strip()]
CORS(app, supports_credentials=True, origins=_ALLOWED_ORIGINS)


def _origin_allowed(origin, environ=None):
    """
    Accept the configured origins, plus whatever address this request was
    actually addressed to.

    THE LIST ALONE WAS THE BUG. engine.io allows the same origin by default,
    working it out per request from X-Forwarded-Proto and Host - and the gateway
    passes both through untouched (`proxy_set_header Host $host`). Handing it an
    explicit list REPLACES that default, so naming airlock.lan silently withdrew
    permission from every other address the same server answers on. A UI opened
    at https://192.168.1.16 then had its Socket.IO handshake rejected while every
    REST call on the identical origin worked, because only Socket.IO checks.

    Deriving the machine's addresses into an environment variable was the wrong
    fix for that: it makes every deployment carry configuration that has to be
    right, and when it is wrong the failure is silent and looks like a hung UI.
    Same-origin is not a configuration question - the request answers it.

    Nothing is loosened here. A request whose Origin equals the host it was sent
    to is by definition same-origin, which is what the library permits by
    default and what the browser would never have applied CORS to in the first
    place. Genuinely cross-origin callers - the CRA dev server on :3000 - still
    have to be in the list.
    """
    if not origin:
        return True                      # not a browser, or same-origin GET
    if origin in _ALLOWED_ORIGINS:
        return True
    if not environ:
        return False
    scheme = environ.get('HTTP_X_FORWARDED_PROTO',
                         environ.get('wsgi.url_scheme', 'http'))
    scheme = scheme.split(',')[0].strip()
    host = (environ.get('HTTP_X_FORWARDED_HOST')
            or environ.get('HTTP_HOST') or '').split(',')[0].strip()
    return bool(host) and origin == f'{scheme}://{host}'


socketio = SocketIO(app, cors_allowed_origins=_origin_allowed,
                    async_mode='threading')

# The application database's schema, BEFORE anything opens it.
#
# Order matters here in a way that is easy to miss: several modules call
# CREATE TABLE IF NOT EXISTS against this file, and the first one to run defines
# the table. catalog.py owns catalog_meta, so its schema must exist before
# Database() or auth.install() touch the file.
if os.environ.get('AIRLOCK_IMPORT_CHECK', '').lower() not in ('1', 'true', 'yes'):
    import catalog as _catalog_module
    _catalog_module.ensure_schema()

# Initialize main database (catalog metadata only — workflows now live per-catalog)
db = Database()

# Authentication. AIRLOCK_AUTH_REQUIRED=0 keeps identity available (login works,
# sessions are issued) while leaving routes reachable - spec phase 1, so the flow
# can be exercised before enforcement is switched on.
_AUTH_ENABLED = os.environ.get('AIRLOCK_AUTH_REQUIRED', '1').lower() not in ('0', 'false', 'no')
auth.install(app, db, enabled=_AUTH_ENABLED)

# Initialize user settings model (uses the same catalogs.db via db)
user_settings_model = UserSettingsModel(db)

# (There was an `active_executions` dict here, declared and never read. It was
# the vestige of a stop feature that was never built; the real one records the
# pid in workflow_executions instead - workflows-review-spec.md section 2.1.)

# Record when this server process started so we can ignore older stale rows
_SERVER_START_TIME = time.strftime('%Y-%m-%d %H:%M:%S', time.gmtime())


def _resolve_path(stored_path: str, target_directory: str) -> str:
    """Return an absolute path for a stored path that may be relative.

    All paths in workflow_results.db are stored relative to the catalog's
    target_directory.  At read-time they must be resolved before any file
    operation (send_file, Image.open, os.path.exists, …).
    """
    if stored_path and not os.path.isabs(stored_path):
        return os.path.join(target_directory, stored_path)
    return stored_path


# Catalogs whose registry row and directory disagree about ingest_mode, and the
# ones already checked. Keyed on (id, target_directory) so re-pointing a catalog
# re-checks. One disk read per catalog per process, not one per path resolution.
_MODE_CHECKED = {}


# ─── Choosing a derivative ────────────────────────────────────────────────────
#
# See documentation/specs/file-sizes-spec.md section 6, and
# album-gallery-enhancement2-spec.md section 5.1, which specified this helper.
#
# One rule, used everywhere, replacing 41 copies of a CASE expression that
# ranked on the `characteristic` text. Text ranking cannot work: TYPE JPG spans
# 489..1801px and the SCALE-1200 band spans 1200..2134px, so some rows in
# the "smaller" band are larger than rows in the "bigger" one. `width` and
# `height` are populated on 100% of rows and are the only correct ranking.

# Image derivatives with the photo's own aspect ratio: mutually substitutable,
# safe to hand a browser as a srcset. Square thumbnails are NOT in this set -
# a square crop chosen for a rows tile renders at the wrong aspect ratio.
#
DISPLAY_RUNGS = ('1200', '1600', '2048', '2560')

# The same set PLUS the full-resolution rung, and used in exactly one place:
# the `sources` array an album manifest carries (_deploy_album).
#
# 'full' is aspect-preserving and interchangeable like the rest, so it would
# read naturally in DISPLAY_RUNGS - but DISPLAY_RUNGS is what this application
# SERVES, and it is also spelled out as a literal rung IN (…) list in twenty
# queries that pick a photo for face work, whose coordinates are all in the
# 1200px space. Widening that set would change nothing (pick_derivative wants
# the smallest derivative at least 1200 wide, which a full-resolution file
# never is while the base exists) and risk something, for a rung that only ever
# needs to reach a PUBLISHED album. So it is added here instead, where the
# manifest is built and nothing else reads it.
MANIFEST_DISPLAY_RUNGS = DISPLAY_RUNGS + ('full',)

# The base. Face detection runs on it and every stored bounding box is in its
# pixel space, so anything that reads coordinates must pin this exact rung
# rather than taking "the best" (spec 2.3, 6.2).
BASE_RUNG = '1200'
BASE_WIDTH = 1200

_DISPLAY_IN = "(" + ",".join("'%s'" % r for r in DISPLAY_RUNGS) + ")"


def _alt_rank(alias=''):
    """
    The SQL form of pick_derivative()'s preference order, as an ORDER BY body.

    Kept identical to the Python by a test that runs both over every file in the
    catalog (spec 11.3): two implementations of one rule will drift, and that
    test is what notices.

      1. the SMALLEST derivative at least BASE_WIDTH wide  - "at least 1200" is
         the requirement, not "as large as possible", so a 2048 rung is not
         served where the base exists;
      2. otherwise the LARGEST there is - some originals are 640px and never
         will be larger. Serving 640px is right; upscaling and failing are both
         wrong.

    `id ASC` last so the order is total: two derivatives of equal width must not
    swap places between queries.
    """
    p = f'{alias}.' if alias else ''
    return (f'({p}width >= {BASE_WIDTH}) DESC, '
            f'CASE WHEN {p}width >= {BASE_WIDTH} THEN {p}width END ASC, '
            f'{p}width DESC, {p}id ASC')


def _alt_display_where(alias=''):
    """WHERE fragment limiting candidates to display rungs."""
    p = f'{alias}.' if alias else ''
    return f'{p}rung IN {_DISPLAY_IN}'


def validate_image_rungs(value):
    """
    A catalog's requested optional rungs, validated.

    Returns (rungs, error). An unknown name is an ERROR, not a silent drop: a
    typo that quietly does nothing looks exactly like a working setting, and the
    user would only find out when the derivatives they asked for never appeared.

    Mandatory rungs are rejected too. They are always generated, and accepting
    one here would put the floor in the extras list where a later write could
    remove it (spec 4.8).
    """
    if value is None:
        return [], None
    if not isinstance(value, list):
        return None, 'image_rungs must be a list of size names'
    known = [r for r in IMAGE_RUNGS if r not in MANDATORY_RUNGS]
    out = []
    for item in value:
        if item in MANDATORY_RUNGS:
            continue          # always generated; harmless to send, never stored
        if item not in known:
            return None, (f'unknown image size {item!r}. '
                          f'Available: {", ".join(sorted(known))}')
        if item not in out:
            out.append(item)
    return out, None


def pick_derivative(rows, target_width=BASE_WIDTH):
    """
    The derivative to serve for one file, chosen by measured pixels.

    `rows` are that file's alternative_versions rows (sqlite3.Row or dict),
    carrying at least `rung` and `width`. Returns the chosen row, or None.

    Candidates are limited by RUNG, not by file extension: after the rung column
    exists there is nothing left to guess, and extensions cannot tell a square
    320px crop from a 320px-wide photo.
    """
    # sqlite3.Row has no .get(), and a dict has no .keys() guarantee about
    # missing columns, so one accessor serves both shapes.
    def field(r, name):
        try:
            return r[name]
        except (KeyError, IndexError):
            return None

    candidates = [r for r in rows if field(r, 'rung') in DISPLAY_RUNGS]
    if not candidates:
        return None

    def width_of(r):
        return field(r, 'width') or 0

    big = [r for r in candidates if width_of(r) >= target_width]
    if big:
        return min(big, key=width_of)
    return max(candidates, key=width_of)


def _mode_conflict(catalog):
    """
    The recorded mode from the catalog DIRECTORY when it contradicts the registry.

    This can only happen if a directory was swapped under a registered catalog -
    but the consequence is bad enough to be worth the check. The two modes read
    file_contents.file_path against different roots, so believing the wrong one
    does not 404: it resolves to whatever file happens to sit at that relative
    path under the other root. Refusing beats guessing (spec section 3.1).
    """
    key = (catalog.id, catalog.target_directory)
    if key in _MODE_CHECKED:
        return _MODE_CHECKED[key]
    declared = None
    try:
        if catalog.target_directory and os.path.exists(
                os.path.join(catalog.target_directory, 'workflow_results.db')):
            recorded = models.read_ingest_mode(catalog.target_directory)
            if recorded != getattr(catalog, 'ingest_mode', 'copy'):
                declared = recorded
                print(f'[catalog {catalog.id}] MODE CONFLICT: the registry says '
                      f'{catalog.ingest_mode!r} but {catalog.target_directory} '
                      f'records {recorded!r}. Refusing to resolve its files.')
    except Exception as exc:
        _logging.getLogger(__name__).warning('mode check failed for catalog %s: %s',
                                             catalog.id, exc)
    _MODE_CHECKED[key] = declared
    return declared


def _original_root(catalog) -> str:
    """
    The root that `file_contents.file_path` is relative to.

    Two roots exist because a reference catalog's originals live outside the
    catalog by definition (file-ingestion-spec.md section 3.2):

      copy mode      -> target_directory, as it always has been
      reference mode -> source_root, the library being indexed

    Derivatives - thumbnails, alternates, proxies, face crops, profile photos,
    location maps - are ALWAYS relative to target_directory and must keep using
    _resolve_path with it directly. Which root applies is a property of the table
    a value came from, so every caller decides explicitly; nothing here guesses
    from the shape of a path, because a wrong guess serves the wrong file rather
    than failing.

    Falls back to target_directory when a reference catalog has no source_root
    yet - a catalog that has been re-attached but not yet pointed at its library
    (section 5.4). Nothing resolves, which is the correct outcome; the paths are
    relative to a root that is not known.
    """
    if _mode_conflict(catalog):
        # A root nothing resolves under. Every read fails and says so in the log,
        # rather than quietly serving files from the wrong library.
        return os.path.join(catalog.target_directory or '/', '__mode_conflict__')
    if getattr(catalog, 'ingest_mode', 'copy') == 'reference':
        return catalog.source_root or catalog.target_directory
    return catalog.target_directory


def _cleanup_stale_running_executions():
    """
    On startup, mark any executions that are still 'running' in the DB as
    'interrupted'.  These were orphaned when the previous server process was
    killed before they could update their own status.

    Except the ones that are not orphaned. Workflow subprocesses are children of
    the gunicorn worker, not of the container: if only the worker was replaced,
    they were reparented and are still running. The stored pid, checked against
    /proc, is what tells the two apart - without it this marks a live import as
    interrupted and the UI repeats that until the import finishes anyway.
    """
    from catalog import Catalog as _Catalog
    try:
        catalogs = _Catalog.get_all()
    except Exception:
        return
    for cat in catalogs:
        target_dir = getattr(cat, 'target_directory', None)
        if not target_dir:
            continue
        db_path = os.path.join(target_dir, 'workflow_results.db')
        if not os.path.exists(db_path):
            continue
        try:
            import sqlite3 as _sq
            conn = _sq.connect(db_path, timeout=10)
            conn.row_factory = _sq.Row
            conn.execute('PRAGMA journal_mode=WAL')
            owner_col = ('owner_instance'
                         if workflow_scheduler.has_owner_column(conn)
                         else 'NULL AS owner_instance')
            rows = conn.execute(
                f"SELECT id, pid, {owner_col} FROM workflow_executions "
                "WHERE status='running'"
            ).fetchall()
            # Same three-way distinction the scheduler's recover() makes, and it
            # has to agree with it: this runs first, and _reap() turns any
            # execution marked 'interrupted' into a failed queue row - so being
            # careless here would undo recover()'s care a moment later.
            stale = []
            for r in rows:
                state = workflow_scheduler.execution_state(
                    r['pid'], r['id'], r['owner_instance'])
                if state == workflow_scheduler.EXEC_ALIVE:
                    print(f'[startup] execution {r["id"]} is still running '
                          f'(pid {r["pid"]}) - left alone')
                elif state == workflow_scheduler.EXEC_FOREIGN:
                    print(f'[startup] execution {r["id"]} belongs to instance '
                          f'{r["owner_instance"]} - left alone')
                else:
                    stale.append(r['id'])
            if stale:
                conn.execute(
                    "UPDATE workflow_executions SET status='interrupted', "
                    'completed_at=CURRENT_TIMESTAMP '
                    'WHERE id IN (%s)' % ','.join('?' * len(stale)), stale)
                # An import killed mid-phase leaves that phase 'running' in the
                # Import Progress report; it did not finish, so say it failed
                # (parallelism.md section 7.3).
                try:
                    conn.execute(
                        "UPDATE import_phases SET status='failed' "
                        "WHERE status IN ('running', 'pending') AND execution_id IN (%s)"
                        % ','.join('?' * len(stale)), stale)
                except _sq.OperationalError:
                    pass          # a catalog that has never run a phased import
            conn.commit()
            conn.close()
        except Exception:
            pass


# ── Startup work that needs writable storage ─────────────────────────────────
#
# AIRLOCK_IMPORT_CHECK=1 skips all of it. The image's smoke test
# (`python3 -c "import app"`) exists to prove the source is present and
# importable; it is not a request to create a database. Without this switch a
# permissions problem on a fresh host arrives as "cannot import app.py", which
# sends the reader looking for a missing module - exactly the afternoon this
# comment is here to save.
if os.environ.get('AIRLOCK_IMPORT_CHECK', '').lower() not in ('1', 'true', 'yes'):
    # ensure_schema() already ran above, before Database() opened the file.
    _cleanup_stale_running_executions()
else:
    print('[startup] AIRLOCK_IMPORT_CHECK: schema and cleanup skipped')

# ---------------------------------------------------------------------------
# Workflow scripts (global — scans the filesystem)
# ---------------------------------------------------------------------------

@app.route('/api/workflows/scripts', methods=['GET'])
def get_workflow_scripts():
    """Get all available Python scripts from the workflows directory"""
    workflows_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workflows')

    try:
        if not os.path.exists(workflows_dir):
            return jsonify([])

        manifest = workflow_defaults.manifest_by_script()
        scripts = []
        for filename in os.listdir(workflows_dir):
            if filename.endswith('.py') and not filename.startswith('__'):
                script_path = f"workflows/{filename}"
                # Prefer the manifest's name/description/type over a
                # title-cased filename, so hand-added workflows start from the
                # same metadata the defaults use (spec 7).
                meta = manifest.get(filename, {})
                scripts.append({
                    'filename': filename,
                    'path': script_path,
                    'name': meta.get('name')
                            or filename.replace('_', ' ').replace('.py', '').title(),
                    'description': meta.get('description', ''),
                    'workflow_type': meta.get('type'),
                    'is_ingestion': bool(meta.get('ingestion')),
                })

        scripts.sort(key=lambda x: x['filename'])
        return jsonify(scripts)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/workflows/defaults', methods=['GET'])
@auth.login_required
def get_workflow_defaults():
    """The default-workflow manifest, so the UI can show what would be created."""
    return jsonify(workflow_defaults.load_manifest())


@app.route('/api/catalogs/<int:catalog_id>/workflows/seed-defaults', methods=['POST'])
@auth.login_required
def seed_catalog_default_workflows(catalog_id):
    """
    Add any missing default workflows to an existing catalog.

    Idempotent on python_script, so this is safe to press twice and safe to run
    against a catalog that already has some of them.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    try:
        catalog_db = CatalogDatabase(catalog.target_directory)
        result = workflow_defaults.seed_default_workflows(catalog_db)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500
    return jsonify(result)


# ---------------------------------------------------------------------------
# Catalog-scoped workflow CRUD
# ---------------------------------------------------------------------------

def _get_catalog_workflow_model(catalog_id: int):
    """Return (catalog, catalog_db, WorkflowModel) or raise a 404 response."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return None, None, None
    catalog_db = CatalogDatabase(catalog.target_directory)
    return catalog, catalog_db, WorkflowModel(catalog_db)


@app.route('/api/catalogs/<int:catalog_id>/workflow-graph', methods=['GET'])
def get_workflow_graph(catalog_id):
    """
    This catalog's workflows as a directed graph, laid out in layers.

    The shape comes from workflow_triggers.DEPENDS_ON - the same map the queue
    uses to decide what may start together - so the picture cannot drift from
    the behaviour. Nothing here re-states the pipeline; it projects the one
    definition onto the workflows this catalog actually has.

    Edges are between WORKFLOW IDS, not scripts. That matters now that a catalog
    can hold several imports, one per storage route: "events depend on an
    import" becomes an edge from each of them, which is the truth.

    `layer` is the longest path from a root, so a node always sits to the right
    of everything it waits for. Computed here rather than in the browser because
    it is a property of the graph, not of the drawing.
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    # A default added to the manifest after this catalog was created would
    # otherwise never appear - and because an unregistered workflow is skipped
    # by every trigger chain, it would never run either, with the drawing below
    # looking complete the whole time. Watermarked in catalog_properties, so a
    # workflow somebody deleted on purpose is not put back. See
    # workflow_defaults.sync_default_workflows.
    workflow_defaults.sync_default_workflows(catalog_db)

    conn = catalog_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM workflows ORDER BY id')]
        # Live state, so the canvas can show what is happening rather than only
        # what could happen.
        queue = {}
        try:
            for q in conn.execute(
                    "SELECT workflow_id, status FROM workflow_queue "
                    "WHERE status IN ('queued','blocked','running') "
                    "ORDER BY id"):
                queue[q['workflow_id']] = q['status']
        except sqlite3.Error:
            pass
    finally:
        conn.close()

    by_script = {}
    nodes = []
    for r in rows:
        script = os.path.basename(r.get('python_script') or '')
        by_script.setdefault(script, []).append(r['id'])
        nodes.append({
            'id': r['id'],
            'name': r.get('name'),
            'script': script,
            'type': r.get('workflow_type'),
            # Enough to enqueue it from the canvas without a second fetch. A
            # route beats a literal path; the backend resolves it and refuses at
            # the click if it is unavailable.
            'source_route_id': r.get('source_route_id'),
            'source_subpath': r.get('source_subpath'),
            'source_directory': r.get('source_directory'),
            'needs_configuration': workflow_defaults.needs_configuration(r),
            'status': queue.get(r['id']),
        })

    edges = []
    for r in rows:
        script = os.path.basename(r.get('python_script') or '')
        for dep_script in sorted(workflow_triggers.DEPENDS_ON.get(script, set())):
            for source_id in by_script.get(dep_script, []):
                edges.append({
                    'from': source_id,
                    'to': r['id'],
                    'reason': workflow_triggers.dependency_reason(script, dep_script),
                })

    # Longest path from a root. Iterated rather than recursed so a cycle - which
    # the map should never contain - cannot blow the stack; it settles instead,
    # and the drawing stays readable.
    incoming = {n['id']: [] for n in nodes}
    for e in edges:
        incoming[e['to']].append(e['from'])
    layer = {n['id']: 0 for n in nodes}
    for _ in range(len(nodes)):
        changed = False
        for n in nodes:
            want = max((layer[src] + 1 for src in incoming[n['id']]), default=0)
            if want > layer[n['id']]:
                layer[n['id']] = want
                changed = True
        if not changed:
            break
    for n in nodes:
        n['layer'] = layer[n['id']]

    return jsonify({
        'nodes': nodes,
        'edges': edges,
        # What a finished workflow sets off, so the canvas can say which nodes
        # are chain starters rather than leaving it to be inferred.
        'raises': {s: list(workflow_triggers.triggers_for_completed_workflow(s))
                   for s in sorted(by_script)
                   if workflow_triggers.triggers_for_completed_workflow(s)},
    })


@app.route('/api/catalogs/<int:catalog_id>/workflows', methods=['GET'])
def get_workflows(catalog_id):
    """Get all workflows for a catalog"""
    catalog, catalog_db, wm = _get_catalog_workflow_model(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    # Same reason as the graph endpoint: a default added after this catalog was
    # created has to arrive on its own, because nothing would report its
    # absence. Watermarked, so a deliberate deletion stays deleted.
    workflow_defaults.sync_default_workflows(catalog_db)
    workflows = wm.get_all()
    # Inject catalog_id so the frontend field stays compatible
    for wf in workflows:
        wf['catalog_id'] = catalog_id
        # Derived once here so the UI and the enqueue guard read the same rule
        # rather than each re-deriving it from filenames (spec 4.3).
        wf['is_ingestion'] = workflow_defaults.is_ingestion_script(
            wf.get('python_script'))
        wf['needs_configuration'] = workflow_defaults.needs_configuration(wf)
        # What the UI shows instead of a raw path, and how it knows a route has
        # gone away. A dangling route id reads as unconfigured, never an error
        # (local-storage-spec.md section 6.4).
        if wf.get('source_route_id'):
            route = storage.get_route(wf['source_route_id'])
            wf['source_route'] = ({'id': route['id'], 'name': route['name'],
                                   'host_path': route['host_path'],
                                   'available': storage.route_available(route)}
                                  if route else None)
            if route is None:
                wf['needs_configuration'] = True
    return jsonify(workflows)


@app.route('/api/catalogs/<int:catalog_id>/workflows', methods=['POST'])
def create_workflow(catalog_id):
    """Create a new workflow inside a catalog"""
    catalog, catalog_db, wm = _get_catalog_workflow_model(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    data = request.json or {}
    required_fields = ['name', 'python_script']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400

    workflow_id = wm.create(
        name=data['name'],
        description=data.get('description', ''),
        source_directory=data.get('source_directory', ''),
        python_script=data['python_script'],
        workflow_type=data.get('workflow_type'),
        source_route_id=data.get('source_route_id') or None,
        source_subpath=(data.get('source_subpath') or '').strip() or None,
    )
    return jsonify({'id': workflow_id, 'message': 'Workflow created successfully'}), 201


@app.route('/api/catalogs/<int:catalog_id>/workflows/<int:workflow_id>', methods=['GET'])
def get_workflow(catalog_id, workflow_id):
    """Get a specific workflow"""
    catalog, catalog_db, wm = _get_catalog_workflow_model(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    workflow = wm.get_by_id(workflow_id)
    if not workflow:
        return jsonify({'error': 'Workflow not found'}), 404
    workflow['catalog_id'] = catalog_id
    return jsonify(workflow)


@app.route('/api/catalogs/<int:catalog_id>/workflows/<int:workflow_id>', methods=['PUT'])
def update_workflow(catalog_id, workflow_id):
    """Update a workflow"""
    catalog, catalog_db, wm = _get_catalog_workflow_model(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    workflow = wm.get_by_id(workflow_id)
    if not workflow:
        return jsonify({'error': 'Workflow not found'}), 404
    data = request.json or {}
    wm.update(workflow_id, **data)
    return jsonify({'message': 'Workflow updated successfully'})


@app.route('/api/catalogs/<int:catalog_id>/workflows/<int:workflow_id>', methods=['DELETE'])
def delete_workflow(catalog_id, workflow_id):
    """Delete a workflow"""
    catalog, catalog_db, wm = _get_catalog_workflow_model(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    workflow = wm.get_by_id(workflow_id)
    if not workflow:
        return jsonify({'error': 'Workflow not found'}), 404
    wm.delete(workflow_id)
    return jsonify({'message': 'Workflow deleted successfully'})


# Workflows that require a --source-dir argument (file-import workflows)
_SOURCE_DIR_WORKFLOWS = set(workflow_triggers.INGEST_SCRIPTS)

def _is_subprocess_workflow(script_path: str) -> bool:
    """
    True if the workflow script runs as its own program rather than in a thread.

    DERIVED FROM WORKFLOW_STOP_MODES, which was already the authoritative
    statement of this. That map's own docstring says so: "'unsafe' - not listed
    here. Unlisted scripts run on the threading path in app.py, and a Python
    thread cannot be killed." A script listed there therefore runs as a
    subprocess by definition, and this used to be a second hand-maintained copy
    of the same set.

    The copies had already drifted once, in the only way they could: two new
    workflows were added to WORKFLOW_STOP_MODES as 'item' and not here, so they
    ran on the threading path - where spec_from_file_location does NOT put
    workflows/ on sys.path, and they failed on `from lib.stopping import ...`
    with "No module named 'lib'" before reaching any of their own code. They
    also advertised a Stop button that could not have worked. One list cannot
    disagree with itself.
    """
    return os.path.basename(script_path) in workflow_scheduler.WORKFLOW_STOP_MODES


# A workflow subprocess is finished when its execution row says one of these.
# 'stopped' is the user's doing and 'interrupted' is a restart's; neither is a
# completion, and only a completion continues a chain.
_TERMINAL_EXEC_STATUSES = ('completed', 'failed', 'interrupted', 'stopped')

# How long a stop waits for the workflow to notice before it is made to stop.
# One item, not one workflow: a CR3 import is a hash, an exiftool subprocess, a
# copy and two thumbnails, so the check at the top of the next iteration can be
# a while away. Overridable for testing.
STOP_GRACE_SECONDS = int(os.environ.get('STOP_GRACE_SECONDS', '90'))
STOP_KILL_AFTER_SECONDS = int(os.environ.get('STOP_KILL_AFTER_SECONDS', '120'))


def _start_stop_watcher(execution_id: int, catalog_db_path: str, pid,
                        workflow_name: str = None):
    """
    Escalate a stop that is not honoured.

    The flag is the mechanism; this is the backstop. A workflow wedged on a
    network call or a very large file never reaches its next check, and a UI
    stuck on "stopping..." forever is worse than a signal.

    Nothing is signalled without confirming the pid is still ours - a pid can be
    recycled, and killing an unrelated process is a far worse outcome than a
    stop that does not take.
    """
    import signal as _signal

    def _still_running():
        try:
            conn = sqlite3.connect(catalog_db_path, timeout=10)
            row = conn.execute('SELECT status FROM workflow_executions WHERE id = ?',
                               (execution_id,)).fetchone()
            conn.close()
            return bool(row) and row[0] == 'running'
        except Exception:
            return False

    def _watch():
        deadline = time.time() + STOP_GRACE_SECONDS
        while time.time() < deadline:
            if not _still_running():
                return                      # stopped cleanly; nothing to do
            time.sleep(2)

        for sig, label, wait in ((_signal.SIGTERM, 'SIGTERM',
                                  STOP_KILL_AFTER_SECONDS - STOP_GRACE_SECONDS),
                                 (_signal.SIGKILL, 'SIGKILL', 5)):
            if not _still_running():
                return
            if not workflow_scheduler.process_is_execution(pid, execution_id):
                # Already gone, or never confirmable (no /proc). Either way the
                # row is lying; correct it rather than signalling blind.
                break
            try:
                os.kill(int(pid), sig)
                print(f'[stop] sent {label} to execution {execution_id} '
                      f'(pid {pid}) after it ignored the stop request')
            except OSError as exc:
                print(f'[stop] could not {label} pid {pid}: {exc}')
                break
            end = time.time() + wait
            while time.time() < end:
                if not _still_running():
                    return
                time.sleep(1)

        try:
            catalog_db = CatalogDatabase(os.path.dirname(catalog_db_path))
            if ExecutionModel(catalog_db).force_stopped(
                    execution_id,
                    f'stopped forcibly - {workflow_name or "the workflow"} did not '
                    f'respond within {STOP_KILL_AFTER_SECONDS}s'):
                print(f'[stop] execution {execution_id} recorded as stopped forcibly')
        except Exception as exc:
            print(f'[stop] could not record the forced stop: {exc}')

    threading.Thread(target=_watch, daemon=True,
                     name=f'stop-watcher-{execution_id}').start()


def _start_subprocess_watcher(execution_id: int, catalog_db_path: str,
                               catalog_id: int, script_name: str = None):
    """
    Poll workflow_executions every 2 s and emit socket progress events so the
    frontend can track an execution that is running in a detached subprocess.
    Stops automatically once the execution reaches a terminal state.

    Also raises the completion trigger for a run started BY HAND. A run started
    from the queue is picked up by the scheduler instead, so this checks first
    and stays out of the way - otherwise an import would fire its chain twice.
    """
    def _watch():
        import sqlite3 as _sqlite3
        last_processed = -1
        while True:
            try:
                conn = _sqlite3.connect(catalog_db_path, timeout=10)
                conn.row_factory = _sqlite3.Row
                row = conn.execute(
                    'SELECT status, processed_files, total_files FROM workflow_executions WHERE id=?',
                    (execution_id,)
                ).fetchone()
                conn.close()
                if row:
                    processed = row['processed_files'] or 0
                    total = row['total_files'] or 0
                    status = row['status']
                    percentage = int(processed / total * 100) if total > 0 else 0

                    if processed != last_processed or status in _TERMINAL_EXEC_STATUSES:
                        socketio.emit('workflow_progress', {
                            'execution_id': execution_id,
                            'processed': processed,
                            'total': total,
                            'percentage': percentage,
                            'status': status,
                        })
                        last_processed = processed

                    if status in _TERMINAL_EXEC_STATUSES:
                        # Only a completion continues the chain. A stopped run
                        # has produced a partial result on purpose, and firing
                        # the chain over it is the failure this whole feature has
                        # to avoid (workflows-review-spec.md section 3.3).
                        if status == 'completed' and script_name:
                            _fire_completion_trigger_if_unqueued(
                                catalog_id, catalog_db_path, execution_id, script_name)
                        break
            except Exception:
                pass
            time.sleep(2)

    t = threading.Thread(target=_watch, daemon=True)
    t.start()


def _fire_completion_trigger_if_unqueued(catalog_id, catalog_db_path,
                                         execution_id, script_name):
    """
    Continue the chain after a hand-started workflow finishes.

    Skipped when a workflow_queue row already points at this execution: the
    scheduler owns those, and firing here as well would enqueue the same chain
    from two places. Coalescing would absorb the duplicate, but relying on that
    to paper over a double-fire hides the real ownership rule.
    """
    triggers = workflow_triggers.triggers_for_completed_workflow(script_name)
    if not triggers:
        return

    # Re-read the status rather than trusting the caller. Every caller today
    # checks first, but this is the function that turns "a workflow finished"
    # into "run everything downstream", and firing it over a stopped run means
    # recognising faces on a half-imported catalog. A second read is cheap
    # insurance against the one bug in this feature that produces bad data
    # rather than a bad message (workflows-review-spec.md section 3.3).
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=60)
        status_row = conn.execute(
            'SELECT status FROM workflow_executions WHERE id = ?',
            (execution_id,)).fetchone()
        conn.close()
        if not status_row or status_row[0] != 'completed':
            return
    except Exception:
        return

    try:
        conn = sqlite3.connect(catalog_db_path, timeout=60)
        row = conn.execute(
            'SELECT 1 FROM workflow_queue WHERE execution_id = ?',
            (execution_id,)).fetchone()
        conn.close()
        if row:
            return
    except Exception:
        # No queue table yet, or it could not be read - treat as unqueued.
        pass

    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return
    for trigger in triggers:
        # One failing trigger must not swallow the next: the try is inside the
        # loop, not around it.
        try:
            results = workflow_triggers.fire(catalog, trigger,
                                             detail={'after': script_name})
            if results:
                print(f'[queue] {script_name} finished by hand -> {trigger} '
                      f'enqueued {len(results)} workflow(s)')
                _emit_queue(catalog)
        except Exception as e:
            print(f'[queue] completion trigger {trigger} failed: {e}')


def start_workflow_execution(catalog, workflow, source_directory=None):
    """
    Launch one workflow and return its execution id.

    Extracted from execute_workflow so the HTTP endpoint and the queue runner
    share one launch path. Two copies of the subprocess-versus-thread dispatch
    would drift, and the drift would only show up as a workflow that behaves
    differently depending on who started it.
    """
    # A storage route wins over the literal source_directory when one is set:
    # the route is the intent, the text is its cached rendering
    # (local-storage-spec.md section 6.4).
    #
    # Resolved BEFORE the execution row is created, so an unavailable route
    # raises out of here and _admit() records the queue item as failed with this
    # message - rather than leaving a 'running' execution nothing will finish.
    route_id = workflow.get('source_route_id')
    if route_id and not source_directory:
        resolved, route_err = storage.resolve_source(
            route_id, workflow.get('source_subpath') or '')
        if route_err:
            raise RuntimeError(route_err)
        source_directory = resolved

    catalog_db = CatalogDatabase(catalog.target_directory)
    execution_model = ExecutionModel(catalog_db)
    execution_id = execution_model.create(
        workflow['id'], workflow['name'],
        owner_instance=workflow_scheduler.instance_id())

    script_path = workflow['python_script']
    if not os.path.isabs(script_path):
        app_dir = os.path.dirname(os.path.abspath(__file__))
        script_path = os.path.join(app_dir, script_path)

    if _is_subprocess_workflow(script_path):
        # -u: the log is a pipe, so without it a workflow's lines arrive in
        # 8 KB blocks - minutes apart, or all at the end - and a slow phase
        # reads as a hung one.
        cmd = [sys.executable, '-u', script_path,
               '--execution-id', str(execution_id),
               '--target-dir', catalog.target_directory,
               '--catalog-id', str(catalog.id)]
        if os.path.basename(script_path) in _SOURCE_DIR_WORKFLOWS:
            cmd += ['--source-dir', source_directory or workflow.get('source_directory') or '']
            # The script re-checks the route itself before walking. A queue item
            # can wait minutes behind other work, and a drive unplugged in
            # between would otherwise produce an import of zero files that
            # reports success (spec section 6.5.2).
            if route_id:
                cmd += ['--route-id', str(route_id)]
        proc = subprocess.Popen(cmd, close_fds=True)
        # The pid is the only handle anything has on a running workflow. It is
        # stored, not held: the backend may be replaced while the workflow keeps
        # going, and a value in the database survives that where an object in
        # memory does not.
        execution_model.set_pid(execution_id, proc.pid)
        _start_subprocess_watcher(execution_id, catalog_db.db_path, catalog.id,
                                  script_name=os.path.basename(script_path))
    else:
        workflow_run = dict(workflow)
        workflow_run['catalog_id'] = catalog.id
        workflow_run['source_directory'] = source_directory or workflow.get('source_directory')
        thread = threading.Thread(target=run_workflow,
                                  args=(execution_id, workflow_run, catalog))
        thread.daemon = True
        thread.start()

    return execution_id


@app.route('/api/catalogs/<int:catalog_id>/workflows/<int:workflow_id>/execute', methods=['POST'])
def execute_workflow(catalog_id, workflow_id):
    """Execute a workflow"""
    catalog, catalog_db, wm = _get_catalog_workflow_model(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    workflow = wm.get_by_id(workflow_id)
    if not workflow:
        return jsonify({'error': 'Workflow not found'}), 404

    data = request.json or {}
    override_source_directory = data.get('source_directory', workflow.get('source_directory'))

    # An ingestion workflow with no source directory walks nothing and reports
    # success — a run that appears in the history as completed, having imported
    # zero files, with no error explaining why. Refuse it, naming the field
    # (workflows-default-spec.md 4.2). Defaults are created unconfigured on
    # purpose, so this is the common case on a new catalog, not an edge case.
    if workflow_defaults.is_ingestion_script(workflow.get('python_script')) \
            and not (override_source_directory or '').strip():
        return jsonify({
            'error': 'This import workflow has no source directory yet. '
                     'Edit the workflow and set Source Directory to the folder '
                     'to import from.',
            'code': 'needs_configuration',
        }), 400

    # target_directory is always taken from the catalog — never overridden by the client
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    workflow_run = dict(workflow)
    workflow_run['catalog_id'] = catalog_id
    workflow_run['source_directory'] = override_source_directory

    execution_id = start_workflow_execution(
        catalog, dict(workflow), override_source_directory)

    return jsonify({
        'execution_id': execution_id,
        'message': 'Workflow execution started'
    }), 202

@app.route('/api/catalogs/<int:catalog_id>/executions/<int:execution_id>', methods=['GET'])
def get_execution(catalog_id, execution_id):
    """Get execution status from catalog-specific database"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    execution_model = ExecutionModel(catalog_db)
    execution = execution_model.get_by_id(execution_id)
    
    if not execution:
        return jsonify({'error': 'Execution not found'}), 404
    return jsonify(execution)

@app.route('/api/catalogs/<int:catalog_id>/executions/<int:execution_id>/files', methods=['GET'])
def get_execution_files(catalog_id, execution_id):
    """Get file operations for a specific workflow execution"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    execution_model = ExecutionModel(catalog_db)
    execution = execution_model.get_by_id(execution_id)
    
    if not execution:
        return jsonify({'error': 'Execution not found'}), 404
    
    # Get file operations for this execution
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    
    # Query file operations linked to this execution
    cursor.execute('''
        SELECT
            fo.id,
            fo.execution_id,
            fo.file_id,
            fo.operation_type,
            fo.source_path,
            fo.target_path,
            fo.file_size,
            fo.status,
            fo.details,
            fo.operation_date,
            fc.file_name,
            fc.file_type,
            fc.file_permissions
        FROM file_operations fo
        LEFT JOIN file_contents fc ON fo.file_id = fc.id
        WHERE fo.execution_id = ?
        ORDER BY fo.operation_date DESC
    ''', (execution_id,))
    
    rows = cursor.fetchall()
    conn.close()
    
    # Convert rows to dictionaries
    operations = []
    for row in rows:
        op_dict = dict(row)
        operations.append(op_dict)

    return jsonify(operations)


# ─── Import Progress report (documentation/features/parallelism.md §8.4) ─────
#
# Read-only, and cheap on purpose: a live page polls the detail endpoint every
# two seconds, so it reads the five import_phases rows the import already keeps
# up to date and never counts import_items while a run is going.

# Both import scripts. Matching only the first hid every "Import Files" run
# (v2) from the Import Progress page until it had written phase rows, and the
# page showed an older, interrupted import in its place (homeserver, 2026-10-12).
_IMPORT_SCRIPTS = ('io_picture_import_workflow.py', 'io_picture_import_v2_workflow.py')
_IMPORT_PHASES = ('scan', 'probe', 'resolve', 'place', 'render')
# Mirrors IMPORT_PROGRESS_FLUSH_SECONDS in the workflow: how often it writes.
_IMPORT_FLUSH_SECONDS = float(os.environ.get('IMPORT_PROGRESS_FLUSH_SECONDS', '2') or 2)


def _parse_ts(value):
    if not value:
        return None
    try:
        return _datetime.fromisoformat(str(value).replace('Z', ''))
    except ValueError:
        return None


def _phase_seconds(row, now):
    started = _parse_ts(row['started_at'])
    if not started:
        return None
    ended = _parse_ts(row['finished_at']) or now
    return max(0.0, (ended - started).total_seconds())


def _import_history_rates(conn, before_id):
    """Median items/s per phase over the last five finished imports - for ETA."""
    rates = {}
    rows = conn.execute(
        '''SELECT p.phase, p.done + p.failed + p.skipped AS handled,
                  p.started_at, p.finished_at
             FROM import_phases p
            WHERE p.status = 'completed' AND p.execution_id IN (
                  SELECT execution_id FROM import_phases
                   WHERE phase = 'render' AND status = 'completed' AND execution_id < ?
                   ORDER BY execution_id DESC LIMIT 5)''', (before_id,)).fetchall()
    for r in rows:
        secs = _phase_seconds(r, _datetime.now())
        if secs and r['handled']:
            rates.setdefault(r['phase'], []).append(r['handled'] / secs)
    return {p: sorted(v)[len(v) // 2] for p, v in rates.items()}


@app.route('/api/catalogs/<int:catalog_id>/imports', methods=['GET'])
def list_imports(catalog_id):
    """Recent picture imports, newest first, for the report's selector."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    try:
        limit = max(1, min(100, int(request.args.get('limit', 20))))
    except ValueError:
        return jsonify({'error': 'limit must be an integer'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    try:
        rows = conn.execute(
            '''SELECT e.id, e.status, e.started_at, e.completed_at, e.total_files,
                      e.processed_files, e.source_directory
                 FROM workflow_executions e
                 LEFT JOIN workflows w ON w.id = e.workflow_id
                WHERE w.python_script LIKE ? OR w.python_script LIKE ?
                   OR EXISTS (SELECT 1 FROM import_phases p WHERE p.execution_id = e.id)
                ORDER BY e.id DESC LIMIT ?''',
            (*(f'%{s}' for s in _IMPORT_SCRIPTS), limit)).fetchall()
        running = {}
        if rows:
            marks = ','.join('?' * len(rows))
            for r in conn.execute(
                    f"SELECT execution_id, phase FROM import_phases "
                    f"WHERE status = 'running' AND execution_id IN ({marks})",
                    [r['id'] for r in rows]):
                running[r['execution_id']] = r['phase']
    finally:
        conn.close()

    imports = []
    for r in rows:
        total = r['total_files'] or 0
        imports.append({
            'execution_id': r['id'],
            'status': r['status'],
            'started_at': r['started_at'],
            'completed_at': r['completed_at'],
            'source_directory': r['source_directory'],
            'total_files': total,
            # Executions from before the phased import have no phase rows.
            'current_phase': running.get(r['id']),
            'overall_percent': round(100.0 * (r['processed_files'] or 0) / total, 1)
                               if total else (100.0 if r['status'] == 'completed' else 0.0),
        })
    return jsonify({'imports': imports})


@app.route('/api/catalogs/<int:catalog_id>/imports/<int:execution_id>/progress',
           methods=['GET'])
def get_import_progress(catalog_id, execution_id):
    """Per-phase progress, ETA and recent failures for one import."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    try:
        execution = conn.execute(
            'SELECT id, status, started_at, completed_at, total_files, processed_files, '
            'stop_requested, source_directory FROM workflow_executions WHERE id = ?',
            (execution_id,)).fetchone()
        if not execution:
            return jsonify({'error': 'Execution not found'}), 404
        phase_rows = {r['phase']: r for r in conn.execute(
            'SELECT * FROM import_phases WHERE execution_id = ?', (execution_id,))}
        running = execution['status'] == 'running'
        history = _import_history_rates(conn, execution_id) if running else {}
        failures = conn.execute(
            '''SELECT state, source_path, phase_error, updated_at FROM import_items
                WHERE execution_id = ? AND state IN
                      ('probe_failed', 'place_failed', 'render_failed')
                ORDER BY updated_at DESC, id DESC LIMIT 20''', (execution_id,)).fetchall()
        state_counts = None
        if not running or request.args.get('counts') == '1':
            state_counts = {r[0]: r[1] for r in conn.execute(
                'SELECT state, COUNT(*) FROM import_items WHERE execution_id = ? '
                'GROUP BY state', (execution_id,))}
    finally:
        conn.close()

    now = _datetime.now()
    stale_after = 3 * _IMPORT_FLUSH_SECONDS + 10
    phases = []
    eta_total = 0.0
    eta_known = running
    running_total = None
    for name in _IMPORT_PHASES:
        r = phase_rows.get(name)
        if r is None:
            phases.append({'phase': name, 'status': 'pending'})
            continue
        item = {k: r[k] for k in ('status', 'total', 'done', 'failed', 'skipped',
                                  'bytes_done', 'workers', 'started_at', 'finished_at',
                                  'rate_per_sec')}
        item['phase'] = name
        item['duration_seconds'] = _phase_seconds(r, now)
        try:
            item['detail'] = _json.loads(r['detail_json']) if r['detail_json'] else {}
        except ValueError:
            item['detail'] = {}
        handled = (r['done'] or 0) + (r['failed'] or 0) + (r['skipped'] or 0)
        item['eta_seconds'] = None
        item['stale'] = False
        if r['status'] == 'running':
            running_total = r['total']
            updated = _parse_ts(r['updated_at'])
            item['stale'] = bool(updated and (now - updated).total_seconds() > stale_after)
            if r['total'] is not None and r['rate_per_sec']:
                item['eta_seconds'] = max(0, int((r['total'] - handled) / r['rate_per_sec']))
                eta_total += item['eta_seconds']
            else:
                eta_known = False
        elif r['status'] == 'pending' and running:
            # A phase that has not started yet: its likely size is the running
            # phase's, and its speed is what it managed in recent imports.
            rate = history.get(name)
            if rate and running_total:
                eta_total += running_total / rate
            else:
                eta_known = False
        phases.append(item)

    started = _parse_ts(execution['started_at'])
    total = execution['total_files'] or 0
    return jsonify({
        'execution_id': execution['id'],
        'status': execution['status'],
        'stop_requested': bool(execution['stop_requested']),
        'source_directory': execution['source_directory'],
        'started_at': execution['started_at'],
        'completed_at': execution['completed_at'],
        'total_files': total,
        'overall_percent': round(100.0 * (execution['processed_files'] or 0) / total, 1)
                           if total else 0.0,
        # started_at is SQLite's CURRENT_TIMESTAMP, which is UTC.
        'elapsed_seconds': int(((_parse_ts(execution['completed_at']) or _datetime.utcnow())
                                - started).total_seconds()) if started else None,
        'eta_seconds': int(eta_total) if (running and eta_known) else None,
        'has_phases': bool(phase_rows),
        'phases': phases,
        'state_counts': state_counts,
        'recent_failures': [
            {'phase': f['state'].split('_')[0], 'source_path': f['source_path'],
             'error': f['phase_error'], 'at': f['updated_at']} for f in failures],
    })


@app.route('/api/catalogs/<int:catalog_id>/executions/running', methods=['GET'])
def get_running_executions(catalog_id):
    """
    Return executions currently in 'running' state for a catalog that were
    started in this server session (started_at >= _SERVER_START_TIME).
    This prevents orphaned rows from previous server sessions from showing up.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    rows = conn.execute(
        '''SELECT id, workflow_id, workflow_name, status,
                  processed_files, total_files, started_at
           FROM workflow_executions
           WHERE status = ? AND started_at >= ?
           ORDER BY started_at DESC''',
        ('running', _SERVER_START_TIME)
    ).fetchall()
    conn.close()

    executions = []
    for row in rows:
        e = dict(row)
        total = e.get('total_files') or 0
        processed = e.get('processed_files') or 0
        e['percentage'] = int(processed / total * 100) if total > 0 else 0
        executions.append(e)

    return jsonify(executions)


@app.route('/api/catalogs/<int:catalog_id>/executions/ingestions', methods=['GET'])
def get_catalog_ingestions(catalog_id):
    """
    Ingestion runs with their file counts, newest first, for the report's top
    row of cards.

    Paged rather than complete: the row shows one screen's worth and steps
    back through the rest, so there is no reason to send 90 runs to draw 6.
    ?limit= (1-48) and ?offset= ; the reply carries `total` so the caller can
    size the control without a second request.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    try:
        limit = max(1, min(48, int(request.args.get('limit', 12))))
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        return jsonify({'error': 'limit and offset must be whole numbers'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    return jsonify(ExecutionModel(catalog_db).get_ingestions(limit=limit, offset=offset))


@app.route('/api/catalogs/<int:catalog_id>/executions', methods=['GET'])
def get_catalog_executions(catalog_id):
    """Get all executions for a catalog"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    execution_model = ExecutionModel(catalog_db)
    executions = execution_model.get_all()
    return jsonify(executions)

# Catalog API endpoints
def _owner_names(user_ids):
    """
    {user_id: display name} for a set of catalog owners, in one query.

    Resolved here rather than in Catalog.to_dict() so listing N catalogs stays
    one lookup instead of N. The stored value is the id; the name is a rendering
    of it, and it follows the user if they are renamed - which the free-text
    column it replaced could not do.
    """
    ids = [i for i in {u for u in user_ids if u}]
    if not ids:
        return {}
    conn = db.get_connection()
    try:
        rows = conn.execute(
            'SELECT id, username, display_name FROM users WHERE id IN (%s)'
            % ','.join('?' * len(ids)), ids).fetchall()
    finally:
        conn.close()
    return {r['id']: (r['display_name'] or r['username']) for r in rows}


def _with_owner(payloads):
    """Add `owner` (a display name) beside `owner_user_id` on catalog payloads."""
    names = _owner_names(p.get('owner_user_id') for p in payloads)
    for p in payloads:
        p['owner'] = names.get(p.get('owner_user_id'))
    return payloads


# ─── Storage routes ───────────────────────────────────────────────────────────
# Named folders catalogs draw input files from. See
# documentation/specs/local-storage-spec.md.

def _route_payload(route, catalog_names=None, counts=True):
    """
    One route, with the catalog that owns it.

    A route belongs to exactly one catalog, so this reports an owner rather
    than the list of catalogs a shared route used to feed.

    counts=False skips walking the folder (file_count None): the count is the
    expensive part of a route, and only the Storage cards show it.
    """
    d = dict(route)
    d['available'] = storage.route_available(route)
    # "Imports may move files" (ingest-move-spec.md 8.1): the owner's flag, and
    # whether this server could act on it at all.
    d['allow_move'] = not route.get('read_only', 1)
    d['mount_writable'] = d['available'] and storage.mount_writable(route)
    if counts and d['available']:
        d['file_count'], d['file_count_complete'] = storage.count_files(route)
    else:
        d['file_count'], d['file_count_complete'] = None, False
    if catalog_names is not None:
        d['catalog_name'] = catalog_names.get(route.get('catalog_id'))
    return d


def _catalog_names():
    """{catalog_id: name} — one query, so a route list needs no join per row."""
    conn = db.get_connection()
    try:
        return {r['id']: r['name']
                for r in conn.execute('SELECT id, name FROM catalog_meta')}
    finally:
        conn.close()


def _workflows_using_route(route):
    """
    The workflows in this route's catalog that read from it.

    Only that catalog: a route belongs to exactly one, so the cross-catalog scan
    this used to do would now be looking for something that cannot be there.
    Workflows still live in the catalog's own database while routes live in the
    application one, so the reference crosses two files and no query can span
    them (local-storage-spec.md section 6.4).

    Returns [{'id', 'name', 'busy'}], where `busy` means the workflow is queued
    or running right now.
    """
    catalog = Catalog.get_by_id(route.get('catalog_id')) if route else None
    if not catalog or not catalog.target_directory:
        return []
    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                'SELECT id, name FROM workflows WHERE source_route_id = ?',
                (route['id'],)).fetchall()
            out = []
            for r in rows:
                busy = conn.execute(
                    "SELECT 1 FROM workflow_queue WHERE workflow_id = ? "
                    "AND status IN ('queued', 'blocked', 'running')",
                    (r['id'],)).fetchone()
                out.append({'id': r['id'], 'name': r['name'], 'busy': bool(busy)})
            return out
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _delete_workflows(catalog, workflow_ids):
    """
    Remove workflows and the queue rows that reference them.

    Written out because foreign keys are inert in this application: the
    ON DELETE CASCADE on workflow_queue.workflow_id would not run, leaving queue
    rows pointing at a workflow that no longer exists.

    Executions are deliberately KEPT. They are history - what ran, when, and how
    many files it touched - and history should not vanish because the thing that
    produced it was removed.
    """
    if not workflow_ids:
        return 0
    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    marks = ','.join('?' * len(workflow_ids))
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        conn.execute(f'DELETE FROM workflow_queue WHERE workflow_id IN ({marks})',
                     workflow_ids)
        cur = conn.execute(f'DELETE FROM workflows WHERE id IN ({marks})',
                           workflow_ids)
        conn.commit()
        return cur.rowcount or 0
    finally:
        conn.close()


@app.route('/api/storage-routes', methods=['GET'])
@auth.login_required
def list_storage_routes():
    """
    Every route on this installation, each naming the catalog that owns it.

    Without file counts unless asked (?counts=1): the Catalogs page lists these
    only to say which catalog reads which folder, and counting walked every
    route's folder first - 346 s on oldserver, so the page never loaded.
    """
    names = _catalog_names()
    counts = request.args.get('counts', '').lower() in ('1', 'true', 'yes')
    return jsonify({
        'enabled': storage.routes_enabled(),
        'message': storage.root_status(),
        'storage_root_host': storage.STORAGE_ROOT_HOST,
        'storage_root_container': storage.STORAGE_ROOT_CONTAINER,
        'routes': [_route_payload(r, names, counts=counts) for r in storage.list_routes()],
    })


@app.route('/api/catalogs/<int:catalog_id>/storage-routes', methods=['POST'])
@auth.admin_required
def create_storage_route(catalog_id):
    """
    Create a storage route owned by this catalog.

    The catalog comes from the URL, not the body: a route is created inside a
    catalog and belongs to it from that moment. There is no separate step that
    attaches it, and no way to attach it to a second catalog - that is the
    concept this replaced.
    """
    if not storage.routes_enabled():
        return jsonify({'error': storage.root_status()}), 400

    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    host_path = (data.get('host_path') or '').strip()
    if not name:
        return jsonify({'error': 'A name is required.'}), 400

    err = storage.validate_host_path(host_path)
    if err:
        return jsonify({'error': err}), 400
    # The same overlap rule reference catalogs already use: a route must not
    # contain, or sit inside, a catalog's own directory.
    err = _validate_source_root(host_path)
    if err:
        return jsonify({'error': err}), 400

    container_path = storage.to_container_path(host_path)
    user = auth.current_user()
    conn = db.get_connection()
    try:
        cur = conn.execute(
            'INSERT INTO storage_routes '
            '(catalog_id, name, kind, host_path, container_path, read_only, created_by) '
            'VALUES (?, ?, ?, ?, ?, ?, ?)',
            (catalog_id, name, (data.get('kind') or 'local').strip(),
             os.path.normpath(host_path), container_path,
             0 if data.get('allow_move') else 1,
             user['id'] if user else None))
        conn.commit()
        route_id = cur.lastrowid
    except sqlite3.IntegrityError:
        # Unique per catalog now, so this only fires for a clash inside THIS
        # catalog - another catalog may hold the same name.
        return jsonify({'error': f'This catalog already has a storage route '
                                 f'named "{name}".'}), 409
    finally:
        conn.close()

    # ── The import workflow that goes with it ────────────────────────────
    # A route exists to be imported from, and every route in this installation
    # already had a hand-made "Import <route>" workflow beside it. Creating it
    # here removes the step where a user adds a folder, sees nothing happen, and
    # has to know that a second object in a different page is what actually
    # reads it.
    #
    # Never fatal. The route is what was asked for; a workflow that could not be
    # created is reported so the user can add one by hand, not a reason to
    # refuse the route and leave nothing behind.
    workflow = None
    workflow_error = None
    try:
        workflow = _create_import_workflow_for_route(
            catalog, name, route_id)
    except Exception as exc:
        workflow_error = str(exc)
        _logging.getLogger(__name__).warning(
            'storage route %s created, but its import workflow was not: %s',
            route_id, exc)

    payload = _route_payload(storage.get_route(route_id), _catalog_names())
    payload['workflow'] = workflow
    if workflow_error:
        payload['workflow_error'] = workflow_error
    return jsonify(payload), 201


# The ingestion script a new route's workflow runs. Named once; its display
# name and type come from the defaults manifest rather than being repeated
# here, the way is_ingestion_script() already derives from it.
#
# The streaming import is the default since 2026-09-27
# (documentation/features/ingestion-architecture.md). The phased import stays
# available while the streaming one proves itself:
# AIRLOCK_INGEST_SCRIPT=io_picture_import_workflow.py gives new routes the
# phased import again. An existing workflow keeps the script it was created
# with; its edit form switches it.
DEFAULT_INGEST_SCRIPT = 'io_picture_import_v2_workflow.py'
INGEST_SCRIPT = os.environ.get('AIRLOCK_INGEST_SCRIPT') or DEFAULT_INGEST_SCRIPT
if INGEST_SCRIPT not in workflow_triggers.INGEST_SCRIPTS:
    print(f'[startup] AIRLOCK_INGEST_SCRIPT={INGEST_SCRIPT!r} is not an ingestion script; '
          f'using {DEFAULT_INGEST_SCRIPT}')
    INGEST_SCRIPT = DEFAULT_INGEST_SCRIPT


def _create_import_workflow_for_route(catalog, route_name, route_id):
    """
    Create the "Import <route>" workflow that reads from a new route.

    Returns {'id', 'name'} for the caller to report.

    source_directory is left empty on purpose: the route supplies the path at
    launch, and workflow_defaults.needs_configuration() already treats a
    workflow with a route as configured. Writing a literal path here would
    duplicate the route and go stale the moment it is edited.
    """
    entry = workflow_defaults.manifest_by_script().get(INGEST_SCRIPT) or {}
    catalog_db = CatalogDatabase(catalog.target_directory)
    model = WorkflowModel(catalog_db)
    workflow_id = model.create(
        name=f'Import {route_name}',
        description=entry.get('description') or '',
        source_directory='',
        python_script=f'workflows/{INGEST_SCRIPT}',
        workflow_type=entry.get('type') or 'Import',
        source_route_id=route_id,
    )
    return {'id': workflow_id, 'name': f'Import {route_name}'}


@app.route('/api/storage-routes/<int:route_id>', methods=['PUT'])
@auth.admin_required
def update_storage_route(route_id):
    """Rename a route, or re-point it after a drive moves."""
    route = storage.get_route(route_id)
    if not route:
        return jsonify({'error': 'Storage route not found'}), 404

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or route['name']).strip()
    host_path = (data.get('host_path') or route['host_path'] or '').strip()
    # "Imports may move files". Administrators only, like the rest of this
    # endpoint: it decides that the application may delete files outside its
    # own folders (ingest-move-spec.md 8.2).
    read_only = (0 if data['allow_move'] else 1) if 'allow_move' in data \
        else route.get('read_only', 1)

    if host_path != (route['host_path'] or ''):
        err = storage.validate_host_path(host_path) or _validate_source_root(host_path)
        if err:
            return jsonify({'error': err}), 400
    container_path = storage.to_container_path(host_path) or route['container_path']

    conn = db.get_connection()
    try:
        conn.execute(
            'UPDATE storage_routes SET name = ?, host_path = ?, container_path = ?, '
            'read_only = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (name, os.path.normpath(host_path) if host_path else None,
             container_path, read_only, route_id))
        conn.commit()
    except sqlite3.IntegrityError:
        return jsonify({'error': f'This catalog already has a storage route '
                                 f'named "{name}".'}), 409
    finally:
        conn.close()

    # Keep the paired workflow's name in step with the route's, so a renamed
    # folder does not leave "Import <the old name>" behind.
    renamed = []
    if name != route['name']:
        try:
            renamed = _rename_route_workflows(route, name)
        except Exception as exc:
            _logging.getLogger(__name__).warning(
                'route %s renamed, but its workflow was not: %s', route_id, exc)

    payload = _route_payload(storage.get_route(route_id), _catalog_names())
    payload['workflows_renamed'] = renamed
    return jsonify(payload)


def _rename_route_workflows(route, new_route_name):
    """
    Rename the auto-created import workflow to follow its route.

    ONLY the ones still carrying the generated name. A workflow the user renamed
    is a name they chose, and following the route would quietly overwrite it -
    so "Import <old route name>" is renamed and anything else is left alone.
    That is also what makes this safe to run on a route whose workflow was
    replaced or repurposed.

    Returns [(old, new), ...] for the caller to report.
    """
    catalog = Catalog.get_by_id(route.get('catalog_id'))
    if not catalog or not catalog.target_directory:
        return []
    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    if not os.path.exists(db_path):
        return []

    generated = f'Import {route["name"]}'
    wanted = f'Import {new_route_name}'
    changed = []
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            'SELECT id, name FROM workflows WHERE source_route_id = ?',
            (route['id'],)).fetchall()
        for r in rows:
            if r['name'] != generated:
                continue
            conn.execute('UPDATE workflows SET name = ?, '
                         'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (wanted, r['id']))
            changed.append((r['name'], wanted))
        if changed:
            conn.commit()
    finally:
        conn.close()
    return changed


@app.route('/api/storage-routes/<int:route_id>', methods=['DELETE'])
@auth.admin_required
def delete_storage_route(route_id):
    route = storage.get_route(route_id)
    if not route:
        return jsonify({'error': 'Storage route not found'}), 404

    # A route and the workflow that imports from it are one thing to the user, so
    # they are created together and removed together. Leaving the workflow behind
    # would leave a workflow that cannot run and that nobody asked to keep.
    workflows = _workflows_using_route(route)

    # Except while one is actually going. Deleting a workflow mid-run leaves an
    # execution attributed to something that no longer exists, and the run keeps
    # writing either way - so this is a wait, not a rule.
    busy = [w['name'] for w in workflows if w['busy']]
    if busy:
        return jsonify({'error': f'"{route["name"]}" is in use right now by: '
                                 f'{", ".join(busy)}. Wait for it to finish, or '
                                 f'stop it, then remove the route.'}), 409

    catalog = Catalog.get_by_id(route.get('catalog_id'))
    removed_workflows = []
    if catalog and workflows:
        try:
            _delete_workflows(catalog, [w['id'] for w in workflows])
            removed_workflows = [w['name'] for w in workflows]
        except Exception as exc:
            # The route is what was asked for. A workflow left behind is visible
            # and fixable; a route that refuses to go is neither.
            _logging.getLogger(__name__).warning(
                'route %s: could not remove its workflows: %s', route_id, exc)

    conn = db.get_connection()
    try:
        # The row only. The folder it names, and every file in it, is untouched -
        # a route is a pointer, and removing a pointer removes nothing.
        conn.execute('DELETE FROM storage_routes WHERE id = ?', (route_id,))
        conn.commit()
    finally:
        conn.close()

    message = f'Storage route "{route["name"]}" removed'
    if removed_workflows:
        message += f', with its workflow {" and ".join(removed_workflows)}'
    return jsonify({'success': True, 'message': message,
                    'workflows_removed': removed_workflows})


@app.route('/api/storage-routes/<int:route_id>/browse', methods=['GET'])
@auth.login_required
def browse_storage_route(route_id):
    """Subfolders inside one route, for the picker. Directories only."""
    route = storage.get_route(route_id)
    if not route:
        return jsonify({'error': 'Storage route not found'}), 404

    subpath = request.args.get('path', '') or ''
    resolved, err = storage.resolve_source(route_id, subpath)
    if err:
        return jsonify({'error': err}), 400
    try:
        entries = sorted(e for e in os.listdir(resolved)
                         if os.path.isdir(os.path.join(resolved, e))
                         and not e.startswith('.'))
    except OSError as exc:
        return jsonify({'error': f'Could not read that folder: {exc}'}), 400

    return jsonify({
        'route_id': route_id, 'path': subpath,
        'directories': entries,
        # Resolved for display only; the client never sends a path back.
        'absolute': resolved,
    })


def _routes_for_catalog(catalog_id):
    """The routes this catalog owns."""
    return storage.list_routes(catalog_id=catalog_id)


@app.route('/api/catalogs/<int:catalog_id>/storage-routes', methods=['GET'])
def get_catalog_storage_routes(catalog_id):
    """
    The routes this catalog owns, and therefore may import from.

    Returns the same envelope as GET /api/storage-routes - the mount status and
    the storage root alongside the list - so a page scoped to one catalog needs
    a single request. That matters more than it looks: every route in a payload
    is counted with storage.count_files(), which walks the folder, so fetching
    the whole installation's routes to read one status line would walk every
    other catalog's folders too.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    names = _catalog_names()
    return jsonify({
        'enabled': storage.routes_enabled(),
        'message': storage.root_status(),
        'storage_root_host': storage.STORAGE_ROOT_HOST,
        'storage_root_container': storage.STORAGE_ROOT_CONTAINER,
        'routes': [_route_payload(r, names)
                   for r in _routes_for_catalog(catalog_id)],
    })


# There is deliberately no endpoint for attaching an existing route to a
# catalog. A route is created inside one catalog and stays there; moving or
# sharing one is the model this replaced. To point a second catalog at the same
# folder, create a route there - the two are independent, and deleting either
# catalog leaves the other's route alone.


@app.route('/api/catalogs', methods=['GET'])
@auth.login_required
def get_catalogs():
    """
    Catalogs this user can reach, with their role on each.

    This is what makes the whole UI appear correctly scoped: CatalogContext
    drives every view from this list (spec 6.1).
    """
    catalogs = Catalog.get_all()

    def _payload(cat):
        # pets_enabled is not on catalog_meta - it lives in each catalog's own
        # database, so that the answer travels with the folder and start-all.sh
        # can read it without the registry. The catalog form edits a catalog
        # straight from THIS list, so the flag has to be here or the switch
        # shows "off" for a moment before the detail request corrects it.
        #
        # Measured on homeserver at 1.9 ms per catalog, against a 24 GB database
        # under a running detection workflow. Worth watching if a deployment
        # ever has dozens of catalogs; with a handful it is below the noise.
        d = cat.to_dict()
        d['pets_enabled'] = pets_enabled_for(cat.target_directory)
        return d

    if not auth.enabled_for(app):
        return jsonify(_with_owner([_payload(cat) for cat in catalogs]))

    user = auth.current_user()
    conn = db.get_connection()
    try:
        roles = {r['catalog_id']: r['role'] for r in conn.execute(
            'SELECT catalog_id, role FROM catalog_access WHERE user_id = ?',
            (user['id'],)).fetchall()}
    finally:
        conn.close()

    out = []
    for cat in catalogs:
        if cat.id in roles:
            payload = _payload(cat)
            payload['role'] = roles[cat.id]
            out.append(payload)
    return jsonify(_with_owner(out))

def _may_allow_moves():
    """True when the current user may let the application delete source files."""
    if not auth.enabled_for(app):
        return True
    user = auth.current_user()
    return bool(user and user['is_admin'])


@app.route('/api/catalogs', methods=['POST'])
@auth.login_required
def create_catalog():
    """Create a new catalog. The creator becomes its owner."""
    data = request.json

    # `owner` used to be required and free text - a name someone typed, from
    # before this application had users. The creator owns what they create;
    # anything the client sends under that key is ignored.
    required_fields = ['name', 'target_directory']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    # Registering a directory that is ALREADY a catalog - a folder carried from
    # another machine, or one deleted from the registry and added back. What it
    # is stays with the data; only where this machine reaches it is new.
    _reattaching = os.path.exists(
        os.path.join(data['target_directory'], 'workflow_results.db'))
    _declared_mode = (models.read_ingest_mode(data['target_directory'])
                      if _reattaching else 'copy')
    if _reattaching and _declared_mode != data.get('ingest_mode', _declared_mode):
        print(f"[catalog] {data['target_directory']} declares ingest_mode "
              f"'{_declared_mode}'; using that over the requested value")

    # A catalog that moves files deletes them from the folders it imports from,
    # so creating one is an administrator's decision (ingest-move-spec.md 8.2).
    if not _reattaching and data.get('ingest_mode') == 'move' \
            and not _may_allow_moves():
        return jsonify({'error': 'Only an administrator can create a catalog that '
                                 'moves files out of the folders it imports from.'}), 403

    # ── slug / directory guard ────────────────────────────────────────────
    #
    # The catalog's URL name and the last segment of its folder are the same
    # thing wearing two hats, and the UI now offers them as one "System name"
    # field. This is what stops anything else - a script, curl, an older client -
    # putting them out of step, which used to happen SILENTLY:
    #
    #   POST slug='family_photos', target_directory='/catalogs/family_photos'
    #     -> slug stored as 'family-photos'   (sanitised by unique_slug)
    #     -> dir  stored as '/catalogs/family_photos'  (verbatim)
    #
    # An EXPLICIT slug is validated rather than rewritten. Silently changing
    # what the caller asked for is worse than refusing it: the caller believes
    # it got what it sent, and the disagreement surfaces much later as a URL
    # that does not resolve.
    _explicit_slug = (data.get('slug') or '').strip()
    if _explicit_slug:
        _slug_problem = slugs.problem(_explicit_slug)
        if _slug_problem:
            return jsonify({'error': _slug_problem}), 400
        if _explicit_slug in _catalog_slugs():
            return jsonify({'error': f'Another catalog already uses the system name '
                                     f'"{_explicit_slug}".'}), 409

    # Re-attaching a directory that is already a catalog is the one case where a
    # mismatch is legitimate: the folder name is a given, and the slug may have
    # had to dodge one already in use. Everywhere else, disagreement is a bug in
    # the caller and saying so is more useful than picking a winner.
    _dir_basename = os.path.basename(os.path.normpath(data['target_directory'] or ''))
    if _explicit_slug and _dir_basename and _explicit_slug != _dir_basename \
            and not _reattaching:
        return jsonify({'error':
            f'The system name and the folder disagree: slug "{_explicit_slug}" but '
            f'folder "{_dir_basename}". They name the same thing — send a '
            f'target_directory ending in "{_explicit_slug}", or omit the slug and '
            f'it will be derived from the folder.'}), 400

    # Resolved before the catalog is built: the creator IS the owner, so the id
    # has to be in hand at construction rather than patched in afterwards.
    creator = auth.current_user()

    # Validated before anything is created: a typo in a size name must fail the
    # request, not create a catalog that silently generates nothing extra.
    _new_rungs, _rung_err = validate_image_rungs(data.get('image_rungs'))
    if _rung_err:
        return jsonify({'error': _rung_err}), 400

    try:
        # Create Catalog instance with file strategy support
        catalog = Catalog(
            name=data['name'],
            owner_user_id=(creator['id'] if creator else None),
            description=data.get('description', ''),
            target_directory=data['target_directory'],
            file_strategy=data.get('file_strategy', 'local'),
            image_rungs=_new_rungs,
            # SCP parameters
            scp_username=data.get('scp_username'),
            scp_host=data.get('scp_host'),
            scp_port=data.get('scp_port', 22),
            # FTP parameters
            ftp_host=data.get('ftp_host'),
            ftp_username=data.get('ftp_username'),
            ftp_password=data.get('ftp_password'),
            ftp_port=data.get('ftp_port', 21),
            # Box parameters
            box_client_id=data.get('box_client_id'),
            box_client_secret=data.get('box_client_secret'),
            box_access_token=data.get('box_access_token'),
            # S3 parameters
            s3_bucket_name=data.get('s3_bucket_name'),
            s3_aws_access_key_id=data.get('s3_aws_access_key_id'),
            s3_aws_secret_access_key=data.get('s3_aws_secret_access_key'),
            s3_region_name=data.get('s3_region_name'),
            # Face recognition metric
            similarity_metric=data.get('similarity_metric', 'cosine'),
            # How this catalog handles files. When a directory that is ALREADY a
            # catalog is registered again - a folder carried from another machine
            # - its own recorded mode wins over anything the caller sent. That
            # fact travels with the data precisely so nobody has to remember it
            # (file-ingestion-spec.md section 3.1).
            ingest_mode=(_declared_mode if _reattaching
                         else data.get('ingest_mode', 'copy')),
            source_root=data.get('source_root'),
            # The catalog's URL segment.
            #
            # An explicit slug is used as sent - it was validated above, so
            # unique_slug has nothing left to change. Without one it is derived
            # from the FOLDER name rather than the display name, because the
            # folder is the thing it has to agree with; falling back to the
            # display name is what allowed 'Family Pictures' + /catalogs/family
            # to produce slug 'family-pictures' and a folder called 'family'.
            slug=(_explicit_slug if _explicit_slug
                  else slugs.unique_slug(_dir_basename or data['name'],
                                         _catalog_slugs(), fallback='catalog')),
        )
        if catalog.ingest_mode == 'reference':
            if catalog.source_root:
                err = _validate_source_root(catalog.source_root)
                if err:
                    return jsonify({'error': err}), 400
            elif not _reattaching:
                # A NEW reference catalog has to say what it indexes. A
                # RE-ATTACHED one does not: its library may not exist on this
                # machine yet, and the catalog is usable without it until
                # someone wants a full-size file (spec section 5.4).
                return jsonify({'error': 'A catalog that indexes files in place '
                                         'needs a source folder.'}), 400
        catalog_id = catalog.save()

        # Default workflows. Without them the catalog has no automation at all:
        # trigger chains resolve by script basename against what is registered
        # here, and silently skip the rest (workflows-default-spec.md 1).
        #
        # Never fatal: a catalog without its defaults is one click from fixed,
        # whereas failing here would leave a saved catalog the user was told
        # was not created.
        #
        # This is also what CREATES the catalog directory and its database -
        # CatalogDatabase.__init__ does the makedirs - so it has to come before
        # anything that writes into that directory.
        try:
            catalog_db = CatalogDatabase(catalog.target_directory)
            # The whole tree now, not as files first need it: each part is then
            # there to be bind-mounted from the storage that suits it, before
            # anything is imported (catalog_layout.py). A re-attached catalog
            # only gains what it lacks.
            try:
                made = catalog_layout.ensure_catalog_dirs(catalog.target_directory,
                                                          catalog.ingest_mode)
                if made:
                    print(f"[catalog {catalog_id}] created {', '.join(made)}")
            except OSError as _dir_exc:
                # Every writer still makes its own directory on first use; this
                # is a convenience for mounting, never a reason to fail.
                _logging.getLogger(__name__).warning(
                    'catalog %s: could not create its directory tree: %s',
                    catalog_id, _dir_exc)
            seeded = workflow_defaults.seed_default_workflows(catalog_db)
            print(f"[catalog {catalog_id}] seeded workflows: "
                  f"{', '.join(seeded['created']) or 'none'}")

            # Pets, ON unless the caller said otherwise.
            #
            # A RE-ATTACHED catalog keeps its own answer, for the same reason
            # ingest_mode does: the folder has been looking for animals, or
            # deliberately not, and a default must not overrule a decision
            # already recorded with the data. Only a genuinely new catalog
            # takes the default.
            _pets_on = pets_default_for_new_catalog(
                data, _reattaching, catalog.target_directory)
            _pet_created, _ = apply_pets_enabled(
                catalog.target_directory, catalog_db, _pets_on)
            print(f"[catalog {catalog_id}] pets "
                  f"{'on' if _pets_on else 'off'}"
                  + (f", added {', '.join(_pet_created)}" if _pet_created else ''))
        except Exception as _exc:
            _logging.getLogger(__name__).warning(
                'default workflows not seeded for catalog %s: %s', catalog_id, _exc)

        # Record the mode WITH the data, once. This is the copy that survives the
        # catalog being moved, deleted from the registry and re-added
        # (file-ingestion-spec.md section 3.1).
        #
        # AFTER the block above, not before: writing here first meant opening a
        # database in a directory nothing had created yet, which failed with
        # "unable to open database file" and left the mode unrecorded.
        try:
            models.write_ingest_mode(catalog.target_directory, catalog.ingest_mode)
        except Exception as _exc:
            _logging.getLogger(__name__).warning(
                'could not record ingest_mode for catalog %s: %s', catalog_id, _exc)

        # Without this the creator cannot see what they just made: the catalog
        # list is a join against catalog_access (spec 6.1). owner_user_id records
        # WHO owns it; this row is what the permission checks read.
        if creator is not None:
            conn = db.get_connection()
            try:
                auth.grant_access(conn, catalog_id, creator['id'], 'owner',
                                  granted_by=creator['id'])
            finally:
                conn.close()

        # The gallery resolves a URL's catalog segment through this file, so a
        # new catalog is unreachable by name until it is rewritten.
        _write_catalogs_index()

        return jsonify({'id': catalog_id, 'message': 'Catalog created successfully'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/catalogs/<int:catalog_id>', methods=['GET'])
def get_catalog(catalog_id):
    """Get a specific catalog with all related data from catalog-specific database"""
    try:
        # Use Catalog class to get catalog
        catalog = Catalog.get_by_id(catalog_id)
        if not catalog:
            return jsonify({'error': 'Catalog not found'}), 404
        
        # Convert to dict
        catalog_dict = catalog.to_dict()
        # One boolean, cheaply. The catalog form needs to know whether the ingest
        # mode is still changeable, and asking it to count the `files` array
        # below would mean loading every row with its EXIF just to learn "yes".
        catalog_dict['has_files'] = _catalog_has_files(catalog)
        # The catalog form's pets switch reads this. It is not on catalog_meta -
        # it lives in the catalog's own database - so it has to be added here
        # rather than coming along in to_dict().
        catalog_dict['pets_enabled'] = pets_enabled_for(catalog.target_directory)
        _with_owner([catalog_dict])

        # Check if target_directory is set
        if not catalog.target_directory:
            # Return catalog without files/executions if no target directory
            catalog_dict['files'] = []
            catalog_dict['operations'] = []
            catalog_dict['workflow_executions'] = []
            return jsonify(catalog_dict)
        
        # Get related data from catalog-specific database
        catalog_db = CatalogDatabase(catalog.target_directory)
        
        # The file list - every row with its parsed EXIF - only when asked for
        # (?include=files). Only the catalog detail view shows it; the
        # executions page, the catalog form and the app's start-up (which
        # restores the active catalog on every load) used to receive all of it
        # and discard it. On oldserver that was ~33,000 rows sorted with their EXIF
        # text: 16 s, and then "database or disk is full" once SQLite's
        # temporary sort space ran out - so the executions page never loaded.
        include = {part.strip() for part in request.args.get('include', '').split(',')}
        files = []
        if 'files' in include:
            files = _catalog_files(catalog_db)

        # Get workflow executions from catalog-specific database
        execution_model = ExecutionModel(catalog_db)
        executions = execution_model.get_all()
        
        catalog_dict['files'] = files
        catalog_dict['operations'] = []  # Deprecated
        catalog_dict['workflow_executions'] = executions
        
        return jsonify(catalog_dict)
    except Exception as e:
        return jsonify({'error': f'Error retrieving catalog: {str(e)}'}), 500

def _catalog_files(catalog_db, limit=None, offset=0):
    """
    The catalog's files, newest first, each with its EXIF parsed into
    `exif_data` - all of them, or one page with `limit`/`offset`.

    Unpaged this is heavy: second-catalog (21,506 files) came to 1.5 GB of JSON
    in 86 s, which is why the catalog page now asks for one page at a time
    (GET /api/catalogs/<id>/files).
    """
    import json
    conn = catalog_db.get_connection()
    try:
        # Two steps. Sorting the joined rows directly made SQLite carry every
        # file's EXIF text (~13 KB a photo) through the sort to keep 25 of them:
        # 20 s for one page on oldserver. Sorting ids alone is milliseconds, and the
        # details are then fetched for just the ids kept.
        sql = ('SELECT id FROM file_contents WHERE removed_at IS NULL '
               'ORDER BY added_at DESC, id DESC')
        params = ()
        if limit is not None:
            sql += ' LIMIT ? OFFSET ?'
            params = (int(limit), int(offset))
        ids = [r[0] for r in conn.execute(sql, params)]
        by_id = {}
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for row in conn.execute(f'''
                SELECT fc.id, fc.file_path, fc.file_name, fc.file_size, fc.file_type,
                       fc.file_permissions, fc.sha256_hash, fc.exif_data_id,
                       fc.modification_time, fc.access_time, fc.change_time,
                       fc.creation_time, fc.added_at
                -- THE RAW EXIF BLOB IS NOT HERE, DELIBERATELY. It averages
                -- 22 KB a row, and CatalogDetail.js renders it for the ONE
                -- row a person expands - so fetching it for every file on
                -- the page was eager work for a lazy display. The expand
                -- calls /files/<id>/exif instead.
                FROM file_contents fc
                LEFT JOIN exif_data ed ON fc.id = ed.file_id
                WHERE fc.id IN ({','.join('?' * len(chunk))})''', chunk):
                by_id.setdefault(row['id'], row)
        rows = [by_id[i] for i in ids if i in by_id]
    finally:
        conn.close()

    # exif_data is NOT included. It is fetched per file, on expand, from
    # /files/<file_id>/exif - see the query above for why.
    return [dict(row) for row in rows]


def _raw_exif_for(conn, file_id):
    """
    The raw exiftool dump for one file, as a dict, or None.

    exif_raw first and the old exif_data column as a fallback, so a catalog
    migrate_split_raw_exif.py has not been run against still answers. Once it
    has, the fallback reads a NULL and costs nothing.
    """
    try:
        row = conn.execute('''
            SELECT COALESCE(er.raw_exif_json, ed.raw_exif_json) AS raw
              FROM exif_data ed
              LEFT JOIN exif_raw er ON er.exif_id = ed.id
             WHERE ed.file_id = ?
             ORDER BY ed.id DESC LIMIT 1''', (file_id,)).fetchone()
    except sqlite3.Error:
        return None                      # catalog predates exif_raw
    if not row or not row['raw']:
        return None
    try:
        return _json.loads(row['raw'])
    except (ValueError, TypeError):
        return None


@app.route('/api/catalogs/<int:catalog_id>/files/<int:file_id>/exif',
           methods=['GET'])
def get_file_raw_exif(catalog_id, file_id):
    """
    One file's full EXIF, fetched when a person expands that row.

    The catalog file list used to carry this for every file on the page while
    CatalogDetail.js rendered it for the one row expanded. At 22 KB a row that
    is the page's largest cost paid for its least-used detail.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        return jsonify({'exif_data': _raw_exif_for(conn, file_id)})
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/files', methods=['GET'])
def get_catalog_files_page(catalog_id):
    """One page of the catalog's files (?page=1&per_page=25), with the total."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'total': 0, 'page': 1, 'per_page': 25, 'files': []})
    page = max(1, request.args.get('page', 1, type=int) or 1)
    per_page = min(200, max(1, request.args.get('per_page', 25, type=int) or 25))
    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    try:
        total = conn.execute(
            'SELECT COUNT(*) FROM file_contents WHERE removed_at IS NULL').fetchone()[0]
    finally:
        conn.close()
    files = _catalog_files(catalog_db, limit=per_page, offset=(page - 1) * per_page)
    return jsonify({'total': total, 'page': page, 'per_page': per_page, 'files': files})


@app.route('/api/catalogs/<int:catalog_id>', methods=['PUT'])
def update_catalog(catalog_id):
    """Update a catalog"""
    # Use Catalog class to get and update catalog
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    data = request.json
    if not data:
        return jsonify({'error': 'No data provided'}), 400
    
    # Update catalog attributes
    if 'name' in data:
        catalog.name = data['name']
    # Ownership is deliberately not settable here. It is a real user reference
    # now, and transferring a catalog is a sharing decision - it belongs with
    # /catalogs/<id>/access, which already refuses to leave a catalog ownerless.
    # The URL name. Editable, because a catalog created before this existed got
    # one derived from its name and may want a better one - and dangerous,
    # because it moves EVERY deployment in the catalog at once. The UI warns; the
    # API only enforces that the result is a usable segment and label.
    if 'slug' in data:
        candidate = (data['slug'] or '').strip().lower()
        err = slugs.problem(candidate)
        if err:
            return jsonify({'error': err}), 400
        if candidate in _catalog_slugs(exclude_id=catalog.id):
            return jsonify({'error': f'Another catalog already uses the URL name '
                                     f'"{candidate}".'}), 409
        catalog.slug = candidate
    if 'description' in data:
        catalog.description = data['description']
    if 'target_directory' in data:
        catalog.target_directory = data['target_directory']
    if 'file_strategy' in data:
        catalog.file_strategy = data['file_strategy']

    # The mode cannot change once anything has been ingested. copy -> reference
    # would orphan every file already copied; reference -> copy would start
    # duplicating a library the user asked not to touch. Either way the paths
    # already in file_contents are relative to the wrong root from that moment on
    # (file-ingestion-spec.md section 3.1).
    if 'ingest_mode' in data and data['ingest_mode'] != catalog.ingest_mode:
        if data['ingest_mode'] not in models.INGEST_MODES:
            return jsonify({'error': f'Unknown ingest mode "{data["ingest_mode"]}".'}), 400
        if data['ingest_mode'] == 'move' and not _may_allow_moves():
            return jsonify({'error': 'Only an administrator can make a catalog move '
                                     'files out of the folders it imports from.'}), 403
        if _catalog_has_files(catalog):
            return jsonify({'error':
                'How this catalog handles files cannot be changed once it has '
                'imported anything — the paths already stored are relative to '
                'the root it was created with.'}), 409
        catalog.ingest_mode = data['ingest_mode']
        try:
            models.write_ingest_mode(catalog.target_directory, catalog.ingest_mode)
        except Exception as _exc:
            _logging.getLogger(__name__).warning('could not record ingest_mode: %s', _exc)

    # The source folder, by contrast, is expected to change: a drive remounts
    # somewhere else, or a re-attached catalog is being pointed at its library
    # for the first time (section 5.4).
    if 'source_root' in data:
        candidate = data['source_root']
        if candidate:
            err = _validate_source_root(candidate, catalog_id=catalog.id)
            if err:
                return jsonify({'error': err}), 400
            fit = _source_root_fits(catalog, candidate)
            if fit:
                return jsonify({'error': fit}), 400
        catalog.source_root = candidate
    
    # Update strategy-specific parameters
    if 'scp_username' in data:
        catalog.scp_username = data['scp_username']
    if 'scp_host' in data:
        catalog.scp_host = data['scp_host']
    if 'scp_port' in data:
        catalog.scp_port = data['scp_port']
    if 'ftp_host' in data:
        catalog.ftp_host = data['ftp_host']
    if 'ftp_username' in data:
        catalog.ftp_username = data['ftp_username']
    if 'ftp_password' in data:
        catalog.ftp_password = data['ftp_password']
    if 'ftp_port' in data:
        catalog.ftp_port = data['ftp_port']
    if 'box_client_id' in data:
        catalog.box_client_id = data['box_client_id']
    if 'box_client_secret' in data:
        catalog.box_client_secret = data['box_client_secret']
    if 'box_access_token' in data:
        catalog.box_access_token = data['box_access_token']
    if 's3_bucket_name' in data:
        catalog.s3_bucket_name = data['s3_bucket_name']
    if 's3_aws_access_key_id' in data:
        catalog.s3_aws_access_key_id = data['s3_aws_access_key_id']
    if 's3_aws_secret_access_key' in data:
        catalog.s3_aws_secret_access_key = data['s3_aws_secret_access_key']
    if 's3_region_name' in data:
        catalog.s3_region_name = data['s3_region_name']
    if 'similarity_metric' in data:
        catalog.similarity_metric = data['similarity_metric']
    if 'image_rungs' in data:
        rungs, err = validate_image_rungs(data['image_rungs'])
        if err:
            return jsonify({'error': err}), 400
        catalog.image_rungs = rungs

    # Pets. Not stored on catalog_meta with the rest of these: it lives in the
    # catalog's OWN database, so it travels with the folder to another machine
    # and is what start-all.sh reads to decide whether backend-pets runs at all.
    #
    # Never fatal. The property gates detection and is written first, so a
    # failure to add or remove the workflow rows leaves the gate correct and is
    # one click from fixed - whereas failing the whole update would discard
    # every other edit the user just made.
    if 'pets_enabled' in data and catalog.target_directory:
        try:
            apply_pets_enabled(catalog.target_directory,
                               CatalogDatabase(catalog.target_directory),
                               bool(data['pets_enabled']))
        except Exception as _exc:
            _logging.getLogger(__name__).warning(
                'catalog %s: pets_enabled set, but its workflows did not '
                'change: %s', catalog_id, _exc)

    catalog.save()
    _write_catalogs_index()

    return jsonify({'message': 'Catalog updated successfully'})

@app.route('/api/catalogs/<int:catalog_id>', methods=['DELETE'])
def delete_catalog(catalog_id):
    """Delete a catalog"""
    # Use Catalog class to delete catalog
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    # Storage routes are the catalog's own, and go with it. The row only: the
    # folder a route names, and every file in it, is left exactly as it was.
    # Same reasoning as the access grants Catalog.delete() removes - both are
    # keyed on a catalog id that will not exist, and re-adding the directory
    # mints a new one.
    routes_removed = 0
    try:
        conn = db.get_connection()
        try:
            cur = conn.execute('DELETE FROM storage_routes WHERE catalog_id = ?',
                               (catalog_id,))
            routes_removed = cur.rowcount or 0
            # Its backup cards too. Never the stored backups: a deleted
            # catalog's backups may be exactly what someone needs next.
            import backups as _backups
            for t in _backups.list_targets(conn, catalog_id):
                _backups.cancel(t['id'])
                _backups.delete_target(conn, t['id'])
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        _logging.getLogger(__name__).warning(
            'could not remove storage routes for catalog %s: %s', catalog_id, exc)

    # Deployments go first, while the catalog still knows its directory.
    #
    # A detach, not a destroy: the directory and every ARTIFACT in it survives -
    # photos, events, people, albums - and re-adding the folder brings them all
    # back. A deployment is not an artifact. It is a decision to publish, and
    # detaching the catalog revokes it, so the definition goes with it rather
    # than lying dormant and republishing itself the next time the folder is
    # added. See _remove_catalog_deployments(): no image is touched.
    removed, warnings = _remove_catalog_deployments(catalog)

    catalog.delete()
    # Dropping the catalog from the index is what makes the gallery stop serving
    # its URLs (albums-enhancement-spec.md section 6.3).
    _write_catalogs_index()

    message = 'Catalog deleted successfully'
    bits = []
    if removed:
        bits.append(f'{removed} deployment{"" if removed == 1 else "s"}')
    if routes_removed:
        bits.append(f'{routes_removed} storage route{"" if routes_removed == 1 else "s"}')
    if bits:
        message += f' ({" and ".join(bits)} removed)'
    payload = {'message': message, 'deployments_removed': removed,
               'storage_routes_removed': routes_removed}
    if warnings:
        # Surfaced rather than swallowed: the catalog IS detached, but something
        # was left on disk and the user is the only one who can go and look.
        payload['warnings'] = warnings
    return jsonify(payload)

def _catalog_slugs(exclude_id=None):
    """Every catalog slug in use. One table, one query - see slugs.py."""
    conn = db.get_connection()
    try:
        rows = conn.execute(
            'SELECT id, slug FROM catalog_meta WHERE slug IS NOT NULL').fetchall()
    finally:
        conn.close()
    return {r['slug'] for r in rows if r['id'] != exclude_id and r['slug']}


def _catalog_has_files(catalog) -> bool:
    """True when this catalog has already ingested something."""
    if not catalog.target_directory:
        return False
    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    if not os.path.exists(db_path):
        return False
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            return conn.execute('SELECT 1 FROM file_contents LIMIT 1').fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


# Roots too broad to hand to the file API. Naming one of these as a source
# folder would make every file under it readable through the catalog endpoints
# (file-ingestion-spec.md section 5.2).
_FORBIDDEN_SOURCE_ROOTS = {
    '/', '/home', '/Users', '/Volumes', '/mnt', '/media', '/etc', '/var', '/tmp', '/app',
}


def _validate_source_root(path, catalog_id=None):
    """
    Check a candidate source folder. Returns an error string, or None.

    Rejects roots that are too broad, and roots that overlap a catalog
    directory - overlapping roots make "which root does this path belong to"
    ambiguous exactly where the answer has to be certain.
    """
    if not path:
        return 'A source folder is required for a catalog that indexes files in place.'
    if not os.path.isabs(path):
        return 'The source folder must be an absolute path.'
    real = os.path.realpath(path)
    if real.rstrip(os.sep) in {r.rstrip(os.sep) for r in _FORBIDDEN_SOURCE_ROOTS} or real == os.sep:
        return (f'{path} is too broad to use as a source folder. Choose the folder that '
                f'holds the photos, not the drive or home directory above it.')
    for other in Catalog.get_all():
        if catalog_id is not None and other.id == catalog_id:
            continue
        for label, root in (('catalog folder', other.target_directory),
                            ('source folder', other.source_root)):
            if not root:
                continue
            other_real = os.path.realpath(root)
            if real == other_real or real.startswith(other_real + os.sep) \
                    or other_real.startswith(real + os.sep):
                return (f'That folder overlaps the {label} of "{other.name}". '
                        f'Source folders and catalog folders must not contain one another.')
    return None


def _sample_stored_paths(catalog, limit=5):
    """A few file_contents paths, for checking a source root without walking it."""
    if not catalog.target_directory:
        return []
    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    if not os.path.exists(db_path):
        return []
    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            rows = conn.execute(
                'SELECT file_path FROM file_contents '
                'WHERE file_path IS NOT NULL ORDER BY RANDOM() LIMIT ?', (limit,)).fetchall()
            return [r[0] for r in rows]
        finally:
            conn.close()
    except sqlite3.Error:
        return []


def _source_root_fits(catalog, candidate):
    """
    Error string when a candidate source folder does not match this catalog's rows.

    A user who picks the wrong folder should hear about it at once. The failure
    this prevents is not an empty catalog - it is a folder that happens to
    contain same-named files from an unrelated library, which would resolve
    silently to the wrong photos (file-ingestion-spec.md section 5.4).
    """
    samples = _sample_stored_paths(catalog)
    if not samples:
        return None                     # nothing indexed yet; nothing to check against
    if not os.path.isdir(candidate):
        return f'{candidate} is not a folder this server can see.'
    found = sum(1 for rel in samples
                if os.path.exists(os.path.join(candidate, rel)))
    if found == 0:
        return (f'None of this catalog\'s files were found under {candidate}. '
                f'It expects paths like "{samples[0]}". Is this the right folder?')
    return None


@app.route('/api/catalogs/<int:catalog_id>/source-status', methods=['GET'])
def catalog_source_status(catalog_id):
    """
    Whether this catalog can reach its originals.

    Sampled, not exhaustive. A reference catalog is expected to index tens of
    thousands of files on media where a stat can be slow, and this runs every
    time the active catalog changes. Five rows distinguishes "mounted" from "not
    mounted", which is the question being asked; full verification is /verify,
    which the user asks for deliberately (spec section 5.3).
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    conflict = _mode_conflict(catalog)
    if conflict:
        return jsonify({
            'catalog_id': catalog.id, 'mode': catalog.ingest_mode, 'state': 'conflict',
            'message': (f'This catalog is registered as "{catalog.ingest_mode}", but the '
                        f'folder at {catalog.target_directory} records itself as '
                        f'"{conflict}". Its files are not being served, because the two '
                        f'modes read stored paths against different roots. Re-add the '
                        f'folder, or point this catalog at the right one.')})

    if getattr(catalog, 'ingest_mode', 'copy') != 'reference':
        # A copy-mode catalog owns its files; there is nothing to be unavailable,
        # and no reason to touch the disk to say so.
        return jsonify({'catalog_id': catalog.id, 'mode': 'copy', 'state': 'ok'})

    root = catalog.source_root
    payload = {'catalog_id': catalog.id, 'mode': 'reference', 'source_root': root,
               'checked': 0, 'missing': 0}

    if not root:
        payload.update(state='unset', message=(
            'This catalog indexes files in place, but has not been told where the '
            'originals are on this machine. Previews and data are all here; set the '
            'source folder to reach the full-size files.'))
        return jsonify(payload)

    if not os.path.isdir(root):
        payload.update(state='unavailable', message=(
            f'The source folder {root} is not there. If it is on an external drive, '
            f'it may not be mounted. If this server runs in a container, the folder '
            f'also has to be mounted into it — a path that exists on the host is not '
            f'visible inside otherwise.'))
        return jsonify(payload)

    samples = _sample_stored_paths(catalog)
    missing = [rel for rel in samples if not os.path.exists(os.path.join(root, rel))]
    payload['checked'] = len(samples)
    payload['missing'] = len(missing)
    if samples and len(missing) == len(samples):
        payload.update(state='degraded', message=(
            f'{root} is there, but none of the files sampled from it were found. '
            f'The library may have moved inside the folder. Expected to find '
            f'"{samples[0]}".'))
    elif missing:
        payload.update(state='degraded', message=(
            f'{len(missing)} of {len(samples)} sampled files are missing from {root}. '
            f'Run Verify for the full picture.'))
    else:
        payload.update(state='ok')
    return jsonify(payload)


@app.route('/api/catalogs/<int:catalog_id>/left-in-source', methods=['GET'])
def get_left_in_source(catalog_id):
    """
    What a move-mode import did not take: the "Left in the source" view
    (ingest-move-spec.md section 7.3). For one run - ?execution_id=, or the
    latest that moved anything - every file set aside in DUPS/, left in place,
    or recorded in the catalog but not yet deleted from the source, plus every
    failure. The list the user goes through by hand.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    kinds = ('move', 'duplicate_moved', 'left_in_place')
    marks = ','.join('?' * len(kinds))
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        runs = [dict(r) for r in conn.execute(
            f"""SELECT fo.execution_id, MIN(fo.operation_date) AS started,
                       we.source_directory,
                       SUM(fo.operation_type = 'move' AND fo.status = 'moved') AS moved,
                       SUM(fo.operation_type = 'duplicate_moved' AND fo.status = 'moved')
                           AS duplicates,
                       SUM(fo.operation_type = 'left_in_place') AS left_in_place,
                       SUM(fo.operation_type = 'move'
                           AND fo.status IN ('pending_delete', 'pending_move'))
                           AS pending_delete,
                       SUM(fo.operation_type = 'move' AND fo.status = 'missing') AS missing,
                       SUM(fo.status = 'failed') AS failed
                  FROM file_operations fo
                  LEFT JOIN workflow_executions we ON we.id = fo.execution_id
                 WHERE fo.operation_type IN ({marks})
                 GROUP BY fo.execution_id ORDER BY fo.execution_id DESC LIMIT 50""",
            kinds)]
        # Across every run: a file missing from both places matters until someone
        # deals with it, whichever run the page is showing.
        missing_total = conn.execute(
            "SELECT COUNT(*) FROM file_operations "
            "WHERE operation_type = 'move' AND status = 'missing'").fetchone()[0]
        eid = request.args.get('execution_id', type=int) or (
            runs[0]['execution_id'] if runs else None)
        files = []
        if eid is not None:
            files = [dict(r) for r in conn.execute(
                f"""SELECT id, operation_type, status, source_path, target_path,
                           file_size, details
                      FROM file_operations
                     WHERE execution_id = ? AND operation_type IN ({marks})
                       AND NOT (operation_type = 'move' AND status = 'moved')
                     ORDER BY operation_type, source_path""", (eid, *kinds))]
    finally:
        conn.close()
    return jsonify({'ingest_mode': getattr(catalog, 'ingest_mode', 'copy'),
                    'runs': runs, 'execution_id': eid, 'files': files,
                    'missing_total': missing_total})


@app.route('/api/catalogs/<int:catalog_id>/verify', methods=['POST'])
def verify_catalog(catalog_id):
    """Verify catalog integrity - check files in catalog vs directory"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    target_dir = catalog.target_directory
    
    # Get files from catalog database
    catalog_db = CatalogDatabase(target_dir)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT file_path, file_name, file_size FROM file_contents')
    catalog_files_rows = cursor.fetchall()
    
    # Get all alternative_versions file paths (files stored in tmp)
    cursor.execute('SELECT alternative_file_path, alternative_file_name, alternative_file_size FROM alternative_versions')
    alt_versions_rows = cursor.fetchall()
    conn.close()
    
    # Build catalog files dict — resolve relative paths to absolute for existence checks
    catalog_files = {}
    for row in catalog_files_rows:
        abs_path = _resolve_path(row['file_path'], _original_root(catalog))
        catalog_files[abs_path] = {
            'file_path': abs_path,
            'file_name': row['file_name'],
            'file_size': row['file_size'],
            'exists': False
        }
    
    # Build alternative_versions (cache) file paths set — resolve relative paths
    cache_file_paths = set()
    for row in alt_versions_rows:
        cache_file_paths.add(_resolve_path(row['alternative_file_path'], target_dir))
    
    # Get files from directory.
    #
    # WHICH directory depends on the mode. A reference catalog's originals live
    # in the source library, so walking the catalog folder would find only
    # derivatives - every original would report as missing and every photo in the
    # library as unindexed, turning a useful check into a wall of false alarms
    # (file-ingestion-spec.md section 6.3).
    directory_files = {}
    cache_dir = os.path.join(target_dir, 'cache')
    scan_dir = _original_root(catalog)
    reference_mode = getattr(catalog, 'ingest_mode', 'copy') == 'reference'
    if reference_mode and (not catalog.source_root or not os.path.isdir(scan_dir)):
        # Nothing to compare against. Say so rather than reporting every indexed
        # file as deleted, which is what an empty walk would look like.
        return jsonify({
            'catalog_id': catalog_id,
            'catalog_name': catalog.name,
            'target_directory': target_dir,
            'source_root': catalog.source_root,
            'ingest_mode': 'reference',
            'source_available': False,
            'message': ('The source folder is not reachable, so the originals cannot '
                        'be checked. Set or mount it and verify again.'),
            'files_in_catalog_exist': [],
            'files_in_catalog_missing': [],
            'files_in_directory_cataloged': [],
            'files_in_directory_cache': [],
            'files_in_directory_not_cataloged': [],
        })
    if os.path.exists(scan_dir):
        for root, dirs, filenames in os.walk(scan_dir):
            # The import's staging area holds copies on their way into the
            # catalog, not files that were missed (io_picture_import_workflow.py,
            # STAGING_DIRNAME).
            dirs[:] = [d for d in dirs if d != '.import-staging']
            for filename in filenames:
                # Skip the database file itself
                if filename == 'workflow_results.db':
                    continue
                file_path = os.path.join(root, filename)
                file_stat = os.stat(file_path)

                # Check if file is in cache directory (any file in cache is considered cache)
                is_in_cache = file_path.startswith(cache_dir + os.sep) if os.path.exists(cache_dir) else False

                directory_files[file_path] = {
                    'file_path': file_path,
                    'file_name': filename,
                    'file_size': file_stat.st_size,
                    'in_catalog': False,
                    'in_cache': is_in_cache  # Mark as cache if in cache directory
                }
    
    # Check which catalog files exist in directory
    files_in_catalog_exist = []
    files_in_catalog_missing = []
    for file_path, file_info in catalog_files.items():
        if os.path.exists(file_path):
            file_info['exists'] = True
            files_in_catalog_exist.append(file_info)
        else:
            files_in_catalog_missing.append(file_info)
    
    # Check which directory files are in catalog or in cache
    files_in_directory_cataloged = []
    files_in_directory_cache = []
    files_in_directory_not_cataloged = []
    for file_path, file_info in directory_files.items():
        if file_path in catalog_files:
            file_info['in_catalog'] = True
            files_in_directory_cataloged.append(file_info)
        elif file_info['in_cache']:
            # File is in cache directory (already marked during directory scan)
            files_in_directory_cache.append(file_info)
        else:
            files_in_directory_not_cataloged.append(file_info)
    
    return jsonify({
        'catalog_id': catalog_id,
        'catalog_name': catalog.name,
        'target_directory': target_dir,
        'ingest_mode': getattr(catalog, 'ingest_mode', 'copy'),
        'source_root': catalog.source_root,
        'scanned_directory': scan_dir,
        'source_available': True,
        'verification_1': {
            'description': 'Files in catalog vs files in directory',
            'total_in_catalog': len(catalog_files),
            'exist_in_directory': len(files_in_catalog_exist),
            'missing_in_directory': len(files_in_catalog_missing),
            'files_exist': files_in_catalog_exist,
            'files_missing': files_in_catalog_missing
        },
        'verification_2': {
            'description': 'Files in directory vs files in catalog',
            'total_in_directory': len(directory_files),
            'cataloged': len(files_in_directory_cataloged),
            'cache': len(files_in_directory_cache),
            'not_cataloged': len(files_in_directory_not_cataloged),
            'files_cataloged': files_in_directory_cataloged,
            'files_cache': files_in_directory_cache,
            'files_not_cataloged': files_in_directory_not_cataloged
        }
    })


def run_workflow(execution_id, workflow, catalog):
    """Run a workflow in a separate thread"""
    import time
    import stat
    
    start_time = time.time()
    files_processed = 0
    files_failed = 0
    new_files = []
    
    # Ensure catalog has target_directory
    if not catalog.target_directory:
        raise Exception("Catalog target directory not set")
    
    # Create catalog-specific database instance
    catalog_db = CatalogDatabase(catalog.target_directory)
    execution_model = ExecutionModel(catalog_db)
    
    try:
        # Get source files
        source_dir = workflow['source_directory']
        target_dir = catalog.target_directory
        catalog_id = workflow['catalog_id']
        
        # Use the catalog instance passed in (already from catalogs.db)
        catalog_instance = catalog
        
        # Only validate and walk source directory if one is provided
        if source_dir:
            if not os.path.exists(source_dir):
                raise Exception(f"Source directory does not exist: {source_dir}")
        
        # Create target directory if it doesn't exist
        os.makedirs(target_dir, exist_ok=True)
        
        # Get existing files in target directory before execution
        existing_files = set()
        if os.path.exists(target_dir):
            for root, dirs, filenames in os.walk(target_dir):
                for filename in filenames:
                    existing_files.add(os.path.join(root, filename))
        
        # Get all files in source directory (only if source_dir is provided)
        files = []
        if source_dir:
            for root, dirs, filenames in os.walk(source_dir):
                for filename in filenames:
                    files.append(os.path.join(root, filename))
        
        total_files = len(files)
        execution_model.update_progress(execution_id, 0, total_files)
        
        # Emit initial progress
        socketio.emit('workflow_progress', {
            'execution_id': execution_id,
            'processed': 0,
            'total': total_files,
            'percentage': 0
        })
        
        # Load and execute the workflow script
        script_path = workflow['python_script']
        
        # Convert relative path to absolute path based on app directory
        if not os.path.isabs(script_path):
            app_dir = os.path.dirname(os.path.abspath(__file__))
            script_path = os.path.join(app_dir, script_path)
        
        if os.path.exists(script_path):
            # Load the module
            spec = importlib.util.spec_from_file_location("workflow_module", script_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["workflow_module"] = module
            spec.loader.exec_module(module)
            
            # Execute the workflow
            if hasattr(module, 'execute'):
                result = module.execute(
                    source_dir=source_dir,
                    target_dir=target_dir,
                    files=files,
                    progress_callback=lambda processed, total_override=None, _total=total_files: update_progress(
                        execution_id, processed,
                        # If the script passes an explicit total (catalog-based workflows),
                        # use it. Otherwise fall back to the source-dir file count.
                        total_override if total_override is not None else (_total if _total > 0 else processed),
                        catalog
                    ),
                    catalog_id=catalog_id,
                    catalog=catalog_instance,
                    execution_id=execution_id
                )
                
                # Get statistics from workflow execution if returned
                if isinstance(result, dict):
                    files_processed = result.get('files_processed', total_files)
                    files_failed = result.get('files_failed', 0)
                    files_skipped = result.get('skipped_files', 0)
                    # The import's breakdown, kept for the ingestion report.
                    # Its vocabulary, not a re-interpretation of it:
                    #   new_files      reached a placed state
                    #   skipped_files  content already in the catalog
                    #                  (duplicate / already_imported)
                    #   ignored_files  not media at all - sidecars, OS files
                    # Only an import returns these; everything else leaves them
                    # None and the columns stay NULL.
                    files_new = result.get('new_files')
                    files_duplicate = result.get('skipped_files')
                    files_ignored = result.get('ignored_files')
                else:
                    files_processed = total_files
                    files_failed = 0
                    files_skipped = 0
                    files_new = files_duplicate = files_ignored = None
            else:
                raise Exception("Workflow script must have an 'execute' function")
        else:
            raise Exception(f"Workflow script not found: {script_path}")
        
        # Note: File recording is now handled by the workflow scripts themselves
        # They add files to the catalog-specific database (workflow_results.db)
        # This prevents duplicate entries
        
        # Detect new files in target directory for reporting purposes only
        if os.path.exists(target_dir):
            for root, dirs, filenames in os.walk(target_dir):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    if file_path not in existing_files:
                        new_files.append(file_path)
        
        # Mark as completed (already recorded in workflow_executions table)
        execution_model.complete(execution_id, 'completed', skipped_files=files_skipped,
                                 new_files=files_new, duplicate_files=files_duplicate,
                                 ignored_files=files_ignored)
        # For catalog-based workflows (no source_dir), total_files is 0; use
        # files_processed from the script result as the final count instead.
        final_count = files_processed if total_files == 0 else total_files
        socketio.emit('workflow_progress', {
            'execution_id': execution_id,
            'processed': final_count,
            'total': final_count,
            'percentage': 100,
            'status': 'completed',
            'new_files_count': len(new_files),
            'skipped_files': files_skipped
        })
        
    except Exception as e:
        # Mark as failed (already recorded in workflow_executions table)
        execution_model.complete(execution_id, 'failed', str(e))
        socketio.emit('workflow_progress', {
            'execution_id': execution_id,
            'status': 'failed',
            'error': str(e)
        })

def update_progress(execution_id, processed, total, catalog):
    """Update execution progress"""
    if not catalog.target_directory:
        return
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    execution_model = ExecutionModel(catalog_db)
    execution_model.update_progress(execution_id, processed, total)
    percentage = int((processed / total) * 100) if total > 0 else 0
    
    socketio.emit('workflow_progress', {
        'execution_id': execution_id,
        'processed': processed,
        'total': total,
        'percentage': percentage
    })

# ─── Backups (documentation/specs/backup-backrest-spec.md) ──────────────────
#
# The Backup tab's cards. Reads are open to anyone with the catalog; creating,
# changing, running and restoring need its owner (auth._minimum_role_for_
# request). Secrets go in encrypted and never come back out. restic runs here,
# in this container (backups.py); a repository over SFTP is reached with
# Airlock's own SSH key (backup_ssh.py).

import backups as backup_store                         # noqa: E402
import backup_secrets                                  # noqa: E402
import backup_ssh                                      # noqa: E402
from backup_restic import Repo as ResticRepo, ResticError  # noqa: E402
import backup_borg                                     # noqa: E402
from backup_borg import BorgError                      # noqa: E402
import backup_rsync                                    # noqa: E402
from backup_rsync import RsyncError                    # noqa: E402

BACKUP_SCHEDULER_SECONDS = 60


def _backup_restore_root():
    """Where restores land: never inside a catalog (spec 7). BACKUP_RESTORE_ROOT,
    else airlock-restores in the storage root (the owner's home by default),
    else the database folder."""
    configured = os.environ.get('BACKUP_RESTORE_ROOT')
    if configured:
        return configured.rstrip('/')
    storage_root = os.environ.get('STORAGE_ROOT_CONTAINER', '/storage')
    if os.path.isdir(storage_root) and os.access(storage_root, os.W_OK):
        return os.path.join(storage_root, 'airlock-restores')
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database', 'restores')


def _backup_host_path(path):
    """A container path as the owner finds it on the machine, when it is under
    the storage root - so a restore's folder can be opened."""
    root = os.environ.get('STORAGE_ROOT_CONTAINER', '/storage').rstrip('/')
    host = os.environ.get('STORAGE_ROOT_HOST')
    if host and (path == root or path.startswith(root + '/')):
        return host.rstrip('/') + path[len(root):]
    return path


def _backup_slug(catalog):
    import re
    return getattr(catalog, 'slug', None) or \
        re.sub(r'[^a-z0-9]+', '-', (catalog.name or 'catalog').lower()).strip('-') or 'catalog'


def _backup_fingerprint(catalog):
    import dashboard
    conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'),
                           timeout=10)
    try:
        return dashboard.fingerprint(conn)
    finally:
        conn.close()


def _backup_sizes(catalog):
    """Each scope's size, from the Dashboard's cached counts when there are any."""
    import dashboard
    path = os.path.join(catalog.target_directory, 'workflow_results.db')
    conn = sqlite3.connect(path, timeout=10)
    try:
        cached = dashboard._read_stats(conn)
    finally:
        conn.close()
    if not cached:
        return None
    s = cached['stats']
    originals = s['library']['photos']['bytes'] + s['library']['videos']['bytes']
    database = dashboard.database_bytes(path)
    return {'originals': originals + database,
            'everything': originals + database + (s.get('catalog_bytes', {}).get('previews') or 0)}


def _backup_catalog(catalog_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return None, (jsonify({'error': 'Catalog not found'}), 404)
    if not catalog.target_directory:
        return None, (jsonify({'error': 'Catalog target directory not set'}), 400)
    return catalog, None


def _backup_card(conn, target, fingerprint):
    runs = backup_store.latest_runs(conn, target['id'])
    return backup_store.public(target, runs, backup_store.status(target, runs, fingerprint))


def _backup_target(catalog_id, backup_id):
    conn = db.get_connection()
    try:
        return backup_store.get_target(conn, catalog_id, backup_id)
    finally:
        conn.close()


def _current_user_id():
    """Who is signed in, or None with authentication off. current_user() is a
    sqlite3.Row - indexable, with no .get()."""
    user = auth.current_user()
    return user['id'] if user is not None else None


def _clean_schedule(data):
    s = data.get('schedule') or {}
    kind = s.get('kind') if s.get('kind') in ('manual', 'daily', 'weekly') else 'daily'
    time_ = str(s.get('time') or '02:00')
    if not _re_mod_match(r'^\d{2}:\d{2}$', time_):
        raise ValueError('The time must look like 02:00')
    weekday = int(s.get('weekday', 6))
    if not 0 <= weekday <= 6:
        raise ValueError('Pick a day of the week')
    return {'kind': kind, 'time': time_, 'weekday': weekday}


def _re_mod_match(pattern, value):
    import re
    return re.match(pattern, value) is not None


def _clean_retention(data):
    r = {**backup_store.DEFAULT_RETENTION, **(data.get('retention') or {})}
    out = {}
    for k in ('daily', 'weekly', 'monthly', 'yearly'):
        v = int(r[k])
        if not 0 <= v <= 1000:
            raise ValueError('Retention counts must be between 0 and 1000')
        out[k] = v
    return out


def _clean_env(values):
    """Extra repository environment (cloud keys): KEY=value lines."""
    out = []
    for item in values or []:
        item = str(item).strip()
        if not item:
            continue
        if not _re_mod_match(r'^[A-Z_][A-Z0-9_]*=', item):
            raise ValueError(f'"{item.split("=")[0]}" is not a KEY=value line')
        out.append(item)
    return out


def _backup_key_info():
    """Airlock's SSH key, for the backup host's authorized_keys."""
    try:
        info = {'public_key': backup_ssh.public_key(),
                'authorized_keys_line': backup_ssh.authorized_keys_line()}
    except Exception as exc:                       # ssh-keygen missing: old image
        return {'public_key': None, 'error': f'No SSH key: {exc}'}
    # borg's key is a second one: its line on the host forces `borg serve`.
    # The form builds that line from the repository and the borg path.
    try:
        info['borg'] = {'public_key': backup_ssh.public_key('borg'),
                        'default_remote_path': backup_borg.DEFAULT_REMOTE_PATH}
    except Exception as exc:
        info['borg'] = {'public_key': None, 'error': f'No SSH key: {exc}'}
    # And rsync's: its line locks it to one folder with rrsync.
    try:
        info['rsync'] = {'public_key': backup_ssh.public_key('rsync')}
    except Exception as exc:
        info['rsync'] = {'public_key': None, 'error': f'No SSH key: {exc}'}
    return info


def _clean_rsync_options(data, uri, current=None):
    """rsync's one option: the folder its key is locked to on the host (rrsync),
    by default the destination's parent."""
    opts = dict(current or {})
    root = ((data.get('options') or {}).get('root') or '').strip().rstrip('/')
    dest = backup_rsync.parse_dest(uri)
    if not dest:
        return {}
    if not root:
        root = opts.get('root') if 'options' not in data and opts.get('root') else \
            (os.path.dirname(dest[2].rstrip('/')) or '/')
    if not root.startswith('/') or not backup_borg.REMOTE_PATH.match(root):
        raise ValueError('The locked folder must be a plain absolute path, like /media/usb1/rsync')
    rel = os.path.relpath(dest[2], root)
    if rel.startswith('..') or rel == '.':
        raise ValueError(f'The destination must be inside {root}, the folder the rsync key is locked to')
    return {'root': root}


def _test_rsync_destination(data, catalog_id):
    """Test connection for an rsync card: reach the host and list the folder -
    or, with create, make it."""
    uri = (data.get('repo_uri') or '').strip()
    if not uri and data.get('backup_id'):
        t = _backup_target(catalog_id, int(data['backup_id']))
        uri = t['repo_uri'] if t else ''
    if not uri:
        return jsonify({'ok': False, 'error': 'Enter the destination'})
    try:
        current = backup_store.options_of(_backup_target(catalog_id, int(data['backup_id']))) \
            if data.get('backup_id') else None
        options = _clean_rsync_options(data, uri, current)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)})
    repo = backup_rsync.Repo(uri, options=options)
    out = {'repo_uri': uri, 'ssh': bool(repo.ssh_host()), 'root': options.get('root')}
    try:
        if repo.ssh_host():
            out['host_fingerprint'] = backup_ssh.trust_host(*repo.ssh_host())
        try:
            repo.probe()
            out['created'] = False
        except RsyncError as e:
            if e.kind != 'missing' or not data.get('create'):
                raise
            repo.init()
            repo.probe()
            out['created'] = True
    except RsyncError as e:
        return jsonify({**out, 'ok': False, 'error': str(e), 'kind': e.kind})
    except RuntimeError as e:
        return jsonify({**out, 'ok': False, 'error': str(e), 'kind': 'unreachable'})
    return jsonify({**out, 'ok': True, 'repo_id': None})


def _backup_kind(data, catalog_id):
    """The type a request is about: the card's, else the form's (restic by default)."""
    if data.get('backup_id'):
        t = _backup_target(catalog_id, int(data['backup_id']))
        if t:
            return t.get('type') or 'restic'
    return data.get('type') or 'restic'


def _clean_borg_options(data, current=None):
    """borg's one option: where borg is on the backup host."""
    opts = dict(current or {})
    if 'options' in data or not opts:
        rp = ((data.get('options') or {}).get('remote_path') or '').strip() \
            or backup_borg.DEFAULT_REMOTE_PATH
        if not backup_borg.REMOTE_PATH.match(rp):
            raise ValueError('The borg path on the server must be a plain path, like '
                             '/opt/borg/bin/borg')
        opts['remote_path'] = rp
    return opts


def _test_borg_repository(data, catalog_id):
    """Test connection for a borg card: reach the host, open the repository with
    the passphrase - or, with create, make it."""
    try:
        uri, password, env = _repo_from_request(data, catalog_id)
        current = backup_store.options_of(_backup_target(catalog_id, int(data['backup_id']))) \
            if data.get('backup_id') else None
        options = _clean_borg_options(data, current)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)})
    if not uri:
        return jsonify({'ok': False, 'error': 'Enter the repository'})
    if not password:
        return jsonify({'ok': False, 'error': 'Enter the repository passphrase'})
    repo = backup_borg.Repo(uri, password, env, options)
    host = repo.ssh_host()
    out = {'repo_uri': uri, 'ssh': bool(host)}
    try:
        if host:
            out['host_fingerprint'] = backup_ssh.trust_host(*host)
        try:
            out['repo_id'] = repo.probe()
            out['created'] = False
        except BorgError as e:
            if data.get('create') and e.kind == 'password':
                raise BorgError('A repository already exists here, and this passphrase does not '
                                'open it. Use its passphrase with "Use an existing repository", '
                                'or choose another folder.', 'exists')
            if e.kind != 'missing' or not data.get('create'):
                raise
            if len(password) < 8:
                return jsonify({**out, 'ok': False,
                                'error': 'A new repository needs a passphrase of at least 8 characters'})
            repo.init()
            out['repo_id'] = repo.probe()
            out['created'] = True
    except BorgError as e:
        return jsonify({**out, 'ok': False, 'error': str(e), 'kind': e.kind})
    except RuntimeError as e:                      # ssh-keyscan could not reach it
        return jsonify({**out, 'ok': False, 'error': str(e), 'kind': 'unreachable'})
    return jsonify({**out, 'ok': True})


@app.route('/api/catalogs/<int:catalog_id>/backups', methods=['GET'])
def list_backups(catalog_id):
    """The cards, from their own rows - nothing remote is asked."""
    catalog, err = _backup_catalog(catalog_id)
    if err:
        return err
    fingerprint = _backup_fingerprint(catalog)
    conn = db.get_connection()
    try:
        cards = [_backup_card(conn, t, fingerprint)
                 for t in backup_store.list_targets(conn, catalog_id)]
    finally:
        conn.close()
    restore_root = _backup_restore_root()
    return jsonify({'backups': cards, 'sizes': _backup_sizes(catalog),
                    'ssh': _backup_key_info(),
                    'restore_root': restore_root,
                    'restore_root_host': _backup_host_path(restore_root)})


def _repo_from_request(data, catalog_id):
    """The repository a form describes; a blank password on an existing card
    means the stored one."""
    uri = (data.get('repo_uri') or '').strip()
    password = data.get('repo_password') or ''
    env = _clean_env(data.get('repo_env'))
    if data.get('backup_id') and (not password or not data.get('repo_env')):
        t = _backup_target(catalog_id, int(data['backup_id']))
        if t:
            stored = backup_store.repo_for(t, app.secret_key)
            password = password or stored.password
            env = env or stored.extra_env
            uri = uri or t['repo_uri']
    return uri, password, env


@app.route('/api/catalogs/<int:catalog_id>/backups/test', methods=['POST'])
def test_backup_repository(catalog_id):
    """Test connection: reach the repository and open it with the password -
    or, with create, make it. Says which it found, and the SSH host's key."""
    catalog, err = _backup_catalog(catalog_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if _backup_kind(data, catalog_id) == 'borg':
        return _test_borg_repository(data, catalog_id)
    if _backup_kind(data, catalog_id) == 'rsync':
        return _test_rsync_destination(data, catalog_id)
    try:
        uri, password, env = _repo_from_request(data, catalog_id)
    except ValueError as e:
        return jsonify({'ok': False, 'error': str(e)})
    if not uri:
        return jsonify({'ok': False, 'error': 'Enter the repository'})
    if not password:
        return jsonify({'ok': False, 'error': 'Enter the repository password'})
    sftp = backup_ssh.parse_sftp(uri)
    out = {'repo_uri': uri, 'sftp': bool(sftp)}
    try:
        if sftp:
            out['host_fingerprint'] = backup_ssh.trust_host(sftp[1], sftp[2])
        repo = ResticRepo(uri, password, env)
        try:
            out['repo_id'] = repo.probe()
            out['created'] = False
        except ResticError as e:
            if data.get('create') and e.kind == 'password':
                raise ResticError('A repository already exists here, and this password does not '
                                  'open it. Use its password with "Use an existing repository", '
                                  'or choose another folder.', 'exists')
            if e.kind != 'missing' or not data.get('create'):
                raise
            if len(password) < 8:
                return jsonify({**out, 'ok': False,
                                'error': 'A new repository needs a password of at least 8 characters'})
            repo.init()
            out['repo_id'] = repo.probe()
            out['created'] = True
    except ResticError as e:
        return jsonify({**out, 'ok': False, 'error': str(e), 'kind': e.kind})
    except RuntimeError as e:                      # ssh-keyscan could not reach it
        return jsonify({**out, 'ok': False, 'error': str(e), 'kind': 'unreachable'})
    return jsonify({**out, 'ok': True})


@app.route('/api/catalogs/<int:catalog_id>/backups', methods=['POST'])
def create_backup(catalog_id):
    """A new card. The repository must already open with its password - the
    form's Test connection creates it when asked to."""
    catalog, err = _backup_catalog(catalog_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    scope = data.get('scope') or 'originals'
    if not name:
        return jsonify({'error': 'Give the backup a name'}), 400
    if scope not in backup_store.SCOPES:
        return jsonify({'error': 'Unknown scope'}), 400
    kind = data.get('type') or 'restic'
    if kind not in backup_store.TYPES:
        return jsonify({'error': 'Unknown backup type'}), 400
    try:
        schedule = _clean_schedule(data)
        retention = _clean_retention(data)
        grace = int(data.get('grace_hours') or backup_store.DEFAULT_GRACE_HOURS)
        uri, password, env = _repo_from_request({**data, 'backup_id': None}, catalog_id)
        options = _clean_borg_options(data) if kind == 'borg' else \
            _clean_rsync_options(data, uri) if kind == 'rsync' else None
    except (ValueError, TypeError) as e:
        return jsonify({'error': str(e)}), 400
    if not uri or (not password and kind != 'rsync'):     # an rsync copy has no password
        return jsonify({'error': 'The repository and its password are required'}), 400
    try:
        if kind == 'rsync':
            rsync_repo = backup_rsync.Repo(uri, options=options)
            if rsync_repo.ssh_host():
                backup_ssh.trust_host(*rsync_repo.ssh_host())
            rsync_repo.probe()
            password, env = None, []
        elif kind == 'borg':
            borg_repo = backup_borg.Repo(uri, password, env, options)
            if borg_repo.ssh_host():
                backup_ssh.trust_host(*borg_repo.ssh_host())
            borg_repo.probe()
        else:
            sftp = backup_ssh.parse_sftp(uri)
            if sftp:
                backup_ssh.trust_host(sftp[1], sftp[2])
            ResticRepo(uri, password, env).probe()
    except (ResticError, BorgError, RsyncError) as e:
        return jsonify({'error': str(e), 'kind': e.kind}), 400
    except RuntimeError as e:
        return jsonify({'error': str(e)}), 400

    conn = db.get_connection()
    try:
        try:
            cur = conn.execute('''
                INSERT INTO backup_targets (catalog_id, name, type, scope, repo_uri,
                    repo_secret_enc, repo_env_enc, schedule_json, schedule_tz,
                    retention_json, grace_hours, created_by, options_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''', (
                catalog_id, name, kind, scope, uri,
                backup_secrets.encrypt(app.secret_key, password),
                backup_secrets.encrypt(app.secret_key, _json.dumps(env)) if env else None,
                _json.dumps(schedule), data.get('schedule_tz') or 'UTC',
                _json.dumps(retention), grace, _current_user_id(),
                _json.dumps(options) if options else None))
            conn.commit()
        except sqlite3.IntegrityError:
            return jsonify({'error': f'This catalog already has a backup called "{name}"'}), 409
        target = backup_store.get_target(conn, catalog_id, cur.lastrowid)
        card = _backup_card(conn, target, _backup_fingerprint(catalog))
    finally:
        conn.close()
    return jsonify(card), 201


@app.route('/api/catalogs/<int:catalog_id>/backups/<int:backup_id>', methods=['PUT'])
def update_backup(catalog_id, backup_id):
    catalog, err = _backup_catalog(catalog_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    conn = db.get_connection()
    try:
        target = backup_store.get_target(conn, catalog_id, backup_id)
        if not target:
            return jsonify({'error': 'Backup not found'}), 404
        if backup_store.active(backup_id):
            return jsonify({'error': 'Wait for the run in progress to finish'}), 409
        fields = {}
        if 'name' in data and (data['name'] or '').strip():
            fields['name'] = data['name'].strip()
        if data.get('scope') in backup_store.SCOPES:
            fields['scope'] = data['scope']
        try:
            if 'schedule' in data:
                fields['schedule_json'] = _json.dumps(_clean_schedule(data))
            if 'retention' in data:
                fields['retention_json'] = _json.dumps(_clean_retention(data))
            if 'grace_hours' in data:
                fields['grace_hours'] = int(data['grace_hours'])
            is_borg = target.get('type') == 'borg'
            is_rsync = target.get('type') == 'rsync'
            options = _clean_borg_options(data, backup_store.options_of(target)) \
                if is_borg else _clean_rsync_options(
                    data, (data.get('repo_uri') or target['repo_uri']).strip(),
                    backup_store.options_of(target)) if is_rsync else None
            repo_changed = any(data.get(k) for k in ('repo_uri', 'repo_password')) \
                or 'repo_env' in data \
                or ((is_borg or is_rsync) and options != backup_store.options_of(target))
            if repo_changed:
                uri, password, env = _repo_from_request({**data, 'backup_id': backup_id},
                                                        catalog_id)
                if 'repo_env' in data:
                    env = _clean_env(data['repo_env'])
        except (ValueError, TypeError) as e:
            return jsonify({'error': str(e)}), 400
        if data.get('schedule_tz'):
            fields['schedule_tz'] = data['schedule_tz']
        if repo_changed:
            try:
                if is_rsync:
                    backup_rsync.Repo(uri, options=options).probe()
                    fields['options_json'] = _json.dumps(options)
                    password, env = None, []
                elif is_borg:
                    backup_borg.Repo(uri, password, env, options).probe()
                    fields['options_json'] = _json.dumps(options)
                else:
                    ResticRepo(uri, password, env).probe()
            except (ResticError, BorgError, RsyncError) as e:
                return jsonify({'error': str(e), 'kind': e.kind}), 400
            except RuntimeError as e:
                return jsonify({'error': str(e)}), 400
            fields.update(repo_uri=uri,
                          repo_secret_enc=backup_secrets.encrypt(app.secret_key, password),
                          repo_env_enc=backup_secrets.encrypt(app.secret_key, _json.dumps(env))
                          if env else None)
            if uri != target['repo_uri']:
                # Another repository: nothing there is this card's yet.
                fields.update(last_backup_at=None, last_backup_status=None,
                              last_snapshot_id=None, last_fingerprint=None, last_error=None)
        try:
            backup_store.update_target(conn, backup_id, **fields)
        except sqlite3.IntegrityError:
            return jsonify({'error': 'This catalog already has a backup with that name'}), 409
        target = backup_store.get_target(conn, catalog_id, backup_id)
        card = _backup_card(conn, target, _backup_fingerprint(catalog))
    finally:
        conn.close()
    return jsonify(card)


@app.route('/api/catalogs/<int:catalog_id>/backups/<int:backup_id>', methods=['DELETE'])
def delete_backup(catalog_id, backup_id):
    """The card and its history. NEVER the stored backups (spec 5.3)."""
    conn = db.get_connection()
    try:
        target = backup_store.get_target(conn, catalog_id, backup_id)
        if not target:
            return jsonify({'error': 'Backup not found'}), 404
        if backup_store.active(backup_id):
            return jsonify({'error': 'Stop the run in progress first'}), 409
        backup_store.delete_target(conn, backup_id)
    finally:
        conn.close()
    return jsonify({'deleted': backup_id})


@app.route('/api/catalogs/<int:catalog_id>/backups/<int:backup_id>/run', methods=['POST'])
def run_backup_now(catalog_id, backup_id):
    """Back up now: copy the database, then back up (in the background)."""
    catalog, err = _backup_catalog(catalog_id)
    if err:
        return err
    target = _backup_target(catalog_id, backup_id)
    if not target:
        return jsonify({'error': 'Backup not found'}), 404
    started = backup_store.start_backup(target, _backup_slug(catalog), catalog.target_directory,
                                        _backup_fingerprint(catalog), app.secret_key,
                                        db.db_path, db.get_connection)
    if not started:
        return jsonify({'error': 'This backup is already running'}), 409
    return jsonify({'started': True}), 202


@app.route('/api/catalogs/<int:catalog_id>/backups/<int:backup_id>/verify', methods=['POST'])
def verify_backup(catalog_id, backup_id):
    """Verify: restic check - the repository's structure, and with read_subset
    (e.g. "5%") that much of the data read back."""
    target = _backup_target(catalog_id, backup_id)
    if not target:
        return jsonify({'error': 'Backup not found'}), 404
    subset = ((request.get_json(silent=True) or {}).get('read_subset') or '').strip()
    if subset and not _re_mod_match(r'^\d{1,3}(\.\d+)?%$', subset):
        return jsonify({'error': 'read_subset is a percentage, like 5%'}), 400
    catalog = Catalog.get_by_id(catalog_id)
    if not backup_store.start_verify(target, app.secret_key, db.get_connection, subset or None,
                                     local_root=catalog.target_directory if catalog else None):
        return jsonify({'error': 'Something is already running for this backup'}), 409
    return jsonify({'started': True}), 202


@app.route('/api/catalogs/<int:catalog_id>/backups/<int:backup_id>/cancel', methods=['POST'])
def cancel_backup(catalog_id, backup_id):
    """Stop: restic stops cleanly, and the next backup reuses what was sent."""
    if not _backup_target(catalog_id, backup_id):
        return jsonify({'error': 'Backup not found'}), 404
    if not backup_store.cancel(backup_id):
        return jsonify({'error': 'Nothing is running for this backup'}), 409
    return jsonify({'stopping': True}), 202


@app.route('/api/catalogs/<int:catalog_id>/backups/<int:backup_id>/snapshots', methods=['GET'])
def list_backup_snapshots(catalog_id, backup_id):
    """This card's snapshots, newest first - what Restore offers."""
    catalog, err = _backup_catalog(catalog_id)
    if err:
        return err
    target = _backup_target(catalog_id, backup_id)
    if not target:
        return jsonify({'error': 'Backup not found'}), 404
    root = _backup_restore_root()
    if target.get('type') == 'rsync':
        # One copy, as of the last backup.
        out = [{'id': backup_rsync.SNAPSHOT_ID, 'short_id': 'copy', 'time': target['last_backup_at'],
                'host': None, 'paths': [], 'files': None, 'bytes': None}] \
            if target.get('last_backup_at') else []
        return jsonify({'snapshots': out, 'catalog_path': catalog.target_directory.rstrip('/'),
                        'restore_root': root, 'restore_root_host': _backup_host_path(root)})
    if target.get('type') == 'borg':
        try:
            out = backup_store.repo_for(target, app.secret_key).snapshots(
                backup_store.ident_for(target, _backup_slug(catalog)))
        except BorgError as e:
            return jsonify({'error': str(e), 'kind': e.kind}), 502
        return jsonify({'snapshots': out, 'catalog_path': catalog.target_directory.rstrip('/'),
                        'restore_root': root, 'restore_root_host': _backup_host_path(root)})
    try:
        snaps = backup_store.repo_for(target, app.secret_key).snapshots(
            backup_store.tags_for(target, _backup_slug(catalog)))
    except ResticError as e:
        return jsonify({'error': str(e), 'kind': e.kind}), 502
    out = []
    for s in snaps:
        summary = s.get('summary') or {}
        out.append({'id': s.get('id'), 'short_id': s.get('short_id') or (s.get('id') or '')[:8],
                    'time': s.get('time'), 'host': s.get('hostname'),
                    'paths': s.get('paths') or [],
                    'files': summary.get('total_files_processed'),
                    'bytes': summary.get('total_bytes_processed')})
    out.sort(key=lambda s: s['time'] or '', reverse=True)
    root = _backup_restore_root()
    return jsonify({'snapshots': out, 'catalog_path': catalog.target_directory.rstrip('/'),
                    'restore_root': root, 'restore_root_host': _backup_host_path(root)})


@app.route('/api/catalogs/<int:catalog_id>/backups/<int:backup_id>/restore', methods=['POST'])
def restore_backup(catalog_id, backup_id):
    """Restore to a folder - never over the live catalog (spec 7)."""
    import re
    catalog, err = _backup_catalog(catalog_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    target = _backup_target(catalog_id, backup_id)
    if not target:
        return jsonify({'error': 'Backup not found'}), 404
    snapshot_id = (data.get('snapshot_id') or '').strip()
    # restic names a snapshot by its hex id; borg an archive by its name.
    valid = backup_borg.ARCHIVE_NAME.match(snapshot_id) if target.get('type') == 'borg' \
        else snapshot_id == backup_rsync.SNAPSHOT_ID if target.get('type') == 'rsync' \
        else re.match(r'^[0-9a-f]{8,64}$', snapshot_id)
    if not valid:
        return jsonify({'error': 'Pick a snapshot'}), 400
    path = (data.get('path') or '').strip()
    if path and (not path.startswith('/') or '..' in path.split('/')):
        return jsonify({'error': 'The path to restore must be an absolute path in the snapshot'}), 400
    folder = re.sub(r'[^A-Za-z0-9._-]+', '-', (data.get('folder') or '').strip()).strip('-.') \
        or f'{_backup_slug(catalog)}-{_datetime.now().strftime("%Y%m%d-%H%M%S")}'
    restore_to = os.path.join(_backup_restore_root(), folder)
    if os.path.exists(restore_to) and os.listdir(restore_to):
        return jsonify({'error': f'{folder} already exists and is not empty - pick another name'}), 409
    if not backup_store.start_restore(target, app.secret_key, db.get_connection,
                                      snapshot_id, path or None, restore_to,
                                      local_root=catalog.target_directory):
        return jsonify({'error': 'Something is already running for this backup'}), 409
    return jsonify({'started': True, 'target': restore_to,
                    'target_host': _backup_host_path(restore_to)}), 202


# ── The schedule: Airlock starts each card's runs (backups.py docstring) ────

def _import_running(catalog):
    """A run waits for an import - in move mode it deletes source files, and a
    snapshot should not catch them half-moved (spec 8)."""
    try:
        conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'),
                               timeout=10)
        try:
            return conn.execute(
                "SELECT 1 FROM workflow_executions e JOIN workflows w ON w.id = e.workflow_id "
                "WHERE e.status = 'running' AND w.python_script LIKE '%io_picture_import%' "
                "LIMIT 1").fetchone() is not None
        finally:
            conn.close()
    except sqlite3.Error:
        return False


def _backup_scheduler_tick():
    conn = db.get_connection()
    try:
        targets = backup_store.list_targets(conn)
    finally:
        conn.close()
    for t in targets:
        if not backup_store.is_due(t) or backup_store.active(t['id']):
            continue
        catalog = Catalog.get_by_id(t['catalog_id'])
        if not catalog or not catalog.target_directory or _import_running(catalog):
            continue
        print(f'[backup] scheduled run: {t["name"]} (catalog {t["catalog_id"]})')
        backup_store.start_backup(t, _backup_slug(catalog), catalog.target_directory,
                                  _backup_fingerprint(catalog), app.secret_key,
                                  db.db_path, db.get_connection)


_backup_scheduler = None


def _start_backup_scheduler():
    """Once per process, and off wherever the queue runner is off (tests, shells).
    Runs a previous process left marked running are marked interrupted first."""
    global _backup_scheduler
    if os.environ.get('DISABLE_WORKFLOW_SCHEDULER', '').lower() in ('1', 'true', 'yes'):
        return None
    if _backup_scheduler is not None:
        return _backup_scheduler
    try:
        conn = db.get_connection()
        try:
            n = backup_store.interrupt_stale_runs(conn)
        finally:
            conn.close()
        if n:
            print(f'[backup] {n} run(s) cut off by the restart marked interrupted')
    except Exception as e:
        print(f'[backup] could not mark interrupted runs: {e}')

    def _loop():
        while True:
            try:
                _backup_scheduler_tick()
            except Exception as e:
                print(f'[backup] scheduler tick failed: {e}')
            time.sleep(BACKUP_SCHEDULER_SECONDS)

    _backup_scheduler = threading.Thread(target=_loop, daemon=True, name='backup-scheduler')
    _backup_scheduler.start()
    return _backup_scheduler


_start_backup_scheduler()


# ─── Dashboard (documentation/specs/dashboard-specs.md) ──────────────────────
#
# One request draws the page: the cached stats (served at once, rebuilt in the
# background when stale), today's On this day, and the live parts - activity,
# failed runs, alerts, disks, birthdays, albums - each milliseconds.

def _dashboard_coverage(conn):
    """The Content report's "What we know about it", as last built - read from
    its cache only. Checking the cache is current costs the report a COUNT(*)
    over every file (0.3 s on family), too much for every Dashboard visit, so
    the card says when it was counted and links to the report to recount."""
    import content_report
    try:
        row = conn.execute('SELECT value FROM catalog_properties WHERE key = ?',
                           (content_report.CACHE_KEY,)).fetchone()
        cached = _json.loads(row[0]) if row else None
    except (sqlite3.Error, ValueError, TypeError):
        cached = None
    if not cached or cached.get('schema_version') != content_report.SCHEMA_VERSION:
        return None
    return {'rows': cached.get('coverage') or [], 'generated_at': cached.get('generated_at')}


def _dashboard_payload(catalog, stats_value):
    import dashboard
    import storage_alerts
    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        payload = {
            'catalog': {'id': catalog.id, 'name': catalog.name},
            'stats': stats_value['stats'],
            'stats_computed_at': stats_value.get('computed_at'),
            'stats_stale': bool(stats_value.get('stale')),
            'stats_rebuilding': dashboard.building(db_path),
            'on_this_day': dashboard.on_this_day(conn),
            'live': dashboard.live(conn),
        }
        payload['live']['coverage'] = _dashboard_coverage(conn)
        import saved_searches
        payload['live']['saved_searches_due'] = saved_searches.due_soon(conn)
    finally:
        conn.close()
    payload['live']['storage'] = dashboard.storage(
        catalog, storage.list_routes(catalog_id=catalog.id))
    payload['live']['database_bytes'] = dashboard.database_bytes(db_path)
    app_conn = db.get_connection()
    try:
        payload['live']['alerts'] = storage_alerts.summary(app_conn)
        # Backups, from each card's own rows - nothing remote is asked, so
        # the Dashboard stays a few milliseconds (backup spec 10).
        targets = backup_store.list_targets(app_conn, catalog.id)
        runs = {t['id']: backup_store.latest_runs(app_conn, t['id'], 3) for t in targets}
    finally:
        app_conn.close()
    fp = None
    if targets:
        fp_conn = sqlite3.connect(db_path, timeout=10)
        try:
            fp = dashboard.fingerprint(fp_conn)
        finally:
            fp_conn.close()
    cards = []
    for t in targets:
        card = backup_store.public(t, runs[t['id']],
                                   backup_store.status(t, runs[t['id']], fp))
        cards.append({k: card[k] for k in ('id', 'name', 'scope', 'repo_uri', 'status',
                                           'last_backup', 'last_verify', 'next_run',
                                           'schedule')})
    states = [c['status']['state'] for c in cards]
    # The mini calendar (calendar-view-spec.md 7): a slice of the cached
    # counts, never a count of its own - a catalog not yet counted gets it on
    # a later visit.
    try:
        import calendar_counts
        counts = calendar_counts.get(db_path, _PHOTO_TYPES, wait=False)
        payload['live']['calendar'] = calendar_counts.recent(counts) if counts else None
    except Exception as e:
        print(f'[dashboard] calendar slice unavailable: {e}')
        payload['live']['calendar'] = None
    payload['live']['backups'] = {'count': len(targets),
                                  'needed': states.count('needed'),
                                  'failed': states.count('failed'),
                                  'running': states.count('running'),
                                  'cards': cards}
    return payload


def _dashboard_db_path(catalog):
    """The catalog's database, set up only if it does not exist yet: building a
    CatalogDatabase re-checks the schema, ~20 ms - the rest of the page's
    reads together are a few ms."""
    path = os.path.join(catalog.target_directory, 'workflow_results.db')
    if not os.path.exists(path):
        CatalogDatabase(catalog.target_directory)
    return path


@app.route('/api/catalogs/<int:catalog_id>/dashboard', methods=['GET'])
def get_dashboard(catalog_id):
    """The Dashboard in one response - under 50 ms with a cache."""
    import dashboard
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    db_path = _dashboard_db_path(catalog)
    metric = _resolve_metric_column(None)[0]
    conn = sqlite3.connect(db_path, timeout=30)
    try:
        value = dashboard.stats(conn, db_path, metric)
    finally:
        conn.close()
    return jsonify(_dashboard_payload(catalog, value))


@app.route('/api/catalogs/<int:catalog_id>/dashboard/refresh', methods=['POST'])
def refresh_dashboard(catalog_id):
    """"Refresh stats": count now (about 2 s on `family`), then answer."""
    import dashboard
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    db_path = _dashboard_db_path(catalog)
    value = dashboard.rebuild(db_path, _resolve_metric_column(None)[0])
    return jsonify(_dashboard_payload(catalog, dict(value, stale=False)))


@app.route('/api/catalogs/<int:catalog_id>/content-report', methods=['GET'])
def get_content_report(catalog_id):
    """
    What is in this catalog, and what is missing from it.

    documentation/specs/contents-spec.md. Every section in one reply rather than
    a request per card: the sections share a connection and several share a
    scan, and a page that issued nine requests would pay nine times for the
    same work.

    ?refresh=1 recomputes rather than reading the cache. The cache is keyed on a
    fingerprint of the catalog, not on age, so this is only needed to time the
    build or after changing the queries.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    refresh = request.args.get('refresh') in ('1', 'true', 'yes')
    data = content_report.report(CatalogDatabase(catalog.target_directory), refresh=refresh)
    data['catalog_id'] = catalog_id
    data['catalog_name'] = catalog.name
    return jsonify(data)


@app.route('/api/catalogs/<int:catalog_id>/content-by-time', methods=['GET'])
def get_content_by_time(catalog_id):
    """Get content distribution by time (year/month) for a catalog"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    # Get year range from query parameters (optional)
    start_year = request.args.get('start_year', type=int)
    end_year = request.args.get('end_year', type=int)
    
    # Get files from catalog database with timestamps
    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    
    # Query to get file counts by year and month using master_datetime
    # Grouped by TYPE and by DATE SOURCE as well as by month, so the chart can
    # separate a year of video from a year of photographs and mark the months
    # whose position on the axis was inferred from a path rather than read from
    # a camera (contents-spec.md sections 5.2 and 5.3).
    cursor.execute('''
        SELECT
            strftime('%Y', master_datetime) as year,
            strftime('%m', master_datetime) as month,
            LOWER(COALESCE(file_type, '')) as ext,
            COALESCE(master_datetime_source, 'unknown') as date_source,
            COUNT(*) as file_count
        FROM file_contents
        WHERE master_datetime IS NOT NULL AND removed_at IS NULL
        GROUP BY year, month, ext, date_source
        ORDER BY year, month
    ''')

    rows = cursor.fetchall()
    conn.close()

    # Process the data
    data_by_year_month = {}
    all_years = set()

    for row in rows:
        year = row['year']
        month = row['month']
        count = row['file_count']

        if year and month:
            all_years.add(int(year))
            bucket = data_by_year_month.setdefault(year, {}).setdefault(
                month, {'count': 0, 'videos': 0, 'stills': 0, 'inferred': 0})
            bucket['count'] += count
            if row['ext'] in VIDEO_EXTENSIONS:
                bucket['videos'] += count
            else:
                bucket['stills'] += count
            # Anything but the camera's own timestamp is a guess at where this
            # file belongs on the axis.
            if row['date_source'] != 'exif':
                bucket['inferred'] += count
    
    # Determine year range
    if all_years:
        min_year = min(all_years)
        max_year = max(all_years)
        
        # If no range specified, use the most recent 10 years
        if not end_year:
            end_year = max_year
        if not start_year:
            start_year = max(min_year, end_year - 9)  # 10 years total
    else:
        # No data available
        return jsonify({
            'catalog_id': catalog_id,
            'catalog_name': catalog.name,
            'data': [],
            'available_years': [],
            'start_year': None,
            'end_year': None,
            'has_previous': False,
            'has_next': False
        })
    
    # Build response data for the requested year range
    result_data = []
    for year in range(start_year, end_year + 1):
        year_str = str(year)
        year_data = {
            'year': year,
            'months': []
        }
        
        # Add data for each month (1-12)
        for month in range(1, 13):
            month_str = f'{month:02d}'
            bucket = (data_by_year_month.get(year_str, {}).get(month_str)
                      or {'count': 0, 'videos': 0, 'stills': 0, 'inferred': 0})

            year_data['months'].append({
                'month': month,
                'month_name': ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
                              'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'][month - 1],
                'count': bucket['count'],
                'stills': bucket['stills'],
                'videos': bucket['videos'],
                # How many of this month's files are placed here by inference.
                'inferred': bucket['inferred'],
            })
        
        result_data.append(year_data)
    
    # Determine if there are more years available
    has_previous = start_year > min_year
    has_next = end_year < max_year
    
    return jsonify({
        'catalog_id': catalog_id,
        'catalog_name': catalog.name,
        'data': result_data,
        'available_years': sorted(list(all_years)),
        'start_year': start_year,
        'end_year': end_year,
        'min_year': min_year,
        'max_year': max_year,
        'has_previous': has_previous,
        'has_next': has_next
    })

# People API endpoints
@app.route('/api/catalogs/<int:catalog_id>/people', methods=['GET'])
def get_people(catalog_id):
    """
    People in a catalog - or pets, which are the same rows with kind='pet'.

    ?kind=person (THE DEFAULT) | pet | all

    The default is what keeps every existing caller correct, including the face
    tagger, which must never offer a pet as an identity for a human face. See
    PeopleModel.get_all_people and the spec's section 7.2.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    kind = request.args.get('kind', 'person')
    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    try:
        people = people_model.get_all_people(kind)
    except ValueError as e:
        return jsonify({'error': str(e)}), 400

    # Resolve profile_photo_file_id to a display path for each person
    if people:
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        for person in people:
            if person.get('profile_photo_file_id'):
                cursor.execute('''
                    SELECT fc.file_path, fc.file_name,
                           av.alternative_file_path, av.alternative_file_name
                    FROM file_contents fc
                    LEFT JOIN (
                        SELECT original_file_id, alternative_file_path, alternative_file_name
                        FROM alternative_versions
                        WHERE rung IN ('1200','1600','2048','2560')
                        ORDER BY (width >= 1200) DESC,
                                 CASE WHEN width >= 1200 THEN width END ASC,
                                 width DESC, id ASC
                    ) av ON av.original_file_id = fc.id
                    WHERE fc.id = ?
                    LIMIT 1
                ''', (person['profile_photo_file_id'],))
                pf_row = cursor.fetchone()
                if pf_row:
                    raw = (
                        pf_row['alternative_file_path'] if pf_row['alternative_file_path']
                        else pf_row['file_path']
                    )
                    person['profile_photo_display_path'] = _resolve_path(raw, catalog.target_directory)

            # Resolve profile_photo_path (face crop) to absolute so the frontend
            # can detect it as a pre-cropped image regardless of storage format.
            if person.get('profile_photo_path'):
                person['profile_photo_path'] = _resolve_path(
                    person['profile_photo_path'], catalog.target_directory
                )

            if person.get('profile_photo_file_id'):
                # Get face detection bounding box for this person in the profile photo file
                # IMPORTANT: Use image_width and image_height from face_detections table
                # These store the actual dimensions of the image used for detection (after orientation)
                cursor.execute('''
                    SELECT fd.x, fd.y, fd.w, fd.h,
                           fd.image_width AS img_width, fd.image_height AS img_height
                    FROM face_detections fd
                    WHERE fd.file_id = ? AND fd.person_id = ?
                    LIMIT 1
                ''', (person['profile_photo_file_id'], person['id']))
                face_row = cursor.fetchone()
                if face_row and face_row['x'] is not None:
                    person['profile_photo_face_box'] = {
                        'x': face_row['x'],
                        'y': face_row['y'],
                        'w': face_row['w'],
                        'h': face_row['h'],
                        'img_width': face_row['img_width'],
                        'img_height': face_row['img_height'],
                    }

        # face_count: how many photographs each person is tagged in. ONE grouped
        # pass, not a COUNT per person: face_detections has no index on
        # person_id, so each per-person count read the whole table - 2.4 of
        # this route's 2.45 s on `family` (31 people, ~480,000 detections).
        # The single pass is 0.05 s and gives the same numbers.
        counts = {row['person_id']: row['n'] for row in cursor.execute('''
            SELECT person_id, COUNT(DISTINCT file_id) AS n
            FROM face_detections
            WHERE person_id IS NOT NULL
            GROUP BY person_id
        ''')}
        for person in people:
            person['face_count'] = counts.get(person['id'], 0)
        conn.close()

    # A pet's card picture.
    #
    # Not the same resolution as a person's. A person's profile photo is the
    # whole file plus a face box the frontend crops to; a pet already HAS a
    # crop on disk, written by the identify pass and served by
    # /pet-detections/<id>/crop, so the card can point straight at it. Doing
    # it any other way would re-crop an animal that has already been cropped.
    if people and kind in ('pet', 'all'):
        _attach_pet_covers(catalog_db, catalog_id,
                           [p for p in people
                            if (p.get('kind') or 'person') == 'pet'])

    # Add tags for each person
    if people:
        for person in people:
            tag_model = TagModel(catalog_db)
            person['tags'] = tag_model.get_tags_for_person(person['id'])

    return jsonify(people)


def _attach_pet_covers(catalog_db, catalog_id, pets):
    """
    Give each pet a `cover_crop_url`, and say which detection it came from.

    Two sources, in order:

      1. The STARRED one. people.profile_photo_file_id holds the FILE, not the
         detection - deliberately, because a file id is permanent while a
         detection id is not: re-running detection deletes and re-inserts the
         automatic boxes for a file, so a pointer to a detection id would come
         back dangling after every pass. The detection is looked up by (file,
         pet) at read time instead.
      2. Failing that, the pet's most confident detection that actually has a
         crop on disk. A card with no picture is worse than a card with a
         picture nobody chose, and this way one appears the moment a group is
         named rather than waiting for somebody to pick a favourite.
    """
    if not pets:
        return
    conn = catalog_db.get_connection()
    try:
        cur = conn.cursor()
        for pet in pets:
            row = None
            if pet.get('profile_photo_file_id'):
                row = cur.execute(
                    'SELECT pd.id, pd.file_id FROM pet_detections pd '
                    '  JOIN ai_pet_contents apc ON apc.pet_detection_id = pd.id '
                    ' WHERE pd.person_id = ? AND pd.file_id = ? '
                    ' ORDER BY pd.confidence DESC LIMIT 1',
                    (pet['id'], pet['profile_photo_file_id'])).fetchone()
            if row is None:
                row = cur.execute(
                    'SELECT pd.id, pd.file_id FROM pet_detections pd '
                    '  JOIN ai_pet_contents apc ON apc.pet_detection_id = pd.id '
                    ' WHERE pd.person_id = ? '
                    ' ORDER BY pd.confidence DESC LIMIT 1',
                    (pet['id'],)).fetchone()
            if row:
                pet['profile_detection_id'] = row['id']
                pet['cover_crop_url'] = _pet_crop_url(catalog_id, row['id'], row['file_id'])
            # How many photographs this animal appears in - the pet equivalent
            # of face_count, which counts face_detections and so is always 0
            # for a pet.
            pet['photo_count'] = cur.execute(
                'SELECT COUNT(DISTINCT file_id) FROM pet_detections '
                ' WHERE person_id = ?', (pet['id'],)).fetchone()[0]
    finally:
        conn.close()

@app.route('/api/catalogs/<int:catalog_id>/people', methods=['POST'])
def create_person(catalog_id):
    """Create a new person in a catalog"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    data = request.json
    if not data or not data.get('name'):
        return jsonify({'error': 'Name is required'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    
    kind = data.get('kind', 'person')
    try:
        person_id = people_model.create_person(
            name=data['name'],
            date_of_birth=data.get('date_of_birth'),
            description=data.get('description'),
            profile_photo_path=data.get('profile_photo_path'),
            category=data.get('category') or 'Other',
            kind=kind,
            species=data.get('species'),
        )
    except ValueError as e:
        return jsonify({'error': str(e)}), 400
    
    noun = 'Pet' if kind == 'pet' else 'Person'
    return jsonify({'id': person_id, 'kind': kind,
                    'message': f'{noun} created successfully'}), 201

@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>', methods=['GET'])
def get_person(catalog_id, person_id):
    """Get a specific person with their photos"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404
    
    # Get all photos for this person
    photos = people_model.get_photos_by_person(person_id)
    person['photos'] = photos
    
    # Get available years from people_photos
    years = set(people_model.get_years_for_person(person_id))

    # Also include years from face_detections
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT DISTINCT
            CAST(SUBSTR(fc.master_datetime, 1, 4) AS INTEGER) as year
        FROM face_detections fd
        JOIN file_contents fc ON fd.file_id = fc.id
        WHERE fd.person_id = ? AND fc.removed_at IS NULL
          AND CAST(SUBSTR(fc.master_datetime, 1, 4) AS INTEGER) > 0
    ''', (person_id,))
    for row in cursor.fetchall():
        if row['year']:
            years.add(row['year'])

    # Resolve profile_photo_file_id to a display path if set
    if person.get('profile_photo_file_id'):
        cursor.execute('''
            SELECT fc.file_path, fc.file_name,
                   av.alternative_file_path, av.alternative_file_name
            FROM file_contents fc
            LEFT JOIN (
                SELECT original_file_id, alternative_file_path, alternative_file_name
                FROM alternative_versions
                WHERE rung IN ('1200','1600','2048','2560')
                ORDER BY (width >= 1200) DESC,
                         CASE WHEN width >= 1200 THEN width END ASC,
                         width DESC, id ASC
            ) av ON av.original_file_id = fc.id
            WHERE fc.id = ?
            LIMIT 1
        ''', (person['profile_photo_file_id'],))
        pf_row = cursor.fetchone()
        if pf_row:
            raw = (
                pf_row['alternative_file_path'] if pf_row['alternative_file_path']
                else pf_row['file_path']
            )
            person['profile_photo_display_path'] = _resolve_path(raw, catalog.target_directory)

    # Resolve profile_photo_path (face crop) to absolute so the frontend
    # can detect it as a pre-cropped image regardless of storage format.
    if person.get('profile_photo_path'):
        person['profile_photo_path'] = _resolve_path(
            person['profile_photo_path'], catalog.target_directory
        )

    # The face's box in the profile photograph, as the people list sends it, so
    # the detail page's picture can be cropped to the face rather than show
    # the whole scene.
    if person.get('profile_photo_file_id'):
        cursor.execute('''
            SELECT fd.x, fd.y, fd.w, fd.h,
                   fd.image_width AS img_width, fd.image_height AS img_height
            FROM face_detections fd
            WHERE fd.file_id = ? AND fd.person_id = ?
            LIMIT 1
        ''', (person['profile_photo_file_id'], person_id))
        face_row = cursor.fetchone()
        if face_row and face_row['x'] is not None:
            person['profile_photo_face_box'] = {k: face_row[k] for k in
                                                ('x', 'y', 'w', 'h', 'img_width', 'img_height')}

    conn.close()

    person['years'] = sorted(years, reverse=True)

    # Add tags for this person
    tag_model = TagModel(catalog_db)
    person['tags'] = tag_model.get_tags_for_person(person_id)

    return jsonify(person)

@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>', methods=['PUT'])
def update_person(catalog_id, person_id):
    """Update a person's details"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404
    
    data = dict(request.json or {})

    # `kind` cannot be changed after creation.
    #
    # Turning a dog into a person would orphan its pet_detections into a row
    # the face chain now treats as a reference, which is precisely the
    # contamination the separate tables exist to make impossible. Turning a
    # person into a pet would do the mirror image. Rejected rather than
    # silently dropped, so a caller that tries learns why.
    if 'kind' in data and data['kind'] != (person.get('kind') or 'person'):
        return jsonify({
            'error': 'kind cannot be changed after creation',
            'detail': "a pet's detections live in pet_detections and a person's "
                      'in face_detections; changing kind would leave them in the '
                      'wrong one. Delete and recreate instead.',
        }), 400
    data.pop('kind', None)

    if data:
        people_model.update_person(person_id, **data)
    
    return jsonify({'message': 'Person updated successfully'})

# ── Pets ─────────────────────────────────────────────────────────────────────
#
# A pet is a `people` row with kind='pet', so creating, renaming, categorising,
# tagging and photographing one all go through the endpoints above with
# ?kind=pet. What lives here is only what has no person equivalent: the
# detections, the crops, the groups, and the switch.
#
# See documentation/specs/pet-recognition-spec.md section 7.

def _pet_catalog(catalog_id):
    """(catalog, catalog_db, db_path) or (None, error_response, None)."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return None, (jsonify({'error': 'Catalog not found'}), 404), None
    if not catalog.target_directory:
        return None, (jsonify({'error': 'Catalog target directory not set'}), 400), None
    return (catalog, CatalogDatabase(catalog.target_directory),
            os.path.join(catalog.target_directory, 'workflow_results.db'))


def _queue_pets_changed(catalog, detail=None):
    """
    A human just named, or unnamed, an animal.

    The mirror of _queue_face_confirmed, and it matters for the same reason:
    naming a pet changes the REFERENCE SET the scoring pass compares against.
    Without this, somebody names Rex from one group of five crops and the other
    four hundred photographs of him stay unidentified until they happen to run
    Identify Pets by hand - they would see the feature "work" once and then
    appear to do nothing.

    Raised only from the HTTP request path. pe_identify_pets.py also writes
    pet_detections.person_id, and if that could reach here the chain would
    re-enqueue the workflow that produced it, forever. It cannot: workflow
    scripts run as separate processes and never call this.

    Never raises: a scheduling failure must not fail the naming.
    """
    try:
        workflow_triggers.fire(catalog, 'pets_changed', detail=detail or {})
        _emit_queue(catalog)
    except Exception as e:
        print(f'[queue] could not queue the pets_changed chain: {e}')


@app.route('/api/catalogs/<int:catalog_id>/pets/settings', methods=['GET'])
def get_pet_settings(catalog_id):
    """
    Whether this catalog looks for pets, and what it has found.

    The counts are here so the page can say "4,812 animals found, 2 named, 7
    groups waiting" rather than offering a bare toggle - a switch with no
    feedback is a switch nobody trusts.
    """
    catalog, catalog_db, db_path = _pet_catalog(catalog_id)
    if catalog is None:
        return catalog_db

    enabled = models.get_catalog_property(db_path, 'pets_enabled') == '1'
    conn = catalog_db.get_connection()
    cur = conn.cursor()

    def count(sql):
        try:
            return cur.execute(sql).fetchone()[0]
        except Exception:
            return 0

    out = {
        'enabled': enabled,
        'detections': count('SELECT COUNT(*) FROM pet_detections'),
        'named_detections': count('SELECT COUNT(*) FROM pet_detections '
                                  ' WHERE person_id IS NOT NULL'),
        'pets': count("SELECT COUNT(*) FROM people WHERE COALESCE(kind,'person')='pet'"),
        'embedded': count('SELECT COUNT(*) FROM ai_pet_contents '
                          ' WHERE embedding IS NOT NULL'),
        'unresolved_groups': count('SELECT COUNT(*) FROM pet_clusters '
                                   ' WHERE resolved_person_id IS NULL'),
        'species': {},
    }
    try:
        for row in cur.execute('SELECT species, COUNT(*) FROM pet_detections '
                               ' WHERE species IS NOT NULL GROUP BY species'):
            out['species'][row[0]] = row[1]
    except Exception:
        pass
    conn.close()
    return jsonify(out)


def apply_pets_enabled(target_directory, catalog_db, enabled):
    """
    Turn pet detection on or off for one catalog. Returns (created, removed).

    THE ONLY WRITER of pets_enabled, and that is the point of it being a
    function rather than an endpoint. The switch lives on the catalog form now,
    so it arrives through POST /catalogs and PUT /catalogs/<id>; there used to
    be a third door at PUT /catalogs/<id>/pets/settings, and two endpoints that
    can set one property are two endpoints that can disagree about it.

    Switching ON creates the two workflows, because `workflows` has no
    `enabled` column - existence IS the switch, the same mechanism a storage
    route uses for its import workflow. Switching OFF deletes them and LEAVES
    THE DATA ALONE: detections, crops, groups and named pets all survive,
    because turning the feature off is not a request to forget the household's
    animals, and turning it back on should find them where they were.

    Raises nothing the caller must handle except what create/remove raise - the
    property is what gates detection and is written first, so a failure to
    adjust the workflow rows leaves the gate correct.
    """
    import workflow_defaults

    db_path = os.path.join(target_directory, 'workflow_results.db')
    models.set_catalog_property(db_path, 'pets_enabled', '1' if enabled else '0')
    if enabled:
        return workflow_defaults.create_pet_workflows(catalog_db), []
    return [], workflow_defaults.remove_pet_workflows(catalog_db)


def pets_default_for_new_catalog(data, reattaching, target_directory):
    """
    Whether a catalog being created should look for animals.

    Three rules, and each is a decision someone could reasonably get wrong,
    which is why this is a function with a test rather than an expression
    inside create_catalog:

      1. An EXPLICIT value wins. The catalog form always sends one.
      2. A RE-ATTACHED catalog keeps its own recorded answer. The same reason
         ingest_mode works this way: that folder has been looking for animals,
         or deliberately not, and a default must not overrule a decision
         already stored with the data.
      3. Otherwise ON. A household with pets should not have to find a setting
         before photographing them; one without pets turns it off on the same
         form and loads no model at all.
    """
    if 'pets_enabled' in data:
        return bool(data['pets_enabled'])
    if reattaching:
        return pets_enabled_for(target_directory)
    return True


def pets_enabled_for(target_directory):
    """
    Whether this catalog looks for animals.

    Absent means OFF, and that is deliberate even though new catalogs are
    created with it ON. A catalog made before the switch existed has no
    property, and reading absence as "on" would silently start the detector,
    create the schema and add two workflows to every catalog already on disk.
    New catalogs get an explicit '1' written at creation instead.
    """
    if not target_directory:
        return False
    return models.get_catalog_property(
        os.path.join(target_directory, 'workflow_results.db'),
        'pets_enabled') == '1'


@app.route('/api/catalogs/<int:catalog_id>/pets/<int:person_id>/detections',
           methods=['GET'])
def get_pet_detections(catalog_id, person_id):
    """
    The photographs this pet appears in, newest first, ONE PAGE at a time.

        ?limit= (default 60, at most 500)  &offset=  &year=

    Returns {detections, total, limit, offset, years: {year: count}}. `total`
    and `years` count everything, so the page can say how many there are and
    head each year correctly while showing only a page of them.

    It returned a bare list capped at 500 - silently: Luna has ~5,000
    photographs, so her page drew 500 thumbnails at once, said "500", and the
    other 4,500 could not be reached.
    """
    catalog, catalog_db, _ = _pet_catalog(catalog_id)
    if catalog is None:
        return catalog_db

    year = request.args.get('year')
    try:
        limit = max(1, min(500, int(request.args.get('limit', 60))))
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify({'error': 'limit and offset must be numbers'}), 400
    conn = catalog_db.get_connection()
    cur = conn.cursor()
    sql = """
        SELECT pd.id, pd.file_id, pd.x, pd.y, pd.w, pd.h, pd.confidence,
               pd.species, pd.source, pd.image_width, pd.image_height,
               fc.file_path, fc.file_name, fc.master_datetime,
               p.species AS person_species,
               apc.id AS crop_id
          FROM pet_detections pd
          JOIN file_contents fc ON fc.id = pd.file_id
     LEFT JOIN people p ON p.id = pd.person_id
     LEFT JOIN ai_pet_contents apc ON apc.pet_detection_id = pd.id
         WHERE pd.person_id = ? AND fc.removed_at IS NULL
    """
    params = [person_id]
    if year:
        sql += " AND substr(fc.master_datetime,1,4) = ? "
        params.append(str(year))
    try:
        years = {r[0] or 'undated': r[1] for r in cur.execute(
            'SELECT substr(fc.master_datetime, 1, 4), COUNT(*) '
            '  FROM pet_detections pd JOIN file_contents fc ON fc.id = pd.file_id '
            ' WHERE pd.person_id = ? AND fc.removed_at IS NULL GROUP BY 1',
            (person_id,))}
        total = (sum(years.values()) if not year
                 else years.get(str(year), 0))
        sql += ' ORDER BY fc.master_datetime DESC, pd.id DESC LIMIT ? OFFSET ?'
        rows = [dict(r) for r in cur.execute(sql, params + [limit, offset])]
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500
    # Which of these is the card picture. Resolved by FILE, because that is
    # what profile_photo_file_id stores - see _attach_pet_covers for why a
    # detection id would not survive the next detection run.
    profile_file_id = None
    try:
        pr = conn.execute('SELECT profile_photo_file_id FROM people WHERE id = ?',
                          (person_id,)).fetchone()
        profile_file_id = pr[0] if pr else None
    except Exception:
        pass
    conn.close()
    for r in rows:
        if r.get('crop_id'):
            r['crop_url'] = _pet_crop_url(catalog_id, r['id'], r['file_id'])
        r['is_profile'] = (profile_file_id is not None
                           and r['file_id'] == profile_file_id)
    return jsonify({'detections': rows, 'total': total, 'limit': limit,
                    'offset': offset, 'years': years})


@app.route('/api/catalogs/<int:catalog_id>/pets/<int:person_id>/profile-photo',
           methods=['PUT'])
def set_pet_profile_photo(catalog_id, person_id):
    """
    Star one of this animal's photographs: it becomes the card picture.

    Takes a detection_id - that is what the gallery has in hand - and stores
    its FILE id, not the detection id. A detection id is not stable: a
    detection run deletes and re-inserts a file's automatic boxes, so a stored
    detection id would dangle afterwards and the card would lose its picture
    for no reason the user could see. The file is permanent, and the crop is
    found from (file, pet) when the card is drawn.

    Deliberately NOT the people profile-photo endpoint, although a pet is a
    people row. That one extracts a face region at twice the face box and
    writes a new crop; a pet already has its crop on disk from the identify
    pass, and re-cropping it would be work in order to get a worse picture.
    """
    catalog, catalog_db, _ = _pet_catalog(catalog_id)
    if catalog is None:
        return catalog_db

    data = request.json or {}
    detection_id = data.get('detection_id')
    if not detection_id:
        return jsonify({'error': 'detection_id is required'}), 400

    conn = catalog_db.get_connection()
    cur = conn.cursor()
    try:
        row = cur.execute(
            "SELECT pd.file_id, COALESCE(p.kind,'person') AS kind "
            "  FROM pet_detections pd "
            "  JOIN people p ON p.id = pd.person_id "
            " WHERE pd.id = ? AND pd.person_id = ?",
            (detection_id, person_id)).fetchone()
        if not row:
            # Either no such detection, or it belongs to a different animal.
            # Both are the caller pointing at something that is not this pet's
            # picture, and neither should quietly succeed.
            return jsonify({'error': 'that detection is not this pet\'s'}), 404
        if row['kind'] != 'pet':
            return jsonify({'error': 'that id is a person, not a pet'}), 400

        cur.execute('UPDATE people SET profile_photo_file_id = ? WHERE id = ?',
                    (row['file_id'], person_id))
        conn.commit()
        return jsonify({'person_id': person_id,
                        'detection_id': detection_id,
                        'profile_photo_file_id': row['file_id']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/images/<int:file_id>/pet-detections',
           methods=['GET'])
def get_image_pet_detections(catalog_id, file_id):
    """
    Every animal found in one photograph, for the viewer's overlay.

    The pet half of /images/<id>/faces, and shaped like it on purpose: the
    viewer draws both on one canvas, so both have to arrive in the same
    coordinate space with the same field names. x/y/w/h are in
    image_width x image_height - the PREPARED detection image, not the
    original - and the caller scales from those two exactly as it already does
    for a face.

    Returns [] rather than 404 when a catalog has no pet tables at all: a
    catalog with pets switched off is not an error, and the viewer asks for
    every photograph it shows.
    """
    catalog, catalog_db, _ = _pet_catalog(catalog_id)
    if catalog is None:
        return catalog_db

    conn = catalog_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute("""
            SELECT pd.id, pd.x, pd.y, pd.w, pd.h, pd.confidence, pd.species,
                   pd.source, pd.image_width, pd.image_height, pd.person_id,
                   p.name AS person_name,
                   -- The species of the ANIMAL, not of the box. pd.species is
                   -- the detector's guess and is wrong about a quarter of the
                   -- time (spec 12.2); people.species is what somebody said
                   -- when they named it. Once an animal is named, the guess
                   -- has been overruled and showing it is just noise - "looks
                   -- like a cat, 56%" under a dog the user already named.
                   p.species AS person_species,
                   apc.id AS crop_id
              FROM pet_detections pd
         LEFT JOIN people p ON p.id = pd.person_id
         LEFT JOIN ai_pet_contents apc ON apc.pet_detection_id = pd.id
             WHERE pd.file_id = ?
             ORDER BY pd.confidence DESC, pd.id
        """, (file_id,))]
    except Exception:
        # No pet schema in this catalog yet.
        rows = []
    finally:
        conn.close()

    for r in rows:
        if r.get('crop_id'):
            r['crop_url'] = _pet_crop_url(catalog_id, r['id'], file_id)
    return jsonify({'pets': rows})


@app.route('/api/catalogs/<int:catalog_id>/pet-detections/<int:detection_id>/crop',
           methods=['GET'])
def get_pet_crop(catalog_id, detection_id):
    """
    The cropped animal, as written by the identify pass into cache/pets-ai.

    Rewritten in place when its photo is edited, so it is cached for a year
    only under a versioned URL (_pet_crop_url, basic-photo-editing.md 7.2).
    """
    from flask import send_file
    catalog, catalog_db, _ = _pet_catalog(catalog_id)
    if catalog is None:
        return catalog_db

    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT file_path FROM ai_pet_contents '
                           ' WHERE pet_detection_id = ?', (detection_id,)).fetchone()
    except Exception:
        row = None
    finally:
        conn.close()
    if not row or not row['file_path']:
        return jsonify({'error': 'no crop for this detection'}), 404

    path = row['file_path']
    if not os.path.isabs(path):
        path = os.path.join(catalog.target_directory, path)
    if not os.path.isfile(path):
        return jsonify({'error': 'crop file missing', 'path': row['file_path']}), 404
    return _derived_image_response(path)


@app.route('/api/catalogs/<int:catalog_id>/pet-detections/<int:detection_id>/assign',
           methods=['POST', 'DELETE'])
def assign_pet_detection(catalog_id, detection_id):
    """
    Name one detected animal, or take the name away again.

    DELETE leaves the detection and clears its person_id. The measurement
    survives the judgement being withdrawn - the animal is still in the
    photograph, it just has no name. Same shape as unnaming a face.
    """
    catalog, catalog_db, _ = _pet_catalog(catalog_id)
    if catalog is None:
        return catalog_db

    conn = catalog_db.get_connection()
    cur = conn.cursor()
    try:
        if request.method == 'DELETE':
            cur.execute('UPDATE pet_detections SET person_id = NULL WHERE id = ?',
                        (detection_id,))
            conn.commit()
            changed = cur.rowcount
            if changed:
                _queue_pets_changed(catalog, {'detections': changed,
                                              'action': 'unnamed'})
            return jsonify({'detection_id': detection_id, 'person_id': None,
                            'changed': changed})

        data = request.json or {}
        person_id = data.get('person_id')
        if not person_id:
            return jsonify({'error': 'person_id is required'}), 400

        # It has to BE a pet. Pointing a pet detection at a person would put a
        # human into pet scoring, which is the mirror of the contamination the
        # separate tables prevent.
        row = cur.execute("SELECT COALESCE(kind,'person') FROM people WHERE id = ?",
                          (person_id,)).fetchone()
        if not row:
            return jsonify({'error': 'no such pet'}), 404
        if row[0] != 'pet':
            return jsonify({'error': 'that id is a person, not a pet'}), 400

        cur.execute('UPDATE pet_detections SET person_id = ?, source = ? WHERE id = ?',
                    (person_id, 'Manual', detection_id))
        conn.commit()
        changed = cur.rowcount
        if changed:
            _queue_pets_changed(catalog, {'detections': changed,
                                          'action': 'named'})
        return jsonify({'detection_id': detection_id, 'person_id': person_id,
                        'changed': changed})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/pets/clusters', methods=['GET'])
def get_pet_clusters(catalog_id):
    """
    Groups of unnamed animals, biggest first, ready to be named.

    Unknown animals are never cards: a `people` row means somebody said who
    this is. These are groups of detections, and naming one is what creates
    the pet - see the spec's section 7.4.

    ?min_members (default 3) and ?species filter; ?max_members caps the sample
    of crops returned per group.
    """
    catalog, catalog_db, _ = _pet_catalog(catalog_id)
    if catalog is None:
        return catalog_db

    try:
        min_members = int(request.args.get('min_members', 3))
    except (TypeError, ValueError):
        min_members = 3
    try:
        max_members = int(request.args.get('max_members', 12))
    except (TypeError, ValueError):
        max_members = 12
    species = request.args.get('species')

    conn = catalog_db.get_connection()
    cur = conn.cursor()
    sql = """
        SELECT id, member_count, cohesion, max_member_distance, species, created_at
          FROM pet_clusters
         WHERE resolved_person_id IS NULL
           AND resolved_at IS NULL          -- a rejected group: see /reject
           AND member_count >= ?
    """
    params = [min_members]
    if species:
        sql += ' AND species = ? '
        params.append(species)
    sql += ' ORDER BY member_count DESC, cohesion ASC LIMIT 200'
    try:
        clusters = [dict(r) for r in cur.execute(sql, params)]
        for c in clusters:
            members = cur.execute("""
                SELECT pcm.pet_detection_id AS detection_id,
                       pcm.distance_to_centroid, pd.species, pd.confidence,
                       pd.file_id
                  FROM pet_cluster_members pcm
                  JOIN pet_detections pd ON pd.id = pcm.pet_detection_id
                 WHERE pcm.cluster_id = ?
                 ORDER BY pcm.distance_to_centroid ASC LIMIT ?
            """, (c['id'], max_members)).fetchall()
            c['members'] = [dict(m) for m in members]
            for m in c['members']:
                m['crop_url'] = _pet_crop_url(catalog_id, m['detection_id'], m['file_id'])
    except Exception as e:
        conn.close()
        return jsonify({'error': str(e)}), 500
    conn.close()
    return jsonify(clusters)


@app.route('/api/catalogs/<int:catalog_id>/pets/clusters/<int:cluster_id>/assign',
           methods=['POST'])
def assign_pet_cluster(catalog_id, cluster_id):
    """
    Name a group. One request, deliberately.

    Takes either {person_id} for a pet that already exists, or
    {name, species, category} to create one - and does the whole promotion in
    one transaction: create the pet, resolve the group, and write person_id to
    every member detection.

    One request because anything else leaves a half-named animal if the second
    call fails. And writing every member is the point: naming the dog once is
    what turns five hundred photographs into "photos with Rex", with nobody
    tagging anything by hand.
    """
    catalog, catalog_db, _ = _pet_catalog(catalog_id)
    if catalog is None:
        return catalog_db

    data = request.json or {}
    person_id = data.get('person_id')
    conn = catalog_db.get_connection()
    cur = conn.cursor()
    try:
        row = cur.execute('SELECT resolved_person_id, species FROM pet_clusters '
                          ' WHERE id = ?', (cluster_id,)).fetchone()
        if not row:
            return jsonify({'error': 'no such group'}), 404
        if row['resolved_person_id']:
            return jsonify({'error': 'that group has already been named',
                            'person_id': row['resolved_person_id']}), 409

        if not person_id:
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'error': 'person_id, or a name to create one, '
                                         'is required'}), 400
            cur.execute("""INSERT INTO people (name, category, kind, species)
                           VALUES (?, ?, 'pet', ?)""",
                        (name, data.get('category') or 'Other',
                         data.get('species') or row['species']))
            person_id = cur.lastrowid
        else:
            kind = cur.execute("SELECT COALESCE(kind,'person') FROM people "
                               " WHERE id = ?", (person_id,)).fetchone()
            if not kind:
                return jsonify({'error': 'no such pet'}), 404
            if kind[0] != 'pet':
                return jsonify({'error': 'that id is a person, not a pet'}), 400

        cur.execute('UPDATE pet_clusters SET resolved_person_id = ?, '
                    ' resolved_at = CURRENT_TIMESTAMP WHERE id = ?',
                    (person_id, cluster_id))
        cur.execute("""UPDATE pet_detections SET person_id = ?
                        WHERE id IN (SELECT pet_detection_id
                                       FROM pet_cluster_members
                                      WHERE cluster_id = ?)""",
                    (person_id, cluster_id))
        named = cur.rowcount
        conn.commit()
        # After the commit, never before: the chain must not be enqueued over a
        # transaction that then rolls back.
        _queue_pets_changed(catalog, {'detections': named, 'action': 'named',
                                      'cluster_id': cluster_id})
        return jsonify({'cluster_id': cluster_id, 'person_id': person_id,
                        'detections_named': named})
    except Exception as e:
        conn.rollback()
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/pets/clusters/<int:cluster_id>/reject',
           methods=['POST'])
def reject_pet_cluster(catalog_id, cluster_id):
    """
    Not an animal, or not one worth naming.

    Recorded by resolving the group to NULL with a timestamp, so it stays out
    of future offers without deleting the detections - which are still a
    measurement somebody may want to look at, and which the clustering pass
    must not simply find again next run.
    """
    catalog, catalog_db, _ = _pet_catalog(catalog_id)
    if catalog is None:
        return catalog_db

    conn = catalog_db.get_connection()
    cur = conn.cursor()
    try:
        cur.execute('UPDATE pet_clusters SET resolved_at = CURRENT_TIMESTAMP, '
                    " species = COALESCE(species,'') || '' WHERE id = ?",
                    (cluster_id,))
        # resolved_at set with resolved_person_id still NULL is the "rejected"
        # state: named groups have both, offered groups have neither.
        conn.commit()
        return jsonify({'cluster_id': cluster_id, 'rejected': cur.rowcount > 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/people-categories', methods=['GET'])
def get_people_categories(catalog_id):
    """Get all people categories for a catalog"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    catalog_db = CatalogDatabase(catalog.target_directory)
    category_model = PeopleCategoryModel(catalog_db)
    return jsonify(category_model.get_all())

@app.route('/api/catalogs/<int:catalog_id>/people-categories', methods=['POST'])
def create_people_category(catalog_id):
    """Create a new people category for a catalog"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    data = request.json
    if not data or not data.get('name', '').strip():
        return jsonify({'error': 'name is required'}), 400
    catalog_db = CatalogDatabase(catalog.target_directory)
    category_model = PeopleCategoryModel(catalog_db)
    name = category_model.create(data['name'].strip())
    return jsonify({'name': name}), 201

@app.route('/api/catalogs/<int:catalog_id>/people-categories/rename', methods=['PUT'])
def rename_people_category(catalog_id):
    """Rename a people category and move everyone in it: {"from": ..., "to": ...}.

    The names travel in the body rather than the URL: a category name is free
    text and may contain a slash.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    data = request.get_json(silent=True) or {}
    old = (data.get('from') or '').strip()
    new = (data.get('to') or '').strip()
    if not old or not new:
        return jsonify({'error': 'Both the current and the new name are required'}), 400
    category_model = PeopleCategoryModel(CatalogDatabase(catalog.target_directory))
    try:
        moved = category_model.rename(old, new)
    except LookupError as exc:
        return jsonify({'error': str(exc)}), 404
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 409
    return jsonify({'from': old, 'to': new, 'people_moved': moved})

@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>', methods=['DELETE'])
def delete_person(catalog_id, person_id):
    """Delete a person and all their photos"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404
    
    affected = people_model.delete_person(person_id)

    # Say what happened to the faces. "Deleted successfully" hid the fact that
    # detections were left pointing at a person that no longer existed.
    unidentified = affected.get('face_detections', 0)
    return jsonify({
        'message': 'Person deleted successfully',
        'faces_unidentified': unidentified,
        'affected': affected,
    })

@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/photos', methods=['POST'])
def add_person_photo(catalog_id, person_id):
    """Add a photo for a person in a specific year"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    data = request.json
    if not data.get('year') or not data.get('photo_path'):
        return jsonify({'error': 'Year and photo_path are required'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404
    
    photo_id = people_model.add_photo(
        person_id=person_id,
        year=data['year'],
        photo_path=data['photo_path'],
        photo_order=data.get('photo_order', 1)
    )
    
    return jsonify({'id': photo_id, 'message': 'Photo added successfully'}), 201

@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/photos/<int:year>', methods=['GET'])
def get_person_photos_by_year(catalog_id, person_id, year):
    """Get photos for a person in a specific year"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404
    
    photos = people_model.get_photos_by_year(person_id, year)
    return jsonify(photos)

@app.route('/api/catalogs/<int:catalog_id>/people/photos/<int:photo_id>', methods=['DELETE'])
def delete_person_photo(catalog_id, photo_id):
    """Delete a specific photo"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    
    people_model.delete_photo(photo_id)
    
    return jsonify({'message': 'Photo deleted successfully'})


def set_profile_photo_internal(catalog, catalog_db, person_id, file_id):
    """Internal helper function to set profile photo for a person.
    Extracts the face region from the image (2x the face detection box),
    saves it to tmp, and updates the person's profile_photo_path.
    
    Args:
        catalog: Catalog object
        catalog_db: CatalogDatabase instance
        person_id: ID of the person
        file_id: ID of the file containing the face
    
    Returns:
        dict: Result with 'success' boolean and optional 'message' or 'warning'
    """
    import os
    from PIL import Image, ImageOps
    
    # Verify the file exists in this catalog and get its path + all alternative versions
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT fc.id, fc.file_path, fc.file_type
        FROM file_contents fc
        WHERE fc.id = ?
    ''', (file_id,))
    file_row = cursor.fetchone()

    if not file_row:
        conn.close()
        return {'success': False, 'warning': 'File not found in catalog'}

    # Check if this file_id is actually an alternative version itself
    # If so, we need to find the original file_id
    original_file_id = file_id
    cursor.execute('''
        SELECT original_file_id
        FROM alternative_versions
        WHERE alternative_file_path = ?
        LIMIT 1
    ''', (file_row['file_path'],))
    alt_check = cursor.fetchone()
    
    if alt_check:
        # This is an alternative version, use the original file_id instead
        original_file_id = alt_check['original_file_id']
        # Re-fetch the original file info
        cursor.execute('''
            SELECT fc.id, fc.file_path, fc.file_type
            FROM file_contents fc
            WHERE fc.id = ?
        ''', (original_file_id,))
        original_file_row = cursor.fetchone()
        if original_file_row:
            file_row = original_file_row
            file_id = original_file_id

    # Fetch the display derivatives for the ORIGINAL file.
    #
    # THE BASE RUNG, PINNED - not "the best available". This function crops a
    # face out of the image, and the coordinates it crops with were computed
    # against the base. Taking a larger rung here would offset every crop by the
    # scale factor between them (spec 6.2).
    cursor.execute('''
        SELECT alternative_file_path, alternative_file_name, rung, characteristic, width
        FROM alternative_versions
        WHERE original_file_id = ?
          AND rung IN ('1200','1600','2048','2560')
        ORDER BY (rung = '1200') DESC, width ASC, id ASC
    ''', (original_file_id,))
    alt_rows = cursor.fetchall()

    # base_path is the 1200px derivative when it exists; any_display_path is the
    # narrowest display derivative there is, which is what a HEIC or CR3 needs
    # when no base was written (it cannot be shown in its original form at all).
    scale_1200_path = None
    type_jpg_path = None

    for alt in alt_rows:
        alt_path = _resolve_path(alt['alternative_file_path'], catalog.target_directory)
        if alt['rung'] == BASE_RUNG and scale_1200_path is None:
            scale_1200_path = alt_path
        if type_jpg_path is None:
            type_jpg_path = alt_path

    # Determine which image was used for face detection.
    # Face detection runs on TYPE JPG (for HEIC/CR3) or the original file (for JPG/PNG).
    # IMPORTANT: Face detection is performed on ORIENTED images (EXIF orientation applied)
    file_type = (file_row['file_type'] or '').lower()
    non_displayable_types = {'.heic', '.heif', '.cr3'}
    # Resolve file_path from DB (may be relative) to absolute
    original_file_abs_path = _resolve_path(file_row['file_path'], _original_root(catalog))
    if file_type in non_displayable_types and type_jpg_path:
        detection_image_path = type_jpg_path
    else:
        detection_image_path = original_file_abs_path

    # Get face detection bounding box for this person in this file
    # IMPORTANT: Also get the 'source' field to know if this is a manual or automatic detection
    # IMPORTANT: Also get image_width and image_height to know the dimensions of the image used for detection
    cursor.execute('''
        SELECT fd.x, fd.y, fd.w, fd.h, fd.source, fd.image_width, fd.image_height
        FROM face_detections fd
        WHERE fd.file_id = ? AND fd.person_id = ?
        LIMIT 1
    ''', (file_id, person_id))
    face_row = cursor.fetchone()
    conn.close()

    people_model = PeopleModel(catalog_db)
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404

    # Always update the profile_photo_file_id
    people_model.update_person(person_id, profile_photo_file_id=file_id)

    # If we have a face detection and the image file exists, crop and save
    if face_row:
        try:
            fx, fy, fw, fh = face_row['x'], face_row['y'], face_row['w'], face_row['h']
            face_source = face_row.get('source', 'Automatic')
            detection_img_width = face_row.get('image_width')
            detection_img_height = face_row.get('image_height')

            # CRITICAL: Determine which image to crop from
            # Face coordinates are stored relative to the image dimensions in image_width/image_height
            # We need to scale coordinates if we're cropping from a different sized image
            
            # Prefer the base rung, fall back to any display derivative, then
            # the original. For HEIC/CR3 there is no fallback to the original:
            # it cannot be decoded for display, so a derivative is required.
            if file_type in non_displayable_types:
                if type_jpg_path and os.path.exists(type_jpg_path):
                    image_path = type_jpg_path
                    crop_image_type = 'TYPE JPG'
                else:
                    # No TYPE JPG alternative found - cannot extract face
                    return {'success': False, 'warning': 'Cannot extract face: HEIC/CR3 file requires JPG alternative for face extraction'}
            else:
                # For displayable formats (JPG/PNG), prefer the base rung
                if scale_1200_path and os.path.exists(scale_1200_path):
                    image_path = scale_1200_path
                    crop_image_type = 'BASE RUNG 1200'
                else:
                    # Use the original file for cropping
                    image_path = original_file_abs_path
                    crop_image_type = 'ORIGINAL'

            # Verify the crop image exists
            if not os.path.exists(image_path):
                return {'success': False, 'warning': f'Cannot extract face: image file not found at {image_path}'}

            # Open the crop image and apply EXIF orientation to get true dimensions
            with Image.open(image_path) as img:
                img = ImageOps.exif_transpose(img)
                if img is None:
                    # No orientation needed, reopen
                    img = Image.open(image_path)
                img = _turn_original(img, image_path, catalog.target_directory, file_id)
                
                img_w, img_h = img.size

                # IMPORTANT: Scale face coordinates if the crop image dimensions differ from detection dimensions
                # Face coordinates are stored relative to the image used for detection (with dimensions in image_width/image_height)
                if detection_img_width and detection_img_height:
                    if detection_img_width != img_w or detection_img_height != img_h:
                        # Scale coordinates proportionally
                        scale_x = img_w / detection_img_width
                        scale_y = img_h / detection_img_height
                        fx = int(fx * scale_x)
                        fy = int(fy * scale_y)
                        fw = int(fw * scale_x)
                        fh = int(fh * scale_y)

                # Expand the crop box to 2x the face dimensions (centered on face)
                cx = fx + fw // 2
                cy = fy + fh // 2
                crop_w = fw * 2
                crop_h = fh * 2

                left = max(0, cx - crop_w // 2)
                top = max(0, cy - crop_h // 2)
                right = min(img_w, cx + crop_w // 2)
                bottom = min(img_h, cy + crop_h // 2)

                cropped = img.crop((left, top, right, bottom))

                # Save to cache directory
                tmp_alternate_dir = os.path.join(catalog.target_directory, 'cache')
                os.makedirs(tmp_alternate_dir, exist_ok=True)

                # Sanitize person name for filename
                safe_name = ''.join(c if c.isalnum() or c in (' ', '-', '_') else '_' for c in person['name'])
                safe_name = safe_name.strip().replace(' ', '_')
                output_filename = f"profile_{safe_name}.jpg"
                output_path = os.path.join(tmp_alternate_dir, output_filename)

                # Convert to RGB if needed (e.g. RGBA PNG)
                if cropped.mode != 'RGB':
                    cropped = cropped.convert('RGB')
                cropped.save(output_path, 'JPEG', quality=90)

                # Store profile_photo_path relative to target_directory (portable)
                rel_profile_path = os.path.relpath(output_path, catalog.target_directory)
                people_model.update_person(person_id, profile_photo_path=rel_profile_path)

        except Exception as e:
            # Cropping failed — still return success since file_id was set
            import traceback
            traceback.print_exc()
            return {'success': False, 'warning': f'Face extraction failed: {str(e)}'}

    return {'success': True, 'message': 'Profile photo updated successfully'}


@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/profile-photo', methods=['PUT'])
def set_person_profile_photo(catalog_id, person_id):
    """Set the profile photo for a person using a catalog file_id.
    Extracts the face region from the image (2x the face detection box),
    saves it to tmp, and updates the person's profile_photo_path."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.json
    if not data or 'file_id' not in data:
        return jsonify({'error': 'file_id is required'}), 400

    file_id = data['file_id']

    catalog_db = CatalogDatabase(catalog.target_directory)
    
    # Call the internal helper function
    result = set_profile_photo_internal(catalog, catalog_db, person_id, file_id)
    
    if result.get('success'):
        return jsonify({'message': result.get('message', 'Profile photo updated successfully')})
    else:
        return jsonify({
            'message': 'Profile photo file_id updated',
            'warning': result.get('warning', 'Face extraction failed')
        }), 200


@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/detected-photos', methods=['GET'])
def get_person_detected_photos(catalog_id, person_id):
    """
    The photographs this person's face is tagged in - one per file, oldest
    first - ONE PAGE at a time, across every year or ?year= one.

        ?limit= (default 60, at most 500)  &offset=  &year=

    Returns {photos, total, limit, offset}, each photo as the per-year route
    below returns it. The person page used to ask that route once per year
    and merge every answer in the browser to show twelve: a person
    photographed across twenty years was twenty requests for every photograph
    of them. Here the display derivative is looked up for the page's rows only.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    try:
        limit = max(1, min(500, int(request.args.get('limit', 60))))
        offset = max(0, int(request.args.get('offset', 0)))
        year = request.args.get('year')
        year = int(year) if year not in (None, '') else None
    except ValueError:
        return jsonify({'error': 'year, limit and offset must be numbers'}), 400

    year_sql, params = '', [person_id]
    if year is not None:
        year_sql = 'AND fc.master_datetime >= ? AND fc.master_datetime < ?'
        params += [f'{year:04d}', f'{year:04d}~']

    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        files = f'''
            SELECT fc.id AS file_id, fc.file_path, fc.file_name, fc.file_type,
                   fc.master_datetime
              FROM file_contents fc
             WHERE fc.removed_at IS NULL {year_sql}
               AND fc.id IN (SELECT file_id FROM face_detections WHERE person_id = ?)'''
        # person_id goes last: the IN subquery comes after the year range.
        args = params[1:] + [person_id]
        total = conn.execute(f'SELECT COUNT(*) FROM ({files})', args).fetchone()[0]
        rows = conn.execute(
            f'{files} ORDER BY fc.master_datetime ASC, fc.id ASC LIMIT ? OFFSET ?',
            args + [limit, offset]).fetchall()
        photos = []
        for row in rows:
            alt = conn.execute('''
                SELECT alternative_file_path, alternative_file_name, characteristic
                  FROM alternative_versions
                 WHERE original_file_id = ?
                   AND rung IN ('1200','1600','2048','2560')
                 ORDER BY (width >= 1200) DESC,
                          CASE WHEN width >= 1200 THEN width END ASC,
                          width DESC, id ASC
                 LIMIT 1''', (row['file_id'],)).fetchone()
            original = _resolve_path(row['file_path'], _original_root(catalog))
            photos.append({
                'file_id': row['file_id'],
                'file_name': row['file_name'],
                'file_path': original,
                'display_path': (_resolve_path(alt['alternative_file_path'],
                                               catalog.target_directory)
                                 if alt else original),
                'display_name': alt['alternative_file_name'] if alt else row['file_name'],
                'characteristic': alt['characteristic'] if alt else None,
                'master_datetime': row['master_datetime'],
            })
    finally:
        conn.close()
    return jsonify({'photos': photos, 'total': total, 'limit': limit, 'offset': offset})


@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/detected-photos/<int:year>', methods=['GET'])
def get_person_detected_photos_by_year(catalog_id, person_id, year):
    """Get photos from face_detections where this person was tagged, for a given year.
    Serves the display derivative chosen by pick_derivative(), then the original."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()

    # Get distinct files where this person was tagged in face_detections,
    # filtered by the year extracted from EXIF creation_date or file modification_time
    cursor.execute('''
        SELECT DISTINCT
            fc.id as file_id,
            fc.file_path,
            fc.file_name,
            fc.file_type,
            ed.creation_date
        FROM face_detections fd
        JOIN file_contents fc ON fd.file_id = fc.id
        LEFT JOIN exif_data ed ON fc.id = ed.file_id
        WHERE fd.person_id = ? AND fc.removed_at IS NULL
          AND CAST(SUBSTR(fc.master_datetime, 1, 4) AS INTEGER) = ?
        ORDER BY fc.master_datetime ASC, fc.file_name ASC
    ''', (person_id, year))
    rows = cursor.fetchall()

    results = []
    for row in rows:
        file_id = row['file_id']
        file_path = row['file_path']
        file_name = row['file_name']
        creation_date = row['creation_date']

        # The display derivative, ranked by measured pixels (spec 6.1)
        cursor.execute('''
            SELECT alternative_file_path, alternative_file_name, rung, characteristic
            FROM alternative_versions
            WHERE original_file_id = ?
              AND rung IN ('1200','1600','2048','2560')
            ORDER BY (width >= 1200) DESC,
                         CASE WHEN width >= 1200 THEN width END ASC,
                         width DESC, id ASC
            LIMIT 1
        ''', (file_id,))
        alt_row = cursor.fetchone()

        if alt_row:
            display_path = _resolve_path(alt_row['alternative_file_path'], catalog.target_directory)
            display_name = alt_row['alternative_file_name']
            characteristic = alt_row['characteristic']
        else:
            display_path = _resolve_path(file_path, _original_root(catalog))
            display_name = file_name
            characteristic = None

        results.append({
            'file_id': file_id,
            'file_name': file_name,
            'file_path': _resolve_path(file_path, _original_root(catalog)),
            'display_path': display_path,
            'display_name': display_name,
            'characteristic': characteristic,
            'creation_date': creation_date,
        })

    conn.close()
    return jsonify(results)


@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/upload-photo', methods=['POST'])
def upload_person_photo(catalog_id, person_id):
    """Upload a photo file for a person"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    if 'photo' not in request.files:
        return jsonify({'error': 'No photo file provided'}), 400
    
    photo = request.files['photo']
    if photo.filename == '':
        return jsonify({'error': 'No photo file selected'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404
    
    # Get year from form data
    year = request.form.get('year')
    if not year:
        return jsonify({'error': 'Year is required'}), 400
    
    # Create people directory structure: catalog_dir/people/person_id/year/
    import os
    from werkzeug.utils import secure_filename
    
    # Get app directory
    app_dir = os.path.dirname(os.path.abspath(__file__))
    people_dir = os.path.join(app_dir, 'people', str(person_id), str(year))
    os.makedirs(people_dir, exist_ok=True)
    
    # Save the file
    filename = secure_filename(photo.filename)
    # Add timestamp to filename to avoid conflicts
    import time
    timestamp = int(time.time())
    name, ext = os.path.splitext(filename)
    filename = f"{name}_{timestamp}{ext}"
    
    file_path = os.path.join(people_dir, filename)
    photo.save(file_path)
    
    # Return relative path for storage in database (without 'people/' prefix since the API route includes it)
    relative_path = os.path.join(str(person_id), str(year), filename)
    
    return jsonify({
        'photo_path': relative_path,
        'message': 'Photo uploaded successfully'
    }), 201

# Tags API endpoints
@app.route('/api/catalogs/<int:catalog_id>/tags', methods=['GET'])
def get_catalog_tags(catalog_id):
    """Get all tags in a catalog, optionally filtered by search query"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    if not catalog.target_directory:
        return jsonify([])

    query = request.args.get('q', '').strip()
    catalog_db = CatalogDatabase(catalog.target_directory)
    tag_model = TagModel(catalog_db)

    if query:
        tags = tag_model.search_tags(query)
    else:
        tags = tag_model.get_all_tags()

    return jsonify(tags)


@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/tags', methods=['GET'])
def get_person_tags(catalog_id, person_id):
    """Get all tags for a specific person"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    tag_model = TagModel(catalog_db)
    tags = tag_model.get_tags_for_person(person_id)
    return jsonify(tags)


@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/tags', methods=['PUT'])
def set_person_tags(catalog_id, person_id):
    """Replace all tags for a person with the provided list"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.json
    if data is None:
        return jsonify({'error': 'Request body is required'}), 400

    tag_names = data.get('tags', [])
    if not isinstance(tag_names, list):
        return jsonify({'error': 'tags must be a list of strings'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404

    tag_model = TagModel(catalog_db)
    tag_model.set_tags_for_person(person_id, tag_names)

    # Return updated tags
    tags = tag_model.get_tags_for_person(person_id)
    return jsonify(tags)


# ─── Path containment for the two path-addressed file routes (spec 6.3) ───────
#
# /api/catalogs/files and /api/people/photos take a filesystem path rather than a
# catalog id, so the catalog_id choke point never sees them. Before this, both
# accepted an ABSOLUTE path and served whatever was there, and resolved relative
# paths by searching every catalog - an arbitrary-file-read with one user, and a
# cross-tenant read as soon as catalogs belong to different people.

def _permitted_catalog_roots():
    """realpaths of the catalog directories the current user may read."""
    catalogs = Catalog.get_all()
    if not auth.enabled_for(app):
        allowed = catalogs
    else:
        user = auth.current_user()
        if user is None:
            return []
        conn = db.get_connection()
        try:
            ids = set(auth.accessible_catalog_ids(conn, user['id']))
        finally:
            conn.close()
        allowed = [c for c in catalogs if c.id in ids]

    roots = []
    for cat in allowed:
        if cat.target_directory and os.path.isdir(cat.target_directory):
            roots.append(os.path.realpath(cat.target_directory))
        # A reference catalog's originals live outside its directory, so serving
        # them means widening what this endpoint will read. Same access gate as
        # the catalog itself - only catalogs this user can already reach - and
        # the same realpath containment, so a symlink cannot walk out of the
        # library (file-ingestion-spec.md section 5.2).
        if (getattr(cat, 'ingest_mode', 'copy') == 'reference'
                and cat.source_root and os.path.isdir(cat.source_root)):
            roots.append(os.path.realpath(cat.source_root))

    # Storage routes OWNED by those catalogs. A route belongs to one catalog, so
    # reaching that catalog is what exposes its files through the file API - and
    # a user who cannot reach the catalog cannot reach its route either
    # (local-storage-spec.md 6.3).
    if allowed:
        conn = db.get_connection()
        try:
            marks = ','.join('?' * len(allowed))
            rows = conn.execute(
                f'SELECT DISTINCT r.container_path FROM storage_routes r '
                f'WHERE r.catalog_id IN ({marks})',
                [c.id for c in allowed]).fetchall()
        except sqlite3.Error:
            rows = []
        finally:
            conn.close()
        for r in rows:
            if r['container_path'] and os.path.isdir(r['container_path']):
                roots.append(os.path.realpath(r['container_path']))
    return roots


def _contain(candidate, roots):
    """
    Return the real path when *candidate* sits inside one of *roots*, else None.

    realpath, not normpath: deployed albums are built from symlinks
    (_deploy_album), so a link is otherwise a containment bypass.
    """
    if not candidate:
        return None
    real = os.path.realpath(candidate)
    for root in roots:
        if real == root or real.startswith(root + os.sep):
            return real
    return None


def _resolve_within_catalogs(path_value):
    """
    Resolve a caller-supplied path to a real file the caller is allowed to read.

    Returns (real_path, error_response). Relative paths are tried only against
    catalogs this user can reach, so the search itself cannot leak the existence
    of someone else's files.
    """
    roots = _permitted_catalog_roots()
    if not roots:
        return None, (jsonify({'error': 'File not found'}), 404)

    if os.path.isabs(path_value):
        real = _contain(path_value, roots)
    else:
        real = None
        for root in roots:
            real = _contain(os.path.join(root, path_value), roots)
            if real and os.path.exists(real):
                break
            real = None

    if not real or not os.path.exists(real):
        # One message for "outside your catalogs" and for "does not exist", so
        # the response cannot be used to probe the filesystem.
        return None, (jsonify({'error': 'File not found'}), 404)
    return real, None


# ─── Browser caching for images ──────────────────────────────────────────────
#
# Every image endpoint that is not content-addressed answers the same way:
#
#   * an ETag from the served file's mtime, size and path, checked BEFORE any
#     work - several of these endpoints decode and re-encode with Pillow, which
#     is the expensive part, so a repeat view costs a 304 and nothing else;
#   * Cache-Control private (they sit behind the login), and then one of two
#     policies:
#       - a photo's own image (/photos/:id/image) keeps max-age an hour: its
#         pixels never change in place, so within the hour a reload makes no
#         request at all;
#       - anything REWRITTEN IN PLACE at a stable URL - a user's avatar, a
#         person's profile photo, a regenerated location map - is no-cache: the
#         browser asks every time and an unchanged image costs a 304. An hour
#         there showed the previous face after linking a different person
#         (2026-10-04).
# Thumbnails, written once under content-specific names, are cached for a year
# instead (serve_catalog_photo_thumbnail).
IMAGE_CACHE_MAX_AGE = 3600


def _image_etag(path):
    """Validator for the file actually served, or None if it cannot be read.
    The path is part of it: a photo whose served derivative changes (a larger
    rung built later) must not match the old one's mtime and size by chance."""
    import zlib
    try:
        st = os.stat(path)
    except OSError:
        return None
    # Nanoseconds: a photo edit rewrites a thumbnail in place, often within
    # the same second and at the same size as the file it replaces.
    return f'{st.st_mtime_ns}-{st.st_size}-{zlib.crc32(os.fsencode(path)):08x}'


def _image_not_modified(etag, revalidate=False):
    """A 304 response when the request already holds this version, else None."""
    if not etag:
        return None
    if etag in request.if_none_match or request.if_none_match.star_tag:
        response = app.response_class(status=304)
        return _image_cached(response, etag, revalidate)
    return None


def _image_cached(response, etag, revalidate=False):
    """revalidate=True for an image rewritten in place at a stable URL."""
    if etag:
        response.set_etag(etag)
    response.headers['Cache-Control'] = (
        'private, no-cache' if revalidate else f'private, max-age={IMAGE_CACHE_MAX_AGE}')
    return response


@app.route('/api/people/photos/<path:photo_path>')
def serve_person_photo(photo_path):
    """Serve a person's photo file with proper EXIF orientation.
    photo_path may be:
      - An absolute path (legacy, or uploaded photos stored globally)
      - A relative path stored in the DB (profile crops stored relative to target_directory)
    For relative paths, all catalog target directories are searched."""
    import os
    import io
    from flask import send_file
    from PIL import Image, ImageOps

    # Resolved only against catalogs this user may read, and contained with
    # realpath so a symlink cannot escape (spec 6.3).
    full_path, error = _resolve_within_catalogs(photo_path)
    if error is not None:
        # Legacy uploads live under the app's own people/ directory rather than
        # in a catalog; allow that one extra root, still contained.
        app_dir = os.path.dirname(os.path.abspath(__file__))
        legacy_root = os.path.realpath(os.path.join(app_dir, 'people'))
        full_path = _contain(os.path.join(legacy_root, photo_path), [legacy_root]) \
            if not os.path.isabs(photo_path) else None
        if not full_path or not os.path.exists(full_path):
            return error

    # Before the re-encode below: a repeat view is a 304.
    etag = _image_etag(full_path)
    not_modified = _image_not_modified(etag, revalidate=True)
    if not_modified is not None:
        return not_modified

    # Check if it's an image file that might have EXIF orientation
    image_extensions = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.webp'}
    file_ext = os.path.splitext(full_path)[1].lower()
    
    # For non-image files, serve directly
    if file_ext not in image_extensions:
        return _image_cached(send_file(full_path), etag, revalidate=True)
    
    try:
        # Open the image and apply EXIF orientation
        with Image.open(full_path) as img:
            # Use ImageOps.exif_transpose to automatically handle EXIF orientation
            img = ImageOps.exif_transpose(img)
            
            if img is None:
                return _image_cached(send_file(full_path), etag, revalidate=True)
            
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode in ('RGBA', 'LA'):
                    background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Save to BytesIO
            img_io = io.BytesIO()
            img.save(img_io, 'JPEG', quality=95)
            img_io.seek(0)
            
            return _image_cached(send_file(img_io, mimetype='image/jpeg'), etag, revalidate=True)
    except Exception as e:
        print(f"Error processing image orientation for {full_path}: {e}")
        return _image_cached(send_file(full_path), etag, revalidate=True)

# Serve catalog photo files
@app.route('/api/catalogs/<int:catalog_id>/photos/<int:photo_id>/image', methods=['GET'])
def serve_catalog_photo(catalog_id, photo_id):
    """Serve a catalog photo file with proper EXIF orientation.
    Uses alternative_file_path if available, otherwise uses file_path."""
    import os
    import io
    from flask import send_file
    from PIL import Image, ImageOps
    
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    
    # Get photo file path and alternative version
    # VIDEO PROXY is included, at the LOWEST priority so nothing about photo
    # behaviour changes. Without it this endpoint served the ORIGINAL for a
    # transcoded video - an HEVC or 4K file the browser cannot play, which is
    # precisely the file that needed a proxy in the first place
    # (video-page-spec.md section 2.5).
    #
    # A video with no proxy row took the passthrough branch and streams as-is,
    # so falling through to file_path is the right answer for it. Resolving that
    # here, on the server, is what lets the frontend point <video> at one URL and
    # not care which case it is.
    cursor.execute('''
        SELECT
            fc.file_path,
            av.alternative_file_path
        FROM file_contents fc
        LEFT JOIN (
            SELECT original_file_id, alternative_file_path
            FROM alternative_versions
            WHERE rung IN ('1200','1600','2048','2560','video_proxy')
            ORDER BY (rung = 'video_proxy') ASC,
                     (width >= 1200) DESC,
                     CASE WHEN width >= 1200 THEN width END ASC,
                     width DESC, id ASC
        ) av ON av.original_file_id = fc.id
        WHERE fc.id = ?
    ''', (_derivative_owner(conn, photo_id),))
    _derivative_owner_id = _derivative_owner(conn, photo_id)
    
    row = cursor.fetchone()

    # ?original=1 serves the FILE the caller asked for, skipping the derivative
    # lookup entirely. This is the "download the original" link: for a CR3 it is
    # a 26 MB download no browser renders, which is the point (spec 6.3).
    if _truthy(request.args.get('original')):
        orig_row = cursor.execute(
            'SELECT file_path, file_name FROM file_contents WHERE id = ?',
            (photo_id,)).fetchone()
        conn.close()
        if not orig_row:
            return jsonify({'error': 'Photo not found'}), 404
        orig_path = _resolve_path(orig_row['file_path'], _original_root(catalog))
        if not os.path.exists(orig_path):
            return jsonify({'error': 'Original file not found on disk'}), 404
        etag = _image_etag(orig_path)
        not_modified = _image_not_modified(etag)
        if not_modified is not None:
            return not_modified
        return _image_cached(send_file(
            orig_path, as_attachment=True,
            download_name=os.path.basename(orig_row['file_name'] or orig_path)), etag)

    conn.close()

    if not row:
        return jsonify({'error': 'Photo not found'}), 404
    
    # Alternate first, original as the fallback - and the two resolve against
    # DIFFERENT roots in a reference catalog: the alternate is a derivative that
    # lives in the catalog, the original lives in the source library. Picking one
    # value and resolving it once would silently use the wrong root for whichever
    # branch was not taken.
    if row['alternative_file_path']:
        file_path = _resolve_path(row['alternative_file_path'], catalog.target_directory)
    else:
        file_path = _resolve_path(row['file_path'], _original_root(catalog))
    
    if not os.path.exists(file_path):
        return jsonify({'error': 'Photo file not found on disk'}), 404

    # Before the re-encode below: a repeat view is a 304, not a Pillow decode
    # and a full download.
    etag = _image_etag(file_path)
    edit_owner = None
    if not row['alternative_file_path']:
        # The original, turned at request time: its own mtime never changes,
        # so the edit's version is part of what identifies the response.
        edit_owner = _derivative_owner_id
        v = _edit_versions(catalog_id).get(edit_owner, 0)
        if etag and v:
            etag = f'{etag}-e{v}'
    not_modified = _image_not_modified(etag)
    if not_modified is not None:
        return not_modified
    
    # Check if it's an image file that might have EXIF orientation
    image_extensions = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.webp'}
    file_ext = os.path.splitext(file_path)[1].lower()
    
    # For non-image files, serve directly
    if file_ext not in image_extensions:
        return _image_cached(send_file(file_path), etag)
    
    try:
        # Open the image and apply EXIF orientation
        with Image.open(file_path) as img:
            # Use ImageOps.exif_transpose to automatically handle EXIF orientation
            img = ImageOps.exif_transpose(img)
            
            if img is None:
                return _image_cached(send_file(file_path), etag)
            if edit_owner is not None:
                img = _turn_original(img, file_path, catalog.target_directory, edit_owner)
            
            # Convert to RGB if necessary
            if img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode in ('RGBA', 'LA'):
                    background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Save to BytesIO
            img_io = io.BytesIO()
            img.save(img_io, 'JPEG', quality=95)
            img_io.seek(0)
            
            return _image_cached(send_file(img_io, mimetype='image/jpeg'), etag)
    except Exception as e:
        print(f"Error processing image orientation for {file_path}: {e}")
        return _image_cached(send_file(file_path), etag)

@app.route('/api/catalogs/<int:catalog_id>/photos/<int:photo_id>/thumbnail', methods=['GET'])
def serve_catalog_photo_thumbnail(catalog_id, photo_id):
    """Serve a prebuilt square thumbnail for a catalog photo.

    ``?size=<px>`` picks the thumbnail size (default 72, the filmstrip size).
    The exact size is served when it exists; otherwise the smallest thumbnail
    at least as large as the request wins, so a thumbnail is never upscaled.
    Falls back to the full /image endpoint redirect when none exists at all.
    """
    import os
    from flask import send_file, redirect, url_for

    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    try:
        requested_size = int(request.args.get('size', 72))
    except (TypeError, ValueError):
        return jsonify({'error': 'size must be an integer'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()

    # Every prebuilt thumbnail for this photo, smallest first. The stored width
    # is the square size, so it is what we match the request against.
    cursor.execute('''
        SELECT av.alternative_file_path, av.width
        FROM alternative_versions av
        WHERE av.original_file_id = ?
          AND UPPER(av.characteristic) LIKE '%TYPE THUMBNAIL%'
        ORDER BY av.width
    ''', (_derivative_owner(conn, photo_id),))
    rows = cursor.fetchall()
    conn.close()

    candidates = [r for r in rows if r['alternative_file_path']]
    # Prefer the smallest thumbnail that is still >= the requested size; if the
    # request is larger than anything we built, fall back to the largest one.
    chosen = next((r for r in candidates if (r['width'] or 0) >= requested_size), None)
    if chosen is None and candidates:
        chosen = candidates[-1]

    if chosen:
        thumb_path = _resolve_path(chosen['alternative_file_path'], catalog.target_directory)
        if os.path.exists(thumb_path):
            # Rewritten in place when the photo is edited: a year's cache only
            # for a URL that names the edit version (basic-photo-editing.md 7.2).
            return _derived_image_response(thumb_path)

    # NO THUMBNAIL: say so. Never the full image.
    #
    # This used to redirect to /image, which serves the ORIGINAL - a 4 MB JPEG,
    # or a HEIC the backend must decode - in a 72 or 213 pixel box. One of
    # those is a slow tile; a gallery of them is the backend decoding
    # full-resolution photographs in a loop while the page waits.
    #
    # A thumbnail that does not exist is a fact about the derivative, not a
    # reason to send something else. 404 lets the card show its own "no photo
    # yet" rather than a correct-looking picture that cost a hundred times what
    # it should.
    #
    # The fix for a missing thumbnail is to build it - the import's rungs do
    # that, and documentation/specs/ has the renderer - not to substitute the
    # original at request time.
    return jsonify({
        'error': 'no thumbnail has been built for this photo',
        'photo_id': photo_id,
        'hint': 'run the import\'s render phase to build the missing rungs',
    }), 404


# Photos API endpoint
# The date every date-scoped photo query groups, filters and orders on. One
# definition, because the calendar and the day listing MUST agree about which
# day a photograph belongs to - two spellings of this is a day that appears in
# the index and is empty when opened.
#
# master_datetime, and nothing else. It is THE date of a photograph: decided
# once, at ingest, by resolve_master_datetime, which always returns one (EXIF,
# then the directory, the filename, the filesystem, the clock) and always as
# ISO 8601 text, 'YYYY-MM-DDT...'. On `family` all 373,020 rows have it and all
# have that shape.
#
# This was COALESCE(master_datetime, added_at), a fallback for a date that is
# never missing - and it cost the index. idx_fc_master_datetime is on the
# column, so an expression over the column could not use it, and every
# scoped query scanned the table: 0.45 s for a day, the same for a year.
# On the bare column, as a range (_photo_date_range), a day is 0.08 s.
_PHOTO_DAY_SQL = "fc.master_datetime"


def _photo_date_range(year, month=None, day=None):
    """
    (sql, params) selecting photographs dated in that year, month or day.

    A RANGE on the stored text rather than CAST(substr(...)) = ?, because a
    range is what an index can answer and a function of the column is not.
    The two are the same set: every master_datetime begins 'YYYY-MM-DD', so
    "begins with 2022-09" is ">= '2022-09' and < '2022-09~'" - '~' sorts after
    every character that can follow the prefix.
    """
    prefix = f'{int(year):04d}'
    if month is not None:
        prefix += f'-{int(month):02d}'
        if day is not None:
            prefix += f'-{int(day):02d}'
    return (f'{_PHOTO_DAY_SQL} >= ? AND {_PHOTO_DAY_SQL} < ?',
            (prefix, prefix + '~'))

# And the same for which file types count as a photograph. The calendar says
# how many a day holds and the listing returns them; disagreeing here means a
# day whose count never matches what opens.
_PHOTO_TYPES = ('.jpg', '.jpeg', '.png', '.heic', '.heif', '.cr3', '.cr2',
                '.nef', '.arw', '.dng', '.tiff', '.tif', '.webp', '.gif',
                '.bmp')
_PHOTO_TYPE_FILTER = ("LOWER(fc.file_type) IN ("
                      + ','.join(f"'{t}'" for t in _PHOTO_TYPES) + ")")


def _photo_page(cursor, cols, where, params, limit, offset, around):
    """
    One page of a photo scope, oldest first, for get_catalog_photos.

    Leaves the page's rows on `cursor` and returns {total, limit, offset} -
    plus `position` when `around` named a photograph, which is that
    photograph's index in the scope (None when it is not in it).

    The order is (date, id): the id breaks ties, because a burst shares its
    second and without it a page boundary could fall differently on two
    requests, showing one photograph twice and skipping another.

    `around` returns the page that holds the photograph, aligned to `limit`,
    so the strip can open "All" of a year on the photo already on screen
    without fetching anything before it. Its position is a COUNT of what sorts
    before it.

    All three - total, position, page - walk idx_fc_master_datetime, whose
    entries are (master_datetime, rowid) and fc.id IS the rowid: the index is
    already in exactly the order the pages are. A year is ~0.1 s for each.

    The page is chosen on ids and dates alone and only its rows are built:
    the derivative subqueries in `cols` run per row returned, never per row
    in scope.
    """
    total = cursor.execute(f'SELECT COUNT(*) FROM file_contents fc {where}',
                           params).fetchone()[0]

    meta = {'total': total, 'limit': limit}
    if around is not None:
        key = cursor.execute(
            f'SELECT {_PHOTO_DAY_SQL} FROM file_contents fc {where} AND fc.id = ?',
            params + (around,)).fetchone()
        position = None
        if key is not None:
            position = cursor.execute(
                f'SELECT COUNT(*) FROM file_contents fc {where} '
                f'AND ({_PHOTO_DAY_SQL} < ? OR ({_PHOTO_DAY_SQL} = ? AND fc.id < ?))',
                params + (key[0], key[0], around)).fetchone()[0]
            offset = (position // limit) * limit
        meta['position'] = position
    meta['offset'] = offset

    cursor.execute(f'''
        WITH page AS (
            SELECT fc.id, {_PHOTO_DAY_SQL} AS k
            FROM file_contents fc
            {where}
            ORDER BY {_PHOTO_DAY_SQL} ASC, fc.id ASC
            LIMIT ? OFFSET ?
        )
        SELECT {cols}
        FROM page p
        JOIN file_contents fc ON fc.id = p.id
        LEFT JOIN exif_data ed ON fc.id = ed.file_id
        ORDER BY p.k ASC, p.id ASC
    ''', params + (limit, offset))
    return meta


@app.route('/api/catalogs/<int:catalog_id>/photos/calendar', methods=['GET'])
def get_catalog_photo_calendar(catalog_id):
    """
    Which days have photographs, and how many. The index the gallery navigates
    by.

    This exists because /photos returned the WHOLE CATALOG and the page needed
    two completely different things from it: a list of days to draw the
    year/month/day navigation, and the photographs of the one day being looked
    at. Measured on `family` (370,751 files):

        /photos             301,195 rows   190.4 MB   ~21 s to build
        /photos/calendar      6,134 rows    154.6 KB    1.3 s

    Three orders of magnitude, for the half the page needs before it can show
    anything. The day itself is then tens to a few thousand rows.

    Returned as bare arrays [year, month, day, count] rather than objects:
    6,134 repetitions of four key names is most of the payload, and nothing
    reads these by name.
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    conn = catalog_db.get_connection()
    try:
        rows = conn.execute(f"""
            SELECT CAST(substr({_PHOTO_DAY_SQL}, 1, 4) AS INTEGER) AS y,
                   CAST(substr({_PHOTO_DAY_SQL}, 6, 2) AS INTEGER) AS m,
                   CAST(substr({_PHOTO_DAY_SQL}, 9, 2) AS INTEGER) AS d,
                   COUNT(*) AS n
              FROM file_contents fc
             WHERE {_PHOTO_TYPE_FILTER}
               AND fc.removed_at IS NULL
               AND fc.file_path NOT LIKE 'cache/%'
               AND fc.id NOT IN (
                   SELECT f.id FROM file_contents f
                     JOIN image_groups g ON g.id = f.image_group_id
                    WHERE f.id <> g.primary_file_id
               )
             GROUP BY y, m, d
             ORDER BY y DESC, m DESC, d DESC
        """).fetchall()
    finally:
        conn.close()
    return jsonify({'days': [[r['y'], r['m'], r['d'], r['n']] for r in rows
                             if r['y'] is not None]})


@app.route('/api/catalogs/<int:catalog_id>/photos/<int:file_id>/day',
           methods=['GET'])
def get_photo_day(catalog_id, file_id):
    """
    Which day one photograph is filed under, for a deep link.

    /photos/9/363120 has to open on the right day without loading the catalog
    to find out which one it is - that lookup was the other reason the page
    fetched everything.
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    try:
        row = conn.execute(
            f'SELECT CAST(substr({_PHOTO_DAY_SQL},1,4) AS INTEGER) y, '
            f'       CAST(substr({_PHOTO_DAY_SQL},6,2) AS INTEGER) m, '
            f'       CAST(substr({_PHOTO_DAY_SQL},9,2) AS INTEGER) d '
            f'  FROM file_contents fc WHERE fc.id = ?', (file_id,)).fetchone()
    finally:
        conn.close()
    if not row or row['y'] is None:
        return jsonify({'error': 'no such photo, or it has no date'}), 404
    return jsonify({'year': row['y'], 'month': row['m'], 'day': row['d']})


@app.route('/api/catalogs/<int:catalog_id>/photos', methods=['GET'])
def get_catalog_photos(catalog_id):
    """
    Photographs from a catalog - ONE DAY of them when asked, all of them
    otherwise.

    ?year=&month=&day= is what the gallery sends. Without it this still
    returns the whole catalog, which on `family` is 301,195 rows and 190 MB:
    kept working because other callers may rely on it, but nothing in the UI
    asks for it any more. See /photos/calendar.

    ?limit=&offset= returns one page of the scope as {photos, total, limit,
    offset}, OLDEST FIRST - the order the gallery's strip browses in. "All" of
    2022 is 29,680 photographs and 19 MB; the strip shows a dozen. ?around=
    adds `position` and returns the page holding that photograph instead of the
    one at `offset`. Unpaged, it is a bare array newest first, as it always was.
    """
    from datetime import datetime
    
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()

    # One day, when asked for one. The gallery always asks; the unscoped form
    # is 301,195 rows and 190 MB on `family` and nothing in the UI wants it.
    #
    # A range on master_datetime (_photo_date_range), which selects exactly the
    # photographs /photos/calendar counts under that day - it groups on the
    # same text - and which idx_fc_master_datetime can answer without reading
    # the rest of the table.
    _day_clause, _day_params = '', ()
    _y = request.args.get('year')
    _m = request.args.get('month')
    _d = request.args.get('day')
    if _d and not _m:
        return jsonify({'error': 'a day needs a month'}), 400
    if _y:
        try:
            _range, _day_params = _photo_date_range(
                _y, _m or None, _d or None)
            _day_clause = '          AND ' + _range
        except ValueError:
            return jsonify({'error': 'year, month and day must be numbers'}), 400

    # ?limit=&offset= pages the scope, and ?around=<file id> asks for the page
    # holding that photograph - see _photo_page. Without a limit, the bare
    # array as before.
    try:
        _raw_limit = request.args.get('limit')
        limit = max(1, min(1000, int(_raw_limit))) if _raw_limit is not None else None
        offset = max(0, int(request.args.get('offset', 0)))
        _raw_around = request.args.get('around')
        around = int(_raw_around) if _raw_around else None
    except ValueError:
        return jsonify({'error': 'limit, offset and around must be numbers'}), 400

    # Get all image files with their EXIF data and alternative versions
    # Note: file_type contains file extensions like .jpg, .heic, not MIME types
    _photo_cols = '''
            fc.id,
            fc.file_path,
            fc.file_name,
            fc.file_size,
            fc.file_type,
            fc.modification_time,
            fc.master_datetime,
            ed.creation_date,
            ed.latitude,
            ed.longitude,
            ed.camera_make,
            ed.camera_model,
            ed.width,
            ed.height,
            ed.geohash,
            -- The display derivative, picked PER ROW.
            --
            -- This was a LEFT JOIN onto a ROW_NUMBER() window over the whole
            -- of alternative_versions, which computes the best derivative for
            -- all 301,091 photographs however few are being asked for.
            -- Measured on `family`: 2.00 s of a 5.27 s query that returned ONE
            -- row, because the window ignores the day filter entirely.
            --
            -- Correlated instead, so it runs once per returned row and uses
            -- idx_alt_versions_file_rung: 3,000 rows in 0.02 s. The ORDER BY
            -- is character-for-character the window's, because it decides
            -- WHICH derivative is shown and a different answer here is a
            -- different picture on the page.
            (SELECT av1.alternative_file_path FROM alternative_versions av1
              WHERE av1.original_file_id = fc.id
                AND av1.rung IN ('1200','1600','2048','2560')
              ORDER BY (av1.width >= 1200) DESC,
                       CASE WHEN av1.width >= 1200 THEN av1.width END ASC,
                       av1.width DESC, av1.id ASC
              LIMIT 1) AS alternative_file_path,
            (SELECT av1.alternative_file_name FROM alternative_versions av1
              WHERE av1.original_file_id = fc.id
                AND av1.rung IN ('1200','1600','2048','2560')
              ORDER BY (av1.width >= 1200) DESC,
                       CASE WHEN av1.width >= 1200 THEN av1.width END ASC,
                       av1.width DESC, av1.id ASC
              LIMIT 1) AS alternative_file_name
    '''
    _photo_where = f'''
        WHERE {_PHOTO_TYPE_FILTER}
          -- Not a derivative listed as if it were a photograph.
          --
          -- This was `file_path NOT IN (SELECT alternative_file_path FROM
          -- alternative_versions)`, which eliminated NOTHING and cost 9.4 s of
          -- every page load. Measured on `family`: 373,020 rows in, 373,020
          -- rows out, zero removed - because the two path spaces are disjoint
          -- BY CONSTRUCTION. Originals are stored `Images/...`, derivatives
          -- `cache/Images/...`, so no file_contents.file_path can ever equal an
          -- alternative_file_path. The plan was SCAN alternative_versions,
          -- loading 1.24 M text values into a list to compare 373,020 paths
          -- against, for an empty intersection.
          --
          -- Kept as a LIKE rather than deleted, because the intent is still
          -- right and this way it costs nothing: if a derivative ever did get
          -- written into file_contents, it would be under cache/ and would
          -- still be excluded.
          AND fc.file_path NOT LIKE 'cache/%'
          -- Removed from the catalog by the user: listed only on Removed files.
          AND fc.removed_at IS NULL
          -- One row per IMAGE, not per file. A RAW+JPG pair is one photograph,
          -- so only the group's primary appears in the gallery; the secondary
          -- keeps its own id and URLs and is reachable from the detail page
          -- (file-unity-spec.md section 6.4).
          --
          -- With no groups this subquery is empty and the result is unchanged.
          AND fc.id NOT IN (
              SELECT f.id FROM file_contents f
                JOIN image_groups g ON g.id = f.image_group_id
               WHERE f.id <> g.primary_file_id
          )
        {_day_clause}
    '''

    if limit is None:
        cursor.execute(f'''
            SELECT {_photo_cols}
            FROM file_contents fc
            LEFT JOIN exif_data ed ON fc.id = ed.file_id
            {_photo_where}
            ORDER BY fc.master_datetime DESC
        ''', _day_params)
        page_meta = None
    else:
        page_meta = _photo_page(cursor, _photo_cols, _photo_where, _day_params,
                                limit, offset, around)
    
    def convert_exif_date_to_iso(exif_date):
        """Convert EXIF date format (YYYY:MM:DD HH:MM:SS) to ISO format (YYYY-MM-DD HH:MM:SS)"""
        if not exif_date:
            return None
        try:
            # EXIF format: "2025:09:23 10:32:45"
            # ISO format: "2025-09-23 10:32:45"
            return exif_date.replace(':', '-', 2)
        except:
            return exif_date
    
    photos = []
    for row in cursor.fetchall():
        # Resolve stored paths (may be relative) to absolute
        file_path = _resolve_path(row['file_path'], _original_root(catalog))
        alt_file_path = _resolve_path(row['alternative_file_path'], catalog.target_directory)
        
        photos.append({
            'id': row['id'],
            'file_path': file_path,
            'file_name': row['file_name'],
            'file_size': row['file_size'],
            'file_type': row['file_type'],
            'modification_time': row['modification_time'],
            'master_datetime': row['master_datetime'],
            'creation_date': convert_exif_date_to_iso(row['creation_date']),
            'latitude': row['latitude'],
            'longitude': row['longitude'],
            'geohash': row['geohash'],
            'camera_make': row['camera_make'],
            'camera_model': row['camera_model'],
            'width': row['width'],
            'height': row['height'],
            'alternative_file_path': alt_file_path,
            'alternative_file_name': row['alternative_file_name'],
        })

    conn.close()
    if page_meta is not None:
        return jsonify({'photos': photos, **page_meta})
    return jsonify(photos)

@app.route('/api/catalogs/<int:catalog_id>/videos', methods=['GET'])
def get_catalog_videos(catalog_id):
    """
    Every video in a catalog, newest first.

    Implements documentation/specs/video-page-spec.md section 5.1. Modelled on
    get_catalog_photos, and disjoint from it: that one filters to image
    extensions, this one to VIDEO_EXTENSIONS, so no file appears in both.

    Three things here are easy to get wrong and each hides part of the library:

      * a video is identified by its EXTENSION, never by having a proxy. Most
        phone video is already browser-playable and takes the passthrough branch,
        which writes no alternative_versions row at all - joining to one would
        silently drop the common case (video-spec.md section 4);
      * the proxy and poster joins are LEFT and either may be NULL, for the same
        reason;
      * proxy files are themselves .mp4 and would list as videos in their own
        right without the NOT IN guard below.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()

    # Bound from the single definition in media_types, never retyped. Two lists
    # of video extensions once drifted by four entries and files in the gap were
    # invisible everywhere - which is why media_types.py exists at all.
    from media_types import VIDEO_EXTENSIONS
    exts = sorted(VIDEO_EXTENSIONS)
    placeholders = ','.join('?' * len(exts))

    cursor.execute(f'''
        SELECT
            fc.id, fc.file_path, fc.file_name, fc.file_size, fc.file_type,
            fc.modification_time, fc.master_datetime, fc.master_datetime_source,
            fc.description, fc.duration_seconds,
            ed.creation_date, ed.latitude, ed.longitude,
            ed.camera_make, ed.camera_model, ed.width, ed.height, ed.geohash,
            proxy.alternative_file_path  AS proxy_path,
            poster.alternative_file_path AS poster_path
        FROM file_contents fc
        LEFT JOIN exif_data ed ON ed.file_id = fc.id
        LEFT JOIN (
            SELECT original_file_id, MIN(alternative_file_path) AS alternative_file_path
            FROM alternative_versions
            WHERE UPPER(characteristic) LIKE '%VIDEO PROXY%'
            GROUP BY original_file_id
        ) proxy ON proxy.original_file_id = fc.id
        LEFT JOIN (
            SELECT original_file_id, MIN(alternative_file_path) AS alternative_file_path
            FROM alternative_versions
            WHERE UPPER(characteristic) LIKE '%TYPE THUMBNAIL 320%'
            GROUP BY original_file_id
        ) poster ON poster.original_file_id = fc.id
        WHERE LOWER(fc.file_type) IN ({placeholders})
          -- Not a derivative listed as if it were a video. Same change, and
          -- the same measurement, as get_catalog_photos above.
          AND fc.file_path NOT LIKE 'cache/%'
          -- Removed from the catalog by the user: listed only on Removed files.
          AND fc.removed_at IS NULL
          -- A Live Photo's clip is part of its still (file-unity-spec.md,
          -- kind 'live_photo'), not a video of its own: the still is listed
          -- on the Photos page and the clip here not at all.
          AND fc.id NOT IN (
              SELECT f.id FROM file_contents f
                JOIN image_groups g ON g.id = f.image_group_id
               WHERE f.id <> g.primary_file_id
          )
        ORDER BY fc.master_datetime DESC
    ''', exts)

    def _iso(raw):
        """EXIF 'YYYY:MM:DD HH:MM:SS' -> 'YYYY-MM-DD HH:MM:SS'."""
        return raw.replace(':', '-', 2) if raw else None

    videos = []
    for row in cursor.fetchall():
        videos.append({
            'id': row['id'],
            # Resolved against the ORIGINAL root: in a reference catalog the
            # source lives outside the catalog directory while its derivatives
            # live inside it.
            'file_path': _resolve_path(row['file_path'], _original_root(catalog)),
            'file_name': row['file_name'],
            'file_size': row['file_size'],
            'file_type': row['file_type'],
            'modification_time': row['modification_time'],
            'master_datetime': row['master_datetime'],
            'master_datetime_source': row['master_datetime_source'],
            'creation_date': _iso(row['creation_date']),
            'latitude': row['latitude'],
            'longitude': row['longitude'],
            'geohash': row['geohash'],
            'camera_make': row['camera_make'],
            'camera_model': row['camera_model'],
            'width': row['width'],
            'height': row['height'],
            'duration_seconds': row['duration_seconds'],
            # BOOLEANS, not paths. The client plays /photos/<id>/image and shows
            # /photos/<id>/thumbnail, both of which resolve the right file
            # server-side; returning the paths would publish the catalog's
            # internal layout to the browser for no purpose.
            'has_proxy': row['proxy_path'] is not None,
            'has_poster': row['poster_path'] is not None,
            'description': row['description'],
        })

    conn.close()
    return jsonify(videos)


@app.route('/api/catalogs/<int:catalog_id>/photos/<int:photo_id>', methods=['GET'])
def get_photo_detail(catalog_id, photo_id):
    """Get detailed information about a specific photo including people, location, and events"""
    from datetime import datetime
    
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    
    # Get photo with EXIF data
    # For HEIC files with alternative versions, we need to get EXIF data from the original file
    cursor.execute('''
        SELECT
            fc.id,
            fc.file_path,
            fc.file_name,
            fc.file_size,
            fc.file_type,
            fc.modification_time,
            fc.creation_time,
            fc.description,
            fc.master_datetime,
            fc.removed_at,
            ed.creation_date,
            ed.latitude,
            ed.longitude,
            ed.altitude,
            ed.camera_make,
            ed.camera_model,
            ed.lens_model,
            ed.focal_length,
            ed.aperture,
            ed.iso,
            ed.shutter_speed,
            ed.flash,
            ed.orientation,
            ed.width,
            ed.height,
            ed.geohash,
            av.alternative_file_path,
            av.alternative_file_name
        FROM file_contents fc
        -- Join EXIF data directly on the current file_id (works for original files)
        LEFT JOIN exif_data ed ON fc.id = ed.file_id
        LEFT JOIN (
            SELECT original_file_id, alternative_file_path, alternative_file_name
            FROM alternative_versions
            WHERE rung IN ('1200','1600','2048','2560')
            ORDER BY (width >= 1200) DESC,
                     CASE WHEN width >= 1200 THEN width END ASC,
                     width DESC, id ASC
        ) av ON av.original_file_id = fc.id
        WHERE fc.id = ?
    ''', (photo_id,))
    
    row = cursor.fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Photo not found'}), 404
    
    def convert_exif_date_to_iso(exif_date):
        if not exif_date:
            return None
        try:
            return exif_date.replace(':', '-', 2)
        except:
            return exif_date
    
    photo = {
        'id': row['id'],
        'file_path': row['file_path'],
        'file_name': row['file_name'],
        'file_size': row['file_size'],
        'file_type': row['file_type'],
        'modification_time': row['modification_time'],
        'creation_time': row['creation_time'],
        'description': row['description'],
        'master_datetime': row['master_datetime'],
        'creation_date': convert_exif_date_to_iso(row['creation_date']),
        'latitude': row['latitude'],
        'longitude': row['longitude'],
        'altitude': row['altitude'],
        'geohash': row['geohash'],
        'camera_make': row['camera_make'],
        'camera_model': row['camera_model'],
        'lens_model': row['lens_model'],
        'focal_length': row['focal_length'],
        'aperture': row['aperture'],
        'iso': row['iso'],
        'shutter_speed': row['shutter_speed'],
        'flash': row['flash'],
        'orientation': row['orientation'],
        'width': row['width'],
        'height': row['height'],
        'alternative_file_path': row['alternative_file_path'],
        'alternative_file_name': row['alternative_file_name'],
        # Set when the user removed it from the catalog (remove_catalog_file).
        'removed_at': row['removed_at'],
    }
    
    # Get people tagged in this photo from both people_photos and face_detections
    cursor.execute('''
        SELECT DISTINCT
            p.id,
            p.name,
            p.profile_photo_file_id
        FROM people p
        WHERE p.id IN (
            SELECT person_id FROM people_photos WHERE photo_path = ?
            UNION
            SELECT person_id FROM face_detections WHERE file_id = ? AND person_id IS NOT NULL
        )
        ORDER BY p.name
    ''', (row['file_path'], photo_id))
    
    people = []
    for person_row in cursor.fetchall():
        people.append({
            'id': person_row['id'],
            'name': person_row['name'],
            'profile_photo_file_id': person_row['profile_photo_file_id']
        })
    
    photo['people'] = people
    
    # Get events associated with this photo
    cursor.execute('''
        SELECT
            e.id,
            e.name,
            e.start_time,
            e.end_time,
            e.description,
            e.type
        FROM event_files ef
        JOIN events e ON ef.event_id = e.id
        WHERE ef.file_id = ?
        ORDER BY e.start_time DESC
    ''', (photo_id,))
    
    events = []
    for event_row in cursor.fetchall():
        events.append({
            'id': event_row['id'],
            'name': event_row['name'],
            'start_time': event_row['start_time'],
            'end_time': event_row['end_time'],
            'description': event_row['description'],
            'event_type': event_row['type']
        })
    
    photo['events'] = events
    
    # ── Where this photo was, and what place that is ─────────────────────────
    #
    # Two different questions with two different answers, so two keys:
    #
    #   photo['location']   the RESOLVED place for this file, with the
    #                       provenance of how it got one (file_locations)
    #   photo['locations']  places reached through the photo's EVENTS
    #
    # The second used to be the only one, and its comment read "locations
    # associated with this photo (through events)" - which was the whole
    # problem: a photo in no event could have no place however good its GPS,
    # and a place tagged on an Auto event does not survive the next Create
    # Events run. The resolved row is now the primary answer and the event path
    # is the fallback it always should have been. See location-spec.md 4.1, 4.7.
    try:
        photo['location'] = LocationModel(catalog_db).get_file_location(photo_id)
    except Exception as _exc:
        # A catalog that predates file_locations renders the card from its own
        # EXIF coordinates rather than failing to load.
        _logging.getLogger(__name__).warning('file location lookup failed: %s', _exc)
        photo['location'] = None

    cursor.execute('''
        SELECT DISTINCT
            l.id,
            l.name,
            l.latitude,
            l.longitude,
            l.description
        FROM event_files ef
        JOIN location_events le ON ef.event_id = le.event_id
        JOIN locations l ON le.location_id = l.id
        WHERE ef.file_id = ?
        ORDER BY l.name
    ''', (photo_id,))

    locations = []
    for loc_row in cursor.fetchall():
        locations.append({
            'id': loc_row['id'],
            'name': loc_row['name'],
            'latitude': loc_row['latitude'],
            'longitude': loc_row['longitude'],
            'description': loc_row['description']
        })

    photo['locations'] = locations

    # One image, possibly several files (file-unity-spec.md).
    #   original_kind  ORIGINAL RAW / ORIGINAL JPG - derived from the extension
    #   image_group    null unless this file shares an image with another
    #   versions       what the Versions card lists: every ORIGINAL in the group
    #                  plus the group's single set of derivatives, thumbnails
    #                  excluded
    photo['original_kind'] = _original_kind(photo.get('file_name') or '')
    try:
        group, versions = _image_group_payload(conn, catalog_id, photo_id)
        photo['image_group'] = group
        photo['versions'] = versions
    except Exception as _exc:
        # A catalog that predates the table, or any other surprise: the page
        # renders without the card rather than failing to load at all.
        _logging.getLogger(__name__).warning('image group lookup failed: %s', _exc)
        photo['image_group'] = None
        photo['versions'] = []

    # The Basic Editor card (basic-photo-editing.md 5.2), for the file that
    # owns the derivatives - the edit belongs to the whole RAW+JPG group.
    try:
        photo['edit'] = _photo_edit_payload(conn, _derivative_owner(conn, photo_id))
    except sqlite3.Error:
        photo['edit'] = None
    conn.close()
    return jsonify(photo)

def _recount_events(cursor, event_ids):
    """
    Recalculate file_count and the time window of each event from the files it
    still has, after files left it (a date edit, or a removal from the catalog).

    An event left empty keeps its row with a count of 0: the user or
    extract_events can clean it up later. Removed files do not count.
    """
    for eid in event_ids:
        # Recount from actual rows to avoid drift
        cursor.execute(
            '''SELECT COUNT(*) AS cnt
               FROM event_files ef JOIN file_contents fc ON fc.id = ef.file_id
               WHERE ef.event_id = ? AND fc.removed_at IS NULL''', (eid,)
        )
        remaining = cursor.fetchone()['cnt']

        if remaining == 0:
            cursor.execute(
                'UPDATE events SET file_count = 0 WHERE id = ?', (eid,)
            )
            continue
        # Recalculate time window from remaining files using master_datetime
        cursor.execute(
            '''SELECT MIN(fc.master_datetime) AS min_dt,
                      MAX(fc.master_datetime) AS max_dt
               FROM event_files ef
               JOIN file_contents fc ON fc.id = ef.file_id
               WHERE ef.event_id = ? AND fc.master_datetime IS NOT NULL
                 AND fc.removed_at IS NULL''',
            (eid,)
        )
        bounds = cursor.fetchone()
        if bounds and bounds['min_dt']:
            cursor.execute(
                '''UPDATE events
                   SET file_count = ?,
                       start_time = ?,
                       end_time   = ?
                   WHERE id = ?''',
                (remaining, bounds['min_dt'][:19], bounds['max_dt'][:19], eid)
            )
        else:
            cursor.execute(
                'UPDATE events SET file_count = ? WHERE id = ?', (remaining, eid)
            )


class PhotoDateError(Exception):
    """A photo date change that could not be made, with the HTTP status the
    single-photo endpoint answers it with and a short code the batch reports."""

    def __init__(self, code, message, status):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def _parse_photo_datetime(dt_str, tz_name):
    """
    The single-photo endpoint's rules for a new date, shared with the batch:
    a naive local "YYYY-MM-DDTHH:MM[:SS]", an IANA zone, a year from 1900 to
    next year. Returns (aware local datetime, None) or (None, error message).
    """
    from datetime import datetime as _dt_cls
    from zoneinfo import ZoneInfo

    dt_str = (dt_str or '').strip()
    if not dt_str:
        return None, 'datetime is required'
    try:
        tz = ZoneInfo((tz_name or 'UTC').strip())
    except Exception:
        return None, 'invalid timezone'
    try:
        naive_dt = _dt_cls.fromisoformat(dt_str)
    except (ValueError, TypeError):
        return None, 'invalid datetime format'
    local_dt = naive_dt.replace(tzinfo=tz)
    current_year = _dt_cls.now().year
    if local_dt.year < 1900 or local_dt.year > current_year + 1:
        return None, 'year out of range'
    return local_dt, None


def _apply_photo_date(conn, catalog, photo_id, local_dt):
    """
    Give one file a new date: rename and move the original into its date folder
    (copy catalogs only), move its derivatives, and write master_datetime,
    master_datetime_source = 'manual' and exif_data.creation_date. Commits.

    Event membership is NOT touched: the single-photo endpoint places the file
    itself (_place_in_nearest_event), and a batch detaches its files and
    rebuilds events once (mass-edit-event-photo-dates-spec.md section 5.2).

    Raises PhotoDateError; returns the update as a dict.
    """
    import shutil
    import re as _re
    from pathlib import Path

    # ISO 8601 master_datetime with offset
    offset   = local_dt.utcoffset()
    tot_secs = int(offset.total_seconds()) if offset is not None else 0
    sign     = '+' if tot_secs >= 0 else '-'
    abs_secs = abs(tot_secs)
    off_hh, rem = divmod(abs_secs, 3600)
    off_mm      = rem // 60
    master_dt_str = local_dt.strftime('%Y-%m-%dT%H:%M:%S') + f"{sign}{off_hh:02d}:{off_mm:02d}"
    exif_date_str = local_dt.strftime('%Y:%m:%d %H:%M:%S')

    cursor = conn.cursor()
    cursor.execute(
        'SELECT id, file_path, file_name, sha256_hash, removed_at FROM file_contents WHERE id = ?',
        (photo_id,)
    )
    fc = cursor.fetchone()
    if not fc:
        raise PhotoDateError('not_found', 'Photo not found', 404)
    if fc['removed_at']:
        # Re-dating would put it back into an event.
        raise PhotoDateError('removed', 'This file has been removed from the catalog', 409)

    cursor.execute('SELECT id FROM exif_data WHERE file_id = ?', (photo_id,))
    exif_row = cursor.fetchone()

    # ── Derive new filename and directory ─────────────────────────────────────
    _TS_RE = _re.compile(r'^(\d{14})-([0-9a-fA-F]{10})-(.+)$')

    def _derive_new_filename(current_name, sha256_hash, new_dt):
        new_ts = new_dt.strftime('%Y%m%d%H%M%S')
        m = _TS_RE.match(current_name)
        if m:
            sha_prefix    = m.group(2)
            original_base = m.group(3)
        else:
            sha_prefix    = (sha256_hash or '')[:10]
            original_base = current_name
        return f"{new_ts}-{sha_prefix}-{original_base}"

    # Videos share this endpoint (video-page-spec.md section 5.2), and are filed
    # under Videos/ and cache/Videos/ rather than Images/ - the same split
    # catalog.Catalog.calculateDir and create_video_proxy/poster make on import.
    media_dir = 'Videos' if is_video_file(fc['file_name']) else 'Images'

    def _derive_new_directory(target_dir, new_dt):
        return os.path.join(
            target_dir, media_dir,
            f"{new_dt.year:04d}",
            f"{new_dt.month:02d}",
            f"{new_dt.day:02d}",
        )

    old_abs = _resolve_path(fc['file_path'], _original_root(catalog))

    # In a reference catalog the file on disk belongs to the user, and this
    # application does not move or rename it - which is the entire point of the
    # mode (file-ingestion-spec.md section 6.1).
    #
    # Nothing is lost by not moving it. The date lives in master_datetime either
    # way; in copy mode the filename and the dated folder are a MIRROR of that
    # value, not the source of truth, and a reference catalog simply has no
    # mirror to update. The derivatives below are still re-filed, because those
    # are ours.
    renamed_original = getattr(catalog, 'ingest_mode', 'copy') != 'reference'

    if renamed_original:
        new_file_name = _derive_new_filename(fc['file_name'], fc['sha256_hash'], local_dt)
        new_dir_abs   = _derive_new_directory(catalog.target_directory, local_dt)
        new_abs       = os.path.join(new_dir_abs, new_file_name)

        # ── Check for filename collision ──────────────────────────────────────
        if os.path.exists(new_abs) and os.path.abspath(new_abs) != os.path.abspath(old_abs):
            raise PhotoDateError('file_move_conflict',
                                 f'{new_file_name} already exists in the new date folder', 409)

        # ── Move original file ────────────────────────────────────────────────
        try:
            if os.path.exists(old_abs):
                if os.path.abspath(old_abs) != os.path.abspath(new_abs):
                    os.makedirs(new_dir_abs, exist_ok=True)
                    shutil.move(old_abs, new_abs)
            else:
                print(f"[update_photo_date] Warning: source file not found on disk: {old_abs}")
                os.makedirs(new_dir_abs, exist_ok=True)
        except Exception as e:
            raise PhotoDateError('rename_failed', f'rename_failed: {e}', 500)

        new_rel_path = os.path.relpath(new_abs, catalog.target_directory)
    else:
        # Untouched, and the stored row keeps pointing at it.
        new_file_name = fc['file_name']
        new_rel_path  = fc['file_path']

    # ── Move cache / alternative-version files ────────────────────────────────
    cursor.execute(
        'SELECT id, alternative_file_path, alternative_file_name '
        'FROM alternative_versions WHERE original_file_id = ?',
        (photo_id,)
    )
    av_rows = cursor.fetchall()
    for av_row in av_rows:
        old_av_abs = _resolve_path(av_row['alternative_file_path'], catalog.target_directory)
        if not os.path.exists(old_av_abs):
            continue  # already missing — skip silently

        # Preserve the variant suffix AND extension (_alt.jpg, _alt_1600.jpg,
        # _thumb_320.jpg, _poster.jpg, _proxy.mp4) so that different alternative
        # versions don't collide on the same destination path.
        #
        # The suffix is anchored at the END of the stem. The earlier pattern,
        # _(?:alt|thumb)\b, never matched _alt_1600 or _thumb_320 ('_' is a word
        # character, so there is no boundary), and never matched a video's
        # _proxy or _poster; every one of those fell back to '_alt.jpg' and the
        # moves overwrote each other - on a video, the poster replaced the proxy.
        old_av_name  = av_row['alternative_file_name']
        old_av_stem  = Path(old_av_name).stem            # e.g. "20260101120000-abc_alt_1600"
        old_av_ext   = Path(old_av_name).suffix          # e.g. ".jpg", ".mp4"
        new_base_stem = Path(new_file_name).stem         # e.g. "20230820120000-abc-photo"
        _SUFFIX_RE = _re.compile(r'(_(?:alt|thumb|poster|proxy)(?:_[A-Za-z0-9]+)?)$',
                                 _re.IGNORECASE)
        _m = _SUFFIX_RE.search(old_av_stem)
        if renamed_original and _m:
            new_alt_name = f"{new_base_stem}{_m.group(1)}{old_av_ext}"
        else:
            # A reference catalog's derivatives are named from a unique stem, not
            # the original's own name, and an unrecognised name has no suffix we
            # can safely re-attach: either way keep the name and move the folder.
            new_alt_name = old_av_name

        new_cache_dir = os.path.join(
            catalog.target_directory, 'cache', media_dir,
            f"{local_dt.year:04d}", f"{local_dt.month:02d}", f"{local_dt.day:02d}",
        )
        os.makedirs(new_cache_dir, exist_ok=True)
        new_av_abs = os.path.join(new_cache_dir, new_alt_name)
        new_av_rel = os.path.relpath(new_av_abs, catalog.target_directory)

        try:
            shutil.move(old_av_abs, new_av_abs)
        except Exception as e:
            print(f"[update_photo_date] Warning: could not move alt version: {e}")
            continue

        cursor.execute(
            'UPDATE alternative_versions '
            'SET alternative_file_path = ?, alternative_file_name = ? WHERE id = ?',
            (new_av_rel, new_alt_name, av_row['id'])
        )

    # ── Update database records ───────────────────────────────────────────────
    cursor.execute(
        '''UPDATE file_contents
           SET file_path = ?, file_name = ?,
               master_datetime = ?, master_datetime_source = 'manual'
           WHERE id = ?''',
        (new_rel_path, new_file_name, master_dt_str, photo_id)
    )

    if exif_row:
        cursor.execute(
            'UPDATE exif_data SET creation_date = ? WHERE file_id = ?',
            (exif_date_str, photo_id)
        )

    conn.commit()

    return {
        'file_id':                photo_id,
        'new_file_path':          new_rel_path,
        'new_file_name':          new_file_name,
        'master_datetime':        master_dt_str,
        'master_datetime_source': 'manual',
        # False for a reference catalog: the date changed, the file did not.
        # Worth stating rather than leaving the client to infer it from an
        # unchanged path.
        'file_renamed':           renamed_original,
    }


def _place_in_nearest_event(conn, photo_id, local_dt):
    """
    Move one re-dated file to the event its new date belongs in: out of every
    event it was in, then into the nearest event within six hours, or into a
    new Manual event of its own. Commits.

    Returns (event_action, event_id, event_name).
    """
    cursor = conn.cursor()
    import datetime as _datetime_mod

    # Convert local_dt to a plain UTC string for SQLite datetime() comparisons.
    # SQLite understands ISO-8601 strings without timezone suffix as local/UTC
    # depending on context, so we strip the offset and work in UTC throughout.
    try:
        utc_dt      = local_dt.astimezone(_datetime_mod.timezone.utc)
        new_iso_utc = utc_dt.strftime('%Y-%m-%d %H:%M:%S')
    except Exception:
        new_iso_utc = local_dt.strftime('%Y-%m-%d %H:%M:%S')

    # Step 8a — capture the events this photo currently belongs to BEFORE
    # removing it, so we can decrement their file_count and recalculate their
    # time windows correctly afterward.
    cursor.execute(
        '''SELECT e.id, e.name, e.start_time, e.end_time, e.file_count
           FROM events e
           JOIN event_files ef ON ef.event_id = e.id
           WHERE ef.file_id = ?''',
        (photo_id,)
    )
    old_events = [dict(r) for r in cursor.fetchall()]

    # Step 8b — remove the photo from all its current events.
    cursor.execute('DELETE FROM event_files WHERE file_id = ?', (photo_id,))
    conn.commit()

    # Step 8c — for every event the photo just left, recalculate file_count and
    # time window from the remaining members.
    _recount_events(cursor, [ev['id'] for ev in old_events])

    conn.commit()

    # Step 8d — find an existing event whose time window (with the ±6 h gap
    # tolerance used by extract_events.py) covers the new datetime.
    event_action = 'no_change'
    event_id     = None
    event_name   = None

    cursor.execute(
        '''SELECT id, name, start_time, end_time
           FROM events
           WHERE datetime(?) BETWEEN
                 datetime(start_time, '-6 hours') AND
                 datetime(end_time,   '+6 hours')
           ORDER BY ABS(strftime('%s', ?) - strftime('%s', start_time)) ASC
           LIMIT 1''',
        (new_iso_utc, new_iso_utc)
    )
    matching_event = cursor.fetchone()

    if matching_event:
        event_id   = matching_event['id']
        event_name = matching_event['name']

        cursor.execute(
            'INSERT OR IGNORE INTO event_files (event_id, file_id) VALUES (?, ?)',
            (event_id, photo_id)
        )
        # Recount after insertion to keep file_count accurate
        cursor.execute(
            'SELECT COUNT(*) AS cnt FROM event_files WHERE event_id = ?', (event_id,)
        )
        new_count = cursor.fetchone()['cnt']
        # Extend time window if the new datetime falls outside the existing range
        cursor.execute(
            '''UPDATE events
               SET file_count = ?,
                   start_time = MIN(start_time, ?),
                   end_time   = MAX(end_time,   ?)
               WHERE id = ?''',
            (new_count, new_iso_utc, new_iso_utc, event_id)
        )
        event_action = 'added_to_existing'
    else:
        # Step 8e — no existing event covers the new date: create a new Manual event.
        ev_start = local_dt.strftime('%Y-%m-%d %H:%M:%S')
        ev_name  = f"Event {local_dt.strftime('%Y-%m-%d %H:%M')} (#manual)"
        ev_desc  = f"Manually created event for 1 file dated {local_dt.strftime('%Y-%m-%d')}."
        exif_row = cursor.execute(
            'SELECT latitude, longitude FROM exif_data WHERE file_id = ?',
            (photo_id,)).fetchone()
        ev_lat   = exif_row['latitude']  if exif_row else None
        ev_lon   = exif_row['longitude'] if exif_row else None

        cursor.execute(
            '''INSERT INTO events
                   (name, start_time, end_time, latitude, longitude,
                    description, file_count, type)
               VALUES (?, ?, ?, ?, ?, ?, 1, 'Manual')''',
            (ev_name, ev_start, ev_start, ev_lat, ev_lon, ev_desc)
        )
        event_id   = cursor.lastrowid
        event_name = ev_name
        cursor.execute(
            'INSERT OR IGNORE INTO event_files (event_id, file_id) VALUES (?, ?)',
            (event_id, photo_id)
        )
        event_action = 'new_event_created'

    conn.commit()
    return event_action, event_id, event_name


@app.route('/api/catalogs/<int:catalog_id>/photos/<int:photo_id>/date', methods=['PUT'])
def update_photo_date(catalog_id, photo_id):
    """Manually update the date/time of a photo.

    Moves the original file and its cache/alternative-version file to the
    correct date-partitioned directory, renames both to reflect the new
    timestamp, updates file_contents and exif_data in the catalog DB, and
    re-assigns the photo to the appropriate event.
    """
    body = request.get_json(silent=True) or {}
    local_dt, err = _parse_photo_datetime(body.get('datetime'), body.get('timezone'))
    if err:
        return jsonify({'error': err}), 400

    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        try:
            update = _apply_photo_date(conn, catalog, photo_id, local_dt)
        except PhotoDateError as e:
            # file_move_conflict has always been answered as the bare code.
            message = e.code if e.code == 'file_move_conflict' else e.message
            return jsonify({'error': message}), e.status
        event_action, event_id, event_name = _place_in_nearest_event(conn, photo_id, local_dt)
    finally:
        conn.close()

    update.update({
        'event_action': event_action,
        'event_id':     event_id,
        'event_name':   event_name,
    })
    return jsonify(update)


# ─── Batch date changes ───────────────────────────────────────────────────────
# The event date editor's Confirm (mass-edit-event-photo-dates-spec.md
# section 5). The request is validated and recorded, answered at once, and the
# re-dating runs on a thread: the user is sent back to the event page and
# follows the batch there.

DATE_BATCH_MAX_FILES = 5000

# Batches whose thread is alive in THIS process. A row still 'running' that is
# not in here belongs to a process that has gone - a restart - and is reported
# as interrupted the next time anyone asks (section 5.1).
#
# Keyed by (catalog_id, batch_id), and the catalog half is not decoration.
# date_change_batches lives in EACH CATALOG'S OWN database with its own
# AUTOINCREMENT, so batch 1 exists in every catalog. Keyed by batch id alone:
#
#   catalog A starts batch 1      -> {1}
#   catalog B starts batch 1      -> {1}, the add is a no-op
#   catalog A's thread finishes   -> discard(1) -> {}
#   anyone asks about B's batch 1 -> "thread is gone" -> marked INTERRUPTED
#                                    while it is still re-dating files
#
# Two catalogs is the normal case, so this was reachable in ordinary use. It
# also made test_photo_dates_batch.py flaky: every test gets a fresh catalog,
# so every test's first batch is id 1, and a straggler thread from the previous
# one discarded the current one's key.
_date_batch_threads = set()
_date_batch_threads_lock = threading.Lock()

# A queue row in one of these has run, or never will.
_QUEUE_DONE_STATUSES = ('completed', 'failed', 'cancelled', 'stopped', 'superseded')


def _create_events_workflow(catalog):
    """This catalog's Create Events workflow, found by script: the user may
    rename it, so the name proves nothing."""
    wm = WorkflowModel(CatalogDatabase(catalog.target_directory))
    for wf in wm.get_all():
        if os.path.basename(wf.get('python_script') or '') == 'ev_extract_events.py':
            return wf
    return None


def _import_running(conn):
    """True when an import started by this server is still running in the
    catalog. Re-dating files while an import places files beside them is not a
    race worth having (section 5.1)."""
    rows = conn.execute(
        "SELECT w.python_script FROM workflow_executions e "
        "JOIN workflows w ON w.id = e.workflow_id "
        "WHERE e.status = 'running' AND e.started_at >= ?",
        (_SERVER_START_TIME,)).fetchall()
    return any(os.path.basename(r['python_script'] or '') in workflow_triggers.INGEST_SCRIPTS
               for r in rows)


def _date_batch_dict(row):
    batch = dict(row)
    batch['file_ids'] = _json.loads(batch['file_ids'] or '[]')
    raw = batch.pop('result_json', None)
    batch['result'] = _json.loads(raw) if raw else None
    return batch


def _date_batch_emit(catalog_id, batch_id, done, total, status):
    try:
        socketio.emit('date_batch_progress', {
            'catalog_id': catalog_id, 'batch_id': batch_id,
            'done': done, 'total': total, 'status': status,
        })
    except Exception as e:
        print(f'[date batch] could not emit progress: {e}')


def _finish_rebuilding_batches(catalog, queue_id, queue_status):
    """A Create Events queue row finished: every batch waiting on it is done."""
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM date_change_batches WHERE status = 'rebuilding' AND queue_id = ?",
            (queue_id,)).fetchall()
        for row in rows:
            result = _json.loads(row['result_json']) if row['result_json'] else {}
            result['rebuild_status'] = queue_status
            conn.execute(
                "UPDATE date_change_batches SET status = 'completed', result_json = ?, "
                "finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (_json.dumps(result), row['id']))
            conn.commit()
            _date_batch_emit(catalog.id, row['id'], row['done'], row['total'], 'completed')
    finally:
        conn.close()


def _refresh_date_batch(catalog, conn, row):
    """
    Bring a batch row up to date before answering for it: a 'running' batch
    whose thread is gone was interrupted, and a 'rebuilding' one is complete
    once its Create Events queue row is. The scheduler's on_finished hook does
    the latter too; this covers a restart in between.
    """
    if row['status'] == 'running':
        with _date_batch_threads_lock:
            alive = (catalog.id, row['id']) in _date_batch_threads
        if not alive:
            conn.execute(
                "UPDATE date_change_batches SET status = 'interrupted', "
                "finished_at = CURRENT_TIMESTAMP WHERE id = ? AND status = 'running'",
                (row['id'],))
            conn.commit()
    elif row['status'] == 'rebuilding' and row['queue_id']:
        item = workflow_scheduler.get_item(_queue_db_path(catalog), row['queue_id'])
        if item is None:
            _finish_rebuilding_batches(catalog, row['queue_id'], 'unknown')
        elif item['status'] == 'superseded':
            # Replaced by a later Create Events row, which is the run that will
            # see these dates: follow it.
            later = conn.execute(
                "SELECT id FROM workflow_queue WHERE script_name = 'ev_extract_events.py' "
                "AND id > ? ORDER BY id LIMIT 1", (row['queue_id'],)).fetchone()
            if later:
                conn.execute('UPDATE date_change_batches SET queue_id = ? WHERE id = ?',
                             (later['id'], row['id']))
                conn.commit()
            else:
                _finish_rebuilding_batches(catalog, row['queue_id'], 'superseded')
        elif item['status'] in _QUEUE_DONE_STATUSES:
            _finish_rebuilding_batches(catalog, row['queue_id'], item['status'])
    return conn.execute('SELECT * FROM date_change_batches WHERE id = ?',
                        (row['id'],)).fetchone()


def _date_batch_payload(catalog, conn, row):
    batch = _date_batch_dict(_refresh_date_batch(catalog, conn, row))
    batch['execution_id'] = None
    if batch.get('queue_id'):
        item = workflow_scheduler.get_item(_queue_db_path(catalog), batch['queue_id'])
        if item:
            batch['execution_id'] = item.get('execution_id')
    return batch


def _run_date_batch(catalog, batch_id, changes, rebuild_events):
    """
    The background half of POST /photos/dates (section 5.1, steps 1 to 4).

    Each file is re-dated and committed on its own: file moves cannot be rolled
    back with the database, so the unit that is all-or-nothing is one file.
    """
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    cursor = conn.cursor()
    done = 0
    total = 0
    try:
        # ── 1. Expand image groups ────────────────────────────────────────────
        # A RAW+JPG pair keeps one date. A file named in the request is never
        # overridden by a partner's entry: what the user asked for wins.
        requested = {}
        for file_ids, local_dt in changes:
            for fid in file_ids:
                requested[fid] = local_dt
        assigned = {fid: (dt, None) for fid, dt in requested.items()}
        for fid, local_dt in requested.items():
            for partner in _image_file_ids(cursor, fid):
                if partner not in assigned:
                    assigned[partner] = (local_dt, fid)
        total = len(assigned)
        conn.execute('UPDATE date_change_batches SET total = ? WHERE id = ?', (total, batch_id))
        conn.commit()

        # ── 2. Re-date each file ──────────────────────────────────────────────
        updated, failed, placed_dt = [], [], {}
        last_emit = 0.0
        for fid, (local_dt, via) in assigned.items():
            try:
                u = _apply_photo_date(conn, catalog, fid, local_dt)
                updated.append({'file_id': fid, 'master_datetime': u['master_datetime'],
                                'new_file_name': u['new_file_name'],
                                'file_renamed': u['file_renamed'], 'via_group': via})
                placed_dt[fid] = local_dt
            except PhotoDateError as e:
                failed.append({'file_id': fid, 'error': e.code, 'message': e.message})
            except Exception as e:
                conn.rollback()
                failed.append({'file_id': fid, 'error': 'error', 'message': str(e)})
            done += 1
            conn.execute('UPDATE date_change_batches SET done = ? WHERE id = ?', (done, batch_id))
            conn.commit()
            now = time.time()
            if now - last_emit >= 0.5 or done == total:
                last_emit = now
                _date_batch_emit(catalog.id, batch_id, done, total, 'running')

        # ── 3. Detach the re-dated files from every event ─────────────────────
        # Without this a file in a Manual event would never move: Create Events
        # leaves files linked to Manual events where they are (section 2.4).
        ids = [u['file_id'] for u in updated]
        left = set()
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            ph = _in_clause(chunk)
            left.update(r['event_id'] for r in cursor.execute(
                f'SELECT DISTINCT event_id FROM event_files WHERE file_id IN ({ph})', chunk))
            cursor.execute(f'DELETE FROM event_files WHERE file_id IN ({ph})', chunk)
        _recount_events(cursor, sorted(left))
        conn.commit()

        # ── 4. Rebuild events, or place each file the single-photo way ────────
        status, queue_id = 'completed', None
        workflow = _create_events_workflow(catalog) if (rebuild_events and updated) else None
        if workflow:
            queue_id, _created = workflow_scheduler.enqueue(
                _queue_db_path(catalog), dict(workflow),
                trigger_source='date_change', trigger_detail={'date_batch': batch_id})
            workflow_scheduler.promote(_queue_db_path(catalog), queue_id)
            if _scheduler:
                _scheduler.kick()
            _emit_queue(catalog)
            status = 'rebuilding'
        else:
            # rebuild_events off, or a catalog with no Create Events workflow:
            # no file may be left without an event.
            for fid, local_dt in placed_dt.items():
                _place_in_nearest_event(conn, fid, local_dt)

        result = {'updated': updated, 'failed': failed,
                  'detached_from_events': sorted(left)}
        if rebuild_events and updated and not workflow:
            result['rebuild_status'] = 'no_create_events_workflow'
        conn.execute(
            "UPDATE date_change_batches SET status = ?, result_json = ?, queue_id = ?, "
            "finished_at = CASE WHEN ? = 'completed' THEN CURRENT_TIMESTAMP END "
            "WHERE id = ?",
            (status, _json.dumps(result), queue_id, status, batch_id))
        conn.commit()
        _date_batch_emit(catalog.id, batch_id, done, total, status)
    except Exception as e:
        print(f'[date batch] batch {batch_id} failed: {e}')
        try:
            conn.rollback()
            conn.execute(
                "UPDATE date_change_batches SET status = 'failed', result_json = ?, "
                "finished_at = CURRENT_TIMESTAMP WHERE id = ?",
                (_json.dumps({'error': str(e)}), batch_id))
            conn.commit()
        except Exception:
            pass
        _date_batch_emit(catalog.id, batch_id, done, total, 'failed')
    finally:
        conn.close()
        with _date_batch_threads_lock:
            _date_batch_threads.discard((catalog.id, batch_id))


def _is_id(value):
    return isinstance(value, int) and not isinstance(value, bool)


@app.route('/api/catalogs/<int:catalog_id>/photos/dates', methods=['POST'])
def update_photo_dates(catalog_id):
    """
    Change the date of many photos at once, then rebuild events
    (mass-edit-event-photo-dates-spec.md section 5.1). Answers 202 with a batch
    id; the work runs in the background.
    """
    body = request.get_json(silent=True) or {}
    raw_changes = body.get('changes')
    if not isinstance(raw_changes, list) or not raw_changes:
        return jsonify({'error': 'changes must be a non-empty list'}), 400

    # ── Validate everything before changing anything ──────────────────────────
    changes, seen = [], set()
    for i, change in enumerate(raw_changes):
        if not isinstance(change, dict):
            return jsonify({'error': f'changes[{i}] must be an object'}), 400
        file_ids = change.get('file_ids')
        if not isinstance(file_ids, list) or not file_ids or not all(_is_id(f) for f in file_ids):
            return jsonify({'error': f'changes[{i}].file_ids must be a non-empty list of ids'}), 400
        local_dt, err = _parse_photo_datetime(change.get('datetime'), change.get('timezone'))
        if err:
            return jsonify({'error': f'changes[{i}]: {err}'}), 400
        if seen.intersection(file_ids) or len(set(file_ids)) != len(file_ids):
            return jsonify({'error': 'a file appears more than once in changes'}), 400
        seen.update(file_ids)
        changes.append((file_ids, local_dt))
    if len(seen) > DATE_BATCH_MAX_FILES:
        return jsonify({'error': f'at most {DATE_BATCH_MAX_FILES} files per request'}), 400

    event_id = body.get('event_id')
    if event_id is not None and not _is_id(event_id):
        return jsonify({'error': 'event_id must be an id'}), 400
    rebuild_events = body.get('rebuild_events', True) is not False

    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    requested = [fid for file_ids, _ in changes for fid in file_ids]
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        if _import_running(conn):
            return jsonify({'error': 'catalog_busy',
                            'message': 'An import is running in this catalog. '
                                       'Confirm again once it has finished.'}), 409
        cur = conn.execute(
            "INSERT INTO date_change_batches (event_id, status, file_ids, total) "
            "VALUES (?, 'running', ?, ?)",
            (event_id, _json.dumps(requested), len(requested)))
        batch_id = cur.lastrowid
        conn.commit()
    finally:
        conn.close()

    with _date_batch_threads_lock:
        _date_batch_threads.add((catalog.id, batch_id))
    threading.Thread(target=_run_date_batch,
                     args=(catalog, batch_id, changes, rebuild_events),
                     daemon=True, name=f'date-batch-{batch_id}').start()
    return jsonify({'batch_id': batch_id, 'file_count': len(requested)}), 202


@app.route('/api/catalogs/<int:catalog_id>/photos/dates/batches/<int:batch_id>', methods=['GET'])
def get_photo_date_batch(catalog_id, batch_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        row = conn.execute('SELECT * FROM date_change_batches WHERE id = ?',
                           (batch_id,)).fetchone()
        if not row:
            return jsonify({'error': 'Batch not found'}), 404
        return jsonify(_date_batch_payload(catalog, conn, row))
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/photos/dates/batches', methods=['GET'])
def list_photo_date_batches(catalog_id):
    """
    The batches opened on an event, newest first; with latest=1, just the
    newest. The event page asks when its event id has vanished in a rebuild
    (section 4.5).
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    event_id = request.args.get('event_id', type=int)
    if event_id is None:
        return jsonify({'error': 'event_id is required'}), 400
    latest = request.args.get('latest') == '1'
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM date_change_batches WHERE event_id = ? ORDER BY id DESC LIMIT ?',
            (event_id, 1 if latest else 20)).fetchall()
        if latest:
            if not rows:
                return jsonify({'error': 'No batch for this event'}), 404
            return jsonify(_date_batch_payload(catalog, conn, rows[0]))
        return jsonify([_date_batch_payload(catalog, conn, r) for r in rows])
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/events/locate', methods=['GET'])
def locate_files_in_events(catalog_id):
    """For each file, the events it belongs to now (section 5.3)."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    try:
        ids = [int(x) for x in (request.args.get('file_ids') or '').split(',') if x.strip()]
    except ValueError:
        return jsonify({'error': 'file_ids must be a comma-separated list of ids'}), 400
    if len(ids) > DATE_BATCH_MAX_FILES:
        return jsonify({'error': f'at most {DATE_BATCH_MAX_FILES} files per request'}), 400
    located = {str(i): [] for i in ids}
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in conn.execute(
                    'SELECT ef.file_id, e.id, e.name, e.start_time, e.type '
                    'FROM event_files ef JOIN events e ON e.id = ef.event_id '
                    f'WHERE ef.file_id IN ({_in_clause(chunk)}) ORDER BY e.start_time',
                    chunk):
                located[str(r['file_id'])].append(
                    {'id': r['id'], 'name': r['name'],
                     'start_time': r['start_time'], 'type': r['type']})
    finally:
        conn.close()
    return jsonify(located)


# ─── Removing files from a catalog ────────────────────────────────────────────
#
# Removal is a catalog operation, not a file operation. The original stays where
# it is - the user deletes it by hand - and its row stays in file_contents with
# removed_at set, so:
#
#   * the importer's SHA-256 check still recognises the file and does not bring
#     it back while it is on disk;
#   * every listing, event, album and face workflow skips it (removed_at IS NULL
#     in each of their queries);
#   * GET /removed-files can report what is still waiting to be deleted by hand.
#
# What removal does throw away is everything this application MADE from the
# file: its alternative versions (sizes, thumbnails, video proxy and poster),
# its event and album memberships, and any cover it served as. Face detections
# are kept, hidden, so confirmed identities keep training recognition.
#
# Once the original is gone from disk, purge deletes the row and everything
# still hanging off it. After that the catalog no longer knows the file, and
# importing it again would bring it back as new.
#
# A RAW+JPG pair is one image (file-unity-spec.md), so every operation here
# applies to the whole group.

def _sqlite_try(cursor, sql, params=()):
    """Execute, treating a table an older catalog does not have as empty."""
    try:
        cursor.execute(sql, params)
        return cursor.fetchall()
    except sqlite3.OperationalError as exc:
        if 'no such table' in str(exc) or 'no such column' in str(exc):
            return []
        raise


def _image_file_ids(cursor, file_id):
    """The file and, when it is one half of a RAW+JPG image, its partner."""
    row = cursor.execute(
        'SELECT id, image_group_id FROM file_contents WHERE id = ?', (file_id,)).fetchone()
    if not row:
        return []
    if row['image_group_id'] is None:
        return [row['id']]
    return [r['id'] for r in cursor.execute(
        'SELECT id FROM file_contents WHERE image_group_id = ? ORDER BY id',
        (row['image_group_id'],))]


def _in_clause(ids):
    return ','.join('?' * len(ids))


@app.route('/api/catalogs/<int:catalog_id>/files/<int:file_id>/remove', methods=['POST'])
def remove_catalog_file(catalog_id, file_id):
    """
    Remove a photo or video from the catalog, leaving the original on disk.

    Database changes are committed first and derivative files deleted after, so
    a failure part-way leaves at worst an orphaned cache file - never a row that
    points at a file which is gone.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    conn = CatalogDatabase(catalog.target_directory).get_connection()
    cursor = conn.cursor()
    try:
        ids = _image_file_ids(cursor, file_id)
        if not ids:
            return jsonify({'error': 'File not found'}), 404
        marks = _in_clause(ids)
        files = [dict(r) for r in cursor.execute(
            f'SELECT id, file_name, file_path, removed_at FROM file_contents '
            f'WHERE id IN ({marks})', ids)]
        if all(f['removed_at'] for f in files):
            return jsonify({'error': 'Already removed from the catalog'}), 409

        # ── Derivatives: remember the files, drop the rows ────────────────────
        derivative_paths = [r['alternative_file_path'] for r in cursor.execute(
            f'SELECT alternative_file_path FROM alternative_versions '
            f'WHERE original_file_id IN ({marks})', ids)]
        cursor.execute(
            f'DELETE FROM alternative_versions WHERE original_file_id IN ({marks})', ids)

        # ── Events ────────────────────────────────────────────────────────────
        event_rows = [dict(r) for r in cursor.execute(
            f'SELECT DISTINCT e.id, e.name FROM events e '
            f'JOIN event_files ef ON ef.event_id = e.id WHERE ef.file_id IN ({marks})', ids)]
        cursor.execute(f'DELETE FROM event_files WHERE file_id IN ({marks})', ids)
        # A cleared cover is re-picked from the remaining files the next time
        # the event is listed (EventModel.resolve_covers).
        cursor.execute(
            f'UPDATE events SET cover_file_id = NULL WHERE cover_file_id IN ({marks})', ids)
        _recount_events(cursor, [e['id'] for e in event_rows])

        # ── Albums ────────────────────────────────────────────────────────────
        album_rows = [dict(r) for r in _sqlite_try(
            cursor,
            f'SELECT DISTINCT a.id, a.name FROM section_photos sp '
            f'JOIN album_sections s ON s.id = sp.section_id '
            f'JOIN albums a ON a.id = s.album_id WHERE sp.file_id IN ({marks})', ids)]
        _sqlite_try(cursor, f'DELETE FROM section_photos WHERE file_id IN ({marks})', ids)
        _sqlite_try(cursor,
                    f'UPDATE album_sections SET cover_file_id = NULL '
                    f'WHERE cover_file_id IN ({marks})', ids)

        # A profile photo is shown from the derivatives that are about to go.
        cursor.execute(
            f'UPDATE people SET profile_photo_file_id = NULL '
            f'WHERE profile_photo_file_id IN ({marks})', ids)

        cursor.execute(
            f'UPDATE file_contents SET removed_at = CURRENT_TIMESTAMP '
            f'WHERE id IN ({marks}) AND removed_at IS NULL', ids)
        conn.commit()

        # ── Derivative files on disk, after the commit ───────────────────────
        deleted, failed = 0, []
        for rel in derivative_paths:
            if not rel:
                continue
            # Never delete something that is itself an original in this catalog.
            if cursor.execute('SELECT 1 FROM file_contents WHERE file_path = ?',
                              (rel,)).fetchone():
                continue
            path = _resolve_path(rel, catalog.target_directory)
            try:
                if os.path.exists(path):
                    os.remove(path)
                    deleted += 1
            except OSError as exc:
                failed.append(f'{rel}: {exc}')
        if failed:
            print(f'[remove_catalog_file] could not delete: {failed}')

        return jsonify({
            'removed_file_ids': ids,
            'file_names': [f['file_name'] for f in files],
            'file_paths': [f['file_path'] for f in files],
            'derivatives_deleted': deleted,
            'derivatives_not_deleted': len(failed),
            'events': event_rows,
            'albums': album_rows,
        })
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/removed-files', methods=['GET'])
def list_removed_files(catalog_id):
    """
    Files removed from the catalog, newest removal first, and whether each
    original is still on disk - i.e. still waiting to be deleted by hand.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        rows = [dict(r) for r in conn.execute('''
            SELECT id, file_name, file_path, file_size, file_type,
                   master_datetime, removed_at, image_group_id
            FROM file_contents
            WHERE removed_at IS NOT NULL
            ORDER BY removed_at DESC, id
        ''')]
    finally:
        conn.close()

    root = _original_root(catalog)
    for r in rows:
        r['absolute_path'] = _resolve_path(r['file_path'], root)
        r['on_disk'] = os.path.exists(r['absolute_path'])
        r['kind'] = 'video' if is_video_file(r['file_name']) else 'photo'
    return jsonify({
        'files': rows,
        'total': len(rows),
        'on_disk': sum(1 for r in rows if r['on_disk']),
        # In a reference catalog the originals are the user's own library, not
        # copies inside the catalog directory - worth saying on the page.
        'ingest_mode': getattr(catalog, 'ingest_mode', 'copy'),
    })


def _restore_derivatives(catalog, file_ids):
    """
    Re-render what removal deleted, with the importer's own render functions so
    the names, sizes and rows are exactly what an import makes. Runs in a
    background thread: a video transcode takes minutes.
    """
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'workflows', 'io_picture_import_workflow.py')
    spec = importlib.util.spec_from_file_location('io_picture_import_workflow', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    catalog_db = CatalogDatabase(catalog.target_directory)
    root = _original_root(catalog)
    for fid in file_ids:
        conn = catalog_db.get_connection()
        try:
            row = conn.execute(
                'SELECT f.id, f.file_path, f.file_name, f.master_datetime, '
                '       g.primary_file_id '
                'FROM file_contents f LEFT JOIN image_groups g ON g.id = f.image_group_id '
                'WHERE f.id = ? AND f.removed_at IS NULL', (fid,)).fetchone()
            if not row:
                continue
            # Only the primary of a RAW+JPG pair owns derivatives.
            if row['primary_file_id'] is not None and row['primary_file_id'] != fid:
                continue
            stem = None
            found = _sqlite_try(conn.cursor(),
                                'SELECT derivative_stem FROM import_items '
                                'WHERE file_id = ? ORDER BY id DESC LIMIT 1', (fid,))
            if found:
                stem = found[0]['derivative_stem']
            from workflows.lib.orientation import user_orientation
            edit = user_orientation(conn, fid)
        finally:
            conn.close()

        target_path = _resolve_path(row['file_path'], root)
        if not os.path.exists(target_path):
            print(f'[restore] original missing, no derivatives made: {target_path}')
            continue
        ts = None
        if row['master_datetime']:
            try:
                ts = _datetime.fromisoformat(row['master_datetime']).strftime('%Y%m%d%H%M%S')
            except ValueError:
                pass
        task = {'id': fid, 'file_id': fid, 'target_path': target_path,
                'target_dir': catalog.target_directory, 'creation_ts': ts,
                'derivative_stem': stem, 'existing': {}, 'user_orientation': edit,
                'rungs': list(catalog.image_rungs) if getattr(catalog, 'image_rungs', None) else None}
        render = module.render_video_task if is_video_file(row['file_name']) \
            else module.render_image_task
        result = render(task)
        for line in result['logs']:
            print(f'[restore] {line}')
        if result['rows']:
            conn = catalog_db.get_connection()
            try:
                conn.executemany(
                    '''INSERT INTO alternative_versions
                       (original_file_id, alternative_file_path, alternative_file_name,
                        alternative_file_size, characteristic, width, height, rung)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', result['rows'])
                conn.commit()
            finally:
                conn.close()
        if not result['ok']:
            print(f"[restore] render failed for file {fid}: {result['error']}")


@app.route('/api/catalogs/<int:catalog_id>/files/<int:file_id>/restore', methods=['POST'])
def restore_catalog_file(catalog_id, file_id):
    """
    Undo a removal: the file lists again and its derivatives are re-rendered in
    the background. Event and album memberships are not restored - the next
    event extraction places it, and albums are the user's to edit.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    conn = CatalogDatabase(catalog.target_directory).get_connection()
    cursor = conn.cursor()
    try:
        ids = _image_file_ids(cursor, file_id)
        if not ids:
            return jsonify({'error': 'File not found'}), 404
        marks = _in_clause(ids)
        rows = [dict(r) for r in cursor.execute(
            f'SELECT id, file_path FROM file_contents WHERE id IN ({marks}) '
            f'AND removed_at IS NOT NULL', ids)]
        if not rows:
            return jsonify({'error': 'Not removed'}), 409
        root = _original_root(catalog)
        if not any(os.path.exists(_resolve_path(r['file_path'], root)) for r in rows):
            return jsonify({'error': 'The original is no longer on disk; it can only be purged'}), 409
        cursor.execute(
            f'UPDATE file_contents SET removed_at = NULL WHERE id IN ({marks})', ids)
        conn.commit()
    finally:
        conn.close()

    threading.Thread(target=_restore_derivatives, args=(catalog, ids),
                     name=f'restore-{catalog_id}-{file_id}', daemon=True).start()
    return jsonify({'restored_file_ids': ids, 'rendering': True})


def _purge_file_rows(cursor, ids):
    """Delete removed files' rows and everything that still points at them."""
    marks = _in_clause(ids)
    faces = [r['id'] for r in cursor.execute(
        f'SELECT id FROM face_detections WHERE file_id IN ({marks})', ids)]
    crop_paths = []
    if faces:
        fm = _in_clause(faces)
        for table in ('face_contents', 'ai_face_contents'):
            crop_paths += [r['file_path'] for r in _sqlite_try(
                cursor, f'SELECT file_path FROM {table} WHERE face_detection_id IN ({fm})', faces)]
            _sqlite_try(cursor, f'DELETE FROM {table} WHERE face_detection_id IN ({fm})', faces)
        for table in ('face_cluster_members', 'auto_tag_audit'):
            _sqlite_try(cursor, f'DELETE FROM {table} WHERE face_detection_id IN ({fm})', faces)
        _sqlite_try(cursor,
                    f'DELETE FROM face_similarity_scores WHERE query_face_detection_id IN ({fm}) '
                    f'OR reference_face_detection_id IN ({fm})', faces + faces)
        _sqlite_try(cursor,
                    f'DELETE FROM face_similarity_scores_audit '
                    f'WHERE source_face_detection_id IN ({fm})', faces)
        _sqlite_try(cursor,
                    f'UPDATE face_similarity_scores_audit SET reference_face_detection_id = NULL '
                    f'WHERE reference_face_detection_id IN ({fm})', faces)
        cursor.execute(f'DELETE FROM face_detections WHERE id IN ({fm})', faces)
    _sqlite_try(cursor,
                f'UPDATE face_similarity_scores_audit SET source_file_id = NULL '
                f'WHERE source_file_id IN ({marks})', ids)

    # Removal already cleared these; repeated in case of a row removed by an
    # older build or edited by hand.
    for sql in (f'DELETE FROM alternative_versions WHERE original_file_id IN ({marks})',
                f'DELETE FROM event_files WHERE file_id IN ({marks})',
                f'DELETE FROM section_photos WHERE file_id IN ({marks})',
                f'UPDATE events SET cover_file_id = NULL WHERE cover_file_id IN ({marks})',
                f'UPDATE album_sections SET cover_file_id = NULL WHERE cover_file_id IN ({marks})',
                f'UPDATE people SET profile_photo_file_id = NULL '
                f'WHERE profile_photo_file_id IN ({marks})',
                f'DELETE FROM exif_data WHERE file_id IN ({marks})',
                f'UPDATE file_operations SET file_id = NULL WHERE file_id IN ({marks})',
                # A checkpoint row still 'placed' would be rendered again by the
                # next import (phase_render) once the file row is gone.
                f'DELETE FROM import_items WHERE file_id IN ({marks})'):
        _sqlite_try(cursor, sql, ids)

    groups = [r['image_group_id'] for r in cursor.execute(
        f'SELECT DISTINCT image_group_id FROM file_contents '
        f'WHERE id IN ({marks}) AND image_group_id IS NOT NULL', ids)]
    cursor.execute(f'DELETE FROM file_contents WHERE id IN ({marks})', ids)
    for gid in groups:
        left = cursor.execute('SELECT id FROM file_contents WHERE image_group_id = ?',
                              (gid,)).fetchall()
        if len(left) < 2:
            # A group of one is not a group.
            cursor.execute('UPDATE file_contents SET image_group_id = NULL '
                           'WHERE image_group_id = ?', (gid,))
            _sqlite_try(cursor, 'DELETE FROM image_groups WHERE id = ?', (gid,))
        else:
            _sqlite_try(cursor,
                        'UPDATE image_groups SET primary_file_id = ? WHERE id = ? '
                        'AND primary_file_id NOT IN (SELECT id FROM file_contents)',
                        (left[0]['id'], gid))
    return crop_paths


@app.route('/api/catalogs/<int:catalog_id>/removed-files/purge', methods=['POST'])
def purge_removed_files(catalog_id):
    """
    Delete removed files from the catalog completely, once their originals are
    gone from disk.

    Body: {"file_ids": [...]} to purge those, or {} for every removed file that
    is no longer on disk. A file whose original is still present is never
    purged: that is the state the Removed files page exists to point out, and
    purging it would let the next import bring it back.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    body = request.get_json(silent=True) or {}
    wanted = body.get('file_ids')
    if wanted is not None and (not isinstance(wanted, list)
                               or not all(isinstance(i, int) for i in wanted)):
        return jsonify({'error': 'file_ids must be a list of integers'}), 400

    conn = CatalogDatabase(catalog.target_directory).get_connection()
    cursor = conn.cursor()
    root = _original_root(catalog)
    try:
        sql = 'SELECT id, file_name, file_path FROM file_contents WHERE removed_at IS NOT NULL'
        params = []
        if wanted:
            sql += f' AND id IN ({_in_clause(wanted)})'
            params = wanted
        candidates = [dict(r) for r in cursor.execute(sql, params)]
        purge, still_on_disk = [], []
        for c in candidates:
            (still_on_disk if os.path.exists(_resolve_path(c['file_path'], root))
             else purge).append(c)
        crop_paths = []
        if purge:
            crop_paths = _purge_file_rows(cursor, [c['id'] for c in purge])
        conn.commit()
    finally:
        conn.close()

    for rel in crop_paths:
        try:
            p = _resolve_path(rel, catalog.target_directory)
            if rel and os.path.exists(p):
                os.remove(p)
        except OSError as exc:
            print(f'[purge] could not delete face crop {rel}: {exc}')

    return jsonify({
        'purged': [{'id': c['id'], 'file_name': c['file_name']} for c in purge],
        'still_on_disk': [{'id': c['id'], 'file_name': c['file_name']} for c in still_on_disk],
    })


@app.route('/api/catalogs/files', methods=['GET'])
def serve_catalog_file():
    """Serve a catalog file (image) from its stored path with proper EXIF orientation"""
    import os
    import io
    from flask import send_file, request
    from PIL import Image, ImageOps
    
    # Get the file path from query parameter
    file_path = request.args.get('path')
    if not file_path:
        return jsonify({'error': 'File path is required'}), 400
    
    # Resolve against the catalogs this user may read, and contain the result
    # with realpath. Before this the endpoint accepted any absolute path and
    # searched every catalog for a relative one (spec 6.3).
    resolved, error = _resolve_within_catalogs(file_path)
    if error is not None:
        return error
    file_path = resolved

    # Re-encoding below costs real CPU per request, so answer conditional
    # requests before any of it happens. The validator tracks file content
    # (mtime + size) rather than the URL because profile photos are rewritten
    # in place at a stable path when the user picks a new one.
    etag = _image_etag(file_path)
    not_modified = _image_not_modified(etag, revalidate=True)
    if not_modified is not None:
        return not_modified

    def _cached(response):
        return _image_cached(response, etag, revalidate=True)

    # Check if it's an image file that might have EXIF orientation
    image_extensions = {'.jpg', '.jpeg', '.png', '.tiff', '.tif', '.webp'}
    file_ext = os.path.splitext(file_path)[1].lower()
    
    # For non-image files or files that don't typically have EXIF, serve directly
    if file_ext not in image_extensions:
        return _cached(send_file(file_path))
    
    try:
        # Open the image and apply EXIF orientation
        with Image.open(file_path) as img:
            # Use ImageOps.exif_transpose to automatically handle EXIF orientation
            # This rotates/flips the image based on EXIF Orientation tag
            img = ImageOps.exif_transpose(img)
            
            # If the image was already correctly oriented or has no EXIF orientation,
            # exif_transpose returns the original image
            if img is None:
                return _cached(send_file(file_path))
            
            # Convert to RGB if necessary (for PNG with transparency, etc.)
            if img.mode in ('RGBA', 'LA', 'P'):
                # Create a white background for transparency
                background = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'P':
                    img = img.convert('RGBA')
                if img.mode in ('RGBA', 'LA'):
                    background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                    img = background
            elif img.mode != 'RGB':
                img = img.convert('RGB')
            
            # Save to a BytesIO object
            img_io = io.BytesIO()
            img.save(img_io, 'JPEG', quality=95)
            img_io.seek(0)
            
            # Determine the mimetype
            mimetype = 'image/jpeg'
            
            return _cached(send_file(img_io, mimetype=mimetype))
    except Exception as e:
        # If there's any error processing the image, fall back to serving it directly
        print(f"Error processing image orientation for {file_path}: {e}")
        return _cached(send_file(file_path))

@app.route('/api/catalogs/<int:catalog_id>/files/<int:file_id>/description', methods=['PUT'])
def update_file_description(catalog_id, file_id):
    """Update the description for a file"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    data = request.json
    if not data or 'description' not in data:
        return jsonify({'error': 'Description is required'}), 400
    
    description = data['description']
    
    try:
        # Use catalog-specific database to update the description
        catalog_db = CatalogDatabase(catalog.target_directory)
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE file_contents
            SET description = ?
            WHERE id = ?
        ''', (description, file_id))
        conn.commit()
        conn.close()
        
        return jsonify({'message': 'Description updated successfully', 'description': description}), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ─── Basic photo editing ──────────────────────────────────────────────────────
# documentation/specs/basic-photo-editing.md. A turn or flip is kept in
# photo_edits and applied when derivatives are made; the original is never
# written. Saving moves the face and pet boxes at once and rebuilds every
# derivative and crop on a thread, in place, so every URL keeps working.

def _turn_original(img, path, target_dir, file_id):
    """
    The Basic Editor's turn or flip on an image opened from the ORIGINAL, after
    its EXIF orientation (spec 5.1). Derivatives under <catalog>/cache already
    carry the edit in their pixels, so those come back unchanged.
    """
    if img is None or file_id is None or not path or not target_dir:
        return img
    cache = os.path.join(os.path.realpath(target_dir), 'cache') + os.sep
    if os.path.realpath(path).startswith(cache):
        return img
    from workflows.lib.orientation import apply_user, user_orientation
    try:
        conn = sqlite3.connect(os.path.join(target_dir, 'workflow_results.db'), timeout=10)
        try:
            value = user_orientation(conn, file_id)
        finally:
            conn.close()
    except sqlite3.Error:
        return img
    return apply_user(img, value)


def _photo_edit_payload(conn, file_id):
    import photo_edits
    edit = photo_edits.get(conn, file_id)
    edit['updated_by_name'] = _owner_names([edit['updated_by']]).get(edit['updated_by'])
    return edit


def _published_albums_with(conn, file_ids):
    """Albums in an enabled deployment that hold any of these files."""
    marks = _in_clause(file_ids)
    try:
        return [dict(r) for r in conn.execute(
            f'SELECT DISTINCT a.id, a.name FROM albums a '
            f'JOIN album_sections s ON s.album_id = a.id '
            f'JOIN section_photos sp ON sp.section_id = s.id '
            f'JOIN album_deployment_albums da ON da.album_id = a.id '
            f'JOIN album_deployments d ON d.id = da.deployment_id AND d.enabled = 1 '
            f'WHERE sp.file_id IN ({marks}) ORDER BY a.name', list(file_ids))]
    except sqlite3.Error:
        return []


def _rebuild_named_face_crops(conn, target_dir, file_id):
    """
    face_contents crops (the references recognition matches against) cut again
    from the new base rung, at their own paths, the way
    _promote_confirmed_face_to_reference cuts them. Their embeddings are
    cleared; the recognition workflow backfills a missing one.
    """
    from PIL import Image
    base = conn.execute("SELECT alternative_file_path FROM alternative_versions "
                        "WHERE original_file_id = ? AND rung = '1200' ORDER BY id LIMIT 1",
                        (file_id,)).fetchone()
    if not base:
        return 0
    rows = conn.execute(
        'SELECT fcn.id, fcn.file_path, fd.x, fd.y, fd.w, fd.h, fd.image_width, fd.image_height '
        'FROM face_contents fcn JOIN face_detections fd ON fd.id = fcn.face_detection_id '
        'WHERE fd.file_id = ?', (file_id,)).fetchall()
    if not rows:
        return 0
    done = 0
    with Image.open(_resolve_path(base['alternative_file_path'], target_dir)) as img:
        img = img.convert('RGB')
        for r in rows:
            sx = img.width / float(r['image_width'] or img.width)
            sy = img.height / float(r['image_height'] or img.height)
            box = (max(0, int(r['x'] * sx)), max(0, int(r['y'] * sy)),
                   min(img.width, int((r['x'] + r['w']) * sx)),
                   min(img.height, int((r['y'] + r['h']) * sy)))
            if box[2] <= box[0] or box[3] <= box[1]:
                continue
            path = _resolve_path(r['file_path'], target_dir)
            part = path + '.tmp'
            img.crop(box).resize((300, 300), Image.Resampling.LANCZOS).save(
                part, 'JPEG', quality=95, optimize=True)
            os.replace(part, path)
            with open(path, 'rb') as f:
                sha = hashlib.sha256(f.read()).hexdigest()
            conn.execute('UPDATE face_contents SET file_size = ?, sha256_hash = ?, '
                         'embedding = NULL WHERE id = ?',
                         (os.path.getsize(path), sha, r['id']))
            done += 1
    conn.commit()
    return done


def _photo_edit_rebuild(catalog, file_id):
    """Every derivative and crop of one photo, rebuilt with its edit (spec 5.3)."""
    import glob as _glob
    import photo_edits
    from workflows.lib.orientation import user_orientation

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT file_path, master_datetime FROM file_contents WHERE id = ?',
                           (file_id,)).fetchone()
        if not row:
            raise RuntimeError('the file is no longer in the catalog')
        original = _resolve_path(row['file_path'], _original_root(catalog))
        if not os.path.exists(original):
            raise RuntimeError('the original is not on disk, so it cannot be rendered')
        stem = None
        found = _sqlite_try(conn.cursor(), 'SELECT derivative_stem FROM import_items '
                                           'WHERE file_id = ? ORDER BY id DESC LIMIT 1', (file_id,))
        if found:
            stem = found[0]['derivative_stem']
        if not stem:
            # The name the existing 1200 rung was written under, so its
            # thumbnails keep their names too.
            alt = conn.execute("SELECT alternative_file_name FROM alternative_versions "
                               "WHERE original_file_id = ? AND rung = '1200' LIMIT 1",
                               (file_id,)).fetchone()
            if alt and alt['alternative_file_name'].endswith('_alt.jpg'):
                stem = alt['alternative_file_name'][:-len('_alt.jpg')]
        ts = None
        if row['master_datetime']:
            try:
                ts = _datetime.fromisoformat(row['master_datetime']).strftime('%Y%m%d%H%M%S')
            except ValueError:
                pass
        rungs = list(catalog.image_rungs) if getattr(catalog, 'image_rungs', None) else None
        value = user_orientation(conn, file_id)

        for line in photo_edits.rerender_derivatives(
                conn, catalog.target_directory, file_id, original, ts, stem, rungs, value):
            print(f'[photo-edit] {line}')
        photo_edits.rescale_boxes(conn, file_id)
        crops = photo_edits.rebuild_crops(conn, catalog.target_directory, file_id)
        named = _rebuild_named_face_crops(conn, catalog.target_directory, file_id)

        # Avatars are cached per (person, photo, detection); built again on
        # their next request. Profile photos are rebuilt now.
        for path in _glob.glob(os.path.join(catalog.target_directory, 'cache', 'avatars',
                                            f'person_*_{file_id}_*.jpg')):
            try:
                os.remove(path)
            except OSError:
                pass
        people = [r['id'] for r in _sqlite_try(
            conn.cursor(), 'SELECT id FROM people WHERE profile_photo_file_id = ?', (file_id,))]
        photo_edits.rescan(conn, file_id)
        photo_edits.finish(conn, file_id)
        conn.close()
        conn = None
        for person_id in people:
            set_profile_photo_internal(catalog, catalog_db, person_id, file_id)
        print(f'[photo-edit] file {file_id}: orientation {value}, '
              f'{crops["faces"]} face and {crops["pets"]} pet crop(s), '
              f'{named} named face crop(s), {len(people)} profile photo(s)')
    except Exception as exc:
        print(f'[photo-edit] file {file_id}: rebuild failed: {exc}')
        if conn is None:
            conn = catalog_db.get_connection()
        photo_edits.finish(conn, file_id, error=exc)
    finally:
        if conn is not None:
            conn.close()


def _start_photo_edit_rebuild(catalog, file_id):
    t = threading.Thread(target=_photo_edit_rebuild, args=(catalog, file_id),
                         name=f'photo-edit-{catalog.id}-{file_id}', daemon=True)
    t.start()
    return t


def _photo_edit_catalog(catalog_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return None, (jsonify({'error': 'Catalog not found'}), 404)
    if not catalog.target_directory:
        return None, (jsonify({'error': 'Catalog target directory not set'}), 400)
    return catalog, None


@app.route('/api/catalogs/<int:catalog_id>/files/<int:file_id>/edits', methods=['GET'])
def get_photo_edit(catalog_id, file_id):
    catalog, err = _photo_edit_catalog(catalog_id)
    if err:
        return err
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        if not conn.execute('SELECT 1 FROM file_contents WHERE id = ?', (file_id,)).fetchone():
            return jsonify({'error': 'File not found'}), 404
        return jsonify(_photo_edit_payload(conn, _derivative_owner(conn, file_id)))
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/files/<int:file_id>/edits', methods=['PUT', 'DELETE'])
def save_photo_edit(catalog_id, file_id):
    """
    Body {orientation: 1-8, base_version}. DELETE is orientation 1 (reset).
    202 while the derivatives are rebuilt; poll GET until render_status is
    'ready' or 'failed'.
    """
    import photo_edits
    from workflows.lib import orientation as orient
    catalog, err = _photo_edit_catalog(catalog_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if request.method == 'DELETE':
        value = 1
    else:
        value = data.get('orientation')
        if not orient.valid(value):
            return jsonify({'error': 'orientation must be a whole number from 1 to 8',
                            'code': 'bad_orientation'}), 400

    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        row = conn.execute('SELECT id, file_name, removed_at FROM file_contents WHERE id = ?',
                           (file_id,)).fetchone()
        if not row:
            return jsonify({'error': 'File not found', 'code': 'not_found'}), 404
        if row['removed_at']:
            return jsonify({'error': 'This photo was removed from the catalog.',
                            'code': 'removed'}), 409
        if is_video_file(row['file_name']):
            return jsonify({'error': 'Only photos can be edited.', 'code': 'not_a_photo'}), 415
        owner = _derivative_owner(conn, file_id)
        try:
            changed, _edit = photo_edits.save(conn, owner, value, data.get('base_version'),
                                              _current_user_id())
        except photo_edits.EditError as e:
            return jsonify({'error': e.message, 'code': e.code}), e.status
        payload = _photo_edit_payload(conn, owner)
        payload['file_id'] = owner
        payload['redirected_from'] = file_id if owner != file_id else None
        payload['published_albums'] = _published_albums_with(conn, sorted({file_id, owner}))
    finally:
        conn.close()
    if not changed:
        return jsonify(payload), 200
    _start_photo_edit_rebuild(catalog, owner)
    return jsonify(payload), 202


@app.route('/api/catalogs/<int:catalog_id>/edits/versions', methods=['GET'])
def get_photo_edit_versions(catalog_id):
    """{file_id: version} for every edited photo - what thumbnail URLs carry."""
    import photo_edits
    catalog, err = _photo_edit_catalog(catalog_id)
    if err:
        return err
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        return jsonify({'versions': {str(k): v for k, v in photo_edits.versions(conn).items()}})
    finally:
        conn.close()


def _edit_versions(catalog_id):
    """{file_id: version} for one catalog, read once per request."""
    import photo_edits
    from flask import g, has_request_context
    memo = g.setdefault('_edit_versions', {}) if has_request_context() else {}
    if catalog_id not in memo:
        catalog = Catalog.get_by_id(catalog_id)
        versions = {}
        if catalog and catalog.target_directory:
            conn = CatalogDatabase(catalog.target_directory).get_connection()
            try:
                versions = photo_edits.versions(conn)
            finally:
                conn.close()
        memo[catalog_id] = versions
    return memo[catalog_id]


def _thumb_url(catalog_id, file_id, size=320):
    """
    A photo's thumbnail URL, carrying its edit version: the route caches a
    versioned URL for a year, so a new edit has to be a new URL (spec 7.2).
    """
    v = _edit_versions(catalog_id).get(int(file_id), 0) if file_id is not None else 0
    return f'/api/catalogs/{catalog_id}/photos/{file_id}/thumbnail?size={size}&v={v}'


def _pet_crop_url(catalog_id, detection_id, file_id):
    v = _edit_versions(catalog_id).get(int(file_id), 0) if file_id is not None else 0
    return f'/api/catalogs/{catalog_id}/pet-detections/{detection_id}/crop?v={v}'


def _derived_image_response(path):
    """
    A thumbnail or crop rewritten in place when a photo is edited. With ?v=
    the URL names one version, cached for a year; without it, revalidated on
    every use so it can never be stale (spec 7.2).
    """
    from flask import send_file
    if request.args.get('v') is not None:
        resp = send_file(path, mimetype='image/jpeg')
        resp.headers['Cache-Control'] = 'public, max-age=31536000, immutable'
        return resp
    etag = _image_etag(path)
    not_modified = _image_not_modified(etag, revalidate=True)
    if not_modified is not None:
        return not_modified
    return _image_cached(send_file(path, mimetype='image/jpeg'), etag, revalidate=True)


def _resume_photo_edits():
    """Rebuilds a restart interrupted, started again (spec 5.3)."""
    import photo_edits
    try:
        catalogs = Catalog.get_all()
    except Exception:
        return
    for catalog in catalogs:
        if not getattr(catalog, 'target_directory', None) or \
                not os.path.exists(os.path.join(catalog.target_directory, 'workflow_results.db')):
            continue
        try:
            conn = CatalogDatabase(catalog.target_directory).get_connection()
            try:
                ids = photo_edits.interrupted(conn)
            finally:
                conn.close()
            for fid in ids:
                print(f'[photo-edit] resuming the rebuild of file {fid} in {catalog.name}')
                _photo_edit_rebuild(catalog, fid)
        except Exception as exc:
            print(f'[photo-edit] could not resume rebuilds in {catalog.name}: {exc}')


@app.route('/api/catalogs/<int:catalog_id>/images/years-months', methods=['GET'])
def get_catalog_images_years_months(catalog_id):
    """Get available years, months, and days for images in a catalog"""
    try:
        catalog = Catalog.get_by_id(catalog_id)
        if not catalog:
            return jsonify({'error': 'Catalog not found'}), 404

        if not catalog.target_directory:
            return jsonify({'years_months': {}})

        catalog_db = CatalogDatabase(catalog.target_directory)
        conn = catalog_db.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT DISTINCT
                CAST(SUBSTR(fc.master_datetime, 1, 4) AS INTEGER) AS year,
                CAST(SUBSTR(fc.master_datetime, 6, 2) AS INTEGER) AS month,
                CAST(SUBSTR(fc.master_datetime, 9, 2) AS INTEGER) AS day
            FROM file_contents fc
            WHERE LOWER(fc.file_type) IN ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp', '.heic', '.heif', '.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng')
              AND fc.removed_at IS NULL
              AND year > 0
              AND month > 0
              AND day > 0
            ORDER BY year DESC, month ASC, day ASC
        ''')
        rows = cursor.fetchall()
        conn.close()

        # Build a nested dict: { year: { month: [day, ...], ... }, ... }
        # Also keep backward-compatible years_months: { year: [month, ...] }
        years_months = {}
        years_months_days = {}
        for row in rows:
            y = row['year']
            m = row['month']
            d = row['day']
            if y and m:
                if y not in years_months:
                    years_months[y] = []
                if m not in years_months[y]:
                    years_months[y].append(m)
                if y not in years_months_days:
                    years_months_days[y] = {}
                if m not in years_months_days[y]:
                    years_months_days[y][m] = []
                if d and d not in years_months_days[y][m]:
                    years_months_days[y][m].append(d)

        # Sort months within each year, and days within each month
        for y in years_months:
            years_months[y] = sorted(years_months[y])
        for y in years_months_days:
            for m in years_months_days[y]:
                years_months_days[y][m] = sorted(years_months_days[y][m])

        return jsonify({'years_months': years_months, 'years_months_days': years_months_days})
    except Exception as e:
        return jsonify({'error': f'Error retrieving years/months: {str(e)}'}), 500


@app.route('/api/catalogs/<int:catalog_id>/images', methods=['GET'])
def get_catalog_images(catalog_id):
    """Get all image files from a catalog for photo tagging"""
    try:
        # Use Catalog class to get catalog
        catalog = Catalog.get_by_id(catalog_id)
        if not catalog:
            return jsonify({'error': 'Catalog not found'}), 404
        
        # Check if target_directory is set
        if not catalog.target_directory:
            return jsonify({'images': []})

        # Optional year/month/day filters
        year = request.args.get('year', type=int)
        month = request.args.get('month', type=int)
        day = request.args.get('day', type=int)
        
        # Get files from catalog-specific database
        catalog_db = CatalogDatabase(catalog.target_directory)
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        
        # Build WHERE clause for year/month/day filtering
        image_types = "'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp', '.heic', '.heif', '.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng'"
        where_clause = f"WHERE LOWER(fc.file_type) IN ({image_types}) AND fc.removed_at IS NULL"
        params = []

        if year:
            where_clause += " AND CAST(SUBSTR(fc.master_datetime, 1, 4) AS INTEGER) = ?"
            params.append(year)
        if month:
            where_clause += " AND CAST(SUBSTR(fc.master_datetime, 6, 2) AS INTEGER) = ?"
            params.append(month)
        if day:
            where_clause += " AND CAST(SUBSTR(fc.master_datetime, 9, 2) AS INTEGER) = ?"
            params.append(day)

        # Get only image files (common image extensions)
        # Note: file_type is stored with leading dot (e.g., '.jpg', '.jpeg')
        cursor.execute(f'''
            SELECT
                fc.id,
                fc.file_path,
                fc.file_name,
                fc.file_size,
                fc.file_type,
                fc.added_at,
                ed.creation_date,
                ed.width,
                ed.height
            FROM file_contents fc
            LEFT JOIN exif_data ed ON fc.id = ed.file_id
            {where_clause}
            ORDER BY fc.master_datetime ASC
        ''', params)
        rows = cursor.fetchall()
        
        # Convert rows to dictionaries and add alternative_file_path
        images = []
        for row in rows:
            image_dict = dict(row)
            file_id = image_dict['id']
            
            # The display derivative, ranked by measured pixels (spec 6.1)
            cursor.execute('''
                SELECT alternative_file_path, alternative_file_name, rung, characteristic
                FROM alternative_versions
                WHERE original_file_id = ?
                  AND rung IN ('1200','1600','2048','2560')
                ORDER BY (width >= 1200) DESC,
                         CASE WHEN width >= 1200 THEN width END ASC,
                         width DESC, id ASC
                LIMIT 1
            ''', (file_id,))
            alt_row = cursor.fetchone()
            
            if alt_row:
                # Resolve stored path (may be relative) to absolute
                image_dict['alternative_file_path'] = _resolve_path(
                    alt_row['alternative_file_path'], catalog.target_directory
                )
                image_dict['alternative_file_name'] = alt_row['alternative_file_name']
                image_dict['alternative_characteristic'] = alt_row['characteristic']
            else:
                image_dict['alternative_file_path'] = None
                image_dict['alternative_file_name'] = None
                image_dict['alternative_characteristic'] = None
            
            images.append(image_dict)
        
        conn.close()
        
        return jsonify({'images': images})
    except Exception as e:
        return jsonify({'error': f'Error retrieving images: {str(e)}'}), 500


@app.route('/api/catalogs/<int:catalog_id>/images/<int:file_id>/alternative-jpg', methods=['GET'])
def get_alternative_jpg(catalog_id, file_id):
    """Get the alternative JPG version for a HEIC/CR3 file, if available"""
    try:
        catalog = Catalog.get_by_id(catalog_id)
        if not catalog:
            return jsonify({'error': 'Catalog not found'}), 404

        if not catalog.target_directory:
            return jsonify({'alternative': None})

        catalog_db = CatalogDatabase(catalog.target_directory)
        conn = catalog_db.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            SELECT id, alternative_file_path, alternative_file_name, alternative_file_size, characteristic
            FROM alternative_versions
            WHERE original_file_id = ? AND UPPER(characteristic) = 'TYPE JPG'
            LIMIT 1
        ''', (file_id,))
        row = cursor.fetchone()
        conn.close()

        if row:
            return jsonify({'alternative': dict(row)})
        else:
            return jsonify({'alternative': None})
    except Exception as e:
        return jsonify({'error': f'Error retrieving alternative version: {str(e)}'}), 500


# Face Detection API endpoints
@app.route('/api/catalogs/<int:catalog_id>/images/<int:file_id>/detect-faces', methods=['POST'])
def detect_faces_in_image(catalog_id, file_id):
    """Detect faces in an image using the deepface backend"""
    import requests
    import shutil
    from datetime import datetime
    
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    # Get the file from catalog database
    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT file_path, file_name, file_type FROM file_contents '
                   'WHERE id = ? AND removed_at IS NULL', (file_id,))
    row = cursor.fetchone()
    conn.close()
    
    if not row:
        return jsonify({'error': 'File not found'}), 404
    
    file_path = row['file_path']
    file_name = row['file_name']
    file_type = (row['file_type'] or '').lower()
    
    # Always use the alternative JPG version for face detection (standardized at max 1200px)
    # This ensures consistent face detection across all image types and sizes
    conn2 = catalog_db.get_connection()
    cursor2 = conn2.cursor()
    # THE BASE RUNG, PINNED. Detection coordinates are stored in this image's
    # pixel space; a larger rung here would offset every box (spec 6.2).
    cursor2.execute('''
        SELECT alternative_file_path, alternative_file_name, rung, characteristic
        FROM alternative_versions
        WHERE original_file_id = ?
          AND rung IN ('1200','1600','2048','2560')
        ORDER BY (rung = '1200') DESC, width ASC, id ASC
        LIMIT 1
    ''', (file_id,))
    alt_row = cursor2.fetchone()
    conn2.close()
    
    # Use alternative version if available, otherwise fall back to original
    # Resolve stored paths (relative in DB) to absolute
    file_path = _resolve_path(file_path, _original_root(catalog))
    if alt_row:
        alt_abs = _resolve_path(alt_row['alternative_file_path'], catalog.target_directory)
        if os.path.exists(alt_abs):
            file_path = alt_abs
            file_name = alt_row['alternative_file_name']
            print(f"Using alternative version for face detection: {file_name}")
        else:
            # For non-displayable types (HEIC, CR3), we require an alternative version
            non_displayable_types = {'.heic', '.heif', '.cr3'}
            if file_type in non_displayable_types:
                return jsonify({'error': 'This image format cannot be processed for face detection and no JPG alternative was found. Please ensure alternative versions are created during the initial workflow.'}), 400
            print(f"No alternative version found, using original: {file_name}")
    else:
        non_displayable_types = {'.heic', '.heif', '.cr3'}
        if file_type in non_displayable_types:
            return jsonify({'error': 'This image format cannot be processed for face detection and no JPG alternative was found. Please ensure alternative versions are created during the initial workflow.'}), 400
        print(f"No alternative version found, using original: {file_name}")
    
    if not os.path.exists(file_path):
        return jsonify({'error': 'File does not exist on disk'}), 404
    
    try:
        # Apply EXIF orientation before sending to deepface backend
        # This ensures face coordinates match the displayed (oriented) image
        from PIL import Image, ImageOps
        import io
        
        # Open image and apply orientation
        detection_width = None
        detection_height = None
        with Image.open(file_path) as img:
            img = ImageOps.exif_transpose(img)
            img = _turn_original(img, file_path, catalog.target_directory, file_id)
            if img is None:
                # No orientation correction needed, use original
                with open(file_path, 'rb') as f:
                    image_data = f.read()
                # Get dimensions from original image
                with Image.open(file_path) as orig_img:
                    detection_width, detection_height = orig_img.size
            else:
                # Save oriented image to bytes
                detection_width, detection_height = img.size
                img_io = io.BytesIO()
                if img.mode in ('RGBA', 'LA', 'P'):
                    background = Image.new('RGB', img.size, (255, 255, 255))
                    if img.mode == 'P':
                        img = img.convert('RGBA')
                    if img.mode in ('RGBA', 'LA'):
                        background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
                        img = background
                elif img.mode != 'RGB':
                    img = img.convert('RGB')
                img.save(img_io, 'JPEG', quality=95)
                image_data = img_io.getvalue()
        
        # Call deepface backend to detect faces
        deepface_base = os.environ.get('DEEPFACE_URL', 'http://deepface.airlock.lan')
        deepface_url = f'{deepface_base}/api/upload'
        
        files = {'file': (file_name, image_data, 'image/jpeg')}
        response = requests.post(deepface_url, files=files, timeout=30)
        
        if response.status_code != 200:
            return jsonify({'error': f'Deepface backend error: {response.text}'}), 500
        
        result = response.json()
        
        if not result.get('success'):
            return jsonify({'error': 'Face detection failed'}), 500
        
        # Store face detections in our database
        face_detection_model = FaceDetectionModel(catalog_db)
        
        # Get existing manual faces to preserve them
        # Preserve human-confirmed AND auto-confirmed faces: both carry a person_id,
        # so a fresh Automatic box over the top would duplicate the person.
        existing_manual_faces = face_detection_model.get_protected_faces_by_file(file_id)
        
        # Delete only automatic face detections for this file
        face_detection_model.delete_automatic_faces_by_file(file_id)
        
        # Helper function to check if two face regions overlap significantly
        def faces_overlap(face1, face2, threshold=0.5):
            """Check if two face bounding boxes overlap by more than threshold (IoU)"""
            x1_min, y1_min = face1['x'], face1['y']
            x1_max, y1_max = x1_min + face1['w'], y1_min + face1['h']
            x2_min, y2_min = face2['x'], face2['y']
            x2_max, y2_max = x2_min + face2['w'], y2_min + face2['h']
            
            # Calculate intersection
            inter_x_min = max(x1_min, x2_min)
            inter_y_min = max(y1_min, y2_min)
            inter_x_max = min(x1_max, x2_max)
            inter_y_max = min(y1_max, y2_max)
            
            if inter_x_max < inter_x_min or inter_y_max < inter_y_min:
                return False  # No overlap
            
            inter_area = (inter_x_max - inter_x_min) * (inter_y_max - inter_y_min)
            face1_area = face1['w'] * face1['h']
            face2_area = face2['w'] * face2['h']
            union_area = face1_area + face2_area - inter_area
            
            iou = inter_area / union_area if union_area > 0 else 0
            return iou > threshold
        
        # Add new face detections, but skip if they overlap with existing manual faces
        faces = []
        for face in result.get('faces', []):
            # Check if this new face overlaps with any existing manual face
            overlaps_with_manual = any(
                faces_overlap(face, manual_face)
                for manual_face in existing_manual_faces
            )
            
            if not overlaps_with_manual:
                face_id = face_detection_model.add_face_detection(
                    file_id=file_id,
                    x=face['x'],
                    y=face['y'],
                    w=face['w'],
                    h=face['h'],
                    confidence=face.get('confidence', 0.0),
                    image_width=detection_width,
                    image_height=detection_height
                )
                faces.append({
                    'id': face_id,
                    'x': face['x'],
                    'y': face['y'],
                    'w': face['w'],
                    'h': face['h'],
                    'confidence': face.get('confidence', 0.0),
                    'person_id': None,
                    'person_name': None,
                    'source': 'Automatic',
                    'image_width': detection_width,
                    'image_height': detection_height
                })
        
        # Add existing manual faces to the response
        for manual_face in existing_manual_faces:
            faces.append({
                'id': manual_face['id'],
                'x': manual_face['x'],
                'y': manual_face['y'],
                'w': manual_face['w'],
                'h': manual_face['h'],
                'confidence': manual_face.get('confidence'),
                'person_id': manual_face.get('person_id'),
                'person_name': manual_face.get('person_name'),
                'source': 'Manual',
                'image_width': manual_face.get('image_width'),
                'image_height': manual_face.get('image_height')
            })
        
        return jsonify({
            'success': True,
            'file_id': file_id,
            'faces': faces,
            'total_faces': len(faces),
            'detection_width': detection_width,
            'detection_height': detection_height
        })
        
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Could not connect to deepface backend. Make sure it is running on port 5001.'}), 503
    except Exception as e:
        return jsonify({'error': f'Error detecting faces: {str(e)}'}), 500

@app.route('/api/catalogs/<int:catalog_id>/images/<int:file_id>/faces', methods=['GET'])
def get_image_faces(catalog_id, file_id):
    """Get all face detections for an image"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    face_detection_model = FaceDetectionModel(catalog_db)
    
    faces = face_detection_model.get_faces_by_file(file_id)
    return jsonify({'faces': faces})

def _promote_confirmed_face_to_reference(catalog_db_path: str, target_dir: str,
                                          face_id: int, person_id: int) -> None:
    """
    When an Automatic face detection is confirmed (tagged with a person), promote
    it so the face recognition workflow can use it as a reference:

      1. Change face_detections.source from 'Automatic' to 'Manual'.
      2. Extract a 300x300 face crop from the original image and save it to
         <target_dir>/tmp/faces/, then register it in face_contents.

    Idempotent: skipped if face_contents already has a row for this detection.
    Errors are logged but never re-raised (tagging must not fail because of this).
    """
    try:
        from PIL import Image, ImageOps

        conn = sqlite3.connect(catalog_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        # ── 1. Fetch everything we need in one query ──────────────────────────
        cursor.execute('''
            SELECT
                fd.id, fd.x, fd.y, fd.w, fd.h,
                fd.image_width AS detection_image_width,
                fd.image_height AS detection_image_height,
                fc.file_path AS original_file_path,
                fc.id AS file_id,
                fc.master_datetime,
                p.name AS person_name,
                ed.creation_date,
                ed.width AS original_width,
                ed.height AS original_height,
                -- The base rung, pinned: this crops a face using stored
                -- coordinates, which are in the base image's space (spec 6.2).
                (SELECT av.alternative_file_path
                   FROM alternative_versions av
                  WHERE av.original_file_id = fc.id
                    AND av.rung IN ('1200','1600','2048','2560')
                  ORDER BY (av.rung = '1200') DESC, av.width ASC, av.id ASC
                  LIMIT 1) AS alt_file_path
            FROM face_detections fd
            INNER JOIN file_contents fc ON fd.file_id = fc.id
            INNER JOIN people p ON p.id = ?
            LEFT JOIN exif_data ed ON fc.exif_data_id = ed.id
            WHERE fd.id = ?
        ''', (person_id, face_id))
        row = cursor.fetchone()
        if not row:
            conn.close()
            print(f"[promote] face_id={face_id} not found — skipping")
            return
        face_data = dict(row)

        # ── 2. Skip if face_contents already populated ─────────────────────────
        cursor.execute('SELECT COUNT(*) FROM face_contents WHERE face_detection_id = ?',
                       (face_id,))
        if cursor.fetchone()[0] > 0:
            # Already promoted; just make sure source is 'Manual'
            cursor.execute("UPDATE face_detections SET source = 'Manual' WHERE id = ?",
                           (face_id,))
            conn.commit()
            conn.close()
            return

        # ── 3. Promote source to 'Manual' ─────────────────────────────────────
        cursor.execute("UPDATE face_detections SET source = 'Manual' WHERE id = ?",
                       (face_id,))
        conn.commit()
        conn.close()

        # ── 4. Pick an image Pillow can open ──────────────────────────────────
        # CR3 and HEIC are raw/HEIF containers that Pillow cannot decode, and
        # roughly half this library is CR3 — opening the original raised
        # UnidentifiedImageError for every one of them. Every such file has a
        # browser-friendly alternative version created at import, so that is what
        # gets opened. Its size need not match the one detection ran against
        # (1200x801 vs 1024x684 is typical here) — the scaling below maps the
        # stored coordinates onto whatever rendition is actually opened.
        non_displayable_types = {'.heic', '.heif', '.cr3'}

        def _abs(path):
            if path and not os.path.isabs(path):
                return os.path.join(target_dir, path)
            return path

        original_file_path = _abs(face_data['original_file_path'])
        alt_file_path = _abs(face_data['alt_file_path'])
        file_type = os.path.splitext(face_data['original_file_path'] or '')[1].lower()

        if file_type in non_displayable_types:
            candidates = [alt_file_path, original_file_path]
        else:
            candidates = [original_file_path, alt_file_path]

        source_path = next((c for c in candidates if c and os.path.exists(c)), None)
        if not source_path:
            print(f"[promote] No readable image for face_id={face_id} "
                  f"({face_data['original_file_path']}) — skipping")
            return
        if file_type in non_displayable_types and source_path == original_file_path:
            print(f"[promote] {file_type} has no alternative version, cannot crop: "
                  f"{original_file_path} — skipping")
            return

        face_x = face_data['x']
        face_y = face_data['y']
        face_w = face_data['w']
        face_h = face_data['h']
        detection_w = face_data['detection_image_width']
        detection_h = face_data['detection_image_height']

        # Open the chosen rendition with EXIF orientation correction. The
        # coordinate scaling below already handles the opened image differing in
        # size from the one detection ran on.
        with Image.open(source_path) as img:
            img = ImageOps.exif_transpose(img) or img
            img = _turn_original(img, source_path, target_dir, face_data['file_id'])
            if img.mode not in ('RGB', 'L'):
                bg = Image.new('RGB', img.size, (255, 255, 255))
                if img.mode == 'RGBA':
                    bg.paste(img, mask=img.split()[3])
                else:
                    bg.paste(img)
                img = bg
            else:
                img = img.copy()
        orig_w, orig_h = img.size

        # Scale coordinates if detection was done on a different-sized image
        if detection_w and detection_h and (detection_w != orig_w or detection_h != orig_h):
            scale_x = orig_w / detection_w
            scale_y = orig_h / detection_h
            face_x = int(face_x * scale_x)
            face_y = int(face_y * scale_y)
            face_w = int(face_w * scale_x)
            face_h = int(face_h * scale_y)

        # ── 5. Crop and resize to 300×300 ──────────────────────────────────────
        left   = max(0, face_x)
        top    = max(0, face_y)
        right  = min(orig_w, face_x + face_w)
        bottom = min(orig_h, face_y + face_h)

        if right <= left or bottom <= top:
            print(f"[promote] Invalid crop box for face_id={face_id}: "
                  f"({left},{top},{right},{bottom}) — skipping")
            return

        face_img = img.crop((left, top, right, bottom))
        face_img = face_img.resize((300, 300), Image.Resampling.LANCZOS)

        # ── 6. Save crop to cache/faces/ ─────────────────────────────────────
        faces_dir = os.path.join(target_dir, 'cache', 'faces')
        os.makedirs(faces_dir, exist_ok=True)

        timestamp = _datetime.now().strftime('%Y%m%d_%H%M%S_%f')
        safe_name = face_data['person_name'].replace(' ', '_').replace('/', '_')
        face_filename = f"{timestamp}_{safe_name}_face_{face_id}.jpg"
        face_path = os.path.join(faces_dir, face_filename)
        face_img.save(face_path, 'JPEG', quality=95, optimize=True)

        file_size = os.path.getsize(face_path)
        sha256 = hashlib.sha256(open(face_path, 'rb').read()).hexdigest()

        # Parse year/month/day from master_datetime (ISO 8601: YYYY-MM-DDTHH:MM:SS...)
        # Fall back to EXIF creation_date if master_datetime is not available.
        year = month = day = None
        master_dt = face_data.get('master_datetime')
        if master_dt:
            try:
                date_part = master_dt[:10]  # "YYYY-MM-DD"
                y, m, d = date_part.split('-')
                year, month, day = int(y), int(m), int(d)
            except (ValueError, IndexError):
                pass
        if year is None:
            creation_date = face_data.get('creation_date')
            if creation_date:
                try:
                    date_part = creation_date.split()[0]
                    y, m, d = date_part.split(':')
                    year, month, day = int(y), int(m), int(d)
                except (ValueError, IndexError):
                    pass

        # ── 7. Register in face_contents ──────────────────────────────────────
        # Carry over any embedding already stored in ai_face_contents so that
        # this newly confirmed face is immediately usable as a reference with
        # a pre-computed embedding, without needing a backfill run.
        conn2 = sqlite3.connect(catalog_db_path)
        ai_row = conn2.execute(
            'SELECT embedding FROM ai_face_contents WHERE face_detection_id = ?',
            (face_id,)
        ).fetchone()
        cached_embedding = ai_row[0] if ai_row else None

        # Store face_path relative to target_dir so the catalog is portable
        rel_face_path = os.path.relpath(face_path, target_dir)
        conn2.execute('''
            INSERT OR IGNORE INTO face_contents
            (file_path, file_name, file_size, sha256_hash, year, month, day,
             person_id, face_detection_id, source_file_id, embedding)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (rel_face_path, face_filename, file_size, sha256,
              year, month, day,
              person_id, face_id, face_data['file_id'], cached_embedding))
        conn2.commit()
        conn2.close()
        if cached_embedding:
            print(f"[promote] face_id={face_id} — embedding carried over from ai_face_contents")

        print(f"[promote] face_id={face_id} promoted to Manual reference for person_id={person_id}")

    except Exception as exc:
        import traceback
        print(f"[promote] Non-fatal error promoting face_id={face_id}: {exc}")
        traceback.print_exc()


def _fire_face_confirmed(catalog, face_count=1):
    """
    A human just gave one or more faces a name.

    Confirming a face makes it a reference the recognition workflows have never
    compared against, and until now nothing re-ran on its own - a user could tag
    twenty faces, see no change, and have no way to know a workflow run was the
    missing step. This closes that loop.

    Raised only from the HTTP request path. The contextual workflow also writes
    person_id (as source='Auto-Confirmed'), and if that could reach here the
    chain would re-enqueue the workflow that produced it, forever. It cannot:
    workflow scripts run as separate processes and never call this.
    """
    try:
        workflow_triggers.fire(catalog, 'face_confirmed',
                               detail={'faces': face_count})
        _emit_queue(catalog)
    except Exception as e:
        # Never fail a confirmation because scheduling failed.
        print(f'[queue] could not queue the face_confirmed chain: {e}')


def _assign_face_to_person(catalog, catalog_db, face_detection_model,
                           face_id, person_id, set_profile_if_missing=False):
    """
    Assign one face to a person, with everything that has to happen alongside it.

    Shared by the single-face tag endpoint and the cluster assign endpoint, so
    confirming twenty faces at once cannot drift from confirming one: the same
    promotion to a reference face, the same removal of now-meaningless candidate
    scores, the same profile-photo fallback.

    Returns False when the face does not exist.
    """
    if not face_detection_model.update_face_person(face_id, person_id):
        return False

    if person_id is None:
        return True

    # Promote this Automatic detection to a Manual reference so the recognition
    # workflow can use it for future comparisons.
    catalog_db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    _promote_confirmed_face_to_reference(
        catalog_db_path, catalog.target_directory, face_id, person_id
    )

    # Identity is now known - candidate similarity scores for this face are noise.
    conn = catalog_db.get_connection()
    conn.execute(
        'DELETE FROM face_similarity_scores WHERE query_face_detection_id = ?',
        (face_id,)
    )
    conn.commit()
    conn.close()

    if set_profile_if_missing:
        try:
            conn = catalog_db.get_connection()
            cursor = conn.cursor()
            cursor.execute('SELECT file_id FROM face_detections WHERE id = ?', (face_id,))
            face_row = cursor.fetchone()
            conn.close()
            if face_row:
                set_profile_photo_internal(catalog, catalog_db, person_id,
                                           face_row['file_id'])
        except Exception as e:
            # Never fail a confirmation because a profile photo could not be set.
            print(f"Warning: Failed to auto-set profile photo: {e}")

    return True


@app.route('/api/catalogs/<int:catalog_id>/faces/<int:face_id>/tag', methods=['PUT'])
def tag_face(catalog_id, face_id):
    """Tag a face with a person"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    data = request.json
    if data is None:
        return jsonify({'error': 'No data provided'}), 400
    
    person_id = data.get('person_id')
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    face_detection_model = FaceDetectionModel(catalog_db)
    
    # Verify person exists if person_id is provided and check if they need a profile photo
    has_no_profile_photo = False
    if person_id is not None:
        people_model = PeopleModel(catalog_db)
        person = people_model.get_person_by_id(person_id)
        if not person:
            return jsonify({'error': 'Person not found'}), 404
        
        # Check if this person has no profile photo yet
        has_no_profile_photo = (person.get('profile_photo_path') is None and
                                person.get('profile_photo_file_id') is None)
    
    if not _assign_face_to_person(catalog, catalog_db, face_detection_model,
                                  face_id, person_id,
                                  set_profile_if_missing=has_no_profile_photo):
        return jsonify({'error': 'Face not found'}), 404

    # Untagging (person_id None) removes a reference rather than adding one, so
    # there is nothing for the recognition chain to learn from.
    if person_id is not None:
        _fire_face_confirmed(catalog, 1)

    return jsonify({'success': True, 'message': 'Face tagged successfully'})

@app.route('/api/catalogs/<int:catalog_id>/faces/tag-bulk', methods=['POST'])
def tag_faces_bulk(catalog_id):
    """
    Assign many faces to one person in a single request.

    Body: {person_id, face_ids: [...]}

    The alternative - the browser looping over the single-face endpoint - means
    a hundred round trips, a hundred reference promotions racing each other, and
    a hundred face_confirmed triggers. This does the same work through the same
    _assign_face_to_person() path, then raises one trigger with the real count.

    Faces that already carry a person are skipped rather than reassigned: a
    bulk accept is a fast path for candidates, not a way to silently overwrite
    decisions already made.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.get_json(silent=True) or {}
    person_id = data.get('person_id')
    face_ids = data.get('face_ids') or []
    if person_id is None:
        return jsonify({'error': 'person_id is required'}), 400
    if not isinstance(face_ids, list) or not face_ids:
        return jsonify({'error': 'face_ids must be a non-empty list'}), 400
    try:
        face_ids = [int(f) for f in face_ids]
    except (TypeError, ValueError):
        return jsonify({'error': 'face_ids must be integers'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404

    needs_profile_photo = (person.get('profile_photo_path') is None and
                           person.get('profile_photo_file_id') is None)

    conn = catalog_db.get_connection()
    rows = conn.execute(
        'SELECT id, person_id FROM face_detections WHERE id IN (%s)'
        % ','.join('?' * len(face_ids)), face_ids).fetchall()
    conn.close()
    existing = {r['id']: r['person_id'] for r in rows}

    face_detection_model = FaceDetectionModel(catalog_db)
    assigned, skipped, failed = [], [], []

    for face_id in face_ids:
        if face_id not in existing:
            failed.append({'face_id': face_id, 'error': 'face not found'})
            continue
        if existing[face_id] is not None:
            skipped.append({'face_id': face_id, 'reason': 'already assigned'})
            continue
        try:
            ok = _assign_face_to_person(
                catalog, catalog_db, face_detection_model, face_id, person_id,
                # Only the first one can become the profile photo.
                set_profile_if_missing=(needs_profile_photo and not assigned),
            )
        except Exception as e:
            failed.append({'face_id': face_id, 'error': str(e)})
            continue
        if ok:
            assigned.append(face_id)
        else:
            failed.append({'face_id': face_id, 'error': 'face not found'})

    if assigned:
        _fire_face_confirmed(catalog, len(assigned))

    return jsonify({
        'success': True,
        'person_id': person_id,
        'person_name': person.get('name'),
        'assigned': len(assigned),
        'assigned_face_ids': assigned,
        'skipped': skipped,
        'failed': failed,
    })


@app.route('/api/catalogs/<int:catalog_id>/faces/<int:face_id>', methods=['DELETE'])
def delete_face(catalog_id, face_id):
    """Delete a face detection"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    face_detection_model = FaceDetectionModel(catalog_db)
    
    face_detection_model.delete_face_detection(face_id)
    
    return jsonify({'success': True, 'message': 'Face detection deleted'})


@app.route('/api/catalogs/<int:catalog_id>/faces/<int:face_id>/ai-crop', methods=['GET'])
def get_ai_face_crop(catalog_id, face_id):
    """
    Return the ai_face_contents record for a specific Automatic face detection,
    i.e. the pre-saved JPEG crop produced by auto_detect_faces_workflow.

    Response when crop exists:
        { "success": true, "face_detection_id": 42, "file_path": "/abs/.../42.jpg", ... }
    Response when no crop exists yet:
        { "success": true, "face_detection_id": 42, "file_path": null }
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, file_path, file_name, file_size, sha256_hash,
               year, month, day, face_detection_id, source_file_id, created_at
        FROM ai_face_contents
        WHERE face_detection_id = ?
    ''', (face_id,))
    row = cursor.fetchone()
    conn.close()

    if row:
        data = dict(row)
        # Resolve file_path (stored relative in DB) to absolute for the client
        data['file_path'] = _resolve_path(data.get('file_path'), catalog.target_directory)
        return jsonify({'success': True, **data})

    return jsonify({'success': True, 'face_detection_id': face_id, 'file_path': None})


@app.route('/api/catalogs/<int:catalog_id>/images/<int:file_id>/faces/manual', methods=['POST'])
def create_manual_face(catalog_id, file_id):
    """Create a manual face detection"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    data = request.json
    if data is None:
        return jsonify({'error': 'No data provided'}), 400
    
    # Validate required fields
    required_fields = ['x', 'y', 'w', 'h']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    person_id = data.get('person_id')
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    face_detection_model = FaceDetectionModel(catalog_db)
    
    # Verify person exists if person_id is provided
    if person_id is not None:
        people_model = PeopleModel(catalog_db)
        person = people_model.get_person_by_id(person_id)
        if not person:
            return jsonify({'error': 'Person not found'}), 404
    
    # Create face detection with source='Manual'
    # Get image dimensions if provided
    image_width = data.get('image_width')
    image_height = data.get('image_height')
    
    face_id = face_detection_model.add_face_detection(
        file_id=file_id,
        x=data['x'],
        y=data['y'],
        w=data['w'],
        h=data['h'],
        confidence=data.get('confidence'),
        person_id=person_id,
        source='Manual',
        image_width=image_width,
        image_height=image_height
    )
    # A box drawn and named in one go is a tagged face like any other: Learn
    # Tagged Faces reads exactly these rows (source 'Manual' with a person).
    if person_id is not None:
        _fire_face_confirmed(catalog, 1)

    return jsonify({
        'success': True,
        'face_id': face_id,
        'message': 'Manual face detection created successfully'
    }), 201

@app.route('/api/catalogs/<int:catalog_id>/faces/manual', methods=['GET'])
def get_all_manual_faces(catalog_id):
    """Get all manually tagged faces in the catalog with their file information"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    face_detection_model = FaceDetectionModel(catalog_db)
    
    # Get all manual faces with file information
    manual_faces = face_detection_model.get_all_manual_faces_with_files()
    
    return jsonify({
        'success': True,
        'total_faces': len(manual_faces),
        'faces': manual_faces
    })

@app.route('/api/catalogs/<int:catalog_id>/faces/manual/files', methods=['GET'])
def get_files_with_manual_faces(catalog_id):
    """Get all files that have manually tagged faces"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    face_detection_model = FaceDetectionModel(catalog_db)
    
    # Get all files with manual faces
    files = face_detection_model.get_files_with_manual_faces()
    
    return jsonify({
        'success': True,
        'total_files': len(files),
        'files': files
    })

@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/faces/manual', methods=['GET'])
def get_person_manual_faces(catalog_id, person_id):
    """Get all manually tagged faces for a specific person"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    face_detection_model = FaceDetectionModel(catalog_db)
    
    # Get manual faces for this person
    manual_faces = face_detection_model.get_manual_faces_by_person(person_id)
    
    return jsonify({
        'success': True,
        'person_id': person_id,
        'total_faces': len(manual_faces),
        'faces': manual_faces
    })

@app.route('/api/catalogs/<int:catalog_id>/faces/<int:face_id>/compare', methods=['POST'])
def compare_face_to_manual_faces(catalog_id, face_id):
    """
    Compare a specific face against all manually tagged faces in the catalog.
    
    Request body (optional):
    - person_id: Only compare against faces of a specific person
    - limit: Maximum number of results to return (default: 10)
    - year_filter: Only compare against faces from photos taken in the same year (default: false)
    """
    import requests
    from PIL import Image, ImageOps
    import io
    
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    data = request.json or {}
    person_id_filter = data.get('person_id')
    limit = data.get('limit', 10)
    year_filter = data.get('year_filter', False)
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    face_detection_model = FaceDetectionModel(catalog_db)
    
    # Get the face to compare (including its photo year)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT fd.*, fc.file_path, fc.file_name,
               fc.master_datetime,
               ed.creation_date,
               CAST(SUBSTR(fc.master_datetime, 1, 4) AS INTEGER) as photo_year
        FROM face_detections fd
        INNER JOIN file_contents fc ON fd.file_id = fc.id
        LEFT JOIN exif_data ed ON fc.exif_data_id = ed.id
        WHERE fd.id = ?
    ''', (face_id,))
    face_row = cursor.fetchone()
    conn.close()
    
    if not face_row:
        return jsonify({'error': 'Face not found'}), 404
    
    face_data = dict(face_row)
    query_photo_year = face_data.get('photo_year')
    
    # Get all manual faces to compare against
    if person_id_filter:
        manual_faces = face_detection_model.get_manual_faces_by_person(person_id_filter)
    else:
        manual_faces = face_detection_model.get_all_manual_faces_with_files()
    
    # Apply year filter if requested
    if year_filter and query_photo_year:
        manual_faces = [f for f in manual_faces if f.get('photo_year') == query_photo_year]
    
    if len(manual_faces) == 0:
        return jsonify({
            'success': True,
            'message': 'No manual faces found to compare against',
            'results': []
        })
    
    try:
        # Extract the query face region from its image
        # Resolve relative path from DB to absolute
        query_file_path = _resolve_path(face_data['file_path'], _original_root(catalog))
        
        # Check for alternative JPG version
        conn2 = catalog_db.get_connection()
        cursor2 = conn2.cursor()
        # Base rung, pinned - the crop below uses stored coordinates.
        cursor2.execute('''
            SELECT alternative_file_path
            FROM alternative_versions
            WHERE original_file_id = ?
              AND rung IN ('1200','1600','2048','2560')
            ORDER BY (rung = '1200') DESC, width ASC, id ASC
            LIMIT 1
        ''', (face_data['file_id'],))
        alt_row = cursor2.fetchone()
        conn2.close()
        
        if alt_row:
            alt_abs = _resolve_path(alt_row['alternative_file_path'], catalog.target_directory)
            if os.path.exists(alt_abs):
                query_file_path = alt_abs
        
        if not os.path.exists(query_file_path):
            return jsonify({'error': 'Query face image file not found'}), 404
        
        # Open and orient the image
        with Image.open(query_file_path) as img:
            img = ImageOps.exif_transpose(img) or img
            img = _turn_original(img, query_file_path, catalog.target_directory,
                                 face_data['file_id'])
            
            # Extract face region
            x, y, w, h = face_data['x'], face_data['y'], face_data['w'], face_data['h']
            face_img = img.crop((x, y, x + w, y + h))
            
            # Convert to RGB if needed
            if face_img.mode in ('RGBA', 'LA', 'P'):
                background = Image.new('RGB', face_img.size, (255, 255, 255))
                if face_img.mode == 'P':
                    face_img = face_img.convert('RGBA')
                if face_img.mode in ('RGBA', 'LA'):
                    background.paste(face_img, mask=face_img.split()[-1])
                    face_img = background
            elif face_img.mode != 'RGB':
                face_img = face_img.convert('RGB')
            
            # Save to bytes
            query_img_io = io.BytesIO()
            face_img.save(query_img_io, 'JPEG', quality=95)
            query_img_data = query_img_io.getvalue()
        
        # Prepare comparison face images
        comparison_files = []
        comparison_metadata = []
        
        for idx, manual_face in enumerate(manual_faces):
            # Skip comparing face to itself
            if manual_face['face_id'] == face_id:
                continue
            
            try:
                # Resolve relative path from DB to absolute
                comp_file_path = _resolve_path(manual_face['file_path'], _original_root(catalog))
                
                # Check for alternative JPG version
                conn3 = catalog_db.get_connection()
                cursor3 = conn3.cursor()
                # Base rung, pinned - same reason as above.
                cursor3.execute('''
                    SELECT alternative_file_path
                    FROM alternative_versions
                    WHERE original_file_id = ?
                      AND rung IN ('1200','1600','2048','2560')
                    ORDER BY (rung = '1200') DESC, width ASC, id ASC
                    LIMIT 1
                ''', (manual_face['file_id'],))
                alt_row2 = cursor3.fetchone()
                conn3.close()
                
                if alt_row2:
                    alt2_abs = _resolve_path(alt_row2['alternative_file_path'], catalog.target_directory)
                    if os.path.exists(alt2_abs):
                        comp_file_path = alt2_abs
                
                if not os.path.exists(comp_file_path):
                    continue
                
                # Extract comparison face region
                with Image.open(comp_file_path) as comp_img:
                    comp_img = ImageOps.exif_transpose(comp_img) or comp_img
                    comp_img = _turn_original(comp_img, comp_file_path,
                                              catalog.target_directory, manual_face['file_id'])
                    
                    cx, cy, cw, ch = manual_face['x'], manual_face['y'], manual_face['w'], manual_face['h']
                    comp_face_img = comp_img.crop((cx, cy, cx + cw, cy + ch))
                    
                    # Convert to RGB if needed
                    if comp_face_img.mode in ('RGBA', 'LA', 'P'):
                        background = Image.new('RGB', comp_face_img.size, (255, 255, 255))
                        if comp_face_img.mode == 'P':
                            comp_face_img = comp_face_img.convert('RGBA')
                        if comp_face_img.mode in ('RGBA', 'LA'):
                            background.paste(comp_face_img, mask=comp_face_img.split()[-1])
                            comp_face_img = background
                    elif comp_face_img.mode != 'RGB':
                        comp_face_img = comp_face_img.convert('RGB')
                    
                    # Save to bytes
                    comp_img_io = io.BytesIO()
                    comp_face_img.save(comp_img_io, 'JPEG', quality=95)
                    comp_img_data = comp_img_io.getvalue()
                    
                    comparison_files.append((f"face_{idx}.jpg", comp_img_data))
                    comparison_metadata.append({
                        'face_id': manual_face['face_id'],
                        'person_id': manual_face['person_id'],
                        'person_name': manual_face['person_name'],
                        'file_id': manual_face['file_id'],
                        'file_name': manual_face['file_name']
                    })
            except Exception as e:
                print(f"Error extracting comparison face {idx}: {e}")
                continue
        
        if len(comparison_files) == 0:
            return jsonify({
                'success': True,
                'message': 'No valid comparison faces found',
                'results': []
            })
        
        # Call deepface backend to compare
        deepface_base = os.environ.get('DEEPFACE_URL', 'http://deepface.airlock.lan')
        deepface_url = f'{deepface_base}/api/compare-multiple'
        
        files = {'face_image': ('query_face.jpg', query_img_data, 'image/jpeg')}
        form_data = {}
        
        for idx, (filename, img_data) in enumerate(comparison_files):
            files[f'comparison_{idx}'] = (filename, img_data, 'image/jpeg')
            metadata = comparison_metadata[idx]
            form_data[f'person_id_{idx}'] = str(metadata['person_id']) if metadata['person_id'] else ''
            form_data[f'face_id_{idx}'] = str(metadata['face_id'])
            form_data[f'person_name_{idx}'] = metadata['person_name'] or ''
        
        response = requests.post(deepface_url, files=files, data=form_data, timeout=60)
        
        if response.status_code != 200:
            return jsonify({'error': f'Deepface backend error: {response.text}'}), 500
        
        result = response.json()
        
        if not result.get('success'):
            return jsonify({'error': 'Face comparison failed'}), 500
        
        # Enhance results with file information
        enhanced_results = []
        for res in result.get('results', []):
            if 'distance' in res:
                # Find the corresponding metadata
                idx = res['index']
                if idx < len(comparison_metadata):
                    metadata = comparison_metadata[idx]
                    enhanced_results.append({
                        **res,
                        'file_id': metadata['file_id'],
                        'file_name': metadata['file_name']
                    })
        
        # Sort by distance and limit results
        enhanced_results.sort(key=lambda x: x.get('distance', float('inf')))
        enhanced_results = enhanced_results[:limit]
        
        return jsonify({
            'success': True,
            'query_face_id': face_id,
            'total_comparisons': result.get('total_comparisons', 0),
            'successful_comparisons': result.get('successful_comparisons', 0),
            'results': enhanced_results,
            'best_match': enhanced_results[0] if enhanced_results else None
        })
        
    except requests.exceptions.ConnectionError:
        return jsonify({'error': 'Could not connect to deepface backend. Make sure it is running on port 5001.'}), 503
    except Exception as e:
        return jsonify({'error': f'Error comparing faces: {str(e)}'}), 500

@app.route('/api/catalogs/<int:catalog_id>/people/<int:person_id>/faces', methods=['GET'])
def get_person_face_contents(catalog_id, person_id):
    """
    Get manually annotated face images for a person, optionally filtered by year and month.
    
    Query parameters:
    - year (optional): Filter by year
    - month (optional): Filter by month (1-12)
    
    Returns list of face_contents records with file information.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    
    # Get query parameters
    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)
    
    # Validate month if provided
    if month is not None and (month < 1 or month > 12):
        return jsonify({'error': 'Month must be between 1 and 12'}), 400
    
    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    
    # Build query with optional filters
    query = '''
        SELECT 
            fc.id,
            fc.file_path,
            fc.file_name,
            fc.file_size,
            fc.sha256_hash,
            fc.year,
            fc.month,
            fc.day,
            fc.person_id,
            fc.face_detection_id,
            fc.source_file_id,
            fc.created_at,
            p.name as person_name,
            src.file_name as source_file_name,
            src.file_path as source_file_path
        FROM face_contents fc
        INNER JOIN people p ON fc.person_id = p.id
        INNER JOIN file_contents src ON fc.source_file_id = src.id
        WHERE fc.person_id = ? AND src.removed_at IS NULL
    '''
    
    params = [person_id]
    
    if year is not None:
        query += ' AND fc.year = ?'
        params.append(year)
    
    if month is not None:
        query += ' AND fc.month = ?'
        params.append(month)
    
    query += ' ORDER BY fc.year DESC, fc.month DESC, fc.day DESC, fc.created_at DESC'
    
    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()
    
    faces = []
    for row in rows:
        face = dict(row)
        # Resolve relative paths (from DB) to absolute for the client
        face['file_path'] = _resolve_path(face.get('file_path'), catalog.target_directory)
        face['source_file_path'] = _resolve_path(face.get('source_file_path'), _original_root(catalog))
        faces.append(face)
    
    return jsonify({
        'success': True,
        'person_id': person_id,
        'year': year,
        'month': month,
        'count': len(faces),
        'faces': faces
    })


# ─── Face Recognition API ─────────────────────────────────────────────────────

# Supported distance metrics and their corresponding column names.
#
# 'euclidean' was here and was removed. The stored euclidean_distance is the L2
# distance between L2-normalised embeddings, and for unit vectors:
#
#     ||a-b||^2 = 2(1 - cos_sim) = 2 * cosine_distance
#     euclidean_l2 = sqrt(2 * cosine_distance)
#
# sqrt is strictly increasing, so ORDER BY euclidean_distance and ORDER BY
# distance produce the identical ranking, always. Offering it as a choice could
# only ever show the same answer with different numbers on it. Verified on the
# Family catalog: mean |euclidean_l2 - sqrt(2 * cosine)| = 0.000000 over 500 rows.
#
# The column and the workflow that fills it are left in place - the data is
# harmless and cheap to compute - but ?metric=euclidean now falls through to
# cosine like any other unrecognised value, which is the correct answer anyway.
_METRIC_COLUMN = {
    'cosine': 'distance',
    # Contextual metric: appearance fused with event/time/co-occurrence context.
    # Stored as 1 - p (probability the face is that person), so it uses the same
    # 0-1 lower-is-closer convention as cosine.
    'contextual': 'contextual_distance',
}
_DEFAULT_METRIC = 'cosine'


def _resolve_metric_column(metric_param):
    """Return (metric_name, column_name) for the given ?metric= param value.
    Falls back to cosine for unknown values."""
    metric = (metric_param or _DEFAULT_METRIC).lower()
    column = _METRIC_COLUMN.get(metric, _METRIC_COLUMN[_DEFAULT_METRIC])
    if metric not in _METRIC_COLUMN:
        metric = _DEFAULT_METRIC
    return metric, column


# ── Identify people: candidates, counted and paged on the server ─────────────
#
# A CANDIDATE is an unidentified face (Automatic, no person, its photo not
# removed) together with its BEST match: the person whose score row for it has
# the smallest distance in the active metric. A face is a candidate for exactly
# one person - its best - and counts when that distance meets the minimum
# similarity the reviewer set.
#
#   /faces/unidentified/summary   how many candidates each person has
#   /faces/unidentified           one person's candidates, a page at a time
#
# Both read face_best_match, where each waiting face's best match is stored and
# kept current by triggers (face_best_match.py). Working it out per request -
# grouping every score row - was 11 s per summary and 13 s per page on `family`
# at 12.5 M score rows (2026-10-11). Read from the table: a page in milliseconds,
# a summary in one index scan.
#
# `stale` in a response means the table was read while a backlog is still being
# drained - after a scoring run, or on the first visit after this table arrived.
# The page polls until it clears.

# Only faces still waiting for somebody: Automatic, unnamed, photo not removed.
# face_best_match holds only those already; a page joins anyway, because it
# needs the face and photo columns and because a face named a moment ago may
# not have been drained yet.
_UNIDENTIFIED_JOIN = '''
        JOIN face_detections fd ON fd.id = b.face_id
                               AND fd.source = 'Automatic' AND fd.person_id IS NULL
        JOIN file_contents fc ON fc.id = fd.file_id AND fc.removed_at IS NULL'''


def _max_distance(min_similarity):
    """
    The largest distance whose card would show at least min_similarity.

    The card shows round((1 - d) * 100) and the page has always counted a face
    when that rounded number meets the minimum (utils/similarity.js
    meetsSimilarity) - so a face labelled 60 is in at a minimum of 60. That is
    d <= 1 - (min - 0.5) / 100. None when every score qualifies.
    """
    if min_similarity is None or min_similarity <= 0:
        return None
    return 1 - (min_similarity - 0.5) / 100


def _face_query_args(req):
    metric, dist_col = _resolve_metric_column(req.args.get('metric'))
    raw = req.args.get('min_similarity')
    min_similarity = float(raw) if raw not in (None, '') else None
    return metric, dist_col, min_similarity


@app.route('/api/catalogs/<int:catalog_id>/faces/unidentified/summary', methods=['GET'])
def get_unidentified_faces_summary(catalog_id):
    """
    Per person: `candidates` (best match is them and meets min_similarity) and
    `matched` (best match is them at any similarity). The people list draws its
    badges and order from the first; the empty states tell "lower the minimum"
    from "no scores yet" with the second.

        ?metric=cosine|contextual  &min_similarity=0..100
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    try:
        metric, dist_col, min_similarity = _face_query_args(request)
    except ValueError:
        return jsonify({'error': 'min_similarity must be a number'}), 400
    max_d = _max_distance(min_similarity)

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    try:
        fresh = face_best_match.ensure_fresh(conn, catalog_db.db_path)
        rows = conn.execute('''
            SELECT person_id,
                   SUM(CASE WHEN ? IS NULL OR d <= ? THEN 1 ELSE 0 END) AS candidates,
                   COUNT(*) AS matched
              FROM face_best_match
             WHERE metric = ?
             GROUP BY person_id''', (max_d, max_d, metric)).fetchall()
        has_unidentified = conn.execute('''
            SELECT EXISTS (SELECT 1 FROM face_detections fd
                             JOIN file_contents fc ON fc.id = fd.file_id
                            WHERE fd.source = 'Automatic' AND fd.person_id IS NULL
                              AND fc.removed_at IS NULL)''').fetchone()[0]
    finally:
        conn.close()

    people = {str(r['person_id']): {'candidates': r['candidates'], 'matched': r['matched']}
              for r in rows}
    return jsonify({
        'metric': metric,
        'min_similarity': min_similarity,
        'people': people,
        'has_scores': bool(people),
        'has_unidentified': bool(has_unidentified),
        'stale': not fresh,
    })


@app.route('/api/catalogs/<int:catalog_id>/faces/unidentified', methods=['GET'])
def get_unidentified_faces(catalog_id):
    """
    One page of one person's candidates, closest first.

        ?person_id=  (required)  &metric=  &min_similarity=  &limit=  &offset=

    Returns {faces, total, limit, offset, metric, stale}. Each face carries what
    its card draws - the crop or the box on the photo, its date - and best_match
    with every metric's distance, as the per-face lookup returned it.

    There is no "everything" form any more. It was 188 MB on `family`, and the
    page it served only ever showed one person's faces a page at a time.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    try:
        metric, dist_col, min_similarity = _face_query_args(request)
        person_id = int(request.args['person_id'])
        limit = max(1, min(500, int(request.args.get('limit', 100))))
        offset = max(0, int(request.args.get('offset', 0)))
    except KeyError:
        return jsonify({'error': 'person_id is required'}), 400
    except ValueError:
        return jsonify({'error': 'person_id, min_similarity, limit and offset '
                                 'must be numbers'}), 400
    max_d = _max_distance(min_similarity)

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    try:
        fresh = face_best_match.ensure_fresh(conn, catalog_db.db_path)
        # Read in idx_fbm_person order, so the page stops at LIMIT.
        rows = conn.execute(f'''
            SELECT fd.id AS face_id, fd.file_id, fd.x, fd.y, fd.w, fd.h,
                   fd.image_width, fd.image_height,
                   fc.file_path, fc.file_name, fc.file_type, fc.master_datetime,
                   afc.file_path AS ai_crop_path,
                   s.reference_face_detection_id, s.person_id, p.name AS person_name,
                   s.distance, s.euclidean_distance, s.contextual_distance,
                   s.contextual_evidence, s.verified, s.threshold,
                   b.d AS metric_distance
              FROM face_best_match b
              {_UNIDENTIFIED_JOIN}
              JOIN face_similarity_scores s ON s.id = b.score_id
              JOIN people p ON p.id = s.person_id
              LEFT JOIN ai_face_contents afc ON afc.face_detection_id = fd.id
             WHERE b.metric = ? AND b.person_id = ?
               AND (? IS NULL OR b.d <= ?)
             ORDER BY b.d ASC, b.face_id ASC
             LIMIT ? OFFSET ?''',
            (metric, person_id, max_d, max_d, limit, offset)).fetchall()
        # The table holds only waiting faces, so the total needs no joins.
        total = conn.execute('''
            SELECT COUNT(*) FROM face_best_match
             WHERE metric = ? AND person_id = ? AND (? IS NULL OR d <= ?)''',
            (metric, person_id, max_d, max_d)).fetchone()[0]
    finally:
        conn.close()

    best_keys = ('reference_face_detection_id', 'person_id', 'person_name',
                 'distance', 'euclidean_distance', 'contextual_distance',
                 'contextual_evidence', 'metric_distance', 'verified', 'threshold')
    faces = []
    for r in rows:
        r = dict(r)
        best = {k: r.pop(k) for k in best_keys}
        best['metric'] = metric
        r['best_match'] = best
        faces.append(r)
    return jsonify({'success': True, 'metric': metric, 'faces': faces,
                    'total': total, 'limit': limit, 'offset': offset,
                    'stale': not fresh})


@app.route('/api/catalogs/<int:catalog_id>/faces/<int:face_id>/similarity-scores', methods=['GET'])
def get_face_similarity_scores(catalog_id, face_id):
    """
    Get all computed similarity scores for one query face.
    Optional query params:
      threshold (float)   — only return results where the active metric distance <= threshold.
      metric    (str)     — 'cosine' (default) or 'contextual'; controls sort order and threshold filter.
    Both distance columns are always included in the response so the UI can display either.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    threshold = request.args.get('threshold', type=float)
    metric, dist_col = _resolve_metric_column(request.args.get('metric'))

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()

    # Verify face exists
    cursor.execute('SELECT id FROM face_detections WHERE id = ?', (face_id,))
    if not cursor.fetchone():
        conn.close()
        return jsonify({'error': 'Face not found'}), 404

    query = f'''
        SELECT
            fss.id AS score_id,
            fss.reference_face_detection_id,
            fss.person_id,
            p.name AS person_name,
            fss.distance,
            fss.euclidean_distance,
            fss.contextual_distance,
            fss.contextual_evidence,
            fss.{dist_col} AS metric_distance,
            fss.verified,
            fss.threshold,
            fss.model,
            fss.distance_metric,
            fss.computed_at
        FROM face_similarity_scores fss
        INNER JOIN people p ON fss.person_id = p.id
        WHERE fss.query_face_detection_id = ?
    '''
    params = [face_id]
    if threshold is not None:
        query += f' AND fss.{dist_col} <= ?'
        params.append(threshold)
    query += f' ORDER BY fss.{dist_col} ASC'

    cursor.execute(query, params)
    rows = cursor.fetchall()
    conn.close()

    scores = [dict(row) for row in rows]
    return jsonify({
        'success': True,
        'face_id': face_id,
        'metric': metric,
        'total_scores': len(scores),
        'scores': scores
    })


@app.route('/api/catalogs/<int:catalog_id>/faces/similarity-scores/summary', methods=['GET'])
def get_similarity_scores_summary(catalog_id):
    """
    Returns a summary per unidentified face: candidate count within threshold and best match.
    Optional query params:
      threshold (float, default 0.4) — filter distance <= threshold.
      metric    (str,  default 'cosine') — 'cosine' or 'contextual'.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    threshold = request.args.get('threshold', default=0.4, type=float)
    metric, dist_col = _resolve_metric_column(request.args.get('metric'))

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()

    cursor.execute(f'''
        SELECT
            fss.query_face_detection_id AS face_id,
            COUNT(*) AS candidate_count,
            MIN(fss.{dist_col}) AS best_distance,
            (SELECT p2.name FROM face_similarity_scores fss2
             INNER JOIN people p2 ON fss2.person_id = p2.id
             WHERE fss2.query_face_detection_id = fss.query_face_detection_id
               AND fss2.{dist_col} IS NOT NULL
             ORDER BY fss2.{dist_col} ASC LIMIT 1) AS best_person_name,
            (SELECT fss2.person_id FROM face_similarity_scores fss2
             WHERE fss2.query_face_detection_id = fss.query_face_detection_id
               AND fss2.{dist_col} IS NOT NULL
             ORDER BY fss2.{dist_col} ASC LIMIT 1) AS best_person_id
        FROM face_similarity_scores fss
        WHERE fss.{dist_col} IS NOT NULL
          AND fss.{dist_col} <= ?
        GROUP BY fss.query_face_detection_id
        ORDER BY best_distance ASC
    ''', (threshold,))
    rows = cursor.fetchall()
    conn.close()

    return jsonify({
        'success': True,
        'metric': metric,
        'threshold': threshold,
        'total_faces_with_candidates': len(rows),
        'summary': [dict(row) for row in rows]
    })


def _load_contextual_workflow():
    """Import the contextual workflow module to reuse its revert logic."""
    import importlib.util
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                        'workflows', 'ai_contextual_recognition_workflow.py')
    spec = importlib.util.spec_from_file_location('ai_contextual_recognition_workflow', path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@app.route('/api/catalogs/<int:catalog_id>/auto-tags', methods=['GET'])
def list_auto_tags(catalog_id):
    """
    Faces tagged by the model without human review, newest first, with the
    evidence that justified each one.

    Optional query params:
      execution_id (int)  - only this run
      include_reverted    - '1' to include already-reverted entries
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()

    clauses = ["ata.action = 'auto_confirmed'"]
    params = []
    if request.args.get('include_reverted') != '1':
        clauses.append('ata.reverted_at IS NULL')
    if request.args.get('execution_id'):
        clauses.append('ata.execution_id = ?')
        params.append(request.args.get('execution_id'))

    try:
        cursor.execute(f'''
            SELECT ata.id, ata.execution_id, ata.face_detection_id, ata.person_id,
                   ata.confidence, ata.contextual_distance, ata.evidence,
                   ata.reverted_at, ata.created_at,
                   p.name AS person_name,
                   fd.source, fd.file_id, fd.x, fd.y, fd.w, fd.h,
                   fd.image_width, fd.image_height,
                   fc.file_name, fc.file_path,
                   afc.file_path AS ai_crop_path
            FROM auto_tag_audit ata
            INNER JOIN face_detections fd ON fd.id = ata.face_detection_id
            INNER JOIN file_contents fc ON fc.id = fd.file_id
            LEFT JOIN people p ON p.id = ata.person_id
            LEFT JOIN ai_face_contents afc ON afc.face_detection_id = fd.id
            WHERE {' AND '.join(clauses)}
            ORDER BY ata.created_at DESC, ata.id DESC
        ''', params)
        rows = [dict(r) for r in cursor.fetchall()]
    except Exception:
        conn.close()
        return jsonify({'success': True, 'total': 0, 'auto_tags': [],
                        'message': 'No auto-tags yet.'})

    for row in rows:
        row['ai_crop_path'] = _resolve_path(row.get('ai_crop_path'),
                                            catalog.target_directory)
        row['file_path'] = _resolve_path(row.get('file_path'),
                                         _original_root(catalog))
    conn.close()
    return jsonify({'success': True, 'total': len(rows), 'auto_tags': rows})


@app.route('/api/catalogs/<int:catalog_id>/auto-tags/revert', methods=['POST'])
def revert_auto_tags_bulk(catalog_id):
    """
    Undo auto-confirmations in bulk, restoring each face to unidentified.

    Body: {"execution_id": <int>}  - the run to undo.
    Faces a human has since confirmed are left alone.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.get_json(silent=True) or {}
    execution_id = data.get('execution_id')
    if execution_id is None:
        return jsonify({'error': 'execution_id is required'}), 400

    catalog_db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    try:
        module = _load_contextual_workflow()
        count = module.revert_auto_tags(catalog_db_path,
                                        execution_id=int(execution_id))
    except Exception as e:
        return jsonify({'error': f'Revert failed: {str(e)}'}), 500

    return jsonify({'success': True, 'reverted': count,
                    'execution_id': execution_id})


@app.route('/api/catalogs/<int:catalog_id>/faces/<int:face_id>/auto-tag',
           methods=['DELETE'])
def revert_auto_tag_single(catalog_id, face_id):
    """Undo one auto-confirmation, returning that face to the review queue."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    try:
        module = _load_contextual_workflow()
        count = module.revert_auto_tags(catalog_db_path, face_detection_id=face_id)
    except Exception as e:
        return jsonify({'error': f'Revert failed: {str(e)}'}), 500

    if count == 0:
        return jsonify({'success': True, 'reverted': 0,
                        'message': 'Face was not auto-confirmed, or has since '
                                   'been confirmed by a person.'})
    return jsonify({'success': True, 'reverted': count})


def _cluster_person_suggestions(conn, cluster_ids):
    """
    The most likely person for each cluster: {cluster_id: {id, name, similarity}}.

    Compares each cluster's stored centroid against the mean embedding of every
    identified person, using the same cosine arithmetic the contextual workflow
    already uses to compute cohesion (ai_contextual_recognition_workflow.py).
    Nothing new is computed or stored - the centroids and the per-face embeddings
    are both already there.

    This is a HINT, never an assignment. Connected-component clustering can chain
    two people together through one ambiguous face, and such a cluster looks just
    as confident as a good one; the reviewer still picks the name.
    """
    if not cluster_ids:
        return {}
    try:
        import numpy as np
    except ImportError:
        return {}

    def _vec(raw):
        try:
            v = np.asarray(_json.loads(raw), dtype=float)
        except Exception:
            return None
        n = np.linalg.norm(v)
        return v / n if n else None

    # One reference vector per person: the mean of their identified faces.
    people_vecs, people_names = {}, {}
    try:
        rows = conn.execute(
            'SELECT fd.person_id, p.name, afc.embedding '
            'FROM ai_face_contents afc '
            'JOIN face_detections fd ON fd.id = afc.face_detection_id '
            'JOIN people p ON p.id = fd.person_id '
            'WHERE afc.embedding IS NOT NULL AND fd.person_id IS NOT NULL'
        ).fetchall()
    except Exception:
        return {}

    grouped = {}
    for r in rows:
        v = _vec(r['embedding'])
        if v is not None:
            grouped.setdefault(r['person_id'], []).append(v)
            people_names[r['person_id']] = r['name']
    for pid, vecs in grouped.items():
        m = np.mean(np.vstack(vecs), axis=0)
        n = np.linalg.norm(m)
        if n:
            people_vecs[pid] = m / n
    if not people_vecs:
        return {}

    pids = list(people_vecs.keys())
    matrix = np.vstack([people_vecs[i] for i in pids])

    out = {}
    placeholders = ','.join('?' * len(cluster_ids))
    for row in conn.execute(
        f'SELECT id, centroid FROM face_clusters WHERE id IN ({placeholders})',
        cluster_ids
    ).fetchall():
        c = _vec(row['centroid'])
        if c is None:
            continue
        sims = matrix @ c                      # cosine, both sides normalised
        best = int(np.argmax(sims))
        out[row['id']] = {
            'person_id': pids[best],
            'name': people_names.get(pids[best]),
            # 0-100, matching how similarity is shown elsewhere in the UI.
            'similarity': round(float(sims[best]) * 100, 1),
        }
    return out


@app.route('/api/catalogs/<int:catalog_id>/embedding-model', methods=['GET'])
def get_embedding_model(catalog_id):
    """
    Which model this catalog's face embeddings come from, and the thresholds
    that belong to it.

    The frontend reads its match threshold from here rather than hardcoding it.
    similarity.js held 0.35 - a number measured for VGG-Face, with nothing in
    the file saying which model it was measured on - so a catalog that moved to
    another model would have gone on being judged by the old model's bar, in a
    place nobody would think to look.

    `counts` reports what is actually stored, which is how you tell a migrated
    catalog from a half-migrated one. `match_distance_measured` is false while a
    threshold is still the provisional value from the registry; the UI says so
    rather than presenting a guess as a measurement.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    active = airlock_embedding_models.active_model(db_path)
    out = airlock_embedding_models.info(active)

    # What is on disk, per model. NULL counts as the legacy model, as everywhere.
    counts = {}
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            rows = conn.execute(
                'SELECT COALESCE(embedding_model_id, 1) AS mid, COUNT(*) '
                '  FROM ai_face_contents WHERE embedding IS NOT NULL '
                ' GROUP BY 1').fetchall()
        finally:
            conn.close()
        for mid, n in rows:
            try:
                counts[airlock_embedding_models.model_name(mid)] = n
            except ValueError:
                counts[f'unknown-id-{mid}'] = n
    except sqlite3.Error:
        counts = None        # pre-migration catalog, or no catalog db yet

    out['counts'] = counts
    out['models'] = [airlock_embedding_models.info(n)
                     for n in sorted(airlock_embedding_models.MODELS)]
    return jsonify(out)


@app.route('/api/catalogs/<int:catalog_id>/faces/clusters', methods=['GET'])
def get_face_clusters(catalog_id):
    """
    Groups of unidentified faces the contextual workflow judged to be the same
    person, so one confirmation can label the whole group.

    Optional query params:
      min_size    (int, default 2) - only clusters with at least this many members
      max_members (int, default 24) - cap on member rows returned per cluster
      limit, offset (default 50, 0) - ONE PAGE of clusters, largest first
      include_resolved (1) - also list clusters somebody already named

    Paged because `family` has 9,479 unresolved clusters: unpaged that was
    15.5 MB, a member query per cluster, and a page drawing ~227,000 faces -
    the browser took most of a minute, showing "0 groups" until it was done.
    `total` and `total_faces` describe every matching cluster, not the page.

    Each cluster carries `cohesion` (mean distance of members to the centroid) and
    `max_member_distance`. Connected-component clustering can chain two people
    together through an intermediate face; a high value in either field is the
    signal that a cluster is mixed and should not be trusted wholesale.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    try:
        min_size = int(request.args.get('min_size', 2))
    except (TypeError, ValueError):
        min_size = 2
    try:
        max_members = int(request.args.get('max_members', 24))
    except (TypeError, ValueError):
        max_members = 24
    try:
        limit = max(1, min(200, int(request.args.get('limit', 50))))
        offset = max(0, int(request.args.get('offset', 0)))
    except (TypeError, ValueError):
        return jsonify({'error': 'limit and offset must be numbers'}), 400
    include_resolved = str(request.args.get('include_resolved', '')).lower() in ('1', 'true', 'yes')
    # Open = unresolved AND still holding a face nobody has named. A group whose
    # every face was tagged some other way since it was built (one at a time, in
    # Identify People) has nothing left to assign: listing it made Assign answer
    # "0 assigned", leave it unresolved, and show it again - 1,079 such groups on
    # family (2026-10-19). 0.04 s there, on idx_fcm_cluster.
    resolved_sql = '' if include_resolved else (
        'AND fc.resolved_person_id IS NULL AND EXISTS ('
        'SELECT 1 FROM face_cluster_members om '
        'JOIN face_detections ofd ON ofd.id = om.face_detection_id '
        'WHERE om.cluster_id = fc.id AND ofd.person_id IS NULL)')

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()

    try:
        cursor.execute('''
            SELECT fc.id, fc.member_count, fc.cohesion, fc.max_member_distance,
                   fc.resolved_person_id, fc.resolved_at, fc.created_at,
                   p.name AS resolved_person_name
            FROM face_clusters fc
            LEFT JOIN people p ON p.id = fc.resolved_person_id
            WHERE fc.member_count >= ? {resolved_sql}
            ORDER BY fc.member_count DESC, fc.cohesion ASC, fc.id ASC
            LIMIT ? OFFSET ?
        '''.format(resolved_sql=resolved_sql), (min_size, limit, offset))
        clusters = [dict(row) for row in cursor.fetchall()]
        total, total_faces = cursor.execute(
            f'SELECT COUNT(*), COALESCE(SUM(fc.member_count), 0) FROM face_clusters fc '
            f'WHERE fc.member_count >= ? {resolved_sql}', (min_size,)).fetchone()
    except Exception:
        conn.close()
        return jsonify({
            'success': True, 'total': 0, 'total_faces': 0, 'clusters': [],
            'limit': limit, 'offset': offset,
            'message': 'No clusters yet - run the Contextual Recognition workflow.'
        })

    for cluster in clusters:
        cursor.execute('''
            SELECT fcm.face_detection_id, fcm.distance_to_centroid,
                   fd.file_id, fd.x, fd.y, fd.w, fd.h,
                   fd.image_width, fd.image_height,
                   fcont.file_name, fcont.file_path,
                   afc.file_path AS ai_crop_path
            FROM face_cluster_members fcm
            INNER JOIN face_detections fd ON fd.id = fcm.face_detection_id
            INNER JOIN file_contents fcont ON fcont.id = fd.file_id
            LEFT JOIN ai_face_contents afc ON afc.face_detection_id = fd.id
            WHERE fcm.cluster_id = ? AND fcont.removed_at IS NULL
              {untagged}
            ORDER BY fcm.distance_to_centroid ASC
            LIMIT ?
        '''.format(untagged='' if include_resolved else 'AND fd.person_id IS NULL'),
            (cluster['id'], max_members))
        members = []
        for row in cursor.fetchall():
            member = dict(row)
            member['ai_crop_path'] = _resolve_path(
                member.get('ai_crop_path'), catalog.target_directory)
            member['file_path'] = _resolve_path(
                member.get('file_path'), _original_root(catalog))
            members.append(member)
        cluster['members'] = members
        if not include_resolved:
            # The faces Assign would tag - not those already named another way.
            cluster['member_count'] = cursor.execute(
                'SELECT COUNT(*) FROM face_cluster_members fcm '
                'JOIN face_detections fd ON fd.id = fcm.face_detection_id '
                'JOIN file_contents fcont ON fcont.id = fd.file_id '
                'WHERE fcm.cluster_id = ? AND fd.person_id IS NULL AND fcont.removed_at IS NULL',
                (cluster['id'],)).fetchone()[0]
        cluster['members_truncated'] = cluster['member_count'] > len(members)

    # The likeliest person for each cluster, shown as a hint the reviewer can
    # accept with one click. Deliberately not applied automatically.
    try:
        suggestions = _cluster_person_suggestions(conn, [c['id'] for c in clusters])
    except Exception as exc:
        _logging.getLogger(__name__).warning('cluster suggestions failed: %s', exc)
        suggestions = {}
    for cluster in clusters:
        hint = suggestions.get(cluster['id'])
        cluster['suggested_person_id'] = hint['person_id'] if hint else None
        cluster['suggested_person_name'] = hint['name'] if hint else None
        cluster['suggested_similarity'] = hint['similarity'] if hint else None

    conn.close()
    return jsonify({
        'success': True,
        'total': total,
        'total_faces': total_faces,
        'limit': limit,
        'offset': offset,
        'clusters': clusters,
    })


@app.route('/api/catalogs/<int:catalog_id>/faces/clusters/<int:cluster_id>/assign',
           methods=['POST'])
def assign_face_cluster(catalog_id, cluster_id):
    """
    Assign every member of a cluster to one person, in a single call.

    Body:
      person_id        (int, required)
      exclude_face_ids (list[int], optional) - members to leave alone, for when
                       the reviewer can see the cluster has pulled in a face
                       that does not belong.

    Faces that already carry a person are skipped rather than overwritten: a
    cluster is a machine's guess, and a person's decision outranks it. The
    cluster is marked resolved only if at least one face was actually assigned.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.get_json(silent=True) or {}
    person_id = data.get('person_id')
    if person_id is None:
        return jsonify({'error': 'person_id is required'}), 400

    try:
        excluded = {int(x) for x in (data.get('exclude_face_ids') or [])}
    except (TypeError, ValueError):
        return jsonify({'error': 'exclude_face_ids must be a list of integers'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    people_model = PeopleModel(catalog_db)
    person = people_model.get_person_by_id(person_id)
    if not person:
        return jsonify({'error': 'Person not found'}), 404

    needs_profile_photo = (person.get('profile_photo_path') is None and
                           person.get('profile_photo_file_id') is None)

    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('SELECT 1 FROM face_clusters WHERE id = ?', (cluster_id,))
        if cursor.fetchone() is None:
            conn.close()
            return jsonify({'error': 'Cluster not found'}), 404

        cursor.execute(
            'SELECT fcm.face_detection_id, fd.person_id '
            'FROM face_cluster_members fcm '
            'INNER JOIN face_detections fd ON fd.id = fcm.face_detection_id '
            'WHERE fcm.cluster_id = ? '
            'ORDER BY fcm.distance_to_centroid ASC',
            (cluster_id,))
        members = [dict(r) for r in cursor.fetchall()]
    except Exception as e:
        conn.close()
        return jsonify({'error': f'Could not read cluster: {e}'}), 500
    conn.close()

    face_detection_model = FaceDetectionModel(catalog_db)
    assigned, skipped, failed = [], [], []

    for member in members:
        face_id = member['face_detection_id']
        if face_id in excluded:
            skipped.append({'face_id': face_id, 'reason': 'excluded by reviewer'})
            continue
        if member['person_id'] is not None:
            skipped.append({'face_id': face_id, 'reason': 'already assigned'})
            continue
        try:
            ok = _assign_face_to_person(
                catalog, catalog_db, face_detection_model, face_id, person_id,
                # Only the first face assigned can become the profile photo.
                set_profile_if_missing=(needs_profile_photo and not assigned),
            )
        except Exception as e:
            failed.append({'face_id': face_id, 'error': str(e)})
            continue
        if ok:
            assigned.append(face_id)
        else:
            failed.append({'face_id': face_id, 'error': 'face not found'})

    # Resolved when it tagged something - or when there was nothing left to
    # tag because every face already had a person (tagged another way since
    # the group was built). Left unresolved, such a group came back on every
    # load and every Assign answered "0 assigned" (2026-10-19).
    nothing_left = bool(skipped) and not assigned and not failed and all(
        s['reason'] == 'already assigned' for s in skipped)
    if assigned or nothing_left:
        conn = catalog_db.get_connection()
        conn.execute(
            'UPDATE face_clusters SET resolved_person_id = ?, resolved_at = ? '
            'WHERE id = ?',
            (person_id, _datetime.now().isoformat(), cluster_id))
        conn.commit()
        conn.close()

    if assigned:
        _fire_face_confirmed(catalog, len(assigned))

    return jsonify({
        'success': True,
        'cluster_id': cluster_id,
        'person_id': person_id,
        'person_name': person.get('name'),
        'assigned': len(assigned),
        'assigned_face_ids': assigned,
        'skipped': skipped,
        'failed': failed,
        'resolved': bool(assigned) or nothing_left,
    })


@app.route('/api/catalogs/<int:catalog_id>/faces/<int:face_id>/similarity-scores/<int:person_id>', methods=['DELETE'])
def reject_face_candidate(catalog_id, face_id, person_id):
    """
    Remove all face_similarity_scores rows for a specific (query_face, person) pair.
    The face detection itself is NOT deleted — it remains in the unidentified pool
    and can still match other people whose scores survive.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    cursor.execute(
        'DELETE FROM face_similarity_scores WHERE query_face_detection_id = ? AND person_id = ?',
        (face_id, person_id)
    )
    deleted = cursor.rowcount
    conn.commit()
    conn.close()

    return jsonify({'success': True, 'deleted': deleted})


# ─── Events API ───────────────────────────────────────────────────────────────

@app.route('/api/catalogs/<int:catalog_id>/events', methods=['GET'])
def get_events(catalog_id):
    """Get all events in a catalog - or, with ?date=YYYY-MM-DD, one day's
    (_events_of_day, calendar-view-spec.md section 6)."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    if request.args.get('date'):
        return _events_of_day(catalog_id, catalog_db, event_model, request.args['date'])
    events = event_model.get_all_events()

    # Covers are resolved here, for every event at once, rather than by each
    # card fetching its own: that would be N requests on page load
    # (event-cards-ui-spec.md section 4). Resolution also persists a first-time
    # random pick, which is what makes a card's cover stable across reloads.
    event_model.resolve_covers(events)
    for event in events:
        event['cover_thumb_url'] = (
            _thumb_url(catalog_id, event['cover_file_id'])
            if event.get('cover_file_id') else None
        )
    return jsonify(events)


def _events_of_day(catalog_id, catalog_db, event_model, day):
    """
    One day in Events: every event with a file dated that day - not only the
    events that START on it, so an event crossing midnight shows on both days -
    and how many of the day's photos and videos are in no event.

    The day's files are found by the indexed master_datetime range the photo
    gallery uses, by the same rules Calendar view counts with
    (calendar_counts.py), so the day's total here is the square's.
    """
    import calendar_counts
    from media_types import VIDEO_EXTENSIONS
    try:
        d = _datetime.strptime(day, '%Y-%m-%d').date()
    except ValueError:
        return jsonify({'error': 'date must be YYYY-MM-DD'}), 400
    range_sql, range_params = _photo_date_range(d.year, d.month, d.day)
    kinds = calendar_counts._types_sql(set(_PHOTO_TYPES) | set(VIDEO_EXTENSIONS))
    conn = catalog_db.get_connection()
    try:
        files = conn.execute(f'''
            SELECT fc.id,
                   (SELECT GROUP_CONCAT(ef.event_id) FROM event_files ef
                     WHERE ef.file_id = fc.id) AS events
              FROM file_contents fc
             WHERE {range_sql}
               AND LOWER(fc.file_type) IN {kinds}
               AND fc.removed_at IS NULL
               AND fc.file_path NOT LIKE 'cache/%'
               AND fc.id NOT IN (SELECT f.id FROM file_contents f
                                   JOIN image_groups g ON g.id = f.image_group_id
                                  WHERE f.id <> g.primary_file_id)''', range_params).fetchall()
        event_ids, loose = set(), 0
        for f in files:
            if f['events']:
                event_ids.update(int(x) for x in str(f['events']).split(','))
            else:
                loose += 1
        events = [dict(r) for r in conn.execute(
            f'SELECT * FROM events WHERE id IN ({_in_clause(sorted(event_ids))}) '
            'ORDER BY start_time', sorted(event_ids)).fetchall()] if event_ids else []
    finally:
        conn.close()
    event_model.resolve_covers(events)
    for event in events:
        event['cover_thumb_url'] = (
            _thumb_url(catalog_id, event['cover_file_id'])
            if event.get('cover_file_id') else None)
    return jsonify({'date': d.isoformat(), 'total': len(files), 'not_in_an_event': loose,
                    'events': events})


# ─── Calendar view (documentation/architecture/calendar-view-spec.md) ────────

def _calendar_db_path(catalog_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return None, (jsonify({'error': 'Catalog not found'}), 404)
    if not catalog.target_directory:
        return None, (jsonify({'error': 'Catalog target directory not set'}), 400)
    path = os.path.join(catalog.target_directory, 'workflow_results.db')
    if not os.path.exists(path):
        CatalogDatabase(catalog.target_directory)
    return path, None


@app.route('/api/catalogs/<int:catalog_id>/calendar', methods=['GET'])
def get_calendar_counts(catalog_id):
    """Photos and videos per day, from the cached count (calendar_counts.py):
    milliseconds, recounted in the background when the catalog has changed."""
    import calendar_counts
    path, err = _calendar_db_path(catalog_id)
    if err:
        return err
    return jsonify(calendar_counts.public(calendar_counts.get(path, _PHOTO_TYPES)))


# ─── Search (documentation/architecture/search-spec.md) ──────────────────────
#
# One box and filters, one list (search.py); every name a file has been known
# by, in a trigram index (search_index.py); and saved searches - a query plus
# the files chosen from it, for operations later (saved_searches.py).

# How long a saved-search write waits for a workflow's write lock. The search
# reads never wait (WAL), but a save does; well inside gunicorn's 300 s.
SAVED_SEARCH_WRITE_WAIT = 120

CATALOG_BUSY = ('The catalog is busy - a workflow is writing to it. '
                'Try again in a minute.')


def _is_locked(exc):
    return isinstance(exc, sqlite3.OperationalError) and 'locked' in str(exc).lower()


@app.errorhandler(sqlite3.OperationalError)
def _catalog_busy(exc):
    """
    A saved-search write that waited out SAVED_SEARCH_WRITE_WAIT behind a
    workflow: say so, rather than a bare 500 the page can only call "Could not
    save it". Anything else is the 500 it always was.
    """
    if _is_locked(exc) and '/saved-searches' in request.path:
        return jsonify({'error': CATALOG_BUSY, 'code': 'catalog_busy'}), 503
    raise exc                    # Flask's own 500 handling, as before


def _search_conn(catalog_id, timeout=30):
    """(conn, db_path) on the catalog's database, with Search's tables, or an error."""
    import search_index
    import saved_searches
    path, err = _calendar_db_path(catalog_id)
    if err:
        return None, None, err
    conn = sqlite3.connect(path, timeout=timeout)
    conn.row_factory = sqlite3.Row
    search_index.ensure_schema(conn)
    saved_searches.ensure_schema(conn)
    return conn, path, None


@app.route('/api/catalogs/<int:catalog_id>/search', methods=['GET'])
def search_catalog(catalog_id):
    """One page of results (search.run). The name index catches up in the
    background when it is behind; until it has, the tail is searched too."""
    import search
    import search_index
    conn, path, err = _search_conn(catalog_id)
    if err:
        return err
    try:
        behind = search_index.behind(conn)
        if behind:
            search_index.catch_up_in_background(path)
        try:
            c = search.parse(request.args.to_dict(), _PHOTO_TYPES)
            limit = max(1, min(search.MAX_PAGE, int(request.args.get('limit', search.PAGE))))
            result = search.run(conn, c, after=request.args.get('after') or None, limit=limit)
        except (search.SearchError, ValueError) as e:
            return jsonify({'error': str(e)}), 400
    finally:
        conn.close()
    for r in result['results']:
        r['thumbnail_url'] = _thumb_url(catalog_id, r['file_id'])
    result['index'] = {'ready': behind == 0, 'behind': behind,
                       'building': search_index.building(path)}
    return jsonify(result)


@app.route('/api/catalogs/<int:catalog_id>/search/index', methods=['POST'])
def rebuild_search_index(catalog_id):
    """Build the name index again, in the background (editor)."""
    import search_index
    conn, path, err = _search_conn(catalog_id)
    if err:
        return err
    conn.close()
    started = search_index.rebuild(path)
    return jsonify({'started': started}), 202


def _saved_search_or_404(conn, sid):
    import saved_searches
    row = saved_searches.get(conn, sid)
    return row


def _search_user():
    user = auth.current_user()
    return {'id': user['id'], 'display_name': user['display_name']} if user is not None else None


def _saved_search_query_ids(conn, query):
    """The catalog files a saved query finds (search.file_ids), capped."""
    import search
    from urllib.parse import parse_qsl
    c = search.parse(dict(parse_qsl(query or '')), _PHOTO_TYPES)
    return search.file_ids(conn, c)


@app.route('/api/catalogs/<int:catalog_id>/saved-searches', methods=['GET'])
def list_saved_searches(catalog_id):
    import saved_searches
    conn, _, err = _search_conn(catalog_id)
    if err:
        return err
    try:
        status = 'done' if request.args.get('status') == 'done' else 'open'
        rows = saved_searches.list_all(conn, status)
    finally:
        conn.close()
    for r in rows:
        r['cover_thumb_url'] = (_thumb_url(catalog_id, r['cover_file_id'])
                                if r.get('cover_file_id') else None)
    return jsonify({'saved_searches': rows, 'idle_days': saved_searches.IDLE_DAYS,
                    'delete_days': saved_searches.DELETE_DAYS})


def _without_excluded(ids, data):
    """ids minus exclude_ids: "all results" with some unticked afterwards."""
    skip = {int(i) for i in (data.get('exclude_ids') or [])}
    return [i for i in ids if i not in skip] if skip else ids


@app.route('/api/catalogs/<int:catalog_id>/saved-searches', methods=['POST'])
def create_saved_search(catalog_id):
    """{name, note, query, file_ids?, add_all?, exclude_ids?} - add_all adds
    what the query finds (up to 10,000) less exclude_ids, file_ids the chosen
    ones."""
    import saved_searches
    import search
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'Give the saved search a name'}), 400
    conn, _, err = _search_conn(catalog_id, timeout=SAVED_SEARCH_WRITE_WAIT)
    if err:
        return err
    try:
        query = (data.get('query') or '').lstrip('?')
        try:
            ids = _without_excluded(_saved_search_query_ids(conn, query), data) \
                if data.get('add_all') else [int(i) for i in (data.get('file_ids') or [])]
        except (search.SearchError, ValueError) as e:
            return jsonify({'error': str(e)}), 400
        sid = saved_searches.create(conn, name, query, data.get('note'), _search_user())
        added, already = saved_searches.add_files(conn, sid, ids, _search_user(), query)
        row = saved_searches.public(saved_searches.get(conn, sid), saved_searches.counts(conn, sid))
    finally:
        conn.close()
    return jsonify({**row, 'added': added, 'already': already}), 201


@app.route('/api/catalogs/<int:catalog_id>/saved-searches/<int:sid>', methods=['GET'])
def get_saved_search(catalog_id, sid):
    """One saved search. ?run=1 records that it was run (a viewer may: it
    changes no photo) - running counts as activity for the idle clock."""
    import saved_searches
    conn, _, err = _search_conn(catalog_id, timeout=SAVED_SEARCH_WRITE_WAIT)
    if err:
        return err
    try:
        row = _saved_search_or_404(conn, sid)
        if row is None:
            return jsonify({'error': 'Saved search not found'}), 404
        if request.args.get('run') == '1':
            t = saved_searches._iso(saved_searches._now())
            conn.execute('UPDATE saved_searches SET last_run_at = ?, last_activity_at = ? WHERE id = ?',
                         (t, t, sid))
            conn.commit()
            row = saved_searches.get(conn, sid)
        out = saved_searches.public(row, saved_searches.counts(conn, sid))
    finally:
        conn.close()
    return jsonify(out)


@app.route('/api/catalogs/<int:catalog_id>/saved-searches/<int:sid>', methods=['PATCH'])
def update_saved_search(catalog_id, sid):
    """{name, note, query, status} - and {keep: true} restarts the idle clock."""
    import saved_searches
    data = request.get_json(silent=True) or {}
    conn, _, err = _search_conn(catalog_id, timeout=SAVED_SEARCH_WRITE_WAIT)
    if err:
        return err
    try:
        if _saved_search_or_404(conn, sid) is None:
            return jsonify({'error': 'Saved search not found'}), 404
        fields = {}
        if 'name' in data:
            if not (data['name'] or '').strip():
                return jsonify({'error': 'A saved search needs a name'}), 400
            fields['name'] = data['name'].strip()
        if 'note' in data:
            fields['note'] = (data['note'] or '').strip() or None
        if 'query' in data:
            fields['query'] = (data['query'] or '').lstrip('?')
        if 'status' in data:
            if data['status'] not in ('open', 'done'):
                return jsonify({'error': 'status is open or done'}), 400
            fields['status'] = data['status']
        if fields:
            conn.execute(f'UPDATE saved_searches SET {", ".join(f"{k} = ?" for k in fields)} WHERE id = ?',
                         (*fields.values(), sid))
        saved_searches.touch(conn, sid)
        conn.commit()
        out = saved_searches.public(saved_searches.get(conn, sid), saved_searches.counts(conn, sid))
    finally:
        conn.close()
    return jsonify(out)


@app.route('/api/catalogs/<int:catalog_id>/saved-searches/<int:sid>', methods=['DELETE'])
def delete_saved_search(catalog_id, sid):
    """The saved search and its list - never its photos or videos."""
    import saved_searches
    conn, _, err = _search_conn(catalog_id, timeout=SAVED_SEARCH_WRITE_WAIT)
    if err:
        return err
    try:
        if _saved_search_or_404(conn, sid) is None:
            return jsonify({'error': 'Saved search not found'}), 404
        saved_searches.delete(conn, sid)
    finally:
        conn.close()
    return jsonify({'deleted': sid})


@app.route('/api/catalogs/<int:catalog_id>/saved-searches/<int:sid>/files', methods=['GET'])
def saved_search_files(catalog_id, sid):
    import saved_searches
    try:
        limit = max(1, min(200, int(request.args.get('limit', 60))))
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify({'error': 'limit and offset must be numbers'}), 400
    conn, _, err = _search_conn(catalog_id)
    if err:
        return err
    try:
        if _saved_search_or_404(conn, sid) is None:
            return jsonify({'error': 'Saved search not found'}), 404
        files, total = saved_searches.files_page(conn, sid, limit, offset)
    finally:
        conn.close()
    for f in files:
        f['thumbnail_url'] = _thumb_url(catalog_id, f['file_id'])
    return jsonify({'files': files, 'total': total, 'limit': limit, 'offset': offset})


@app.route('/api/catalogs/<int:catalog_id>/saved-searches/<int:sid>/files', methods=['POST'])
def add_saved_search_files(catalog_id, sid):
    """{file_ids: [...]} or {query: "...", exclude_ids?: [...]} - the query's
    results, up to 10,000 per request, so "all N results" never travels as N
    ids; exclude_ids are the ones unticked after selecting all."""
    import saved_searches
    import search
    data = request.get_json(silent=True) or {}
    conn, _, err = _search_conn(catalog_id, timeout=SAVED_SEARCH_WRITE_WAIT)
    if err:
        return err
    try:
        if _saved_search_or_404(conn, sid) is None:
            return jsonify({'error': 'Saved search not found'}), 404
        try:
            if 'query' in data:
                query = (data.get('query') or '').lstrip('?')
                ids = _saved_search_query_ids(conn, query)
                capped = len(ids) >= search.ADD_CAP
                ids = _without_excluded(ids, data)
            else:
                query = data.get('added_from')
                ids = [int(i) for i in (data.get('file_ids') or [])]
                capped = len(ids) >= search.ADD_CAP
        except (search.SearchError, ValueError) as e:
            return jsonify({'error': str(e)}), 400
        added, already = saved_searches.add_files(conn, sid, ids, _search_user(), query)
    finally:
        conn.close()
    return jsonify({'added': added, 'already': already, 'capped': capped})


@app.route('/api/catalogs/<int:catalog_id>/saved-searches/<int:sid>/files', methods=['DELETE'])
def remove_saved_search_files(catalog_id, sid):
    import saved_searches
    data = request.get_json(silent=True) or {}
    try:
        ids = [int(i) for i in (data.get('file_ids') or [])]
    except ValueError:
        return jsonify({'error': 'file_ids must be numbers'}), 400
    if not ids:
        return jsonify({'error': 'Choose files to remove'}), 400
    conn, _, err = _search_conn(catalog_id, timeout=SAVED_SEARCH_WRITE_WAIT)
    if err:
        return err
    try:
        if _saved_search_or_404(conn, sid) is None:
            return jsonify({'error': 'Saved search not found'}), 404
        removed = saved_searches.remove_files(conn, sid, ids)
    finally:
        conn.close()
    return jsonify({'removed': removed})


def _saved_search_sweep_once():
    """Delete saved searches idle for 60 days, in every catalog that has them."""
    import saved_searches
    for catalog in Catalog.get_all():
        if not catalog.target_directory:
            continue
        path = os.path.join(catalog.target_directory, 'workflow_results.db')
        if not os.path.exists(path):
            continue
        try:
            conn = sqlite3.connect(path, timeout=30)
            try:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'saved_searches'").fetchone():
                    gone = saved_searches.sweep(conn)
                    if gone:
                        print(f'[saved searches] {catalog.name}: deleted idle {gone}')
            finally:
                conn.close()
        except Exception as e:
            print(f'[saved searches] sweep failed for {catalog.name}: {e}')


_saved_search_sweeper = None


def _start_saved_search_sweeper():
    """Every six hours; off wherever the workflow scheduler is off."""
    global _saved_search_sweeper
    if os.environ.get('DISABLE_WORKFLOW_SCHEDULER', '').lower() in ('1', 'true', 'yes'):
        return None
    if _saved_search_sweeper is not None:
        return _saved_search_sweeper

    def _loop():
        while True:
            try:
                _saved_search_sweep_once()
            except Exception as e:
                print(f'[saved searches] sweep failed: {e}')
            time.sleep(6 * 3600)
    _saved_search_sweeper = threading.Thread(target=_loop, daemon=True, name='saved-search-sweep')
    _saved_search_sweeper.start()
    return _saved_search_sweeper


_start_saved_search_sweeper()

# Photo edits whose rebuild a restart cut short (basic-photo-editing.md 5.3).
if os.environ.get('DISABLE_WORKFLOW_SCHEDULER', '').lower() not in ('1', 'true', 'yes'):
    threading.Thread(target=_resume_photo_edits, daemon=True, name='photo-edit-resume').start()


@app.route('/api/catalogs/<int:catalog_id>/calendar/refresh', methods=['POST'])
def refresh_calendar_counts(catalog_id):
    """Count now (the Dashboard's Refresh stats, for the calendar)."""
    import calendar_counts
    path, err = _calendar_db_path(catalog_id)
    if err:
        return err
    return jsonify(calendar_counts.public({**calendar_counts.rebuild(path, _PHOTO_TYPES),
                                           'stale': False}))


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/cover', methods=['PUT'])
def set_event_cover(catalog_id, event_id):
    """Set the photo shown on this event's card."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.json or {}
    file_id = data.get('file_id')
    if not file_id:
        return jsonify({'error': 'file_id is required'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    if not event_model.get_event_by_id(event_id):
        return jsonify({'error': 'Event not found'}), 404

    # set_cover refuses a file that is not in this event. That check is what
    # stops an event being given a cover from another event, or from a catalog
    # the caller cannot read.
    if not event_model.set_cover(event_id, int(file_id)):
        return jsonify({'error': 'File does not belong to this event'}), 400

    return jsonify({
        'cover_file_id': int(file_id),
        'cover_thumb_url': _thumb_url(catalog_id, int(file_id)),
    })


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/cover', methods=['DELETE'])
def clear_event_cover(catalog_id, event_id):
    """Clear the cover. The next read rolls a fresh one and stores it."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    if not event_model.get_event_by_id(event_id):
        return jsonify({'error': 'Event not found'}), 404

    event_model.clear_cover(event_id)
    return jsonify({'message': 'Event cover cleared'})


@app.route('/api/catalogs/<int:catalog_id>/events', methods=['POST'])
def create_event(catalog_id):
    """Create a new event in a catalog"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.json
    if not data or not data.get('name'):
        return jsonify({'error': 'Name is required'}), 400
    if not data.get('start_time'):
        return jsonify({'error': 'start_time is required'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    event_id = event_model.create_event(
        name=data['name'],
        start_time=data['start_time'],
        end_time=data.get('end_time', data['start_time']),
        latitude=data.get('latitude'),
        longitude=data.get('longitude'),
        description=data.get('description'),
        file_count=data.get('file_count', 0),
    )
    return jsonify({'id': event_id, 'message': 'Event created successfully'}), 201


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>', methods=['GET'])
def get_event(catalog_id, event_id):
    """Get a specific event"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    event = event_model.get_event_by_id(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404
    return jsonify(event)


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>', methods=['PUT'])
def update_event(catalog_id, event_id):
    """Update an event"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    event = event_model.get_event_by_id(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404

    data = request.json
    if data:
        # Mark the event as manually edited whenever it is updated by the user
        data['type'] = 'Manual'
        event_model.update_event(event_id, **data)
    return jsonify({'message': 'Event updated successfully'})


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>', methods=['DELETE'])
def delete_event(catalog_id, event_id):
    """Delete an event"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    event = event_model.get_event_by_id(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404

    event_model.delete_event(event_id)
    return jsonify({'message': 'Event deleted successfully'})


# ─── Merging events ───────────────────────────────────────────────────────────
# Automatic grouping sometimes splits one occasion in two - a gap in the
# shooting, a camera whose clock was off. Merging moves one event's photos into
# another and removes the first. Only events that are close in time qualify:
# the same day, the day before, or the day after - by the events' DATES, so an
# event spanning several days qualifies against anything within a day of it.
#
# The event merged INTO keeps everything that is its own - name, description,
# cover, locations - and only widens its time window to hold the photos it
# receives. It becomes Manual, as any user edit makes an event (update_event):
# Create Events deletes and rebuilds every Auto event, which would otherwise
# split the merged photos apart again on its next run.

def _event_day(value):
    """The calendar date of a stored event time, whichever format it was written in."""
    from datetime import date as _date
    try:
        return _date.fromisoformat(str(value or '')[:10])
    except ValueError:
        return None


def _merge_candidates(conn, event):
    """Other events whose dates come within a day of this one's, nearest first."""
    from datetime import timedelta
    start, end = _event_day(event['start_time']), _event_day(event['end_time'])
    if start is None:
        return []
    end = end or start
    lo, hi = start - timedelta(days=1), end + timedelta(days=1)
    rows = conn.execute('''
        SELECT e.id, e.name, e.start_time, e.end_time, e.type, e.cover_file_id,
               (SELECT COUNT(*) FROM event_files ef
                  JOIN file_contents fc ON fc.id = ef.file_id
                 WHERE ef.event_id = e.id AND fc.removed_at IS NULL) AS photos
        FROM events e
        WHERE e.id <> ?
          AND substr(e.start_time, 1, 10) <= ?
          AND substr(COALESCE(e.end_time, e.start_time), 1, 10) >= ?
    ''', (event['id'], hi.isoformat(), lo.isoformat())).fetchall()
    out = []
    for r in rows:
        c_start = _event_day(r['start_time'])
        c_end = _event_day(r['end_time']) or c_start
        if c_start is None:
            continue
        if c_end < start:
            relation = 'day before'
        elif c_start > end:
            relation = 'day after'
        else:
            relation = 'same day'
        out.append({**dict(r), 'relation': relation})
    order = {'same day': 0, 'day before': 1, 'day after': 2}
    out.sort(key=lambda c: (order[c['relation']], str(c['start_time'])))
    return out


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/merge-candidates', methods=['GET'])
def get_event_merge_candidates(catalog_id, event_id):
    """The events this one can be merged into (see the section comment above)."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        event = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
        if not event:
            return jsonify({'error': 'Event not found'}), 404
        return jsonify(_merge_candidates(conn, event))
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/merge', methods=['POST'])
def merge_event(catalog_id, event_id):
    """
    Move this event's photos into another event, then delete this one.

    Body: {"into_event_id": <id>}. The target must be one of this event's merge
    candidates. One transaction: either everything moves or nothing does.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    target_id = (request.get_json(silent=True) or {}).get('into_event_id')
    if not isinstance(target_id, int):
        return jsonify({'error': 'into_event_id must be an event id'}), 400

    conn = CatalogDatabase(catalog.target_directory).get_connection()
    try:
        source = conn.execute('SELECT * FROM events WHERE id = ?', (event_id,)).fetchone()
        if not source:
            return jsonify({'error': 'Event not found'}), 404
        if target_id not in {c['id'] for c in _merge_candidates(conn, source)}:
            return jsonify({'error': 'Only an event on the same day, the day before or '
                                     'the day after can be merged into'}), 400
        target = conn.execute('SELECT * FROM events WHERE id = ?', (target_id,)).fetchone()

        moved = conn.execute('''
            INSERT OR IGNORE INTO event_files (event_id, file_id)
            SELECT ?, file_id FROM event_files WHERE event_id = ?
        ''', (target_id, event_id)).rowcount
        conn.execute('DELETE FROM event_files WHERE event_id = ?', (event_id,))

        # The target's own information stays; only its window widens to hold
        # what it received. Stored times use either separator, so compare them
        # normalised and store the target's own format.
        def _norm(v):
            return str(v or '').replace('T', ' ')[:19]
        starts = [t for t in (_norm(target['start_time']), _norm(source['start_time'])) if t]
        ends = [t for t in (_norm(target['end_time']), _norm(source['end_time'])) if t]
        count = conn.execute(
            'SELECT COUNT(*) FROM event_files ef JOIN file_contents fc ON fc.id = ef.file_id '
            'WHERE ef.event_id = ? AND fc.removed_at IS NULL', (target_id,)).fetchone()[0]
        conn.execute('''
            UPDATE events SET start_time = ?, end_time = ?, file_count = ?, type = 'Manual',
                              updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (min(starts) if starts else target['start_time'],
              max(ends) if ends else target['end_time'], count, target_id))

        # Nothing that names an event cascades (foreign keys are off). An album
        # section built from the merged event now follows the target - unless
        # that album already has a section for the target, where it is simply
        # detached and keeps the photos it holds.
        conn.execute('DELETE FROM location_events WHERE event_id = ?', (event_id,))
        _sqlite_try(conn.cursor(), '''
            UPDATE album_sections SET source_event_id = ?
            WHERE source_event_id = ?
              AND album_id NOT IN (SELECT album_id FROM album_sections WHERE source_event_id = ?)
        ''', (target_id, event_id, target_id))
        _sqlite_try(conn.cursor(),
                    'UPDATE album_sections SET source_event_id = NULL WHERE source_event_id = ?',
                    (event_id,))
        _sqlite_try(conn.cursor(),
                    'UPDATE face_similarity_scores_audit SET source_event_id = ? '
                    'WHERE source_event_id = ?', (target_id, event_id))
        conn.execute('DELETE FROM events WHERE id = ?', (event_id,))
        conn.commit()
        return jsonify({'merged_event_id': event_id, 'into_event_id': target_id,
                        'photos_moved': moved, 'photo_count': count})
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


import re as _re_mod  # noqa: E402

_CATALOG_NAME_PREFIX = _re_mod.compile(r'^\d{14}-[0-9a-fA-F]{10}-')
_filename_date_parser = None


def _filename_datetime(file_name):
    """
    The date in a file's original name, as ('YYYY-MM-DDTHH:MM:SS', has_time),
    or (None, False). The catalog's own '<14 digits>-<10 hex>-' prefix is the
    import's date, not the name's, so it is stripped first.

    The parser is the import's own (extract_date_from_filename), imported
    rather than copied so the suggestion and the import cannot disagree.
    Computed on read: it is cheap, and its rules change.
    """
    global _filename_date_parser
    if not file_name:
        return None, False
    if _filename_date_parser is None:
        workflows_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'workflows')
        if workflows_dir not in sys.path:
            sys.path.insert(0, workflows_dir)
        from io_picture_import_workflow import extract_date_from_filename
        _filename_date_parser = extract_date_from_filename
    found = _filename_date_parser(_CATALOG_NAME_PREFIX.sub('', file_name, count=1))
    if not found:
        return None, False
    has_time = found.get('hour') is not None
    try:
        dt = _datetime(int(found['year']), int(found['month']), int(found['day'] or 1),
                      int(found['hour']) if has_time else 12,
                      int(found['minute']) if has_time else 0,
                      int(found.get('second') or 0) if has_time else 0)
    except (ValueError, TypeError):
        return None, False
    return dt.strftime('%Y-%m-%dT%H:%M:%S'), has_time


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/files', methods=['GET'])
def get_event_files(catalog_id, event_id):
    """Get all files associated with an event"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    event = event_model.get_event_by_id(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404

    files = event_model.get_files_for_event(event_id)
    
    # Resolve stored paths to absolute and fetch alternative_file_path from alternative_versions
    conn = catalog_db.get_connection()
    cursor = conn.cursor()
    for file in files:
        # Resolve file_path (may be relative in DB) to absolute
        file['file_path'] = _resolve_path(file.get('file_path'), _original_root(catalog))
        
        # Fetch alternative_file_path from alternative_versions table.
        # Video proxies join the same lookup (see video-spec.md section 12): a
        # video's playable derivative is an alternative version like any other.
        cursor.execute('''
            SELECT alternative_file_path, characteristic
            FROM alternative_versions
            WHERE original_file_id = ?
              AND rung IN ('1200','1600','2048','2560','video_proxy')
            ORDER BY (rung = 'video_proxy') ASC,
                     (width >= 1200) DESC,
                     CASE WHEN width >= 1200 THEN width END ASC,
                     width DESC, id ASC
            LIMIT 1
        ''', (file['id'],))
        alt_row = cursor.fetchone()
        if alt_row and alt_row['alternative_file_path']:
            file['alternative_file_path'] = _resolve_path(
                alt_row['alternative_file_path'], catalog.target_directory
            )
            file['is_video_proxy'] = 'VIDEO PROXY' in (alt_row['characteristic'] or '').upper()
        else:
            file['alternative_file_path'] = None
            file['is_video_proxy'] = False

        # is_video is NOT the same question as is_video_proxy. A video whose
        # source already streams in a browser takes the passthrough branch and
        # has no proxy row at all, but the frontend still has to render <video>
        # rather than <img>. Derived from the shared extension set so the two
        # ends cannot disagree about what a video is.
        file['is_video'] = is_video_file(file.get('file_name') or '')

        # The date the import's file-name parser reads in the ORIGINAL name -
        # the date editor's "Looks like …" suggestion
        # (mass-edit-event-photo-dates-spec.md section 5.6).
        file['filename_datetime'], file['filename_has_time'] = \
            _filename_datetime(file.get('file_name'))
    
    conn.close()
    return jsonify(files)


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/people', methods=['GET'])
def get_event_people(catalog_id, event_id):
    """Get all people tagged in photos associated with an event"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    event = event_model.get_event_by_id(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404

    people = event_model.get_people_for_event(event_id)
    return jsonify(people)


# ─── Locations API ────────────────────────────────────────────────────────────

# ── Static map snapshots ──────────────────────────────────────────────────────
# One renderer, used by a named location and by a proposed one. It was inline in
# update_location, which meant a cluster could not have a map at all - and the
# card that shows them is the same card now, so one of the two would have been
# conspicuously blank.

MAP_SNAPSHOT_ZOOM = 13
MAP_TILE_SIZE = 256
MAP_OUTPUT_SIZE = 512


def _deg2tile(lat_deg, lon_deg, zoom):
    import math
    n = 2 ** zoom
    x = int((lon_deg + 180.0) / 360.0 * n)
    y = int((1.0 - math.asinh(math.tan(math.radians(lat_deg))) / math.pi) / 2.0 * n)
    return x, y


def _deg2pixel_offset(lat_deg, lon_deg, zoom, tile_x, tile_y):
    """Pixel offset of a coordinate within a tile grid whose top-left is tile_x/y."""
    import math
    n = 2 ** zoom
    px = (lon_deg + 180.0) / 360.0 * n * MAP_TILE_SIZE - tile_x * MAP_TILE_SIZE
    py = ((1.0 - math.asinh(math.tan(math.radians(lat_deg))) / math.pi) / 2.0
          * n * MAP_TILE_SIZE - tile_y * MAP_TILE_SIZE)
    return int(px), int(py)


def _render_map_snapshot(lat, lon, out_path):
    """
    Write a 512x512 JPEG centred on (lat, lon) from OpenStreetMap tiles.

    A 3x3 grid is fetched and a centred square cropped out of it, because a
    single tile only contains the point somewhere - usually not the middle.
    A tile that fails to load is left blank rather than failing the snapshot:
    a map missing one square is more useful than no map.

    Returns the path on success. Raises on anything that leaves no image.
    """
    import requests as http_requests
    from PIL import Image
    import io as _io

    zoom = MAP_SNAPSHOT_ZOOM
    tile_x, tile_y = _deg2tile(lat, lon, zoom)
    grid, half = 3, 1
    canvas_size = grid * MAP_TILE_SIZE
    canvas = Image.new('RGB', (canvas_size, canvas_size), (238, 238, 238))

    # Named per OSM's tile usage policy, which asks for an identifying agent.
    headers = {'User-Agent': 'AirlockPhotos/1.0 (location map snapshot)'}
    for dy in range(-half, half + 1):
        for dx in range(-half, half + 1):
            url = (f'https://tile.openstreetmap.org/{zoom}/'
                   f'{tile_x + dx}/{tile_y + dy}.png')
            try:
                resp = http_requests.get(url, headers=headers, timeout=10)
                if resp.status_code == 200:
                    tile = Image.open(_io.BytesIO(resp.content)).convert('RGB')
                    canvas.paste(tile, ((dx + half) * MAP_TILE_SIZE,
                                        (dy + half) * MAP_TILE_SIZE))
            except Exception:
                pass

    cx, cy = _deg2pixel_offset(lat, lon, zoom, tile_x - half, tile_y - half)
    half_out = MAP_OUTPUT_SIZE // 2
    left = min(max(0, cx - half_out), canvas_size - MAP_OUTPUT_SIZE)
    top = min(max(0, cy - half_out), canvas_size - MAP_OUTPUT_SIZE)
    cropped = canvas.crop((left, top, left + MAP_OUTPUT_SIZE, top + MAP_OUTPUT_SIZE))

    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    cropped.save(out_path, 'JPEG', quality=90)
    return out_path


def _map_snapshot_path(catalog, kind, row_id):
    """Where a snapshot lives: cache/, beside the other derived images."""
    return os.path.join(catalog.target_directory, 'cache',
                        f'location_map_{kind}_{row_id}.jpg')


def _body_coord(data, canonical, legacy):
    """A coordinate from a request body under either spelling, canonical first.

    Named _body_coord, not _coord: there is already a local _coord(value) nested
    inside the user-settings route that parses a single value. Two functions of
    that name with different signatures in one file is a trap even when scoping
    keeps them apart.

    Input tolerance only - see create_location for why the old names are still
    read here and never written back.
    """
    if not data:
        return None
    value = data.get(canonical)
    return data.get(legacy) if value is None else value


@app.route('/api/catalogs/<int:catalog_id>/locations', methods=['GET'])
def get_locations(catalog_id):
    """Get all locations in a catalog"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    location_model = LocationModel(catalog_db)
    # ?confirmed=1 is what the event-tagging picker asks for. The clustering
    # workflow writes an unconfirmed location per cluster - over a thousand on
    # a real catalog - and offering those when somebody wants to tag one event
    # would bury the handful they have actually named.
    confirmed_only = str(request.args.get('confirmed', '')).lower() in ('1', 'true', 'yes')

    # ?limit=&offset= pages it, for the Locations page.
    #
    # Not because the JSON is large - 1,041 locations is 337 KB in 0.27 s on
    # `family` - but because every card drawn fetches a MAP TILE and a
    # thumbnail. The whole list is a thousand cards and some two thousand image
    # requests on one click, and that is what makes the page slow. A page of
    # cards is a page of requests.
    #
    # Without a limit this still returns a plain array of everything, which is
    # what the event-tagging picker asks for with ?confirmed=1 and what every
    # older bundle expects.
    raw_limit = request.args.get('limit')
    if raw_limit is None:
        return jsonify(location_model.get_all_locations(
            confirmed_only=confirmed_only))

    try:
        limit = max(1, min(500, int(raw_limit)))
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify({'error': 'limit and offset must be numbers'}), 400

    # The shape is an OBJECT when paged and an ARRAY when not, so a caller
    # cannot accidentally read a page as if it were the whole list - which is
    # the failure that would show a thousand locations as eighteen and look
    # like data loss.
    return jsonify({
        'locations': location_model.get_all_locations(
            confirmed_only=confirmed_only, limit=limit, offset=offset),
        'total': location_model.count_locations(confirmed_only=confirmed_only),
        'limit': limit,
        'offset': offset,
    })


@app.route('/api/catalogs/<int:catalog_id>/locations', methods=['POST'])
def create_location(catalog_id):
    """Create a new location in a catalog"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.json
    if not data or not data.get('name'):
        return jsonify({'error': 'Name is required'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    location_model = LocationModel(catalog_db)
    # 'latitude'/'longitude' is the spelling everywhere now (location-spec.md
    # 9.3). 'lat'/'long' is still ACCEPTED on input, and only on input: a
    # frontend bundle is built and deployed separately from the backend, so a
    # bundle that lags by one release would otherwise post coordinates the
    # backend silently discarded. Responses carry the canonical names only.
    location_id = location_model.create_location(
        name=data['name'],
        latitude=_body_coord(data, 'latitude', 'lat'),
        longitude=_body_coord(data, 'longitude', 'long'),
        description=data.get('description'),
        source=data.get('source'),
    )
    return jsonify({'id': location_id, 'message': 'Location created successfully'}), 201


@app.route('/api/catalogs/<int:catalog_id>/locations/<int:location_id>', methods=['GET'])
def get_location(catalog_id, location_id):
    """Get a specific location"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    location_model = LocationModel(catalog_db)
    location = location_model.get_location_by_id(location_id)
    if not location:
        return jsonify({'error': 'Location not found'}), 404
    return jsonify(location)


@app.route('/api/catalogs/<int:catalog_id>/locations/<int:location_id>', methods=['PUT'])
def update_location(catalog_id, location_id):
    """Update a location. When lat/long are provided, generates a static map
    snapshot image and saves it to tmp in the catalog target directory."""
    import requests as http_requests
    from PIL import Image
    import io

    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    location_model = LocationModel(catalog_db)
    location = location_model.get_location_by_id(location_id)
    if not location:
        return jsonify({'error': 'Location not found'}), 404

    data = request.json
    if data:
        # Normalise the old spelling before it reaches the model, which now
        # knows only 'latitude'/'longitude' (location-spec.md 9.3).
        fields = {k: v for k, v in data.items() if k not in ('lat', 'long')}
        # Naming an unconfirmed location confirms it. Someone who edits the
        # geocoder's guess has plainly decided this is a location, and asking
        # them to then press Confirm would be asking twice.
        if (fields.get('name') or '').strip() and 'confirmed' not in fields:
            fields['confirmed'] = 1
        if 'lat' in data:
            fields['latitude'] = data['lat']
        if 'long' in data:
            fields['longitude'] = data['long']
        location_model.update_location(location_id, **fields)

    # Generate map snapshot image when a coordinate is present
    lat = _body_coord(data, 'latitude', 'lat') if data else None
    long = _body_coord(data, 'longitude', 'long') if data else None

    # Fall back to existing location coords if not provided in this update
    if lat is None:
        lat = location.get('latitude')
    if long is None:
        long = location.get('longitude')

    if lat is not None and long is not None:
        try:
            out = _map_snapshot_path(catalog, 'location', location_id)
            _render_map_snapshot(lat, long, out)
            # Stored relative to target_directory so the catalog stays portable.
            location_model.update_location(
                location_id,
                map_image_path=os.path.relpath(out, catalog.target_directory))
        except Exception as e:
            # The location itself was updated; only its picture failed.
            return jsonify({'message': 'Location updated (map image generation failed)',
                            'error': str(e)})

    return jsonify({'message': 'Location updated successfully'})


# ── The location map ──────────────────────────────────────────────────────────
# documentation/specs/location-map-spec.md.
#
# Which decimal place to round a coordinate to, per Leaflet zoom level. Measured
# on the family catalog's 1,041 locations:
#
#   0 dp  ~111 km cells    88 groups
#   1 dp  ~11 km          256 groups
#   2 dp  ~1.1 km         850 groups
#   3 dp  ~111 m        1,041 groups - every location already separate
#
# So the payload is bounded between 88 and 1,041 markers however large a catalog
# grows, and there is no need for a distinct "individual markers" mode: at 3 dp
# the grouping is one-to-one here, and where it is not, two locations 50 m apart
# are a reasonable thing to show as one marker.
MAP_ZOOM_ROUNDING = ((5, 0), (8, 1), (11, 2))      # (max zoom, decimal places)
MAP_ROUNDING_MAX = 3
# The busiest group would otherwise carry 1,838 events' worth of identifiers
# into a payload nothing reads at that zoom.
MAP_IDS_PER_GROUP = 50


def _map_rounding(zoom):
    """Decimal places for a zoom level, clamped rather than refused."""
    try:
        z = int(zoom)
    except (TypeError, ValueError):
        z = 2
    for max_zoom, places in MAP_ZOOM_ROUNDING:
        if z <= max_zoom:
            return places
    return MAP_ROUNDING_MAX


@app.route('/api/version', methods=['GET'])
def get_version():
    """
    What build is this.

    Requires a session, like every other /api route: auth.py denies by default
    and this path is deliberately NOT in PUBLIC_PATHS. The chrome that shows it
    only renders for a signed-in user, so nothing needs it earlier than that.

    Add '/api/version' to auth.PUBLIC_PATHS if a health check ever has to read
    the version without credentials. That is a deliberate disclosure - a release
    number and a commit, to anyone who can reach the port - not an oversight to
    be fixed in passing.

    `license` is who this copy is licensed to (airlock_license.py): shown in the
    version badge's tooltip, and read by nothing else - a notice, never a lock.
    """
    info = airlock_version.version_info()
    try:
        info['license'] = airlock_license.status()
    except Exception:                      # never let the notice break the version
        info['license'] = {'status': 'unlicensed'}
    return jsonify(info)


@app.route('/api/catalogs/<int:catalog_id>/locations/map', methods=['GET'])
def get_locations_map(catalog_id):
    """
    Every location in the catalog, grouped for a map, with how much happened at
    each.

    A marker's count comes through the FILES, not through location_events.
    There are two paths from a location to an event and the obvious one is
    empty: location_events is the hand-tagging path, 0 rows on both real
    catalogs, and every location on this page was made by the clustering
    workflow, which does not touch it. A map built on it would show 1,041
    markers all reading zero - and would look entirely correct. See
    location-map-spec.md section 3.

    `from`/`to` filter the FILES, which is the only thing here with a date, so
    they change which locations appear at all and not only their counts: 2011
    has 1 location in this catalog and 2024 has 235.

    `years` is deliberately NOT filtered by them. The slider needs the whole
    range it can select, including the years the current filter excludes;
    computing it from the filtered set would move the slider's own extent as it
    was dragged.
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    places = _map_rounding(request.args.get('zoom'))
    try:
        year_from = request.args.get('from', type=int)
        year_to = request.args.get('to', type=int)
    except (TypeError, ValueError):
        year_from = year_to = None

    conn = catalog_db.get_connection()
    cur = conn.cursor()

    # The year predicate sits on the file_contents JOIN, not in a WHERE: a WHERE
    # would turn the LEFT JOIN into an inner one and drop the locations that
    # have no file in range - which is right when filtering and wrong when not.
    # event_files then joins on fc.id rather than fl.file_id, so the events
    # counted follow the filtered files instead of all of them.
    taken = "fc.master_datetime"
    join_extra, params = '', []
    if year_from is not None:
        join_extra += f' AND CAST(SUBSTR({taken}, 1, 4) AS INTEGER) >= ?'
        params.append(year_from)
    if year_to is not None:
        join_extra += f' AND CAST(SUBSTR({taken}, 1, 4) AS INTEGER) <= ?'
        params.append(year_to)
    # Filtering means "only locations with something in these years"; not
    # filtering keeps the 25 locations that have photos and no event, because a
    # location with no event is a real thing and hiding it would make this map
    # disagree with the Locations page.
    having = 'HAVING COUNT(DISTINCT fc.id) > 0' if join_extra else ''

    groups = []
    totals = {'locations': 0, 'events': 0, 'files': 0}
    try:
        cur.execute(f"""
            WITH loc AS (
                SELECT l.id, l.name, l.latitude, l.longitude, l.confirmed,
                       COUNT(DISTINCT ef.event_id) AS events,
                       COUNT(DISTINCT fc.id)       AS files
                  FROM locations l
                  LEFT JOIN file_locations fl ON fl.location_id = l.id
                  LEFT JOIN file_contents fc ON fc.id = fl.file_id
                                             AND fc.removed_at IS NULL{join_extra}
                  LEFT JOIN event_files ef ON ef.file_id = fc.id
                 WHERE l.latitude IS NOT NULL AND l.longitude IS NOT NULL
                 GROUP BY l.id
                 {having}
            )
            SELECT COUNT(*)            AS locations,
                   AVG(latitude)       AS lat,
                   AVG(longitude)      AS lng,
                   SUM(events)         AS events,
                   SUM(files)          AS files,
                   SUM(confirmed)      AS verified,
                   MIN(name)           AS name,
                   GROUP_CONCAT(id)    AS ids
              FROM loc
             GROUP BY ROUND(latitude, ?), ROUND(longitude, ?)
             ORDER BY events DESC, locations DESC
        """, params + [places, places])
        for r in cur.fetchall():
            ids = [int(x) for x in (r['ids'] or '').split(',') if x]
            groups.append({
                # The MEAN of the members, not the rounded grid point, so a
                # marker lands on the places rather than on a lattice.
                'lat': r['lat'], 'lng': r['lng'],
                'locations': r['locations'],
                'events': r['events'] or 0,
                'files': r['files'] or 0,
                'verified': r['verified'] or 0,
                # Only meaningful for a group of one; the page shows the count
                # otherwise.
                'name': r['name'] if r['locations'] == 1 else None,
                'ids': ids[:MAP_IDS_PER_GROUP],
            })
            totals['locations'] += r['locations']
            totals['events'] += r['events'] or 0
            totals['files'] += r['files'] or 0
    except sqlite3.Error as exc:
        # A catalog that predates file_locations has no map to draw rather than
        # an error to report.
        _logging.getLogger(__name__).warning('location map query failed: %s', exc)

    # Locations per year, over everything. Guarded against a year that is not a
    # year: master_datetime is text and a bad import can leave anything in it.
    years, counts = {}, {}
    try:
        cur.execute(f"""
            SELECT CAST(SUBSTR({taken}, 1, 4) AS INTEGER) AS y,
                   COUNT(DISTINCT fl.location_id) AS locations
              FROM file_locations fl
              JOIN file_contents fc ON fc.id = fl.file_id
             WHERE fc.removed_at IS NULL
               AND CAST(SUBSTR({taken}, 1, 4) AS INTEGER) BETWEEN 1826 AND 2200
             GROUP BY y ORDER BY y
        """)
        for y, n in cur.fetchall():
            counts[str(y)] = n
        if counts:
            ys = [int(k) for k in counts]
            years = {'min': min(ys), 'max': max(ys), 'counts': counts}
    except sqlite3.Error:
        pass
    conn.close()

    return jsonify({
        'groups': groups,
        'years': years or {'min': None, 'max': None, 'counts': {}},
        'zoom': request.args.get('zoom', type=int),
        'round_to': places,
        # The totals OF WHAT WAS RETURNED, so they match the markers on screen
        # and so a test can assert the groups sum to them.
        'total': totals,
    })


@app.route('/api/catalogs/<int:catalog_id>/locations/<int:location_id>/confirm',
           methods=['POST'])
def confirm_location(catalog_id, location_id):
    """
    Confirm a location, optionally renaming it as you go.

    Body: {} to accept the name it has, or {"name": "Abuela's house"} to change
    it. Both are the same act - a person has looked at this and says it is a
    location - which is why there is one endpoint and not two.

    Confirming is what makes a location offerable when tagging an event, and it
    is reversible: POST to .../unconfirm.
    """
    model, err = _location_model(catalog_id)
    if err:
        return err
    if not model.get_location_by_id(location_id):
        return jsonify({'error': 'Location not found'}), 404

    data = request.json or {}
    fields = {'confirmed': 1}
    name = (data.get('name') or '').strip()
    if name:
        fields['name'] = name
    model.update_location(location_id, **fields)
    return jsonify({'location': model.get_location_by_id(location_id)})


@app.route('/api/catalogs/<int:catalog_id>/locations/<int:location_id>/unconfirm',
           methods=['POST'])
def unconfirm_location(catalog_id, location_id):
    """Put a location back to unconfirmed, so it leaves the event picker."""
    model, err = _location_model(catalog_id)
    if err:
        return err
    if not model.get_location_by_id(location_id):
        return jsonify({'error': 'Location not found'}), 404
    model.update_location(location_id, confirmed=0)
    return jsonify({'location': model.get_location_by_id(location_id)})


@app.route('/api/catalogs/<int:catalog_id>/locations/<int:location_id>/map-image', methods=['GET'])
def get_location_map_image(catalog_id, location_id):
    """Serve the static map snapshot image for a location"""
    from flask import send_file

    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    location_model = LocationModel(catalog_db)
    location = location_model.get_location_by_id(location_id)
    if not location:
        return jsonify({'error': 'Location not found'}), 404

    map_image_path = _resolve_path(location.get('map_image_path'), catalog.target_directory)
    if not map_image_path or not os.path.exists(map_image_path):
        # Rendered on demand rather than 404'd. The snapshot used to be written
        # only by update_location, so a location created and never edited - and
        # every location promoted from a cluster - had no map at all, which the
        # card that asks for one has no way to distinguish from a broken image.
        lat, lon = location.get('latitude'), location.get('longitude')
        if lat is None or lon is None:
            return jsonify({'error': 'Location has no coordinates'}), 404
        try:
            map_image_path = _render_map_snapshot(
                lat, lon, _map_snapshot_path(catalog, 'location', location_id))
            location_model.update_location(
                location_id,
                map_image_path=os.path.relpath(map_image_path,
                                               catalog.target_directory))
        except Exception as exc:
            return jsonify({'error': f'Could not render the map: {exc}'}), 502

    etag = _image_etag(map_image_path)
    not_modified = _image_not_modified(etag, revalidate=True)
    if not_modified is not None:
        return not_modified
    return _image_cached(send_file(map_image_path, mimetype='image/jpeg'), etag, revalidate=True)


@app.route('/api/catalogs/<int:catalog_id>/locations/<int:location_id>', methods=['DELETE'])
def delete_location(catalog_id, location_id):
    """Delete a location"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    location_model = LocationModel(catalog_db)
    location = location_model.get_location_by_id(location_id)
    if not location:
        return jsonify({'error': 'Location not found'}), 404

    location_model.delete_location(location_id)
    return jsonify({'message': 'Location deleted successfully'})


def _location_model(catalog_id):
    """(location_model, None) or (None, (response, status)) - the four-line
    preamble every location endpoint below repeats otherwise."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return None, (jsonify({'error': 'Catalog not found'}), 404)
    if not catalog.target_directory:
        return None, (jsonify({'error': 'Catalog target directory not set'}), 400)
    return LocationModel(CatalogDatabase(catalog.target_directory)), None


# How long an interactive geocode may wait.
#
# Generous, and safe to be: geocode.py now admits ONE request at a time and
# turns the rest away in ten seconds, so at most one of the backend's eight
# threads can be sitting here however many times somebody clicks. That is what
# makes the length affordable - without the concurrency limit, eight clicks at
# this timeout would be the whole server.
#
# It needs to be generous because the latency is not ours to predict. The SAME
# coordinate (-33.9, 151.2) was measured at 6.7s in the morning and 63.8s in the
# afternoon - the slow path reaches Nominatim, a public service that throttles.
# So 45s is a bound, NOT a promise: a first lookup in a cold region can exceed
# it, and the message says to retry because a retry genuinely is faster once
# OSMnx has the area. There is no value here that always succeeds.
INTERACTIVE_GEOCODE_TIMEOUT = float(os.environ.get('GEOLOC_UI_TIMEOUT', '45'))


@app.route('/api/catalogs/<int:catalog_id>/geocode', methods=['GET'])
def geocode_point(catalog_id):
    """
    What is near a coordinate, for naming a place.

    A proxy onto backend-geoloc so the browser needs no route to an internal
    service, the answer can be cached server-side, and Nominatim's etiquette
    limit is enforced in one place instead of once per open tab. The Locations
    page called GEOLOC_HOST/poi directly before this.

    The clustering workflow does NOT come through here - it has no session, so
    an authenticated endpoint would answer it 401, which is exactly how the old
    home-address fallback came to fail silently. Both callers share
    workflows/lib/geocode.py instead; see its docstring.
    """
    _, err = _location_model(catalog_id)
    if err:
        return err
    try:
        lat = float(request.args.get('lat'))
        lon = float(request.args.get('lon'))
    except (TypeError, ValueError):
        return jsonify({'error': "'lat' and 'lon' must be numbers"}), 400
    if not (-90 <= lat <= 90) or not (-180 <= lon <= 180):
        return jsonify({'error': 'Coordinates out of range'}), 400

    try:
        limit = int(request.args.get('limit', 5))
        dist = int(request.args.get('dist', 500))
    except ValueError:
        return jsonify({'error': "'limit' and 'dist' must be integers"}), 400

    try:
        # A SHORTER budget than the clustering workflow's, deliberately. This
        # runs in gunicorn, which has one worker and eight threads, so every
        # second spent here is a second one of those eight cannot serve anything
        # else. The workflow is its own process and can afford to wait a minute;
        # a person who clicked a button would rather be told.
        pois = geocode_nearby(lat, lon, limit=limit, dist=dist,
                              timeout=INTERACTIVE_GEOCODE_TIMEOUT)
    except GeocodeUnavailable as exc:
        # 503, not 200-with-an-empty-list: "there is nothing named here" and
        # "we could not ask" are different answers and the page says different
        # things for each.
        #
        # The reason goes in `error`, not only in `detail`: the page shows
        # `error` and nothing else, so a bare "Geocoding service unavailable"
        # sent somebody looking for a stopped container when the service was
        # running and merely slow.
        return jsonify({'error': f'Geocoding service unavailable - {exc}',
                        'detail': str(exc)}), 503
    return jsonify({'lat': lat, 'lon': lon, 'count': len(pois), 'pois': pois})


@app.route('/api/catalogs/<int:catalog_id>/photos/<int:photo_id>/location',
           methods=['PUT'])
def set_photo_location(catalog_id, photo_id):
    """
    Tag one photo with a location by hand, or clear that tag.

    Body: {"location_id": 12} or {"location_id": null}. Recorded as
    location_source='manual', the top of the place ladder, so neither the
    clustering nor the resolve workflow overwrites it on a later run - a
    person's own answer outranks anything derived.
    """
    model, err = _location_model(catalog_id)
    if err:
        return err
    data = request.json or {}
    if 'location_id' not in data:
        return jsonify({'error': "'location_id' is required (null to clear)"}), 400
    location_id = data['location_id']
    if location_id is not None:
        if not model.get_location_by_id(location_id):
            return jsonify({'error': 'Location not found'}), 404
    model.set_file_location(photo_id, location_id)
    return jsonify({'location': model.get_file_location(photo_id)})


def _fire_locations_changed(catalog_id, detail=None):
    """
    The set of named locations changed, so the resolved per-file locations are
    stale.

    Accepting a cluster creates a location that no photo points at until the
    resolve pass runs, and rejecting one leaves files pointing at a location
    that is no longer accepted. Both need the same follow-up, which is why the
    trigger is named for the change rather than for the accept.

    Raised only from the HTTP request path, like _fire_face_confirmed, and for
    the same reason: a workflow subprocess has no way in, so the chain cannot
    re-enqueue what produced it.
    """
    try:
        catalog = Catalog.get_by_id(catalog_id)
        if not catalog:
            return
        workflow_triggers.fire(catalog, 'locations_changed', detail=detail)
        _emit_queue(catalog)
    except Exception as exc:
        # Never fail the accept because scheduling failed. The place is written;
        # the resolve pass can be run by hand.
        print(f'[queue] could not queue the locations_changed chain: {exc}')


@app.route('/api/catalogs/<int:catalog_id>/location-clusters', methods=['GET'])
def get_location_clusters(catalog_id):
    """
    Proposed locations, biggest first, with the geocoder's suggestions.

    A catalog with almost no GPS gets an empty list, which is the normal state
    for a digitised collection rather than an error - second-catalog has 17
    coordinates across 24,269 files. `counts` lets the page say so.
    """
    model, err = _location_model(catalog_id)
    if err:
        return err
    status = request.args.get('status', 'proposed')
    try:
        limit = max(1, min(int(request.args.get('limit', 60)), 500))
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify({'error': "'limit' and 'offset' must be integers"}), 400

    clusters = model.get_clusters(status=status, limit=limit, offset=offset)
    return jsonify({
        'clusters': clusters,
        'total': model.count_clusters(status),
        'counts': {s: model.count_clusters(s)
                   for s in ('proposed', 'accepted', 'rejected')},
        'limit': limit,
        'offset': offset,
    })


@app.route('/api/catalogs/<int:catalog_id>/location-clusters/<int:cluster_id>/map-image',
           methods=['GET'])
def get_location_cluster_map_image(catalog_id, cluster_id):
    """
    The static map snapshot for a PROPOSED location.

    Rendered from the cluster's centroid on first request and cached in cache/,
    the same way and in the same place as a named location's. A proposal had no
    map before this: the renderer was inline in update_location and there is no
    locations row to update. Both kinds are shown on the same card now, so one
    of the two would have been conspicuously blank.
    """
    from flask import send_file

    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    conn = catalog_db.get_connection()
    try:
        row = conn.execute(
            'SELECT latitude, longitude FROM location_clusters WHERE id = ?',
            (cluster_id,)).fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row:
        return jsonify({'error': 'Cluster not found'}), 404

    path = _map_snapshot_path(catalog, 'cluster', cluster_id)
    if not os.path.exists(path):
        try:
            _render_map_snapshot(row['latitude'], row['longitude'], path)
        except Exception as exc:
            return jsonify({'error': f'Could not render the map: {exc}'}), 502

    etag = _image_etag(path)
    not_modified = _image_not_modified(etag, revalidate=True)
    if not_modified is not None:
        return not_modified
    return _image_cached(send_file(path, mimetype='image/jpeg'), etag,
                         revalidate=True)


# No .../accept endpoint. Naming a proposal is POST /locations/<id>/confirm now:
# the clustering workflow creates the locations row, so there is nothing for an
# accept to create. The merge it also offered - "this proposal is somewhere I
# already have" - is NOT currently exposed anywhere; merging two locations is a
# different operation from accepting a cluster and wants its own endpoint.


@app.route('/api/catalogs/<int:catalog_id>/location-clusters/<int:cluster_id>/reject',
           methods=['POST'])
def reject_location_cluster(catalog_id, cluster_id):
    """Mark a cluster as not a location. Kept as a rejection, so the next
    clustering run does not propose it again."""
    model, err = _location_model(catalog_id)
    if err:
        return err
    # Deletes the unconfirmed locations row with it, so the card goes away
    # rather than lingering as a location nobody believes in. A CONFIRMED one
    # survives: somebody named that, and rejecting the grouping that suggested
    # it is not the same as deleting their work.
    model.reject_cluster(cluster_id, delete_unconfirmed_location=True)
    # Files this cluster located must lose that location, which is the resolve
    # pass's job.
    _fire_locations_changed(catalog_id, detail={'cluster': cluster_id})
    return jsonify({'message': 'No longer a location'})


@app.route('/api/catalogs/<int:catalog_id>/location-clusters/<int:cluster_id>/reopen',
           methods=['POST'])
def reopen_location_cluster(catalog_id, cluster_id):
    """Undo an accept or a reject."""
    model, err = _location_model(catalog_id)
    if err:
        return err
    model.reopen_cluster(cluster_id)
    _fire_locations_changed(catalog_id, detail={'cluster': cluster_id})
    return jsonify({'message': 'Cluster reopened'})


@app.route('/api/catalogs/<int:catalog_id>/locations/<int:location_id>/photos', methods=['GET'])
def get_location_photos(catalog_id, location_id):
    """
    Images at a location: the ones whose own resolved location it is, plus the
    ones reached through an event tagged with it.

    It used to be the event path ALONE, and that was a bug with a visible
    symptom: a location's card said "5 photos" from file_locations while its
    gallery, querying location_events, showed none. Both numbers were honest
    about different questions. Every location the clustering workflow produces
    has file_locations rows and no location_events row at all, so its gallery
    was always empty - location-spec.md 4.1 replaced the event-only path with a
    direct per-file link and this endpoint was never moved over.

    The event path stays, as a UNION: a location somebody tagged onto an event
    should still list that event's photos (4.7). UNION and not UNION ALL, so a
    file reachable both ways is listed once.

    Optionally filtered by year and/or month, which applies to both halves.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    location_model = LocationModel(catalog_db)
    location = location_model.get_location_by_id(location_id)
    if not location:
        return jsonify({'error': 'Location not found'}), 404

    year = request.args.get('year', type=int)
    month = request.args.get('month', type=int)

    # ?limit=&offset= pages it. The gallery draws twelve tiles at a time, and it
    # used to be sent all 9,257 photographs of location 347 to draw them -
    # every row built, every display derivative ranked, a few MB of JSON - and
    # then sliced in the browser. Without a limit this still returns the lot,
    # as the Locations list does, for any caller that predates paging.
    raw_limit = request.args.get('limit')
    try:
        limit = max(1, min(500, int(raw_limit))) if raw_limit is not None else None
        offset = max(0, int(request.args.get('offset', 0)))
    except ValueError:
        return jsonify({'error': 'limit and offset must be numbers'}), 400

    conn = catalog_db.get_connection()
    cursor = conn.cursor()

    image_types = "'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp', '.heic', '.heif', '.raw', '.cr2', '.cr3', '.nef', '.arw', '.dng'"

    # The filters apply to both halves of the union, so they are built once and
    # the parameters are supplied twice.
    where_clauses = [f"LOWER(fc.file_type) IN ({image_types})"]
    filter_params = []
    if year:
        where_clauses.append("CAST(SUBSTR(fc.master_datetime, 1, 4) AS INTEGER) = ?")
        filter_params.append(year)
    if month:
        where_clauses.append("CAST(SUBSTR(fc.master_datetime, 6, 2) AS INTEGER) = ?")
        filter_params.append(month)
    where_sql = " AND ".join(where_clauses)

    # The display derivative comes back WITH the row. It used to be a separate
    # query per photograph - 9,753 of them on one location - and the ranking is
    # expressible as a correlated scalar subquery, so it costs one indexed
    # lookup on (original_file_id, rung) per row instead of a round trip.
    _best_alt = """
            SELECT av.{col} FROM alternative_versions av
             WHERE av.original_file_id = fc.id
               AND av.rung IN ('1200','1600','2048','2560')
             ORDER BY (av.width >= 1200) DESC,
                      CASE WHEN av.width >= 1200 THEN av.width END ASC,
                      av.width DESC, av.id ASC
             LIMIT 1"""
    columns = """
            fc.id AS file_id,
            fc.file_path,
            fc.file_name,
            fc.file_type,
            ed.creation_date,
            ed.latitude,
            ed.longitude,
            fc.master_datetime AS taken_at,
            (%s) AS alt_path,
            (%s) AS alt_name,
            (%s) AS alt_characteristic""" % (
        _best_alt.format(col='alternative_file_path'),
        _best_alt.format(col='alternative_file_name'),
        _best_alt.format(col='characteristic'))

    # file_locations may not exist on a catalog that predates it, and an empty
    # gallery is a worse answer than the event path alone - so the direct half
    # is attempted first and dropped if the table is absent.
    has_file_locations = bool(cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='file_locations'"
    ).fetchone())

    # WHICH photographs, decided on ids alone; WHAT is shown of them, built only
    # for the page. The union has to see every candidate to de-duplicate and
    # order them, and when it carried the full columns that meant ranking the
    # display derivative of all 9,257 to return twelve. Now the union is ids and
    # dates, and the derivative subqueries run once per row of the page.
    direct_ids = f"""
        SELECT fc.id AS file_id, fc.master_datetime AS taken_at
        FROM file_contents fc
        JOIN file_locations fl ON fl.file_id = fc.id
        WHERE fl.location_id = ? AND fc.removed_at IS NULL
          AND {where_sql}"""
    event_ids = f"""
        SELECT fc.id AS file_id, fc.master_datetime AS taken_at
        FROM file_contents fc
        JOIN event_files ef ON fc.id = ef.file_id
        JOIN location_events le ON ef.event_id = le.event_id
        WHERE le.location_id = ? AND fc.removed_at IS NULL
          AND {where_sql}"""

    if has_file_locations:
        ids_sql = f"{direct_ids}\n        UNION\n{event_ids}"
        params = [location_id] + filter_params + [location_id] + filter_params
    else:
        ids_sql = event_ids
        params = [location_id] + filter_params

    # file_id breaks ties in taken_at, so a page boundary falls in the same
    # place on every request - photographs from a burst share a second, and
    # without it the same one could appear at the foot of one page and the
    # head of the next.
    sql = f"""
        WITH page AS (
            {ids_sql}
            ORDER BY taken_at ASC, file_id ASC
            LIMIT ? OFFSET ?
        )
        SELECT {columns}
        FROM page p
        JOIN file_contents fc ON fc.id = p.file_id
        LEFT JOIN exif_data ed ON fc.id = ed.file_id
        ORDER BY p.taken_at ASC, p.file_id ASC"""

    # LIMIT -1 is SQLite for "no limit".
    cursor.execute(sql, params + [limit if limit is not None else -1, offset])
    rows = cursor.fetchall()

    total = None
    if limit is not None:
        total = cursor.execute(f'SELECT COUNT(*) FROM ({ids_sql})',
                               params).fetchone()[0]

    results = []
    for row in rows:
        file_id = row['file_id']
        file_path = row['file_path']
        file_name = row['file_name']
        creation_date = row['creation_date']

        # Ranked by measured pixels in the subquery above; the original when
        # the photograph has no derivative of that size.
        display_path = row['alt_path'] or file_path
        display_name = row['alt_name'] or file_name
        characteristic = row['alt_characteristic'] if row['alt_path'] else None

        results.append({
            'file_id': file_id,
            'file_name': file_name,
            'file_path': file_path,
            'display_path': display_path,
            'display_name': display_name,
            'characteristic': characteristic,
            'creation_date': creation_date,
            'latitude': row['latitude'],
            'longitude': row['longitude'],
        })

    # Years and months available here, for the navigation above the gallery.
    #
    # This asked the EVENT path only, which is the same bug the docstring above
    # describes as fixed for the gallery and which was never fixed here: every
    # location the clustering workflow produces has file_locations rows and no
    # location_events row at all, so a location with 9,753 photographs offered
    # an empty year picker. It takes both halves, like the gallery.
    years_months = {}
    _ym_year = ("CAST(SUBSTR(fc.master_datetime, 1, 4) "
                "AS INTEGER)")
    _ym_month = ("CAST(SUBSTR(fc.master_datetime, 6, 2) "
                 "AS INTEGER)")
    _ym_direct = f'''
        SELECT DISTINCT {_ym_year} AS year, {_ym_month} AS month
        FROM file_contents fc
        JOIN file_locations fl ON fl.file_id = fc.id
        WHERE fl.location_id = ? AND fc.removed_at IS NULL
          AND LOWER(fc.file_type) IN ({image_types})
          AND {_ym_year} > 0 AND {_ym_month} > 0'''
    _ym_event = f'''
        SELECT DISTINCT {_ym_year} AS year, {_ym_month} AS month
        FROM file_contents fc
        JOIN event_files ef ON fc.id = ef.file_id
        JOIN location_events le ON ef.event_id = le.event_id
        WHERE le.location_id = ? AND fc.removed_at IS NULL
          AND LOWER(fc.file_type) IN ({image_types})
          AND {_ym_year} > 0 AND {_ym_month} > 0'''
    if has_file_locations:
        cursor.execute(f'{_ym_direct}\n        UNION\n{_ym_event}\n'
                       '        ORDER BY year DESC, month ASC',
                       [location_id, location_id])
    else:
        cursor.execute(f'{_ym_event}\n        ORDER BY year DESC, month ASC',
                       [location_id])
    for ym_row in cursor.fetchall():
        y = ym_row['year']
        m = ym_row['month']
        if y and m:
            if y not in years_months:
                years_months[y] = []
            if m not in years_months[y]:
                years_months[y].append(m)

    conn.close()
    body = {'photos': results, 'years_months': years_months}
    if limit is not None:
        body.update(total=total, limit=limit, offset=offset)
    return jsonify(body)


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/locations', methods=['GET'])
def get_event_locations(catalog_id, event_id):
    """Get all locations associated with an event"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    event = event_model.get_event_by_id(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404

    location_model = LocationModel(catalog_db)
    locations = location_model.get_locations_for_event(event_id)
    return jsonify(locations)


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/locations', methods=['POST'])
def add_location_to_event(catalog_id, event_id):
    """Create a new location and associate it with an event"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.json
    if not data or not data.get('name'):
        return jsonify({'error': 'Name is required'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    event = event_model.get_event_by_id(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404

    location_model = LocationModel(catalog_db)
    # The same spelling rule as create_location above: 'latitude'/'longitude',
    # with 'lat'/'long' accepted on input. This route was missed when the model
    # was renamed (v4.13) and raised TypeError on every save from an event.
    location_id = location_model.create_location(
        name=data['name'],
        latitude=_body_coord(data, 'latitude', 'lat'),
        longitude=_body_coord(data, 'longitude', 'long'),
        description=data.get('description'),
        source=data.get('source'),
    )
    location_model.add_location_event(location_id, event_id)
    event_model.update_event(event_id, type='Manual')
    location = location_model.get_location_by_id(location_id)
    return jsonify(location), 201


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/locations/<int:location_id>', methods=['PUT'])
def link_location_to_event(catalog_id, event_id, location_id):
    """Link an existing location to an event"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    event = event_model.get_event_by_id(event_id)
    if not event:
        return jsonify({'error': 'Event not found'}), 404

    location_model = LocationModel(catalog_db)
    location = location_model.get_location_by_id(location_id)
    if not location:
        return jsonify({'error': 'Location not found'}), 404

    location_model.add_location_event(location_id, event_id)
    event_model.update_event(event_id, type='Manual')
    return jsonify(location)


@app.route('/api/catalogs/<int:catalog_id>/events/<int:event_id>/locations/<int:location_id>', methods=['DELETE'])
def remove_location_from_event(catalog_id, event_id, location_id):
    """Remove a location association from an event"""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    catalog_db = CatalogDatabase(catalog.target_directory)
    event_model = EventModel(catalog_db)
    location_model = LocationModel(catalog_db)
    location_model.remove_location_event(location_id, event_id)
    event_model.update_event(event_id, type='Manual')
    return jsonify({'message': 'Location removed from event successfully'})

# ============================================================================
# User Settings Routes
# ============================================================================

@app.route('/api/user/settings', methods=['GET'])
def get_user_settings():
    """
    The signed-in account's own settings.

    A user who has never saved gets a blank record seeded from their account
    rather than a 404: the settings page should open with their own name and
    email filled in, not an error.
    """
    try:
        me = auth.current_user()
        settings = user_settings_model.get_user_settings(me['id']) if me else None
        if not settings:
            return jsonify({
                'id': None,
                'user_id': me['id'] if me else None,
                'name': me['display_name'] if me else '',
                'email': me['email'] if me else '',
                'address': '', 'city': '', 'state': '', 'country': '',
                'lat': None, 'long': None,
            })
        return jsonify(settings)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/user/settings', methods=['PUT'])
def update_user_settings():
    """Update user settings"""
    try:
        data = request.json
        if not data:
            return jsonify({'error': 'No data provided'}), 400
        
        name = data.get('name', '')
        email = data.get('email', '')
        address = data.get('address', '')
        city = data.get('city', '')
        state = data.get('state', '')
        country = data.get('country', '')
        lat = data.get('lat')
        long = data.get('long')
        
        if not name:
            return jsonify({'error': 'Name is required'}), 400
        
        # Convert lat/long to float if provided and not empty
        if lat is not None and lat != '':
            try:
                lat = float(lat)
            except (ValueError, TypeError):
                lat = None
        else:
            lat = None
            
        if long is not None and long != '':
            try:
                long = float(long)
            except (ValueError, TypeError):
                long = None
        else:
            long = None
        
        me = auth.current_user()
        if me is None:
            return auth.unauthenticated()
        settings = user_settings_model.update_user_settings(
            me['id'], name, email, address, city, state, country, lat, long)
        return jsonify(settings)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/user/location', methods=['GET'])
def get_user_location():
    """Get the active user's home latitude and longitude.

    For the VIEW, not for stored data. A map with nothing to show may centre on
    this and draw no marker; nothing writes it into a latitude column. It used
    to be the fallback ev_extract_events wrote into events.latitude when no
    photo carried GPS, which put an invented coordinate in the column that
    otherwise holds a measurement - see location-spec.md 4.8 and 9.1.
    """
    try:
        me = auth.current_user()
        settings = user_settings_model.get_user_settings(me['id']) if me else None
        if not settings:
            return jsonify({'error': 'User settings not found'}), 404
        return jsonify({'lat': settings.get('lat'), 'long': settings.get('long')})
    except Exception as e:
        return jsonify({'error': str(e)}), 500


# ---------------------------------------------------------------------------
# Albums API
# ---------------------------------------------------------------------------

# Was a bare literal with no environment override - a config-only migration
# would have missed it entirely and left one service still addressed by port.
ALBUMS_BACKEND_URL = os.environ.get('ALBUMS_URL', 'http://albums.airlock.lan')

SCHEMA_HINT = """
Tables (SQLite, per-catalog workflow_results.db):
- file_contents fc: id, file_name, file_type, description, added_at
- exif_data ed: file_id, creation_date, latitude, longitude, camera_make, camera_model
- events ev: id, name, start_time, end_time, type
- event_files ef: event_id, file_id
- people p: id, name
- face_detections fd: file_id, person_id, source
- locations loc: id, name
- location_events le: location_id, event_id
- tags t: id, name
The base query is: SELECT fc.id FROM file_contents fc
LEFT JOIN exif_data ed ON ed.file_id = fc.id
Use fc.id as the primary key for file_contents rows.
"""


def _get_catalog_db_or_404(catalog_id):
    """Return (catalog, CatalogDatabase) or (None, None) if not found."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return None, None
    return catalog, CatalogDatabase(catalog.target_directory)


def _run_cumulative_query(catalog_db, filters):
    """
    Execute the cumulative filter query and return the matching file count.
    `filters` is a list of sql_fragment strings (WHERE clause bodies), applied
    as successive AND conditions.
    """
    conn = catalog_db.get_connection()
    try:
        base = '''
            SELECT COUNT(DISTINCT fc.id) AS cnt
            FROM file_contents fc
            LEFT JOIN exif_data ed ON ed.file_id = fc.id
            WHERE fc.removed_at IS NULL
              AND fc.id NOT IN (''' + album_templates.PAIR_SECONDARY_IDS + ''')
        '''
        if filters:
            where_clause = ' AND '.join(f'({f})' for f in filters)
            query = f'{base} AND {where_clause}'
        else:
            query = base
        row = conn.execute(query).fetchone()
        return row['cnt'] if row else 0
    except Exception:
        return 0
    finally:
        conn.close()


def _get_total_photo_count(catalog_db):
    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT COUNT(*) AS cnt FROM file_contents '
                           'WHERE removed_at IS NULL '
                           f'AND id NOT IN ({album_templates.PAIR_SECONDARY_IDS})').fetchone()
        return row['cnt'] if row else 0
    finally:
        conn.close()


def _refresh_filter_counts(catalog_db, album_id):
    """Re-run all filters for an album and update cached photo_count values."""
    conn = catalog_db.get_connection()
    try:
        rows = conn.execute(
            'SELECT id, sql_fragment FROM album_filters WHERE album_id=? ORDER BY position',
            (album_id,)
        ).fetchall()
        cumulative = []
        for row in rows:
            cumulative.append(row['sql_fragment'])
            count = _run_cumulative_query(catalog_db, cumulative)
            conn.execute(
                'UPDATE album_filters SET photo_count=? WHERE id=?',
                (count, row['id'])
            )
        conn.commit()
    finally:
        conn.close()


def _truthy(value):
    """Query-string booleans: ?original=1, =true, =yes all mean yes."""
    return str(value or '').strip().lower() in ('1', 'true', 'yes', 'on')


def _derivative_owner(conn, file_id):
    """
    Whose derivatives to use for this file.

    A logical image can arrive as several files - a RAW and a JPG from the same
    shutter actuation - and it has ONE set of renderings, made from the primary
    (file-unity-spec.md section 5.3). A secondary has none of its own, so asking
    for its thumbnail would 404 and asking for its image would hand a browser a
    26 MB CR3 it cannot render.

    Returns the primary's id when this file belongs to a group, otherwise the id
    unchanged. With no groups in the database this is the identity function, so
    every caller behaves exactly as it did before grouping existed.
    """
    try:
        row = conn.execute(
            'SELECT g.primary_file_id FROM file_contents f '
            'JOIN image_groups g ON g.id = f.image_group_id WHERE f.id = ?',
            (file_id,)).fetchone()
    except sqlite3.Error:
        return file_id          # catalog predates the table
    return row[0] if row and row[0] else file_id


def _image_group_payload(conn, catalog_id, file_id):
    """
    (image_group, versions) for the photo detail response.

    image_group is None for an ungrouped file, which is every file until grouping
    runs. `versions` is always present: one entry per ORIGINAL in the group, plus
    the group's single set of derivatives - and never a thumbnail.
    """
    try:
        row = conn.execute(
            'SELECT g.id, g.primary_file_id, g.kind, g.match_rule '
            'FROM file_contents f JOIN image_groups g ON g.id = f.image_group_id '
            'WHERE f.id = ?', (file_id,)).fetchone()
    except sqlite3.Error:
        row = None

    if row:
        members = [dict(r) for r in conn.execute(
            'SELECT id, file_name, file_size FROM file_contents WHERE image_group_id = ? '
            'ORDER BY id', (row['id'],))]
        primary_id = row['primary_file_id']
        group = {
            'id': row['id'],
            'primary_file_id': primary_id,
            'kind': row['kind'],
            'match_rule': row['match_rule'],
            'files': [{
                'id': m['id'],
                'file_name': m['file_name'],
                'file_size': m['file_size'],
                'original_kind': _original_kind(m['file_name']),
                'is_primary': m['id'] == primary_id,
            } for m in members],
        }
    else:
        member = conn.execute(
            'SELECT id, file_name, file_size FROM file_contents WHERE id = ?',
            (file_id,)).fetchone()
        members = [dict(member)] if member else []
        primary_id = file_id
        group = None

    versions = []
    for m in members:
        _kind = _original_kind(m['file_name'])          # ORIGINAL RAW | ORIGINAL JPG
        _ext = os.path.splitext(m['file_name'] or '')[1].lstrip('.').upper()
        versions.append({
            # 'Original RAW (CR3)' / 'Original JPG' - the extension is named for
            # the RAW because "RAW" alone does not tell you what will download.
            'label': (f'Original RAW ({_ext})' if _kind == 'ORIGINAL RAW' and _ext
                      else 'Original RAW' if _kind == 'ORIGINAL RAW'
                      else 'Original JPG'),
            'file_id': m['id'],
            'kind': 'original',
            'is_raw': _kind == 'ORIGINAL RAW',
            'original_kind': _kind,
            'bytes': m['file_size'],
            'width': None, 'height': None,
            'url': f'/api/catalogs/{catalog_id}/photos/{m["id"]}/image?original=1',
        })

    # The group's derivatives, which are the PRIMARY's and the only ones that
    # exist. Thumbnails are excluded per the brief.
    for r in conn.execute(
            "SELECT characteristic, width, height, alternative_file_size "
            "FROM alternative_versions WHERE original_file_id = ? "
            "AND UPPER(characteristic) NOT LIKE '%THUMBNAIL%' ORDER BY width DESC",
            (primary_id,)):
        w = r['width']
        versions.append({
            'label': f'Large ({w}px)' if w else 'Large',
            'file_id': primary_id,
            'kind': 'alternate',
            'is_raw': False,
            'original_kind': None,
            'bytes': r['alternative_file_size'],
            'width': w, 'height': r['height'],
            'url': f'/api/catalogs/{catalog_id}/photos/{primary_id}/image',
        })

    versions.sort(key=lambda v: (v['kind'] != 'original', -(v['bytes'] or 0)))
    return group, versions


def _measure_image(path):
    """
    (width, height) for an image on disk, or (None, None).

    Only ever called for an alternative_versions row that has no recorded size -
    which is none of them in practice. Every result is written back, so this
    runs at most once per file in the life of the catalog.
    """
    try:
        from PIL import Image
        with Image.open(path) as img:
            return img.size
    except Exception:
        return (None, None)


def _deploy_album(catalog, catalog_db, album_id: int) -> dict:
    """
    Build the deployed album folder for the given album.

    Creates cache/Albums/<padded_id>/ inside catalog.target_directory,
    populates it with symlinks to every alt-version file assigned to any
    section of the album, and writes the album.json manifest.

    Returns a stats dict:
      {album_dir, manifest, symlinks_created, symlinks_skipped, deployed_at}.
    Raises OSError / sqlite3.Error on unrecoverable errors.
    """
    import json as _json
    import shutil as _shutil
    from datetime import datetime as _datetime

    album_dir_name = f"{album_id:05d}"
    album_dir = os.path.join(catalog.target_directory, "cache", "Albums", album_dir_name)
    os.makedirs(album_dir, exist_ok=True)

    conn = catalog_db.get_connection()
    try:
        # ── Fetch album metadata ──────────────────────────────────────────────
        album_row = conn.execute('SELECT * FROM albums WHERE id=?', (album_id,)).fetchone()
        if not album_row:
            raise ValueError(f"Album {album_id} not found")
        album = dict(album_row)

        # One entry per photograph: a RAW+JPG pair's RAW half becomes its JPG
        # (or goes, when the JPG is already there). Albums built from events
        # used to hold both, and publishing is where an existing album heals.
        folded = album_templates.fold_pairs(conn, album_id)
        if folded:
            conn.commit()
            import logging as _logging
            _logging.getLogger(__name__).info(
                'album %s: %d RAW+JPG secondaries folded into their primaries',
                album_id, folded)

        # ── Q1: sections + photos + best alt version + EXIF ──────────────────
        q1_rows = conn.execute('''
            SELECT
                s.id            AS section_id,
                s.name          AS section_name,
                s.description   AS section_description,
                s.position      AS section_position,
                s.category_id   AS section_category_id,
                sp.position     AS photo_position,
                fc.id           AS file_id,
                fc.file_name,
                fc.master_datetime,
                ed.creation_date
            FROM album_sections s
            -- LEFT, not INNER. An inner join drops a section with no photos
            -- from the manifest entirely, and once sections are cards an empty
            -- card is meaningful - it is a visible thing the user can fix,
            -- where a silently absent one is not. Categories make empty
            -- sections reachable, so this stops being an edge case
            -- (album-gallery-enhancement-spec.md section 6.3).
            LEFT JOIN section_photos sp ON sp.section_id = s.id
            LEFT JOIN file_contents  fc ON fc.id = sp.file_id
            LEFT JOIN exif_data ed ON ed.file_id = fc.id
            WHERE s.album_id = ?
            ORDER BY s.position, s.id, sp.position
        ''', (album_id,)).fetchall()

        # A LEFT JOIN yields one row with file_id NULL for an empty section.
        # It is a section marker, not a photo, and must not reach any of the
        # per-file lookups below.
        file_ids = list({r['file_id'] for r in q1_rows if r['file_id'] is not None})

        # ── Q2: people per file ───────────────────────────────────────────────
        people_map = {}  # file_id -> [name, …]
        if file_ids:
            placeholders = ','.join('?' * len(file_ids))
            for row in conn.execute(f'''
                SELECT fd.file_id, p.name
                FROM face_detections fd
                JOIN people p ON p.id = fd.person_id
                WHERE fd.file_id IN ({placeholders})
                  AND fd.person_id IS NOT NULL
                ORDER BY fd.file_id, p.name
            ''', file_ids).fetchall():
                people_map.setdefault(row['file_id'], []).append(row['name'])

        # ── Q3: location per file ─────────────────────────────────────────────
        location_map = {}  # file_id -> location_name
        if file_ids:
            for row in conn.execute(f'''
                SELECT ef.file_id, loc.name AS location_name
                FROM event_files ef
                JOIN location_events le ON le.event_id = ef.event_id
                JOIN locations loc      ON loc.id = le.location_id
                WHERE ef.file_id IN ({placeholders})
                GROUP BY ef.file_id
                ORDER BY ef.file_id
            ''', file_ids).fetchall():
                location_map[row['file_id']] = row['location_name']

        # ── Q4: tags per file ─────────────────────────────────────────────────
        tags_map = {}  # file_id -> [tag_name, …]
        if file_ids:
            for row in conn.execute(f'''
                SELECT DISTINCT fd.file_id, t.name AS tag_name
                FROM face_detections fd
                JOIN tag_person tp ON tp.person_id = fd.person_id
                JOIN tags t        ON t.id = tp.tag_id
                WHERE fd.file_id IN ({placeholders})
                  AND fd.person_id IS NOT NULL
                ORDER BY fd.file_id, t.name
            ''', file_ids).fetchall():
                tags_map.setdefault(row['file_id'], []).append(row['tag_name'])

        # ── All alt versions for symlink creation ─────────────────────────────
        all_alt_rows = conn.execute('''
            SELECT DISTINCT av.id AS alt_id,
                            av.alternative_file_path, av.alternative_file_name,
                            av.width, av.height, av.characteristic, av.rung,
                            fc.file_name AS source_name,
                            ed.width  AS source_width,
                            ed.height AS source_height,
                            sp.file_id
            FROM section_photos sp
            JOIN album_sections s   ON s.id = sp.section_id
            JOIN file_contents  fc  ON fc.id = sp.file_id
            JOIN alternative_versions av ON av.original_file_id = sp.file_id
            LEFT JOIN exif_data ed  ON ed.file_id = fc.id
            WHERE s.album_id = ?
            ORDER BY sp.section_id, sp.position
        ''', (album_id,)).fetchall()
    finally:
        conn.close()

    # ── Idempotency: remove stale symlinks ────────────────────────────────────
    # Collect the set of alt file names that SHOULD be in this deployment.
    expected_names = set()
    seen_names = {}  # link_name -> file_id (to detect conflicts)
    # Build the final mapping: file_id+orig_name -> link_name (conflict-resolved)
    link_plan = []  # [(alt_abs_path, link_name)]
    for row in all_alt_rows:
        orig_name = row['alternative_file_name']
        alt_abs_path = _resolve_path(row['alternative_file_path'], catalog.target_directory)
        if orig_name not in seen_names:
            link_name = orig_name
            seen_names[orig_name] = row['file_id']
        else:
            # Conflict: append file_id before extension
            base, ext = os.path.splitext(orig_name)
            link_name = f"{base}_{row['file_id']}{ext}"
        expected_names.add(link_name)
        link_plan.append((alt_abs_path, link_name, row['file_id'],
                          row['alt_id'], row['width'], row['height'],
                          row['characteristic'] or '', row['source_name'] or '',
                          row['source_width'], row['source_height'],
                          row['rung'] or ''))

    # Remove any existing symlinks in album_dir that are no longer expected
    if os.path.isdir(album_dir):
        for entry in os.listdir(album_dir):
            if entry.endswith('.json'):
                continue
            entry_path = os.path.join(album_dir, entry)
            if os.path.islink(entry_path) and entry not in expected_names:
                os.unlink(entry_path)

    # ── Create symlinks ───────────────────────────────────────────────────────
    symlinks_created = 0
    symlinks_skipped = 0
    # Map file_id -> link_name for JSON manifest alt_path
    file_link_map = {}  # file_id -> link_name of the chosen display derivative
    # file_id -> [{rung, path, width, height}], the display rungs shipped for
    # this photo, and separately its square thumbnails. Both become manifest
    # arrays (spec 8.1); both are what lets the packer decide what to ship
    # without ever reading the database (spec 4.5).
    file_sources_map = {}
    file_thumbs_map = {}
    # file_id -> (width, height) of the ALT THAT WAS LINKED, captured in the same
    # step so the two can never describe different images. Baked into the
    # manifest below: the gallery must not have to ask a server for image sizes,
    # because a packed bundle on a static host has no server to ask
    # (albums-enhancement-spec.md section 4.4).
    file_dims_map = {}
    dims_backfilled = []

    # ── Video: the proxy and the poster are different things ─────────────────
    # A video carries a VIDEO PROXY MP4 row (what plays) and a TYPE THUMBNAIL
    # row (the poster frame). Both get symlinked like any other derivative, but
    # only the manifest can say which is which - and the generic "first alt
    # seen" rule below cannot, so it could hand a photo tile an .mp4.
    #
    # alt_path deliberately stays the POSTER for a video. It is what an older
    # gallery build renders, and a still frame is a far better degradation than
    # an <img> pointed at an .mp4 (album-gallery-enhancement2-spec.md 3.5).
    video_files = set()
    video_proxy_map = {}     # file_id -> (link_name, width, height)
    video_poster_map = {}    # file_id -> (link_name, width, height)
    video_source_dims = {}   # file_id -> (width, height) of the video itself
    for _p, _link, _fid, _aid, _w, _h, _char, _source_name, _sw, _sh, _rung in link_plan:
        # The FILE NAME, not the stored file_type: file_type is '.mov', and
        # os.path.splitext('.mov') reads that as a hidden file with no
        # extension, so is_video_file('.mov') is False.
        if not is_video_file(_source_name or ''):
            continue
        video_files.add(_fid)
        if _sw and _sh:
            video_source_dims[_fid] = (_sw, _sh)
        if _rung == 'video_proxy':
            video_proxy_map.setdefault(_fid, (_link, _w, _h))
        elif _rung in ('video_poster', 'thumb320', 'thumb72'):
            # Prefer the largest poster available; they are square, but a 320
            # still is a usable placeholder where a 72 is not.
            best = video_poster_map.get(_fid)
            if best is None or (_w or 0) > (best[1] or 0):
                video_poster_map[_fid] = (_link, _w, _h)

    # ── Which derivative does each photo's tile use? ──────────────────────────
    # This used to be "the first row SQLite happened to return", with a comment
    # calling it the best alt. Nothing computed a best. It worked only because
    # insertion order puts the base first, which is luck, not a rule - and with
    # several display rungs on disk it would become a dependency on the order a
    # backfill happened to run in (spec 6.3).
    #
    # Now: pick_derivative over that file's display rungs, by measured pixels.
    _rows_by_file = {}
    for (_ap, _link, _fid, _aid, _w, _h, _char, _sn, _sw, _sh, _rung) in link_plan:
        _rows_by_file.setdefault(_fid, []).append(
            {'rung': _rung, 'width': _w, 'height': _h,
             'link': _link, 'abs': _ap, 'alt_id': _aid})

    for _fid, _rows in _rows_by_file.items():
        if _fid in video_files:
            continue                      # pinned below, by poster and proxy
        _chosen = pick_derivative(_rows)
        if not _chosen:
            continue
        file_link_map[_fid] = _chosen['link']
        _cw, _ch = _chosen['width'], _chosen['height']
        if not (_cw and _ch):
            # A row written before width/height were recorded. Measure once and
            # remember it, rather than measuring on every publish - and rather
            # than shipping a manifest with a hole the gallery would paper over
            # with a 4:3 guess.
            _cw, _ch = _measure_image(_chosen['abs'])
            if _cw and _ch:
                dims_backfilled.append((_cw, _ch, _chosen['alt_id']))
        file_dims_map[_fid] = (_cw, _ch)

        # Every display rung this photo has, narrowest first: the gallery's
        # srcSet and the lightbox's full-resolution image both come from here.
        _srcs = sorted((r for r in _rows if r['rung'] in MANIFEST_DISPLAY_RUNGS
                        and r['width'] and r['height']),
                       key=lambda r: r['width'])
        # Omitted entirely when there is only the base - a one-element array on
        # every photo would grow every manifest for nothing (spec 8.1).
        if len(_srcs) > 1:
            file_sources_map[_fid] = [
                {'rung': r['rung'], 'path': r['link'],
                 'width': r['width'], 'height': r['height']} for r in _srcs]

        # Square crops, described separately and never mixed into sources: a
        # srcSet entry may be chosen by the browser at any time, and a square
        # crop chosen for a rows tile renders at the wrong aspect ratio. They
        # exist for the gallery types that want them (spec 4.11).
        #
        # thumb72 is left out: 72px and 2KB, the Photos-page filmstrip artefact,
        # below anything a gallery would show.
        _thumbs = sorted((r for r in _rows if r['rung'] == 'thumb320'),
                         key=lambda r: r['width'] or 0)
        if _thumbs:
            file_thumbs_map[_fid] = [
                {'rung': r['rung'], 'path': r['link'], 'size': r['width']}
                for r in _thumbs]

    for (alt_abs_path, link_name, file_id, alt_id, alt_w, alt_h,
         characteristic, source_name, source_width, source_height,
         rung) in link_plan:
        if file_id in video_files:
            # Pin it, rather than letting whichever row arrives first decide.
            poster = video_poster_map.get(file_id)
            if poster and file_link_map.get(file_id) != poster[0]:
                file_link_map[file_id] = poster[0]
                proxy = video_proxy_map.get(file_id)
                # Lay the tile out with the PROXY's shape. Posters are square
                # crops, so using them would make every video a square.
                if proxy and proxy[1] and proxy[2]:
                    file_dims_map[file_id] = (proxy[1], proxy[2])
                elif file_id in video_source_dims:
                    # No proxy was generated. The video's own EXIF dimensions
                    # still give the right shape - all 178 videos in the
                    # reference catalog have them - and the alternative is a
                    # 320x320 poster that would lay a 16:9 clip out as a square.
                    file_dims_map[file_id] = video_source_dims[file_id]
                else:
                    file_dims_map[file_id] = (poster[1], poster[2])
        link_path = os.path.join(album_dir, link_name)
        if os.path.lexists(link_path):
            # A link left over from an older deployment may be absolute (and so
            # broken outside this container). Replace anything that no longer
            # resolves rather than skipping it forever.
            if os.path.islink(link_path) and not os.path.exists(link_path):
                os.unlink(link_path)
            else:
                symlinks_skipped += 1
                continue
        if not os.path.exists(alt_abs_path):
            import logging as _logging
            _logging.getLogger(__name__).warning(
                "deploy_album: source file missing, skipping symlink: %s", alt_abs_path
            )
            symlinks_skipped += 1
            continue
        # Link RELATIVE to the album directory, not to an absolute path.
        # An absolute target here is a container path (/catalogs/family/...) that
        # resolves only inside the backend container: on the host and in the
        # album-gallery container those links dangle. A relative target
        # (../../Images/...) resolves correctly wherever the catalog tree is
        # mounted, as long as its internal structure is preserved.
        os.symlink(os.path.relpath(alt_abs_path, album_dir), link_path)
        symlinks_created += 1

    if dims_backfilled:
        try:
            conn = catalog_db.get_connection()
            try:
                conn.executemany(
                    'UPDATE alternative_versions SET width = ?, height = ? WHERE id = ?',
                    dims_backfilled)
                conn.commit()
            finally:
                conn.close()
        except Exception as _e:
            import logging as _logging
            _logging.getLogger(__name__).warning(
                'deploy_album: could not record measured dimensions: %s', _e)

    # ── Build JSON manifest ───────────────────────────────────────────────────
    def _convert_dt(raw):
        """Normalise EXIF / master_datetime to ISO-8601 or return None."""
        if not raw:
            return None
        import re as _re
        # 'YYYY:MM:DD HH:MM:SS' → 'YYYY-MM-DD HH:MM:SS'
        raw = _re.sub(r'^(\d{4}):(\d{2}):(\d{2})', r'\1-\2-\3', str(raw).strip())
        return raw.replace(' ', 'T') if raw else None

    # Group rows by section
    sections_dict = {}  # section_id -> {meta, photos[]}
    for row in q1_rows:
        sid = row['section_id']
        if sid not in sections_dict:
            sections_dict[sid] = {
                'id': sid,
                'name': row['section_name'],
                'description': row['section_description'] or None,
                'position': row['section_position'],
                # null means the album root, which is where every section sat
                # before categories existed (spec 6.2).
                'category_id': row['section_category_id'],
                'cover': None,          # filled in below, once photos are known
                'photos': [],
            }
        fid = row['file_id']
        if fid is None:
            # The LEFT JOIN's empty-section marker. The section is now in the
            # manifest, which is the point; there is simply no photo on this row.
            continue
        # alt_path: relative to target_directory, pointing at the symlink
        best_link = file_link_map.get(fid)
        rel_alt_path = best_link if best_link else None
        # datetime: prefer EXIF creation_date, fall back to master_datetime
        dt_raw = row['creation_date'] or row['master_datetime']
        _w, _h = file_dims_map.get(fid, (None, None))
        # A video's alt_path is its poster frame; media_type is what tells the
        # gallery that. Both keys are additive - an older build reads alt_path
        # and renders the still, which is the intended degradation.
        _is_video = fid in video_files
        _proxy = video_proxy_map.get(fid)
        sections_dict[sid]['photos'].append({
            'file_id': fid,
            'file_name': row['file_name'],
            'position': row['photo_position'],
            'alt_path': rel_alt_path,
            'media_type': 'video' if _is_video else 'image',
            # The browser-friendly file. None when no proxy was ever generated -
            # 16 of 178 videos in the reference catalog - and the gallery then
            # shows the poster with a badge rather than dropping the video.
            'video_path': _proxy[0] if _proxy else None,
            # Explicit as well as being alt_path, so the gallery never has to
            # infer that one field means two things.
            'poster_path': rel_alt_path if _is_video else None,
            # Present so the gallery never asks a server for them. Null only
            # when the file could not be measured at all, and the gallery falls
            # back to a placeholder ratio for that one photo.
            'width': _w,
            'height': _h,
            # Every display rung available for this photo, narrowest first.
            # ADDITIVE: alt_path keeps its exact meaning, so a gallery build
            # that has never heard of this field renders the manifest exactly as
            # it does today (spec 4.7). Absent when only the base exists.
            **({'sources': file_sources_map[fid]} if fid in file_sources_map else {}),
            # Square crops, for the gallery types that use them. A SEPARATE
            # array, never merged into sources: these have the wrong aspect
            # ratio for a rows tile, and a browser choosing one out of a srcSet
            # would render it distorted (spec 4.6, 4.11).
            **({'thumbnails': file_thumbs_map[fid]} if fid in file_thumbs_map else {}),
            'datetime': _convert_dt(dt_raw),
            'tags': tags_map.get(fid, []),
            'people': people_map.get(fid, []),
            'location': location_map.get(fid),
        })

    # ── Covers ────────────────────────────────────────────────────────────────
    # Resolved from the section rows, then rendered against the photos already
    # in the manifest so the gallery gets a path it can use without asking a
    # server for anything - the same rule the width/height fields follow.
    conn_meta = catalog_db.get_connection()
    try:
        cover_ids = _resolve_section_covers(conn_meta, album_id)
        category_rows = _category_rows(conn_meta, album_id)
    finally:
        conn_meta.close()
    for sid, sec in sections_dict.items():
        fid = cover_ids.get(sid)
        if not fid:
            continue
        photo = next((p for p in sec['photos'] if p['file_id'] == fid), None)
        if photo:
            sec['cover'] = {
                'file_id': fid,
                'alt_path': photo['alt_path'],
                'width': photo['width'],
                'height': photo['height'],
            }
            # The cover's square crop, so an event card can be drawn from
            # album.json alone - the section's photos live in their own file now.
            _thumbs = photo.get('thumbnails') or []
            _thumb = next((t for t in _thumbs if t['rung'] == 'thumb320'),
                          _thumbs[0] if _thumbs else None)
            if _thumb:
                sec['cover']['thumb_path'] = _thumb['path']

    # ── Categories ────────────────────────────────────────────────────────────
    # Their OWN query. Deriving them from the photo join would mean a category
    # the user created but has not filled yet never reaches the gallery
    # (spec 6.3).
    # A cycle serialises into a file no consumer can render, and publishing is
    # the last place to notice. Anything not reachable from the root is
    # re-parented to the root and logged: a visibly misfiled category is
    # something the user can fix, a failed deployment is not (spec 6.4).
    _by_id = {r['id']: r for r in category_rows}
    _reachable, _orphans = set(), []
    for _r in category_rows:
        _seen, _cur = set(), _r['id']
        while _cur is not None and _cur not in _seen:
            _seen.add(_cur)
            _parent = _by_id.get(_cur)
            _cur = _parent['parent_id'] if _parent else None
        if _cur is None:
            _reachable.add(_r['id'])
        else:
            _orphans.append(_r['id'])
    if _orphans:
        import logging as _logging
        _logging.getLogger(__name__).warning(
            'album %s: categories %s are not reachable from the root; '
            'publishing them at the root', album_id, _orphans)

    categories_out = [{
        'id': r['id'],
        'parent_id': r['parent_id'] if r['id'] in _reachable else None,
        'name': r['name'],
        'position': r['position'],
    } for r in category_rows]

    # A section pointing at a category that is gone renders at the root rather
    # than vanishing. Same principle as the orphan rule above.
    _known = {c['id'] for c in categories_out}
    for sec in sections_dict.values():
        if sec['category_id'] is not None and sec['category_id'] not in _known:
            sec['category_id'] = None

    # ── One file per section ──────────────────────────────────────────────────
    # album.json used to carry every photo of every section inline: 6.9 MB for
    # a 9,000-photo year, all downloaded and parsed before the gallery showed
    # anything. It is now an INDEX - each section's counts, cover and tags - and
    # a section's photos are in section-<id>.json beside it, fetched when that
    # event is opened (format 2). The gallery still reads format 1, so bundles
    # packed before this keep working.
    #
    # Section files are written BEFORE album.json, and each through a rename, so
    # a reader never finds an index naming a file that is missing or half
    # written. Compact JSON: nobody reads these by eye, and indenting doubled
    # the size.
    def _write_json(path, data, **kwargs):
        tmp = f'{path}.tmp{os.getpid()}'
        with open(tmp, 'w', encoding='utf-8') as fh:
            _json.dump(data, fh, ensure_ascii=False, **kwargs)
        os.replace(tmp, path)

    section_files = set()
    for sec in sections_dict.values():
        photos = sec.pop('photos')
        sec['photo_count'] = len(photos)
        # What the gallery can actually show: a still, or a video with a proxy
        # or at least a poster. Needed before the photos are loaded.
        sec['renderable_count'] = sum(
            1 for p in photos if p['alt_path'] or p['video_path'])
        # For the tag filter, which has to say which events hold a tag without
        # opening every one of them.
        sec['tags'] = sorted({t for p in photos for t in p['tags']})
        sec['photos_file'] = f"section-{sec['id']}.json"
        section_files.add(sec['photos_file'])
        _write_json(os.path.join(album_dir, sec['photos_file']),
                    {'section_id': sec['id'], 'photos': photos},
                    separators=(',', ':'))
    for entry in os.listdir(album_dir):
        if entry.startswith('section-') and entry.endswith('.json') \
                and entry not in section_files:
            os.unlink(os.path.join(album_dir, entry))

    deployed_at = _datetime.utcnow().isoformat()
    manifest_data = {
        # 2: sections carry photos_file instead of photos (see above).
        'format': 2,
        'id': album['id'],
        'name': album['name'],
        'description': album['description'] or None,
        'created_at': album['created_at'],
        'updated_at': album['updated_at'],
        'deployed_at': deployed_at,
        # Flat, and the root is not in it: parent_id null means "under the
        # album", labelled by this manifest's own `name` (spec 6.2).
        'categories': categories_out,
        # sections[] keeps its shape and stays flat. A gallery build that has
        # never heard of categories renders this exactly as it does today, which
        # is what lets the manifest ship before the gallery is updated (spec 2.4).
        'sections': sorted(sections_dict.values(),
                           key=lambda s: (s['position'], s['id'])),
    }

    manifest_filename = "album.json"
    manifest_path = os.path.join(album_dir, manifest_filename)
    _write_json(manifest_path, manifest_data, indent=2)

    rel_manifest = os.path.join("cache", "Albums", album_dir_name, manifest_filename)
    return {
        'album_id': album_id,
        'album_dir': album_dir,
        'manifest': rel_manifest,
        'symlinks_created': symlinks_created,
        'symlinks_skipped': symlinks_skipped,
        'deployed_at': deployed_at,
    }


# ─── Album covers ─────────────────────────────────────────────────────────────
# One photo OUT OF THE ALBUM, drawn at the top of its card on the Albums page.
#
# Derived on read rather than stored in a column, which is where this differs
# from an event's cover (models.EventModel.resolve_covers): an event holds a
# fixed set of files, but an album's contents are a QUERY - its filters, or its
# sections - so a stored pick would go stale the moment a filter changed, and
# there is no cover control on an album card for a stored value to remember.
# What a user CAN choose is the cover of a section, in the album editor, and
# step 1 below honours that: setting the first section's cover sets the album's.
#
# The pick is the album's FIRST photo, not a random one. Random would need
# storing to stay put, and "first" is the same photo the album itself opens
# with, so the card and the album agree.
_COVER_THUMB_EXISTS = """
    EXISTS (SELECT 1 FROM alternative_versions av
            WHERE av.original_file_id = fc.id
              AND UPPER(av.characteristic) LIKE '%THUMBNAIL 320%')
"""


def _album_cover_file_id(conn, album_id):
    """
    One file id from the album to draw as its cover, or None.

    A 320 thumbnail is REQUIRED of the candidate, not merely preferred: the card
    renders that asset and nothing else, so a file without one would pin the
    album to a permanently broken image - and albums do contain such files, both
    the stray .DS_Store entries the importer files and any photo whose thumbnail
    workflow has not run yet. A video qualifies through its poster frame, which
    is stored under the same characteristic (video-spec.md section 7).
    """
    thumb = _COVER_THUMB_EXISTS

    # 1. A curated album whose first section has a cover chosen in the editor,
    #    if that photo is still in the section - conditional for the same reason
    #    _resolve_section_covers checks membership: foreign keys are inert here,
    #    so a stored id is a hint rather than a fact.
    row = conn.execute(f"""
        SELECT s.cover_file_id AS file_id
        FROM album_sections s
        JOIN section_photos sp ON sp.section_id = s.id AND sp.file_id = s.cover_file_id
        JOIN file_contents fc ON fc.id = sp.file_id
        WHERE s.album_id = ? AND s.cover_file_id IS NOT NULL AND {thumb}
        ORDER BY s.position, s.id
        LIMIT 1
    """, (album_id,)).fetchone()
    if row:
        return row['file_id']

    # 2. Otherwise the first photo of its first section, in the order the album
    #    presents them.
    row = conn.execute(f"""
        SELECT sp.file_id
        FROM album_sections s
        JOIN section_photos sp ON sp.section_id = s.id
        JOIN file_contents fc ON fc.id = sp.file_id
        WHERE s.album_id = ? AND {thumb}
        ORDER BY s.position, s.id, sp.position, sp.file_id
        LIMIT 1
    """, (album_id,)).fetchone()
    if row:
        return row['file_id']

    # 3. A filter album keeps no photo rows of its own, so ask its filters -
    #    the same query GET .../photos runs, ordered the same way, so this is
    #    the album's first photo and not merely one of them.
    fragments = [r['sql_fragment'] for r in conn.execute(
        'SELECT sql_fragment FROM album_filters WHERE album_id=? ORDER BY position',
        (album_id,)).fetchall()]
    if not fragments:
        # No sections and no filters. GET .../photos answers "the whole catalog"
        # for this album, but a cover taken from a catalog the album has not
        # filtered would claim contents it has not chosen yet, so: no cover.
        return None
    where_clause = ' AND '.join(f'({f})' for f in fragments)
    try:
        row = conn.execute(f"""
            SELECT fc.id AS file_id
            FROM file_contents fc
            LEFT JOIN exif_data ed ON ed.file_id = fc.id
            WHERE ({where_clause}) AND {thumb} AND fc.removed_at IS NULL
              AND fc.id NOT IN ({album_templates.PAIR_SECONDARY_IDS})
            ORDER BY fc.id
            LIMIT 1
        """).fetchone()
    except Exception:
        # A fragment that no longer runs - a column renamed under it, say.
        # GET .../photos swallows the same failure and returns no photos; here
        # the card shows its placeholder rather than the whole list failing.
        return None
    return row['file_id'] if row else None


@app.route('/api/catalogs/<int:catalog_id>/albums', methods=['GET'])
def get_albums(catalog_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    try:
        rows = conn.execute(
            'SELECT * FROM albums ORDER BY created_at DESC'
        ).fetchall()
        albums = []
        for row in rows:
            a = dict(row)
            # Attach photo count of the last filter (if any)
            last = conn.execute(
                '''SELECT photo_count FROM album_filters
                   WHERE album_id=? ORDER BY position DESC LIMIT 1''',
                (a['id'],)
            ).fetchone()
            a['photo_count'] = last['photo_count'] if last else None
            # The card's cover. Resolved here rather than by the card asking per
            # album, which is N requests to open a page listing every album.
            a['cover_file_id'] = _album_cover_file_id(conn, a['id'])
            albums.append(a)
    finally:
        conn.close()
    return jsonify(albums)


@app.route('/api/catalogs/<int:catalog_id>/albums', methods=['POST'])
def create_album(catalog_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    name = data.get('name', '').strip()
    # 'tools' unless asked: making an album with words is moving to Search
    # (2026-10-20); 'words' is still accepted for albums made that way.
    mode = (data.get('mode') or 'tools').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    if mode not in ('words', 'tools'):
        return jsonify({'error': 'mode must be one of: words, tools'}), 400
    conn = catalog_db.get_connection()
    cur = conn.execute(
        'INSERT INTO albums (name, description, mode) VALUES (?, ?, ?)',
        (name, data.get('description', ''), mode)
    )
    album_id = cur.lastrowid
    conn.commit()
    row = conn.execute('SELECT * FROM albums WHERE id=?', (album_id,)).fetchone()
    conn.close()
    a = dict(row)
    a['total_count'] = _get_total_photo_count(catalog_db)
    a['filters'] = []
    a['sections'] = []
    return jsonify(a), 201


# ─── Album templates (documentation/specs/template-albums-spec.md) ──────────
# The logic is in album_templates.py; these routes validate, call it, then
# deploy and publish ONCE - where adding the same events one by one through
# import-event would rebuild the album folder after every event.

@app.route('/api/catalogs/<int:catalog_id>/album-templates/year', methods=['GET'])
def get_year_album_template(catalog_id):
    """The years that have events, with the months each would become (spec 5.1)."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    try:
        return jsonify(album_templates.year_options(conn))
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/albums/from-template', methods=['POST'])
def create_album_from_template(catalog_id):
    """
    Make an album from a template in one request (spec 5.2).

    Everything is validated before anything is written, and the album itself is
    one transaction: an error leaves nothing behind. Deploying and publishing
    come after the commit; a failure there is reported, not rolled back, as
    every other album route treats a deploy failure.
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    data = request.get_json(silent=True) or {}
    if data.get('template') not in album_templates.TEMPLATES:
        return jsonify({'error': 'unknown template'}), 400
    try:
        year = album_templates.validate_year(data.get('year'))
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    name = data.get('name')
    if name is not None and not str(name).strip():
        return jsonify({'error': 'name is required'}), 400

    deployment_id = data.get('deployment_id')
    if deployment_id in ('', None):
        deployment_id = None
    else:
        try:
            deployment_id = int(deployment_id)
        except (TypeError, ValueError):
            return jsonify({'error': 'deployment not found'}), 400
        if not _deployment_row(catalog_db, deployment_id):
            return jsonify({'error': 'deployment not found'}), 400

    conn = catalog_db.get_connection()
    try:
        album_id, summary = album_templates.create_year_album(
            conn, year, name=name, description=data.get('description'))
    except album_templates.NoEventsError as exc:
        return jsonify({'error': str(exc)}), 422
    except ValueError as exc:
        return jsonify({'error': str(exc)}), 400
    except Exception as exc:
        app.logger.exception('album template failed')
        return jsonify({'error': f'Could not create the album: {exc}'}), 500
    finally:
        conn.close()

    # Build the album folder BEFORE attaching it: a deployment publishing an
    # album that was never built lists it as skipped (spec 2.5).
    deploy_error = publish_error = None
    try:
        _deploy_album(catalog, catalog_db, album_id)
    except Exception as exc:
        deploy_error = str(exc)
        app.logger.warning('deploy_album failed after album template: %s', exc)
    if deployment_id is not None:
        try:
            _attach_album_to_deployment(catalog_db, deployment_id, album_id)
            _publish_deployment(catalog, catalog_db, deployment_id)
        except Exception as exc:
            publish_error = str(exc)
            app.logger.warning('publishing deployment %s failed after album template: %s',
                               deployment_id, exc)

    return jsonify({
        'album': _album_payload(catalog_id, catalog_db, album_id),
        'summary': summary,
        'deployment_id': deployment_id,
        'deploy_error': deploy_error,
        'publish_error': publish_error,
    }), 201


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>', methods=['GET'])
def get_album(catalog_id, album_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    a = _album_payload(catalog_id, catalog_db, album_id)
    if a is None:
        return jsonify({'error': 'Album not found'}), 404
    return jsonify(a)


def _album_payload(catalog_id, catalog_db, album_id):
    """The album as GET /albums/:id returns it, or None. Shared with the
    template route, which answers with the album it made."""
    conn = catalog_db.get_connection()
    row = conn.execute('SELECT * FROM albums WHERE id=?', (album_id,)).fetchone()
    if not row:
        conn.close()
        return None
    a = dict(row)
    filters = [dict(r) for r in conn.execute(
        'SELECT * FROM album_filters WHERE album_id=? ORDER BY position',
        (album_id,)
    ).fetchall()]
    sections = [dict(r) for r in conn.execute(
        'SELECT * FROM album_sections WHERE album_id=? ORDER BY position, id',
        (album_id,)
    ).fetchall()]
    # Counts and covers for the whole album in two queries, on the connection
    # that is already open. This used to open a fresh connection per section to
    # count its photos, which is N+1 round trips to draw one album - and the
    # card grid needs a cover per section on top of that (spec 4.5).
    counts = {r['section_id']: r['c'] for r in conn.execute(
        'SELECT sp.section_id, COUNT(*) AS c FROM section_photos sp '
        'JOIN album_sections s ON s.id = sp.section_id '
        'WHERE s.album_id=? GROUP BY sp.section_id', (album_id,)).fetchall()}
    covers = _resolve_section_covers(conn, album_id)
    categories = _category_payload(conn, album_id)
    conn.close()
    for sec in sections:
        sec['photo_count'] = counts.get(sec['id'], 0)
        resolved = covers.get(sec['id'])
        sec['resolved_cover_file_id'] = resolved
        sec['cover_thumb_url'] = (
            _thumb_url(catalog_id, resolved)
            if resolved else None)
    a['filters'] = filters
    a['sections'] = sections
    # Sent with the album so opening the editor is one request, not two.
    a['categories'] = categories
    a['total_count'] = _get_total_photo_count(catalog_db)
    return a


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>', methods=['PUT'])
def update_album(catalog_id, album_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    conn = catalog_db.get_connection()
    row = conn.execute('SELECT id FROM albums WHERE id=?', (album_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Album not found'}), 404
    fields, values = [], []
    for col in ('name', 'description', 'mode'):
        if col in data:
            if col == 'mode' and data[col] not in ('words', 'tools'):
                conn.close()
                return jsonify({'error': 'mode must be one of: words, tools'}), 400
            fields.append(f'{col}=?')
            values.append(data[col])
    if fields:
        values.append(album_id)
        conn.execute(
            f"UPDATE albums SET {', '.join(fields)}, updated_at=CURRENT_TIMESTAMP WHERE id=?",
            values
        )
        conn.commit()
    updated = dict(conn.execute('SELECT * FROM albums WHERE id=?', (album_id,)).fetchone())
    conn.close()
    try:
        _deploy_album(catalog, catalog_db, album_id)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("deploy_album failed on update: %s", _e)
    return jsonify(updated)


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>', methods=['DELETE'])
def delete_album(catalog_id, album_id):
    import shutil as _shutil
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    # Refused while a deployment publishes it, and the message names them - the
    # same rule deleting a storage route in use already follows. Letting it
    # through and rendering the album as "missing" would break a link someone
    # has already shared, silently and days later
    # (albums-enhancement-spec.md section 6.3).
    used_by = _deployments_using_album(catalog_db, album_id)
    if used_by:
        return jsonify({'error': f'This album is published by: '
                                 f'{", ".join(used_by)}. Remove it from '
                                 f'{"those deployments" if len(used_by) > 1 else "that deployment"} '
                                 f'first.'}), 409

    conn = catalog_db.get_connection()
    # Written out rather than left to ON DELETE CASCADE: this application never
    # enables PRAGMA foreign_keys, so every cascade in the album schema is inert
    # and deleting only the album row leaves its sections, their photo lists,
    # its filters and its categories behind as unreachable rows (spec 4.4).
    section_ids = [r['id'] for r in conn.execute(
        'SELECT id FROM album_sections WHERE album_id=?', (album_id,)).fetchall()]
    if section_ids:
        marks = ','.join('?' * len(section_ids))
        conn.execute(f'DELETE FROM section_photos WHERE section_id IN ({marks})',
                     section_ids)
    conn.execute('DELETE FROM album_sections   WHERE album_id=?', (album_id,))
    conn.execute('DELETE FROM album_categories WHERE album_id=?', (album_id,))
    # Explicitly, like every other cascade here: this application never enables
    # PRAGMA foreign_keys, so a REFERENCES clause would be inert and the rows
    # would outlive the album that explains them.
    _transfer_ids = [r['id'] for r in conn.execute(
        'SELECT id FROM wormhole_transfers WHERE album_id=?', (album_id,)).fetchall()]
    if _transfer_ids:
        _marks = ','.join('?' * len(_transfer_ids))
        conn.execute(f'DELETE FROM wormhole_transfer_files '
                     f'WHERE transfer_id IN ({_marks})', _transfer_ids)
    # Runners still sending this album are stopped first: their rows are about
    # to go, and a code for a deleted album must not keep working. Signalled
    # directly - wormhole_transfer.cancel() would open a second connection
    # while this one holds the write lock.
    for _r in conn.execute(
            'SELECT pid FROM wormhole_transfers WHERE album_id=? AND status IN '
            f"({','.join('?' * len(wormhole_transfer.ACTIVE_STATUSES))})",
            (album_id, *wormhole_transfer.ACTIVE_STATUSES)).fetchall():
        if wormhole_transfer._is_runner(_r['pid']):
            try:
                os.kill(_r['pid'], signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
    conn.execute('DELETE FROM wormhole_transfers WHERE album_id=?', (album_id,))
    conn.execute('DELETE FROM wormhole_transfer_batches WHERE album_id=?', (album_id,))
    conn.execute('DELETE FROM album_filters    WHERE album_id=?', (album_id,))
    conn.execute('DELETE FROM albums           WHERE id=?', (album_id,))
    conn.commit()
    conn.close()
    # Remove the deployed album folder if it exists
    album_dir_name = f"{album_id:05d}"
    album_dir = os.path.join(catalog.target_directory, "cache", "Albums", album_dir_name)
    try:
        if os.path.isdir(album_dir):
            _shutil.rmtree(album_dir)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("deploy_album: rmtree failed on delete: %s", _e)
    return jsonify({'message': 'Album deleted'})


@app.route(
    '/api/catalogs/<int:catalog_id>/albums/<int:album_id>/deploy',
    methods=['POST']
)
def deploy_album(catalog_id, album_id):
    """Explicitly (re-)deploy an album: create/refresh its cache folder and manifest."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    row = conn.execute('SELECT id FROM albums WHERE id=?', (album_id,)).fetchone()
    conn.close()
    if not row:
        return jsonify({'error': 'Album not found'}), 404
    try:
        stats = _deploy_album(catalog, catalog_db, album_id)
        return jsonify(stats)
    except Exception as e:
        return jsonify({'error': str(e)}), 500

# ---------------------------------------------------------------------------
# Album deployments
# ---------------------------------------------------------------------------
#
# A deployment is a named, shareable gallery holding one or more albums
# (documentation/specs/albums-enhancement-spec.md).
#
# It is a DIRECTORY, not a container image. Publishing writes an albums.json and
# one relative symlink per album into
#
#     <catalog.target_directory>/cache/Deployments/<slug>/
#
# and that is the whole publish step: no image build, no container restart, no
# downtime. The one long-lived gallery container finds the new directory on the
# next request. That is deliberate - the backend cannot start containers and
# must not be given the ability (local-storage-spec.md section 4E), so the design
# makes container work unnecessary rather than automating it.
#
# Everything here lives INSIDE the catalog, which is what lets a catalog folder
# carried to another machine bring its deployments with it.


# What a deployment packs when it has never been told. Reproduces today's
# bundle minus the 72px filmstrip thumbnail (spec 4.11).
DEFAULT_PACKED_RUNGS = ['1200', 'thumb320']


def _deployment_rungs(row):
    """A deployment's packed rungs, as a list. Never returns the stored text."""
    try:
        raw = row['packed_rungs']
    except (KeyError, IndexError):
        return list(DEFAULT_PACKED_RUNGS)
    if not raw:
        return list(DEFAULT_PACKED_RUNGS)
    try:
        value = _json.loads(raw)
    except (ValueError, TypeError):
        return list(DEFAULT_PACKED_RUNGS)
    if not isinstance(value, list):
        return list(DEFAULT_PACKED_RUNGS)
    # The base is not optional: a bundle without it has no image to show.
    out = [r for r in value if isinstance(r, str)]
    if BASE_RUNG not in out:
        out.insert(0, BASE_RUNG)
    return out


def _deployments_root(catalog):
    return os.path.join(catalog.target_directory, 'cache', 'Deployments')


def _deployment_dir(catalog, slug):
    return os.path.join(_deployments_root(catalog), slug)


def _remove_catalog_deployments(catalog):
    """
    Delete a catalog's deployments, leaving every file it holds alone.

    Called when a catalog is detached. Detaching keeps the directory and every
    artifact in it - photos, events, people, albums - because those describe the
    files and come back with them. A deployment is not that kind of thing. It is
    a decision to PUBLISH, addressed as <catalog-slug>/<deployment-slug>, and
    detaching the catalog revokes it: _write_catalogs_index() drops the catalog
    and the URLs stop resolving. Leaving the definition behind means re-adding
    the folder silently republishes shares the user believed were gone - and
    album_deployments carries `password_hash`, so what comes back is a live
    share definition with its credential.

    Catalog.delete() already draws this line for access grants, and deletes them
    rather than leaving unreachable state behind. This is the same call.

    NOTHING here touches an image. A deployment directory holds only
    albums.json, deployment.json, .auth.json and SYMLINKS into cache/Albums;
    shutil.rmtree unlinks a symlink rather than following it, so the album
    directories and the photos beneath them are untouched. cache/Albums itself
    is never removed - albums are artifacts and survive the detach.

    Never raises. A drive that is unplugged must not block detaching the
    catalog, so problems are collected and returned for the caller to report.

    Returns (removed_count, [warning, ...]).
    """
    import shutil as _shutil

    warnings = []
    target = getattr(catalog, 'target_directory', None)
    if not target:
        return 0, warnings

    db_path = os.path.join(target, 'workflow_results.db')
    if not os.path.exists(db_path):
        # Deliberately a plain existence check rather than CatalogDatabase():
        # constructing one runs init_db() and would CREATE a database inside a
        # directory we are in the middle of detaching.
        return 0, warnings

    rows = []
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        try:
            has_table = conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' "
                "AND name='album_deployments'").fetchone()
            if not has_table:
                return 0, warnings
            rows = [dict(r) for r in conn.execute(
                'SELECT id, slug, name FROM album_deployments')]
            if rows:
                # Both tables, explicitly. Foreign keys are inert in this
                # application, so an ON DELETE CASCADE would leave the
                # membership rows pointing at deployments that no longer exist.
                conn.execute('DELETE FROM album_deployment_albums')
                conn.execute('DELETE FROM album_deployments')
                conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        warnings.append(f'could not remove deployment records: {exc}')
        _logging.getLogger(__name__).warning(
            'catalog %s: could not remove deployment records: %s',
            getattr(catalog, 'id', '?'), exc)
        return 0, warnings

    for row in rows:
        directory = _deployment_dir(catalog, row['slug'])
        try:
            if os.path.isdir(directory):
                _shutil.rmtree(directory)
        except Exception as exc:
            warnings.append(f'could not remove the published files for '
                            f'"{row["name"]}": {exc}')
            _logging.getLogger(__name__).warning(
                'could not remove deployment directory %s: %s', directory, exc)

    # Tidy the now-empty parent, but only if it IS empty - a directory with
    # something unexpected in it is left for a person to look at.
    try:
        root = _deployments_root(catalog)
        if os.path.isdir(root) and not os.listdir(root):
            os.rmdir(root)
    except Exception:
        pass

    return len(rows), warnings


def _deployment_row(catalog_db, deployment_id):
    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT * FROM album_deployments WHERE id = ?',
                           (deployment_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _deployment_slugs(catalog_db, exclude_id=None):
    """
    Every slug already used in THIS catalog.

    The whole uniqueness check, and deliberately so. A deployment is addressed
    as <catalog-slug>/<deployment-slug>; the catalog half is UNIQUE in
    catalog_meta, so this half only has to be unique here. There is no
    cross-catalog scan anywhere in this file, and two catalogs may each hold a
    'summer-2019' without the URLs fighting (spec section 3.1).
    """
    conn = catalog_db.get_connection()
    try:
        rows = conn.execute(
            'SELECT id, slug FROM album_deployments').fetchall()
    finally:
        conn.close()
    return {r['slug'] for r in rows if r['id'] != exclude_id}


def _deployment_albums(catalog_db, deployment_id):
    """The albums in a deployment, in order, with their names and photo counts."""
    conn = catalog_db.get_connection()
    try:
        return [dict(r) for r in conn.execute('''
            SELECT a.id, a.name, a.description, da.position
            FROM album_deployment_albums da
            JOIN albums a ON a.id = da.album_id
            WHERE da.deployment_id = ?
            ORDER BY da.position, a.id
        ''', (deployment_id,)).fetchall()]
    finally:
        conn.close()


def _deployment_payload(catalog, catalog_db, row):
    """
    The shape the browser gets. Never the password hash - only whether one is
    set, which is the same rule deployment.json follows for the same reason.
    """
    albums = _deployment_albums(catalog_db, row['id'])
    directory = _deployment_dir(catalog, row['slug'])
    # No URL is returned or stored: a deployment does not know how it was
    # reached, and a baked-in URL would be wrong under the other route and wrong
    # again in a packed bundle (spec section 7.3). The browser composes both
    # links from the host it is already talking to, plus the two slugs below.
    return {
        'id': row['id'],
        'slug': row['slug'],
        'name': row['name'],
        'description': row['description'],
        'enabled': bool(row['enabled']),
        'has_password': bool(row['password_hash']),
        # "theme1/cardDeco<n>": the card's top strip, in colour while enabled
        # and its black-and-white twin while not (card_decorators.py).
        'decorator': row['decorator'] if 'decorator' in row.keys() else None,
        'created_by': row['created_by'],
        'created_at': row['created_at'],
        'updated_at': row['updated_at'],
        # The catalog half of the URL, so the UI can show the whole link without
        # a second request. Null when the catalog has no slug yet, which is the
        # one state that stops a deployment being reachable.
        'catalog_slug': catalog.slug,
        'album_count': len(albums),
        'albums': albums,
        # Whether the directory is actually on disk. Drift is possible - a hand
        # edit, a restore from backup - and POST /publish is the repair.
        'published': os.path.isdir(directory),
        # Which derivative sizes a bundle of this deployment carries.
        'packed_rungs': _deployment_rungs(row),
        # What this CATALOG can offer, so the card can show a size the catalog
        # has not generated as unavailable WITH ITS REASON rather than hiding
        # it: the fix is one click away in catalog settings, and hiding it makes
        # that undiscoverable (spec 9.2).
        'available_rungs': [BASE_RUNG] + list(catalog.image_rungs) + ['thumb320'],
    }
    # No URL is returned or stored: a deployment does not know how it was
    # reached, and a baked-in URL would be wrong under the other route and wrong
    # again in a packed bundle (spec section 7.3). The browser composes it from
    # the host it is already talking to.


def _catalog_slug_problem(catalog):
    """Why this catalog cannot host a deployment, or None."""
    if not catalog.slug:
        return (f'"{catalog.name}" has no URL name yet, so a deployment under it '
                f'would have no address. Set one on the Catalogs page first.')
    return None


def _auth_file_path(catalog, slug):
    return os.path.join(_deployment_dir(catalog, slug), '.auth.json')


def _write_auth_file(catalog, slug, password_hash):
    """
    Put the hash where the gallery can verify it, and nowhere else.

    Not in deployment.json, which is served to unauthenticated browsers. The
    leading dot plus the asset route's explicit dotfile refusal are two
    independent reasons this file cannot be fetched; mode 0600 is the third.
    """
    path = _auth_file_path(catalog, slug)
    if not password_hash:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # Opened with the mode set at creation rather than chmod-ed afterwards:
    # between the two there is a window where the hash is world-readable.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w', encoding='utf-8') as fh:
        _json.dump({'password_hash': password_hash,
                    'updated_at': _datetime.utcnow().isoformat()}, fh)
    os.chmod(path, 0o600)


def _publish_deployment(catalog, catalog_db, deployment_id) -> dict:
    """
    Write the deployment directory from the database. The single place that does.

    Idempotent by construction, so POST /publish is always safe and every
    mutation can simply call it rather than trying to patch the directory.

    Returns {slug, directory, linked, skipped, removed}. `skipped` names albums
    whose deployed folder is missing - never deployed, or the catalog was
    detached and re-added. One broken album must not take a whole deployment
    down, so they are reported rather than raised.
    """
    row = _deployment_row(catalog_db, deployment_id)
    if not row:
        raise ValueError(f'Deployment {deployment_id} not found')

    slug = row['slug']
    directory = _deployment_dir(catalog, slug)
    os.makedirs(directory, exist_ok=True)

    albums = _deployment_albums(catalog_db, deployment_id)
    albums_root = os.path.join(catalog.target_directory, 'cache', 'Albums')

    linked, skipped = [], []
    for album in albums:
        folder = f"{album['id']:05d}"
        album_dir = os.path.join(albums_root, folder)
        if not os.path.isdir(album_dir):
            skipped.append({'album_id': album['id'], 'name': album['name'],
                            'reason': 'not deployed yet'})
            continue
        link_path = os.path.join(directory, folder)
        # RELATIVE, always. An absolute target here is a backend-container path
        # that dangles on the host and inside the gallery container - the exact
        # failure _deploy_album already had to learn. Living inside the catalog
        # cache keeps the link short and wholly internal: ../../Albums/00001.
        target = os.path.relpath(album_dir, directory)
        if os.path.islink(link_path):
            if os.readlink(link_path) != target:
                os.unlink(link_path)
                os.symlink(target, link_path)
        elif os.path.lexists(link_path):
            # Something that is not a link is in the way. Leave it: removing an
            # unknown file inside someone's catalog is not this function's call.
            skipped.append({'album_id': album['id'], 'name': album['name'],
                            'reason': 'a non-link file already occupies that name'})
            continue
        else:
            os.symlink(target, link_path)
        linked.append(folder)

    # Photo counts for deployment.json, read from the album manifests that
    # _deploy_album already wrote - so this never re-queries what is already
    # published, and a count can never disagree with what the gallery serves.
    album_entries = []
    for album in albums:
        folder = f"{album['id']:05d}"
        if folder not in linked:
            continue
        count = None
        try:
            with open(os.path.join(directory, folder, 'album.json'),
                      encoding='utf-8') as fh:
                manifest = _json.load(fh)
            # photo_count in a format-2 index; the inline list in format 1.
            count = sum(sec['photo_count'] if 'photo_count' in sec
                        else len(sec.get('photos') or [])
                        for sec in manifest.get('sections') or [])
        except Exception:
            pass
        album_entries.append({'folder': folder, 'title': album['name'],
                              'photo_count': count})

    # albums.json is deliberately the shape the gallery already expected and
    # nothing ever wrote - a plain list of folder names (spec section 2.2). It
    # is also what makes the manifest authoritative: the gallery lists exactly
    # these albums, never whatever directories happen to be present, which is
    # both the access-control property and the one a static bundle needs.
    with open(os.path.join(directory, 'albums.json'), 'w', encoding='utf-8') as fh:
        _json.dump(linked, fh)

    with open(os.path.join(directory, 'deployment.json'), 'w', encoding='utf-8') as fh:
        _json.dump({
            'slug': slug,
            'name': row['name'],
            'description': row['description'],
            # What tools/pack_deployment.py is allowed to put in a bundle. Here
            # rather than in the database because the packer never opens one -
            # and because an album folder is SYMLINKED into a deployment and may
            # belong to several, so album.json cannot carry any one deployment's
            # choice (spec 10.1).
            'packed_rungs': _deployment_rungs(row),
            # A BOOLEAN, never the hash. This file is served to unauthenticated
            # browsers; it says whether to show a password form and nothing more.
            'requires_password': bool(row['password_hash']),
            'enabled': bool(row['enabled']),
            'albums': album_entries,
            'generated_at': _datetime.utcnow().isoformat() + 'Z',
        }, fh, ensure_ascii=False, indent=2)

    # Links for albums no longer in the deployment. Only links, and only ones we
    # would have made: anything else in the directory is left alone.
    import re as _re
    removed = 0
    keep = set(linked)
    for entry in os.listdir(directory):
        entry_path = os.path.join(directory, entry)
        if os.path.islink(entry_path) and entry not in keep and _re.fullmatch(r'\d+', entry):
            os.unlink(entry_path)
            removed += 1

    return {'slug': slug, 'directory': directory, 'linked': linked,
            'skipped': skipped, 'removed': removed}


def _write_catalogs_index():
    """
    Tell the gallery which directory each catalog slug names.

    The gallery is given the catalogs ROOT, and the URL names a catalog by SLUG,
    so it needs the one mapping it cannot derive: {slug: directory-name}. It is
    written HERE, by the only process that knows when a catalog is created,
    renamed or removed - and read there at boot and on a miss. One small file,
    rewritten rarely, and no directory scanning on either side.

    Catalogs are grouped by their parent directory and each root gets only the
    catalogs that live in it, because that root is exactly what a gallery mounts.
    Directory NAMES are stored, never absolute paths: the gallery joins them to
    its own CATALOGS_ROOT, which is a different absolute path in every container.

    Never fatal. A stale index costs one deployment its URL until the next write;
    a raised exception would cost the user their catalog.
    """
    by_root = {}
    for cat in Catalog.get_all():
        if not cat.target_directory or not cat.slug:
            continue
        parent = os.path.dirname(os.path.normpath(cat.target_directory))
        if not parent or not os.path.isdir(parent):
            continue
        by_root.setdefault(parent, {})[cat.slug] = os.path.basename(
            os.path.normpath(cat.target_directory))

    for root, mapping in by_root.items():
        try:
            path = os.path.join(root, 'catalogs.json')
            tmp = path + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as fh:
                _json.dump(mapping, fh, ensure_ascii=False, indent=2)
            # Replaced atomically: the gallery may read this file at any moment,
            # and half a JSON document is an outage rather than a stale answer.
            os.replace(tmp, path)
        except Exception as exc:
            _logging.getLogger(__name__).warning(
                'could not write %s/catalogs.json: %s', root, exc)


@app.route('/api/catalogs/<int:catalog_id>/deployments', methods=['GET'])
def get_deployments(catalog_id):
    """Every deployment in this catalog, with its albums and publish state."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    try:
        rows = [dict(r) for r in conn.execute(
            'SELECT * FROM album_deployments ORDER BY name').fetchall()]
    finally:
        conn.close()
    return jsonify({
        'catalog_slug': catalog.slug,
        # The reason a deployment cannot be created, when there is one, so the
        # page can say it once instead of every create failing with a 400.
        'catalog_slug_problem': _catalog_slug_problem(catalog),
        'deployments': [_deployment_payload(catalog, catalog_db, r) for r in rows],
    })


@app.route('/api/catalogs/<int:catalog_id>/deployments', methods=['POST'])
def create_deployment(catalog_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    problem = _catalog_slug_problem(catalog)
    if problem:
        return jsonify({'error': problem}), 409

    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'error': 'A name is required.'}), 400

    taken = _deployment_slugs(catalog_db)
    requested = (data.get('slug') or '').strip().lower()
    if requested:
        err = slugs.problem(requested)
        if err:
            return jsonify({'error': err}), 400
        if requested in taken:
            return jsonify({'error': f'This catalog already has a deployment at '
                                     f'"{requested}".'}), 409
        slug = requested
    else:
        slug = slugs.unique_slug(name, taken, fallback='deployment')

    user = auth.current_user()
    conn = catalog_db.get_connection()
    try:
        cur = conn.execute(
            'INSERT INTO album_deployments (slug, name, description, created_by, decorator) '
            'VALUES (?, ?, ?, ?, ?)',
            (slug, name, (data.get('description') or '').strip() or None,
             user['id'] if user else None,
             card_decorators.random_decorator(card_decorators.DEPLOYMENT_THEME)))
        conn.commit()
        deployment_id = cur.lastrowid
    except sqlite3.IntegrityError:
        return jsonify({'error': f'This catalog already has a deployment at '
                                 f'"{slug}".'}), 409
    finally:
        conn.close()

    try:
        _publish_deployment(catalog, catalog_db, deployment_id)
    except Exception as exc:
        # The row is already committed and nothing was published.
        #
        # Leaving it is what makes a retry fail with 'This catalog already has a
        # deployment at "x"' for a deployment the user was told had failed and
        # never saw. The slug is the scarce thing here, and reserving it for a
        # deployment that does not exist is worse than losing the row - which
        # holds nothing yet: it was created seconds ago and has no albums.
        #
        # This rollback belongs to CREATE only. When publishing fails for a
        # deployment that already existed, the row predates the failure and
        # deleting it would destroy something real; there the correct state is
        # published=false, which _deployment_payload already reports and
        # POST /publish repairs.
        import shutil as _shutil
        conn = catalog_db.get_connection()
        try:
            conn.execute('DELETE FROM album_deployment_albums WHERE deployment_id = ?',
                         (deployment_id,))
            conn.execute('DELETE FROM album_deployments WHERE id = ?', (deployment_id,))
            conn.commit()
        except Exception as cleanup_exc:
            _logging.getLogger(__name__).error(
                'create_deployment: publish failed AND the row could not be '
                'removed - slug "%s" is now reserved by a deployment that was '
                'never published: %s', slug, cleanup_exc)
        finally:
            conn.close()
        try:
            directory = _deployment_dir(catalog, slug)
            if os.path.isdir(directory):
                _shutil.rmtree(directory)
        except Exception as dir_exc:
            _logging.getLogger(__name__).warning(
                'create_deployment: could not remove the partial directory for '
                '"%s": %s', slug, dir_exc)
        _logging.getLogger(__name__).warning(
            'create_deployment: publishing "%s" failed, the deployment was not '
            'created: %s', slug, exc)
        return jsonify({'error': f'The deployment could not be published, so it '
                                 f'was not created: {exc}'}), 500

    return jsonify(_deployment_payload(
        catalog, catalog_db, _deployment_row(catalog_db, deployment_id))), 201


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>',
           methods=['PUT'])
def update_deployment(catalog_id, deployment_id):
    """Rename, re-describe, re-slug, enable or disable."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    row = _deployment_row(catalog_db, deployment_id)
    if not row:
        return jsonify({'error': 'Deployment not found'}), 404

    import shutil as _shutil
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or row['name']).strip()
    if not name:
        return jsonify({'error': 'A name is required.'}), 400
    description = data.get('description', row['description'])
    enabled = 1 if data.get('enabled', bool(row['enabled'])) else 0

    # Which sizes a bundle carries. Only rungs the CATALOG generates are
    # accepted: offering one it does not have would produce a bundle whose
    # manifest names files that were never made.
    packed = _deployment_rungs(row)
    if 'packed_rungs' in data:
        requested = data['packed_rungs']
        if not isinstance(requested, list):
            return jsonify({'error': 'packed_rungs must be a list'}), 400
        offered = set([BASE_RUNG] + list(catalog.image_rungs) + ['thumb320'])
        unknown = [r for r in requested if r not in offered]
        if unknown:
            return jsonify({'error':
                f'this catalog does not generate {", ".join(sorted(unknown))}. '
                f'Enable it in catalog settings first.'}), 400
        # The base is not optional - a bundle without it has no image to show.
        packed = [r for r in requested if isinstance(r, str)]
        if BASE_RUNG not in packed:
            packed.insert(0, BASE_RUNG)

    old_slug = row['slug']
    slug = (data.get('slug') or old_slug).strip().lower()
    if slug != old_slug:
        err = slugs.problem(slug)
        if err:
            return jsonify({'error': err}), 400
        if slug in _deployment_slugs(catalog_db, exclude_id=deployment_id):
            return jsonify({'error': f'This catalog already has a deployment at '
                                     f'"{slug}".'}), 409

    conn = catalog_db.get_connection()
    try:
        conn.execute(
            'UPDATE album_deployments SET slug = ?, name = ?, description = ?, '
            'enabled = ?, packed_rungs = ?, updated_at = CURRENT_TIMESTAMP '
            'WHERE id = ?',
            (slug, name, (description or '').strip() or None, enabled,
             _json.dumps(packed), deployment_id))
        conn.commit()
    except sqlite3.IntegrityError:
        return jsonify({'error': f'This catalog already has a deployment at '
                                 f'"{slug}".'}), 409
    finally:
        conn.close()

    # The directory is named by the slug, so a rename has to move it - and the
    # password file with it, since that lives inside. Moved rather than
    # republished from scratch so the hash is never briefly absent.
    if slug != old_slug:
        old_dir = _deployment_dir(catalog, old_slug)
        new_dir = _deployment_dir(catalog, slug)
        try:
            if os.path.isdir(old_dir) and not os.path.exists(new_dir):
                os.rename(old_dir, new_dir)
            elif os.path.isdir(old_dir):
                _shutil.rmtree(old_dir)
        except Exception as exc:
            _logging.getLogger(__name__).warning(
                'deployment rename: could not move %s: %s', old_dir, exc)

    _publish_deployment(catalog, catalog_db, deployment_id)
    return jsonify(_deployment_payload(
        catalog, catalog_db, _deployment_row(catalog_db, deployment_id)))


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>',
           methods=['DELETE'])
def delete_deployment(catalog_id, deployment_id):
    """Remove the deployment and its directory. The albums are untouched."""
    import shutil as _shutil
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    row = _deployment_row(catalog_db, deployment_id)
    if not row:
        return jsonify({'error': 'Deployment not found'}), 404

    conn = catalog_db.get_connection()
    try:
        # Both rows in one transaction. Foreign keys are inert in this
        # application, so an ON DELETE CASCADE would leave the membership rows
        # behind pointing at a deployment that no longer exists.
        conn.execute('DELETE FROM album_deployment_albums WHERE deployment_id = ?',
                     (deployment_id,))
        conn.execute('DELETE FROM album_deployments WHERE id = ?', (deployment_id,))
        conn.commit()
        # Its servers, their runs and their keys (publish-to-server spec 5).
        import publish
        publish.delete_for_deployment(conn, deployment_id)
    finally:
        conn.close()

    directory = _deployment_dir(catalog, row['slug'])
    try:
        if os.path.isdir(directory):
            _shutil.rmtree(directory)
    except Exception as exc:
        _logging.getLogger(__name__).warning(
            'could not remove deployment directory %s: %s', directory, exc)

    return jsonify({'message': f'Deployment "{row["name"]}" deleted'})


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/password',
           methods=['PUT'])
@auth.catalog_access('owner')
def set_deployment_password(catalog_id, deployment_id):
    """
    Set, change or remove a deployment's password.

    OWNER, not editor, and deliberately: this decides who can see the catalog's
    photographs from outside the application entirely, which is the same kind of
    decision as sharing the catalog itself.

    Enforced twice on purpose. auth._OWNER_RULE_FRAGMENTS matches '/password' in
    the URL rule, so the requirement holds even for a route added later that
    forgets the decorator; the decorator is here so the requirement is visible
    where the route is read.
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    row = _deployment_row(catalog_db, deployment_id)
    if not row:
        return jsonify({'error': 'Deployment not found'}), 404

    data = request.get_json(silent=True) or {}
    password = data.get('password')

    if password in (None, ''):
        password_hash = None
    else:
        complaint = auth.password_problem(password)
        if complaint:
            return jsonify({'error': complaint}), 400
        # Hashed HERE, by the same function that hashes user passwords, so there
        # is one answer to "how does this system hash a password". The gallery
        # only ever verifies.
        password_hash = auth.hash_password(password)

    conn = catalog_db.get_connection()
    try:
        conn.execute('UPDATE album_deployments SET password_hash = ?, '
                     'updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (password_hash, deployment_id))
        conn.commit()
    finally:
        conn.close()

    _publish_deployment(catalog, catalog_db, deployment_id)
    _write_auth_file(catalog, row['slug'], password_hash)
    return jsonify(_deployment_payload(
        catalog, catalog_db, _deployment_row(catalog_db, deployment_id)))


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/albums',
           methods=['GET'])
def get_deployment_albums(catalog_id, deployment_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not _deployment_row(catalog_db, deployment_id):
        return jsonify({'error': 'Deployment not found'}), 404
    return jsonify(_deployment_albums(catalog_db, deployment_id))


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/albums',
           methods=['POST'])
def add_deployment_album(catalog_id, deployment_id):
    """
    Add an album. One symlink and a rewritten albums.json - no build, no restart.
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not _deployment_row(catalog_db, deployment_id):
        return jsonify({'error': 'Deployment not found'}), 404

    data = request.get_json(silent=True) or {}
    album_id = data.get('album_id')
    if not album_id:
        return jsonify({'error': 'album_id is required.'}), 400

    if not _attach_album_to_deployment(catalog_db, deployment_id, album_id):
        return jsonify({'error': 'Album not found in this catalog'}), 404

    stats = _publish_deployment(catalog, catalog_db, deployment_id)
    payload = _deployment_payload(
        catalog, catalog_db, _deployment_row(catalog_db, deployment_id))
    payload['publish'] = stats
    return jsonify(payload)


def _attach_album_to_deployment(catalog_db, deployment_id, album_id):
    """
    Add the album at the end of the deployment. False if the album is not in
    this catalog. Idempotent. Does not publish - the caller does, once.
    """
    conn = catalog_db.get_connection()
    try:
        # Same database as album_deployments, so this is one query rather than
        # the cross-file lookup a cross-catalog deployment would have needed.
        if not conn.execute('SELECT 1 FROM albums WHERE id = ?', (album_id,)).fetchone():
            return False
        position = conn.execute(
            'SELECT COALESCE(MAX(position), -1) + 1 FROM album_deployment_albums '
            'WHERE deployment_id = ?', (deployment_id,)).fetchone()[0]
        conn.execute(
            'INSERT OR IGNORE INTO album_deployment_albums '
            '(deployment_id, album_id, position) VALUES (?, ?, ?)',
            (deployment_id, album_id, position))
        conn.commit()
        return True
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>'
           '/albums/<int:album_id>', methods=['DELETE'])
def remove_deployment_album(catalog_id, deployment_id, album_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not _deployment_row(catalog_db, deployment_id):
        return jsonify({'error': 'Deployment not found'}), 404

    conn = catalog_db.get_connection()
    try:
        conn.execute('DELETE FROM album_deployment_albums '
                     'WHERE deployment_id = ? AND album_id = ?',
                     (deployment_id, album_id))
        conn.commit()
    finally:
        conn.close()

    _publish_deployment(catalog, catalog_db, deployment_id)
    return jsonify(_deployment_payload(
        catalog, catalog_db, _deployment_row(catalog_db, deployment_id)))


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/publish',
           methods=['POST'])
def publish_deployment(catalog_id, deployment_id):
    """
    Rewrite the directory from the database.

    Every mutation above calls the publisher anyway, so this is not how a
    deployment normally reaches disk - it is the explicit repair for a directory
    that was hand-edited, restored from a backup, or lost.
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    row = _deployment_row(catalog_db, deployment_id)
    if not row:
        return jsonify({'error': 'Deployment not found'}), 404
    try:
        stats = _publish_deployment(catalog, catalog_db, deployment_id)
        # The password file lives in the directory, so a directory that was lost
        # took the hash with it. Rewriting it here is what makes publish a real
        # repair rather than one that leaves a locked deployment open.
        _write_auth_file(catalog, row['slug'], row['password_hash'])
        return jsonify(stats)
    except Exception as exc:
        return jsonify({'error': str(exc)}), 500


# ─── Publish to a server ──────────────────────────────────────────────────────
# documentation/specs/deployment-publish-to-server-spec.md, phase 3: servers,
# their keys and host keys, and the Preview run. Owner only (auth.py,
# '/servers' and '/publish-runs'). The work is publish.py; these routes hand it
# the catalog, the republish step and the remote.

def _interrupt_publish_runs():
    """At start-up: runs the previous process left going are interrupted
    (spec 3.6), which also frees their server for the next run."""
    import publish
    try:
        catalogs = Catalog.get_all()
    except Exception:
        return
    for catalog in catalogs:
        path = os.path.join(catalog.target_directory or '', 'workflow_results.db')
        if not catalog.target_directory or not os.path.exists(path):
            continue
        try:
            conn = sqlite3.connect(path, timeout=60)
            try:
                n = publish.interrupt_stale(conn)
            finally:
                conn.close()
            if n:
                print(f'[publish] {n} run(s) in {catalog.name} cut off by the restart')
        except Exception as exc:
            print(f'[publish] could not mark interrupted runs in {catalog.name}: {exc}')


# Here, after the function: module code runs top to bottom (test_module_ordering).
if os.environ.get('DISABLE_WORKFLOW_SCHEDULER', '').lower() not in ('1', 'true', 'yes'):
    threading.Thread(target=_interrupt_publish_runs, daemon=True,
                     name='publish-interrupted').start()


def _publish_remote(server, run_id):
    """The connection a run uses. One SSH connection shared by its steps."""
    import publish
    return publish.SSHRemote(server, control_path=f'/tmp/airlock-publish-{run_id}.sock')


def _publish_route(catalog_id, deployment_id):
    """(catalog, catalog_db, deployment row) or a ready error response."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return None, None, None, (jsonify({'error': 'Catalog not found'}), 404)
    row = _deployment_row(catalog_db, deployment_id)
    if not row:
        return None, None, None, (jsonify({'error': 'Deployment not found'}), 404)
    return catalog, catalog_db, row, None


def _publish_error(e):
    return jsonify({'error': e.message, 'code': e.code}), e.status


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/servers',
           methods=['GET', 'POST'])
def deployment_servers(catalog_id, deployment_id):
    import publish
    catalog, catalog_db, row, err = _publish_route(catalog_id, deployment_id)
    if err:
        return err
    conn = catalog_db.get_connection()
    try:
        if request.method == 'GET':
            return jsonify({'servers': publish.list_servers(conn, deployment_id)})
        data = request.get_json(silent=True) or {}
        host = str(data.get('host') or '').strip()
        try:
            server = publish.add_server(
                conn, deployment_id, data,
                f"airlock-publish {catalog.slug}/{row['slug']} -> {host}")
        except publish.PublishError as e:
            return _publish_error(e)
        return jsonify(server), 201
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/servers/<int:server_id>',
           methods=['PUT', 'DELETE'])
def deployment_server(catalog_id, deployment_id, server_id):
    import publish
    catalog, catalog_db, row, err = _publish_route(catalog_id, deployment_id)
    if err:
        return err
    conn = catalog_db.get_connection()
    try:
        if request.method == 'DELETE':
            server = publish.delete_server(conn, server_id, deployment_id)
            return jsonify({'deleted': server['id'],
                            'note': 'Remove its line from ~/.ssh/authorized_keys on the '
                                    'server too.'})
        return jsonify(publish.update_server(conn, server_id, deployment_id,
                                             request.get_json(silent=True) or {}))
    except publish.PublishError as e:
        return _publish_error(e)
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/servers/<int:server_id>/key',
           methods=['GET'])
def deployment_server_key(catalog_id, deployment_id, server_id):
    """The line to add on the server, again - the public half only."""
    import publish
    catalog, catalog_db, row, err = _publish_route(catalog_id, deployment_id)
    if err:
        return err
    conn = catalog_db.get_connection()
    try:
        server = publish.get_server(conn, server_id, deployment_id)
        return jsonify({'authorized_keys_line': publish.key_line(server)})
    except publish.PublishError as e:
        return _publish_error(e)
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/servers/<int:server_id>/pin',
           methods=['POST'])
def deployment_server_pin(catalog_id, deployment_id, server_id):
    """
    Without `confirm`: read the server's host key and return its fingerprint
    for the person to check - nothing is stored. With `confirm: true` and that
    `fingerprint`: store it, if the server still shows the same one (8.2).
    """
    import publish
    catalog, catalog_db, row, err = _publish_route(catalog_id, deployment_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    conn = catalog_db.get_connection()
    try:
        server = publish.get_server(conn, server_id, deployment_id)
        if not data.get('confirm'):
            return jsonify({'fingerprint': publish.scan(server),
                            'pinned': server.get('host_key')})
        return jsonify({'pinned': publish.pin(conn, server, data.get('fingerprint'))})
    except publish.PublishError as e:
        return _publish_error(e)
    finally:
        conn.close()


def _publish_fetch(url):
    """(status, headers, body) for the site check (spec 6.6). Never follows a
    redirect: the first check IS the redirect."""
    import urllib.error
    import urllib.request

    class _NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *a, **k):
            return None

    opener = urllib.request.build_opener(_NoRedirect)
    req = urllib.request.Request(url, headers={'User-Agent': 'airlock-publish-check'})
    try:
        with opener.open(req, timeout=20) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, \
                r.read(65536).decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in e.headers.items()}, \
            e.read(65536).decode('utf-8', 'replace')


def _start_publish_run(catalog_id, deployment_id, server_id, kind):
    """Start a Preview or a Publish run in the background. 202 {run_id}."""
    import publish
    catalog, catalog_db, row, err = _publish_route(catalog_id, deployment_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    db_path = os.path.join(catalog.target_directory, 'workflow_results.db')
    conn = catalog_db.get_connection()
    try:
        server = publish.get_server(conn, server_id, deployment_id)
        first = not conn.execute(
            "SELECT 1 FROM deployment_publish_runs WHERE server_id = ? AND kind = 'publish' "
            "AND status = 'succeeded' LIMIT 1", (server_id,)).fetchone()
        run_id = publish.start_run(conn, server_id, kind, _current_user_id())
    except publish.PublishError as e:
        return _publish_error(e)
    finally:
        conn.close()

    def republish():
        stats = _publish_deployment(catalog, catalog_db, deployment_id)
        _write_auth_file(catalog, row['slug'], row['password_hash'])
        return stats

    ctx = {'republish': republish,
           'conn_factory': lambda: sqlite3.connect(db_path, timeout=60),
           'catalogs_root': os.path.dirname(os.path.normpath(catalog.target_directory)),
           'catalog_slug': catalog.slug, 'catalog_dir': catalog.target_directory,
           'deployment_id': deployment_id, 'deployment_slug': row['slug'],
           'first_publish': first, 'locked': bool(row['password_hash']),
           'confirm_deletions': bool(data.get('confirm_deletions')),
           'carry_certificates_from': data.get('carry_certificates_from'),
           'fetch': _publish_fetch}
    run = publish.Run(db_path, run_id)
    publish.register(run)

    def work():
        try:
            try:
                remote = _publish_remote(server, run_id)
            except publish.PublishError as e:
                run.finish('failed', error=e.message)
                return
            publish.execute(run, kind, ctx, server, remote)
        finally:
            publish.unregister(run)

    threading.Thread(target=work, name=f'publish-{catalog_id}-{run_id}', daemon=True).start()
    return jsonify({'run_id': run_id}), 202


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/servers/<int:server_id>/preview',
           methods=['POST'])
def deployment_server_preview(catalog_id, deployment_id, server_id):
    """Start a Preview run (spec 6.1-6.4): what a publish would send and delete."""
    return _start_publish_run(catalog_id, deployment_id, server_id, 'preview')


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/servers/<int:server_id>/publish',
           methods=['POST'])
def deployment_server_publish(catalog_id, deployment_id, server_id):
    """
    Start a Publish run (spec 6.5, 6.6): the preview, then media, album data
    and deletions, then a check of the live site. {confirm_deletions: true}
    when a previous run asked for it.
    """
    return _start_publish_run(catalog_id, deployment_id, server_id, 'publish')


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/servers/<int:server_id>/setup',
           methods=['POST'])
def deployment_server_setup(catalog_id, deployment_id, server_id):
    """
    Start a Set up run (spec 7): an EMPTY server folder made a running gallery -
    checks, the first send, .env made on the server, optionally
    {carry_certificates_from: "<docker volume>"}, start, check the site.
    Refuses a folder that already holds a gallery.
    """
    return _start_publish_run(catalog_id, deployment_id, server_id, 'setup')


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/servers/<int:server_id>/runs',
           methods=['GET'])
def deployment_server_runs(catalog_id, deployment_id, server_id):
    import publish
    catalog, catalog_db, row, err = _publish_route(catalog_id, deployment_id)
    if err:
        return err
    conn = catalog_db.get_connection()
    try:
        publish.get_server(conn, server_id, deployment_id)
        rows = conn.execute(
            'SELECT id, kind, status, phase, error, started_at, finished_at, files_new, '
            'files_changed, files_deleted, bytes_sent, progress_json, result_json '
            'FROM deployment_publish_runs WHERE server_id = ? ORDER BY id DESC LIMIT 20',
            (server_id,)).fetchall()
        return jsonify({'runs': [publish._run_dict(r) for r in rows]})
    except publish.PublishError as e:
        return _publish_error(e)
    finally:
        conn.close()


def _publish_run_or_error(catalog_db, deployment_id, run_id):
    import publish
    conn = catalog_db.get_connection()
    try:
        ids = {r[0] for r in conn.execute(
            'SELECT id FROM deployment_servers WHERE deployment_id = ?', (deployment_id,))}
        return publish.get_run(conn, run_id, ids)
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/publish-runs/<int:run_id>',
           methods=['GET'])
def deployment_publish_run(catalog_id, deployment_id, run_id):
    """A run: status, phase, progress, counts, result and the newest log lines."""
    import publish
    catalog, catalog_db, row, err = _publish_route(catalog_id, deployment_id)
    if err:
        return err
    try:
        run = _publish_run_or_error(catalog_db, deployment_id, run_id)
    except publish.PublishError as e:
        return _publish_error(e)
    run['log'] = '\n'.join((run.get('log') or '').splitlines()[-200:])
    return jsonify(run)


@app.route('/api/catalogs/<int:catalog_id>/deployments/<int:deployment_id>/publish-runs/<int:run_id>/stop',
           methods=['POST'])
def deployment_publish_run_stop(catalog_id, deployment_id, run_id):
    import publish
    catalog, catalog_db, row, err = _publish_route(catalog_id, deployment_id)
    if err:
        return err
    try:
        run = _publish_run_or_error(catalog_db, deployment_id, run_id)
    except publish.PublishError as e:
        return _publish_error(e)
    if run['status'] not in ('queued', 'running'):
        return jsonify({'error': 'This run has already finished.', 'code': 'finished'}), 409
    return jsonify({'stopping': publish.stop_run(run_id)})


def _deployments_using_album(catalog_db, album_id):
    """Names of the deployments that publish this album. One query (spec 6.3)."""
    conn = catalog_db.get_connection()
    try:
        return [r['name'] for r in conn.execute('''
            SELECT d.name FROM album_deployments d
              JOIN album_deployment_albums a ON a.deployment_id = d.id
             WHERE a.album_id = ?
        ''', (album_id,)).fetchall()]
    except sqlite3.Error:
        # A catalog database from before deployments existed. Nothing publishes
        # this album, because nothing can.
        return []
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Album Sections API
# ---------------------------------------------------------------------------

@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/sections', methods=['GET'])
def get_album_sections(catalog_id, album_id):
    """List all sections for an album, each with its photo count."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    row = conn.execute('SELECT id FROM albums WHERE id=?', (album_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Album not found'}), 404
    sections = [dict(r) for r in conn.execute(
        'SELECT * FROM album_sections WHERE album_id=? ORDER BY position, id', (album_id,)
    ).fetchall()]
    # One pass for the whole album rather than one query per card (spec 4.5).
    covers = _resolve_section_covers(conn, album_id)
    for s in sections:
        cnt = conn.execute(
            'SELECT COUNT(*) AS c FROM section_photos WHERE section_id=?', (s['id'],)
        ).fetchone()
        s['photo_count'] = cnt['c'] if cnt else 0
        # cover_file_id stays the raw column so the UI can tell an explicit
        # choice from an inherited one; resolved_cover_file_id is what to draw.
        resolved = covers.get(s['id'])
        s['resolved_cover_file_id'] = resolved
        s['cover_thumb_url'] = (
            _thumb_url(catalog_id, resolved)
            if resolved else None)
    conn.close()
    return jsonify(sections)


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/sections', methods=['POST'])
def create_album_section(catalog_id, album_id):
    """Add a new section to an album."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    conn = catalog_db.get_connection()
    row = conn.execute('SELECT id FROM albums WHERE id=?', (album_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Album not found'}), 404
    max_pos = conn.execute(
        'SELECT COALESCE(MAX(position), 0) AS mp FROM album_sections WHERE album_id=?',
        (album_id,)
    ).fetchone()['mp']
    description = data.get('description', '') or None
    cur = conn.execute(
        'INSERT INTO album_sections (album_id, name, description, position) VALUES (?, ?, ?, ?)',
        (album_id, name, description, max_pos + 1)
    )
    section_id = cur.lastrowid
    conn.commit()
    section = dict(conn.execute(
        'SELECT * FROM album_sections WHERE id=?', (section_id,)
    ).fetchone())
    conn.close()
    section['photo_count'] = 0
    return jsonify(section), 201


@app.route(
    '/api/catalogs/<int:catalog_id>/albums/<int:album_id>/sections/<int:section_id>',
    methods=['PUT']
)
def update_album_section(catalog_id, album_id, section_id):
    """Rename/update a section."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    name = data.get('name', '').strip()
    if not name:
        return jsonify({'error': 'name is required'}), 400
    conn = catalog_db.get_connection()
    row = conn.execute(
        'SELECT id FROM album_sections WHERE id=? AND album_id=?', (section_id, album_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Section not found'}), 404
    description = data.get('description', '') or None
    conn.execute(
        'UPDATE album_sections SET name=?, description=? WHERE id=?',
        (name, description, section_id)
    )
    conn.commit()
    section = dict(conn.execute(
        'SELECT * FROM album_sections WHERE id=?', (section_id,)
    ).fetchone())
    cnt = conn.execute(
        'SELECT COUNT(*) AS c FROM section_photos WHERE section_id=?', (section_id,)
    ).fetchone()
    conn.close()
    section['photo_count'] = cnt['c'] if cnt else 0
    return jsonify(section)


# ── Album categories ─────────────────────────────────────────────────────────
# A tree of categories inside one album, holding sections
# (album-gallery-enhancement-spec.md section 4).
#
# The root is NOT a row: parent_id / category_id NULL means "directly under the
# album", and the root is labelled with the album's name. So every helper below
# treats None as a legitimate node id meaning "the root", not as a missing value.

MAX_CATEGORY_DEPTH = 5      # root counted as 0
MAX_CATEGORY_NAME = 100


def _category_rows(conn, album_id):
    """Every category of one album, in sibling order.

    ORDER BY position, id and not position alone: positions carry no UNIQUE
    constraint (spec 2.6), so duplicates are possible and a stable tiebreak is
    what stops two categories swapping places between two reads.
    """
    return [dict(r) for r in conn.execute(
        'SELECT * FROM album_categories WHERE album_id=? ORDER BY position, id',
        (album_id,)).fetchall()]


def _category_in_album(conn, album_id, category_id):
    """The category row, only if it belongs to THIS album.

    Category ids are unique per catalog database, not per album, so an id from
    another album is a well-formed id that points somewhere real. Without this
    check a stale browser tab is enough to graft a subtree into a different
    album, silently (spec 4.6).
    """
    if category_id is None:
        return None
    return conn.execute(
        'SELECT * FROM album_categories WHERE id=? AND album_id=?',
        (category_id, album_id)).fetchone()


def _ancestor_ids(by_id, category_id):
    """Ids from category_id up to (not including) the root.

    Stops on a repeat rather than trusting the data: the cycle guard below is
    what should prevent one, but a walk that loops forever on bad data would
    take the request thread with it.
    """
    out, cur = [], category_id
    while cur is not None and cur not in out:
        out.append(cur)
        row = by_id.get(cur)
        cur = row['parent_id'] if row else None
    return out


def _descendant_ids(rows, category_id):
    """category_id and everything beneath it."""
    children = {}
    for r in rows:
        children.setdefault(r['parent_id'], []).append(r['id'])
    found, stack = set(), [category_id]
    while stack:
        cid = stack.pop()
        if cid in found:
            continue
        found.add(cid)
        stack.extend(children.get(cid, []))
    return found


def _category_payload(conn, album_id):
    """
    Every category with its three counts, computed in ONE pass.

    Per-node queries would be N round trips to render one tree, which is the
    same mistake EventModel.resolve_covers already refused to make for event
    cards (models.py).
    """
    rows = _category_rows(conn, album_id)
    sections = conn.execute(
        'SELECT s.id, s.category_id, '
        '  (SELECT COUNT(*) FROM section_photos sp WHERE sp.section_id = s.id) AS photos '
        'FROM album_sections s WHERE s.album_id=?', (album_id,)).fetchall()

    direct, direct_photos = {}, {}
    for sec in sections:
        cid = sec['category_id']
        direct[cid] = direct.get(cid, 0) + 1
        direct_photos[cid] = direct_photos.get(cid, 0) + (sec['photos'] or 0)

    children = {}
    for r in rows:
        children.setdefault(r['parent_id'], []).append(r['id'])

    out = []
    for r in rows:
        subtree = _descendant_ids(rows, r['id'])
        out.append({
            'id': r['id'],
            'parent_id': r['parent_id'],
            'name': r['name'],
            'position': r['position'],
            'section_count': direct.get(r['id'], 0),
            'descendant_section_count': sum(direct.get(c, 0) for c in subtree),
            'photo_count': sum(direct_photos.get(c, 0) for c in subtree),
        })
    return out


def _validate_category_name(name, conn, album_id, parent_id, exclude_id=None):
    """None when the name is usable here, else (message, status)."""
    name = (name or '').strip()
    if not name:
        return ('name is required', 400)
    if len(name) > MAX_CATEGORY_NAME:
        return (f'name must be {MAX_CATEGORY_NAME} characters or fewer', 400)
    # Enforced here rather than by a UNIQUE constraint: with parent_id NULL at
    # the root, SQLite would not enforce it where it matters most (spec 2.6).
    clash = conn.execute(
        'SELECT id FROM album_categories '
        'WHERE album_id=? AND parent_id IS ? AND LOWER(name)=LOWER(?) AND id IS NOT ?',
        (album_id, parent_id, name, exclude_id)).fetchone()
    if clash:
        return ('a category with this name already exists here', 409)
    return None


def _redeploy_quietly(catalog, catalog_db, album_id, what):
    """
    Refresh album.json after a change, without letting a publish problem fail
    the change the user actually asked for. Same contract
    import_event_to_section already uses: log it, carry on (spec 5.4).
    """
    try:
        _deploy_album(catalog, catalog_db, album_id)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning('deploy_album failed after %s: %s', what, _e)


def _album_or_404(conn, album_id):
    return conn.execute('SELECT id, name FROM albums WHERE id=?', (album_id,)).fetchone()


def _resolve_section_covers(conn, album_id):
    """
    {section_id: file_id or None} for every section of an album, in one pass.

    Order, per spec 4.5:
      1. the section's own cover_file_id, IF that file is still in the section
      2. the source event's cover, IF that file is in the section
      3. the section's first photo
      4. nothing - the card shows a placeholder

    Step 1 is conditional for the same reason EventModel.resolve_covers re-rolls
    a dangling id: foreign keys are inert here, so nothing clears a cover when
    its photo is deleted or removed from the section, and a stored id is a hint
    rather than a fact.

    Resolved in bulk deliberately. A card grid that asks per card is N requests
    to open one album, which is the cost resolve_covers was written to avoid.
    """
    sections = conn.execute(
        'SELECT id, cover_file_id, source_event_id FROM album_sections WHERE album_id=?',
        (album_id,)).fetchall()
    if not sections:
        return {}

    ids = [s['id'] for s in sections]
    marks = ','.join('?' * len(ids))

    # Every (section, file) pair, plus each section's first photo.
    members = {}
    first = {}
    for r in conn.execute(
            f'SELECT section_id, file_id, position FROM section_photos '
            f'WHERE section_id IN ({marks}) ORDER BY section_id, position, file_id', ids):
        members.setdefault(r['section_id'], set()).add(r['file_id'])
        first.setdefault(r['section_id'], r['file_id'])

    # The source events' own covers, for step 2.
    event_ids = [s['source_event_id'] for s in sections if s['source_event_id']]
    event_cover = {}
    if event_ids:
        em = ','.join('?' * len(event_ids))
        for r in conn.execute(
                f'SELECT id, cover_file_id FROM events WHERE id IN ({em})', event_ids):
            if r['cover_file_id']:
                event_cover[r['id']] = r['cover_file_id']

    out = {}
    for sec in sections:
        sid = sec['id']
        have = members.get(sid, set())
        chosen = None
        if sec['cover_file_id'] and sec['cover_file_id'] in have:
            chosen = sec['cover_file_id']
        if chosen is None:
            ev_cover = event_cover.get(sec['source_event_id'])
            if ev_cover and ev_cover in have:
                chosen = ev_cover
        if chosen is None:
            chosen = first.get(sid)
        out[sid] = chosen
    return out


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/categories',
           methods=['GET'])
def get_album_categories(catalog_id, album_id):
    """
    The album's categories, flat.

    Flat rather than nested on purpose: the client builds the tree, a flat list
    cannot be malformed by a cycle, and it is the same shape album.json carries
    (spec 5.1).
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    album = _album_or_404(conn, album_id)
    if not album:
        conn.close()
        return jsonify({'error': 'Album not found'}), 404
    payload = _category_payload(conn, album_id)
    root_sections = conn.execute(
        'SELECT COUNT(*) AS c FROM album_sections WHERE album_id=? AND category_id IS NULL',
        (album_id,)).fetchone()['c']
    conn.close()
    # The root is not a row, so its label travels with the list rather than in
    # it. The client needs both to draw the tree.
    return jsonify({'root': {'name': album['name'], 'section_count': root_sections},
                    'categories': payload})


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/categories',
           methods=['POST'])
def create_album_category(catalog_id, album_id):
    """Add a sub-category under another category, or under the root."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    parent_id = data.get('parent_id')
    conn = catalog_db.get_connection()
    try:
        if not _album_or_404(conn, album_id):
            return jsonify({'error': 'Album not found'}), 404

        rows = _category_rows(conn, album_id)
        by_id = {r['id']: r for r in rows}
        if parent_id is not None:
            if not _category_in_album(conn, album_id, parent_id):
                return jsonify({'error': 'Parent category not found in this album'}), 400
            if len(_ancestor_ids(by_id, parent_id)) >= MAX_CATEGORY_DEPTH:
                return jsonify({'error': f'categories may not nest deeper than '
                                         f'{MAX_CATEGORY_DEPTH} levels'}), 409

        err = _validate_category_name(data.get('name'), conn, album_id, parent_id)
        if err:
            return jsonify({'error': err[0]}), err[1]

        name = data['name'].strip()
        max_pos = conn.execute(
            'SELECT COALESCE(MAX(position), 0) AS mp FROM album_categories '
            'WHERE album_id=? AND parent_id IS ?', (album_id, parent_id)).fetchone()['mp']
        cur = conn.execute(
            'INSERT INTO album_categories (album_id, parent_id, name, position) '
            'VALUES (?, ?, ?, ?)', (album_id, parent_id, name, max_pos + 1))
        category_id = cur.lastrowid
        conn.commit()
        created = next(c for c in _category_payload(conn, album_id)
                       if c['id'] == category_id)
    finally:
        conn.close()
    _redeploy_quietly(catalog, catalog_db, album_id, 'create_album_category')
    return jsonify(created), 201


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/categories/<int:category_id>',
           methods=['PUT'])
def update_album_category(catalog_id, album_id, category_id):
    """Rename a category, and/or move it under a different parent."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    conn = catalog_db.get_connection()
    try:
        row = _category_in_album(conn, album_id, category_id)
        if not row:
            return jsonify({'error': 'Category not found'}), 404

        rows = _category_rows(conn, album_id)
        by_id = {r['id']: r for r in rows}
        moving = 'parent_id' in data
        new_parent = data.get('parent_id') if moving else row['parent_id']

        if moving and new_parent is not None:
            if not _category_in_album(conn, album_id, new_parent):
                return jsonify({'error': 'Parent category not found in this album'}), 400
            # A category moved under its own descendant detaches that whole
            # subtree from the root - it still exists, is reachable from
            # nothing, and renders nowhere.
            if new_parent == category_id or category_id in _ancestor_ids(by_id, new_parent):
                return jsonify({'error': 'a category cannot be moved inside itself'}), 409
            subtree = _descendant_ids(rows, category_id)
            deepest = max(len(_ancestor_ids(by_id, c)) for c in subtree)
            own = len(_ancestor_ids(by_id, category_id))
            # depth of the deepest node, re-based under the new parent
            if (deepest - own) + len(_ancestor_ids(by_id, new_parent)) + 1 > MAX_CATEGORY_DEPTH:
                return jsonify({'error': f'that move would nest deeper than '
                                         f'{MAX_CATEGORY_DEPTH} levels'}), 409

        name = data.get('name', row['name'])
        err = _validate_category_name(name, conn, album_id, new_parent,
                                      exclude_id=category_id)
        if err:
            return jsonify({'error': err[0]}), err[1]

        if moving and new_parent != row['parent_id']:
            max_pos = conn.execute(
                'SELECT COALESCE(MAX(position), 0) AS mp FROM album_categories '
                'WHERE album_id=? AND parent_id IS ?',
                (album_id, new_parent)).fetchone()['mp']
            conn.execute('UPDATE album_categories SET parent_id=?, position=?, '
                         'name=?, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                         (new_parent, max_pos + 1, name.strip(), category_id))
        else:
            conn.execute('UPDATE album_categories SET name=?, '
                         'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                         (name.strip(), category_id))
        conn.commit()
        updated = next(c for c in _category_payload(conn, album_id)
                       if c['id'] == category_id)
    finally:
        conn.close()
    _redeploy_quietly(catalog, catalog_db, album_id, 'update_album_category')
    return jsonify(updated)


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/categories/<int:category_id>',
           methods=['DELETE'])
def delete_album_category(catalog_id, album_id, category_id):
    """
    Delete a category. Refused by default when it holds anything.

    Three modes, because the two ways to resolve a non-empty category are not
    interchangeable and picking one silently would be the wrong call:

      (none)    refuse with the counts, so the UI can ask a real question
      reparent  children move up one level; nothing is lost
      cascade   the subtree and its sections go, INCLUDING their curated photo
                lists - this destroys work and must be asked for explicitly

    Every delete here is written out rather than left to ON DELETE CASCADE:
    this application never enables PRAGMA foreign_keys, so the clauses in the
    schema do not run (spec 2.5, 4.4).
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    mode = (request.args.get('mode') or '').strip().lower()
    if mode not in ('', 'reparent', 'cascade'):
        return jsonify({'error': "mode must be 'reparent' or 'cascade'"}), 400

    conn = catalog_db.get_connection()
    try:
        row = _category_in_album(conn, album_id, category_id)
        if not row:
            return jsonify({'error': 'Category not found'}), 404

        rows = _category_rows(conn, album_id)
        subtree = _descendant_ids(rows, category_id)
        marks = ','.join('?' * len(subtree))
        child_categories = sum(1 for r in rows if r['parent_id'] == category_id)
        direct_sections = conn.execute(
            'SELECT COUNT(*) AS c FROM album_sections WHERE album_id=? AND category_id=?',
            (album_id, category_id)).fetchone()['c']
        descendant_sections = conn.execute(
            f'SELECT COUNT(*) AS c FROM album_sections WHERE album_id=? '
            f'AND category_id IN ({marks})', (album_id, *subtree)).fetchone()['c']

        if not mode and (child_categories or descendant_sections):
            return jsonify({
                'error': 'category is not empty',
                'child_categories': child_categories,
                'sections': direct_sections,
                'descendant_sections': descendant_sections,
            }), 409

        if mode == 'cascade':
            section_ids = [r['id'] for r in conn.execute(
                f'SELECT id FROM album_sections WHERE album_id=? '
                f'AND category_id IN ({marks})', (album_id, *subtree)).fetchall()]
            if section_ids:
                sm = ','.join('?' * len(section_ids))
                conn.execute(f'DELETE FROM section_photos WHERE section_id IN ({sm})',
                             section_ids)
                conn.execute(f'DELETE FROM album_sections WHERE id IN ({sm})', section_ids)
            conn.execute(f'DELETE FROM album_categories WHERE id IN ({marks})',
                         tuple(subtree))
        else:
            # 'reparent', and also the empty case - where both statements are
            # no-ops and the category is simply removed.
            conn.execute('UPDATE album_sections SET category_id=? '
                         'WHERE album_id=? AND category_id=?',
                         (row['parent_id'], album_id, category_id))
            conn.execute('UPDATE album_categories SET parent_id=? '
                         'WHERE album_id=? AND parent_id=?',
                         (row['parent_id'], album_id, category_id))
            conn.execute('DELETE FROM album_categories WHERE id=?', (category_id,))
        conn.commit()
    finally:
        conn.close()
    _redeploy_quietly(catalog, catalog_db, album_id, 'delete_album_category')
    return jsonify({'message': 'Category deleted', 'mode': mode or 'empty'})


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>'
           '/sections/<int:section_id>/category', methods=['PUT'])
def set_section_category(catalog_id, album_id, section_id):
    """File a section under a category. category_id null puts it at the root."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    category_id = data.get('category_id')
    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT id FROM album_sections WHERE id=? AND album_id=?',
                           (section_id, album_id)).fetchone()
        if not row:
            return jsonify({'error': 'Section not found'}), 404
        if category_id is not None and not _category_in_album(conn, album_id, category_id):
            return jsonify({'error': 'Category not found in this album'}), 400
        conn.execute('UPDATE album_sections SET category_id=? WHERE id=?',
                     (category_id, section_id))
        conn.commit()
    finally:
        conn.close()
    _redeploy_quietly(catalog, catalog_db, album_id, 'set_section_category')
    return jsonify({'section_id': section_id, 'category_id': category_id})


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>'
           '/sections/<int:section_id>/cover', methods=['PUT'])
def set_section_cover(catalog_id, album_id, section_id):
    """
    Choose the photo that fronts a section's card.

    The file must be IN this section. That check is what stops a section being
    given a cover it does not contain - the same rule set_event_cover already
    applies for events (spec 4.5).
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    file_id = data.get('file_id')
    if not file_id:
        return jsonify({'error': 'file_id is required'}), 400
    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT id FROM album_sections WHERE id=? AND album_id=?',
                           (section_id, album_id)).fetchone()
        if not row:
            return jsonify({'error': 'Section not found'}), 404
        in_section = conn.execute(
            'SELECT 1 FROM section_photos WHERE section_id=? AND file_id=?',
            (section_id, file_id)).fetchone()
        if not in_section:
            return jsonify({'error': 'that photo is not in this section'}), 400
        conn.execute('UPDATE album_sections SET cover_file_id=? WHERE id=?',
                     (file_id, section_id))
        conn.commit()
    finally:
        conn.close()
    _redeploy_quietly(catalog, catalog_db, album_id, 'set_section_cover')
    return jsonify({'section_id': section_id, 'cover_file_id': file_id})


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>'
           '/sections/<int:section_id>/cover', methods=['DELETE'])
def clear_section_cover(catalog_id, album_id, section_id):
    """Drop the explicit cover; the fallback chain picks the next candidate."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT id FROM album_sections WHERE id=? AND album_id=?',
                           (section_id, album_id)).fetchone()
        if not row:
            return jsonify({'error': 'Section not found'}), 404
        conn.execute('UPDATE album_sections SET cover_file_id=NULL WHERE id=?',
                     (section_id,))
        conn.commit()
    finally:
        conn.close()
    _redeploy_quietly(catalog, catalog_db, album_id, 'clear_section_cover')
    return jsonify({'section_id': section_id, 'cover_file_id': None})


@app.route(
    '/api/catalogs/<int:catalog_id>/albums/<int:album_id>/sections/<int:section_id>',
    methods=['DELETE']
)
def delete_album_section(catalog_id, album_id, section_id):
    """Delete a section (and its photo assignments). The last section cannot be deleted."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    row = conn.execute(
        'SELECT id FROM album_sections WHERE id=? AND album_id=?', (section_id, album_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Section not found'}), 404
    conn.execute('DELETE FROM album_sections WHERE id=?', (section_id,))
    conn.commit()
    # Re-number remaining sections to close gaps
    remaining = conn.execute(
        'SELECT id FROM album_sections WHERE album_id=? ORDER BY position', (album_id,)
    ).fetchall()
    for idx, r in enumerate(remaining, start=1):
        conn.execute('UPDATE album_sections SET position=? WHERE id=?', (idx, r['id']))
    conn.commit()
    conn.close()
    return jsonify({'message': 'Section deleted'})


# ---------------------------------------------------------------------------
# Section Photos API
# ---------------------------------------------------------------------------

@app.route(
    '/api/catalogs/<int:catalog_id>/albums/<int:album_id>/sections/<int:section_id>/photos',
    methods=['GET']
)
def get_section_photos(catalog_id, album_id, section_id):
    """Return the photos assigned to a section, including the web-friendly alternative path."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    row = conn.execute(
        'SELECT id FROM album_sections WHERE id=? AND album_id=?', (section_id, album_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Section not found'}), 404
    rows = conn.execute(
        '''SELECT sp.id AS assignment_id, sp.position, sp.added_at,
                  fc.id AS file_id, fc.file_path, fc.file_name, fc.file_type,
                  av.alternative_file_path
           FROM section_photos sp
           JOIN file_contents fc ON fc.id = sp.file_id
           LEFT JOIN (
               SELECT original_file_id,
                      alternative_file_path,
                      ROW_NUMBER() OVER (
                          PARTITION BY original_file_id
                          ORDER BY (width >= 1200) DESC,
                                   CASE WHEN width >= 1200 THEN width END ASC,
                                   width DESC, id ASC
                      ) AS rn
               FROM alternative_versions
               WHERE rung IN ('1200','1600','2048','2560')
           ) av ON av.original_file_id = fc.id AND av.rn = 1
           WHERE sp.section_id=?
           ORDER BY sp.position, sp.added_at''',
        (section_id,)
    ).fetchall()
    conn.close()
    photos = [dict(r) for r in rows]
    for p in photos:
        if p.get('file_path'):
            p['file_path'] = _resolve_path(p['file_path'], _original_root(catalog))
        if p.get('alternative_file_path'):
            p['alternative_file_path'] = _resolve_path(
                p['alternative_file_path'], catalog.target_directory
            )
    return jsonify(photos)


@app.route(
    '/api/catalogs/<int:catalog_id>/albums/<int:album_id>/sections/<int:section_id>/photos',
    methods=['POST']
)
def add_section_photo(catalog_id, album_id, section_id):
    """Add a photo (by file_id) to a section."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    file_id = data.get('file_id')
    if not file_id:
        return jsonify({'error': 'file_id is required'}), 400
    conn = catalog_db.get_connection()
    row = conn.execute(
        'SELECT id FROM album_sections WHERE id=? AND album_id=?', (section_id, album_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Section not found'}), 404
    file_row = conn.execute(
        'SELECT id FROM file_contents WHERE id=? AND removed_at IS NULL', (file_id,)
    ).fetchone()
    if not file_row:
        conn.close()
        return jsonify({'error': 'File not found'}), 404
    # A RAW+JPG pair is one photograph, held by its primary
    # (album_templates.PAIR_SECONDARY_IDS).
    file_id = album_templates.primary_file_id(conn, file_id)
    max_pos = conn.execute(
        'SELECT COALESCE(MAX(position), 0) AS mp FROM section_photos WHERE section_id=?',
        (section_id,)
    ).fetchone()['mp']
    try:
        cur = conn.execute(
            'INSERT INTO section_photos (section_id, file_id, position) VALUES (?, ?, ?)',
            (section_id, file_id, max_pos + 1)
        )
        conn.commit()
        assignment_id = cur.lastrowid
    except Exception:
        conn.close()
        return jsonify({'error': 'Photo already in this section'}), 409
    result = dict(conn.execute(
        '''SELECT sp.id AS assignment_id, sp.position, sp.added_at,
                  fc.id AS file_id, fc.file_path, fc.file_name, fc.file_type
           FROM section_photos sp
           JOIN file_contents fc ON fc.id = sp.file_id
           WHERE sp.id=?''',
        (assignment_id,)
    ).fetchone())
    conn.close()
    if result.get('file_path'):
        result['file_path'] = _resolve_path(result['file_path'], _original_root(catalog))
    try:
        _deploy_album(catalog, catalog_db, album_id)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("deploy_album failed after add_section_photo: %s", _e)
    return jsonify(result), 201


@app.route(
    '/api/catalogs/<int:catalog_id>/albums/<int:album_id>/sections/<int:section_id>/photos/<int:file_id>',
    methods=['DELETE']
)
def remove_section_photo(catalog_id, album_id, section_id, file_id):
    """Remove a photo from a section."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    row = conn.execute(
        'SELECT id FROM album_sections WHERE id=? AND album_id=?', (section_id, album_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Section not found'}), 404
    conn.execute(
        'DELETE FROM section_photos WHERE section_id=? AND file_id=?', (section_id, file_id)
    )
    conn.commit()
    conn.close()
    try:
        _deploy_album(catalog, catalog_db, album_id)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("deploy_album failed after remove_section_photo: %s", _e)
    return jsonify({'message': 'Photo removed from section'})


@app.route(
    '/api/catalogs/<int:catalog_id>/albums/<int:album_id>/sections/import-event',
    methods=['POST']
)
def import_event_to_section(catalog_id, album_id):
    """
    Bulk-import an event's photos into an album section.

    Creates a new section named after the event (with source_event_id set) on
    first import.  On re-import the existing section's photos are replaced with
    the event's current file list and the section name is refreshed.
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    data = request.json or {}
    event_id = data.get('event_id')
    if not event_id:
        return jsonify({'error': 'event_id is required'}), 400
    # Omitted or null means the root, which is exactly what this endpoint did
    # before categories existed - so existing callers are unaffected (spec 5.2).
    category_id = data.get('category_id')

    conn = catalog_db.get_connection()

    # Verify album exists
    album_row = conn.execute('SELECT id FROM albums WHERE id=?', (album_id,)).fetchone()
    if not album_row:
        conn.close()
        return jsonify({'error': 'Album not found'}), 404

    # Verify event exists
    event_row = conn.execute('SELECT id, name FROM events WHERE id=?', (event_id,)).fetchone()
    if not event_row:
        conn.close()
        return jsonify({'error': 'Event not found'}), 404

    if category_id is not None and not _category_in_album(conn, album_id, category_id):
        conn.close()
        return jsonify({'error': 'Category not found in this album'}), 400

    # The one way an event becomes a section, shared with the year template
    # (album_templates.add_event_section). A re-import replaces the photos and
    # refreshes the name, and deliberately leaves the category alone: the
    # caller is told which section it landed in so the UI can say so.
    try:
        section_id, is_reimport = album_templates.add_event_section(
            conn, album_id, event_id, event_row['name'], category_id)
        conn.commit()
    except Exception:
        conn.rollback()
        conn.close()
        raise

    # Build response with photo count
    section = dict(conn.execute(
        'SELECT * FROM album_sections WHERE id=?', (section_id,)
    ).fetchone())
    photo_count = conn.execute(
        'SELECT COUNT(*) AS c FROM section_photos WHERE section_id=?', (section_id,)
    ).fetchone()['c']
    covers = _resolve_section_covers(conn, album_id)
    conn.close()

    section['photo_count'] = photo_count
    resolved = covers.get(section_id)
    section['resolved_cover_file_id'] = resolved
    section['cover_thumb_url'] = (
        _thumb_url(catalog_id, resolved)
        if resolved else None)
    # On a re-import this is where the section ALREADY was, not where the caller
    # asked for it. The UI needs it to say so rather than appear to do nothing.
    section['was_reimport'] = is_reimport
    try:
        _deploy_album(catalog, catalog_db, album_id)
    except Exception as _e:
        import logging as _logging
        _logging.getLogger(__name__).warning("deploy_album failed after import_event_to_section: %s", _e)
    return jsonify(section), (200 if is_reimport else 201)


def _nl_to_sql(nl_text: str, catalog_db, previous_fragments: list):
    """
    Convert a natural-language photo filter to a SQLite WHERE clause body.

    Strategy (in order):
      1. Ollama via Albums backend (port 5005) — primary path, full LLM translation.
      2. Built-in pattern matcher — deterministic fallback when Ollama is unavailable.
      3. Keyword fallback — last resort, matches description/filename text.

    Returns (sql_fragment: str, method: str).
    """
    import re as _re
    import requests as _requests

    text = nl_text.lower().strip()

    # ── 1. Try Ollama Albums backend first ────────────────────────────────────
    try:
        resp = _requests.post(
            f'{ALBUMS_BACKEND_URL}/translate',
            json={
                'nl_text': nl_text,
                'schema_hint': SCHEMA_HINT,
                'previous_filters': previous_fragments,
            },
            timeout=60
        )
        if resp.ok:
            payload = resp.json()
            frag = payload.get('sql_fragment', '').strip()
            # Accept the result unless it is the trivial pass-through or empty
            if frag and frag != '1=1' and not payload.get('fallback'):
                return frag, 'ollama'
    except Exception:
        pass  # backend not running — fall through to pattern matcher

    # ── 2. Built-in pattern matcher ───────────────────────────────────────────
    year_m = _re.search(r'\b(20\d{2}|19\d{2})\b', nl_text)
    year = year_m.group(1) if year_m else None

    # Pattern: person name present verbatim in the text
    def _find_person_in_text():
        conn = catalog_db.get_connection()
        try:
            rows = conn.execute(
                "SELECT id, name FROM people ORDER BY length(name) DESC"
            ).fetchall()
        finally:
            conn.close()
        for r in rows:
            if r['name'] and r['name'].lower() in text:
                return r['id'], r['name']
        return None, None

    matched_person_id, matched_person_name = _find_person_in_text()

    if matched_person_id:
        person_fragment = (
            f"fc.id IN ("
            f"SELECT DISTINCT ef_all.file_id FROM event_files ef_all "
            f"WHERE ef_all.event_id IN ("
            f"SELECT DISTINCT ef2.event_id FROM event_files ef2 "
            f"JOIN face_detections fd2 ON fd2.file_id = ef2.file_id "
            f"WHERE fd2.person_id = {matched_person_id}"
            f"))"
        )
        if year:
            year_filter = (
                f"fc.id IN ("
                f"SELECT DISTINCT ef3.file_id FROM event_files ef3 "
                f"JOIN events ev3 ON ev3.id = ef3.event_id "
                f"WHERE strftime('%Y', ev3.start_time) = '{year}' "
                f"   OR ev3.start_time LIKE '{year}%'"
                f")"
            )
            return (
                f"({person_fragment}) AND ({year_filter})",
                f'pattern:person+year ({matched_person_name}, {year})'
            )
        return person_fragment, f'pattern:person ({matched_person_name})'

    if year and _re.search(r'\b(year|from|in|during|taken)\b', text):
        fragment = (
            f"(SUBSTR(fc.master_datetime, 1, 4) = '{year}')"
        )
        return fragment, f'pattern:year ({year})'

    em = _re.search(
        r'\bevent[s]?\s+(?:named?|called|titled?)\s+["\']?([^"\']+?)["\']?(?:\s|$)',
        text, _re.I
    )
    if em:
        ev_name = em.group(1).strip().replace("'", "''")
        return (
            f"fc.id IN (SELECT ef.file_id FROM event_files ef "
            f"JOIN events ev ON ev.id = ef.event_id "
            f"WHERE ev.name LIKE '%{ev_name}%')",
            f'pattern:event_name ({ev_name})'
        )

    lm = _re.search(
        r'\b(?:in|at|from|location)\s+([A-Za-z][A-Za-z ,]{2,40}?)(?:\s*$|[,.])',
        nl_text, _re.I
    )
    if lm:
        loc_name = lm.group(1).strip().replace("'", "''")
        if not _re.match(r'^\d{4}$', loc_name):
            return (
                f"fc.id IN (SELECT ef.file_id FROM event_files ef "
                f"JOIN events ev ON ev.id = ef.event_id "
                f"JOIN location_events le ON le.event_id = ev.id "
                f"JOIN locations loc ON loc.id = le.location_id "
                f"WHERE loc.name LIKE '%{loc_name}%')",
                f'pattern:location ({loc_name})'
            )

    cm = _re.search(
        r'\b(iphone|canon|nikon|sony|fuji(?:film)?|samsung|pixel|olympus|leica)\b',
        text, _re.I
    )
    if cm:
        cam = cm.group(1).replace("'", "''")
        return (
            f"(ed.camera_make LIKE '%{cam}%' OR ed.camera_model LIKE '%{cam}%')",
            f'pattern:camera ({cam})'
        )

    # ── 3. Last-resort keyword search ─────────────────────────────────────────
    safe = nl_text.replace("'", "''")
    return (
        f"(fc.description LIKE '%{safe}%' OR fc.file_name LIKE '%{safe}%')",
        'fallback:keyword'
    )


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/filters', methods=['POST'])
def add_album_filter(catalog_id, album_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.json or {}
    nl_text = data.get('nl_text', '').strip()
    if not nl_text:
        return jsonify({'error': 'nl_text is required'}), 400

    # Fetch existing filters to provide as context
    conn = catalog_db.get_connection()
    row = conn.execute('SELECT id FROM albums WHERE id=?', (album_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Album not found'}), 404
    existing = [dict(r) for r in conn.execute(
        'SELECT sql_fragment FROM album_filters WHERE album_id=? ORDER BY position',
        (album_id,)
    ).fetchall()]
    conn.close()

    previous_fragments = [r['sql_fragment'] for r in existing]

    # Translate NL → SQL
    sql_fragment, translation_method = _nl_to_sql(nl_text, catalog_db, previous_fragments)

    # Determine next position
    conn = catalog_db.get_connection()
    max_pos_row = conn.execute(
        'SELECT COALESCE(MAX(position), 0) AS mp FROM album_filters WHERE album_id=?',
        (album_id,)
    ).fetchone()
    next_pos = (max_pos_row['mp'] or 0) + 1

    # Compute photo count for cumulative filters up to and including this one
    cumulative = previous_fragments + [sql_fragment]
    photo_count = _run_cumulative_query(catalog_db, cumulative)

    cur = conn.execute(
        '''INSERT INTO album_filters (album_id, position, nl_text, sql_fragment, photo_count)
           VALUES (?, ?, ?, ?, ?)''',
        (album_id, next_pos, nl_text, sql_fragment, photo_count)
    )
    filter_id = cur.lastrowid
    conn.commit()
    new_filter = dict(conn.execute(
        'SELECT * FROM album_filters WHERE id=?', (filter_id,)
    ).fetchone())
    conn.close()
    new_filter['translation_method'] = translation_method
    return jsonify(new_filter), 201


@app.route(
    '/api/catalogs/<int:catalog_id>/albums/<int:album_id>/filters/<int:filter_id>',
    methods=['DELETE']
)
def delete_album_filter(catalog_id, album_id, filter_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    conn.execute('DELETE FROM album_filters WHERE id=? AND album_id=?', (filter_id, album_id))
    conn.commit()
    # Re-number positions to close gaps
    rows = conn.execute(
        'SELECT id FROM album_filters WHERE album_id=? ORDER BY position', (album_id,)
    ).fetchall()
    for idx, r in enumerate(rows, start=1):
        conn.execute('UPDATE album_filters SET position=? WHERE id=?', (idx, r['id']))
    conn.commit()
    conn.close()
    # Refresh all cached counts
    _refresh_filter_counts(catalog_db, album_id)
    return jsonify({'message': 'Filter deleted'})


@app.route(
    '/api/catalogs/<int:catalog_id>/albums/<int:album_id>/filters/<int:filter_id>/debug',
    methods=['GET']
)
def debug_album_filter(catalog_id, album_id, filter_id):
    """Return the stored SQL fragment and its live result count for a single filter."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    row = conn.execute(
        'SELECT * FROM album_filters WHERE id=? AND album_id=?', (filter_id, album_id)
    ).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Filter not found'}), 404
    f = dict(row)
    # Collect all fragments up to and including this position for cumulative count
    all_rows = conn.execute(
        'SELECT sql_fragment, position FROM album_filters WHERE album_id=? ORDER BY position',
        (album_id,)
    ).fetchall()
    conn.close()
    cumulative = [r['sql_fragment'] for r in all_rows if r['position'] <= f['position']]
    live_count = _run_cumulative_query(catalog_db, cumulative)
    total = _get_total_photo_count(catalog_db)
    # Also try the fragment in isolation
    isolated_count = _run_cumulative_query(catalog_db, [f['sql_fragment']])
    return jsonify({
        'filter_id': filter_id,
        'position': f['position'],
        'nl_text': f['nl_text'],
        'sql_fragment': f['sql_fragment'],
        'cached_photo_count': f['photo_count'],
        'live_cumulative_count': live_count,
        'isolated_count': isolated_count,
        'total_photos_in_catalog': total,
    })


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/photos', methods=['GET'])
def get_album_photos(catalog_id, album_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    conn = catalog_db.get_connection()
    row = conn.execute('SELECT id FROM albums WHERE id=?', (album_id,)).fetchone()
    if not row:
        conn.close()
        return jsonify({'error': 'Album not found'}), 404
    filters = [dict(r) for r in conn.execute(
        'SELECT sql_fragment FROM album_filters WHERE album_id=? ORDER BY position',
        (album_id,)
    ).fetchall()]
    conn.close()
    fragments = [f['sql_fragment'] for f in filters]

    conn = catalog_db.get_connection()
    try:
        base = '''
            SELECT DISTINCT fc.id, fc.file_path, fc.file_name, fc.file_type
            FROM file_contents fc
            LEFT JOIN exif_data ed ON ed.file_id = fc.id
            WHERE fc.removed_at IS NULL
              AND fc.id NOT IN (''' + album_templates.PAIR_SECONDARY_IDS + ''')
        '''
        if fragments:
            where_clause = ' AND '.join(f'({f})' for f in fragments)
            query = f'{base} AND {where_clause} ORDER BY fc.id'
        else:
            query = f'{base} ORDER BY fc.id'
        rows = conn.execute(query).fetchall()
        photos = [dict(r) for r in rows]
    except Exception as e:
        photos = []
    finally:
        conn.close()

    # Resolve paths
    for p in photos:
        if p.get('file_path'):
            p['file_path'] = _resolve_path(p['file_path'], _original_root(catalog))
    return jsonify(photos)


# ─── Workflow queue ───────────────────────────────────────────────────────────
# See documentation/specs/workflow-scheduling-spec.md section 7.

def _queue_db_path(catalog):
    return os.path.join(catalog.target_directory, 'workflow_results.db')


def _queue_payload(catalog, include_finished=False):
    db_path = _queue_db_path(catalog)
    items = workflow_scheduler.list_queue(db_path, include_finished=include_finished)
    for item in items:
        item['trigger_label'] = workflow_triggers.describe(
            item.get('trigger_source'), item.get('trigger_detail'))
        item['quiet_seconds'] = workflow_triggers.quiet_seconds(
            item.get('trigger_source'))
    running = sum(1 for i in items if i['status'] == 'running')
    queued = sum(1 for i in items if i['status'] in ('queued', 'blocked'))
    return {
        'success': True,
        'catalog_id': catalog.id,
        'paused': workflow_scheduler.is_paused(db_path),
        'running_count': running,
        'queued_count': queued,
        'max_concurrent': workflow_scheduler.MAX_CONCURRENT_WORKFLOWS,
        'queue': items,
    }


def _emit_queue(catalog):
    try:
        socketio.emit('queue_updated', _queue_payload(catalog))
    except Exception as e:
        print(f'[queue] could not emit queue_updated: {e}')


@app.route('/api/catalogs/<int:catalog_id>/queue', methods=['GET'])
def get_queue(catalog_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400
    include = request.args.get('include_finished') == '1'
    return jsonify(_queue_payload(catalog, include_finished=include))


@app.route('/api/catalogs/<int:catalog_id>/queue', methods=['POST'])
def enqueue_workflow(catalog_id):
    """Add one workflow to the queue by hand."""
    catalog, catalog_db, wm = _get_catalog_workflow_model(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.get_json(silent=True) or {}
    workflow_id = data.get('workflow_id')
    if workflow_id is None:
        return jsonify({'error': 'workflow_id is required'}), 400
    workflow = wm.get_by_id(workflow_id)
    if not workflow:
        return jsonify({'error': 'Workflow not found'}), 404

    # A per-run source, chosen on the workflow card. When it names a storage
    # route it is resolved HERE, so an unavailable route is refused at the click
    # with the route named - rather than queueing a row that fails later, or
    # worse, silently walking a folder that is not there.
    source_directory = data.get('source_directory')
    if data.get('source_route_id'):
        resolved, route_err = storage.resolve_source(
            int(data['source_route_id']), (data.get('source_subpath') or '').strip())
        if route_err:
            return jsonify({'error': route_err}), 400
        source_directory = resolved

    queue_id, created = workflow_scheduler.enqueue(
        _queue_db_path(catalog), dict(workflow),
        trigger_source='manual',
        source_directory=source_directory,
        priority=int(data.get('priority', 0)),
    )

    # run_now is what the card's "Run Workflow" button sends: same queue as
    # everything else, but first in line and starting on this pass rather than
    # the next poll. Going through the queue is what gives it a progress bar -
    # a direct execution has no queue row for the panel to show.
    run_now = bool(data.get('run_now'))
    message = 'Queued' if created else 'Already queued - folded into the existing entry'
    if run_now:
        ok, promote_message = workflow_scheduler.promote(_queue_db_path(catalog), queue_id)
        if ok:
            message = promote_message
        if _scheduler:
            _scheduler.kick()

    _emit_queue(catalog)
    return jsonify({'success': True, 'queue_id': queue_id, 'created': created,
                    'run_now': run_now, 'message': message}), 202


@app.route('/api/catalogs/<int:catalog_id>/queue/chain', methods=['POST'])
def enqueue_chain(catalog_id):
    """Enqueue a whole named chain, e.g. {"trigger": "import_completed"}."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.get_json(silent=True) or {}
    trigger = data.get('trigger')
    if trigger not in workflow_triggers.TRIGGERS:
        return jsonify({'error': f'Unknown trigger. Known: '
                                 f'{sorted(workflow_triggers.TRIGGERS)}'}), 400
    try:
        results = workflow_triggers.fire(catalog, trigger,
                                         detail={'requested_by': 'user'})
    except Exception as e:
        return jsonify({'error': f'Could not enqueue chain: {e}'}), 500
    _emit_queue(catalog)
    return jsonify({'success': True, 'trigger': trigger,
                    'enqueued': [{'queue_id': q, 'workflow_name': n, 'created': c}
                                 for q, n, c in results]}), 202


@app.route('/api/catalogs/<int:catalog_id>/queue/<int:queue_id>', methods=['DELETE'])
def cancel_queue_item(catalog_id, queue_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    ok, message = workflow_scheduler.cancel(_queue_db_path(catalog), queue_id)
    if not ok:
        return jsonify({'error': message}), 409 if 'running' in message else 404
    _emit_queue(catalog)
    return jsonify({'success': True, 'message': message})


@app.route('/api/catalogs/<int:catalog_id>/queue/<int:queue_id>/position', methods=['PUT'])
def move_queue_item(catalog_id, queue_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    data = request.get_json(silent=True) or {}
    if 'position' not in data:
        return jsonify({'error': 'position is required'}), 400
    ok, message = workflow_scheduler.reorder(_queue_db_path(catalog), queue_id,
                                             int(data['position']))
    if not ok:
        return jsonify({'error': message}), 409
    _emit_queue(catalog)
    return jsonify({'success': True, 'message': message})


@app.route('/api/catalogs/<int:catalog_id>/queue/<int:queue_id>/run-now', methods=['POST'])
def run_queue_item_now(catalog_id, queue_id):
    """Skip an item's quiet period and let it start on the next pass."""
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    ok, message = workflow_scheduler.run_now(_queue_db_path(catalog), queue_id)
    if not ok:
        return jsonify({'error': message}), 409
    _emit_queue(catalog)
    return jsonify({'success': True, 'message': message})


def _stop_execution(catalog, execution_id, workflow_name, script_name,
                    remove=False, queue_id=None):
    """
    Ask one running execution to stop. Shared by both entry points.

    Returns (payload, http_status).
    """
    refusal = workflow_scheduler.stop_refusal(script_name, workflow_name)
    if refusal:
        return {'error': refusal, 'stoppable': False}, 409

    catalog_db = CatalogDatabase(catalog.target_directory)
    row = ExecutionModel(catalog_db).request_stop(execution_id)
    if row is None:
        return {'error': 'Execution not found'}, 404
    if row['status'] != 'running':
        return {'error': f"This workflow has already finished ({row['status']})."}, 409

    if queue_id is not None:
        workflow_scheduler.mark_stop_requested(_queue_db_path(catalog), queue_id)

    _start_stop_watcher(execution_id, catalog_db.db_path, row.get('pid'),
                        workflow_name=workflow_name)
    _emit_queue(catalog)
    return {
        'success': True,
        'execution_id': execution_id,
        'remove': bool(remove),
        # The flag is set; the workflow leaves at its next item boundary. Saying
        # "stopping" rather than "stopped" is the honest report - the row is
        # still 'running' until the script writes its own.
        'message': ('Stopping after the current item. Files already imported stay '
                    'in the catalog.' if remove else
                    'Stopping after the current item. Run it again to continue '
                    'from here.'),
    }, 200


@app.route('/api/catalogs/<int:catalog_id>/executions/<int:execution_id>/stop',
           methods=['POST'])
def stop_execution(catalog_id, execution_id):
    """
    Stop a running workflow.

    The primitive: works for a run started by hand, which has an execution row
    but no queue row, as well as for one the scheduler started.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    data = request.get_json(silent=True) or {}
    remove = bool(data.get('remove'))

    catalog_db = CatalogDatabase(catalog.target_directory)
    execution = ExecutionModel(catalog_db).get_by_id(execution_id)
    if not execution:
        return jsonify({'error': 'Execution not found'}), 404

    # The script name lives on the workflow, not the execution, and decides
    # whether this workflow can be stopped at all.
    wm = WorkflowModel(catalog_db)
    workflow = wm.get_by_id(execution['workflow_id']) or {}

    payload, status = _stop_execution(
        catalog, execution_id, execution.get('workflow_name'),
        workflow.get('python_script'), remove=remove)

    if status == 200:
        item = _queue_item_for_execution(catalog, execution_id)
        if item:
            workflow_scheduler.mark_stop_requested(_queue_db_path(catalog), item['id'])
    return jsonify(payload), status


def _queue_item_for_execution(catalog, execution_id):
    conn = sqlite3.connect(_queue_db_path(catalog), timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute('SELECT * FROM workflow_queue WHERE execution_id = ?',
                           (execution_id,)).fetchone()
        return dict(row) if row else None
    except sqlite3.Error:
        return None
    finally:
        conn.close()


@app.route('/api/catalogs/<int:catalog_id>/queue/<int:queue_id>/stop', methods=['POST'])
def stop_queue_item(catalog_id, queue_id):
    """
    Stop the workflow this queue row is running.

    `{"remove": true}` is the second verb: the row ends as 'cancelled' rather
    than 'stopped' and is not offered a resume link. Neither verb undoes work the
    run already did (workflows-review-spec.md section 3.6).
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    item = workflow_scheduler.get_item(_queue_db_path(catalog), queue_id)
    if not item:
        return jsonify({'error': 'Queue item not found'}), 404
    if item['status'] != 'running':
        return jsonify({'error': f"This item is {item['status']}, not running."}), 409
    if not item.get('execution_id'):
        return jsonify({'error': 'This item has no execution to stop yet.'}), 409

    data = request.get_json(silent=True) or {}
    remove = bool(data.get('remove'))

    payload, status = _stop_execution(
        catalog, item['execution_id'], item.get('workflow_name'),
        item.get('script_name'), remove=remove, queue_id=queue_id)

    if status == 200 and remove:
        # Recorded now rather than when the process exits: "remove" is a
        # statement about the queue row, and _reap would otherwise overwrite it
        # with 'stopped' a moment later.
        workflow_scheduler.mark_stopped(_queue_db_path(catalog), queue_id, remove=True,
                                        message='removed while running')
    return jsonify(payload), status


@app.route('/api/catalogs/<int:catalog_id>/queue/<int:queue_id>/resume', methods=['POST'])
def resume_queue_item(catalog_id, queue_id):
    """
    Re-queue a workflow that was stopped.

    There is nothing to reload: every workflow skips what an earlier run finished
    (workflows-review-spec.md section 2.4), so resuming is running it again. The
    trigger source is recorded so the panel can say where the row came from
    rather than calling it 'manual'.
    """
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not catalog.target_directory:
        return jsonify({'error': 'Catalog target directory not set'}), 400

    item = workflow_scheduler.get_item(_queue_db_path(catalog), queue_id)
    if not item:
        return jsonify({'error': 'Queue item not found'}), 404
    if item['status'] != 'stopped':
        return jsonify({'error': f"Only a stopped item can be resumed; "
                                 f"this one is {item['status']}."}), 409

    catalog_db = CatalogDatabase(catalog.target_directory)
    workflow = WorkflowModel(catalog_db).get_by_id(item['workflow_id'])
    if not workflow:
        return jsonify({'error': 'That workflow no longer exists'}), 404

    new_id, created = workflow_scheduler.enqueue(
        _queue_db_path(catalog), dict(workflow),
        trigger_source='resume',
        trigger_detail={'resumed_from': queue_id},
        source_directory=item.get('source_directory'),
    )
    # The stopped row has served its purpose - it carried the resume link. Retire
    # it so the panel shows the new row rather than both.
    workflow_scheduler.supersede(_queue_db_path(catalog), queue_id)
    if _scheduler:
        _scheduler.kick()
    _emit_queue(catalog)
    return jsonify({'success': True, 'queue_id': new_id, 'created': created,
                    'message': 'Queued again. Work already done will be skipped.'})


@app.route('/api/catalogs/<int:catalog_id>/queue/pause', methods=['POST'])
def pause_queue(catalog_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    workflow_scheduler.set_paused(_queue_db_path(catalog), True)
    _emit_queue(catalog)
    return jsonify({'success': True, 'paused': True,
                    'message': 'Queue paused. Running workflows will finish.'})


@app.route('/api/catalogs/<int:catalog_id>/queue/resume', methods=['POST'])
def resume_queue(catalog_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    workflow_scheduler.set_paused(_queue_db_path(catalog), False)
    _emit_queue(catalog)
    return jsonify({'success': True, 'paused': False})


@app.route('/api/catalogs/<int:catalog_id>/queue/status', methods=['GET'])
def queue_status(catalog_id):
    catalog = Catalog.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    payload = _queue_payload(catalog)
    payload.pop('queue', None)
    payload['lease_owner'] = _scheduler.owner if _scheduler else None
    payload['scheduler_running'] = bool(_scheduler and _scheduler._has_lease)
    # Which scripts can be stopped at all. Sent with the status rather than per
    # row so the panel can hide a control it must never offer, instead of showing
    # one and rejecting it (workflows-review-spec.md section 3.8).
    payload['stop_modes'] = dict(workflow_scheduler.WORKFLOW_STOP_MODES)
    return jsonify(payload)


# ─── Queue runner ─────────────────────────────────────────────────────────────

def _scheduler_on_finished(catalog, item):
    """
    A queue item reached a terminal state.

    This is the one place a finished workflow may cause more work, and it is
    deliberately narrow: only on success, only for the workflows that genuinely
    invalidate downstream data, and always through workflow_triggers so the
    chain-depth guard applies. A workflow subprocess still has no way to
    enqueue anything - it does not run in this process.

    A script may raise more than one trigger - Auto Detect Faces raises both
    faces_detected and pets_detected, because one pass now finds two different
    things - so this loops.
    """
    try:
        _emit_queue(catalog)
    except Exception:
        pass
    # Whatever it did, the Dashboard's cached counts may now be behind: the
    # next visit rebuilds them (dashboard-specs.md section 4). Even a failed
    # run counts - it shows on the Dashboard as one.
    try:
        import dashboard
        conn = sqlite3.connect(os.path.join(catalog.target_directory, 'workflow_results.db'),
                               timeout=10)
        try:
            dashboard.mark_stale(conn)
        finally:
            conn.close()
    except Exception as e:
        print(f'[dashboard] could not mark stale for catalog {catalog.id}: {e}')
    # Search's name index catches up with whatever the run added - only for a
    # catalog that has been searched (its index exists); the first search
    # builds it (search-spec.md 6.1).
    try:
        import search_index
        path = os.path.join(catalog.target_directory, 'workflow_results.db')
        conn = sqlite3.connect(path, timeout=10)
        try:
            indexed = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'search_state'").fetchone()
        finally:
            conn.close()
        if indexed:
            search_index.catch_up_in_background(path)
    except Exception as e:
        print(f'[search] could not start catching up for catalog {catalog.id}: {e}')
    # A date batch waits for its Create Events run, however that run ended
    # (mass-edit-event-photo-dates-spec.md section 4.5).
    if item.get('script_name') == 'ev_extract_events.py':
        try:
            _finish_rebuilding_batches(catalog, item.get('id'), item.get('status'))
        except Exception as e:
            print(f'[date batch] could not close batches for queue row {item.get("id")}: {e}')
    if item.get('status') != 'completed':
        return
    triggers = workflow_triggers.triggers_for_completed_workflow(
        item.get('script_name'))
    for trigger in triggers:
        # One failing trigger must not swallow the next. Auto Detect Faces
        # raises two, and a pet chain that cannot be enqueued is no reason for
        # the face chain to be skipped as well.
        try:
            results = workflow_triggers.fire(
                catalog, trigger,
                detail={'after': item.get('workflow_name')},
                chain_depth=int(item.get('chain_depth') or 0),
            )
            if results:
                print(f'[queue] {item.get("workflow_name")} finished -> '
                      f'{trigger} enqueued {len(results)} workflow(s)')
                _emit_queue(catalog)
        except Exception as e:
            print(f'[queue] chain trigger {trigger} failed: {e}')


_scheduler = None


def _start_scheduler():
    """
    Start the queue runner once per process.

    Guarded by an environment flag so a shell that imports app.py for a one-off
    script does not quietly start executing the user's queue.
    """
    global _scheduler
    if os.environ.get('DISABLE_WORKFLOW_SCHEDULER', '').lower() in ('1', 'true', 'yes'):
        print('[scheduler] disabled by DISABLE_WORKFLOW_SCHEDULER')
        return None
    if _scheduler is not None:
        return _scheduler
    app_db_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                               'database', 'catalogs.db')
    try:
        workflow_queue_schema.ensure_scheduler_lease_schema(app_db_path)
    except Exception as e:
        print(f'[scheduler] could not prepare the lease table: {e}')
    # Settle this machine's identity now rather than inside the first workflow
    # launch. It is written to every execution row, so creating it on a quiet
    # startup path is better than creating it under a queue runner's hand.
    print(f'[scheduler] instance {workflow_scheduler.instance_id()}')
    _scheduler = workflow_scheduler.WorkflowScheduler(
        app_db_path=app_db_path,
        start_execution=start_workflow_execution,
        emit=socketio.emit,
        on_finished=_scheduler_on_finished,
    )
    _scheduler.start()
    return _scheduler


_start_scheduler()


# Does the mounted workflows/ agree with the wiring baked into this image?
#
# They arrive by different routes - workflows/ is bind-mounted, the maps in
# workflow_triggers.py and workflow_scheduler.py are compiled in - so adding a
# workflow half-deploys on its own: the row is created and it is in no chain.
# Said once, at startup, because the alternative is noticing a node with no
# edges on a canvas, which is how it was found the first time.
workflow_defaults.report_manifest_wiring()


# ─── Storage alerts ───────────────────────────────────────────────────────────
# Every few minutes, how full is each disk the catalogs write to
# (storage_alerts.py). A full disk once took the import and every catalog on it
# down with nothing but "disk I/O error" to say why (oldserver, 2026-10-04).

STORAGE_CHECK_SECONDS = int(os.environ.get('AIRLOCK_STORAGE_CHECK_SECONDS', '300'))
_storage_monitor = None


def _storage_check():
    """One pass over every catalog; pushes alerts_updated when anything changed."""
    import storage_alerts
    app_db_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'database')
    conn = db.get_connection()
    try:
        changed, _entries = storage_alerts.check(
            Catalog.get_all(), conn, extra=[('application database', app_db_dir)])
        counts = storage_alerts.summary(conn)
    finally:
        conn.close()
    if changed:
        try:
            socketio.emit('alerts_updated', counts)
        except Exception as e:
            print(f'[alerts] could not emit alerts_updated: {e}')
    return counts


def _start_storage_monitor():
    """
    The background check, once per process. Off wherever the queue runner is
    off (DISABLE_WORKFLOW_SCHEDULER): a shell or a test that imports app.py must
    not start background work.
    """
    global _storage_monitor
    if os.environ.get('DISABLE_WORKFLOW_SCHEDULER', '').lower() in ('1', 'true', 'yes'):
        return None
    if _storage_monitor is not None:
        return _storage_monitor

    def _loop():
        while True:
            try:
                _storage_check()
            except Exception as e:
                print(f'[alerts] storage check failed: {e}')
            time.sleep(STORAGE_CHECK_SECONDS)

    _storage_monitor = threading.Thread(target=_loop, daemon=True, name='storage-monitor')
    _storage_monitor.start()
    return _storage_monitor


_start_storage_monitor()


@app.route('/api/alerts', methods=['GET'])
@auth.login_required
def get_alerts():
    """
    Alerts, a page at a time, plus the counts the bell shows.
      ?include_resolved=1  history as well as open alerts
      ?limit=&offset=      paging (limit at most 200)
      ?recent=1            newest first, open or not - the bell's last ten
    """
    import storage_alerts
    include_resolved = request.args.get('include_resolved') == '1'
    limit = max(1, min(request.args.get('limit', 50, type=int) or 50, 200))
    offset = max(0, request.args.get('offset', 0, type=int) or 0)
    conn = db.get_connection()
    try:
        alerts = storage_alerts.list_alerts(
            conn, include_resolved=include_resolved, limit=limit, offset=offset,
            newest_first=request.args.get('recent') == '1')
        total = storage_alerts.count_alerts(conn, include_resolved=include_resolved)
        counts = storage_alerts.summary(conn)
    finally:
        conn.close()
    return jsonify({'alerts': alerts, 'total': total, 'limit': limit, 'offset': offset,
                    **counts,
                    'thresholds': {
                        'warn_percent': storage_alerts.WARN_PERCENT,
                        'critical_percent': storage_alerts.CRITICAL_PERCENT,
                        'warn_free_gb': storage_alerts.WARN_FREE_GB,
                        'critical_free_gb': storage_alerts.CRITICAL_FREE_GB,
                    }})


@app.route('/api/alerts/check', methods=['POST'])
@auth.login_required
def check_alerts_now():
    """Measure now rather than at the next tick - the page's "Check now"."""
    _storage_check()
    return get_alerts()


@app.route('/api/alerts/<int:alert_id>/acknowledge', methods=['POST'])
@auth.login_required
def acknowledge_alert(alert_id):
    import storage_alerts
    user = auth.current_user()
    conn = db.get_connection()
    try:
        ok = storage_alerts.acknowledge(conn, alert_id, user['id'] if user else None)
        counts = storage_alerts.summary(conn)
    finally:
        conn.close()
    if not ok:
        return jsonify({'error': 'No open, unacknowledged alert with that id'}), 404
    try:
        socketio.emit('alerts_updated', counts)
    except Exception:
        pass
    return jsonify({'acknowledged': alert_id, **counts})


def _sweep_sessions_at_startup():
    """Drop sessions that expired while the server was down. lookup_session
    also expires rows lazily; this clears the ones nobody returns for."""
    try:
        conn = db.get_connection()
        try:
            auth.sweep_expired_sessions(conn)
        finally:
            conn.close()
    except Exception as exc:  # never block startup on housekeeping
        print(f'[auth] session sweep skipped: {exc}')


_sweep_sessions_at_startup()


# ─── Authentication (spec section 7) ──────────────────────────────────────────
#
# The error contract matters to the UI: 401 means "not authenticated" and the
# browser shows the login view; 403 means "authenticated but not permitted" and
# the browser shows a message WITHOUT redirecting, because bouncing a logged-in
# user to a login page they already passed is a confusing loop.

def _set_session_cookie(response, session_id):
    """Attach the session cookie - see auth.set_session_cookie."""
    return auth.set_session_cookie(
        response, session_id, request.headers.get('X-Forwarded-Proto', request.scheme))


@app.route('/api/auth/setup-required', methods=['GET'])
def auth_setup_required():
    """True only while no account exists, so the UI can offer first-run setup."""
    conn = db.get_connection()
    try:
        return jsonify({'setup_required': not auth.any_users_exist(conn)})
    finally:
        conn.close()


@app.route('/api/auth/setup', methods=['POST'])
def auth_setup():
    """Create the first administrator. Refuses once any account exists."""
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    display_name = (data.get('display_name') or username).strip()
    email = (data.get('email') or '').strip() or None

    if not username:
        return jsonify({'error': 'Username is required'}), 400
    problem = auth.password_problem(password)
    if problem:
        return jsonify({'error': problem}), 400

    conn = db.get_connection()
    try:
        # The existence check and the insert share one transaction so this
        # cannot be raced into creating a second administrator (spec 7).
        conn.execute('BEGIN IMMEDIATE')
        if auth.any_users_exist(conn):
            conn.rollback()
            return jsonify({'error': 'Setup has already been completed'}), 409
        cur = conn.execute(
            'INSERT INTO users (username, email, display_name, password_hash, is_admin) '
            'VALUES (?,?,?,?,1)',
            (username, email, display_name, auth.hash_password(password)))
        user_id = cur.lastrowid
        conn.commit()

        # The first admin owns every catalog that has no access rows yet,
        # otherwise a fresh install would show an empty catalog list.
        for cat in Catalog.get_all():
            if not conn.execute('SELECT 1 FROM catalog_access WHERE catalog_id = ?',
                                (cat.id,)).fetchone():
                auth.grant_access(conn, cat.id, user_id, 'owner', granted_by=user_id)

        session_id = auth.create_session(conn, user_id,
                                         request.headers.get('User-Agent'), request.remote_addr)
        user = auth.get_user(conn, user_id)
    finally:
        conn.close()

    return _set_session_cookie(jsonify({'user': auth.user_public(user)}), session_id)


@app.route('/api/auth/login', methods=['POST'])
def auth_login():
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''

    keys = [f'user:{username.lower()}', f'ip:{request.remote_addr}']
    wait = auth.throttle_retry_after(keys)
    if wait:
        return jsonify({'error': f'Too many attempts. Try again in {wait}s.',
                        'code': 'rate_limited'}), 429

    conn = db.get_connection()
    try:
        user = auth.get_user_by_username(conn, username)
        # Always run a hash check, even with no such user, so the two cases take
        # comparable time and the response cannot be used to enumerate accounts.
        ok = auth.verify_password(user['password_hash'] if user else None, password)

        if not user or not ok or not user['is_active']:
            auth.record_failure(keys)
            return jsonify({'error': 'Invalid username or password',
                            'code': 'unauthenticated'}), 401

        auth.record_success(keys)
        conn.execute('UPDATE users SET last_login_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (user['id'],))
        conn.commit()
        session_id = auth.create_session(conn, user['id'],
                                         request.headers.get('User-Agent'), request.remote_addr)
        user = auth.get_user(conn, user['id'])
    finally:
        conn.close()

    return _set_session_cookie(jsonify({'user': auth.user_public(user)}), session_id)


@app.route('/api/auth/session', methods=['GET'])
def auth_session():
    """The UI's boot probe. 401 here is expected and must not be an error."""
    user = auth.current_user()
    if user is None:
        return auth.unauthenticated('No active session')
    return jsonify({'user': auth.user_public(user)})


@app.route('/api/auth/logout', methods=['POST'])
def auth_logout():
    session_id = getattr(g, 'session_id', None)
    if session_id:
        conn = db.get_connection()
        try:
            auth.delete_session(conn, session_id)
        finally:
            conn.close()
    response = jsonify({'ok': True})
    auth.clear_session_cookies(response)
    return response


@app.route('/api/auth/logout-all', methods=['POST'])
def auth_logout_all():
    user = auth.current_user()
    if user is None:
        return auth.unauthenticated()
    conn = db.get_connection()
    try:
        auth.delete_user_sessions(conn, user['id'])
    finally:
        conn.close()
    response = jsonify({'ok': True})
    auth.clear_session_cookies(response)
    return response


@app.route('/api/auth/sessions', methods=['GET'])
def auth_list_sessions():
    """The signed-in devices shown on the settings page."""
    user = auth.current_user()
    if user is None:
        return auth.unauthenticated()
    conn = db.get_connection()
    try:
        rows = conn.execute(
            'SELECT id, created_at, last_seen_at, expires_at, ip, user_agent '
            'FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC',
            (user['id'],)).fetchall()
    finally:
        conn.close()
    current = getattr(g, 'session_id', None)
    return jsonify([{
        'id': r['id'][:12],
        'created_at': r['created_at'],
        'last_seen_at': r['last_seen_at'],
        'expires_at': r['expires_at'],
        'ip': r['ip'],
        'user_agent': r['user_agent'],
        'current': r['id'] == current,
    } for r in rows])


@app.route('/api/auth/password', methods=['POST'])
def auth_change_password():
    user = auth.current_user()
    if user is None:
        return auth.unauthenticated()

    data = request.json or {}
    current_password = data.get('current') or ''
    new_password = data.get('new') or ''

    if not auth.verify_password(user['password_hash'], current_password):
        return jsonify({'error': 'Current password is incorrect'}), 400
    problem = auth.password_problem(new_password)
    if problem:
        return jsonify({'error': problem}), 400

    conn = db.get_connection()
    try:
        conn.execute(
            'UPDATE users SET password_hash = ?, must_change_password = 0, '
            '                 updated_at = CURRENT_TIMESTAMP WHERE id = ?',
            (auth.hash_password(new_password), user['id']))
        conn.commit()
        # Other sessions are dropped: a password change is how you evict someone
        # who should no longer be signed in.
        auth.delete_user_sessions(conn, user['id'], keep=getattr(g, 'session_id', None))
        updated = auth.get_user(conn, user['id'])
    finally:
        conn.close()
    return jsonify({'ok': True, 'user': auth.user_public(updated)})


# ─── User administration ──────────────────────────────────────────────────────

@app.route('/api/users', methods=['GET'])
@auth.login_required
def list_users():
    """Any signed-in user may list accounts: sharing a catalog needs a name to
    share with. Only administrators see the administrative detail."""
    user = auth.current_user()
    conn = db.get_connection()
    try:
        rows = conn.execute('SELECT * FROM users ORDER BY display_name').fetchall()
    finally:
        conn.close()
    if user is not None and user['is_admin']:
        return jsonify([auth.user_public(r) for r in rows])
    return jsonify([{'id': r['id'], 'username': r['username'],
                     'display_name': r['display_name']} for r in rows])


@app.route('/api/users', methods=['POST'])
@auth.admin_required
def create_user():
    data = request.json or {}
    username = (data.get('username') or '').strip()
    password = data.get('password') or ''
    if not username:
        return jsonify({'error': 'Username is required'}), 400
    problem = auth.password_problem(password)
    if problem:
        return jsonify({'error': problem}), 400

    conn = db.get_connection()
    try:
        if auth.get_user_by_username(conn, username):
            return jsonify({'error': 'That username is taken'}), 409
        cur = conn.execute(
            'INSERT INTO users (username, email, display_name, password_hash, is_admin, '
            '                   must_change_password) VALUES (?,?,?,?,?,1)',
            (username, (data.get('email') or '').strip() or None,
             (data.get('display_name') or username).strip(),
             auth.hash_password(password), 1 if data.get('is_admin') else 0))
        conn.commit()
        created = auth.get_user(conn, cur.lastrowid)
    finally:
        conn.close()
    return jsonify(auth.user_public(created)), 201


@app.route('/api/users/<int:user_id>', methods=['PATCH'])
@auth.login_required
def update_user(user_id):
    actor = auth.current_user()
    is_admin = bool(actor and actor['is_admin'])
    if not is_admin and (actor is None or actor['id'] != user_id):
        return auth.forbidden('You may only change your own profile')

    data = request.json or {}

    # You cannot demote or deactivate yourself. Both are one request away from
    # locking yourself out of the page that would undo it, and the "last
    # administrator" check below does not catch it while a second admin exists.
    # The UI disables these controls on your own row; this is the half that
    # holds when the request does not come from the UI. manage_users.py still
    # works from a shell, so there is always a way back.
    if actor is not None and actor['id'] == user_id:
        if data.get('is_admin') is False:
            return jsonify({'error': 'You cannot remove your own administrator rights. '
                                     'Ask another administrator, or use '
                                     'manage_users.py set-admin.'}), 409
        if data.get('is_active') is False:
            return jsonify({'error': 'You cannot deactivate your own account.'}), 409

    fields, values = [], []
    for key in ('display_name', 'email'):
        if key in data:
            fields.append(f'{key} = ?')
            values.append((data[key] or '').strip() or None)

    # ── Link to a person ─────────────────────────────────────────────────────
    # Sent as a pair. Either both are given (link), or person_id is null
    # (unlink). person_id alone is meaningless: people.id is unique only within
    # a catalog.
    if 'person_id' in data or 'person_catalog_id' in data:
        link_catalog = data.get('person_catalog_id')
        link_person = data.get('person_id')

        if link_person in (None, '', 0):
            fields += ['person_catalog_id = ?', 'person_id = ?']
            values += [None, None]
        else:
            try:
                link_catalog = int(link_catalog)
                link_person = int(link_person)
            except (TypeError, ValueError):
                return jsonify({'error': 'person_catalog_id and person_id must be integers'}), 400

            # The person must be in a catalog the CALLER can reach; otherwise
            # this endpoint would let anyone probe for people in catalogs they
            # cannot see, one id at a time.
            if auth.enabled_for(app) and actor is not None:
                c = db.get_connection()
                try:
                    reachable = auth.role_for(c, actor['id'], link_catalog) is not None
                finally:
                    c.close()
                if not reachable:
                    return auth.forbidden('That catalog is not available to you')

            if _fetch_person(link_catalog, link_person) is None:
                return jsonify({'error': 'That person no longer exists'}), 404

            c = db.get_connection()
            try:
                clash = c.execute(
                    'SELECT id, display_name FROM users '
                    'WHERE person_catalog_id = ? AND person_id = ? AND id != ?',
                    (link_catalog, link_person, user_id)).fetchone()
            finally:
                c.close()
            if clash:
                return jsonify({'error': f'That person is already linked to '
                                         f'{clash["display_name"]}.'}), 409

            fields += ['person_catalog_id = ?', 'person_id = ?']
            values += [link_catalog, link_person]

    # Only an administrator may change privileges, and never their own last one.
    if is_admin:
        for key in ('is_admin', 'is_active'):
            if key in data:
                fields.append(f'{key} = ?')
                values.append(1 if data[key] else 0)
    if not fields:
        return jsonify({'error': 'Nothing to update'}), 400

    conn = db.get_connection()
    try:
        if is_admin and data.get('is_admin') is False:
            others = conn.execute(
                'SELECT COUNT(*) AS n FROM users WHERE is_admin = 1 AND id != ?',
                (user_id,)).fetchone()['n']
            if others == 0:
                return jsonify({'error': 'That is the only administrator'}), 409
        values.append(user_id)
        conn.execute(f'UPDATE users SET {", ".join(fields)}, '
                     f'updated_at = CURRENT_TIMESTAMP WHERE id = ?', values)
        conn.commit()
        if data.get('is_active') is False:
            auth.delete_user_sessions(conn, user_id)
        updated = auth.get_user(conn, user_id)
    finally:
        conn.close()
    if updated is None:
        return jsonify({'error': 'User not found'}), 404
    return jsonify(auth.user_public(updated))


@app.route('/api/users/<int:user_id>', methods=['DELETE'])
@auth.admin_required
def delete_user(user_id):
    actor = auth.current_user()
    if actor is not None and actor['id'] == user_id:
        return jsonify({'error': 'You cannot delete your own account'}), 409

    conn = db.get_connection()
    try:
        # Refuse if this would leave a catalog with no owner, which would make
        # that catalog unadministrable (spec 4.2).
        orphaned = conn.execute(
            "SELECT c.catalog_id FROM catalog_access c WHERE c.user_id = ? AND c.role = 'owner' "
            "AND (SELECT COUNT(*) FROM catalog_access o "
            "     WHERE o.catalog_id = c.catalog_id AND o.role = 'owner') = 1",
            (user_id,)).fetchall()
        if orphaned:
            ids = ', '.join(str(r['catalog_id']) for r in orphaned)
            return jsonify({'error': f'That user is the only owner of catalog(s) {ids}. '
                                     f'Give another user ownership first.'}), 409
        conn.execute('DELETE FROM catalog_access WHERE user_id = ?', (user_id,))
        conn.execute('DELETE FROM sessions WHERE user_id = ?', (user_id,))
        conn.execute('DELETE FROM users WHERE id = ?', (user_id,))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/users/<int:user_id>/profile', methods=['GET'])
@auth.login_required
def get_user_profile(user_id):
    """
    One account's profile fields — address, city, country, birthdate, home
    coordinates. These live in user_settings, which is the per-user profile
    table; the users row itself carries only identity and rights.

    /api/user/settings is the self-service equivalent and always answers for the
    caller. This one is addressed by id so an administrator can edit somebody
    else from Admin > Users.
    """
    actor = auth.current_user()
    if actor is not None and not actor['is_admin'] and actor['id'] != user_id:
        return auth.forbidden('You may only view your own profile')

    conn = db.get_connection()
    try:
        target = auth.get_user(conn, user_id)
    finally:
        conn.close()
    if target is None:
        return jsonify({'error': 'User not found'}), 404

    settings = user_settings_model.get_user_settings(user_id)
    if not settings:
        # Never saved: hand back a blank record seeded from the account, so the
        # form opens populated rather than empty.
        settings = {
            'id': None, 'user_id': user_id,
            'name': target['display_name'], 'email': target['email'],
            'address': '', 'city': '', 'state': '', 'country': '',
            'lat': None, 'long': None, 'birthdate': '',
        }
    return jsonify(settings)


@app.route('/api/users/<int:user_id>/profile', methods=['PUT'])
@auth.login_required
def update_user_profile(user_id):
    actor = auth.current_user()
    if actor is not None and not actor['is_admin'] and actor['id'] != user_id:
        return auth.forbidden('You may only change your own profile')

    conn = db.get_connection()
    try:
        target = auth.get_user(conn, user_id)
    finally:
        conn.close()
    if target is None:
        return jsonify({'error': 'User not found'}), 404

    data = request.json or {}
    name = (data.get('name') or target['display_name'] or '').strip()
    if not name:
        return jsonify({'error': 'Name is required'}), 400

    def _coord(value):
        if value in (None, ''):
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    try:
        settings = user_settings_model.update_user_settings(
            user_id, name,
            (data.get('email') or '').strip(),
            (data.get('address') or '').strip(),
            (data.get('city') or '').strip(),
            (data.get('state') or '').strip(),
            (data.get('country') or '').strip(),
            _coord(data.get('lat')), _coord(data.get('long')),
            (data.get('birthdate') or '').strip() or None,
        )
        return jsonify(settings)
    except Exception as e:
        return jsonify({'error': str(e)}), 500


@app.route('/api/users/<int:user_id>/password', methods=['POST'])
@auth.admin_required
def admin_set_password(user_id):
    """
    An administrator sets another account's password from its page in Admin >
    Users - what manage_users.py reset-password does from a shell. Like the
    temporary password an account is created with, the holder must choose their
    own at the next sign in, and every device they are signed in on is signed
    out. Your OWN password is changed with the current one (/api/auth/password).
    """
    actor = auth.current_user()
    if actor is not None and actor['id'] == user_id:
        return jsonify({'error': 'Change your own password with your current one.'}), 409
    new_password = (request.json or {}).get('new') or ''
    problem = auth.password_problem(new_password)
    if problem:
        return jsonify({'error': problem}), 400
    conn = db.get_connection()
    try:
        if auth.get_user(conn, user_id) is None:
            return jsonify({'error': 'User not found'}), 404
        conn.execute('UPDATE users SET password_hash = ?, must_change_password = 1, '
                     '                 updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (auth.hash_password(new_password), user_id))
        conn.commit()
        auth.delete_user_sessions(conn, user_id)
    finally:
        conn.close()
    return jsonify({'ok': True})


def _sessions_target(user_id):
    """An administrator, or the account holder; else a 403 response."""
    actor = auth.current_user()
    if actor is not None and not actor['is_admin'] and actor['id'] != user_id:
        return None, auth.forbidden('You may only see your own devices')
    return actor, None


@app.route('/api/users/<int:user_id>/sessions', methods=['GET'])
@auth.login_required
def list_user_sessions(user_id):
    """An account's signed-in devices - /api/auth/sessions, by id, so an
    administrator sees anyone's on their page in Admin > Users."""
    _, err = _sessions_target(user_id)
    if err:
        return err
    conn = db.get_connection()
    try:
        rows = conn.execute(
            'SELECT id, created_at, last_seen_at, expires_at, ip, user_agent '
            'FROM sessions WHERE user_id = ? ORDER BY last_seen_at DESC',
            (user_id,)).fetchall()
    finally:
        conn.close()
    current = getattr(g, 'session_id', None)
    return jsonify([{
        'id': r['id'][:12],
        'created_at': r['created_at'],
        'last_seen_at': r['last_seen_at'],
        'expires_at': r['expires_at'],
        'ip': r['ip'],
        'user_agent': r['user_agent'],
        'current': r['id'] == current,
    } for r in rows])


@app.route('/api/users/<int:user_id>/sessions', methods=['DELETE'])
@auth.login_required
def sign_out_user_everywhere(user_id):
    """Sign an account out on every device. For your own account this is
    /api/auth/logout-all, this session included."""
    actor, err = _sessions_target(user_id)
    if err:
        return err
    conn = db.get_connection()
    try:
        auth.delete_user_sessions(conn, user_id)
    finally:
        conn.close()
    response = jsonify({'ok': True})
    if actor is not None and actor['id'] == user_id:
        auth.clear_session_cookies(response)
    return response


# ─── Catalog sharing ──────────────────────────────────────────────────────────
# These carry catalog_id, so the before_request hook has already required the
# owner role by the time they run (auth._minimum_role_for_request).

@app.route('/api/catalogs/<int:catalog_id>/access', methods=['GET'])
@auth.login_required
def get_catalog_access(catalog_id):
    conn = db.get_connection()
    try:
        rows = conn.execute(
            'SELECT a.user_id, a.role, a.granted_at, u.username, u.display_name, u.email '
            'FROM catalog_access a JOIN users u ON u.id = a.user_id '
            'WHERE a.catalog_id = ? ORDER BY a.role, u.display_name', (catalog_id,)).fetchall()
    finally:
        conn.close()
    return jsonify([dict(r) for r in rows])


@app.route('/api/catalogs/<int:catalog_id>/access', methods=['POST'])
@auth.login_required
def set_catalog_access(catalog_id):
    data = request.json or {}
    user_id = data.get('user_id')
    role = data.get('role')
    if role not in auth.ROLES:
        return jsonify({'error': f'Role must be one of {", ".join(auth.ROLES)}'}), 400

    conn = db.get_connection()
    try:
        if auth.get_user(conn, user_id) is None:
            return jsonify({'error': 'User not found'}), 404
        # Demoting the last owner would leave nobody able to administer it.
        if auth.role_for(conn, user_id, catalog_id) == 'owner' and role != 'owner' \
                and auth.owner_count(conn, catalog_id) <= 1:
            return jsonify({'error': 'That is the last owner of this catalog'}), 409
        auth.grant_access(conn, catalog_id, user_id, role,
                          granted_by=(auth.current_user() or {'id': None})['id'])
    finally:
        conn.close()
    return jsonify({'ok': True})


@app.route('/api/catalogs/<int:catalog_id>/access/<int:user_id>', methods=['DELETE'])
@auth.login_required
def revoke_catalog_access(catalog_id, user_id):
    conn = db.get_connection()
    try:
        if auth.role_for(conn, user_id, catalog_id) == 'owner' \
                and auth.owner_count(conn, catalog_id) <= 1:
            return jsonify({'error': 'That is the last owner of this catalog'}), 409
        conn.execute('DELETE FROM catalog_access WHERE catalog_id = ? AND user_id = ?',
                     (catalog_id, user_id))
        conn.commit()
    finally:
        conn.close()
    return jsonify({'ok': True})


# ─── User ↔ Person link ───────────────────────────────────────────────────────
#
# A user is identity (application database); a person is catalog data (each
# catalog's workflow_results.db). They are deliberately NOT merged: a person
# exists whether or not they can sign in, and a user exists whether or not they
# appear in any photo. The link lets a profile borrow the person's details and,
# most visibly, their face.
#
# The reference is soft — SQLite has no cross-database foreign key — so these
# helpers treat a dangling link as "no person" rather than an error.

def _catalog_for_person_link(catalog_id):
    catalog = Catalog.get_by_id(catalog_id) if catalog_id else None
    if not catalog or not catalog.target_directory:
        return None
    return catalog


def _fetch_person(catalog_id, person_id):
    """The person row, or None when the catalog or person has gone."""
    catalog = _catalog_for_person_link(catalog_id)
    if not catalog or not person_id:
        return None
    try:
        catalog_db = CatalogDatabase(catalog.target_directory)
        conn = catalog_db.get_connection()
        try:
            return conn.execute('SELECT * FROM people WHERE id = ?', (person_id,)).fetchone()
        finally:
            conn.close()
    except Exception:
        return None


# A square avatar, built once and cached. 256 covers the 64px card at 2x and
# the 32px header at 2x with room to spare.
AVATAR_SIZE = 256
# How much of the surrounding photo to keep around the face box. 1.0 would crop
# to the detection rectangle itself, which cuts off hair and chin.
AVATAR_PADDING = 1.8


def _build_person_avatar(catalog, person_id):
    """
    Build a square, undistorted avatar for a person and cache it to disk.

    Why not reuse the face_contents crops: those are produced by cropping the
    face RECTANGLE and then calling resize((300, 300)), which does not preserve
    aspect ratio - a 34x43 detection becomes 300x300, stretching the face about
    26% wider than it should be. The files are square, so the distortion is
    inside the image and no amount of CSS fixes it.

    Those crops feed face recognition (ai_face_recognition_workflow joins
    face_contents), so they are deliberately left alone. This instead crops a
    SQUARE region around the face from the source photo, so the resize is
    square-to-square and changes nothing about the proportions.

    Returns the cached path, or None when there is no face to work from.
    """
    import logging as _logging
    from PIL import Image, ImageOps

    import glob as _glob

    target_dir = catalog.target_directory
    cache_dir = os.path.join(target_dir, 'cache', 'avatars')

    catalog_db = CatalogDatabase(target_dir)
    conn = catalog_db.get_connection()
    try:
        person = conn.execute('SELECT * FROM people WHERE id = ?', (person_id,)).fetchone()
        if not person:
            return None
        keys = person.keys()
        preferred_file = person['profile_photo_file_id'] if 'profile_photo_file_id' in keys else None

        # The detection on the chosen profile photo when there is one, else any.
        face = conn.execute(
            'SELECT fd.id, fd.file_id, fd.x, fd.y, fd.w, fd.h, '
            '       fd.image_width, fd.image_height '
            'FROM face_detections fd '
            'WHERE fd.person_id = ? AND fd.x IS NOT NULL '
            'ORDER BY CASE WHEN fd.file_id = ? THEN 0 ELSE 1 END, fd.id DESC LIMIT 1',
            (person_id, preferred_file)
        ).fetchone()
        if not face:
            return None

        # Cached per (person, photo, detection), not per person. Keyed by the
        # person alone, the first avatar ever built was served forever: picking
        # a new profile photo in the catalog never reached the linked user's
        # picture (2026-10-04 - built 2026-09-09, still served).
        out_path = os.path.join(
            cache_dir, f"person_{person_id}_{face['file_id']}_{face['id']}.jpg")
        if os.path.exists(out_path):
            return out_path

        # The image the detection was run against: the browser-friendly version
        # when there is one, since image_width/height refer to it.
        # The base rung, pinned: image_width/image_height on the detection
        # refer to it, and this crops with them (spec 6.2).
        src = conn.execute(
            'SELECT fc.file_path, av.alternative_file_path '
            'FROM file_contents fc '
            'LEFT JOIN alternative_versions av ON av.original_file_id = fc.id '
            "  AND av.rung IN ('1200','1600','2048','2560') "
            'WHERE fc.id = ? '
            "ORDER BY (av.rung = '1200') DESC, "
            '         av.alternative_file_path IS NULL, av.width ASC, av.id ASC LIMIT 1',
            (face['file_id'],)
        ).fetchone()
        if not src:
            return None
        # Resolved here, inside the branch, because the alternate and the
        # original live under different roots in a reference catalog.
        if src['alternative_file_path']:
            raw_path = _resolve_path(src['alternative_file_path'], target_dir)
        else:
            raw_path = _resolve_path(src['file_path'], _original_root(catalog))
    finally:
        conn.close()

    image_path = raw_path
    if not image_path or not os.path.exists(image_path):
        return None

    try:
        with Image.open(image_path) as img:
            img = ImageOps.exif_transpose(img) or img
            img = _turn_original(img, image_path, target_dir, face['file_id'])
            if img.mode != 'RGB':
                img = img.convert('RGB')

            # The box was measured against an image of image_width x image_height.
            # If we opened a different rendition, scale it across.
            sx = img.width / face['image_width'] if face['image_width'] else 1.0
            sy = img.height / face['image_height'] if face['image_height'] else 1.0
            fx, fy = face['x'] * sx, face['y'] * sy
            fw, fh = face['w'] * sx, face['h'] * sy

            # A SQUARE region around the face centre. Square in, square out, so
            # the resize below cannot distort anything.
            cx, cy = fx + fw / 2.0, fy + fh / 2.0
            side = max(fw, fh) * AVATAR_PADDING
            side = min(side, img.width, img.height)      # must fit the photo
            left = min(max(cx - side / 2.0, 0), img.width - side)
            top = min(max(cy - side / 2.0, 0), img.height - side)

            crop = img.crop((int(left), int(top), int(left + side), int(top + side)))
            crop = crop.resize((AVATAR_SIZE, AVATAR_SIZE), Image.Resampling.LANCZOS)

            os.makedirs(cache_dir, exist_ok=True)
            crop.save(out_path, 'JPEG', quality=88, optimize=True)
            # The avatars of earlier choices, and the old person-only name.
            for stale in (_glob.glob(os.path.join(cache_dir, f'person_{person_id}_*.jpg'))
                          + [os.path.join(cache_dir, f'person_{person_id}.jpg')]):
                if stale != out_path:
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
            return out_path
    except Exception as exc:
        _logging.getLogger(__name__).warning('avatar build failed for person %s: %s',
                                             person_id, exc)
        return None


def _person_avatar_file(catalog_id, person_id):
    """
    Absolute path of the face picture for a person, or None.

    Preference:
      1. people.profile_photo_path - a crop chosen deliberately as their
         profile photo.
      2. A square avatar built from the source photo (see _build_person_avatar).
      3. A face_contents crop, only as a last resort: those are aspect-distorted
         (see above), but a stretched face beats no face.
    """
    catalog = _catalog_for_person_link(catalog_id)
    if not catalog or not person_id:
        return None

    try:
        catalog_db = CatalogDatabase(catalog.target_directory)
        conn = catalog_db.get_connection()
        try:
            person = conn.execute('SELECT * FROM people WHERE id = ?', (person_id,)).fetchone()
            if not person:
                return None

            chosen = person['profile_photo_path'] if 'profile_photo_path' in person.keys() else None
            if chosen:
                path = _resolve_path(chosen, catalog.target_directory)
                if path and os.path.exists(path):
                    return path

            built = _build_person_avatar(catalog, person_id)
            if built:
                return built

            source_id = person['profile_photo_file_id'] if 'profile_photo_file_id' in person.keys() else None
            row = conn.execute(
                'SELECT file_path FROM face_contents WHERE person_id = ? '
                'ORDER BY CASE WHEN source_file_id = ? THEN 0 ELSE 1 END, id DESC LIMIT 1',
                (person_id, source_id)
            ).fetchone()
            if row and row['file_path']:
                path = _resolve_path(row['file_path'], catalog.target_directory)
                if path and os.path.exists(path):
                    return path
        finally:
            conn.close()
    except Exception:
        return None
    return None


def _person_payload(catalog_id, person_id):
    """What a linked person contributes to a user profile."""
    person = _fetch_person(catalog_id, person_id)
    if person is None:
        return None
    catalog = _catalog_for_person_link(catalog_id)
    keys = person.keys()
    return {
        'catalog_id': catalog_id,
        'catalog_name': catalog.name if catalog else None,
        'person_id': person['id'],
        'name': person['name'],
        'date_of_birth': person['date_of_birth'] if 'date_of_birth' in keys else None,
        'description': person['description'] if 'description' in keys else None,
        'category': person['category'] if 'category' in keys else None,
        'has_avatar': _person_avatar_file(catalog_id, person_id) is not None,
    }


@app.route('/api/users/<int:user_id>/person', methods=['GET'])
@auth.login_required
def get_user_person(user_id):
    """The person a user is linked to, or null. Visible to the user and admins."""
    actor = auth.current_user()
    if actor is not None and not actor['is_admin'] and actor['id'] != user_id:
        return auth.forbidden('You may only view your own linked person')

    conn = db.get_connection()
    try:
        row = auth.get_user(conn, user_id)
    finally:
        conn.close()
    if row is None:
        return jsonify({'error': 'User not found'}), 404

    payload = _person_payload(auth._opt(row, 'person_catalog_id'), auth._opt(row, 'person_id'))
    # A link that no longer resolves is reported as unlinked with a note, rather
    # than as an error: the catalog may simply have been removed.
    return jsonify({
        'person': payload,
        'link': {
            'person_catalog_id': auth._opt(row, 'person_catalog_id'),
            'person_id': auth._opt(row, 'person_id'),
        },
        'dangling': bool(auth._opt(row, 'person_id')) and payload is None,
    })


@app.route('/api/users/<int:user_id>/avatar', methods=['GET'])
@auth.login_required
def get_user_avatar(user_id):
    """
    The linked person's face picture.

    Readable by any signed-in user: a name and a face are how you tell people
    apart when sharing a catalog, and this picture was chosen deliberately as a
    profile photo. 404 when there is no link or no crop, so the UI can fall back
    to initials.
    """
    from flask import send_file

    conn = db.get_connection()
    try:
        row = auth.get_user(conn, user_id)
    finally:
        conn.close()
    if row is None:
        return jsonify({'error': 'User not found'}), 404

    path = _person_avatar_file(auth._opt(row, 'person_catalog_id'), auth._opt(row, 'person_id'))
    if not path:
        return jsonify({'error': 'No avatar'}), 404

    # The crop is rewritten when the profile photo changes, and the URL does not
    # change with the link either: the browser revalidates every time and an
    # unchanged face costs a 304.
    etag = _image_etag(path)
    not_modified = _image_not_modified(etag, revalidate=True)
    if not_modified is not None:
        return not_modified
    response = _image_cached(send_file(path, mimetype='image/jpeg'), etag, revalidate=True)
    return response


@app.route('/api/people/linkable', methods=['GET'])
@auth.login_required
def list_linkable_people():
    """
    People across the catalogs the caller can reach, for the link picker.

    One call rather than one per catalog, and it reports who is already taken so
    the UI can grey those out instead of failing on the unique index.
    """
    actor = auth.current_user()
    conn = db.get_connection()
    try:
        if actor is not None and auth.enabled_for(app):
            allowed = set(auth.accessible_catalog_ids(conn, actor['id']))
        else:
            allowed = {c.id for c in Catalog.get_all()}
        taken = {
            (r['person_catalog_id'], r['person_id']): r['display_name']
            for r in conn.execute(
                'SELECT person_catalog_id, person_id, display_name FROM users '
                'WHERE person_id IS NOT NULL').fetchall()
        }
    finally:
        conn.close()

    out = []
    for catalog in Catalog.get_all():
        if catalog.id not in allowed or not catalog.target_directory:
            continue
        try:
            catalog_db = CatalogDatabase(catalog.target_directory)
            c = catalog_db.get_connection()
            try:
                rows = c.execute(
                    'SELECT id, name, category FROM people ORDER BY name').fetchall()
            finally:
                c.close()
        except Exception:
            continue
        for r in rows:
            out.append({
                'catalog_id': catalog.id,
                'catalog_name': catalog.name,
                'person_id': r['id'],
                'name': r['name'],
                'category': r['category'] if 'category' in r.keys() else None,
                'linked_to': taken.get((catalog.id, r['id'])),
            })
    return jsonify(out)


# ─── Socket.IO handshake (spec 6.4) ───────────────────────────────────────────

@socketio.on('connect')
def _socket_authenticate(*_args):
    """
    Authenticate the websocket handshake with the same session cookie.

    Returning False rejects the connection. Without this the progress stream
    stays open to anyone who can reach the port, which would leak one account's
    workflow activity to another even though every HTTP route is protected.
    """
    if not auth.enabled_for(app):
        return True

    session_id = auth.session_cookie(request)
    if not session_id:
        return False

    conn = db.get_connection()
    try:
        return auth.lookup_session(conn, session_id) is not None
    finally:
        conn.close()


@app.route('/api/systems/', methods=['GET'])
def get_systems():
    return jsonify({'systems': []})


# The gallery reads catalogs.json to turn a URL's catalog segment into a
# directory. Written at import time as well as on every catalog change, so an
# installation that has never edited a catalog since this feature shipped still
# has one - and so a file deleted by hand comes back on the next restart. Runs
# under gunicorn too, which is why it is here and not in the __main__ block.
try:
    _write_catalogs_index()
except Exception as _exc:  # pragma: no cover - never worth failing boot for
    _logging.getLogger(__name__).warning('catalogs.json not written at boot: %s', _exc)



# ═══ Album transfer over magic-wormhole ═══════════════════════════════════════
#
# See documentation/specs/wormhole-album-transfer-spec.md.
#
# The bytes move in a subprocess (wormhole_runner.py) because magic-wormhole is
# Twisted and this process is gunicorn with gevent monkey-patching. These
# endpoints only ever create rows, launch that process, and read state back.

def _transfer_scratch(catalog):
    """Where a send manifest is staged. Inside the catalog, so it is writable."""
    path = os.path.join(catalog.target_directory, 'cache', 'Transfers')
    os.makedirs(path, exist_ok=True)
    return path


def _send_manifest(album_name, files):
    return {'album': {'name': album_name,
                      'slug': wormhole_transfer.slugify(album_name),
                      'photo_count': len(files)},
            'files': files}


def _launch_send(catalog, catalog_db, transfer_id, album_name, files,
                 expires_file=None):
    """One sender runner for one transfer row - single, resumed or batched."""
    return wormhole_transfer.launch(
        catalog_db, transfer_id, 'send', socketio.emit,
        manifest=_send_manifest(album_name, files),
        scratch_dir=_transfer_scratch(catalog), expires_file=expires_file)


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/transfer',
           methods=['POST'])
def start_album_transfer(catalog_id, album_id):
    """Begin sending an album's originals to another airlock install."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    conn = catalog_db.get_connection()
    try:
        album = conn.execute('SELECT id, name FROM albums WHERE id = ?',
                             (album_id,)).fetchone()
    finally:
        conn.close()
    if not album:
        return jsonify({'error': 'Album not found'}), 404
    album = dict(album)

    # Two senders writing the same partial files on the far side is a corruption,
    # and the useful answer to "already running" is the thing that is running
    # (spec 3.6).
    running = wormhole_transfer.active_transfer_for_album(catalog_db, album_id)
    if running:
        return jsonify({'error': 'A transfer of this album is already running.',
                        'transfer_id': running['id']}), 409

    files = wormhole_transfer.album_files(catalog, catalog_db, album_id)
    if not files:
        return jsonify({'error': 'This album has no original files to transfer. '
                                 'Photos need a sha256 hash, which ingestion '
                                 'records; re-run the import if this is '
                                 'unexpected.'}), 400

    missing = [f['name'] for f in files if not os.path.exists(f['path'])]
    transfer_id = wormhole_transfer.create_send_transfer(
        catalog, catalog_db, album, files)
    _launch_send(catalog, catalog_db, transfer_id, album['name'], files)

    return jsonify({'transfer_id': transfer_id,
                    'total_files': len(files),
                    'total_bytes': sum(f['size'] for f in files),
                    # Named up front rather than discovered as failures one by
                    # one halfway through a multi-gigabyte transfer.
                    'missing_files': missing}), 201


@app.route('/api/catalogs/<int:catalog_id>/transfers/receive', methods=['POST'])
def start_receive_transfer(catalog_id):
    """Accept an album from another airlock install into one of this catalog's routes."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404

    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({'error': 'A wormhole code is required.'}), 400
    # The row first, because the folder is named after it: one folder per
    # transfer, and the id is the only thing unique at this point - the album's
    # own name arrives later, with the peer's manifest.
    transfer_id = wormhole_transfer.create_receive_transfer(
        catalog_db, code, None, None)
    destination = wormhole_transfer.transfer_dir(
        catalog, transfer_id, data.get('label') or data.get('subdir'))
    # Only the PARENT is created. The transfer folder itself comes into
    # existence when the runner renames the finished album into place, so a
    # folder under transfers/ always means a complete album - which is what
    # makes it safe for a workflow to ingest anything it finds there.
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
    except OSError as exc:
        wormhole_transfer.update(catalog_db, transfer_id, status='failed',
                                 error_message=f'cannot create {destination}: {exc}')
        return jsonify({'error': f'Cannot create {destination}: {exc}'}), 400
    wormhole_transfer.update(catalog_db, transfer_id, destination=destination)
    state_dir = wormhole_transfer.transfer_state_dir(catalog, transfer_id)
    wormhole_transfer.launch(catalog_db, transfer_id, 'receive', socketio.emit,
                             code=code, destination=destination,
                             state_dir=state_dir)
    return jsonify({'transfer_id': transfer_id,
                    'destination': destination}), 201


@app.route('/api/catalogs/<int:catalog_id>/transfers', methods=['GET'])
def list_album_transfers(catalog_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    wormhole_transfer.sweep(catalog_db)
    return jsonify({'transfers': wormhole_transfer.list_transfers(catalog_db),
                    'batches': wormhole_transfer.list_batches(catalog_db)})


@app.route('/api/catalogs/<int:catalog_id>/transfers/<int:transfer_id>',
           methods=['GET'])
def get_album_transfer(catalog_id, transfer_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    row = wormhole_transfer.payload(catalog_db, transfer_id)
    if not row:
        return jsonify({'error': 'Transfer not found'}), 404
    return jsonify(row)


@app.route('/api/catalogs/<int:catalog_id>/transfers/<int:transfer_id>/files',
           methods=['GET'])
def get_album_transfer_files(catalog_id, transfer_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    return jsonify(wormhole_transfer.list_files(
        catalog_db, transfer_id,
        page=request.args.get('page', 1),
        per_page=request.args.get('per_page', 50)))


@app.route('/api/catalogs/<int:catalog_id>/transfers/<int:transfer_id>/cancel',
           methods=['POST'])
def cancel_album_transfer(catalog_id, transfer_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    if not wormhole_transfer.cancel(catalog_db, transfer_id):
        return jsonify({'error': 'Transfer not found'}), 404
    return jsonify({'ok': True})


@app.route('/api/catalogs/<int:catalog_id>/transfers/<int:transfer_id>/resume',
           methods=['POST'])
def resume_album_transfer(catalog_id, transfer_id):
    """
    Run again, against the same rows.

    A new session needs a new code - codes are single-use - so what "resume"
    saves is the transfer, not the typing. Files already `done` or `skipped` are
    still offered: the peer's own inventory decides what actually moves, and it
    is the only side that knows what survived (spec 3.5).
    """
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    row = wormhole_transfer.get_transfer(catalog_db, transfer_id)
    if not row:
        return jsonify({'error': 'Transfer not found'}), 404
    if row['status'] in wormhole_transfer.ACTIVE_STATUSES:
        return jsonify({'error': 'That transfer is still running.',
                        'transfer_id': transfer_id}), 409

    if row['direction'] == 'send':
        expires_file = None
        if row.get('batch_id'):
            # A group member resumes on the group's deadline. Past it, the
            # group makes new codes, which is where a deadline is chosen
            # (album-mass-transfer-spec.md 3.6).
            batch = wormhole_transfer.get_batch(catalog_db, row['batch_id'])
            if (not batch or batch['cancelled_at'] or
                    wormhole_transfer.from_utc_text(batch['expires_at'])
                    <= wormhole_transfer.utc_now()):
                return jsonify({'error': 'The codes in this group have expired or '
                                         'were stopped. Make a new code from the '
                                         'group.', 'batch_id': row['batch_id']}), 400
            expires_file = wormhole_transfer.write_expiry_file(
                _transfer_scratch(catalog), batch['id'], batch['expires_at'])
        files = wormhole_transfer.album_files(catalog, catalog_db, row['album_id'])
        if not files:
            return jsonify({'error': 'The album no longer has any originals.'}), 400
        wormhole_transfer.update(
            catalog_db, transfer_id, status='pending', error_message=None,
            wormhole_code=None, completed_at=None, done_files=0, done_bytes=0,
            skipped_bytes=0)
        _launch_send(catalog, catalog_db, transfer_id, row['album_name'], files,
                     expires_file=expires_file)
    else:
        code = (request.get_json(silent=True) or {}).get('code') or row['wormhole_code']
        if not code:
            return jsonify({'error': 'A new wormhole code is required: the '
                                     'previous one has been used.'}), 400
        wormhole_transfer.update(
            catalog_db, transfer_id, status='pending', error_message=None,
            wormhole_code=code, completed_at=None, done_files=0, done_bytes=0,
            skipped_bytes=0)
        wormhole_transfer.launch(catalog_db, transfer_id, 'receive',
                                 socketio.emit, code=code,
                                 destination=row['destination'],
                                 state_dir=wormhole_transfer.transfer_state_dir(
                                     catalog, transfer_id))
    return jsonify({'transfer_id': transfer_id, 'ok': True})


# ─── Sending an album to several people ──────────────────────────────────────
# documentation/specs/album-mass-transfer-spec.md. A batch is N ordinary send
# transfers sharing one deadline; wormhole_transfer owns the rows, these routes
# launch the runners.

def _album_or_none(catalog_db, album_id):
    conn = catalog_db.get_connection()
    try:
        row = conn.execute('SELECT id, name FROM albums WHERE id = ?',
                           (album_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _batch_album_files(catalog, catalog_db, album_id):
    files = wormhole_transfer.album_files(catalog, catalog_db, album_id)
    if not files:
        raise wormhole_transfer.BatchError(
            'This album has no original files to transfer. Photos need a sha256 '
            'hash, which ingestion records; re-run the import if this is '
            'unexpected.')
    return files


def _launch_members(catalog, catalog_db, batch_id, album_name, files, ids, expires_at):
    path = wormhole_transfer.write_expiry_file(
        _transfer_scratch(catalog), batch_id, expires_at)
    for transfer_id in ids:
        _launch_send(catalog, catalog_db, transfer_id, album_name, files,
                     expires_file=path)


@app.route('/api/catalogs/<int:catalog_id>/albums/<int:album_id>/mass-transfer',
           methods=['POST'])
def start_mass_transfer(catalog_id, album_id):
    """One code per person, all waiting at once, one deadline (spec 3.1-3.3)."""
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    album = _album_or_none(catalog_db, album_id)
    if not album:
        return jsonify({'error': 'Album not found'}), 404

    # One open group per album: people are added to it rather than to a second
    # one, and the answer to "already exists" is the group (spec 3.8).
    existing = wormhole_transfer.open_batch_for_album(catalog_db, album_id)
    if existing:
        return jsonify({'error': 'This album already has a group of codes. Add '
                                 'people to it there.',
                        'batch_id': existing['id']}), 409

    data = request.get_json(silent=True) or {}
    recipients = data.get('recipients')
    if not isinstance(recipients, list):
        return jsonify({'error': 'Say who the album is for.'}), 400
    labels = [(r or {}).get('label', '') if isinstance(r, dict) else '' for r in recipients]
    try:
        files = _batch_album_files(catalog, catalog_db, album_id)
        made = wormhole_transfer.create_batch(catalog_db, album, files, labels,
                                              data.get('ttl_hours'))
    except wormhole_transfer.BatchError as exc:
        return jsonify({'error': str(exc)}), 400

    _launch_members(catalog, catalog_db, made['batch_id'], album['name'], files,
                    made['transfer_ids'], made['expires_at'])
    missing = [f['name'] for f in files if not os.path.exists(f['path'])]
    return jsonify({**made, 'missing_files': missing}), 201


def _batch_or_404(catalog_id, batch_id):
    catalog, catalog_db = _get_catalog_db_or_404(catalog_id)
    if not catalog:
        return None, None, None, (jsonify({'error': 'Catalog not found'}), 404)
    batch = wormhole_transfer.get_batch(catalog_db, batch_id)
    if not batch:
        return None, None, None, (jsonify({'error': 'Group not found'}), 404)
    return catalog, catalog_db, batch, None


@app.route('/api/catalogs/<int:catalog_id>/transfer-batches/<int:batch_id>',
           methods=['GET'])
def get_transfer_batch(catalog_id, batch_id):
    catalog, catalog_db, batch, err = _batch_or_404(catalog_id, batch_id)
    if err:
        return err
    wormhole_transfer.sweep(catalog_db)
    return jsonify(wormhole_transfer.batch_payload(catalog_db, batch_id))


@app.route('/api/catalogs/<int:catalog_id>/transfer-batches/<int:batch_id>/people',
           methods=['POST'])
def add_transfer_batch_person(catalog_id, batch_id):
    """One more person, on the group's deadline (spec 3.8)."""
    catalog, catalog_db, batch, err = _batch_or_404(catalog_id, batch_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        files = _batch_album_files(catalog, catalog_db, batch['album_id'])
        transfer_id = wormhole_transfer.add_to_batch(
            catalog_db, batch_id, {'id': batch['album_id'], 'name': batch['album_name']},
            files, data.get('label'))
    except wormhole_transfer.BatchError as exc:
        return jsonify({'error': str(exc)}), 400
    _launch_members(catalog, catalog_db, batch_id, batch['album_name'], files,
                    [transfer_id], batch['expires_at'])
    return jsonify({'transfer_id': transfer_id}), 201


@app.route('/api/catalogs/<int:catalog_id>/transfer-batches/<int:batch_id>/renew',
           methods=['POST'])
def renew_transfer_batch(catalog_id, batch_id):
    """
    New codes - for one person or everyone unfinished - and a new deadline for
    the whole group (spec 3.6). Members still waiting adopt it through the
    expiry file, which is rewritten before the new runners start.
    """
    catalog, catalog_db, batch, err = _batch_or_404(catalog_id, batch_id)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    try:
        files = _batch_album_files(catalog, catalog_db, batch['album_id'])
        renewed = wormhole_transfer.renew_batch(
            catalog_db, batch_id, data.get('ttl_hours'), data.get('transfer_ids'))
    except wormhole_transfer.BatchError as exc:
        return jsonify({'error': str(exc)}), 400
    _launch_members(catalog, catalog_db, batch_id, batch['album_name'], files,
                    renewed['transfer_ids'], renewed['expires_at'])
    return jsonify(renewed)


@app.route('/api/catalogs/<int:catalog_id>/transfer-batches/<int:batch_id>/cancel',
           methods=['POST'])
def cancel_transfer_batch(catalog_id, batch_id):
    catalog, catalog_db, batch, err = _batch_or_404(catalog_id, batch_id)
    if err:
        return err
    return jsonify({'stopped': wormhole_transfer.cancel_batch(catalog_db, batch_id)})


def _reap_orphan_transfers():
    """At start-up: transfers the previous process left active are ended, so the
    screen never shows a code that no longer exists (album-mass-transfer-spec.md
    3.7). Single transfers too - they had the same defect."""
    try:
        catalogs = Catalog.get_all()
    except Exception:
        return
    for catalog in catalogs:
        path = os.path.join(catalog.target_directory or '', 'workflow_results.db')
        if not catalog.target_directory or not os.path.exists(path):
            continue
        try:
            conn = sqlite3.connect(path, timeout=60)
            try:
                n = wormhole_transfer.reap_orphans(conn)
            finally:
                conn.close()
            if n:
                print(f'[wormhole] {n} transfer(s) in {catalog.name} ended by the restart')
        except Exception as exc:
            print(f'[wormhole] could not end orphaned transfers in {catalog.name}: {exc}')


# Here, after the function: module code runs top to bottom (test_module_ordering).
if os.environ.get('DISABLE_WORKFLOW_SCHEDULER', '').lower() not in ('1', 'true', 'yes'):
    threading.Thread(target=_reap_orphan_transfers, daemon=True,
                     name='wormhole-orphans').start()

if __name__ == '__main__':
    # Development entrypoint only - containers run gunicorn (see Dockerfile).
    # debug is off unless FLASK_DEBUG is set: the Werkzeug debugger exposes an
    # interactive console to anyone who can reach the port, which is not something
    # to leave on by default on a machine serving a photo library.
    _debug = os.environ.get('FLASK_DEBUG', '').lower() in ('1', 'true', 'yes')
    socketio.run(app, debug=_debug, host='0.0.0.0', port=5000,
                 allow_unsafe_werkzeug=True)
