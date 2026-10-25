import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CContainer,
  CButton,
  CFormInput,
  CFormTextarea,
  CFormLabel,
  CSpinner,
  CAlert,
  CBadge,
  CRow,
  CCol,
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilArrowLeft,
  cilSave,
  cilX,
  cilPlus,
  cilImage,
  cilPencil,
  cilTrash,
  cilSync,
  cilChevronBottom,
  cilChevronRight,
  cilCheck,
  cilFolder,
  cilFolderOpen,
  cilStar,
  cilLevelUp,
} from '@coreui/icons';
import { albumAPI, eventAPI, peopleAPI, locationAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { FILES_BASE_URL, galleryThumbUrl } from '../config';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatDateTime(ts) {
  if (!ts) return '—';
  try {
    const d = new Date(ts);
    if (isNaN(d.getTime())) return ts;
    return d.toLocaleString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch { return ts; }
}

// ─── Sankey SVG ───────────────────────────────────────────────────────────────

/**
 * d3-gallery–style Sankey diagram.
 *
 * Layout
 * ──────
 * Row 1 (top):  All photos ── Filter 1 ── Filter 2 ── … ── Final album
 * Row 2 (below Final album): Removed sink
 *
 * Nodes are tall vertical bars whose height is proportional to their count.
 * All flows connect right-side → left-side of neighbouring bars.
 * Drop ribbons exit the right side of each intermediate bar and curve down
 * to the left side of the Removed sink.
 * Flow volumes are printed on the ribbons.
 * Bar labels appear below each bar.
 */
function SankeyDiagram({ totalCount, filters }) {

  // ── Data ─────────────────────────────────────────────────────────────────
  const pipeline = [{ label: 'All photos', count: totalCount }];
  for (let i = 0; i < filters.length; i++) {
    const label = i === filters.length - 1 ? 'Final album' : `Filter ${i + 1}`;
    pipeline.push({ label, count: filters[i].photo_count ?? 0 });
  }
  const hasFilters = filters.length > 0;
  const n = pipeline.length;

  // ── Layout ────────────────────────────────────────────────────────────────
  const BAR_W    = 14;    // bar width
  const MAX_BAR_H = 160;  // bar height for the largest node (totalCount)
  const COL_GAP  = 130;   // left-edge to left-edge column spacing
  const TOP_PAD  = 20;
  const STEP_Y   = 18;    // vertical drop added per filter step (staircase)
  const LABEL_H  = 36;    // space below bar for label text

  // ── Bar height: exact sum of all ribbons entering/leaving the bar ─────────
  // For each node, bar height = ribbons exiting its right side
  //   = passOut + dropOut  (for source nodes)
  //   = passIn             (for the final destination)
  // We scale so that the node with the most flow occupies MAX_BAR_H.
  // Since source flow = node.count and total = totalCount, we use count-ratio.
  const barH = (count) =>
    totalCount > 0 ? Math.max(6, (count / totalCount) * MAX_BAR_H) : 6;

  // ── Per-column x positions ────────────────────────────────────────────────
  const colX  = (i) => 60 + i * COL_GAP;
  const colCx = (i) => colX(i) + BAR_W / 2;

  // ── Staircase: each bar's top-Y steps down by STEP_Y per index ───────────
  const barTopY = (i) => TOP_PAD + i * STEP_Y;

  // Sink: same x-column as Final album, positioned below it
  const sinkColX      = colX(n - 1);
  const sinkCx        = colCx(n - 1);
  const finalBarBottomY = barTopY(n - 1) + barH(pipeline[n - 1].count);
  const totalDropped  = pipeline.slice(0, -1).reduce(
    (acc, node, i) => acc + Math.max(0, node.count - pipeline[i + 1].count), 0
  );
  const sinkH         = barH(totalDropped);
  const sinkTopY      = finalBarBottomY + 50;

  // Canvas dimensions
  const W = colX(n - 1) + BAR_W + 60;
  const H = sinkTopY + sinkH + LABEL_H + 10;

  // ── Colours ───────────────────────────────────────────────────────────────
  const PASS_COLOR  = '#3b82d4';
  const DROP_COLOR  = '#e57373';
  const FINAL_COLOR = '#2e7d32';
  const SINK_COLOR  = '#c62828';
  const BAR_COLOR   = '#3b82d4';
  const TEXT_COLOR  = '#1f2328';
  const MUTED       = '#57606a';

  // ── Ribbon widths: pass + drop for bar i must sum to barH(src.count) ──────
  // ribbonW scales a flow count against the source bar's total height so the
  // two ribbons together fill the bar exactly.
  const ribbonW = (flowCount, srcCount) =>
    srcCount > 0 ? Math.max(2, (flowCount / srcCount) * barH(srcCount)) : 2;

  // ── Accumulate offsets so ribbons tile flush against each bar side ────────
  // rightOffset[i] = how many px of bar i's right edge are already occupied
  const rightOffsets = pipeline.map(() => 0);
  // leftOffset for the Final album bar and the Removed sink
  let finalLeftOffset = 0;
  let sinkLeftOffset  = 0;

  // ── Pre-compute all ribbons ───────────────────────────────────────────────
  const ribbons = [];
  pipeline.slice(0, -1).forEach((src, i) => {
    const dst       = pipeline[i + 1];
    const passCount = dst.count;
    const dropCount = Math.max(0, src.count - passCount);

    const srcBarTopY = barTopY(i);
    const dstBarTopY = barTopY(i + 1);
    const srcRightX  = colX(i) + BAR_W;
    const dstLeftX   = colX(i + 1);

    // ── Pass ribbon: right of bar i → left of bar i+1 ───────────────────────
    if (passCount > 0) {
      const pw       = ribbonW(passCount, src.count);
      const srcExitY = srcBarTopY + rightOffsets[i];
      rightOffsets[i] += pw;

      let entryY;
      if (i + 1 === n - 1) {
        entryY = dstBarTopY + finalLeftOffset;
        finalLeftOffset += pw;
      } else {
        entryY = dstBarTopY;   // intermediate bars: entry starts at bar top
      }

      const cx = (srcRightX + dstLeftX) / 2;
      ribbons.push({
        type: 'pass',
        d: `M ${srcRightX} ${srcExitY}
            C ${cx} ${srcExitY}, ${cx} ${entryY}, ${dstLeftX} ${entryY}
            L ${dstLeftX} ${entryY + pw}
            C ${cx} ${entryY + pw}, ${cx} ${srcExitY + pw}, ${srcRightX} ${srcExitY + pw}
            Z`,
        color: PASS_COLOR,
        count: passCount,
        labelX: cx,
        labelY: (srcExitY + entryY) / 2 + pw / 2 + 4,
      });
    }

    // ── Drop ribbon: right of bar i → left of Removed sink ──────────────────
    if (dropCount > 0) {
      const dw       = ribbonW(dropCount, src.count);
      const srcExitY = srcBarTopY + rightOffsets[i];
      rightOffsets[i] += dw;

      const entryY   = sinkTopY + sinkLeftOffset;
      sinkLeftOffset += dw;

      const cx1 = srcRightX + (sinkColX - srcRightX) * 0.55;
      const cy1 = srcExitY;
      const cx2 = sinkColX - (sinkColX - srcRightX) * 0.25;
      const cy2 = entryY;

      ribbons.push({
        type: 'drop',
        d: `M ${srcRightX} ${srcExitY}
            C ${cx1} ${cy1}, ${cx2} ${cy2}, ${sinkColX} ${entryY}
            L ${sinkColX} ${entryY + dw}
            C ${cx2} ${cy2 + dw}, ${cx1} ${cy1 + dw}, ${srcRightX} ${srcExitY + dw}
            Z`,
        color: DROP_COLOR,
        count: dropCount,
        labelX: (srcRightX + sinkColX) / 2,
        labelY: (srcExitY + entryY) / 2 + dw / 2 + 4,
      });
    }
  });

  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      style={{ width: '100%', height: H, display: 'block' }}
    >
      {/* ── Ribbons (behind bars) ── */}
      {hasFilters && ribbons.map((r, i) => (
        <g key={i}>
          <path
            d={r.d}
            fill={r.color}
            fillOpacity={0.25}
            stroke={r.color}
            strokeWidth={0.5}
          />
          {/* flow count label on the ribbon */}
          <text
            x={r.labelX} y={r.labelY}
            textAnchor="middle"
            fontSize={10} fill={r.color}
            fontWeight={600}
          >
            {r.count}
          </text>
        </g>
      ))}

      {/* ── Pipeline bars (row 1) ── */}
      {pipeline.map((node, i) => {
        const bh    = barH(node.count);
        const bx    = colX(i);
        const by    = barTopY(i);
        const cx    = colCx(i);
        const isFin = i === n - 1 && hasFilters;
        const color = isFin ? FINAL_COLOR : BAR_COLOR;
        const labelBaseY = by + bh; // label sits below the bottom of this bar
        return (
          <g key={i}>
            <rect x={bx} y={by} width={BAR_W} height={bh} fill={color} rx={2} />
            {/* label below bar */}
            <text
              x={cx} y={labelBaseY + 14}
              textAnchor="middle" fontSize={11} fontWeight={600} fill={TEXT_COLOR}
            >
              {node.label}
            </text>
            <text
              x={cx} y={labelBaseY + 27}
              textAnchor="middle" fontSize={10} fill={MUTED}
            >
              {node.count}
            </text>
          </g>
        );
      })}

      {/* ── Removed sink bar (row 2, below Final album) ── */}
      {hasFilters && totalDropped > 0 && (
        <g>
          <rect
            x={sinkColX} y={sinkTopY}
            width={BAR_W} height={sinkH}
            fill={SINK_COLOR} rx={2}
          />
          <text
            x={sinkCx} y={sinkTopY + sinkH + 14}
            textAnchor="middle" fontSize={11} fontWeight={600} fill={TEXT_COLOR}
          >
            Removed
          </text>
          <text
            x={sinkCx} y={sinkTopY + sinkH + 27}
            textAnchor="middle" fontSize={10} fill={MUTED}
          >
            {totalDropped}
          </text>
        </g>
      )}
    </svg>
  );
}

// ─── Main component ────────────────────────────────────────────────────────────

const ALBUM_MODE_COPY = {
  words: {
    route: 'words',
    title: 'Create album with words',
    subtitle: 'Add natural-language filters to narrow down your photo collection',
    placeholder: 'e.g. "Photos of all events that include Izelpi in 2025"',
    helper: 'Press Enter or click Add to apply',
    loading: 'Translating…',
    button: 'Add filter',
  },
  tools: {
    route: 'tools',
    title: 'Create album with tools',
    subtitle: 'Add natural-language filters to narrow down your photo collection',
    placeholder: 'e.g. "Photos of all events that include Izelpi in 2025"',
    helper: 'Press Enter or click Add to apply',
    loading: 'Translating…',
    button: 'Add filter',
  },
};

function getAlbumRouteByMode(mode, albumId) {
  const route = ALBUM_MODE_COPY[mode]?.route || 'words';
  return `/albums/${route}/${albumId}`;
}

const AlbumEditor = ({ mode = 'words' }) => {
  const { albumId } = useParams();
  const { activeCatalog } = useCatalog();
  const navigate = useNavigate();

  // Section 1 — header
  const [album, setAlbum] = useState(null);
  const [headerName, setHeaderName] = useState('');
  const [headerDesc, setHeaderDesc] = useState('');
  const [headerSaving, setHeaderSaving] = useState(false);
  const [headerSaved, setHeaderSaved] = useState(false);
  const [headerError, setHeaderError] = useState(null);

  // Section 2 — filters
  const [filters, setFilters] = useState([]);
  const [totalCount, setTotalCount] = useState(0);
  const [filterText, setFilterText] = useState('');
  const [addingFilter, setAddingFilter] = useState(false);
  const [filterError, setFilterError] = useState(null);

  // General
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);

  const catalogId = activeCatalog?.id;

  const loadAlbum = useCallback(async () => {
    if (!catalogId) return;
    setLoading(true);
    setLoadError(null);
    try {
      const res = await albumAPI.getById(catalogId, albumId);
      const data = res.data;
      if (data.mode && data.mode !== mode) {
        navigate(getAlbumRouteByMode(data.mode, albumId), { replace: true });
        return;
      }
      setAlbum(data);
      setHeaderName(data.name || '');
      setHeaderDesc(data.description || '');
      setFilters(data.filters || []);
      setTotalCount(data.total_count ?? 0);
    } catch (e) {
      setLoadError('Failed to load album');
    } finally {
      setLoading(false);
    }
  }, [catalogId, albumId, mode, navigate]);

  useEffect(() => { loadAlbum(); }, [loadAlbum]);

  // ── Section 1 save ──────────────────────────────────────────────────────────
  const handleSaveHeader = async () => {
    const name = headerName.trim();
    if (!name) { setHeaderError('Name is required'); return; }
    setHeaderSaving(true);
    setHeaderError(null);
    try {
      await albumAPI.update(catalogId, albumId, { name, description: headerDesc, mode });
      setHeaderSaved(true);
      setTimeout(() => setHeaderSaved(false), 2000);
    } catch (e) {
      setHeaderError(e.response?.data?.error || 'Failed to save');
    } finally {
      setHeaderSaving(false);
    }
  };

  // ── Section 2 add filter ────────────────────────────────────────────────────
  const handleAddFilter = async () => {
    const text = filterText.trim();
    if (!text) return;
    setAddingFilter(true);
    setFilterError(null);
    try {
      const res = await albumAPI.addFilter(catalogId, albumId, text);
      setFilters((prev) => [...prev, res.data]);
      setFilterText('');
      // Update total_count from a fresh load
      const refresh = await albumAPI.getById(catalogId, albumId);
      setTotalCount(refresh.data.total_count ?? totalCount);
    } catch (e) {
      setFilterError(e.response?.data?.error || 'Failed to add filter');
    } finally {
      setAddingFilter(false);
    }
  };

  // ── Section 2 delete filter ─────────────────────────────────────────────────
  const handleDeleteFilter = async (filterId) => {
    try {
      await albumAPI.deleteFilter(catalogId, albumId, filterId);
      // Reload to get updated positions and counts
      const refresh = await albumAPI.getById(catalogId, albumId);
      setFilters(refresh.data.filters || []);
      setTotalCount(refresh.data.total_count ?? totalCount);
    } catch {
      alert('Failed to remove filter');
    }
  };

  // ──────────────────────────────────────────────────────────────────────────
  if (!activeCatalog) {
    return (
      <CContainer fluid className="px-4 py-4">
        <div className="text-center text-muted py-5">Select a catalog from the top menu first.</div>
      </CContainer>
    );
  }

  if (loading) {
    return (
      <CContainer fluid className="px-4 py-4">
        <div className="text-center py-5"><CSpinner color="primary" /></div>
      </CContainer>
    );
  }

  if (loadError) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="danger">{loadError}</CAlert>
      </CContainer>
    );
  }

  const modeLabel = ALBUM_MODE_COPY[album?.mode || mode]?.title || ALBUM_MODE_COPY.words.title;

  return (
    <CContainer fluid className="px-4 py-4">
      {/* Back navigation */}
      <div className="mb-3">
        {/* Back to the album list this album is a card in. */}
        <CButton color="secondary" variant="ghost" size="sm"
                 onClick={() => navigate('/albums')}>
          <CIcon icon={cilArrowLeft} className="me-1" />
          Albums
        </CButton>
      </div>

      {/* ── Section 1: Album header ── */}
      <CCard className="mb-4">
        <CCardHeader>
          <strong>Album Details</strong>
        </CCardHeader>
        <CCardBody>
          {headerError && <CAlert color="danger" className="mb-3">{headerError}</CAlert>}
          <CRow className="mb-3">
            <CCol md={8}>
              <CFormLabel>Name <span className="text-danger">*</span></CFormLabel>
              <CFormInput
                value={headerName}
                onChange={(e) => setHeaderName(e.target.value)}
                placeholder="Album name"
              />
            </CCol>
            <CCol md={4} className="d-flex align-items-end">
              <div className="small text-muted">
                <div style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Created</div>
                {formatDateTime(album?.created_at)}
              </div>
            </CCol>
          </CRow>
          <div className="mb-3">
            <div className="small text-muted mb-1">Album creation mode</div>
            <div>{modeLabel}</div>
          </div>
          <div className="mb-3">
            <CFormLabel>Description</CFormLabel>
            <CFormTextarea
              rows={3}
              value={headerDesc}
              onChange={(e) => setHeaderDesc(e.target.value)}
              placeholder="Optional description"
            />
          </div>
          <div className="d-flex align-items-center gap-2">
            <CButton color="primary" size="sm" onClick={handleSaveHeader} disabled={headerSaving}>
              {headerSaving
                ? <><CSpinner size="sm" className="me-1" />Saving…</>
                : <><CIcon icon={cilSave} className="me-1" />Save</>
              }
            </CButton>
            {headerSaved && <span className="text-success small">Saved ✓</span>}
          </div>
        </CCardBody>
      </CCard>

      {/* ── Section 2: Filter builder ── */}
      <CCard className="mb-4">
        <CCardHeader>
          <strong>{ALBUM_MODE_COPY[mode]?.title || ALBUM_MODE_COPY.words.title}</strong>
          <span className="text-muted ms-2 small">
            {ALBUM_MODE_COPY[mode]?.subtitle || ALBUM_MODE_COPY.words.subtitle}
          </span>
        </CCardHeader>
        <CCardBody>
          {/* Implicit "all photos" chip */}
          <div className="mb-2">
            <FilterChip
              position={0}
              label="All photos in catalog"
              photoCount={totalCount}
              readonly
            />
          </div>

          {/* User filter chips */}
          {filters.map((f) => (
            <div key={f.id} className="mb-2">
              <FilterChip
                position={f.position}
                label={f.nl_text}
                sqlFragment={f.sql_fragment}
                photoCount={f.photo_count}
                onDelete={() => handleDeleteFilter(f.id)}
              />
            </div>
          ))}

          {/* Add filter textarea */}
          {filterError && <CAlert color="danger" className="mb-2">{filterError}</CAlert>}
          <div className="mt-3 d-flex gap-2 align-items-start">
            <div style={{ flex: 1 }}>
              <CFormTextarea
                rows={2}
                value={filterText}
                onChange={(e) => setFilterText(e.target.value)}
                placeholder={ALBUM_MODE_COPY[mode]?.placeholder || ALBUM_MODE_COPY.words.placeholder}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    handleAddFilter();
                  }
                }}
                disabled={addingFilter}
                style={{ resize: 'none' }}
              />
              <div className="small text-muted mt-1">{ALBUM_MODE_COPY[mode]?.helper || ALBUM_MODE_COPY.words.helper}</div>
            </div>
            <CButton
              color="primary" size="sm"
              onClick={handleAddFilter}
              disabled={addingFilter || !filterText.trim()}
              style={{ whiteSpace: 'nowrap', minWidth: 100, marginTop: 2 }}
            >
              {addingFilter
                ? <><CSpinner size="sm" className="me-1" />{ALBUM_MODE_COPY[mode]?.loading || ALBUM_MODE_COPY.words.loading}</>
                : <><CIcon icon={cilPlus} className="me-1" />{ALBUM_MODE_COPY[mode]?.button || ALBUM_MODE_COPY.words.button}</>
              }
            </CButton>
          </div>
        </CCardBody>
      </CCard>

      {/* ── Section 3: Sankey diagram ── */}
      <CCard>
        <CCardHeader>
          <strong>Visual filter pipeline</strong>
          <span className="text-muted ms-2 small">
            How your filters narrow down the collection
          </span>
        </CCardHeader>
        <CCardBody>
          {filters.length === 0 ? (
            <div className="text-center text-muted py-4">
              <CIcon icon={cilImage} style={{ width: 32, height: 32, marginBottom: 8 }} />
              <p className="mb-0 small">Add a filter above to see the pipeline.</p>
              <p className="small" style={{ color: '#aaa' }}>
                Starting with {totalCount} photo{totalCount !== 1 ? 's' : ''} in the catalog.
              </p>
            </div>
          ) : (
            <SankeyDiagram totalCount={totalCount} filters={filters} />
          )}
        </CCardBody>
      </CCard>
    </CContainer>
  );
};

