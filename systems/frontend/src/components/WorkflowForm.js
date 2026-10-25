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
import { workflowAPI, storageRouteAPI } from '../services/api';
import FolderCombo from './FolderCombo';

const WorkflowForm = ({ visible, onClose, workflow, onSuccess, catalogId }) => {
  const WORKFLOW_TYPE_SUGGESTIONS = [
    'Import', 'Export', 'Processing', 'AI / ML', 'Face Recognition',
    'Tagging', 'Archiving', 'Reporting', 'Cleanup', 'Sync', 'Create Events',
  ];

  const [formData, setFormData] = useState({
    name: '',
    description: '',
    source_directory: '',
    source_route_id: '',
    source_subpath: '',
    python_script: '',
    workflow_type: '',
  });
  // Routes this catalog may draw from. A route beats the literal path: the
  // route is the intent, the path is its cached rendering, and the backend
  // rewrites the path from the route at launch.
  const [routes, setRoutes] = useState([]);

  useEffect(() => {
    if (!visible || !catalogId) return;
    storageRouteAPI.forCatalog(catalogId)
      // The endpoint returns an envelope - the mount status alongside the
      // routes - not a bare array.
      .then((r) => setRoutes(r.data?.routes || []))
      // A catalog with no routes linked is normal, not an error: the raw path
      // below still works, which is what keeps existing workflows running.
      .catch(() => setRoutes([]));
  }, [visible, catalogId]);
  const [scripts, setScripts] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (visible) {
      loadScripts();
    }
    if (workflow) {
      setFormData({
        name: workflow.name || '',
        description: workflow.description || '',
        source_directory: workflow.source_directory || '',
        source_route_id: workflow.source_route_id || '',
        source_subpath: workflow.source_subpath || '',
        python_script: workflow.python_script || '',
        workflow_type: workflow.workflow_type || '',
      });
    } else {
      setFormData({
        name: '',
        description: '',
        source_directory: '',
        source_route_id: '',
        source_subpath: '',
        python_script: '',
        workflow_type: '',
      });
    }
    setError(null);
  }, [workflow, visible]);

  const loadScripts = async () => {
    try {
      const response = await workflowAPI.getScripts();
      setScripts(response.data);
    } catch (err) {
      console.error('Failed to load workflow scripts:', err);
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
        await workflowAPI.update(catalogId, workflow.id, formData);
      } else {
        await workflowAPI.create(catalogId, formData);
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
          {/* Where the files come from.
              A storage route is a named folder an administrator has already
              made reachable, so this is a list rather than a path the user has
              to get right. The raw path stays available for the cases routes do
              not cover yet — see documentation/specs/local-storage-spec.md. */}
          <div className="mb-3">
            <CFormLabel htmlFor="source_route_id">Source</CFormLabel>
            <CFormSelect
              id="source_route_id"
              name="source_route_id"
              value={formData.source_route_id}
              onChange={handleChange}
            >
              <option value="">— use a path below —</option>
              {routes.map((r) => (
                <option key={r.id} value={r.id} disabled={!r.available}>
                  {r.name}{r.available ? '' : ' (unavailable)'} — {r.host_path}
                </option>
              ))}
            </CFormSelect>
            {routes.length === 0 && (
              <small className="text-muted">
                No storage routes are linked to this catalog. Link one on the
                Catalogs page, or give a path below.
              </small>
            )}
          </div>

          {formData.source_route_id && (
            <div className="mb-3">
              <CFormLabel htmlFor="source_subpath">Subfolder (optional)</CFormLabel>
              {/* Browsed from the server, because only the server can see the
                  folder. Type to filter, or type a path outright. */}
              <FolderCombo
                routeId={Number(formData.source_route_id)}
                value={formData.source_subpath}
                onChange={(v) => setFormData({ ...formData, source_subpath: v })}
              />
              <small className="text-muted">
                A folder inside the route. Leave empty to import the whole route.
              </small>
            </div>
          )}

          {!formData.source_route_id && (
            <div className="mb-3">
              <CFormLabel htmlFor="source_directory">Source Directory</CFormLabel>
              <CFormInput
                type="text"
                id="source_directory"
                name="source_directory"
                value={formData.source_directory}
                onChange={handleChange}
                placeholder="/input"
              />
              <small className="text-muted">
                A path the backend container can already see, such as{' '}
                <code>/input</code>. A storage route above is usually the better
                answer — nothing validates this field.
              </small>
            </div>
          )}
          <div className="mb-3">
            <CFormLabel htmlFor="workflow_type">Type</CFormLabel>
            <input
              type="text"
              id="workflow_type"
              name="workflow_type"
              list="workflow-type-suggestions"
              className="form-control"
              value={formData.workflow_type}
              onChange={handleChange}
              placeholder="e.g. Import, Processing, AI / ML…"
            />
            <datalist id="workflow-type-suggestions">
              {WORKFLOW_TYPE_SUGGESTIONS.map((t) => (
                <option key={t} value={t} />
              ))}
            </datalist>
          </div>
          <div className="mb-3">
            <CFormLabel htmlFor="python_script">Python Script *</CFormLabel>
            <CFormSelect
              id="python_script"
              name="python_script"
              value={formData.python_script}
              onChange={handleChange}
              required
            >
              <option value="">Select a workflow script</option>
              {scripts.map((script) => (
                <option key={script.path} value={script.path}>
                  {script.name} ({script.filename})
                </option>
              ))}
            </CFormSelect>
            <small className="text-muted">
              Select the Python script that implements the workflow from the workflows directory
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
