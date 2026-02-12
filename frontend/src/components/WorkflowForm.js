import React, { useState, useEffect } from 'react';
import {
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CModalFooter,
  CButton,
  CForm,
  CFormInput,
  CFormTextarea,
  CFormLabel,
  CFormSelect,
  CSpinner,
} from '@coreui/react';
import { workflowAPI, catalogAPI } from '../services/api';

const WorkflowForm = ({ visible, onClose, workflow, onSuccess }) => {
  const [formData, setFormData] = useState({
    name: '',
    description: '',
    source_directory: '',
    catalog_id: '',
    python_script: '',
  });
  const [catalogs, setCatalogs] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (visible) {
      loadCatalogs();
    }
    if (workflow) {
      setFormData({
        name: workflow.name || '',
        description: workflow.description || '',
        source_directory: workflow.source_directory || '',
        catalog_id: workflow.catalog_id || '',
        python_script: workflow.python_script || '',
      });
    } else {
      setFormData({
        name: '',
        description: '',
        source_directory: '',
        catalog_id: '',
        python_script: '',
      });
    }
    setError(null);
  }, [workflow, visible]);

  const loadCatalogs = async () => {
    try {
      const response = await catalogAPI.getAll();
      setCatalogs(response.data);
    } catch (err) {
      console.error('Failed to load catalogs:', err);
    }
  };

  const handleChange = (e) => {
    const { name, value } = e.target;
    setFormData((prev) => ({
      ...prev,
      [name]: value,
    }));
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    try {
      if (workflow) {
        await workflowAPI.update(workflow.id, formData);
      } else {
        await workflowAPI.create(formData);
      }
      onSuccess();
      onClose();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to save workflow');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <CModal visible={visible} onClose={onClose} size="lg">
      <CModalHeader>
        <CModalTitle>{workflow ? 'Edit Workflow' : 'Create Workflow'}</CModalTitle>
      </CModalHeader>
      <CForm onSubmit={handleSubmit}>
        <CModalBody>
          {error && (
            <div className="alert alert-danger" role="alert">
              {error}
            </div>
          )}
          <div className="mb-3">
            <CFormLabel htmlFor="name">Name *</CFormLabel>
            <CFormInput
              type="text"
              id="name"
              name="name"
              value={formData.name}
              onChange={handleChange}
              required
              placeholder="Enter workflow name"
            />
          </div>
          <div className="mb-3">
            <CFormLabel htmlFor="description">Description</CFormLabel>
            <CFormTextarea
              id="description"
              name="description"
              value={formData.description}
              onChange={handleChange}
              rows="3"
              placeholder="Enter workflow description"
            />
          </div>
          <div className="mb-3">
            <CFormLabel htmlFor="source_directory">Source Directory *</CFormLabel>
            <CFormInput
              type="text"
              id="source_directory"
              name="source_directory"
              value={formData.source_directory}
              onChange={handleChange}
              required
              placeholder="/path/to/source"
            />
            <small className="text-muted">
              The directory containing files to process
            </small>
          </div>
          <div className="mb-3">
            <CFormLabel htmlFor="catalog_id">Catalog *</CFormLabel>
            <CFormSelect
              id="catalog_id"
              name="catalog_id"
              value={formData.catalog_id}
              onChange={handleChange}
              required
            >
              <option value="">Select a catalog</option>
              {catalogs.map((catalog) => (
                <option key={catalog.id} value={catalog.id}>
                  {catalog.name} - {catalog.target_directory}
                </option>
              ))}
            </CFormSelect>
            <small className="text-muted">
              The catalog whose target directory will be used for workflow results
            </small>
          </div>
          <div className="mb-3">
            <CFormLabel htmlFor="python_script">Python Script Path *</CFormLabel>
            <CFormInput
              type="text"
              id="python_script"
              name="python_script"
              value={formData.python_script}
              onChange={handleChange}
              required
              placeholder="workflows/file_copy_workflow.py"
            />
            <small className="text-muted">
              Path to the Python script that implements the workflow
            </small>
          </div>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={onClose} disabled={loading}>
            Cancel
          </CButton>
          <CButton color="primary" type="submit" disabled={loading}>
            {loading ? (
              <>
                <CSpinner size="sm" className="me-2" />
                Saving...
              </>
            ) : (
              'Save'
            )}
          </CButton>
        </CModalFooter>
      </CForm>
    </CModal>
  );
};

export default WorkflowForm;

// Made with Bob
