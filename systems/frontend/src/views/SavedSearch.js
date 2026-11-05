import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  CAlert, CBadge, CButton, CCard, CCardBody, CCol, CContainer, CDropdown, CDropdownItem, CDropdownMenu,
  CDropdownToggle, CFormCheck, CFormInput, CFormTextarea, CRow, CSpinner,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilArrowLeft, cilPencil, cilTrash } from '@coreui/icons';
import { photoAPI, savedSearchAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { API_HOST } from '../config';
import { describeQuery } from './SavedSearches';
import SavedSearchTools, { isVideoType } from '../components/SavedSearchTools';
import useMarquee, { previewChecked } from '../hooks/useMarquee';
import useDateBatch, { isTerminal } from '../hooks/useDateBatch';
import { describeEventsRebuild } from '../utils/eventsRebuild';

// ─── One saved search ─────────────────────────────────────────────────────────
// documentation/architecture/search-spec.md section 7.4, and the tool panel of
// documentation/specs/saved-search-tools-spec.md.
//
// Its photos and videos in a grid, as an Event's page shows its own; remove
// some, rename it, run its search again (new matches can be added from there),
// and the Operations menu - where "make it an album" will live. On the left,
// the tool panel works on the selected photos (Change date).

const PAGE = 60;
// A batch that finished this recently still shows its result when the page
// opens, so a reload during or just after the work keeps it (section 5.5).
const RECENT_MS = 10 * 60 * 1000;

const plural = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

/** SQLite's CURRENT_TIMESTAMP ("YYYY-MM-DD HH:MM:SS", UTC) as epoch ms. */
const sqliteUtc = (s) => (s ? Date.parse(`${s.replace(' ', 'T')}Z`) : NaN);

const DateBatchBanner = ({ batch, onDismiss }) => {
  const [details, setDetails] = useState(false);
  if (!batch) return null;
  const total = batch.total || 0;
  const failed = batch.result?.failed || [];
  const updated = batch.result?.updated?.length ?? batch.done ?? 0;
  if (batch.status === 'running' || !batch.status) {
    return (
      <CAlert color="info" className="d-flex align-items-center gap-2" data-testid="date-batch-banner">
        <CSpinner size="sm" />
        Changing the dates of {plural(total, 'photo or video', 'photos and videos')} ({batch.done || 0}/{total})…
      </CAlert>
    );
  }
  if (batch.status === 'rebuilding') {
    return (
      <CAlert color="info" className="d-flex align-items-center gap-2" data-testid="date-batch-banner">
        <CSpinner size="sm" />
        Dates saved. Rebuilding events…
      </CAlert>
    );
  }
  const ok = batch.status === 'completed';
  const rebuild = ok ? describeEventsRebuild(batch.result?.rebuild_status, batch.result?.rebuild_at) : '';
  return (
    <CAlert color={ok && !failed.length ? 'success' : 'warning'} dismissible onClose={onDismiss}
            data-testid="date-batch-banner">
      {ok
        ? <>Done. {plural(updated, 'date', 'dates')} changed.</>
        : <>The date change {batch.status === 'interrupted' ? 'was interrupted' : 'failed'} after {batch.done || 0} of {total}.</>}
      {rebuild && <> {rebuild}</>}
      {failed.length > 0 && (
        <>
          {' '}{plural(failed.length, 'file', 'files')} could not be changed and kept {failed.length === 1 ? 'its' : 'their'} date.{' '}
          <button type="button" className="btn btn-link btn-sm p-0 align-baseline" onClick={() => setDetails(!details)}>
            {details ? 'Hide details' : 'Details'}
          </button>
          {details && (
            <ul className="mb-0 mt-1" style={{ fontSize: '0.8rem' }}>
              {failed.map((f) => (
                <li key={f.file_id}>#{f.file_id}: {f.message || f.error}</li>
              ))}
            </ul>
          )}
        </>
      )}
    </CAlert>
  );
};

const SavedSearch = () => {
  const { id } = useParams();
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;
  const canEdit = activeCatalog?.role !== 'viewer';
  const indexInPlace = activeCatalog?.ingest_mode === 'reference';
  const navigate = useNavigate();
  const [s, setS] = useState(null);
  const [files, setFiles] = useState([]);
  const [total, setTotal] = useState(0);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(new Set());
  // What is known of every file seen so far, by id: its date and type. The
  // panel describes and re-dates selections that span pages (section 4.2).
  const [taken, setTaken] = useState(() => new Map());
  const [types, setTypes] = useState(() => new Map());
  const [removedIds, setRemovedIds] = useState(() => new Set());
  const [selectingAll, setSelectingAll] = useState(false);
  const [editing, setEditing] = useState(false);
  const [form, setForm] = useState({ name: '', note: '' });
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [batchId, setBatchId] = useState(null);
  const gridRef = useRef(null);

  const remember = useCallback((rows) => {
    setTaken((cur) => { const n = new Map(cur); rows.forEach((f) => n.set(f.file_id, f.taken)); return n; });
    setTypes((cur) => { const n = new Map(cur); rows.forEach((f) => n.set(f.file_id, f.file_type)); return n; });
  }, []);

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
      remember(b.data.files);
      setRemovedIds((cur) => {
        const n = new Set(cur);
        b.data.files.forEach((f) => { if (f.removed_at) n.add(f.file_id); else n.delete(f.file_id); });
        return n;
      });
      setError(null);
    } catch (e) {
      setError(e.response?.data?.error || 'Could not load the saved search.');
    }
  }, [catalogId, id, offset, remember]);
  useEffect(() => { load(); }, [load]);

  // A batch still running from this saved search, or one that has just
  // finished, is shown when the page opens (section 5.5).
  useEffect(() => {
    if (!catalogId) return;
    photoAPI.latestDateBatchForSavedSearch(catalogId, id)
      .then((r) => {
        const b = r.data;
        const recent = Date.now() - sqliteUtc(b.finished_at) < RECENT_MS;
        if (!isTerminal(b.status) || recent) setBatchId(b.id);
      })
      .catch(() => {});
  }, [catalogId, id]);

  const batch = useDateBatch(catalogId, batchId, () => load());
  const busy = Boolean(batchId) && (!batch || !isTerminal(batch.status));

  // Removed files are skipped by every operation, so they cannot be selected
  // (section 4.1).
  const selectable = (fid) => !removedIds.has(fid);
  const setChecked = (fid, on) => setSelected((cur) => {
    const n = new Set(cur);
    if (on && selectable(fid)) n.add(fid); else n.delete(fid);
    return n;
  });
  const toggle = (fid) => setChecked(fid, !selected.has(fid));

  const marquee = useMarquee({
    containerRef: gridRef,
    onCommit: (ids, mode) => setSelected((cur) => {
      const n = new Set(cur);
      ids.forEach((raw) => {
        const fid = Number(raw);
        if (!selectable(fid)) return;
        const want = mode === 'check' ? true : mode === 'uncheck' ? false : !n.has(fid);
        if (want) n.add(fid); else n.delete(fid);
      });
      return n;
    }),
  });

  const pageIds = files.filter((f) => !f.removed_at).map((f) => f.file_id);
  const pageAllSelected = pageIds.length > 0 && pageIds.every((fid) => selected.has(fid));
  const selectedIds = useMemo(() => [...selected], [selected]);
  const onOtherPages = selectedIds.filter((fid) => !files.some((f) => f.file_id === fid)).length;
  const selectableTotal = Math.max(0, (s?.file_count ?? total) - (s?.removed_count ?? 0));

  const selectAll = async () => {
    setSelectingAll(true);
    try {
      const r = await savedSearchAPI.fileIds(catalogId, id);
      remember(r.data.files);
      setSelected(new Set(r.data.files.map((f) => f.file_id)));
      if (r.data.truncated) {
        setNotice(`Selected the first ${r.data.files.length.toLocaleString()} of ${r.data.total.toLocaleString()}.`);
      }
    } catch (e) {
      setError(e.response?.data?.error || 'Could not select every file.');
    } finally {
      setSelectingAll(false);
    }
  };

  const remove = async () => {
    const r = await savedSearchAPI.removeFiles(catalogId, id, selectedIds);
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

  const dateChangeStarted = (newBatchId) => {
    setSelected(new Set());
    setNotice(null);
    setBatchId(newBatchId);
  };

  const open = (f) => navigate(`/${isVideoType(f.file_type) ? 'videos' : 'photos'}/${catalogId}/${f.file_id}`);

  if (!activeCatalog) return <CContainer className="py-4"><CAlert color="info">Choose a catalog.</CAlert></CContainer>;
  if (error && !s) return <CContainer className="py-4"><CAlert color="danger">{error}</CAlert></CContainer>;
  if (!s) return <CContainer className="py-5 text-center"><CSpinner color="primary" /></CContainer>;

  return (
    <CContainer fluid className="py-4 px-4">
      <CButton color="secondary" variant="ghost" size="sm" className="mb-2" onClick={() => navigate('/saved-searches')}>
        <CIcon icon={cilArrowLeft} className="me-1" />Saved searches
      </CButton>

      <CRow className="g-3">
        {/* ── The tool panel: as wide as the Photos page's left column ── */}
        <CCol md={2}>
          <SavedSearchTools
            catalogId={catalogId}
            savedSearchId={Number(id)}
            selectedIds={selectedIds}
            takenById={taken}
            typeById={types}
            canEdit={canEdit}
            indexInPlace={indexInPlace}
            busy={busy}
            onDateChangeStarted={dateChangeStarted}
          />
        </CCol>

        <CCol md={10}>
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

          <DateBatchBanner batch={batchId ? (batch || { status: 'running', total: 0 }) : null}
                           onDismiss={() => setBatchId(null)} />
          {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}
          {notice && <CAlert color="success" dismissible onClose={() => setNotice(null)}>{notice}</CAlert>}

          <div className="d-flex align-items-center gap-2 mb-2 flex-wrap" style={{ fontSize: '0.85rem' }}>
            <CButton size="sm" color="secondary" variant="outline" disabled={!pageIds.length}
                     onClick={() => setSelected((cur) => {
                       const n = new Set(cur);
                       pageIds.forEach((fid) => (pageAllSelected ? n.delete(fid) : n.add(fid)));
                       return n;
                     })}>
              {pageAllSelected ? 'Unselect page' : 'Select page'}
            </CButton>
            {selectableTotal > pageIds.length && (
              <CButton size="sm" color="secondary" variant="outline" disabled={selectingAll} onClick={selectAll}>
                {selectingAll && <CSpinner size="sm" className="me-1" />}
                Select all {selectableTotal.toLocaleString()}
              </CButton>
            )}
            {selected.size > 0 && (
              <>
                <CButton size="sm" color="secondary" variant="ghost" onClick={() => setSelected(new Set())}>
                  Clear
                </CButton>
                <span data-testid="selected-count">
                  {selected.size.toLocaleString()} selected
                  {onOtherPages > 0 ? ` (${onOtherPages.toLocaleString()} on other pages)` : ''}
                </span>
                <CButton size="sm" color="danger" variant="outline" onClick={remove}>
                  Remove {selected.size} from this saved search
                </CButton>
              </>
            )}
            <span className="ms-auto text-body-secondary">
              {total ? `${offset + 1}–${Math.min(total, offset + PAGE)} of ${total.toLocaleString()}` : 'No photos yet - run the search and add some'}
            </span>
            <CButton size="sm" color="secondary" variant="outline" disabled={offset === 0}
                     onClick={() => setOffset(Math.max(0, offset - PAGE))}>←</CButton>
            <CButton size="sm" color="secondary" variant="outline" disabled={offset + PAGE >= total}
                     onClick={() => setOffset(offset + PAGE)}>→</CButton>
          </div>

          {/* The marquee's container: relative, so the rectangle is drawn in its
              coordinates, and no text selection, since a drag here selects photos. */}
          <div ref={gridRef} data-testid="saved-search-grid" onPointerDown={marquee.onPointerDown}
               style={{ display: 'flex', flexWrap: 'wrap', gap: 6, position: 'relative',
                        userSelect: 'none', WebkitUserSelect: 'none' }}>
            {files.map((f) => {
              const checked = previewChecked(f.file_id, selected.has(f.file_id), f.removed_at ? null : marquee.preview);
              return (
                <div key={f.file_id} data-marquee-id={f.removed_at ? undefined : f.file_id}
                     style={{ position: 'relative', width: 150, height: 150, cursor: 'pointer',
                              opacity: f.removed_at ? 0.4 : 1,
                              outline: checked ? '3px solid #0d6efd' : 'none', borderRadius: 4 }}
                     title={`${f.file_name}${f.removed_at ? ' (removed from the catalog)' : ''}`} onClick={() => open(f)}>
                  <img src={`${API_HOST}${f.thumbnail_url}`} alt="" loading="lazy" draggable={false}
                       style={{ width: '100%', height: '100%', objectFit: 'cover', borderRadius: 4, background: '#f1f3f5' }} />
                  {!f.removed_at && (
                    <div style={{ position: 'absolute', top: 4, left: 6 }} onClick={(e) => e.stopPropagation()}
                         onPointerDown={(e) => e.stopPropagation()}>
                      <CFormCheck checked={checked} onChange={() => toggle(f.file_id)}
                                  aria-label={`Select ${f.file_name}`} />
                    </div>
                  )}
                </div>
              );
            })}
            {marquee.box && (
              <div data-testid="marquee-box"
                   style={{ position: 'absolute', left: marquee.box.left, top: marquee.box.top,
                            width: marquee.box.width, height: marquee.box.height,
                            border: '1px solid #0d6efd', background: 'rgba(13,110,253,0.12)',
                            pointerEvents: 'none' }} />
            )}
          </div>
        </CCol>
      </CRow>
    </CContainer>
  );
};

export default SavedSearch;
