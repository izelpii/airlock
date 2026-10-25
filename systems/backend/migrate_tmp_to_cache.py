"""
migrate_tmp_to_cache.py
=======================
One-off migration that moves existing derived files from the old flat ``tmp/``
directory to the new structured ``cache/`` directory layout, and updates the
stored relative paths in workflow_results.db accordingly.

New layout
----------
  alternative_versions  : cache/Images/YYYY/MM/DD/<filename>
  people.profile_photo  : cache/<filename>          (no date subdirectory)
  faces                 : cache/faces/<filename>

The year/month/day for alternative versions is derived from the first 8
characters of the filename, which always starts with a YYYYMMDDHHmmss
timestamp (e.g. ``20180416093652-...``).  If the filename does not start with a
recognisable date, the file is moved to ``cache/Images/unknown/`` and a warning
is printed.

Usage
-----
    python backend/migrate_tmp_to_cache.py                  # all catalogs
    python backend/migrate_tmp_to_cache.py --catalog-id 3   # single catalog
    python backend/migrate_tmp_to_cache.py --dry-run        # preview only
"""

import argparse
import os
import shutil
import sqlite3
import sys

# ── path setup ────────────────────────────────────────────────────────────────
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
sys.path.insert(0, _ROOT)

from catalog import Catalog  # noqa: E402


# ── helpers ───────────────────────────────────────────────────────────────────

def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name=?",
        (table,)
    ).fetchone()
    return bool(row and row[0])


def _column_exists(conn: sqlite3.Connection, table: str, column: str) -> bool:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return any(r[1] == column for r in rows)


def _resolve_path(stored: str, target_dir: str) -> str:
    """Return an absolute path for a stored value that may be relative."""
    if stored and not os.path.isabs(stored):
        return os.path.join(target_dir, stored)
    return stored


def _date_from_filename(filename: str):
    """
    Extract (year, month, day) strings from a filename that starts with
    a 14-digit YYYYMMDDHHmmss timestamp.  Returns (None, None, None) on failure.
    """
    base = os.path.basename(filename)
    if len(base) >= 8:
        prefix = base[:8]
        if prefix.isdigit():
            y, m, d = prefix[:4], prefix[4:6], prefix[6:8]
            # Sanity-check the values are plausible dates
            if 1970 <= int(y) <= 2100 and 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
                return y, m, d
    return None, None, None


def _move(src: str, dst: str, dry_run: bool) -> bool:
    """Move src → dst, creating parent directories as needed.  Returns True on success."""
    if dry_run:
        print(f"      [dry-run] move  {src}")
        print(f"                  →   {dst}")
        return True
    try:
        os.makedirs(os.path.dirname(dst), exist_ok=True)
        shutil.move(src, dst)
        return True
    except Exception as exc:
        print(f"      [ERROR] Could not move {src}: {exc}")
        return False


# ── per-catalog migration ─────────────────────────────────────────────────────