// ─── Filter chip component ─────────────────────────────────────────────────────

function FilterChip({ position, label, sqlFragment, photoCount, readonly, onDelete }) {
  const isAll = position === 0;
  return (
    <div
      style={{
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        border: `1px solid ${isAll ? '#d8dbe0' : '#b8cef0'}`,
        borderRadius: 8,
        padding: '8px 12px',
        backgroundColor: isAll ? '#f7f8fa' : '#f0f5ff',
      }}
    >
      <div style={{ flex: 1 }}>
        <div className="d-flex align-items-center gap-2 mb-1">
          <CBadge
            color={isAll ? 'secondary' : 'primary'}
            style={{ minWidth: 26, textAlign: 'center', fontSize: '0.72rem' }}
          >
            {position}
          </CBadge>
          <span style={{ fontSize: '0.9rem', color: '#1f2328' }}>{label}</span>
        </div>
        {sqlFragment && (
          <div style={{
            paddingLeft: 34,
            marginTop: 4,
            fontFamily: 'monospace',
            fontSize: '0.75rem',
            color: '#57606a',
            background: '#f0f0f0',
            borderRadius: 4,
            padding: '3px 6px 3px 34px',
            marginLeft: 0,
            wordBreak: 'break-all',
          }}>
            {sqlFragment}
          </div>
        )}
        {photoCount != null && (
          <div style={{ paddingLeft: 34, fontSize: '0.8rem', color: '#3b82d4', marginTop: 4 }}>
            → {photoCount} photo{photoCount !== 1 ? 's' : ''}
          </div>
        )}
      </div>
      {!readonly && onDelete && (
        <CButton
          color="danger"
          variant="ghost"
          size="sm"
          onClick={onDelete}
          style={{ padding: '0 4px', marginLeft: 8 }}
          title="Remove filter"
        >
          <CIcon icon={cilX} />
        </CButton>
      )}
    </div>
  );
}

