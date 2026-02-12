# Database Architecture Changes

## Overview
This document describes the major architectural change to move execution-related tables from the central `workflows.db` to catalog-specific `workflow_results.db` files located in each catalog's target directory.

## Motivation
- **Data Locality**: Keep execution results with the files they process
- **Scalability**: Distribute data across multiple databases
- **Portability**: Each catalog becomes self-contained with its own results
- **Isolation**: Catalog data is isolated and can be backed up independently

## Architecture Changes

### Before
```
workflows.db (central database)
├── workflows
├── catalog_meta
├── workflow_executions          ← Moved
├── file_contents                ← Moved
├── exif_data                    ← Moved
├── catalog_workflow_executions  ← Moved
└── file_operations              ← Deprecated
```

### After
```
workflows.db (central database)
├── workflows
└── catalog_meta

<catalog_target_directory>/workflow_results.db (per catalog)
├── workflow_executions
├── file_contents
├── exif_data
├── catalog_workflow_executions
└── catalog_meta (local copy)
```

## Changes Made

### 1. Models (`backend/models.py`)

#### New Classes
- **`CatalogDatabase`**: Manages catalog-specific database connections
  - Creates `workflow_results.db` in catalog's target directory
  - Initializes execution-related tables
  - Stores local copy of catalog metadata

#### Modified Classes
- **`Database`**: Now only manages workflows and catalog metadata
  - Removed execution-related table creation
  
- **`ExecutionModel`**: Now works with `CatalogDatabase` instead of `Database`
  - Takes `CatalogDatabase` instance in constructor
  - All operations use catalog-specific database
  
- **`CatalogModel`**: Updated to use catalog-specific databases
  - `create()`: Initializes catalog-specific database on creation
  - `get_catalog_db()`: Returns `CatalogDatabase` instance for a catalog
  - `add_file()`: Stores files in catalog-specific database
  - `get_files()`: Retrieves files from catalog-specific database
  - `add_exif_data()`: Stores EXIF data in catalog-specific database
  - `get_exif_data()`: Retrieves EXIF data from catalog-specific database
  - `add_workflow_execution()`: Records executions in catalog-specific database
  - `get_workflow_executions()`: Retrieves executions from catalog-specific database

### 2. API (`backend/app.py`)

#### Modified Endpoints
- **`POST /api/workflows/<workflow_id>/execute`**
  - Creates `CatalogDatabase` instance for the workflow's catalog
  - Creates execution record in catalog-specific database
  - Passes catalog info to workflow execution thread

- **`GET /api/catalogs/<catalog_id>/executions/<execution_id>`** (new)
  - Retrieves execution from catalog-specific database
  
- **`GET /api/catalogs/<catalog_id>/executions`** (new)
  - Lists all executions for a catalog from its database

- **`GET /api/catalogs/<catalog_id>`**
  - Retrieves files and executions from catalog-specific database

#### Removed Endpoints
- `POST /api/catalogs/<catalog_id>/files`
- `POST /api/catalogs/<catalog_id>/operations`
- `POST /api/catalogs/<catalog_id>/workflow-executions`

#### Modified Functions
- **`run_workflow()`**: Now accepts catalog parameter
  - Creates `CatalogDatabase` and `ExecutionModel` instances
  - Records all results in catalog-specific database
  
- **`update_progress()`**: Now accepts catalog parameter
  - Creates `CatalogDatabase` instance to update progress

### 3. Migration Script (`backend/migrate_to_catalog_databases.py`)

A comprehensive migration script that:
- Reads all catalogs from central database
- Creates catalog-specific databases in target directories
- Migrates workflow_executions, file_contents, exif_data, and catalog_workflow_executions
- Verifies migration by counting records in each catalog database
- Preserves original data in central database for safety

## Migration Process

### Step 1: Backup
```bash
cd backend
cp workflows.db workflows.db.backup
```

### Step 2: Run Migration
```bash
cd backend
python3 migrate_to_catalog_databases.py
```

### Step 3: Verify
The script automatically verifies the migration and displays:
- Number of executions migrated per catalog
- Number of files migrated per catalog
- Number of EXIF records migrated per catalog
- Number of catalog executions migrated per catalog

### Step 4: Test
Start the application and verify:
- Existing catalogs load correctly
- Workflow execution works
- Progress updates display correctly
- Execution history is visible

### Step 5: Cleanup (Optional)
After verifying everything works, you can optionally drop old tables from `workflows.db`:
```sql
DROP TABLE IF EXISTS workflow_executions;
DROP TABLE IF EXISTS file_contents;
DROP TABLE IF EXISTS exif_data;
DROP TABLE IF EXISTS catalog_workflow_executions;
DROP TABLE IF EXISTS file_operations;
```

## Benefits

### 1. Data Locality
- Execution results stored with processed files
- Easy to backup catalog with its results
- Catalog directories are self-contained

### 2. Scalability
- No single database bottleneck
- Each catalog has its own database
- Parallel operations on different catalogs don't conflict

### 3. Portability
- Move catalog directory = move all data
- Share catalogs with results included
- Archive catalogs independently

### 4. Performance
- Smaller database files
- Faster queries (less data per database)
- Better concurrent access

### 5. Maintenance
- Easier to manage individual catalog databases
- Can rebuild/repair single catalog without affecting others
- Clear data ownership

## API Changes for Frontend

### Execution Endpoints
**Old:**
```
GET /api/executions/<execution_id>
GET /api/workflows/<workflow_id>/executions
```

**New:**
```
GET /api/catalogs/<catalog_id>/executions/<execution_id>
GET /api/catalogs/<catalog_id>/executions
```

### Notes
- Frontend needs to know catalog_id to query executions
- Workflow execution still uses `POST /api/workflows/<workflow_id>/execute`
- Response includes execution_id as before

## File Structure

Each catalog's target directory now contains:
```
<target_directory>/
├── workflow_results.db          ← New catalog-specific database
├── <processed files>
└── <subdirectories>
```

## Backward Compatibility

- Old `workflows.db` tables remain intact after migration
- Migration script is non-destructive
- Can rollback by reverting code changes
- Frontend changes are minimal (execution card UI already updated)

## Testing Checklist

- [ ] Create new catalog
- [ ] Execute workflow on catalog
- [ ] Verify progress updates work
- [ ] Check execution history in catalog
- [ ] Verify files are recorded
- [ ] Test EXIF data extraction
- [ ] Run migration script on existing data
- [ ] Verify migrated data integrity
- [ ] Test multiple concurrent executions
- [ ] Verify catalog deletion cleans up database

## Troubleshooting

### Issue: Migration fails for a catalog
**Solution**: Check target directory permissions and disk space

### Issue: Execution not recorded
**Solution**: Verify catalog's target directory exists and is writable

### Issue: Can't find execution results
**Solution**: Ensure you're querying the correct catalog's database

### Issue: Database locked errors
**Solution**: Ensure no other processes are accessing the database files

## Future Enhancements

1. **Database Compression**: Compress old execution records
2. **Archival**: Move old executions to archive database
3. **Replication**: Sync catalog databases to backup location
4. **Analytics**: Aggregate statistics across catalog databases
5. **Search**: Full-text search across all catalog databases

## Made with Bob