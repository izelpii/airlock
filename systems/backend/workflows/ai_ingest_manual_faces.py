## EXTRACT FACE DETECTIONS TO FILES

import json
import os
import sys
import sqlite3
import hashlib
import requests
from datetime import datetime
from PIL import Image, ImageOps
from typing import Dict, List, Optional, Tuple

DEEPFACE_EMBED_URL = os.environ.get('DEEPFACE_URL', 'http://deepface.airlock.lan') + '/api/embed'

# Add parent directory to path to import catalog module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import embedding_models
from face_recognition_schema import ensure_face_recognition_schema

# Which model this run embeds with; set in execute() from the catalog's own
# property. See embedding_models.py and the face-recognition-migration spec.
ACTIVE_MODEL = embedding_models.LEGACY_MODEL
ACTIVE_MODEL_ID = embedding_models.model_id(embedding_models.LEGACY_MODEL)
# Cooperative stop - see workflows-review-spec.md section 3.4. This loop is
# incremental (an already-done item is skipped), so leaving between two
# items needs no checkpoint.
from lib.stopping import stop_requested, record_stopped, stop_message


# ─── Subprocess progress helpers ──────────────────────────────────────────────

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
    """Write progress directly to workflow_executions (used when running as subprocess)."""
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
    """Mark execution finished in the DB (used when running as subprocess)."""
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