export function AlbumEditorWords() {
  return <AlbumEditor mode="words" />;
}

// ─────────────────────────────────────────────────────────────────────────────
// AlbumEditorTools — filter-driven event browsing + section mosaic
// ─────────────────────────────────────────────────────────────────────────────

export function AlbumEditorTools() {
  const { albumId } = useParams();
  const { activeCatalog } = useCatalog();
  const navigate = useNavigate();
  const location = useLocation();
  const catalogId = activeCatalog?.id;
  // Handed over by whatever created this album - the year template, when the
  // album was made but publishing it afterwards failed.
  const [arrivalNotice, setArrivalNotice] = useState(location.state?.notice || null);

  // ── Header state ──────────────────────────────────────────────────────────
  const [album, setAlbum] = useState(null);
  const [headerName, setHeaderName] = useState('');
  const [headerDesc, setHeaderDesc] = useState('');
  const [headerSaving, setHeaderSaving] = useState(false);
  const [headerSaved, setHeaderSaved] = useState(false);
  const [headerError, setHeaderError] = useState(null);

  // ── Core data ─────────────────────────────────────────────────────────────
  const [sections, setSections] = useState([]);
  const [sectionPhotos, setSectionPhotos] = useState({});

  // ── Categories ────────────────────────────────────────────────────────────
  // The root is NOT in `categories`: null means "at the root", and the root is
  // labelled with the album's name (album-gallery-enhancement-spec.md 3.1).
  // currentCategory is the node new events are filed into.
  const [categories, setCategories] = useState([]);
  const [currentCategory, setCurrentCategory] = useState(null);
  const [expanded, setExpanded] = useState(() => new Set());
  const [categoryBusy, setCategoryBusy] = useState(false);
  const [categoryError, setCategoryError] = useState(null);
  // The section whose photos are open. Cards show a cover only; the mosaic
  // moved behind this (spec 7.4).
  const [detailSectionId, setDetailSectionId] = useState(null);
  const [importNotice, setImportNotice] = useState(null);
  const [events, setEvents] = useState([]);
  const [people, setPeople] = useState([]);
  const [locations, setLocations] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);

  // ── Filter state ──────────────────────────────────────────────────────────
  const [filterYear, setFilterYear] = useState('');
  const [filterMonth, setFilterMonth] = useState('');
  const [filterPeople, setFilterPeople] = useState([]);    // [] = no active people filter
  const [filterLocation, setFilterLocation] = useState(''); // '' = all locations

  // ── Lazy per-event associations ───────────────────────────────────────────
  const [eventPeople, setEventPeople] = useState({});    // { eventId: [person, ...] }
  const [eventLocations, setEventLocations] = useState({}); // { eventId: [location, ...] }

  // ── Import state ──────────────────────────────────────────────────────────
  const [importingEventId, setImportingEventId] = useState(null);
  const [importError, setImportError] = useState(null);

  // ── Undo state ────────────────────────────────────────────────────────────
  const [undoBuffer, setUndoBuffer] = useState([]);

  // ── Derived ───────────────────────────────────────────────────────────────
  const importedEventIds = useMemo(
    () => new Set(sections.filter(s => s.source_event_id != null).map(s => s.source_event_id)),
    [sections]
  );

  // Distinct years from all events (desc)
  const availableYears = useMemo(() => {
    const years = [...new Set(events.map(ev => new Date(ev.start_time).getFullYear()))];
    return years.sort((a, b) => b - a);
  }, [events]);

  // Distinct months in the selected year (desc) as { value, label }
  const availableMonths = useMemo(() => {
    const src = filterYear
      ? events.filter(ev => String(new Date(ev.start_time).getFullYear()) === filterYear)
      : events;
    const monthIdxs = [...new Set(src.map(ev => new Date(ev.start_time).getMonth()))];
    return monthIdxs
      .sort((a, b) => b - a)
      .map(m => ({ value: String(m + 1), label: MONTH_NAMES[m] }));
  }, [events, filterYear]);

  // Filtered events list (client-side)
  const filteredEvents = useMemo(() => {
    return events.filter(ev => {
      const d = new Date(ev.start_time);
      if (filterYear && String(d.getFullYear()) !== filterYear) return false;
      if (filterMonth && String(d.getMonth() + 1) !== filterMonth) return false;
      if (filterLocation) {
        const locs = eventLocations[ev.id] ?? null;
        if (locs === null) return true; // not yet loaded — show tentatively
        if (!locs.some(l => String(l.id) === filterLocation)) return false;
      }
      if (filterPeople.length > 0) {
        const ep = eventPeople[ev.id] ?? null;
        if (ep === null) return true; // not yet loaded — show tentatively
        if (!ep.some(p => filterPeople.includes(p.id))) return false;
      }
      return true;
    });
  }, [events, filterYear, filterMonth, filterPeople, filterLocation, eventPeople, eventLocations]);

  // ── Mount: load album + events + people + locations ────────────────────────
  const loadAll = useCallback(async () => {
    if (!catalogId) return;
    setLoading(true);
    setLoadError(null);
    try {
      const [albumRes, eventsRes, peopleRes, locationsRes] = await Promise.all([
        albumAPI.getById(catalogId, albumId),
        eventAPI.getAll(catalogId),
        peopleAPI.getAll(catalogId),
        locationAPI.getAll(catalogId),
      ]);
      const data = albumRes.data;
      setAlbum(data);
      setHeaderName(data.name || '');
      setHeaderDesc(data.description || '');
      const secs = data.sections || [];
      setSections(secs);
      setCategories(data.categories || []);
      setEvents(eventsRes.data || []);
      setPeople(peopleRes.data || []);
      setLocations(locationsRes.data || []);

      // Pre-load photos for non-empty sections
      const photoMap = {};
      await Promise.all(
        secs.filter(s => s.photo_count > 0).map(async s => {
          const r = await albumAPI.getSectionPhotos(catalogId, albumId, s.id);
          photoMap[s.id] = r.data;
        })
      );
      setSectionPhotos(photoMap);
    } catch {
      setLoadError('Failed to load album data');
    } finally {
      setLoading(false);
    }
  }, [catalogId, albumId]);

  useEffect(() => { loadAll(); }, [loadAll]);

  // ── Lazy-load per-event people when people filter becomes active ───────────
  useEffect(() => {
    if (!filterPeople.length || !catalogId) return;
    const unloaded = filteredEvents.filter(ev => eventPeople[ev.id] === undefined);
    if (!unloaded.length) return;
    // Mark as loading (null) to prevent re-triggering
    setEventPeople(prev => {
      const patch = {};
      unloaded.forEach(ev => { if (prev[ev.id] === undefined) patch[ev.id] = null; });
      return { ...prev, ...patch };
    });
    unloaded.forEach(async ev => {
      try {
        const res = await eventAPI.getPeople(catalogId, ev.id);
        setEventPeople(prev => ({ ...prev, [ev.id]: res.data || [] }));
      } catch {
        setEventPeople(prev => ({ ...prev, [ev.id]: [] }));
      }
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterPeople, filteredEvents, catalogId]);

  // ── Lazy-load per-event locations when location filter becomes active ──────
  useEffect(() => {
    if (!filterLocation || !catalogId) return;
    const unloaded = filteredEvents.filter(ev => eventLocations[ev.id] === undefined);
    if (!unloaded.length) return;
    setEventLocations(prev => {
      const patch = {};
      unloaded.forEach(ev => { if (prev[ev.id] === undefined) patch[ev.id] = null; });
      return { ...prev, ...patch };
    });
    unloaded.forEach(async ev => {
      try {
        const res = await eventAPI.getLocations(catalogId, ev.id);
        setEventLocations(prev => ({ ...prev, [ev.id]: res.data || [] }));
      } catch {
        setEventLocations(prev => ({ ...prev, [ev.id]: [] }));
      }
    });
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [filterLocation, filteredEvents, catalogId]);

  // ── Header save ───────────────────────────────────────────────────────────
  const handleSaveHeader = async () => {
    const name = headerName.trim();
    if (!name) { setHeaderError('Name is required'); return; }
    setHeaderSaving(true);
    setHeaderError(null);
    try {
      await albumAPI.update(catalogId, albumId, { name, description: headerDesc, mode: 'tools' });
      setHeaderSaved(true);
      setTimeout(() => setHeaderSaved(false), 2000);
    } catch (e) {
      setHeaderError(e.response?.data?.error || 'Failed to save');
    } finally {
      setHeaderSaving(false);
    }
  };

  // ── Categories ────────────────────────────────────────────────────────────
  // The root is not a row (spec 3.1): `null` is the root everywhere below, and
  // its label is the album's name, so renaming the album renames the root.

  const categoryById = useMemo(
    () => new Map(categories.map(c => [c.id, c])), [categories]);

  const childrenOf = useMemo(() => {
    const m = new Map();
    categories.forEach(c => {
      const k = c.parent_id ?? null;
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(c);
    });
    return m;
  }, [categories]);

  const rootLabel = headerName.trim() || album?.name || 'Album';

  // Root -> ... -> node. Cycle-safe: a malformed parent chain must not hang the
  // editor, so the walk stops when it revisits a node.
  const categoryPath = useCallback((id) => {
    const out = [];
    const seen = new Set();
    let cur = id;
    while (cur != null && !seen.has(cur)) {
      seen.add(cur);
      const node = categoryById.get(cur);
      if (!node) break;
      out.unshift(node);
      cur = node.parent_id ?? null;
    }
    return out;
  }, [categoryById]);

  const categoryPathLabel = useCallback(
    (id) => [rootLabel, ...categoryPath(id).map(c => c.name)].join(' / '),
    [categoryPath, rootLabel]);

  const refreshCategories = useCallback(async () => {
    try {
      const res = await albumAPI.getCategories(catalogId, albumId);
      setCategories(res.data.categories || []);
    } catch {
      /* counts going stale is not worth an error banner; the next load fixes it */
    }
  }, [catalogId, albumId]);

  // What the grid shows: the sub-categories and the sections of the current node.
  const visibleCategories = useMemo(
    () => childrenOf.get(currentCategory ?? null) || [], [childrenOf, currentCategory]);
  const visibleSections = useMemo(
    () => sections.filter(sec => (sec.category_id ?? null) === (currentCategory ?? null)),
    [sections, currentCategory]);

  const handleAddCategory = async (parentId) => {
    // The sketch requires the label at creation: nothing is created without one,
    // and Cancel creates nothing.
    const name = window.prompt(
      `Name for the new sub-category under ${parentId == null ? rootLabel : categoryPathLabel(parentId)}:`);
    if (name === null) return;
    if (!name.trim()) return;
    setCategoryBusy(true);
    setCategoryError(null);
    try {
      const res = await albumAPI.createCategory(catalogId, albumId, name.trim(), parentId);
      setCategories(prev => [...prev, res.data]);
      setExpanded(prev => new Set(prev).add(parentId));
      setCurrentCategory(res.data.id);
    } catch (e) {
      setCategoryError(e.response?.data?.error || 'Could not create the category');
    } finally {
      setCategoryBusy(false);
    }
  };

  const handleRenameCategory = async (id) => {
    const node = categoryById.get(id);
    if (!node) return;
    const name = window.prompt('Rename category:', node.name);
    if (name === null || !name.trim() || name.trim() === node.name) return;
    setCategoryError(null);
    try {
      const res = await albumAPI.updateCategory(catalogId, albumId, id, { name: name.trim() });
      setCategories(prev => prev.map(c => (c.id === id ? res.data : c)));
    } catch (e) {
      setCategoryError(e.response?.data?.error || 'Could not rename the category');
    }
  };

  const handleDeleteCategory = async (id) => {
    const node = categoryById.get(id);
    if (!node) return;
    setCategoryError(null);
    const finish = async (mode) => {
      await albumAPI.deleteCategory(catalogId, albumId, id, mode);
      if (currentCategory === id) setCurrentCategory(node.parent_id ?? null);
      await refreshCategories();
      // Sections moved or vanished; re-read rather than guess which.
      const secs = await albumAPI.getSections(catalogId, albumId);
      setSections(secs.data || []);
    };
    try {
      await finish(undefined);          // empty categories delete outright
    } catch (e) {
      const d = e.response?.data;
      if (e.response?.status !== 409 || !d) {
        setCategoryError(d?.error || 'Could not delete the category');
        return;
      }
      const inside = [
        d.child_categories ? `${d.child_categories} sub-categor${d.child_categories === 1 ? 'y' : 'ies'}` : null,
        d.descendant_sections ? `${d.descendant_sections} event${d.descendant_sections === 1 ? '' : 's'}` : null,
      ].filter(Boolean).join(' and ');
      // Two questions rather than one, because the two resolutions are not
      // interchangeable: one keeps everything, the other destroys curated
      // photo selections (spec 4.4).
      if (window.confirm(
        `"${node.name}" contains ${inside}.\n\n` +
        `Keep the contents and move them up one level?`)) {
        try { await finish('reparent'); }
        catch (e2) { setCategoryError(e2.response?.data?.error || 'Could not delete the category'); }
        return;
      }
      if (window.confirm(
        `Delete "${node.name}" AND everything inside it?\n\n` +
        `This removes ${inside} from the album, including the hand-picked ` +
        `photo selection of each event. It cannot be undone.`)) {
        try { await finish('cascade'); }
        catch (e2) { setCategoryError(e2.response?.data?.error || 'Could not delete the category'); }
      }
    }
  };

  const handleMoveSection = async (sectionId, categoryId) => {
    setCategoryError(null);
    try {
      await albumAPI.setSectionCategory(catalogId, albumId, sectionId, categoryId);
      setSections(prev => prev.map(sec =>
        sec.id === sectionId ? { ...sec, category_id: categoryId } : sec));
      refreshCategories();
    } catch (e) {
      setCategoryError(e.response?.data?.error || 'Could not move that event');
    }
  };

  const handleSetCover = async (sectionId, fileId) => {
    try {
      const res = await albumAPI.setSectionCover(catalogId, albumId, sectionId, fileId);
      setSections(prev => prev.map(sec => sec.id === sectionId
        ? { ...sec,
            cover_file_id: res.data.cover_file_id,
            resolved_cover_file_id: res.data.cover_file_id }
        : sec));
    } catch (e) {
      alert(e.response?.data?.error || 'Could not set that as the cover');
    }
  };

  // ── Import event ──────────────────────────────────────────────────────────
  const handleImport = async (eventId) => {
    setImportingEventId(eventId);
    setImportError(null);
    setImportNotice(null);
    try {
      const res = await albumAPI.importEventToSection(
        catalogId, albumId, eventId, currentCategory);
      const updated = res.data;
      // A re-import refreshes the section where it already is and does NOT move
      // it here (spec 5.2). Without saying so, adding an event that is filed
      // elsewhere looks like nothing happened.
      if (updated.was_reimport && updated.category_id !== currentCategory) {
        setImportNotice(
          `"${updated.name}" was re-synced in ${categoryPathLabel(updated.category_id)}.`);
      }
      setSections(prev => {
        const idx = prev.findIndex(s => s.id === updated.id);
        return idx >= 0
          ? prev.map(s => s.id === updated.id ? updated : s)
          : [...prev, updated];
      });
      const photosRes = await albumAPI.getSectionPhotos(catalogId, albumId, updated.id);
      setSectionPhotos(prev => ({ ...prev, [updated.id]: photosRes.data }));
      refreshCategories();
    } catch (e) {
      setImportError(e.response?.data?.error || 'Failed to import event');
    } finally {
      setImportingEventId(null);
    }
  };

  // ── Rename section ────────────────────────────────────────────────────────
  const handleRenameSection = async (sectionId, name) => {
    try {
      const res = await albumAPI.updateSection(catalogId, albumId, sectionId, name);
      setSections(prev => prev.map(s => s.id === sectionId ? { ...s, name: res.data.name } : s));
    } catch {
      alert('Failed to rename section');
    }
  };

  // ── Delete section ────────────────────────────────────────────────────────
  const handleDeleteSection = async (sectionId) => {
    try {
      await albumAPI.deleteSection(catalogId, albumId, sectionId);
      setSections(prev => prev.filter(s => s.id !== sectionId));
      setSectionPhotos(prev => { const n = { ...prev }; delete n[sectionId]; return n; });
    } catch (e) {
      alert(e.response?.data?.error || 'Failed to delete section');
    }
  };

  // ── Remove photo ──────────────────────────────────────────────────────────
  const handleRemovePhoto = useCallback((sectionId, sectionName, photo) => {
    setSectionPhotos(prev => ({
      ...prev,
      [sectionId]: (prev[sectionId] || []).filter(p => p.file_id !== photo.file_id),
    }));
    setSections(prev => prev.map(s =>
      s.id === sectionId ? { ...s, photo_count: Math.max(0, (s.photo_count || 1) - 1) } : s
    ));
    const entry = { sectionId, sectionName, fileId: photo.file_id, fileName: photo.file_name, position: photo.position };
    setUndoBuffer(prev => [...prev.slice(-2), entry]);
    albumAPI.removeSectionPhoto(catalogId, albumId, sectionId, photo.file_id).catch(() => {});
  }, [catalogId, albumId]);

  // ── Undo ──────────────────────────────────────────────────────────────────
  const handleUndo = async () => {
    if (!undoBuffer.length) return;
    const entry = undoBuffer[undoBuffer.length - 1];
    setUndoBuffer(prev => prev.slice(0, -1));
    try {
      await albumAPI.addSectionPhoto(catalogId, albumId, entry.sectionId, entry.fileId);
      const res = await albumAPI.getSectionPhotos(catalogId, albumId, entry.sectionId);
      setSectionPhotos(prev => ({ ...prev, [entry.sectionId]: res.data }));
      setSections(prev => prev.map(s =>
        s.id === entry.sectionId ? { ...s, photo_count: res.data.length } : s
      ));
    } catch {}
  };

  const handleUndoAll = async () => {
    const toRestore = [...undoBuffer];
    setUndoBuffer([]);
    for (const entry of toRestore) {
      try {
        await albumAPI.addSectionPhoto(catalogId, albumId, entry.sectionId, entry.fileId);
      } catch {}
    }
    const affectedIds = [...new Set(toRestore.map(e => e.sectionId))];
    const updates = {};
    await Promise.all(affectedIds.map(async sid => {
      const res = await albumAPI.getSectionPhotos(catalogId, albumId, sid);
      updates[sid] = res.data;
    }));
    setSectionPhotos(prev => ({ ...prev, ...updates }));
    setSections(prev => prev.map(s =>
      updates[s.id] ? { ...s, photo_count: updates[s.id].length } : s
    ));
  };

  // ── Guards ────────────────────────────────────────────────────────────────
  if (!activeCatalog) {
    return (
      <CContainer fluid className="px-4 py-4">
        <div className="text-center text-muted py-5">Select a catalog from the top menu first.</div>
      </CContainer>
    );
  }
  if (loading) {
    return (
      <CContainer fluid className="px-4 py-4">
        <div className="text-center py-5"><CSpinner color="primary" /></div>
      </CContainer>
    );
  }
  if (loadError) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="danger">{loadError}</CAlert>
      </CContainer>
    );
  }

  // ── People filter helpers ─────────────────────────────────────────────────
  const allPeopleSelected = people.length > 0 && filterPeople.length === people.length;
  const toggleAllPeople = () => {
    if (allPeopleSelected) {
      setFilterPeople([]);
    } else {
      setFilterPeople(people.map(p => p.id));
    }
  };
  const togglePerson = (personId) => {
    setFilterPeople(prev =>
      prev.includes(personId) ? prev.filter(id => id !== personId) : [...prev, personId]
    );
  };

  return (
    <CContainer fluid className="px-4 py-4">
      {/* Back nav */}
      <div className="mb-3">
        {/* Back to the album list this album is a card in. */}
        <CButton color="secondary" variant="ghost" size="sm"
                 onClick={() => navigate('/albums')}>
          <CIcon icon={cilArrowLeft} className="me-1" />
          Albums
        </CButton>
      </div>

      {arrivalNotice && (
        <CAlert color="warning" dismissible onClose={() => setArrivalNotice(null)}>
          {arrivalNotice}
        </CAlert>
      )}

      {/* ── Album Details card ── */}
      <CCard className="mb-4">
        <CCardHeader><strong>Album Details</strong></CCardHeader>
        <CCardBody>
          {headerError && <CAlert color="danger" className="mb-3">{headerError}</CAlert>}
          <CRow className="mb-3">
            <CCol md={8}>
              <CFormLabel>Name <span className="text-danger">*</span></CFormLabel>
              <CFormInput
                value={headerName}
                onChange={e => setHeaderName(e.target.value)}
                placeholder="Album name"
              />
            </CCol>
            <CCol md={4} className="d-flex align-items-end">
              <div className="small text-muted">
                <div style={{ fontSize: '0.72rem', textTransform: 'uppercase',
                  letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Created</div>
                {album?.created_at
                  ? new Date(album.created_at).toLocaleString('en-US', {
                      year: 'numeric', month: 'short', day: 'numeric',
                      hour: '2-digit', minute: '2-digit',
                    })
                  : '—'}
              </div>
            </CCol>
          </CRow>
          <div className="mb-3">
            <CFormLabel>Description</CFormLabel>
            <CFormTextarea
              rows={3}
              value={headerDesc}
              onChange={e => setHeaderDesc(e.target.value)}
              placeholder="Optional description"
            />
          </div>
          <div className="d-flex align-items-center gap-2">
            <CButton color="primary" size="sm" onClick={handleSaveHeader} disabled={headerSaving}>
              {headerSaving
                ? <><CSpinner size="sm" className="me-1" />Saving…</>
                : <><CIcon icon={cilSave} className="me-1" />Save</>
              }
            </CButton>
            {headerSaved && <span className="text-success small">Saved ✓</span>}
          </div>
        </CCardBody>
      </CCard>

      {/* ── Two-column layout ── */}
      <CRow className="g-3" style={{ alignItems: 'flex-start' }}>

        {/* ── Filters panel (md=2) ── */}
        <CCol md={2} style={{ position: 'sticky', top: 16 }}>

          {/* Date filter */}
          <CCard className="mb-2">
            <CCardHeader className="py-2"><strong style={{ fontSize: '0.8rem' }}>Date</strong></CCardHeader>
            <CCardBody className="py-2 px-2">
              <div className="mb-2">
                <label style={{ fontSize: '0.75rem', color: '#666', marginBottom: 2, display: 'block' }}>Year</label>
                <select
                  className="form-select form-select-sm"
                  value={filterYear}
                  onChange={e => { setFilterYear(e.target.value); setFilterMonth(''); }}
                >
                  <option value="">All years</option>
                  {availableYears.map(y => (
                    <option key={y} value={String(y)}>{y}</option>
                  ))}
                </select>
              </div>
              <div>
                <label style={{ fontSize: '0.75rem', color: '#666', marginBottom: 2, display: 'block' }}>Month</label>
                <select
                  className="form-select form-select-sm"
                  value={filterMonth}
                  onChange={e => setFilterMonth(e.target.value)}
                >
                  <option value="">All months</option>
                  {availableMonths.map(m => (
                    <option key={m.value} value={m.value}>{m.label}</option>
                  ))}
                </select>
              </div>
            </CCardBody>
          </CCard>

          {/* People filter */}
          <CCard className="mb-2">
            <CCardHeader className="py-2"><strong style={{ fontSize: '0.8rem' }}>People</strong></CCardHeader>
            <CCardBody className="py-2 px-2">
              {people.length === 0 ? (
                <div style={{ fontSize: '0.75rem', color: '#888' }}>No people detected in this catalog.</div>
              ) : (
                <>
                  <button
                    className="btn btn-link btn-sm p-0 mb-2"
                    style={{ fontSize: '0.72rem' }}
                    onClick={toggleAllPeople}
                  >
                    {allPeopleSelected ? 'Deselect all' : 'Select all'}
                  </button>
                  <div style={{ maxHeight: 160, overflowY: 'auto' }}>
                    {people.map(person => (
                      <div key={person.id} style={{ display: 'flex', alignItems: 'center', gap: 5, marginBottom: 3 }}>
                        <input
                          type="checkbox"
                          id={`person-${person.id}`}
                          checked={filterPeople.includes(person.id)}
                          onChange={() => togglePerson(person.id)}
                          style={{ cursor: 'pointer' }}
                        />
                        <label
                          htmlFor={`person-${person.id}`}
                          style={{ fontSize: '0.78rem', cursor: 'pointer', overflow: 'hidden',
                            textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1 }}
                          title={person.name}
                        >
                          {person.name}
                        </label>
                      </div>
                    ))}
                  </div>
                </>
              )}
            </CCardBody>
          </CCard>

          {/* Location filter */}
          <CCard className="mb-2">
            <CCardHeader className="py-2"><strong style={{ fontSize: '0.8rem' }}>Location</strong></CCardHeader>
            <CCardBody className="py-2 px-2">
              {locations.length === 0 ? (
                <div style={{ fontSize: '0.75rem', color: '#888' }}>No locations found.</div>
              ) : (
                <select
                  className="form-select form-select-sm"
                  value={filterLocation}
                  onChange={e => setFilterLocation(e.target.value)}
                >
                  <option value="">All locations</option>
                  {locations.map(loc => (
                    <option key={loc.id} value={String(loc.id)}>{loc.name}</option>
                  ))}
                </select>
              )}
            </CCardBody>
          </CCard>

          {/* Events card */}
          <CCard>
            <CCardHeader className="py-2">
              <strong>Events</strong>
            </CCardHeader>
            <CCardBody style={{ overflowY: 'auto', flex: 1, padding: '0.75rem', maxHeight: 'calc(100vh - 520px)', minHeight: 100 }}>
              {importError && (
                <CAlert color="danger" className="py-2 mb-2" style={{ fontSize: '0.8rem' }}>
                  {importError}
                </CAlert>
              )}
              {filteredEvents.length === 0 ? (
                <div className="text-muted small text-center py-3">No events match the current filters.</div>
              ) : (
                filteredEvents.map(ev => {
                  const isImported = importedEventIds.has(ev.id);
                  const isLoading = importingEventId === ev.id;
                  return (
                    <div
                      key={ev.id}
                      style={{ display: 'flex', alignItems: 'center',
                        gap: 6, padding: '3px 0', fontSize: '0.8rem' }}
                    >
                      {isImported && (
                        <CIcon icon={cilCheck}
                          style={{ width: 12, height: 12, color: '#28a745', flexShrink: 0 }} />
                      )}
                      <span
                        style={{ flex: 1, overflow: 'hidden', textOverflow: 'ellipsis',
                          whiteSpace: 'nowrap', color: isImported ? '#333' : '#555' }}
                        title={ev.name}
                      >
                        {ev.name}
                      </span>
                      <CButton
                        size="sm"
                        color={isImported ? 'secondary' : 'primary'}
                        variant="ghost"
                        title={isImported ? 'Re-sync event' : 'Add event to album'}
                        onClick={() => handleImport(ev.id)}
                        disabled={isLoading}
                        style={{ padding: '1px 4px', flexShrink: 0 }}
                      >
                        {isLoading
                          ? <CSpinner size="sm" style={{ width: 12, height: 12 }} />
                          : <CIcon icon={isImported ? cilSync : cilPlus}
                              style={{ width: 12, height: 12 }} />
                        }
                      </CButton>
                    </div>
                  );
                })
              )}
            </CCardBody>
          </CCard>
        </CCol>

        {/* ── Category tree (md=2) ── */}
        <CCol md={2}>
          <CCard className="mb-2">
            <CCardHeader className="py-2 d-flex align-items-center justify-content-between">
              <strong style={{ fontSize: '0.8rem' }}>Categories</strong>
              <CButton
                size="sm" color="primary" variant="ghost" style={{ padding: '0 4px' }}
                title="Add a sub-category at the top level"
                disabled={categoryBusy}
                onClick={() => handleAddCategory(null)}
              >
                <CIcon icon={cilPlus} style={{ width: 13, height: 13 }} />
              </CButton>
            </CCardHeader>
            <CCardBody className="py-2 px-2"
              style={{ overflowY: 'auto', maxHeight: 'calc(100vh - 260px)' }}>
              {categoryError && (
                <CAlert color="danger" className="py-1 px-2 mb-2" style={{ fontSize: '0.75rem' }}>
                  {categoryError}
                </CAlert>
              )}
              <CategoryNode
                id={null}
                label={rootLabel}
                depth={0}
                childrenOf={childrenOf}
                expanded={expanded}
                setExpanded={setExpanded}
                current={currentCategory}
                onSelect={setCurrentCategory}
                onAdd={handleAddCategory}
                onRename={handleRenameCategory}
                onDelete={handleDeleteCategory}
                onDropSection={handleMoveSection}
                rootSectionCount={sections.filter(x => (x.category_id ?? null) === null).length}
              />
            </CCardBody>
          </CCard>
        </CCol>

        {/* ── Card grid (md=8) ── */}
        <CCol md={8}>
          {undoBuffer.length > 0 && (
            <UndoBar
              buffer={undoBuffer}
              onUndo={handleUndo}
              onUndoAll={handleUndoAll}
              onDismiss={() => setUndoBuffer([])}
            />
          )}

          {importNotice && (
            <CAlert color="info" className="py-2" dismissible
              onClose={() => setImportNotice(null)} style={{ fontSize: '0.82rem' }}>
              {importNotice}
            </CAlert>
          )}

          {/* Breadcrumb: a deep selection has to be legible without reading the
              tree (spec 7.2). */}
          <div className="mb-2" style={{ fontSize: '0.85rem', color: '#555' }}>
            <button type="button" onClick={() => setCurrentCategory(null)}
              style={{ background: 'none', border: 'none', padding: 0,
                color: currentCategory == null ? '#333' : '#0d6efd',
                fontWeight: currentCategory == null ? 600 : 400, cursor: 'pointer' }}>
              {rootLabel}
            </button>
            {categoryPath(currentCategory).map((c, i, arr) => (
              <span key={c.id}>
                <span style={{ color: '#aaa', margin: '0 6px' }}>/</span>
                <button type="button" onClick={() => setCurrentCategory(c.id)}
                  style={{ background: 'none', border: 'none', padding: 0,
                    color: i === arr.length - 1 ? '#333' : '#0d6efd',
                    fontWeight: i === arr.length - 1 ? 600 : 400, cursor: 'pointer' }}>
                  {c.name}
                </button>
              </span>
            ))}
          </div>

          {visibleCategories.length === 0 && visibleSections.length === 0 ? (
            <div style={{ padding: 32, textAlign: 'center', color: '#aaa',
              border: '2px dashed #dee2e6', borderRadius: 8 }}>
              <CIcon icon={cilImage} style={{ width: 40, height: 40, marginBottom: 8 }} />
              {sections.length === 0 && categories.length === 0 ? (
                <>
                  <p className="mb-0">No events yet.</p>
                  <p className="small mb-0">
                    Click <strong>+</strong> on an event in the left panel to add it here,
                    or create a category first.
                  </p>
                </>
              ) : (
                <>
                  <p className="mb-0">Nothing in this category yet.</p>
                  <p className="small mb-0">
                    Click <strong>+</strong> on an event in the left panel to add it here.
                  </p>
                </>
              )}
            </div>
          ) : (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12 }}>
              {visibleCategories.map(cat => (
                <CategoryCard
                  key={`c${cat.id}`}
                  category={cat}
                  onOpen={() => setCurrentCategory(cat.id)}
                  onDropSection={handleMoveSection}
                />
              ))}
              {visibleSections.map(section => (
                <EventCard
                  key={`s${section.id}`}
                  section={section}
                  onOpen={() => setDetailSectionId(section.id)}
                  onDelete={handleDeleteSection}
                  moveTargets={[{ id: null, label: rootLabel }, ...categories
                    .filter(c => c.id !== (section.category_id ?? null))
                    .map(c => ({ id: c.id, label: categoryPathLabel(c.id) }))]}
                  onMove={(categoryId) => handleMoveSection(section.id, categoryId)}
                />
              ))}
            </div>
          )}
        </CCol>
      </CRow>

      {/* The photos, one click behind the card (spec 7.4). SectionBlock is
          reused verbatim rather than rewritten, so rename, delete, per-photo
          removal and the undo buffer all keep working. */}
      <CModal size="xl" visible={detailSectionId != null}
        onClose={() => setDetailSectionId(null)} alignment="center">
        {(() => {
          const sec = sections.find(x => x.id === detailSectionId);
          if (!sec) return null;
          return (
            <>
              <CModalHeader>
                <CModalTitle style={{ fontSize: '1rem' }}>
                  {sec.name}
                  <span style={{ color: '#888', fontWeight: 400, marginLeft: 8,
                    fontSize: '0.85rem' }}>
                    in {categoryPathLabel(sec.category_id ?? null)}
                  </span>
                </CModalTitle>
              </CModalHeader>
              <CModalBody>
                <SectionBlock
                  section={sec}
                  photos={sectionPhotos[sec.id] || []}
                  onRename={handleRenameSection}
                  onDelete={(id) => { setDetailSectionId(null); handleDeleteSection(id); }}
                  onRemovePhoto={(photo) => handleRemovePhoto(sec.id, sec.name, photo)}
                  onSetCover={(fileId) => handleSetCover(sec.id, fileId)}
                  coverFileId={sec.resolved_cover_file_id}
                />
              </CModalBody>
            </>
          );
        })()}
      </CModal>
    </CContainer>
  );
}

