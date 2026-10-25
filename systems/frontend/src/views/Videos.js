import React, { useState, useEffect, useMemo, useRef, useCallback } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import {
  CContainer, CRow, CCol, CCard, CCardHeader, CCardBody, CButton, CAlert,
  CSpinner, CBadge, CFormInput, CFormSelect, CFormTextarea,
  CListGroup, CListGroupItem,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilCalendar, cilLocationPin, cilPencil, cilCheck, cilX,
  cilVideo, cilSearch,
} from '@coreui/icons';
import { videoAPI, photoAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { groupByYearMonthDay, MONTH_NAMES, itemsInScope } from '../utils/dateGrouping';
import { getTimezoneOptions, getBrowserTimezone } from '../utils/timezones';
import { API_HOST, galleryThumbUrl } from '../config';
import { Filmstrip } from '../components/Filmstrip';
import CatalogFileCard from '../components/CatalogFileCard';

// ─── Videos ───────────────────────────────────────────────────────────────────
// documentation/specs/video-page-spec.md
//
// Photos and videos are disjoint: /photos filters to image extensions and
// /videos to VIDEO_EXTENSIONS, so nothing appears on both pages.
//
// The player points at /photos/<id>/image for EVERY video, proxy or not. The
// backend resolves which file that is — the transcoded proxy when one exists,
// the original when the video was already browser-playable — so this page never
// has to know which branch a video took.

/** `m:ss`, or `h:mm:ss` past an hour. `—` when the duration is unknown. */
function formatDuration(seconds) {
  if (seconds === null || seconds === undefined || !isFinite(seconds) || seconds <= 0) {
    return '—';
  }
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  return h > 0
    ? `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`
    : `${m}:${String(s).padStart(2, '0')}`;
}

function formatBytes(bytes) {
  if (!bytes) return '—';
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  return `${(mb / 1024).toFixed(2)} GB`;
}

/** "July 15, 2024" — matches the Photos event card date format. */
function formatDate(value) {
  if (!value) return null;
  try {
    const d = new Date(value);
    if (isNaN(d.getTime())) return value;
    return d.toLocaleString('en-US', { year: 'numeric', month: 'long', day: 'numeric' });
  } catch { return value; }
}

function formatDateTime(value) {
  if (!value) return '—';
  const d = new Date(value);
  if (isNaN(d.getTime())) return value;
  return d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

const Field = ({ label, children }) => (
  <div className="mb-2">
    <div style={{ fontSize: '0.72rem', color: '#888' }}>{label}</div>
    <div style={{ fontSize: '0.85rem', color: '#333', wordBreak: 'break-word' }}>{children}</div>
  </div>
);

const Videos = () => {
  // The catalog segment of the deep link is not read: the page follows the
  // ACTIVE catalog, exactly as Photos does. A link into a catalog that is not
  // the active one falls through to the "not in this catalog" notice below
  // rather than silently switching what the rest of the UI is pointed at.
  const { fileId: urlFileId } = useParams();
  const { activeCatalog } = useCatalog();
  const navigate = useNavigate();

  const catalogId = activeCatalog?.id;

  const [videos, setVideos] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const [selected, setSelected] = useState(null);
  // Detail record from photoAPI.getById — carries events, locations, people.
  // Reset to null on every video switch so stale data never bleeds through.
  const [videoDetail, setVideoDetail] = useState(null);
  const [selectedYear, setSelectedYear] = useState(null);
  const [selectedMonth, setSelectedMonth] = useState(null);
  const [selectedDay, setSelectedDay] = useState(null);

  const [search, setSearch] = useState('');
  const [gpsFilter, setGpsFilter] = useState('all');   // all | has | none

  // Filled from the <video> element when the API has no stored duration
  // (video-page-spec.md section 4.3) — for the SELECTED video only, never per
  // card, which would mean loading metadata for every tile.
  const [liveDuration, setLiveDuration] = useState(null);

  const [editingDescription, setEditingDescription] = useState(false);
  const [descriptionDraft, setDescriptionDraft] = useState('');

  // Date editing — the same form and endpoint as Photos (PUT /photos/:id/date,
  // which is file-generic: it files a video under Videos/ and cache/Videos/).
  const [editingDate, setEditingDate] = useState(false);
  const [dateInput, setDateInput] = useState('');   // "YYYY-MM-DD"
  const [timeInput, setTimeInput] = useState('');   // "HH:MM:SS"
  const [tzInput, setTzInput] = useState(getBrowserTimezone);
  const [dateSaving, setDateSaving] = useState(false);
  const [dateError, setDateError] = useState(null);
  const [dateSuccess, setDateSuccess] = useState(null);

  const pendingFileIdRef = useRef(urlFileId ? Number(urlFileId) : null);
  // /videos?date=YYYY-MM-DD - a day from Calendar view or the Events day view
  // (calendar-view-spec.md 6.3). Read once, like the file id above.
  const routerLocation = useLocation();
  const pendingDateRef = useRef(
    /^\d{4}-\d{2}-\d{2}$/.test(new URLSearchParams(routerLocation.search).get('date') || '')
      ? new URLSearchParams(routerLocation.search).get('date') : null);


  const load = useCallback(async () => {
    if (!catalogId) { setLoading(false); return; }
    setLoading(true);
    try {
      const res = await videoAPI.getAll(catalogId);
      // _catalogId rides along so the cards can build thumbnail URLs without
      // threading the catalog through every component.
      setVideos((res.data || []).map((v) => ({ ...v, _catalogId: catalogId })));
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load videos.');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return videos.filter((v) => {
      if (needle) {
        const hay = `${v.file_name || ''} ${v.description || ''}`.toLowerCase();
        if (!hay.includes(needle)) return false;
      }
      const hasGps = v.latitude !== null && v.longitude !== null;
      if (gpsFilter === 'has' && !hasGps) return false;
      if (gpsFilter === 'none' && hasGps) return false;
      return true;
    });
  }, [videos, search, gpsFilter]);

  const grouped = useMemo(() => groupByYearMonthDay(filtered), [filtered]);

  const years = useMemo(
    () => Object.keys(grouped).filter((y) => y !== 'Unknown')
      .map(Number).sort((a, b) => b - a),
    [grouped],
  );
  // Ascending, because these are drawn as a ROW of buttons now rather than a
  // dropdown: January on the left and the 31st on the right is how a year and a
  // month read, and it is how the same row reads on Photos.
  const months = useMemo(
    () => Object.keys(grouped[String(selectedYear)] ?? {})
      .map(Number).filter((m) => m !== -1).sort((a, b) => a - b),
    [grouped, selectedYear],
  );
  const days = useMemo(
    () => Object.keys(grouped[String(selectedYear)]?.[selectedMonth] ?? {})
      .map(Number).filter((d) => d !== -1).sort((a, b) => a - b),
    [grouped, selectedYear, selectedMonth],
  );

  // ─── Year / Month / Day, the way Photos does it ─────────────────────────────
  // Picking a year opens its most recent month, and that month's most recent
  // day, so a click always lands on something to watch rather than on two empty
  // rows waiting for two more clicks. Nothing selects the video here: the effect
  // further down owns that, and it is the same effect whether the day changed
  // from these buttons, from the URL, or from the strip.
  const pickYear = (year) => {
    setSelectedYear(year);
    const ms = Object.keys(grouped[String(year)] ?? {})
      .map(Number).filter((m) => m !== -1).sort((a, b) => b - a);
    const month = ms[0] ?? null;
    setSelectedMonth(month);
    const ds = Object.keys(grouped[String(year)]?.[month] ?? {})
      .map(Number).filter((d) => d !== -1).sort((a, b) => b - a);
    setSelectedDay(ds[0] ?? null);
  };

  // Each "All" clears its own level and everything under it.
  //
  // None of them picks a video, and none of them has to: a wider scope always
  // contains the narrower one, so the video being watched is still in the list
  // and the day-sync effect below leaves it alone. Widening keeps your place;
  // picking a year or a day is a request to go elsewhere, and those move.
  const showAllYears = () => {
    setSelectedYear(null); setSelectedMonth(null); setSelectedDay(null);
  };
  const showAllOfYear = () => { setSelectedMonth(null); setSelectedDay(null); };
  const showAllOfMonth = () => setSelectedDay(null);

  const pickMonth = (month) => {
    setSelectedMonth(month);
    const ds = Object.keys(grouped[String(selectedYear)]?.[month] ?? {})
      .map(Number).filter((d) => d !== -1).sort((a, b) => b - a);
    setSelectedDay(ds[0] ?? null);
  };

  // ── What is being browsed ───────────────────────────────────────────────────
  // The day, the month, the year or every video, depending on how much of the
  // date is picked - each "All" button clears one level and widens this. Oldest
  // first, which is the order the strip draws them in
  // (utils/dateGrouping.itemsInScope, shared with Photos).
  const dayVideos = useMemo(
    () => itemsInScope(grouped, selectedYear, selectedMonth, selectedDay),
    [grouped, selectedYear, selectedMonth, selectedDay],
  );

  const dayIndex = dayVideos.findIndex((v) => v.id === selected?.id);


  /**
   * Point the whole page at one video, opening its date bucket, and put that
   * video in the address bar.
   *
   * The URL is the shareable handle for a single video — /videos/67/25 the way
   * /photos/67/25 is for a still — so every selection has to update it, or the
   * route exists and nothing ever produces a link to copy.
   *
   *   replace  a DERIVED selection (first load, resolving a deep link). Rewrites
   *            the current entry so Back leaves the page instead of stepping
   *            through videos the user never chose.
   *   fromUrl  the selection CAME from the URL (Back/Forward). Must not navigate
   *            again — that would push a duplicate entry and fight the history.
   */
  const selectVideo = useCallback((video, { replace = false, fromUrl = false } = {}) => {
    if (!video) return;
    const src = video.master_datetime;
    const d = src ? new Date(src) : null;
    if (d && !isNaN(d.getTime())) {
      setSelectedYear(d.getFullYear());
      setSelectedMonth(d.getMonth());
      setSelectedDay(d.getDate());
    }
    setSelected(video);
    setVideoDetail(null);
    setLiveDuration(null);
    setEditingDescription(false);
    setEditingDate(false);
    setDateError(null);
    if (!fromUrl && catalogId && String(video.id) !== String(urlFileId)) {
      navigate(`/videos/${catalogId}/${video.id}`, { replace });
    }
  }, [catalogId, urlFileId, navigate]);

  // First load: honour a deep link, else open the newest video. Both are
  // derived rather than chosen, so neither should add a history entry.
  useEffect(() => {
    if (loading || videos.length === 0 || selected) return;
    const wanted = pendingFileIdRef.current;
    if (wanted) {
      pendingFileIdRef.current = null;
      const match = videos.find((v) => v.id === wanted);
      if (match) { selectVideo(match, { fromUrl: true }); return; }
      // A file id that is not a video, or belongs to another catalog. Say so
      // and show something rather than rendering an empty page — and replace
      // the bad URL rather than leaving it in the bar.
      setNotice(`No video with id ${wanted} in this catalog — showing the most recent one.`);
    }
    const day = pendingDateRef.current;
    if (day) {
      pendingDateRef.current = null;
      // Newest first: the first video dated that day or earlier is that day's
      // latest - or, if it has none, the nearest day before that does.
      const match = videos.find((v) => (v.master_datetime || '').slice(0, 10) <= day);
      if (match) { selectVideo(match, { replace: true }); return; }
    }
    selectVideo(videos[0], { replace: true });
  }, [loading, videos, selected, selectVideo]);

  // Changing the day in the filter bar used to move the dropdowns and nothing
  // else: the player kept whatever video it had, which could be months away
  // from the day on screen. With a strip to pick from, the day and the player
  // have to agree, so a day that does not contain the current video opens on
  // its first one. Same rule Photos applies to its own day row.
  //
  // replace, because this selection is DERIVED from the day the user picked -
  // Back should leave the page, not step through videos nobody chose.
  //
  // Only ever CORRECTS a selection; the first one is the effect above's job.
  // Without the `!selected` guard both ran in the same commit once the list
  // loaded: that one selected the deep-linked (or newest) video, but this one
  // still saw `selected` as null and a scope of "every video, oldest first" -
  // no day was picked yet - and replaced it with the OLDEST video in the
  // catalog. Every link into this page opened the same wrong video.
  useEffect(() => {
    if (loading || dayVideos.length === 0 || !selected) return;
    if (dayVideos.some((v) => v.id === selected.id)) return;
    selectVideo(dayVideos[0], { replace: true });
  }, [dayVideos, selected, loading, selectVideo]);

  // Back and Forward. Without this the address bar changes and the page does
  // not, which makes the URL decorative rather than the handle on a video.
  useEffect(() => {
    if (loading || videos.length === 0 || !urlFileId) return;
    const wanted = Number(urlFileId);
    if (selected && selected.id === wanted) return;
    const match = videos.find((v) => v.id === wanted);
    if (match) selectVideo(match, { fromUrl: true });
  }, [urlFileId, videos, loading, selected, selectVideo]);

  // Fetch the full detail record (events, locations, EXIF) whenever the
  // selected video id changes. Same pattern as Photos' handleImageLoad call to
  // photoAPI.getById — the endpoint is file-generic and works for videos.
  const selectedId = selected?.id ?? null;
  useEffect(() => {
    if (!catalogId || selectedId === null) return;
    let cancelled = false;
    (async () => {
      try {
        const res = await photoAPI.getById(catalogId, selectedId);
        if (!cancelled) setVideoDetail(res.data);
      } catch {
        if (!cancelled) setVideoDetail(null);
      }
    })();
    return () => { cancelled = true; };
  }, [catalogId, selectedId]);

  const saveDescription = async () => {
    try {
      await photoAPI.updateDescription(catalogId, selected.id, descriptionDraft);
      setVideos((prev) => prev.map((v) =>
        v.id === selected.id ? { ...v, description: descriptionDraft } : v));
      setSelected((prev) => ({ ...prev, description: descriptionDraft }));
      setEditingDescription(false);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not save the description.');
    }
  };

  // A removed video leaves the page at once; the next one of the same day (or
  // the one before) opens in its place.
  const onVideoRemoved = (result) => {
    const gone = new Set(result.removed_file_ids);
    const neighbour = dayVideos[dayIndex + 1] ?? dayVideos[dayIndex - 1] ?? null;
    setVideos((prev) => prev.filter((v) => !gone.has(v.id)));
    if (neighbour && !gone.has(neighbour.id)) {
      selectVideo(neighbour, { replace: true });
    } else {
      // Nothing left that day: the first-load effect opens the newest video.
      setSelected(null);
      setVideoDetail(null);
    }
    setNotice(
      `Removed ${result.file_names.join(' and ')} from the catalog. `
      + 'The original is still on disk - it is listed on Removed files until you delete it.');
  };

  const editDate = () => {
    const src = selected?.master_datetime;
    const parsed = src ? new Date(src) : null;
    const d = parsed && !isNaN(parsed.getTime()) ? parsed : new Date();
    // Local-time components, so the form opens on what the card shows rather
    // than on an offset-adjusted UTC value.
    const pad = (n) => String(n).padStart(2, '0');
    setDateInput(`${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`);
    setTimeInput(`${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`);
    setDateError(null);
    setEditingDate(true);
  };

  const saveDate = async () => {
    if (!selected) return;
    setDateSaving(true);
    setDateError(null);
    try {
      const res = await photoAPI.updateDate(
        catalogId, selected.id, `${dateInput}T${timeInput}`, tzInput);
      const updated = {
        ...selected,
        master_datetime: res.data.master_datetime,
        master_datetime_source: res.data.master_datetime_source,
        file_name: res.data.new_file_name ?? selected.file_name,
      };
      setVideos((prev) => prev.map((v) => (v.id === updated.id ? updated : v)));
      // Follow the video to its new day. The id is unchanged, so the detail
      // effect will not refire on its own — and the events did change.
      selectVideo(updated, { fromUrl: true });
      setDateSuccess(res.data.file_renamed
        ? 'Date updated. File has been renamed and moved.'
        : 'Date updated.');
      photoAPI.getById(catalogId, updated.id)
        .then((detail) => setVideoDetail(detail.data))
        .catch(() => {});
    } catch (e) {
      setDateError(e?.response?.data?.error || 'Failed to update date');
    } finally {
      setDateSaving(false);
    }
  };

  if (!activeCatalog) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="info">Select a catalog to browse its videos.</CAlert>
      </CContainer>
    );
  }

  const shownDuration = selected?.duration_seconds ?? liveDuration;

  return (
    <CContainer fluid className="px-4 py-4">
      <div className="d-flex justify-content-between align-items-center mb-3">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Videos</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            {loading ? 'Loading…' : `${videos.length} video${videos.length === 1 ? '' : 's'} in ${activeCatalog.name}`}
          </div>
        </div>
      </div>

      {/* ── Search and location, then the date rows, then the day's strip ──
          One card, in the order Photos has it: what narrows the library, then
          which day, then which item of that day. The three date DROPDOWNS that
          used to live here are gone - a select hides the shape of the library
          behind a click, and these rows show at a glance which years, months and
          days actually hold video. */}
      <CCard className="mb-4" style={{ backgroundColor: 'white' }}>
        <CCardBody className="py-3">
          <div className="d-flex flex-column gap-2">

            {/* Videos' own filters, which Photos has no equivalent of. They
                narrow the library BEFORE the date rows are built, so a year
                with nothing matching disappears from the row. */}
            <div className="d-flex flex-wrap gap-2 align-items-center">
              <div style={{ position: 'relative', minWidth: 240 }}>
                <CIcon icon={cilSearch} style={{
                  width: 14, height: 14, position: 'absolute', left: 10, top: 10, color: '#888',
                }} />
                <CFormInput
                  size="sm"
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder="Search name or description"
                  style={{ paddingLeft: 30 }}
                />
              </div>
              <CFormSelect size="sm" style={{ width: 130 }} value={gpsFilter}
                           onChange={(e) => setGpsFilter(e.target.value)}>
                <option value="all">Any location</option>
                <option value="has">Has GPS</option>
                <option value="none">No GPS</option>
              </CFormSelect>
            </div>

            {/* ── Year row ──
                "All" first, active while no year is picked, as on the Events
                filter card. */}
            {loading ? (
              <CSpinner size="sm" color="secondary" />
            ) : (
              <div className="d-flex gap-1 flex-wrap">
                <button
                  className={`btn btn-sm ${selectedYear == null ? 'btn-primary' : 'btn-outline-secondary'}`}
                  onClick={showAllYears}
                >
                  All
                </button>
                {years.map((year) => (
                  <button
                    key={year}
                    className={`btn btn-sm ${selectedYear === year ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={() => pickYear(year)}
                  >
                    {year}
                  </button>
                ))}
              </div>
            )}

            {/* ── Month row — visible once a year is picked ── */}
            {selectedYear != null && (
              <div className="d-flex gap-1 flex-wrap">
                {/* The whole year, so the row has a visible "no month" state
                    rather than an implied one. */}
                <button
                  className={`btn btn-sm ${selectedMonth == null ? 'btn-primary' : 'btn-outline-secondary'}`}
                  onClick={showAllOfYear}
                >
                  All {selectedYear}
                </button>
                {months.map((monthIdx) => (
                  <button
                    key={monthIdx}
                    className={`btn btn-sm ${selectedMonth === monthIdx ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={() => pickMonth(monthIdx)}
                  >
                    {MONTH_NAMES[monthIdx]?.slice(0, 3) ?? monthIdx}
                  </button>
                ))}
              </div>
            )}

            {/* ── Day row — visible once a month is picked ── */}
            {selectedYear != null && selectedMonth != null && (
              <div className="d-flex gap-1 flex-wrap">
                <button
                  className={`btn btn-sm ${selectedDay == null ? 'btn-primary' : 'btn-outline-secondary'}`}
                  onClick={showAllOfMonth}
                >
                  All {MONTH_NAMES[selectedMonth]?.slice(0, 3) ?? ''}
                </button>
                {days.map((day) => (
                  <button
                    key={day}
                    className={`btn btn-sm ${selectedDay === day ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={() => setSelectedDay(day)}
                  >
                    {day}
                  </button>
                ))}
              </div>
            )}
          </div>

          {/* ── The day's filmstrip ──
              Here rather than under the player, and outside the column above,
              exactly as Photos has it: the date rows choose a day, and this
              chooses which video of that day - so the two belong together, and
              the player is left showing only the video.

              Thumbnails load as the strip is scrolled; see
              components/Filmstrip.js. */}
          {dayVideos.length > 0 && (
            <div style={{ borderTop: '1px solid #e0e0e0', paddingTop: 8, marginTop: 8 }}>
              <Filmstrip
                items={dayVideos}
                activeId={selected?.id ?? null}
                // NULL WHEN THERE IS NO POSTER, deliberately: the thumbnail
                // endpoint redirects to the full file when it has no thumbnail
                // to serve, so a URL here would pull an entire .MOV into an
                // <img>.
                srcFor={(video) => (video.has_poster
                  ? galleryThumbUrl(catalogId, video.id, 72)
                  : null)}
                onSelect={selectVideo}
              />
              {dayIndex >= 0 && (
                <div style={{ fontSize: '0.75rem', color: '#888',
                              textAlign: 'center', marginTop: 4 }}>
                  {dayIndex + 1} / {dayVideos.length}
                  {(dayVideos[dayIndex]?.master_datetime
                    || dayVideos[dayIndex]?.creation_date) && (
                    <span style={{ marginLeft: 8 }}>
                      · {formatDateTime(dayVideos[dayIndex].master_datetime
                                        || dayVideos[dayIndex].creation_date)}
                    </span>
                  )}
                </div>
              )}
            </div>
          )}
        </CCardBody>
      </CCard>

      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}
      {notice && <CAlert color="info" dismissible onClose={() => setNotice(null)}>{notice}</CAlert>}
      {dateSuccess && (
        <CAlert color="success" dismissible onClose={() => setDateSuccess(null)}>{dateSuccess}</CAlert>
      )}

      {loading ? (
        <div className="text-center py-5"><CSpinner color="primary" /></div>
      ) : videos.length === 0 ? (
        <div className="text-center py-5 text-muted" style={{ fontSize: '0.9rem' }}>
          <CIcon icon={cilVideo} style={{ width: 28, height: 28 }} className="mb-2 d-block mx-auto" />
          No videos in this catalog yet. Import some and they will appear here.
        </div>
      ) : (
        // md=2 / md=8 / md=2, matching Photos: metadata left, media centre.
        <CRow className="g-3">
          {/* ── Left: the editable metadata, as Photos has it ── */}
          <CCol md={2} className="d-flex flex-column gap-3">
            <CCard>
              <CCardHeader className="py-2 d-flex justify-content-between align-items-center">
                <div className="d-flex align-items-center">
                  <CIcon icon={cilCalendar} className="me-2" /><strong>Date</strong>
                </div>
                {!editingDate && selected && (
                  <CButton size="sm" color="secondary" variant="ghost" title="Edit date"
                           onClick={editDate}>
                    <CIcon icon={cilPencil} />
                  </CButton>
                )}
              </CCardHeader>
              <CCardBody className="py-2">
                {editingDate ? (
                  <>
                    <div className="mb-2">
                      <label style={{ fontSize: '0.72rem', color: '#888' }}>Date</label>
                      <input type="date" className="form-control form-control-sm"
                             value={dateInput} onChange={(e) => setDateInput(e.target.value)} />
                    </div>
                    <div className="mb-2">
                      <label style={{ fontSize: '0.72rem', color: '#888' }}>Time</label>
                      <input type="time" step="1" className="form-control form-control-sm"
                             value={timeInput} onChange={(e) => setTimeInput(e.target.value)} />
                    </div>
                    <div className="mb-2">
                      <label style={{ fontSize: '0.72rem', color: '#888' }}>Time Zone</label>
                      <select className="form-select form-select-sm" value={tzInput}
                              onChange={(e) => setTzInput(e.target.value)}>
                        {getTimezoneOptions().map((tz) => (
                          <option key={tz} value={tz}>{tz}</option>
                        ))}
                      </select>
                    </div>
                    {dateError && (
                      <div style={{ fontSize: '0.72rem', color: '#c00' }} className="mb-2">{dateError}</div>
                    )}
                    <div className="d-flex gap-2">
                      <CButton size="sm" color="primary" onClick={saveDate}
                               disabled={dateSaving || !dateInput || !timeInput}>
                        {dateSaving ? <CSpinner size="sm" /> : 'Save'}
                      </CButton>
                      <CButton size="sm" color="secondary" variant="ghost"
                               onClick={() => { setEditingDate(false); setDateError(null); }}
                               disabled={dateSaving}>
                        Cancel
                      </CButton>
                    </div>
                  </>
                ) : (
                  <>
                    <Field label="Taken">{formatDateTime(selected?.master_datetime)}</Field>
                    {selected?.master_datetime_source && (
                      <CBadge color="light" className="text-dark" style={{ fontSize: '0.68rem' }}>
                        from {selected.master_datetime_source}
                      </CBadge>
                    )}
                  </>
                )}
              </CCardBody>
            </CCard>

            {/* ── Event ── second card, matching the Photos left column order */}
            <CCard>
              <CCardHeader className="d-flex align-items-center py-2">
                <CIcon icon={cilCalendar} className="me-2" />
                <strong>Event</strong>
                {videoDetail?.events && videoDetail.events.length > 0 && (
                  <CBadge color="primary" className="ms-2">{videoDetail.events.length}</CBadge>
                )}
              </CCardHeader>
              <CCardBody className="py-2" style={{ maxHeight: '140px', overflowY: 'auto' }}>
                {videoDetail?.events && videoDetail.events.length > 0 ? (
                  <CListGroup flush>
                    {videoDetail.events.map((event) => (
                      <CListGroupItem key={event.id} className="px-0 py-2 border-0">
                        <div
                          style={{ fontSize: '0.8rem', fontWeight: 500, color: '#1a73e8', cursor: 'pointer', textDecoration: 'underline' }}
                          onClick={() => navigate(`/events/${catalogId}/${event.id}`)}
                        >
                          {event.name}
                        </div>
                        {event.event_type && (
                          <CBadge color="secondary" className="me-2" style={{ fontSize: '0.62rem' }}>
                            {event.event_type}
                          </CBadge>
                        )}
                        {event.start_time && (
                          <div style={{ fontSize: '0.72rem', color: '#666' }}>
                            {formatDate(event.start_time)}
                          </div>
                        )}
                      </CListGroupItem>
                    ))}
                  </CListGroup>
                ) : (
                  <div style={{ fontSize: '0.8rem', color: '#888', fontStyle: 'italic' }}>
                    No events tagged
                  </div>
                )}
              </CCardBody>
            </CCard>

            <CCard>
              <CCardHeader className="py-2 d-flex justify-content-between align-items-center">
                <strong>Description</strong>
                {!editingDescription && selected && (
                  <CButton size="sm" color="secondary" variant="ghost" title="Edit description"
                           onClick={() => {
                             setDescriptionDraft(selected.description || '');
                             setEditingDescription(true);
                           }}>
                    <CIcon icon={cilPencil} />
                  </CButton>
                )}
              </CCardHeader>
              <CCardBody className="py-2">
                {editingDescription ? (
                  <>
                    <CFormTextarea rows="3" value={descriptionDraft}
                                   onChange={(e) => setDescriptionDraft(e.target.value)} />
                    <div className="d-flex gap-1 mt-2">
                      <CButton size="sm" color="primary" onClick={saveDescription}>
                        <CIcon icon={cilCheck} />
                      </CButton>
                      <CButton size="sm" color="secondary" variant="outline"
                               onClick={() => setEditingDescription(false)}>
                        <CIcon icon={cilX} />
                      </CButton>
                    </div>
                  </>
                ) : (
                  <div style={{ fontSize: '0.85rem', color: selected?.description ? '#333' : '#aaa' }}>
                    {selected?.description || 'No description'}
                  </div>
                )}
              </CCardBody>
            </CCard>

            <CCard>
              <CCardHeader className="py-2">
                <CIcon icon={cilLocationPin} className="me-2" /><strong>Location</strong>
              </CCardHeader>
              <CCardBody className="py-2">
                {selected?.latitude != null && selected?.longitude != null ? (
                  <Field label="Coordinates">
                    {selected.latitude.toFixed(5)}, {selected.longitude.toFixed(5)}
                  </Field>
                ) : (
                  <div style={{ fontSize: '0.85rem', color: '#aaa' }}>No GPS data</div>
                )}
              </CCardBody>
            </CCard>
            {/* No People card: face detection does not run on video, so it would
                be permanently empty on every video in the catalog — which reads
                as a broken feature rather than a future one. */}
          </CCol>

          {/* ── Centre: the player. The day's strip is up in the date card,
              where Photos keeps it. ── */}
          <CCol md={8}>
            <CCard>
              <CCardBody className="p-2">
                {selected ? (
                  <video
                    // key: React must REPLACE the element rather than mutate
                    // src. Changing src on a playing element leaves the previous
                    // frame on screen in Chrome until new metadata arrives.
                    key={selected.id}
                    src={`${API_HOST}/api/catalogs/${catalogId}/photos/${selected.id}/image`}
                    poster={selected.has_poster ? galleryThumbUrl(catalogId, selected.id) : undefined}
                    controls
                    preload="metadata"
                    playsInline
                    onLoadedMetadata={(e) => setLiveDuration(e.target.duration)}
                    style={{ width: '100%', maxHeight: '68vh', background: '#000', display: 'block' }}
                  />
                ) : (
                  <div className="text-center text-muted py-5">Select a video</div>
                )}

              </CCardBody>
            </CCard>

          </CCol>

          {/* ── Right: the facts about the file itself ── */}
          <CCol md={2} className="d-flex flex-column gap-3">
            <CCard>
              <CCardHeader className="py-2">
                <CIcon icon={cilVideo} className="me-2" /><strong>Video</strong>
              </CCardHeader>
              <CCardBody className="py-2">
                <Field label="Duration">{formatDuration(shownDuration)}</Field>
                <Field label="File name">{selected?.file_name ?? '—'}</Field>
                <Field label="Type">{selected?.file_type ?? '—'}</Field>
                <Field label="Size">{formatBytes(selected?.file_size)}</Field>
                <Field label="Dimensions">
                  {selected?.width && selected?.height
                    ? `${selected.width} × ${selected.height}`
                    : '—'}
                </Field>
                <Field label="Camera">
                  {[selected?.camera_make, selected?.camera_model].filter(Boolean).join(' ') || '—'}
                </Field>
                <Field label="Playback">
                  {selected?.has_proxy
                    ? <span title="A browser-friendly copy was made at import">Converted copy</span>
                    : <span title="The original already streams in a browser">Original file</span>}
                </Field>
              </CCardBody>
            </CCard>

            {/* ── File: the catalog's original, and removing it ── */}
            <CatalogFileCard catalogId={catalogId} detail={videoDetail}
                             kind="video" onRemoved={onVideoRemoved} />
          </CCol>
        </CRow>
      )}
    </CContainer>
  );
};

export default Videos;
