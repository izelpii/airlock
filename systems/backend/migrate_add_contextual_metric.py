#!/usr/bin/env python3
"""
Migration script for the contextual similarity metric.

Adds to every existing catalog database:
  - face_similarity_scores.contextual_distance    (REAL)
  - face_similarity_scores.contextual_evidence    (TEXT, JSON)
  - face_similarity_scores.contextual_computed_at (TIMESTAMP)
  - index idx_fss_contextual
  - table person_cooccurrence + its indexes

See documentation/specs/contextual-similarity-metric-spec.md sections 5.1 and 5.2.
Safe to run multiple times - every statement is IF NOT EXISTS or wrapped.
"""

import argparse
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from catalog import Catalog
from face_recognition_schema import ensure_face_recognition_schema
from migration_utils import resolve_catalog_db




def migrate_catalog_database(catalog_db_path):
    """Apply the face-recognition schema to one catalog database."""
    if not os.path.exists(catalog_db_path):
        print(f'  Database not found: {catalog_db_path}')
        return False

    try:
        changes = ensure_face_recognition_schema(catalog_db_path, verbose=True)
        if not changes:
            print('  . already up to date')
        return True
    except Exception as e:
        print(f'  x Error: {e}')
        return False


def main():
    ap = argparse.ArgumentParser(description='Add the contextual metric schema to catalog databases.')
    ap.add_argument('--db', action='append',
                    help='Migrate this workflow_results.db directly, skipping the '
                         'catalog registry. Repeatable.')
    args = ap.parse_args()

    if args.db:
        ok = sum(1 for d in args.db if (print(f'Database: {d}') or migrate_catalog_database(d)))
        print(f'\nMigrated {ok}/{len(args.db)} database(s).')
        return sys.exit(0 if ok == len(args.db) else 1)

    print('=' * 60)
    print('Contextual Similarity Metric Migration')
    print('Tables: face_similarity_scores, person_cooccurrence')
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
    print(f'  Successful: {success_count}')
    print(f'  Failed:     {failed_count}')
    print('=' * 60)

    if failed_count > 0:
        sys.exit(1)


if __name__ == '__main__':
    main()
