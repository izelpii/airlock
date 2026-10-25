#!/usr/bin/env python3
"""
Migration: add `embedding` column to ai_face_contents in all catalog databases.

Safe to run multiple times — ALTER TABLE ADD COLUMN is a no-op when the column
already exists (the OperationalError is caught and ignored).

Usage:
    python backend/migrate_add_ai_face_embedding.py
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog
from migration_utils import resolve_catalog_db


def add_embedding_column(catalog_db_path: str) -> bool:
    if not os.path.exists(catalog_db_path):
        print(f'  Database not found: {catalog_db_path}')
        return False
    try:
        conn = sqlite3.connect(catalog_db_path)
        try:
            conn.execute('ALTER TABLE ai_face_contents ADD COLUMN embedding TEXT')
            conn.commit()
            print('  ✓ Column added')
        except Exception as e:
            if 'duplicate column' in str(e).lower():
                print('  ✓ Column already exists — skipping')
            else:
                raise
        conn.close()
        return True
    except Exception as e:
        print(f'  ✗ Error: {e}')
        return False


def main():
    print('=' * 60)
    print('Migration: add embedding column to ai_face_contents')
    print('=' * 60)

    catalogs = Catalog.get_all()
    if not catalogs:
        print('No catalogs found. Nothing to migrate.')
        return

    success = failed = 0
    for catalog in catalogs:
        print(f"\nCatalog: {catalog.name} (ID: {catalog.id})")
        db_path = resolve_catalog_db(catalog)
        if add_embedding_column(db_path):
            success += 1
        else:
            failed += 1

    print(f'\n{"=" * 60}')
    print(f'Summary: {success} succeeded, {failed} failed')
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
