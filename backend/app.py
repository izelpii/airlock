from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_socketio import SocketIO, emit
import os
import threading
import importlib.util
import sys
from models import Database, WorkflowModel, ExecutionModel, CatalogModel, CatalogDatabase

app = Flask(__name__)
CORS(app)
socketio = SocketIO(app, cors_allowed_origins="*")

# Initialize main database (for workflows and catalog metadata)
db = Database()
workflow_model = WorkflowModel(db)
catalog_model = CatalogModel(db)

# Store active executions
active_executions = {}

@app.route('/api/workflows', methods=['GET'])
def get_workflows():
    """Get all workflows"""
    workflows = workflow_model.get_all()
    return jsonify(workflows)

@app.route('/api/workflows', methods=['POST'])
def create_workflow():
    """Create a new workflow"""
    data = request.json
    
    required_fields = ['name', 'source_directory', 'catalog_id', 'python_script']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    # Verify catalog exists
    catalog = catalog_model.get_by_id(data['catalog_id'])
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    workflow_id = workflow_model.create(
        name=data['name'],
        description=data.get('description', ''),
        source_directory=data['source_directory'],
        catalog_id=data['catalog_id'],
        python_script=data['python_script']
    )
    
    return jsonify({'id': workflow_id, 'message': 'Workflow created successfully'}), 201

@app.route('/api/workflows/<int:workflow_id>', methods=['GET'])
def get_workflow(workflow_id):
    """Get a specific workflow"""
    workflow = workflow_model.get_by_id(workflow_id)
    if not workflow:
        return jsonify({'error': 'Workflow not found'}), 404
    return jsonify(workflow)

@app.route('/api/workflows/<int:workflow_id>', methods=['PUT'])
def update_workflow(workflow_id):
    """Update a workflow"""
    workflow = workflow_model.get_by_id(workflow_id)
    if not workflow:
        return jsonify({'error': 'Workflow not found'}), 404
    
    data = request.json
    workflow_model.update(workflow_id, **data)
    
    return jsonify({'message': 'Workflow updated successfully'})

@app.route('/api/workflows/<int:workflow_id>', methods=['DELETE'])
def delete_workflow(workflow_id):
    """Delete a workflow"""
    workflow = workflow_model.get_by_id(workflow_id)
    if not workflow:
        return jsonify({'error': 'Workflow not found'}), 404
    
    workflow_model.delete(workflow_id)
    return jsonify({'message': 'Workflow deleted successfully'})

@app.route('/api/workflows/<int:workflow_id>/execute', methods=['POST'])
def execute_workflow(workflow_id):
    """Execute a workflow"""
    workflow = workflow_model.get_by_id(workflow_id)
    if not workflow:
        return jsonify({'error': 'Workflow not found'}), 404
    
    # Get catalog to access catalog-specific database
    catalog = catalog_model.get_by_id(workflow['catalog_id'])
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    # Create catalog-specific database instance
    catalog_db = CatalogDatabase(catalog['target_directory'])
    execution_model = ExecutionModel(catalog_db)
    
    # Create execution record in catalog-specific database
    execution_id = execution_model.create(workflow_id, workflow['name'])
    
    # Start workflow execution in a separate thread
    thread = threading.Thread(
        target=run_workflow,
        args=(execution_id, workflow, catalog)
    )
    thread.daemon = True
    thread.start()
    
    return jsonify({
        'execution_id': execution_id,
        'message': 'Workflow execution started'
    }), 202

@app.route('/api/catalogs/<int:catalog_id>/executions/<int:execution_id>', methods=['GET'])
def get_execution(catalog_id, execution_id):
    """Get execution status from catalog-specific database"""
    catalog = catalog_model.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    catalog_db = CatalogDatabase(catalog['target_directory'])
    execution_model = ExecutionModel(catalog_db)
    execution = execution_model.get_by_id(execution_id)
    
    if not execution:
        return jsonify({'error': 'Execution not found'}), 404
    return jsonify(execution)

@app.route('/api/catalogs/<int:catalog_id>/executions', methods=['GET'])
def get_catalog_executions(catalog_id):
    """Get all executions for a catalog"""
    catalog = catalog_model.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    catalog_db = CatalogDatabase(catalog['target_directory'])
    execution_model = ExecutionModel(catalog_db)
    executions = execution_model.get_all()
    return jsonify(executions)

# Catalog API endpoints
@app.route('/api/catalogs', methods=['GET'])
def get_catalogs():
    """Get all catalogs"""
    catalogs = catalog_model.get_all()
    return jsonify(catalogs)

@app.route('/api/catalogs', methods=['POST'])
def create_catalog():
    """Create a new catalog"""
    data = request.json
    
    required_fields = ['name', 'owner', 'target_directory']
    for field in required_fields:
        if field not in data:
            return jsonify({'error': f'Missing required field: {field}'}), 400
    
    try:
        catalog_id = catalog_model.create(
            name=data['name'],
            owner=data['owner'],
            description=data.get('description', ''),
            target_directory=data['target_directory']
        )
        return jsonify({'id': catalog_id, 'message': 'Catalog created successfully'}), 201
    except Exception as e:
        return jsonify({'error': str(e)}), 400

@app.route('/api/catalogs/<int:catalog_id>', methods=['GET'])
def get_catalog(catalog_id):
    """Get a specific catalog with all related data from catalog-specific database"""
    catalog = catalog_model.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    # Get related data from catalog-specific database
    catalog['files'] = catalog_model.get_files(catalog_id)
    catalog['operations'] = []  # Deprecated
    catalog['workflow_executions'] = catalog_model.get_workflow_executions(catalog_id)
    
    return jsonify(catalog)

