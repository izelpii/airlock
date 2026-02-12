# Duplicate Execution Recording Fix

## Issue
Workflow executions were being recorded twice in the catalog-specific database:
1. Once in the `workflow_executions` table
2. Once in the `catalog_workflow_executions` table

This resulted in duplicate entries for every workflow execution.

## Root Cause
The original design had two separate tables for tracking executions:
- `workflow_executions`: Detailed execution tracking with progress
- `catalog_workflow_executions`: Summary of workflow executions per catalog

When migrating to catalog-specific databases, both tables were being populated, causing duplication.

## Solution
Consolidated all execution tracking into a single `workflow_executions` table in each catalog-specific database.

### Changes Made

#### 1. Removed `catalog_workflow_executions` Table
**File: `backend/models.py`**
- Removed table creation from `CatalogDatabase.init_db()`
- Removed `CatalogModel.add_workflow_execution()` method
- Updated `CatalogModel.get_workflow_executions()` to query `workflow_executions` table instead

#### 2. Removed Duplicate Recording
**File: `backend/app.py`**
- Removed calls to `catalog_model.add_workflow_execution()` in `run_workflow()`
- Now only uses `execution_model.create()` and `execution_model.complete()`
- Single source of truth: `workflow_executions` table

#### 3. Updated Migration Script
**File: `backend/migrate_to_catalog_databases.py`**
- Removed migration of `catalog_workflow_executions` table
- Only migrates `workflow_executions` table
- Updated verification to check only `workflow_executions`

## Database Schema (After Fix)

### Catalog-Specific Database (`workflow_results.db`)
```sql
-- Single execution tracking table
CREATE TABLE workflow_executions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL,
    workflow_name TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    total_files INTEGER DEFAULT 0,
    processed_files INTEGER DEFAULT 0,
    error_message TEXT
);

-- File tracking
CREATE TABLE file_contents (...);

-- EXIF data
CREATE TABLE exif_data (...);

-- Local catalog metadata
CREATE TABLE catalog_meta (...);
```

## Benefits

1. **No Duplication**: Each execution recorded exactly once
2. **Simpler Schema**: One table instead of two
3. **Clearer Data Model**: Single source of truth for executions
4. **Better Performance**: Fewer database writes
5. **Easier Queries**: No need to join or deduplicate data

## Migration Impact

### For New Installations
- No action needed
- Only `workflow_executions` table will be created

### For Existing Installations
- Run migration script: `python3 backend/migrate_to_catalog_databases.py`
- Old `catalog_workflow_executions` data will not be migrated (deprecated)
- All execution data will be in `workflow_executions` table

## Verification

After the fix, verify that:
1. Each workflow execution creates only ONE record in `workflow_executions`
2. No records are created in `catalog_workflow_executions` (table doesn't exist)
3. Execution history displays correctly in the UI
4. Progress updates work as expected

### SQL Query to Check
```sql
-- In catalog's workflow_results.db
SELECT COUNT(*) FROM workflow_executions WHERE workflow_id = <id>;
-- Should return 1 per execution, not 2
```

## API Impact

No API changes required. The endpoints remain the same:
- `POST /api/workflows/<workflow_id>/execute`
- `GET /api/catalogs/<catalog_id>/executions`
- `GET /api/catalogs/<catalog_id>/executions/<execution_id>`

All endpoints now query the unified `workflow_executions` table.

## Made with Bob