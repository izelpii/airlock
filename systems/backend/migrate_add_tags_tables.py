#!/usr/bin/env python3
"""
Migration script to add tags and tag_person tables to existing catalog databases.
"""
import os
import sys
import sqlite3
import glob

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import Catalog
from migration_utils import resolve_catalog_db


def migrate_catalog_db(db_path: str):
    """Add tags and tag_person tables to a catalog database if they don't exist."""
    print(f"  Migrating: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Add tags table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tags (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )
    ''')

    # Add tag_person table
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS tag_person (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            tag_id INTEGER NOT NULL,
            person_id INTEGER NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE,
            FOREIGN KEY (person_id) REFERENCES people (id) ON DELETE CASCADE,
            UNIQUE (tag_id, person_id)
        )
    ''')

    conn.commit()
    conn.close()
    print(f"  Done: {db_path}")


def main():
    print("Starting tags migration...")

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
