## AUTO DETECT FACES WORKFLOW

import hashlib
import os
import sys
import sqlite3
import requests
from datetime import datetime
from PIL import Image, ImageOps
import io
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from typing import Optional, Tuple

# Add parent directory to path to import catalog module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import Catalog
# Cooperative stop. This workflow already had the machinery for leaving early -
# abort_event, for a detector that has gone down - so a stop request is simply a
# second reason to set it, with a different outcome.
from lib.stopping import stop_requested, record_stopped, stop_message


# Abort after this many images fail back-to-back. Individual failures are
# expected and tolerated, but a run where nothing succeeds is an outage, not a
# workload - and because failures are deliberately NOT marked as scanned (so a
# transient error doesn't permanently skip an image), a persistent outage would
# otherwise walk the entire catalog achieving nothing, with the progress counter
# advancing the whole way. That is exactly what it did: a crash-looping detector
# produced 1.2 hours of "progress" with zero images completed.
MAX_CONSECUTIVE_FAILURES = 10

# ── Detection image sizing ────────────────────────────────────────────────────
#
# YuNet needs at most DETECTION_INPUT_MAX_DIM px on the longest side; the
# service downsamples anything larger before inference.
# Sending a 24 MP original therefore costs a full-resolution decode, a
# full-resolution EXIF rotation, a full-resolution JPEG re-encode and ~12 MB
# over the wire, and buys exactly nothing - detection itself is ~2 % of the
# per-image wall clock.
#
# We instead ask libjpeg for a DCT-scaled decode (1/2, 1/4, 1/8) via
# Image.draft() and ship a small JPEG. The boxes come back in that smaller
# coordinate space, which is already handled: face_detections stores the
# detection dimensions, and extract_and_save_ai_face() rescales from them to
# whatever image it crops.
#
# Sizing note: backend-deepface runs YuNet at up to DETECTION_INPUT_MAX_DIM
# (1600 px by default). Sending less than that throws away faces - measured on
# one 18-person group shot, 1024 px found 12 faces where 1600 px found all 18.
# Sending more is wasted work, since the service caps inference anyway. Keep
# this value equal to the service's cap.
#
# Set DETECTION_MAX_DIM=0 to restore the old full-resolution behaviour.
DETECTION_MAX_DIM = int(os.environ.get('DETECTION_MAX_DIM', '1600'))
DETECTION_JPEG_QUALITY = int(os.environ.get('DETECTION_JPEG_QUALITY', '90'))

# How many images to have in flight at once. The expensive stages - JPEG decode,
# resize, re-encode and the HTTP round-trip - all release the GIL, so threads
# give real parallelism here. Set to 1 to restore the old strictly serial run.
DETECTION_CONCURRENCY = int(
    os.environ.get('DETECTION_CONCURRENCY', '0')
) or min(4, (os.cpu_count() or 4))


# EXIF orientation -> transpose ops, mirroring PIL's ImageOps.exif_transpose.
# Applied by hand so the rotation happens on the *downscaled* image rather than
# on the full-resolution one, and so it survives resize() dropping .info.
_ORIENTATION_OPS = {
    2: (Image.Transpose.FLIP_LEFT_RIGHT,),
    3: (Image.Transpose.ROTATE_180,),
    4: (Image.Transpose.FLIP_TOP_BOTTOM,),
    5: (Image.Transpose.FLIP_LEFT_RIGHT, Image.Transpose.ROTATE_90),
    6: (Image.Transpose.ROTATE_270,),
    7: (Image.Transpose.FLIP_LEFT_RIGHT, Image.Transpose.ROTATE_270),
    8: (Image.Transpose.ROTATE_90,),
}


def prepare_detection_jpeg(image_path: str, user_orientation: int = 1) -> Tuple[bytes, int, int]:
    """
    Build the smallest JPEG the detector needs, as cheaply as possible.

    user_orientation is the Basic Editor's turn or flip, for an ORIGINAL only:
    a display copy already carries it (basic-photo-editing.md 5.1).

    Returns (jpeg_bytes, width, height) in *detection* coordinate space. The
    caller must persist those dimensions alongside the returned boxes.
    """
    img = Image.open(image_path)

    # Read orientation before touching the pixels.
    orientation = None
    try:
        exif = img.getexif()
        if exif:
            orientation = exif.get(0x0112)
    except Exception:
        orientation = None

    if DETECTION_MAX_DIM > 0:
        try:
            # No-op for anything but JPEG; harmless where it does not apply.
            img.draft('RGB', (DETECTION_MAX_DIM, DETECTION_MAX_DIM))
        except Exception:
            pass

    full_size = img.size        # before draft() - to tell whether pixels were lost

    if img.mode in ('RGBA', 'LA', 'P'):
        if img.mode == 'P':
            img = img.convert('RGBA')
        flat = Image.new('RGB', img.size, (255, 255, 255))
        flat.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        img = flat
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    if DETECTION_MAX_DIM > 0 and max(img.size) > DETECTION_MAX_DIM:
        scale = DETECTION_MAX_DIM / float(max(img.size))
        img = img.resize(
            (max(1, round(img.width * scale)), max(1, round(img.height * scale))),
            Image.BILINEAR,
        )

    for op in _ORIENTATION_OPS.get(orientation, ()):
        img = img.transpose(op)
    if user_orientation and user_orientation != 1:
        from lib.orientation import apply_user
        img = apply_user(img, user_orientation)

    buf = io.BytesIO()
    img.save(buf, 'JPEG', quality=DETECTION_JPEG_QUALITY)
    width, height = img.size
    # DECODE ONCE. The face crops are cut from this same file, and used to
    # decode and rotate it again for EVERY face - ~469,000 extra decodes on
    # `family`. When nothing was scaled away (the 1200 px display copy is
    # under DETECTION_MAX_DIM, so the common case), this oriented RGB image is
    # exactly what the crop step would build, and is left for it on this
    # thread. A downscaled one is not: the crop must come from full resolution.
    if (width, height) in (full_size, full_size[::-1]):
        _thread.decoded = (os.path.abspath(image_path), img)
    else:
        img.close()
    return buf.getvalue(), width, height



# ─── The pet detector, riding along ──────────────────────────────────────────
#
# Animals are found HERE rather than in a pass of their own, and the reason is
# measured: detection is ~2 % of the per-image wall clock and the other ~97 ms
# is finding the file, decoding it, downsampling it and shipping it. A separate
# pet pass would pay that again over every file in the catalog - about ten
# hours of re-reading to rebuild an image this function already produced and
# threw away.
#
# So prepare_detection_jpeg() runs once and both detectors get the same bytes.
#
#   PRINCIPLE: pet detection never opens a file. It is handed a decoded image
#   by whatever already opened it.
#
# That is also what will make video work when frames arrive: whatever extracts
# them does it once, and both detectors see the same frames rather than the dog
# being found at 0:14 and the child holding him at 0:12.
#
# Measured on homeserver, 2026-10-09: yolox_tiny at 416 px costs ~91 ms/image, so
# this roughly doubles the pass on a one-off backlog and sees only newly
# imported files afterwards. See the spec's section 12.
PETS_URL = os.environ.get('PETS_URL', 'http://pets.airlock.lan')
PETS_DETECT_URL = PETS_URL.rstrip('/') + '/detect'
PETS_TIMEOUT = float(os.environ.get('PETS_DETECT_TIMEOUT', '60'))

