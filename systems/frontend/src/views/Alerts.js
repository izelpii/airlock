import React, { useCallback, useEffect, useState } from 'react';
import {
  CAlert, CBadge, CButton, CButtonGroup, CCard, CCardBody, CContainer, CPagination,
  CPaginationItem, CProgress, CSpinner, CTable, CTableBody, CTableDataCell, CTableHead,
  CTableHeaderCell, CTableRow,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilBell, cilReload } from '@coreui/icons';
import { alertsAPI } from '../services/api';
import socketService from '../services/socket';

// Reports > Alerts: every alert the system has raised, open and resolved, a page
// at a time. The bell in the top bar lists the ten most recent; this is the
// whole history. Storage alerts (storage_alerts.py) carry how full the disk is
// and which catalogs live on it, shown under the message.

const PAGE_SIZE = 25;
const GB = 1024 ** 3;
const fmtGB = (bytes) => (bytes == null ? '—' : `${(bytes / GB).toFixed(bytes < 10 * GB ? 1 : 0)} GB`);
// Timestamps are stored in UTC without a zone.
const fmtWhen = (s) => (s ? new Date(`${s.replace(' ', 'T')}Z`).toLocaleString() : '—');
const KIND_LABEL = { storage: 'Storage' };

const Severity = ({ value }) => (
  <CBadge color={value === 'critical' ? 'danger' : 'warning'}
          style={{ textTransform: 'uppercase', fontSize: '0.65rem' }}>
    {value}
  </CBadge>
);

const StorageDetails = ({ d, severity }) => (
  <div className="mt-1" style={{ maxWidth: 420 }}>
    <CProgress value={d.percent || 0} color={severity === 'critical' ? 'danger' : 'warning'}
               style={{ height: 6 }} />
    <div className="text-muted" style={{ fontSize: '0.75rem' }}>
      {d.percent != null ? `${d.percent}%` : '—'} used · {fmtGB(d.free)} free of {fmtGB(d.total)}
      {Object.keys(d.users || {}).length > 0 && ' · '}
      {Object.entries(d.users || {}).map(([name, u], i) => (
        <span key={name}>{i > 0 && '; '}<strong>{name}</strong> ({(u.parts || []).join(', ')})</span>
      ))}
    </div>
  </div>
);

