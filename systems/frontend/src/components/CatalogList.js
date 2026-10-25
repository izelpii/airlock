import React, { useState, useEffect } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CButton,
  CSpinner,
  CRow,
  CCol,
  CFormLabel,
  CFormInput,
  CFormTextarea,
  CFormSelect,
  CBadge,
  CAlert,
  CTabContent,
  CTabPane,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPencil, cilTrash, cilPlus, cilX, cilSave, cilCheck, cilShare } from '@coreui/icons';
import { catalogAPI, storageRouteAPI } from '../services/api';
import StorageRouteCards from './StorageRouteCards';
import BackupCards from './BackupCards';
import { useCatalog } from '../context/CatalogContext';
import CatalogSharePanel from './CatalogSharePanel';
import CardBand, { CARD_TOP_CORNERS } from './CardBand';

// Catalog card decorators live in public/theme2/: cardDeco1..20.png, each with a
// cardDeco<n>bw.png twin. The backend assigns one per catalog and stores it as
// "<theme>/cardDeco<n>"; a catalog served before it had one falls back to a
// pick derived from its id, so the card never loses its strip.
const DECORATOR_FALLBACK_THEME = 'theme2';
const DECORATOR_FALLBACK_COUNT = 20;
export const catalogDecorator = (catalog) => (
  catalog?.decorator
  || `${DECORATOR_FALLBACK_THEME}/cardDeco${((Number(catalog?.id) || 1) - 1) % DECORATOR_FALLBACK_COUNT + 1}`
);

// Routes that do not require a catalog — navigating to them when closing is fine
const CATALOG_FREE_ROUTES = ['/catalogs', '/admin', '/settings'];
const isCatalogDependent = (pathname) =>
  !CATALOG_FREE_ROUTES.some((r) => pathname === r || pathname.startsWith(r + '/'));

