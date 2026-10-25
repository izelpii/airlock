## REFINE FACE RECONGNITIONS


import io
import json
import math
import os
import sqlite3
import sys
from typing import Dict, List, Optional

import requests
from PIL import Image, ImageOps

# Add parent directory to path to import catalog module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Cooperative stop - see workflows-review-spec.md section 3.4. This loop is
# incremental (an already-done item is skipped), so leaving between two
# items needs no checkpoint.
from lib.stopping import stop_requested, record_stopped, stop_message
from catalog import Catalog
import embedding_models


RULE_SAME_IMAGE_EXCLUSION = 'same_image_exclusion'
RULE_SAME_EVENT_EXPANSION = 'same_event_confirmed_face_expansion'
# DeepFace's tuned verification threshold for the active model + cosine.
# Used only as a fallback when the backend response omits one. Was 0.4, an older
# DeepFace default that no longer matches the installed version.
# Per-model, and reassigned in execute() for the catalog's active model: one
# number here is silently wrong for whichever model it was not chosen for.
DEFAULT_THRESHOLD = embedding_models.verification_distance(embedding_models.LEGACY_MODEL)
DEFAULT_MODEL = embedding_models.LEGACY_MODEL
DEFAULT_MODEL_ID = embedding_models.model_id(embedding_models.LEGACY_MODEL)
DEFAULT_DISTANCE_METRIC = 'cosine'
_DEEPFACE_BASE     = os.environ.get('DEEPFACE_URL', 'http://deepface.airlock.lan')
DEEPFACE_EMBED_URL = _DEEPFACE_BASE + '/api/embed'
DEEPFACE_URL       = _DEEPFACE_BASE + '/api/compare-multiple'
BATCH_SIZE = 10  # references per /api/compare-multiple call


def _wal_pragmas(conn, timeout_ms: int = 30000):
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
    """Write progress directly to workflow_executions."""
    # NEVER raises. This runs from the progress callback inside the main loop,
    # so an exception here kills a workflow that is otherwise working - which is
    # exactly what happened three times: runs died on the pragma above after
    # finishing their real work. Progress reporting is not worth a job.
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
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
    """Mark execution finished in the DB."""
    # Never raises either. This is usually called FROM an exception handler, so
    # throwing here replaces the real error with a database one and loses the
    # original - and leaves the execution stuck at 'running' for good, holding a
    # concurrency slot that nothing ever frees.
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
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


def ensure_tables(catalog_db_path: str):
    """Create workflow tables if they do not exist."""
    conn = sqlite3.connect(catalog_db_path)
    cursor = conn.cursor()
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS face_similarity_scores_audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER,
            rule_name TEXT NOT NULL,
            action_type TEXT NOT NULL,
            source_face_detection_id INTEGER NOT NULL,
            face_similarity_score_id INTEGER,
            candidate_person_id INTEGER,
            reference_face_detection_id INTEGER,
            source_file_id INTEGER,
            source_event_id INTEGER,
            previous_distance REAL,
            new_distance REAL,
            reason TEXT NOT NULL,
            metadata TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (execution_id) REFERENCES workflow_executions (id) ON DELETE SET NULL,
            FOREIGN KEY (source_face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
            FOREIGN KEY (face_similarity_score_id) REFERENCES face_similarity_scores (id) ON DELETE SET NULL,
            FOREIGN KEY (candidate_person_id) REFERENCES people (id) ON DELETE SET NULL,
            FOREIGN KEY (reference_face_detection_id) REFERENCES face_detections (id) ON DELETE SET NULL,
            FOREIGN KEY (source_file_id) REFERENCES file_contents (id) ON DELETE SET NULL,
            FOREIGN KEY (source_event_id) REFERENCES events (id) ON DELETE SET NULL
        )
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_fssa_source_face
        ON face_similarity_scores_audit(source_face_detection_id)
    ''')
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_fssa_execution
        ON face_similarity_scores_audit(execution_id)
    ''')
    conn.commit()
    conn.close()


