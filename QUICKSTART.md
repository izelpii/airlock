# Quick Start Guide

## Prerequisites

- Python 3.8 or higher
- Node.js 16 or higher
- npm or yarn

## Quick Start (Automated)

The easiest way to start the application:

```bash
./start.sh
```

This script will:
1. Create a Python virtual environment (if needed)
2. Install all backend dependencies
3. Install all frontend dependencies
4. Start both servers

The application will be available at:
- Frontend: http://localhost:3000
- Backend API: http://localhost:5000

## Manual Start

### Backend

```bash
cd backend
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

### Frontend (in a new terminal)

```bash
cd frontend
npm install
npm start
```

## First Steps

1. **Open the application** at http://localhost:3000

2. **Create your first workflow**:
   - Click "Create Workflow"
   - Fill in the form:
     - Name: "File Copy Test"
     - Description: "Copy files from source to target"
     - Source Directory: `/path/to/source` (use an existing directory with some files)
     - Target Directory: `/path/to/target` (will be created if it doesn't exist)
     - Python Script Path: `workflows/file_copy_workflow.py`
   - Click "Save"

3. **Execute the workflow**:
   - Click the play button (▶) next to your workflow
   - Watch the progress bar update in real-time
   - Check the target directory for copied files and the `workflow_results.db` database

## Testing the Sample Workflow

To test the included file copy workflow:

1. Create a test source directory with some files:
```bash
mkdir -p /tmp/workflow-test/source
echo "Test file 1" > /tmp/workflow-test/source/file1.txt
echo "Test file 2" > /tmp/workflow-test/source/file2.txt
mkdir /tmp/workflow-test/source/subdir
echo "Test file 3" > /tmp/workflow-test/source/subdir/file3.txt
```

2. Create a workflow with:
   - Source Directory: `/tmp/workflow-test/source`
   - Target Directory: `/tmp/workflow-test/target`
   - Python Script Path: `workflows/file_copy_workflow.py`

3. Execute the workflow and verify:
   - Files are copied to `/tmp/workflow-test/target`
   - Database created at `/tmp/workflow-test/target/workflow_results.db`
   - Check the database:
   ```bash
   sqlite3 /tmp/workflow-test/target/workflow_results.db "SELECT * FROM file_operations;"
   ```

## Troubleshooting

### Backend won't start
- Ensure Python 3.8+ is installed: `python3 --version`
- Check if port 5000 is available: `lsof -i :5000`
- Verify all dependencies are installed: `pip list`

### Frontend won't start
- Ensure Node.js 16+ is installed: `node --version`
- Check if port 3000 is available: `lsof -i :3000`
- Clear npm cache: `npm cache clean --force`
- Delete node_modules and reinstall: `rm -rf node_modules && npm install`

### Workflow execution fails
- Verify source directory exists and contains files
- Ensure target directory is writable
- Check Python script path is correct (relative to project root)
- View backend logs for detailed error messages

### Progress not updating
- Check browser console for WebSocket connection errors
- Ensure backend is running and accessible
- Verify CORS settings allow connections from frontend

## Next Steps

- Read the full [README.md](README.md) for detailed documentation
- Create custom workflows by following the workflow template
- Explore the API endpoints for integration with other tools
- Check execution history for each workflow

## Support

For issues or questions:
1. Check the backend terminal for error logs
2. Check the browser console for frontend errors
3. Verify all prerequisites are met
4. Review the README.md for detailed information