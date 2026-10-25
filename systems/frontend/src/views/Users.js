import React, { useCallback, useEffect, useState } from 'react';
import {
  CContainer, CRow, CCol, CCard, CCardBody, CCardHeader, CButton, CForm,
  CFormInput, CFormLabel, CFormCheck, CFormSwitch, CAlert, CSpinner, CBadge,
  CModal, CModalHeader, CModalTitle, CModalBody, CModalFooter,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPlus, cilPencil, cilTrash } from '@coreui/icons';
import { useNavigate } from 'react-router-dom';
import { useAuth } from '../context/AuthContext';
import { userAPI } from '../services/api';
import UserAvatar from '../components/UserAvatar';

/**
 * Admin > Users — account administration.
 *
 * A card per account: who it is and the two switches that actually get
 * toggled. Edit (the pencil) opens the account's own page, /users/:userId
 * (views/UserSettings.js) - profile, linked person, password, signed-in
 * devices. Creating an account is a modal rather than a permanent card, for
 * the same reason CatalogForm is one.
 *
 * The sidebar hides this page from non-administrators, but hiding a link is not
 * a control: someone can still type the URL. The guard below is the client-side
 * half; every mutating /api/users route is @admin_required regardless.
 * See documentation/specs/user-authentication-spec.md sections 6.1 and 8.3.
 */

// Narrower and shorter than a catalog card (400x600): a user has an avatar, a
// name and two switches, not a storage configuration.
const CARD_W = 320;
const CARD_H = 360;

