import React, { useState, useEffect } from 'react';
import {
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
  CRow,
  CCol,
  CPagination,
  CPaginationItem,
} from '@coreui/react';
import { catalogAPI } from '../services/api';

const CatalogDetail = ({ catalogId, onClose }) => {
  const [catalog, setCatalog] = useState(null);
  const [loading, setLoading] = useState(true);
  const [filesPage, setFilesPage] = useState(1);
  const [workflowExecutionsPage, setWorkflowExecutionsPage] = useState(1);
  const [expandedFileId, setExpandedFileId] = useState(null);
  const itemsPerPage = 8;

  useEffect(() => {
    if (catalogId) {
      loadCatalog();
    }
  }, [catalogId]);

  const loadCatalog = async () => {
    try {
      setLoading(true);
      const response = await catalogAPI.getById(catalogId);
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

  const formatDuration = (seconds) => {
    if (seconds < 60) return `${seconds.toFixed(2)}s`;
    const minutes = Math.floor(seconds / 60);
    const secs = (seconds % 60).toFixed(0);
    return `${minutes}m ${secs}s`;
  };

  const toggleExifData = (fileId) => {
    setExpandedFileId(expandedFileId === fileId ? null : fileId);
  };

  const renderExifData = (exifData) => {
    if (!exifData || Object.keys(exifData).length === 0) {
      return <div className="text-muted">No EXIF data available for this file</div>;
    }

    // Filter EXIF data to show only relevant fields
    // Include fields related to: date, creator, camera, lens, exposure
    // Exclude: MakerNote, UserComment
    const relevantFields = [
      // Date/Time fields
      'DateTime', 'DateTimeOriginal', 'DateTimeDigitized', 'CreateDate', 'ModifyDate',
      // Creator/Copyright fields
      'Artist', 'Creator', 'Copyright', 'Author', 'By-line',
      // Camera fields
      'Make', 'Model', 'CameraModel', 'CameraModelName',
      // Lens fields
      'LensModel', 'LensMake', 'Lens', 'LensInfo', 'LensID',
      // Exposure fields
      'ExposureTime', 'FNumber', 'ISO', 'ISOSpeedRatings', 'ShutterSpeed', 'ShutterSpeedValue',
      'Aperture', 'ApertureValue', 'ExposureProgram', 'ExposureMode', 'ExposureBiasValue',
      'MeteringMode', 'Flash', 'FocalLength', 'FocalLengthIn35mmFormat',
      // Additional useful fields
      'WhiteBalance', 'ColorSpace', 'Orientation', 'ImageWidth', 'ImageHeight',
      'XResolution', 'YResolution', 'ResolutionUnit'
    ];

    // Filter the exifData to only include relevant fields and exclude unwanted ones
    const filteredData = Object.entries(exifData).filter(([key, value]) => {
      // Exclude MakerNote and UserComment
      if (key === 'MakerNote' || key === 'UserComment') {
        return false;
      }
      
      // Include if key matches any relevant field (case-insensitive partial match)
      const keyLower = key.toLowerCase();
      return relevantFields.some(field =>
        keyLower.includes(field.toLowerCase()) || field.toLowerCase().includes(keyLower)
      );
    });

    if (filteredData.length === 0) {
      return <div className="text-muted">No relevant EXIF data available for this file</div>;
    }

    return (
      <CTable small bordered>
        <CTableHead>
          <CTableRow>
            <CTableHeaderCell style={{ width: '30%' }}>Property</CTableHeaderCell>
            <CTableHeaderCell>Value</CTableHeaderCell>
          </CTableRow>
        </CTableHead>
        <CTableBody>
          {filteredData.map(([key, value]) => (
            <CTableRow key={key}>
              <CTableDataCell><strong>{key}</strong></CTableDataCell>
              <CTableDataCell>
                {typeof value === 'object' ? JSON.stringify(value, null, 2) : String(value)}
              </CTableDataCell>
            </CTableRow>
          ))}
        </CTableBody>
      </CTable>
    );
  };

  // Pagination helper functions
  const getPaginatedData = (data, page) => {
    const startIndex = (page - 1) * itemsPerPage;
    const endIndex = startIndex + itemsPerPage;
    return data.slice(startIndex, endIndex);
  };

  const getTotalPages = (dataLength) => {
    return Math.ceil(dataLength / itemsPerPage);
  };

  const renderPagination = (currentPage, totalItems, onPageChange) => {
    const totalPages = getTotalPages(totalItems);
    
    if (totalPages <= 1) return null;

    const pages = [];
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
          <CPaginationItem
            disabled={currentPage === 1}
            onClick={() => onPageChange(currentPage - 1)}
          >
            Previous
          </CPaginationItem>
          
          {startPage > 1 && (
            <>
              <CPaginationItem onClick={() => onPageChange(1)}>1</CPaginationItem>
              {startPage > 2 && <CPaginationItem disabled>...</CPaginationItem>}
            </>
          )}
          
          {Array.from({ length: endPage - startPage + 1 }, (_, i) => startPage + i).map((page) => (
            <CPaginationItem
              key={page}
              active={page === currentPage}
              onClick={() => onPageChange(page)}
            >
              {page}
            </CPaginationItem>
          ))}
          
          {endPage < totalPages && (
            <>
              {endPage < totalPages - 1 && <CPaginationItem disabled>...</CPaginationItem>}
              <CPaginationItem onClick={() => onPageChange(totalPages)}>{totalPages}</CPaginationItem>
            </>
          )}
          
          <CPaginationItem
            disabled={currentPage === totalPages}
            onClick={() => onPageChange(currentPage + 1)}
          >
            Next
          </CPaginationItem>
        </CPagination>
      </div>
    );
  };

  if (loading) {
    return (
      <div className="text-center py-5">
        <CSpinner color="primary" />
      </div>
    );
  }

  if (!catalog) {
    return null;
  }

  return (
    <div className="mt-4">
      {/* Header with Back Button */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <h3>Catalog Details: {catalog.name}</h3>
        <CButton color="secondary" onClick={onClose}>
          ← Back to Catalogs
        </CButton>
      </div>

      {/* Metadata Card */}
      <CCard className="mb-4">
        <CCardHeader>
          <strong>Metadata</strong>
        </CCardHeader>
        <CCardBody>
          <CRow>
            <CCol md={6}>
              <table className="table table-borderless mb-0">
                <tbody>
                  <tr>
                    <th style={{ width: '150px' }}>Catalog Name:</th>
                    <td><strong>{catalog.name}</strong></td>
                  </tr>
                  <tr>
                    <th>Owner:</th>
                    <td>{catalog.owner}</td>
                  </tr>
                  <tr>
                    <th>Description:</th>
                    <td>{catalog.description || '-'}</td>
                  </tr>
                </tbody>
              </table>
            </CCol>
            <CCol md={6}>
              <table className="table table-borderless mb-0">
                <tbody>
                  <tr>
                    <th style={{ width: '150px' }}>Target Directory:</th>
                    <td><code>{catalog.target_directory}</code></td>
                  </tr>
                  <tr>
                    <th>Created:</th>
                    <td>{new Date(catalog.created_at).toLocaleString()}</td>
                  </tr>
                  <tr>
                    <th>Last Updated:</th>
                    <td>{new Date(catalog.updated_at).toLocaleString()}</td>
                  </tr>
                </tbody>
              </table>
            </CCol>
          </CRow>
        </CCardBody>
      </CCard>

      {/* Files Card */}
      <CCard className="mb-4">
        <CCardHeader>
          <strong>Files</strong>
          <CBadge color="info" className="ms-2">{catalog.files?.length || 0}</CBadge>
        </CCardHeader>
        <CCardBody>
          {catalog.files && catalog.files.length > 0 ? (
            <>
              <CTable hover responsive>
                <CTableHead>
                  <CTableRow>
                    <CTableHeaderCell>File Name</CTableHeaderCell>
                    <CTableHeaderCell>Path</CTableHeaderCell>
                    <CTableHeaderCell>Type</CTableHeaderCell>
                    <CTableHeaderCell>Size</CTableHeaderCell>
                    <CTableHeaderCell>Permissions</CTableHeaderCell>
                    <CTableHeaderCell>Added</CTableHeaderCell>
                    <CTableHeaderCell>Actions</CTableHeaderCell>
                  </CTableRow>
                </CTableHead>
                <CTableBody>
                  {getPaginatedData(catalog.files, filesPage).map((file) => {
                    return (
                      <React.Fragment key={file.id}>
                        <CTableRow>
                          <CTableDataCell><strong>{file.file_name}</strong></CTableDataCell>
                          <CTableDataCell><code className="small">{file.file_path}</code></CTableDataCell>
                          <CTableDataCell><CBadge color="info">{file.file_type}</CBadge></CTableDataCell>
                          <CTableDataCell>{formatBytes(file.file_size)}</CTableDataCell>
                          <CTableDataCell><code>{file.file_permissions || '-'}</code></CTableDataCell>
                          <CTableDataCell>{new Date(file.added_at).toLocaleString()}</CTableDataCell>
                          <CTableDataCell>
                            <CButton
                              color="primary"
                              size="sm"
                              onClick={() => toggleExifData(file.id)}
                            >
                              {expandedFileId === file.id ? 'Hide Details' : 'Show Details'}
                            </CButton>
                          </CTableDataCell>
                        </CTableRow>
                        {expandedFileId === file.id && (
                          <CTableRow>
                            <CTableDataCell colSpan="7">
                              <CCard className="mt-2 mb-2">
                                <CCardHeader>
                                  <strong>File Details for {file.file_name}</strong>
                                </CCardHeader>
                                <CCardBody>
                                  {renderExifData(file.exif_data)}
                                </CCardBody>
                              </CCard>
                            </CTableDataCell>
                          </CTableRow>
                        )}
                      </React.Fragment>
                    );
                  })}
                </CTableBody>
              </CTable>
              {renderPagination(filesPage, catalog.files.length, setFilesPage)}
            </>
          ) : (
            <div className="text-center text-muted py-4">No files in this catalog</div>
          )}
        </CCardBody>
      </CCard>

      {/* Operations Card */}
      <CCard className="mb-4">
        <CCardHeader>
          <strong>Operations</strong>
          <CBadge color="info" className="ms-2">{catalog.operations?.length || 0}</CBadge>
        </CCardHeader>
        <CCardBody>
          {catalog.operations && catalog.operations.length > 0 ? (
            <CTable hover responsive>
              <CTableHead>
                <CTableRow>
                  <CTableHeaderCell>Operation Type</CTableHeaderCell>
                  <CTableHeaderCell>File Path</CTableHeaderCell>
                  <CTableHeaderCell>Status</CTableHeaderCell>
                  <CTableHeaderCell>Details</CTableHeaderCell>
                  <CTableHeaderCell>Date</CTableHeaderCell>
                </CTableRow>
              </CTableHead>
              <CTableBody>
                {catalog.operations.map((op) => (
                  <CTableRow key={op.id}>
                    <CTableDataCell><CBadge color="primary">{op.operation_type}</CBadge></CTableDataCell>
                    <CTableDataCell><code className="small">{op.file_path}</code></CTableDataCell>
                    <CTableDataCell>
                      <CBadge color={op.status === 'success' ? 'success' : 'danger'}>
                        {op.status}
                      </CBadge>
                    </CTableDataCell>
                    <CTableDataCell>{op.details || '-'}</CTableDataCell>
                    <CTableDataCell>{new Date(op.operation_date).toLocaleString()}</CTableDataCell>
                  </CTableRow>
                ))}
              </CTableBody>
            </CTable>
          ) : (
            <div className="text-center text-muted py-4">No operations recorded</div>
          )}
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
                  </CTableRow>
                </CTableHead>
                <CTableBody>
                  {getPaginatedData(catalog.workflow_executions, workflowExecutionsPage).map((exec) => (
                    <CTableRow key={exec.id}>
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
                    </CTableRow>
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
  );
};

export default CatalogDetail;

// Made with Bob