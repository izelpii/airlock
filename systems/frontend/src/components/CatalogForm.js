import React, { useState, useEffect } from 'react';
import {
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CModalFooter,
  CButton,
  CForm,
  CFormLabel,
  CFormInput,
  CFormTextarea,
  CFormSelect,
  CFormCheck,
  CFormSwitch,
  CSpinner,
  CAlert,
} from '@coreui/react';
import { catalogAPI } from '../services/api';
import { useAuth } from '../context/AuthContext';

// Mirrors systems/backend/slugs.py, so the value shown while typing is the one
// the backend would derive if the field were left empty. Kept deliberately
// small: the backend is what validates, this is only what previews.
// The grammar the BACKEND enforces (systems/backend/slugs.py) and that nginx's
// wildcard server_name matches. Kept here only to give the user the complaint
// before the request is made; the backend is still what validates.
const SYSTEM_NAME_RE = /^[a-z0-9][a-z0-9-]{1,38}[a-z0-9]$/;

const systemNameComplaint = (value) => {
  if (!value) return 'A system name is required.';
  if (/[A-Z]/.test(value)) return 'Use lowercase only — this is part of a web address.';
  if (value.includes('_')) return 'Underscores are not allowed: a web address cannot contain one. Use a hyphen.';
  if (/\s/.test(value)) return 'Spaces are not allowed. Use a hyphen.';
  if (value.length < 3) return 'At least 3 characters.';
  if (value.length > 40) return 'At most 40 characters.';
  if (!SYSTEM_NAME_RE.test(value)) {
    return 'Only lowercase letters, digits and hyphens, starting and ending with a letter or digit.';
  }
  return null;
};

const slugify = (name) =>
  (name || '')
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, '-')
    .replace(/^-+|-+$/g, '')
    .slice(0, 40)
    .replace(/-+$/, '');

// Mirrors the backend's IMAGE_RUNGS minus the mandatory base. `label` is only
// needed where "<key> px" would be wrong, which is the uncapped rung - it has
// no width to name, because its width is whatever the original's was.
const IMAGE_RUNG_OPTIONS = [
  { key: '1600', gbPerThousand: 0.33 },
  { key: '2048', gbPerThousand: 0.48 },
  { key: '2560', gbPerThousand: 0.66 },
  // Measured over a 40-photo sample of the reference catalog (mixed HEIC and
  // JPG, 3024..6000 px wide) at the same quality 90 the other rungs use. Four
  // to five times the 2048 rung, which is why it says so on the box.
  { key: 'full', label: 'Full resolution', gbPerThousand: 2.26 },
];

