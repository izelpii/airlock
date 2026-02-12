import React, { useState, useEffect } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CTable,
  CTableHead,
  CTableRow,
  CTableHeaderCell,
  CTableBody,
  CTableDataCell,
  CButton,
  CSpinner,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPencil, cilTrash, cilPlus, cilFolderOpen } from '@coreui/icons';
import { catalogAPI } from '../services/api';

const CatalogList = ({ onEdit, onCreate, onView }) => {
  const [catalogs, setCatalogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    loadCatalogs();
  }, []);

  const loadCatalogs = async () => {
    try {
      setLoading(true);
      const response = await catalogAPI.getAll();
      setCatalogs(response.data);
      setError(null);
    } catch (err) {
      setError('Failed to load catalogs');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id) => {
    if (window.confirm('Are you sure you want to delete this catalog? This will also delete all associated files, operations, and workflow executions.')) {
      try {
        await catalogAPI.delete(id);
        loadCatalogs();
      } catch (err) {
        alert('Failed to delete catalog');
        console.error(err);
      }
    }
  };

  if (loading) {
    return (
      <CCard>
        <CCardBody className="text-center">
          <CSpinner color="primary" />
        </CCardBody>
      </CCard>
    );
  }

  return (
    <CCard>
      <CCardHeader className="d-flex justify-content-between align-items-center">
        <strong>Catalogs</strong>
        <CButton color="primary" size="sm" onClick={onCreate}>
          <CIcon icon={cilPlus} className="me-2" />
          Create Catalog
        </CButton>
      </CCardHeader>
      <CCardBody>
        {error && (
          <div className="alert alert-danger" role="alert">
            {error}
          </div>
        )}
        {catalogs.length === 0 ? (
          <div className="text-center text-muted py-4">
            No catalogs found. Create your first catalog to get started.
          </div>
        ) : (
          <CTable hover responsive>
            <CTableHead>
              <CTableRow>
                <CTableHeaderCell>Name</CTableHeaderCell>
                <CTableHeaderCell>Owner</CTableHeaderCell>
                <CTableHeaderCell>Description</CTableHeaderCell>
                <CTableHeaderCell>Target Directory</CTableHeaderCell>
                <CTableHeaderCell>Created</CTableHeaderCell>
                <CTableHeaderCell>Actions</CTableHeaderCell>
              </CTableRow>
            </CTableHead>
            <CTableBody>
              {catalogs.map((catalog) => (
                <CTableRow key={catalog.id}>
                  <CTableDataCell>
                    <strong>{catalog.name}</strong>
                  </CTableDataCell>
                  <CTableDataCell>{catalog.owner}</CTableDataCell>
                  <CTableDataCell>{catalog.description || '-'}</CTableDataCell>
                  <CTableDataCell>
                    <code className="text-muted small">{catalog.target_directory}</code>
                  </CTableDataCell>
                  <CTableDataCell>
                    {new Date(catalog.created_at).toLocaleDateString()}
                  </CTableDataCell>
                  <CTableDataCell>
                    <CButton
                      color="primary"
                      size="sm"
                      className="me-2"
                      onClick={() => onView(catalog)}
                    >
                      <CIcon icon={cilFolderOpen} />
                    </CButton>
                    <CButton
                      color="info"
                      size="sm"
                      className="me-2"
                      onClick={() => onEdit(catalog)}
                    >
                      <CIcon icon={cilPencil} />
                    </CButton>
                    <CButton
                      color="danger"
                      size="sm"
                      onClick={() => handleDelete(catalog.id)}
                    >
                      <CIcon icon={cilTrash} />
                    </CButton>
                  </CTableDataCell>
                </CTableRow>
              ))}
            </CTableBody>
          </CTable>
        )}
      </CCardBody>
    </CCard>
  );
};

export default CatalogList;

// Made with Bob