def get_eligible_face_detections(catalog_db_path: str) -> List[Dict]:
    """
    Return all Automatic face detections with no confirmed person, including
    full bounding box and file path details needed for image cropping.
    """
    conn = sqlite3.connect(catalog_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT
            fd.id,
            fd.file_id,
            fd.person_id,
            fd.source,
            fd.x,
            fd.y,
            fd.w,
            fd.h,
            fd.image_width AS detection_image_width,
            fd.image_height AS detection_image_height,
            fc.file_path,
            fc.file_type,
            ed.width AS original_width,
            ed.height AS original_height
        FROM face_detections fd
        INNER JOIN file_contents fc ON fc.id = fd.file_id
        LEFT JOIN exif_data ed ON fc.exif_data_id = ed.id
        WHERE fd.source = 'Automatic' AND fd.person_id IS NULL
          -- Faces on a removed file are kept, not tagged.
          AND fc.removed_at IS NULL
        ORDER BY fd.id
    ''')
    rows = cursor.fetchall()
    conn.close()
    return [dict(row) for row in rows]


# Basename of this script — used to identify prior runs of THIS workflow.
# Keyed off the script path rather than workflow_executions.workflow_name because
# that name is whatever the user typed when creating the workflow in the UI (it is
# 'Additional ML Face Recognition' in the current catalogs, not the literal string
# this function used to match on). Matching a user-editable display name silently
# disabled incremental mode: the lookup never hit, so every run fell back to full
# mode and re-compared references it had already processed.
_SCRIPT_BASENAME = os.path.basename(os.path.abspath(__file__))


def get_last_run_completed_at(catalog_db_path: str) -> Optional[str]:
    """
    Return the completed_at timestamp of this workflow's most recent successful
    execution, or None if no prior run exists.

    Resolution order:
      1. Join workflow_executions -> workflows and match on python_script, which is
         set by the system and cannot be renamed by the user.
      2. Fall back to matching workflow_name against the known display names, for
         executions recorded before workflow_id was populated.
    """
    conn = sqlite3.connect(catalog_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Primary: match on the script path via the workflows table
    try:
        cursor.execute('''
            SELECT we.completed_at
            FROM workflow_executions we
            INNER JOIN workflows w ON we.workflow_id = w.id
            WHERE w.python_script LIKE ?
              AND we.status = 'completed'
              AND we.completed_at IS NOT NULL
            ORDER BY we.completed_at DESC
            LIMIT 1
        ''', (f'%{_SCRIPT_BASENAME}',))
        row = cursor.fetchone()
        if row and row['completed_at']:
            conn.close()
            return row['completed_at']
    except sqlite3.Error:
        pass  # older catalog without workflow_id — fall through

    # Fallback: legacy executions that predate workflow_id, matched by display name
    cursor.execute('''
        SELECT completed_at
        FROM workflow_executions
        WHERE workflow_name IN ('Refine Face Recognitions',
                                'Additional ML Face Recognition')
          AND status = 'completed'
          AND completed_at IS NOT NULL
        ORDER BY completed_at DESC
        LIMIT 1
    ''')
    row = cursor.fetchone()
    conn.close()
    return row['completed_at'] if row else None


def get_face_context(cursor: sqlite3.Cursor, face_detection_id: int,
                     face_row: Dict,
                     new_refs_since: Optional[str] = None) -> Optional[Dict]:
    """
    Load all relational context needed to process a single face detection.
    face_row must already contain the file/bbox info from get_eligible_face_detections.

    new_refs_since: ISO timestamp string. When provided, a second list
    ``new_event_confirmed_faces`` is populated with only those manually confirmed
    faces in the same event whose face_detection created_at is strictly after
    that timestamp. These are the references that must be compared even when a
    similarity score for the same person already exists.
    """
    face = dict(face_row)

    # Existing candidate scores
    cursor.execute('''
        SELECT id, query_face_detection_id, reference_face_detection_id, person_id, distance, threshold, model
        FROM face_similarity_scores
        WHERE query_face_detection_id = ?
        ORDER BY id
    ''', (face_detection_id,))
    face['candidate_scores'] = [dict(row) for row in cursor.fetchall()]

    # Other confirmed faces in the same image (for same-image exclusion)
    cursor.execute('''
        SELECT id, person_id
        FROM face_detections
        WHERE file_id = ? AND id != ? AND person_id IS NOT NULL AND source = 'Manual'
    ''', (face['file_id'], face_detection_id))
    face['same_file_confirmed_faces'] = [dict(row) for row in cursor.fetchall()]

    # Event associated with this image
    cursor.execute('''
        SELECT e.id
        FROM event_files ef
        INNER JOIN events e ON e.id = ef.event_id
        WHERE ef.file_id = ?
        ORDER BY e.id
        LIMIT 1
    ''', (face['file_id'],))
    event_row = cursor.fetchone()
    face['event_id'] = event_row['id'] if event_row else None

    # Manually confirmed faces from the same event.
    # face_contents holds crops for faces that were tagged manually from the
    # start; ai_face_contents holds crops for faces that were detected
    # automatically and later confirmed.  We coalesce both sources so that
    # either provides the crop path and embedding needed for DeepFace.
    if face['event_id'] is not None:
        cursor.execute('''
            SELECT DISTINCT
                fd.id,
                fd.person_id,
                fd.file_id,
                fd.created_at,
                COALESCE(fcon.id, aifc.id)             AS face_content_id,
                COALESCE(fcon.file_path, aifc.file_path) AS face_file_path,
                COALESCE(fcon.embedding, aifc.embedding) AS embedding_json
            FROM event_files ef
            INNER JOIN face_detections fd ON fd.file_id = ef.file_id
            LEFT JOIN face_contents    fcon ON fcon.face_detection_id = fd.id
            LEFT JOIN ai_face_contents aifc ON aifc.face_detection_id = fd.id
            WHERE ef.event_id = ?
              AND fd.source = 'Manual'
              AND fd.person_id IS NOT NULL
              AND fd.id != ?
            ORDER BY fd.person_id, fd.id
        ''', (face['event_id'], face_detection_id))
        all_event_refs = [dict(row) for row in cursor.fetchall()]
        face['event_confirmed_faces'] = all_event_refs

        # Subset: only refs confirmed after the last workflow run.
        # These must be compared even when a score for the same person already
        # exists, because they represent new evidence that was unavailable
        # during the previous execution.
        if new_refs_since:
            face['new_event_confirmed_faces'] = [
                r for r in all_event_refs
                if r['created_at'] and r['created_at'] > new_refs_since
            ]
        else:
            face['new_event_confirmed_faces'] = []
    else:
        face['event_confirmed_faces'] = []
        face['new_event_confirmed_faces'] = []

    return face


def get_alternative_version(catalog_db_path: str, file_id: int) -> Optional[Dict]:
    """
    The 1200px base derivative - PINNED, not "the best available".

    Stored face embeddings and bounding boxes are in this image's pixel space,
    so taking a larger rung when one exists would silently invalidate them
    (documentation/specs/file-sizes-spec.md section 6.2).
    """
    conn = sqlite3.connect(catalog_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('''
        SELECT alternative_file_path, alternative_file_name, width, height
        FROM alternative_versions
        WHERE original_file_id = ?
          AND rung IN ('1200','1600','2048','2560')
        ORDER BY (rung = '1200') DESC, width ASC, id ASC
        LIMIT 1
    ''', (file_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def crop_face_to_bytes(
    file_path: str,
    x: int, y: int, w: int, h: int,
    detection_width: Optional[int],
    detection_height: Optional[int],
    original_width: Optional[int],
    original_height: Optional[int],
    user_orientation: int = 1,
) -> bytes:
    """
    Open image, correct EXIF orientation, scale bounding box if needed, crop face,
    and return JPEG bytes.  Never writes to disk.

    user_orientation: the Basic Editor's edit, passed only when file_path is
    the original (a display copy already carries it).
    """
    file_ext = os.path.splitext(file_path)[1].lower()

    if file_ext == '.cr3':
        try:
            import rawpy
            with rawpy.imread(file_path) as raw:
                rgb = raw.postprocess(use_camera_wb=True, half_size=False,
                                      no_auto_bright=False, output_bps=8)
            img = Image.fromarray(rgb)
        except ImportError:
            raise Exception(f'rawpy not available for CR3: {file_path}')
    else:
        if file_ext in ('.heic', '.heif'):
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass
        img = Image.open(file_path)

    oriented = ImageOps.exif_transpose(img)
    if oriented is not None:
        img = oriented
    if user_orientation and user_orientation != 1:
        from lib.orientation import apply_user
        img = apply_user(img, user_orientation)

    img_w, img_h = img.size
    fx, fy, fw, fh = x, y, w, h
    if (detection_width and detection_height and original_width and original_height
            and (detection_width != img_w or detection_height != img_h)):
        scale_x = img_w / detection_width
        scale_y = img_h / detection_height
        fx = int(x * scale_x)
        fy = int(y * scale_y)
        fw = int(w * scale_x)
        fh = int(h * scale_y)

    left = max(0, fx)
    top = max(0, fy)
    right = min(img_w, fx + fw)
    bottom = min(img_h, fy + fh)

    if right <= left or bottom <= top:
        raise ValueError(f'Invalid crop box ({left},{top},{right},{bottom}) for {img_w}x{img_h}')

    face_crop = img.crop((left, top, right, bottom))
    face_crop = face_crop.resize((300, 300), Image.Resampling.LANCZOS)

    if face_crop.mode in ('RGBA', 'LA', 'P'):
        bg = Image.new('RGB', face_crop.size, (255, 255, 255))
        if face_crop.mode == 'P':
            face_crop = face_crop.convert('RGBA')
        bg.paste(face_crop, mask=face_crop.split()[-1] if face_crop.mode in ('RGBA', 'LA') else None)
        face_crop = bg
    elif face_crop.mode != 'RGB':
        face_crop = face_crop.convert('RGB')

    buf = io.BytesIO()
    face_crop.save(buf, 'JPEG', quality=95)
    return buf.getvalue()


def compute_embedding_for_bytes(face_bytes: bytes) -> Optional[List[float]]:
    """Call POST /api/embed and return the active model's embedding, or None."""
    try:
        response = requests.post(
            DEEPFACE_EMBED_URL,
            files={'face_image': ('query.jpg', face_bytes, 'image/jpeg')},
            data={'model': DEFAULT_MODEL},
            timeout=30,
        )
        if response.status_code == 200:
            data = response.json()
            if data.get('success') and data.get('embedding'):
                served = data.get('model')
                if served and served != DEFAULT_MODEL:
                    print(f'    Warning: asked for {DEFAULT_MODEL}, got {served}; '
                          f'discarding')
                    return None
                try:
                    embedding_models.check_embedding(data['embedding'], DEFAULT_MODEL)
                except embedding_models.EmbeddingModelMismatch as e:
                    print(f'    Warning: {e}; discarding')
                    return None
                return data['embedding']
    except Exception as e:
        print(f'    Warning: embed call failed for in-memory bytes: {e}')
    return None


def save_query_embedding(catalog_db_path: str, face_detection_id: int,
                         embedding: List[float]) -> None:
    """Persist query face embedding into ai_face_contents (no-op if row absent)."""
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    _wal_pragmas(conn, 30000)
    conn.execute(
        'UPDATE ai_face_contents SET embedding = ?, embedding_model_id = ? '
        'WHERE face_detection_id = ?',
        (json.dumps(embedding), DEFAULT_MODEL_ID, face_detection_id)
    )
    conn.commit()
    conn.close()


def load_cached_query_embeddings(catalog_db_path: str) -> dict:
    """
    Load the active model's embeddings. Returns {face_detection_id -> List[float]}.

    Through the face_embeddings view and filtered to one model: an embedding
    from a different model must be invisible here, not merely unlikely to win.
    """
    conn = sqlite3.connect(catalog_db_path)
    cursor = conn.cursor()
    cursor.execute('''
        SELECT face_detection_id, embedding
        FROM face_embeddings
        WHERE embedding_model_id = ?
    ''', (DEFAULT_MODEL_ID,))
    cached: dict = {}
    expected = embedding_models.dims(DEFAULT_MODEL)
    for face_id, emb_json in cursor.fetchall():
        try:
            vec = json.loads(emb_json)
        except (ValueError, TypeError):
            continue
        if len(vec) == expected:
            cached[face_id] = vec
    conn.close()
    return cached


def insert_audit_row(cursor: sqlite3.Cursor, execution_id: Optional[int], rule_name: str,
                     action_type: str, source_face_detection_id: int,
                     face_similarity_score_id: Optional[int], candidate_person_id: Optional[int],
                     reference_face_detection_id: Optional[int], source_file_id: Optional[int],
                     source_event_id: Optional[int], previous_distance: Optional[float],
                     new_distance: Optional[float], reason: str, metadata: Dict):
    cursor.execute('''
        INSERT INTO face_similarity_scores_audit (
            execution_id,
            rule_name,
            action_type,
            source_face_detection_id,
            face_similarity_score_id,
            candidate_person_id,
            reference_face_detection_id,
            source_file_id,
            source_event_id,
            previous_distance,
            new_distance,
            reason,
            metadata
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        execution_id,
        rule_name,
        action_type,
        source_face_detection_id,
        face_similarity_score_id,
        candidate_person_id,
        reference_face_detection_id,
        source_file_id,
        source_event_id,
        previous_distance,
        new_distance,
        reason,
        json.dumps(metadata, sort_keys=True)
    ))