def get_manual_face_detections(catalog_db_path: str) -> List[Dict]:
    """
    Query all manual face detections from the catalog database.
    Returns a list of face detections with associated file and person information.
    """
    conn = sqlite3.connect(catalog_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT
            fd.id as face_detection_id,
            fd.file_id,
            fd.x,
            fd.y,
            fd.w,
            fd.h,
            fd.person_id,
            fd.image_width as detection_image_width,
            fd.image_height as detection_image_height,
            fc.file_path as original_file_path,
            fc.file_name as original_file_name,
            fc.file_type,
            fc.master_datetime,
            p.name as person_name,
            ed.creation_date,
            ed.width as original_width,
            ed.height as original_height
        FROM face_detections fd
        INNER JOIN file_contents fc ON fd.file_id = fc.id
        LEFT JOIN people p ON fd.person_id = p.id
        LEFT JOIN exif_data ed ON fc.exif_data_id = ed.id
        WHERE fd.source = 'Manual' AND fd.person_id IS NOT NULL
        ORDER BY fd.created_at
    ''')
    
    rows = cursor.fetchall()
    conn.close()
    
    return [dict(row) for row in rows]


def get_alternative_version_info(catalog_db_path: str, file_id: int) -> Optional[Dict]:
    """
    The 1200px base derivative for a file - PINNED.

    A manual face is drawn on this image in the UI, so the coordinates that
    arrive are in its pixel space (file-sizes-spec.md 6.2).
    """
    conn = sqlite3.connect(catalog_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    cursor.execute('''
        SELECT
            alternative_file_path,
            alternative_file_name,
            width,
            height,
            characteristic
        FROM alternative_versions
        WHERE original_file_id = ?
          AND rung IN ('1200','1600','2048','2560')
        ORDER BY (rung = '1200') DESC, width ASC, id ASC
        LIMIT 1
    ''', (file_id,))
    
    row = cursor.fetchone()
    conn.close()
    
    return dict(row) if row else None


def calculate_face_coordinates(
    face_x: int, face_y: int, face_w: int, face_h: int,
    detection_width: int, detection_height: int,
    original_width: int, original_height: int
) -> Tuple[int, int, int, int]:
    """
    Transform face coordinates from detection image to original image dimensions.
    
    Args:
        face_x, face_y, face_w, face_h: Face coordinates in detection image
        detection_width, detection_height: Dimensions of image used for detection
        original_width, original_height: Dimensions of original image
    
    Returns:
        Tuple of (x, y, w, h) in original image coordinates
    """
    # Calculate scale factors
    scale_x = original_width / detection_width
    scale_y = original_height / detection_height
    
    # Transform coordinates
    orig_x = int(face_x * scale_x)
    orig_y = int(face_y * scale_y)
    orig_w = int(face_w * scale_x)
    orig_h = int(face_h * scale_y)
    
    return orig_x, orig_y, orig_w, orig_h


def convert_image_to_jpg(image_path: str) -> Image.Image:
    """
    Open an image and convert it to RGB format suitable for JPG extraction.
    Handles HEIC, CR3, and other formats.
    
    Args:
        image_path: Path to the image file
    
    Returns:
        PIL Image object in RGB mode with proper orientation
    """
    file_ext = os.path.splitext(image_path)[1].lower()
    
    # Handle CR3 (Canon RAW) files
    if file_ext == '.cr3':
        try:
            import rawpy
            with rawpy.imread(image_path) as raw:
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    half_size=False,
                    no_auto_bright=False,
                    output_bps=8,
                )
            img = Image.fromarray(rgb)
        except ImportError:
            raise Exception(f"rawpy not available, cannot process CR3 file: {image_path}")
    else:
        # Handle HEIC files
        if file_ext in ['.heic', '.heif']:
            try:
                import pillow_heif
                pillow_heif.register_heif_opener()
            except ImportError:
                pass  # Will fail on Image.open if pillow_heif not available
        
        # Open the image
        img = Image.open(image_path)
    
    # Apply EXIF orientation correction
    oriented_image = ImageOps.exif_transpose(img)
    if oriented_image is not None:
        img = oriented_image
    
    # Convert to RGB if necessary
    if img.mode in ('RGBA', 'LA', 'P'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        img = background
    elif img.mode != 'RGB':
        img = img.convert('RGB')
    
    return img


def extract_face_from_image(
    image_path: str,
    x: int, y: int, w: int, h: int,
    target_size: int = 300
) -> Image.Image:
    """
    Extract a face region from an image and resize to target size.
    
    Args:
        image_path: Path to the source image
        x, y, w, h: Face coordinates in the image
        target_size: Target size for the output (default 300x300 pixels)
    
    Returns:
        PIL Image object containing the extracted and resized face
    """
    # Open and convert image to RGB
    img = convert_image_to_jpg(image_path)
    
    # Calculate extraction box (no padding)
    left = max(0, x)
    top = max(0, y)
    right = min(img.width, x + w)
    bottom = min(img.height, y + h)
    
    if right <= left or bottom <= top:
        raise ValueError(
            f"Invalid face crop after coordinate conversion: "
            f"box=({left}, {top}, {right}, {bottom}), image_size={img.width}x{img.height}"
        )
    
    # Extract face region
    face_img = img.crop((left, top, right, bottom))
    
    # Resize to target size (300x300)
    face_img = face_img.resize((target_size, target_size), Image.Resampling.LANCZOS)
    
    return face_img


def calculate_sha256(file_path: str) -> str:
    """Calculate SHA256 hash of a file"""
    sha256_hash = hashlib.sha256()
    with open(file_path, "rb") as f:
        for byte_block in iter(lambda: f.read(4096), b""):
            sha256_hash.update(byte_block)
    return sha256_hash.hexdigest()


def parse_date_from_master_datetime(master_datetime: Optional[str],
                                    creation_date_fallback: Optional[str] = None
                                    ) -> Tuple[Optional[int], Optional[int], Optional[int]]:
    """
    Parse year, month, day from master_datetime (ISO 8601: YYYY-MM-DDTHH:MM:SS...).
    Falls back to EXIF creation_date format (YYYY:MM:DD HH:MM:SS) when master_datetime
    is not available (legacy rows not yet backfilled).

    Returns:
        Tuple of (year, month, day) or (None, None, None) if parsing fails
    """
    # Primary: master_datetime in ISO 8601 format
    if master_datetime:
        try:
            date_part = master_datetime[:10]  # "YYYY-MM-DD"
            year, month, day = date_part.split('-')
            return int(year), int(month), int(day)
        except (ValueError, IndexError):
            pass

    # Fallback: legacy EXIF format "YYYY:MM:DD HH:MM:SS"
    if creation_date_fallback:
        try:
            date_part = creation_date_fallback.split()[0]
            year, month, day = date_part.split(':')
            return int(year), int(month), int(day)
        except (ValueError, IndexError):
            pass

    return None, None, None


def compute_embedding(face_path: str) -> Optional[str]:
    """
    Call POST /api/embed on the DeepFace backend to get the active model's
    embedding for the face crop at face_path.

    Returns the embedding as a JSON string (suitable for storing in TEXT column),
    or None if the DeepFace backend is unavailable or returns an error.
    The failure is non-fatal — the face is still registered without an embedding;
    the face_recognition_workflow will backfill it on its first run.
    """
    try:
        with open(face_path, 'rb') as fh:
            response = requests.post(
                DEEPFACE_EMBED_URL,
                files={'face_image': ('face.jpg', fh, 'image/jpeg')},
                data={'model': ACTIVE_MODEL},
                timeout=30,
            )
        if response.status_code == 200:
            data = response.json()
            if data.get('success') and data.get('embedding'):
                # Refuse an embedding from a model we did not ask for: an older
                # deepface image ignores the `model` field and answers with its
                # own default, and storing that under the active model's id is
                # the silent corruption this change exists to prevent.
                served = data.get('model')
                if served and served != ACTIVE_MODEL:
                    print(f'  Warning: asked for {ACTIVE_MODEL} and got {served}; '
                          f'storing no embedding for {face_path}')
                    return None
                try:
                    embedding_models.check_embedding(
                        data['embedding'], ACTIVE_MODEL, face_path)
                except embedding_models.EmbeddingModelMismatch as e:
                    print(f'  Warning: {e}; storing no embedding')
                    return None
                return json.dumps(data['embedding'])
    except Exception as e:
        print(f'  Warning: could not compute embedding for {face_path}: {e}')
    return None


def save_face_and_register(
    catalog_db_path: str,
    target_dir: str,
    face_img: Image.Image,
    face_detection_id: int,
    person_id: int,
    source_file_id: int,
    master_datetime: Optional[str],
    person_name: str,
    creation_date: Optional[str] = None,
) -> Dict:
    """
    Save extracted face image, compute its embedding, and register
    both in the face_contents table.

    Args:
        catalog_db_path: Path to catalog database
        target_dir: Target directory of the catalog
        face_img: PIL Image object of the extracted face
        face_detection_id: ID of the face detection record
        person_id: ID of the person
        source_file_id: ID of the source file
        master_datetime: Canonical ISO 8601 date string from file_contents
        person_name: Name of the person
        creation_date: Legacy EXIF date string (fallback only)

    Returns:
        Dictionary with information about the saved face
    """
    # Create faces directory
    faces_dir = os.path.join(target_dir, 'tmp', 'faces')
    os.makedirs(faces_dir, exist_ok=True)

    # Generate filename
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    safe_name = person_name.replace(' ', '_').replace('/', '_')
    face_filename = f"{timestamp}_{safe_name}_face_{face_detection_id}.jpg"
    face_path = os.path.join(faces_dir, face_filename)

    # Save face image as JPG
    face_img.save(face_path, 'JPEG', quality=95, optimize=True)

    # Get file size and hash
    file_size = os.path.getsize(face_path)
    sha256_hash = calculate_sha256(face_path)

    # Parse date from master_datetime (with EXIF fallback for legacy rows)
    year, month, day = parse_date_from_master_datetime(master_datetime, creation_date)

    # Compute the embedding (non-fatal if the DeepFace backend is not running)
    embedding_json = compute_embedding(face_path)
    if embedding_json:
        print(f'  ✓ Embedding computed ({len(json.loads(embedding_json))} dims)')
    else:
        print(f'  ⚠ Embedding not available — will be backfilled by face_recognition_workflow')

    # Register in database
    conn = sqlite3.connect(catalog_db_path)
    cursor = conn.cursor()

    cursor.execute('''
        INSERT INTO face_contents
        (file_path, file_name, file_size, sha256_hash, year, month, day,
         person_id, face_detection_id, source_file_id, embedding,
         embedding_model_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (face_path, face_filename, file_size, sha256_hash, year, month, day,
          person_id, face_detection_id, source_file_id, embedding_json,
          ACTIVE_MODEL_ID if embedding_json else None))

    face_content_id = cursor.lastrowid
    conn.commit()
    conn.close()

    return {
        'id': face_content_id,
        'path': face_path,
        'filename': face_filename,
        'size': file_size,
        'year': year,
        'month': month,
        'day': day,
        'has_embedding': embedding_json is not None,
    }


def execute(source_dir, target_dir, files, progress_callback, catalog_id=None, catalog=None, execution_id=None):
    """
    Process all manual face detections in the catalog and create face files.
    
    This workflow:
    1. Reads all manual face detections from the catalog database
    2. For each detection, extracts the face from the original image
    3. Handles coordinate transformation if detection was on alternative version
    4. Converts non-JPG images (HEIC, CR3, etc.) to JPG for extraction
    5. Saves extracted faces to tmp/faces directory
    6. Registers faces in face_contents table with proper foreign keys
    
    Args:
        source_dir: Source directory path (not used, workflow operates on catalog data)
        target_dir: Target directory path (catalog target directory)
        files: List of file paths to process (not used, workflow processes all manual detections)
        progress_callback: Callback function for progress updates
        catalog_id: ID of the catalog (deprecated, use catalog parameter)
        catalog: Catalog instance to use for file operations (recommended)
        execution_id: ID of the workflow execution (for tracking)
    
    Returns:
        dict: Statistics about the execution including faces_processed and faces_failed
    """
    # If catalog is not provided, create one from catalog_id
    if catalog is None and catalog_id is not None:
        # Import here to avoid circular dependency
        import sys
        sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        from catalog import Catalog
        
        catalog = Catalog.get_by_id(catalog_id)
        if catalog is None:
            raise ValueError(f"Catalog with ID {catalog_id} not found")
    
    # If still no catalog, we need to fail
    if catalog is None:
        raise ValueError("Either catalog or catalog_id must be provided")
    
    catalog_db_path = os.path.join(target_dir, 'workflow_results.db')

    # The schema, before the first INSERT that names embedding_model_id. This
    # workflow did not ensure it before - it only ever wrote columns that
    # models.py had already created - and a catalog whose first touch is this
    # workflow would otherwise fail on a column that does not exist yet.
    ensure_face_recognition_schema(catalog_db_path)

    global ACTIVE_MODEL, ACTIVE_MODEL_ID
    ACTIVE_MODEL = embedding_models.active_model(catalog_db_path)
    ACTIVE_MODEL_ID = embedding_models.model_id(ACTIVE_MODEL)
    print(f"Embedding model: {ACTIVE_MODEL} "
          f"({embedding_models.dims(ACTIVE_MODEL)}-d, id {ACTIVE_MODEL_ID})")

    # Get all manual face detections
    print("Fetching manual face detections...")
    face_detections = get_manual_face_detections(catalog_db_path)
    
    total_faces = len(face_detections)
    print(f"Found {total_faces} manual face detections to process")
    
    if total_faces == 0:
        return {
            'faces_processed': 0,
            'faces_failed': 0,
            'faces_created': 0
        }
    
    processed = 0
    failed = 0
    created = 0

    # The total from the start, and with every report below: it used to pass
    # only `processed`, so the page showed "n of n" all the way through.
    if progress_callback:
        progress_callback(0, total_faces)
    
    stopped = False

    for face_data in face_detections:
        # Top of the iteration: the previous face is committed and reported.
        if execution_id and stop_requested(catalog_db_path, execution_id):
            print(f'\nStop requested - leaving after {processed} of {total_faces} face(s).')
            stopped = True
            break

        face_detection_id = None  # Initialize for error handling
        try:
            face_detection_id = face_data['face_detection_id']
            file_id = face_data['file_id']
            person_id = face_data['person_id']
            person_name = face_data['person_name'] or f"Person_{person_id}"
            
            print(f"\nProcessing face {processed + 1}/{total_faces}: {person_name}")
            
            # Check if face already exists in face_contents
            conn = sqlite3.connect(catalog_db_path)
            cursor = conn.cursor()
            cursor.execute('''
                SELECT COUNT(*) as count FROM face_contents
                WHERE face_detection_id = ?
            ''', (face_detection_id,))
            exists = cursor.fetchone()[0] > 0
            conn.close()
            
            if exists:
                print(f"  Face already extracted, skipping...")
                processed += 1
                if progress_callback:
                    progress_callback(processed, total_faces)
                continue
            
            # Get face coordinates
            face_x = face_data['x']
            face_y = face_data['y']
            face_w = face_data['w']
            face_h = face_data['h']
            
            # Get detection image dimensions
            detection_width = face_data['detection_image_width']
            detection_height = face_data['detection_image_height']
            
            # Get original image dimensions from EXIF/database metadata
            original_width = face_data['original_width']
            original_height = face_data['original_height']
            
            # Step 1: Get face coordinates from detection (may be from alternative version)
            # Manual face coordinates are stored relative to the image shown in the UI when tagging,
            # which is typically an alternative JPG version if one exists.
            detection_face_x = face_x
            detection_face_y = face_y
            detection_face_w = face_w
            detection_face_h = face_h
            
            # Step 2: Determine the image dimensions that the coordinates are relative to.
            # The face was tagged in the UI which shows an alternative JPG version when available.
            # However, if detection_width/height already matches the original, the tagging
            # happened directly on the original — do NOT apply the alt version dimensions.
            alt_info = get_alternative_version_info(catalog_db_path, file_id)
            detection_source_width = detection_width
            detection_source_height = detection_height

            if alt_info:
                alt_width = alt_info.get('width')
                alt_height = alt_info.get('height')
                if alt_width and alt_height:
                    # Only treat coordinates as alt-relative if the stored detection
                    # dimensions actually match the alt version (not the full original).
                    if detection_width == alt_width and detection_height == alt_height:
                        detection_source_width = alt_width
                        detection_source_height = alt_height
                        print(f"  Coordinates confirmed from alternative version: {alt_width}x{alt_height}")
                    else:
                        print(f"  Alt version exists ({alt_width}x{alt_height}) but detection "
                              f"was on {detection_width}x{detection_height} — using detection dims")
            
            # Step 3: Always extract from ORIGINAL picture
            original_file_path = face_data['original_file_path']
            
            if not os.path.exists(original_file_path):
                print(f"  ERROR: Original image not found: {original_file_path}")
                failed += 1
                processed += 1
                if progress_callback:
                    progress_callback(processed, total_faces)
                continue
            
            # Step 4: Convert original image to JPG (handles RAW, HEIC, etc.)
            print(f"  Converting original image to JPG: {os.path.basename(original_file_path)}")
            original_image = convert_image_to_jpg(original_file_path)
            original_actual_width, original_actual_height = original_image.size
            print(f"  Original image size after orientation: {original_actual_width}x{original_actual_height}")
            
            # Step 5: Transform coordinates from detection image to original image
            needs_transform = False
            if detection_source_width and detection_source_height:
                if detection_source_width != original_actual_width or detection_source_height != original_actual_height:
                    needs_transform = True
                    print(
                        f"  Transforming coordinates from {detection_source_width}x{detection_source_height} "
                        f"to {original_actual_width}x{original_actual_height}"
                    )
                    face_x, face_y, face_w, face_h = calculate_face_coordinates(
                        detection_face_x, detection_face_y, detection_face_w, detection_face_h,
                        detection_source_width, detection_source_height,
                        original_actual_width, original_actual_height
                    )
                    print(f"  Transformed coordinates: ({face_x}, {face_y}, {face_w}, {face_h})")
                else:
                    print(f"  No coordinate transformation needed (same dimensions)")
            
            # Step 6: Extract face from original image at best resolution
            print(f"  Extracting face from original image at best resolution...")
            left = max(0, face_x)
            top = max(0, face_y)
            right = min(original_image.width, face_x + face_w)
            bottom = min(original_image.height, face_y + face_h)
            
            if right <= left or bottom <= top:
                raise ValueError(
                    f"Invalid face crop after transformation: "
                    f"coords=({face_x}, {face_y}, {face_w}, {face_h}), "
                    f"box=({left}, {top}, {right}, {bottom}), "
                    f"image_size={original_image.width}x{original_image.height}"
                )
            
            face_img = original_image.crop((left, top, right, bottom))
            print(f"  Extracted face region: {face_img.width}x{face_img.height}")
            
            # Step 7: Resize to 300x300 (final step)
            print(f"  Resizing face to 300x300...")
            face_img = face_img.resize((300, 300), Image.Resampling.LANCZOS)
            
            # Save and register face
            print(f"  Saving face image...")
            face_info = save_face_and_register(
                catalog_db_path,
                target_dir,
                face_img,
                face_detection_id,
                person_id,
                file_id,
                master_datetime=face_data.get('master_datetime'),
                person_name=person_name,
                creation_date=face_data.get('creation_date'),
            )
            
            created += 1
            print(f"  ✓ Created: {face_info['filename']} ({face_info['size']} bytes)")
            if face_info['year']:
                print(f"    Date: {face_info['year']}-{face_info['month']:02d}-{face_info['day']:02d}")
            
        except Exception as e:
            failed += 1
            print(f"  ERROR: Failed to process face {face_detection_id}: {str(e)}")
            import traceback
            traceback.print_exc()
        
        processed += 1
        if progress_callback:
            progress_callback(processed, total_faces)
    
    if stopped:
        if execution_id:
            record_stopped(catalog_db_path, execution_id, processed_files=processed,
                           message=stop_message(processed, total_faces))
        return {
            'faces_processed': processed - failed,
            'faces_failed': failed,
            'faces_created': created,
            'stopped': True,
        }

    print(f"\n{'='*60}")
    print(f"Face extraction complete!")
    print(f"  Total processed: {processed}")
    print(f"  Successfully created: {created}")
    print(f"  Failed: {failed}")
    print(f"{'='*60}")

    return {
        'faces_processed': processed - failed,
        'faces_failed': failed,
        'faces_created': created
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
    from catalog import Catalog

    parser = argparse.ArgumentParser(description='Process manual faces workflow (subprocess mode)')
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
                         skipped_files=0)
            print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)
