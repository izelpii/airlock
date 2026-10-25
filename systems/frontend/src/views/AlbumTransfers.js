import React, { useEffect, useState, useCallback } from 'react';
import { Link, useNavigate } from 'react-router-dom';
import {
  CCard, CCardBody, CRow, CCol, CButton, CAlert, CForm,
  CFormLabel, CFormInput, CSpinner, CBadge, CTable, CTableHead, CTableRow,
  CTableHeaderCell, CTableBody, CTableDataCell,
} from '@coreui/react';
import { transferAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import TransferPanel, { TRANSFER_ACTIVE_STATUSES } from '../components/TransferPanel';
import TransferBatch from '../components/TransferBatch';

// ─── The transfers area ───────────────────────────────────────────────────────
//
// Both directions in one place:
//
//   left column   row 1 — whatever is transferring RIGHT NOW: its code, its
//                 progress and its files. Empty when nothing is.
//                 row 2 — Receiving, a code from another machine
//   right column  Recent transfers — the history, either direction, in a panel
//                 the width of a deployment card
//
// Row one used to be a second album list, whose only purpose was to carry a
// Transfer button. That button is on the album card now, which is where an
// album's actions live, and this row shows the transfer instead of offering to
// start one - the album was the subject of two pages, and only one of them was
// about albums (transfer-spec.md sections 3.1 and 3.8).
//
// An album sent to several people is a GROUP of transfers. It is one chip and
// one history entry here, never one per person, and selecting it draws the
// group view in row one (album-mass-transfer-spec.md 7.4).
//
// See documentation/specs/transfer-spec.md, and
// wormhole-album-transfer-spec.md section 8 for the machinery underneath.

// The history column, matching a deployment card (Deployments.js CARD_WIDTH).
// Fixed rather than a percentage: it holds one table of short rows, so it needs
// a readable width, not a share of the window. The live panel beside it takes
// whatever is left.
const HISTORY_WIDTH = 340;

const STATUS_COLOR = {
  completed: 'success', failed: 'danger', cancelled: 'secondary', expired: 'warning',
};

// Newest first across both kinds of entry. Both timestamps are SQLite's
// CURRENT_TIMESTAMP, so they compare as text.
const startedOf = (e) => (e.kind === 'batch' ? e.item.created_at : e.item.started_at) || '';
const newestFirst = (a, b) => (startedOf(b).localeCompare(startedOf(a))
  || (b.item.id - a.item.id));

// "sent to 10 · 7 done · 2 expired"
const groupCaption = (b) => {
  const c = b.counts || {};
  const parts = [`sent to ${b.people}`];
  if (c.completed) parts.push(`${c.completed} done`);
  const waiting = (c.waiting_peer || 0) + (c.pending || 0);
  if (waiting) parts.push(`${waiting} waiting`);
  const moving = (c.connected || 0) + (c.transferring || 0);
  if (moving) parts.push(`${moving} sending`);
  if (c.expired) parts.push(`${c.expired} expired`);
  if (c.failed) parts.push(`${c.failed} failed`);
  return parts.join(' · ');
};

// Eight rows of files in the panel, against 25 on the full-page transfer view.
// This is the number that keeps Receiving on screen underneath: the table's box
// is locked at eight rows' height, so the first row is the same height for a
// four-file transfer and a four-thousand-file one, and only the pager's
// "page / pages" changes (transfer-spec.md 3.7).
const PANEL_PER_PAGE = 8;

const AlbumTransfers = () => {
  const navigate = useNavigate();
  const { activeCatalog } = useCatalog();
  const [transfers, setTransfers] = useState([]);
  const [batches, setBatches] = useState([]);
  const [code, setCode] = useState('');
  const [label, setLabel] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);
  // Which entry the first row is showing - {kind: 'transfer' | 'batch', id}.
  // Null means "whichever is newest", which is what the user wants every time
  // but the one time they picked another from the chips.
  const [shown, setShown] = useState(null);

  const catalogId = activeCatalog?.id;

  const load = useCallback(async () => {
    if (!catalogId) return;
    try {
      const t = await transferAPI.list(catalogId);
      setTransfers(t.data?.transfers || []);
      setBatches(t.data?.batches || []);
    } catch {
      // Keep whatever was last known. Emptying the list here would draw the
      // "nothing is running" line, which would be a guess rather than news.
      setError('Could not read the transfer list.');
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  // What is moving right now, newest first. Both directions: a receive has no
  // code to show but has the same progress and the same files, and "what is
  // moving" is the question this row answers either way (spec 3.4).
  // A group's members are not entries of their own: the group is.
  const singles = transfers.filter((t) => !t.batch_id);
  const active = [
    ...singles.filter((t) => TRANSFER_ACTIVE_STATUSES.includes(t.status))
      .map((item) => ({ kind: 'transfer', item })),
    ...batches.filter((b) => b.is_active).map((item) => ({ kind: 'batch', item })),
  ].sort(newestFirst);

  // The one the panel draws. An entry the user picked stays picked until it
  // leaves the list entirely - INCLUDING once it finishes, because a panel that
  // vanishes the instant a transfer completes hides the completion, which is the
  // one moment the user was waiting for.
  const pickedItem = shown && (shown.kind === 'batch'
    ? batches.find((b) => b.id === shown.id)
    : transfers.find((t) => t.id === shown.id));
  const panel = (pickedItem && { kind: shown.kind, item: pickedItem }) || active[0] || null;
  const sameEntry = (a, b) => a && b && a.kind === b.kind && a.item.id === b.item.id;

  const history = [
    ...singles.map((item) => ({ kind: 'transfer', item })),
    ...batches.map((item) => ({ kind: 'batch', item })),
  ].sort(newestFirst);

  const submitReceive = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      const r = await transferAPI.receive(catalogId, {
        code: code.trim(), label: label.trim(),
      });
      // Shown in row one, on this page, rather than navigated to. Starting a
      // receive used to jump to the full-page view; row one is now where a live
      // transfer lives, and it is directly above the form that was just
      // submitted - so leaving the page would be walking away from the answer.
      setCode('');
      setLabel('');
      await load();
      setShown({ kind: 'transfer', id: r.data.transfer_id });
    } catch (err) {
      setError(err?.response?.data?.error || 'Could not start receiving.');
    } finally {
      setBusy(false);
    }
  };

  const labelSlug = label.trim()
    .toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

  const separator = {
    marginTop: '2.5rem', marginBottom: '1.5rem',
    border: 0, borderTop: '1px solid #dee2e6', opacity: 1,
  };

  if (!catalogId) {
    return <div className="p-4"><CAlert color="warning">Open a catalog first.</CAlert></div>;
  }

  return (
    <div className="p-4">
      {error && <CAlert color="danger">{error}</CAlert>}

      <CRow className="g-4 align-items-start">
        {/* ── Left: what is moving now, then how to receive ────────────── */}
        <CCol xs={12} xl>
          {/* ── Row one: the live transfer ──
              Empty when nothing is running - one line rather than nothing at
              all, because a blank half-page reads as broken and says nothing
              about where transfers now begin, at the moment the reader is
              asking (spec 3.5). */}
          {panel ? (
            <>
              {/* Chips only when there is a choice to make. Two albums can send
                  at once, and a receive can run beside either - the backend
                  refuses only a SECOND single send of the SAME album. A group
                  is one chip, however many people it reaches. */}
              {active.length > 1 && (
                <div className="d-flex flex-wrap gap-2 mb-3">
                  {active.map((e) => {
                    const on = sameEntry(e, panel);
                    const t = e.item;
                    return (
                      <CButton
                        key={`${e.kind}-${t.id}`}
                        size="sm"
                        color={on ? 'primary' : 'secondary'}
                        variant={on ? undefined : 'outline'}
                        onClick={() => setShown({ kind: e.kind, id: t.id })}
                      >
                        {t.album_name || '—'}
                        <span className="ms-1 small">
                          · {e.kind === 'batch' ? `${t.people} people`
                            : (t.direction === 'send' ? 'sent' : 'received')}
                        </span>
                      </CButton>
                    );
                  })}
                </div>
              )}
              {panel.kind === 'batch' ? (
                <TransferBatch key={`batch-${panel.item.id}`} batchId={panel.item.id}
                  onChanged={load} />
              ) : (
                <TransferPanel
                  key={panel.item.id}
                  transferId={panel.item.id}
                  perPage={PANEL_PER_PAGE}
                  compact
                  onFinished={load}
                />
              )}
            </>
          ) : (
            <div className="text-muted mb-1" style={{ fontSize: '0.9rem' }}>
              No transfer is running. Start one from <Link to="/albums">Albums</Link>
              {' '}— the Transfer or Mass transfer button on any album card.
            </div>
          )}

          <hr style={separator} />
          <h4 className="mb-3 fw-bold" style={{ color: '#212121' }}>Receiving</h4>

          <CCard style={{ maxWidth: 620 }}>
            <CCardBody>
              <CForm onSubmit={submitReceive}>
                <div className="mb-3">
                  <CFormLabel htmlFor="wh-code">Wormhole code</CFormLabel>
                  <CFormInput
                    id="wh-code" value={code} required
                    placeholder="7-revenue-bookseller"
                    onChange={(e) => setCode(e.target.value)}
                    style={{ fontFamily: 'monospace', fontSize: '1.1rem' }}
                  />
                  <small className="text-muted">
                    Three parts, separated by dashes, exactly as the sending
                    machine shows it.
                  </small>
                </div>

                <div className="mb-3">
                  <CFormLabel htmlFor="wh-label">Label (optional)</CFormLabel>
                  <CFormInput id="wh-label" value={label} placeholder="summer 2019"
                    onChange={(e) => setLabel(e.target.value)} />
                  <small className="text-muted d-block">
                    Every transfer gets its own numbered folder under{' '}
                    <code>{activeCatalog?.target_directory || '<catalog>'}/transfers/</code>
                    {labelSlug && <> — this one would be <code>…-{labelSlug}</code>.</>}
                  </small>
                  <small className="text-muted">
                    The folder appears only when every photo has arrived and been
                    checked, so a half-finished transfer never looks complete.
                  </small>
                </div>

                <CButton color="primary" type="submit" disabled={busy || !code.trim()}>
                  {busy ? <><CSpinner size="sm" className="me-2" />Connecting…</>
                    : 'Start receiving'}
                </CButton>
              </CForm>
            </CCardBody>
          </CCard>
        </CCol>

        {/* ── Right: what has already happened ─────────────────────────────
            Its own column rather than a section below, and its own column
            rather than part of either action, because it lists whichever
            direction a transfer went. Drops under the left column on a narrow
            window (xs={12}) instead of squeezing both. */}
        <CCol xs={12} xl="auto" style={{ width: HISTORY_WIDTH }}>
          <div className="d-flex justify-content-between align-items-center mb-3">
            <h4 className="fw-bold mb-0" style={{ color: '#212121' }}>
              Recent transfers
            </h4>
            <CButton size="sm" variant="ghost" color="secondary" onClick={load}>
              Refresh
            </CButton>
          </div>
          <CCard>
            <CCardBody className="p-0">
              {history.length === 0 ? (
                <div className="p-3 small text-muted">Nothing yet.</div>
              ) : (
                <CTable hover responsive className="mb-0 align-middle">
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell>Album</CTableHeaderCell>
                      <CTableHeaderCell>Files</CTableHeaderCell>
                      <CTableHeaderCell />
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {history.map(({ kind, item: t }) => (kind === 'batch' ? (
                      <CTableRow key={`batch-${t.id}`}>
                        <CTableDataCell>
                          <div className="text-truncate" style={{ maxWidth: 150 }}>
                            {t.album_name || '—'}
                          </div>
                          <div className="small text-muted">{groupCaption(t)}</div>
                        </CTableDataCell>
                        <CTableDataCell className="small text-nowrap">
                          {(t.counts?.completed || 0)} / {t.people}
                        </CTableDataCell>
                        <CTableDataCell className="text-end">
                          <CButton size="sm" variant="ghost" color="primary"
                            onClick={() => navigate(`/albums/transfers/batches/${t.id}`)}>
                            Open
                          </CButton>
                        </CTableDataCell>
                      </CTableRow>
                    ) : (
                      <CTableRow key={t.id}>
                        <CTableDataCell>
                          <div className="text-truncate" style={{ maxWidth: 150 }}>
                            {t.album_name || '—'}
                          </div>
                          {/* Direction and status are a caption here rather
                              than two more columns: the panel is one card wide
                              and a five-column table would truncate the one
                              thing that identifies the row. */}
                          <div className="small text-muted">
                            {t.direction === 'send' ? 'sent' : 'received'}
                            {' · '}
                            <CBadge color={STATUS_COLOR[t.status] || 'info'}
                              style={{ fontSize: '0.65rem' }}>
                              {t.status}
                            </CBadge>
                          </div>
                        </CTableDataCell>
                        <CTableDataCell className="small text-nowrap">
                          {t.done_files} / {t.total_files}
                        </CTableDataCell>
                        <CTableDataCell className="text-end">
                          <CButton size="sm" variant="ghost" color="primary"
                            onClick={() => navigate(`/albums/transfers/${t.id}`)}>
                            Open
                          </CButton>
                        </CTableDataCell>
                      </CTableRow>
                    )))}
                  </CTableBody>
                </CTable>
              )}
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>
    </div>
  );
};

export default AlbumTransfers;
