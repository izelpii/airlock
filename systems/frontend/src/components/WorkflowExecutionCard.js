import React, { useState, useEffect, useRef } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CProgress,
  CProgressBar,
  CAlert,
  CButton,
  CBadge,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilX, cilCheckCircle, cilWarning } from '@coreui/icons';
import { workflowAPI } from '../services/api';
import socketService from '../services/socket';

const WorkflowExecutionCard = ({ workflow, onClose }) => {
  const [execution, setExecution] = useState(null);
  const [progress, setProgress] = useState({
    processed: 0,
    total: 0,
    percentage: 0,
    status: 'running',
  });
  const [error, setError] = useState(null);
  const hasStartedRef = useRef(false);

  useEffect(() => {
    const startExecution = async () => {
      // Prevent duplicate execution calls (React StrictMode double-invokes effects in dev)
      if (hasStartedRef.current) {
        return;
      }
      
      hasStartedRef.current = true;
      
      try {
        setError(null);
        const response = await workflowAPI.execute(workflow.id, workflow.executionConfig || {});
        setExecution(response.data);
      } catch (err) {
        setError(err.response?.data?.error || 'Failed to start workflow execution');
        console.error(err);
      }
    };

    if (workflow) {
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
  }, [workflow]);

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

  const getStatusIcon = () => {
    switch (progress.status) {
      case 'completed':
        return cilCheckCircle;
      case 'failed':
        return cilWarning;
      default:
        return null;
    }
  };

  const isComplete = progress.status === 'completed' || progress.status === 'failed';

  return (
    <CCard className="mb-3 shadow-sm" style={{ height: '400px' }}>
      <CCardHeader className="d-flex justify-content-between align-items-center">
        <div className="d-flex align-items-center">
          <strong className="me-3">Executing: {workflow?.name}</strong>
          <CBadge color={getStatusColor()}>
            {getStatusIcon() && <CIcon icon={getStatusIcon()} className="me-1" />}
            {getStatusText()}
          </CBadge>
        </div>
        {isComplete && (
          <CButton
            color="light"
            size="sm"
            onClick={onClose}
            className="text-muted"
          >
            <CIcon icon={cilX} />
          </CButton>
        )}
      </CCardHeader>
      <CCardBody style={{ flex: 1, display: 'flex', flexDirection: 'column', justifyContent: 'space-between', overflow: 'auto' }}>
        {error && (
          <CAlert color="danger" className="mb-3">
            <strong>Error:</strong> {error}
          </CAlert>
        )}

        <div className="mb-3">
          <div className="d-flex justify-content-between mb-2">
            <span className="text-muted">Progress:</span>
            <strong>
              {progress.processed} / {progress.total} files
            </strong>
          </div>
        </div>

        <CProgress className="mb-3" height={20}>
          <CProgressBar
            color={getStatusColor()}
            value={progress.percentage}
            animated={!isComplete}
          >
            {progress.percentage}%
          </CProgressBar>
        </CProgress>

        {progress.status === 'completed' && (
          <CAlert color="success" className="mb-0">
            <strong>Success!</strong> Workflow completed successfully.
            Results have been saved to the target directory.
          </CAlert>
        )}

        {progress.status === 'failed' && (
          <CAlert color="danger" className="mb-0">
            <strong>Failed!</strong> The workflow execution encountered an error.
          </CAlert>
        )}
      </CCardBody>
    </CCard>
  );
};

export default WorkflowExecutionCard;
