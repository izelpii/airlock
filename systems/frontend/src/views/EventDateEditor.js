import React, {
  useState, useEffect, useMemo, useRef, useContext, useCallback,
} from 'react';
import {
  useParams, useNavigate, Link, UNSAFE_NavigationContext,
} from 'react-router-dom';
import {
  CContainer, CButton, CSpinner, CAlert, CFormSelect,
  CModal, CModalHeader, CModalTitle, CModalBody, CModalFooter,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilArrowLeft } from '@coreui/icons';
import { eventAPI, photoAPI } from '../services/api';
import DateGroupCard from '../components/DateGroupCard';
import { getTimezoneOptions, getBrowserTimezone } from '../utils/timezones';
import { parseWallTime, formatDayShort, formatTime12 } from '../utils/wallTime';

// ─── The event date editor ───────────────────────────────────────────────────
//
// /events/:catalogId/:eventId/dates (mass-edit-event-photo-dates-spec.md
// section 4). The event's photos are shown one card per day; each Apply
// STAGES a new date for a card's checked photos, and nothing is saved until
// Confirm sends the whole batch at once. The page always shows the event the
// way it will look once confirmed.
//
// Its own route rather than a modal, so Back leaves it and a reload keeps it.

// The same list as EventDetail's: formats a browser cannot show until an
// alternative version is rendered. Here they get a placeholder tile instead of
// being skipped (section 4.1).
const NEEDS_ALTERNATIVE_EXTS = new Set(['heic', 'heif', 'cr3', 'cr2', 'nef', 'arw', 'dng', 'raf', 'orf', 'rw2']);

const LEAVE_MESSAGE = 'You have date changes that are not saved. Leave and lose them?';

const extOf = (name) => {
  const parts = (name || '').split('.');
  return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
};

/**
 * The editor's view of the files: each with its original wall time, its
 * current one (staged, or else the original) and whether it is staged.
 *
 * master_datetime may carry an offset or a `Z`. Its wall-clock part is taken
 * literally, as Create Events does (section 5.5).
 */
export function viewFiles(files, staged) {
  return files.map((f) => {
    const original = parseWallTime(f.master_datetime);
    const stagedAt = staged[f.id];
    return {
      id: f.id,
      file_name: f.file_name,
      file_type: f.file_type,
      is_video: Boolean(f.is_video),
      alternative_file_path: f.alternative_file_path,
      needsAlternative: NEEDS_ALTERNATIVE_EXTS.has(extOf(f.file_name)),
      source: f.master_datetime_source,
      filename_datetime: f.filename_datetime,
      filename_has_time: Boolean(f.filename_has_time),
      original,
      current: stagedAt ? parseWallTime(stagedAt) : original,
      staged: Boolean(stagedAt),
    };
  });
}

/**
 * One group per day of the current (staged-or-original) wall time, ordered by
 * date, undated photos last. Section 4.2.
 */
export function groupByDay(viewed) {
  const byDay = new Map();
  viewed.forEach((f) => {
    const day = f.current ? f.current.day : null;
    if (!byDay.has(day)) byDay.set(day, []);
    byDay.get(day).push(f);
  });
  const key = (f) => (f.current ? f.current.sortKey : '');
  return [...byDay.entries()]
    .sort(([a], [b]) => {
      if (a === b) return 0;
      if (a === null) return 1;
      if (b === null) return -1;
      return a < b ? -1 : 1;
    })
    .map(([day, members]) => ({
      day,
      files: members.sort((x, y) => (key(x) < key(y) ? -1 : key(x) > key(y) ? 1 : x.id - y.id)),
    }));
}

/** The POST body's `changes`: one entry per target date and time. */
export function buildChanges(staged, timezone) {
  const byTime = new Map();
  Object.entries(staged).forEach(([id, dt]) => {
    if (!byTime.has(dt)) byTime.set(dt, []);
    byTime.get(dt).push(Number(id));
  });
  return [...byTime.entries()]
    .sort(([a], [b]) => (a < b ? -1 : 1))
    .map(([datetime, ids]) => ({ file_ids: ids.sort((a, b) => a - b), datetime, timezone }));
}

/**
 * Ask before leaving while `when` is true (section 4.4).
 *
 * The spec names a router blocker, but useBlocker only works under a data
 * router and App.js mounts a plain <BrowserRouter>, where it throws. So the
 * guard is built from what that router does have:
 *
 * - In-app navigation (links, the sidebar, navigate()) all goes through the
 *   router's navigator, so its push/replace/go are wrapped to confirm first.
 * - Back is a popstate the router has already acted on by the time anyone
 *   hears it, so it cannot be cancelled. Instead a duplicate history entry is
 *   pushed on top while there are changes: Back lands on the copy (same URL,
 *   the page stays), and the listener asks, then either goes back for real or
 *   re-arms the copy.
 * - Closing or reloading the tab is `beforeunload`.
 *
 * Returns `disarm`, which Confirm calls before redirecting: its own navigation
 * must not ask.
 */
