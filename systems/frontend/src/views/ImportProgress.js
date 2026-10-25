import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import {
  CAlert,
  CBadge,
  CButton,
  CCard,
  CCardBody,
  CCardHeader,
  CCol,
  CContainer,
  CFormSelect,
  CProgress,
  CProgressBar,
  CRow,
  CSpinner,
  CTable,
  CTableBody,
  CTableDataCell,
  CTableHead,
  CTableHeaderCell,
  CTableRow,
} from '@coreui/react';
import { Line, LineChart, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts';
import { catalogAPI } from '../services/api';
import socketService from '../services/socket';
import { useCatalog } from '../context/CatalogContext';

// Import Progress report (documentation/features/parallelism.md section 8.5).
//
// The overhead rule: poll one small endpoint every 2 s, only while the selected
// import is running AND this tab is visible. The workflow_progress socket event
// is used only as a nudge to poll now. The throughput chart is built here from
// successive polls and never stored anywhere.

const POLL_MS = 2000;
const SPARK_WINDOW_MS = 15 * 60 * 1000;

const PHASE_LABELS = {
  scan: 'Scan',
  probe: 'Probe',
  resolve: 'Resolve',
  place: 'Place',
  render: 'Render',
};

const PHASE_HINTS = {
  scan: 'Walk the source folder',
  probe: 'Hash and read metadata',
  resolve: 'Find duplicates and RAW+JPG pairs',
  place: 'Copy or index, and add to the catalog',
  render: 'Friendly versions, thumbnails, video',
};

const STATUS_COLORS = {
  running: 'primary',
  completed: 'success',
  stopped: 'warning',
  failed: 'danger',
  interrupted: 'danger',
  skipped: 'secondary',
  pending: 'light',
};

const fmtNum = (n) => (n === null || n === undefined ? '–' : Number(n).toLocaleString());

const fmtDuration = (secs) => {
  if (secs === null || secs === undefined) return '–';
  const s = Math.max(0, Math.round(secs));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (h) return `${h}h${String(m).padStart(2, '0')}m`;
  if (m) return `${m}m${String(s % 60).padStart(2, '0')}s`;
  return `${s}s`;
};

const fmtBytes = (b) => {
  if (!b) return null;
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = b;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${units[i]}`;
};

const handledOf = (p) => (p.done || 0) + (p.failed || 0) + (p.skipped || 0);

// What finished phases say about themselves, from their counters and detail.
const phaseOutcome = (p) => {
  const d = p.detail || {};
  const parts = [];
  switch (p.phase) {
    case 'scan':
      parts.push(`${fmtNum(p.total)} files`);
      if (d.dirs) parts.push(`${fmtNum(d.dirs)} folders`);
      if (d.ignored) parts.push(`${fmtNum(d.ignored)} ignored`);
      if (d.already_imported) parts.push(`${fmtNum(d.already_imported)} already imported`);
      break;
    case 'probe':
      parts.push(`${fmtNum(p.done)} probed`);
      if (fmtBytes(p.bytes_done)) parts.push(`${fmtBytes(p.bytes_done)} hashed`);
      if (d.adopted) parts.push(`${fmtNum(d.adopted)} reused from an earlier run`);
      break;
    case 'resolve':
      parts.push(`${fmtNum(p.done)} new`);
      if (d.catalogue_duplicates) parts.push(`${fmtNum(d.catalogue_duplicates)} already in catalog`);
      if (d.in_run_duplicates) parts.push(`${fmtNum(d.in_run_duplicates)} repeated in this import`);
      if (d.groups) parts.push(`${fmtNum(d.groups)} RAW+JPG pairs`);
      break;
    case 'place':
      parts.push(`${fmtNum(p.done)} added`);
      if (fmtBytes(p.bytes_done)) parts.push(`${fmtBytes(p.bytes_done)} copied`);
      if (d.groups) parts.push(`${fmtNum(d.groups)} pairs grouped`);
      break;
    case 'render':
      parts.push(`${fmtNum(p.done)} rendered`);
      if (p.skipped) parts.push(`${fmtNum(p.skipped)} need none`);
      if (d.from_earlier_runs) parts.push(`${fmtNum(d.from_earlier_runs)} left by an earlier import`);
      break;
    default:
      break;
  }
  if (p.failed) parts.push(`${fmtNum(p.failed)} failed`);
  return parts.join(' · ');
};

// What a phase is doing that its counter cannot show: housekeeping before the
// scan, work left by an earlier import, and each video being converted - one
// can take ten minutes without the render counter moving.
const PhaseNotes = ({ p }) => {
  const d = p.detail || {};
  const notes = [];
  if (p.phase === 'scan' && p.status === 'pending' && d.housekeeping) {
    notes.push(
      <div key="hk">Clearing records of earlier imports first. After a long gap this can take a few minutes.</div>
    );
  }
  if (p.phase === 'render' && p.status === 'running') {
    if (d.from_earlier_runs) {
      notes.push(
        <div key="earlier">
          Includes {fmtNum(d.from_earlier_runs)} file{d.from_earlier_runs === 1 ? '' : 's'} an earlier, interrupted
          import added but did not finish.
        </div>
      );
    }
    (d.videos_now || []).forEach((v) => {
      const size = fmtBytes(v.size);
      const written = fmtBytes(v.written);
      notes.push(
        <div key={`v-${v.name}`}>
          Converting video <strong>{v.name}</strong>
          {size ? ` (${size})` : ''} · {fmtDuration(v.seconds)} so far
          {written ? ` · ${written} written` : ''}
        </div>
      );
    });
  }
  if (!notes.length) return null;
  return <div className="small text-body-secondary mt-1">{notes}</div>;
};

const PhaseRow = ({ p, index }) => {
  const status = p.status || 'pending';
  const pending = status === 'pending' || status === 'skipped';
  const total = p.total;
  const handled = handledOf(p);
  const pct = total ? Math.min(100, Math.round((100 * handled) / total)) : (status === 'completed' ? 100 : 0);

  return (
    <div className="py-2" style={{ borderBottom: '1px solid var(--cui-border-color)', opacity: pending ? 0.55 : 1 }}>
      <div className="d-flex justify-content-between align-items-baseline flex-wrap gap-2">
        <div>
          <strong>{index + 1}. {PHASE_LABELS[p.phase]}</strong>{' '}
          <span className="text-body-secondary small">{PHASE_HINTS[p.phase]}</span>
        </div>
        <div className="d-flex align-items-center gap-2">
          {p.stale && (
            <CBadge color="warning" title="The import has not reported progress recently">
              no progress for a while
            </CBadge>
          )}
          <CBadge color={STATUS_COLORS[status] || 'secondary'} textColor={status === 'pending' ? 'dark' : undefined}>
            {status}
          </CBadge>
          {p.duration_seconds !== null && p.duration_seconds !== undefined && (
            <span className="small text-body-secondary">{fmtDuration(p.duration_seconds)}</span>
          )}
        </div>
      </div>

      {status === 'running' && (
        <>
          <CProgress height={10} className="mt-2">
            <CProgressBar value={pct} color="primary" animated={p.phase !== 'resolve'} />
          </CProgress>
          <div className="small text-body-secondary mt-1 d-flex flex-wrap gap-3">
            <span>
              {fmtNum(handled)}{total !== null && total !== undefined ? ` / ${fmtNum(total)}` : ''}
            </span>
            {p.rate_per_sec ? <span>{p.rate_per_sec.toFixed(1)}/s</span> : null}
            {p.workers ? <span>{p.workers} worker{p.workers === 1 ? '' : 's'}</span> : null}
            {fmtBytes(p.bytes_done) ? <span>{fmtBytes(p.bytes_done)}</span> : null}
            {p.failed ? <span className="text-danger">{fmtNum(p.failed)} failed</span> : null}
            {p.eta_seconds !== null && p.eta_seconds !== undefined ? (
              <span>ETA {fmtDuration(p.eta_seconds)}</span>
            ) : null}
          </div>
        </>
      )}

      <PhaseNotes p={p} />

      {(status === 'completed' || status === 'stopped' || status === 'failed') && (
        <div className="small text-body-secondary mt-1">{phaseOutcome(p)}</div>
      )}
    </div>
  );
};

const ImportProgress = () => {
  const { activeCatalog } = useCatalog();
  const location = useLocation();
  const navigate = useNavigate();
  const [imports, setImports] = useState([]);
  const [selected, setSelected] = useState(null);
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [stopping, setStopping] = useState(false);
  const [spark, setSpark] = useState([]);
  const [visible, setVisible] = useState(
    typeof document === 'undefined' ? true : document.visibilityState === 'visible'
  );
  const sparkPrev = useRef(null);

  const catalogId = activeCatalog?.id;
  const deepLink = new URLSearchParams(location.search).get('execution');

  // The list: the running import if there is one, else the most recent.
  const loadImports = useCallback(async () => {
    if (!catalogId) return;
    try {
      const res = await catalogAPI.getImports(catalogId);
      const list = res.data?.imports || [];
      setImports(list);
      setSelected((prev) => {
        if (prev && list.some((i) => String(i.execution_id) === String(prev))) return prev;
        if (deepLink && list.some((i) => String(i.execution_id) === deepLink)) return deepLink;
        const running = list.find((i) => i.status === 'running');
        return running ? String(running.execution_id) : list[0] ? String(list[0].execution_id) : null;
      });
    } catch (err) {
      setError('Could not load imports: ' + err.message);
    }
  }, [catalogId, deepLink]);

  useEffect(() => {
    setImports([]);
    setSelected(null);
    setData(null);
    loadImports();
  }, [loadImports]);

  const loadProgress = useCallback(async () => {
    if (!catalogId || !selected) return;
    try {
      setLoading((l) => l || !data);
      const res = await catalogAPI.getImportProgress(catalogId, selected);
      const d = res.data;
      setData(d);
      setError(null);
      // Throughput of the running phase, from the change in its counter
      // between two polls. Kept in memory only; resets on reload.
      const running = (d.phases || []).find((p) => p.status === 'running');
      if (running) {
        const now = Date.now();
        const handled = handledOf(running);
        const prev = sparkPrev.current;
        if (prev && prev.phase === running.phase && now > prev.at) {
          const rate = Math.max(0, (handled - prev.handled) / ((now - prev.at) / 1000));
          setSpark((s) =>
            [...s, { t: now, rate: Number(rate.toFixed(2)), phase: running.phase }].filter(
              (x) => now - x.t <= SPARK_WINDOW_MS
            )
          );
        }
        sparkPrev.current = { phase: running.phase, handled, at: now };
      }
      if (d.status !== 'running') {
        // The list's status column may be stale for a run that just ended.
        setImports((list) =>
          list.map((i) => (String(i.execution_id) === String(d.execution_id) ? { ...i, status: d.status } : i))
        );
      }
    } catch (err) {
      setError('Could not load progress: ' + err.message);
    } finally {
      setLoading(false);
    }
    // `data` deliberately omitted: it only decides whether to show the spinner.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [catalogId, selected]);

  // New selection: fresh chart, immediate load.
  useEffect(() => {
    setSpark([]);
    sparkPrev.current = null;
    setData(null);
    loadProgress();
  }, [loadProgress]);

  useEffect(() => {
    const onVis = () => setVisible(document.visibilityState === 'visible');
    document.addEventListener('visibilitychange', onVis);
    return () => document.removeEventListener('visibilitychange', onVis);
  }, []);

  const isRunning = data?.status === 'running';

  // Poll only while running and visible.
  useEffect(() => {
    if (!isRunning || !visible) return undefined;
    loadProgress();
    const t = setInterval(loadProgress, POLL_MS);
    return () => clearInterval(t);
  }, [isRunning, visible, loadProgress]);

  // A progress event for this import (a phase change, the end) means "poll now".
  useEffect(() => {
    socketService.connect();
    const off = socketService.onWorkflowProgress((ev) => {
      if (!ev?.execution_id || String(ev.execution_id) !== String(selected)) return;
      if (document.visibilityState === 'visible') loadProgress();
      if (ev.status && ev.status !== 'running') loadImports();
    });
    return () => off();
  }, [selected, loadProgress, loadImports]);

  const handleSelect = (e) => {
    const id = e.target.value;
    setSelected(id);
    navigate(`/import-progress?execution=${id}`, { replace: true });
  };

  const handleStop = async () => {
    if (!catalogId || !selected) return;
    if (!window.confirm('Stop this import? Work in hand finishes first; running the import again continues from where it stopped.')) return;
    setStopping(true);
    try {
      await catalogAPI.stopExecution(catalogId, selected);
      await loadProgress();
    } catch (err) {
      setError('Could not stop the import: ' + (err.response?.data?.error || err.message));
    } finally {
      setStopping(false);
    }
  };

  if (!activeCatalog) {
    return (
      <CContainer>
        <CCard>
          <CCardBody>
            <p className="text-body-secondary mb-0">No catalog selected. Choose a catalog to see its imports.</p>
          </CCardBody>
        </CCard>
      </CContainer>
    );
  }

  const phases = data?.phases || [];
  const running = phases.find((p) => p.status === 'running');
  const counts = data?.state_counts;

  return (
    <CContainer fluid>
      <div className="mb-3">
        <h4 className="mb-1">Import progress</h4>
        <div className="text-body-secondary small">
          Each picture import runs in five phases. This page follows one while it runs and keeps the record after it ends.
        </div>
      </div>

      {error && <CAlert color="danger">{error}</CAlert>}

      <CCard className="mb-3">
        <CCardBody>
          <CRow className="g-2 align-items-center">
            <CCol md={9}>
              <CFormSelect value={selected || ''} onChange={handleSelect} disabled={!imports.length} aria-label="Import">
                {!imports.length && <option value="">No imports in this catalog yet</option>}
                {imports.map((i) => (
                  <option key={i.execution_id} value={i.execution_id}>
                    #{i.execution_id} · {i.source_directory || 'source not recorded'} · {i.status}
                    {i.started_at ? ` · started ${i.started_at}` : ''}
                  </option>
                ))}
              </CFormSelect>
            </CCol>
            <CCol md={3} className="text-md-end">
              {isRunning && (
                <CButton color="danger" variant="outline" onClick={handleStop} disabled={stopping || data?.stop_requested}>
                  {data?.stop_requested ? 'Stopping…' : stopping ? <CSpinner size="sm" /> : 'Stop import'}
                </CButton>
              )}
            </CCol>
          </CRow>
        </CCardBody>
      </CCard>

      {loading && !data && (
        <div className="text-center py-4">
          <CSpinner />
        </div>
      )}

      {data && (
        <>
          <CCard className="mb-3">
            <CCardBody>
              <div className="d-flex justify-content-between align-items-baseline flex-wrap gap-2 mb-2">
                <div>
                  <strong>Overall</strong>{' '}
                  <CBadge color={STATUS_COLORS[data.status] || 'secondary'}>{data.status}</CBadge>
                  {!visible && isRunning && (
                    <span className="small text-body-secondary ms-2">paused while this tab is hidden</span>
                  )}
                </div>
                <div className="small text-body-secondary">
                  elapsed {fmtDuration(data.elapsed_seconds)}
                  {isRunning && (
                    <>
                      {' · '}
                      {data.eta_seconds !== null && data.eta_seconds !== undefined
                        ? `ETA ~${fmtDuration(data.eta_seconds)}`
                        : 'ETA unknown until a phase has a rate and earlier imports exist'}
                    </>
                  )}
                  {' · '}
                  {fmtNum(data.total_files)} files
                </div>
              </div>
              <CProgress height={16}>
                <CProgressBar value={data.overall_percent || 0} color={STATUS_COLORS[data.status] || 'primary'}>
                  {data.overall_percent >= 8 ? `${Math.round(data.overall_percent)}%` : ''}
                </CProgressBar>
              </CProgress>
            </CCardBody>
          </CCard>

          {!data.has_phases ? (
            <CAlert color="info">
              This import ran before phase tracking existed, so only its overall count is recorded:{' '}
              {data.overall_percent}% of {fmtNum(data.total_files)} files.
            </CAlert>
          ) : (
            <CRow>
              <CCol lg={8}>
                <CCard className="mb-3">
                  <CCardHeader>
                    <strong>Phases</strong>
                  </CCardHeader>
                  <CCardBody className="pt-1">
                    {phases.map((p, i) => (
                      <PhaseRow key={p.phase} p={p} index={i} />
                    ))}
                  </CCardBody>
                </CCard>
              </CCol>
              <CCol lg={4}>
                {isRunning && (
                  <CCard className="mb-3">
                    <CCardHeader>
                      <strong>Throughput</strong>{' '}
                      <span className="small text-body-secondary">
                        {running ? `${PHASE_LABELS[running.phase]}, items/s, last 15 min` : ''}
                      </span>
                    </CCardHeader>
                    <CCardBody>
                      {spark.length < 2 ? (
                        <div className="small text-body-secondary">Collecting samples while this page is open…</div>
                      ) : (
                        <ResponsiveContainer width="100%" height={120}>
                          <LineChart data={spark} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
                            <XAxis dataKey="t" hide />
                            <YAxis width={36} tick={{ fontSize: 11 }} allowDecimals={false} />
                            <Tooltip
                              labelFormatter={(t) => new Date(t).toLocaleTimeString()}
                              formatter={(v) => [`${v}/s`, 'rate']}
                            />
                            <Line type="monotone" dataKey="rate" stroke="var(--cui-primary)" dot={false} isAnimationActive={false} />
                          </LineChart>
                        </ResponsiveContainer>
                      )}
                    </CCardBody>
                  </CCard>
                )}

                {!isRunning && counts && (
                  <CCard className="mb-3">
                    <CCardHeader>
                      <strong>Where every file ended up</strong>
                    </CCardHeader>
                    <CCardBody className="p-0">
                      <CTable small className="mb-0">
                        <CTableBody>
                          {Object.entries(counts)
                            .sort((a, b) => b[1] - a[1])
                            .map(([state, n]) => (
                              <CTableRow key={state}>
                                <CTableDataCell>{state.replace(/_/g, ' ')}</CTableDataCell>
                                <CTableDataCell className="text-end">{fmtNum(n)}</CTableDataCell>
                              </CTableRow>
                            ))}
                        </CTableBody>
                      </CTable>
                    </CCardBody>
                  </CCard>
                )}
              </CCol>
            </CRow>
          )}

          {data.recent_failures?.length > 0 && (
            <CCard className="mb-3">
              <CCardHeader>
                <strong>Recent failures</strong>{' '}
                <span className="small text-body-secondary">latest {data.recent_failures.length}</span>
              </CCardHeader>
              <CCardBody className="p-0">
                <CTable small responsive className="mb-0">
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell>Phase</CTableHeaderCell>
                      <CTableHeaderCell>File</CTableHeaderCell>
                      <CTableHeaderCell>Error</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {data.recent_failures.map((f, i) => (
                      <CTableRow key={`${f.source_path}-${i}`}>
                        <CTableDataCell>{PHASE_LABELS[f.phase] || f.phase}</CTableDataCell>
                        <CTableDataCell style={{ wordBreak: 'break-all' }}>{f.source_path}</CTableDataCell>
                        <CTableDataCell style={{ wordBreak: 'break-word' }}>{f.error}</CTableDataCell>
                      </CTableRow>
                    ))}
                  </CTableBody>
                </CTable>
              </CCardBody>
            </CCard>
          )}
        </>
      )}
    </CContainer>
  );
};

export default ImportProgress;