const Alerts = () => {
  const [openOnly, setOpenOnly] = useState(false);
  const [page, setPage] = useState(0);
  const [data, setData] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    try {
      const { data: d } = await alertsAPI.list({
        includeResolved: !openOnly, limit: PAGE_SIZE, offset: page * PAGE_SIZE });
      setData(d);
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load alerts.');
    }
  }, [openOnly, page]);

  useEffect(() => {
    load();
    socketService.connect();
    const off = socketService.onAlertsUpdated(() => load());
    return () => { if (typeof off === 'function') off(); };
  }, [load]);

  const checkNow = async () => {
    setBusy(true);
    try {
      await alertsAPI.checkNow();
      await load();
    } catch (e) {
      setError(e?.response?.data?.error || 'The check failed.');
    } finally {
      setBusy(false);
    }
  };

  const acknowledge = async (id) => {
    try {
      await alertsAPI.acknowledge(id);
      await load();
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not acknowledge that alert.');
    }
  };

  const alerts = data?.alerts || [];
  const total = data?.total || 0;
  const pages = Math.max(1, Math.ceil(total / PAGE_SIZE));
  const t = data?.thresholds;
  const from = total ? page * PAGE_SIZE + 1 : 0;
  const to = Math.min(total, (page + 1) * PAGE_SIZE);

  return (
    <CContainer fluid className="px-4 py-4">
      <div className="d-flex align-items-center justify-content-between mb-3 flex-wrap gap-2">
        <h4 className="mb-0 fw-bold d-flex align-items-center gap-2">
          <CIcon icon={cilBell} size="lg" /> Alerts
          {data && (
            <span className="text-muted fw-normal" style={{ fontSize: '0.9rem' }}>
              {data.active} open · {data.critical} critical
            </span>
          )}
        </h4>
        <div className="d-flex align-items-center gap-2">
          <CButtonGroup size="sm" role="group" aria-label="Which alerts">
            <CButton color="secondary" variant={openOnly ? 'outline' : undefined}
                     onClick={() => { setOpenOnly(false); setPage(0); }}>All</CButton>
            <CButton color="secondary" variant={openOnly ? undefined : 'outline'}
                     onClick={() => { setOpenOnly(true); setPage(0); }}>Open only</CButton>
          </CButtonGroup>
          <CButton color="primary" variant="outline" size="sm" onClick={checkNow} disabled={busy}>
            {busy ? <CSpinner size="sm" /> : <><CIcon icon={cilReload} className="me-1" />Check now</>}
          </CButton>
        </div>
      </div>

      {error && <CAlert color="danger">{error}</CAlert>}

      {data && total === 0 && (
        <CAlert color="success" data-testid="alerts-none">
          {openOnly ? 'No open alerts.' : 'No alerts yet.'} Every disk the catalogs use has room.
        </CAlert>
      )}

      {total > 0 && (
        <CCard className="mb-3">
          <CCardBody className="p-0">
            <CTable hover responsive className="mb-0 align-middle">
              <CTableHead>
                <CTableRow>
                  <CTableHeaderCell style={{ width: 90 }}>Severity</CTableHeaderCell>
                  <CTableHeaderCell>Alert</CTableHeaderCell>
                  <CTableHeaderCell style={{ width: 90 }}>Kind</CTableHeaderCell>
                  <CTableHeaderCell style={{ width: 200 }}>When</CTableHeaderCell>
                  <CTableHeaderCell style={{ width: 120 }} />
                </CTableRow>
              </CTableHead>
              <CTableBody>
                {alerts.map((a) => (
                  <CTableRow key={a.id} data-testid="alert-row" style={{ opacity: a.active ? 1 : 0.6 }}>
                    <CTableDataCell><Severity value={a.severity} /></CTableDataCell>
                    <CTableDataCell>
                      <div style={{ fontWeight: 600 }}>{a.title}</div>
                      <div className="text-muted" style={{ fontSize: '0.78rem' }}>{a.message}</div>
                      {a.kind === 'storage' && a.details && a.active && (
                        <StorageDetails d={a.details} severity={a.severity} />
                      )}
                    </CTableDataCell>
                    <CTableDataCell style={{ fontSize: '0.8rem' }}>
                      {KIND_LABEL[a.kind] || a.kind}
                    </CTableDataCell>
                    <CTableDataCell style={{ fontSize: '0.75rem', whiteSpace: 'nowrap' }}>
                      <div>raised {fmtWhen(a.first_seen)}</div>
                      <div className="text-muted">
                        {a.active ? `checked ${fmtWhen(a.last_seen)}` : `resolved ${fmtWhen(a.resolved_at)}`}
                      </div>
                    </CTableDataCell>
                    <CTableDataCell className="text-end">
                      {a.active && !a.acknowledged_at && (
                        <CButton size="sm" color="secondary" variant="outline"
                                 onClick={() => acknowledge(a.id)}>
                          Acknowledge
                        </CButton>
                      )}
                      {a.active && a.acknowledged_at && (
                        <span className="text-muted" style={{ fontSize: '0.75rem' }}>acknowledged</span>
                      )}
                      {!a.active && (
                        <span className="text-muted" style={{ fontSize: '0.75rem' }}>resolved</span>
                      )}
                    </CTableDataCell>
                  </CTableRow>
                ))}
              </CTableBody>
            </CTable>
          </CCardBody>
        </CCard>
      )}

      {total > PAGE_SIZE && (
        <div className="d-flex align-items-center justify-content-between mb-3">
          <span className="text-muted" style={{ fontSize: '0.85rem' }} data-testid="alerts-range">
            {from}–{to} of {total}
          </span>
          <CPagination size="sm" className="mb-0" aria-label="Alert pages">
            <CPaginationItem disabled={page === 0} onClick={() => setPage(page - 1)}
                             style={{ cursor: 'pointer' }}>Previous</CPaginationItem>
            <CPaginationItem active>{page + 1} / {pages}</CPaginationItem>
            <CPaginationItem disabled={page >= pages - 1} onClick={() => setPage(page + 1)}
                             style={{ cursor: 'pointer' }} data-testid="alerts-next">Next</CPaginationItem>
          </CPagination>
        </div>
      )}

      {t && (
        <p className="text-muted" style={{ fontSize: '0.78rem' }}>
          Storage: a disk raises a warning at {t.warn_percent}% used or under {t.warn_free_gb} GB
          free, and is critical at {t.critical_percent}% or under {t.critical_free_gb} GB free.
          Imports stop on their own at the critical level - every file not yet done stays queued,
          and running the import again carries on. An alert clears itself once space is freed,
          and an acknowledged warning is raised again if it turns critical.
        </p>
      )}
    </CContainer>
  );
};

export default Alerts;
