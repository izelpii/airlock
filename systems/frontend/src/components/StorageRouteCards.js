import React, { useState, useEffect, useCallback } from 'react';
import {
  CCard, CCardBody, CCardHeader, CRow, CCol, CButton, CAlert, CSpinner, CBadge,
  CModal, CModalHeader, CModalTitle, CModalBody, CModalFooter,
  CForm, CFormLabel, CFormInput, CFormSwitch,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilTrash, cilPencil, cilPlus, cilFolder } from '@coreui/icons';
import { storageRouteAPI } from '../services/api';

// ─── The open catalog's storage routes, as cards ──────────────────────────────
// Rendered beside the open catalog on the Catalogs page, in the same row, so a
// catalog and the folders it imports from are one thing on screen instead of
// two pages. A route belongs to exactly one catalog, so there was never a
// reason for it to live anywhere else.
//
// Renders as ONE column of the caller's row, holding its own row of cards. That
// nesting is the point: the route cards wrap among themselves inside their
// column, instead of flowing underneath the catalog card when there are more of
// them than fit. The catalog keeps its column whatever the window does.
//
// A route is always a subpath of the ONE storage root mounted into the backend
// container, because a bind mount is fixed when the container is created — a
// folder outside it simply cannot be read, whatever the UI says. That is why
// the add form states the root: a user refused a folder needs to see the rule,
// not just hit it.

const CARD_WIDTH = 320;

const Field = ({ label, children, mono }) => (
  <div className="mb-2">
    <CFormLabel className="small fw-semibold mb-1">{label}</CFormLabel>
    <div style={{
      fontSize: mono ? '0.72rem' : '0.82rem',
      fontFamily: mono ? 'monospace' : undefined,
      color: '#495057',
      wordBreak: 'break-all',
    }}>
      {children}
    </div>
  </div>
);

const RouteCard = ({ route, onEdit, onDelete }) => (
  <CCol xs="auto">
    <CCard className="shadow-sm" style={{ width: CARD_WIDTH }}>
      <CCardHeader
        className="d-flex justify-content-between align-items-start"
        style={{ minHeight: '64px' }}
      >
        <div className="me-2 overflow-hidden">
          <div className="fw-semibold text-truncate d-flex align-items-center gap-2">
            {/* The availability dot is the feature that pays for itself: it
                turns "the import found nothing" into "that folder is not
                there", before a workflow ever runs. */}
            <span
              title={route.available ? 'Available' : 'Not readable right now'}
              style={{
                width: 9, height: 9, borderRadius: '50%', flexShrink: 0,
                background: route.available ? '#2e7d32' : '#c62828',
              }}
            />
            {route.name}
          </div>
          <div className="mt-1 d-flex gap-1 flex-wrap">
            <CBadge color="secondary" style={{ fontSize: '0.7rem' }}>{route.kind}</CBadge>
            {/* "Imports may move files" (ingest-move-spec.md 8.1): only then
                can a move-mode catalog take files out of this folder. */}
            {route.allow_move ? (
              <CBadge color="warning" className="text-dark" style={{ fontSize: '0.7rem' }}
                      title="A catalog that moves files may take them out of this folder">
                imports may move files
              </CBadge>
            ) : (
              <CBadge color="light" className="text-dark" style={{ fontSize: '0.7rem' }}>
                read-only
              </CBadge>
            )}
          </div>
        </div>
        <div className="d-flex flex-shrink-0">
          <CButton color="danger" variant="ghost" size="sm" className="me-1"
                   title="Remove route" onClick={() => onDelete(route)}>
            <CIcon icon={cilTrash} />
          </CButton>
          <CButton color="info" variant="ghost" size="sm"
                   title="Edit route" onClick={() => onEdit(route)}>
            <CIcon icon={cilPencil} />
          </CButton>
        </div>
      </CCardHeader>

      <CCardBody className="d-flex flex-column" style={{ padding: '0.75rem' }}>
        <Field label="Folder" mono>{route.host_path || '—'}</Field>

        {/* Both paths are shown because both are real: the one the user
            recognises, and the one this server actually opens. Storing and
            showing the translation is what stops either side guessing. */}
        <Field label="Seen by the server as" mono>{route.container_path}</Field>

        {route.allow_move && route.available && route.mount_writable === false && (
          <Field label="Moves">
            <span style={{ color: '#c62828' }}>
              Marked "imports may move files", but this server cannot write to the
              folder - the storage root is mounted read-only.
            </span>
          </Field>
        )}

        <Field label="Status">
          {route.available ? (
            <span style={{ color: '#2e7d32' }}>
              Available
              {route.file_count !== null && route.file_count !== undefined
                // A count the backend stopped early (2 s, or 100,000 files) is
                // a floor, so it says "+".
                && ` · ${route.file_count.toLocaleString()}${route.file_count_complete === false ? '+' : ''}`
                  + ` file${route.file_count === 1 && route.file_count_complete !== false ? '' : 's'}`}
            </span>
          ) : (
            <span style={{ color: '#c62828' }}>
              Unavailable — folder not found. If it is on an external drive, check
              that it is mounted. On macOS, folders under Desktop, Documents and
              Downloads also need Full Disk Access for the container runtime, or
              they appear empty.
            </span>
          )}
        </Field>
      </CCardBody>
    </CCard>
  </CCol>
);

