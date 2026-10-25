import React, { useState, useEffect, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  CContainer, CCard, CCardBody, CRow, CCol, CButton, CAlert, CSpinner, CBadge,
  CModal, CModalHeader, CModalTitle, CModalBody, CModalFooter,
  CForm, CFormLabel, CFormInput, CFormTextarea, CFormSelect,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilPlus, cilTrash, cilPencil, cilSync, cilLayers, cilCloudUpload, cilCheckCircle,
  cilDataTransferUp, cilCalendar, cilPeople,
} from '@coreui/icons';
import { albumAPI, deploymentAPI, transferAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { TRANSFER_ACTIVE_STATUSES } from '../components/TransferPanel';
import AlbumCard, {
  CARD_WIDTH, CARD_MIN_HEIGHT, getAlbumEditorRoute,
} from '../components/AlbumCard';
import YearAlbumTemplateModal from '../components/YearAlbumTemplateModal';
import MassTransferModal from '../components/MassTransferModal';

// ─── Albums ───────────────────────────────────────────────────────────────────
// Every album in the open catalog, as cards, in one place.
//
// The cards used to be drawn on the Deployments page, each row showing a
// deployment and the albums it publishes. That put an album in as many places
// as there were deployments publishing it, and an album in two deployments was
// two cards. Here there is one card per album, whatever publishes it, and
// Deployments is left showing deployments.
//
// What a deployment still owns is its MEMBERSHIP: ticking an album into or out
// of a deployment happens on that deployment's card, on its albums face. This
// page shows the result of that as badges, so an album that nothing publishes
// is visible as such.

// A card that acts as a button, reachable and usable from the keyboard too.
const activate = (onClick) => ({
  role: 'button',
  tabIndex: 0,
  onClick,
  onKeyDown: (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onClick(); }
  },
});

const DASHED_CARD_STYLE = {
  width: CARD_WIDTH, minHeight: CARD_MIN_HEIGHT, border: '2px dashed #ced4da',
  background: 'transparent', cursor: 'pointer',
};

// The tile that adds one, sitting at the end of the row - so an empty catalog
// still shows something to click, and the next album has somewhere to go.
const AddAlbumCard = ({ onClick }) => (
  <CCol xs="auto">
    <CCard className="shadow-sm h-auto" style={DASHED_CARD_STYLE} {...activate(onClick)}>
      <CCardBody className="d-flex flex-column align-items-center justify-content-center text-center">
        <CIcon icon={cilPlus}
               style={{ width: 26, height: 26, color: '#adb5bd', marginBottom: 8 }} />
        <div style={{ fontSize: '0.88rem', color: '#495057' }}>Create an album</div>
        <div style={{ fontSize: '0.76rem', color: '#adb5bd', marginTop: 4 }}>
          A curated collection of this catalog's photographs
        </div>
      </CCardBody>
    </CCard>
  </CCol>
);

// Its twin, beside it: the same dashed tile, so the two read as two ways to
// start an album, but a calendar and an unmissable TEMPLATE so they are not
// mistaken for each other (template-albums-spec.md §7.1). Shown even in a
// catalog with no events; the window then says why nothing can be made.
const TemplateAlbumCard = ({ onClick }) => (
  <CCol xs="auto">
    <CCard className="shadow-sm h-auto" style={DASHED_CARD_STYLE} {...activate(onClick)}>
      <CCardBody className="d-flex flex-column align-items-center justify-content-center text-center">
        <CIcon icon={cilCalendar}
               style={{ width: 26, height: 26, color: '#adb5bd', marginBottom: 8 }} />
        <div style={{ fontSize: '0.8rem', fontWeight: 700, letterSpacing: '0.12em',
                      color: 'var(--cui-primary, #321fdb)' }}>
          TEMPLATE
        </div>
        <div style={{ fontSize: '0.76rem', color: '#adb5bd', marginTop: 4 }}>
          Create an Album with all events for the Year
        </div>
      </CCardBody>
    </CCard>
  </CCol>
);