@app.route('/api/catalogs/<int:catalog_id>', methods=['PUT'])
def update_catalog(catalog_id):
    """Update a catalog"""
    catalog = catalog_model.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    data = request.json
    catalog_model.update(catalog_id, **data)
    
    return jsonify({'message': 'Catalog updated successfully'})

@app.route('/api/catalogs/<int:catalog_id>', methods=['DELETE'])
def delete_catalog(catalog_id):
    """Delete a catalog"""
    catalog = catalog_model.get_by_id(catalog_id)
    if not catalog:
        return jsonify({'error': 'Catalog not found'}), 404
    
    catalog_model.delete(catalog_id)
    return jsonify({'message': 'Catalog deleted successfully'})


def run_workflow(execution_id, workflow, catalog):
    """Run a workflow in a separate thread"""
    import time
    import stat
    
    start_time = time.time()
    files_processed = 0
    files_failed = 0
    new_files = []
    
    # Create catalog-specific database instance
    catalog_db = CatalogDatabase(catalog['target_directory'])
    execution_model = ExecutionModel(catalog_db)
    
    try:
        # Get source files
        source_dir = workflow['source_directory']
        target_dir = catalog['target_directory']
        catalog_id = workflow['catalog_id']
        
        if not os.path.exists(source_dir):
            raise Exception(f"Source directory does not exist: {source_dir}")
        
        # Create target directory if it doesn't exist
        os.makedirs(target_dir, exist_ok=True)
        
        # Get existing files in target directory before execution
        existing_files = set()
        if os.path.exists(target_dir):
            for root, dirs, filenames in os.walk(target_dir):
                for filename in filenames:
                    existing_files.add(os.path.join(root, filename))
        
        # Get all files in source directory
        files = []
        for root, dirs, filenames in os.walk(source_dir):
            for filename in filenames:
                files.append(os.path.join(root, filename))
        
        total_files = len(files)
        execution_model.update_progress(execution_id, 0, total_files)
        
        # Emit initial progress
        socketio.emit('workflow_progress', {
            'execution_id': execution_id,
            'processed': 0,
            'total': total_files,
            'percentage': 0
        })
        
        # Load and execute the workflow script
        script_path = workflow['python_script']
        
        if os.path.exists(script_path):
            # Load the module
            spec = importlib.util.spec_from_file_location("workflow_module", script_path)
            module = importlib.util.module_from_spec(spec)
            sys.modules["workflow_module"] = module
            spec.loader.exec_module(module)
            
            # Execute the workflow
            if hasattr(module, 'execute'):
                result = module.execute(
                    source_dir=source_dir,
                    target_dir=target_dir,
                    files=files,
                    progress_callback=lambda processed: update_progress(
                        execution_id, processed, total_files, catalog
                    ),
                    catalog_id=catalog_id
                )
                
                # Get statistics from workflow execution if returned
                if isinstance(result, dict):
                    files_processed = result.get('files_processed', total_files)
                    files_failed = result.get('files_failed', 0)
                    files_skipped = result.get('skipped_files', 0)
                else:
                    files_processed = total_files
                    files_failed = 0
                    files_skipped = 0
            else:
                raise Exception("Workflow script must have an 'execute' function")
        else:
            raise Exception(f"Workflow script not found: {script_path}")
        
        # Detect new files in target directory
        if os.path.exists(target_dir):
            for root, dirs, filenames in os.walk(target_dir):
                for filename in filenames:
                    file_path = os.path.join(root, filename)
                    if file_path not in existing_files:
                        new_files.append(file_path)
        
        # Record new files in catalog
        for file_path in new_files:
            try:
                file_stat = os.stat(file_path)
                file_size = file_stat.st_size
                file_name = os.path.basename(file_path)
                file_type = os.path.splitext(file_name)[1] or 'unknown'
                
                # Get file permissions in octal format
                file_permissions = oct(stat.S_IMODE(file_stat.st_mode))
                
                catalog_model.add_file(
                    catalog_id=catalog_id,
                    file_path=file_path,
                    file_name=file_name,
                    file_size=file_size,
                    file_type=file_type,
                    file_permissions=file_permissions
                )
            except Exception as e:
                print(f"Error recording file {file_path}: {str(e)}")
        
        # Mark as completed (already recorded in workflow_executions table)
        execution_model.complete(execution_id, 'completed', skipped_files=files_skipped)
        socketio.emit('workflow_progress', {
            'execution_id': execution_id,
            'processed': total_files,
            'total': total_files,
            'percentage': 100,
            'status': 'completed',
            'new_files_count': len(new_files),
            'skipped_files': files_skipped
        })
        
    except Exception as e:
        # Mark as failed (already recorded in workflow_executions table)
        execution_model.complete(execution_id, 'failed', str(e))
        socketio.emit('workflow_progress', {
            'execution_id': execution_id,
            'status': 'failed',
            'error': str(e)
        })

def update_progress(execution_id, processed, total, catalog):
    """Update execution progress"""
    catalog_db = CatalogDatabase(catalog['target_directory'])
    execution_model = ExecutionModel(catalog_db)
    execution_model.update_progress(execution_id, processed, total)
    percentage = int((processed / total) * 100) if total > 0 else 0
    
    socketio.emit('workflow_progress', {
        'execution_id': execution_id,
        'processed': processed,
        'total': total,
        'percentage': percentage
    })

if __name__ == '__main__':
    socketio.run(app, debug=True, host='0.0.0.0', port=5000, allow_unsafe_werkzeug=True)

# Made with Bob
