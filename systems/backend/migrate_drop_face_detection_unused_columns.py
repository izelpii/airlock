#!/usr/bin/env python3
"""
Migration: drop unused columns `suggested_person_id` and `recognition_confidence`
from the `face_detections` table in all catalog databases.

These columns were never populated — candidates are stored in
face_similarity_scores instead.

Safe to run multiple times — missing columns are silently skipped.

Usage:
    python backend/migrate_drop_face_detection_unused_columns.py
"""

import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog
from migration_utils import resolve_catalog_db

COLUMNS_TO_DROP = ('suggested_person_id', 'recognition_confidence')


def drop_columns(catalog_db_path: str) -> bool:
    if not os.path.exists(catalog_db_path):
        print(f'  Database not found: {catalog_db_path}')
        return False
    try:
        conn = sqlite3.connect(catalog_db_path)
        existing = {col[1] for col in conn.execute('PRAGMA table_info(face_detections)').fetchall()}
        dropped = []
        for col in COLUMNS_TO_DROP:
            if col in existing:
                conn.execute(f'ALTER TABLE face_detections DROP COLUMN {col}')
                dropped.append(col)
        if dropped:
            conn.commit()
            print(f'  ✓ Dropped: {", ".join(dropped)}')
        else:
            print(f'  ✓ Columns already absent — skipping')
        conn.close()
        return True
    except Exception as e:
        print(f'  ✗ Error: {e}')
        return False


def main():
    print('=' * 60)
    print('Migration: drop suggested_person_id, recognition_confidence')
    print('          from face_detections')
    print('=' * 60)

    catalogs = Catalog.get_all()
    if not catalogs:
        print('No catalogs found. Nothing to migrate.')
        return

    success = failed = 0
    for catalog in catalogs:
        print(f"\nCatalog: {catalog.name} (ID: {catalog.id})")
        db_path = resolve_catalog_db(catalog)
        if drop_columns(db_path):
            success += 1
        else:
            failed += 1

    print(f'\n{"=" * 60}')
    print(f'Summary: {success} succeeded, {failed} failed')
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