# Resolved once per run in execute(), from the catalog's own property. With
# pets off this stays False and nothing below it costs anything: no HTTP
# session, no schema ensured, no table touched.
PETS_ENABLED = False

# A pet detector that is down must not take the face pass with it. Faces are
# the feature people already depend on; animals are new. So pet failures are
# counted separately and, past this many in a row, the ride-along switches
# itself off for the rest of the run and says so once.
PETS_MAX_CONSECUTIVE_FAILURES = int(os.environ.get('PETS_MAX_FAILURES', '10'))
_pets_consecutive_failures = 0
_pets_disabled_midrun = False


def get_files_already_pet_scanned(catalog_db_path: str):
    """
    File ids already examined for animals.

    A SEPARATE watermark from face_detection_scanned_at, and that is the whole
    point: every file in an existing catalog has been face-scanned and none has
    been pet-scanned. Sharing the column would mean the pet pass never looks at
    any of them, and the feature would appear to work while finding nothing.
    """
    conn = sqlite3.connect(catalog_db_path, timeout=60)
    try:
        cur = conn.cursor()
        cols = {r[1] for r in cur.execute('PRAGMA table_info(file_contents)')}
        if 'pets_scanned_at' not in cols:
            return set()
        return {r[0] for r in cur.execute(
            'SELECT id FROM file_contents WHERE pets_scanned_at IS NOT NULL')}
    except sqlite3.Error:
        return set()
    finally:
        conn.close()


def _boxes_overlap(a, b, threshold=0.5):
    """IoU over the threshold. Same rule and same number as faces_overlap()."""
    ax2, ay2 = a['x'] + a['w'], a['y'] + a['h']
    bx2, by2 = b['x'] + b['w'], b['y'] + b['h']
    ix1, iy1 = max(a['x'], b['x']), max(a['y'], b['y'])
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return False
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = a['w'] * a['h'] + b['w'] * b['h'] - inter
    return union > 0 and inter / union > threshold


def detect_pets_in_prepared(catalog_db_path: str, file_id: int, image_data: bytes,
                            detection_width: int, detection_height: int,
                            detection_file_name: str = 'image.jpg'):
    """
    Animals in an ALREADY PREPARED jpeg. Returns how many were stored.

    Also moves this file's pets_scanned_at watermark, in the SAME transaction
    as the boxes. The two are one fact - "this file has been looked at, and
    here is what was in it" - and splitting them across two commits is how a
    crash in between leaves a file marked scanned with its animals missing.

    Never raises: a pet failure must not fail the face pass. It counts towards
    PETS_MAX_CONSECUTIVE_FAILURES and, past that, turns the ride-along off for
    the rest of the run.

    The boxes come back in the PREPARED image's coordinate space, and
    detection_width/height are stored beside them for exactly that reason - the
    crop rescales from those two. documentation/fixes/ has nine files about
    getting that chain wrong.
    """
    global _pets_consecutive_failures, _pets_disabled_midrun

    if _pets_disabled_midrun:
        return 0
    try:
        resp = requests.post(
            PETS_DETECT_URL,
            files={'image': (detection_file_name, image_data, 'image/jpeg')},
            timeout=PETS_TIMEOUT)
        if resp.status_code != 200:
            raise RuntimeError(f'HTTP {resp.status_code}: {resp.text[:160]}')
        dets = (resp.json() or {}).get('detections') or []
        _pets_consecutive_failures = 0
    except Exception as e:
        _pets_consecutive_failures += 1
        print(f'  ! pet detection failed for file {file_id}: {e}')
        if _pets_consecutive_failures >= PETS_MAX_CONSECUTIVE_FAILURES:
            _pets_disabled_midrun = True
            print(f'  ! {_pets_consecutive_failures} pet detections failed in a '
                  f'row - turning the pet detector OFF for the rest of this run. '
                  f'Faces continue. Is backend-pets up, and does it have '
                  f'yolox_tiny.onnx? GET {PETS_URL}/health')
        return 0

    conn = _conn(catalog_db_path)
    try:
        cur = conn.cursor()

        # Anything NAMED survives re-detection. Not just source <> 'Automatic':
        # that was the first version of this line and it threw away the
        # household's pets.
        #
        # Naming one detection by hand sets source='Manual', but naming a GROUP
        # - which is how pets are actually named, one click for all of them -
        # writes person_id and leaves source alone. So every cluster-named
        # detection was still 'Automatic', and the next detection run deleted
        # the lot: the pet kept its `people` row and lost every photograph it
        # had been found in, silently, with the Pets page simply going empty.
        #
        # Faces avoid this by promoting a confirmed face to 'Auto-Confirmed'.
        # person_id IS NOT NULL says the same thing more directly: a detection
        # somebody has attached a name to is a judgement, whatever wrote it.
        cur.execute("SELECT x, y, w, h FROM pet_detections "
                    " WHERE file_id = ? "
                    "   AND (source <> 'Automatic' OR person_id IS NOT NULL)",
                    (file_id,))
        keep = [dict(zip(('x', 'y', 'w', 'h'), r)) for r in cur.fetchall()]

        cur.execute("DELETE FROM pet_detections WHERE file_id = ? "
                    "  AND source = 'Automatic' AND person_id IS NULL", (file_id,))

        for d in dets:
            # A box over an animal that already has a name is the SAME animal
            # found again. Inserting it would put an unnamed duplicate on top
            # of a named one - the duplicate the face path documents at its
            # own overlap check.
            if any(_boxes_overlap(d, k) for k in keep):
                continue
            cur.execute(
                'INSERT INTO pet_detections '
                ' (file_id, x, y, w, h, confidence, species, '
                '  image_width, image_height, source) '
                " VALUES (?,?,?,?,?,?,?,?,?,'Automatic')",
                (file_id, int(d['x']), int(d['y']), int(d['w']), int(d['h']),
                 float(d.get('confidence') or 0), d.get('species'),
                 detection_width, detection_height))
        cur.execute('UPDATE file_contents SET pets_scanned_at = CURRENT_TIMESTAMP '
                    ' WHERE id = ?', (file_id,))
        conn.commit()
        return len(dets)
    except sqlite3.Error as e:
        conn.rollback()
        print(f'  ! could not store pet detections for {file_id}: {e}')
        return 0


# ─── One connection per worker thread ────────────────────────────────────────
#
# Every per-file step used to open its own connection: the alternative-version
# lookup, the pet write, the detection write, the scanned mark, the new-
# detections query, and one more per face for its crop row - ~2.3 M connections
# and ~1.6 M commits on `family`, each commit fsyncing at synchronous=FULL,
# because enable_wal() set NORMAL on a connection it then closed and the
# setting is per connection. Each worker thread now keeps one, with NORMAL.
_thread = threading.local()
_conns_lock = threading.Lock()
_conns_open = []
_conns_generation = [0]


def _conn(catalog_db_path: str) -> sqlite3.Connection:
    """This thread's connection to the catalog, opened once."""
    if getattr(_thread, 'generation', None) != _conns_generation[0]:
        _thread.conns = {}
        _thread.generation = _conns_generation[0]
    conn = _thread.conns.get(catalog_db_path)
    if conn is None:
        # check_same_thread off only so close_thread_connections() can close
        # it from the main thread at the end; each is used by one thread.
        conn = sqlite3.connect(catalog_db_path, timeout=60, check_same_thread=False)
        _wal_pragmas(conn, 60000)
        try:
            conn.execute('PRAGMA synchronous=NORMAL')
        except sqlite3.Error:
            pass
        _thread.conns[catalog_db_path] = conn
        with _conns_lock:
            _conns_open.append(conn)
    return conn


