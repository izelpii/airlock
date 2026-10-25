import React, { useState, useEffect } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CContainer,
  CRow,
  CCol,
  CSpinner,
  CAlert,
  CTable,
  CTableHead,
  CTableRow,
  CTableHeaderCell,
  CTableBody,
  CTableDataCell,
  CProgress,
  CProgressBar,
  CPagination,
  CPaginationItem
} from '@coreui/react';
import { PieChart, Pie, Cell, ResponsiveContainer, Legend, Tooltip } from 'recharts';
import { catalogAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import LeftInSource from '../components/LeftInSource';

// A CProgressBar with a colour of our own. CoreUI sets the bar's width through
// its `style` and then spreads ours over it, so a style carrying only the
// colour drew the bar at zero width. The width goes in with the colour.
const barStyle = (value, color) => ({ width: `${value}%`, backgroundColor: color });

const Operations = () => {
  const { activeCatalog } = useCatalog();
  const [verifying, setVerifying] = useState(false);
  const [verificationResult, setVerificationResult] = useState(null);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (activeCatalog) {
      handleVerifyCatalog();
    }
  }, [activeCatalog]);

  // Pagination state for verification file lists
  const itemsPerPage = 8; // Same as CatalogDetail.js
  const [v1ExistPage, setV1ExistPage] = useState(1);
  const [v1MissingPage, setV1MissingPage] = useState(1);
  const [v2CatalogedPage, setV2CatalogedPage] = useState(1);
  const [v2CachePage, setV2CachePage] = useState(1);
  const [v2NotCatalogedPage, setV2NotCatalogedPage] = useState(1);

  const handleVerifyCatalog = async () => {
    if (!activeCatalog) {
      setError('Please select a catalog');
      return;
    }

    try {
      setVerifying(true);
      setError(null);
      setVerificationResult(null);
      
      // Reset pagination when new verification is run
      setV1ExistPage(1);
      setV1MissingPage(1);
      setV2CatalogedPage(1);
      setV2CachePage(1);
      setV2NotCatalogedPage(1);
      
      const response = await catalogAPI.verify(activeCatalog.id);
      setVerificationResult(response.data);
    } catch (err) {
      setError('Verification failed: ' + err.message);
    } finally {
      setVerifying(false);
    }
  };

  const formatFileSize = (bytes) => {
    if (!bytes) return '0 B';
    const k = 1024;
    const sizes = ['B', 'KB', 'MB', 'GB'];
    const i = Math.floor(Math.log(bytes) / Math.log(k));
    return Math.round(bytes / Math.pow(k, i) * 100) / 100 + ' ' + sizes[i];
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

  const COLORS = {
    green: '#0d6efd',  // Blue for files that exist/cataloged (matches theme)
    red: '#dc3545',    // Standard red for files that don't exist/not cataloged
    orange: '#fd7e14'  // Orange for cache files (in tmp / alternative_versions)
  };

  const renderVerificationResults = () => {
    if (!verificationResult) return null;

    const { verification_1, verification_2 } = verificationResult;

    // Prepare data for pie charts
    const catalogVsDirectoryData = [
      { name: 'Exist in Directory', value: verification_1.exist_in_directory, color: COLORS.green },
      { name: 'Missing in Directory', value: verification_1.missing_in_directory, color: COLORS.red }
    ];

    const cacheCount = verification_2.cache || 0;
    const directoryVsCatalogData = [
      { name: 'Cataloged', value: verification_2.cataloged, color: COLORS.green },
      ...(cacheCount > 0 ? [{ name: 'Cache', value: cacheCount, color: COLORS.orange }] : []),
      { name: 'Not Cataloged', value: verification_2.not_cataloged, color: COLORS.red }
    ];

    return (
      <div className="mt-4">
        <CAlert color="info">
          <strong>Catalog:</strong> {verificationResult.catalog_name}<br />
          <strong>Target Directory:</strong> {verificationResult.target_directory}
        </CAlert>

        {/* Verification 1: Files in Catalog vs Directory */}
        <CCard className="mb-4">
          <CCardHeader>
            <strong>Verification 1: Files in Catalog vs Files in Directory</strong>
          </CCardHeader>
          <CCardBody>
            <CRow>
              <CCol md={6}>
                <h5>Summary</h5>
                <p className="text-muted">{verification_1.description}</p>
                <div className="mb-3">
                  <strong>Total files in catalog:</strong> {verification_1.total_in_catalog}<br />
                  <strong style={{ color: COLORS.green }}>Exist in directory:</strong> {verification_1.exist_in_directory}<br />
                  <strong style={{ color: COLORS.red }}>Missing in directory:</strong> {verification_1.missing_in_directory}
                </div>
                <CProgress className="mb-2">
                  <CProgressBar
                    style={barStyle((verification_1.exist_in_directory / verification_1.total_in_catalog) * 100, COLORS.green)}
                  >
                    {Math.round((verification_1.exist_in_directory / verification_1.total_in_catalog) * 100)}%
                  </CProgressBar>
                </CProgress>
              </CCol>
              <CCol md={6}>
                <ResponsiveContainer width="100%" height={250}>
                  <PieChart>
                    <Pie
                      data={catalogVsDirectoryData}
                      cx="50%"
                      cy="50%"
                      labelLine={false}
                      label={({ name, percent }) => `${name}: ${(percent * 100).toFixed(0)}%`}
                      outerRadius={80}
                      fill="#8884d8"
                      dataKey="value"
                    >
                      {catalogVsDirectoryData.map((entry, index) => (
                        <Cell key={`cell-${index}`} fill={entry.color} />
                      ))}
                    </Pie>
                    <Tooltip />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              </CCol>
            </CRow>

            {/* Files that exist */}
            {verification_1.files_exist.length > 0 && (
              <div className="mt-4">
                <h6 style={{ color: COLORS.green }}>Files Exist in Directory ({verification_1.files_exist.length})</h6>
                <CTable small hover>
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell>File Name</CTableHeaderCell>
                      <CTableHeaderCell>File Path</CTableHeaderCell>
                      <CTableHeaderCell>Size</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {getPaginatedData(verification_1.files_exist, v1ExistPage).map((file, idx) => (
                      <CTableRow key={idx}>
                        <CTableDataCell>{file.file_name}</CTableDataCell>
                        <CTableDataCell><small>{file.file_path}</small></CTableDataCell>
                        <CTableDataCell>{formatFileSize(file.file_size)}</CTableDataCell>
                      </CTableRow>
                    ))}
                  </CTableBody>
                </CTable>
                {renderPagination(v1ExistPage, verification_1.files_exist.length, setV1ExistPage)}
              </div>
            )}

            {/* Files that are missing */}
            {verification_1.files_missing.length > 0 && (
              <div className="mt-4">
                <h6 style={{ color: COLORS.red }}>Files Missing in Directory ({verification_1.files_missing.length})</h6>
                <CTable small hover>
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell>File Name</CTableHeaderCell>
                      <CTableHeaderCell>File Path</CTableHeaderCell>
                      <CTableHeaderCell>Size</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {getPaginatedData(verification_1.files_missing, v1MissingPage).map((file, idx) => (
                      <CTableRow key={idx}>
                        <CTableDataCell>{file.file_name}</CTableDataCell>
                        <CTableDataCell><small>{file.file_path}</small></CTableDataCell>
                        <CTableDataCell>{formatFileSize(file.file_size)}</CTableDataCell>
                      </CTableRow>
                    ))}
                  </CTableBody>
                </CTable>
                {renderPagination(v1MissingPage, verification_1.files_missing.length, setV1MissingPage)}
              </div>
            )}
          </CCardBody>
        </CCard>

        {/* Verification 2: Files in Directory vs Catalog */}
        <CCard className="mb-4">
          <CCardHeader>
            <strong>Verification 2: Files in Directory vs Files in Catalog</strong>
          </CCardHeader>
          <CCardBody>
            <CRow>
              <CCol md={6}>
                <h5>Summary</h5>
                <p className="text-muted">{verification_2.description}</p>
                <div className="mb-3">
                  <strong>Total files in directory:</strong> {verification_2.total_in_directory}<br />
                  <strong style={{ color: COLORS.green }}>Cataloged:</strong> {verification_2.cataloged}<br />
                  {cacheCount > 0 && (
                    <><strong style={{ color: COLORS.orange }}>Cache (tmp):</strong> {cacheCount}<br /></>
                  )}
                  <strong style={{ color: COLORS.red }}>Not cataloged:</strong> {verification_2.not_cataloged}
                </div>
                <CProgress className="mb-2">
                  <CProgressBar
                    style={barStyle((verification_2.cataloged / verification_2.total_in_directory) * 100, COLORS.green)}
                  />
                  {cacheCount > 0 && (
                    <CProgressBar
                      style={barStyle((cacheCount / verification_2.total_in_directory) * 100, COLORS.orange)}
                    />
                  )}
                </CProgress>
              </CCol>
              <CCol md={6}>
                <ResponsiveContainer width="100%" height={250}>
                  <PieChart>
                    <Pie
                      data={directoryVsCatalogData}
                      cx="50%"
                      cy="50%"
                      labelLine={false}
                      label={({ name, percent }) => `${name}: ${(percent * 100).toFixed(0)}%`}
                      outerRadius={80}
                      fill="#8884d8"
                      dataKey="value"
                    >
                      {directoryVsCatalogData.map((entry, index) => (
                        <Cell key={`cell-${index}`} fill={entry.color} />
                      ))}
                    </Pie>
                    <Tooltip />
                    <Legend />
                  </PieChart>
                </ResponsiveContainer>
              </CCol>
            </CRow>

            {/* Files that are cataloged */}
            {verification_2.files_cataloged.length > 0 && (
              <div className="mt-4">
                <h6 style={{ color: COLORS.green }}>Files Cataloged ({verification_2.files_cataloged.length})</h6>
                <CTable small hover>
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell>File Name</CTableHeaderCell>
                      <CTableHeaderCell>File Path</CTableHeaderCell>
                      <CTableHeaderCell>Size</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {getPaginatedData(verification_2.files_cataloged, v2CatalogedPage).map((file, idx) => (
                      <CTableRow key={idx}>
                        <CTableDataCell>{file.file_name}</CTableDataCell>
                        <CTableDataCell><small>{file.file_path}</small></CTableDataCell>
                        <CTableDataCell>{formatFileSize(file.file_size)}</CTableDataCell>
                      </CTableRow>
                    ))}
                  </CTableBody>
                </CTable>
                {renderPagination(v2CatalogedPage, verification_2.files_cataloged.length, setV2CatalogedPage)}
              </div>
            )}

            {/* Cache files (in tmp, catalogued via alternative_versions) */}
            {(verification_2.files_cache || []).length > 0 && (
              <div className="mt-4">
                <h6 style={{ color: COLORS.orange }}>
                  Cache — Files in tmp ({verification_2.files_cache.length})
                </h6>
                <p className="text-muted small mb-2">
                  These files are stored in the <code>tmp</code> folder and are catalogued as alternative versions.
                </p>
                <CTable small hover>
                  <CTableHead>
                    <CTableRow style={{ backgroundColor: '#fff3e0' }}>
                      <CTableHeaderCell>File Name</CTableHeaderCell>
                      <CTableHeaderCell>File Path</CTableHeaderCell>
                      <CTableHeaderCell>Size</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {getPaginatedData(verification_2.files_cache, v2CachePage).map((file, idx) => (
                      <CTableRow key={idx} style={{ backgroundColor: '#fff8f0' }}>
                        <CTableDataCell style={{ color: COLORS.orange }}>{file.file_name}</CTableDataCell>
                        <CTableDataCell><small style={{ color: COLORS.orange }}>{file.file_path}</small></CTableDataCell>
                        <CTableDataCell>{formatFileSize(file.file_size)}</CTableDataCell>
                      </CTableRow>
                    ))}
                  </CTableBody>
                </CTable>
                {renderPagination(v2CachePage, verification_2.files_cache.length, setV2CachePage)}
              </div>
            )}

            {/* Files that are not cataloged */}
            {verification_2.files_not_cataloged.length > 0 && (
              <div className="mt-4">
                <h6 style={{ color: COLORS.red }}>Files Not Cataloged ({verification_2.files_not_cataloged.length})</h6>
                <CTable small hover>
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell>File Name</CTableHeaderCell>
                      <CTableHeaderCell>File Path</CTableHeaderCell>
                      <CTableHeaderCell>Size</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {getPaginatedData(verification_2.files_not_cataloged, v2NotCatalogedPage).map((file, idx) => (
                      <CTableRow key={idx}>
                        <CTableDataCell>{file.file_name}</CTableDataCell>
                        <CTableDataCell><small>{file.file_path}</small></CTableDataCell>
                        <CTableDataCell>{formatFileSize(file.file_size)}</CTableDataCell>
                      </CTableRow>
                    ))}
                  </CTableBody>
                </CTable>
                {renderPagination(v2NotCatalogedPage, verification_2.files_not_cataloged.length, setV2NotCatalogedPage)}
              </div>
            )}
          </CCardBody>
        </CCard>
      </div>
    );
  };

  if (!activeCatalog) {
    return (
      <CContainer fluid className="py-4 px-4">
        <div className="text-center py-5 text-muted">
          <strong>No catalog selected</strong>
          <div style={{ marginTop: 8, fontSize: '0.9rem' }}>
            Select a catalog from the dropdown in the top menu to get started.
          </div>
        </div>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="py-4 px-4">
      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Workflow Manager</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Verify catalog integrity and manage workflows
          </div>
        </div>
      </div>

      {error && (
        <CAlert color="danger" dismissible onClose={() => setError(null)}>
          {error}
        </CAlert>
      )}

      {/* Move-mode catalogs only; renders nothing for the others. */}
      <LeftInSource key={activeCatalog.id} catalogId={activeCatalog.id} />

      {verifying && (
        <div className="text-center py-5">
          <CSpinner color="primary" />
          <div className="mt-2 text-muted">Verifying catalog...</div>
        </div>
      )}

      {!verifying && renderVerificationResults()}
    </CContainer>
  );
};

export default Operations;
