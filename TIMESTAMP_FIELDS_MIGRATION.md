# Timestamp Fields Migration

## Overview
This document describes the addition of filesystem timestamp fields to the file_contents table in the workflow system.

## Changes Made

### 1. New Timestamp Fields
Four new timestamp fields have been added to the `file_contents` table:

- **modification_time**: File modification time (mtime) - when file content was last modified
- **access_time**: File access time (atime) - when file was last accessed  
- **change_time**: File metadata change time (ctime on Unix) or creation time (on Windows)
- **creation_time**: File creation time (birthtime on macOS/Windows, falls back to ctime on Linux)

All timestamps are stored as ISO 8601 formatted strings (e.g., "2026-02-12T01:43:00.123456").

### 2. Modified Files

#### workflows/file_copy_workflow.py
- Added `get_file_timestamps()` function to extract all filesystem timestamps
- Updated `execute()` function to:
  - Extract timestamps for each file being processed
  - Store timestamps in the database when inserting file records
  - Updated CREATE TABLE statement to include the four new timestamp columns

#### backend/models.py
- Updated `CatalogDatabase.init_db()` to include timestamp columns in the file_contents table schema
- Updated `CatalogModel.add_file()` method to accept timestamp parameters
- Updated `CatalogModel.get_files()` query to include timestamp fields in SELECT statement

#### backend/migrate_add_timestamps.py (NEW)
- Created migration script to add timestamp columns to existing databases
- Automatically populates timestamps for existing files by reading their current filesystem metadata
- Handles missing files gracefully

## Database Schema Changes

### Before
```sql
CREATE TABLE file_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER,
    file_type TEXT,
    file_permissions TEXT,
    sha256_hash TEXT,
    file_id INTEGER,
    exif_data_id INTEGER,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (exif_data_id) REFERENCES exif_data(id)
)
```

### After
```sql
CREATE TABLE file_contents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_path TEXT NOT NULL,
    file_name TEXT NOT NULL,
    file_size INTEGER,
    file_type TEXT,
    file_permissions TEXT,
    sha256_hash TEXT,
    file_id INTEGER,
    exif_data_id INTEGER,
    modification_time TEXT,
    access_time TEXT,
    change_time TEXT,
    creation_time TEXT,
    added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (exif_data_id) REFERENCES exif_data(id)
)
```

## Migration Instructions

### For Existing Databases

Run the migration script to add timestamp columns to existing catalog databases:

```bash
python backend/migrate_add_timestamps.py
```

The script will:
1. Find all `workflow_results.db` files in the directory tree
2. Add the four timestamp columns if they don't exist
3. Populate timestamps for existing files by reading their current filesystem metadata
4. Report any files that no longer exist on the filesystem

### For New Installations

No migration is needed. The new schema will be created automatically when:
- A new catalog is created
- A workflow is executed for the first time

## Usage

### In Workflows

Timestamps are automatically extracted and stored when files are processed:

```python
# Extract filesystem timestamps
timestamps = get_file_timestamps(file_path)

# Store in database
results_cursor.execute('''
    INSERT INTO file_contents
    (file_path, file_name, file_size, file_type, file_permissions, sha256_hash, file_id,
     modification_time, access_time, change_time, creation_time)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
''', (target_path, new_filename, file_size, file_type, file_permissions, sha256_hash, None,
      timestamps['modification_time'], timestamps['access_time'], 
      timestamps['change_time'], timestamps['creation_time']))
```

### In API/Backend

Timestamps are included in file records returned by `CatalogModel.get_files()`:

```python
files = catalog_model.get_files(catalog_id)
for file in files:
    print(f"File: {file['file_name']}")
    print(f"  Modified: {file['modification_time']}")
    print(f"  Accessed: {file['access_time']}")
    print(f"  Changed: {file['change_time']}")
    print(f"  Created: {file['creation_time']}")
```

## Platform Differences

### macOS and Windows
- All four timestamp fields are fully supported
- `creation_time` uses the native birthtime attribute

### Linux
- `modification_time`, `access_time`, and `change_time` are fully supported
- `creation_time` falls back to `change_time` (ctime) since Linux doesn't track true creation time

## Benefits

1. **File History Tracking**: Track when files were created, modified, and accessed
2. **Forensics**: Understand file lifecycle and changes over time
3. **Sorting and Filtering**: Enable sorting files by various timestamp criteria
4. **Backup Verification**: Compare timestamps to verify file integrity
5. **Compliance**: Meet requirements for timestamp tracking in regulated environments

## Notes

- Timestamps are captured at the time of file processing, not when the file was originally created
- For existing files, the migration script captures their current filesystem timestamps
- Timestamps are stored in ISO 8601 format for consistency and easy parsing
- All timestamps are in the local timezone of the system where the workflow runs