const CatalogList = ({ onEdit, onCreate }) => {
  const { activeCatalog, setActiveCatalog, clearActiveCatalog } = useCatalog();
  const navigate = useNavigate();
  const location = useLocation();
  const [catalogs, setCatalogs] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [flippedCatalogId, setFlippedCatalogId] = useState(null);
  // { catalogId: [route, …] } — read-only, for the front of each card. Built
  // from ONE /storage-routes call rather than one per catalog, grouping by the
  // catalog_id each route now carries.
  const [routesByCatalog, setRoutesByCatalog] = useState({});
  // What the back of the flipped card shows: 'edit' or 'share'.
  // One card is flipped at a time, so a single mode is enough.
  const [flipMode, setFlipMode] = useState('edit');
  const [editFormData, setEditFormData] = useState({});
  const [saveError, setSaveError] = useState(null);
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    loadCatalogs();
  }, []);

  const loadCatalogs = async () => {
    try {
      setLoading(true);
      const response = await catalogAPI.getAll();
      setCatalogs(response.data);
      setError(null);

      // Never fatal: the card simply omits the line if this fails. Storage
      // routes are an admin concern and a viewer may not be able to list them,
      // which is not a reason to break the Catalogs page.
      try {
        const routes = await storageRouteAPI.getAll();
        const map = {};
        // One catalog per route now: a route is owned, not linked to any number
        // of catalogs, so this is a group-by rather than a fan-out.
        for (const route of routes.data?.routes || []) {
          if (route.catalog_id == null) continue;
          (map[route.catalog_id] = map[route.catalog_id] || []).push(route);
        }
        setRoutesByCatalog(map);
      } catch {
        setRoutesByCatalog({});
      }
    } catch (err) {
      setError('Failed to load catalogs');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const handleDelete = async (id) => {
    if (window.confirm(
      'Remove this catalog from the server?\n\n' +
      'Its folder and files are left untouched — add the same folder again later '
      + 'and everything comes back. Access grants are not restored.')) {
      try {
        await catalogAPI.delete(id);
        // Drop the selection if it was this one. Without this the app keeps
        // pointing at a catalog that no longer exists: every catalog-scoped
        // call answers 403, and each page shows its own "failed to load" for
        // the rest of the session.
        if (activeCatalog?.id === id) clearActiveCatalog();
        setFlippedCatalogId(null);
        setEditFormData({});
        loadCatalogs();
      } catch (err) {
        alert('Failed to delete catalog');
        console.error(err);
      }
    }
  };

  const openEditCard = (catalog) => {
    setEditFormData({
      name: catalog.name || '',
      description: catalog.description || '',
      target_directory: catalog.target_directory || '',
      file_strategy: catalog.file_strategy || 'local',
      similarity_metric: catalog.similarity_metric || 'cosine',
    });
    setSaveError(null);
    setFlipMode('edit');
    setFlippedCatalogId(catalog.id);
  };

  // Same flip, different back. Sharing is not part of the catalog's
  // own fields, so it gets its own face rather than another form section.
  const openShareCard = (catalog) => {
    setSaveError(null);
    setFlipMode('share');
    setFlippedCatalogId(catalog.id);
  };

  const closeEditCard = () => {
    setFlippedCatalogId(null);
    setEditFormData({});
    setSaveError(null);
    setFlipMode('edit');
  };

  const handleEditChange = (e) => {
    const { name, value } = e.target;
    setEditFormData((prev) => ({
      ...prev,
      [name]: value,
    }));
  };

  const handleSaveEdit = async (catalogId) => {
    setSaving(true);
    setSaveError(null);
    try {
      await catalogAPI.update(catalogId, editFormData);
      // Flip the card back first
      closeEditCard();
      // Then reload the catalogs to show updated data
      await loadCatalogs();
    } catch (err) {
      setSaveError(err.response?.data?.error || 'Failed to update catalog');
    } finally {
      setSaving(false);
    }
  };

  const getStrategyBadge = (strategy) => {
    const badges = {
      local: { color: 'secondary', label: 'Local' },
      scp: { color: 'primary', label: 'SCP' },
      ftp: { color: 'info', label: 'FTP' },
      box: { color: 'warning', label: 'Box' },
      s3: { color: 'success', label: 'S3' },
    };
    const badge = badges[strategy] || badges.local;
    return badge;
  };

  // The open catalog leads its own row; everything else shares the one below.
  // Split here rather than sorting, because the two rows are different things:
  // one is "what you are working on, and the folders it reads from", the other
  // is "everything else you could open".
  const openCatalog = catalogs.find((c) => c.id === activeCatalog?.id) || null;
  const closedCatalogs = catalogs.filter((c) => c.id !== activeCatalog?.id);
  // The open catalog's right-hand column: Storage routes, and Backup (to come).
  // /catalogs?tab=backup opens on the Backup tab (the Dashboard's Backups card links there).
  const [catalogTab, setCatalogTab] = useState(
    () => (new URLSearchParams(window.location.search).get('tab') === 'backup' ? 'backup' : 'routes'));

  // One catalog card. Extracted from the grid so the SAME card can be drawn
  // in either row: the open catalog on its own row beside its storage routes,
  // and every closed catalog together below.
  const renderCatalogCard = (catalog) => {
          // The strip across the top: the decorator picked for this catalog
          // when it was created ("theme2/cardDeco7"), in colour while open and
          // in its black-and-white twin while closed.
          const isOpen = activeCatalog?.id === catalog.id;
          const topImage = `${catalogDecorator(catalog)}${isOpen ? '' : 'bw'}.png`;
          const strategyBadge = getStrategyBadge(catalog.file_strategy);
          const isFlipped = flippedCatalogId === catalog.id;

          // ── A closed catalog: half the height, and only what you need to
          //    choose it ───────────────────────────────────────────────────
          // Name, owner, Open. The full card answers "how is this catalog
          // configured", which is a question about the catalog you are working
          // in - and the row below the open one is not that, it is "everything
          // else you could open". Twelve of these at full height pushed the
          // open catalog off the screen to show configuration nobody was
          // reading.
          //
          // No delete, edit or share here either, and no flip: every action on
          // a catalog now starts with opening it, which is the rule storage
          // routes already followed (see the note in the body below). One less
          // way to delete the wrong catalog from a grid of twelve look-alikes.
          if (!isOpen) {
            return (
              <CCol key={catalog.id} xs="auto">
                <CCard
                  className="shadow-sm"
                  style={{
                    // One width for every catalog card, open or closed, and
                    // for the deployment cards (Deployments.js).
                    width: '360px',
                    // Half of the open card's 600px.
                    height: '300px',
                    display: 'flex',
                    flexDirection: 'column',
                    overflow: 'hidden',
                    ...CARD_TOP_CORNERS,
                  }}
                >
                  <CardBand image={topImage} label="catalog" />
                  <CCardHeader style={{ minHeight: '64px' }}>
                    <div className="overflow-hidden">
                      <div className="fw-semibold text-truncate">{catalog.name}</div>
                      <small className="text-muted d-block text-truncate">
                        {catalog.owner || 'No owner'}
                      </small>
                    </div>
                  </CCardHeader>
                  <CCardBody className="d-flex flex-column justify-content-end">
                    <div className="d-flex justify-content-end">
                      <CButton
                        color="primary"
                        size="sm"
                        onClick={() => setActiveCatalog(catalog)}
                      >
                        <CIcon icon={cilCheck} className="me-2" />
                        Open
                      </CButton>
                    </div>
                  </CCardBody>
                </CCard>
              </CCol>
            );
          }

          return (
            <CCol key={catalog.id} xs="auto">
              {/* Flip card container. 360px (was 400): narrower beside the
                  tabs, and still enough for the edit form and the sharing
                  panel on its back. */}
              <div style={{
                perspective: '1000px',
                height: '600px',
                width: '360px',
              }}>
                <div style={{
                  position: 'relative',
                  width: '100%',
                  height: '100%',
                  transformStyle: 'preserve-3d',
                  transition: 'transform 0.55s cubic-bezier(0.4, 0.2, 0.2, 1)',
                  transform: isFlipped ? 'rotateY(180deg)' : 'rotateY(0deg)',
                }}>
                  {/* FRONT FACE */}
                  <div style={{
                    position: 'absolute',
                    width: '100%',
                    height: '100%',
                    backfaceVisibility: 'hidden',
                    WebkitBackfaceVisibility: 'hidden',
                  }}>
                    <CCard
                      className="shadow-sm"
                      style={{
                        width: '100%',
                        height: '100%',
                        display: 'flex',
                        flexDirection: 'column',
                        overflow: 'hidden',
                        ...CARD_TOP_CORNERS,
                      }}
                    >
                      <CardBand image={topImage} label="catalog" />
                      <CCardHeader
                        className="d-flex justify-content-between align-items-start"
                        style={{ minHeight: '64px' }}
                      >
                        <div className="me-3 overflow-hidden">
                          <div className="fw-semibold text-truncate">{catalog.name}</div>
                          <small className="text-muted d-block text-truncate">
                            {catalog.owner || 'No owner'}
                          </small>
                        </div>
                        <div className="d-flex flex-shrink-0 gap-1">
                          <CButton
                            color="danger"
                            variant="ghost"
                            size="sm"
                            title="Delete catalog"
                            className="ms-1"
                            onClick={() => handleDelete(catalog.id)}
                          >
                            <CIcon icon={cilTrash} />
                          </CButton>
                          {/* Only an owner may share, and the API enforces
                              it too — hiding the button just avoids offering
                              an action that would 403. */}
                          {(!catalog.role || catalog.role === 'owner') && (
                            <CButton
                              color="success"
                              variant="ghost"
                              size="sm"
                              title="Share catalog"
                              onClick={() => openShareCard(catalog)}
                            >
                              <CIcon icon={cilShare} />
                            </CButton>
                          )}
                          {/* No storage-routes button. Routes are managed
                              beside the catalog once it is OPEN, which is the
                              one place they can be added, renamed or removed —
                              two entry points for the same thing was the
                              redundancy this removed. */}
                          <CButton
                            color="info"
                            variant="ghost"
                            size="sm"
                            title="Edit catalog"
                            onClick={() => openEditCard(catalog)}
                          >
                            <CIcon icon={cilPencil} />
                          </CButton>
                        </div>
                      </CCardHeader>

                      <CCardBody className="d-flex flex-column" style={{ flex: 1, overflow: 'auto' }}>
                        <div className="flex-grow-1 mb-3">
                          <div className="mb-3">
                            <CFormLabel className="small fw-semibold mb-1">Storage Strategy</CFormLabel>
                            <div>
                              <CBadge color={strategyBadge.color}>{strategyBadge.label}</CBadge>
                              {/* The two kinds of catalog behave differently
                                  enough — one owns its files, one does not —
                                  to be tellable apart at a glance. */}
                              {catalog.ingest_mode === 'reference' && (
                                <CBadge color="info" className="ms-1">indexed in place</CBadge>
                              )}
                              {catalog.ingest_mode === 'move' && (
                                <CBadge color="warning" className="ms-1">moves files in</CBadge>
                              )}
                            </div>
                          </div>

                          {catalog.ingest_mode === 'reference' && (
                            <div className="mb-3">
                              <CFormLabel className="small fw-semibold mb-1">Source Folder</CFormLabel>
                              <div style={{ fontSize: '0.75rem', fontFamily: 'monospace', color: '#6c757d', wordBreak: 'break-all' }}>
                                {catalog.source_root || <em>not set on this machine</em>}
                              </div>
                            </div>
                          )}

                          <div className="mb-3">
                            <CFormLabel className="small fw-semibold mb-1">Similarity Metric</CFormLabel>
                            <div style={{ fontSize: '0.875rem', color: '#495057' }}>
                              {catalog.similarity_metric === 'contextual' ? 'Contextual' : 'Cosine'}
                            </div>
                          </div>

                          <div className="mb-3">
                            <CFormLabel className="small fw-semibold mb-1">Description</CFormLabel>
                            <div style={{ fontSize: '0.875rem', color: '#495057' }}>
                              {catalog.description || 'No description'}
                            </div>
                          </div>

                          <div className="mb-3">
                            <CFormLabel className="small fw-semibold mb-1">
                              {catalog.ingest_mode === 'reference' ? 'Catalog Folder' : 'Target Directory'}
                            </CFormLabel>
                            <div style={{ fontSize: '0.75rem', fontFamily: 'monospace', color: '#6c757d', wordBreak: 'break-all' }}>
                              {catalog.target_directory}
                            </div>
                          </div>

                          {/* ── Storage routes ────────────────────────────
                              Reference only, so a closed catalog still says
                              which folders it reads from. Adding, renaming and
                              removing happen beside the catalog once it is
                              open — there is deliberately no way to do it from
                              here, because doing anything to a catalog starts
                              with opening it.

                              A route's availability dot is repeated here
                              because "the import found nothing" and "that
                              folder is not mounted right now" look identical
                              from the workflow side. */}
                          <div className="mb-0">
                            <CFormLabel className="small fw-semibold mb-1">
                              Storage Routes
                            </CFormLabel>
                            {(routesByCatalog[catalog.id] || []).length === 0 ? (
                              <div style={{ fontSize: '0.8rem', color: '#adb5bd' }}>
                                <em>None yet</em>
                              </div>
                            ) : (
                              <div className="d-flex flex-wrap gap-1">
                                {routesByCatalog[catalog.id].map((route) => (
                                  <CBadge
                                    key={route.id}
                                    color="light"
                                    className="text-dark d-inline-flex align-items-center gap-1"
                                    style={{ fontSize: '0.72rem', fontWeight: 500 }}
                                    title={route.available
                                      ? `${route.host_path || route.container_path} — available`
                                      : `${route.host_path || route.container_path} — not readable right now`}
                                  >
                                    <span style={{
                                      width: 7, height: 7, borderRadius: '50%',
                                      display: 'inline-block', flexShrink: 0,
                                      background: route.available ? '#2e7d32' : '#c62828',
                                    }} />
                                    {route.name}
                                  </CBadge>
                                ))}
                              </div>
                            )}
                          </div>
                        </div>

                        <div className="pt-3 mt-auto d-flex justify-content-end">
                          {activeCatalog?.id === catalog.id && (
                            <CButton
                              color="secondary"
                              variant="outline"
                              size="sm"
                              onClick={() => {
                                clearActiveCatalog();
                                if (isCatalogDependent(location.pathname)) {
                                  navigate('/catalogs');
                                }
                              }}
                            >
                              <CIcon icon={cilX} className="me-2" />
                              Close
                            </CButton>
                          )}
                          {activeCatalog?.id !== catalog.id && (
                            <CButton
                              color="primary"
                              size="sm"
                              onClick={() => setActiveCatalog(catalog)}
                            >
                              <CIcon icon={cilCheck} className="me-2" />
                              Open
                            </CButton>
                          )}
                        </div>

                      </CCardBody>
                    </CCard>
                  </div>

                  {/* BACK FACE (Edit details) */}
                  <div style={{
                    position: 'absolute',
                    width: '100%',
                    height: '100%',
                    backfaceVisibility: 'hidden',
                    WebkitBackfaceVisibility: 'hidden',
                    transform: 'rotateY(180deg)',
                  }}>
                    <CCard className="h-100 shadow-sm border-primary" style={CARD_TOP_CORNERS}>
                      <CCardHeader className="bg-primary text-white d-flex justify-content-between align-items-center py-2"
                                   style={CARD_TOP_CORNERS}>
                        <strong style={{ fontSize: '0.9rem' }}>
                          {isFlipped && flipMode === 'share' ? 'Share Catalog'
                            : 'Edit Catalog'}
                        </strong>
                        <CButton
                          color="link"
                          size="sm"
                          className="p-0 text-white"
                          onClick={closeEditCard}
                        >
                          <CIcon icon={cilX} />
                        </CButton>
                      </CCardHeader>
                      <CCardBody className="d-flex flex-column" style={{ overflowY: 'auto', padding: '0.75rem' }}>
                        {/* Same flip, two backs. Sharing is about who can
                            reach the catalog, not about its own fields, so it
                            gets its own face instead of another form section. */}
                        {/* isFlipped as well as the mode: every card's back face
                            is in the DOM, and flipMode is one shared value, so
                            without this the share panel would mount for every
                            catalog and fetch access for all of them on each flip. */}
                        {isFlipped && flipMode === 'share' ? (
                          <CatalogSharePanel catalogId={catalog.id} catalogName={catalog.name} />
                        ) : (
                          <>
                        {saveError && (
                          <CAlert color="danger" className="mb-2 py-2" style={{ fontSize: '0.8rem' }}>
                            {saveError}
                          </CAlert>
                        )}
                        <div className="flex-grow-1">
                          <div className="mb-2">
                            <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Name</CFormLabel>
                            <CFormInput
                              size="sm"
                              name="name"
                              value={isFlipped ? editFormData.name : ''}
                              onChange={handleEditChange}
                              placeholder="Catalog name"
                            />
                          </div>
                          {/* Owner is shown, not edited. It is a user
                              reference now, and handing a catalog to someone
                              else is a sharing decision — that lives behind
                              the Share button, which refuses to leave a
                              catalog with no owner. */}
                          <div className="mb-2">
                            <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Owner</CFormLabel>
                            <div style={{ fontSize: '0.85rem', color: '#495057' }}>
                              {catalog.owner || 'No owner'}
                            </div>
                          </div>
                          <div className="mb-2">
                            <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Description</CFormLabel>
                            <CFormTextarea
                              size="sm"
                              rows={3}
                              name="description"
                              value={isFlipped ? editFormData.description : ''}
                              onChange={handleEditChange}
                              placeholder="Description"
                              style={{ fontSize: '0.82rem', resize: 'none' }}
                            />
                          </div>
                          <div className="mb-2">
                            <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Storage Strategy</CFormLabel>
                            <CFormSelect
                              size="sm"
                              name="file_strategy"
                              value={isFlipped ? editFormData.file_strategy : 'local'}
                              onChange={handleEditChange}
                            >
                              <option value="local">Local Storage</option>
                              <option value="scp">SCP (Secure Copy)</option>
                              <option value="ftp">FTP</option>
                              <option value="box">Box Cloud Storage</option>
                              <option value="s3">Amazon S3</option>
                            </CFormSelect>
                          </div>
                          <div className="mb-2">
                            <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Similarity Metric</CFormLabel>
                            <CFormSelect
                              size="sm"
                              name="similarity_metric"
                              value={isFlipped ? editFormData.similarity_metric : 'cosine'}
                              onChange={handleEditChange}
                            >
                              <option value="cosine">Cosine</option>
                              <option value="contextual">Contextual</option>
                            </CFormSelect>
                          </div>
                          <div className="mb-2">
                            <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Target Directory</CFormLabel>
                            <CFormInput
                              size="sm"
                              name="target_directory"
                              value={isFlipped ? editFormData.target_directory : ''}
                              onChange={handleEditChange}
                              placeholder="/path/to/directory"
                              style={{ fontFamily: 'monospace', fontSize: '0.75rem' }}
                            />
                          </div>
                        </div>
                        <div className="d-flex gap-2 mt-2 pt-2 border-top">
                          <CButton
                            color="primary"
                            size="sm"
                            className="flex-grow-1"
                            onClick={() => handleSaveEdit(catalog.id)}
                            disabled={saving}
                          >
                            {saving ? (
                              <>
                                <CSpinner size="sm" className="me-1" />
                                Saving...
                              </>
                            ) : (
                              <>
                                <CIcon icon={cilSave} className="me-1" />
                                Save
                              </>
                            )}
                          </CButton>
                          <CButton
                            color="danger"
                            variant="outline"
                            size="sm"
                            onClick={() => handleDelete(catalog.id)}
                            disabled={saving}
                          >
                            <CIcon icon={cilTrash} />
                          </CButton>
                        </div>
                          </>
                        )}
                      </CCardBody>
                    </CCard>
                  </div>
                </div>
              </div>
            </CCol>
          );
  };

  if (loading) {
    return (
      <div className="text-center">
        <CSpinner color="primary" />
      </div>
    );
  }

  return (
    <>
      {/* Header */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Catalogs</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Manage your photo catalogs and storage configurations
          </div>
        </div>
      </div>

      <CCard className="mb-4" style={{ backgroundColor: 'white' }}>
        <CCardBody className="d-flex justify-content-start align-items-center py-3">
          <CButton color="primary" size="sm" onClick={onCreate}>
            <CIcon icon={cilPlus} className="me-2" />
            Create Catalog
          </CButton>
        </CCardBody>
      </CCard>

      {error && (
        <div className="alert alert-danger" role="alert">
          {error}
        </div>
      )}

      {catalogs.length === 0 ? (
        <div className="text-center text-muted py-4">
          No catalogs found. Create your first catalog to get started.
        </div>
      ) : (
        <>
          {/* ── The open catalog, and the folders it imports from ──────────
              TWO COLUMNS, not one row of cards: the catalog on the left, and a
              column on the right that holds its own row of route cards. The
              nesting is what keeps them apart - in a single flat row, the
              fourth route card wraps underneath the CATALOG card, which reads
              as if it belongs to nothing. Nested, the routes wrap among
              themselves and the catalog keeps its column at any width.

              A route belongs to exactly one catalog, so showing them side by
              side is the arrangement that matches the model - and it is why
              there is no longer an Admin -> Storage page.

              Absent entirely when nothing is open: an empty first row would
              be a heading over nothing. */}
          {openCatalog && (
            <CRow className="g-3 align-items-start mb-4">
              {renderCatalogCard(openCatalog)}
              {/* The right-hand column is tabbed: the folders the catalog
                  imports from, and its backups. Both panes stay mounted, so
                  switching back does not reload the routes. */}
              <CCol xs={12} lg style={{ minWidth: 0 }}>
                {/* Underline tabs, after the owner's sample (sample.png): plain
                    labels on one white panel, the selected one with a 2px
                    underline resting on the bar's rule. The application's own
                    colours: the menu's active blue #1976d2 and its text grey
                    #5c6873, and the page's border #dee2e6. */}
                <div className="bg-white rounded" style={{ border: '1px solid #dee2e6' }}>
                  <div role="tablist" className="d-flex px-3"
                       style={{ borderBottom: '1px solid #dee2e6' }}>
                    {[['routes', 'Storage routes'], ['backup', 'Backup']].map(([key, label]) => {
                      const on = catalogTab === key;
                      return (
                        <button key={key} type="button" role="tab" aria-selected={on}
                                onClick={() => setCatalogTab(key)}
                                className="catalog-tab"
                                style={{
                                  background: 'none', border: 'none',
                                  padding: '0.85rem 1.1rem 0.7rem',
                                  marginBottom: -1,
                                  borderBottom: `2px solid ${on ? '#1976d2' : 'transparent'}`,
                                  color: on ? '#1976d2' : '#5c6873',
                                  fontWeight: on ? 600 : 500,
                                  fontSize: '0.95rem',
                                  cursor: 'pointer',
                                }}>
                          {label}
                        </button>
                      );
                    })}
                  </div>
                {/* Under the white tab strip, the application's light page
                    grey (bg-light) - the sample's darker interior - so the
                    white route cards stand out on it. The cards sit in a g-3
                    row, whose -1rem top margin would pull them against the tab
                    bar: 2rem of padding leaves a 1rem gap. */}
                <CTabContent className="bg-light rounded-bottom px-3 pb-3"
                             style={{ paddingTop: '2rem' }}>
                  <CTabPane role="tabpanel" visible={catalogTab === 'routes'}>
                    <StorageRouteCards
                      catalogId={openCatalog.id}
                      catalogName={openCatalog.name}
                      onChanged={loadCatalogs}
                      inColumn={false}
                    />
                  </CTabPane>
                  <CTabPane role="tabpanel" visible={catalogTab === 'backup'}>
                    {/* Mounted only once opened: it polls while a backup runs. */}
                    {catalogTab === 'backup' && (
                      <BackupCards
                        catalogId={openCatalog.id}
                        catalogName={openCatalog.name}
                        canManage={!openCatalog.role || openCatalog.role === 'owner'}
                      />
                    )}
                  </CTabPane>
                </CTabContent>
                </div>
              </CCol>
            </CRow>
          )}

          {/* The other catalogs only while none is open: with one open, this
              page is about that catalog. Close it - or switch in the top bar's
              catalog picker - to choose another. */}
          {!openCatalog && closedCatalogs.length > 0 && (
            <CRow className="g-3 align-items-start">
              {closedCatalogs.map(renderCatalogCard)}
            </CRow>
          )}
        </>
      )}
    </>
  );
};

export default CatalogList;
