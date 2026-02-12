#!/usr/bin/env python3
"""
Migration script to add timestamp fields to file_contents table.

This script adds the following columns to the file_contents table:
- modification_time: File modification time (mtime)
- access_time: File access time (atime)
- change_time: File metadata change time (ctime)
- creation_time: File creation time (birthtime on macOS/Windows)

Usage:
    python backend/migrate_add_timestamps.py
"""

import sqlite3
import os
import sys
from datetime import datetime

def get_file_timestamps(file_path):
    """
    Extract all filesystem timestamps from a file.
    Returns a dictionary with modification_time, access_time, change_time, and creation_time.
    """
    try:
        stat_info = os.stat(file_path)
        
        # Modification time (mtime) - when file content was last modified
        modification_time = datetime.fromtimestamp(stat_info.st_mtime).isoformat()
        
        # Access time (atime) - when file was last accessed
        access_time = datetime.fromtimestamp(stat_info.st_atime).isoformat()
        
        # Change time (ctime) - when file metadata was last changed (Unix) or creation time (Windows)
        change_time = datetime.fromtimestamp(stat_info.st_ctime).isoformat()
        
        # Creation time (birthtime) - available on macOS and Windows
        creation_time = None
        if hasattr(stat_info, 'st_birthtime'):
            creation_time = datetime.fromtimestamp(stat_info.st_birthtime).isoformat()
        else:
            # On Linux, st_birthtime is not available, use ctime as fallback
            creation_time = change_time
        
        return {
            'modification_time': modification_time,
            'access_time': access_time,
            'change_time': change_time,
            'creation_time': creation_time
        }
    except Exception as e:
        print(f"Error extracting timestamps from {file_path}: {e}")
        return {
            'modification_time': None,
            'access_time': None,
            'change_time': None,
            'creation_time': None
        }

def migrate_catalog_database(db_path):
    """Migrate a single catalog database"""
    print(f"\nMigrating: {db_path}")
    
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    
    # Check if columns already exist
    cursor.execute("PRAGMA table_info(file_contents)")
    columns = [row[1] for row in cursor.fetchall()]
    
    timestamp_columns = ['modification_time', 'access_time', 'change_time', 'creation_time']
    columns_to_add = [col for col in timestamp_columns if col not in columns]
    
    if not columns_to_add:
        print("  ✓ Timestamp columns already exist")
        conn.close()
        return
    
    # Add missing columns
    for column in columns_to_add:
        print(f"  Adding column: {column}")
        cursor.execute(f'ALTER TABLE file_contents ADD COLUMN {column} TEXT')
    
    conn.commit()
    
    # Populate timestamp data for existing files
    cursor.execute('SELECT id, file_path FROM file_contents WHERE modification_time IS NULL')
    files = cursor.fetchall()
    
    if files:
        print(f"  Populating timestamps for {len(files)} existing files...")
        updated = 0
        failed = 0
        
        for file_row in files:
            file_id = file_row['id']
            file_path = file_row['file_path']
            
            if os.path.exists(file_path):
                timestamps = get_file_timestamps(file_path)
                cursor.execute('''
                    UPDATE file_contents
                    SET modification_time = ?,
                        access_time = ?,
                        change_time = ?,
                        creation_time = ?
                    WHERE id = ?
                ''', (timestamps['modification_time'],
                      timestamps['access_time'],
                      timestamps['change_time'],
                      timestamps['creation_time'],
                      file_id))
                updated += 1
            else:
                print(f"    Warning: File not found: {file_path}")
                failed += 1
        
        conn.commit()
        print(f"  ✓ Updated {updated} files, {failed} files not found")
    else:
        print("  ✓ No existing files to update")
    
    conn.close()
    print(f"  ✓ Migration complete for {db_path}")

def find_catalog_databases():
    """Find all workflow_results.db files in the current directory tree"""
    databases = []
    
    # Look for workflow_results.db files
    for root, dirs, files in os.walk('.'):
        if 'workflow_results.db' in files:
            db_path = os.path.join(root, 'workflow_results.db')
            databases.append(db_path)
    
    return databases

def main():
    print("=" * 70)
    print("Migration: Add Timestamp Fields to file_contents Table")
    print("=" * 70)
    
    # Find all catalog databases
    databases = find_catalog_databases()
    
    if not databases:
        print("\nNo catalog databases found.")
        print("This is normal if you haven't created any catalogs yet.")
        return
    
    print(f"\nFound {len(databases)} catalog database(s):")
    for db in databases:
        print(f"  - {db}")
    
    # Migrate each database
    for db_path in databases:
        try:
            migrate_catalog_database(db_path)
        except Exception as e:
            print(f"  ✗ Error migrating {db_path}: {e}")
            import traceback
            traceback.print_exc()
    
    print("\n" + "=" * 70)
    print("Migration Complete!")
    print("=" * 70)
    print("\nThe following timestamp fields have been added to file_contents:")
    print("  - modification_time: File modification time (mtime)")
    print("  - access_time: File access time (atime)")
    print("  - change_time: File metadata change time (ctime)")
    print("  - creation_time: File creation time (birthtime)")
    print("\nExisting files have been updated with their current timestamps.")

if __name__ == '__main__':
    main()

# Made with Bob
