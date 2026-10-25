import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  CAlert, CBadge, CButton, CCard, CCardBody, CCardHeader, CCol, CContainer,
  CProgress, CProgressBar, CRow, CSpinner,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilImage, cilMediaPlay, cilPeople, cilPaw, cilCalendar, cilLocationPin,
  cilWarning, cilXCircle, cilFace, cilStorage, cilBirthdayCake, cilShareAlt,
  cilReload, cilCheckCircle, cilCloudUpload,
} from '@coreui/icons';
import {
  Bar, BarChart, Cell, Pie, PieChart, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from 'recharts';
import { dashboardAPI } from '../services/api';
import CalendarHeatmap, { CalendarLegend, rollingYearBlock } from '../components/CalendarHeatmap';
import { linkFor } from '../utils/calendarDays';
import { Meter } from '../components/ReportFigures';
import socketService from '../services/socket';
import { useCatalog } from '../context/CatalogContext';
import { galleryThumbUrl } from '../config';

// ─── Dashboard ────────────────────────────────────────────────────────────────
//
// The application's home: the open catalog at a glance, in one request
// (documentation/specs/dashboard-specs.md). The backend serves the library
// counts from a cache and rebuilds it in the background; activity, alerts and
// disks are live. Every number links to the page that explains it.

// Colour by meaning, the same in every chart (validated palette, light theme):
// photos and videos are categorical slots 1 and 2; status colours are reserved
// for state and always come with an icon and a label.
const PHOTO = '#2a78d6';
const VIDEO = '#eb6834';
const TRACK = '#e9ecef';
const STATUS = { good: '#0ca30c', warning: '#fab219', critical: '#d03b3b' };
const MUTED = '#6c757d';

const fmt = (n) => (n === null || n === undefined ? '–' : Number(n).toLocaleString());

const fmtBytes = (b) => {
  if (!b) return '0 B';
  const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
  let v = b;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
};

const ago = (iso) => {
  if (!iso) return null;
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

const pct = (done, of) => (of ? Math.round((100 * done) / of) : 0);

const STATUS_BADGE = {
  completed: 'success', running: 'primary', failed: 'danger', interrupted: 'danger',
  stopped: 'warning', queued: 'secondary',
};

// ── pieces ───────────────────────────────────────────────────────────────────

const Tile = ({ icon, label, value, sub, onClick }) => (
  <CCard className="h-100" style={{ cursor: 'pointer' }} onClick={onClick}
         role="link" tabIndex={0} onKeyDown={(e) => e.key === 'Enter' && onClick()}>
    <CCardBody className="d-flex align-items-center gap-3 py-3">
      <CIcon icon={icon} style={{ width: 28, height: 28, color: MUTED, flexShrink: 0 }} />
      <div style={{ minWidth: 0 }}>
        <div className="fw-bold" style={{ fontSize: '1.35rem', lineHeight: 1.1 }}>{fmt(value)}</div>
        <div className="text-body-secondary small">{label}</div>
        {sub && <div className="text-body-secondary" style={{ fontSize: 11 }}>{sub}</div>}
      </div>
    </CCardBody>
  </CCard>
);

const Panel = ({ title, extra, children, className = 'h-100' }) => (
  <CCard className={className}>
    <CCardHeader className="fw-semibold d-flex justify-content-between align-items-center">
      <span>{title}</span>
      {extra}
    </CCardHeader>
    <CCardBody>{children}</CCardBody>
  </CCard>
);

const Swatch = ({ color }) => (
  <span style={{ display: 'inline-block', width: 10, height: 10, borderRadius: 2,
                 background: color, marginRight: 6, verticalAlign: 'middle' }} />
);

// A small ring: one share of a whole.
const Ring = ({ label, done, of }) => {
  const p = pct(done, of);
  const data = [{ v: done }, { v: Math.max(0, of - done) }];
  return (
    <div className="text-center" style={{ width: 96 }} title={`${fmt(done)} of ${fmt(of)}`}>
      <div style={{ position: 'relative', width: 72, height: 72, margin: '0 auto' }}>
        <ResponsiveContainer width="100%" height="100%">
          <PieChart>
            <Pie data={data} dataKey="v" innerRadius={26} outerRadius={34} startAngle={90}
                 endAngle={-270} stroke="none" isAnimationActive={false}>
              <Cell fill={PHOTO} />
              <Cell fill={TRACK} />
            </Pie>
          </PieChart>
        </ResponsiveContainer>
        <div style={{ position: 'absolute', inset: 0, display: 'flex', alignItems: 'center',
                      justifyContent: 'center', fontWeight: 600, fontSize: 13 }}>
          {p}%
        </div>
      </div>
      <div className="small text-body-secondary mt-1" style={{ lineHeight: 1.2 }}>{label}</div>
    </div>
  );
};

// One line of "What we know about it" (content_report._coverage), compact: the
// share covered as a thin bar. An unused feature says so rather than showing
// an empty bar - on a real catalog that is the common case, not a failure.
const KnownRow = ({ what, covered, of, used }) => {
  const p = pct(covered, of);
  return (
    <div className="mb-2">
      <div className="d-flex justify-content-between small" style={{ lineHeight: 1.3 }}>
        <span className="text-truncate me-2">{what}</span>
        {used ? (
          <span className="text-nowrap" title={`${fmt(covered)} of ${fmt(of)}`}>
            <strong>{p}%</strong>
          </span>
        ) : (
          <CBadge color="light" className="text-dark fw-normal" style={{ fontSize: 10 }}>not used yet</CBadge>
        )}
      </div>
      {/* The report's own Meter: a plain bar, in the app's photo blue. */}
      {used && <div className="mt-1"><Meter covered={covered} total={of} color={PHOTO} height={5} /></div>}
    </div>
  );
};

// One drive: used vs free, coloured by the storage alert thresholds.
const DriveBar = ({ drive }) => {
  const sev = drive.severity;
  const color = sev === 'critical' ? STATUS.critical : sev === 'warning' ? STATUS.warning : PHOTO;
  return (
    <div className="mb-3">
      <div className="d-flex justify-content-between small">
        <span className="text-truncate me-2" title={drive.parts.join(', ')}>
          {sev && (
            <CIcon icon={sev === 'critical' ? cilXCircle : cilWarning}
                   style={{ width: 13, height: 13, color, marginRight: 4 }} />
          )}
          <strong>{drive.mount}</strong>
          {sev && <span className="ms-1" style={{ color }}>{sev}</span>}
        </span>
        <span className="text-body-secondary text-nowrap">
          {fmtBytes(drive.free)} free of {fmtBytes(drive.total)}
        </span>
      </div>
      {/* A plain bar: CProgressBar's own style carries its width, and a
          style={{ background }} passed to it replaced that - the bar drew at
          zero width (v5.12.5). */}
      <div className="mt-1" title={`${drive.percent}% used`}>
        <Meter covered={drive.percent} total={100} color={color} height={10} />
      </div>
      <div className="text-body-secondary" style={{ fontSize: 11 }}>
        {drive.percent}% used · {drive.parts.join(', ')}
      </div>
    </div>
  );
};

// The open catalog's backups, one line each (backup spec §5.3). State is
// colour + icon + words, never colour alone.
const BACKUP_LOOK = {
  up_to_date: { color: STATUS.good, icon: cilCheckCircle, label: 'Up to date' },
  needed: { color: STATUS.warning, icon: cilWarning, label: 'Backup needed' },
  failed: { color: STATUS.critical, icon: cilXCircle, label: 'Failed' },
  running: { color: PHOTO, icon: cilCloudUpload, label: 'Running' },
};
const RUN_LABEL = { backup: 'Backing up', verify: 'Verifying', restore: 'Restoring' };

const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, {
  weekday: 'short', hour: '2-digit', minute: '2-digit',
}) : null);

