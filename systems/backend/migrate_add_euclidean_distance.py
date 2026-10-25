#!/usr/bin/env python3
"""
Migration script to add euclidean_distance column to face_similarity_scores
in all existing catalog databases.
Safe to run multiple times — ALTER TABLE is wrapped in try/except.
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog
from migration_utils import resolve_catalog_db


def migrate_catalog_database(catalog_db_path):
    """Add euclidean_distance column to face_similarity_scores if not present."""
    if not os.path.exists(catalog_db_path):
        print(f'  Database not found: {catalog_db_path}')
        return False

    try:
        conn = sqlite3.connect(catalog_db_path)
        cursor = conn.cursor()

        try:
            cursor.execute(
                'ALTER TABLE face_similarity_scores ADD COLUMN euclidean_distance REAL'
            )
            conn.commit()
            print('  ✓ euclidean_distance column added')
        except Exception:
            print('  · euclidean_distance column already exists, skipped')

        conn.close()
        return True

    except Exception as e:
        print(f'  ✗ Error: {str(e)}')
        return False


def main():
    print('=' * 60)
    print('Euclidean Distance Column Migration')
    print('Table: face_similarity_scores')
    print('=' * 60)
    print()

    catalogs = Catalog.get_all()

    if not catalogs:
        print('No catalogs found. Nothing to migrate.')
        return

    print(f'Found {len(catalogs)} catalog(s) to migrate:\n')

    success_count = 0
    failed_count = 0

    for catalog in catalogs:
        print(f"Catalog: {catalog.name} (ID: {catalog.id})")
        print(f'  Target Directory: {catalog.target_directory}')

        catalog_db_path = resolve_catalog_db(catalog)

        if migrate_catalog_database(catalog_db_path):
            success_count += 1
        else:
            failed_count += 1

        print()

    print('=' * 60)
    print('Migration Summary:')
    print(f'  ✓ Successful: {success_count}')
    print(f'  ✗ Failed: {failed_count}')
    print('=' * 60)

    if failed_count > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
