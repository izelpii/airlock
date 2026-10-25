import React, { useEffect, useState, useCallback, useRef } from 'react';
import { Link } from 'react-router-dom';
import {
  CCard, CCardBody, CCardHeader, CButton, CAlert, CSpinner, CBadge,
  CProgress, CProgressBar, CTable, CTableHead, CTableRow, CTableHeaderCell,
  CTableBody, CTableDataCell,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilCopy, cilX, cilReload, cilCheckAlt } from '@coreui/icons';
import { transferAPI } from '../services/api';
import socketService from '../services/socket';
import { useCatalog } from '../context/CatalogContext';

// ─── One transfer, live ───────────────────────────────────────────────────────
// THE transfer screen: the code, both progress bars, and the paginated file
// list. See documentation/specs/transfer-spec.md section 3.6 and
// wormhole-album-transfer-spec.md section 8.2.
//
// It is a component rather than a page because it is drawn in two places - the
// full-page route at /albums/transfers/:transferId, and the first row of the
// Transfers page, where whatever is running right now lives. Two spellings of
// this screen is how they drift apart, and this one carries four socket
// handlers and a poll behind them.
//
//   transferId  which transfer to draw
//   perPage     rows of the file table per page. 25 full-page; 8 in the panel,
//               which is what keeps Receiving on screen below it (spec 3.7).
//   compact     one card instead of three, smaller code, height-locked table
//   onFinished  called when the transfer reaches a terminal state, so the
//               Transfers page can refresh its history without a second socket

// The statuses a transfer can still move on from. Exported because the album
// cards and the Transfers page both have to agree with this screen about what
// "still running" means (spec 5.5). The backend keeps its own copy in
// wormhole_transfer.ACTIVE_STATUSES - it is the authority.
export const TRANSFER_ACTIVE_STATUSES = [
  'pending', 'waiting_peer', 'connected', 'transferring',
];

// One row of the file table, near enough. The table's box is fixed at
// perPage × this, so a short last page does not shrink the panel and jump
// whatever is underneath it up the page (spec 3.7).
export const TRANSFER_ROW_HEIGHT = 41;

const formatBytes = (n) => {
  const bytes = Number(n) || 0;
  if (bytes < 1024) return `${bytes} B`;
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit += 1; }
  return `${value.toFixed(value < 10 ? 1 : 0)} ${units[unit]}`;
};

const pct = (done, total) => {
  const t = Number(total) || 0;
  if (!t) return 0;
  return Math.min(100, Math.round((Number(done) || 0) * 100 / t));
};

export const STATUS_LABEL = {
  pending: 'Starting…',
  waiting_peer: 'Waiting for the other machine',
  connected: 'Connected',
  transferring: 'Transferring',
  completed: 'Complete',
  failed: 'Failed',
  cancelled: 'Cancelled',
  // A group member nobody used by the group's deadline
  // (album-mass-transfer-spec.md 3.4).
  expired: 'Code expired',
};

const FILE_STATUS = {
  pending: { label: 'waiting', color: 'secondary' },
  sending: { label: 'transferring', color: 'info' },
  done: { label: 'done', color: 'success' },
  // The visible face of resume, and worth its own wording rather than a second
  // kind of tick (wormhole spec 8.2).
  skipped: { label: 'already on the other machine', color: 'success' },
  failed: { label: 'failed', color: 'danger' },
};

// A section of the panel. Its own card when the panel owns the page; a plain
// block when the panel is already inside one card.
const Block = ({ compact, className, children }) => (compact
  ? <div className={className}>{children}</div>
  : <CCard className={className}><CCardBody>{children}</CCardBody></CCard>);