def apply_same_image_exclusion(cursor: sqlite3.Cursor, face: Dict,
                                execution_id: Optional[int]) -> int:
    """
    Rule 1: remove candidate rows whose person is already confirmed on a
    different face detection in the same image.
    """
    confirmed_person_ids = {
        row['person_id'] for row in face['same_file_confirmed_faces']
        if row['person_id'] is not None
    }
    removed = 0

    for candidate in face['candidate_scores']:
        if candidate['person_id'] not in confirmed_person_ids:
            continue

        reason = 'Person is already confirmed on another face in the same image'
        insert_audit_row(
            cursor,
            execution_id,
            RULE_SAME_IMAGE_EXCLUSION,
            'delete',
            face['id'],
            candidate['id'],
            candidate['person_id'],
            candidate['reference_face_detection_id'],
            face['file_id'],
            face['event_id'],
            candidate['distance'],
            None,
            reason,
            {
                'same_file_confirmed_face_ids': [
                    row['id'] for row in face['same_file_confirmed_faces']
                    if row['person_id'] == candidate['person_id']
                ]
            }
        )
        cursor.execute('DELETE FROM face_similarity_scores WHERE id = ?', (candidate['id'],))
        removed += 1

    return removed


def euclidean_l2_distance(v1: Optional[List[float]],
                          v2: Optional[List[float]]) -> Optional[float]:
    """
    Euclidean distance between two L2-normalised embeddings ("euclidean_l2").
    Mirrors DeepFace's euclidean_l2 metric; bounded to [0, 2]; VGG-Face threshold
    is 1.17. Returns None when either embedding is missing so the column stays
    NULL rather than holding a wrong value.

    Kept identical to the implementation in ai_face_recognition_workflow.py so
    both writers produce directly comparable values.
    """
    if not v1 or not v2 or len(v1) != len(v2):
        return None
    n1 = math.sqrt(sum(a * a for a in v1))
    n2 = math.sqrt(sum(b * b for b in v2))
    if n1 == 0.0 or n2 == 0.0:
        return None
    total = 0.0
    for a, b in zip(v1, v2):
        d = (a / n1) - (b / n2)
        total += d * d
    return math.sqrt(total)