def close_thread_connections():
    """Close every worker's connection; the next _conn() opens afresh."""
    with _conns_lock:
        for conn in _conns_open:
            try:
                conn.close()
            except sqlite3.Error:
                pass
        _conns_open.clear()
        _conns_generation[0] += 1


def _rows(conn, sql, params=()):
    """Dict rows from the shared connection without changing its row_factory."""
    cur = conn.cursor()
    cur.row_factory = sqlite3.Row
    return [dict(r) for r in cur.execute(sql, params)]


# ─── Subprocess progress helpers ──────────────────────────────────────────────

def _wal_pragmas(conn, timeout_ms: int = 60000):
    """
    Best-effort connection pragmas for a catalog database.

    journal_mode is deliberately NOT allowed to raise. It is PERSISTENT - written
    into the database header when the file is created and inherited by every
    later connection - so setting it again here is redundant. It is also the
    statement most likely to fail under contention, because switching journal
    mode needs an exclusive lock and a header read. Letting it throw is how a
    transient fault killed workflows that had already done their work, with
    "disk I/O error" and "file is not a database" both raised from this one line.

    busy_timeout is set first and matters more: it makes a concurrent writer
    wait rather than fail.
    """
    try:
        conn.execute('PRAGMA busy_timeout=%d' % timeout_ms)
    except sqlite3.Error:
        pass
    try:
        return conn.execute('PRAGMA journal_mode=WAL').fetchone()
    except sqlite3.Error:
        return None


def _db_update_progress(catalog_db_path: str, execution_id: int,
                        processed: int, total: int):
    """Write progress directly to workflow_executions (used when running as subprocess)."""
    # NEVER raises. This runs from the progress callback inside the main loop,
    # so an exception here kills a workflow that is otherwise working - which is
    # exactly what happened three times: runs died on the pragma above after
    # finishing their real work. Progress reporting is not worth a job.
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=60)
        try:
            _wal_pragmas(conn, 60000)
            conn.execute(
                'UPDATE workflow_executions SET processed_files=?, total_files=? WHERE id=?',
                (processed, total, execution_id)
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[progress] could not record {processed}/{total}: {exc}', file=sys.stderr)


def _db_complete(catalog_db_path: str, execution_id: int, status: str,
                 error_message: str = None, skipped_files: int = 0):
    """Mark execution finished in the DB (used when running as subprocess)."""
    # Never raises either. This is usually called FROM an exception handler, so
    # throwing here replaces the real error with a database one and loses the
    # original - and leaves the execution stuck at 'running' for good, holding a
    # concurrency slot that nothing ever frees.
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=60)
        try:
            _wal_pragmas(conn, 60000)
            conn.execute(
                '''UPDATE workflow_executions
                   SET status=?, completed_at=CURRENT_TIMESTAMP,
                       error_message=?, skipped_files=?
                   WHERE id=?''',
                (status, error_message, skipped_files, execution_id)
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[complete] could not mark execution {execution_id} as {status}: {exc}',
              file=sys.stderr)


# ─── Catalog image queries ─────────────────────────────────────────────────────

def get_outstanding_images(catalog_db_path: str, pets_enabled: bool):
    """
    (images still to visit, how many images there are in all).

    Each image carries need_faces / need_pets - which of its two watermarks is
    still empty. Asked in SQL rather than by loading every image and both
    scanned sets and filtering in Python: on a caught-up catalog that was
    ~373,000 rows and two big sets to find nothing to do, on every trigger.
    """
    _ensure_scanned_column(catalog_db_path)
    eligible = '''
        FROM file_contents fc
        WHERE fc.file_type IN ('.jpg', '.jpeg', '.png', '.heic', '.heif', '.cr3')
          AND fc.removed_at IS NULL   -- removed from the catalog by the user
          -- One scan per photograph: the RAW half of a RAW+JPG pair has no
          -- display copy, and its JPG is scanned instead.
          AND fc.id NOT IN (SELECT f.id FROM file_contents f
                              JOIN image_groups g ON g.id = f.image_group_id
                             WHERE f.id <> g.primary_file_id)'''
    need_pets = 'fc.pets_scanned_at IS NULL' if pets_enabled else '0'
    conn = sqlite3.connect(catalog_db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        total = conn.execute(f'SELECT COUNT(*) {eligible}').fetchone()[0]
        rows = conn.execute(f'''
            SELECT fc.id AS file_id, fc.file_path, fc.file_name, fc.file_type,
                   fc.face_detection_scanned_at IS NULL AS need_faces,
                   {need_pets} AS need_pets
            {eligible}
              AND (fc.face_detection_scanned_at IS NULL OR {need_pets})
            ORDER BY fc.id''').fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows], total


def _ensure_scanned_column(catalog_db_path: str):
    conn = sqlite3.connect(catalog_db_path, timeout=60)
    try:
        cols = {r[1] for r in conn.execute('PRAGMA table_info(file_contents)')}
        if 'face_detection_scanned_at' not in cols:
            conn.execute('ALTER TABLE file_contents ADD COLUMN face_detection_scanned_at TIMESTAMP')
            conn.commit()
    finally:
        conn.close()


def get_files_already_scanned(catalog_db_path: str):
    """
    Get a set of all file IDs that have already been scanned for face detection.
    This includes files where faces were found AND files where no faces were found.
    This is much more efficient than checking each file individually.
    
    Args:
        catalog_db_path: Path to catalog database
    
    Returns:
        set: Set of file IDs that have been scanned for faces
    """
    conn = sqlite3.connect(catalog_db_path, timeout=60)
    cursor = conn.cursor()
    
    # First, ensure the face_detection_scanned_at column exists
    cursor.execute("PRAGMA table_info(file_contents)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'face_detection_scanned_at' not in columns:
        # Add the column if it doesn't exist
        cursor.execute('''
            ALTER TABLE file_contents
            ADD COLUMN face_detection_scanned_at TIMESTAMP
        ''')
        conn.commit()
    
    # Get all file IDs that have been scanned (have a timestamp)
    cursor.execute('''
        SELECT id
        FROM file_contents
        WHERE face_detection_scanned_at IS NOT NULL
    ''')
    
    file_ids = {row[0] for row in cursor.fetchall()}
    conn.close()
    
    return file_ids


def get_alternative_version_for_detection(catalog_db_path: str, file_id: int):
    """
    The 1200px base derivative to run detection on - PINNED, not "the best
    available": the coordinates this produces are stored, and every later reader
    assumes they are in the base image's pixel space (file-sizes-spec.md 6.2).
    
    Args:
        catalog_db_path: Path to catalog database
        file_id: ID of the original file
    
    Returns:
        Dictionary with alternative version info or None
    """
    rows = _rows(_conn(catalog_db_path), '''
        SELECT
            alternative_file_path,
            alternative_file_name,
            characteristic,
            width,
            height
        FROM alternative_versions
        WHERE original_file_id = ?
          AND rung IN ('1200','1600','2048','2560')
        ORDER BY (rung = '1200') DESC, width ASC, id ASC
        LIMIT 1
    ''', (file_id,))
    return rows[0] if rows else None


def mark_image_as_scanned(catalog_db_path: str, file_id: int, commit: bool = True):
    """
    Mark an image as scanned for face detection by setting the face_detection_scanned_at timestamp.
    
    Args:
        catalog_db_path: Path to catalog database
        file_id: ID of the file in the database
    """
    conn = _conn(catalog_db_path)
    conn.execute('''
        UPDATE file_contents
        SET face_detection_scanned_at = ?
        WHERE id = ?
    ''', (datetime.now().isoformat(), file_id))
    if commit:
        conn.commit()


# ─── ai_face_contents table helpers ───────────────────────────────────────────

def enable_wal(catalog_db_path: str):
    """
    Put the catalog database in WAL mode.

    With several images in flight the short INSERT/DELETE bursts from different
    threads overlap. WAL lets readers proceed during a write and turns the
    rollback-journal lock contention into a brief wait that the 60 s busy
    timeout absorbs. It is a persistent property of the file, so this is a
    no-op on every run after the first.
    """
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=60)
        mode = _wal_pragmas(conn, 60000)
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.close()
        print(f"Catalog DB journal mode: {mode[0] if mode else 'unknown'}")
    except Exception as e:
        print(f"Warning: could not enable WAL on the catalog DB: {e}")


