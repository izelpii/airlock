# Workflow Manager

A modern web application for creating, managing, and executing Python workflows with real-time progress tracking.

## Features

- **Modern UI**: Built with CoreUI React components
- **Workflow Management**: Create, edit, delete, and list workflows
- **Real-time Execution**: Execute workflows with live progress updates via WebSocket
- **Database Integration**: SQLite database for storing workflow configurations and execution results
- **Sample Workflow**: Includes a file copy workflow that demonstrates the system

## Architecture

### Backend (Python/Flask)
- Flask REST API for workflow CRUD operations
- Flask-SocketIO for real-time progress updates
- SQLite database for persistence
- Modular workflow execution system

### Frontend (React/CoreUI)
- Modern, responsive UI built with CoreUI
- Real-time progress tracking with Socket.IO
- Workflow creation and management interface
- Progress bars and status indicators

## Project Structure

```
workflow/
├── backend/
│   ├── app.py              # Flask application and API endpoints
│   ├── models.py           # Database models
│   ├── requirements.txt    # Python dependencies
│   └── workflows.db        # SQLite database (created on first run)
├── frontend/
│   ├── public/
│   │   └── index.html
│   ├── src/
│   │   ├── components/     # React components
│   │   │   ├── WorkflowList.js
│   │   │   ├── WorkflowForm.js
│   │   │   └── WorkflowExecutionModal.js
│   │   ├── views/
│   │   │   └── Dashboard.js
│   │   ├── services/       # API and Socket services
│   │   │   ├── api.js
│   │   │   └── socket.js
│   │   ├── App.js
│   │   └── index.js
│   └── package.json
└── workflows/
    └── file_copy_workflow.py  # Sample workflow
```

## Installation

### Backend Setup

1. Navigate to the backend directory:
```bash
cd backend
```

2. Create a virtual environment (recommended):
```bash
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
```

3. Install dependencies:
```bash
pip install -r requirements.txt
```

4. Run the backend server:
```bash
python app.py
```

The backend will start on `http://localhost:5000`

### Frontend Setup

1. Navigate to the frontend directory:
```bash
cd frontend
```

2. Install dependencies:
```bash
npm install
```

3. Start the development server:
```bash
npm start
```

The frontend will start on `http://localhost:3000`

## Usage

### Creating a Workflow

1. Click the "Create Workflow" button
2. Fill in the workflow details:
   - **Name**: A descriptive name for your workflow
   - **Description**: Optional description
   - **Source Directory**: Path to the directory containing files to process
   - **Target Directory**: Path where results will be stored
   - **Python Script Path**: Path to the Python script (e.g., `workflows/file_copy_workflow.py`)
3. Click "Save"

### Executing a Workflow

1. Click the play button (▶) next to a workflow in the list
2. A modal will appear showing real-time progress
3. The progress bar updates as files are processed
4. Results are stored in the target directory's SQLite database

### Sample Workflow

The included `file_copy_workflow.py` demonstrates:
- Copying files from source to target directory
- Maintaining directory structure
- Logging operations to SQLite database
- Progress callback integration

## Creating Custom Workflows

To create a custom workflow, create a Python file with an `execute` function:

```python
def execute(source_dir, target_dir, files, progress_callback):
    """
    Custom workflow implementation
    
    Args:
        source_dir: Source directory path
        target_dir: Target directory path
        files: List of file paths to process
        progress_callback: Function to call with progress updates
    """
    processed = 0
    
    for file_path in files:
        # Your processing logic here
        
        processed += 1
        if progress_callback:
            progress_callback(processed)
```

## API Endpoints

- `GET /api/workflows` - List all workflows
- `POST /api/workflows` - Create a new workflow
- `GET /api/workflows/<id>` - Get workflow details
- `PUT /api/workflows/<id>` - Update a workflow
- `DELETE /api/workflows/<id>` - Delete a workflow
- `POST /api/workflows/<id>/execute` - Execute a workflow
- `GET /api/executions/<id>` - Get execution status
- `GET /api/workflows/<id>/executions` - Get workflow execution history

## WebSocket Events

- `workflow_progress` - Real-time progress updates during execution

## Technologies Used

### Backend
- Flask 3.0.0
- Flask-CORS 4.0.0
- Flask-SocketIO 5.3.5
- SQLite3

### Frontend
- React 18.2.0
- CoreUI React 5.0.0
- React Router 6.20.0
- Axios 1.6.2
- Socket.IO Client 4.5.4

## License

MIT