// Who publishes this album, on the card itself. A deployment badge each, or the
// one badge that says nothing publishes it - which is the state worth noticing,
// since an album in no deployment has no gallery and no link.
const DeploymentBadges = ({ deployments }) => {
  if (deployments.length === 0) {
    return (
      <CBadge color="warning" style={{ fontSize: '0.7rem' }}
              title="No deployment publishes this album yet">
        not deployed
      </CBadge>
    );
  }
  return deployments.map((d) => (
    <CBadge key={d.id} color="info" style={{ fontSize: '0.7rem' }}
            title={`Published by the deployment "${d.name}"`}>
      {d.name}
    </CBadge>
  ));
};

// ─── Publish this album in a deployment ───────────────────────────────────────
// The right-hand half of the card's control row, in one of three states:
//
//   no deployments exist   disabled, with the hint that says what to do first.
//                          A deployment is the thing that HAS a URL, so an
//                          album cannot be published until one exists.
//   not published yet      enabled. One deployment: straight there. Several:
//                          the picker below, because the album has to land in
//                          one of them and only the user knows which.
//   already published      disabled and says so, naming what publishes it.
//                          Adding it to a SECOND deployment is the deployment
//                          card's albums face, where membership lives.
const DeployButton = ({ deployments, publishedBy, busy, onDeploy }) => {
  if (publishedBy.length > 0) {
    return (
      <CButton color="success" variant="outline" size="sm" disabled
               title={`Published by ${publishedBy.map((d) => d.name).join(', ')}`}>
        <CIcon icon={cilCheckCircle} className="me-1" />
        Deployed
      </CButton>
    );
  }

  if (deployments.length === 0) {
    return (
      <CButton color="primary" size="sm" disabled
               title="There is no deployment to publish this album in yet">
        <CIcon icon={cilCloudUpload} className="me-1" />
        Deploy
      </CButton>
    );
  }

  const only = deployments.length === 1 ? deployments[0] : null;
  return (
    <CButton color="primary" size="sm" disabled={busy}
             title={only
               ? `Publish this album in "${only.name}"`
               : 'Choose the deployment to publish this album in'}
             onClick={() => onDeploy(only)}>
      {busy ? <CSpinner size="sm" /> : (
        <>
          <CIcon icon={cilCloudUpload} className="me-1" />
          {only ? `Deploy to ${only.name}` : 'Deploy…'}
        </>
      )}
    </CButton>
  );
};

// ─── Send this album to another machine ───────────────────────────────────────
// Left of Deploy, and the ONLY way to start a transfer: the Transfers page no
// longer lists albums (transfer-spec.md 3.1).
//
// Never disabled for want of content. The card cannot tell whether an album has
// transferable originals - photo_count comes from its last filter and is null
// for a tools album, and the real test is every file having a sha256 and being
// on disk, which is what the endpoint runs. Guessing here would kill the button
// for albums that can in fact be sent, so the 400 is reported instead (3.2).
const TransferButton = ({ active, busy, onTransfer }) => (
  <CButton color="secondary" variant="outline" size="sm" disabled={busy}
           title={active
             ? 'A transfer of this album is already running — open it'
             : 'Send this album\'s original files to another machine'}
           onClick={onTransfer}>
    {busy ? <CSpinner size="sm" /> : (
      <>
        <CIcon icon={cilDataTransferUp} className="me-1" />
        {active ? 'Open transfer' : 'Transfer'}
      </>
    )}
  </CButton>
);

// ─── Send this album to several people ────────────────────────────────────────
// Beside Transfer: one code per person, all waiting at once, one deadline
// (album-mass-transfer-spec.md). An album has at most one open group, so once
// it has one this opens it rather than starting a second (spec 3.8).
const MassTransferButton = ({ group, onClick }) => (
  <CButton color="secondary" variant="outline" size="sm" onClick={onClick}
           title={group
             ? 'This album already has a group of codes — open it'
             : 'Send this album to several people, each with their own code'}>
    <CIcon icon={cilPeople} className="me-1" />
    {group ? 'Open group' : 'Mass transfer'}
  </CButton>
);

