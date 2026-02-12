#!/usr/bin/env python3
"""
Migration script to add file_id column to file_contents table in catalog databases.
This ensures compatibility with the updated workflow script.
"""

import os
import sqlite3
from pathlib import Path


def migrate_catalog_database(catalog_db_path):
    """Add file_id column to file_contents table if it doesn't exist"""
    
    if not os.path.exists(catalog_db_path):
        print(f"Database not found: {catalog_db_path}")
        return False
    
    conn = sqlite3.connect(catalog_db_path)
    cursor = conn.cursor()
    
    # Check if file_id column already exists
    cursor.execute("PRAGMA table_info(file_contents)")
    columns = [row[1] for row in cursor.fetchall()]
    
    if 'file_id' in columns:
        print(f"  ✓ Column 'file_id' already exists in {catalog_db_path}")
        conn.close()
        return False
    
    print(f"  Adding 'file_id' column to {catalog_db_path}")
    
    try:
        # Add the file_id column
        cursor.execute('''
            ALTER TABLE file_contents
            ADD COLUMN file_id INTEGER
        ''')
        
        # Update existing rows to set file_id = id
        cursor.execute('''
            UPDATE file_contents
            SET file_id = id
        ''')
        
        conn.commit()
        print(f"  ✓ Successfully added 'file_id' column and populated values")
        
        # Verify the migration
        cursor.execute("SELECT COUNT(*) FROM file_contents WHERE file_id IS NOT NULL")
        count = cursor.fetchone()[0]
        print(f"  ✓ Updated {count} existing records")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"  ✗ Error migrating {catalog_db_path}: {e}")
        conn.rollback()
        conn.close()
        return False


def main():
    """Find and migrate all catalog databases"""
    
    # Get the workflow directory
    workflow_dir = Path(__file__).parent.parent
    
    print("=" * 70)
    print("File ID Column Migration Script")
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
    
    print("\nMigrating databases...")
    migrated_count = 0
    
    for db_path in catalog_dbs:
        if migrate_catalog_database(str(db_path)):
            migrated_count += 1
    
    print("\n" + "=" * 70)
    print(f"Migration complete! {migrated_count} database(s) migrated")
    print("=" * 70)


if __name__ == '__main__':
    main()

# Made with Bob
