import React, { useState, useEffect, useMemo } from 'react';
import {
  CModal, CModalHeader, CModalTitle, CModalBody, CModalFooter,
  CButton, CAlert, CSpinner, CForm, CFormLabel, CFormInput, CFormTextarea, CFormSelect,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilCalendar } from '@coreui/icons';
import { albumAPI } from '../services/api';

// ─── The "a year in events" template ─────────────────────────────────────────
// documentation/specs/template-albums-spec.md §7.2.
//
// Its own window rather than a fourth mode in "Create an album": it asks for a
// year instead of a way of building, it previews what it will make, and one
// request builds the whole album - so its loading, busy and result states are
// all different. Month names come from the backend with the preview, which is
// what keeps them identical to the categories it creates.

const plural = (n, one, many = `${one}s`) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

const YearAlbumTemplateModal = ({ visible, catalogId, deployments, onClose, onCreated }) => {
  const [options, setOptions] = useState(null);     // GET …/album-templates/year
  const [loadError, setLoadError] = useState(null);
  const [year, setYear] = useState('');
  const [name, setName] = useState('');
  // The name follows the year until the user types in it, then it is theirs.
  const [nameEdited, setNameEdited] = useState(false);
  const [description, setDescription] = useState('');
  const [deploymentId, setDeploymentId] = useState('');
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // Fresh every time it opens: events may have been extracted since.
  useEffect(() => {
    if (!visible || !catalogId) return undefined;
    let cancelled = false;
    setOptions(null);
    setLoadError(null);
    setError(null);
    setDescription('');
    setDeploymentId('');
    setNameEdited(false);
    albumAPI.getYearTemplate(catalogId)
      .then((res) => {
        if (cancelled) return;
        const data = res.data || { years: [] };
        setOptions(data);
        const newest = data.years?.[0]?.year;
        setYear(newest ? String(newest) : '');
        setName(newest ? String(newest) : '');
      })
      .catch((e) => {
        if (!cancelled) setLoadError(e?.response?.data?.error || 'Could not read this catalog\'s events.');
      });
    return () => { cancelled = true; };
  }, [visible, catalogId]);

  const chosen = useMemo(
    () => options?.years?.find((y) => String(y.year) === year) || null,
    [options, year],
  );

  const chooseYear = (value) => {
    setYear(value);
    if (!nameEdited) setName(value);
  };

  const create = async () => {
    const trimmed = name.trim();
    if (!chosen || !trimmed) return;
    setSaving(true);
    setError(null);
    try {
      const res = await albumAPI.createFromTemplate(catalogId, {
        template: 'year',
        year: chosen.year,
        name: trimmed,
        description,
        deployment_id: deploymentId ? Number(deploymentId) : null,
      });
      const { album, deploy_error: deployError, publish_error: publishError } = res.data;
      // The album exists either way; a failed publish is a warning for the
      // editor to show, not a reason to stay here.
      const problem = deployError || publishError;
      onCreated(album, problem
        ? `The album was created, but publishing it failed: ${problem}`
        : null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not create that album.');
    } finally {
      setSaving(false);
    }
  };

  const hasYears = !!options?.years?.length;
  const disabled = saving;

  let body;
  if (loadError) {
    body = <CAlert color="danger" className="mb-0">{loadError}</CAlert>;
  } else if (!options) {
    body = (
      <div className="text-center py-4">
        <CSpinner color="primary" />
      </div>
    );
  } else if (!hasYears) {
    body = (
      <div className="text-muted" style={{ fontSize: '0.9rem' }}>
        This catalog has no events yet. Events are made by the Extract Events workflow.
      </div>
    );
  } else {
    body = (
      <CForm onSubmit={(e) => { e.preventDefault(); create(); }}>
        <div className="mb-3">
          <CFormLabel htmlFor="year-template-year">
            Year <span className="text-danger">*</span>
          </CFormLabel>
          <CFormSelect id="year-template-year" value={year} disabled={disabled}
                       onChange={(e) => chooseYear(e.target.value)}>
            {options.years.map((y) => (
              <option key={y.year} value={y.year}>
                {y.year} — {plural(y.event_count, 'event')}
              </option>
            ))}
          </CFormSelect>
        </div>
        <div className="mb-3">
          <CFormLabel htmlFor="year-template-name">
            Name <span className="text-danger">*</span>
          </CFormLabel>
          <CFormInput id="year-template-name" value={name} disabled={disabled}
                      onChange={(e) => { setName(e.target.value); setNameEdited(true); }} />
        </div>
        {/* The same choice as in "Create an album", and optional in the same way. */}
        <div className="mb-3">
          <CFormLabel htmlFor="year-template-deployment">Publish it in</CFormLabel>
          <CFormSelect id="year-template-deployment" value={deploymentId}
                       onChange={(e) => setDeploymentId(e.target.value)}
                       disabled={disabled || deployments.length === 0}>
            <option value="">No deployment for now</option>
            {deployments.map((d) => (
              <option key={d.id} value={d.id}>{d.name}</option>
            ))}
          </CFormSelect>
          {deployments.length === 0 && (
            <small className="text-muted">This catalog has no deployments yet.</small>
          )}
        </div>
        <div className="mb-2">
          <CFormLabel htmlFor="year-template-description">Description</CFormLabel>
          <CFormTextarea id="year-template-description" rows={3} value={description}
                         disabled={disabled} placeholder="Optional description"
                         onChange={(e) => setDescription(e.target.value)} />
        </div>
        <div className="text-muted mb-3" style={{ fontSize: '0.8rem' }}>
          Created as a &lsquo;with tools&rsquo; album.
        </div>

        {chosen && (
          <div style={{ fontSize: '0.88rem' }}>
            Will create{' '}
            <strong>
              {plural(chosen.months.length, 'month')} · {plural(chosen.event_count, 'event')}
              {' · '}{plural(chosen.photo_count, 'photo')}
            </strong>
            : {chosen.months.map((m) => m.name).join(', ')}.
            {chosen.skipped_empty_events > 0 && (
              <div className="text-muted" style={{ fontSize: '0.8rem', marginTop: 2 }}>
                {plural(chosen.skipped_empty_events, 'event')} without photos will be left out.
              </div>
            )}
          </div>
        )}
      </CForm>
    );
  }

  return (
    <CModal visible={visible} onClose={saving ? undefined : onClose} backdrop="static">
      <CModalHeader closeButton={!saving}>
        <CModalTitle>
          <CIcon icon={cilCalendar} className="me-2" />
          Create an album from a template
        </CModalTitle>
      </CModalHeader>
      <CModalBody>
        {/* Always first: this window makes the whole album at once, and says so
            before asking for anything. */}
        <CAlert color="info" className="py-2" style={{ fontSize: '0.85rem' }}>
          <strong>Template: a year in events.</strong> Creates an album with every event from
          the year you choose. Each month that has events becomes a category, holding its
          events in date order. Months with no events are left out. The album opens in the
          tools editor afterwards, where you can change anything.
        </CAlert>
        {error && <CAlert color="danger" className="py-2">{error}</CAlert>}
        {body}
      </CModalBody>
      <CModalFooter>
        <CButton color="secondary" variant="outline" onClick={onClose} disabled={saving}>
          Cancel
        </CButton>
        <CButton color="primary" onClick={create}
                 disabled={saving || !chosen || !name.trim()}>
          {saving ? <CSpinner size="sm" /> : 'Create album'}
        </CButton>
      </CModalFooter>
    </CModal>
  );
};

export default YearAlbumTemplateModal;
