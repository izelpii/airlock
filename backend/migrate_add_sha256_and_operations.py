#!/usr/bin/env python3
"""
Migration script to add SHA256 hash support and file operations tracking
to existing catalog databases.

This script:
1. Adds sha256_hash column to file_contents table
2. Adds skipped_files column to workflow_executions table
3. Creates file_operations table if it doesn't exist
4. Optionally calculates SHA256 hashes for existing files
"""

import sqlite3
import os
import sys
import hashlib
from pathlib import Path

def calculate_sha256(file_path):
    """Calculate SHA256 hash of a file"""
    sha256_hash = hashlib.sha256()
    try:
        with open(file_path, "rb") as f:
            for byte_block in iter(lambda: f.read(4096), b""):
                sha256_hash.update(byte_block)
        return sha256_hash.hexdigest()
    except Exception as e:
        print(f"  Error calculating SHA256 for {file_path}: {e}")
        return None

def migrate_catalog_database(db_path, calculate_hashes=False):
    """Migrate a single catalog database"""
    print(f"\nMigrating: {db_path}")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Check if sha256_hash column exists in file_contents
        cursor.execute("PRAGMA table_info(file_contents)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'sha256_hash' not in columns:
            print("  Adding sha256_hash column to file_contents table...")
            cursor.execute('''
                ALTER TABLE file_contents ADD COLUMN sha256_hash TEXT
            ''')
            conn.commit()
            print("  ✓ Added sha256_hash column")
        else:
            print("  ✓ sha256_hash column already exists")
        
        # Check if skipped_files column exists in workflow_executions
        cursor.execute("PRAGMA table_info(workflow_executions)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'skipped_files' not in columns:
            print("  Adding skipped_files column to workflow_executions table...")
            cursor.execute('''
                ALTER TABLE workflow_executions ADD COLUMN skipped_files INTEGER DEFAULT 0
            ''')
            conn.commit()
            print("  ✓ Added skipped_files column")
        else:
            print("  ✓ skipped_files column already exists")
        
        # Check if file_operations table exists
        cursor.execute('''
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='file_operations'
        ''')
        
        if not cursor.fetchone():
            print("  Creating file_operations table...")
            cursor.execute('''
                CREATE TABLE file_operations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    file_id INTEGER,
                    operation_type TEXT NOT NULL,
                    source_path TEXT,
                    target_path TEXT,
                    file_size INTEGER,
                    status TEXT NOT NULL,
                    details TEXT,
                    operation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (file_id) REFERENCES file_contents (id) ON DELETE SET NULL
                )
            ''')
            conn.commit()
            print("  ✓ Created file_operations table")
        else:
            print("  ✓ file_operations table already exists")
        
        # Optionally calculate hashes for existing files
        if calculate_hashes:
            print("  Calculating SHA256 hashes for existing files...")
            cursor.execute('''
                SELECT id, file_path, sha256_hash FROM file_contents
                WHERE sha256_hash IS NULL
            ''')
            files = cursor.fetchall()
            
            if files:
                updated = 0
                for file_row in files:
                    file_id = file_row['id']
                    file_path = file_row['file_path']
                    
                    if os.path.exists(file_path):
                        sha256_hash = calculate_sha256(file_path)
                        if sha256_hash:
                            cursor.execute('''
                                UPDATE file_contents
                                SET sha256_hash = ?
                                WHERE id = ?
                            ''', (sha256_hash, file_id))
                            updated += 1
                
                conn.commit()
                print(f"  ✓ Calculated hashes for {updated} files")
            else:
                print("  ✓ No files need hash calculation")
        
        print(f"  ✓ Migration completed successfully")
        
    except Exception as e:
        print(f"  ✗ Error during migration: {e}")
        conn.rollback()
        return False
    finally:
        conn.close()
    
    return True

def main():
    """Main migration function"""
    print("=" * 60)
    print("SHA256 Hash and Operations Tracking Migration")
    print("=" * 60)
    
    # Check for --calculate-hashes flag
    calculate_hashes = '--calculate-hashes' in sys.argv
    
    if calculate_hashes:
        print("\nNote: Will calculate SHA256 hashes for existing files")
        print("This may take a while for large catalogs...")
    
    # Get the backend directory
    backend_dir = Path(__file__).parent
    
    # Migrate main workflows database
    main_db = backend_dir / 'workflows.db'
    if main_db.exists():
        print("\n" + "=" * 60)
        print("Migrating main workflows database")
        print("=" * 60)
        migrate_catalog_database(str(main_db), calculate_hashes=False)
    
    # Find all catalog databases
    print("\n" + "=" * 60)
    print("Searching for catalog databases...")
    print("=" * 60)
    
    # Look for workflow_results.db files in the project
    project_root = backend_dir.parent
    catalog_dbs = list(project_root.rglob('workflow_results.db'))
    
    if not catalog_dbs:
        print("\nNo catalog databases found.")
    else:
        print(f"\nFound {len(catalog_dbs)} catalog database(s)")
        
        for db_path in catalog_dbs:
            migrate_catalog_database(str(db_path), calculate_hashes)
    
    print("\n" + "=" * 60)
    print("Migration completed!")
    print("=" * 60)
    
    if not calculate_hashes:
        print("\nTip: Run with --calculate-hashes flag to calculate SHA256")
        print("hashes for existing files (may take time for large catalogs)")

if __name__ == '__main__':
    main()

# Made with Bob
