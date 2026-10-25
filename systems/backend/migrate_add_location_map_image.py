#!/usr/bin/env python3
"""
Migration script to add map_image_path column to the locations table
in existing catalog databases.
"""
import os
import sys
import sqlite3

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import Catalog
from migration_utils import resolve_catalog_db


def migrate_catalog_db(db_path: str):
    """Add map_image_path column to locations table if it doesn't exist."""
    print(f"  Migrating: {db_path}")
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check if map_image_path column already exists
    cursor.execute("PRAGMA table_info(locations)")
    columns = [row[1] for row in cursor.fetchall()]

    if 'map_image_path' not in columns:
        cursor.execute("ALTER TABLE locations ADD COLUMN map_image_path TEXT")
        conn.commit()
        print(f"  Added map_image_path column to locations table.")
    else:
        print(f"  map_image_path column already exists, skipping.")

    conn.close()
    print(f"  Done: {db_path}")


def main():
    print("Starting location map_image_path migration...")

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
