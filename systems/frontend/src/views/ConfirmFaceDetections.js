import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import {
  CCard,
  CCardBody,
  CContainer,
  CRow,
  CCol,
  CButton,
  CFormLabel,
  CFormRange,
  CFormInput,
  CForm,
  CSpinner,
  CAlert,
  CBadge,
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CModalFooter,
  CNav,
  CNavItem,
  CNavLink,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPlus } from '@coreui/icons';
import api, { faceDetectionAPI, faceRecognitionAPI, peopleCategoryAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import FaceThumbnail from '../components/FaceThumbnail';
import {
  defaultMinSimilarity,
  formatSimilarity,
  matchSimilarityLabel,
} from '../utils/similarity';
import { categoryOf, categoryTabs, countByCategory, openingTab } from '../utils/peopleCategories';
import { API_HOST, FILES_BASE_URL, PEOPLE_PHOTOS_BASE_URL } from '../config';

const IMAGE_BASE = FILES_BASE_URL;

/**
 * Pull the distance for the active metric out of a best_match / score row.
 * Every metric's column is always present in the API response, so the view only
 * has to choose; adding a metric means one line here plus a METRICS entry.
 */
const pickMetricDistance = (row, metric) => {
  if (!row) return null;
  if (metric === 'contextual') return row.contextual_distance;
  return row.distance;
};
const PAGE_SIZE = 100;
const DEFAULT_METRIC = 'cosine';

// Metrics available for comparison — add entries here as new columns are computed.
// Scores are stored as distances but always presented as a 0–100 similarity
// score (higher = closer). See utils/similarity.js for the conversion.
//
// Euclidean was here and has been removed: for L2-normalised embeddings it is
// sqrt(2 * cosine_distance), a strictly monotone transform of the metric beside
// it. It produced an identical ranking with different numbers on the cards, so
// the only thing it could do was make the same pair look like two answers.
const METRICS = [
  { value: 'cosine',     label: 'Cosine',     description: 'Cosine similarity (100 = identical)' },
  { value: 'contextual', label: 'Contextual', description: 'Appearance fused with event, time and co-occurrence context — the score is the probability this is that person' },
];


// ─── Similarity bar ───────────────────────────────────────────────────────────

// Fills in proportion to the similarity score — a fuller, greener bar = closer match.
const SimilarityBar = ({ similarity, minSimilarity }) => {
  const pct = Math.max(0, Math.min(100, similarity ?? 0));
  const color = pct >= minSimilarity ? '#198754' : '#e6a817';
  return (
    <div style={{ background: '#e9ecef', borderRadius: 4, height: 5, width: '100%', marginTop: 4 }}>
      <div style={{ width: `${pct}%`, background: color, height: '100%', borderRadius: 4 }} />
    </div>
  );
};

// ─── Profile photo thumbnail for the people list ──────────────────────────────

const ProfileThumb = ({ person, size = 40 }) => {
  const [error, setError] = useState(false);
  const path = person.profile_photo_display_path;
  const box = person.profile_photo_face_box;

  if (!path || error) {
    return (
      <div style={{
        width: size, height: size, borderRadius: '50%', background: '#e9ecef',
        display: 'flex', alignItems: 'center', justifyContent: 'center',
        fontSize: size * 0.4, color: '#adb5bd', flexShrink: 0,
      }}>
        {person.name.charAt(0).toUpperCase()}
      </div>
    );
  }

  const src = `${IMAGE_BASE}${encodeURIComponent(path)}`;

  if (!box || !box.img_width || !box.img_height) {
    return (
      <img
        src={src}
        alt={person.name}
        style={{ width: size, height: size, borderRadius: '50%', objectFit: 'cover', flexShrink: 0 }}
        onError={() => setError(true)}
      />
    );
  }

  const scale = Math.min(size / box.w, size / box.h);
  const renderedW = box.img_width * scale;
  const renderedH = box.img_height * scale;
  const offsetX = -(box.x * scale);
  const offsetY = -(box.y * scale);

  return (
    <div style={{ width: size, height: size, borderRadius: '50%', overflow: 'hidden', flexShrink: 0 }}>
      <img
        src={src}
        alt={person.name}
        style={{ width: renderedW, height: renderedH, marginLeft: offsetX, marginTop: offsetY, display: 'block' }}
        onError={() => setError(true)}
      />
    </div>
  );
};

// ─── Candidate card ───────────────────────────────────────────────────────────

const CandidateCard = ({ face, minSimilarity, metric, catalogId, personId, onConfirmed, onRejected, onOpenTagModal }) => {
  const [busy, setBusy] = useState(null); // 'confirm' | 'reject' | null
  const [done, setDone] = useState(null); // 'confirmed' | 'rejected' | null
  const [err, setErr] = useState(null);

  // The photograph's date: master_datetime, decided at ingest.
  const dateStr = face.master_datetime ? face.master_datetime.slice(0, 10) : null;

  const handleConfirm = async () => {
    setBusy('confirm');
    setErr(null);
    try {
      await faceDetectionAPI.tagFace(catalogId, face.face_id, personId);
      setDone('confirmed');
      onConfirmed(face.face_id);
    } catch {
      setErr('Failed to confirm. Try again.');
    } finally {
      setBusy(null);
    }
  };

  const handleReject = async () => {
    setBusy('reject');
    setErr(null);
    try {
      await faceRecognitionAPI.rejectCandidate(catalogId, face.face_id, personId);
      setDone('rejected');
      onRejected(face.face_id);
    } catch {
      setErr('Failed to reject. Try again.');
    } finally {
      setBusy(null);
    }
  };

  // Pick the right distance value for the active metric, then present it as a 0–100 similarity
  const metricDistance = pickMetricDistance(face.best_match, metric);
  const similarity = formatSimilarity(metricDistance, metric);
  const similarityColor = similarity != null && similarity >= minSimilarity ? '#198754' : '#e6a817';

  return (
    <div style={{
      border: '1px solid #dee2e6',
      borderRadius: 10,
      padding: 12,
      background: done === 'confirmed' ? '#d1e7dd' : done === 'rejected' ? '#f8d7da' : '#fff',
      opacity: done ? 0.65 : 1,
      display: 'flex',
      flexDirection: 'column',
      alignItems: 'center',
      gap: 8,
      width: 148,
    }}>
      <div
        onClick={() => onOpenTagModal(face)}
        title="Tag this face"
        style={{ cursor: 'pointer' }}
      >
        <FaceThumbnail face={face} size={96} />
      </div>

      {dateStr && (
        <div style={{ width: '100%', textAlign: 'center', fontSize: '0.75rem', color: '#495057' }}>
          {dateStr}
        </div>
      )}

      {face.best_match && similarity != null && (
        <div style={{ width: '100%' }}>
          <div style={{ textAlign: 'center' }}>
            <span
              style={{
                fontSize: '0.72rem', fontWeight: 600, color: similarityColor,
                background: similarityColor + '1a', borderRadius: 4, padding: '1px 6px',
              }}
              title={`${metric} distance ${metricDistance.toFixed(3)}`}
            >
              {similarity}
            </span>
          </div>
          <SimilarityBar similarity={similarity} minSimilarity={minSimilarity} />
        </div>
      )}
      {face.best_match && metricDistance == null && (
        <div style={{ width: '100%', textAlign: 'center' }}>
          <span style={{ fontSize: '0.68rem', color: '#adb5bd' }}>no {metric} score</span>
        </div>
      )}

      {err && (
        <div style={{ fontSize: '0.7rem', color: '#dc3545', textAlign: 'center' }}>{err}</div>
      )}

      {!done && (
        <div style={{ display: 'flex', gap: 6, width: '100%' }}>
          <CButton
            size="sm"
            color="success"
            style={{ flex: 1, fontSize: '0.75rem', padding: '3px 0' }}
            onClick={handleConfirm}
            disabled={!!busy}
          >
            {busy === 'confirm' ? <CSpinner size="sm" /> : '✓'}
          </CButton>
          <CButton
            size="sm"
            color="danger"
            variant="outline"
            style={{ flex: 1, fontSize: '0.75rem', padding: '3px 0' }}
            onClick={handleReject}
            disabled={!!busy}
          >
            {busy === 'reject' ? <CSpinner size="sm" /> : '✗'}
          </CButton>
        </div>
      )}

      {done === 'confirmed' && (
        <div style={{ fontSize: '0.75rem', color: '#198754', fontWeight: 600 }}>✓ Confirmed</div>
      )}
      {done === 'rejected' && (
        <div style={{ fontSize: '0.75rem', color: '#dc3545', fontWeight: 600 }}>✗ Rejected</div>
      )}
    </div>
  );
};

// ─── Auto-tagged faces panel (Phase 4) ────────────────────────────────────────
// Surfaces anything the model tagged without human review, with the evidence
// behind each decision and a one-click undo. Auto-tags never enter the reference
// set, so reverting one costs nothing downstream.

const EvidenceList = ({ evidenceJson }) => {
  let evidence = null;
  try {
    evidence = JSON.parse(evidenceJson || '{}');
  } catch {
    return null;
  }
  const terms = evidence.terms || [];
  if (!terms.length) return null;
  return (
    <div style={{ marginTop: 6, fontSize: '0.7rem', color: '#555' }}>
      {terms.map((t, i) => (
        <div key={i} style={{ display: 'flex', gap: 6, lineHeight: 1.5 }}>
          <span style={{
            fontWeight: 600, minWidth: 92,
            color: t.contrib >= 0 ? '#198754' : '#b02a37',
          }}>
            {t.signal} {t.contrib >= 0 ? '+' : ''}{Number(t.contrib).toFixed(2)}
          </span>
          <span style={{ color: '#888' }}>{t.detail}</span>
        </div>
      ))}
    </div>
  );
};

const AutoTaggedPanel = ({ catalogId }) => {
  const [tags, setTags] = useState([]);
  const [loading, setLoading] = useState(false);
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(null);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    if (!catalogId) return;
    setLoading(true);
    setError(null);
    try {
      const r = await faceRecognitionAPI.getAutoTags(catalogId);
      setTags(r.data.auto_tags || []);
    } catch {
      setTags([]);
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  const revertOne = async (faceId) => {
    setBusy(faceId);
    try {
      await faceRecognitionAPI.revertAutoTag(catalogId, faceId);
      setTags(prev => prev.filter(t => t.face_detection_id !== faceId));
    } catch {
      setError('Could not revert that tag.');
    } finally {
      setBusy(null);
    }
  };

  const revertRun = async (executionId) => {
    setBusy(`run-${executionId}`);
    try {
      await faceRecognitionAPI.revertAutoTagRun(catalogId, executionId);
      setTags(prev => prev.filter(t => t.execution_id !== executionId));
    } catch {
      setError('Could not revert that run.');
    } finally {
      setBusy(null);
    }
  };

  if (!loading && tags.length === 0) return null;   // nothing auto-tagged, stay out of the way

  const runs = [...new Set(tags.map(t => t.execution_id).filter(Boolean))];

  return (
    <CCard className="mb-3" style={{ borderLeft: '4px solid #6f42c1' }}>
      <CCardBody className="py-3">
        <div className="d-flex align-items-center justify-content-between">
          <div>
            <strong style={{ fontSize: '0.9rem' }}>Auto-tagged by the model</strong>
            <CBadge color="secondary" className="ms-2">{tags.length}</CBadge>
            <div style={{ fontSize: '0.78rem', color: '#888', marginTop: 2 }}>
              Assigned without human review. These are not used as reference faces —
              confirm one to promote it, or revert it back to the queue.
            </div>
          </div>
          <div className="d-flex gap-2">
            {runs.map(run => (
              <CButton
                key={run} size="sm" color="secondary" variant="outline"
                disabled={busy === `run-${run}`}
                onClick={() => revertRun(run)}
              >
                {busy === `run-${run}` ? <CSpinner size="sm" /> : `Revert run #${run}`}
              </CButton>
            ))}
            <CButton size="sm" color="secondary" variant="outline"
                     onClick={() => setOpen(o => !o)}>
              {open ? 'Hide' : 'Review'}
            </CButton>
          </div>
        </div>

        {error && <div style={{ fontSize: '0.78rem', color: '#dc3545', marginTop: 6 }}>{error}</div>}

        {open && (
          <div className="mt-3" style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
            {tags.map(tag => (
              <div key={tag.id} style={{
                border: '1px solid #dee2e6', borderRadius: 8, padding: 10, width: 300,
              }}>
                <div style={{ display: 'flex', gap: 10 }}>
                  <FaceThumbnail
                    face={{
                      ai_crop_path: tag.ai_crop_path, file_path: tag.file_path,
                      x: tag.x, y: tag.y, w: tag.w, h: tag.h,
                      image_width: tag.image_width, image_height: tag.image_height,
                    }}
                    size={64}
                  />
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div className="fw-semibold" style={{ fontSize: '0.85rem' }}>
                      {tag.person_name || `Person ${tag.person_id}`}
                    </div>
                    <div style={{ fontSize: '0.75rem', color: '#198754', fontWeight: 600 }}>
                      similarity {Math.round((1 - tag.contextual_distance) * 100)}
                      <span style={{ color: '#888', fontWeight: 400 }}>
                        {' '}({(tag.confidence * 100).toFixed(1)}% confidence)
                      </span>
                    </div>
                    <div style={{
                      fontSize: '0.7rem', color: '#888', overflow: 'hidden',
                      textOverflow: 'ellipsis', whiteSpace: 'nowrap',
                    }}>
                      {tag.file_name}
                    </div>
                  </div>
                </div>
                <EvidenceList evidenceJson={tag.evidence} />
                <CButton
                  size="sm" color="secondary" variant="outline" className="w-100 mt-2"
                  disabled={busy === tag.face_detection_id}
                  onClick={() => revertOne(tag.face_detection_id)}
                >
                  {busy === tag.face_detection_id ? <CSpinner size="sm" /> : 'Revert'}
                </CButton>
              </div>
            ))}
          </div>
        )}
      </CCardBody>
    </CCard>
  );
};


// ─── Main component ───────────────────────────────────────────────────────────

const ConfirmFaceDetections = () => {
  const { activeCatalog } = useCatalog();
  const [minSimilarity, setMinSimilarity] = useState(() => defaultMinSimilarity());
  const metric = activeCatalog?.similarity_metric || DEFAULT_METRIC;

  const [people, setPeople] = useState([]);
  // Per person {candidates, matched}, counted on the server
  // (/faces/unidentified/summary). This page used to download every
  // unidentified face in the catalog and count them here - 469,839 faces and
  // 188 MB on `family` - to show the handful that had a match.
  const [summary, setSummary] = useState({ people: {}, has_scores: false, has_unidentified: false });
  const [loadingData, setLoadingData] = useState(false);
  const [error, setError] = useState(null);

  const [selectedPerson, setSelectedPerson] = useState(null);
  const [actioned, setActioned] = useState(new Set()); // face_ids confirmed or rejected this session
  // How many of each person's candidates were actioned since the summary was
  // counted, so the badges go down as the reviewer works without a recount.
  const [actionedByPerson, setActionedByPerson] = useState({});
  const [confirmedCount, setConfirmedCount] = useState(0);

  // The selected person's candidates: ONE PAGE, from the server.
  const [candidates, setCandidates] = useState({ faces: [], total: 0, offset: 0 });
  const [loadingFaces, setLoadingFaces] = useState(false);

  // The slider moves a step per pixel; counting per step would be a request
  // per pixel. The view follows the value it settles on.
  const [settledMinSimilarity, setSettledMinSimilarity] = useState(minSimilarity);
  useEffect(() => {
    const t = setTimeout(() => setSettledMinSimilarity(minSimilarity), 300);
    return () => clearTimeout(t);
  }, [minSimilarity]);

  // ── Tag Face modal state ──────────────────────────────────────────────────
  const [showTagModal, setShowTagModal] = useState(false);
  const [showAddPersonModal, setShowAddPersonModal] = useState(false);
  const [tagModalFace, setTagModalFace] = useState(null); // face object being tagged
  const [newPersonName, setNewPersonName] = useState('');
  const [shouldReopenTagModal, setShouldReopenTagModal] = useState(false);
  const [peopleCategories, setPeopleCategories] = useState([]);
  const [tagCategory, setTagCategory] = useState('Family');   // the picker's open tab

  // ─── The Tag Face picker's tabs: one per people category ─────────────────
  // Family first and where it opens - see utils/peopleCategories. The same
  // tabs as the picker on the Photos page.
  const tagTabs = categoryTabs(peopleCategories, people);
  const tagTabCounts = countByCategory(people);
  const peopleInTagTab = people.filter((p) => categoryOf(p) === tagCategory);

  // Every time the picker opens, it opens on Family.
  useEffect(() => {
    if (showTagModal) setTagCategory(openingTab(tagTabs));
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [showTagModal]);

  // ── People, and how many candidates each has ─────────────────────────────
  // Numbered so a slow answer for an old minimum cannot land over a newer one.
  const summaryRequest = useRef(0);
  const loadSummary = useCallback(async (catalogId, activeMetric, minSim) => {
    const seq = ++summaryRequest.current;
    const r = await faceRecognitionAPI.getUnidentifiedSummary(
      catalogId, { metric: activeMetric, min_similarity: minSim });
    if (seq !== summaryRequest.current) return;
    setSummary(r.data);
    setActionedByPerson({});
  }, []);

  const loadData = useCallback(async (catalogId, activeMetric, minSim) => {
    if (!catalogId) return;
    setLoadingData(true);
    setError(null);
    setSelectedPerson(null);
    setActioned(new Set());
    setConfirmedCount(0);
    setCandidates({ faces: [], total: 0, offset: 0 });
    try {
      const [peopleRes, categories] = await Promise.all([
        api.get(`/catalogs/${catalogId}/people`),
        // The Tag Face picker's tabs. A failure leaves them to be derived
        // from the people alone rather than losing the picker.
        peopleCategoryAPI.getAll(catalogId).catch(() => ({ data: [] })),
        loadSummary(catalogId, activeMetric, minSim),
      ]);
      setPeople(peopleRes.data || []);
      setPeopleCategories(categories.data || []);
    } catch {
      setError('Failed to load data. Check that the catalog is accessible.');
    } finally {
      setLoadingData(false);
    }
  }, [loadSummary]);

  useEffect(() => {
    if (activeCatalog) {
      loadData(activeCatalog.id, metric, settledMinSimilarity);
    } else {
      setPeople([]);
      setSummary({ people: {}, has_scores: false, has_unidentified: false });
    }
  // A new minimum recounts below without clearing the selection.
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCatalog, metric, loadData]);

  // A new minimum recounts the badges, keeping the person selected.
  const firstMin = useRef(true);
  useEffect(() => {
    if (firstMin.current) { firstMin.current = false; return; }
    if (activeCatalog) {
      loadSummary(activeCatalog.id, metric, settledMinSimilarity).catch(() => {});
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [settledMinSimilarity]);

  // ── One page of the selected person's candidates ─────────────────────────
  const facesRequest = useRef(0);
  const loadCandidates = useCallback(async (offset) => {
    if (!activeCatalog || !selectedPerson) return;
    const seq = ++facesRequest.current;
    setLoadingFaces(true);
    try {
      const r = await faceRecognitionAPI.getUnidentifiedFaces(activeCatalog.id, {
        person_id: selectedPerson.id,
        metric,
        min_similarity: settledMinSimilarity,
        limit: PAGE_SIZE,
        offset,
      });
      if (seq !== facesRequest.current) return;
      setCandidates({ faces: r.data.faces || [], total: r.data.total || 0,
                      offset: r.data.offset ?? offset });
    } catch {
      if (seq !== facesRequest.current) return;
      setError('Failed to load candidates.');
      setCandidates({ faces: [], total: 0, offset: 0 });
    } finally {
      if (seq === facesRequest.current) setLoadingFaces(false);
    }
  }, [activeCatalog, selectedPerson, metric, settledMinSimilarity]);

  // A new person, minimum or metric starts at their first page.
  useEffect(() => {
    if (selectedPerson) loadCandidates(0);
    else setCandidates({ faces: [], total: 0, offset: 0 });
  }, [selectedPerson, loadCandidates]);

  // ── Counts still settling ─────────────────────────────────────────────────
  // `stale`: the server answered from its stored best matches while it is
  // still bringing them up to date - after a scoring run, or on the first visit
  // after an update, which builds them (a minute on a large catalog). Ask again
  // until it is done, then refresh the page on screen once.
  useEffect(() => {
    if (!summary.stale || !activeCatalog) return undefined;
    const t = setTimeout(() => {
      loadSummary(activeCatalog.id, metric, settledMinSimilarity).catch(() => {});
    }, 5000);
    return () => clearTimeout(t);
  }, [summary, activeCatalog, metric, settledMinSimilarity, loadSummary]);

  const wasStale = useRef(false);
  useEffect(() => {
    if (wasStale.current && !summary.stale && selectedPerson) {
      loadCandidates(candidates.offset);
    }
    wasStale.current = !!summary.stale;
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [summary]);

  // ── Per-person candidate counts, as the badges show them ──────────────────
  const candidatesByPerson = useMemo(() => {
    const map = {};
    for (const [pid, counts] of Object.entries(summary.people || {})) {
      const n = (counts.candidates || 0) - (actionedByPerson[pid] || 0);
      if (n > 0) map[pid] = n;
    }
    return map;
  }, [summary, actionedByPerson]);

  // ── Sorted people list ────────────────────────────────────────────────────
  const sortedPeople = useMemo(() => {
    return [...people].sort((a, b) => {
      const ca = candidatesByPerson[a.id] || 0;
      const cb = candidatesByPerson[b.id] || 0;
      return cb - ca;
    });
  }, [people, candidatesByPerson]);

  // ── The page on screen ────────────────────────────────────────────────────
  // Confirmed and rejected faces leave the grid at once; the server stops
  // returning them, which is why paging moves by what is LEFT on this page
  // rather than by a fixed page size - see goNext.
  const pageSlice = useMemo(
    () => candidates.faces.filter(f => !actioned.has(f.face_id)),
    [candidates, actioned]);
  const actionedHere = candidates.faces.length - pageSlice.length;
  const remainingTotal = Math.max(0, candidates.total - actionedHere);
  const safePage = Math.floor(candidates.offset / PAGE_SIZE) + 1;
  const totalPages = Math.max(safePage, Math.ceil(remainingTotal / PAGE_SIZE));

  const goNext = () => loadCandidates(candidates.offset + pageSlice.length);
  const goPrev = () => loadCandidates(Math.max(0, candidates.offset - PAGE_SIZE));

  // Everything on this page dealt with and more waiting: bring in the next lot
  // at the same place, which is where the server's list now continues.
  useEffect(() => {
    if (!loadingFaces && candidates.faces.length > 0 && pageSlice.length === 0
        && remainingTotal > 0) {
      loadCandidates(candidates.offset);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pageSlice.length]);

  // The bulk-accept summary describes one page of one person; keep it from
  // trailing along after the view it refers to has changed.
  useEffect(() => { setAcceptAllResult(null); },
            [selectedPerson, candidates.offset, settledMinSimilarity, metric]);

  // ── Reopen tag modal after people list updates ────────────────────────────
  useEffect(() => {
    if (shouldReopenTagModal && people.length > 0) {
      setShowTagModal(true);
      setShouldReopenTagModal(false);
    }
  }, [people, shouldReopenTagModal]);

  // A face leaves the grid, and its person's badge drops by one.
  const markActioned = useCallback((faceIds) => {
    if (!faceIds.length) return;
    setActioned(prev => new Set([...prev, ...faceIds]));
    const owners = {};
    for (const f of candidates.faces) {
      if (faceIds.includes(f.face_id) && f.best_match) {
        const pid = String(f.best_match.person_id);
        owners[pid] = (owners[pid] || 0) + 1;
      }
    }
    setActionedByPerson(prev => {
      const next = { ...prev };
      for (const [pid, n] of Object.entries(owners)) next[pid] = (next[pid] || 0) + n;
      return next;
    });
  }, [candidates]);

  // ── Action handlers ───────────────────────────────────────────────────────
  // ── Accept every candidate on this page ───────────────────────────────────
  // Scoped to the current page on purpose. "Accept all" over a 900-candidate
  // list would be an irreversible bulk action behind a single click, and the
  // page is the unit the reviewer has actually looked at.
  const [acceptingAll, setAcceptingAll] = useState(false);
  const [acceptAllResult, setAcceptAllResult] = useState(null);
  const [showAcceptAllModal, setShowAcceptAllModal] = useState(false);

  const confirmAcceptPage = useCallback(async () => {
    if (!activeCatalog || !selectedPerson || pageSlice.length === 0) return;
    const faceIds = pageSlice.map(f => f.face_id);

    setAcceptingAll(true);
    setAcceptAllResult(null);
    try {
      const r = await faceDetectionAPI.tagFacesBulk(
        activeCatalog.id, selectedPerson.id, faceIds);
      const assigned = r.data.assigned_face_ids || [];
      markActioned(assigned);
      setConfirmedCount(c => c + assigned.length);
      setAcceptAllResult({
        assigned: r.data.assigned,
        skipped: (r.data.skipped || []).length,
        failed: (r.data.failed || []).length,
      });
    } catch (e) {
      setAcceptAllResult({ error: e?.response?.data?.error || 'Could not tag these faces.' });
    } finally {
      setAcceptingAll(false);
      setShowAcceptAllModal(false);
    }
  }, [activeCatalog, selectedPerson, pageSlice, markActioned]);

  const handleConfirmed = useCallback((faceId) => {
    markActioned([faceId]);
    setConfirmedCount(c => c + 1);
  }, [markActioned]);

  const handleRejected = useCallback((faceId) => {
    markActioned([faceId]);
  }, [markActioned]);

  const handleOpenTagModal = useCallback((face) => {
    setTagModalFace(face);
    setShowTagModal(true);
  }, []);

  const handleTagFace = async (personId) => {
    if (!tagModalFace) return;
    try {
      await faceDetectionAPI.tagFace(activeCatalog.id, tagModalFace.face_id, personId);
      // Mark as confirmed and remove from the grid
      markActioned([tagModalFace.face_id]);
      setConfirmedCount(c => c + 1);
      setShowTagModal(false);
      setTagModalFace(null);
    } catch {
      setError('Failed to tag face. Try again.');
    }
  };

  const handleAddNewPerson = async () => {
    if (!newPersonName.trim()) return;
    try {
      // Into the tab the picker is showing, so the new person is found where
      // they were added rather than under Other.
      const response = await api.post(`/catalogs/${activeCatalog.id}/people`, {
        name: newPersonName.trim(),
        category: tagCategory,
      });
      const newPerson = response.data;
      // Reload people list
      const peopleResponse = await api.get(`/catalogs/${activeCatalog.id}/people`);
      setPeople(peopleResponse.data || []);
      if (tagModalFace && newPerson.id) {
        await faceDetectionAPI.tagFace(activeCatalog.id, tagModalFace.face_id, newPerson.id);
        markActioned([tagModalFace.face_id]);
        setConfirmedCount(c => c + 1);
        setTagModalFace(null);
        setShowTagModal(false);
      } else if (!tagModalFace) {
        setShouldReopenTagModal(true);
      }
      setShowAddPersonModal(false);
      setNewPersonName('');
    } catch {
      setError('Failed to add person.');
    }
  };

  // ── Derived summary ───────────────────────────────────────────────────────
  const totalCandidates = Object.values(candidatesByPerson).reduce((s, n) => s + n, 0);
  const peopleWithCandidates = Object.keys(candidatesByPerson).length;
  const catalogName = activeCatalog?.name ?? '';
  // hasScores: at least one waiting face has the active metric's score
  const hasScores = summary.has_scores;
  const hasUnidentified = summary.has_unidentified;
  const selectedCounts = selectedPerson ? summary.people?.[String(selectedPerson.id)] : null;
  const selectedActioned = selectedPerson ? (actionedByPerson[String(selectedPerson.id)] || 0) : 0;
  const activeMetricLabel = METRICS.find(m => m.value === metric)?.label ?? metric;

  // ── Render ────────────────────────────────────────────────────────────────
  if (!activeCatalog) {
    return (
      <CContainer fluid className="py-4 px-4">
        <div className="text-center py-5 text-muted">
          <strong>No catalog selected</strong>
          <div style={{ marginTop: 8, fontSize: '0.9rem' }}>
            Select a catalog from the dropdown in the top menu to get started.
          </div>
        </div>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="py-4 px-4">

      {/* Page header */}
      <div className="mb-4">
        <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Identify people</h4>
        <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
          Review AI-suggested candidates person by person and confirm or reject each match
        </div>
      </div>

      {/* Controls */}
      <CCard className="mb-3">
        <CCardBody className="py-3">
          <CRow className="align-items-end g-3">
            <CCol md={5}>
              <CFormLabel className="mb-1 fw-semibold" style={{ fontSize: '0.85rem' }}>
                Minimum similarity: <strong>{minSimilarity}</strong>
                <span style={{ fontWeight: 'normal', color: '#888', marginLeft: 8 }}>
                  (0–100, higher = stricter · {matchSimilarityLabel(metric)})
                </span>
              </CFormLabel>
              <CFormRange
                min={0} max={100} step={1}
                value={minSimilarity}
                onChange={e => setMinSimilarity(parseInt(e.target.value, 10))}
              />
            </CCol>
            <CCol md="auto">
              <CButton
                color="secondary"
                variant="outline"
                onClick={() => loadData(activeCatalog.id, metric)}
                disabled={loadingData}
              >
                {loadingData ? <CSpinner size="sm" className="me-1" /> : null}
                Reload
              </CButton>
            </CCol>
          </CRow>
        </CCardBody>
      </CCard>

      {/* Auto-tagged faces (only rendered when the model has tagged something) */}

      <AutoTaggedPanel catalogId={activeCatalog.id} />

      {/* Alerts */}
      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}

      {/* Summary row */}
      {!loadingData && (
        <div className="mb-3" style={{ fontSize: '0.82rem', color: '#6c757d' }}>
          {catalogName && <><strong style={{ color: '#212121' }}>{catalogName}</strong> · </>}
          <strong style={{ color: '#212121' }}>{peopleWithCandidates}</strong> {peopleWithCandidates === 1 ? 'person' : 'people'} with candidates ·{' '}
          <strong style={{ color: '#212121' }}>{totalCandidates}</strong> total candidates ·{' '}
          <strong style={{ color: '#198754' }}>{confirmedCount}</strong> confirmed ·{' '}
          metric: <strong style={{ color: '#212121' }}>{activeMetricLabel}</strong> ·{' '}
          min similarity: <strong style={{ color: '#212121' }}>{minSimilarity}</strong>
          {summary.stale && (
            <CBadge color="info" className="ms-3">
              <CSpinner size="sm" className="me-1" style={{ width: '0.7rem', height: '0.7rem' }} />
              Updating counts…
            </CBadge>
          )}
          {!hasScores && hasUnidentified && !summary.stale && (
            <CBadge color="warning" textColor="dark" className="ms-3">
              No {activeMetricLabel} scores — run the Face Recognition Workflow first
            </CBadge>
          )}
        </div>
      )}

      {/* Loading spinner */}
      {loadingData && (
        <div className="text-center py-5">
          <CSpinner color="primary" />
          <div className="mt-2 text-muted">Loading people and face detections…</div>
        </div>
      )}

      {/* Main two-column layout */}
      {!loadingData && (
        <CRow className="g-3" style={{ alignItems: 'flex-start' }}>

          {/* Left: people list */}
          <CCol md={3} lg={2}>
            <CCard style={{ position: 'sticky', top: 80 }}>
              <CCardBody style={{ padding: 0 }}>
                {people.length === 0 && (
                  <div className="text-center py-4 text-muted" style={{ fontSize: '0.85rem', padding: '16px' }}>
                    No people found.<br />Add people in the People section.
                  </div>
                )}
                {!hasScores && people.length > 0 && hasUnidentified && (
                  <div className="text-center py-3 text-muted" style={{ fontSize: '0.8rem', padding: '12px' }}>
                    No similarity scores yet.<br />Run the Face Recognition Workflow first.
                  </div>
                )}
                {sortedPeople.map(person => {
                  const count = candidatesByPerson[person.id] || 0;
                  const isSelected = selectedPerson?.id === person.id;
                  return (
                    <div
                      key={person.id}
                      onClick={() => setSelectedPerson(person)}
                      style={{
                        display: 'flex',
                        alignItems: 'center',
                        gap: 10,
                        padding: '10px 14px',
                        cursor: 'pointer',
                        background: isSelected ? '#e8f0fe' : 'transparent',
                        borderLeft: isSelected ? '3px solid #3b82d4' : '3px solid transparent',
                        borderBottom: '1px solid #f0f0f0',
                        opacity: count === 0 ? 0.45 : 1,
                        transition: 'background 0.15s',
                      }}
                    >
                      <ProfileThumb person={person} size={36} />
                      <div style={{ flex: 1, minWidth: 0 }}>
                        <div style={{
                          fontSize: '0.83rem', fontWeight: isSelected ? 600 : 400,
                          color: '#212121', whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
                        }}>
                          {person.name}
                        </div>
                      </div>
                      {count > 0 && (
                        <CBadge color="primary" style={{ fontSize: '0.72rem', flexShrink: 0 }}>
                          {count}
                        </CBadge>
                      )}
                    </div>
                  );
                })}
              </CCardBody>
            </CCard>
          </CCol>

          {/* Right: candidate grid */}
          <CCol md={9} lg={10}>
            {!selectedPerson && (
              <CCard>
                <CCardBody className="text-center py-5 text-muted">
                  Select a person from the list to review their face detection candidates.
                </CCardBody>
              </CCard>
            )}

            {selectedPerson && (
              <>
                <div className="mb-3 d-flex align-items-center justify-content-between">
                  <div>
                    <span className="fw-semibold" style={{ fontSize: '1rem', color: '#212121' }}>
                      {selectedPerson.name}
                    </span>
                    <span style={{ fontSize: '0.82rem', color: '#888', marginLeft: 10 }}>
                      {remainingTotal} candidate{remainingTotal !== 1 ? 's' : ''}
                    </span>
                  </div>
                  <div className="d-flex align-items-center gap-2">
                    {pageSlice.length > 0 && (
                      <CButton
                        size="sm" color="primary"
                        disabled={acceptingAll}
                        onClick={() => setShowAcceptAllModal(true)}
                        title={totalPages > 1
                          ? `Tag the ${pageSlice.length} faces on this page as ${selectedPerson.name}`
                          : `Tag all ${pageSlice.length} candidates as ${selectedPerson.name}`}
                      >
                        {`Accept all${totalPages > 1 ? ' on this page' : ''} (${pageSlice.length})`}
                      </CButton>
                    )}
                  {totalPages > 1 && (
                    <div className="d-flex align-items-center gap-2">
                      <CButton
                        size="sm" color="light"
                        disabled={safePage <= 1}
                        onClick={goPrev}
                      >‹ Prev</CButton>
                      <span style={{ fontSize: '0.82rem', color: '#555' }}>
                        {safePage} / {totalPages}
                      </span>
                      <CButton
                        size="sm" color="light"
                        disabled={safePage >= totalPages}
                        onClick={goNext}
                      >Next ›</CButton>
                    </div>
                  )}
                  </div>
                </div>

                {acceptAllResult && (
                  <div className="mb-3" style={{ fontSize: '0.82rem' }}>
                    {acceptAllResult.error ? (
                      <span style={{ color: '#dc3545' }}>{acceptAllResult.error}</span>
                    ) : (
                      <span style={{ color: '#198754' }}>
                        Tagged {acceptAllResult.assigned} face
                        {acceptAllResult.assigned === 1 ? '' : 's'} as {selectedPerson.name}.
                        {acceptAllResult.skipped > 0 &&
                          ` ${acceptAllResult.skipped} already had a person and were left alone.`}
                        {acceptAllResult.failed > 0 &&
                          ` ${acceptAllResult.failed} could not be tagged.`}
                      </span>
                    )}
                  </div>
                )}

                {/* Empty states for selected person */}
                {/* Empty states for selected person - from the server's counts:
                    `matched` is every face whose best match is them, at any
                    similarity. */}
                {!loadingFaces && remainingTotal === 0 && selectedActioned > 0 && (
                  <CCard>
                    <CCardBody className="text-center py-5 text-muted">
                      All candidates for <strong>{selectedPerson.name}</strong> have been reviewed.
                    </CCardBody>
                  </CCard>
                )}
                {!loadingFaces && remainingTotal === 0 && selectedActioned === 0 &&
                  (selectedCounts?.matched || 0) > 0 && (
                  <CCard>
                    <CCardBody className="text-center py-5 text-muted">
                      No candidates for <strong>{selectedPerson.name}</strong> with similarity {minSimilarity} or above.
                      Try lowering the minimum similarity.
                    </CCardBody>
                  </CCard>
                )}
                {!loadingFaces && remainingTotal === 0 && selectedActioned === 0 &&
                  !(selectedCounts?.matched > 0) && (
                  <CCard>
                    <CCardBody className="text-center py-5 text-muted">
                      No similarity scores found for <strong>{selectedPerson.name}</strong>.
                      Run the Face Recognition Workflow first.
                    </CCardBody>
                  </CCard>
                )}
                {loadingFaces && (
                  <div className="text-center py-5"><CSpinner color="primary" /></div>
                )}

                {/* Card grid */}
                {!loadingFaces && pageSlice.length > 0 && (
                  <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
                    {pageSlice.map(face => (
                      <CandidateCard
                        key={face.face_id}
                        face={face}
                        minSimilarity={minSimilarity}
                        metric={metric}
                        catalogId={activeCatalog.id}
                        personId={selectedPerson.id}
                        onConfirmed={handleConfirmed}
                        onRejected={handleRejected}
                        onOpenTagModal={handleOpenTagModal}
                      />
                    ))}
                  </div>
                )}

                {/* Bottom pagination */}
                {totalPages > 1 && (
                  <div className="d-flex align-items-center justify-content-center gap-2 mt-4">
                    <CButton size="sm" color="light" disabled={safePage <= 1} onClick={goPrev}>
                      ‹ Prev
                    </CButton>
                    <span style={{ fontSize: '0.85rem', color: '#555' }}>
                      Page {safePage} of {totalPages}
                    </span>
                    <CButton size="sm" color="light" disabled={safePage >= totalPages} onClick={goNext}>
                      Next ›
                    </CButton>
                  </div>
                )}
              </>
            )}
          </CCol>
        </CRow>
      )}
      {/* ── Tag Face Modal ─────────────────────────────────────────────────── */}
      {/* ── Accept all confirmation ────────────────────────────────────────── */}
      <CModal
        visible={showAcceptAllModal}
        onClose={() => !acceptingAll && setShowAcceptAllModal(false)}
      >
        <CModalHeader>
          <CModalTitle>Accept all candidates</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p className="mb-3">
            Tag <strong>{pageSlice.length}</strong> face{pageSlice.length === 1 ? '' : 's'} as{' '}
            <strong>{selectedPerson?.name}</strong>?
          </p>
          {totalPages > 1 && (
            <CAlert color="warning" className="mb-3 py-2" style={{ fontSize: '0.85rem' }}>
              This affects the current page only — {pageSlice.length} of{' '}
              {remainingTotal} candidates. Page {safePage} of {totalPages}.
            </CAlert>
          )}
          <p className="mb-0 text-muted" style={{ fontSize: '0.85rem' }}>
            Each face becomes a confirmed reference for {selectedPerson?.name}, so future
            recognition runs compare against it. Faces that already have a person are
            left alone.
          </p>
        </CModalBody>
        <CModalFooter>
          <CButton
            color="secondary"
            disabled={acceptingAll}
            onClick={() => setShowAcceptAllModal(false)}
          >
            Cancel
          </CButton>
          <CButton color="primary" disabled={acceptingAll} onClick={confirmAcceptPage}>
            {acceptingAll ? (
              <>
                <CSpinner size="sm" className="me-2" />
                Tagging…
              </>
            ) : (
              `Tag ${pageSlice.length} face${pageSlice.length === 1 ? '' : 's'}`
            )}
          </CButton>
        </CModalFooter>
      </CModal>

      <CModal visible={showTagModal} onClose={() => setShowTagModal(false)} size="lg">
        <CModalHeader>
          <CModalTitle>Tag Face</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p className="mb-3">Select a person to tag this face, or add a new person:</p>
          {/* One tab per people category, Family first. */}
          <CNav variant="tabs" className="mb-3" role="tablist">
            {tagTabs.map((cat) => (
              <CNavItem key={cat}>
                <CNavLink
                  href="#"
                  active={cat === tagCategory}
                  onClick={(e) => { e.preventDefault(); setTagCategory(cat); }}
                >
                  {cat}
                  <CBadge color={cat === tagCategory ? 'primary' : 'secondary'} className="ms-2">
                    {tagTabCounts[cat] || 0}
                  </CBadge>
                </CNavLink>
              </CNavItem>
            ))}
          </CNav>
          {peopleInTagTab.length === 0 && (
            <div className="text-muted text-center py-4" style={{ fontSize: '0.9rem' }}>
              Nobody in {tagCategory} yet. Add a new person to put them here.
            </div>
          )}
          <div className="d-flex flex-wrap gap-3 mb-3">
            {peopleInTagTab.map((person) => {
              const hasProfilePhoto = person.profile_photo_display_path || person.profile_photo_path;
              let imgSrc = null;
              if (hasProfilePhoto) {
                const isCroppedFace = person.profile_photo_path && person.profile_photo_path.startsWith('/');
                if (isCroppedFace) {
                  imgSrc = `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_path)}`;
                } else if (person.profile_photo_display_path) {
                  imgSrc = `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_display_path)}`;
                } else if (person.profile_photo_path) {
                  imgSrc = `${PEOPLE_PHOTOS_BASE_URL}${person.profile_photo_path}`;
                }
              }
              const fb = person.profile_photo_face_box;

              return (
                <div
                  key={person.id}
                  onClick={() => handleTagFace(person.id)}
                  style={{
                    cursor: 'pointer',
                    border: '2px solid #d8dbe0',
                    borderRadius: '8px',
                    overflow: 'hidden',
                    width: '120px',
                    transition: 'all 0.2s',
                    backgroundColor: '#fff',
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = '#321fdb';
                    e.currentTarget.style.boxShadow = '0 2px 8px rgba(50, 31, 219, 0.2)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = '#d8dbe0';
                    e.currentTarget.style.boxShadow = 'none';
                  }}
                >
                  {/* Profile Photo */}
                  <div
                    style={{
                      width: '120px',
                      height: '120px',
                      background: '#f5f5f5',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      overflow: 'hidden',
                      position: 'relative',
                    }}
                  >
                    {imgSrc ? (() => {
                      const isCroppedFace = person.profile_photo_path && person.profile_photo_path.startsWith('/');
                      if (isCroppedFace || !fb || !fb.img_width || !fb.img_height) {
                        return (
                          <img
                            src={imgSrc}
                            alt={person.name}
                            style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                          />
                        );
                      }
                      const pad = Math.max(fb.w, fb.h) * 0.5;
                      const cropX = Math.max(0, fb.x - pad);
                      const cropY = Math.max(0, fb.y - pad);
                      const cropW = Math.min(fb.img_width - cropX, fb.w + pad * 2);
                      const cropH = Math.min(fb.img_height - cropY, fb.h + pad * 2);
                      const scale = fb.img_width / cropW;
                      const bgSize = `${scale * 100}%`;
                      const bgPosX = cropX === 0 ? '0%' : `${(cropX / (fb.img_width - cropW)) * 100}%`;
                      const bgPosY = cropY === 0 ? '0%' : `${(cropY / (fb.img_height - cropH)) * 100}%`;
                      return (
                        <div
                          style={{
                            width: '100%',
                            height: '100%',
                            backgroundImage: `url(${imgSrc})`,
                            backgroundSize: bgSize,
                            backgroundPosition: `${bgPosX} ${bgPosY}`,
                            backgroundRepeat: 'no-repeat',
                          }}
                        />
                      );
                    })() : (
                      <div style={{ fontSize: '2.5rem', opacity: 0.35 }}>👤</div>
                    )}
                  </div>

                  {/* Person Name */}
                  <div
                    style={{
                      padding: '8px',
                      textAlign: 'center',
                      fontSize: '0.85rem',
                      fontWeight: 500,
                      color: '#333',
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {person.name}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="border-top pt-3">
            <CButton
              color="success"
              onClick={() => {
                setShowTagModal(false);
                setShowAddPersonModal(true);
              }}
            >
              <CIcon icon={cilPlus} className="me-2" />
              Add New Person
            </CButton>
          </div>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowTagModal(false)}>
            Cancel
          </CButton>
        </CModalFooter>
      </CModal>

      {/* ── Add New Person Modal ───────────────────────────────────────────── */}
      <CModal visible={showAddPersonModal} onClose={() => setShowAddPersonModal(false)}>
        <CModalHeader>
          <CModalTitle>Add New Person</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <CForm>
            <div className="mb-3">
              <label className="form-label">Name *</label>
              <CFormInput
                value={newPersonName}
                onChange={(e) => setNewPersonName(e.target.value)}
                placeholder="Enter person's name"
                onKeyPress={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    handleAddNewPerson();
                  }
                }}
              />
            </div>
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowAddPersonModal(false)}>
            Cancel
          </CButton>
          <CButton
            color="primary"
            onClick={handleAddNewPerson}
            disabled={!newPersonName.trim()}
          >
            Add Person
          </CButton>
        </CModalFooter>
      </CModal>

    </CContainer>
  );
};

export default ConfirmFaceDetections;