const IMAGE_BASE = FILES_BASE_URL;

function photoUrl(photo) {
  const p = photo.alternative_file_path || photo.file_path;
  return p ? `${IMAGE_BASE}${encodeURIComponent(p)}` : null;
}

const MONTH_NAMES = [
  'January','February','March','April','May','June',
  'July','August','September','October','November','December',
];

// ── PhotoTile ──────────────────────────────────────────────────────────────

function PhotoTile({ photo, onRemove, onSetCover, isCover }) {
  const [hovered, setHovered] = useState(false);
  const src = photoUrl(photo);
  return (
    <div
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => setHovered(false)}
      style={{ position: 'relative', width: 'calc(25% - 5px)', aspectRatio: '1',
        overflow: 'hidden', borderRadius: 4, backgroundColor: '#e9ecef', flexShrink: 0 }}
    >
      {src ? (
        <img
          src={src}
          alt={photo.file_name}
          loading="lazy"
          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
        />
      ) : (
        <div style={{ width: '100%', height: '100%', display: 'flex', alignItems: 'center',
          justifyContent: 'center' }}>
          <CIcon icon={cilImage} style={{ width: 24, height: 24, color: '#aaa' }} />
        </div>
      )}
      {/* The current cover stays marked even when not hovered, so the choice is
          visible without hunting for it. */}
      {isCover && (
        <div
          title="This is the section's cover"
          style={{ position: 'absolute', bottom: 4, left: 4,
            background: 'rgba(0,0,0,0.6)', borderRadius: '50%',
            width: 20, height: 20, display: 'flex', alignItems: 'center',
            justifyContent: 'center' }}
        >
          <CIcon icon={cilStar} style={{ width: 11, height: 11, color: '#ffc107' }} />
        </div>
      )}

      {/* Remove / set-cover overlay */}
      {(hovered || 'ontouchstart' in window) && (
        <div style={{ position: 'absolute', top: 4, right: 4, display: 'flex', gap: 4 }}>
          {onSetCover && !isCover && (
            <button
              onClick={() => onSetCover(photo.file_id)}
              title="Use as the section cover"
              style={{ background: 'rgba(0,0,0,0.55)', border: 'none', borderRadius: '50%',
                width: 22, height: 22, display: 'flex', alignItems: 'center',
                justifyContent: 'center', cursor: 'pointer', padding: 0 }}
            >
              <CIcon icon={cilStar} style={{ width: 12, height: 12, color: '#fff' }} />
            </button>
          )}
          <button
            onClick={() => onRemove(photo)}
            title={`Remove ${photo.file_name}`}
            style={{ background: 'rgba(0,0,0,0.55)', border: 'none', borderRadius: '50%',
              width: 22, height: 22, display: 'flex', alignItems: 'center',
              justifyContent: 'center', cursor: 'pointer', padding: 0 }}
          >
            <CIcon icon={cilX} style={{ width: 12, height: 12, color: '#fff' }} />
          </button>
        </div>
      )}
    </div>
  );
}

