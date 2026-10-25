import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  CCard,
  CCardBody,
  CContainer,
  CRow,
  CCol,
  CButton,
  CFormLabel,
  CFormRange,
  CSpinner,
  CAlert,
  CBadge,
} from '@coreui/react';
import { faceDetectionAPI, faceRecognitionAPI, workflowAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import {
  defaultMinSimilarity,
  formatSimilarity,
  matchSimilarity,
  meetsSimilarity,
} from '../utils/similarity';
import { API_HOST, FILES_BASE_URL } from '../config';

const SIMILARITY_IMAGE_BASE = FILES_BASE_URL;

// ─── Small helpers ────────────────────────────────────────────────────────────

/** Render a face crop from a source image via CSS clip; falls back to full image. */
const FaceThumbnail = ({ face, size = 100, style = {} }) => {
  const [error, setError] = useState(false);
  const src = `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(face.file_path)}`;

  if (!face.image_width || !face.image_height || error) {
    return (
      <img
        src={src}
        alt="face"
        style={{ width: size, height: size, objectFit: 'cover', borderRadius: 6, ...style }}
        onError={() => setError(true)}
      />
    );
  }

  // Scale so the face crop fills `size` px square
  const scaleX = size / face.w;
  const scaleY = size / face.h;
  const scale = Math.min(scaleX, scaleY);
  const renderedW = face.image_width * scale;
  const renderedH = face.image_height * scale;
  const offsetX = -(face.x * scale);
  const offsetY = -(face.y * scale);

  return (
    <div style={{ width: size, height: size, overflow: 'hidden', borderRadius: 6, flexShrink: 0, ...style }}>
      <img
        src={src}
        alt="face"
        style={{
          width: renderedW,
          height: renderedH,
          marginLeft: offsetX,
          marginTop: offsetY,
          display: 'block',
        }}
        onError={() => setError(true)}
      />
    </div>
  );
};

/** Show a reference face thumbnail from its pre-extracted face_contents file. */
const ReferenceFaceThumbnail = ({ filePath, size = 72 }) => {
  const [error, setError] = useState(false);
  if (!filePath || error) return <div style={{ width: size, height: size, background: '#eee', borderRadius: 4 }} />;
  const src = `${SIMILARITY_IMAGE_BASE}${encodeURIComponent(filePath)}`;
  return (
    <img
      src={src}
      alt="ref"
      style={{ width: size, height: size, objectFit: 'cover', borderRadius: 4 }}
      onError={() => setError(true)}
    />
  );
};

/** Visual similarity bar: fuller and green when close, shorter and red when far. */
const SimilarityBar = ({ similarity, minSimilarity }) => {
  const pct = Math.max(0, Math.min(100, similarity ?? 0));
  const color = pct >= minSimilarity ? '#198754' : '#dc3545';
  return (
    <div style={{ background: '#e9ecef', borderRadius: 4, height: 6, width: '100%', marginTop: 4 }}>
      <div style={{ width: `${pct}%`, background: color, height: '100%', borderRadius: 4, transition: 'width 0.3s' }} />
    </div>
  );
};


// ─── Main component ───────────────────────────────────────────────────────────

const TagWithAI = () => {
  const { activeCatalog } = useCatalog();
  const [minSimilarity, setMinSimilarity] = useState(() => defaultMinSimilarity());
  const [faces, setFaces] = useState([]);
  const [skipped, setSkipped] = useState(new Set());
  const [confirmed, setConfirmed] = useState(new Set());
  const [loading, setLoading] = useState(false);
  const [workflowRunning, setWorkflowRunning] = useState(false);
  const [error, setError] = useState(null);
  const [successMsg, setSuccessMsg] = useState(null);

  // Load each face's scores separately on demand; cache by face_id
  const [scoresCache, setScoresCache] = useState({});
  const [loadingScores, setLoadingScores] = useState({});

  // ── Faces loading ──
  const loadFaces = useCallback(async (catalogId) => {
    if (!catalogId) return;
    setLoading(true);
    setError(null);
    setFaces([]);
    setScoresCache({});
    setSkipped(new Set());
    setConfirmed(new Set());
    try {
      const r = await faceRecognitionAPI.getUnidentifiedFaces(catalogId);
      setFaces(r.data.faces || []);
    } catch {
      setError('Failed to load unidentified faces');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeCatalog) {
      loadFaces(activeCatalog.id);
    } else {
      setFaces([]);
    }
  }, [activeCatalog, loadFaces]);

  // ── Load scores for a single face (lazy) ──
  const loadScores = useCallback(async (faceId) => {
    if (scoresCache[faceId] !== undefined || loadingScores[faceId]) return;
    setLoadingScores(prev => ({ ...prev, [faceId]: true }));
    try {
      const r = await faceRecognitionAPI.getSimilarityScores(activeCatalog?.id, faceId);
      setScoresCache(prev => ({ ...prev, [faceId]: r.data.scores || [] }));
    } catch {
      setScoresCache(prev => ({ ...prev, [faceId]: [] }));
    } finally {
      setLoadingScores(prev => ({ ...prev, [faceId]: false }));
    }
  }, [scoresCache, loadingScores, activeCatalog]);

  // ── Run workflow ──
  const handleRunWorkflow = async () => {
    if (!activeCatalog) return;
    setWorkflowRunning(true);
    setError(null);
    setSuccessMsg(null);
    try {
      // Find the face_recognition_workflow by script filename
      const scriptsR = await workflowAPI.getScripts();
      const scripts = scriptsR.data || [];
      const script = scripts.find(s => s.filename === 'face_recognition_workflow.py');
      if (!script) {
        setError('face_recognition_workflow.py not found in the workflows directory');
        return;
      }

      // Find or create workflow for this catalog
      const wfListR = await workflowAPI.getAll(activeCatalog.id);
      const existing = (wfListR.data || []).find(
        w => w.python_script === script.path
      );

      let workflowId;
      if (existing) {
        workflowId = existing.id;
      } else {
        const newWf = await workflowAPI.create(activeCatalog.id, {
          name: 'Face Recognition',
          description: 'Match unidentified faces against known people using cosine similarity',
          python_script: script.path,
          source_directory: activeCatalog?.target_directory || '',
        });
        workflowId = newWf.data.id;
      }

      await workflowAPI.execute(activeCatalog.id, workflowId, {});
      setSuccessMsg('Workflow started. Reload faces once it completes.');
    } catch (err) {
      setError(`Failed to run workflow: ${err.response?.data?.error || err.message}`);
    } finally {
      setWorkflowRunning(false);
    }
  };

  // ── Confirm a match ──
  const handleConfirm = useCallback(async (faceId, personId) => {
    try {
      await faceDetectionAPI.tagFace(activeCatalog?.id, faceId, personId);
      setConfirmed(prev => new Set([...prev, faceId]));
      setSuccessMsg(null);
    } catch {
      setError(`Failed to tag face ${faceId}`);
    }
  }, [activeCatalog]);

  // ── Derived state ──
  const visibleFaces = faces
    .filter(f => !confirmed.has(f.face_id) && !skipped.has(f.face_id))
    .sort((a, b) => {
      const da = a.best_match?.distance ?? Infinity;
      const db = b.best_match?.distance ?? Infinity;
      return da - db;
    });
  const scoresReady = faces.some(f => f.best_match !== null);

  // ── Render ──
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
      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Tag with AI</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Review AI-suggested identities for unidentified face detections
          </div>
        </div>
        <CButton
          color="primary"
          onClick={handleRunWorkflow}
          disabled={workflowRunning}
        >
          {workflowRunning ? <CSpinner size="sm" className="me-2" /> : null}
          Run Face Recognition Workflow
        </CButton>
      </div>

      {/* Controls */}
      <CCard className="mb-4">
        <CCardBody className="py-3">
          <CRow className="align-items-end g-3">
            <CCol md={4}>
              <CFormLabel className="mb-1 fw-semibold" style={{ fontSize: '0.85rem' }}>
                Minimum similarity: <strong>{minSimilarity}</strong>
                <span style={{ fontWeight: 'normal', color: '#888', marginLeft: 8 }}>
                  (0–100, higher = stricter · DeepFace calls {matchSimilarity('cosine')}+ a match)
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
                onClick={() => loadFaces(activeCatalog.id)}
                disabled={loading}
              >
                {loading ? <CSpinner size="sm" className="me-1" /> : null}
                Reload Faces
              </CButton>
            </CCol>
          </CRow>
        </CCardBody>
      </CCard>

      {/* Status bar */}
      {!loading && (
        <div className="mb-3 d-flex align-items-center gap-3" style={{ fontSize: '0.875rem', color: '#555' }}>
          <span>
            <strong>{visibleFaces.length}</strong> unidentified face{visibleFaces.length !== 1 ? 's' : ''} remaining
            {confirmed.size > 0 && <> · <strong>{confirmed.size}</strong> confirmed</>}
            {skipped.size > 0 && <> · <strong>{skipped.size}</strong> skipped</>}
          </span>
          {!scoresReady && faces.length > 0 && (
            <CBadge color="warning" textColor="dark">
              No similarity scores yet — run the workflow first
            </CBadge>
          )}
          {scoresReady && (
            <CBadge color="success">Similarity scores available</CBadge>
          )}
        </div>
      )}

      {/* Alerts */}
      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}
      {successMsg && <CAlert color="info" dismissible onClose={() => setSuccessMsg(null)}>{successMsg}</CAlert>}

      {/* Spinner */}
      {loading && (
        <div className="text-center py-5">
          <CSpinner color="primary" />
          <div className="mt-2 text-muted">Loading unidentified faces…</div>
        </div>
      )}

      {/* Empty state */}
      {!loading && visibleFaces.length === 0 && faces.length === 0 && (
        <CCard>
          <CCardBody className="text-center py-5 text-muted">
            No unidentified face detections found in this catalog.<br />
            Run <em>Find Faces</em> on photos first, then use this page to identify them.
          </CCardBody>
        </CCard>
      )}

      {!loading && visibleFaces.length === 0 && faces.length > 0 && (
        <CCard>
          <CCardBody className="text-center py-5 text-muted">
            All faces have been confirmed or skipped.
          </CCardBody>
        </CCard>
      )}

      {/* Face cards */}
      {!loading && visibleFaces.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          {visibleFaces.map(face => (
            <FaceCard
              key={face.face_id}
              face={face}
              minSimilarity={minSimilarity}
              catalogId={activeCatalog.id}
              scores={scoresCache[face.face_id]}
              loadingScores={!!loadingScores[face.face_id]}
              onLoadScores={() => loadScores(face.face_id)}
              onConfirm={personId => handleConfirm(face.face_id, personId)}
              onSkip={() => setSkipped(prev => new Set([...prev, face.face_id]))}
            />
          ))}
        </div>
      )}
    </CContainer>
  );
};