const TransferPanel = ({ transferId, perPage = 25, compact = false, onFinished }) => {
  const { activeCatalog } = useCatalog();
  const [transfer, setTransfer] = useState(null);
  const [files, setFiles] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(1);
  const [current, setCurrent] = useState(null);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);
  const [busy, setBusy] = useState(false);
  const pageRef = useRef(page);
  pageRef.current = page;

  const catalogId = activeCatalog?.id;

  // Another transfer in the same panel starts at its own first page, rather
  // than on page 7 of the one before it.
  useEffect(() => { setPage(1); setFiles([]); setCurrent(null); }, [transferId]);

  const loadTransfer = useCallback(async () => {
    if (!catalogId) return;
    try {
      const r = await transferAPI.get(catalogId, transferId);
      setTransfer(r.data);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load this transfer.');
    }
  }, [catalogId, transferId]);

  const loadFiles = useCallback(async (which) => {
    if (!catalogId) return;
    try {
      const r = await transferAPI.files(catalogId, transferId,
        which ?? pageRef.current, perPage);
      setFiles(r.data.files || []);
      setTotal(r.data.total || 0);
    } catch {
      /* the transfer header already reports a failure; do not double-report */
    }
  }, [catalogId, transferId, perPage]);

  useEffect(() => { loadTransfer(); }, [loadTransfer]);
  useEffect(() => { loadFiles(page); }, [loadFiles, page]);

  // A POLL, BEHIND THE SOCKET, WHILE THE TRANSFER IS LIVE.
  //
  // The socket below carries everything once it connects, and this screen used
  // to rely on it alone. That made a dead socket indistinguishable from a dead
  // transfer: the wormhole code is written by the runner a second or two AFTER
  // the panel mounts, so the one-shot fetch above always misses it, and the code
  // then arrives only in a progress event. On a deployment whose origin was not
  // in the backend's allowed list the handshake was rejected and this screen sat
  // empty forever, showing no code and no error - nothing was broken except the
  // delivery of the news.
  //
  // Three seconds, and only while the status is non-terminal, so a finished or
  // failed transfer costs nothing. Redundant when the socket works, which is the
  // point: the expensive failure here is a user staring at a blank code box.
  const statusRef = useRef(null);
  statusRef.current = transfer?.status;
  useEffect(() => {
    if (!catalogId) return undefined;
    const id = setInterval(() => {
      if (!TRANSFER_ACTIVE_STATUSES.includes(statusRef.current ?? 'pending')) return;
      loadTransfer();
      loadFiles();
    }, 3000);
    return () => clearInterval(id);
  }, [catalogId, loadTransfer, loadFiles]);

  // Held in a ref so the socket effect below does not re-subscribe every time
  // the caller re-renders with a new inline function.
  const finishedRef = useRef(onFinished);
  finishedRef.current = onFinished;

  useEffect(() => {
    if (!catalogId) return undefined;
    socketService.connect();
    const mine = (d) => String(d.transfer_id) === String(transferId);

    const offProgress = socketService.onTransferProgress((d) => {
      if (!mine(d)) return;
      setCurrent(d.current || null);
      setTransfer((prev) => (prev ? {
        ...prev,
        status: d.status ?? prev.status,
        done_files: d.done_files ?? prev.done_files,
        done_bytes: d.done_bytes ?? prev.done_bytes,
        skipped_bytes: d.skipped_bytes ?? prev.skipped_bytes,
        wormhole_code: d.code ?? prev.wormhole_code,
      } : prev));
    });

    // Patched in place rather than refetched: a file finishing must not cost a
    // round trip per file on a 5 000-photo album. This is also what keeps a
    // page the user has paged FORWARD to live - the rows on screen are updated
    // wherever they are in the list.
    const offFile = socketService.onTransferFile((d) => {
      if (!mine(d)) return;
      setFiles((prev) => prev.map((f) => (f.sha256 === d.sha256
        ? { ...f, status: d.status, sent_bytes: d.sent_bytes ?? f.sent_bytes,
            error: d.error ?? f.error }
        : f)));
    });

    const offDone = socketService.onTransferDone((d) => {
      if (!mine(d)) return;
      setCurrent(null);
      loadTransfer();
      loadFiles();
      if (finishedRef.current) finishedRef.current(d);
    });

    // The receiving side learns its file list from the peer, so the table is
    // empty until the manifest arrives.
    const offReady = socketService.onTransferFilesReady((d) => {
      if (!mine(d)) return;
      loadTransfer();
      loadFiles();
    });

    return () => { offProgress(); offFile(); offDone(); offReady(); };
  }, [catalogId, transferId, loadTransfer, loadFiles]);

  const copyCode = () => {
    if (!transfer?.wormhole_code) return;
    navigator.clipboard?.writeText(transfer.wormhole_code);
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  };

  const act = async (fn) => {
    setBusy(true);
    setError(null);
    try {
      await fn();
      await loadTransfer();
      await loadFiles();
    } catch (e) {
      setError(e?.response?.data?.error || 'That did not work.');
    } finally {
      setBusy(false);
    }
  };

  if (!catalogId) {
    return <CAlert color="warning">Open a catalog first.</CAlert>;
  }
  if (!transfer) {
    return <CSpinner />;
  }

  const isActive = TRANSFER_ACTIVE_STATUSES.includes(transfer.status);
  const overall = pct(transfer.done_bytes, transfer.total_bytes);
  const currentPct = current ? pct(current.sent_bytes, current.size_bytes) : 0;
  const pages = Math.max(1, Math.ceil(total / perPage));
  const sending = transfer.direction === 'send';
  // One person in a group sent to several people. Its new codes are made by
  // the group, which owns the deadline, not by Resume here (spec 3.6).
  const member = !!transfer.batch_id;
  const memberLabel = transfer.recipient_label || `Person ${transfer.recipient_order}`;

  const title = (
    <>
      {sending ? 'Sending album' : 'Receiving album'}
      {transfer.album_name ? ` — ${transfer.album_name}` : ''}
      {member ? ` · for ${memberLabel}` : ''}
    </>
  );
  const subtitle = (
    <>
      {STATUS_LABEL[transfer.status] || transfer.status}
      {transfer.destination && ` · into ${transfer.destination}`}
    </>
  );
  const controls = (
    <div className="d-flex gap-2">
      {isActive && (
        <CButton color="danger" variant="outline" size="sm" disabled={busy}
          onClick={() => act(() => transferAPI.cancel(catalogId, transferId))}>
          <CIcon icon={cilX} className="me-1" />Cancel
        </CButton>
      )}
      {!isActive && transfer.status !== 'completed' && !member && (
        <CButton color="primary" size="sm" disabled={busy}
          onClick={() => act(() => transferAPI.resume(catalogId, transferId))}>
          <CIcon icon={cilReload} className="me-1" />Resume
        </CButton>
      )}
      {!isActive && transfer.status !== 'completed' && member && (
        <Link className="btn btn-sm btn-outline-primary"
          to={`/albums/transfers/batches/${transfer.batch_id}`}>
          New code from the group
        </Link>
      )}
    </div>
  );

  const body = (
    <>
      {error && <CAlert color="danger">{error}</CAlert>}
      {transfer.error_message && (
        <CAlert color="danger">{transfer.error_message}</CAlert>
      )}

      {/* The one thing a person has to carry between two machines by hand. */}
      {sending && transfer.wormhole_code && isActive && (
        <Block compact={compact} className="mb-3">
          <div className="text-center py-1">
            <div className="small text-muted mb-2">
              On the other machine, open <strong>Albums ▸ Transfers</strong> and
              enter this code:
            </div>
            <div className="d-flex justify-content-center align-items-center gap-2">
              <code style={{
                // Smaller in the panel, where it shares the first row with the
                // bars and the file table.
                fontSize: compact ? '1.25rem' : '1.6rem',
                fontWeight: 600, letterSpacing: '0.02em',
                background: '#f1f3f5', padding: '0.4rem 1rem', borderRadius: 8,
                color: '#212121',
              }}>{transfer.wormhole_code}</code>
              <CButton color="secondary" variant="ghost" size="sm" onClick={copyCode}
                title="Copy the code">
                <CIcon icon={copied ? cilCheckAlt : cilCopy} />
              </CButton>
            </div>
            <div className="small text-muted mt-2">
              It works once, and anyone who has it can receive these photos —
              read it out, don&apos;t post it.
            </div>
          </div>
        </Block>
      )}

      <Block compact={compact} className="mb-3">
        <div className="d-flex justify-content-between small mb-1">
          <strong>Overall</strong>
          <span className="text-muted">
            {transfer.done_files} of {transfer.total_files} files ·{' '}
            {formatBytes(transfer.done_bytes)} of {formatBytes(transfer.total_bytes)}
            {transfer.skipped_bytes > 0 &&
              ` · ${formatBytes(transfer.skipped_bytes)} already there`}
          </span>
        </div>
        <CProgress height={18} className="mb-3">
          <CProgressBar value={overall} color="success">{overall}%</CProgressBar>
        </CProgress>

        <div className="d-flex justify-content-between small mb-1">
          <strong>Current file</strong>
          <span className="text-muted">
            {current
              ? `${current.name} · ${formatBytes(current.sent_bytes)} of ${formatBytes(current.size_bytes)}`
              : (isActive ? 'waiting…' : '—')}
          </span>
        </div>
        <CProgress height={18}>
          <CProgressBar value={currentPct} color="info">
            {current ? `${currentPct}%` : ''}
          </CProgressBar>
        </CProgress>
      </Block>

      <CCard>
        <CCardHeader className="d-flex justify-content-between align-items-center">
          <strong>Files</strong>
          {pages > 1 && (
            <div className="d-flex align-items-center gap-2">
              <CButton size="sm" variant="outline" color="secondary"
                disabled={page <= 1} onClick={() => setPage((p) => p - 1)}>‹</CButton>
              <span className="small text-muted">{page} / {pages}</span>
              <CButton size="sm" variant="outline" color="secondary"
                disabled={page >= pages} onClick={() => setPage((p) => p + 1)}>›</CButton>
            </div>
          )}
        </CCardHeader>
        {/* Height-locked in the panel: a short last page must not shrink the
            first row and pull Receiving up the page (spec 3.7). The row count
            never exceeds perPage, because that is what the server was asked
            for. */}
        <CCardBody className="p-0"
                   style={compact ? { minHeight: perPage * TRANSFER_ROW_HEIGHT } : undefined}>
          {files.length === 0 ? (
            <div className="p-3 text-muted small">
              {sending
                ? 'No files in this transfer.'
                : 'Waiting for the sending machine to describe the album…'}
            </div>
          ) : (
            <CTable hover responsive className="mb-0 align-middle">
              <CTableHead>
                <CTableRow>
                  <CTableHeaderCell>File</CTableHeaderCell>
                  <CTableHeaderCell className="text-end">Size</CTableHeaderCell>
                  <CTableHeaderCell style={{ width: '30%' }}>Status</CTableHeaderCell>
                </CTableRow>
              </CTableHead>
              <CTableBody>
                {files.map((f) => {
                  const meta = FILE_STATUS[f.status] || FILE_STATUS.pending;
                  const filePct = pct(f.sent_bytes, f.size_bytes);
                  return (
                    <CTableRow key={f.sha256}>
                      <CTableDataCell className="text-truncate"
                        style={{ maxWidth: 340 }}>{f.file_name}</CTableDataCell>
                      <CTableDataCell className="text-end small text-muted">
                        {formatBytes(f.size_bytes)}
                      </CTableDataCell>
                      <CTableDataCell>
                        {f.status === 'sending' ? (
                          <CProgress height={14}>
                            <CProgressBar value={filePct} color="info">
                              {filePct}%
                            </CProgressBar>
                          </CProgress>
                        ) : (
                          <>
                            <CBadge color={meta.color}>{meta.label}</CBadge>
                            {f.error && (
                              <div className="small text-danger mt-1">{f.error}</div>
                            )}
                          </>
                        )}
                      </CTableDataCell>
                    </CTableRow>
                  );
                })}
              </CTableBody>
            </CTable>
          )}
        </CCardBody>
      </CCard>
    </>
  );

  // One card with the title in its header when the panel is a section of
  // someone else's page; a page heading and three cards when it IS the page.
  if (compact) {
    return (
      <CCard>
        <CCardHeader className="d-flex justify-content-between align-items-start">
          <div>
            <strong style={{ color: '#212121' }}>{title}</strong>
            <div className="small text-muted">{subtitle}</div>
          </div>
          {controls}
        </CCardHeader>
        <CCardBody>{body}</CCardBody>
      </CCard>
    );
  }

  return (
    <>
      <div className="d-flex justify-content-between align-items-start mb-3">
        <div>
          <h4 className="fw-bold mb-0" style={{ color: '#212121' }}>{title}</h4>
          <div className="small text-muted">{subtitle}</div>
        </div>
        {controls}
      </div>
      {body}
    </>
  );
};

export default TransferPanel;
