import os
import shutil
import sqlite3
import json
import hashlib
from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
import stat

def extract_exif_data(file_path):
    """
    Extract EXIF data from an image file
    Returns a dictionary with EXIF information or None if not an image
    """
    try:
        image = Image.open(file_path)
        exif_data = image._getexif()
        
        if not exif_data:
            return None
        
        exif_dict = {}
        gps_info = {}
        
        # Extract basic EXIF data
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            
            # Handle GPS data separately
            if tag == 'GPSInfo':
                for gps_tag_id in value:
                    gps_tag = GPSTAGS.get(gps_tag_id, gps_tag_id)
                    gps_info[gps_tag] = value[gps_tag_id]
            else:
                # Convert value to string if it's not JSON serializable
                try:
                    json.dumps(value)
                    exif_dict[tag] = value
                except (TypeError, ValueError):
                    exif_dict[tag] = str(value)
        
        # Parse GPS coordinates
        latitude = None
        longitude = None
        altitude = None
        
        if gps_info:
            # Parse latitude
            if 'GPSLatitude' in gps_info and 'GPSLatitudeRef' in gps_info:
                lat = gps_info['GPSLatitude']
                lat_ref = gps_info['GPSLatitudeRef']
                latitude = convert_to_degrees(lat)
                if lat_ref == 'S':
                    latitude = -latitude
            
            # Parse longitude
            if 'GPSLongitude' in gps_info and 'GPSLongitudeRef' in gps_info:
                lon = gps_info['GPSLongitude']
                lon_ref = gps_info['GPSLongitudeRef']
                longitude = convert_to_degrees(lon)
                if lon_ref == 'W':
                    longitude = -longitude
            
            # Parse altitude
            if 'GPSAltitude' in gps_info:
                altitude = float(gps_info['GPSAltitude'])
        
        # Extract common fields
        creation_date = exif_dict.get('DateTime') or exif_dict.get('DateTimeOriginal')
        camera_make = exif_dict.get('Make')
        camera_model = exif_dict.get('Model')
        lens_model = exif_dict.get('LensModel')
        
        # Format focal length
        focal_length = None
        if 'FocalLength' in exif_dict:
            fl = exif_dict['FocalLength']
            if isinstance(fl, tuple):
                focal_length = f"{fl[0]/fl[1]:.1f}mm"
            else:
                focal_length = f"{fl}mm"
        
        # Format aperture
        aperture = None
        if 'FNumber' in exif_dict:
            fn = exif_dict['FNumber']
            if isinstance(fn, tuple):
                aperture = f"f/{fn[0]/fn[1]:.1f}"
            else:
                aperture = f"f/{fn}"
        
        # ISO
        iso = str(exif_dict.get('ISOSpeedRatings', ''))
        
        # Shutter speed
        shutter_speed = None
        if 'ExposureTime' in exif_dict:
            et = exif_dict['ExposureTime']
            if isinstance(et, tuple):
                shutter_speed = f"{et[0]}/{et[1]}s"
            else:
                shutter_speed = f"{et}s"
        
        # Flash
        flash = str(exif_dict.get('Flash', ''))
        
        # Orientation
        orientation = exif_dict.get('Orientation')
        
        # Image dimensions
        width = image.width
        height = image.height
        
        return {
            'creation_date': creation_date,
            'latitude': latitude,
            'longitude': longitude,
            'altitude': altitude,
            'camera_make': camera_make,
            'camera_model': camera_model,
            'lens_model': lens_model,
            'focal_length': focal_length,
            'aperture': aperture,
            'iso': iso,
            'shutter_speed': shutter_speed,
            'flash': flash,
            'orientation': orientation,
            'width': width,
            'height': height,
            'raw_exif_json': json.dumps(exif_dict)
        }
        
    except Exception as e:
        print(f"Error extracting EXIF from {file_path}: {e}")
        return None

def convert_to_degrees(value):
    """Convert GPS coordinates to degrees"""
    d = float(value[0])
    m = float(value[1])
    s = float(value[2])
    return d + (m / 60.0) + (s / 3600.0)

def calculate_sha256(file_path):
    """Calculate SHA256 hash of a file"""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            # Read file in chunks to handle large files efficiently
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception as e:
        print(f"Error calculating SHA256 for {file_path}: {e}")
        return None

