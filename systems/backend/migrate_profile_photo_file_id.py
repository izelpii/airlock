#!/usr/bin/env python3
"""
Migration script to add 'profile_photo_file_id' column to the people table
in all catalog databases. This column references file_contents(id) and replaces
the old profile_photo_path string with a proper catalog asset reference.
"""

import sqlite3
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog
from migration_utils import resolve_catalog_db


def migrate_catalog_database(catalog_db_path):
    """Add profile_photo_file_id column to people table if it doesn't exist"""
    if not os.path.exists(catalog_db_path):
        print(f"  Database not found: {catalog_db_path}")
        return False

    try:
        conn = sqlite3.connect(catalog_db_path)
        cursor = conn.cursor()

        # Check if profile_photo_file_id column already exists
        cursor.execute("PRAGMA table_info(people)")
        columns = [row[1] for row in cursor.fetchall()]

        if 'profile_photo_file_id' in columns:
            print(f"  ✓ Column 'profile_photo_file_id' already exists")
            conn.close()
            return True

        # Add the profile_photo_file_id column referencing file_contents
        cursor.execute('''
            ALTER TABLE people
            ADD COLUMN profile_photo_file_id INTEGER REFERENCES file_contents(id) ON DELETE SET NULL
        ''')

        conn.commit()
        conn.close()
        print(f"  ✓ Successfully added 'profile_photo_file_id' column")
        return True

    except Exception as e:
        print(f"  ✗ Error: {str(e)}")
        return False


def main():
    print("=" * 60)
    print("People Profile Photo File ID Migration")
    print("=" * 60)
    print()

    catalogs = Catalog.get_all()

    if not catalogs:
        print("No catalogs found. Nothing to migrate.")
        return

    print(f"Found {len(catalogs)} catalog(s) to migrate:\n")

    success_count = 0
    failed_count = 0

    for catalog in catalogs:
        print(f"Catalog: {catalog.name} (ID: {catalog.id})")
        print(f"  Target Directory: {catalog.target_directory}")

        catalog_db_path = resolve_catalog_db(catalog)

        if migrate_catalog_database(catalog_db_path):
            success_count += 1
        else:
            failed_count += 1

        print()

    print("=" * 60)
    print(f"Migration Summary:")
    print(f"  ✓ Successful: {success_count}")
    print(f"  ✗ Failed: {failed_count}")
    print("=" * 60)

    if failed_count > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