const CatalogForm = ({ visible, onClose, catalog, onSuccess }) => {
  const [formData, setFormData] = useState({
    name: '',
    // ONE value behind two: the catalog's URL name (slug) and the last segment
    // of its folder. handleSubmit expands it into both.
    system_name: '',
    description: '',
    target_directory: '',
    file_strategy: 'local',
    similarity_metric: 'cosine',
    // Animals, on by default. A household with pets should not have to find a
    // setting to photograph them; one without pets loses a detector pass it can
    // turn off here. The default is applied by the backend too, for anything
    // that creates a catalog without going through this form.
    pets_enabled: true,
    image_rungs: [],
    ingest_mode: 'copy',
    source_root: '',
    // SCP parameters
    scp_username: '',
    scp_host: '',
    scp_port: 22,
    // FTP parameters
    ftp_host: '',
    ftp_username: '',
    ftp_password: '',
    ftp_port: 21,
    // Box parameters
    box_client_id: '',
    box_client_secret: '',
    box_access_token: '',
    // S3 parameters
    s3_bucket_name: '',
    s3_aws_access_key_id: '',
    s3_aws_secret_access_key: '',
    s3_region_name: '',
  });
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  // How the catalog handles files is fixed once it has imported anything. The
  // backend refuses the change either way; disabling the control here is so the
  // user sees why rather than meeting a 409.
  const [modeLocked, setModeLocked] = useState(false);
  // Move mode deletes files outside the application's own folders, so only an
  // administrator may choose it (ingest-move-spec.md 8.2). No user at all
  // means sign-in is off, and the backend allows it.
  const { user, isAdmin } = useAuth();
  const mayMove = !user || isAdmin;

  useEffect(() => {
    if (!catalog?.id) { setModeLocked(false); return; }
    catalogAPI.getById(catalog.id)
      .then((res) => setModeLocked(!!res.data?.has_files))
      .catch(() => setModeLocked(true));   // unknown: assume locked, the safe way round
  }, [catalog?.id]);

  useEffect(() => {
    if (catalog) {
      setFormData({
        name: catalog.name || '',
        // An existing catalog is described by its slug; the folder was derived
        // from it at creation and is not editable here.
        system_name: catalog.slug || '',
        description: catalog.description || '',
        target_directory: catalog.target_directory || '',
        file_strategy: catalog.file_strategy || 'local',
        similarity_metric: catalog.similarity_metric || 'cosine',
        // Absent means off: a catalog created before the switch existed has no
        // property, and reading absence as "on" here would show a switch that
        // disagrees with what the catalog actually does.
        pets_enabled: !!catalog.pets_enabled,
        image_rungs: catalog.image_rungs || [],
        ingest_mode: catalog.ingest_mode || 'copy',
        source_root: catalog.source_root || '',
        // SCP parameters
        scp_username: catalog.scp_username || '',
        scp_host: catalog.scp_host || '',
        scp_port: catalog.scp_port || 22,
        // FTP parameters
        ftp_host: catalog.ftp_host || '',
        ftp_username: catalog.ftp_username || '',
        ftp_password: catalog.ftp_password || '',
        ftp_port: catalog.ftp_port || 21,
        // Box parameters
        box_client_id: catalog.box_client_id || '',
        box_client_secret: catalog.box_client_secret || '',
        box_access_token: catalog.box_access_token || '',
        // S3 parameters
        s3_bucket_name: catalog.s3_bucket_name || '',
        s3_aws_access_key_id: catalog.s3_aws_access_key_id || '',
        s3_aws_secret_access_key: catalog.s3_aws_secret_access_key || '',
        s3_region_name: catalog.s3_region_name || '',
      });
    } else {
      setFormData({
        name: '',
        system_name: '',
        description: '',
        target_directory: '',
        file_strategy: 'local',
        similarity_metric: 'cosine',
        pets_enabled: true,
        image_rungs: [],
        ingest_mode: 'copy',
        source_root: '',
        // SCP parameters
        scp_username: '',
        scp_host: '',
        scp_port: 22,
        // FTP parameters
        ftp_host: '',
        ftp_username: '',
        ftp_password: '',
        ftp_port: 21,
        // Box parameters
        box_client_id: '',
        box_client_secret: '',
        box_access_token: '',
        // S3 parameters
        s3_bucket_name: '',
        s3_aws_access_key_id: '',
        s3_aws_secret_access_key: '',
        s3_region_name: '',
      });
    }
    setError(null);
  }, [catalog, visible]);

  // Extras only - the mandatory 1200 is never in this list. It lives in the
  // backend's IMAGE_RUNGS, so no edit here can produce a catalog without it.
  const toggleRung = (key) => {
    setFormData((prev) => ({
      ...prev,
      image_rungs: prev.image_rungs.includes(key)
        ? prev.image_rungs.filter((r) => r !== key)
        : [...prev.image_rungs, key],
    }));
  };

  const rungCost = (() => {
    const total = IMAGE_RUNG_OPTIONS
      .filter((r) => formData.image_rungs.includes(r.key))
      .reduce((sum, r) => sum + r.gbPerThousand, 0);
    return total ? `${total.toFixed(2)} GB` : null;
  })();

  const handleChange = (e) => {
    const { name, value } = e.target;
    if (name === 'ingest_mode' && value === 'move' && formData.ingest_mode !== 'move'
        && !window.confirm('Imports from this catalog will move and delete files in the '
                           + 'folders you import from.\n\nPhotos and videos are moved into '
                           + 'the catalog; duplicates are gathered in a DUPS folder; '
                           + 'anything else is left where it is. Continue?')) {
      return;
    }
    setFormData((prev) => {
      const next = { ...prev, [name]: value };
      // While CREATING, the URL name follows the catalog name as it is typed -
      // the same derivation the backend applies when none is sent, so what is
      // shown is what will be saved. Never while editing: an existing slug is
      // in links people already have, and silently rewriting it on a rename is
      // exactly the breakage the warning below is about.
      // While CREATING, the system name follows the catalog name as it is
      // typed. Never while editing: it names a folder that already exists.
      if (name === 'name' && !catalog) next.system_name = slugify(value);
      return next;
    });
  };

  const systemNameError = formData.system_name
    ? systemNameComplaint(formData.system_name)
    : null;

  const handleSubmit = async (e) => {
    e.preventDefault();
    setLoading(true);
    setError(null);

    // One field, two stored values. The backend is unchanged: it still receives
    // a slug and a target_directory, they are simply no longer typed separately.
    const complaint = systemNameComplaint(formData.system_name);
    if (complaint) {
      setError(complaint);
      setLoading(false);
      return;
    }
    const payload = {
      ...formData,
      slug: formData.system_name,
      target_directory: `/catalogs/${formData.system_name}`,
    };
    delete payload.system_name;

    try {
      if (catalog) {
        // No confirmation needed any more: the system name is disabled once the
        // catalog exists, so neither the slug nor the folder can change here.
        // That is also what removed the "this breaks every shared link" warning
        // - the situation it warned about can no longer arise from this form.
        await catalogAPI.update(catalog.id, payload);
      } else {
        await catalogAPI.create(payload);
      }
      onSuccess();
      onClose();
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to save catalog');
      console.error(err);
    } finally {
      setLoading(false);
    }
  };

  const renderStrategyFields = () => {
    switch (formData.file_strategy) {
      case 'scp':
        return (
          <>
            <CAlert color="info" className="mb-3">
              <strong>SCP Configuration</strong> - Secure Copy Protocol for remote file transfer
            </CAlert>
            <div className="mb-3">
              <CFormLabel htmlFor="scp_username">SCP Username *</CFormLabel>
              <CFormInput
                type="text"
                id="scp_username"
                name="scp_username"
                value={formData.scp_username}
                onChange={handleChange}
                required
                placeholder="Remote username"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="scp_host">SCP Host *</CFormLabel>
              <CFormInput
                type="text"
                id="scp_host"
                name="scp_host"
                value={formData.scp_host}
                onChange={handleChange}
                required
                placeholder="Remote host address"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="scp_port">SCP Port</CFormLabel>
              <CFormInput
                type="number"
                id="scp_port"
                name="scp_port"
                value={formData.scp_port}
                onChange={handleChange}
                placeholder="22"
              />
            </div>
          </>
        );
      case 'ftp':
        return (
          <>
            <CAlert color="info" className="mb-3">
              <strong>FTP Configuration</strong> - File Transfer Protocol
            </CAlert>
            <div className="mb-3">
              <CFormLabel htmlFor="ftp_host">FTP Host *</CFormLabel>
              <CFormInput
                type="text"
                id="ftp_host"
                name="ftp_host"
                value={formData.ftp_host}
                onChange={handleChange}
                required
                placeholder="FTP server address"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="ftp_username">FTP Username *</CFormLabel>
              <CFormInput
                type="text"
                id="ftp_username"
                name="ftp_username"
                value={formData.ftp_username}
                onChange={handleChange}
                required
                placeholder="FTP username"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="ftp_password">FTP Password *</CFormLabel>
              <CFormInput
                type="password"
                id="ftp_password"
                name="ftp_password"
                value={formData.ftp_password}
                onChange={handleChange}
                required
                placeholder="FTP password"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="ftp_port">FTP Port</CFormLabel>
              <CFormInput
                type="number"
                id="ftp_port"
                name="ftp_port"
                value={formData.ftp_port}
                onChange={handleChange}
                placeholder="21"
              />
            </div>
          </>
        );
      case 'box':
        return (
          <>
            <CAlert color="info" className="mb-3">
              <strong>Box Configuration</strong> - Box cloud storage
            </CAlert>
            <div className="mb-3">
              <CFormLabel htmlFor="box_client_id">Box Client ID *</CFormLabel>
              <CFormInput
                type="text"
                id="box_client_id"
                name="box_client_id"
                value={formData.box_client_id}
                onChange={handleChange}
                required
                placeholder="Box application client ID"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="box_client_secret">Box Client Secret *</CFormLabel>
              <CFormInput
                type="password"
                id="box_client_secret"
                name="box_client_secret"
                value={formData.box_client_secret}
                onChange={handleChange}
                required
                placeholder="Box application client secret"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="box_access_token">Box Access Token</CFormLabel>
              <CFormInput
                type="password"
                id="box_access_token"
                name="box_access_token"
                value={formData.box_access_token}
                onChange={handleChange}
                placeholder="Box access token (optional)"
              />
            </div>
          </>
        );
      case 's3':
        return (
          <>
            <CAlert color="info" className="mb-3">
              <strong>S3 Configuration</strong> - Amazon S3 cloud storage
            </CAlert>
            <div className="mb-3">
              <CFormLabel htmlFor="s3_bucket_name">S3 Bucket Name *</CFormLabel>
              <CFormInput
                type="text"
                id="s3_bucket_name"
                name="s3_bucket_name"
                value={formData.s3_bucket_name}
                onChange={handleChange}
                required
                placeholder="my-bucket-name"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="s3_aws_access_key_id">AWS Access Key ID</CFormLabel>
              <CFormInput
                type="text"
                id="s3_aws_access_key_id"
                name="s3_aws_access_key_id"
                value={formData.s3_aws_access_key_id}
                onChange={handleChange}
                placeholder="AWS access key (optional if using IAM role)"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="s3_aws_secret_access_key">AWS Secret Access Key</CFormLabel>
              <CFormInput
                type="password"
                id="s3_aws_secret_access_key"
                name="s3_aws_secret_access_key"
                value={formData.s3_aws_secret_access_key}
                onChange={handleChange}
                placeholder="AWS secret key (optional if using IAM role)"
              />
            </div>
            <div className="mb-3">
              <CFormLabel htmlFor="s3_region_name">AWS Region</CFormLabel>
              <CFormInput
                type="text"
                id="s3_region_name"
                name="s3_region_name"
                value={formData.s3_region_name}
                onChange={handleChange}
                placeholder="us-east-1"
              />
            </div>
          </>
        );
      case 'local':
      default:
        return (
          <CAlert color="info" className="mb-3">
            <strong>Local Storage</strong> - Files will be stored on the local filesystem
          </CAlert>
        );
    }
  };

  return (
    <CModal visible={visible} onClose={onClose} size="lg">
      <CModalHeader>
        <CModalTitle>{catalog ? 'Edit Catalog' : 'Create New Catalog'}</CModalTitle>
      </CModalHeader>
      <CForm onSubmit={handleSubmit}>
        <CModalBody>
          {error && (
            <div className="alert alert-danger" role="alert">
              {error}
            </div>
          )}
          <div className="mb-3">
            <CFormLabel htmlFor="name">Catalog Name *</CFormLabel>
            <CFormInput
              type="text"
              id="name"
              name="name"
              value={formData.name}
              onChange={handleChange}
              required
              placeholder="Enter catalog name"
            />
          </div>
          {/* ── System name ────────────────────────────────────────────────
              ONE field behind TWO stored values: the catalog's URL name and its
              folder on disk. They were always required to agree in practice, and
              asking for them separately invited them to disagree.

              The grammar is the stricter of the two. A folder name would accept
              almost anything; the URL name becomes a DNS label under the
              subdomain form (family.airlock.lan), and a hostname allows only
              letters, digits and HYPHENS — no underscores, no capitals, no
              spaces. So the field enforces the hostname rule and the folder
              inherits it. */}
          <div className="mb-3">
            <CFormLabel htmlFor="system_name">System name *</CFormLabel>
            <CFormInput
              type="text"
              id="system_name"
              name="system_name"
              value={formData.system_name}
              onChange={handleChange}
              required
              disabled={!!catalog}
              placeholder="family-photos"
              pattern="[a-z0-9][a-z0-9-]{1,38}[a-z0-9]"
              autoComplete="off"
            />
            {catalog ? (
              <small className="text-muted">
                Fixed once the catalog exists — it names the folder holding the
                data, and changing it here would point the catalog at a directory
                that does not exist.
              </small>
            ) : (
              <small className="text-muted">
                Lowercase letters, digits and hyphens — no spaces, capitals or
                underscores, because this is also used as a web address.
              </small>
            )}
            {systemNameError && (
              <CAlert color="warning" className="mt-2 mb-0 py-2" style={{ fontSize: '0.82rem' }}>
                {systemNameError}
              </CAlert>
            )}
            {/* Show both things this one field decides, so nothing is hidden. */}
            <div className="mt-2" style={{ fontSize: '0.78rem', color: '#666' }}>
              <div>
                Folder: <code>/catalogs/{formData.system_name || '…'}</code>
              </div>
              <div>
                Albums published at{' '}
                <code>/gallery/{formData.system_name || '…'}/&lt;album&gt;</code>
              </div>
            </div>
          </div>

          {/* No Owner field. It was free text from before this application had
              users; a catalog now belongs to whoever creates it, and the backend
              takes that from the session rather than from the form. */}
          <div className="mb-3">
            <CFormLabel htmlFor="description">Description</CFormLabel>
            <CFormTextarea
              id="description"
              name="description"
              value={formData.description}
              onChange={handleChange}
              rows="3"
              placeholder="Enter catalog description"
            />
          </div>
          <div className="mb-3">
            <CFormLabel htmlFor="file_strategy">File Storage Strategy *</CFormLabel>
            <CFormSelect
              id="file_strategy"
              name="file_strategy"
              value={formData.file_strategy}
              onChange={handleChange}
              required
            >
              <option value="local">Local Storage</option>
              <option value="scp">SCP (Secure Copy)</option>
              <option value="ftp">FTP (File Transfer Protocol)</option>
              <option value="box">Box Cloud Storage</option>
              <option value="s3">Amazon S3</option>
            </CFormSelect>
            <small className="text-muted">
              Select how files will be stored for this catalog
            </small>
          </div>
          {/* How this catalog handles files (file-ingestion-spec.md section 8).
              Fixed once anything has been imported: the paths already stored are
              relative to the root the catalog was created with, so changing the
              mode later would make them resolve against the wrong one. */}
          <div className="mb-3">
            <CFormLabel>How should this catalog handle your files? *</CFormLabel>
            <CFormCheck
              type="radio"
              name="ingest_mode"
              id="ingest_mode_copy"
              value="copy"
              checked={formData.ingest_mode === 'copy'}
              onChange={handleChange}
              disabled={modeLocked}
              label={
                <>
                  <strong>Copy files into the catalog</strong>
                  <div className="text-muted" style={{ fontSize: '0.82rem' }}>
                    The catalog keeps its own dated, renamed copy of every file.
                  </div>
                </>
              }
            />
            <CFormCheck
              type="radio"
              name="ingest_mode"
              id="ingest_mode_reference"
              value="reference"
              checked={formData.ingest_mode === 'reference'}
              onChange={handleChange}
              disabled={modeLocked || formData.file_strategy !== 'local'}
              label={
                <>
                  <strong>Index files where they are</strong>
                  <div className="text-muted" style={{ fontSize: '0.82rem' }}>
                    Nothing is copied, moved or renamed. Your originals stay exactly
                    where they are.
                  </div>
                </>
              }
            />
            <CFormCheck
              type="radio"
              name="ingest_mode"
              id="ingest_mode_move"
              value="move"
              checked={formData.ingest_mode === 'move'}
              onChange={handleChange}
              disabled={modeLocked || formData.file_strategy !== 'local' || !mayMove}
              label={
                <>
                  <strong>Move files into the catalog</strong>
                  <div className="text-muted" style={{ fontSize: '0.82rem' }}>
                    Photos and videos are moved and renamed into the catalog, so nothing
                    is stored twice. Duplicates are gathered in a <code>DUPS</code> folder
                    in the source; anything else stays where it is. Only storage routes
                    marked &quot;imports may move files&quot; can be imported from.
                    {!mayMove && ' Administrators only.'}
                  </div>
                </>
              }
            />
            {modeLocked && (
              <small className="text-muted d-block mt-1">
                This cannot be changed now that the catalog has imported files —
                the paths already stored are relative to the folder it was created
                with.
              </small>
            )}
            {!modeLocked && formData.file_strategy !== 'local' && (
              <small className="text-muted d-block mt-1">
                Indexing in place and moving are only available with local storage.
              </small>
            )}
          </div>

          {formData.ingest_mode === 'reference' && (
            <div className="mb-3">
              <CFormLabel htmlFor="source_root">Source Folder *</CFormLabel>
              <CFormInput
                type="text"
                id="source_root"
                name="source_root"
                value={formData.source_root}
                onChange={handleChange}
                placeholder="/photos/my-library"
                required={!catalog}
              />
              <small className="text-muted">
                The folder holding the photos to index — not the drive or home
                directory above it. It must also be reachable from inside the
                backend container, which means mounting it there.
              </small>
            </div>
          )}

          <div className="mb-3">
            <CFormLabel htmlFor="similarity_metric">Face Recognition Metric</CFormLabel>
            <CFormSelect
              id="similarity_metric"
              name="similarity_metric"
              value={formData.similarity_metric}
              onChange={handleChange}
            >
              <option value="cosine">Cosine — geometric similarity (default)</option>
              <option value="contextual">Contextual — fuses appearance with event, time &amp; co-occurrence</option>
            </CFormSelect>
            <small className="text-muted">
              Metric used in the Identify People workflow to rank face match candidates.
            </small>
          </div>

          {/* Animals. Beside the face metric because it is the same kind of
              question - what this catalog recognises - and on by default,
              so a household with pets never has to go looking for it. */}
          <div className="mb-3">
            <CFormLabel>Pets</CFormLabel>
            <CFormSwitch
              id="pets_enabled"
              name="pets_enabled"
              label={formData.pets_enabled
                ? 'Look for animals in this catalog'
                : 'Do not look for animals in this catalog'}
              checked={!!formData.pets_enabled}
              onChange={(e) => setFormData((prev) => ({
                ...prev, pets_enabled: e.target.checked,
              }))}
            />
            <small className="text-muted">
              {catalog
                ? 'Turning this off removes the two pet workflows. Nothing is '
                  + 'deleted — the animals already found, and any you have named, '
                  + 'stay where they are.'
                : 'Adds Measure and Match Pets and Group Unnamed Pets, which run '
                  + 'after Find Faces. Turn it off for a catalog with no animals '
                  + 'in it and no pet model is loaded at all.'}
            </small>
          </div>

          <div className="mb-3">
            <CFormLabel>Image sizes</CFormLabel>
            <div className="d-flex flex-wrap gap-3">
              {/* The mandatory rung is shown checked and DISABLED rather than
                  hidden. A user who cannot see the floor cannot understand what
                  the extras are relative to. */}
              <CFormCheck
                id="rung-1200"
                label="1200 px (always)"
                checked
                disabled
                readOnly
              />
              {IMAGE_RUNG_OPTIONS.map((rung) => (
                <CFormCheck
                  key={rung.key}
                  id={`rung-${rung.key}`}
                  label={rung.label || `${rung.key} px`}
                  checked={formData.image_rungs.includes(rung.key)}
                  onChange={() => toggleRung(rung.key)}
                />
              ))}
            </div>
            <small className="text-muted d-block mt-1">
              1200&nbsp;px is always generated and cannot be turned off — it is
              what this application displays and what face detection reads.
              Larger sizes let a deployed gallery show a sharp photo full-screen,
              and Full resolution is the original's own pixels as a JPG — what a
              visitor downloading a photo should get.
              {rungCost && <> Selected: about <strong>{rungCost}</strong> per 1,000 photos.</>}
            </small>
            <small className="text-muted d-block">
              Larger sizes are generated for photos imported from now on.
              Existing photos need the backfill:{' '}
              <code>python backend/migrate_add_image_rungs.py</code>
            </small>
          </div>

          {renderStrategyFields()}
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={onClose} disabled={loading}>
            Cancel
          </CButton>
          <CButton color="primary" type="submit" disabled={loading}>
            {loading ? (
              <>
                <CSpinner size="sm" className="me-2" />
                Saving...
              </>
            ) : (
              catalog ? 'Update Catalog' : 'Create Catalog'
            )}
          </CButton>
        </CModalFooter>
      </CForm>
    </CModal>
  );
};

export default CatalogForm;