// ─── Face card ────────────────────────────────────────────────────────────────

const FaceCard = ({
  face, minSimilarity, catalogId, scores, loadingScores,
  onLoadScores, onConfirm, onSkip
}) => {
  const [expanded, setExpanded] = useState(false);
  const navigate = useNavigate();

  const handleFaceThumbnailClick = () => {
    navigate('/photos', { state: { catalogId, fileId: face.file_id } });
  };

  const handleExpand = () => {
    if (!expanded) onLoadScores();
    setExpanded(e => !e);
  };

  // Group by person_id keeping the best (lowest) distance row per person, then
  // filter by minimum similarity and sort so the closest match is shown first.
  const filteredScores = Object.values(
    (scores || []).reduce((acc, s) => {
      if (!acc[s.person_id] || s.distance < acc[s.person_id].distance) {
        acc[s.person_id] = s;
      }
      return acc;
    }, {})
  )
    .filter(s => meetsSimilarity(s.distance, minSimilarity))
    .sort((a, b) => a.distance - b.distance); // ascending distance == descending similarity

  // Format date
  const dateStr = face.creation_date
    ? face.creation_date.split(' ')[0].replace(/:/g, '-')
    : null;

  return (
    <CCard style={{ borderLeft: face.best_match ? '4px solid #198754' : '4px solid #adb5bd' }}>
      <CCardBody>
        <CRow className="align-items-center g-3">
          {/* Face thumbnail — click opens Tag People on the source photo */}
          <CCol xs="auto">
            <div
              onClick={handleFaceThumbnailClick}
              title="Open in Tag People"
              style={{ cursor: 'pointer' }}
            >
              <FaceThumbnail face={face} size={90} />
            </div>
          </CCol>

          {/* Source info */}
          <CCol>
            <div className="fw-semibold" style={{ fontSize: '0.9rem' }}>{face.file_name}</div>
            {dateStr && (
              <div style={{ fontSize: '0.8rem', color: '#888', marginTop: 2 }}>{dateStr}</div>
            )}
            {face.best_match && (
              <div className="mt-1" style={{ fontSize: '0.82rem' }}>
                <span style={{ color: '#555' }}>Best match: </span>
                <strong>{face.best_match.person_name}</strong>
                <span
                  style={{ color: '#888', marginLeft: 6 }}
                  title={`cosine distance ${face.best_match.distance.toFixed(3)}`}
                >
                  similarity {formatSimilarity(face.best_match.distance)}
                </span>
                {face.best_match.verified
                  ? <CBadge color="success" className="ms-2">verified</CBadge>
                  : <CBadge color="warning" textColor="dark" className="ms-2">unverified</CBadge>}
              </div>
            )}
            {!face.best_match && (
              <div className="mt-1" style={{ fontSize: '0.82rem', color: '#aaa' }}>
                No scores computed yet
              </div>
            )}
          </CCol>

          {/* Action buttons */}
          <CCol xs="auto" className="d-flex gap-2">
            <CButton size="sm" color="secondary" variant="outline" onClick={handleExpand}>
              {expanded ? 'Hide candidates' : 'Show candidates'}
            </CButton>
            <CButton size="sm" color="light" onClick={onSkip}>
              Skip
            </CButton>
          </CCol>
        </CRow>

        {/* Candidates panel */}
        {expanded && (
          <div className="mt-3 border-top pt-3">
            {loadingScores && (
              <div className="text-center py-2">
                <CSpinner size="sm" /> <span className="ms-2 text-muted">Loading scores…</span>
              </div>
            )}
            {!loadingScores && filteredScores.length === 0 && (
              <div className="text-muted" style={{ fontSize: '0.85rem' }}>
                {scores === undefined
                  ? 'Scores not loaded yet.'
                  : `No candidates with similarity ${minSimilarity} or above. ${(scores || []).length} total score(s) exist.`}
              </div>
            )}
            {!loadingScores && filteredScores.length > 0 && (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
                {filteredScores.map(score => (
                  <CandidateCard
                    key={score.score_id}
                    score={score}
                    minSimilarity={minSimilarity}
                    catalogId={catalogId}
                    onConfirm={() => onConfirm(score.person_id)}
                  />
                ))}
              </div>
            )}
          </div>
        )}
      </CCardBody>
    </CCard>
  );
};