def is_image_file(file_path):
    """Check if file is an image based on extension"""
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp', '.heic', '.heif'}
    ext = os.path.splitext(file_path)[1].lower()
    return ext in image_extensions

def get_creation_date(file_path, exif_data=None):
    """
    Get creation date from EXIF data or filesystem.
    Returns datetime object.
    
    Priority:
    1. EXIF creation date (for images)
    2. Filesystem creation date (birth time on macOS/Windows, ctime on Linux)
    """
    # Try EXIF data first if available
    if exif_data and exif_data.get('creation_date'):
        exif_date_str = exif_data['creation_date']
        try:
            # EXIF date format is typically "YYYY:MM:DD HH:MM:SS"
            # Parse the EXIF date
            return datetime.strptime(exif_date_str, '%Y:%m:%d %H:%M:%S')
        except (ValueError, TypeError) as e:
            print(f"Warning: Could not parse EXIF date '{exif_date_str}': {e}")
    
    # Fall back to filesystem creation date
    try:
        stat_info = os.stat(file_path)
        # Try to get birth time (creation time) - available on macOS and Windows
        if hasattr(stat_info, 'st_birthtime'):
            return datetime.fromtimestamp(stat_info.st_birthtime)
        else:
            # Fall back to ctime (metadata change time on Unix, creation time on Windows)
            return datetime.fromtimestamp(stat_info.st_ctime)
    except Exception as e:
        print(f"Warning: Could not get filesystem date for {file_path}: {e}")
        # Last resort: use current time
        return datetime.now()

def generate_new_filename(file_path, sha256_hash, exif_data=None):
    """
    Generate new filename with format: YYYYMMDD-SHA256-original_filename.ext
    
    Args:
        file_path: Original file path
        sha256_hash: Full SHA256 hash of the file
        exif_data: Optional EXIF data dictionary
    
    Returns:
        New filename string
    """
    # Get creation date
    creation_date = get_creation_date(file_path, exif_data)
    
    # Format date as YYYYMMDD
    date_str = creation_date.strftime('%Y%m%d')
    
    # Get last 10 characters of SHA256 hash
    hash_suffix = sha256_hash[-10:] if sha256_hash else '0000000000'
    
    # Get original filename with extension
    original_filename = os.path.basename(file_path)
    
    # Construct new filename
    new_filename = f"{date_str}-{hash_suffix}-{original_filename}"
    
    return new_filename

def copy_file_with_attributes(source_path, target_path):
    """
    Copy file preserving ownership and all file attributes.
    
    Args:
        source_path: Source file path
        target_path: Target file path
    """
    # Copy file with metadata (timestamps, permissions)
    shutil.copy2(source_path, target_path)
    
    # Try to preserve ownership (requires appropriate permissions)
    try:
        stat_info = os.stat(source_path)
        os.chown(target_path, stat_info.st_uid, stat_info.st_gid)
    except (OSError, AttributeError) as e:
        # chown may fail if not running as root or on Windows
        print(f"Warning: Could not preserve ownership for {target_path}: {e}")
    
    # Preserve extended attributes if available
    try:
        # This works on macOS and some Linux systems
        if hasattr(os, 'listxattr') and hasattr(os, 'getxattr') and hasattr(os, 'setxattr'):
            for attr in os.listxattr(source_path):
                try:
                    value = os.getxattr(source_path, attr)
                    os.setxattr(target_path, attr, value)
                except OSError:
                    pass  # Some attributes may not be copyable
    except (OSError, AttributeError):
        pass  # Extended attributes not supported on this system

