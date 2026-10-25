#!/usr/bin/env python3
"""
Migration script to add the face_similarity_scores table to all existing catalog databases.
Safe to run multiple times — uses CREATE TABLE IF NOT EXISTS.
"""

import sqlite3
import os
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog
from migration_utils import resolve_catalog_db


def migrate_catalog_database(catalog_db_path):
    """Add face_similarity_scores table if it doesn't exist"""
    if not os.path.exists(catalog_db_path):
        print(f"  Database not found: {catalog_db_path}")
        return False

    try:
        conn = sqlite3.connect(catalog_db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS face_similarity_scores (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                query_face_detection_id INTEGER NOT NULL,
                reference_face_detection_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                distance REAL NOT NULL,
                model TEXT NOT NULL DEFAULT 'VGG-Face',
                distance_metric TEXT NOT NULL DEFAULT 'cosine',
                verified INTEGER NOT NULL DEFAULT 0,
                threshold REAL NOT NULL,
                computed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (query_face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
                FOREIGN KEY (reference_face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
                FOREIGN KEY (person_id) REFERENCES people (id) ON DELETE CASCADE,
                UNIQUE (query_face_detection_id, reference_face_detection_id)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fss_query_face
            ON face_similarity_scores(query_face_detection_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fss_person
            ON face_similarity_scores(person_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fss_distance
            ON face_similarity_scores(distance)
        ''')

        conn.commit()
        conn.close()
        print(f"  ✓ face_similarity_scores table ready")
        return True

    except Exception as e:
        print(f"  ✗ Error: {str(e)}")
        return False


def main():
    print("=" * 60)
    print("Face Similarity Scores Table Migration")
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