// ─── Candidate card ───────────────────────────────────────────────────────────

const CandidateCard = ({ score, minSimilarity, catalogId, onConfirm }) => {
  const [confirming, setConfirming] = useState(false);
  const [done, setDone] = useState(false);

  const handleConfirm = async () => {
    setConfirming(true);
    await onConfirm();
    setDone(true);
    setConfirming(false);
  };

  return (
    <div style={{
      border: '1px solid #dee2e6',
      borderRadius: 8,
      padding: '10px 12px',
      width: 170,
      background: done ? '#d1e7dd' : '#fff',
      opacity: done ? 0.7 : 1,
    }}>
      <div style={{ display: 'flex', gap: 8, alignItems: 'center', marginBottom: 6 }}>
        <ReferenceFaceThumbnail filePath={null} size={48} />
        <div>
          <div className="fw-semibold" style={{ fontSize: '0.82rem', lineHeight: 1.2 }}>
            {score.person_name}
          </div>
          <div
            style={{ fontSize: '0.75rem', color: '#666', marginTop: 2 }}
            title={`cosine distance ${score.distance.toFixed(3)}`}
          >
            {formatSimilarity(score.distance)}
          </div>
        </div>
      </div>
      <SimilarityBar similarity={formatSimilarity(score.distance)} minSimilarity={minSimilarity} />
      {!done && (
        <CButton
          size="sm"
          color="success"
          className="w-100 mt-2"
          onClick={handleConfirm}
          disabled={confirming}
        >
          {confirming ? <CSpinner size="sm" /> : 'Confirm'}
        </CButton>
      )}
      {done && (
        <div className="text-center mt-2" style={{ fontSize: '0.78rem', color: '#198754' }}>
          ✓ Tagged
        </div>
      )}
    </div>
  );
};

export default TagWithAI;