def _compare_query_against_refs(
    catalog_db_path: str,
    face: Dict,
    refs_to_compare: List[Dict],
    cached_query_embeddings: dict,
) -> List[Dict]:
    """
    Call the DeepFace /api/compare-multiple endpoint for the query face against a
    list of reference faces.

    Returns a list of result dicts, each containing:
        reference_face_detection_id, person_id, distance, model,
        distance_metric, verified, threshold

    Returns an empty list when the DeepFace backend is unreachable or returns an
    error — the caller must decide how to handle the missing scores.
    """
    query_face_id = face['id']

    # ── Resolve the image path to use for the query face ──────────────────────
    file_type = face.get('file_type', '')
    alt = get_alternative_version(catalog_db_path, face['file_id'])
    if alt and os.path.exists(alt['alternative_file_path']):
        use_path = alt['alternative_file_path']
        edit = 1                      # a display copy already carries the edit
        det_w = alt.get('width') or face.get('detection_image_width')
        det_h = alt.get('height') or face.get('detection_image_height')
    else:
        non_displayable = {'.heic', '.heif', '.cr3'}
        if file_type.lower() in non_displayable:
            print(f'    Skipping event-expansion for face {query_face_id} — '
                  f'no JPG alternative for {file_type} file')
            return []
        use_path = face.get('file_path', '')
        det_w = face.get('detection_image_width')
        det_h = face.get('detection_image_height')
        from lib.orientation import user_orientation as _user_orientation
        _c = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            edit = _user_orientation(_c, face['file_id'])
        finally:
            _c.close()

    if not use_path or not os.path.exists(use_path):
        print(f'    Skipping event-expansion for face {query_face_id} — '
              f'file not found: {use_path}')
        return []

    # ── Crop the query face ───────────────────────────────────────────────────
    try:
        query_bytes = crop_face_to_bytes(
            use_path,
            face['x'], face['y'], face['w'], face['h'],
            det_w, det_h,
            face.get('original_width'), face.get('original_height'),
            edit,
        )
    except Exception as e:
        print(f'    Skipping event-expansion for face {query_face_id} — crop failed: {e}')
        return []

    # ── Get or compute query embedding ───────────────────────────────────────
    query_embedding = cached_query_embeddings.get(query_face_id)
    if not query_embedding:
        query_embedding = compute_embedding_for_bytes(query_bytes)
        if query_embedding:
            save_query_embedding(catalog_db_path, query_face_id, query_embedding)
            cached_query_embeddings[query_face_id] = query_embedding

    # ── Send in batches ───────────────────────────────────────────────────────
    all_results: List[Dict] = []

    for batch_start in range(0, len(refs_to_compare), BATCH_SIZE):
        batch = refs_to_compare[batch_start:batch_start + BATCH_SIZE]

        files_payload: dict = {}
        form_data: dict = {}

        if query_embedding is not None:
            form_data['query_embedding'] = json.dumps(query_embedding)
        else:
            files_payload['face_image'] = ('query.jpg', query_bytes, 'image/jpeg')

        ref_meta_for_request: List[Dict] = []
        for ridx, ref in enumerate(batch):
            ref_embedding = None
            if ref.get('embedding_json'):
                try:
                    ref_embedding = json.loads(ref['embedding_json'])
                except (ValueError, TypeError):
                    pass

            if ref_embedding is not None:
                form_data[f'embedding_{ridx}'] = json.dumps(ref_embedding)
            elif ref.get('face_file_path') and os.path.exists(ref['face_file_path']):
                with open(ref['face_file_path'], 'rb') as fh:
                    ref_bytes = fh.read()
                files_payload[f'comparison_{ridx}'] = (f'ref_{ridx}.jpg', ref_bytes, 'image/jpeg')
            else:
                # No embedding and no accessible face image — cannot compare.
                print(f'      Skipping reference face {ref["id"]} — no embedding or face image')
                continue

            form_data[f'person_id_{ridx}'] = str(ref['person_id'])
            form_data[f'face_id_{ridx}'] = str(ref['id'])
            form_data[f'person_name_{ridx}'] = ''
            ref_meta_for_request.append(ref)

        if not ref_meta_for_request:
            continue

        try:
            response = requests.post(DEEPFACE_URL, files=files_payload,
                                     data=form_data, timeout=60)
        except requests.exceptions.Timeout:
            print(f'    Timeout on batch {batch_start // BATCH_SIZE + 1}, skipping batch')
            continue
        except requests.exceptions.ConnectionError:
            print(f'    Cannot connect to DeepFace backend at {DEEPFACE_URL} — skipping batch')
            continue

        if response.status_code != 200:
            print(f'    DeepFace error {response.status_code} on batch — skipping batch')
            continue

        result = response.json()
        if not result.get('success'):
            print(f'    Comparison returned success=false — skipping batch')
            continue

        for res in result.get('results', []):
            if 'distance' not in res:
                continue
            idx = res['index']
            if idx >= len(ref_meta_for_request):
                continue
            meta = ref_meta_for_request[idx]
            ref_emb = None
            if meta.get('embedding_json'):
                try:
                    ref_emb = json.loads(meta['embedding_json'])
                except (ValueError, TypeError):
                    ref_emb = None
            all_results.append({
                'reference_face_detection_id': meta['id'],
                'person_id': meta['person_id'],
                'distance': float(res['distance']),
                'euclidean_distance': euclidean_l2_distance(query_embedding, ref_emb),
                'model': res.get('model', DEFAULT_MODEL),
                'distance_metric': res.get('distance_metric', DEFAULT_DISTANCE_METRIC),
                'verified': 1 if res.get('verified') else 0,
                'threshold': float(res.get('threshold', DEFAULT_THRESHOLD)),
            })

    return all_results