export function useLeaveGuard(when, message = LEAVE_MESSAGE) {
  const { navigator } = useContext(UNSAFE_NavigationContext);
  const whenRef = useRef(when);
  whenRef.current = when;
  const disarmedRef = useRef(false);
  const trapRef = useRef(false);
  const active = () => whenRef.current && !disarmedRef.current;

  useEffect(() => {
    if (!when) return undefined;
    const onBeforeUnload = (e) => {
      if (!active()) return undefined;
      e.preventDefault();
      // Chrome still needs returnValue set to show its prompt.
      e.returnValue = '';
      return '';
    };
    window.addEventListener('beforeunload', onBeforeUnload);
    return () => window.removeEventListener('beforeunload', onBeforeUnload);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [when]);

  useEffect(() => {
    if (!when || !navigator) return undefined;
    const { push, replace, go } = navigator;
    const guard = (fn) => (...args) => {
      if (active()) {
        if (!window.confirm(message)) return;
        disarmedRef.current = true;
      }
      fn.apply(navigator, args);
    };
    navigator.push = guard(push);
    navigator.replace = guard(replace);
    navigator.go = guard(go);
    return () => {
      navigator.push = push;
      navigator.replace = replace;
      navigator.go = go;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [when, navigator, message]);

  // Arm the Back trap the first time there is something to lose. It is left
  // in place when the changes are undone; the listener then just lets Back
  // carry on, so the user never presses Back twice.
  useEffect(() => {
    if (!when || trapRef.current) return;
    window.history.pushState(window.history.state, '', window.location.href);
    trapRef.current = true;
  }, [when]);

  useEffect(() => {
    const onPop = () => {
      if (!trapRef.current) return;
      trapRef.current = false;
      if (disarmedRef.current) return;
      if (!whenRef.current) { window.history.back(); return; }
      if (window.confirm(message)) {
        disarmedRef.current = true;
        window.history.back();
      } else {
        window.history.pushState(window.history.state, '', window.location.href);
        trapRef.current = true;
      }
    };
    window.addEventListener('popstate', onPop);
    return () => window.removeEventListener('popstate', onPop);
  }, [message]);

  const disarm = useCallback(() => { disarmedRef.current = true; }, []);
  // Whether the copy entry is on top: Confirm then REPLACES it with the event
  // page, so Back from there returns to the editor rather than to the copy.
  const trapped = useCallback(() => trapRef.current, []);
  return { disarm, trapped };
}

const EventDateEditor = () => {
  const { catalogId, eventId } = useParams();
  const navigate = useNavigate();

  const [event, setEvent] = useState(null);
  const [files, setFiles] = useState([]);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState(null);

  // {file_id: "YYYY-MM-DDTHH:MM"}. Grouping is computed from this, so the
  // cards always reflect it (section 4.4).
  const [staged, setStaged] = useState({});
  // One timezone for the whole page, the browser's by default; it applies to
  // every staged change when the batch is sent.
  const [timezone, setTimezone] = useState(getBrowserTimezone);
  const [flash, setFlash] = useState({ day: null, key: 0 });

  const [confirmOpen, setConfirmOpen] = useState(false);
  const [sending, setSending] = useState(false);
  const [sendError, setSendError] = useState(null);

  const stagedCount = Object.keys(staged).length;
  const { disarm, trapped } = useLeaveGuard(stagedCount > 0);

  useEffect(() => {
    if (!catalogId || !eventId) return;
    let cancelled = false;
    setLoading(true);
    setLoadError(null);
    Promise.all([
      // The same lookup as EventDetail: the list endpoint carries every field.
      eventAPI.getAll(catalogId).then((r) => (r.data || []).find((e) => String(e.id) === String(eventId))),
      eventAPI.getFiles(catalogId, eventId).then((r) => r.data || []),
    ])
      .then(([found, rows]) => {
        if (cancelled) return;
        if (!found) {
          setLoadError('This event no longer exists. Its photos may have moved to another event.');
          return;
        }
        setEvent(found);
        setFiles(rows);
      })
      .catch(() => { if (!cancelled) setLoadError('Could not load this event\'s photos.'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [catalogId, eventId]);

  const viewed = useMemo(() => viewFiles(files, staged), [files, staged]);
  const groups = useMemo(() => groupByDay(viewed), [viewed]);
  const tzOptions = useMemo(() => getTimezoneOptions(), []);

  const handleApply = useCallback((ids, datetime) => {
    setStaged((prev) => {
      const next = { ...prev };
      ids.forEach((id) => { next[id] = datetime; });
      return next;
    });
    setFlash((f) => ({ day: datetime.slice(0, 10), key: f.key + 1 }));
  }, []);

  const handleUndo = useCallback((ids) => {
    setStaged((prev) => {
      const next = { ...prev };
      ids.forEach((id) => { delete next[id]; });
      return next;
    });
    const back = files.find((f) => f.id === ids[0]);
    const day = parseWallTime(back?.master_datetime)?.day ?? null;
    setFlash((f) => ({ day, key: f.key + 1 }));
  }, [files]);

  const handleDiscard = () => {
    if (stagedCount === 0) return;
    if (!window.confirm(`Discard the date changes of ${stagedCount} photo${stagedCount === 1 ? '' : 's'}?`)) return;
    setStaged({});
  };

  const changes = useMemo(() => buildChanges(staged, timezone), [staged, timezone]);

  const send = async () => {
    setSending(true);
    setSendError(null);
    try {
      const r = await photoAPI.updateDates(catalogId, eventId, changes, true);
      // Accepted: the server does the work in the background, and the event
      // page follows it (section 4.5). The redirect is ours, so no warning.
      const replace = trapped();
      disarm();
      setStaged({});
      setConfirmOpen(false);
      navigate(`/events/${catalogId}/${eventId}`, {
        state: { dateBatchId: r.data?.batch_id }, replace,
      });
    } catch (e) {
      const data = e?.response?.data || {};
      // 409 catalog_busy: an import is running. The staged changes are kept
      // and the editor stays open (section 5.1).
      if (e?.response?.status === 409 && data.error === 'catalog_busy') {
        setSendError(data.message || 'An import is running in this catalog. '
          + 'Your changes are kept; confirm again when it has finished.');
      } else {
        setSendError(data.message || data.error || 'Could not save the new dates.');
      }
    } finally {
      setSending(false);
    }
  };

  const groupSummary = changes.map((c) => {
    const at = parseWallTime(c.datetime);
    return `${c.file_ids.length} to ${formatDayShort(at)} ${formatTime12(at)}`;
  });

  return (
    <CContainer fluid className="px-4 pb-4">
      {/* The header stays in view under the app's own sticky top menu (60px)
          while the cards scroll beneath it. */}
      <div
        style={{
          position: 'sticky', top: 60, zIndex: 1020, background: '#f8f9fa',
          borderBottom: '1px solid #dee2e6', margin: '0 -1.5rem 12px', padding: '10px 1.5rem',
        }}
        className="d-flex flex-wrap align-items-center gap-3"
      >
        <Link
          to={`/events/${catalogId}/${eventId}`}
          data-testid="back-to-event"
          style={{ textDecoration: 'none', fontWeight: 600 }}
        >
          <CIcon icon={cilArrowLeft} className="me-1" />
          {event?.name || 'Event'}
        </Link>
        <strong style={{ fontSize: '1rem' }}>Modify dates</strong>
        <div className="ms-auto d-flex flex-wrap align-items-center gap-2">
          <label htmlFor="date-editor-tz" style={{ fontSize: '0.78rem', color: '#666' }}>Timezone</label>
          <CFormSelect
            id="date-editor-tz" size="sm" style={{ width: 220 }}
            value={timezone} onChange={(e) => setTimezone(e.target.value)}
          >
            {tzOptions.map((tz) => <option key={tz} value={tz}>{tz}</option>)}
          </CFormSelect>
          <CButton
            size="sm" color="secondary" variant="outline" data-testid="discard"
            disabled={stagedCount === 0 || sending} onClick={handleDiscard}
          >
            Discard
          </CButton>
          <CButton
            size="sm" color="primary" data-testid="confirm"
            disabled={stagedCount === 0 || sending}
            onClick={() => { setSendError(null); setConfirmOpen(true); }}
          >
            Confirm ({stagedCount})
          </CButton>
        </div>
      </div>

      {sendError && !confirmOpen && (
        <CAlert color="warning" dismissible onClose={() => setSendError(null)}>{sendError}</CAlert>
      )}

      {loading && (
        <div className="text-center py-5"><CSpinner color="primary" /></div>
      )}
      {!loading && loadError && <CAlert color="danger">{loadError}</CAlert>}
      {!loading && !loadError && files.length === 0 && (
        <div className="text-muted py-4 text-center">This event has no photos.</div>
      )}

      {!loading && !loadError && groups.map((g) => (
        <DateGroupCard
          key={g.day || 'none'}
          group={g}
          catalogId={catalogId}
          eventFiles={viewed}
          onApply={handleApply}
          onUndo={handleUndo}
          flashKey={flash.day === g.day ? flash.key : 0}
          disabled={sending}
        />
      ))}

      <CModal visible={confirmOpen} onClose={() => !sending && setConfirmOpen(false)} alignment="center">
        <CModalHeader>
          <CModalTitle>Change photo dates</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p data-testid="confirm-summary">
            Change the date of <strong>{stagedCount} photo{stagedCount === 1 ? '' : 's'}</strong>.
            {' '}{groupSummary.join(', ')} ({timezone}).
          </p>
          <p className="mb-0" style={{ fontSize: '0.88rem', color: '#555' }}>
            Files will be renamed and moved to their new date folders. Create Events
            then runs, and this event may be split, merged or replaced.
          </p>
          {sendError && <CAlert color="warning" className="mt-3 mb-0">{sendError}</CAlert>}
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" disabled={sending}
                   onClick={() => setConfirmOpen(false)}>
            Cancel
          </CButton>
          <CButton color="primary" disabled={sending} onClick={send} data-testid="confirm-send">
            {sending ? <CSpinner size="sm" /> : 'Change dates'}
          </CButton>
        </CModalFooter>
      </CModal>
    </CContainer>
  );
};

export default EventDateEditor;
