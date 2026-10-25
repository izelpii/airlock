import React, { useState, useEffect, useCallback } from 'react';
import {
  CContainer, CCard, CCardBody, CCardHeader, CButton, CAlert, CSpinner,
  CBadge, CModal, CModalHeader, CModalTitle, CModalBody, CModalFooter,
  CForm, CFormLabel, CFormInput, CFormTextarea, CFormCheck, CRow, CCol,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilPlus, cilTrash, cilPencil, cilLockLocked, cilLockUnlocked, cilBriefcase, cilX,
  cilSync, cilImage,
} from '@coreui/icons';
import { deploymentAPI, albumAPI } from '../services/api';
import CardBand, { CARD_TOP_CORNERS } from '../components/CardBand';
import { ServerLine, ServersModal } from '../components/PublishServers';
import { useCatalog } from '../context/CatalogContext';

// ─── Album deployments ────────────────────────────────────────────────────────
// A deployment is a named, shareable gallery holding one or more albums.
// See documentation/specs/albums-enhancement-spec.md.
//
// This page lives under Albums rather than Admin: storage routes are
// installation-wide, but a deployment belongs to one catalog, and everything
// here is scoped to the active one.
//
// The album CARDS live on the Albums page, which is the one place an album is
// made, edited or deleted - an album in two deployments was two cards here, and
// one card there. What stays is membership: the front of the card is where an
// album is ticked into or out of this deployment, and the back is where the
// sizes it packs are chosen.

// Mirrors systems/backend/slugs.py so the value previewed while typing is the
// one the backend would derive. The backend still validates.
const slugify = (name) =>
  (name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/, '');

/**
 * The two URLs a deployment answers on.
 *
 * Composed HERE, in the browser, from the host it is already talking to — never
 * stored and never returned by the API. A deployment does not know how it was
 * reached, and a baked-in URL would be wrong under the other route and wrong
 * again in a packed bundle.
 */
function urlsFor(catalogSlug, slug) {
  if (!catalogSlug || !slug) return { path: null, subdomain: null };
  const { protocol, host, hostname } = window.location;
  // The subdomain form puts the CATALOG in the label, because a TLS wildcard
  // matches exactly one label — *.airlock.lan covers family.airlock.lan and does
  // not cover summer-2019.family.airlock.lan.
  const port = host.includes(':') ? `:${host.split(':')[1]}` : '';
  return {
    // /gallery/, not /albums/: the application owns /albums/* for its own
    // routes (Deployments, both album editors), so the gateway cannot proxy
    // that prefix without breaking them on refresh.
    path: `${protocol}//${host}/gallery/${catalogSlug}/${slug}`,
    subdomain: `${protocol}//${catalogSlug}.${hostname}${port}/${slug}`,
  };
}

const Field = ({ label, children }) => (
  <div className="mb-2">
    <CFormLabel className="small fw-semibold mb-1">{label}</CFormLabel>
    <div style={{ fontSize: '0.82rem', color: '#495057', wordBreak: 'break-all' }}>
      {children}
    </div>
  </div>
);

// The link a recipient will actually open, shown whole - catalog segment
// included - and clickable. No copy button: the URL is the text, so the
// browser's own "copy link address" already does it, and a caveat is a tooltip
// rather than a word competing with the address for the same line.
//
// target="_blank" with rel="noopener noreferrer": without noopener the opened
// page gets a handle on this one through window.opener.
const DeploymentLink = ({ url, title }) => {
  if (!url) return null;
  return (
    <div className="mb-1">
      <a
        href={url}
        target="_blank"
        rel="noopener noreferrer"
        title={title || url}
        style={{ fontSize: '0.72rem', wordBreak: 'break-all',
                 fontFamily: 'monospace', color: '#0d6efd' }}
      >
        {url}
      </a>
    </div>
  );
};