def apply_same_event_expansion(
    cursor: sqlite3.Cursor,
    face: Dict,
    execution_id: Optional[int],
    catalog_db_path: str,
    cached_query_embeddings: dict,
) -> int:
    """
    Rule 2: for each manually confirmed face in the same event that has no
    existing candidate score against this query face, compute the real DeepFace
    distance and insert a new face_similarity_scores row.

    Two categories of references are compared:
    - Refs with no existing (query, reference) pair at all — genuinely new
      comparisons.
    - Refs in ``new_event_confirmed_faces`` — manually confirmed faces added
      after the last workflow run.  These are compared even when a score for
      the same person already exists, because they are new evidence that was
      unavailable in prior executions.
    """
    if face['event_id'] is None:
        return 0

    # Only treat a pair as already covered if it has a real computed distance.
    # Contextual placeholder rows (model='contextual') must not block a genuine
    # DeepFace comparison from being computed and inserted.
    existing_pairs = {
        (candidate['reference_face_detection_id'], candidate['person_id'])
        for candidate in face['candidate_scores']
        if candidate.get('model', '') != 'contextual'
    }

    # New refs confirmed after the last run — must be compared regardless of
    # whether any score for that person already exists.
    new_ref_ids = {r['id'] for r in face.get('new_event_confirmed_faces', [])}

    # Identify confirmed faces that are genuinely missing from the candidate set
    # OR are newly added references that bypass the existing-pairs check.
    missing_refs = []
    for confirmed_face in face['event_confirmed_faces']:
        pair = (confirmed_face['id'], confirmed_face['person_id'])
        is_new_ref = confirmed_face['id'] in new_ref_ids
        if pair in existing_pairs and not is_new_ref:
            continue
        # Skip if this confirmed face belongs to the same file as the query face
        # — same-image exclusion handles that, and self-comparisons add no value.
        if confirmed_face['file_id'] == face['file_id']:
            continue
        missing_refs.append(confirmed_face)

    if not missing_refs:
        return 0

    print(f'    Face {face["id"]}: computing distances for {len(missing_refs)} '
          f'event-confirmed reference(s) in event {face["event_id"]}')

    computed_results = _compare_query_against_refs(
        catalog_db_path, face, missing_refs, cached_query_embeddings
    )

    # Build lookup so we can match results back to the refs in one pass
    result_by_ref_id: Dict[int, Dict] = {
        r['reference_face_detection_id']: r for r in computed_results
    }

    inserted = 0

    for confirmed_face in missing_refs:
        pair = (confirmed_face['id'], confirmed_face['person_id'])
        res = result_by_ref_id.get(confirmed_face['id'])

        if res is not None:
            distance = res['distance']
            euclidean = res.get('euclidean_distance')
            model = res['model']
            distance_metric = res['distance_metric']
            verified = res['verified']
            threshold = res['threshold']
        else:
            # DeepFace call failed or returned no result for this ref — skip.
            # We do NOT insert a placeholder; a missing score is better than a
            # misleading one.
            print(f'      No distance result for reference face {confirmed_face["id"]} '
                  f'(person {confirmed_face["person_id"]}) — skipping insertion')
            continue

        # Use INSERT OR REPLACE so that a contextual placeholder row for this
        # (query, reference) pair is upgraded to a real computed distance rather
        # than being silently ignored by the UNIQUE constraint.
        cursor.execute('''
            INSERT INTO face_similarity_scores (
                query_face_detection_id,
                reference_face_detection_id,
                person_id,
                distance,
                euclidean_distance,
                model,
                distance_metric,
                verified,
                threshold
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (query_face_detection_id, reference_face_detection_id)
            DO UPDATE SET
                person_id          = excluded.person_id,
                distance           = excluded.distance,
                euclidean_distance = excluded.euclidean_distance,
                model              = excluded.model,
                distance_metric    = excluded.distance_metric,
                verified           = excluded.verified,
                threshold          = excluded.threshold
            WHERE face_similarity_scores.model = 'contextual'
        ''', (
            face['id'],
            confirmed_face['id'],
            confirmed_face['person_id'],
            distance,
            euclidean,
            model,
            distance_metric,
            verified,
            threshold,
        ))

        if cursor.rowcount <= 0:
            continue

        score_id = cursor.lastrowid
        reason = (
            'Person has a manually confirmed face in the same event and the '
            'candidate comparison was missing; distance computed via DeepFace'
        )
        insert_audit_row(
            cursor,
            execution_id,
            RULE_SAME_EVENT_EXPANSION,
            'insert',
            face['id'],
            score_id,
            confirmed_face['person_id'],
            confirmed_face['id'],
            face['file_id'],
            face['event_id'],
            None,
            distance,
            reason,
            {
                'reference_file_id': confirmed_face['file_id'],
                'model': model,
                'verified': bool(verified),
                'threshold': threshold,
            }
        )
        existing_pairs.add(pair)
        inserted += 1

    return inserted


