import React, { useCallback, useEffect, useState } from 'react';
import {
  CButton, CFormSelect, CAlert, CSpinner, CBadge,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilTrash } from '@coreui/icons';
import { catalogAccessAPI, userAPI } from '../services/api';
import { useAuth } from '../context/AuthContext';

/**
 * Who can reach one catalog — the back face of a catalog card.
 *
 * Sized for a 400px card rather than a full-width table: one row per person,
 * with the role as a compact select.
 *
 * Only owners get here (the Share button is hidden otherwise), and the API
 * enforces that independently: /catalogs/<id>/access requires the owner role.
 */
const ROLE_HELP = {
  viewer: 'Can look, not change',
  editor: 'Can edit and run workflows',
  owner: 'Can also share and delete',
};

const CatalogSharePanel = ({ catalogId, catalogName }) => {
  const { user } = useAuth();
  const [rows, setRows] = useState([]);
  const [people, setPeople] = useState([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState(null);

  const load = useCallback(async () => {
    if (!catalogId) return;
    setLoading(true);
    try {
      const [{ data: access }, { data: users }] = await Promise.all([
        catalogAccessAPI.list(catalogId),
        userAPI.getAll(),
      ]);
      setRows(access || []);
      setPeople(users || []);
    } catch (err) {
      setMsg({ color: 'danger', text: err?.response?.data?.error || 'Could not load sharing.' });
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  const grant = async (userId, role) => {
    setMsg(null);
    try {
      await catalogAccessAPI.set(catalogId, Number(userId), role);
      await load();
    } catch (err) {
      // The API refuses to demote or remove the last owner, and says so.
      setMsg({ color: 'danger', text: err?.response?.data?.error || 'Could not update sharing.' });
    }
  };

  const revoke = async (userId) => {
    setMsg(null);
    try {
      await catalogAccessAPI.revoke(catalogId, userId);
      await load();
    } catch (err) {
      setMsg({ color: 'danger', text: err?.response?.data?.error || 'Could not revoke access.' });
    }
  };

  const taken = new Set(rows.map((r) => r.user_id));
  const candidates = people.filter((p) => !taken.has(p.id));

  if (loading) {
    return <div className="text-center py-4"><CSpinner size="sm" /></div>;
  }

  return (
    <>
      {msg && (
        <CAlert color={msg.color} className="py-2 mb-2" style={{ fontSize: '0.8rem' }}
                dismissible onClose={() => setMsg(null)}>
          {msg.text}
        </CAlert>
      )}

      <p className="text-muted mb-2" style={{ fontSize: '0.78rem' }}>
        People who can reach <strong>{catalogName}</strong>.
      </p>

      <div className="flex-grow-1" style={{ overflowY: 'auto' }}>
        {rows.map((r) => (
          <div key={r.user_id}
               className="d-flex align-items-center gap-2 py-2"
               style={{ borderTop: '1px solid #f0f0f0' }}>
            <div style={{ minWidth: 0, flex: 1 }}>
              <div className="text-truncate" style={{ fontSize: '0.85rem' }}>
                {r.display_name}
                {r.user_id === user?.id && <CBadge color="info" className="ms-1">you</CBadge>}
              </div>
              <div className="text-muted text-truncate" style={{ fontSize: '0.72rem' }}>
                {r.username}
              </div>
            </div>
            <CFormSelect
              size="sm"
              style={{ width: 100 }}
              value={r.role}
              title={ROLE_HELP[r.role]}
              onChange={(e) => grant(r.user_id, e.target.value)}
            >
              <option value="viewer">viewer</option>
              <option value="editor">editor</option>
              <option value="owner">owner</option>
            </CFormSelect>
            <CButton size="sm" color="danger" variant="ghost" title="Remove access"
                     onClick={() => revoke(r.user_id)}>
              <CIcon icon={cilTrash} />
            </CButton>
          </div>
        ))}
        {rows.length === 0 && (
          <p className="text-muted" style={{ fontSize: '0.8rem' }}>Nobody has access yet.</p>
        )}
      </div>

      <div className="pt-2" style={{ borderTop: '1px solid #e0e0e0' }}>
        {candidates.length > 0 ? (
          <CFormSelect
            size="sm"
            value=""
            onChange={(e) => { if (e.target.value) grant(e.target.value, 'viewer'); }}
          >
            <option value="">Share with…</option>
            {candidates.map((p) => (
              <option key={p.id} value={p.id}>{p.display_name} ({p.username})</option>
            ))}
          </CFormSelect>
        ) : (
          <p className="text-muted mb-0" style={{ fontSize: '0.78rem' }}>
            Everyone already has access. Add accounts in Admin &rsaquo; Users.
          </p>
        )}
        <small className="text-muted" style={{ fontSize: '0.72rem' }}>
          Added as a viewer — change the role above.
        </small>
      </div>
    </>
  );
};

export default CatalogSharePanel;
