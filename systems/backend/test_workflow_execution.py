#!/usr/bin/env python3
"""
Test script to verify workflow execution tracking
"""
import sys
import os
import time
import requests
import json

# Add backend to path
sys.path.insert(0, os.path.dirname(__file__))

from models import Database, WorkflowModel, CatalogModel

def test_workflow_execution():
    """Test workflow execution and verify catalog tracking"""
    
    print("=" * 60)
    print("Testing Workflow Execution Tracking")
    print("=" * 60)
    
    # Initialize database
    db = Database('../database/workflows.db')
    workflow_model = WorkflowModel(db)
    catalog_model = CatalogModel(db)
    
    # Get the test workflow
    workflows = workflow_model.get_all()
    if not workflows:
        print("❌ No workflows found in database")
        return False
    
    workflow = workflows[0]
    print(f"\n✓ Found workflow: {workflow['name']} (ID: {workflow['id']})")
    print(f"  Source: {workflow['source_directory']}")
    print(f"  Catalog ID: {workflow['catalog_id']}")
    
    # Get the catalog
    catalog = catalog_model.get_by_id(workflow['catalog_id'])
    if not catalog:
        print(f"❌ Catalog {workflow['catalog_id']} not found")
        return False
    
    print(f"\n✓ Found catalog: {catalog['name']}")
    print(f"  Target: {catalog['target_directory']}")
    
    # Check existing workflow executions
    print("\n" + "-" * 60)
    print("Checking existing workflow executions in catalog...")
    print("-" * 60)
    
    executions_before = catalog_model.get_workflow_executions(catalog['id'])
    print(f"Executions before test: {len(executions_before)}")
    
    if executions_before:
        print("\nRecent executions:")
        for i, exec in enumerate(executions_before[:3], 1):
            print(f"  {i}. {exec['execution_date']}")
            print(f"     Status: {exec['status']}")
            print(f"     Runtime: {exec['runtime_seconds']:.2f}s")
            print(f"     Files processed: {exec['files_processed']}")
            print(f"     Files failed: {exec['files_failed']}")
    
    # Check existing files in catalog
    print("\n" + "-" * 60)
    print("Checking existing files in catalog...")
    print("-" * 60)
    
    files_before = catalog_model.get_files(catalog['id'])
    print(f"Files before test: {len(files_before)}")
    
    if files_before:
        print("\nRecent files:")
        for i, file in enumerate(files_before[:5], 1):
            print(f"  {i}. {file['file_name']}")
            print(f"     Size: {file['file_size']} bytes")
            print(f"     Type: {file['file_type']}")
            print(f"     Permissions: {file.get('file_permissions', 'N/A')}")
    
    # Test execution via API
    print("\n" + "=" * 60)
    print("Testing workflow execution via API...")
    print("=" * 60)
    
    try:
        # Start the workflow execution
        response = requests.post(
            f'http://localhost:5000/api/workflows/{workflow["id"]}/execute',
            timeout=5
        )
        
        if response.status_code == 202:
            result = response.json()
            execution_id = result['execution_id']
            print(f"\n✓ Workflow execution started (Execution ID: {execution_id})")
            
            # Wait a bit for execution to complete
            print("\nWaiting for execution to complete...")
            time.sleep(3)
            
            # Check execution status
            status_response = requests.get(
                f'http://localhost:5000/api/executions/{execution_id}',
                timeout=5
            )
            
            if status_response.status_code == 200:
                execution = status_response.json()
                print(f"\n✓ Execution status: {execution['status']}")
                print(f"  Processed: {execution['processed_files']}/{execution['total_files']}")
                
                # Check catalog for new execution record
                executions_after = catalog_model.get_workflow_executions(catalog['id'])
                print(f"\n✓ Executions after test: {len(executions_after)}")
                
                if len(executions_after) > len(executions_before):
                    latest = executions_after[0]
                    print("\n✓ New execution recorded in catalog:")
                    print(f"  Date: {latest['execution_date']}")
                    print(f"  Status: {latest['status']}")
                    print(f"  Runtime: {latest['runtime_seconds']:.2f}s")
                    print(f"  Files processed: {latest['files_processed']}")
                    print(f"  Files failed: {latest['files_failed']}")
                    print(f"  Script: {latest['workflow_script']}")
                
                # Check for new files
                files_after = catalog_model.get_files(catalog['id'])
                new_files_count = len(files_after) - len(files_before)
                
                if new_files_count > 0:
                    print(f"\n✓ {new_files_count} new file(s) recorded in catalog")
                    print("\nNew files:")
                    for file in files_after[:new_files_count]:
                        print(f"  - {file['file_name']}")
                        print(f"    Size: {file['file_size']} bytes")
                        print(f"    Type: {file['file_type']}")
                        print(f"    Permissions: {file.get('file_permissions', 'N/A')}")
                else:
                    print("\n⚠ No new files detected (files may have existed already)")
                
                print("\n" + "=" * 60)
                print("✓ TEST PASSED: Workflow execution tracking is working!")
                print("=" * 60)
                return True
            else:
                print(f"❌ Failed to get execution status: {status_response.status_code}")
                return False
        else:
            print(f"❌ Failed to start workflow: {response.status_code}")
            print(f"Response: {response.text}")
            return False
            
    except requests.exceptions.ConnectionError:
        print("\n⚠ Could not connect to API server")
        print("Please ensure the backend server is running:")
        print("  cd backend && python app.py")
        return False
    except Exception as e:
        print(f"\n❌ Error during test: {str(e)}")
        return False

if __name__ == '__main__':
    success = test_workflow_execution()
    sys.exit(0 if success else 1)
