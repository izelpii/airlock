import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  CAlert, CBadge, CButton, CCard, CCardBody, CCol, CContainer, CDropdown, CDropdownDivider,
  CDropdownItem, CDropdownMenu, CDropdownToggle, CFormCheck, CFormInput, CFormLabel, CFormSelect,
  CModal, CModalBody, CModalFooter, CModalHeader, CModalTitle, CRow, CSpinner, CTable, CTableBody,
  CTableDataCell, CTableHead, CTableHeaderCell, CTableRow,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilSearch, cilX, cilBookmark } from '@coreui/icons';
import { searchAPI, savedSearchAPI, peopleAPI, petAPI, locationAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { API_HOST } from '../config';

// ─── Content · Search ─────────────────────────────────────────────────────────
// documentation/architecture/search-spec.md.
//
// One box and a few filters; one row per result, thumbnail on the left. A
// DUPLICATE row is a search hit, not a file: a copy an import turned away, which
// points to the file now in the catalog (owner, decision 2) - opening or
// selecting it means that file. Everything that defines the search is in the
// URL, so a search can be bookmarked, shared and saved; a saved search keeps
// that query and the results chosen from it.

// The URL parameters that make up a search (and are saved with one).
const KEYS = ['q', 'person', 'person_mode', 'pet', 'date', 'from', 'to', 'on', 'location',
  'type', 'camera', 'unnamed_faces', 'has_gps', 'duplicates', 'removed', 'sort', 'dir'];

const LABEL = {
  person: 'Person', pet: 'Pet', date: 'Date', from: 'From', to: 'To', on: 'On this day',
  location: 'Place', type: 'Type', camera: 'Camera', unnamed_faces: 'Unnamed faces',
  has_gps: 'GPS', removed: 'Including removed', duplicates: 'Duplicates hidden',
};

const fmtBytes = (b) => {
  if (!b) return '';
  const u = ['B', 'KB', 'MB', 'GB'];
  let v = b;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 10 || i === 0 ? 0 : 1)} ${u[i]}`;
};

const fmtTaken = (t) => {
  if (!t) return '';
  const d = new Date(t.slice(0, 19));
  return Number.isNaN(d.getTime()) ? t.slice(0, 10)
    : d.toLocaleDateString(undefined, { weekday: 'short', day: 'numeric', month: 'short', year: 'numeric' });
};

const basename = (p) => (p || '').split('/').pop();

export const queryOf = (params) => {
  const out = new URLSearchParams();
  KEYS.forEach((k) => { if (params.get(k)) out.set(k, params.get(k)); });
  return out.toString();
};

const Search = () => {
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;
  const navigate = useNavigate();
  const [params, setParams] = useSearchParams();
  const [box, setBox] = useState(params.get('q') || '');
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [loading, setLoading] = useState(false);
  const [cursors, setCursors] = useState([null]);        // the cursor of each page seen
  const [showFilters, setShowFilters] = useState(false);
  const [people, setPeople] = useState([]);
  const [pets, setPets] = useState([]);
  const [places, setPlaces] = useState([]);
  const [saved, setSaved] = useState([]);
  const [selected, setSelected] = useState(new Set());
  // "Select all" selects the search itself, not a list of ids (it can be far
  // more than a page); `excluded` are the ones unticked afterwards.
  const [allSelected, setAllSelected] = useState(false);
  const [excluded, setExcluded] = useState(new Set());
  // Select range: the first click is the anchor and decides select or
  // unselect; the second applies that to everything between (shift-click too).
  const [rangeMode, setRangeMode] = useState(false);
  const [anchor, setAnchor] = useState(null);             // {id, on}
  const [notice, setNotice] = useState(null);
  const [nameModal, setNameModal] = useState(null);      // {mode: 'save'|'new-target'}
  const [newName, setNewName] = useState('');
  const query = queryOf(params);
  const target = params.get('ss');                        // the saved search being added to

  // The pickers' choices: once per catalog.
  useEffect(() => {
    if (!catalogId) return;
    peopleAPI.getAll(catalogId).then((r) => setPeople((r.data || []).filter((p) => (p.kind || 'person') === 'person')))
      .catch(() => setPeople([]));
    petAPI.getAll(catalogId).then((r) => setPets(r.data || [])).catch(() => setPets([]));
    locationAPI.getAll(catalogId, { confirmedOnly: true }).then((r) => setPlaces(r.data || []))
      .catch(() => setPlaces([]));
  }, [catalogId]);

  const loadSaved = useCallback(() => {
    if (!catalogId) return;
    savedSearchAPI.list(catalogId).then((r) => setSaved(r.data.saved_searches || [])).catch(() => setSaved([]));
  }, [catalogId]);
  useEffect(() => { loadSaved(); }, [loadSaved]);

  // A new search starts on page one, with nothing selected.
  useEffect(() => {
    setCursors([null]);
    setSelected(new Set());
    setAllSelected(false);
    setExcluded(new Set());
    setRangeMode(false);
    setAnchor(null);
    setBox(params.get('q') || '');
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [query, catalogId]);

  const page = cursors.length - 1;
  const load = useCallback(async () => {
    if (!catalogId) return;
    setLoading(true);
    setError(null);
    try {
      const p = Object.fromEntries(new URLSearchParams(query));
      if (cursors[page]) p.after = cursors[page];
      const r = await searchAPI.search(catalogId, p);
      setData(r.data);
    } catch (e) {
      setError(e.response?.data?.error || 'The search failed.');
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [catalogId, query, cursors, page]);
  useEffect(() => { load(); }, [load]);

  const setParam = (key, value) => {
    const next = new URLSearchParams(params);
    if (value === null || value === undefined || value === '') next.delete(key);
    else next.set(key, value);
    setParams(next);
  };

  const submit = (e) => {
    e.preventDefault();
    setParam('q', box.trim());
  };

  const chips = KEYS.filter((k) => !['q', 'sort', 'dir', 'person_mode'].includes(k) && params.get(k))
    .map((k) => {
      let v = params.get(k);
      if (k === 'person') v = v.split(',').map((id) => people.find((p) => String(p.id) === id)?.name || id).join(' + ');
      if (k === 'pet') v = v.split(',').map((id) => pets.find((p) => String(p.id) === id)?.name || id).join(' + ');
      if (k === 'location') v = places.find((p) => String(p.id) === v)?.name || v;
      if (['removed', 'duplicates', 'unnamed_faces'].includes(k)) v = '';
      if (k === 'has_gps') v = v === '1' ? 'has GPS' : 'no GPS';
      return { k, text: `${LABEL[k]}${v ? `: ${v}` : ''}` };
    });

  const results = data?.results || [];
  const pageIds = useMemo(() => [...new Set(results.map((r) => r.file_id))], [results]);
  const isChecked = (fid) => (allSelected ? !excluded.has(fid) : selected.has(fid));
  // Tick or untick some ids, in whichever mode the selection is in.
  const setChecked = (ids, on) => {
    const apply = (cur) => {
      const n = new Set(cur);
      // Under "all", a ticked photo is one NOT in excluded.
      ids.forEach((i) => ((allSelected ? !on : on) ? n.add(i) : n.delete(i)));
      return n;
    };
    if (allSelected) setExcluded(apply); else setSelected(apply);
  };
  const clearSelection = () => {
    setSelected(new Set());
    setAllSelected(false);
    setExcluded(new Set());
    setAnchor(null);
  };
  const toggle = (fid, shift = false) => {
    const on = !isChecked(fid);
    if ((rangeMode || shift) && anchor && anchor.id !== fid) {
      const a = pageIds.indexOf(anchor.id);
      const b = pageIds.indexOf(fid);
      if (a >= 0 && b >= 0) {
        setChecked(pageIds.slice(Math.min(a, b), Math.max(a, b) + 1), anchor.on);
        setAnchor(null);
        setRangeMode(false);
        return;
      }
    }
    setChecked([fid], on);
    setAnchor({ id: fid, on });
  };
  const pageChecked = pageIds.length > 0 && pageIds.every(isChecked);
  const selectPage = () => setChecked(pageIds, !pageChecked);
  const total = data?.total || 0;
  const selectedCount = allSelected ? Math.max(0, total - excluded.size) : selected.size;
  const howMany = allSelected && data?.total_capped && excluded.size === 0
    ? `${total.toLocaleString()}+` : selectedCount.toLocaleString();
  const anySelected = selectedCount > 0;

  // Add the selection - or, with "all results", the search itself - to a saved search.
  const addTo = async (sid, name) => {
    setNotice(null);
    try {
      const body = allSelected ? { query, exclude_ids: [...excluded] }
        : { file_ids: [...selected], added_from: query };
      const r = await savedSearchAPI.addFiles(catalogId, sid, body);
      setNotice({ color: 'success', text: `${name}: added ${r.data.added.toLocaleString()}`
        + (r.data.already ? `, ${r.data.already.toLocaleString()} already there` : '')
        + (r.data.capped ? ' - the first 10,000; add again for more' : '') });
      clearSelection();
      loadSaved();
    } catch (e) {
      setNotice({ color: 'danger', text: e.response?.data?.error || 'Could not add them.' });
    }
  };

  const createSaved = async () => {
    const name = newName.trim();
    if (!name) return;
    try {
      const body = { name, query };
      if (allSelected) { body.add_all = true; body.exclude_ids = [...excluded]; }
      else body.file_ids = [...selected];
      const r = await savedSearchAPI.create(catalogId, body);
      setNotice({ color: 'success', text: `Saved "${name}" with ${r.data.file_count.toLocaleString()} photos and videos.` });
      setNameModal(null);
      setNewName('');
      clearSelection();
      loadSaved();
    } catch (e) {
      setNotice({ color: 'danger', text: e.response?.data?.error || 'Could not save it.' });
    }
  };

  const runSaved = (s) => {
    savedSearchAPI.get(catalogId, s.id, { run: true }).catch(() => {});
    const next = new URLSearchParams(s.query || '');
    next.set('ss', String(s.id));
    setParams(next);
  };

  const open = (r) => navigate(`/${r.type === 'Video' ? 'videos' : 'photos'}/${catalogId}/${r.file_id}`);
  const targetSearch = saved.find((s) => String(s.id) === target);
  // Run again from a saved search: what is selected goes into THAT one - no
  // name to ask for, no other saved search to pick.
  const running = target
    ? (targetSearch || { id: Number(target), name: 'this saved search' }) : null;

  if (!activeCatalog) {
    return (
      <CContainer className="py-4">
        <CCard><CCardBody className="text-body-secondary">Choose a catalog to search it.</CCardBody></CCard>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="py-4 px-4">
      <div className="d-flex justify-content-between align-items-center mb-3">
        <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>
          <CIcon icon={cilSearch} className="me-2" style={{ width: 22, height: 22 }} />Search
        </h4>
        {data && (
          <span className="text-body-secondary" style={{ fontSize: '0.9rem' }}>
            {data.total_capped ? `${data.total.toLocaleString()}+` : data.total.toLocaleString()} results
          </span>
        )}
      </div>

      <CCard className="mb-3">
        <CCardBody className="py-3">
          <form onSubmit={submit} className="d-flex gap-2">
            <CFormInput value={box} onChange={(e) => setBox(e.target.value)} autoFocus
                        placeholder='A name, part of a path, a person, a place - or person:Ana date:2019 near:40.71,-74.01,1km sha:09c4…' />
            <CButton type="submit" color="primary"><CIcon icon={cilSearch} /></CButton>
            <CButton color="secondary" variant="outline" onClick={() => setShowFilters((v) => !v)}>
              Filters
            </CButton>
          </form>

          {showFilters && (
            <CRow className="g-2 mt-2" style={{ fontSize: '0.85rem' }}>
              <CCol md={3}>
                <CFormLabel className="small mb-1">Person</CFormLabel>
                <CFormSelect size="sm" value="" onChange={(e) => {
                  const cur = (params.get('person') || '').split(',').filter(Boolean);
                  if (e.target.value && !cur.includes(e.target.value)) setParam('person', [...cur, e.target.value].join(','));
                }}>
                  <option value="">Add a person…</option>
                  {people.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </CFormSelect>
                <CFormCheck className="mt-1" label="any of them (not all)" checked={params.get('person_mode') === 'any'}
                            onChange={(e) => setParam('person_mode', e.target.checked ? 'any' : '')} />
              </CCol>
              <CCol md={2}>
                <CFormLabel className="small mb-1">Pet</CFormLabel>
                <CFormSelect size="sm" value="" onChange={(e) => {
                  const cur = (params.get('pet') || '').split(',').filter(Boolean);
                  if (e.target.value && !cur.includes(e.target.value)) setParam('pet', [...cur, e.target.value].join(','));
                }}>
                  <option value="">Add a pet…</option>
                  {pets.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </CFormSelect>
              </CCol>
              <CCol md={2}>
                <CFormLabel className="small mb-1">Place</CFormLabel>
                <CFormSelect size="sm" value={params.get('location') || ''}
                             onChange={(e) => setParam('location', e.target.value)}>
                  <option value="">Any place</option>
                  {places.map((p) => <option key={p.id} value={p.id}>{p.name}</option>)}
                </CFormSelect>
              </CCol>
              <CCol md={2}>
                <CFormLabel className="small mb-1">Date (2019, 2019-07…)</CFormLabel>
                <CFormInput size="sm" defaultValue={params.get('date') || ''} placeholder="YYYY[-MM[-DD]]"
                            onBlur={(e) => setParam('date', e.target.value.trim())} />
              </CCol>
              <CCol md={3}>
                <CFormLabel className="small mb-1">Between</CFormLabel>
                <div className="d-flex gap-1">
                  <CFormInput size="sm" type="date" value={params.get('from') || ''}
                              onChange={(e) => setParam('from', e.target.value)} />
                  <CFormInput size="sm" type="date" value={params.get('to') || ''}
                              onChange={(e) => setParam('to', e.target.value)} />
                </div>
              </CCol>
              <CCol md={2}>
                <CFormLabel className="small mb-1">Type</CFormLabel>
                <CFormSelect size="sm" value={params.get('type') || ''} onChange={(e) => setParam('type', e.target.value)}>
                  <option value="">Photos and videos</option>
                  <option value="photo">Photos</option>
                  <option value="video">Videos</option>
                  <option value="raw">RAW</option>
                  <option value="live">Live Photos</option>
                </CFormSelect>
              </CCol>
              <CCol md={2}>
                <CFormLabel className="small mb-1">Camera</CFormLabel>
                <CFormInput size="sm" defaultValue={params.get('camera') || ''} placeholder="iPhone, Canon…"
                            onBlur={(e) => setParam('camera', e.target.value.trim())} />
              </CCol>
              <CCol md={2}>
                <CFormLabel className="small mb-1">Location data</CFormLabel>
                <CFormSelect size="sm" value={params.get('has_gps') || ''} onChange={(e) => setParam('has_gps', e.target.value)}>
                  <option value="">Either</option>
                  <option value="1">Has GPS</option>
                  <option value="0">No GPS</option>
                </CFormSelect>
              </CCol>
              <CCol md={2}>
                <CFormLabel className="small mb-1">Sort</CFormLabel>
                <CFormSelect size="sm" value={`${params.get('sort') || 'date'}:${params.get('dir') || 'desc'}`}
                             onChange={(e) => {
                               const [s, d] = e.target.value.split(':');
                               const next = new URLSearchParams(params);
                               next.set('sort', s); next.set('dir', d);
                               setParams(next);
                             }}>
                  <option value="date:desc">Newest first</option>
                  <option value="date:asc">Oldest first</option>
                  <option value="name:asc">Name</option>
                  <option value="size:desc">Largest first</option>
                </CFormSelect>
              </CCol>
              <CCol md={4} className="d-flex align-items-end gap-3">
                <CFormCheck label="Unnamed faces" checked={params.get('unnamed_faces') === '1'}
                            onChange={(e) => setParam('unnamed_faces', e.target.checked ? '1' : '')} />
                <CFormCheck label="Hide duplicates" checked={params.get('duplicates') === '0'}
                            onChange={(e) => setParam('duplicates', e.target.checked ? '0' : '')} />
                <CFormCheck label="Include removed" checked={params.get('removed') === '1'}
                            onChange={(e) => setParam('removed', e.target.checked ? '1' : '')} />
              </CCol>
            </CRow>
          )}

          {chips.length > 0 && (
            <div className="d-flex flex-wrap gap-1 mt-2">
              {chips.map((c) => (
                <CBadge key={c.k} color="light" className="text-dark border d-flex align-items-center gap-1"
                        style={{ fontWeight: 500, fontSize: '0.78rem' }}>
                  {c.text}
                  <CIcon icon={cilX} style={{ width: 11, height: 11, cursor: 'pointer' }}
                         onClick={() => setParam(c.k, '')} />
                </CBadge>
              ))}
            </div>
          )}

          {/* Saved searches: one click runs one, as the target of "Add". */}
          <div className="d-flex flex-wrap align-items-center gap-1 mt-2" style={{ fontSize: '0.8rem' }}>
            <span className="text-body-secondary me-1">
              <CIcon icon={cilBookmark} style={{ width: 13, height: 13 }} /> Saved:
            </span>
            {saved.length === 0 && <span className="text-body-secondary">none yet</span>}
            {saved.slice(0, 8).map((s) => (
              <CButton key={s.id} size="sm" color={String(s.id) === target ? 'primary' : 'light'}
                       className="py-0 px-2" onClick={() => runSaved(s)} title={s.query || 'no query'}>
                {s.name} <span className="text-body-secondary">· {s.file_count}</span>
              </CButton>
            ))}
            <CButton size="sm" color="link" className="py-0" onClick={() => navigate('/saved-searches')}>all…</CButton>
            {running ? (
              <CButton size="sm" color="primary" variant="outline" className="py-0 ms-auto"
                       disabled={!anySelected} onClick={() => addTo(running.id, running.name)}
                       title={anySelected ? undefined : 'Select results to add them'}>
                {anySelected ? `Add ${howMany} selected to “${running.name}”` : `Add to “${running.name}”`}
              </CButton>
            ) : (
              <CButton size="sm" color="primary" variant="outline" className="py-0 ms-auto"
                       onClick={() => { setNewName(params.get('q') || ''); setNameModal({ mode: 'save' }); }}>
                Save this search{anySelected ? ` with ${howMany} selected` : ''}
              </CButton>
            )}
          </div>
        </CCardBody>
      </CCard>

      {notice && <CAlert color={notice.color} dismissible onClose={() => setNotice(null)}>{notice.text}</CAlert>}
      {error && <CAlert color="danger">{error}</CAlert>}
      {data?.index && !data.index.ready && (
        <CAlert color="info" className="py-2" style={{ fontSize: '0.85rem' }}>
          The name index is catching up ({data.index.behind.toLocaleString()} names to go) - results are complete,
          just a little slower until it has.
        </CAlert>
      )}

      {/* Selection bar */}
      {results.length > 0 && (
        <div className="d-flex align-items-center flex-wrap gap-2 mb-2" style={{ fontSize: '0.85rem' }}>
          <CButton size="sm" color="secondary" variant="outline" onClick={selectPage}>
            {pageChecked ? 'Unselect this page' : 'Select this page'}
          </CButton>
          <CButton size="sm" color="secondary" variant="outline"
                   onClick={() => {
                     if (allSelected) { clearSelection(); return; }
                     setSelected(new Set());
                     setExcluded(new Set());
                     setAnchor(null);
                     setAllSelected(true);
                   }}>
            {allSelected ? 'Unselect all' : `Select all ${data.total_capped ? `${data.total.toLocaleString()}+` : data.total.toLocaleString()} results`}
          </CButton>
          <CButton size="sm" color="secondary" variant={rangeMode ? undefined : 'outline'}
                   title="Click the first photo, then the last: everything between follows the first click - selected or unselected. Shift-click does the same."
                   onClick={() => { setRangeMode((v) => !v); setAnchor(null); }}>
            {rangeMode ? 'Cancel range' : 'Select range'}
          </CButton>
          {rangeMode && (
            <span className="text-body-secondary">
              {anchor
                ? `Now click the last photo to ${anchor.on ? 'select' : 'unselect'} everything between`
                : 'Click the first photo of the range'}
            </span>
          )}
          {anySelected && (
            <>
              <strong>{howMany} selected</strong>
              {running ? (
                <CButton color="primary" size="sm" onClick={() => addTo(running.id, running.name)}>
                  Add to “{running.name}”
                </CButton>
              ) : (
              <CDropdown>
                <CDropdownToggle color="primary" size="sm">Add to saved search</CDropdownToggle>
                <CDropdownMenu>
                  {saved.map((s) => (
                    <CDropdownItem key={s.id} onClick={() => addTo(s.id, s.name)}>{s.name}</CDropdownItem>
                  ))}
                  {saved.length > 0 && <CDropdownDivider />}
                  <CDropdownItem onClick={() => { setNewName(''); setNameModal({ mode: 'save' }); }}>
                    New saved search…
                  </CDropdownItem>
                </CDropdownMenu>
              </CDropdown>
              )}
            </>
          )}
          {loading && <CSpinner size="sm" />}
        </div>
      )}

      {!loading && data && results.length === 0 && (
        <CCard><CCardBody className="text-center text-body-secondary py-5">
          Nothing matches{chips.length ? ' - try removing a filter' : ''}.
        </CCardBody></CCard>
      )}

      {results.length > 0 && (
        <CCard>
          <CTable hover responsive className="mb-0 align-middle" style={{ fontSize: '0.84rem' }}>
            <CTableHead>
              <CTableRow>
                <CTableHeaderCell style={{ width: 32 }} />
                <CTableHeaderCell style={{ width: 92 }} />
                <CTableHeaderCell>Name</CTableHeaderCell>
                <CTableHeaderCell>Taken</CTableHeaderCell>
                <CTableHeaderCell>People · Pets</CTableHeaderCell>
                <CTableHeaderCell className="d-none d-lg-table-cell">Place</CTableHeaderCell>
                <CTableHeaderCell className="d-none d-xl-table-cell">Event</CTableHeaderCell>
                <CTableHeaderCell className="d-none d-md-table-cell">Type</CTableHeaderCell>
                <CTableHeaderCell className="d-none d-md-table-cell text-end">Size</CTableHeaderCell>
              </CTableRow>
            </CTableHead>
            <CTableBody>
              {results.map((r) => {
                const dup = r.kind === 'duplicate';
                const key = dup ? `d${r.duplicate?.op_id}` : `f${r.file_id}`;
                return (
                  <CTableRow key={key} style={{ cursor: 'pointer', opacity: r.removed_at ? 0.55 : 1 }}
                             onClick={() => open(r)}>
                    <CTableDataCell onClick={(e) => e.stopPropagation()}>
                      <CFormCheck checked={isChecked(r.file_id)}
                                  onChange={(e) => toggle(r.file_id, !!e.nativeEvent?.shiftKey)} />
                    </CTableDataCell>
                    <CTableDataCell>
                      <img src={`${API_HOST}${r.thumbnail_url}`} alt="" loading="lazy"
                           style={{ width: 80, height: 80, objectFit: 'cover', borderRadius: 4, display: 'block',
                                    background: '#f1f3f5' }} />
                    </CTableDataCell>
                    <CTableDataCell style={{ maxWidth: 380 }}>
                      {dup ? (
                        <>
                          <div className="text-truncate">
                            <CBadge color="warning" className="me-1">DUPLICATE</CBadge>
                            <strong>{basename(r.duplicate?.source_path)}</strong>
                            <span className="text-body-secondary"> → {r.name}</span>
                          </div>
                          <div className="text-body-secondary text-truncate" style={{ fontSize: '0.76rem' }}
                               title={r.duplicate?.source_path}>
                            turned away {fmtTaken(r.duplicate?.at)}
                            {r.duplicate?.moved_to ? ` · now ${r.duplicate.moved_to}` : ` · was ${r.duplicate?.source_path}`}
                          </div>
                        </>
                      ) : (
                        <>
                          <div className="text-truncate fw-semibold" title={r.name}>
                            {r.name}
                            {r.removed_at && <CBadge color="secondary" className="ms-1">removed</CBadge>}
                          </div>
                          {(r.matched && r.matched.on !== 'stored') || r.original ? (
                            <div className="text-body-secondary text-truncate" style={{ fontSize: '0.76rem' }}
                                 title={r.matched?.text || r.original}>
                              {r.matched && r.matched.on === 'original' ? `original: ${r.matched.text}`
                                : r.original ? `original: ${basename(r.original)}` : ''}
                            </div>
                          ) : null}
                        </>
                      )}
                    </CTableDataCell>
                    <CTableDataCell className="text-nowrap">
                      {fmtTaken(r.taken)}
                      {r.taken_source && !['exif', 'EXIF'].includes(r.taken_source) && (
                        <div className="text-body-secondary" style={{ fontSize: '0.72rem' }}>from {r.taken_source}</div>
                      )}
                    </CTableDataCell>
                    <CTableDataCell style={{ maxWidth: 220 }}>
                      <div className="text-truncate">{[...r.people, ...r.pets].join(', ')}</div>
                      {r.unnamed_faces > 0 && (
                        <div className="text-body-secondary" style={{ fontSize: '0.72rem' }}>
                          + {r.unnamed_faces} unnamed face{r.unnamed_faces === 1 ? '' : 's'}
                        </div>
                      )}
                    </CTableDataCell>
                    <CTableDataCell className="d-none d-lg-table-cell text-truncate" style={{ maxWidth: 160 }}>{r.place}</CTableDataCell>
                    <CTableDataCell className="d-none d-xl-table-cell text-truncate" style={{ maxWidth: 160 }}>{r.event}</CTableDataCell>
                    <CTableDataCell className="d-none d-md-table-cell">{r.type}</CTableDataCell>
                    <CTableDataCell className="d-none d-md-table-cell text-end text-nowrap">{fmtBytes(r.size)}</CTableDataCell>
                  </CTableRow>
                );
              })}
            </CTableBody>
          </CTable>
          <div className="d-flex justify-content-between align-items-center p-2 border-top">
            <CButton size="sm" color="secondary" variant="outline" disabled={page === 0}
                     onClick={() => setCursors((c) => c.slice(0, -1))}>← Previous</CButton>
            <span className="text-body-secondary" style={{ fontSize: '0.8rem' }}>
              Page {page + 1}{data?.took_ms != null ? ` · ${data.took_ms} ms` : ''}
            </span>
            <CButton size="sm" color="secondary" variant="outline" disabled={!data?.next_cursor}
                     onClick={() => setCursors((c) => [...c, data.next_cursor])}>Next →</CButton>
          </div>
        </CCard>
      )}

      <CModal visible={!!nameModal} onClose={() => setNameModal(null)}>
        <CModalHeader><CModalTitle>Save this search</CModalTitle></CModalHeader>
        <CModalBody>
          <CFormLabel>Name</CFormLabel>
          <CFormInput value={newName} onChange={(e) => setNewName(e.target.value)} autoFocus
                      onKeyDown={(e) => { if (e.key === 'Enter') createSaved(); }} />
          <div className="text-body-secondary mt-2" style={{ fontSize: '0.82rem' }}>
            Keeps this search's query, to run again, and
            {anySelected ? ` the ${howMany} results selected` : ' no photos yet - add some from the results'}.
            Idle for 30 days it is flagged, and deleted at 60 - never its photos.
          </div>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setNameModal(null)}>Cancel</CButton>
          <CButton color="primary" disabled={!newName.trim()} onClick={createSaved}>Save</CButton>
        </CModalFooter>
      </CModal>
    </CContainer>
  );
};

export default Search;