// ── CategoryNode ───────────────────────────────────────────────────────────
// One node of the tree, recursive. `id === null` is the root, which is the
// album itself: it is always present, cannot be deleted, and is renamed by the
// header field rather than here (spec 7.2).

const DRAG_TYPE = 'application/x-airlock-section';

function CategoryNode({
  id, label, depth, childrenOf, expanded, setExpanded, current, onSelect,
  onAdd, onRename, onDelete, onDropSection, node, rootSectionCount,
}) {
  const [dropping, setDropping] = useState(false);
  const kids = childrenOf.get(id ?? null) || [];
  const isRoot = id === null;
  const isOpen = isRoot || expanded.has(id);
  const isCurrent = (current ?? null) === (id ?? null);
  const count = isRoot ? rootSectionCount : (node?.descendant_section_count ?? 0);

  const toggle = () => setExpanded(prev => {
    const next = new Set(prev);
    if (next.has(id)) next.delete(id); else next.add(id);
    return next;
  });

  return (
    <div>
      <div
        onClick={() => onSelect(id)}
        onDragOver={e => { if (e.dataTransfer.types.includes(DRAG_TYPE)) { e.preventDefault(); setDropping(true); } }}
        onDragLeave={() => setDropping(false)}
        onDrop={e => {
          e.preventDefault();
          setDropping(false);
          const sectionId = Number(e.dataTransfer.getData(DRAG_TYPE));
          if (sectionId) onDropSection(sectionId, id);
        }}
        style={{ display: 'flex', alignItems: 'center', gap: 4, cursor: 'pointer',
          padding: '2px 4px', borderRadius: 4, marginLeft: depth * 10,
          backgroundColor: dropping ? '#cfe2ff' : (isCurrent ? '#e7f1ff' : 'transparent'),
          border: dropping ? '1px dashed #0d6efd' : '1px solid transparent' }}
      >
        {kids.length > 0 && !isRoot ? (
          <span onClick={e => { e.stopPropagation(); toggle(); }} style={{ lineHeight: 0 }}>
            <CIcon icon={isOpen ? cilChevronBottom : cilChevronRight}
              style={{ width: 10, height: 10, color: '#666' }} />
          </span>
        ) : (
          <span style={{ width: 10, display: 'inline-block' }} />
        )}
        <CIcon icon={isOpen && kids.length ? cilFolderOpen : cilFolder}
          style={{ width: 12, height: 12, color: isCurrent ? '#0d6efd' : '#888', flexShrink: 0 }} />
        <span
          title={label}
          style={{ flex: 1, fontSize: '0.78rem', overflow: 'hidden',
            textOverflow: 'ellipsis', whiteSpace: 'nowrap',
            fontWeight: isCurrent ? 600 : 400 }}
        >
          {label}
        </span>
        {count > 0 && (
          <CBadge color="secondary" style={{ fontSize: '0.62rem' }}>{count}</CBadge>
        )}
        <span onClick={e => e.stopPropagation()} style={{ display: 'flex' }}>
          <CButton size="sm" color="secondary" variant="ghost" style={{ padding: '0 2px' }}
            title="Add a sub-category here" onClick={() => onAdd(id)}>
            <CIcon icon={cilPlus} style={{ width: 10, height: 10 }} />
          </CButton>
          {!isRoot && (
            <>
              <CButton size="sm" color="secondary" variant="ghost" style={{ padding: '0 2px' }}
                title="Rename category" onClick={() => onRename(id)}>
                <CIcon icon={cilPencil} style={{ width: 10, height: 10 }} />
              </CButton>
              <CButton size="sm" color="danger" variant="ghost" style={{ padding: '0 2px' }}
                title="Delete category" onClick={() => onDelete(id)}>
                <CIcon icon={cilTrash} style={{ width: 10, height: 10 }} />
              </CButton>
            </>
          )}
        </span>
      </div>

      {isOpen && kids.map(kid => (
        <CategoryNode
          key={kid.id}
          id={kid.id}
          label={kid.name}
          node={kid}
          depth={depth + 1}
          childrenOf={childrenOf}
          expanded={expanded}
          setExpanded={setExpanded}
          current={current}
          onSelect={onSelect}
          onAdd={onAdd}
          onRename={onRename}
          onDelete={onDelete}
          onDropSection={onDropSection}
          rootSectionCount={rootSectionCount}
        />
      ))}
    </div>
  );
}

