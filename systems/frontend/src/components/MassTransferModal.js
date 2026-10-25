import React, { useEffect, useState } from 'react';
import {
  CModal, CModalHeader, CModalTitle, CModalBody, CModalFooter, CButton, CAlert,
  CSpinner, CFormSelect, CFormInput, CFormLabel, CRow, CCol,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPeople } from '@coreui/icons';
import { transferAPI } from '../services/api';
import { TTL_OPTIONS, DEFAULT_TTL, formatDeadline } from './TransferBatch';

// ─── Send an album to several people ──────────────────────────────────────────
// How many, how long the codes wait, and optionally who each one is for. Each
// person gets their own wormhole code - one code reaches one person, once - and
// all the codes share one deadline of at most three days
// (documentation/specs/album-mass-transfer-spec.md, section 7.2).

export const MIN_PEOPLE = 2;
export const MAX_PEOPLE = 20;
const MAX_LABEL = 40;

const MassTransferModal = ({ visible, album, catalogId, onClose, onCreated }) => {
  const [count, setCount] = useState(MIN_PEOPLE);
  const [ttl, setTtl] = useState(DEFAULT_TTL);
  const [labels, setLabels] = useState(Array(MIN_PEOPLE).fill(''));
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  // A fresh form for each album the dialog is opened for.
  useEffect(() => {
    if (!visible) return;
    setCount(MIN_PEOPLE);
    setTtl(DEFAULT_TTL);
    setLabels(Array(MIN_PEOPLE).fill(''));
    setError(null);
  }, [visible, album?.id]);

  // Fields come and go from the END, so what was typed is kept (spec 7.2).
  const changeCount = (n) => {
    setCount(n);
    setLabels((prev) => (n > prev.length
      ? [...prev, ...Array(n - prev.length).fill('')]
      : prev.slice(0, n)));
  };

  const setLabel = (i, value) => setLabels((prev) => prev.map(
    (l, k) => (k === i ? value : l)));

  // Labels need not be unique, but telling codes apart is what they are for.
  const typed = labels.map((l) => l.trim().toLowerCase()).filter(Boolean);
  const duplicate = typed.find((l, i) => typed.indexOf(l) !== i);

  const deadline = new Date(Date.now() + ttl * 3600 * 1000);

  const create = async () => {
    setSaving(true);
    setError(null);
    try {
      const r = await transferAPI.massStart(catalogId, album.id,
        labels.map((label) => ({ label: label.trim() })), ttl);
      const missing = r.data?.missing_files || [];
      onCreated(r.data.batch_id, missing);
    } catch (e) {
      // The album already has a group: that group is the answer (spec 3.8).
      if (e?.response?.status === 409 && e.response.data?.batch_id) {
        onCreated(e.response.data.batch_id, []);
        return;
      }
      setError(e?.response?.data?.error || 'Could not create the codes.');
    } finally {
      setSaving(false);
    }
  };

  const half = Math.ceil(count / 2);
  const field = (i) => (
    <div key={i} className="d-flex align-items-center gap-2 mb-2">
      <span className="text-muted small text-end" style={{ width: 22 }}>{i + 1}</span>
      <CFormInput size="sm" value={labels[i]} maxLength={MAX_LABEL}
        placeholder={`Person ${i + 1}`} aria-label={`Label for person ${i + 1}`}
        onChange={(e) => setLabel(i, e.target.value)} />
    </div>
  );

  return (
    <CModal visible={visible} onClose={onClose} size="lg">
      <CModalHeader>
        <CModalTitle>
          <CIcon icon={cilPeople} className="me-2" />
          Send &ldquo;{album?.name}&rdquo; to several people
        </CModalTitle>
      </CModalHeader>
      <CModalBody>
        {error && <CAlert color="danger">{error}</CAlert>}
        <CRow className="g-3 mb-3 align-items-center">
          <CCol xs="auto"><CFormLabel htmlFor="mt-count" className="mb-0">How many people?</CFormLabel></CCol>
          <CCol xs="auto">
            <CFormSelect id="mt-count" size="sm" value={count}
              onChange={(e) => changeCount(Number(e.target.value))}>
              {Array.from({ length: MAX_PEOPLE - MIN_PEOPLE + 1 }, (_, k) => k + MIN_PEOPLE)
                .map((n) => <option key={n} value={n}>{n}</option>)}
            </CFormSelect>
          </CCol>
          <CCol xs="auto" className="small text-muted">{MIN_PEOPLE} to {MAX_PEOPLE}</CCol>
        </CRow>
        <CRow className="g-3 mb-3 align-items-center">
          <CCol xs="auto"><CFormLabel htmlFor="mt-ttl" className="mb-0">Codes expire in</CFormLabel></CCol>
          <CCol xs="auto">
            <CFormSelect id="mt-ttl" size="sm" value={ttl}
              onChange={(e) => setTtl(Number(e.target.value))}>
              {TTL_OPTIONS.map((o) => <option key={o.hours} value={o.hours}>{o.label}</option>)}
            </CFormSelect>
          </CCol>
          <CCol xs="auto" className="small text-muted" data-testid="mt-deadline">
            {formatDeadline(deadline)}
          </CCol>
        </CRow>

        <div className="small mb-2">
          <strong>Labels</strong> <span className="text-muted">(optional) — so you know which code is whose</span>
        </div>
        <CRow>
          <CCol md={6}>{labels.slice(0, half).map((_, i) => field(i))}</CCol>
          <CCol md={6}>{labels.slice(half).map((_, i) => field(i + half))}</CCol>
        </CRow>
        {duplicate && (
          <div className="small text-warning mt-1">
            Two people are called &ldquo;{duplicate}&rdquo; - you may not be able to
            tell their codes apart.
          </div>
        )}

        <div className="small text-muted mt-3">
          Each person needs Airlock and enters their code in <strong>Albums ▸
          Transfers ▸ Receiving</strong>. Codes nobody has used by the deadline stop
          working, and you can make new ones. Airlock must stay running until
          everyone has the album - restarting it ends the codes still waiting.
        </div>
      </CModalBody>
      <CModalFooter>
        <CButton color="secondary" variant="ghost" onClick={onClose}>Cancel</CButton>
        <CButton color="primary" disabled={saving} onClick={create}>
          {saving ? <CSpinner size="sm" /> : `Create ${count} codes`}
        </CButton>
      </CModalFooter>
    </CModal>
  );
};

export default MassTransferModal;
