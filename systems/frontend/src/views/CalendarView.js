import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import { CAlert, CButton, CCard, CCardBody, CContainer, CSpinner } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilCalendar, cilReload } from '@coreui/icons';
import { calendarAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import CalendarHeatmap, { CalendarLegend, yearBlocks } from '../components/CalendarHeatmap';
import { linkFor } from '../utils/calendarDays';

// ─── Views · Calendar view ────────────────────────────────────────────────────
// documentation/architecture/calendar-view-spec.md.
//
// The open catalog as a calendar heatmap: one row per year, a square per day,
// coloured by how many photos and videos were taken that day. Ten years with
// files at a time - the owner's choice - paged with a small control at the
// top; the page is in the URL. The counts come cached (calendar_counts.py), so
// opening and paging ask the server nothing heavy. A click opens that day in
// Events, or in Photos (Videos) when no event holds it.

const PAGE_YEARS = 10;

const ago = (iso) => {
  if (!iso) return null;
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

const CalendarView = () => {
  const { activeCatalog } = useCatalog();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [refreshing, setRefreshing] = useState(false);
  const catalogId = activeCatalog?.id;

  const load = useCallback(async () => {
    if (!catalogId) return;
    try {
      const r = await calendarAPI.get(catalogId);
      setData(r.data);
      setError(null);
    } catch (e) {
      setError(e.response?.data?.error || 'Could not load the calendar.');
    }
  }, [catalogId]);

  useEffect(() => { setData(null); load(); }, [load]);

  // A stale count is being redone in the background: ask again shortly.
  useEffect(() => {
    if (!data?.stale) return undefined;
    const t = setTimeout(() => { if (document.visibilityState === 'visible') load(); }, 5000);
    return () => clearTimeout(t);
  }, [data, load]);

  const refresh = async () => {
    setRefreshing(true);
    try {
      const r = await calendarAPI.refresh(catalogId);
      setData(r.data);
      setError(null);
    } catch (e) {
      setError(e.response?.data?.error || 'Could not recount.');
    } finally {
      setRefreshing(false);
    }
  };

  // Years that have files, newest first, and each year's total.
  const { years, yearTotals } = useMemo(() => {
    const totals = {};
    for (const r of data?.days || []) totals[r[0]] = (totals[r[0]] || 0) + r[3] + r[4];
    return { years: Object.keys(totals).map(Number).sort((a, b) => b - a), yearTotals: totals };
  }, [data]);

  const pages = Math.max(1, Math.ceil(years.length / PAGE_YEARS));
  const page = Math.min(pages - 1, Math.max(0, Number(searchParams.get('page')) || 0));
  const shown = years.slice(page * PAGE_YEARS, (page + 1) * PAGE_YEARS);
  const blocks = useMemo(() => yearBlocks(shown, yearTotals),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [shown.join(','), yearTotals]);
  const goPage = (p) => setSearchParams(p ? { page: String(p) } : {});

  const onDayClick = useCallback((row) => navigate(linkFor(row)), [navigate]);

  if (!activeCatalog) {
    return (
      <CContainer className="py-4">
        <CCard><CCardBody className="text-body-secondary">
          No catalog selected. Choose a catalog to see its calendar.
        </CCardBody></CCard>
      </CContainer>
    );
  }

  const t = data?.totals;
  return (
    <CContainer fluid className="py-4 px-4">
      <div className="d-flex justify-content-between align-items-start mb-3 flex-wrap gap-2">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>
            <CIcon icon={cilCalendar} className="me-2" style={{ width: 22, height: 22 }} />
            Calendar view
          </h4>
          {t && t.days > 0 && (
            <div style={{ fontSize: '0.85rem', color: '#6c757d', marginTop: 2 }}>
              {activeCatalog.name} · {t.photos.toLocaleString()} photos and {t.videos.toLocaleString()} videos
              on {t.days.toLocaleString()} days · {t.first_year}–{t.last_year}
              {data.undated > 0 && ` · ${data.undated.toLocaleString()} without a date are not shown`}
            </div>
          )}
        </div>
        <div className="d-flex align-items-center gap-2" style={{ fontSize: '0.8rem', color: '#6c757d' }}>
          {data && (data.stale ? <span><CSpinner size="sm" className="me-1" />counting…</span>
            : <span>counted {ago(data.computed_at)}</span>)}
          <CButton color="secondary" variant="outline" size="sm" onClick={refresh} disabled={refreshing || !data}
                   title="Count again now">
            {refreshing ? <CSpinner size="sm" /> : <CIcon icon={cilReload} style={{ width: 14, height: 14 }} />}
            <span className="ms-1">Refresh</span>
          </CButton>
        </div>
      </div>

      {error && <CAlert color="danger">{error}</CAlert>}
      {!data && !error && <div className="text-center py-5"><CSpinner color="primary" /></div>}

      {data && years.length === 0 && (
        <CCard><CCardBody className="text-center text-body-secondary py-5">
          <CIcon icon={cilCalendar} style={{ width: 40, height: 40, opacity: 0.25 }} />
          <div className="mt-2">No dated photos or videos in this catalog yet.</div>
          <CButton color="link" size="sm" onClick={() => navigate('/photos')}>Open Photos</CButton>
        </CCardBody></CCard>
      )}

      {data && years.length > 0 && (
        <CCard>
          <CCardBody>
            {/* Paging: ten years with files at a time, newest first. */}
            <div className="d-flex justify-content-center align-items-center gap-3 mb-3">
              <CButton color="secondary" variant="ghost" size="sm" disabled={page >= pages - 1}
                       onClick={() => goPage(page + 1)}>‹ Earlier</CButton>
              <span style={{ fontSize: '0.9rem', fontWeight: 600, minWidth: 110, textAlign: 'center' }}>
                {shown[shown.length - 1]} – {shown[0]}
              </span>
              <CButton color="secondary" variant="ghost" size="sm" disabled={page === 0}
                       onClick={() => goPage(page - 1)}>Later ›</CButton>
            </div>
            <CalendarHeatmap days={data.days} blocks={blocks} busiest={t.busiest}
                             onDayClick={onDayClick} />
            <div className="mt-3">
              <CalendarLegend busiest={t.busiest} />
            </div>
          </CCardBody>
        </CCard>
      )}
    </CContainer>
  );
};

export default CalendarView;
