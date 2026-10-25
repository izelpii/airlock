"""
Migration script to add width and height columns to alternative_versions table.
This ensures compatibility with the updated schema that stores dimensions of alternative versions.
"""

import sqlite3
import os
import sys

def migrate_catalog_database(db_path):
    """Add width and height columns to alternative_versions table if they don't exist"""
    print(f"Migrating database: {db_path}")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Check if columns already exist
        cursor.execute("PRAGMA table_info(alternative_versions)")
        columns = [row[1] for row in cursor.fetchall()]
        
        needs_width = 'width' not in columns
        needs_height = 'height' not in columns
        
        if needs_width:
            print("  Adding 'width' column...")
            cursor.execute("ALTER TABLE alternative_versions ADD COLUMN width INTEGER")
            conn.commit()
            print("  ✓ Added 'width' column")
        else:
            print("  'width' column already exists")
        
        if needs_height:
            print("  Adding 'height' column...")
            cursor.execute("ALTER TABLE alternative_versions ADD COLUMN height INTEGER")
            conn.commit()
            print("  ✓ Added 'height' column")
        else:
            print("  'height' column already exists")
        
        if not needs_width and not needs_height:
            print("  Database already up to date")
        else:
            print("  Migration completed successfully")
        
        return True
        
    except Exception as e:
        print(f"  ERROR: Migration failed: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()


def find_catalog_databases(base_dir):
    """Find all workflow_results.db files in the directory tree"""
    db_files = []
    
    for root, dirs, files in os.walk(base_dir):
        if 'workflow_results.db' in files:
            db_path = os.path.join(root, 'workflow_results.db')
            db_files.append(db_path)
    
    return db_files


if __name__ == '__main__':
    # Get the base directory to search (default to 'out' directory)
    if len(sys.argv) > 1:
        base_dir = sys.argv[1]
    else:
        base_dir = os.path.join(os.path.dirname(__file__), '..', 'out')
    
    if not os.path.exists(base_dir):
        print(f"Directory not found: {base_dir}")
        sys.exit(1)
    
    print(f"Searching for catalog databases in: {base_dir}")
    print("=" * 60)
    
    db_files = find_catalog_databases(base_dir)
    
    if not db_files:
        print("No catalog databases found")
        sys.exit(0)
    
    print(f"Found {len(db_files)} catalog database(s)\n")
    
    success_count = 0
    fail_count = 0
    
    for db_path in db_files:
        if migrate_catalog_database(db_path):
            success_count += 1
        else:
            fail_count += 1
        print()
    
    print("=" * 60)
    print(f"Migration summary:")
    print(f"  Successfully migrated: {success_count}")
    print(f"  Failed: {fail_count}")
    print("=" * 60)