// The band across the top of a card: the decorator the backend picked for this
// deployment when it was created ("theme1/cardDeco7", one of 20 in
// public/theme1/, 720x100 - the band at 2x), with the kind of card written on
// it. In colour while the deployment is enabled, its black-and-white twin
// (cardDeco<n>bw.png) while not. A deployment served before it had one falls
// back to a pick derived from its id, so the card never reshuffles.
const DECOR_FALLBACK_THEME = 'theme1';
const DECOR_FALLBACK_COUNT = 20;
const decorFor = (deployment) => {
  const base = deployment?.decorator
    || `${DECOR_FALLBACK_THEME}/cardDeco${(Number(deployment?.id) || 0) % DECOR_FALLBACK_COUNT + 1}`;
  return `${base}${deployment?.enabled === false ? 'bw' : ''}.png`;
};

// As wide as a catalog card (CatalogList.js), open or closed, so the two pages
// line up.
const CARD_WIDTH = 360;
// Fixed, because a flip needs both faces to occupy the same box - and about a
// fifth taller than the content alone needed, which is what the coloured band
// and the roomier body use up.
const CARD_HEIGHT = 430;

// What a bundle can carry, in the order the card lists them. The 1200px base
// is always packed: a bundle without it has no image to show.
//
// Thumbnails are ticked by default and the label says who uses them, so
// unticking is a decision rather than an accident. The current gallery layout
// cannot use a square crop, but other gallery types are planned that use it
// natively - and a bundle is a one-way copy, so a size left out cannot be added
// back at the destination (file-sizes-spec.md 4.11).
const PACK_ROWS = [
  { key: '1200', label: '1200 px display image', always: true },
  { key: '1600', label: '1600 px display image', gb: 0.33 },
  { key: '2048', label: '2048 px display image', gb: 0.48 },
  { key: '2560', label: '2560 px display image', gb: 0.66 },
  // The original's own pixels, as a JPG. Last in the list and by far the
  // largest: the gallery's lightbox opens the widest size a photo has, so
  // ticking this is what makes a published album show - and give away - the
  // real photograph rather than a version of it.
  { key: 'full', label: 'Full resolution JPG', gb: 2.26,
    note: 'the original at full size — what the lightbox opens and a visitor downloads' },
  { key: 'thumb320', label: '320 px square thumbnails', gb: 0.006,
    note: 'used by grid and contact-sheet gallery types' },
];