// The tile that adds one. A card rather than a button in a toolbar, so the row
// reads as "this catalog, and the folders it reads from" with somewhere to put
// the next one — and so the empty case still shows something to click.
const AddRouteCard = ({ onClick, disabled, reason }) => (
  <CCol xs="auto">
    <CCard
      className="shadow-sm h-auto"
      style={{
        width: CARD_WIDTH, minHeight: 180, border: '2px dashed #ced4da',
        background: 'transparent', cursor: disabled ? 'default' : 'pointer',
      }}
      onClick={disabled ? undefined : onClick}
    >
      <CCardBody className="d-flex flex-column align-items-center justify-content-center text-center">
        <CIcon icon={disabled ? cilFolder : cilPlus}
               style={{ width: 26, height: 26, color: '#adb5bd', marginBottom: 8 }} />
        <div style={{ fontSize: '0.88rem', color: disabled ? '#adb5bd' : '#495057' }}>
          {disabled ? 'Storage is not available' : 'Add a storage route'}
        </div>
        <div style={{ fontSize: '0.76rem', color: '#adb5bd', marginTop: 4 }}>
          {disabled ? reason : 'A folder this catalog can import from'}
        </div>
      </CCardBody>
    </CCard>
  </CCol>
);

// `inColumn` (the default) wraps the cards in their own grid column, for a
// row beside the catalog card; false draws them bare, inside a container the
// caller already placed - the Catalogs page's Storage routes tab.
const StorageRouteCards = ({ catalogId, catalogName, onChanged, inColumn = true }) => {
  const [state, setState] = useState({ enabled: true, routes: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [editing, setEditing] = useState(null);   // route being edited, or {} for new
  const [form, setForm] = useState({ name: '', host_path: '', allow_move: false });
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState(null);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      // One request: the endpoint returns the mount status alongside this
      // catalog's own routes, so nothing here lists the installation.
      const r = await storageRouteAPI.forCatalog(catalogId);
      setState(r.data);
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load storage routes.');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  const openNew = () => {
    setForm({ name: '', host_path: '', allow_move: false });
    setFormError(null);
    setEditing({});
  };

  const openEdit = (route) => {
    setForm({ name: route.name, host_path: route.host_path || '',
              allow_move: Boolean(route.allow_move) });
    setFormError(null);
    setEditing(route);
  };

  const save = async () => {
    setSaving(true);
    setFormError(null);
    try {
      // A new route is owned by the catalog whose row this is. That is the
      // whole binding — there is no attach step.
      if (editing.id) {
        const res = await storageRouteAPI.update(editing.id, form);
        // Only reported when something actually changed: a workflow the user
        // renamed themselves is left alone, and saying nothing happened is
        // better than implying it did.
        const [pair] = res.data?.workflows_renamed || [];
        setNotice(pair ? `Route saved. Its workflow is now "${pair[1]}".` : null);
      } else {
        const res = await storageRouteAPI.create(catalogId, form);
        // The backend also creates the import workflow that reads from this
        // route. Saying so is the point: otherwise a workflow appears on
        // another page that the user did not ask for and cannot account for.
        const wf = res.data?.workflow;
        setNotice(
          wf ? `Route added, with a workflow "${wf.name}" that imports from it.`
             : res.data?.workflow_error
               ? `Route added, but its import workflow could not be created: `
                 + `${res.data.workflow_error}`
               : 'Route added.',
        );
      }
      setEditing(null);
      await load();
      if (onChanged) onChanged();
    } catch (e) {
      setFormError(e?.response?.data?.error || 'Could not save that route.');
    } finally {
      setSaving(false);
    }
  };

  const remove = async (route) => {
    // Says both halves, because both go: the route and the import workflow that
    // reads from it were created together and are removed together.
    if (!window.confirm(
      `Remove the storage route "${route.name}"?\n\n` +
      'The workflow that imports from it is removed too.\n\n' +
      'The folder and its files are not touched — only this catalog\'s pointer '
      + 'to them is removed.')) return;
    try {
      const res = await storageRouteAPI.remove(route.id);
      setNotice(res.data?.message || 'Route removed.');
      setError(null);
      await load();
      if (onChanged) onChanged();
    } catch (e) {
      // The backend refuses while a workflow is actually running, and names it.
      setError(e?.response?.data?.error || 'Could not remove that route.');
    }
  };

  // xs={12} lg: below the large breakpoint the catalog card (400px) and a route
  // card (320px) cannot sit side by side without one of them being squeezed, so
  // the routes take the full width underneath instead. minWidth 0 because a
  // flex child defaults to min-content and would otherwise refuse to shrink.
  const column = (children) => (inColumn
    ? <CCol xs={12} lg style={{ minWidth: 0 }}>{children}</CCol>
    : <>{children}</>);

  if (loading) {
    return column(
      <div className="d-flex align-items-center px-2 py-4">
        <CSpinner size="sm" color="primary" />
      </div>,
    );
  }

  return column(
    <>
      {error && <CAlert color="danger" style={{ fontSize: '0.85rem' }}>{error}</CAlert>}
      {notice && (
        <CAlert color="success" dismissible onClose={() => setNotice(null)}
                style={{ fontSize: '0.85rem' }}>
          {notice}
        </CAlert>
      )}

      <CRow className="g-3 align-items-start">
        {state.routes.map((r) => (
          <RouteCard key={r.id} route={r} onEdit={openEdit} onDelete={remove} />
        ))}

        <AddRouteCard onClick={openNew} disabled={!state.enabled} reason={state.message} />
      </CRow>

      <CModal visible={editing !== null} onClose={() => setEditing(null)}>
        <CModalHeader>
          <CModalTitle>
            {editing?.id ? 'Edit storage route' : `Add a storage route to ${catalogName}`}
          </CModalTitle>
        </CModalHeader>
        <CModalBody>
          {formError && <CAlert color="danger" style={{ fontSize: '0.85rem' }}>{formError}</CAlert>}
          <CForm>
            <div className="mb-3">
              <CFormLabel>Name *</CFormLabel>
              <CFormInput
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="Camera dumps"
              />
            </div>
            <div className="mb-2">
              <CFormLabel>Folder *</CFormLabel>
              <CFormInput
                value={form.host_path}
                onChange={(e) => setForm({ ...form, host_path: e.target.value })}
                placeholder={`${state.storage_root_host || ''}/Pictures`}
              />
              <small className="text-muted">
                Must be inside <code>{state.storage_root_host}</code>, and narrower than
                it — a route names a folder of files, not the whole drive.
              </small>
            </div>
            <div className="mt-3">
              <CFormSwitch
                id="route_allow_move"
                label="Imports may move files"
                checked={form.allow_move}
                onChange={(e) => {
                  const on = e.target.checked;
                  if (on && !window.confirm(
                    'Allow imports to move files out of this folder?\n\n'
                    + 'A catalog set to move files will delete the photos and videos it '
                    + 'imports from here, and gather duplicates in a DUPS folder.')) return;
                  setForm({ ...form, allow_move: on });
                }}
              />
              <small className="text-muted">
                Only a catalog that moves files uses this; copy and index-in-place
                catalogs never change the folder. Administrators only.
              </small>
            </div>
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setEditing(null)}>
            Cancel
          </CButton>
          <CButton color="primary" onClick={save}
                   disabled={saving || !form.name.trim() || !form.host_path.trim()}>
            {saving ? <CSpinner size="sm" /> : 'Save'}
          </CButton>
        </CModalFooter>
      </CModal>
    </>,
  );
};

export default StorageRouteCards;
