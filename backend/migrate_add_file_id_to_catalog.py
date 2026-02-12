#!/usr/bin/env python3
"""
Migration script to add file_id column to file_contents table in catalog databases.

This ensures consistency across all tables where:
- file_contents.id is the primary file_id
- exif_data.file_id references file_contents.id
- file_operations.file_id references file_contents.id

The file_id column in file_contents will store the same value as the id column
for consistency and clarity when working with the database.
"""

import sqlite3
import os
import sys

def migrate_catalog_database(db_path):
    """Add file_id column to file_contents table if it doesn't exist"""
    print(f"\nMigrating catalog database: {db_path}")
    
    if not os.path.exists(db_path):
        print(f"  ⚠️  Database not found: {db_path}")
        return False
    
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        
        # Check if file_id column already exists
        cursor.execute("PRAGMA table_info(file_contents)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'file_id' in columns:
            print(f"  ✓ file_id column already exists in file_contents table")
            conn.close()
            return True
        
        print(f"  → Adding file_id column to file_contents table...")
        
        # Add file_id column (will be NULL for existing rows initially)
        cursor.execute('''
            ALTER TABLE file_contents ADD COLUMN file_id INTEGER
        ''')
        
        # Update file_id to match id for all existing rows
        cursor.execute('''
            UPDATE file_contents SET file_id = id
        ''')
        
        conn.commit()
        
        # Verify the migration
        cursor.execute("SELECT COUNT(*) FROM file_contents WHERE file_id IS NULL")
        null_count = cursor.fetchone()[0]
        
        cursor.execute("SELECT COUNT(*) FROM file_contents")
        total_count = cursor.fetchone()[0]
        
        print(f"  ✓ Migration complete!")
        print(f"    - Total records: {total_count}")
        print(f"    - Records with file_id: {total_count - null_count}")
        print(f"    - Records without file_id: {null_count}")
        
        conn.close()
        return True
        
    except Exception as e:
        print(f"  ✗ Error migrating database: {e}")
        return False

def find_catalog_databases(base_dir='.'):
    """Find all workflow_results.db files in the directory tree"""
    catalog_dbs = []
    
    for root, dirs, files in os.walk(base_dir):
        if 'workflow_results.db' in files:
            db_path = os.path.join(root, 'workflow_results.db')
            catalog_dbs.append(db_path)
    
    return catalog_dbs

def main():
    print("=" * 70)
    print("Catalog Database Migration: Add file_id to file_contents")
    print("=" * 70)
    
    # Get the script directory
    script_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(script_dir)
    
    # Find all catalog databases
    print(f"\nSearching for catalog databases in: {project_root}")
    catalog_dbs = find_catalog_databases(project_root)
    
    if not catalog_dbs:
        print("\n⚠️  No catalog databases found!")
        print("   Looking for workflow_results.db files in catalog target directories.")
        return 1
    
    print(f"\nFound {len(catalog_dbs)} catalog database(s):")
    for db_path in catalog_dbs:
        print(f"  - {db_path}")
    
    # Migrate each database
    print("\n" + "=" * 70)
    print("Starting migration...")
    print("=" * 70)
    
    success_count = 0
    for db_path in catalog_dbs:
        if migrate_catalog_database(db_path):
            success_count += 1
    
    # Summary
    print("\n" + "=" * 70)
    print("Migration Summary")
    print("=" * 70)
    print(f"Total databases: {len(catalog_dbs)}")
    print(f"Successfully migrated: {success_count}")
    print(f"Failed: {len(catalog_dbs) - success_count}")
    
    if success_count == len(catalog_dbs):
        print("\n✓ All catalog databases migrated successfully!")
        return 0
    else:
        print("\n⚠️  Some databases failed to migrate. Check the output above.")
        return 1

if __name__ == '__main__':
    sys.exit(main())

# Made with Bob
