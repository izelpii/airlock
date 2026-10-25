"""
RETIRED — kept for reference only. Do not wire this back up.

Superseded by io_picture_import_workflow.py, which is a strict superset: every
function in this file exists there, plus structure-aware date extraction (the
directory and filename tiers), the date_source audit trail, and the empty-source
guard that turns a broken mount into an error instead of a silent success.

The one behavioural difference was date resolution. Both call
resolve_master_datetime(), but this one called it WITHOUT source_dir, which
disables the directory and filename tiers — so a file with no usable EXIF fell
straight through to its filesystem timestamp, which for a copied archive is the
copy date rather than the capture date.

Retired 2026-09-06. Its references were removed from workflow_triggers.py,
workflow_scheduler.py, app.py and workflows/defaults.json at the same time, so
nothing enqueues, chains or schedules it any more. No catalog had it registered.

Note that this file no longer RUNS from here: `lib/` is a sibling of the
workflows directory, not of legacy/, so the `from lib.utilities import ...`
below will not resolve. That is deliberate — it is an archive, not a fallback.
"""

## Import Pictures

import os
import sys
import sqlite3
import stat

# Add parent directory to path to import catalog module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import Catalog

# Add workflows directory to path to import lib module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import common utilities
from lib.utilities import (
    extract_exif_data_with_exiftool,
    extract_exif_data,
    convert_to_degrees,
    calculate_sha256,
    get_file_timestamps,
    is_image_file,
    is_raw_file,
    is_heic_file,
    is_cr3_file,
    is_jpg_or_png_file,
    create_alternate_version,
    create_thumbnail,
    THUMBNAIL_SIZES,
    is_video_file,
    create_video_proxy,
    create_video_poster,
    get_next_alternate_sequence,
    convert_heic_to_jpg,
    convert_cr3_to_jpg,
    resize_image_to_width,
    get_creation_date,
    format_creation_timestamp,
    generate_new_filename,
    copy_file_with_catalog,
    compute_geohash,
    resolve_master_datetime,
)
# Cooperative stop - see io_picture_import_workflow.py; same shape, same
# reason it needs no checkpoint.
from lib.stopping import stop_requested, record_stopped, stop_message

# ─── Subprocess progress helpers ──────────────────────────────────────────────

def _db_update_progress(catalog_db_path: str, execution_id: int,
                        processed: int, total: int):
    """Write progress directly to workflow_executions (used when running as subprocess)."""
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(
        'UPDATE workflow_executions SET processed_files=?, total_files=? WHERE id=?',
        (processed, total, execution_id)
    )
    conn.commit()
    conn.close()


def _db_complete(catalog_db_path: str, execution_id: int, status: str,
                 error_message: str = None, skipped_files: int = 0):
    """Mark execution finished in the DB (used when running as subprocess)."""
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute(
        '''UPDATE workflow_executions
           SET status=?, completed_at=CURRENT_TIMESTAMP,
               error_message=?, skipped_files=?
           WHERE id=?''',
        (status, error_message, skipped_files, execution_id)
    )
    conn.commit()
    conn.close()


