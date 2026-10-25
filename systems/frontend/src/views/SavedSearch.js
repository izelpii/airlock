import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  CAlert, CBadge, CButton, CCard, CCardBody, CContainer, CDropdown, CDropdownItem, CDropdownMenu,
  CDropdownToggle, CFormCheck, CFormInput, CFormTextarea, CSpinner,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilArrowLeft, cilPencil, cilTrash } from '@coreui/icons';
import { savedSearchAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { API_HOST } from '../config';
import { describeQuery } from './SavedSearches';

// ─── One saved search ─────────────────────────────────────────────────────────
// documentation/architecture/search-spec.md section 7.4.
//
// Its photos and videos in a grid, as an Event's page shows its own; remove
// some, rename it, run its search again (new matches can be added from there),
// and the Operations menu - where "make it an album" will live.

const PAGE = 60;
const VIDEO = new Set(['.mov', '.mp4', '.m4v', '.avi', '.mkv', '.mts', '.m2ts', '.3gp', '.wmv', '.mpg', '.mpeg', '.webm']);

const SavedSearch = () => {
  const { id } = useParams();
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;
  const navigate = useNavigate();
  const [s, setS] = useState(null);
  const [files, setFiles] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(new Set());
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ name: '', note: '' });
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async () => {
    if (!catalogId) return;
    try {
      const [a, b] = await Promise.all([
        savedSearchAPI.get(catalogId, id),
        savedSearchAPI.files(catalogId, id, { limit: PAGE, offset }),
      ]);
      setS(a.data);
      setFiles(b.data.files);
      setTotal(b.data.total);
      setError(null);
    } catch (e) {
      setError(e.response?.data?.error || 'Could not load the saved search.');
    }
  }, [catalogId, id, offset]);
  useEffect(() => { load(); }, [load]);

  const toggle = (fid) => setSelected((cur) => {
    const n = new Set(cur);
    if (n.has(fid)) n.delete(fid); else n.add(fid);
    return n;
  });

  const remove = async () => {
    const r = await savedSearchAPI.removeFiles(catalogId, id, [...selected]);
    setNotice(`Removed ${r.data.removed} from this saved search (the photos themselves are untouched).`);
    setSelected(new Set());
    load();
  };

  const save = async () => {
    await savedSearchAPI.update(catalogId, id, { name: form.name, note: form.note });
    setEditing(false);
    load();
  };

  const del = async () => {
    if (!window.confirm(`Delete the saved search "${s.name}"?\n\nIts photos and videos stay in the catalog.`)) return;
    await savedSearchAPI.remove(catalogId, id);
    navigate('/saved-searches');
  };

  const runAgain = () => {
    savedSearchAPI.get(catalogId, id, { run: true }).catch(() => {});
    const next = new URLSearchParams(s.query || '');
    next.set('ss', id);
    navigate(`/search?${next.toString()}`);
  };

  const open = (f) => navigate(`/${VIDEO.has((f.file_type || '').toLowerCase()) ? 'videos' : 'photos'}/${catalogId}/${f.file_id}`);

  if (!activeCatalog) return <CContainer className="py-4"><CAlert color="info">Choose a catalog.</CAlert></CContainer>;
  if (error) return <CContainer className="py-4"><CAlert color="danger">{error}</CAlert></CContainer>;
  if (!s) return <CContainer className="py-5 text-center"><CSpinner color="primary" /></CContainer>;

  return (
    <CContainer fluid className="py-4 px-4">
      <CButton color="secondary" variant="ghost" size="sm" className="mb-2" onClick={() => navigate('/saved-searches')}>
        <CIcon icon={cilArrowLeft} className="me-1" />Saved searches
      </CButton>

      <CCard className="mb-3">
        <CCardBody>
          {editing ? (
            <div className="d-flex flex-column gap-2" style={{ maxWidth: 520 }}>
              <CFormInput value={form.name} onChange={(e) => setForm({ ...form, name: e.target.value })} />
              <CFormTextarea rows={2} placeholder="A note" value={form.note}
                             onChange={(e) => setForm({ ...form, note: e.target.value })} />
              <div className="d-flex gap-2">
                <CButton size="sm" color="primary" onClick={save} disabled={!form.name.trim()}>Save</CButton>
                <CButton size="sm" color="secondary" variant="outline" onClick={() => setEditing(false)}>Cancel</CButton>
              </div>
            </div>
          ) : (
            <div className="d-flex justify-content-between align-items-start flex-wrap gap-2">
              <div>
                <h4 className="mb-1 fw-bold">
                  {s.name}
                  {s.status === 'done' && <CBadge color="success" className="ms-2" style={{ fontSize: '0.7rem' }}>done</CBadge>}
                </h4>
                <div className="text-body-secondary" style={{ fontSize: '0.85rem' }}>
                  Search: {describeQuery(s.query)} · {s.file_count.toLocaleString()} photos and videos
                  {s.removed_count ? ` (${s.removed_count} removed from the catalog)` : ''}
                </div>
                {s.note && <div style={{ fontSize: '0.85rem' }} className="mt-1">{s.note}</div>}
                {s.idle && (
                  <CBadge color="warning" className="text-dark mt-1" style={{ fontWeight: 500 }}>
                    idle - will be deleted on {new Date(s.delete_on).toLocaleDateString()}
                  </CBadge>
                )}
              </div>
              <div className="d-flex gap-2 flex-wrap">
                <CButton size="sm" color="primary" onClick={runAgain}>Run again</CButton>
                <CDropdown>
                  <CDropdownToggle size="sm" color="secondary" variant="outline">Operations</CDropdownToggle>
                  <CDropdownMenu>
                    <CDropdownItem disabled>Make an album - coming later</CDropdownItem>
                    <CDropdownItem onClick={async () => {
                      await savedSearchAPI.update(catalogId, id, { status: s.status === 'done' ? 'open' : 'done' });
                      load();
                    }}>
                      {s.status === 'done' ? 'Reopen' : 'Mark as done'}
                    </CDropdownItem>
                  </CDropdownMenu>
                </CDropdown>
                <CButton size="sm" color="info" variant="ghost" title="Rename"
                         onClick={() => { setForm({ name: s.name, note: s.note || '' }); setEditing(true); }}>
                  <CIcon icon={cilPencil} />
                </CButton>
                <CButton size="sm" color="danger" variant="ghost" title="Delete (its photos stay)" onClick={del}>
                  <CIcon icon={cilTrash} />
                </CButton>
              </div>
            </div>
          )}
        </CCardBody>
      </CCard>

      {notice && <CAlert color="success" dismissible onClose={() => setNotice(null)}>{notice}</CAlert>}

      <div className="d-flex align-items-center gap-2 mb-2" style={{ fontSize: '0.85rem' }}>
        <CButton size="sm" color="secondary" variant="outline"
                 onClick={() => setSelected(new Set(files.every((f) => selected.has(f.file_id)) ? [] : files.map((f) => f.file_id)))}>
          {files.length && files.every((f) => selected.has(f.file_id)) ? 'Unselect page' : 'Select page'}
        </CButton>
        {selected.size > 0 && (
          <CButton size="sm" color="danger" variant="outline" onClick={remove}>
            Remove {selected.size} from this saved search
          </CButton>
        )}
        <span className="ms-auto text-body-secondary">
          {total ? `${offset + 1}–${Math.min(total, offset + PAGE)} of ${total.toLocaleString()}` : 'No photos yet - run the search and add some'}
        </span>
        <CButton size="sm" color="secondary" variant="outline" disabled={offset === 0}
                 onClick={() => setOffset(Math.max(0, offset - PAGE))}>←</CButton>
        <CButton size="sm" color="secondary" variant="outline" disabled={offset + PAGE >= total}
                 onClick={() => setOffset(offset + PAGE)}>→</CButton>
      </div>

      <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
        {files.map((f) => (
          <div key={f.file_id} style={{ position: 'relative', width: 150, height: 150, cursor: 'pointer',
                                        opacity: f.removed_at ? 0.4 : 1 }}
               title={`${f.file_name}${f.removed_at ? ' (removed from the catalog)' : ''}`} onClick={() => open(f)}>
            <img src={`${API_HOST}${f.thumbnail_url}`} alt="" loading="lazy"
                 style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: 4, background: '#f1f3f5' }} />
            <div style={{ position: 'absolute', top: 4, left: 6 }} onClick={(e) => e.stopPropagation()}>
              <CFormCheck checked={selected.has(f.file_id)} onChange={() => toggle(f.file_id)} />
            </div>
          </div>
        ))}
      </div>
    </CContainer>
  );
};

export default SavedSearch;