def execute(source_dir, target_dir, files, progress_callback,
            catalog_id=None, catalog=None, execution_id=None):
    if catalog is None and catalog_id is not None:
        catalog = Catalog.get_by_id(catalog_id)
        if catalog is None:
            raise ValueError(f'Catalog with ID {catalog_id} not found')
    if catalog is None:
        raise ValueError('Either catalog or catalog_id must be provided')

    catalog_db_path = os.path.join(target_dir, 'workflow_results.db')
    ensure_tables(catalog_db_path)

    global DEFAULT_MODEL, DEFAULT_MODEL_ID, DEFAULT_THRESHOLD
    DEFAULT_MODEL = embedding_models.active_model(catalog_db_path)
    DEFAULT_MODEL_ID = embedding_models.model_id(DEFAULT_MODEL)
    DEFAULT_THRESHOLD = embedding_models.verification_distance(DEFAULT_MODEL)
    print(f'Embedding model: {DEFAULT_MODEL} '
          f'({embedding_models.dims(DEFAULT_MODEL)}-d, id {DEFAULT_MODEL_ID})')

    faces = get_eligible_face_detections(catalog_db_path)
    total = len(faces)
    skipped = 0
    processed = 0
    removed = 0
    inserted = 0
    failed = 0
    audit_rows = 0

    print(f'Found {total} face detection(s) to review')

    # Determine the cutoff timestamp for new manually confirmed refs.
    # We read this before creating the current execution row so that we pick
    # up the completed_at of the *previous* run, not the current one.
    last_run_ts = get_last_run_completed_at(catalog_db_path)
    if last_run_ts:
        print(f'Incremental mode: new refs confirmed after {last_run_ts} will be '
              f'compared even when a per-person score already exists')
    else:
        print('No prior completed run found — running in full mode')

    # Pre-load all cached query embeddings once so per-face processing can
    # reuse them without hitting the DB on every iteration.
    cached_query_embeddings = load_cached_query_embeddings(catalog_db_path)
    print(f'Loaded {len(cached_query_embeddings)} cached query embedding(s)')

    if progress_callback:
        progress_callback(0, total)

    stopped = False

    for index, face_row in enumerate(faces, 1):
        # Top of the iteration: the previous face's rows are committed and its
        # connection closed.
        if execution_id and stop_requested(catalog_db_path, execution_id):
            print(f'\nStop requested - leaving after {index - 1} of {total} face(s).')
            stopped = True
            break

        conn = sqlite3.connect(catalog_db_path)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()

        try:
            face = get_face_context(cursor, face_row['id'], face_row,
                                    new_refs_since=last_run_ts)
            if face is None:
                skipped += 1
                continue

            if not face['candidate_scores'] and not face['event_confirmed_faces']:
                skipped += 1
                if progress_callback and (index % 5 == 0 or index == total):
                    progress_callback(index, total)
                continue

            removed_count = apply_same_image_exclusion(cursor, face, execution_id)
            inserted_count = apply_same_event_expansion(
                cursor, face, execution_id, catalog_db_path, cached_query_embeddings
            )
            conn.commit()

            processed += 1
            removed += removed_count
            inserted += inserted_count
            audit_rows += removed_count + inserted_count
        except Exception as exc:
            conn.rollback()
            print(f'ERROR processing face detection {face_row["id"]}: {exc}')
            failed += 1
        finally:
            conn.close()

        if progress_callback and (index % 5 == 0 or index == total):
            progress_callback(index, total)

    if stopped:
        if execution_id:
            record_stopped(catalog_db_path, execution_id, processed_files=processed,
                           skipped_files=skipped,
                           message=stop_message(processed, total))
        return {
            'faces_reviewed': total,
            'faces_processed': processed,
            'faces_skipped': skipped,
            'candidates_removed': removed,
            'candidates_inserted': inserted,
            'audit_rows_written': audit_rows,
            'stopped': True,
        }

    print(f'Faces reviewed: {total}')
    print(f'Processed:      {processed}')
    print(f'Skipped:        {skipped}')
    print(f'Removed:        {removed}')
    print(f'Inserted:       {inserted}')
    print(f'Audit rows:     {audit_rows}')
    print(f'Failed:         {failed}')

    return {
        'faces_reviewed': total,
        'faces_processed': processed,
        'faces_skipped': skipped,
        'candidates_removed': removed,
        'candidates_inserted': inserted,
        'audit_rows_written': audit_rows,
        'faces_failed': failed
    }


if __name__ == '__main__':
    import argparse

    parser = argparse.ArgumentParser(description='AI/ML face recognition workflow (subprocess mode)')
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
            # fire the downstream chain over a partial run.
            print(f'Subprocess execution {args.execution_id} stopped by request.')
        else:
            _db_complete(catalog_db_path, args.execution_id, 'completed',
                         skipped_files=result.get('faces_skipped', 0) if isinstance(result, dict) else 0)
            print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)