// The back of the card. Same rule as the album checkboxes on the front: a
// change saves and republishes immediately.
const DeploymentSizes = ({ catalogId, deployment, onChanged }) => {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const packed = new Set(deployment.packed_rungs || ['1200', 'thumb320']);
  const available = new Set(deployment.available_rungs || ['1200', 'thumb320']);

  const toggle = async (key) => {
    const next = new Set(packed);
    if (next.has(key)) next.delete(key); else next.add(key);
    next.add('1200');
    setBusy(true);
    setError(null);
    try {
      await deploymentAPI.update(catalogId, deployment.id,
        { packed_rungs: Array.from(next) });
      await onChanged();
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not change that size.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      {error &&<CAlert color="danger" style={{ fontSize: '0.8rem' }}>{error}</CAlert>}
      {PACK_ROWS.map((row) => {
        // A size the catalog does not generate is shown DISABLED WITH THE
        // REASON, never hidden: the fix is one click away in catalog settings,
        // and hiding it makes that undiscoverable.
        const ungenerated = !available.has(row.key);
        return (
          <div key={row.key} className="d-flex align-items-center gap-2 mt-1">
            <CFormCheck
              id={`dep-${deployment.id}-rung-${row.key}`}
              checked={row.always || packed.has(row.key)}
              disabled={row.always || ungenerated || busy}
              onChange={() => toggle(row.key)}
              label={(
                <span style={{ fontSize: '0.82rem' }}>
                  {row.label}
                  {row.always && <span className="text-muted"> (always)</span>}
                  {!row.always && !ungenerated && (
                    <span className="text-muted"> +{row.gb} GB / 1,000 photos</span>
                  )}
                  {ungenerated && (
                    <span className="text-muted">
                      {' '}— not generated for this catalog; enable it in catalog settings
                    </span>
                  )}
                  {row.note && !ungenerated && (
                    <span className="text-muted d-block" style={{ fontSize: '0.72rem' }}>
                      {row.note}
                    </span>
                  )}
                </span>
              )}
            />
          </div>
        );
      })}
    </>
  );
};

// The album checklist on the front of the card. Ticking a box saves
// immediately: each add or remove republishes on the backend, so the published
// folder is never out of step with the database, and there is nothing here for
// a Save button to batch.
const DeploymentAlbums = ({ catalogId, deployment, albums, onChanged }) => {
  const [busyId, setBusyId] = useState(null);
  const [error, setError] = useState(null);
  const chosen = new Set((deployment.albums || []).map((a) => a.id));

  const toggle = async (album) => {
    setBusyId(album.id);
    setError(null);
    try {
      if (chosen.has(album.id)) {
        await deploymentAPI.removeAlbum(catalogId, deployment.id, album.id);
      } else {
        await deploymentAPI.addAlbum(catalogId, deployment.id, album.id);
      }
      await onChanged();
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not change that album.');
    } finally {
      setBusyId(null);
    }
  };

  return (
    <>
      {error && <CAlert color="danger" style={{ fontSize: '0.8rem' }}>{error}</CAlert>}
      {albums.length === 0 ? (
        <span className="text-muted">This catalog has no albums yet.</span>
      ) : (
        albums.map((a) => (
          <div key={a.id} className="d-flex align-items-center gap-2 mb-1">
            <CFormCheck
              id={`dep-${deployment.id}-album-${a.id}`}
              checked={chosen.has(a.id)}
              disabled={busyId !== null}
              onChange={() => toggle(a)}
              label={<span style={{ fontSize: '0.85rem' }}>{a.name}</span>}
            />
            {busyId === a.id && <CSpinner size="sm" />}
          </div>
        ))
      )}
    </>
  );
};

// The back of the card: only the sizes a bundle carries.
const DeploymentPackFace = ({ catalogId, deployment, onChanged, onClose }) => (
  <>
    <CCardHeader className="bg-primary text-white d-flex justify-content-between
                            align-items-center py-2">
      <strong style={{ fontSize: '0.9rem' }}>What to pack</strong>
      <CButton color="link" size="sm" className="p-0 text-white" onClick={onClose}>
        <CIcon icon={cilX} />
      </CButton>
    </CCardHeader>
    <CCardBody style={{ overflowY: 'auto', padding: '0.75rem' }}>
      <DeploymentSizes
        catalogId={catalogId}
        deployment={deployment}
        onChanged={onChanged}
      />
      <div style={{ fontSize: '0.75rem', color: '#888', marginTop: 8 }}>
        Each change republishes the deployment straight away.
      </div>
    </CCardBody>
  </>
);

const DeploymentCard = ({
  catalogId, deployment, catalogSlug, albums, onEdit, onDelete, onRepublish, onChanged,
  isOwner, onServers, serversTick,
}) => {
  const urls = urlsFor(catalogSlug, deployment.slug);
  const [flipped, setFlipped] = useState(false);

  return (
    <CCol xs="auto">
      <div style={{ perspective: '1200px', width: CARD_WIDTH, height: CARD_HEIGHT }}>
        <div style={{
          position: 'relative', width: '100%', height: '100%',
          transition: 'transform 0.5s', transformStyle: 'preserve-3d',
          transform: flipped ? 'rotateY(180deg)' : 'none',
        }}>
          {/* ── Front ── */}
          <div style={{
            position: 'absolute', inset: 0, backfaceVisibility: 'hidden',
          }}>
            <CCard className="h-100 shadow-sm d-flex flex-column"
                   style={{ width: '100%', overflow: 'hidden', ...CARD_TOP_CORNERS }}>
              <CardBand image={decorFor(deployment)} label="deployment" />
              <CCardHeader
                className="d-flex justify-content-between align-items-start"
                style={{ minHeight: '64px', flexShrink: 0 }}
              >
                <div className="me-2 overflow-hidden">
                  <div className="fw-semibold text-truncate d-flex align-items-center gap-2">
                    <CIcon
                      icon={deployment.has_password ? cilLockLocked : cilLockUnlocked}
                      style={{ width: 14, height: 14, flexShrink: 0 }}
                      title={deployment.has_password
                        ? 'Password protected'
                        : 'Public — anyone with the link can view it'}
                    />
                    {deployment.name}
                  </div>
                  <div className="mt-1 d-flex gap-1 flex-wrap">
                    <CBadge color="secondary" style={{ fontSize: '0.7rem' }}>
                      {deployment.album_count} album{deployment.album_count === 1 ? '' : 's'}
                    </CBadge>
                    {!deployment.enabled && (
                      <CBadge color="dark" style={{ fontSize: '0.7rem' }}>disabled</CBadge>
                    )}
                    {!deployment.published && (
                      <CBadge color="warning" style={{ fontSize: '0.7rem' }}>not on disk</CBadge>
                    )}
                  </div>
                </div>
                <div className="d-flex flex-shrink-0">
                  <CButton color="danger" variant="ghost" size="sm" className="me-1"
                           title="Delete deployment" onClick={() => onDelete(deployment)}>
                    <CIcon icon={cilTrash} />
                  </CButton>
                  <CButton color="secondary" variant="ghost" size="sm" className="me-1"
                           title="Rewrite the published folder from the database"
                           onClick={() => onRepublish(deployment)}>
                    <CIcon icon={cilSync} />
                  </CButton>
                  <CButton color="success" variant="ghost" size="sm" className="me-1"
                           title="What to pack"
                           onClick={() => setFlipped(true)}>
                    <CIcon icon={cilBriefcase} />
                  </CButton>
                  <CButton color="info" variant="ghost" size="sm"
                           title="Edit deployment" onClick={() => onEdit(deployment)}>
                    <CIcon icon={cilPencil} />
                  </CButton>
                </div>
              </CCardHeader>

              <CCardBody className="d-flex flex-column"
                         style={{ padding: '0.75rem', overflowY: 'auto', minHeight: 0 }}>
                {/* Albums lead: what a deployment CONTAINS is the thing being
                    looked for, and they are ticked in and out right here - adding
                    one is the thing done most often, and it republishes on its
                    own rather than waiting for a save. */}
                <Field label="Albums">
                  <DeploymentAlbums
                    catalogId={catalogId}
                    deployment={deployment}
                    albums={albums}
                    onChanged={onChanged}
                  />
                </Field>

                {deployment.description && (
                  <Field label="Description">{deployment.description}</Field>
                )}

                <Field label="Access">
                  {deployment.has_password
                    ? <span style={{ color: '#2e7d32' }}>Password required</span>
                    : <span style={{ color: '#c62828' }}>Public — anyone with the link</span>}
                </Field>

                {/* Both links, after the rest of the card. Shown in full
                    including the catalog segment — that is what a recipient
                    will paste. The subdomain one is marked because it needs
                    wildcard DNS that does not exist yet. */}
                <Field label="Links">
                  <DeploymentLink url={urls.path} />
                  <DeploymentLink
                    url={urls.subdomain}
                    title={'Needs wildcard DNS — this name does not resolve yet. '
                           + urls.subdomain}
                  />
                </Field>
              </CCardBody>

              {/* Publishing to a server, on the lower right: owner only, as its
                  routes are (deployment-publish-to-server-spec.md section 12). */}
              {isOwner && (
                <div className="mt-auto border-top"
                     style={{ padding: '0.5rem 0.75rem', flexShrink: 0 }}>
                  <ServerLine catalogId={catalogId} deployment={deployment}
                              refreshKey={serversTick}
                              onOpen={(serverId, kind) => onServers(deployment, serverId, kind)} />
                </div>
              )}
            </CCard>
          </div>

          {/* ── Back ── */}
          <div style={{
            position: 'absolute', inset: 0, backfaceVisibility: 'hidden',
            transform: 'rotateY(180deg)',
          }}>
            <CCard className="h-100 shadow-sm border-primary d-flex flex-column"
                   style={{ width: '100%', overflow: 'hidden', ...CARD_TOP_CORNERS }}>
              {/* Mounted only while flipped: otherwise every card's pack face
                  would be live in the DOM at once. */}
              {flipped && (
                <DeploymentPackFace
                  catalogId={catalogId}
                  deployment={deployment}
                  onChanged={onChanged}
                  onClose={() => setFlipped(false)}
                />
              )}
            </CCard>
          </div>
        </div>
      </div>
    </CCol>
  );
};

const Deployments = () => {
  const { activeCatalog } = useCatalog();
  const [state, setState] = useState({ deployments: [], catalog_slug: null,
                                       catalog_slug_problem: null });
  const [albums, setAlbums] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  // The Servers panel: {deployment, serverId, kind} while open. serversTick
  // makes the cards' server lines read again after a run or a change.
  const [servers, setServers] = useState(null);
  const [serversTick, setServersTick] = useState(0);
  const isOwner = !activeCatalog?.role || activeCatalog.role === 'owner';

  const [editing, setEditing] = useState(null);   // deployment, or {} for new
  const [form, setForm] = useState({ name: '', slug: '', description: '', enabled: true });
  const [selectedAlbums, setSelectedAlbums] = useState([]);
  const [password, setPassword] = useState('');
  const [clearPassword, setClearPassword] = useState(false);
  const [saving, setSaving] = useState(false);
  const [formError, setFormError] = useState(null);

  const catalogId = activeCatalog?.id;

  const load = useCallback(async () => {
    if (!catalogId) { setLoading(false); return; }
    try {
      const [dep, alb] = await Promise.all([
        deploymentAPI.getAll(catalogId),
        albumAPI.getAll(catalogId),
      ]);
      setState(dep.data);
      setAlbums(alb.data || []);
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load deployments.');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  const openNew = () => {
    setForm({ name: '', slug: '', description: '', enabled: true });
    setSelectedAlbums([]);
    setPassword('');
    setClearPassword(false);
    setFormError(null);
    setEditing({});
  };

  const openEdit = (deployment) => {
    setForm({
      name: deployment.name,
      slug: deployment.slug,
      description: deployment.description || '',
      enabled: deployment.enabled,
    });
    setSelectedAlbums(deployment.albums.map((a) => a.id));
    setPassword('');
    setClearPassword(false);
    setFormError(null);
    setEditing(deployment);
  };

  const onNameChange = (value) => {
    setForm((prev) => ({
      ...prev,
      name: value,
      // Follows the name only while creating. An existing slug is in links
      // people already have; it changes when the user changes it, and warns.
      slug: editing?.id ? prev.slug : slugify(value),
    }));
  };

  const save = async () => {
    setSaving(true);
    setFormError(null);
    try {
      let deployment = editing;
      if (editing.id) {
        if (form.slug !== editing.slug && !window.confirm(
          `Change this gallery's address from "${editing.slug}" to "${form.slug}"?\n\n`
          + 'Any link already shared will stop working.')) {
          setSaving(false);
          return;
        }
        const res = await deploymentAPI.update(catalogId, editing.id, form);
        deployment = res.data;
      } else {
        const res = await deploymentAPI.create(catalogId, form);
        deployment = res.data;
      }

      // Albums, as a diff. Each call republishes on the backend, so the folder
      // is never out of step with the database at any point in between.
      const before = new Set((editing.albums || []).map((a) => a.id));
      const after = new Set(selectedAlbums);
      for (const id of after) {
        if (!before.has(id)) await deploymentAPI.addAlbum(catalogId, deployment.id, id);
      }
      for (const id of before) {
        if (!after.has(id)) await deploymentAPI.removeAlbum(catalogId, deployment.id, id);
      }

      if (clearPassword) {
        await deploymentAPI.setPassword(catalogId, deployment.id, null);
      } else if (password) {
        await deploymentAPI.setPassword(catalogId, deployment.id, password);
      }

      setEditing(null);
      load();
    } catch (e) {
      setFormError(e?.response?.data?.error || 'Could not save that deployment.');
    } finally {
      setSaving(false);
    }
  };

  const remove = async (deployment) => {
    if (!window.confirm(
      `Delete the deployment "${deployment.name}"?\n\n`
      + 'Its published folder is removed and its links stop working. The albums '
      + 'themselves are not touched.')) return;
    try {
      await deploymentAPI.remove(catalogId, deployment.id);
      load();
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not delete that deployment.');
    }
  };

  const republish = async (deployment) => {
    try {
      const res = await deploymentAPI.publish(catalogId, deployment.id);
      const skipped = res.data.skipped || [];
      setNotice(
        `Published "${deployment.name}": ${res.data.linked.length} album(s)`
        + (skipped.length
          ? `. Skipped ${skipped.map((s) => `${s.name} (${s.reason})`).join(', ')}.`
          : '.'),
      );
      load();
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not publish that deployment.');
    }
  };

  if (!activeCatalog) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="info">Select a catalog to see its deployments.</CAlert>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="px-4 py-4">
      {/* A plain heading with the cards on the page background, matching Users,
          Catalogs and Storage. */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Deployments</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Shareable galleries, each with the albums it publishes beside it
          </div>
        </div>
      </div>

      <CCard className="mb-4" style={{ backgroundColor: 'white' }}>
        <CCardBody className="d-flex justify-content-between align-items-center py-3">
          <CButton color="primary" size="sm" onClick={openNew}
                   disabled={!!state.catalog_slug_problem}>
            <CIcon icon={cilPlus} className="me-2" />
            New Deployment
          </CButton>
          {state.catalog_slug && (
            <div style={{ fontSize: '0.82rem', color: '#666' }}>
              <CIcon icon={cilImage} className="me-1" style={{ width: 13, height: 13 }} />
              Published under <code>/gallery/{state.catalog_slug}/</code>
            </div>
          )}
        </CCardBody>
      </CCard>

      {/* A catalog with no URL name cannot host a deployment. Said once, here,
          rather than letting every create fail with the same message. */}
      {state.catalog_slug_problem && (
        <CAlert color="warning">
          <strong>This catalog has no URL name yet.</strong>
          <div style={{ fontSize: '0.85rem', marginTop: 4 }}>
            {state.catalog_slug_problem}
          </div>
        </CAlert>
      )}

      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}
      {notice && <CAlert color="success" dismissible onClose={() => setNotice(null)}>{notice}</CAlert>}

      {loading ? (
        <div className="text-center"><CSpinner color="primary" /></div>
      ) : state.deployments.length === 0 ? (
        <div className="text-center py-5 text-muted" style={{ fontSize: '0.9rem' }}>
          No deployments yet. Make one, then tick the albums it should publish
          on the front of its card.
        </div>
      ) : (
        /* One wrapping row of deployment cards. Each card ticks its albums in
           and out on its front and chooses what to pack on its back; the
           album cards themselves are on the Albums page. */
        <CRow className="g-3 align-items-start">
          {state.deployments.map((d) => (
            <DeploymentCard
              key={d.id}
              catalogId={catalogId}
              deployment={d}
              catalogSlug={state.catalog_slug}
              albums={albums}
              onEdit={openEdit}
              onDelete={remove}
              onRepublish={republish}
              onChanged={load}
              isOwner={isOwner}
              serversTick={serversTick}
              onServers={(deployment, serverId, kind) => setServers({ deployment, serverId, kind })}
            />
          ))}
        </CRow>
      )}

      <CModal visible={editing !== null} onClose={() => setEditing(null)} size="lg">
        <CModalHeader>
          <CModalTitle>{editing?.id ? 'Edit deployment' : 'New deployment'}</CModalTitle>
        </CModalHeader>
        <CModalBody>
          {formError && <CAlert color="danger" style={{ fontSize: '0.85rem' }}>{formError}</CAlert>}
          <CForm>
            <div className="mb-3">
              <CFormLabel>Name *</CFormLabel>
              <CFormInput
                value={form.name}
                onChange={(e) => onNameChange(e.target.value)}
                placeholder="Summer 2019"
              />
            </div>

            <div className="mb-3">
              <CFormLabel>Address</CFormLabel>
              <CFormInput
                value={form.slug}
                onChange={(e) => setForm({ ...form, slug: e.target.value })}
                placeholder="summer-2019"
                pattern="[a-z0-9][a-z0-9-]{1,38}[a-z0-9]"
              />
              <small className="text-muted">
                <code>/gallery/{state.catalog_slug || 'catalog'}/{form.slug || 'album'}</code>
              </small>
              {editing?.id && form.slug !== editing.slug && (
                <CAlert color="warning" className="mt-2 mb-0 py-2" style={{ fontSize: '0.82rem' }}>
                  Any link already shared with <code>{editing.slug}</code> will stop working.
                </CAlert>
              )}
            </div>

            <div className="mb-3">
              <CFormLabel>Description</CFormLabel>
              <CFormTextarea
                rows="2"
                value={form.description}
                onChange={(e) => setForm({ ...form, description: e.target.value })}
              />
            </div>

            {/* A plain checklist — no catalog column, because there is only one
                catalog in play. That is the visible dividend of a deployment
                belonging to a single catalog. */}
            <div className="mb-3">
              <CFormLabel>Albums</CFormLabel>
              {albums.length === 0 ? (
                <div className="text-muted" style={{ fontSize: '0.85rem' }}>
                  This catalog has no albums yet.
                </div>
              ) : (
                <div style={{ maxHeight: 220, overflowY: 'auto', border: '1px solid #e0e0e0',
                              borderRadius: 6, padding: '0.5rem' }}>
                  {albums.map((album) => (
                    <CFormCheck
                      key={album.id}
                      id={`deployment-album-${album.id}`}
                      label={album.name}
                      checked={selectedAlbums.includes(album.id)}
                      onChange={(e) => setSelectedAlbums((prev) => (
                        e.target.checked
                          ? [...prev, album.id]
                          : prev.filter((id) => id !== album.id)
                      ))}
                    />
                  ))}
                </div>
              )}
            </div>

            <div className="mb-3">
              <CFormLabel>Password</CFormLabel>
              <CFormInput
                type="password"
                autoComplete="new-password"
                value={password}
                disabled={clearPassword}
                onChange={(e) => setPassword(e.target.value)}
                placeholder={editing?.has_password
                  ? 'Leave blank to keep the current password'
                  : 'Leave blank for a public gallery'}
              />
              {editing?.has_password && (
                <CFormCheck
                  className="mt-2"
                  id="deployment-clear-password"
                  label="Remove the password and make this gallery public"
                  checked={clearPassword}
                  onChange={(e) => setClearPassword(e.target.checked)}
                />
              )}
              <small className="text-muted d-block mt-1">
                Without one, anyone with the link can view every photo in it.
              </small>
            </div>

            <CFormCheck
              id="deployment-enabled"
              label="Enabled"
              checked={form.enabled}
              onChange={(e) => setForm({ ...form, enabled: e.target.checked })}
            />
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setEditing(null)}>
            Cancel
          </CButton>
          <CButton color="primary" onClick={save} disabled={saving || !form.name.trim()}>
            {saving ? <CSpinner size="sm" /> : 'Save'}
          </CButton>
        </CModalFooter>
      </CModal>

      <ServersModal
        catalogId={catalogId}
        deployment={servers?.deployment}
        visible={!!servers}
        focusServer={servers?.serverId}
        startKind={servers?.kind}
        onClose={() => setServers(null)}
        onChanged={() => setServersTick((n) => n + 1)}
      />
    </CContainer>
  );
};

export default Deployments;
