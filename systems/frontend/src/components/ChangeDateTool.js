import React, { useEffect, useMemo, useState } from 'react';
import {
  CAlert, CButton, CCard, CCardBody, CCardHeader, CModal, CModalBody, CModalFooter,
  CModalHeader, CModalTitle, CSpinner,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilCalendar } from '@coreui/icons';
import { photoAPI } from '../services/api';
import { getBrowserTimezone, getTimezoneOptions } from '../utils/timezones';
import { formatDayShort, formatDayTitle, formatTime12, parseWallTime } from '../utils/wallTime';

// ─── The Change date tool ─────────────────────────────────────────────────────
// saved-search-tools-spec.md section 5. A card in a saved search's tool panel:
// every selected file gets a new date through the same batch the event date
// editor uses (POST /photos/dates), so each file is renamed, moved and
// regrouped into events exactly as a one-photo Edit date would do, with one
// Create Events run for the whole batch.

// The batch endpoint's own cap (DATE_BATCH_MAX_FILES, section 5.1).
export const MAX_FILES = 5000;

const LABEL = { fontSize: '0.72rem', color: '#888' };

/**
 * The batch's `changes` for these files (section 5.2). With a time, one entry:
 * every file gets the same instant. Without one, each file keeps its own time
 * of day, as stored, so a group's order survives: one entry per distinct time.
 * A file with no date gets 12:00:00.
 */
export function buildDateChanges(fileIds, takenById, date, time, timezone) {
  if (time) {
    const t = time.length === 5 ? `${time}:00` : time;
    return [{ file_ids: [...fileIds], datetime: `${date}T${t}`, timezone }];
  }
  const byTime = new Map();
  fileIds.forEach((id) => {
    const w = parseWallTime(takenById.get(id));
    const pad = (n) => String(n).padStart(2, '0');
    const t = w ? `${pad(w.hour)}:${pad(w.minute)}:${pad(w.second)}` : '12:00:00';
    if (!byTime.has(t)) byTime.set(t, []);
    byTime.get(t).push(id);
  });
  return [...byTime.entries()].map(([t, ids]) => ({
    file_ids: ids, datetime: `${date}T${t}`, timezone,
  }));
}

/** The span of the files' current days: {first, last} as parsed wall times, or null. */
export function dateSpan(fileIds, takenById) {
  let first = null;
  let last = null;
  fileIds.forEach((id) => {
    const w = parseWallTime(takenById.get(id));
    if (!w) return;
    if (!first || w.sortKey < first.sortKey) first = w;
    if (!last || w.sortKey > last.sortKey) last = w;
  });
  return first ? { first, last } : null;
}

export function describeSpan(span) {
  if (!span) return null;
  return span.first.day === span.last.day
    ? formatDayShort(span.first)
    : `${formatDayShort(span.first)} – ${formatDayShort(span.last)}`;
}