const repoHost = (uri) => {
  const m = /^sftp:(?:\/\/)?(?:[^@:/]+@)?([^:/]+)/.exec(uri || '');
  return m ? m[1] : uri;
};

const BackupLine = ({ card }) => {
  const st = card.status || {};
  const look = BACKUP_LOOK[st.state] || BACKUP_LOOK.needed;
  const lb = card.last_backup || {};
  const running = st.state === 'running';
  return (
    <div className="py-2" style={{ borderBottom: '1px solid #f1f3f5' }}>
      <div className="d-flex justify-content-between align-items-center small">
        <span className="text-truncate me-2">
          <strong>{card.name}</strong>
          <span className="text-body-secondary"> · {repoHost(card.repo_uri)}</span>
        </span>
        <span className="text-nowrap" style={{ color: look.color, fontWeight: 600 }}>
          <CIcon icon={look.icon} style={{ width: 13, height: 13, marginRight: 4 }} />
          {running ? RUN_LABEL[st.kind] || look.label : look.label}
        </span>
      </div>
      {running && st.progress && (
        <CProgress height={6} className="mt-1"
                   title={st.progress.percent != null ? `${st.progress.percent}%` : 'Working'}>
          {/* borg's backup has no total: a full, moving bar. The colour by
              `color`, never by style - that would replace the bar's width. */}
          <CProgressBar value={st.progress.percent ?? 100} animated={st.progress.percent == null}
                        color="primary" />
        </CProgress>
      )}
      <div className="text-body-secondary" style={{ fontSize: 11 }}>
        {running && st.progress?.percent != null ? `${st.progress.percent}% · ` : ''}
        {running && st.progress?.percent == null && st.progress?.files_done
          ? `${fmt(st.progress.files_done)} files read · ` : ''}
        {lb.at ? `Last backup ${ago(lb.at)}` : 'Never backed up'}
        {lb.at && lb.data_added != null ? ` · ${fmtBytes(lb.data_added)} added` : ''}
        {card.next_run ? ` · next ${when(card.next_run)}` : card.schedule?.kind === 'manual' ? ' · manual' : ''}
        {card.last_verify?.at ? ` · verified ${ago(card.last_verify.at)}${card.last_verify.status === 'success' ? ' ✓' : ' ✗'}` : ''}
      </div>
      {st.reason && !running && st.state !== 'up_to_date' && (
        <div style={{ fontSize: 11, color: look.color }}>{st.reason}</div>
      )}
    </div>
  );
};

