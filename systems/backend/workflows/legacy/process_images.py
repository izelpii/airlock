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
    image_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp', '.heic', '.heif', '.cr3'}
    ext = os.path.splitext(file_path)[1].lower()
    return ext in image_extensions

def is_cr3_file(file_path):
    """Check if file is a CR3 (Canon RAW) file"""
    return os.path.splitext(file_path)[1].lower() == '.cr3'

def create_alternate_version(original_path, target_dir, file_id, max_width=1200):
    """
    Create an alternate JPG version of an image, resized to max_width if needed.
    Always converts to JPG format and resizes if image width exceeds max_width.
    Supports HEIC, CR3, and standard image formats.
    
    Args:
        original_path: Path to the original image file
        target_dir: Target directory where alternate version will be saved
        file_id: Database ID of the original file
        max_width: Maximum width in pixels (default 1200)
    
    Returns:
        dict: Information about the alternate version (path, size, dimensions) or None if failed
    """
    try:
        # Import ImageOps for orientation handling
        from PIL import ImageOps
        
        # Register HEIF opener for PIL to handle HEIC files
        try:
            import pillow_heif
            pillow_heif.register_heif_opener()
        except ImportError:
            pass  # pillow_heif not available, HEIC files won't be supported
        
        # Check if this is a CR3 file and handle it specially
        if is_cr3_file(original_path):
            try:
                import rawpy
                # Open CR3 with rawpy and post-process to an 8-bit RGB array
                with rawpy.imread(original_path) as raw:
                    rgb = raw.postprocess(
                        use_camera_wb=True,
                        half_size=False,
                        no_auto_bright=False,
                        output_bps=8,
                    )
                # Convert numpy array to Pillow Image
                img = Image.fromarray(rgb)
                
                # Apply EXIF orientation if present
                oriented_image = ImageOps.exif_transpose(img)
                if oriented_image is not None:
                    img = oriented_image
            except ImportError:
                print(f"rawpy not available, cannot process CR3 file: {original_path}")
                return None
        else:
            # Open the image normally for other formats
            img = Image.open(original_path)
            
            # Apply EXIF orientation correction for all image types
            oriented_image = ImageOps.exif_transpose(img)
            if oriented_image is not None:
                img = oriented_image
        
        # Get original dimensions
        original_width, original_height = img.size
        
        # Determine if resizing is needed
        needs_resize = original_width > max_width
        
        # Calculate new dimensions if resizing
        if needs_resize:
            ratio = max_width / original_width
            new_width = max_width
            new_height = int(original_height * ratio)
        else:
            new_width = original_width
            new_height = original_height
        
        # Convert to RGB if necessary (for PNG with transparency, HEIC, etc.)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create white background
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Resize if needed
        if needs_resize:
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Generate alternate file path in tmp subdirectory
        tmp_dir = os.path.join(target_dir, 'tmp')
        os.makedirs(tmp_dir, exist_ok=True)
        
        original_name = os.path.basename(original_path)
        name_without_ext = os.path.splitext(original_name)[0]
        alternate_name = f"{name_without_ext}_alt.jpg"
        alternate_path = os.path.join(tmp_dir, alternate_name)
        
        # Save as JPG with high quality
        img.save(alternate_path, 'JPEG', quality=90, optimize=True)
        
        # Get file size
        alternate_size = os.path.getsize(alternate_path)
        
        # Build characteristic description
        characteristics = []
        characteristics.append("TYPE JPG")
        if needs_resize:
            characteristics.append(f"TYPE SCALE {max_width}")
        characteristic = " ".join(characteristics)
        
        return {
            'path': alternate_path,
            'name': alternate_name,
            'size': alternate_size,
            'width': new_width,
            'height': new_height,
            'characteristic': characteristic
        }
        
    except Exception as e:
        print(f"Error creating alternate version for {original_path}: {e}")
        return None