const ChangeDateTool = ({
  catalogId, savedSearchId, selectedIds, takenById, canEdit, indexInPlace,
  busy, onStarted,
}) => {
  const count = selectedIds.length;
  const span = useMemo(() => dateSpan(selectedIds, takenById), [selectedIds, takenById]);
  const [date, setDate] = useState('');
  const [time, setTime] = useState('');
  const [tz, setTz] = useState(getBrowserTimezone);
  const [confirming, setConfirming] = useState(false);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState(null);

  // One shared day starts as the date, so a one-day correction is one edit
  // (section 5.1). Keyed on the days, not the span object, so a page loading
  // or a re-render never wipes what is being typed.
  const sharedDay = span && span.first.day === span.last.day ? span.first.day : '';
  const spanKey = span ? `${span.first.day}|${span.last.day}|${count}` : `|${count}`;
  useEffect(() => {
    setDate(sharedDay);
    setError(null);
  }, [spanKey]); // eslint-disable-line react-hooks/exhaustive-deps

  const reason = !canEdit ? 'Changing dates needs editor access.'
    : busy ? 'A date change is running for this saved search.'
    : count > MAX_FILES ? `At most ${MAX_FILES.toLocaleString()} at a time. Select fewer, or change them in parts.`
    : null;
  const disabled = Boolean(reason) || count === 0 || !date || sending;

  const send = async () => {
    setSending(true);
    setError(null);
    try {
      const changes = buildDateChanges(selectedIds, takenById, date, time, tz);
      const r = await photoAPI.updateDates(catalogId, null, changes, true, savedSearchId);
      setConfirming(false);
      setTime('');
      onStarted(r.data.batch_id, r.data.file_count);
    } catch (e) {
      setConfirming(false);
      setError(e.response?.data?.message || e.response?.data?.error || 'Could not change the dates.');
    } finally {
      setSending(false);
    }
  };

  const target = parseWallTime(date ? `${date}T${time || '12:00:00'}` : null);
  const files = `${count.toLocaleString()} ${count === 1 ? 'photo or video' : 'photos and videos'}`;

  return (
    <CCard data-testid="change-date-tool">
      <CCardHeader className="d-flex align-items-center py-2">
        <CIcon icon={cilCalendar} className="me-2" />
        <strong>Change date</strong>
      </CCardHeader>
      <CCardBody className="py-2">
        <fieldset disabled={!canEdit || busy}>
          <div className="mb-2">
            <label style={LABEL} htmlFor="ss-date">Date</label>
            <input id="ss-date" type="date" className="form-control form-control-sm"
                   value={date} onChange={(e) => setDate(e.target.value)} />
          </div>
          <div className="mb-2">
            <label style={LABEL} htmlFor="ss-time">Time</label>
            <input id="ss-time" type="time" step="1" className="form-control form-control-sm"
                   value={time} onChange={(e) => setTime(e.target.value)} />
            {!time && <div style={LABEL}>Empty: each keeps its own time</div>}
          </div>
          <div className="mb-2">
            <label style={LABEL} htmlFor="ss-tz">Timezone</label>
            <select id="ss-tz" className="form-select form-select-sm" value={tz}
                    onChange={(e) => setTz(e.target.value)}>
              {getTimezoneOptions().map((z) => <option key={z} value={z}>{z}</option>)}
            </select>
          </div>
        </fieldset>
        {reason && <div style={{ fontSize: '0.75rem' }} className="text-body-secondary mb-2">{reason}</div>}
        {error && <CAlert color="danger" className="py-1 px-2 mb-2" style={{ fontSize: '0.78rem' }}>{error}</CAlert>}
        <CButton size="sm" color="primary" className="w-100" disabled={disabled}
                 onClick={() => setConfirming(true)}>
          {count ? `Change date of ${count.toLocaleString()}…` : 'Change date…'}
        </CButton>
      </CCardBody>

      <CModal visible={confirming} onClose={() => !sending && setConfirming(false)} alignment="center">
        <CModalHeader>
          <CModalTitle>Change the date of {files}?</CModalTitle>
        </CModalHeader>
        <CModalBody>
          {target && (
            <p className="mb-2">
              {time ? (
                <>They will all be dated <strong>{formatDayTitle(target)}, {formatTime12(target)}</strong> ({tz}).</>
              ) : (
                <>They will be dated <strong>{formatDayTitle(target)}</strong>, each keeping its own
                  time of day ({tz}).</>
              )}
            </p>
          )}
          {span && <p className="mb-2 text-body-secondary">Today they span {describeSpan(span)}.</p>}
          <ul className="mb-0" style={{ fontSize: '0.9rem' }}>
            <li>
              {indexInPlace
                ? 'Each file\'s date is changed in the catalog; the original stays where it is.'
                : 'Each file is renamed and moved to its new date folder, with its thumbnails and previews.'}
            </li>
            <li>Any RAW or JPG partner of these files changes with it.</li>
            <li>
              <strong>Create Events then runs for the catalog</strong>, so these photos join the events of
              their new date. Events may be split, merged or replaced, and automatic events get new ids.
            </li>
            <li>There is no undo. To go back, change the dates again.</li>
          </ul>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" disabled={sending} onClick={() => setConfirming(false)}>
            Cancel
          </CButton>
          <CButton color="primary" disabled={sending} onClick={send}>
            {sending && <CSpinner size="sm" className="me-1" />}
            Change {count.toLocaleString()} {count === 1 ? 'date' : 'dates'}
          </CButton>
        </CModalFooter>
      </CModal>
    </CCard>
  );
};

export default ChangeDateTool;