// The card's control row: Transfer and Mass transfer, then Deploy, bottom
// right. It wraps rather than squeezes: three buttons and a long deployment
// name do not fit CARD_WIDTH, and Deploy dropping to its own line is better
// than a truncated label. The deployment hint is a line UNDER them (3.2).
const CardActions = ({
  deployments, publishedBy, activeTransfer, group, busy, sending, onDeploy,
  onTransfer, onMassTransfer,
}) => (
  <>
    <div className="d-flex flex-wrap justify-content-end gap-2">
      <TransferButton active={activeTransfer} busy={sending} onTransfer={onTransfer} />
      <MassTransferButton group={group} onClick={onMassTransfer} />
      <DeployButton deployments={deployments} publishedBy={publishedBy}
                    busy={busy} onDeploy={onDeploy} />
    </div>
    {/* A disabled button does not reliably raise a tooltip, and the reason it
        is dead is the only thing worth saying here. */}
    {publishedBy.length === 0 && deployments.length === 0 && (
      <div className="text-end mt-1" style={{ fontSize: '0.72rem', color: '#adb5bd' }}>
        First create a deployment
      </div>
    )}
  </>
);

const Albums = () => {
  const navigate = useNavigate();
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;

  const [albums, setAlbums] = useState([]);
  const [deployments, setDeployments] = useState([]);
  const [transfers, setTransfers] = useState([]);
  const [batches, setBatches] = useState([]);
  // The album the "several people" dialog is open for.
  const [massAlbum, setMassAlbum] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [busyId, setBusyId] = useState(null);
  // Separate from busyId: a transfer POST ends in a navigation, not a reload,
  // and the card should not look as though it were deleting itself meanwhile.
  const [sendingId, setSendingId] = useState(null);

  // The album waiting for a deployment to be chosen, when there is more than
  // one to choose from.
  const [picking, setPicking] = useState(null);
  const [pickId, setPickId] = useState('');

  const [creating, setCreating] = useState(false);
  const [form, setForm] = useState({ name: '', description: '', mode: 'tools',
                                     deploymentId: '' });
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState(null);
  const [templateOpen, setTemplateOpen] = useState(false);

  const load = useCallback(async () => {
    if (!catalogId) { setLoading(false); return; }
    try {
      // Deployments come along for what publishes each album and for the
      // optional attach below; transfers, for which albums are mid-send. Three
      // calls in one round trip rather than three renders - a card that cannot
      // say which of the three states it is in is worse than a card that
      // arrives a moment later.
      const [alb, dep, tra] = await Promise.all([
        albumAPI.getAll(catalogId),
        deploymentAPI.getAll(catalogId),
        transferAPI.list(catalogId),
      ]);
      setAlbums(alb.data || []);
      setDeployments(dep.data?.deployments || []);
      setTransfers(tra.data?.transfers || []);
      setBatches(tra.data?.batches || []);
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load albums.');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  // albumId -> the deployments publishing it. Built from the deployment list
  // rather than asked of the album endpoint, which does not carry it.
  const deploymentsOf = (albumId) => deployments.filter(
    (d) => (d.albums || []).some((a) => a.id === albumId),
  );

  const run = async (album, fn, fallback) => {
    setBusyId(album.id);
    setError(null);
    try {
      await fn();
      await load();
    } catch (e) {
      setError(e?.response?.data?.error || fallback);
    } finally {
      setBusyId(null);
    }
  };

  const remove = (album) => {
    if (!window.confirm(
      `Delete the album "${album.name}" and all its filters?\n\n`
      + 'It is removed from every deployment that publishes it. The photographs '
      + 'themselves are not touched.')) return;
    run(album, () => albumAPI.delete(catalogId, album.id),
        'Could not delete that album.');
  };

  const deploy = (album) => run(album, async () => {
    await albumAPI.deploy(catalogId, album.id);
    // A deployment's symlinks and photo counts are written from what is on
    // disk, so every deployment publishing this album is republished after the
    // album folder changes - otherwise the gallery still shows the old
    // contents. Sequential, because they write the same tree.
    for (const d of deploymentsOf(album.id)) {
      await deploymentAPI.publish(catalogId, d.id);
    }
  }, 'Could not deploy that album.');

  // The running send for this album, so the card offers the transfer that
  // exists rather than starting a duplicate the backend refuses (409).
  // Group members do not count: a group and a single send of one album never
  // collide, so neither blocks the other (album-mass-transfer-spec.md 3.8).
  const activeTransferFor = (albumId) => transfers.find(
    (t) => t.direction === 'send' && t.album_id === albumId && !t.batch_id
           && TRANSFER_ACTIVE_STATUSES.includes(t.status));

  // The album's group that still has someone to reach - is_open is the
  // backend's own test, the one its 409 uses.
  const openGroupFor = (albumId) => batches.find(
    (b) => b.album_id === albumId && b.is_open);

  const startMassTransfer = (album) => {
    const group = openGroupFor(album.id);
    if (group) { navigate(`/albums/transfers/batches/${group.id}`); return; }
    setMassAlbum(album);
  };

  const massCreated = (batchId, missing) => {
    setMassAlbum(null);
    const notice = missing?.length
      ? `${missing.length} original file(s) are missing on disk and will be reported `
        + `as failed: ${missing.slice(0, 3).join(', ')}${missing.length > 3 ? '…' : ''}`
      : null;
    navigate(`/albums/transfers/batches/${batchId}`, notice ? { state: { notice } } : undefined);
  };

  // Send the album's originals to another machine.
  //
  // Ends on the Transfers page, in THIS tab. The live transfer has a permanent
  // home there now - the first row rebuilds itself from the transfer list and
  // the socket on every visit - so nothing is pinned to the tab this was
  // started from, and a second tab would cost a socket connection per copy
  // (transfer-spec.md 3.3).
  const startTransfer = async (album) => {
    const running = activeTransferFor(album.id);
    if (running) { navigate('/albums/transfers'); return; }

    setSendingId(album.id);
    setError(null);
    try {
      const r = await transferAPI.start(catalogId, album.id);
      const missing = r.data?.missing_files || [];
      if (missing.length) {
        // Named up front rather than discovered as failures one by one halfway
        // through a multi-gigabyte transfer.
        setError(`${missing.length} original file(s) are missing on disk and will `
          + `be reported as failed: ${missing.slice(0, 3).join(', ')}`
          + `${missing.length > 3 ? '…' : ''}`);
      }
      navigate('/albums/transfers');
    } catch (e) {
      // 409 means one is already running: the card's view of that can be a few
      // seconds stale, and the panel on the Transfers page shows the running
      // one, so there is nothing to report.
      if (e?.response?.status === 409) {
        navigate('/albums/transfers');
        return;
      }
      setError(e?.response?.data?.error || 'Could not start the transfer.');
    } finally {
      setSendingId(null);
    }
  };

  // Publish the album in a deployment: BUILD IT, then attach it.
  //
  // In that order deliberately. Attaching republishes the deployment on the
  // backend, and a deployment publish links what is on disk - so an album that
  // has never been built would be linked as "skipped" and the gallery would
  // show an empty album until something rebuilt it.
  const deployTo = (album, deployment) => {
    setPicking(null);
    run(album, async () => {
      await albumAPI.deploy(catalogId, album.id);
      await deploymentAPI.addAlbum(catalogId, deployment.id, album.id);
    }, `Could not publish that album in "${deployment.name}".`);
  };

  const startDeploy = (album, only) => {
    if (only) { deployTo(album, only); return; }
    setPickId(String(deployments[0].id));
    setPicking(album);
  };

  const openNew = () => {
    setForm({ name: '', description: '', mode: 'tools', deploymentId: '' });
    setFormError(null);
    setCreating(true);
  };

  // Create, then attach if a deployment was chosen. The attach republishes on
  // the backend, so an album never exists in a deployment whose published
  // folder does not know about it - and then the editor opens, because a new
  // album is empty and choosing its photos is the next thing to do.
  const create = async () => {
    const name = form.name.trim();
    if (!name) { setFormError('Album name is required'); return; }
    setSaving(true);
    setFormError(null);
    try {
      const res = await albumAPI.create(catalogId, {
        name, description: form.description, mode: form.mode,
      });
      const album = res.data;
      if (form.deploymentId) {
        await deploymentAPI.addAlbum(catalogId, Number(form.deploymentId), album.id);
      }
      setCreating(false);
      navigate(getAlbumEditorRoute(album));
    } catch (e) {
      setFormError(e?.response?.data?.error || 'Could not create that album.');
    } finally {
      setSaving(false);
    }
  };

  // The template made the whole album; open it where it can be edited. A
  // publish that failed after the album was made travels along as a warning.
  const templateCreated = (album, notice) => {
    setTemplateOpen(false);
    navigate(getAlbumEditorRoute(album), notice ? { state: { notice } } : undefined);
  };

  if (!activeCatalog) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="info">Select a catalog to see its albums.</CAlert>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="px-4 py-4">
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Albums</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Every album in this catalog, and what publishes it
          </div>
        </div>
      </div>

      <CCard className="mb-4" style={{ backgroundColor: 'white' }}>
        <CCardBody className="d-flex justify-content-between align-items-center py-3">
          <CButton color="primary" size="sm" onClick={openNew}>
            <CIcon icon={cilPlus} className="me-2" />
            New Album
          </CButton>
          <div style={{ fontSize: '0.82rem', color: '#666' }}>
            Add or remove an album from a deployment on the
            {' '}
            <span role="link" tabIndex={0}
                  style={{ color: '#0d6efd', cursor: 'pointer' }}
                  onClick={() => navigate('/albums/deployments')}
                  onKeyDown={(e) => e.key === 'Enter' && navigate('/albums/deployments')}>
              Deployments
            </span>
            {' '}
            page.
          </div>
        </CCardBody>
      </CCard>

      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}

      {loading ? (
        <div className="text-center"><CSpinner color="primary" /></div>
      ) : (
        <CRow className="g-3 align-items-start">
          {albums.map((album) => {
            const busy = busyId === album.id;
            const publishedBy = deploymentsOf(album.id);
            return (
              <AlbumCard
                key={album.id}
                album={album}
                catalogId={catalogId}
                badges={<DeploymentBadges deployments={publishedBy} />}
                headerActions={(
                  <>
                    <CButton color="danger" variant="ghost" size="sm" className="me-1"
                             disabled={busy} title="Delete this album and all its filters"
                             onClick={() => remove(album)}>
                      <CIcon icon={cilTrash} />
                    </CButton>
                    {/* Rebuild, not publish: the Deploy control at the foot of
                        the card is what puts an album in a deployment for the
                        first time. This one refreshes an album whose contents
                        have moved on, and republishes whatever already shows
                        it. */}
                    <CButton color="success" variant="ghost" size="sm" className="me-1"
                             disabled={busy}
                             title="Rebuild this album and refresh what publishes it"
                             onClick={() => deploy(album)}>
                      {busy ? <CSpinner size="sm" /> : <CIcon icon={cilSync} />}
                    </CButton>
                    <CButton color="info" variant="ghost" size="sm"
                             disabled={busy} title="Open album editor"
                             onClick={() => navigate(getAlbumEditorRoute(album))}>
                      <CIcon icon={cilPencil} />
                    </CButton>
                  </>
                )}
              >
                <CardActions
                  deployments={deployments}
                  publishedBy={publishedBy}
                  activeTransfer={!!activeTransferFor(album.id)}
                  group={openGroupFor(album.id)}
                  busy={busy}
                  sending={sendingId === album.id}
                  onDeploy={(only) => startDeploy(album, only)}
                  onTransfer={() => startTransfer(album)}
                  onMassTransfer={() => startMassTransfer(album)}
                />
              </AlbumCard>
            );
          })}
          <AddAlbumCard onClick={openNew} />
          <TemplateAlbumCard onClick={() => setTemplateOpen(true)} />
        </CRow>
      )}

      <MassTransferModal visible={massAlbum !== null} album={massAlbum}
                         catalogId={catalogId} onClose={() => setMassAlbum(null)}
                         onCreated={massCreated} />

      {/* Which deployment, when there is more than one. A modal rather than a
          dropdown on the card: the choice decides the album's public URL, and a
          menu that publishes on hover-and-click is too easy to hit by accident. */}
      <CModal visible={picking !== null} onClose={() => setPicking(null)}>
        <CModalHeader>
          <CModalTitle>
            <CIcon icon={cilCloudUpload} className="me-2" />
            Publish {picking?.name}
          </CModalTitle>
        </CModalHeader>
        <CModalBody>
          <CFormLabel>Deployment</CFormLabel>
          <CFormSelect value={pickId} onChange={(e) => setPickId(e.target.value)}>
            {deployments.map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </CFormSelect>
          <small className="text-muted d-block mt-2">
            The album is built and the deployment republished straight away.
          </small>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setPicking(null)}>
            Cancel
          </CButton>
          <CButton
            color="primary"
            onClick={() => deployTo(picking,
              deployments.find((d) => String(d.id) === pickId))}
            disabled={!deployments.some((d) => String(d.id) === pickId)}
          >
            Deploy
          </CButton>
        </CModalFooter>
      </CModal>

      <CModal visible={creating} onClose={() => setCreating(false)}>
        <CModalHeader>
          <CModalTitle>
            <CIcon icon={cilLayers} className="me-2" />
            Create an album
          </CModalTitle>
        </CModalHeader>
        <CModalBody>
          {formError && <CAlert color="danger" style={{ fontSize: '0.85rem' }}>{formError}</CAlert>}
          <CForm>
            <div className="mb-3">
              <CFormLabel>Name <span className="text-danger">*</span></CFormLabel>
              <CFormInput
                value={form.name}
                onChange={(e) => setForm({ ...form, name: e.target.value })}
                placeholder="e.g. Summer 2024"
                onKeyDown={(e) => e.key === 'Enter' && create()}
              />
            </div>
            {/* Albums are made with tools. Building one with words (natural-
                language filters) is moving to Search; existing word-built
                albums still open in their own editor. */}
            {/* Optional, and off by default: an album exists on its own, and
                what publishes it is a separate decision that can be made later
                on the deployment card. Offered here because making one for a
                deployment is still the common case. */}
            <div className="mb-3">
              <CFormLabel>Publish it in</CFormLabel>
              <CFormSelect
                value={form.deploymentId}
                onChange={(e) => setForm({ ...form, deploymentId: e.target.value })}
                disabled={deployments.length === 0}
              >
                <option value="">No deployment for now</option>
                {deployments.map((d) => (
                  <option key={d.id} value={d.id}>{d.name}</option>
                ))}
              </CFormSelect>
              {deployments.length === 0 && (
                <small className="text-muted">
                  This catalog has no deployments yet.
                </small>
              )}
            </div>
            <div className="mb-1">
              <CFormLabel>Description</CFormLabel>
              <CFormTextarea
                rows={3}
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
                placeholder="Optional description"
              />
            </div>
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setCreating(false)}>
            Cancel
          </CButton>
          <CButton color="primary" onClick={create} disabled={saving || !form.name.trim()}>
            {saving ? <CSpinner size="sm" /> : 'Create'}
          </CButton>
        </CModalFooter>
      </CModal>

      <YearAlbumTemplateModal
        visible={templateOpen}
        catalogId={catalogId}
        deployments={deployments}
        onClose={() => setTemplateOpen(false)}
        onCreated={templateCreated}
      />
    </CContainer>
  );
};

export default Albums;
