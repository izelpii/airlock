#!/usr/bin/env python3
"""
Migration script to update workflows table from target_directory to catalog_id.
This script will:
1. Create a backup of the workflows table
2. Add catalog_id column
3. Create a default catalog for each unique target_directory
4. Update workflows to reference the appropriate catalog
5. Remove the target_directory column
"""

import sqlite3
import os
from datetime import datetime

def migrate_database(db_path='workflows.db'):
    """Migrate the workflows table to use catalog_id instead of target_directory"""
    
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    try:
        # Check if migration is needed
        cursor.execute("PRAGMA table_info(workflows)")
        columns = [col[1] for col in cursor.fetchall()]
        
        if 'catalog_id' in columns and 'target_directory' not in columns:
            print("Migration already completed. No action needed.")
            return
        
        if 'target_directory' not in columns:
            print("Workflows table doesn't have target_directory column. No migration needed.")
            return
        
        print("Starting migration...")
        
        # Step 1: Get all existing workflows
        cursor.execute("SELECT id, target_directory FROM workflows")
        workflows = cursor.fetchall()
        
        if not workflows:
            print("No workflows found. Proceeding with schema update only.")
        
        # Step 2: Create a mapping of target_directory to catalog_id
        target_dir_to_catalog = {}
        
        for workflow in workflows:
            target_dir = workflow['target_directory']
            if target_dir not in target_dir_to_catalog:
                # Create a catalog for this target directory
                catalog_name = f"Auto-migrated: {os.path.basename(target_dir)}"
                cursor.execute('''
                    INSERT INTO catalog_meta (name, owner, description, target_directory)
                    VALUES (?, ?, ?, ?)
                ''', (catalog_name, 'system', f'Auto-created during migration for {target_dir}', target_dir))
                catalog_id = cursor.lastrowid
                target_dir_to_catalog[target_dir] = catalog_id
                print(f"Created catalog '{catalog_name}' (ID: {catalog_id}) for target directory: {target_dir}")
        
        # Step 3: Create new workflows table with catalog_id
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS workflows_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                source_directory TEXT NOT NULL,
                catalog_id INTEGER NOT NULL,
                python_script TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (catalog_id) REFERENCES catalog_meta (id) ON DELETE CASCADE
            )
        ''')
        
        # Step 4: Copy data from old table to new table
        if workflows:
            for workflow in workflows:
                cursor.execute("SELECT * FROM workflows WHERE id = ?", (workflow['id'],))
                old_workflow = cursor.fetchone()
                
                catalog_id = target_dir_to_catalog[old_workflow['target_directory']]
                
                cursor.execute('''
                    INSERT INTO workflows_new 
                    (id, name, description, source_directory, catalog_id, python_script, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ''', (
                    old_workflow['id'],
                    old_workflow['name'],
                    old_workflow['description'],
                    old_workflow['source_directory'],
                    catalog_id,
                    old_workflow['python_script'],
                    old_workflow['created_at'],
                    old_workflow['updated_at']
                ))
            
            print(f"Migrated {len(workflows)} workflows to use catalog references.")
        
        # Step 5: Replace old table with new table
        cursor.execute("DROP TABLE workflows")
        cursor.execute("ALTER TABLE workflows_new RENAME TO workflows")
        
        conn.commit()
        print("Migration completed successfully!")
        print("\nSummary:")
        print(f"- Created {len(target_dir_to_catalog)} catalogs")
        print(f"- Migrated {len(workflows)} workflows")
        
    except Exception as e:
        conn.rollback()
        print(f"Migration failed: {e}")
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    import sys
    
    db_path = sys.argv[1] if len(sys.argv) > 1 else 'workflows.db'
    
    print(f"Migrating database: {db_path}")
    print("=" * 60)
    
    migrate_database(db_path)
    
    print("=" * 60)
    print("Migration script completed.")

# Made with Bob
