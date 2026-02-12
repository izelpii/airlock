#!/usr/bin/env python3
"""
Migration script to extract and store EXIF data for existing files in catalog databases.
This script processes all files in catalog databases that don't have EXIF data yet.
"""

import os
import sqlite3
import sys
from pathlib import Path

# Add parent directory to path to import from workflows
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from workflows.file_copy_workflow import extract_exif_data, is_image_file


def migrate_catalog_exif(catalog_db_path):
    """Extract and store EXIF data for files in a catalog database"""
    
    if not os.path.exists(catalog_db_path):
        print(f"Database not found: {catalog_db_path}")
        return
    
    conn = sqlite3.connect(catalog_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Get all files that don't have EXIF data yet
    cursor.execute('''
        SELECT id, file_path, file_name, file_type
        FROM file_contents
        WHERE exif_data_id IS NULL
    ''')
    
    files = cursor.fetchall()
    total_files = len(files)
    processed = 0
    exif_added = 0
    
    print(f"\nProcessing {total_files} files in {catalog_db_path}")
    
    for file_row in files:
        file_id = file_row['id']
        file_path = file_row['file_path']
        file_name = file_row['file_name']
        file_type = file_row['file_type']
        
        processed += 1
        
        # Check if file is an image
        if not is_image_file(file_path):
            print(f"  [{processed}/{total_files}] Skipping non-image: {file_name}")
            continue
        
        # Check if file exists
        if not os.path.exists(file_path):
            print(f"  [{processed}/{total_files}] File not found: {file_path}")
            continue
        
        # Extract EXIF data
        exif_data = extract_exif_data(file_path)
        
        if not exif_data:
            print(f"  [{processed}/{total_files}] No EXIF data: {file_name}")
            continue
        
        # Insert EXIF data
        cursor.execute('''
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
        
        exif_id = cursor.lastrowid
        
        # Update file record to link to EXIF data
        cursor.execute('''
            UPDATE file_contents
            SET exif_data_id = ?
            WHERE id = ?
        ''', (exif_id, file_id))
        
        exif_added += 1
        print(f"  [{processed}/{total_files}] ✓ Added EXIF data: {file_name}")
    
    conn.commit()
    conn.close()
    
    print(f"\nCompleted: {exif_added} files with EXIF data added out of {total_files} total files")
    return exif_added


def main():
    """Find and process all catalog databases"""
    
    # Get the workflow directory
    workflow_dir = Path(__file__).parent.parent
    
    print("=" * 70)
    print("EXIF Data Migration Script")
    print("=" * 70)
    print(f"Searching for catalog databases in: {workflow_dir}")
    
    # Find all workflow_results.db files
    catalog_dbs = list(workflow_dir.rglob('workflow_results.db'))
    
    if not catalog_dbs:
        print("\nNo catalog databases found!")
        return
    
    print(f"\nFound {len(catalog_dbs)} catalog database(s):")
    for db_path in catalog_dbs:
        print(f"  - {db_path}")
    
    total_exif_added = 0
    
    for db_path in catalog_dbs:
        exif_added = migrate_catalog_exif(str(db_path))
        total_exif_added += exif_added
    
    print("\n" + "=" * 70)
    print(f"Migration complete! Total EXIF records added: {total_exif_added}")
    print("=" * 70)


if __name__ == '__main__':
    main()

# Made with Bob
