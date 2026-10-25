#!/usr/bin/env python3
"""
Migration: add `embedding` column to face_contents in all catalog databases.

Run once against existing catalogs.  Safe to run multiple times — uses
ALTER TABLE ... ADD COLUMN which is a no-op if the column already exists
(handled gracefully via the OperationalError catch below).

After adding the column, the embedding values remain NULL.  They are
backfilled lazily the first time face_recognition_workflow runs, or you
can run the backfill immediately by passing --backfill to this script
(requires the DeepFace backend to be running on port 5001).

Usage:
    python backend/migrate_add_face_embedding.py
    python backend/migrate_add_face_embedding.py --backfill
"""

import json
import os
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog
from migration_utils import resolve_catalog_db

# Was hardcoded. Every other caller of deepface reads DEEPFACE_URL; this one
# did not, so it would have kept dialling a port nobody publishes any more.
DEEPFACE_EMBED_URL = os.environ.get(
    'DEEPFACE_URL', 'http://deepface.airlock.lan') + '/api/embed'


def add_embedding_column(catalog_db_path: str) -> bool:
    """Add `embedding TEXT` column to face_contents if it doesn't already exist."""
    if not os.path.exists(catalog_db_path):
        print(f'  Database not found: {catalog_db_path}')
        return False
    try:
        conn = sqlite3.connect(catalog_db_path)
        try:
            conn.execute('ALTER TABLE face_contents ADD COLUMN embedding TEXT')
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


def backfill_embeddings(catalog_db_path: str) -> tuple:
    """
    For every face_contents row where embedding IS NULL and the file exists,
    call POST /api/embed and store the result.

    Returns (computed, skipped, failed).
    """
    import requests

    if not os.path.exists(catalog_db_path):
        return 0, 0, 0

    conn = sqlite3.connect(catalog_db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute('SELECT id, file_path FROM face_contents WHERE embedding IS NULL')
    rows = cursor.fetchall()

    computed = skipped = failed = 0
    for row in rows:
        face_path = row['file_path']
        if not os.path.exists(face_path):
            print(f'    File not found, skipping: {face_path}')
            skipped += 1
            continue
        try:
            with open(face_path, 'rb') as fh:
                resp = requests.post(
                    DEEPFACE_EMBED_URL,
                    files={'face_image': ('face.jpg', fh, 'image/jpeg')},
                    timeout=30,
                )
            if resp.status_code == 200:
                data = resp.json()
                if data.get('success') and data.get('embedding'):
                    conn.execute(
                        'UPDATE face_contents SET embedding = ? WHERE id = ?',
                        (json.dumps(data['embedding']), row['id'])
                    )
                    conn.commit()
                    computed += 1
                    continue
            print(f'    DeepFace error for id={row["id"]}: {resp.status_code}')
            failed += 1
        except Exception as e:
            print(f'    Error for id={row["id"]}: {e}')
            failed += 1

    conn.close()
    return computed, skipped, failed


def main():
    do_backfill = '--backfill' in sys.argv

    print('=' * 60)
    print('Migration: add embedding column to face_contents')
    if do_backfill:
        print('Backfill mode enabled — will compute missing embeddings')
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
            if do_backfill:
                print('  Backfilling embeddings...')
                computed, skipped, err = backfill_embeddings(db_path)
                print(f'    computed={computed}, skipped={skipped}, failed={err}')
        else:
            failed += 1

    print(f'\n{"=" * 60}')
    print(f'Summary: {success} succeeded, {failed} failed')
    if failed:
        sys.exit(1)


if __name__ == '__main__':
    main()