def execute(source_dir, target_dir, files, progress_callback, catalog_id=None, catalog=None, execution_id=None):
    """
    Enhanced workflow: Copy new files from source to target directory,
    extract EXIF data from images, and record in catalog database
    
    Args:
        source_dir: Source directory path
        target_dir: Target directory path (catalog target directory)
        files: List of file paths to process
        progress_callback: Callback function for progress updates
        catalog_id: ID of the catalog (deprecated, use catalog parameter)
        catalog: Catalog instance to use for file operations (recommended)
        execution_id: ID of the workflow execution (for tracking)
    
    Returns:
        dict: Statistics about the execution including files_processed and files_failed
    """
    # If catalog is not provided, create one from catalog_id
    if catalog is None and catalog_id is not None:
        catalog = Catalog.get_by_id(catalog_id)
        if catalog is None:
            raise ValueError(f"Catalog with ID {catalog_id} not found")
    
    # If still no catalog, we need to fail
    if catalog is None:
        raise ValueError("Either catalog or catalog_id must be provided")
    
    # Create results database in target directory (catalog-specific database)
    results_db_path = os.path.join(target_dir, 'workflow_results.db')
    results_conn = sqlite3.connect(results_db_path)
    results_cursor = results_conn.cursor()
    
    # Create results tables with sha256_hash column
    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER,
            file_id INTEGER,
            operation_type TEXT NOT NULL,
            source_path TEXT,
            target_path TEXT,
            file_size INTEGER,
            status TEXT NOT NULL,
            details TEXT,
            operation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES file_contents(id)
        )
    ''')
    
    # Ensure execution_id column exists (for backward compatibility)
    try:
        results_cursor.execute('''
            SELECT execution_id FROM file_operations LIMIT 1
        ''')
    except sqlite3.OperationalError:
        # Column doesn't exist, add it
        results_cursor.execute('''
            ALTER TABLE file_operations ADD COLUMN execution_id INTEGER
        ''')
        results_conn.commit()
    
    # Create file_contents table in results database with sha256_hash, file_id, and timestamp fields
    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size INTEGER,
            file_type TEXT,
            file_permissions TEXT,
            sha256_hash TEXT,
            file_id INTEGER,
            exif_data_id INTEGER,
            modification_time TEXT,
            access_time TEXT,
            change_time TEXT,
            creation_time TEXT,
            master_datetime TEXT,
            master_datetime_source TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (exif_data_id) REFERENCES exif_data(id)
        )
    ''')

    # Migration: add master_datetime columns if not present in existing databases
    for _col in ('master_datetime', 'master_datetime_source'):
        try:
            results_cursor.execute(f'ALTER TABLE file_contents ADD COLUMN {_col} TEXT')
            results_conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists
    
    # Create exif_data table in results database
    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS exif_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            file_name TEXT,
            creation_date TEXT,
            latitude REAL,
            longitude REAL,
            altitude REAL,
            camera_make TEXT,
            camera_model TEXT,
            lens_model TEXT,
            focal_length TEXT,
            aperture TEXT,
            iso TEXT,
            shutter_speed TEXT,
            flash TEXT,
            orientation INTEGER,
            width INTEGER,
            height INTEGER,
            raw_exif_json TEXT,
            geohash TEXT,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES file_contents(id) ON DELETE CASCADE
        )
    ''')

    # Migration: add geohash column if not present in existing databases
    try:
        results_cursor.execute('SELECT geohash FROM exif_data LIMIT 1')
    except Exception:
        try:
            results_cursor.execute('ALTER TABLE exif_data ADD COLUMN geohash TEXT')
            results_conn.commit()
        except Exception:
            pass
    
    # Create alternative_versions table in results database
    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS alternative_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_file_id INTEGER NOT NULL,
            alternative_file_path TEXT NOT NULL,
            alternative_file_name TEXT NOT NULL,
            alternative_file_size INTEGER,
            characteristic TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (original_file_id) REFERENCES file_contents(id) ON DELETE CASCADE
        )
    ''')
    
    # Ensure width and height columns exist (for backward compatibility)
    try:
        results_cursor.execute('''
            SELECT width, height FROM alternative_versions LIMIT 1
        ''')
    except sqlite3.OperationalError:
        # Columns don't exist, add them
        try:
            results_cursor.execute('''
                ALTER TABLE alternative_versions ADD COLUMN width INTEGER
            ''')
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            results_cursor.execute('''
                ALTER TABLE alternative_versions ADD COLUMN height INTEGER
            ''')
        except sqlite3.OperationalError:
            pass  # Column already exists
        results_conn.commit()
    
    results_conn.commit()
    
    processed = 0
    failed = 0
    new_files = 0
    skipped_files = 0
    
    stopped = False
    results_db_path = os.path.join(target_dir, 'workflow_results.db')

    for file_path in files:
        # Top of the iteration: the previous file is committed and reported, so
        # leaving here loses nothing.
        if execution_id and stop_requested(results_db_path, execution_id):
            print(f'\nStop requested - leaving after {processed} of {len(files)} file(s).')
            stopped = True
            break

        target_path = None  # Initialize to avoid unbound variable
        file_id = None  # Initialize file_id for this file
        
        try:
            file_name = os.path.basename(file_path)
            
            # Calculate SHA256 hash of source file FIRST (needed for filename generation)
            sha256_hash = calculate_sha256(file_path)
            if not sha256_hash:
                print(f"Warning: Could not calculate hash for {file_name}, skipping duplicate check")
            
            # Extract EXIF data early if it's an image (needed for date extraction)
            exif_data = None
            if is_image_file(file_path):
                # Use ExifTool for RAW files (CR3, CR2, NEF, etc.)
                if is_raw_file(file_path):
                    exif_data = extract_exif_data_with_exiftool(file_path)
                else:
                    # Use PIL for standard image formats
                    exif_data = extract_exif_data(file_path)
                print(exif_data)
            
            # Generate new filename with format: YYYYMMDDHHmmss-SHA256-original_filename.ext
            # Also get the 14-digit creation timestamp
            new_filename, creation_timestamp = generate_new_filename(file_path, sha256_hash, exif_data)
            
            # Calculate target directory using catalog's calculateDir function with timestamp
            # This returns the full path: target_directory/FileType/YYYY/MM/DD
            calculated_dir = catalog.calculateDir(file_path, creation_timestamp)
            
            # Build the complete target path with the calculated directory and new filename
            target_path = os.path.join(calculated_dir, new_filename)
            
            # Check if file with same hash already exists in catalog (using results database only)
            if sha256_hash:
                results_cursor.execute('''
                    SELECT id, file_path, file_name FROM file_contents
                    WHERE sha256_hash = ?
                    LIMIT 1
                ''', (sha256_hash,))
                existing_file = results_cursor.fetchone()
                
                if existing_file:
                    skipped_files += 1
                    # existing_file is a tuple: (id, file_path, file_name)
                    existing_id, existing_path, existing_name = existing_file
                    print(f"Skipping duplicate file: {file_name} (same as {existing_name})")
                    
                    # Log in results database only
                    results_cursor.execute('''
                        INSERT INTO file_operations
                        (execution_id, file_id, operation_type, source_path, target_path, status, details)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (execution_id, existing_id, 'skip', file_path, target_path, 'skipped',
                          f'Duplicate hash: {sha256_hash}'))
                    results_conn.commit()
                    
                    processed += 1
                    if progress_callback:
                        progress_callback(processed)
                    continue
            
            # Calculate relative target path for catalog (relative to target_dir)
            rel_target_path = os.path.relpath(target_path, target_dir)
            
            # Copy file using catalog's file write strategy
            success = copy_file_with_catalog(file_path, rel_target_path, catalog)
            if not success:
                raise Exception(f"Failed to copy file using catalog strategy")
            new_files += 1
            
            # Get file info
            file_size = os.path.getsize(file_path)
            file_type = os.path.splitext(new_filename)[1].lower()
            
            # Get file permissions
            try:
                file_permissions = oct(os.stat(file_path).st_mode)[-3:]
            except:
                file_permissions = None
            
            # Extract filesystem timestamps
            timestamps = get_file_timestamps(file_path)

            # Resolve master_datetime — single canonical date for this file
            master_dt, master_dt_src = resolve_master_datetime(file_path, exif_data)

            # EXIF data was already extracted earlier (if applicable)
            exif_id = None
            
            # Add file to catalog's local database (workflow_results.db) FIRST to get file_id
            # Store file_path as relative to target_dir so the catalog is portable
            rel_file_path = os.path.relpath(target_path, target_dir)
            results_cursor.execute('''
                INSERT INTO file_contents
                (file_path, file_name, file_size, file_type, file_permissions, sha256_hash, file_id,
                 modification_time, access_time, change_time, creation_time,
                 master_datetime, master_datetime_source)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (rel_file_path, new_filename, file_size, file_type, file_permissions, sha256_hash, None,
                  timestamps['modification_time'], timestamps['access_time'],
                  timestamps['change_time'], timestamps['creation_time'],
                  master_dt, master_dt_src))
            file_id = results_cursor.lastrowid  # This is our unique file_id
            
            # Update the file_id column to match the id (for consistency)
            results_cursor.execute('''
                UPDATE file_contents
                SET file_id = ?
                WHERE id = ?
            ''', (file_id, file_id))
            
            # Store EXIF data in catalog's local database if available, using file_id
            if exif_data:
                geohash_value = compute_geohash(exif_data.get('latitude'), exif_data.get('longitude'))
                results_cursor.execute('''
                    INSERT INTO exif_data
                    (file_id, file_name, creation_date, latitude, longitude,
                     altitude, camera_make, camera_model, lens_model, focal_length,
                     aperture, iso, shutter_speed, flash, orientation, width, height, raw_exif_json,
                     geohash)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (file_id, new_filename,
                      exif_data.get('creation_date'),
                      exif_data.get('latitude'),
                      exif_data.get('longitude'),
                      exif_data.get('altitude'),
                      exif_data.get('camera_make'),
                      exif_data.get('camera_model'),
                      exif_data.get('lens_model'),
                      exif_data.get('focal_length'),
                      exif_data.get('aperture'),
                      exif_data.get('iso'),
                      exif_data.get('shutter_speed'),
                      exif_data.get('flash'),
                      exif_data.get('orientation'),
                      exif_data.get('width'),
                      exif_data.get('height'),
                      exif_data.get('raw_exif_json'),
                      geohash_value))
                exif_id = results_cursor.lastrowid
                
                # Update file record to link to EXIF data
                results_cursor.execute('''
                    UPDATE file_contents
                    SET exif_data_id = ?
                    WHERE id = ?
                ''', (exif_id, file_id))
            
            # Log operation to file_operations table with execution_id and file_id
            results_cursor.execute('''
                INSERT INTO file_operations
                (execution_id, file_id, operation_type, source_path, target_path, file_size, status, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (execution_id, file_id, 'copy', file_path, target_path, file_size, 'success',
                  f'SHA256: {sha256_hash}' if sha256_hash else 'No hash calculated'))
            
            # Create ONE standardized alternate JPG version (always, for all images) with max 1200px width
            # This handles ALL image types: HEIC, CR3, JPG, PNG, etc.
            if is_image_file(target_path) and file_id:
                # Parse creation_date from the 14-digit creation_timestamp (YYYYMMDDHHmmss)
                _creation_date = None
                if creation_timestamp and len(creation_timestamp) == 14:
                    try:
                        from datetime import datetime as _dt
                        _creation_date = _dt.strptime(creation_timestamp[:8], '%Y%m%d')
                    except ValueError:
                        pass
                alternate_info = create_alternate_version(
                    target_path, target_dir, file_id,
                    max_width=1200, creation_date=_creation_date
                )
                
                if alternate_info:
                    # Store relative path from target_directory
                    alt_relative_path = os.path.relpath(alternate_info['path'], target_dir)
                    
                    # Store alternate version in database
                    results_cursor.execute('''
                        INSERT INTO alternative_versions
                        (original_file_id, alternative_file_path, alternative_file_name,
                         alternative_file_size, characteristic, width, height, rung)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (file_id, alt_relative_path, alternate_info['name'],
                          alternate_info['size'], alternate_info['characteristic'],
                          alternate_info['width'], alternate_info['height'], '1200'))
                    
                    print(f"Created standardized alternate version: {alternate_info['name']} ({alternate_info['characteristic']}) - {alternate_info['width']}x{alternate_info['height']}px")

                    # Create the gallery thumbnails from the alt file (see THUMBNAIL_SIZES)
                    for _thumb_size in THUMBNAIL_SIZES:
                        thumb_info = create_thumbnail(
                            alternate_info['path'], alternate_info['name'], target_dir, size=_thumb_size
                        )
                        if thumb_info:
                            thumb_relative_path = os.path.relpath(thumb_info['path'], target_dir)
                            results_cursor.execute('''
                                INSERT INTO alternative_versions
                                (original_file_id, alternative_file_path, alternative_file_name,
                                 alternative_file_size, characteristic, width, height, rung)
                                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            ''', (file_id, thumb_relative_path, thumb_info['name'],
                                  thumb_info['size'], thumb_info['characteristic'],
                                  thumb_info['width'], thumb_info['height'],
                                  f'thumb{_thumb_size}'))
                            print(f"Created thumbnail: {thumb_info['name']} ({thumb_info['characteristic']}) - {thumb_info['width']}x{thumb_info['height']}px")

            # ── Video: a playable proxy and a poster frame ────────────────────
            # See documentation/specs/video-spec.md. create_video_proxy returns
            # {'action': 'passthrough'} with no path when the source already
            # streams in a browser, in which case there is nothing to record and
            # the frontend plays the original. The poster is registered as
            # TYPE THUMBNAIL so every existing thumbnail path works for videos.
            if is_video_file(target_path) and file_id:
                proxy_info = create_video_proxy(
                    target_path, target_dir, creation_date=_creation_date
                )
                if proxy_info and proxy_info.get('path'):
                    proxy_rel_path = os.path.relpath(proxy_info['path'], target_dir)
                    results_cursor.execute('''
                        INSERT INTO alternative_versions
                        (original_file_id, alternative_file_path, alternative_file_name,
                         alternative_file_size, characteristic, width, height, rung)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (file_id, proxy_rel_path, proxy_info['name'],
                          proxy_info['size'], proxy_info['characteristic'],
                          proxy_info['width'], proxy_info['height'], 'video_proxy'))
                    print(f"Created video {proxy_info['action']}: {proxy_info['name']} "
                          f"({proxy_info['width']}x{proxy_info['height']}px, "
                          f"{proxy_info['size'] // 1024} KB)")
                elif proxy_info:
                    print(f"Video already browser-playable, no proxy needed: "
                          f"{os.path.basename(target_path)}")

                poster_info = create_video_poster(
                    target_path, target_dir, creation_date=_creation_date
                )
                if poster_info:
                    poster_rel_path = os.path.relpath(poster_info['path'], target_dir)
                    results_cursor.execute('''
                        INSERT INTO alternative_versions
                        (original_file_id, alternative_file_path, alternative_file_name,
                         alternative_file_size, characteristic, width, height, rung)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (file_id, poster_rel_path, poster_info['name'],
                          poster_info['size'], poster_info['characteristic'],
                          poster_info['width'], poster_info['height'], 'video_poster'))
                    print(f"Created video poster: {poster_info['name']} "
                          f"({poster_info['characteristic']})")
            
        except Exception as e:
            failed += 1
            error_msg = str(e)
            print(f"Error processing {file_path}: {error_msg}")
            
            # Log error to results database (file_id will be None if file creation failed)
            results_cursor.execute('''
                INSERT INTO file_operations
                (execution_id, file_id, operation_type, source_path, target_path, status, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (execution_id, file_id, 'copy', file_path, target_path or '', 'failed', error_msg))
        
        processed += 1
        results_conn.commit()
        
        # Update progress
        if progress_callback:
            progress_callback(processed)
    
    results_conn.close()

    if stopped and execution_id:
        record_stopped(results_db_path, execution_id, processed_files=processed,
                       skipped_files=skipped_files,
                       message=stop_message(processed, len(files)))

    # Return statistics
    return {
        'files_processed': processed - failed,
        'files_failed': failed,
        'new_files': new_files,
        'skipped_files': skipped_files,
        'stopped': stopped,
    }

# ─── Standalone subprocess entry point ────────────────────────────────────────

if __name__ == '__main__':
    """
    When launched as a subprocess by the backend, arguments are:
        --execution-id  <int>   workflow_executions.id
        --source-dir    <path>  source directory to import from
        --target-dir    <path>  catalog target directory (contains workflow_results.db)
        --catalog-id    <int>   catalog id
    """
    import argparse

    parser = argparse.ArgumentParser(description='File copy workflow (subprocess mode)')
    parser.add_argument('--execution-id', type=int, required=True)
    parser.add_argument('--source-dir', type=str, required=True)
    parser.add_argument('--target-dir', type=str, required=True)
    parser.add_argument('--catalog-id', type=int, required=True)
    args = parser.parse_args()

    catalog_db_path = os.path.join(args.target_dir, 'workflow_results.db')

    if not os.path.exists(args.source_dir):
        print(f'ERROR: Source directory does not exist: {args.source_dir}', file=sys.stderr)
        _db_complete(catalog_db_path, args.execution_id, 'failed',
                     f'Source directory does not exist: {args.source_dir}')
        sys.exit(1)

    catalog = Catalog.get_by_id(args.catalog_id)
    if catalog is None:
        print(f'ERROR: Catalog {args.catalog_id} not found', file=sys.stderr)
        _db_complete(catalog_db_path, args.execution_id, 'failed',
                     f'Catalog {args.catalog_id} not found')
        sys.exit(1)

    # Walk source directory to collect files
    source_files = []
    for root, dirs, filenames in os.walk(args.source_dir):
        for filename in filenames:
            source_files.append(os.path.join(root, filename))
    _db_update_progress(catalog_db_path, args.execution_id, 0, len(source_files))

    def subprocess_progress(processed, total=None):
        _db_update_progress(catalog_db_path, args.execution_id,
                            processed, total if total is not None else len(source_files))

    try:
        result = execute(
            source_dir=args.source_dir,
            target_dir=args.target_dir,
            files=source_files,
            progress_callback=subprocess_progress,
            catalog_id=args.catalog_id,
            catalog=catalog,
            execution_id=args.execution_id,
        )
        if isinstance(result, dict) and result.get('stopped'):
            # execute() already wrote 'stopped'; overwriting it with 'completed'
            # would fire the downstream chain on a half-copied catalog.
            print(f'Subprocess execution {args.execution_id} stopped by request.')
        else:
            _db_complete(catalog_db_path, args.execution_id, 'completed',
                         skipped_files=result.get('skipped_files', 0) if isinstance(result, dict) else 0)
            print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)
