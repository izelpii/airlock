import React, { useState, useEffect } from 'react';
import {
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CModalFooter,
  CButton,
  CProgress,
  CProgressBar,
  CAlert,
} from '@coreui/react';
import { workflowAPI } from '../services/api';
import socketService from '../services/socket';

const WorkflowExecutionModal = ({ visible, onClose, workflow }) => {
  const [execution, setExecution] = useState(null);
  const [progress, setProgress] = useState({
    processed: 0,
    total: 0,
    percentage: 0,
    status: 'running',
  });
  const [error, setError] = useState(null);

  useEffect(() => {
    if (visible && workflow) {
      startExecution();
    }

    return () => {
      // Cleanup
      setExecution(null);
      setProgress({
        processed: 0,
        total: 0,
        percentage: 0,
        status: 'running',
      });
      setError(null);
    };
  }, [visible, workflow]);

  useEffect(() => {
    if (!execution) return;

    // Connect to socket and listen for progress updates
    socketService.connect();

    const unsubscribe = socketService.onWorkflowProgress((data) => {
      if (data.execution_id === execution.execution_id) {
        setProgress({
          processed: data.processed || 0,
          total: data.total || 0,
          percentage: data.percentage || 0,
          status: data.status || 'running',
        });

        if (data.error) {
          setError(data.error);
        }
      }
    });

    return () => {
      unsubscribe();
    };
  }, [execution]);

  const startExecution = async () => {
    try {
      setError(null);
      const response = await workflowAPI.execute(workflow.id);
      setExecution(response.data);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to start workflow execution');
      console.error(err);
    }
  };

  const getStatusColor = () => {
    switch (progress.status) {
      case 'completed':
        return 'success';
      case 'failed':
        return 'danger';
      case 'running':
        return 'primary';
      default:
        return 'info';
    }
  };

  const getStatusText = () => {
    switch (progress.status) {
      case 'completed':
        return 'Completed Successfully';
      case 'failed':
        return 'Execution Failed';
      case 'running':
        return 'Running...';
      default:
        return 'Initializing...';
    }
  };

  const isComplete = progress.status === 'completed' || progress.status === 'failed';

  return (
    <CModal visible={visible} onClose={onClose} backdrop="static">
      <CModalHeader>
        <CModalTitle>Executing: {workflow?.name}</CModalTitle>
      </CModalHeader>
      <CModalBody>
        {error && (
          <CAlert color="danger" className="mb-3">
            <strong>Error:</strong> {error}
          </CAlert>
        )}

        <div className="mb-3">
          <div className="d-flex justify-content-between mb-2">
            <span className="text-muted">Status:</span>
            <strong className={`text-${getStatusColor()}`}>
              {getStatusText()}
            </strong>
          </div>
          <div className="d-flex justify-content-between mb-2">
            <span className="text-muted">Progress:</span>
            <strong>
              {progress.processed} / {progress.total} files
            </strong>
          </div>
        </div>

        <CProgress className="mb-3" height={25}>
          <CProgressBar
            color={getStatusColor()}
            value={progress.percentage}
            animated={!isComplete}
          >
            {progress.percentage}%
          </CProgressBar>
        </CProgress>

        {progress.status === 'completed' && (
          <CAlert color="success">
            <strong>Success!</strong> Workflow completed successfully.
            Results have been saved to the target directory.
          </CAlert>
        )}

        {progress.status === 'failed' && (
          <CAlert color="danger">
            <strong>Failed!</strong> The workflow execution encountered an error.
          </CAlert>
        )}
      </CModalBody>
      <CModalFooter>
        <CButton
          color="secondary"
          onClick={onClose}
          disabled={!isComplete}
        >
          {isComplete ? 'Close' : 'Running...'}
        </CButton>
      </CModalFooter>
    </CModal>
  );
};

export default WorkflowExecutionModal;