def ensure_ai_face_contents_table(catalog_db_path: str):
    """
    Create the ai_face_contents table and its indexes if they do not exist.
    Safe to call on every workflow run — uses CREATE TABLE IF NOT EXISTS.
    """
    conn = sqlite3.connect(catalog_db_path, timeout=60)
    _wal_pragmas(conn, 60000)
    cursor = conn.cursor()

    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ai_face_contents (
            id                INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path         TEXT    NOT NULL,
            file_name         TEXT    NOT NULL,
            file_size         INTEGER,
            sha256_hash       TEXT,
            year              INTEGER,
            month             INTEGER,
            day               INTEGER,
            face_detection_id INTEGER NOT NULL UNIQUE,
            source_file_id    INTEGER NOT NULL,
            created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
            FOREIGN KEY (source_file_id)    REFERENCES file_contents    (id) ON DELETE CASCADE
        )
    ''')

    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_ai_face_contents_face_detection_id
        ON ai_face_contents (face_detection_id)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_ai_face_contents_source_file_id
        ON ai_face_contents (source_file_id)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_ai_face_contents_date
        ON ai_face_contents (year, month, day)
    ''')

    conn.commit()
    conn.close()


# ─── Face-crop helpers ─────────────────────────────────────────────────────────

def _calculate_sha256(file_path: str) -> str:
    """Calculate SHA-256 hash of a file."""
    h = hashlib.sha256()
    with open(file_path, 'rb') as f:
        for chunk in iter(lambda: f.read(65536), b''):
            h.update(chunk)
    return h.hexdigest()


