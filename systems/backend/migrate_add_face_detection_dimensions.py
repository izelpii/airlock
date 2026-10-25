#!/usr/bin/env python3
"""
Migration script to add 'image_width' and 'image_height' columns to face_detections table in all catalog databases.
These columns store the dimensions of the image that was used for face detection, allowing proper coordinate scaling.
This script should be run once to update existing databases.
"""

import os
import sqlite3
import sys

# Add parent directory to path to import models
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog

def migrate_catalog_database(catalog_db_path):
    """Add image_width and image_height columns to face_detections table if they don't exist"""
    if not os.path.exists(catalog_db_path):
        print(f"  Database not found: {catalog_db_path}")
        return False
    
    try:
        conn = sqlite3.connect(catalog_db_path)
        cursor = conn.cursor()
        
        # Check if columns already exist
        cursor.execute("PRAGMA table_info(face_detections)")
        columns = [row[1] for row in cursor.fetchall()]
        
        needs_image_width = 'image_width' not in columns
        needs_image_height = 'image_height' not in columns
        
        if not needs_image_width and not needs_image_height:
            print(f"  ✓ Columns already exist in {catalog_db_path}")
            conn.close()
            return True
        
        # Add image_width column if needed
        if needs_image_width:
            print(f"  Adding image_width column to {catalog_db_path}")
            cursor.execute('''
                ALTER TABLE face_detections
                ADD COLUMN image_width INTEGER
            ''')
        
        # Add image_height column if needed
        if needs_image_height:
            print(f"  Adding image_height column to {catalog_db_path}")
            cursor.execute('''
                ALTER TABLE face_detections
                ADD COLUMN image_height INTEGER
            ''')
        
        conn.commit()
        conn.close()
        print(f"  ✓ Successfully migrated {catalog_db_path}")
        return True
        
    except Exception as e:
        print(f"  ✗ Error migrating {catalog_db_path}: {str(e)}")
        return False

def main():
    print("=" * 60)
    print("Face Detection Dimensions Migration")
    print("=" * 60)
    print()
    
    catalogs = Catalog.get_all()
    
    if not catalogs:
        print("No catalogs found in database.")
        return
    
    print(f"Found {len(catalogs)} catalog(s) to migrate:\n")
    
    success_count = 0
    for catalog in catalogs:
        catalog_id = catalog.id
        name = catalog.name
        target_dir = catalog.target_directory
        print(f"Catalog: {name} (ID: {catalog_id})")
        catalog_db_path = os.path.join(target_dir, 'catalog.db')
        
        if migrate_catalog_database(catalog_db_path):
            success_count += 1
        print()
    
    print("=" * 60)
    print(f"Migration complete: {success_count}/{len(catalogs)} catalogs migrated successfully")
    print("=" * 60)

if __name__ == '__main__':
    main()