// ── CategoryCard ───────────────────────────────────────────────────────────
// A sub-category shown inside the grid, so the tree is not the only way down.

function CategoryCard({ category, onOpen, onDropSection }) {
  const [dropping, setDropping] = useState(false);
  return (
    <div
      onClick={onOpen}
      onDragOver={e => { if (e.dataTransfer.types.includes(DRAG_TYPE)) { e.preventDefault(); setDropping(true); } }}
      onDragLeave={() => setDropping(false)}
      onDrop={e => {
        e.preventDefault();
        setDropping(false);
        const sectionId = Number(e.dataTransfer.getData(DRAG_TYPE));
        if (sectionId) onDropSection(sectionId, category.id);
      }}
      style={{ width: 190, cursor: 'pointer', borderRadius: 8, overflow: 'hidden',
        border: dropping ? '2px dashed #0d6efd' : '1px solid #e5e7eb',
        backgroundColor: dropping ? '#eef5ff' : '#fbfbfc' }}
    >
      <div style={{ height: 110, display: 'flex', alignItems: 'center',
        justifyContent: 'center', backgroundColor: '#f1f3f5' }}>
        <CIcon icon={cilFolder} style={{ width: 34, height: 34, color: '#adb5bd' }} />
      </div>
      <div style={{ padding: '6px 8px' }}>
        <div style={{ fontSize: '0.82rem', fontWeight: 600, overflow: 'hidden',
          textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={category.name}>
          {category.name}
        </div>
        <div style={{ fontSize: '0.7rem', color: '#888' }}>
          {category.descendant_section_count} event
          {category.descendant_section_count === 1 ? '' : 's'}
          {category.photo_count > 0 && ` · ${category.photo_count} photos`}
        </div>
      </div>
    </div>
  );
}

// ── EventCard ──────────────────────────────────────────────────────────────
// Cover and name only - the sketch's requirement. The photos are one click
// away, in the detail modal (spec 7.4).

function EventCard({ section, onOpen, onDelete, moveTargets, onMove }) {
  const { catalogId } = useCatalog();
  const [hovered, setHovered] = useState(false);
  const [moving, setMoving] = useState(false);
  const coverUrl = section.resolved_cover_file_id && catalogId
    ? galleryThumbUrl(catalogId, section.resolved_cover_file_id, 320)
    : null;

  return (
    <div
      draggable
      onDragStart={e => {
        e.dataTransfer.setData(DRAG_TYPE, String(section.id));
        e.dataTransfer.effectAllowed = 'move';
      }}
      onMouseEnter={() => setHovered(true)}
      onMouseLeave={() => { setHovered(false); setMoving(false); }}
      style={{ width: 190, borderRadius: 8, overflow: 'hidden',
        border: '1px solid #e5e7eb', backgroundColor: '#fff', position: 'relative' }}
    >
      <div onClick={onOpen} style={{ height: 110, cursor: 'pointer',
        backgroundColor: '#e9ecef', display: 'flex', alignItems: 'center',
        justifyContent: 'center', overflow: 'hidden' }}>
        {coverUrl ? (
          <img src={coverUrl} alt={section.name} loading="lazy"
            style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
        ) : (
          <CIcon icon={cilImage} style={{ width: 28, height: 28, color: '#adb5bd' }} />
        )}
      </div>

      {hovered && (
        <div style={{ position: 'absolute', top: 4, right: 4, display: 'flex', gap: 4 }}>
          {/* The accessible equivalent of dragging onto the tree - required,
              not an extra (spec 7.5). */}
          <button
            onClick={() => setMoving(m => !m)}
            title="Move to another category"
            style={{ background: 'rgba(0,0,0,0.55)', border: 'none', borderRadius: 4,
              width: 22, height: 22, display: 'flex', alignItems: 'center',
              justifyContent: 'center', cursor: 'pointer', padding: 0 }}
          >
            <CIcon icon={cilLevelUp} style={{ width: 12, height: 12, color: '#fff' }} />
          </button>
          <button
            onClick={() => {
              if (window.confirm(
                `Remove "${section.name}" from the album? Its photo selection is lost.`))
                onDelete(section.id);
            }}
            title="Remove this event from the album"
            style={{ background: 'rgba(0,0,0,0.55)', border: 'none', borderRadius: 4,
              width: 22, height: 22, display: 'flex', alignItems: 'center',
              justifyContent: 'center', cursor: 'pointer', padding: 0 }}
          >
            <CIcon icon={cilX} style={{ width: 12, height: 12, color: '#fff' }} />
          </button>
        </div>
      )}

      {moving && (
        <div style={{ position: 'absolute', top: 30, right: 4, left: 4, zIndex: 5 }}>
          <select
            className="form-select form-select-sm"
            autoFocus
            defaultValue=""
            onChange={e => {
              const v = e.target.value;
              setMoving(false);
              onMove(v === 'root' ? null : Number(v));
            }}
            style={{ fontSize: '0.72rem' }}
          >
            <option value="" disabled>Move to…</option>
            {moveTargets.map(t => (
              <option key={t.id ?? 'root'} value={t.id ?? 'root'}>{t.label}</option>
            ))}
          </select>
        </div>
      )}

      <div onClick={onOpen} style={{ padding: '6px 8px', cursor: 'pointer' }}>
        <div style={{ fontSize: '0.82rem', fontWeight: 600, overflow: 'hidden',
          textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={section.name}>
          {section.name}
        </div>
        <div style={{ fontSize: '0.7rem', color: '#888' }}>
          {section.photo_count} photo{section.photo_count === 1 ? '' : 's'}
        </div>
      </div>
    </div>
  );
}

// ── SectionBlock ───────────────────────────────────────────────────────────

function SectionBlock({ section, photos, onRename, onDelete, onRemovePhoto,
                       onSetCover, coverFileId }) {
  const [open, setOpen] = useState(true);
  const [editing, setEditing] = useState(false);
  const [nameInput, setNameInput] = useState(section.name);
  const [saving, setSaving] = useState(false);

  const commitRename = async () => {
    const trimmed = nameInput.trim();
    if (!trimmed || trimmed === section.name) { setEditing(false); return; }
    setSaving(true);
    await onRename(section.id, trimmed);
    setSaving(false);
    setEditing(false);
  };

  return (
    <div className="mb-3">
      {/* Section header */}
      <div
        style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '6px 8px',
          backgroundColor: '#f7f8fa', borderRadius: 6, border: '1px solid #e5e7eb',
          marginBottom: 6 }}
      >
        <CButton
          size="sm" color="secondary" variant="ghost"
          style={{ padding: '0 4px' }}
          onClick={() => setOpen(o => !o)}
        >
          <CIcon icon={open ? cilChevronBottom : cilChevronRight}
            style={{ width: 13, height: 13 }} />
        </CButton>

        {editing ? (
          <CFormInput
            size="sm"
            value={nameInput}
            autoFocus
            onChange={e => setNameInput(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') commitRename(); if (e.key === 'Escape') setEditing(false); }}
            onBlur={commitRename}
            style={{ flex: 1, maxWidth: 220 }}
            disabled={saving}
          />
        ) : (
          <span style={{ flex: 1, fontWeight: 600, fontSize: '0.875rem' }}>
            {section.name}
          </span>
        )}

        <CBadge color="secondary" style={{ fontSize: '0.7rem' }}>
          {photos.length} photo{photos.length !== 1 ? 's' : ''}
        </CBadge>

        <CButton size="sm" color="secondary" variant="ghost" style={{ padding: '0 4px' }}
          title="Rename section" onClick={() => { setNameInput(section.name); setEditing(true); }}>
          <CIcon icon={cilPencil} style={{ width: 13, height: 13 }} />
        </CButton>

        <CButton
          size="sm" color="danger" variant="ghost" style={{ padding: '0 4px' }}
          title="Delete section"
          onClick={() => {
            if (window.confirm(`Delete section "${section.name}" and remove all its photos from the album?`))
              onDelete(section.id);
          }}
        >
          <CIcon icon={cilTrash} style={{ width: 13, height: 13 }} />
        </CButton>
      </div>

      {/* Photo grid */}
      {open && (
        photos.length === 0 ? (
          <div style={{ padding: '12px 8px', color: '#aaa', fontSize: '0.8rem',
            border: '1px dashed #dee2e6', borderRadius: 6, textAlign: 'center' }}>
            No photos — import an event from the left panel
          </div>
        ) : (
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 6 }}>
            {photos.map(photo => (
              <PhotoTile
                key={photo.file_id}
                photo={photo}
                onRemove={onRemovePhoto}
                onSetCover={onSetCover}
                isCover={coverFileId === photo.file_id}
              />
            ))}
          </div>
        )
      )}
    </div>
  );
}

// ── UndoBar ────────────────────────────────────────────────────────────────

function UndoBar({ buffer, onUndo, onUndoAll, onDismiss }) {
  const timerRef = useRef(null);

  const resetTimer = useCallback(() => {
    clearTimeout(timerRef.current);
    timerRef.current = setTimeout(onDismiss, 8000);
  }, [onDismiss]);

  useEffect(() => {
    resetTimer();
    return () => clearTimeout(timerRef.current);
  }, [buffer, resetTimer]);

  if (!buffer.length) return null;
  const latest = buffer[buffer.length - 1];

  return (
    <div style={{ marginBottom: 12, padding: '8px 12px', backgroundColor: '#fff3cd',
      border: '1px solid #ffc107', borderRadius: 6, display: 'flex',
      alignItems: 'center', gap: 10, fontSize: '0.82rem' }}>
      <span style={{ flex: 1 }}>
        ↩ <strong>"{latest.fileName}"</strong> removed from <em>{latest.sectionName}</em>.
      </span>
      <CButton size="sm" color="warning" variant="outline" style={{ padding: '2px 8px' }}
        onClick={() => { onUndo(); resetTimer(); }}>
        Undo
      </CButton>
      {buffer.length > 1 && (
        <CButton size="sm" color="secondary" variant="outline" style={{ padding: '2px 8px' }}
          onClick={() => { onUndoAll(); }}>
          Undo all ({buffer.length})
        </CButton>
      )}
      <CButton size="sm" color="secondary" variant="ghost" style={{ padding: '0 4px' }}
        onClick={onDismiss}>
        <CIcon icon={cilX} style={{ width: 12, height: 12 }} />
      </CButton>
    </div>
  );
}

export default AlbumEditor;