def migrate_catalog(catalog, dry_run: bool = False) -> dict:
    """
    Migrate all tmp/ files for *catalog* to the new cache/ layout.
    Returns a summary dict with counts.
    """
    target_dir = catalog.target_directory
    if not target_dir:
        print(f"  [skip] Catalog '{catalog.name}' has no target_directory.")
        return {}

    db_path = os.path.join(target_dir, 'workflow_results.db')
    if not os.path.exists(db_path):
        print(f"  [skip] No workflow_results.db at {db_path}.")
        return {}

    tmp_dir   = os.path.join(target_dir, 'tmp')
    cache_dir = os.path.join(target_dir, 'cache')

    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA journal_mode=WAL')

    summary = {'alt_moved': 0, 'alt_missing': 0, 'alt_error': 0,
               'profile_moved': 0, 'face_moved': 0, 'faces_ai_moved': 0,
               'location_moved': 0}

    # ── 1. alternative_versions ───────────────────────────────────────────────
    if _table_exists(conn, 'alternative_versions'):
        rows = conn.execute(
            'SELECT id, alternative_file_path FROM alternative_versions'
        ).fetchall()
        print(f"  alternative_versions: {len(rows)} row(s)")

        for row in rows:
            stored = row['alternative_file_path']
            if not stored:
                continue

            old_abs = _resolve_path(stored, target_dir)

            # Skip rows that are already under cache/
            if old_abs.startswith(cache_dir + os.sep):
                continue

            if not os.path.exists(old_abs):
                print(f"    [warn] file missing, skipping: {stored}")
                summary['alt_missing'] += 1
                continue

            filename = os.path.basename(old_abs)
            year, month, day = _date_from_filename(filename)
            if year is None:
                sub = os.path.join(cache_dir, 'Images', 'unknown')
                print(f"    [warn] cannot parse date from '{filename}', placing in cache/Images/unknown/")
            else:
                sub = os.path.join(cache_dir, 'Images', year, month, day)

            new_abs = os.path.join(sub, filename)
            new_rel = os.path.relpath(new_abs, target_dir)

            if _move(old_abs, new_abs, dry_run):
                if not dry_run:
                    conn.execute(
                        'UPDATE alternative_versions SET alternative_file_path=? WHERE id=?',
                        (new_rel, row['id'])
                    )
                summary['alt_moved'] += 1
            else:
                summary['alt_error'] += 1

    # ── 2. people.profile_photo_path ─────────────────────────────────────────
    if _table_exists(conn, 'people') and _column_exists(conn, 'people', 'profile_photo_path'):
        rows = conn.execute(
            "SELECT id, profile_photo_path FROM people WHERE profile_photo_path IS NOT NULL"
        ).fetchall()
        print(f"  people (profile photos): {len(rows)} row(s)")

        for row in rows:
            stored = row['profile_photo_path']
            old_abs = _resolve_path(stored, target_dir)

            if old_abs.startswith(cache_dir + os.sep):
                continue
            if not os.path.exists(old_abs):
                continue

            filename = os.path.basename(old_abs)
            new_abs = os.path.join(cache_dir, filename)
            new_rel = os.path.relpath(new_abs, target_dir)

            if _move(old_abs, new_abs, dry_run):
                if not dry_run:
                    conn.execute(
                        'UPDATE people SET profile_photo_path=? WHERE id=?',
                        (new_rel, row['id'])
                    )
                summary['profile_moved'] += 1

    # ── 3. locations.map_image_path ──────────────────────────────────────────
    if _table_exists(conn, 'locations') and _column_exists(conn, 'locations', 'map_image_path'):
        rows = conn.execute(
            "SELECT id, map_image_path FROM locations WHERE map_image_path IS NOT NULL"
        ).fetchall()
        print(f"  locations (map images): {len(rows)} row(s)")

        for row in rows:
            stored = row['map_image_path']
            old_abs = _resolve_path(stored, target_dir)

            if old_abs.startswith(cache_dir + os.sep):
                continue
            if not os.path.exists(old_abs):
                continue

            filename = os.path.basename(old_abs)
            new_abs = os.path.join(cache_dir, filename)
            new_rel = os.path.relpath(new_abs, target_dir)

            if _move(old_abs, new_abs, dry_run):
                if not dry_run:
                    conn.execute(
                        'UPDATE locations SET map_image_path=? WHERE id=?',
                        (new_rel, row['id'])
                    )
                summary['location_moved'] += 1

    # ── 4. face crops in tmp/faces/ ───────────────────────────────────────────
    old_faces_dir = os.path.join(tmp_dir, 'faces')
    if os.path.isdir(old_faces_dir):
        new_faces_dir = os.path.join(cache_dir, 'faces')
        face_files = [f for f in os.listdir(old_faces_dir)
                      if os.path.isfile(os.path.join(old_faces_dir, f))]
        print(f"  face crops (tmp/faces/): {len(face_files)} file(s)")
        for fname in face_files:
            old_abs = os.path.join(old_faces_dir, fname)
            new_abs = os.path.join(new_faces_dir, fname)
            if _move(old_abs, new_abs, dry_run):
                summary['face_moved'] += 1

    # ── 5. AI face crops in tmp/faces-ai/ ────────────────────────────────────
    # Files stored here are referenced by ai_face_contents.file_path relative
    # to target_dir, so we must update the DB rows too.
    old_faces_ai_dir = os.path.join(tmp_dir, 'faces-ai')
    if os.path.isdir(old_faces_ai_dir):
        new_faces_ai_dir = os.path.join(cache_dir, 'faces-ai')
        faces_ai_files = [f for f in os.listdir(old_faces_ai_dir)
                          if os.path.isfile(os.path.join(old_faces_ai_dir, f))]
        print(f"  AI face crops (tmp/faces-ai/): {len(faces_ai_files)} file(s)")
        for fname in faces_ai_files:
            old_abs = os.path.join(old_faces_ai_dir, fname)
            new_abs = os.path.join(new_faces_ai_dir, fname)
            old_rel = os.path.relpath(old_abs, target_dir)
            new_rel = os.path.relpath(new_abs, target_dir)
            if _move(old_abs, new_abs, dry_run):
                if not dry_run and _table_exists(conn, 'ai_face_contents'):
                    conn.execute(
                        'UPDATE ai_face_contents SET file_path=? WHERE file_path=?',
                        (new_rel, old_rel)
                    )
                summary['faces_ai_moved'] += 1

    # ── commit and tidy up ────────────────────────────────────────────────────
    if not dry_run:
        conn.commit()

        # Remove tmp/ if it is now empty (best-effort)
        if os.path.isdir(tmp_dir):
            for dirpath, dirnames, filenames in os.walk(tmp_dir, topdown=False):
                if not os.listdir(dirpath):
                    try:
                        os.rmdir(dirpath)
                    except OSError:
                        pass

    conn.close()
    return summary


# ── entry point ───────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='Migrate alternative versions from tmp/ to cache/ directory layout'
    )
    parser.add_argument(
        '--catalog-id', type=int, default=None,
        help='Migrate a single catalog by ID (default: all catalogs)'
    )
    parser.add_argument(
        '--dry-run', action='store_true',
        help='Print what would change without modifying files or the database'
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

    total = {'alt_moved': 0, 'alt_missing': 0, 'alt_error': 0,
             'profile_moved': 0, 'face_moved': 0, 'faces_ai_moved': 0,
             'location_moved': 0}

    for cat in catalogs:
        print(f"\nCatalog '{cat.name}' (id={cat.id}), target={cat.target_directory}")
        result = migrate_catalog(cat, dry_run=args.dry_run)
        for k in total:
            total[k] += result.get(k, 0)

    action = 'Would move' if args.dry_run else 'Moved'
    print(f"\n{action}: {total['alt_moved']} alternative version(s), "
          f"{total['profile_moved']} profile photo(s), "
          f"{total['face_moved']} face crop(s), "
          f"{total['faces_ai_moved']} AI face crop(s), "
          f"{total['location_moved']} location map(s).")
    if total['alt_missing']:
        print(f"  Skipped (file missing): {total['alt_missing']}")
    if total['alt_error']:
        print(f"  Errors: {total['alt_error']}")


if __name__ == '__main__':
    main()
