# Workflow Execution Tracking

## Overview

This document describes the enhanced workflow execution tracking system that records detailed information about workflow executions in the corresponding catalog.

## Database Schema Changes

### 1. File Contents Table (`file_contents`)

Added a new column to track file permissions:

- **file_permissions** (TEXT): Stores file permissions in octal format (e.g., "0o644")

### 2. Catalog Workflow Executions Table (`catalog_workflow_executions`)

This table already existed and stores workflow execution information:

- **id** (INTEGER): Primary key
- **catalog_id** (INTEGER): Foreign key to catalog_meta
- **workflow_script** (TEXT): Path to the workflow script that was executed
- **execution_date** (TIMESTAMP): When the workflow was executed
- **runtime_seconds** (REAL): How long the workflow took to execute
- **status** (TEXT): Execution status ('completed' or 'failed')
- **files_processed** (INTEGER): Number of files successfully processed
- **files_failed** (INTEGER): Number of files that failed to process
- **error_message** (TEXT): Error message if execution failed

## Workflow Execution Process

When a workflow is executed, the system now:

1. **Tracks Existing Files**: Before execution, records all files currently in the target directory
2. **Executes Workflow**: Runs the workflow script with progress tracking
3. **Detects New Files**: After execution, identifies files that were newly added to the target directory
4. **Records File Information**: For each new file, records:
   - File path
   - File name
   - File size (in bytes)
   - File type (extension)
   - File permissions (octal format)
5. **Records Execution Statistics**: Stores in the catalog:
   - Execution timestamp
   - Workflow script path
   - Runtime duration
   - Number of files processed
   - Number of files failed
   - Any error messages

## Workflow Script Requirements

Workflow scripts should return a dictionary with execution statistics:

```python
def execute(source_dir, target_dir, files, progress_callback):
    """
    Execute the workflow
    
    Returns:
        dict: Statistics about the execution
            - files_processed: Number of files successfully processed
            - files_failed: Number of files that failed
    """
    processed = 0
    failed = 0
    
    # ... workflow logic ...
    
    return {
        'files_processed': processed,
        'files_failed': failed
    }
```

## API Endpoints

### Get Catalog with Execution History

```
GET /api/catalogs/<catalog_id>
```

Returns catalog information including:
- `files`: List of all files in the catalog
- `operations`: List of file operations
- `workflow_executions`: List of workflow executions with statistics

### Add Workflow Execution (Manual)

```
POST /api/catalogs/<catalog_id>/workflow-executions
```

Request body:
```json
{
  "workflow_script": "/path/to/script.py",
  "runtime_seconds": 12.5,
  "status": "completed",
  "files_processed": 10,
  "files_failed": 0,
  "error_message": null
}
```

## Migration

To add the `file_permissions` column to an existing database:

```bash
cd backend
python migrate_add_file_permissions.py
```

## Benefits

1. **Complete Audit Trail**: Every workflow execution is recorded with detailed statistics
2. **File Tracking**: All new files are automatically cataloged with metadata
3. **Performance Monitoring**: Runtime statistics help identify slow workflows
4. **Error Tracking**: Failed executions are logged with error messages
5. **File Permissions**: Security-relevant file permissions are tracked

## Example Usage

When a workflow copies files from `/in` to `/out`:

1. Workflow executes and copies 5 files
2. System detects 5 new files in `/out`
3. Each file is recorded in `file_contents` table with:
   - Path: `/out/file1.txt`
   - Name: `file1.txt`
   - Size: `1024` bytes
   - Type: `.txt`
   - Permissions: `0o644`
4. Execution is recorded in `catalog_workflow_executions` with:
   - Script: `workflows/file_copy_workflow.py`
   - Runtime: `2.5` seconds
   - Status: `completed`
   - Files processed: `5`
   - Files failed: `0`

## Future Enhancements

Potential improvements:
- File checksums for integrity verification
- File modification tracking
- Workflow execution scheduling
- Execution comparison and diff views
- Performance analytics and reporting