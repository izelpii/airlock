#!/usr/bin/env python3
"""
Migration script to re-extract EXIF data for HEIC files using ExifTool.

This script finds all HEIC files in the catalog that have missing EXIF data
and re-extracts it using ExifTool, which properly supports HEIC format.
"""

import sqlite3
import os
import sys
import json

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import exiftool
except ImportError:
    print("Error: exiftool module not found. Install with: pip install pyexiftool")
    sys.exit(1)

def extract_exif_data_with_exiftool(file_path):
    """
    Extract EXIF data from a file using ExifTool
    Returns a dictionary with EXIF information or None if extraction fails
    """
    try:
        with exiftool.ExifToolHelper() as et:
            metadata = et.get_metadata(file_path)
            
            if not metadata:
                return None
            
            # ExifTool returns a list with one dict per file
            exif_dict = metadata[0]
            
            # Extract relevant EXIF fields
            result = {}
            
            # Creation date
            creation_date = exif_dict.get('EXIF:DateTimeOriginal') or exif_dict.get('EXIF:CreateDate')
            if creation_date:
                result['creation_date'] = creation_date
            
            # GPS coordinates
            lat = exif_dict.get('EXIF:GPSLatitude')
            lon = exif_dict.get('EXIF:GPSLongitude')
            if lat and lon:
                result['latitude'] = lat
                result['longitude'] = lon
            
            alt = exif_dict.get('EXIF:GPSAltitude')
            if alt:
                result['altitude'] = alt
            
            # Camera info
            make = exif_dict.get('EXIF:Make')
            if make:
                result['camera_make'] = make
            
            model = exif_dict.get('EXIF:Model')
            if model:
                result['camera_model'] = model
            
            lens = exif_dict.get('EXIF:LensModel')
            if lens:
                result['lens_model'] = lens
            
            # Focal length
            fl = exif_dict.get('EXIF:FocalLength')
            if fl:
                # ExifTool returns focal length as string like "70.0 mm"
                if isinstance(fl, str):
                    fl = fl.replace(' mm', '').strip()
                try:
                    result['focal_length'] = str(float(fl))
                except (ValueError, TypeError):
                    result['focal_length'] = str(fl)
            
            # Aperture
            aperture = exif_dict.get('EXIF:FNumber')
            if aperture:
                result['aperture'] = str(aperture)
            
            # ISO
            iso = exif_dict.get('EXIF:ISO')
            if iso:
                result['iso'] = str(iso)
            
            # Shutter speed
            shutter = exif_dict.get('EXIF:ExposureTime')
            if shutter:
                result['shutter_speed'] = str(shutter)
            
            # Flash
            flash = exif_dict.get('EXIF:Flash')
            if flash:
                result['flash'] = str(flash)
            
            # Orientation
            orientation = exif_dict.get('EXIF:Orientation')
            if orientation:
                # Convert orientation string to number
                orientation_map = {
                    'Horizontal (normal)': 1,
                    'Rotate 90 CW': 6,
                    'Rotate 180': 3,
                    'Rotate 270 CW': 8,
                }
                result['orientation'] = orientation_map.get(orientation, 1)
            
            # Image dimensions
            width = exif_dict.get('EXIF:ImageWidth') or exif_dict.get('File:ImageWidth')
            if width:
                result['width'] = int(width)
            
            height = exif_dict.get('EXIF:ImageHeight') or exif_dict.get('File:ImageHeight')
            if height:
                result['height'] = int(height)
            
            # Store raw EXIF as JSON
            result['raw_exif_json'] = json.dumps(exif_dict)
            
            return result
            
    except Exception as e:
        print(f"Error extracting EXIF with ExifTool from {file_path}: {e}")
        return None

def migrate_heic_exif(catalog_db_path, catalog_target_dir):
    """Re-extract EXIF data for HEIC files"""
    
    conn = sqlite3.connect(catalog_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Find all HEIC files that have no EXIF data
    cursor.execute('''
        SELECT fc.id, fc.file_path, fc.file_name
        FROM file_contents fc
        LEFT JOIN exif_data ed ON fc.id = ed.file_id
        WHERE LOWER(fc.file_type) IN ('.heic', '.heif')
          AND ed.id IS NULL
    ''')
    
    heic_files = cursor.fetchall()
    
    if not heic_files:
        print("No HEIC files found without EXIF data.")
        conn.close()
        return
    
    print(f"Found {len(heic_files)} HEIC files without EXIF data.")
    
    updated_count = 0
    failed_count = 0
    
    for row in heic_files:
        file_id = row['id']
        file_path = row['file_path']
        file_name = row['file_name']
        
        # Make path absolute if relative
        if not os.path.isabs(file_path):
            file_path = os.path.join(catalog_target_dir, file_path)
        
        if not os.path.exists(file_path):
            print(f"  ⚠️  File not found: {file_path}")
            failed_count += 1
            continue
        
        print(f"  Processing: {file_name}")
        
        # Extract EXIF data
        exif_data = extract_exif_data_with_exiftool(file_path)
        
        if not exif_data:
            print(f"    ❌ Failed to extract EXIF data")
            failed_count += 1
            continue
        
        # Insert EXIF data
        cursor.execute('''
            INSERT INTO exif_data
            (file_id, file_name, creation_date, latitude, longitude, altitude,
             camera_make, camera_model, lens_model, focal_length, aperture,
             iso, shutter_speed, flash, orientation, width, height)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (
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
            exif_data.get('height')
        ))

        exif_id = cursor.lastrowid

        # The raw dump goes to exif_raw, not into exif_data - see
        # migrate_split_raw_exif.py for why it was taken out of that table.
        if exif_data.get('raw_exif_json'):
            cursor.execute(
                'INSERT OR REPLACE INTO exif_raw (exif_id, raw_exif_json) '
                'VALUES (?, ?)', (exif_id, exif_data.get('raw_exif_json')))
        
        # Update file_contents to link to EXIF data
        cursor.execute('''
            UPDATE file_contents
            SET exif_data_id = ?
            WHERE id = ?
        ''', (exif_id, file_id))
        
        print(f"    ✅ EXIF data extracted and stored (ID: {exif_id})")
        updated_count += 1
    
    conn.commit()
    conn.close()
    
    print(f"\n✅ Migration complete!")
    print(f"   Updated: {updated_count} files")
    print(f"   Failed: {failed_count} files")

if __name__ == '__main__':
    # Default catalog database path
    catalog_db_path = '/Users/izelpi/Development/workflow/out/workflow_results.db'
    catalog_target_dir = '/Users/izelpi/Development/workflow/out'
    
    if len(sys.argv) > 1:
        catalog_db_path = sys.argv[1]
    
    if len(sys.argv) > 2:
        catalog_target_dir = sys.argv[2]
    
    print(f"Catalog database: {catalog_db_path}")
    print(f"Catalog directory: {catalog_target_dir}")
    print()
    
    migrate_heic_exif(catalog_db_path, catalog_target_dir)
