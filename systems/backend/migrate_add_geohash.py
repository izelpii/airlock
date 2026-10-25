"""
Migration: add geohash column to exif_data and back-fill it for all rows
that already have latitude + longitude.

Usage:
    python backend/migrate_add_geohash.py

The script is safe to run multiple times (idempotent).
"""
import os
import sqlite3
import sys

# Allow the script to be run from the repo root or from inside backend/.
# This directory must be on the path, not its parent: models.py imports its
# siblings (face_recognition_schema) by bare name, so reaching it as
# "backend.models" resolves the package but then fails on those imports.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

try:
    import pygeohash as pgh
except ImportError:
    print("ERROR: pygeohash is not installed. Run: pip install pygeohash>=1.2.0")
    sys.exit(1)

from models import Database


def compute_geohash(lat, lon, precision=10):
    if lat is None or lon is None:
        return None
    try:
        return pgh.encode(float(lat), float(lon), precision=precision)
    except Exception as e:
        print(f"  Warning: could not compute geohash for ({lat}, {lon}): {e}")
        return None


def migrate_catalog_db(db_path: str):
    """Add geohash column and back-fill existing rows in one catalog DB."""
    print(f"  Migrating {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()

    # 1. Add column if missing
    cursor.execute("PRAGMA table_info(exif_data)")
    cols = {row["name"] for row in cursor.fetchall()}
    if "geohash" not in cols:
        cursor.execute("ALTER TABLE exif_data ADD COLUMN geohash TEXT")
        conn.commit()
        print("    Added geohash column.")
    else:
        print("    geohash column already exists.")

    # 2. Back-fill rows that have coordinates but no geohash yet
    cursor.execute("""
        SELECT id, latitude, longitude
        FROM exif_data
        WHERE latitude IS NOT NULL
          AND longitude IS NOT NULL
          AND (geohash IS NULL OR geohash = '')
    """)
    rows = cursor.fetchall()
    updated = 0
    for row in rows:
        gh = compute_geohash(row["latitude"], row["longitude"])
        if gh:
            cursor.execute("UPDATE exif_data SET geohash = ? WHERE id = ?", (gh, row["id"]))
            updated += 1
    conn.commit()
    conn.close()
    print(f"    Back-filled {updated} row(s).")


def main():
    main_db = Database()
    conn = main_db.get_connection()
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    cursor.execute("SELECT id, target_directory FROM catalogs WHERE target_directory IS NOT NULL")
    catalogs = cursor.fetchall()
    conn.close()

    if not catalogs:
        print("No catalogs found.")
        return

    for cat in catalogs:
        db_path = os.path.join(cat["target_directory"], "workflow_results.db")
        if os.path.exists(db_path):
            migrate_catalog_db(db_path)
        else:
            print(f"  Skipping catalog {cat['id']}: DB not found at {db_path}")

    print("Migration complete.")


if __name__ == "__main__":
    main()
