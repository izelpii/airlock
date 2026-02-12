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
  CBadge,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilMediaPlay,
  cilPencil,
  cilTrash,
  cilPlus
} from '@coreui/icons';
import { workflowAPI, catalogAPI } from '../services/api';

const WorkflowList = ({ onEdit, onCreate, onExecute }) => {
  const [workflows, setWorkflows] = useState([]);
  const [catalogs, setCatalogs] = useState({});
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  useEffect(() => {
    loadData();
  }, []);

  const loadData = async () => {
    try {
      setLoading(true);
      const [workflowsResponse, catalogsResponse] = await Promise.all([
        workflowAPI.getAll(),
        catalogAPI.getAll()
      ]);
      setWorkflows(workflowsResponse.data);
      
      // Create a map of catalog_id to catalog for easy lookup
      const catalogMap = {};
      catalogsResponse.data.forEach(catalog => {
        catalogMap[catalog.id] = catalog;
      });
      setCatalogs(catalogMap);
      setError(null);
    } catch (err) {
      setError('Failed to load workflows');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id) => {
    if (window.confirm('Are you sure you want to delete this workflow?')) {
      try {
        await workflowAPI.delete(id);
        loadData();
      } catch (err) {
        alert('Failed to delete workflow');
        console.error(err);
      }
    }
  };

  const getCatalogInfo = (catalogId) => {
    const catalog = catalogs[catalogId];
    if (!catalog) {
      return { name: 'Unknown', targetDirectory: 'N/A' };
    }
    return {
      name: catalog.name,
      targetDirectory: catalog.target_directory
    };
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
        <strong>Workflows</strong>
        <CButton color="primary" size="sm" onClick={onCreate}>
          <CIcon icon={cilPlus} className="me-2" />
          Create Workflow
        </CButton>
      </CCardHeader>
      <CCardBody>
        {error && (
          <div className="alert alert-danger" role="alert">
            {error}
          </div>
        )}
        {workflows.length === 0 ? (
          <div className="text-center text-muted py-4">
            No workflows found. Create your first workflow to get started.
          </div>
        ) : (
          <CTable hover responsive>
            <CTableHead>
              <CTableRow>
                <CTableHeaderCell>Name</CTableHeaderCell>
                <CTableHeaderCell>Description</CTableHeaderCell>
                <CTableHeaderCell>Source Directory</CTableHeaderCell>
                <CTableHeaderCell>Catalog</CTableHeaderCell>
                <CTableHeaderCell>Target Directory</CTableHeaderCell>
                <CTableHeaderCell>Actions</CTableHeaderCell>
              </CTableRow>
            </CTableHead>
            <CTableBody>
              {workflows.map((workflow) => {
                const catalogInfo = getCatalogInfo(workflow.catalog_id);
                return (
                  <CTableRow key={workflow.id}>
                    <CTableDataCell>
                      <strong>{workflow.name}</strong>
                    </CTableDataCell>
                    <CTableDataCell>{workflow.description || '-'}</CTableDataCell>
                    <CTableDataCell>
                      <code className="text-muted small">{workflow.source_directory}</code>
                    </CTableDataCell>
                    <CTableDataCell>
                      <strong>{catalogInfo.name}</strong>
                    </CTableDataCell>
                    <CTableDataCell>
                      <code className="text-muted small">{catalogInfo.targetDirectory}</code>
                    </CTableDataCell>
                    <CTableDataCell>
                    <CButton
                      color="success"
                      size="sm"
                      className="me-2"
                      onClick={() => onExecute(workflow)}
                    >
                      <CIcon icon={cilMediaPlay} />
                    </CButton>
                    <CButton
                      color="info"
                      size="sm"
                      className="me-2"
                      onClick={() => onEdit(workflow)}
                    >
                      <CIcon icon={cilPencil} />
                    </CButton>
                    <CButton
                      color="danger"
                      size="sm"
                      onClick={() => handleDelete(workflow.id)}
                    >
                      <CIcon icon={cilTrash} />
                    </CButton>
                    </CTableDataCell>
                  </CTableRow>
                );
              })}
            </CTableBody>
          </CTable>
        )}
      </CCardBody>
    </CCard>
  );
};

export default WorkflowList;

// Made with Bob
