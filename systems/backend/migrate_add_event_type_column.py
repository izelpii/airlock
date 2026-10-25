"""
Migration: Add 'type' column to events table in all catalog databases.

Run this script once to update existing catalog databases that were created
before the 'type' column was introduced.

Usage:
    python backend/migrate_add_event_type_column.py
"""

import os
import sys
import sqlite3

# Add parent directory to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import Catalog


def migrate_catalog_db(db_path: str) -> bool:
    """
    Add the 'type' column to the events table in a catalog database.
    Returns True if migration was applied, False if already up-to-date.
    """
    if not os.path.exists(db_path):
        print(f"  [SKIP] Database not found: {db_path}")
        return False

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Check if the events table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='events'")
    if not cursor.fetchone():
        print(f"  [SKIP] No events table in: {db_path}")
        conn.close()
        return False

    # Check if 'type' column already exists
    cursor.execute("PRAGMA table_info(events)")
    columns = [row['name'] for row in cursor.fetchall()]
    if 'type' in columns:
        print(f"  [OK]   Column already exists in: {db_path}")
        conn.close()
        return False

    # Add the column
    cursor.execute("ALTER TABLE events ADD COLUMN type TEXT DEFAULT 'Auto'")
    conn.commit()
    conn.close()
    print(f"  [DONE] Added 'type' column to: {db_path}")
    return True


def main():
    print("Migration: add 'type' column to events table")
    print("=" * 50)

    catalogs = Catalog.get_all()
    if not catalogs:
        print("No catalogs found.")
        return

    migrated = 0
    for catalog in catalogs:
        target_dir = getattr(catalog, 'target_directory', None)
        if not target_dir:
            print(f"  [SKIP] Catalog '{catalog.name}' has no target directory.")
            continue

        db_path = os.path.join(target_dir, 'workflow_results.db')
        print(f"Catalog '{catalog.name}': {db_path}")
        if migrate_catalog_db(db_path):
            migrated += 1

    print("=" * 50)
    print(f"Migration complete. {migrated} database(s) updated.")


if __name__ == '__main__':
    main()