def execute(source_dir, target_dir, files, progress_callback, catalog_id=None, catalog=None, execution_id=None):
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
    # Connect to catalog-specific database in target directory
    results_db_path = os.path.join(target_dir, 'workflow_results.db')
    results_conn = sqlite3.connect(results_db_path)
    results_conn.row_factory = sqlite3.Row
    results_cursor = results_conn.cursor()
    
    # Tables are already created by CatalogDatabase init
    # Just ensure we have the execution_id column in file_operations
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
    
    processed = 0
    failed = 0
    new_files = 0
    skipped_files = 0
    
    for file_path in files:
        target_path = None  # Initialize to avoid unbound variable
        file_id = None  # Initialize file_id for this file
        operation_status = 'success'
        operation_type = 'copy'
        
        try:
            # Calculate relative path
            rel_path = os.path.relpath(file_path, source_dir)
            target_path = os.path.join(target_dir, rel_path)
            file_name = os.path.basename(file_path)
            
            # Check if file already exists in catalog database
            results_cursor.execute('''
                SELECT COUNT(*) as count FROM file_contents
                WHERE file_path = ?
            ''', (target_path,))
            row = results_cursor.fetchone()
            file_exists = row['count'] > 0
            
            if file_exists:
                skipped_files += 1
                operation_status = 'skipped'
                operation_type = 'skip'
                print(f"Skipping existing file: {file_name}")
                
                # Log the skip operation
                results_cursor.execute('''
                    INSERT INTO file_operations
                    (execution_id, operation_type, source_path, target_path, file_size, status, details)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (execution_id, operation_type, file_path, target_path,
                      os.path.getsize(file_path), operation_status, 'File already exists in catalog'))
                results_conn.commit()
                
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
            
            # Add file to catalog's database (workflow_results.db)
            results_cursor.execute('''
                INSERT INTO file_contents
                (file_path, file_name, file_size, file_type, file_permissions)
                VALUES (?, ?, ?, ?, ?)
            ''', (target_path, file_name, file_size, file_type, file_permissions))
            file_id = results_cursor.lastrowid
            
            # Store EXIF data in catalog's local database if available, using file_id
            if exif_data:
                # Prepare values tuple, ensuring all values are properly typed
                exif_values = (
                    file_id,
                    file_name,
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
                    exif_data.get('raw_exif_json')
                )
                
                results_cursor.execute('''
                    INSERT INTO exif_data
                    (file_id, file_name, creation_date, latitude, longitude,
                     altitude, camera_make, camera_model, lens_model, focal_length,
                     aperture, iso, shutter_speed, flash, orientation, width, height, raw_exif_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ''', exif_values)
                exif_id = results_cursor.lastrowid
                
                # Update file record to link to EXIF data
                results_cursor.execute('''
                    UPDATE file_contents
                    SET exif_data_id = ?
                    WHERE id = ?
                ''', (exif_id, file_id))
            
            # Create alternate JPG version (always, for all images)
            if is_image_file(target_path) and file_id:
                alternate_info = create_alternate_version(target_path, target_dir, file_id, max_width=1200)
                
                if alternate_info:
                    # Store alternate version in database
                    results_cursor.execute('''
                        INSERT INTO alternative_versions
                        (original_file_id, alternative_file_path, alternative_file_name,
                         alternative_file_size, characteristic, width, height)
                        VALUES (?, ?, ?, ?, ?, ?, ?)
                    ''', (file_id, alternate_info['path'], alternate_info['name'],
                          alternate_info['size'], alternate_info['characteristic'],
                          alternate_info['width'], alternate_info['height']))
                    
                    print(f"Created alternate version: {alternate_info['name']} ({alternate_info['characteristic']})")
            
            # Log operation to file_operations table with execution_id and file_id
            results_cursor.execute('''
                INSERT INTO file_operations
                (execution_id, file_id, operation_type, source_path, target_path, file_size, status, details)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (execution_id, file_id, operation_type, file_path, target_path, file_size, operation_status,
                  'Copied with EXIF data' if exif_data else 'Copied'))
            
        except Exception as e:
            failed += 1
            error_msg = str(e)
            print(f"Error processing {file_path}: {error_msg}")
            
            # Log error to results database with execution_id
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
    
    # Return statistics
    return {
        'files_processed': processed - failed,
        'files_failed': failed,
        'new_files': new_files,
        'skipped_files': skipped_files
    }