const Users = () => {
  const { user: me, isAdmin } = useAuth();
  const navigate = useNavigate();

  const [rows, setRows] = useState([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState(null);

  const [showCreate, setShowCreate] = useState(false);
  const [createForm, setCreateForm] = useState({
    username: '', display_name: '', email: '', password: '', is_admin: false,
  });
  const [createError, setCreateError] = useState(null);
  const [creating, setCreating] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const { data } = await userAPI.getAll();
      setRows(data || []);
    } catch {
      setRows([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { if (isAdmin) load(); }, [isAdmin, load]);

  const setFlag = async (id, key, value) => {
    setMsg(null);
    try {
      await userAPI.update(id, { [key]: value });
      await load();
    } catch (err) {
      setMsg({ color: 'danger', text: err?.response?.data?.error || 'Could not update the account.' });
    }
  };

  const remove = async (u) => {
    if (!window.confirm(`Delete ${u.display_name}? Their catalog access is removed too.`)) return;
    setMsg(null);
    try {
      await userAPI.delete(u.id);
      setMsg({ color: 'success', text: `Deleted ${u.username}.` });
      await load();
    } catch (err) {
      // The API refuses when it would leave a catalog with no owner, and names
      // which ones — surface that rather than a generic failure.
      setMsg({ color: 'danger', text: err?.response?.data?.error || 'Could not delete the account.' });
    }
  };

  const submitCreate = async (e) => {
    e.preventDefault();
    setCreateError(null);
    setCreating(true);
    try {
      const created = await userAPI.create(createForm);
      setShowCreate(false);
      setCreateForm({ username: '', display_name: '', email: '', password: '', is_admin: false });
      setMsg({
        color: 'success',
        text: `Created ${created.data.username}. They must choose a new password at first sign in.`,
      });
      await load();
    } catch (err) {
      setCreateError(err?.response?.data?.error || 'Could not create the account.');
    } finally {
      setCreating(false);
    }
  };

  if (!isAdmin) {
    return (
      <CContainer fluid className="p-4">
        <CAlert color="warning">
          This page is for administrators. Ask an administrator if you need access.
        </CAlert>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="px-4 py-4">
      {/* Header — same shape as the Catalogs header */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Users</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Manage accounts and administrator rights
          </div>
        </div>
      </div>

      {/* Action bar — mirrors "Create Catalog" */}
      <CCard className="mb-4" style={{ backgroundColor: 'white' }}>
        <CCardBody className="d-flex justify-content-start align-items-center py-3">
          <CButton color="primary" size="sm"
                   onClick={() => { setCreateError(null); setShowCreate(true); }}>
            <CIcon icon={cilPlus} className="me-2" />
            New User
          </CButton>
        </CCardBody>
      </CCard>

      {msg && (
        <CAlert color={msg.color} dismissible onClose={() => setMsg(null)}>{msg.text}</CAlert>
      )}

      {loading ? (
        <div className="text-center"><CSpinner color="primary" /></div>
      ) : (
        <CRow className="g-3">
          {rows.map((u) => {
            // You cannot demote, deactivate or delete yourself: each is one
            // click from locking yourself out of the page that would undo it.
            const isSelf = u.id === me?.id;

            return (
              <CCol key={u.id} xs="auto">
                <div style={{ width: CARD_W, height: CARD_H }}>
                  <CCard className="shadow-sm" style={{
                    width: '100%', height: '100%',
                    display: 'flex', flexDirection: 'column', overflow: 'hidden',
                  }}>
                    <CCardHeader className="d-flex justify-content-between align-items-center py-2">
                      <div className="me-2 overflow-hidden">
                        <div className="fw-semibold text-truncate">
                          {u.display_name}
                          {isSelf && <CBadge color="info" className="ms-2">you</CBadge>}
                        </div>
                        <small className="text-muted d-block text-truncate">{u.username}</small>
                      </div>
                      <div className="d-flex flex-shrink-0 gap-1">
                        {!isSelf && (
                          <CButton color="danger" variant="ghost" size="sm"
                                   title="Delete user" onClick={() => remove(u)}>
                            <CIcon icon={cilTrash} />
                          </CButton>
                        )}
                        <CButton color="info" variant="ghost" size="sm"
                                 title="Edit user: profile, linked person, password, devices"
                                 onClick={() => navigate(`/users/${u.id}`)}>
                          <CIcon icon={cilPencil} />
                        </CButton>
                      </div>
                    </CCardHeader>

                    <CCardBody className="d-flex flex-column align-items-center"
                               style={{ flex: 1, overflow: 'auto' }}>
                      <UserAvatar
                        userId={u.id}
                        name={u.display_name}
                        size={96}
                        version={u.person_id || 0}
                        style={{ marginTop: 4, marginBottom: 12 }}
                      />
                      <div className="text-muted text-truncate mb-3"
                           style={{ fontSize: '0.82rem', maxWidth: '100%' }}
                           title={u.email || ''}>
                        {u.email || 'No email'}
                      </div>

                      <div className="w-100 mt-auto">
                        <div className="d-flex align-items-center justify-content-between py-2"
                             style={{ borderTop: '1px solid #f0f0f0' }}>
                          <span style={{ fontSize: '0.85rem' }}>Administrator</span>
                          <div className="d-flex align-items-center gap-2">
                            <span className={u.is_admin ? 'text-body' : 'text-muted'}
                                  style={{ fontSize: '0.8rem' }}>
                              {u.is_admin ? 'Yes' : 'No'}
                            </span>
                            <CFormSwitch
                              checked={Boolean(u.is_admin)}
                              disabled={isSelf}
                              onChange={(e) => setFlag(u.id, 'is_admin', e.target.checked)}
                              aria-label={`Administrator rights for ${u.username}`}
                              title={isSelf
                                ? 'You cannot remove your own administrator rights'
                                : 'Administrator rights'}
                            />
                          </div>
                        </div>
                        <div className="d-flex align-items-center justify-content-between py-2"
                             style={{ borderTop: '1px solid #f0f0f0' }}>
                          <span style={{ fontSize: '0.85rem' }}>Active</span>
                          <div className="d-flex align-items-center gap-2">
                            <span className={u.is_active ? 'text-body' : 'text-danger'}
                                  style={{ fontSize: '0.8rem' }}>
                              {u.is_active ? 'Active' : 'Disabled'}
                            </span>
                            <CFormSwitch
                              checked={Boolean(u.is_active)}
                              disabled={isSelf}
                              onChange={(e) => setFlag(u.id, 'is_active', e.target.checked)}
                              aria-label={`Active status for ${u.username}`}
                              title={isSelf
                                ? 'You cannot deactivate your own account'
                                : 'Deactivating signs the account out immediately'}
                            />
                          </div>
                        </div>
                      </div>
                    </CCardBody>
                  </CCard>
                </div>
              </CCol>
            );
          })}
        </CRow>
      )}

      {/* ── New user modal — the CatalogForm pattern ─────────────────────── */}
      <CModal visible={showCreate} onClose={() => setShowCreate(false)} size="lg">
        <CModalHeader>
          <CModalTitle>New User</CModalTitle>
        </CModalHeader>
        <CForm onSubmit={submitCreate}>
          <CModalBody>
            {createError && <CAlert color="danger" className="py-2">{createError}</CAlert>}
            <CRow className="g-3">
              <CCol md={6}>
                <CFormLabel>Username *</CFormLabel>
                <CFormInput value={createForm.username} required
                            onChange={(e) => setCreateForm({ ...createForm, username: e.target.value })} />
              </CCol>
              <CCol md={6}>
                <CFormLabel>Display name</CFormLabel>
                <CFormInput value={createForm.display_name}
                            onChange={(e) => setCreateForm({ ...createForm, display_name: e.target.value })} />
              </CCol>
              <CCol md={6}>
                <CFormLabel>Email</CFormLabel>
                <CFormInput type="email" value={createForm.email}
                            onChange={(e) => setCreateForm({ ...createForm, email: e.target.value })} />
              </CCol>
              <CCol md={6}>
                <CFormLabel>Temporary password *</CFormLabel>
                <CFormInput type="password" value={createForm.password} required
                            autoComplete="new-password"
                            onChange={(e) => setCreateForm({ ...createForm, password: e.target.value })} />
                <small className="text-muted">
                  At least 12 characters. They must change it at first sign in.
                </small>
              </CCol>
              <CCol xs={12}>
                <CFormCheck
                  id="new-user-admin"
                  label="Administrator"
                  checked={createForm.is_admin}
                  onChange={(e) => setCreateForm({ ...createForm, is_admin: e.target.checked })}
                />
                <small className="text-muted">
                  Administrators manage accounts. It does not by itself grant access to any
                  catalog — share those from the Catalogs page.
                </small>
              </CCol>
            </CRow>
          </CModalBody>
          <CModalFooter>
            <CButton color="secondary" variant="outline" onClick={() => setShowCreate(false)}>
              Cancel
            </CButton>
            <CButton color="primary" type="submit" disabled={creating}>
              {creating ? <CSpinner size="sm" /> : 'Create user'}
            </CButton>
          </CModalFooter>
        </CForm>
      </CModal>
    </CContainer>
  );
};

export default Users;
