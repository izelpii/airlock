# Workflow to Catalog Migration Guide

## Overview

The Workflows component has been updated to use Catalogs instead of storing Target Directory directly. This change provides better organization and allows workflows to leverage the catalog system for managing output directories.

## What Changed

### Backend Changes

1. **Database Schema**: The `workflows` table now uses `catalog_id` (foreign key to `catalog_meta`) instead of `target_directory`
   - Old: `target_directory TEXT NOT NULL`
   - New: `catalog_id INTEGER NOT NULL` with foreign key constraint

2. **WorkflowModel**: Updated to accept `catalog_id` instead of `target_directory`
   - `create()` method now requires `catalog_id` parameter
   - `update()` method now accepts `catalog_id` for updates

3. **API Endpoints**: 
   - POST `/api/workflows` now requires `catalog_id` instead of `target_directory`
   - Workflow execution now fetches the target directory from the associated catalog

### Frontend Changes

1. **WorkflowForm Component**:
   - Replaced target directory text input with a catalog dropdown selector
   - Loads all available catalogs on form open
   - Displays catalog name and target directory in the dropdown

2. **WorkflowList Component**:
   - Added "Catalog" column to display the associated catalog name
   - Still shows target directory (from catalog) for reference
   - Loads catalogs alongside workflows for display

## Migration Process

### Automatic Migration

A migration script (`backend/migrate_workflows_to_catalogs.py`) has been created and executed to:

1. Create a catalog for each unique target directory in existing workflows
2. Update workflows to reference the appropriate catalog
3. Remove the target_directory column from the workflows table

The migration creates catalogs with names like "Auto-migrated: [directory_name]" for easy identification.

### Manual Steps (if needed)

If you need to run the migration manually:

```bash
cd backend
python migrate_workflows_to_catalogs.py workflows.db
```

## Using the Updated System

### Creating a New Workflow

1. Navigate to the Dashboard
2. Click "Create Workflow"
3. Fill in the workflow details:
   - **Name**: Workflow name
   - **Description**: Optional description
   - **Source Directory**: Directory containing files to process
   - **Catalog**: Select from dropdown (shows catalog name and target directory)
   - **Python Script Path**: Path to the workflow script

### Editing an Existing Workflow

1. Click the edit button on a workflow
2. The form will show the currently selected catalog
3. You can change the catalog to use a different target directory

### Benefits of This Change

1. **Centralized Management**: Target directories are managed through catalogs
2. **Reusability**: Multiple workflows can share the same catalog/target directory
3. **Consistency**: Ensures workflows use properly configured catalogs
4. **Tracking**: Catalog system tracks all operations and workflow executions

## Verification

After migration, verify:

1. All existing workflows are visible in the Dashboard
2. Each workflow shows its associated catalog name
3. Workflow execution still works correctly
4. New workflows can be created with catalog selection

## Rollback (if needed)

If you need to rollback:

1. Restore the database backup (if created before migration)
2. Revert code changes to the previous commit

## Support

If you encounter any issues with the migration, check:

1. Database schema: `sqlite3 workflows.db ".schema workflows"`
2. Workflow-catalog relationships: `sqlite3 workflows.db "SELECT w.id, w.name, w.catalog_id, c.name FROM workflows w JOIN catalog_meta c ON w.catalog_id = c.id;"`
3. Backend logs for any errors during workflow execution