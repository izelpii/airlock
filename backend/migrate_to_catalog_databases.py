#!/usr/bin/env python3
"""
Migration script to move execution-related tables from central workflows.db
to catalog-specific workflow_results.db files in each catalog's target directory.
"""

import sqlite3
import os
import sys
from models import Database, CatalogDatabase

def migrate_data():
    """Migrate data from central database to catalog-specific databases"""
    print("Starting migration to catalog-specific databases...")
    
    # Connect to main database
    main_db = Database()
    conn = main_db.get_connection()
    cursor = conn.cursor()
    
    # Get all catalogs
    cursor.execute('SELECT * FROM catalog_meta')
    catalogs = cursor.fetchall()
    
    if not catalogs:
        print("No catalogs found. Nothing to migrate.")
        conn.close()
        return
    
    print(f"Found {len(catalogs)} catalog(s) to migrate")
    
    for catalog in catalogs:
        catalog_id = catalog['id']
        catalog_name = catalog['name']
        target_directory = catalog['target_directory']
        
        print(f"\nMigrating catalog: {catalog_name} (ID: {catalog_id})")
        print(f"  Target directory: {target_directory}")
        
        # Create catalog-specific database
        try:
            catalog_db = CatalogDatabase(target_directory)
            catalog_conn = catalog_db.get_connection()
            catalog_cursor = catalog_conn.cursor()
            
            # Store catalog metadata in catalog-specific database
            catalog_cursor.execute('''
                INSERT OR REPLACE INTO catalog_meta 
                (id, name, owner, description, target_directory, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            ''', (catalog_id, catalog['name'], catalog['owner'], 
                  catalog['description'], catalog['target_directory'],
                  catalog['created_at'], catalog['updated_at']))
            
            # Migrate workflow_executions
            cursor.execute('''
                SELECT we.*, w.name as workflow_name
                FROM workflow_executions we
                JOIN workflows w ON we.workflow_id = w.id
                WHERE w.catalog_id = ?
            ''', (catalog_id,))
            executions = cursor.fetchall()
            
            if executions:
                print(f"  Migrating {len(executions)} workflow execution(s)...")
                for execution in executions:
                    catalog_cursor.execute('''
                        INSERT INTO workflow_executions 
                        (id, workflow_id, workflow_name, status, started_at, completed_at, 
                         total_files, processed_files, error_message)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (execution['id'], execution['workflow_id'], execution['workflow_name'],
                          execution['status'], execution['started_at'], execution['completed_at'],
                          execution['total_files'], execution['processed_files'], 
                          execution['error_message']))
            
            # Migrate file_contents
            cursor.execute('''
                SELECT * FROM file_contents WHERE catalog_id = ?
            ''', (catalog_id,))
            files = cursor.fetchall()
            
            if files:
                print(f"  Migrating {len(files)} file record(s)...")
                for file in files:
                    catalog_cursor.execute('''
                        INSERT INTO file_contents 
                        (id, file_path, file_name, file_size, file_type, 
                         file_permissions, exif_data_id, added_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (file['id'], file['file_path'], file['file_name'],
                          file['file_size'], file['file_type'], file['file_permissions'],
                          file['exif_data_id'], file['added_at']))
            
            # Migrate exif_data
            cursor.execute('''
                SELECT * FROM exif_data WHERE catalog_id = ?
            ''', (catalog_id,))
            exif_records = cursor.fetchall()
            
            if exif_records:
                print(f"  Migrating {len(exif_records)} EXIF record(s)...")
                for exif in exif_records:
                    catalog_cursor.execute('''
                        INSERT INTO exif_data 
                        (id, file_id, file_name, creation_date, latitude, longitude, altitude,
                         camera_make, camera_model, lens_model, focal_length, aperture, iso,
                         shutter_speed, flash, orientation, width, height, raw_exif_json, extracted_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (exif['id'], exif['file_id'], exif['file_name'], exif['creation_date'],
                          exif['latitude'], exif['longitude'], exif['altitude'], exif['camera_make'],
                          exif['camera_model'], exif['lens_model'], exif['focal_length'],
                          exif['aperture'], exif['iso'], exif['shutter_speed'], exif['flash'],
                          exif['orientation'], exif['width'], exif['height'], 
                          exif['raw_exif_json'], exif['extracted_at']))
            
            catalog_conn.commit()
            catalog_conn.close()
            print(f"  ✓ Successfully migrated catalog: {catalog_name}")
            
        except Exception as e:
            print(f"  ✗ Error migrating catalog {catalog_name}: {str(e)}")
            continue
    
    conn.close()
    
    print("\n" + "="*60)
    print("Migration completed!")
    print("="*60)
    print("\nNOTE: The old tables in workflows.db are still present.")
    print("After verifying the migration, you can optionally drop them:")
    print("  - workflow_executions (now in catalog-specific databases)")
    print("  - file_contents (now in catalog-specific databases)")
    print("  - exif_data (now in catalog-specific databases)")
    print("  - catalog_workflow_executions (deprecated - merged into workflow_executions)")
    print("  - file_operations (deprecated)")

def verify_migration():
    """Verify the migration by checking catalog databases"""
    print("\nVerifying migration...")
    
    main_db = Database()
    conn = main_db.get_connection()
    cursor = conn.cursor()
    
    cursor.execute('SELECT * FROM catalog_meta')
    catalogs = cursor.fetchall()
    
    for catalog in catalogs:
        catalog_id = catalog['id']
        catalog_name = catalog['name']
        target_directory = catalog['target_directory']
        
        db_path = os.path.join(target_directory, 'workflow_results.db')
        if not os.path.exists(db_path):
            print(f"  ✗ {catalog_name}: Database not found at {db_path}")
            continue
        
        catalog_db = CatalogDatabase(target_directory)
        catalog_conn = catalog_db.get_connection()
        catalog_cursor = catalog_conn.cursor()
        
        # Count records
        catalog_cursor.execute('SELECT COUNT(*) as count FROM workflow_executions')
        exec_count = catalog_cursor.fetchone()['count']
        
        catalog_cursor.execute('SELECT COUNT(*) as count FROM file_contents')
        file_count = catalog_cursor.fetchone()['count']
        
        catalog_cursor.execute('SELECT COUNT(*) as count FROM exif_data')
        exif_count = catalog_cursor.fetchone()['count']
        
        catalog_conn.close()
        
        print(f"  ✓ {catalog_name}:")
        print(f"      Workflow Executions: {exec_count}")
        print(f"      Files: {file_count}")
        print(f"      EXIF records: {exif_count}")
    
    conn.close()

if __name__ == '__main__':
    try:
        migrate_data()
        verify_migration()
    except Exception as e:
        print(f"\nMigration failed: {str(e)}")
        sys.exit(1)

# Made with Bob
