import os
import shutil
import sqlite3
import json
from datetime import datetime
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS

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

def is_image_file(file_path):
    """Check if file is an image based on extension"""
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp', '.heic', '.heif'}
    ext = os.path.splitext(file_path)[1].lower()
    return ext in image_extensions

def execute(source_dir, target_dir, files, progress_callback, catalog_id=None):
    """
    Enhanced workflow: Copy new files from source to target directory,
    extract EXIF data from images, and record in catalog database
    
    Args:
        source_dir: Source directory path
        target_dir: Target directory path (catalog target directory)
        files: List of file paths to process
        progress_callback: Callback function for progress updates
        catalog_id: ID of the catalog (required for database operations)
    
    Returns:
        dict: Statistics about the execution including files_processed and files_failed
    """
    # Connect to main workflow database
    main_db_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'backend', 'workflows.db')
    main_conn = sqlite3.connect(main_db_path)
    main_conn.row_factory = sqlite3.Row
    main_cursor = main_conn.cursor()
    
    # Also create results database in target directory for backward compatibility
    results_db_path = os.path.join(target_dir, 'workflow_results.db')
    results_conn = sqlite3.connect(results_db_path)
    results_cursor = results_conn.cursor()
    
    # Create results tables
    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER,
            source_path TEXT NOT NULL,
            target_path TEXT NOT NULL,
            file_size INTEGER,
            status TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            error_message TEXT,
            FOREIGN KEY (file_id) REFERENCES files(id)
        )
    ''')
    
    # Create files table in results database
    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size INTEGER,
            file_type TEXT,
            file_permissions TEXT,
            exif_data_id INTEGER,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES files(id)
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
            # Calculate relative path
            rel_path = os.path.relpath(file_path, source_dir)
            target_path = os.path.join(target_dir, rel_path)
            file_name = os.path.basename(file_path)
            
            # Check if file already exists in catalog
            if catalog_id:
                main_cursor.execute('''
                    SELECT COUNT(*) as count FROM file_contents
                    WHERE catalog_id = ? AND file_path = ?
                ''', (catalog_id, target_path))
                row = main_cursor.fetchone()
                file_exists = row['count'] > 0
                
                if file_exists:
                    skipped_files += 1
                    print(f"Skipping existing file: {file_name}")
                    processed += 1
                    if progress_callback:
                        progress_callback(processed)
                    continue
            
            # Create target subdirectories if needed
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            
            # Copy file
            shutil.copy2(file_path, target_path)
            new_files += 1
            
            # Get file info
            file_size = os.path.getsize(file_path)
            file_type = os.path.splitext(file_name)[1].lower()
            
            # Get file permissions
            try:
                file_permissions = oct(os.stat(file_path).st_mode)[-3:]
            except:
                file_permissions = None
            
            # Extract EXIF data if it's an image
            exif_data = None
            exif_id = None
            
            if is_image_file(file_path):
                exif_data = extract_exif_data(file_path)
            
            # Add file to catalog's local database (workflow_results.db) FIRST to get file_id
            results_cursor.execute('''
                INSERT INTO files
                (file_path, file_name, file_size, file_type, file_permissions)
                VALUES (?, ?, ?, ?, ?)
            ''', (target_path, file_name, file_size, file_type, file_permissions))
            file_id = results_cursor.lastrowid  # This is our unique file_id
            
            # Add file to catalog database (main workflows.db) using the same file_id concept
            if catalog_id:
                # Add the file record to main catalog database
                main_cursor.execute('''
                    INSERT INTO file_contents
                    (catalog_id, file_path, file_name, file_size, file_type, file_permissions)
                    VALUES (?, ?, ?, ?, ?, ?)
                ''', (catalog_id, target_path, file_name, file_size, file_type, file_permissions))
                main_file_id = main_cursor.lastrowid
                
                # Add operation record to main catalog database with file reference
                main_cursor.execute('''
                    INSERT INTO file_operations
                    (catalog_id, operation_type, file_path, status, details)
                    VALUES (?, ?, ?, ?, ?)
                ''', (catalog_id, 'copy', target_path, 'success',
                      f'Copied from {file_path} (file_id: {file_id})' + (' with EXIF data' if exif_data else '')))
                
                main_conn.commit()
            
            # Store EXIF data in catalog's local database if available, using file_id
            if exif_data:
                results_cursor.execute('''
                    INSERT INTO exif_data
                    (file_id, file_name, creation_date, latitude, longitude,
                     altitude, camera_make, camera_model, lens_model, focal_length,
                     aperture, iso, shutter_speed, flash, orientation, width, height, raw_exif_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', (file_id, file_name,
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
                    UPDATE files
                    SET exif_data_id = ?
                    WHERE id = ?
                ''', (exif_id, file_id))
            
            # Log operation to file_operations table with file_id
            results_cursor.execute('''
                INSERT INTO file_operations
                (file_id, source_path, target_path, file_size, status)
                VALUES (?, ?, ?, ?, ?)
            ''', (file_id, file_path, target_path, file_size, 'success'))
            
        except Exception as e:
            failed += 1
            error_msg = str(e)
            print(f"Error processing {file_path}: {error_msg}")
            
            # Log error to results database (file_id will be None if file creation failed)
            results_cursor.execute('''
                INSERT INTO file_operations
                (file_id, source_path, target_path, status, error_message)
                VALUES (?, ?, ?, ?, ?)
            ''', (file_id, file_path, target_path or '', 'failed', error_msg))
            
            # Log error to catalog database
            if catalog_id:
                main_cursor.execute('''
                    INSERT INTO file_operations
                    (catalog_id, operation_type, file_path, status, details)
                    VALUES (?, ?, ?, ?, ?)
                ''', (catalog_id, 'copy', file_path, 'failed', error_msg))
                main_conn.commit()
        
        processed += 1
        results_conn.commit()
        
        # Update progress
        if progress_callback:
            progress_callback(processed)
    
    main_conn.close()
    results_conn.close()
    
    # Return statistics
    return {
        'files_processed': processed - failed,
        'files_failed': failed,
        'new_files': new_files,
        'skipped_files': skipped_files
    }

# Made with Bob