def _parse_date_from_master_datetime(
    master_datetime: Optional[str],
    creation_date_fallback: Optional[str] = None,
) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Parse year, month, day from master_datetime (ISO 8601: YYYY-MM-DDTHH:MM:SS...).
    Falls back to EXIF creation_date format (YYYY:MM:DD HH:MM:SS) for legacy rows
    that have not yet been backfilled.
    Returns (None, None, None) if both sources fail.
    """
    if master_datetime:
        try:
            date_part = master_datetime[:10]  # "YYYY-MM-DD"
            year, month, day = date_part.split('-')
            return int(year), int(month), int(day)
        except (ValueError, IndexError):
            pass
    if creation_date_fallback:
        try:
            date_part = creation_date_fallback.split()[0]
            year, month, day = date_part.split(':')
            return int(year), int(month), int(day)
        except (ValueError, IndexError):
            pass
    return None, None, None


def _open_and_orient(image_path: str) -> Image.Image:
    """
    Open an image, apply EXIF orientation correction, and convert to RGB.
    Handles standard JPG/PNG as well as HEIC and CR3 via optional libraries.
    """
    file_ext = os.path.splitext(image_path)[1].lower()

    if file_ext == '.cr3':
        try:
            import rawpy
            with rawpy.imread(image_path) as raw:
                rgb = raw.postprocess(use_camera_wb=True, output_bps=8)
            img = Image.fromarray(rgb)
        except ImportError:
            raise RuntimeError(f'rawpy not available, cannot process CR3: {image_path}')
    else:
        if file_ext in ('.heic', '.heif'):
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
        img = Image.open(image_path)

    oriented = ImageOps.exif_transpose(img)
    if oriented is not None:
        img = oriented

    if img.mode in ('RGBA', 'LA', 'P'):
        bg = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        bg.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        img = bg
    elif img.mode != 'RGB':
        img = img.convert('RGB')

    return img


def extract_and_save_ai_face(
    catalog_db_path: str,
    target_dir: str,
    face_detection_id: int,
    source_file_id: int,
    file_path: str,
    file_type: str,
    x: int, y: int, w: int, h: int,
    image_width: Optional[int],
    image_height: Optional[int],
    master_datetime: Optional[str] = None,
    creation_date: Optional[str] = None,
    commit: bool = True,
) -> dict:
    """
    Crop the detected face region from the source image, save it as a JPEG, and
    register it in the ai_face_contents table.

    Args:
        catalog_db_path:    Path to the catalog's workflow_results.db
        target_dir:         Catalog target directory (output root)
        face_detection_id:  PK of the face_detections row
        source_file_id:     PK of the file_contents row (original photo)
        file_path:          Absolute path to the original photo
        file_type:          File extension, e.g. '.jpg', '.heic'
        x, y, w, h:         Bounding box in detection-image space
        image_width:        Width of the image used during detection (from face_detections)
        image_height:       Height of the image used during detection
        master_datetime:    Canonical ISO 8601 date from file_contents (primary)
        creation_date:      EXIF creation_date string (fallback for legacy rows)

    Returns:
        {'skipped': False, 'id': <ai_face_contents.id>, 'path': <file_path>}
        {'skipped': True,  'reason': <str>}
    """
    # ── 1. Resolve the best image to crop from ─────────────────────────────
    non_displayable = {'.heic', '.heif', '.cr3'}
    alt = get_alternative_version_for_detection(catalog_db_path, source_file_id)

    alt_abs_path = None
    if alt:
        alt_abs_path = alt['alternative_file_path']
        # Resolve relative path to absolute
        if alt_abs_path and not os.path.isabs(alt_abs_path):
            alt_abs_path = os.path.join(target_dir, alt_abs_path)

    if alt_abs_path and os.path.exists(alt_abs_path):
        crop_image_path = alt_abs_path
    elif file_type.lower() in non_displayable:
        return {'skipped': True, 'reason': 'No JPG alternative for non-displayable format'}
    elif os.path.exists(file_path):
        crop_image_path = file_path
    else:
        return {'skipped': True, 'reason': f'Source file not found: {file_path}'}

    # ── 2. Open + orientation-correct - or take the decode detection made ──
    decoded = getattr(_thread, 'decoded', None)
    if decoded and decoded[0] == os.path.abspath(crop_image_path):
        img = decoded[1]
    else:
        try:
            img = _open_and_orient(crop_image_path)
        except Exception as e:
            return {'skipped': True, 'reason': f'Could not open image: {e}'}

    img_w, img_h = img.size

    # ── 3. Scale bounding box from detection space to crop-image space ──────
    fx, fy, fw, fh = x, y, w, h
    if image_width and image_height and (image_width != img_w or image_height != img_h):
        scale_x = img_w / image_width
        scale_y = img_h / image_height
        fx = int(fx * scale_x)
        fy = int(fy * scale_y)
        fw = int(fw * scale_x)
        fh = int(fh * scale_y)

    # ── 4. Add 20 % padding and clamp to image bounds ──────────────────────
    pad_x = int(fw * 0.20)
    pad_y = int(fh * 0.20)
    left   = max(0,     fx - pad_x)
    top    = max(0,     fy - pad_y)
    right  = min(img_w, fx + fw + pad_x)
    bottom = min(img_h, fy + fh + pad_y)

    if right <= left or bottom <= top:
        return {'skipped': True, 'reason': f'Invalid crop box after padding: ({left},{top},{right},{bottom})'}

    cropped = img.crop((left, top, right, bottom))

    # ── 5. Ensure RGB ──────────────────────────────────────────────────────
    if cropped.mode != 'RGB':
        cropped = cropped.convert('RGB')

    # ── 6. Save as JPEG ────────────────────────────────────────────────────
    out_dir = os.path.join(target_dir, 'cache', 'faces-ai')
    os.makedirs(out_dir, exist_ok=True)

    face_filename = f'{face_detection_id}.jpg'
    face_path = os.path.join(out_dir, face_filename)
    cropped.save(face_path, 'JPEG', quality=92, optimize=True)

    # ── 7. SHA-256 ─────────────────────────────────────────────────────────
    sha256 = _calculate_sha256(face_path)
    file_size = os.path.getsize(face_path)

    # ── 8. Parse date from master_datetime (with EXIF fallback) ───────────
    year, month, day = _parse_date_from_master_datetime(master_datetime, creation_date)

    # ── 9. Insert into ai_face_contents ────────────────────────────────────
    # Store path relative to target_dir so the catalog is portable
    rel_face_path = os.path.relpath(face_path, target_dir)
    conn = _conn(catalog_db_path)
    cursor = conn.cursor()
    cursor.execute('''
        INSERT INTO ai_face_contents
            (file_path, file_name, file_size, sha256_hash,
             year, month, day, face_detection_id, source_file_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (rel_face_path, face_filename, file_size, sha256,
          year, month, day, face_detection_id, source_file_id))
    row_id = cursor.lastrowid
    if commit:
        conn.commit()

    return {'skipped': False, 'id': row_id, 'path': face_path}


def _get_new_detections_for_file(catalog_db_path: str, file_id: int) -> list:
    """
    Query all Automatic face detections for file_id that do not yet have an
    ai_face_contents entry.  Returns list of dicts with all fields needed for
    extract_and_save_ai_face().
    """
    return _rows(_conn(catalog_db_path), '''
        SELECT
            fd.id         AS face_detection_id,
            fd.x, fd.y, fd.w, fd.h,
            fd.image_width,
            fd.image_height,
            fc.id         AS source_file_id,
            fc.file_path,
            fc.file_type,
            fc.master_datetime,
            ed.creation_date
        FROM face_detections fd
        INNER JOIN file_contents fc ON fd.file_id = fc.id
        LEFT  JOIN exif_data     ed ON fc.exif_data_id = ed.id
        LEFT  JOIN ai_face_contents afc ON afc.face_detection_id = fd.id
        WHERE fd.file_id = ?
          AND fd.source  = 'Automatic'
          AND afc.id IS NULL
    ''', (file_id,))


# ─── Face detection ────────────────────────────────────────────────────────────

def detect_faces_for_image(catalog_db_path: str, file_id: int, file_path: str,
                           file_name: str, file_type: str, target_dir: str = None,
                           do_faces: bool = True, do_pets: bool = False):
    """
    Detect faces in a single image using the deepface backend.
    
    Args:
        catalog_db_path: Path to catalog database
        file_id: ID of the file in the database
        file_path: Absolute path to the image file
        file_name: Name of the image file
        file_type: File extension/type
        target_dir: Catalog target directory (used to resolve relative alt paths)
        do_faces: run the face detector. False when this file was already
            face-scanned and is only here for the pet pass.
        do_pets: run the pet detector on the SAME prepared jpeg. The decode is
            the expensive part and is shared; see detect_pets_in_prepared.
    
    Returns:
        Dictionary with detection results: {'success': bool, 'faces_count': int,
        'pets_count': int, 'error': str}
    """
    # Always use the alternative JPG version for face detection (standardized at max 1200px)
    alt_version = get_alternative_version_for_detection(catalog_db_path, file_id)
    
    # Use alternative version if available, otherwise fall back to original
    if alt_version:
        alt_path = alt_version['alternative_file_path']
        # Resolve relative path to absolute
        if alt_path and not os.path.isabs(alt_path) and target_dir:
            alt_path = os.path.join(target_dir, alt_path)
        if alt_path and os.path.exists(alt_path):
            detection_file_path = alt_path
            detection_file_name = alt_version['alternative_file_name']
            print(f"  Using alternative version for detection: {detection_file_name}")
        else:
            alt_version = None  # alt not usable, fall through
    if not alt_version:
        # For non-displayable types (HEIC, CR3), we require an alternative version
        non_displayable_types = {'.heic', '.heif', '.cr3'}
        if file_type.lower() in non_displayable_types:
            return {
                'success': False,
                'faces_count': 0,
                'error': 'No JPG alternative found for non-displayable format',
                # Not a detector failure: there is nothing to send it yet. The
                # caller skips it without counting towards the abort.
                'no_display_copy': True,
            }
        detection_file_path = file_path
        detection_file_name = file_name
        print(f"  Using original file for detection: {detection_file_name}")
        from lib.orientation import user_orientation as _user_orientation
        edit = _user_orientation(_conn(catalog_db_path), file_id)
    else:
        edit = 1
    
    if not os.path.exists(detection_file_path):
        return {
            'success': False,
            'faces_count': 0,
            'error': 'File does not exist on disk'
        }
    
    try:
        # Orientation-correct and downscale to detection size. See
        # prepare_detection_jpeg() for why this is not the full-resolution
        # image the detector used to receive.
        #
        # Caught separately, because a file THIS cannot read is a different
        # kind of failure from a detector that is down, and conflating them
        # stopped a run dead. On 2026-10-09 ten unreadable BMPs in a row
        # aborted the pass with "the detector is almost certainly down or
        # crash-looping" - stated with confidence, about a service that was
        # answering perfectly. The remaining 3,715 files could not be
        # completed at all, because the abort fired in the same place every
        # time.
        try:
            image_data, detection_width, detection_height = (
                prepare_detection_jpeg(detection_file_path, edit) if edit != 1
                else prepare_detection_jpeg(detection_file_path))
        except Exception as decode_err:
            return {
                'success': False,
                'faces_count': 0,
                'pets_count': 0,
                'unreadable': True,
                'error': f'could not read the image: {decode_err}',
            }

        # The pet detector gets these exact bytes. This is the whole reason it
        # lives in this workflow: the decode above is ~98 % of the per-image
        # cost and it has just been paid.
        pets_count = 0
        if do_pets:
            # It moves pets_scanned_at itself, in the same transaction as the
            # boxes, so there is no window where a file is marked examined
            # with its animals missing.
            pets_count = detect_pets_in_prepared(
                catalog_db_path, file_id, image_data,
                detection_width, detection_height, detection_file_name)

        if not do_faces:
            # Already face-scanned; it was only here for the animals.
            return {'success': True, 'faces_count': 0, 'pets_count': pets_count,
                    'faces_skipped': True}

        # Call deepface backend to detect faces.
        # DEEPFACE_URL can be overridden via environment variable for container deployments
        # where the deepface service runs on a different host (e.g. a separate container).
        deepface_url = os.environ.get('DEEPFACE_URL', 'http://deepface.airlock.lan') + '/api/upload'
        files = {'file': (detection_file_name, image_data, 'image/jpeg')}
        response = requests.post(deepface_url, files=files, timeout=60)
        
        if response.status_code != 200:
            return {
                'success': False,
                'faces_count': 0,
                'pets_count': pets_count,
                'error': f'Deepface backend error: {response.text}'
            }
        
        result = response.json()
        
        if not result.get('success'):
            return {
                'success': False,
                'faces_count': 0,
                'pets_count': pets_count,
                'error': 'Face detection failed'
            }
        
        # Get existing manual faces to preserve them
        conn = _conn(catalog_db_path)
        cursor = conn.cursor()
        cursor.row_factory = sqlite3.Row
        # Preserve both human-confirmed and auto-confirmed faces. An
        # 'Auto-Confirmed' face is not 'Automatic', so it already survives the
        # delete below - but it must also be included here, or the new detection
        # gets no overlap check against it and inserts a duplicate box over a face
        # that already has a person assigned.
        cursor.execute('''
            SELECT x, y, w, h
            FROM face_detections
            WHERE file_id = ? AND source IN ('Manual', 'Auto-Confirmed')
        ''', (file_id,))
        existing_manual_faces = [dict(row) for row in cursor.fetchall()]
        
        # Delete only automatic face detections for this file
        # In the same transaction as the inserts below: committed apart, a
        # crash between them left the file with no automatic faces at all.
        cursor.execute('''
            DELETE FROM face_detections
            WHERE file_id = ? AND source = 'Automatic'
        ''', (file_id,))
        
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
        faces_added = 0
        for face in result.get('faces', []):
            # Check if this new face overlaps with any existing manual face
            overlaps_with_manual = any(
                faces_overlap(face, manual_face)
                for manual_face in existing_manual_faces
            )
            
            if not overlaps_with_manual:
                cursor.execute('''
                    INSERT INTO face_detections
                    (file_id, x, y, w, h, confidence, source, image_width, image_height, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, 'Automatic', ?, ?, ?)
                ''', (
                    file_id,
                    face['x'],
                    face['y'],
                    face['w'],
                    face['h'],
                    face.get('confidence', 0.0),
                    detection_width,
                    detection_height,
                    datetime.now().isoformat()
                ))
                faces_added += 1
        
        conn.commit()
        
        return {
            'success': True,
            'faces_count': faces_added,
            'pets_count': pets_count,
            'error': None
        }
        
    except Exception as e:
        try:
            _conn(catalog_db_path).rollback()
        except sqlite3.Error:
            pass
        return {
            'success': False,
            'faces_count': 0,
            # pets_count may not be bound yet if the failure was in
            # prepare_detection_jpeg, which is before the pet call.
            'pets_count': locals().get('pets_count', 0),
            'error': str(e)
        }


# ─── Main execute ──────────────────────────────────────────────────────────────

def execute(source_dir, target_dir, files, progress_callback, catalog_id=None, catalog=None, execution_id=None):
    """
    Auto-detect faces in all images in the catalog, then extract and persist a
    cropped JPEG for each detected face in ai_face_contents.

    This workflow:
    1. Reads all image files from the catalog database
    2. For each image, calls the face detection API (deepface backend)
    3. Stores detected faces in the face_detections table with source='Automatic'
    4. Skips faces that overlap with existing manual face detections
    5. Uses alternative JPG versions when available for consistent detection
    6. [NEW] Crops and saves each detected face to <target_dir>/cache/faces-ai/
    7. [NEW] Records the crop in ai_face_contents (linked to face_detections via FK)

    Args:
        source_dir: Source directory path (not used, workflow operates on catalog data)
        target_dir: Target directory path (catalog target directory)
        files: List of file paths to process (not used, workflow processes all catalog images)
        progress_callback: Callback function for progress updates
        catalog_id: ID of the catalog (deprecated, use catalog parameter)
        catalog: Catalog instance to use for file operations (recommended)
        execution_id: ID of the workflow execution (for tracking)

    Returns:
        dict: Statistics about the execution including images_processed, faces_detected,
              face_crops_saved, face_crops_failed, images_skipped, images_failed
    """
    # If catalog is not provided, create one from catalog_id
    if catalog is None and catalog_id is not None:
        catalog = Catalog.get_by_id(catalog_id)
        if catalog is None:
            raise ValueError(f"Catalog with ID {catalog_id} not found")
    
    # If still no catalog, we need to fail
    if catalog is None:
        raise ValueError("Either catalog or catalog_id must be provided")
    
    catalog_db_path = os.path.join(target_dir, 'workflow_results.db')

    # Ensure the ai_face_contents table exists (safe to call on every run)
    print("Ensuring ai_face_contents table exists...")
    ensure_ai_face_contents_table(catalog_db_path)

    # Several images are in flight at once; WAL keeps their short writes from
    # blocking each other. Persistent, so this is a no-op after the first run.
    enable_wal(catalog_db_path)


    # ── Preflight: is the detector actually answering? ───────────────────────
    # Checked once, up front. Without this the run starts optimistically and only
    # reveals a dead backend image by image, thousands of times over.
    deepface_base = os.environ.get('DEEPFACE_URL', 'http://deepface.airlock.lan')
    try:
        _health = requests.get(f'{deepface_base}/api/health', timeout=10)
        if _health.status_code != 200:
            raise ValueError(
                f'DeepFace backend at {deepface_base} answered '
                f'{_health.status_code} on /api/health. Start it before running '
                f'this workflow.')
        print(f"DeepFace backend healthy at {deepface_base}")
    except requests.exceptions.RequestException as exc:
        raise ValueError(
            f'Cannot reach the DeepFace backend at {deepface_base}: {exc}. '
            f'Start it (systems/backend-deepface/start-host.sh) and re-run. '
            f'Services are addressed by name through the gateway; if this is '
            f'running in a container, the pod needs deepface.airlock.lan in its '
            f'hostAliases (see systems/gateway/).'
        ) from exc

    # Does this catalog look for animals? Read ONCE per run, not per file.
    #
    # With pets off nothing below costs anything: no schema ensured, no table
    # touched, no HTTP session opened, and this workflow behaves and runs
    # exactly as it did before pets existed. "Off" has to cost zero rather
    # than nearly zero, and there is a test that says so.
    global PETS_ENABLED
    try:
        import models as _models
        PETS_ENABLED = _models.get_catalog_property(
            catalog_db_path, 'pets_enabled') == '1'
    except Exception as e:
        PETS_ENABLED = False
        print(f'  (could not read pets_enabled, treating pets as off: {e})')
    if PETS_ENABLED:
        # Only now. The schema is five tables, two columns and a view, and a
        # catalog that does not want pets should not grow them.
        from pet_schema import ensure_pet_schema
        ensure_pet_schema(catalog_db_path)

    # Only the images with something outstanding - see get_outstanding_images.
    # A file is worth visiting if EITHER half is outstanding: without the pets
    # clause the feature would find nothing on any existing catalog, where
    # every file is already face-scanned.
    print("Finding images not yet scanned...")
    images_to_process, total_images = get_outstanding_images(catalog_db_path, PETS_ENABLED)
    skipped = total_images - len(images_to_process)
    print(f"Found {total_images} images; {skipped} already scanned, "
          f"{len(images_to_process)} to process")
    if PETS_ENABLED:
        pets_outstanding = sum(1 for img in images_to_process if img['need_pets'])
        print(f'Pets: ON. {pets_outstanding} file(s) not yet examined for animals')

    # Early exit if nothing to process
    if len(images_to_process) == 0:
        print("All images have already been scanned for faces!")
        return {
            'images_processed': 0,
            'faces_detected': 0,
            'pets_detected': 0,
            'face_crops_saved': 0,
            'face_crops_failed': 0,
            'images_skipped': skipped,
            'images_failed': 0,
        }
    
    processed = 0
    total_faces_detected = 0
    total_pets_detected = 0
    failed = 0
    no_display_copy = 0
    unreadable = 0
    consecutive_failures = 0
    face_crops_saved = 0
    face_crops_failed = 0
    
    # Send initial progress with total count (only for images to process)
    if progress_callback:
        progress_callback(0, len(images_to_process))
    
    total_to_process = len(images_to_process)
    abort_event = threading.Event()
    abort_reason = []
    # Separate from abort_reason: both set abort_event, but a stop is not a
    # failure and must not raise. process_one already returns early when
    # abort_event is set, so no new per-worker check is needed.
    stopped = threading.Event()

    def process_one(idx, image_data):
        """
        Detect faces in one image and extract its crops.

        Runs on a worker thread, so it touches no shared counters directly and
        returns its log lines instead of printing them - otherwise output from
        concurrent images interleaves into nonsense.
        """
        log = []
        outcome = {
            'faces': 0,
            'pets': 0,
            'ok': False,
            'error': None,
            'crops_saved': 0,
            'crops_failed': 0,
            'skipped_by_abort': False,
            'unreadable': False,
        }

        if abort_event.is_set():
            outcome['skipped_by_abort'] = True
            return log, outcome
        _thread.decoded = None

        file_id = image_data['file_id']
        file_path = image_data['file_path']
        if file_path and not os.path.isabs(file_path):
            file_path = os.path.join(target_dir, file_path)
        file_name = image_data['file_name']
        file_type = image_data['file_type']

        log.append(f"[{idx}/{total_to_process}] {file_name}")

        try:
            # -- Phase 1: detect faces ------------------------------------
            result = detect_faces_for_image(
                catalog_db_path,
                file_id,
                file_path,
                file_name,
                file_type,
                target_dir=target_dir,
                # Two independent questions, because the two watermarks are
                # independent: a file face-scanned before pets were switched on
                # needs the animals half only.
                do_faces=bool(image_data['need_faces']),
                do_pets=PETS_ENABLED and bool(image_data['need_pets']),
            )

            if not result['success'] and result.get('no_display_copy'):
                # Left unscanned, so it is picked up once a display copy exists.
                outcome['no_display_copy'] = True
                log.append('  - Skipped: no display copy to scan yet')
                return log, outcome

            if not result['success'] and result.get('unreadable'):
                # Says nothing about the detector. Left unscanned rather than
                # marked done, because the file may be replaced - 18 files
                # retried each run is nothing, and marking them would hide
                # them for ever.
                outcome['unreadable'] = True
                log.append(f"  - Skipped: {result['error']}")
                return log, outcome

            if not result['success']:
                outcome['error'] = result['error']
                log.append(f"  x Failed: {result['error']}")
                # Do NOT mark as scanned on failure - a connection error or
                # service outage should not permanently skip the image.
                return log, outcome

            outcome['ok'] = True
            outcome['pets'] = result.get('pets_count', 0)

            if result.get('faces_skipped'):
                # Already face-scanned on an earlier run; this visit was for the
                # animals half only, and the face watermark must not move again.
                log.append(f"  + Detected {outcome['pets']} pet(s)"
                           f" (faces already scanned)")
            else:
                outcome['faces'] = result['faces_count']
                log.append(f"  + Detected {result['faces_count']} face(s)"
                           + (f", {outcome['pets']} pet(s)" if PETS_ENABLED else ''))

            # -- Phase 2: extract and save face crops ---------------------
            # Then the scanned mark, in ONE transaction with the crop rows.
            # It used to be marked before the crops were made, so a crash
            # between left the file done for ever with its crops missing.
            if result['faces_count'] > 0:
                new_detections = _get_new_detections_for_file(catalog_db_path, file_id)
                for det in new_detections:
                    det_id = det['face_detection_id']
                    try:
                        det_file_path = det['file_path']
                        if det_file_path and not os.path.isabs(det_file_path):
                            det_file_path = os.path.join(target_dir, det_file_path)
                        crop_result = extract_and_save_ai_face(
                            catalog_db_path=catalog_db_path,
                            target_dir=target_dir,
                            face_detection_id=det_id,
                            source_file_id=det['source_file_id'],
                            file_path=det_file_path,
                            file_type=det['file_type'],
                            x=det['x'], y=det['y'],
                            w=det['w'], h=det['h'],
                            image_width=det['image_width'],
                            image_height=det['image_height'],
                            master_datetime=det.get('master_datetime'),
                            creation_date=det.get('creation_date'),
                            commit=False,
                        )
                        if crop_result.get('skipped'):
                            log.append(f"    ! Crop skipped for detection {det_id}: {crop_result['reason']}")
                            outcome['crops_failed'] += 1
                        else:
                            log.append(f"    + Crop saved: {crop_result['path']}")
                            outcome['crops_saved'] += 1
                    except Exception as crop_err:
                        log.append(f"    x Crop error for detection {det_id}: {crop_err}")
                        outcome['crops_failed'] += 1
            if not result.get('faces_skipped'):
                mark_image_as_scanned(catalog_db_path, file_id, commit=False)
            _conn(catalog_db_path).commit()

        except Exception as ex:
            try:
                _conn(catalog_db_path).rollback()
            except sqlite3.Error:
                pass
            outcome['ok'] = False
            outcome['error'] = str(ex)
            log.append(f"  ERROR: Failed to process image {file_id}: {ex}")
            log.append(traceback.format_exc())
        finally:
            decoded = getattr(_thread, 'decoded', None)
            if decoded:
                decoded[1].close()
            _thread.decoded = None

        return log, outcome

    workers = max(1, min(DETECTION_CONCURRENCY, total_to_process))
    print(f"Detecting with {workers} concurrent worker(s), "
          f"detection size {DETECTION_MAX_DIM or 'full resolution'} px.")

    def _completed(pool):
        """
        Results as they finish, with only a few images in flight per worker.
        Every image used to be submitted at once, and every future - with its
        log lines - held until the run ended: ~373,000 of them on `family`.
        """
        pending = set()
        queue = iter(enumerate(images_to_process, 1))
        window = workers * 4
        while True:
            while len(pending) < window and not abort_event.is_set():
                nxt = next(queue, None)
                if nxt is None:
                    break
                pending.add(pool.submit(process_one, *nxt))
            if not pending:
                return
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for f in done:
                yield f

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for future in _completed(pool):
            try:
                log, outcome = future.result()
            except Exception as ex:
                # process_one catches its own errors; this is a pool-level fault.
                failed += 1
                processed += 1
                print(f"  ERROR: worker raised: {ex}")
                continue

            for line in log:
                print(line)

            if outcome['skipped_by_abort']:
                continue

            processed += 1
            total_faces_detected += outcome['faces']
            total_pets_detected += outcome['pets']
            face_crops_saved += outcome['crops_saved']
            face_crops_failed += outcome['crops_failed']

            if outcome['ok']:
                consecutive_failures = 0
            elif outcome.get('no_display_copy'):
                # Says nothing about the detector either way: neither resets
                # nor advances the run of failures that triggers the abort.
                no_display_copy += 1
            elif outcome.get('unreadable'):
                # Nor does this. A corrupt file is a fact about the file.
                unreadable += 1
            else:
                failed += 1
                consecutive_failures += 1
                # "Consecutive" is measured in completion order. With several
                # images in flight that is approximate, but the signal it exists
                # for - the detector being down, so every image fails - is not
                # subtle enough for the approximation to matter.
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES and not abort_event.is_set():
                    abort_event.set()
                    abort_reason.append(outcome['error'])

            if progress_callback and (processed % 10 == 0 or processed == total_to_process):
                progress_callback(processed, total_to_process)

            # Checked here rather than inside the workers: one thread asking once
            # per completed image, instead of four asking in parallel for the
            # same answer. Images already in flight finish and are recorded -
            # face_detection_scanned_at is written per image, so they are not
            # redone on the next run.
            if (execution_id and not abort_event.is_set()
                    and stop_requested(catalog_db_path, execution_id)):
                print(f'\nStop requested - finishing the images in flight '
                      f'({processed} of {total_to_process} done).')
                stopped.set()
                abort_event.set()

    close_thread_connections()

    if progress_callback:
        progress_callback(processed, total_to_process)

    if stopped.is_set():
        # Not an exception: a stop is not a failure. The row is written here
        # because only this function knows how far it got, and the caller would
        # otherwise record 'completed'.
        if execution_id:
            record_stopped(catalog_db_path, execution_id,
                           processed_files=processed, skipped_files=skipped,
                           message=stop_message(processed, total_to_process))
        return {
            'images_processed': processed,
            'faces_detected': total_faces_detected,
            'pets_detected': total_pets_detected,
            'face_crops_saved': face_crops_saved,
            'face_crops_failed': face_crops_failed,
            'images_skipped': skipped,
            'images_failed': failed,
            'stopped': True,
        }

    if abort_event.is_set():
        raise RuntimeError(
            f'{MAX_CONSECUTIVE_FAILURES} images failed in a row - aborted. '
            f'Last error: {abort_reason[0] if abort_reason else "unknown"}. '
            f'The detector is almost certainly down or crash-looping. Images '
            f'that succeeded before it are done and kept; the ones that failed '
            f'were not marked scanned, so re-running after fixing it retries '
            f'exactly those.')

    print(f"\n{'='*60}")
    print(f"Face detection complete!")
    print(f"  Total images checked:              {processed}")
    print(f"  Images skipped (already scanned):  {skipped}")
    print(f"  Images processed:                  {processed - failed - no_display_copy - unreadable}")
    print(f"  Images with no display copy yet:   {no_display_copy}")
    if unreadable:
        print(f"  Images that could not be read:     {unreadable}  "
              f"(corrupt or an unsupported format; skipped, not a detector fault)")
    print(f"  Total faces detected:              {total_faces_detected}")
    if PETS_ENABLED:
        print(f"  Total pets detected:               {total_pets_detected}")
        if _pets_disabled_midrun:
            print(f"  NOTE: the pet detector was turned off part-way through "
                  f"this run after repeated failures. Faces are complete; the "
                  f"animals half is not, and re-running picks up where it "
                  f"stopped - nothing was marked pet-scanned.")
    print(f"  Face crops saved:                  {face_crops_saved}")
    print(f"  Face crops failed/skipped:         {face_crops_failed}")
    print(f"  Images failed:                     {failed}")
    if failed and not total_faces_detected:
        print("  NOTE: every image failed, so none was marked as scanned - "
              "check the DeepFace backend.")
    print(f"{'='*60}")
    
    return {
        'images_processed': processed - failed - no_display_copy - unreadable,
        'images_without_display_copy': no_display_copy,
        'images_unreadable': unreadable,
        'faces_detected': total_faces_detected,
        'pets_detected': total_pets_detected,
        'face_crops_saved': face_crops_saved,
        'face_crops_failed': face_crops_failed,
        'images_skipped': skipped,
        'images_failed': failed,
    }


# ─── Standalone subprocess entry point ────────────────────────────────────────

if __name__ == '__main__':
    """
    When launched as a subprocess by the backend, arguments are:
        --execution-id  <int>   workflow_executions.id
        --target-dir    <path>  catalog target directory (contains workflow_results.db)
        --catalog-id    <int>   catalog id
    """
    import argparse

    # ── Make a hang diagnosable ──────────────────────────────────────────
    #
    # On 2026-10-09 this workflow wedged eleven hours into a run: four threads
    # parked, no CPU, no database write for seventy minutes, holding a lock
    # that starved the backend until it looked to the browser as though the
    # server was down. Why it wedged could not be determined, because there is
    # no way to get a Python stack out of a running subprocess after the fact -
    # gdb cannot read the interpreter's symbols across the container boundary
    # and py-spy is not installed.
    #
    # These three lines fix that for next time. SIGUSR1 dumps every thread's
    # stack to stderr, which conmon already captures into `podman logs`, and
    # it does not disturb the run:
    #
    #     kill -USR1 <pid>   then   podman logs workflow-backend
    #
    # Registered here rather than in execute() so it covers the whole
    # subprocess, including whatever happens before and after the main loop.
    # documentation/fixes/2026-10-09-backend-starved-by-a-wedged-workflow.md
    try:
        import faulthandler
        import signal
        faulthandler.enable()                         # and on a hard crash
        if hasattr(faulthandler, 'register'):         # not on every platform
            faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    except Exception as _fh_exc:
        # Never fatal. A workflow that cannot be debugged is better than one
        # that will not start.
        print(f'  (no faulthandler: {_fh_exc})')

    parser = argparse.ArgumentParser(description='Auto detect faces workflow (subprocess mode)')
    parser.add_argument('--execution-id', type=int, required=True)
    parser.add_argument('--target-dir', type=str, required=True)
    parser.add_argument('--catalog-id', type=int, required=True)
    args = parser.parse_args()

    catalog_db_path = os.path.join(args.target_dir, 'workflow_results.db')

    catalog = Catalog.get_by_id(args.catalog_id)
    if catalog is None:
        print(f'ERROR: Catalog {args.catalog_id} not found', file=sys.stderr)
        _db_complete(catalog_db_path, args.execution_id, 'failed',
                     f'Catalog {args.catalog_id} not found')
        sys.exit(1)

    def subprocess_progress(processed, total=None):
        _db_update_progress(catalog_db_path, args.execution_id,
                            processed, total if total is not None else processed)

    try:
        result = execute(
            source_dir=None,
            target_dir=args.target_dir,
            files=[],
            progress_callback=subprocess_progress,
            catalog_id=args.catalog_id,
            catalog=catalog,
            execution_id=args.execution_id,
        )
        if isinstance(result, dict) and result.get('stopped'):
            # execute() already wrote 'stopped'. Recording 'completed' here would
            # fire the whole recognition chain over a partial scan.
            print(f'Subprocess execution {args.execution_id} stopped by request.')
        else:
            _db_complete(catalog_db_path, args.execution_id, 'completed',
                         skipped_files=result.get('images_skipped', 0) if isinstance(result, dict) else 0)
            print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)
