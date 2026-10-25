import React, { useEffect, useState, useCallback, useRef } from 'react';
import {
  CCard, CCardBody, CCardHeader, CButton, CAlert, CSpinner, CBadge,
  CProgress, CProgressBar, CTable, CTableHead, CTableRow, CTableHeaderCell,
  CTableBody, CTableDataCell, CFormSelect, CFormInput,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilCopy, cilCheckAlt, cilX, cilReload, cilPlus } from '@coreui/icons';
import { transferAPI } from '../services/api';
import socketService from '../services/socket';
import { useCatalog } from '../context/CatalogContext';
import TransferPanel, { TRANSFER_ACTIVE_STATUSES } from './TransferPanel';

// ─── One album, several people ────────────────────────────────────────────────
// The group view: one row per person, each with their own code, status and
// progress, and one deadline for all of them. See
// documentation/specs/album-mass-transfer-spec.md section 7.3.
//
// A component rather than a page, like TransferPanel: it is drawn on the
// Transfers page, where whatever is running lives, and at its own route,
// /albums/transfers/batches/:batchId. Clicking a person opens their transfer -
// the ordinary TransferPanel - underneath, because each member IS an ordinary
// transfer (spec 3.1).

// When the codes expire. Three days is both the default and the most the
// backend accepts (spec 3.3).
export const TTL_OPTIONS = [
  { hours: 1, label: '1 hour' },
  { hours: 12, label: '12 hours' },
  { hours: 24, label: '1 day' },
  { hours: 48, label: '2 days' },
  { hours: 72, label: '3 days' },
];
export const DEFAULT_TTL = 72;

// "Sat 26 Oct, 14:05" - a local date and time, never only a duration (3.3).
export const formatDeadline = (when) => {
  const d = when instanceof Date ? when : new Date(when);
  return d.toLocaleString(undefined, {
    weekday: 'short', day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit',
  });
};

// "2 d 23 h left", "5 h 12 min left", "12 min left"; null once it has passed.
export const timeLeft = (when, now = Date.now()) => {
  const ms = new Date(when).getTime() - now;
  if (ms <= 0) return null;
  const min = Math.floor(ms / 60000);
  const d = Math.floor(min / 1440);
  const h = Math.floor((min % 1440) / 60);
  const m = min % 60;
  if (d > 0) return `${d} d ${h} h left`;
  if (h > 0) return `${h} h ${m} min left`;
  return `${Math.max(1, m)} min left`;
};

const RENEWABLE = ['expired', 'failed', 'cancelled'];

const MEMBER_STATUS = {
  pending: { label: 'getting a code…', color: 'secondary' },
  waiting_peer: { label: 'waiting', color: 'info' },
  completed: { label: 'done', color: 'success' },
  expired: { label: 'code expired', color: 'warning' },
  failed: { label: 'failed', color: 'danger' },
  cancelled: { label: 'stopped', color: 'secondary' },
};

// The order the summary line reads in.
const SUMMARY = [
  ['waiting_peer', 'waiting'], ['pending', 'starting'], ['transferring', 'sending'],
  ['connected', 'sending'], ['completed', 'done'], ['expired', 'expired'],
  ['failed', 'failed'], ['cancelled', 'stopped'],
];

const pct = (done, total) => {
  const t = Number(total) || 0;
  return t ? Math.min(100, Math.round((Number(done) || 0) * 100 / t)) : 0;
};

const NEW_CODE_HINT = 'Someone who never connected enters the new code in Albums ▸ '
  + 'Transfers ▸ Receiving. Someone whose transfer failed partway presses Resume on '
  + 'it and enters the new code, so the photos they already have are kept.';