def execute(source_dir, target_dir, files, progress_callback, catalog_id=None):
    """
    Enhanced workflow: Copy new files from source to target directory,
    extract EXIF data from images, and record in catalog database
    
    Args:
        source_dir: Source directory path
        target_dir: Target directory path (catalog target directory)
        files: List of file paths to process
        progress_callback: Callback function for progress updates
        catalog_id: ID of the catalog (not used, kept for compatibility)
    
    Returns:
        dict: Statistics about the execution including files_processed and files_failed
    """
    # Create results database in target directory (catalog-specific database)
    results_db_path = os.path.join(target_dir, 'workflow_results.db')
    results_conn = sqlite3.connect(results_db_path)
    results_cursor = results_conn.cursor()
    
    # Create results tables with sha256_hash column
    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
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
    
    # Create file_contents table in results database with sha256_hash and file_id
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
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (exif_data_id) REFERENCES exif_data(id)
        )
    ''')
    
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
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES file_contents(id) ON DELETE CASCADE
        )
    ''')
    
    results_conn.commit()
    
    processed = 0
    failed = 0
    new_files = 0
    skipped_files = 0
    
    for file_path in files:
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
                exif_data = extract_exif_data(file_path)
                print(exif_data)
            
            # Generate new filename with format: YYYYMMDD-SHA256-original_filename.ext
            new_filename = generate_new_filename(file_path, sha256_hash, exif_data)
            
            # Calculate target path using new filename (preserve directory structure)
            rel_dir = os.path.dirname(os.path.relpath(file_path, source_dir))
            target_subdir = os.path.join(target_dir, rel_dir) if rel_dir != '.' else target_dir
            target_path = os.path.join(target_subdir, new_filename)
            
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
                    existing_file_dict = dict(existing_file)
                    print(f"Skipping duplicate file: {file_name} (same as {existing_file_dict['file_name']})")
                    
                    # Log in results database only
                    results_cursor.execute('''
                        INSERT INTO file_operations
                        (file_id, operation_type, source_path, target_path, status, details)
                        VALUES (?, ?, ?, ?, ?, ?)
                    ''', (existing_file_dict['id'], 'skip', file_path, target_path, 'skipped',
                          f'Duplicate hash: {sha256_hash}'))
                    results_conn.commit()
                    
                    processed += 1
                    if progress_callback:
                        progress_callback(processed)
                    continue
            
            # Create target subdirectories if needed
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            
            # Copy file preserving ownership and all attributes
            copy_file_with_attributes(file_path, target_path)
            new_files += 1
            
            # Get file info
            file_size = os.path.getsize(file_path)
            file_type = os.path.splitext(new_filename)[1].lower()
            
            # Get file permissions
            try:
                file_permissions = oct(os.stat(file_path).st_mode)[-3:]
            except:
                file_permissions = None
            
            # EXIF data was already extracted earlier (if applicable)
            exif_id = None
            
            # Add file to catalog's local database (workflow_results.db) FIRST to get file_id
            # Use new_filename for storage
            results_cursor.execute('''
                INSERT INTO file_contents
                (file_path, file_name, file_size, file_type, file_permissions, sha256_hash, file_id)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (target_path, new_filename, file_size, file_type, file_permissions, sha256_hash, None))
            file_id = results_cursor.lastrowid  # This is our unique file_id
            
            # Update the file_id column to match the id (for consistency)
            results_cursor.execute('''
                UPDATE file_contents
                SET file_id = ?
                WHERE id = ?
            ''', (file_id, file_id))
            
            # Store EXIF data in catalog's local database if available, using file_id
            if exif_data:
                results_cursor.execute('''
                    INSERT INTO exif_data
                    (file_id, file_name, creation_date, latitude, longitude,
                     altitude, camera_make, camera_model, lens_model, focal_length,
                     aperture, iso, shutter_speed, flash, orientation, width, height, raw_exif_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                      exif_data.get('raw_exif_json')))
                exif_id = results_cursor.lastrowid
                
                # Update file record to link to EXIF data
                results_cursor.execute('''
                    UPDATE file_contents
                    SET exif_data_id = ?
                    WHERE id = ?
                ''', (exif_id, file_id))
            
            # Log operation to file_operations table with file_id
            results_cursor.execute('''
                INSERT INTO file_operations
                (file_id, operation_type, source_path, target_path, file_size, status, details)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (file_id, 'copy', file_path, target_path, file_size, 'success',
                  f'SHA256: {sha256_hash}' if sha256_hash else 'No hash calculated'))
            
        except Exception as e:
            failed += 1
            error_msg = str(e)
            print(f"Error processing {file_path}: {error_msg}")
            
            # Log error to results database (file_id will be None if file creation failed)
            results_cursor.execute('''
                INSERT INTO file_operations
                (file_id, operation_type, source_path, target_path, status, details)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (file_id, 'copy', file_path, target_path or '', 'failed', error_msg))
        
        processed += 1
        results_conn.commit()
        
        # Update progress
        if progress_callback:
            progress_callback(processed)
    
    results_conn.close()
    
    # Return statistics
    return {
        'files_processed': processed - failed,
        'files_failed': failed,
        'new_files': new_files,
        'skipped_files': skipped_files
    }

# Made with Bob