// ── the page ─────────────────────────────────────────────────────────────────

const Dashboard = () => {
  const { activeCatalog } = useCatalog();
  const navigate = useNavigate();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const catalogId = activeCatalog?.id;
  const lastLoad = useRef(0);

  const load = useCallback(async () => {
    if (!catalogId) return;
    lastLoad.current = Date.now();
    try {
      const r = await dashboardAPI.get(catalogId);
      setData(r.data);
      setError(null);
    } catch (e) {
      setError(e.response?.data?.error || 'Could not load the dashboard.');
    }
  }, [catalogId]);

  useEffect(() => { setData(null); load(); }, [load]);

  // While something runs (or the counts are being rebuilt), a workflow's
  // progress events nudge a reload - at most every 5 s, and never while the
  // tab is hidden. The request is milliseconds.
  useEffect(() => {
    const off = socketService.onWorkflowProgress(() => {
      if (document.visibilityState === 'visible' && Date.now() - lastLoad.current > 5000) load();
    });
    return () => { if (typeof off === 'function') off(); };
  }, [load]);
  // A backup reports no workflow progress: while one runs, ask every 10 s.
  useEffect(() => {
    if (!data?.live?.backups?.running) return undefined;
    const t = setTimeout(() => { if (document.visibilityState === 'visible') load(); }, 10000);
    return () => clearTimeout(t);
  }, [data, load]);
  useEffect(() => {
    if (!data?.stats_rebuilding) return undefined;
    const t = setTimeout(() => { if (document.visibilityState === 'visible') load(); }, 4000);
    return () => clearTimeout(t);
  }, [data, load]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const r = await dashboardAPI.refresh(catalogId);
      setData(r.data);
      setError(null);
    } catch (e) {
      setError(e.response?.data?.error || 'Could not refresh the stats.');
    } finally {
      setRefreshing(false);
    }
  };

  if (!activeCatalog) {
    return (
      <CContainer className="py-4">
        <CCard><CCardBody className="text-body-secondary">
          No catalog selected. Choose a catalog to see its dashboard.
        </CCardBody></CCard>
      </CContainer>
    );
  }
  if (!data) {
    return (
      <CContainer className="py-5 text-center">
        {error ? <CAlert color="danger">{error}</CAlert> : <CSpinner />}
      </CContainer>
    );
  }

  const { stats, live, on_this_day: otd } = data;
  const lib = stats.library;
  const att = stats.attention;
  const total = lib.photos.count + lib.videos.count;
  const go = (path) => () => navigate(path);

  const attention = [
    live.failed_7d > 0 && {
      icon: cilXCircle, color: STATUS.critical, n: live.failed_7d,
      label: 'workflow runs failed or interrupted in the last 7 days', to: '/workflow-executions',
    },
    live.alerts?.critical > 0 && {
      icon: cilXCircle, color: STATUS.critical, n: live.alerts.critical,
      label: 'critical storage alerts', to: '/alerts',
    },
    live.alerts?.active - (live.alerts?.critical || 0) > 0 && {
      icon: cilWarning, color: STATUS.warning, n: live.alerts.active - live.alerts.critical,
      label: 'storage warnings', to: '/alerts',
    },
    live.backups?.failed > 0 && {
      icon: cilXCircle, color: STATUS.critical, n: live.backups.failed,
      label: `backup${live.backups.failed === 1 ? '' : 's'} failed`, to: '/catalogs',
    },
    live.saved_searches_due > 0 && {
      icon: cilWarning, color: STATUS.warning, n: live.saved_searches_due,
      label: `saved search${live.saved_searches_due === 1 ? '' : 'es'} will be deleted within 7 days`,
      to: '/saved-searches',
    },
    live.backups && live.backups.count === 0 && {
      icon: cilWarning, color: STATUS.warning, n: null,
      label: 'No backup is set up for this catalog', to: '/catalogs',
    },
    live.backups?.needed > 0 && {
      icon: cilWarning, color: STATUS.warning, n: live.backups.needed,
      label: `backup${live.backups.needed === 1 ? ' needs' : 's need'} running`, to: '/catalogs',
    },
    att.faces_unidentified > 0 && {
      icon: cilFace, color: STATUS.warning, n: att.faces_unidentified,
      label: 'faces not yet named', to: '/identify-people',
    },
    att.face_groups > 0 && {
      icon: cilPeople, color: STATUS.warning, n: att.face_groups,
      label: 'groups of faces to name', to: '/identify-groups',
    },
    att.pet_groups > 0 && {
      icon: cilPaw, color: STATUS.warning, n: att.pet_groups,
      label: 'groups of pets to name', to: '/identify-pets',
    },
    att.locations_proposed > 0 && {
      icon: cilLocationPin, color: STATUS.warning, n: att.locations_proposed,
      label: 'proposed locations to confirm', to: '/locations',
    },
  ].filter(Boolean);

  const act = live.activity;
  const footprint = [
    { name: 'Originals', bytes: stats.catalog_bytes.originals, color: PHOTO },
    { name: 'Previews', bytes: stats.catalog_bytes.previews, color: VIDEO },
    { name: 'Database', bytes: live.database_bytes, color: MUTED },
  ];
  const footprintTotal = footprint.reduce((s, f) => s + (f.bytes || 0), 0);

  return (
    <CContainer fluid className="py-3">
      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}

      <div className="d-flex align-items-baseline justify-content-between flex-wrap gap-2 mb-3">
        <h4 className="mb-0 fw-bold">Dashboard <span className="text-body-secondary fw-normal">· {data.catalog.name}</span></h4>
        <div className="small text-body-secondary d-flex align-items-center gap-2">
          <span title={data.stats_computed_at || ''}>
            Updated {ago(data.stats_computed_at) || '–'}
            {(data.stats_rebuilding || data.stats_stale) && ' · updating…'}
          </span>
          <CButton color="link" size="sm" className="p-0" onClick={refresh} disabled={refreshing}>
            {refreshing ? <CSpinner size="sm" /> : <CIcon icon={cilReload} style={{ width: 12, height: 12 }} />}
            <span className="ms-1">Refresh stats</span>
          </CButton>
        </div>
      </div>

      {/* The library at a glance */}
      <CRow className="g-3 mb-3" xs={{ cols: 2 }} md={{ cols: 3 }} xl={{ cols: 6 }}>
        <CCol><Tile icon={cilImage} label="Photos" value={lib.photos.count}
                    sub={fmtBytes(lib.photos.bytes)} onClick={go('/photos')} /></CCol>
        <CCol><Tile icon={cilMediaPlay} label="Videos" value={lib.videos.count}
                    sub={fmtBytes(lib.videos.bytes)} onClick={go('/videos')} /></CCol>
        <CCol><Tile icon={cilPeople} label="People" value={lib.people} onClick={go('/people')} /></CCol>
        <CCol><Tile icon={cilPaw} label="Pets" value={lib.pets} onClick={go('/pets')} /></CCol>
        <CCol><Tile icon={cilCalendar} label="Events" value={lib.events} onClick={go('/events')} /></CCol>
        <CCol><Tile icon={cilLocationPin} label="Locations" value={lib.locations.confirmed}
                    sub={lib.locations.proposed ? `${fmt(lib.locations.proposed)} proposed` : null}
                    onClick={go('/locations')} /></CCol>
      </CRow>

      <CRow className="g-3 mb-3">
        {/* The last 12 months, as Calendar view draws years
            (calendar-view-spec.md 7): a slice of the cached counts. */}
        {live.calendar && (
          <CCol lg={4}>
            <Panel title={<><CIcon icon={cilCalendar} className="me-2" />The last 12 months
                     <span className="text-body-secondary fw-normal small ms-2">
                       {fmt(live.calendar.days.reduce((n, r) => n + r[3] + r[4], 0))} photos and videos
                     </span></>}
                   extra={(
                     <CButton color="link" size="sm" className="p-0" onClick={go('/calendar-view')}>
                       Calendar view ›
                     </CButton>
                   )}>
              <CalendarHeatmap days={live.calendar.days} blocks={rollingYearBlock()} cell={13} minCell={5}
                               busiest={live.calendar.busiest} showTotals={false}
                               onDayClick={(row) => navigate(linkFor(row))}
                               ariaLabel="Photos and videos per day, the last 12 months" />
              <div className="mt-3"><CalendarLegend busiest={live.calendar.busiest} /></div>
            </Panel>
          </CCol>
        )}
        <CCol lg={live.calendar ? 8 : 12}>
          <Panel title="Your library over time"
                 extra={<span className="small text-body-secondary fw-normal">
                   <Swatch color={PHOTO} />Photos <Swatch color={VIDEO} />Videos
                 </span>}>
            {stats.years.length === 0 ? (
              <div className="text-body-secondary small">Nothing imported yet.</div>
            ) : (
              <ResponsiveContainer width="100%" height={170}>
                <BarChart data={stats.years} margin={{ top: 4, right: 4, left: 0, bottom: 0 }}
                          onClick={go('/photos')}>
                  <XAxis dataKey="year" tick={{ fontSize: 11, fill: MUTED }} tickLine={false}
                         axisLine={{ stroke: TRACK }} interval="preserveStartEnd" />
                  <YAxis tick={{ fontSize: 11, fill: MUTED }} tickLine={false} axisLine={false}
                         width={44} tickFormatter={(v) => (v >= 1000 ? `${Math.round(v / 1000)}k` : v)} />
                  <Tooltip cursor={{ fill: 'rgba(0,0,0,0.04)' }}
                           formatter={(v, n) => [fmt(v), n === 'photos' ? 'Photos' : 'Videos']} />
                  <Bar dataKey="photos" stackId="a" fill={PHOTO} isAnimationActive={false} />
                  <Bar dataKey="videos" stackId="a" fill={VIDEO} radius={[4, 4, 0, 0]}
                       isAnimationActive={false} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Panel>
        </CCol>
      </CRow>

      <CRow className="g-3 mb-3">
        <CCol lg={4}>
          <Panel title="Needs attention">
            {attention.length === 0 ? (
              <div className="d-flex align-items-center gap-2 text-body-secondary">
                <CIcon icon={cilCheckCircle} style={{ color: STATUS.good }} />
                Nothing needs you right now.
              </div>
            ) : attention.map((a) => (
              <div key={a.label} className="d-flex align-items-center gap-2 py-1"
                   style={{ cursor: 'pointer' }} onClick={go(a.to)} role="link">
                <CIcon icon={a.icon} style={{ width: 16, height: 16, color: a.color, flexShrink: 0 }} />
                {a.n !== null && <strong>{fmt(a.n)}</strong>}
                <span className="text-body-secondary">{a.label}</span>
              </div>
            ))}
          </Panel>
        </CCol>
        <CCol lg={4}>
          <Panel title="Activity"
                 extra={<CButton color="link" size="sm" className="p-0" onClick={go('/workflows')}>
                   Workflows</CButton>}>
            {act.running.length === 0 ? (
              <div className="small text-body-secondary mb-2">Nothing running.</div>
            ) : act.running.map((r) => {
              const p = r.total_files ? pct(r.processed_files, r.total_files) : null;
              return (
                <div key={r.id} className="mb-2">
                  <div className="d-flex justify-content-between small">
                    <strong>{r.workflow_name}</strong>
                    <span className="text-body-secondary">
                      {fmt(r.processed_files)}{r.total_files ? ` of ${fmt(r.total_files)}` : ''}
                    </span>
                  </div>
                  <CProgress height={8} className="mt-1">
                    <CProgressBar value={p ?? 100} animated={p === null} color="primary" />
                  </CProgress>
                </div>
              );
            })}
            {act.queued > 0 && (
              <div className="small text-body-secondary mb-2">
                {fmt(act.queued)} queued{act.next ? ` · next: ${act.next}` : ''}
              </div>
            )}
            {act.last_import && (
              <div className="small mb-2" style={{ cursor: 'pointer' }} role="link"
                   onClick={go(`/import-progress?execution=${act.last_import.id}`)}>
                <span className="text-body-secondary">Last import: </span>
                <strong>{act.last_import.workflow_name}</strong>{' '}
                <CBadge color={STATUS_BADGE[act.last_import.status] || 'secondary'}>
                  {act.last_import.status}</CBadge>{' '}
                <span className="text-body-secondary">
                  {act.last_import.new_files != null && `${fmt(act.last_import.new_files)} new · `}
                  {act.last_import.duplicate_files != null && `${fmt(act.last_import.duplicate_files)} duplicates · `}
                  {ago(act.last_import.started_at)}
                </span>
              </div>
            )}
            <div className="small text-body-secondary mt-2 mb-1">Recently</div>
            {act.recent.length === 0 ? (
              <div className="small text-body-secondary">No runs yet.</div>
            ) : act.recent.map((r) => (
              <div key={r.id} className="d-flex justify-content-between small py-1"
                   style={{ borderTop: '1px solid var(--cui-border-color)' }}>
                <span className="text-truncate me-2">{r.workflow_name}</span>
                <span className="text-nowrap">
                  <CBadge color={STATUS_BADGE[r.status] || 'secondary'} className="me-2">{r.status}</CBadge>
                  <span className="text-body-secondary">{ago(r.started_at)}</span>
                </span>
              </div>
            ))}
          </Panel>
        </CCol>
        <CCol lg={4}>
          <Panel title={<><CIcon icon={cilBirthdayCake} className="me-2" />Coming up</>}>
            {live.birthdays.length === 0 ? (
              <div className="small text-body-secondary">No birthdays in the next two weeks.</div>
            ) : live.birthdays.map((b) => (
              <div key={b.id} className="small py-1" style={{ cursor: 'pointer' }} role="link"
                   onClick={go(`/people/${catalogId}/${b.id}`)}>
                <strong>{b.name}</strong>
                <span className="text-body-secondary">
                  {' '}{b.in_days === 0 ? 'today' : b.in_days === 1 ? 'tomorrow' : `in ${b.in_days} days`}
                  {b.turning ? ` · turns ${b.turning}` : ''}
                </span>
              </div>
            ))}
            <div className="small mt-3" style={{ cursor: 'pointer' }} role="link"
                 onClick={go('/albums/deployments')}>
              <CIcon icon={cilShareAlt} className="me-1" style={{ width: 13, height: 13 }} />
              <strong>{fmt(live.albums.albums)}</strong>
              <span className="text-body-secondary"> albums · {fmt(live.albums.deployments)} published</span>
              {live.albums.latest && (
                <div className="text-body-secondary" style={{ fontSize: 11 }}>
                  Latest: {live.albums.latest.name}, {ago(live.albums.latest.updated_at)}
                </div>
              )}
            </div>
          </Panel>
        </CCol>
      </CRow>

      <CRow className="g-3 mb-3">
        <CCol md={6} xl={3}>
          <Panel title="Photos and videos">
            <div style={{ position: 'relative', height: 180 }}>
              <ResponsiveContainer width="100%" height="100%">
                <PieChart>
                  <Pie data={[{ name: 'Photos', value: lib.photos.count, bytes: lib.photos.bytes },
                              { name: 'Videos', value: lib.videos.count, bytes: lib.videos.bytes }]}
                       dataKey="value" nameKey="name" innerRadius={58} outerRadius={80}
                       paddingAngle={1} stroke="#fff" strokeWidth={2} isAnimationActive={false}>
                    <Cell fill={PHOTO} />
                    <Cell fill={VIDEO} />
                  </Pie>
                  <Tooltip formatter={(v, n, p) => [`${fmt(v)} · ${fmtBytes(p.payload.bytes)}`, n]} />
                </PieChart>
              </ResponsiveContainer>
              <div style={{ position: 'absolute', inset: 0, display: 'flex', flexDirection: 'column',
                            alignItems: 'center', justifyContent: 'center', pointerEvents: 'none' }}>
                <div className="fw-bold" style={{ fontSize: '1.2rem' }}>{fmt(total)}</div>
                <div className="small text-body-secondary">items</div>
              </div>
            </div>
            <div className="d-flex justify-content-center gap-4 small mt-2">
              <span><Swatch color={PHOTO} />Photos {pct(lib.photos.count, total)}%</span>
              <span><Swatch color={VIDEO} />Videos {pct(lib.videos.count, total)}%</span>
            </div>
          </Panel>
        </CCol>
        {/* The Content report's "What we know about it", compact - as last
            counted there (its cache; recounting is the report's job). */}
        <CCol md={6} xl={3}>
          <Panel title="What we know about it"
                 extra={(
                   <CButton color="link" size="sm" className="p-0" onClick={go('/content-report')}>
                     Report ›
                   </CButton>
                 )}>
            {live.coverage ? (
              <>
                {live.coverage.rows.map((r) => <KnownRow key={r.what} {...r} />)}
                {live.coverage.generated_at && (
                  <div className="text-body-secondary mt-1" style={{ fontSize: 11 }}>
                    As counted {ago(live.coverage.generated_at)} by the Content report
                  </div>
                )}
              </>
            ) : (
              <div className="small text-body-secondary">
                Not counted yet. <CButton color="link" size="sm" className="p-0 align-baseline"
                                          onClick={go('/content-report')}>Open the Content report</CButton>{' '}
                to count it.
              </div>
            )}
          </Panel>
        </CCol>
        <CCol xl={6}>
          <Panel title="How far the automatic work has reached">
            <div className="d-flex flex-wrap justify-content-around gap-2">
              {stats.coverage.map((c) => <Ring key={c.key} label={c.label} done={c.done} of={c.of} />)}
            </div>
          </Panel>
        </CCol>
      </CRow>

      {otd.years.length > 0 && (
        <Panel className="mb-3" title={`On this day · ${new Date(`${otd.date}T00:00:00`)
          .toLocaleDateString(undefined, { month: 'long', day: 'numeric' })}`}>
          <div className="d-flex gap-3" style={{ overflowX: 'auto' }}>
            {otd.years.map((y) => (
              <div key={y.year} style={{ width: 140, flexShrink: 0, cursor: 'pointer' }} role="link"
                   onClick={go(`/photos/${catalogId}/${y.file_id}`)}>
                <div style={{ width: 140, height: 140, borderRadius: 6, overflow: 'hidden',
                              background: TRACK }}>
                  <img src={galleryThumbUrl(catalogId, y.file_id)} alt={`${y.year}`} loading="lazy"
                       style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
                </div>
                <div className="small mt-1"><strong>{y.year}</strong>
                  <span className="text-body-secondary"> · {fmt(y.count)} photo{y.count === 1 ? '' : 's'}</span>
                </div>
              </div>
            ))}
          </div>
        </Panel>
      )}

      <CRow className="g-3 mb-3">
        <CCol lg={4}>
          <Panel title={<><CIcon icon={cilStorage} className="me-2" />Storage</>}>
            {live.storage.length === 0 ? (
              <div className="small text-body-secondary">No drive could be read.</div>
            ) : live.storage.map((d) => <DriveBar key={d.mount} drive={d} />)}
            <div className="small text-body-secondary mt-3 mb-1">
              What this catalog holds · {fmtBytes(footprintTotal)}
            </div>
            <div className="d-flex" style={{ height: 10, borderRadius: 5, overflow: 'hidden', gap: 2 }}>
              {footprint.filter((f) => f.bytes > 0).map((f) => (
                <div key={f.name} title={`${f.name}: ${fmtBytes(f.bytes)}`}
                     style={{ width: `${(100 * f.bytes) / footprintTotal}%`, background: f.color }} />
              ))}
            </div>
            <div className="d-flex flex-wrap gap-3 small mt-1">
              {footprint.map((f) => (
                <span key={f.name}><Swatch color={f.color} />{f.name} {fmtBytes(f.bytes)}</span>
              ))}
            </div>
          </Panel>
        </CCol>
        <CCol lg={4}>
          <Panel title={<><CIcon icon={cilCloudUpload} className="me-2" />Backups</>}
                 extra={(
                   <CButton color="link" size="sm" className="p-0" onClick={go('/catalogs?tab=backup')}>
                     Manage
                   </CButton>
                 )}>
            {!live.backups || live.backups.count === 0 ? (
              <div className="small">
                <div className="d-flex align-items-center gap-2" style={{ color: STATUS.warning, fontWeight: 600 }}>
                  <CIcon icon={cilWarning} style={{ width: 14, height: 14 }} />
                  Not backed up
                </div>
                <div className="text-body-secondary mt-1">
                  {activeCatalog.name} has no backup set up. Add one in the catalog&apos;s{' '}
                  <span role="link" style={{ cursor: 'pointer', textDecoration: 'underline' }}
                        onClick={go('/catalogs?tab=backup')}>Backup tab</span>.
                </div>
              </div>
            ) : live.backups.cards.map((c) => <BackupLine key={c.id} card={c} />)}
          </Panel>
        </CCol>
        <CCol lg={4}>
          <Panel title="Most photographed">
            {stats.top_people.length === 0 ? (
              <div className="small text-body-secondary">Nobody named in a photograph yet.</div>
            ) : (
              <ResponsiveContainer width="100%" height={stats.top_people.length * 32 + 10}>
                <BarChart data={stats.top_people} layout="vertical"
                          margin={{ top: 0, right: 40, left: 0, bottom: 0 }}>
                  <XAxis type="number" hide />
                  <YAxis type="category" dataKey="name" width={80} tickLine={false} axisLine={false}
                         tick={{ fontSize: 12, fill: '#212529' }} />
                  <Tooltip cursor={{ fill: 'rgba(0,0,0,0.04)' }}
                           formatter={(v) => [fmt(v), 'Photographs']} />
                  <Bar dataKey="photos" fill={PHOTO} radius={[0, 4, 4, 0]} barSize={14}
                       isAnimationActive={false}
                       label={{ position: 'right', fontSize: 11, fill: MUTED, formatter: fmt }}
                       onClick={(d) => navigate(`/people/${catalogId}/${d.id}`)}
                       style={{ cursor: 'pointer' }} />
                </BarChart>
              </ResponsiveContainer>
            )}
          </Panel>
        </CCol>
      </CRow>
    </CContainer>
  );
};

export default Dashboard;
