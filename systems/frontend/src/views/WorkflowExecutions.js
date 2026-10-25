import React, { useState, useEffect } from 'react';
import {
  CContainer,
  CAlert,
  CCard,
  CCardHeader,
  CCardBody,
  CButton,
  CTable,
  CTableHead,
  CTableRow,
  CTableHeaderCell,
  CTableBody,
  CTableDataCell,
  CSpinner,
  CBadge,
  CPagination,
  CPaginationItem,
} from '@coreui/react';
import { useCatalog } from '../context/CatalogContext';
import { catalogAPI, workflowAPI } from '../services/api';
import IngestionCards from '../components/IngestionCards';

const WorkflowExecutions = () => {
  const { activeCatalog } = useCatalog();
  const [catalog, setCatalog] = useState(null);
  const [loading, setLoading] = useState(true);
  const [workflowExecutionsPage, setWorkflowExecutionsPage] = useState(1);
  const [expandedExecutionId, setExpandedExecutionId] = useState(null);
  const [executionFiles, setExecutionFiles] = useState({});
  const [loadingExecutionFiles, setLoadingExecutionFiles] = useState({});
  const [executionFilesPage, setExecutionFilesPage] = useState({});
  const itemsPerPage = 25;

  useEffect(() => {
    if (activeCatalog) {
      loadCatalog();
    } else {
      setLoading(false);
    }
  }, [activeCatalog]);

  const loadCatalog = async () => {
    try {
      setLoading(true);
      const response = await catalogAPI.getById(activeCatalog.id);
      setCatalog(response.data);
    } catch (err) {
      console.error('Failed to load catalog:', err);
    } finally {
      setLoading(false);
    }
  };

  const formatBytes = (bytes) => {
    if (bytes === 0) return '0 Bytes';
    const k = 1024;
    const sizes = ['Bytes', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
  };

  const toggleExecutionDetails = async (executionId) => {
    if (expandedExecutionId === executionId) {
      setExpandedExecutionId(null);
    } else {
      setExpandedExecutionId(executionId);
      if (!executionFiles[executionId]) {
        setLoadingExecutionFiles(prev => ({ ...prev, [executionId]: true }));
        try {
          const response = await workflowAPI.getExecutionFiles(activeCatalog.id, executionId);
          setExecutionFiles(prev => ({ ...prev, [executionId]: response.data }));
          setExecutionFilesPage(prev => ({ ...prev, [executionId]: 1 }));
        } catch (err) {
          console.error('Failed to load execution files:', err);
          setExecutionFiles(prev => ({ ...prev, [executionId]: [] }));
        } finally {
          setLoadingExecutionFiles(prev => ({ ...prev, [executionId]: false }));
        }
      }
    }
  };

  const getPaginatedData = (data, page) => {
    const startIndex = (page - 1) * itemsPerPage;
    return data.slice(startIndex, startIndex + itemsPerPage);
  };

  const renderPagination = (currentPage, totalItems, onPageChange) => {
    const totalPages = Math.ceil(totalItems / itemsPerPage);
    if (totalPages <= 1) return null;

    const maxVisiblePages = 5;
    let startPage = Math.max(1, currentPage - Math.floor(maxVisiblePages / 2));
    let endPage = Math.min(totalPages, startPage + maxVisiblePages - 1);
    if (endPage - startPage < maxVisiblePages - 1) {
      startPage = Math.max(1, endPage - maxVisiblePages + 1);
    }

    return (
      <div className="d-flex justify-content-between align-items-center mt-3">
        <div className="text-muted small">
          Showing {((currentPage - 1) * itemsPerPage) + 1} to {Math.min(currentPage * itemsPerPage, totalItems)} of {totalItems} entries
        </div>
        <CPagination aria-label="Page navigation">
          <CPaginationItem disabled={currentPage === 1} onClick={() => onPageChange(currentPage - 1)}>
            Previous
          </CPaginationItem>
          {startPage > 1 && (
            <>
              <CPaginationItem onClick={() => onPageChange(1)}>1</CPaginationItem>
              {startPage > 2 && <CPaginationItem disabled>...</CPaginationItem>}
            </>
          )}
          {Array.from({ length: endPage - startPage + 1 }, (_, i) => startPage + i).map((page) => (
            <CPaginationItem key={page} active={page === currentPage} onClick={() => onPageChange(page)}>
              {page}
            </CPaginationItem>
          ))}
          {endPage < totalPages && (
            <>
              {endPage < totalPages - 1 && <CPaginationItem disabled>...</CPaginationItem>}
              <CPaginationItem onClick={() => onPageChange(totalPages)}>{totalPages}</CPaginationItem>
            </>
          )}
          <CPaginationItem disabled={currentPage === totalPages} onClick={() => onPageChange(currentPage + 1)}>
            Next
          </CPaginationItem>
        </CPagination>
      </div>
    );
  };

  if (!activeCatalog) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="info">
          No catalog selected. Please open a catalog from the <strong>Catalogs</strong> page first.
        </CAlert>
      </CContainer>
    );
  }

  if (loading) {
    return (
      <CContainer fluid className="px-4 py-4">
        <div className="text-center py-5">
          <CSpinner color="primary" />
        </div>
      </CContainer>
    );
  }

  if (!catalog) return null;

  return (
    <CContainer fluid className="px-4 py-4">
      <div className="mt-4">
        {/* Page title */}
        <div className="d-flex justify-content-between align-items-center mb-4">
          <h3>Workflow Executions: {catalog.name}</h3>
        </div>

        {/* ── Ingestion runs ──────────────────────────────────────────────
            FIRST, because it answers the question the page is usually opened
            with: what did the last import bring in. The table below still
            lists every execution of every workflow; this row is only the
            ingestions, and only their counts. */}
        <CCard className="mb-4">
          <CCardHeader>
            <strong>Ingestion Runs</strong>
          </CCardHeader>
          <CCardBody>
            <IngestionCards catalogId={activeCatalog?.id} />
          </CCardBody>
        </CCard>

        {/* Workflow Executions Card */}
        <CCard className="mb-4">
          <CCardHeader>
            <strong>Workflow Executions</strong>
            <CBadge color="info" className="ms-2">{catalog.workflow_executions?.length || 0}</CBadge>
          </CCardHeader>
          <CCardBody>
            {catalog.workflow_executions && catalog.workflow_executions.length > 0 ? (
              <>
                <CTable hover responsive>
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell>Workflow Name</CTableHeaderCell>
                      <CTableHeaderCell>Status</CTableHeaderCell>
                      <CTableHeaderCell>Started</CTableHeaderCell>
                      <CTableHeaderCell>Completed</CTableHeaderCell>
                      <CTableHeaderCell>Total Files</CTableHeaderCell>
                      <CTableHeaderCell>Processed</CTableHeaderCell>
                      <CTableHeaderCell>Skipped Files</CTableHeaderCell>
                      <CTableHeaderCell>Error</CTableHeaderCell>
                      <CTableHeaderCell>Actions</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {getPaginatedData(catalog.workflow_executions, workflowExecutionsPage).map((exec) => (
                      <React.Fragment key={exec.id}>
                        <CTableRow>
                          <CTableDataCell><strong>{exec.workflow_name}</strong></CTableDataCell>
                          <CTableDataCell>
                            <CBadge color={exec.status === 'completed' ? 'success' : exec.status === 'failed' ? 'danger' : 'warning'}>
                              {exec.status}
                            </CBadge>
                          </CTableDataCell>
                          <CTableDataCell>{new Date(exec.started_at).toLocaleString()}</CTableDataCell>
                          <CTableDataCell>
                            {exec.completed_at ? new Date(exec.completed_at).toLocaleString() : '-'}
                          </CTableDataCell>
                          <CTableDataCell>
                            <CBadge color="info">{exec.total_files || 0}</CBadge>
                          </CTableDataCell>
                          <CTableDataCell>
                            <CBadge color="success">{exec.processed_files || 0}</CBadge>
                          </CTableDataCell>
                          <CTableDataCell>
                            <CBadge color="warning">{exec.skipped_files || 0}</CBadge>
                          </CTableDataCell>
                          <CTableDataCell>
                            {exec.error_message ? (
                              <span className="text-danger small" title={exec.error_message}>
                                {exec.error_message.substring(0, 50)}{exec.error_message.length > 50 ? '...' : ''}
                              </span>
                            ) : (
                              '-'
                            )}
                          </CTableDataCell>
                          <CTableDataCell>
                            <CButton color="primary" size="sm" onClick={() => toggleExecutionDetails(exec.id)}>
                              {expandedExecutionId === exec.id ? 'Hide Details' : 'Show Details'}
                            </CButton>
                          </CTableDataCell>
                        </CTableRow>
                        {expandedExecutionId === exec.id && (
                          <CTableRow>
                            <CTableDataCell colSpan="9">
                              <CCard className="mt-2 mb-2">
                                <CCardHeader>
                                  <strong>Files Processed in Execution: {exec.workflow_name}</strong>
                                </CCardHeader>
                                <CCardBody>
                                  {loadingExecutionFiles[exec.id] ? (
                                    <div className="text-center py-3">
                                      <CSpinner color="primary" size="sm" />
                                      <span className="ms-2">Loading files...</span>
                                    </div>
                                  ) : executionFiles[exec.id] && executionFiles[exec.id].length > 0 ? (
                                    <>
                                      <CTable hover responsive size="sm">
                                        <CTableHead>
                                          <CTableRow>
                                            <CTableHeaderCell>File Name</CTableHeaderCell>
                                            <CTableHeaderCell>Source Path</CTableHeaderCell>
                                            <CTableHeaderCell>Target Path</CTableHeaderCell>
                                            <CTableHeaderCell>Size</CTableHeaderCell>
                                            <CTableHeaderCell>Action</CTableHeaderCell>
                                            <CTableHeaderCell>Status</CTableHeaderCell>
                                            <CTableHeaderCell>Date</CTableHeaderCell>
                                          </CTableRow>
                                        </CTableHead>
                                        <CTableBody>
                                          {getPaginatedData(
                                            executionFiles[exec.id],
                                            executionFilesPage[exec.id] || 1
                                          ).map((operation) => (
                                            <CTableRow key={operation.id}>
                                              <CTableDataCell><strong>{operation.file_name || operation.source_path?.split('/').pop() || '-'}</strong></CTableDataCell>
                                              <CTableDataCell><code className="small">{operation.source_path}</code></CTableDataCell>
                                              <CTableDataCell><code className="small">{operation.target_path}</code></CTableDataCell>
                                              <CTableDataCell>{operation.file_size ? formatBytes(operation.file_size) : '-'}</CTableDataCell>
                                              <CTableDataCell>
                                                <CBadge color={operation.operation_type === 'copy' ? 'primary' : operation.operation_type === 'skip' ? 'warning' : 'secondary'}>
                                                  {operation.operation_type === 'copy' ? 'Copied' : operation.operation_type === 'skip' ? 'Skipped' : operation.operation_type || 'Unknown'}
                                                </CBadge>
                                              </CTableDataCell>
                                              <CTableDataCell>
                                                <CBadge color={operation.status === 'success' ? 'success' : operation.status === 'skipped' ? 'warning' : 'danger'}>
                                                  {operation.status}
                                                </CBadge>
                                              </CTableDataCell>
                                              <CTableDataCell>{new Date(operation.operation_date).toLocaleString()}</CTableDataCell>
                                            </CTableRow>
                                          ))}
                                        </CTableBody>
                                      </CTable>
                                      {renderPagination(
                                        executionFilesPage[exec.id] || 1,
                                        executionFiles[exec.id].length,
                                        (page) => setExecutionFilesPage(prev => ({ ...prev, [exec.id]: page }))
                                      )}
                                    </>
                                  ) : (
                                    <div className="text-center text-muted py-3">
                                      No files found for this execution
                                    </div>
                                  )}
                                </CCardBody>
                              </CCard>
                            </CTableDataCell>
                          </CTableRow>
                        )}
                      </React.Fragment>
                    ))}
                  </CTableBody>
                </CTable>
                {renderPagination(workflowExecutionsPage, catalog.workflow_executions.length, setWorkflowExecutionsPage)}
              </>
            ) : (
              <div className="text-center text-muted py-4">No workflow executions recorded</div>
            )}
          </CCardBody>
        </CCard>
      </div>
    </CContainer>
  );
};

export default WorkflowExecutions;