const TransferBatch = ({ batchId, onChanged }) => {
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;
  const [batch, setBatch] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  const [copied, setCopied] = useState(null);
  const [open, setOpen] = useState(null);          // the member shown underneath
  const [renewing, setRenewing] = useState(null);  // null | {ids: [..] | null}
  const [renewTtl, setRenewTtl] = useState(DEFAULT_TTL);
  const [adding, setAdding] = useState(false);
  const [newLabel, setNewLabel] = useState('');
  const [, setTick] = useState(0);

  const load = useCallback(async () => {
    if (!catalogId) return;
    try {
      const r = await transferAPI.batch(catalogId, batchId);
      setBatch(r.data);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load this group.');
    }
  }, [catalogId, batchId]);

  useEffect(() => { setBatch(null); setOpen(null); load(); }, [load]);

  // The countdown is the browser's own clock: there is no server event for
  // time passing. Once a minute is as fine as "2 h 14 min left" needs.
  useEffect(() => {
    const id = setInterval(() => setTick((t) => t + 1), 60000);
    return () => clearInterval(id);
  }, []);

  // Behind the socket, as TransferPanel does it: a code arrives a second or
  // two after its runner starts, and a dead socket must not look like a dead
  // group. Only while someone is still waiting or sending.
  const activeRef = useRef(false);
  activeRef.current = !!batch?.members?.some(
    (m) => TRANSFER_ACTIVE_STATUSES.includes(m.status));
  useEffect(() => {
    const id = setInterval(() => { if (activeRef.current) load(); }, 3000);
    return () => clearInterval(id);
  }, [load]);

  const changedRef = useRef(onChanged);
  changedRef.current = onChanged;

  // Which transfers are this group's, in a ref so the subscription below is
  // made once rather than again on every progress tick.
  const idsRef = useRef(new Set());
  idsRef.current = new Set((batch?.members || []).map((m) => String(m.id)));

  useEffect(() => {
    if (!catalogId) return undefined;
    socketService.connect();
    const mine = (d) => idsRef.current.has(String(d.transfer_id));
    const offProgress = socketService.onTransferProgress((d) => {
      if (!mine(d)) return;
      setBatch((prev) => (prev ? {
        ...prev,
        members: prev.members.map((m) => (String(m.id) === String(d.transfer_id) ? {
          ...m,
          status: d.status ?? m.status,
          done_bytes: d.done_bytes ?? m.done_bytes,
          done_files: d.done_files ?? m.done_files,
          wormhole_code: d.code ?? m.wormhole_code,
        } : m)),
      } : prev));
    });
    const offDone = socketService.onTransferDone((d) => {
      if (!mine(d)) return;
      load();
      if (changedRef.current) changedRef.current();
    });
    return () => { offProgress(); offDone(); };
  }, [catalogId, load]);

  const act = async (fn, fallback) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await load();
      if (changedRef.current) changedRef.current();
      return true;
    } catch (e) {
      setError(e?.response?.data?.error || fallback);
      return false;
    } finally {
      setBusy(false);
    }
  };

  const copy = (text, key) => {
    navigator.clipboard?.writeText(text);
    setCopied(key);
    setTimeout(() => setCopied(null), 1500);
  };

  if (!catalogId) return <CAlert color="warning">Open a catalog first.</CAlert>;
  if (!batch) return error ? <CAlert color="danger">{error}</CAlert> : <CSpinner />;

  const members = batch.members || [];
  const waiting = members.filter((m) => m.wormhole_code);
  const renewable = members.filter((m) => RENEWABLE.includes(m.status));
  const anyActive = members.some((m) => TRANSFER_ACTIVE_STATUSES.includes(m.status));
  const expired = !timeLeft(batch.expires_at);
  const left = timeLeft(batch.expires_at);
  const full = members.length >= (batch.max_people || 20);

  const summary = [];
  const seen = {};
  SUMMARY.forEach(([status, word]) => {
    const n = batch.counts?.[status] || 0;
    if (n) seen[word] = (seen[word] || 0) + n;
  });
  Object.entries(seen).forEach(([word, n]) => summary.push(`${n} ${word}`));

  const startRenew = (ids) => { setRenewTtl(DEFAULT_TTL); setRenewing({ ids }); };
  const confirmRenew = async () => {
    const ok = await act(() => transferAPI.renew(catalogId, batchId, renewTtl,
      renewing.ids || undefined), 'Could not make new codes.');
    if (ok) setRenewing(null);
  };
  const addPerson = async () => {
    const ok = await act(() => transferAPI.addPerson(catalogId, batchId, newLabel.trim()),
      'Could not add that person.');
    if (ok) { setAdding(false); setNewLabel(''); }
  };
  const stopAll = () => {
    if (!window.confirm('Stop every code in this group that is still waiting or '
      + 'sending? Anyone holding one will no longer be able to use it.')) return;
    act(() => transferAPI.cancelBatch(catalogId, batchId), 'Could not stop the group.');
  };

  const renewLabel = renewing?.ids
    ? `A new code for ${members.find((m) => m.id === renewing.ids[0])?.label}`
    : `New codes for ${renewable.length} ${renewable.length === 1 ? 'person' : 'people'}`;

  return (
    <>
      <CCard className="mb-3" data-testid="transfer-batch">
        <CCardHeader>
          <div className="d-flex justify-content-between align-items-start gap-2">
            <div>
              <strong style={{ color: '#212121' }}>
                {batch.album_name} · {members.length} people
              </strong>
              <div className="small text-muted">
                {expired
                  ? <>Codes expired {formatDeadline(batch.expires_at)}</>
                  : <>Codes expire {formatDeadline(batch.expires_at)} · {left}</>}
                {batch.cancelled_at && ' · stopped'}
              </div>
            </div>
            {anyActive && (
              <CButton color="danger" variant="outline" size="sm" disabled={busy}
                onClick={stopAll}>
                <CIcon icon={cilX} className="me-1" />Stop all
              </CButton>
            )}
          </div>
          <div className="d-flex flex-wrap gap-2 mt-2">
            <CButton color="secondary" variant="outline" size="sm"
              disabled={waiting.length === 0}
              title="Every code still waiting, one per line, as Label: code"
              onClick={() => copy(waiting.map((m) => `${m.label}: ${m.wormhole_code}`)
                .join('\n'), 'all')}>
              <CIcon icon={copied === 'all' ? cilCheckAlt : cilCopy} className="me-1" />
              Copy all codes
            </CButton>
            <CButton color="primary" size="sm" variant={expired ? undefined : 'outline'}
              disabled={busy || renewable.length === 0} title={NEW_CODE_HINT}
              onClick={() => startRenew(null)}>
              <CIcon icon={cilReload} className="me-1" />New codes for unfinished
            </CButton>
            <CButton color="secondary" variant="outline" size="sm"
              disabled={busy || full || expired}
              title={full ? `A group holds at most ${batch.max_people} people`
                : expired ? 'Make new codes first; a new person takes the new deadline'
                  : 'One more person, with the same deadline'}
              onClick={() => setAdding(true)}>
              <CIcon icon={cilPlus} className="me-1" />Add person
            </CButton>
          </div>
        </CCardHeader>
        <CCardBody className="p-0">
          {error && <CAlert color="danger" className="m-2">{error}</CAlert>}

          {renewing && (
            <div className="p-3 border-bottom" style={{ background: '#f8f9fa' }}>
              <div className="small mb-2"><strong>{renewLabel}</strong></div>
              <div className="d-flex align-items-center gap-2 flex-wrap">
                <span className="small">Codes expire in</span>
                <CFormSelect size="sm" style={{ width: 120 }} value={renewTtl}
                  aria-label="New deadline"
                  onChange={(e) => setRenewTtl(Number(e.target.value))}>
                  {TTL_OPTIONS.map((o) => (
                    <option key={o.hours} value={o.hours}>{o.label}</option>))}
                </CFormSelect>
                <CButton color="primary" size="sm" disabled={busy} onClick={confirmRenew}>
                  {busy ? <CSpinner size="sm" /> : 'Make codes'}
                </CButton>
                <CButton color="secondary" variant="ghost" size="sm"
                  onClick={() => setRenewing(null)}>Cancel</CButton>
              </div>
              <div className="small text-muted mt-2">
                The deadline moves for the whole group, including codes still
                waiting. {NEW_CODE_HINT}
              </div>
            </div>
          )}

          {adding && (
            <div className="p-3 border-bottom d-flex align-items-center gap-2 flex-wrap"
              style={{ background: '#f8f9fa' }}>
              <CFormInput size="sm" style={{ maxWidth: 220 }} maxLength={40}
                aria-label="Label for the new person"
                placeholder={`Person ${members.length + 1}`} value={newLabel}
                onChange={(e) => setNewLabel(e.target.value)} />
              <CButton color="primary" size="sm" disabled={busy} onClick={addPerson}>
                {busy ? <CSpinner size="sm" /> : 'Add'}
              </CButton>
              <CButton color="secondary" variant="ghost" size="sm"
                onClick={() => { setAdding(false); setNewLabel(''); }}>Cancel</CButton>
            </div>
          )}

          <CTable hover responsive className="mb-0 align-middle">
            <CTableHead>
              <CTableRow>
                <CTableHeaderCell>Person</CTableHeaderCell>
                <CTableHeaderCell>Code</CTableHeaderCell>
                <CTableHeaderCell style={{ width: '32%' }}>Status</CTableHeaderCell>
                <CTableHeaderCell />
              </CTableRow>
            </CTableHead>
            <CTableBody>
              {members.map((m) => {
                const moving = m.status === 'connected' || m.status === 'transferring';
                const meta = MEMBER_STATUS[m.status] || { label: m.status, color: 'secondary' };
                const p = pct(m.done_bytes, m.total_bytes);
                return (
                  <CTableRow key={m.id} active={open === m.id} data-testid="batch-member"
                    style={{ cursor: 'pointer' }}
                    onClick={() => setOpen(open === m.id ? null : m.id)}>
                    <CTableDataCell className="text-truncate" style={{ maxWidth: 160 }}>
                      {m.label}
                    </CTableDataCell>
                    <CTableDataCell className="text-nowrap">
                      {m.wormhole_code ? (
                        <>
                          <code style={{ fontSize: '0.95rem', color: '#212121' }}>
                            {m.wormhole_code}
                          </code>
                          <CButton color="secondary" variant="ghost" size="sm"
                            title="Copy this code"
                            onClick={(e) => { e.stopPropagation(); copy(m.wormhole_code, m.id); }}>
                            <CIcon icon={copied === m.id ? cilCheckAlt : cilCopy} />
                          </CButton>
                        </>
                      ) : <span className="text-muted">—</span>}
                    </CTableDataCell>
                    <CTableDataCell>
                      {moving ? (
                        <CProgress height={14}>
                          <CProgressBar value={p} color="info">{p}%</CProgressBar>
                        </CProgress>
                      ) : (
                        <>
                          <CBadge color={meta.color}>{meta.label}</CBadge>
                          {m.error_message && (
                            <div className="small text-danger mt-1">{m.error_message}</div>
                          )}
                        </>
                      )}
                    </CTableDataCell>
                    <CTableDataCell className="text-end text-nowrap">
                      {RENEWABLE.includes(m.status) && (
                        <CButton color="primary" variant="ghost" size="sm" disabled={busy}
                          title={NEW_CODE_HINT}
                          onClick={(e) => { e.stopPropagation(); startRenew([m.id]); }}>
                          New code
                        </CButton>
                      )}
                      {TRANSFER_ACTIVE_STATUSES.includes(m.status) && (
                        <CButton color="danger" variant="ghost" size="sm" disabled={busy}
                          title={`Stop ${m.label}'s code`}
                          onClick={(e) => {
                            e.stopPropagation();
                            act(() => transferAPI.cancel(catalogId, m.id),
                              'Could not stop that code.');
                          }}>
                          Stop
                        </CButton>
                      )}
                    </CTableDataCell>
                  </CTableRow>
                );
              })}
            </CTableBody>
          </CTable>
          <div className="d-flex justify-content-between small text-muted px-3 py-2">
            <span>
              Each code works once, for one person - send each privately.
              Airlock must stay running until they have it.
            </span>
            <span className="text-nowrap ms-3">{summary.join(' · ')}</span>
          </div>
        </CCardBody>
      </CCard>

      {open && (
        <TransferPanel key={open} transferId={open} perPage={8} compact onFinished={load} />
      )}
    </>
  );
};

export default TransferBatch;
