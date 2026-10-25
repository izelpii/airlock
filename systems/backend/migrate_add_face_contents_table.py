#!/usr/bin/env python3
"""
Migration script to add face_contents table to existing catalog databases.
This table stores extracted face images from photos for face tagging purposes.
"""
import os
import sys
import sqlite3

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import Catalog
from migration_utils import resolve_catalog_db


def migrate_catalog_db(db_path: str):
    """Add face_contents table to a catalog database if it doesn't exist."""
    print(f"  Migrating: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Add face_contents table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS face_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size INTEGER,
            sha256_hash TEXT,
            year INTEGER,
            month INTEGER,
            day INTEGER,
            person_id INTEGER NOT NULL,
            face_detection_id INTEGER NOT NULL,
            source_file_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (person_id) REFERENCES people (id) ON DELETE CASCADE,
            FOREIGN KEY (face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
            FOREIGN KEY (source_file_id) REFERENCES file_contents (id) ON DELETE CASCADE
        )
    ''')

    # Create indexes for better query performance
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_face_contents_person_id 
        ON face_contents(person_id)
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_face_contents_face_detection_id 
        ON face_contents(face_detection_id)
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_face_contents_source_file_id 
        ON face_contents(source_file_id)
    ''')
    
    cursor.execute('''
        CREATE INDEX IF NOT EXISTS idx_face_contents_date 
        ON face_contents(year, month, day)
    ''')

    conn.commit()
    conn.close()
    print(f"  Done: {db_path}")


def main():
    print("Starting face_contents table migration...")

    # Get all catalogs from the catalog registry
    catalogs = Catalog.get_all()
    if not catalogs:
        print("No catalogs found.")
        return

    migrated = 0
    for catalog in catalogs:
        if not catalog.target_directory:
            print(f"  Skipping catalog '{catalog.name}' (no target directory)")
            continue

        db_path = resolve_catalog_db(catalog)
        if not os.path.exists(db_path):
            print(f"  Skipping catalog '{catalog.name}' (database not found at {db_path})")
            continue

        print(f"Catalog: {catalog.name}")
        migrate_catalog_db(db_path)
        migrated += 1

    print(f"\nMigration complete. Migrated {migrated} catalog(s).")


if __name__ == '__main__':
    main()
