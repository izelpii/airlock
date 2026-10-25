#!/usr/bin/env python3
"""
Migration script to add the face_similarity_scores_audit table to all existing catalog databases.
Safe to run multiple times — uses CREATE TABLE IF NOT EXISTS.
"""

import os
import sqlite3
import sys

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog
from migration_utils import resolve_catalog_db


def migrate_catalog_database(catalog_db_path):
    """Add face_similarity_scores_audit table if it doesn't exist."""
    if not os.path.exists(catalog_db_path):
        print(f"  Database not found: {catalog_db_path}")
        return False

    try:
        conn = sqlite3.connect(catalog_db_path)
        cursor = conn.cursor()

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS face_similarity_scores_audit (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id INTEGER,
                rule_name TEXT NOT NULL,
                action_type TEXT NOT NULL,
                source_face_detection_id INTEGER NOT NULL,
                face_similarity_score_id INTEGER,
                candidate_person_id INTEGER,
                reference_face_detection_id INTEGER,
                source_file_id INTEGER,
                source_event_id INTEGER,
                previous_distance REAL,
                new_distance REAL,
                reason TEXT NOT NULL,
                metadata TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (execution_id) REFERENCES workflow_executions (id) ON DELETE SET NULL,
                FOREIGN KEY (source_face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
                FOREIGN KEY (face_similarity_score_id) REFERENCES face_similarity_scores (id) ON DELETE SET NULL,
                FOREIGN KEY (candidate_person_id) REFERENCES people (id) ON DELETE SET NULL,
                FOREIGN KEY (reference_face_detection_id) REFERENCES face_detections (id) ON DELETE SET NULL,
                FOREIGN KEY (source_file_id) REFERENCES file_contents (id) ON DELETE SET NULL,
                FOREIGN KEY (source_event_id) REFERENCES events (id) ON DELETE SET NULL
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fssa_source_face
            ON face_similarity_scores_audit(source_face_detection_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fssa_execution
            ON face_similarity_scores_audit(execution_id)
        ''')

        conn.commit()
        conn.close()
        print('  ✓ face_similarity_scores_audit table ready')
        return True

    except Exception as e:
        print(f"  ✗ Error: {str(e)}")
        return False


def main():
    print('=' * 60)
    print('Face Similarity Scores Audit Table Migration')
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
        print(f"  Target Directory: {catalog.target_directory}")

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
