"""
migrate_to_relative_paths.py
============================
One-off migration that converts absolute file paths stored in workflow_results.db
to paths relative to the catalog's target_directory.

The only absolute path that should remain in the system is
catalog_meta.target_directory (in database/catalogs.db).  Everything inside
workflow_results.db must be relative so the catalog can be moved or containerised
by updating that single field.

Tables / columns migrated:
    file_contents          → file_path
    alternative_versions   → alternative_file_path
    face_contents          → file_path
    ai_face_contents       → file_path
    people                 → profile_photo_path
    locations              → map_image_path

Columns intentionally left as absolute (audit log, never used to serve files):
    file_operations.source_path
    file_operations.target_path

Usage:
    python backend/migrate_to_relative_paths.py                 # all catalogs
    python backend/migrate_to_relative_paths.py --catalog-id 3  # single catalog
    python backend/migrate_to_relative_paths.py --dry-run       # preview only
"""

import argparse
import os
import sqlite3
import sys

# ── path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from catalog import Catalog  # noqa: E402


# ── tables / columns to migrate ───────────────────────────────────────────────

_MIGRATIONS = [
    ('file_contents',       'file_path'),
    ('alternative_versions','alternative_file_path'),
    ('face_contents',       'file_path'),
    ('ai_face_contents',    'file_path'),
    ('people',              'profile_photo_path'),
    ('locations',           'map_image_path'),
]


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone()
    return bool(row and row[0])


def migrate_catalog(catalog, dry_run: bool = False) -> dict:
    """
    Convert all absolute paths in *catalog*'s workflow_results.db to relative
    paths.  Returns a summary dict: {table_col: rows_updated}.

    Only rows whose stored value starts with ``target_directory`` (i.e. the
    path is already an absolute child of the catalog root) are updated.
    Relative paths are left untouched (idempotent).
    """
    target_dir = catalog.target_directory
    if not target_dir:
        print(f"  [skip] Catalog '{catalog.name}' has no target_directory — skipping.")
        return {}

    db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(db_path):
        print(f"  [skip] No workflow_results.db found at {db_path} — skipping.")
        return {}

    # Normalise target_dir so prefix matching works regardless of trailing slash
    target_prefix = target_dir.rstrip(os.sep) + os.sep

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')

    summary = {}

    for table, column in _MIGRATIONS:
        if not _table_exists(conn, table):
            continue

        # Fetch rows where the stored value looks like an absolute path that
        # lives under this catalog's target_directory.
        rows = conn.execute(
            f"SELECT id, {column} FROM {table} WHERE {column} IS NOT NULL"
        ).fetchall()

        updated = 0
        for row in rows:
            stored = row[column]
            if not stored:
                continue
            # Already relative — nothing to do
            if not os.path.isabs(stored):
                continue
            # Absolute but outside this catalog — leave it alone
            if not stored.startswith(target_prefix):
                continue

            rel = os.path.relpath(stored, target_dir)

            if dry_run:
                print(f"    [dry-run] {table}.{column} id={row['id']}: "
                      f"'{stored}' → '{rel}'")
            else:
                conn.execute(
                    f"UPDATE {table} SET {column}=? WHERE id=?",
                    (rel, row['id'])
                )
            updated += 1

        if updated:
            key = f"{table}.{column}"
            summary[key] = updated
            print(f"    {'[dry-run] ' if dry_run else ''}Updated {updated} row(s) "
                  f"in {table}.{column}")

    if not dry_run:
        conn.commit()

    conn.close()
    return summary


def main():
    parser = argparse.ArgumentParser(
        description='Convert absolute DB paths to relative paths in workflow_results.db'
    )
    parser.add_argument(
        '--catalog-id', type=int, default=None,
        help='Migrate a single catalog by ID (default: all catalogs)'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print what would change without modifying the database'
    )
    args = parser.parse_args()

    if args.catalog_id is not None:
        catalog = Catalog.get_by_id(args.catalog_id)
        if not catalog:
            print(f"Error: catalog with id={args.catalog_id} not found.")
            sys.exit(1)
        catalogs = [catalog]
    else:
        catalogs = Catalog.get_all()

    total_updated = 0
    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id}), target={cat.target_directory}")
        result = migrate_catalog(cat, dry_run=args.dry_run)
        cat_total = sum(result.values())
        total_updated += cat_total
        if not result:
            print("  → Nothing to migrate.")
        else:
            print(f"  → {cat_total} path(s) converted.")

    action = 'Would update' if args.dry_run else 'Updated'
    print(f"\n{action} {total_updated} path(s) across {len(catalogs)} catalog(s).")


if __name__ == '__main__':
    main()
