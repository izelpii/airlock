"""
Migration: move workflow rows from database/workflows.db → per-catalog workflow_results.db

Run once after deploying the catalog-scoped workflow change:
    python backend/migrate_workflows_to_catalog_db.py
"""
import os
import sys
import sqlite3

# Resolve project root so imports work when run from any directory
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

GLOBAL_DB = os.path.join(PROJECT_ROOT, 'database', 'workflows.db')


def main():
    if not os.path.exists(GLOBAL_DB):
        print(f'Global DB not found at {GLOBAL_DB} — nothing to migrate.')
        return

    src = sqlite3.connect(GLOBAL_DB)
    src.row_factory = sqlite3.Row

    # Check the old table still exists
    tables = {r[0] for r in src.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'workflows' not in tables:
        print('workflows table not found in global DB — already migrated or never existed.')
        src.close()
        return

    rows = src.execute('SELECT * FROM workflows').fetchall()
    print(f'Found {len(rows)} workflow row(s) to migrate.')

    if not rows:
        src.close()
        return

    # Load catalog metadata so we can locate target_directory
    if 'catalog_meta' not in tables:
        print('ERROR: catalog_meta table not found in global DB.')
        src.close()
        return

    catalogs = {r['id']: dict(r) for r in src.execute('SELECT * FROM catalog_meta').fetchall()}
    src.close()

    migrated = 0
    skipped = 0

    for row in rows:
        catalog_id = row['catalog_id']
        catalog = catalogs.get(catalog_id)
        if not catalog:
            print(f'  SKIP workflow id={row["id"]} — catalog_id={catalog_id} not found in catalog_meta')
            skipped += 1
            continue

        target_dir = catalog.get('target_directory', '')
        if not target_dir or not os.path.isdir(target_dir):
            print(f'  SKIP workflow id={row["id"]} — target_directory "{target_dir}" does not exist')
            skipped += 1
            continue

        catalog_db_path = os.path.join(target_dir, 'workflow_results.db')
        dst = sqlite3.connect(catalog_db_path)
        dst.execute('PRAGMA journal_mode=WAL')

        # Ensure workflows table exists (CatalogDatabase.init_db creates it, but the DB
        # might already exist without it if it pre-dates this migration)
        dst.execute('''
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                source_directory TEXT,
                python_script TEXT NOT NULL,
                workflow_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        dst.execute('''
            INSERT INTO workflows (name, description, source_directory, python_script, workflow_type, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (row['name'], row['description'], row['source_directory'],
              row['python_script'], row['workflow_type'],
              row['created_at'], row['updated_at']))
        dst.commit()
        dst.close()
        print(f'  Migrated workflow "{row["name"]}" → {catalog_db_path}')
        migrated += 1

    print(f'\nDone. Migrated: {migrated}, Skipped: {skipped}')
    print('You may now remove the "workflows" table from database/workflows.db if desired.')


if __name__ == '__main__':
    main()
