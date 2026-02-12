import React, { useState, useEffect } from 'react';
import {
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CModalFooter,
  CButton,
  CForm,
  CFormLabel,
  CFormInput,
  CFormTextarea,
  CSpinner,
} from '@coreui/react';
import { catalogAPI } from '../services/api';

const CatalogForm = ({ visible, onClose, catalog, onSuccess }) => {
  const [formData, setFormData] = useState({
    name: '',
    owner: '',
    description: '',
    target_directory: '',
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (catalog) {
      setFormData({
        name: catalog.name || '',
        owner: catalog.owner || '',
        description: catalog.description || '',
        target_directory: catalog.target_directory || '',
      });
    } else {
      setFormData({
        name: '',
        owner: '',
        description: '',
        target_directory: '',
      });
    }
    setError(null);
  }, [catalog, visible]);

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
      if (catalog) {
        await catalogAPI.update(catalog.id, formData);
      } else {
        await catalogAPI.create(formData);
      }
      onSuccess();
      onClose();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to save catalog');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  return (
    <CModal visible={visible} onClose={onClose} size="lg">
      <CModalHeader>
        <CModalTitle>{catalog ? 'Edit Catalog' : 'Create New Catalog'}</CModalTitle>
      </CModalHeader>
      <CForm onSubmit={handleSubmit}>
        <CModalBody>
          {error && (
            <div className="alert alert-danger" role="alert">
              {error}
            </div>
          )}
          <div className="mb-3">
            <CFormLabel htmlFor="name">Catalog Name *</CFormLabel>
            <CFormInput
              type="text"
              id="name"
              name="name"
              value={formData.name}
              onChange={handleChange}
              required
              placeholder="Enter catalog name"
            />
          </div>
          <div className="mb-3">
            <CFormLabel htmlFor="owner">Owner *</CFormLabel>
            <CFormInput
              type="text"
              id="owner"
              name="owner"
              value={formData.owner}
              onChange={handleChange}
              required
              placeholder="Enter owner name"
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
              placeholder="Enter catalog description"
            />
          </div>
          <div className="mb-3">
            <CFormLabel htmlFor="target_directory">Target Directory *</CFormLabel>
            <CFormInput
              type="text"
              id="target_directory"
              name="target_directory"
              value={formData.target_directory}
              onChange={handleChange}
              required
              placeholder="/path/to/target/directory"
            />
            <small className="text-muted">
              The directory where catalog files will be stored
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
              catalog ? 'Update Catalog' : 'Create Catalog'
            )}
          </CButton>
        </CModalFooter>
      </CForm>
    </CModal>
  );
};

export default CatalogForm;

// Made with Bob