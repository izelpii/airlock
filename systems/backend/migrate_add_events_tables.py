"""
Migration: Add events and event_files tables to all existing catalog databases.

Run this script once to upgrade existing workflow_results.db files that were
created before the Events feature was introduced.

Usage:
    cd backend
    python migrate_add_events_tables.py
"""

import os
import sqlite3
import sys

# Allow importing from the project root
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from catalog import Catalog


EVENTS_TABLE_SQL = '''
    CREATE TABLE IF NOT EXISTS events (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        start_time TIMESTAMP NOT NULL,
        end_time TIMESTAMP NOT NULL,
        latitude REAL,
        longitude REAL,
        description TEXT,
        file_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
'''

EVENT_FILES_TABLE_SQL = '''
    CREATE TABLE IF NOT EXISTS event_files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        event_id INTEGER NOT NULL,
        file_id INTEGER NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE CASCADE,
        FOREIGN KEY (file_id) REFERENCES file_contents (id) ON DELETE CASCADE,
        UNIQUE (event_id, file_id)
    )
'''


def migrate_catalog_db(db_path: str) -> bool:
    """
    Add events and event_files tables to a single catalog database.
    Returns True if migration was applied, False if already up-to-date.
    """
    if not os.path.exists(db_path):
        print(f"  [SKIP] Database not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()

    # Check which tables already exist
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table'")
    existing_tables = {row[0] for row in cursor.fetchall()}

    changed = False

    if 'events' not in existing_tables:
        cursor.execute(EVENTS_TABLE_SQL)
        print(f"  [OK] Created 'events' table in {db_path}")
        changed = True
    else:
        print(f"  [--] 'events' table already exists in {db_path}")

    if 'event_files' not in existing_tables:
        cursor.execute(EVENT_FILES_TABLE_SQL)
        print(f"  [OK] Created 'event_files' table in {db_path}")
        changed = True
    else:
        print(f"  [--] 'event_files' table already exists in {db_path}")

    conn.commit()
    conn.close()
    return changed


def main():
    print("=== Migration: Add events and event_files tables ===\n")

    catalogs = Catalog.get_all()

    if not catalogs:
        print("No catalogs found. Nothing to migrate.")
        return

    migrated = 0
    skipped = 0

    for catalog in catalogs:
        target_dir = catalog.target_directory
        name = catalog.name or f"id={catalog.id}"

        if not target_dir:
            print(f"Catalog '{name}': no target_directory set, skipping.")
            skipped += 1
            continue

        db_path = os.path.join(target_dir, 'workflow_results.db')
        print(f"Catalog '{name}' → {db_path}")

        changed = migrate_catalog_db(db_path)
        if changed:
            migrated += 1
        else:
            skipped += 1

    print(f"\nDone. Migrated: {migrated}, Skipped/already up-to-date: {skipped}")


if __name__ == '__main__':
    main()
