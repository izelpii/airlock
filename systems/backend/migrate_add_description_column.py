#!/usr/bin/env python3
"""
Migration script to add 'description' column to file_contents table in all catalog databases.
This script should be run once to update existing databases.
"""

import sqlite3
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog
from migration_utils import resolve_catalog_db

def migrate_catalog_database(catalog_db_path):
    """Add description column to file_contents table if it doesn't exist"""
    if not os.path.exists(catalog_db_path):
        print(f"  Database not found: {catalog_db_path}")
        return False
    
    try:
        conn = sqlite3.connect(catalog_db_path)
        cursor = conn.cursor()
        
        # Check if description column already exists
        cursor.execute("PRAGMA table_info(file_contents)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'description' in columns:
            print(f"  ✓ Column 'description' already exists")
            conn.close()
            return True
        
        # Add the description column (TEXT type for long text content)
        cursor.execute('''
            ALTER TABLE file_contents 
            ADD COLUMN description TEXT
        ''')
        
        conn.commit()
        conn.close()
        print(f"  ✓ Successfully added 'description' column")
        return True
        
    except Exception as e:
        print(f"  ✗ Error: {str(e)}")
        return False

def main():
    print("=" * 60)
    print("File Contents Description Column Migration")
    print("=" * 60)
    print()
    
    catalogs = Catalog.get_all()
    
    if not catalogs:
        print("No catalogs found. Nothing to migrate.")
        return
    
    print(f"Found {len(catalogs)} catalog(s) to migrate:\n")
    
    success_count = 0
    failed_count = 0
    
    for catalog in catalogs:
        print(f"Catalog: {catalog.name} (ID: {catalog.id})")
        print(f"  Target Directory: {catalog.target_directory}")
        
        catalog_db_path = resolve_catalog_db(catalog)
        
        if migrate_catalog_database(catalog_db_path):
            success_count += 1
        else:
            failed_count += 1
        
        print()
    
    print("=" * 60)
    print(f"Migration Summary:")
    print(f"  ✓ Successful: {success_count}")
    print(f"  ✗ Failed: {failed_count}")
    print("=" * 60)
    
    if failed_count > 0:
        sys.exit(1)

if __name__ == '__main__':
    main()
