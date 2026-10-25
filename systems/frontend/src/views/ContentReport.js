import React, { useState, useEffect, useCallback } from 'react';
import {
  CContainer, CButton, CSpinner, CAlert, CBadge,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilChevronLeft, cilChevronRight, cilReload,
} from '@coreui/icons';
import {
  BarChart, Bar, XAxis, YAxis, CartesianGrid, ResponsiveContainer, Tooltip, Legend,
} from 'recharts';
import { catalogAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import {
  Figure, FigureRow, Section, CoverageRow, MiniTable, Meter, n, bytes,
} from '../components/ReportFigures';

// ─── Content report ──────────────────────────────────────────────────────────
// documentation/specs/contents-spec.md. What is in this catalog, and what is
// missing from it - the second half being the point. The page answers three
// questions without the reader running a query: how much is here and from when,
// how good is what we know about it, and what is incomplete.
//
// Every number names its source in the spec, and nothing here reads
// raw_exif_json. The sections arrive in ONE request: they share a connection
// and several share a scan, so nine requests would pay nine times over.

// The provenance colours are deliberately a traffic light: a camera timestamp
// is a measurement and everything else is an inference (spec section 5.3).
const PROVENANCE = {
  exif: { label: 'Camera (EXIF)', color: '#2e7d32' },
  directory: { label: 'Folder name', color: '#ef6c00' },
  filename: { label: 'File name', color: '#ef6c00' },
  filesystem: { label: 'File timestamp', color: '#c62828' },
  system: { label: 'Import time', color: '#c62828' },
  unknown: { label: 'Unknown', color: '#9e9e9e' },
};

// The same traffic light applied to location: a coordinate the camera recorded
// is a measurement, a location a person typed is a judgement, and one inherited
// from the rest of an event is an inference about this photo (spec 8.4).
const LOCATION_SOURCES = [
  { key: 'cluster', label: "From the photo's own coordinates", color: '#2e7d32' },
  { key: 'manual', label: 'Tagged by hand', color: '#1a73e8' },
  { key: 'event_tagged', label: 'Tagged on its event', color: '#ef6c00' },
  { key: 'event_voted', label: 'Agreed by its event', color: '#ef6c00' },
  { key: 'without_location', label: 'No location yet', color: '#9e9e9e' },
];

const shortDate = (value) => {
  if (!value) return '—';
  const d = new Date(value);
  return Number.isNaN(d.getTime()) ? value
    : d.toLocaleDateString('en-US', { year: 'numeric', month: 'short', day: 'numeric' });
};

const ContentReport = () => {
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;

  const [report, setReport] = useState(null);
  const [time, setTime] = useState(null);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState(null);

  const loadReport = useCallback(async (refresh = false) => {
    if (!catalogId) { setLoading(false); return; }
    refresh ? setRefreshing(true) : setLoading(true);
    try {
      const res = await catalogAPI.getContentReport(catalogId, { refresh });
      setReport(res.data);
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not build the content report.');
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [catalogId]);

  const loadTime = useCallback(async (startYear = null, endYear = null) => {
    if (!catalogId) return;
    try {
      const res = await catalogAPI.getContentByTime(catalogId, startYear, endYear);
      setTime(res.data);
    } catch {
      /* the chart is one section; its failure must not take the page */
    }
  }, [catalogId]);

  useEffect(() => { loadReport(); loadTime(); }, [loadReport, loadTime]);

  if (!catalogId) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="info">Select a catalog to see its content report.</CAlert>
      </CContainer>
    );
  }
  if (loading) {
    return (
      <CContainer fluid className="px-4 py-4">
        <div className="text-center py-5"><CSpinner color="primary" /></div>
      </CContainer>
    );
  }

  const t = report?.totals || {};
  const prov = report?.provenance || { sources: [], inferred: 0 };
  const locs = report?.locations || { available: false };

  // ── The time chart ─────────────────────────────────────────────────────────
  // Stacked by kind, so a year of video is distinguishable from a year of
  // photographs, and marked where the month's position is inferred.
  const chart = [];
  let windowInferred = 0;
  (time?.data || []).forEach((year) => {
    year.months.forEach((m) => {
      windowInferred += m.inferred || 0;
      chart.push({
        label: `${m.month_name} ${String(year.year).slice(2)}`,
        stills: m.stills ?? m.count ?? 0,
        videos: m.videos ?? 0,
        inferred: m.inferred ?? 0,
      });
    });
  });

  return (
    <CContainer fluid className="px-4 py-4">
      <div className="d-flex justify-content-between align-items-start mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Content report</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            {report?.catalog_name} — what is in this catalog, and what is missing from it
          </div>
        </div>
        <div className="text-end">
          <CButton color="secondary" variant="outline" size="sm"
                   disabled={refreshing} onClick={() => loadReport(true)}>
            {refreshing ? <CSpinner size="sm" /> : <CIcon icon={cilReload} />}
            <span className="ms-2">Recount</span>
          </CButton>
          <div style={{ fontSize: '0.7rem', color: '#aaa', marginTop: 4 }}>
            {/* Said plainly: a cached report is not a stale one here - the cache
                is keyed on a fingerprint of the catalog, so it is only served
                while nothing the report counts has changed. */}
            {report?.from_cache
              ? `cached, unchanged since ${shortDate(report.generated_at)}`
              : `counted in ${report?.built_in_seconds ?? '?'}s`}
          </div>
        </div>
      </div>

      {error && <CAlert color="danger">{error}</CAlert>}

      {/* ── The top row: the library, and what has left it ────────────────
          Three parts to one, because the two answer questions of different
          weight. "How much is here" is what the page is opened for. "What was
          removed or duplicated" is a footnote that still has to be visible:
          every other figure on this page silently excludes the removed files,
          and the reader is owed the number being excluded. */}
      <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', alignItems: 'stretch' }}>
        <div style={{ flex: '3 1 480px', minWidth: 0 }}>
        <Section title="Library">
          <FigureRow>
            <Figure value={n(t.live_files)} label="Files" title="Live files; removed ones are excluded from every figure on this page" />
            <Figure value={bytes(t.total_bytes)} label="Originals" title="Total size of the original files" />
            <Figure value={n(t.stills)} label="Photos" title="Stills, including RAW" />
            <Figure value={n(t.videos)} label="Videos" title="By file extension, not by whether a preview was made" />
            <Figure value={n(t.raw)} label="RAW" title="Camera RAW originals" color="#6a1b9a" />
            <Figure value={shortDate(t.earliest)} label="Earliest" sub={shortDate(t.latest) + ' latest'} title="By each file's authoritative date" />
            {t.removed_files > 0 && (
              <Figure value={n(t.removed_files)} label="Removed" color="#757575"
                      title="Soft-removed: excluded from every other figure here" />
            )}
          </FigureRow>
        </Section>
        </div>
        <div style={{ flex: '1 1 220px', minWidth: 0 }}>
        <Section title="Removed and duplicated">
          <FigureRow>
            <Figure value={n(report?.removed?.removed_files)} label="Soft-removed" color="#757575"
                    title="Excluded from every other figure on this page" />
            <Figure value={n(report?.removed?.duplicate_groups)} label="Duplicate contents"
                    color={report?.removed?.duplicate_groups > 0 ? '#ef6c00' : '#2e7d32'}
                    title="Groups of live files sharing one SHA-256" />
          </FigureRow>
          {/* Near-duplicates are ABSENT, not zero: perceptual_hash has no source
              filling it, so there is no number to show (spec section 7). */}
          {report?.removed?.perceptual_hashed > 0 && (
            <div style={{ fontSize: '0.75rem', color: '#999', marginTop: 8 }}>
              {n(report.removed.perceptual_hashed)} file(s) have a perceptual hash.
            </div>
          )}
        </Section>
        </div>
      </div>


      {/* ── 5.2 Time distribution ── */}
      <Section
        title="Files over time"
        hint={windowInferred > 0
          ? `${n(windowInferred)} file(s) in this window are placed by inference, not by a camera timestamp`
          : 'Every file in this window is placed by its camera timestamp'}
        right={time && (
          <div className="d-flex align-items-center gap-2">
            <span style={{ fontSize: '0.78rem', color: '#888' }}>
              {time.start_year}–{time.end_year}
            </span>
            <CButton color="secondary" variant="outline" size="sm"
                     disabled={!time.has_previous}
                     onClick={() => loadTime(time.start_year - 10, time.start_year - 1)}>
              <CIcon icon={cilChevronLeft} />
            </CButton>
            <CButton color="secondary" variant="outline" size="sm"
                     disabled={!time.has_next}
                     onClick={() => loadTime(time.end_year + 1, time.end_year + 10)}>
              <CIcon icon={cilChevronRight} />
            </CButton>
          </div>
        )}
      >
        {chart.length === 0 ? (
          <div className="text-muted" style={{ fontSize: '0.85rem' }}>No dated files yet.</div>
        ) : (
          <div style={{ height: 300 }}>
            <ResponsiveContainer width="100%" height="100%">
              <BarChart data={chart}>
                <CartesianGrid strokeDasharray="3 3" />
                <XAxis dataKey="label" interval="preserveStartEnd"
                       tick={{ fontSize: 11 }} angle={-45} textAnchor="end" height={60} />
                <YAxis tick={{ fontSize: 11 }} />
                <Tooltip formatter={(v, name) => [n(v), name]} />
                <Legend wrapperStyle={{ fontSize: '0.78rem' }} />
                <Bar dataKey="stills" stackId="k" name="Photos" fill="#1976d2" />
                <Bar dataKey="videos" stackId="k" name="Videos" fill="#7b1fa2" />
              </BarChart>
            </ResponsiveContainer>
          </div>
        )}
      </Section>

      {/* ── Three columns: what we know, what can be shown, what came in ──
            Order is a reading order, not an arrangement. The first column is the
            state of the library; the middle is what can actually be displayed,
            with the date provenance beneath it because that provenance is the
            chart above's margin of error and belongs near the coverage it
            qualifies; the third is where it all came from.

            The middle column carries two sections stacked - it is the only one
            with a short table AND a short bar, so it absorbs the extra height
            the other two do not have. */}
      <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap', alignItems: 'flex-start' }}>
        <div style={{ flex: '1 1 320px', minWidth: 0 }}>
          <Section title="What we know about it"
                   hint="Each line is a pair: how much is covered, and how much is not">
            {(report?.coverage || []).map((row) => (
              <CoverageRow key={row.what} {...row} />
            ))}
          </Section>
        </div>
        <div style={{ flex: '1 1 320px', minWidth: 0 }}>
          <Section
            title="Previews and proxies"
            hint="A photo with no display image cannot be shown; a video with no proxy will not play. A deployment can only pack what exists."
          >
            <MiniTable
              columns={[
                { key: 'rung', label: 'Size' },
                { key: 'files', label: 'Have it', numeric: true, render: (r) => n(r.files) },
                { key: 'denominator', label: 'Of', numeric: true, render: (r) => n(r.denominator) },
                { key: 'missing', label: 'Missing', numeric: true,
                  render: (r) => (
                    <span style={{ color: r.missing > 0 ? '#ef6c00' : '#2e7d32', fontWeight: 500 }}>
                      {n(r.missing)}
                    </span>
                  ) },
                { key: 'bar', label: '', render: (r) => (
                  <div style={{ width: 120 }}>
                    <Meter covered={r.files} total={r.denominator}
                           color={r.missing > 0 ? '#ef6c00' : '#2e7d32'} height={6} />
                </div>
                ) },
              ]}
              rows={(report?.rungs || []).map((r) => ({ ...r, key: r.rung }))}
            />
            <div style={{ fontSize: '0.72rem', color: '#999', marginTop: 6 }}>
              Photo sizes are counted against photos and video sizes against videos — a photo
              size compared with every file would overstate its gap several times over.
          </div>
          </Section>
          <Section
            title="Where each date came from"
            hint="The chart above is built on these dates, so this is its margin of error"
          >
            <div style={{ display: 'flex', height: 14, borderRadius: 7, overflow: 'hidden',
                          marginBottom: 10 }}>
              {prov.sources.map((src) => {
                const meta = PROVENANCE[src.source] || PROVENANCE.unknown;
                const pct = t.live_files ? (src.files / t.live_files) * 100 : 0;
                return (
                  <div key={src.source} title={`${meta.label}: ${n(src.files)}`}
                       style={{ width: `${pct}%`, background: meta.color }} />
                );
              })}
          </div>
            <MiniTable
              columns={[
                { key: 'label', label: 'Source' },
                { key: 'files', label: 'Files', numeric: true, render: (r) => n(r.files) },
                { key: 'share', label: 'Share', numeric: true,
                  render: (r) => (t.live_files ? `${((r.files / t.live_files) * 100).toFixed(1)}%` : '—') },
              ]}
              rows={prov.sources.map((src) => ({
                key: src.source, files: src.files,
                label: (
                  <span>
                    <span style={{ display: 'inline-block', width: 8, height: 8, borderRadius: 2,
                                   background: (PROVENANCE[src.source] || PROVENANCE.unknown).color,
                                   marginRight: 6 }} />
                    {(PROVENANCE[src.source] || PROVENANCE.unknown).label}
                  </span>
                ),
              }))}
            />
            {prov.inferred > 0 && (
              <div style={{ fontSize: '0.75rem', color: '#ef6c00', marginTop: 8 }}>
                {n(prov.inferred)} file(s) sit on the time axis at a position inferred from a
                path rather than read from a camera.
            </div>
            )}
          </Section>
          <Section
            title="Where each location came from"
            hint="How much of the map is evidence, and how much is inference"
          >
            {!locs.available ? (
              /* Not the same as "nothing has a location": this catalog has no
                 file_locations table, so the location workflows have never
                 run here. Saying so is the useful answer. */
              <div style={{ fontSize: '0.8rem', color: '#888' }}>
                Locations have not been resolved for this catalog. Run{' '}
                <strong>Extract Locations</strong> and{' '}
                <strong>Resolve Locations</strong>.
              </div>
            ) : (
              <>
                <div style={{ display: 'flex', height: 14, borderRadius: 7,
                              overflow: 'hidden', marginBottom: 10 }}>
                  {LOCATION_SOURCES.map((src) => {
                    const files = locs[src.key] || 0;
                    const pct = t.live_files ? (files / t.live_files) * 100 : 0;
                    if (!pct) return null;
                    return (
                      <div key={src.key} title={`${src.label}: ${n(files)}`}
                           style={{ width: `${pct}%`, background: src.color }} />
                    );
                  })}
                </div>
                <MiniTable
                  columns={[
                    { key: 'label', label: 'Source' },
                    { key: 'files', label: 'Files', numeric: true, render: (r) => n(r.files) },
                    { key: 'share', label: 'Share', numeric: true,
                      render: (r) => (t.live_files ? `${((r.files / t.live_files) * 100).toFixed(1)}%` : '—') },
                  ]}
                  rows={LOCATION_SOURCES.map((src) => ({
                    key: src.key, files: locs[src.key] || 0,
                    label: (
                      <span>
                        <span style={{ display: 'inline-block', width: 8, height: 8,
                                       borderRadius: 2, background: src.color,
                                       marginRight: 6 }} />
                        {src.label}
                      </span>
                    ),
                  }))}
                />
                <div style={{ fontSize: '0.72rem', color: '#999', marginTop: 6 }}>
                  {n(locs.measured)} file(s) carry a coordinate the camera recorded.
                  A location is only ever inherited from an event — never a
                  coordinate, which would make every distance here a guess.
                </div>
                {locs.clusters_proposed > 0 && (
                  <div style={{ fontSize: '0.75rem', color: '#1a73e8', marginTop: 6 }}>
                    {n(locs.clusters_proposed)} proposed location(s) are waiting to be named.
                  </div>
                )}
              </>
            )}
          </Section>
        </div>
        <div style={{ flex: '1 1 320px', minWidth: 0 }}>
      <Section
          title="Imports"
          hint="From what each run recorded when it ended — never from the item rows, which retention prunes"
      >
          {(report?.ingestion?.runs || []).length === 0 ? (
            <div className="text-muted" style={{ fontSize: '0.85rem' }}>No imports yet.</div>
          ) : (
            <MiniTable
              columns={[
                { key: 'started_at', label: 'Started', render: (r) => shortDate(r.started_at) },
                { key: 'status', label: 'Status', render: (r) => (
                  <CBadge color={r.status === 'completed' ? 'success'
                    : r.status === 'failed' ? 'danger'
                      : r.status === 'running' ? 'info' : 'secondary'}
                          style={{ fontSize: '0.66rem' }}>
                    {r.status}
                  </CBadge>
                ) },
                { key: 'new_files', label: 'New', numeric: true,
                  render: (r) => (r.recorded ? n(r.new_files) : '—') },
                { key: 'duplicate_files', label: 'Duplicates', numeric: true,
                  render: (r) => (r.recorded ? n(r.duplicate_files) : '—') },
                { key: 'ignored_files', label: 'Ignored', numeric: true,
                  render: (r) => (r.recorded ? n(r.ignored_files) : '—') },
                { key: 'recorded', label: '', render: (r) => (r.recorded ? '' : (
                  <span style={{ fontSize: '0.68rem', color: '#b26500' }}
                        title="This run finished before its counts were recorded. The numbers are not available, and showing zero would be a lie.">
                    not recorded
                  </span>
                )) },
              ]}
              rows={(report?.ingestion?.runs || []).map((r) => ({ ...r, key: r.id }))}
            />
          )}
          {report?.ingestion?.failed_operations > 0 && (
            <div style={{ fontSize: '0.78rem', color: '#c62828', marginTop: 8 }}>
              {n(report.ingestion.failed_operations)} file operation(s) failed across all imports.
          </div>
          )}
      </Section>
        </div>
      </div>


      {/* ── 5.4 + 5.6 side by side: both are short distributions ── */}
      <div style={{ display: 'flex', gap: '1rem', flexWrap: 'wrap' }}>
        <div style={{ flex: '1 1 340px', minWidth: 0 }}>
          <Section title="Formats" hint="HEIC and RAW need a derivative to be viewable at all">
            <MiniTable
              columns={[
                { key: 'ext', label: 'Type' },
                { key: 'kind', label: '', render: (r) => (
                  <CBadge color={r.kind === 'video' ? 'info' : r.kind === 'raw' ? 'dark' : 'light'}
                          className={r.kind === 'still' ? 'text-dark' : ''}
                          style={{ fontSize: '0.64rem' }}>
                    {r.kind}
                  </CBadge>
                ) },
                { key: 'files', label: 'Files', numeric: true, render: (r) => n(r.files) },
                { key: 'bytes', label: 'Size', numeric: true, render: (r) => bytes(r.bytes) },
              ]}
              rows={(report?.formats || []).slice(0, 10).map((f) => ({ ...f, key: f.ext }))}
            />
          </Section>
        </div>
        <div style={{ flex: '1 1 340px', minWidth: 0 }}>
          <Section
            title="One capture, several files"
            hint={`${n(report?.groups?.files_grouped)} file(s) belong to a group`}
          >
            <MiniTable
              columns={[
                { key: 'kind', label: 'Kind' },
                { key: 'match_rule', label: 'Matched by' },
                { key: 'groups', label: 'Groups', numeric: true, render: (r) => n(r.groups) },
              ]}
              rows={(report?.groups?.rules || []).map((g, i) => ({ ...g, key: i }))}
            />
            <div style={{ fontSize: '0.72rem', color: '#999', marginTop: 6 }}>
              The rule matters as much as the count: <code>serial+shuttercount</code> is a
              strong identity claim, <code>subsec_datetime</code> an inference.
            </div>
          </Section>
        </div>
      </div>

    </CContainer>
  );
};

export default ContentReport;
