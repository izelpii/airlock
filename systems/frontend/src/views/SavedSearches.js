import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  CAlert, CBadge, CButton, CCard, CCardBody, CContainer, CSpinner,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilBookmark } from '@coreui/icons';
import { savedSearchAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { API_HOST } from '../config';

// ─── Content · Saved searches ─────────────────────────────────────────────────
// documentation/architecture/search-spec.md section 7.
//
// A card per saved search, as Events has a card per event: its query (to run
// again) and the photos and videos chosen from it (a fixed set, for operations
// later). Idle for 30 days a card warns that it will be deleted at 60 - never
// its photos - and Keep restarts the clock.

export const describeQuery = (query) => {
  const p = new URLSearchParams(query || '');
  const parts = [];
  if (p.get('q')) parts.push(`“${p.get('q')}”`);
  if (p.get('date')) parts.push(p.get('date'));
  if (p.get('from') || p.get('to')) parts.push(`${p.get('from') || '…'} – ${p.get('to') || '…'}`);
  if (p.get('person')) parts.push(`${p.get('person').split(',').length} person filter`);
  if (p.get('pet')) parts.push('pet filter');
  if (p.get('location')) parts.push('a place');
  if (p.get('type')) parts.push(p.get('type'));
  if (p.get('camera')) parts.push(p.get('camera'));
  return parts.join(' · ') || 'everything';
};

const when = (iso) => (iso ? new Date(iso).toLocaleDateString(undefined,
  { day: 'numeric', month: 'short', year: 'numeric' }) : '');

const SavedSearches = () => {
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;
  const navigate = useNavigate();
  const [rows, setRows] = useState(null);
  const [status, setStatus] = useState('open');
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    if (!catalogId) return;
    try {
      const r = await savedSearchAPI.list(catalogId, status === 'done' ? 'done' : undefined);
      setRows(r.data.saved_searches || []);
      setError(null);
    } catch (e) {
      setError(e.response?.data?.error || 'Could not load the saved searches.');
    }
  }, [catalogId, status]);
  useEffect(() => { setRows(null); load(); }, [load]);

  const keep = async (e, s) => {
    e.stopPropagation();
    await savedSearchAPI.keep(catalogId, s.id).catch(() => {});
    load();
  };

  if (!activeCatalog) {
    return <CContainer className="py-4"><CAlert color="info">Choose a catalog.</CAlert></CContainer>;
  }

  return (
    <CContainer fluid className="py-4 px-4">
      <div className="d-flex justify-content-between align-items-center mb-3 flex-wrap gap-2">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>
            <CIcon icon={cilBookmark} className="me-2" style={{ width: 22, height: 22 }} />Saved searches
          </h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            A search and the photos and videos chosen from it - to run again, or act on later
          </div>
        </div>
        <div className="d-flex gap-2">
          <CButton size="sm" color={status === 'open' ? 'primary' : 'secondary'}
                   variant={status === 'open' ? undefined : 'outline'} onClick={() => setStatus('open')}>Open</CButton>
          <CButton size="sm" color={status === 'done' ? 'primary' : 'secondary'}
                   variant={status === 'done' ? undefined : 'outline'} onClick={() => setStatus('done')}>Done</CButton>
          <CButton size="sm" color="primary" variant="outline" onClick={() => navigate('/search')}>New search</CButton>
        </div>
      </div>

      {error && <CAlert color="danger">{error}</CAlert>}
      {rows === null && !error && <div className="text-center py-5"><CSpinner color="primary" /></div>}
      {rows && rows.length === 0 && (
        <CCard><CCardBody className="text-center text-body-secondary py-5">
          {status === 'done' ? 'Nothing done yet.' : 'No saved searches. Search, select results, and save them.'}
        </CCardBody></CCard>
      )}

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
        {(rows || []).map((s) => (
          <CCard key={s.id} style={{ width: 260, cursor: 'pointer' }} className="shadow-sm"
                 onClick={() => navigate(`/saved-searches/${s.id}`)}>
            <div style={{ height: 150, background: '#f1f3f5', overflow: 'hidden' }}>
              {s.cover_thumb_url && (
                <img src={`${API_HOST}${s.cover_thumb_url}`} alt="" loading="lazy"
                     style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
              )}
            </div>
            <CCardBody className="p-2">
              <div className="fw-semibold text-truncate">{s.name}</div>
              <div className="text-body-secondary text-truncate" style={{ fontSize: '0.78rem' }}>
                {describeQuery(s.query)}
              </div>
              <div style={{ fontSize: '0.8rem' }} className="mt-1">
                <strong>{s.file_count.toLocaleString()}</strong> photos and videos
                {s.first_taken && (
                  <span className="text-body-secondary"> · {s.first_taken.slice(0, 4)}
                    {s.last_taken && s.last_taken.slice(0, 4) !== s.first_taken.slice(0, 4)
                      ? `–${s.last_taken.slice(0, 4)}` : ''}</span>
                )}
              </div>
              <div className="text-body-secondary" style={{ fontSize: '0.72rem' }}>
                {s.created_by_name ? `${s.created_by_name} · ` : ''}changed {when(s.updated_at)}
              </div>
              {s.idle && (
                <div className="d-flex align-items-center justify-content-between mt-1">
                  <CBadge color="warning" className="text-dark" style={{ fontWeight: 500 }}>
                    will be deleted on {when(s.delete_on)}
                  </CBadge>
                  <CButton size="sm" color="link" className="p-0" onClick={(e) => keep(e, s)}>Keep</CButton>
                </div>
              )}
            </CCardBody>
          </CCard>
        ))}
      </div>
    </CContainer>
  );
};

export default SavedSearches;
