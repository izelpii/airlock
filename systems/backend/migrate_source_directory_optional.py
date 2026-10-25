#!/usr/bin/env python3
"""
Migration: Make source_directory optional in workflows table.
SQLite does not support ALTER COLUMN, so we recreate the table.
"""
import sqlite3
import os

def migrate():
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(project_root, 'database', 'workflows.db')

    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}. Nothing to migrate.")
        return

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # Check current schema
    cursor.execute("PRAGMA table_info(workflows)")
    columns = cursor.fetchall()
    for col in columns:
        if col['name'] == 'source_directory':
            if col['notnull'] == 0:
                print("source_directory is already nullable. No migration needed.")
                conn.close()
                return
            break

    print("Migrating workflows table: making source_directory nullable...")

    # SQLite doesn't support ALTER COLUMN, so we recreate the table
    cursor.executescript('''
        BEGIN;

        -- Create new table with source_directory as nullable
        CREATE TABLE workflows_new (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL,
            description TEXT,
            source_directory TEXT,
            catalog_id INTEGER NOT NULL,
            python_script TEXT NOT NULL,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (catalog_id) REFERENCES catalog_meta (id) ON DELETE CASCADE
        );

        -- Copy data from old table
        INSERT INTO workflows_new
            (id, name, description, source_directory, catalog_id, python_script, created_at, updated_at)
        SELECT
            id, name, description, source_directory, catalog_id, python_script, created_at, updated_at
        FROM workflows;

        -- Drop old table
        DROP TABLE workflows;

        -- Rename new table
        ALTER TABLE workflows_new RENAME TO workflows;

        COMMIT;
    ''')

    conn.close()
    print("Migration complete: source_directory is now optional in workflows table.")

if __name__ == '__main__':
    migrate()
