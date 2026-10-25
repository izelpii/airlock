import React from 'react';
import { CCard, CCardBody, CCardHeader, CButton } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilCalendar } from '@coreui/icons';
import {
  parseWallTime, weekdayIndex, formatTime12,
  MONTHS_LONG, MONTHS_SHORT, WEEKDAYS_LONG, WEEKDAYS_SHORT,
} from '../utils/wallTime';

// ─── The event's Date card ───────────────────────────────────────────────────
//
// When the event happened, and the way into the date editor
// (mass-edit-event-photo-dates-spec.md section 3.2). It follows the Photos
// page's day-number pattern, larger, because this card has more room.
//
// Times are shown as stored, never converted: start_time and end_time are
// wall-clock strings, read by parseWallTime rather than `new Date()`.

const pad2 = (n) => String(n).padStart(2, '0');

/**
 * What the card shows, as plain strings. Exported so the literal parsing can
 * be tested without rendering.
 */
export function describeEventDates(startTime, endTime) {
  const start = parseWallTime(startTime);
  if (!start) return null;
  const end = parseWallTime(endTime) || start;
  const sameDay = start.day === end.day;

  const dayNumber = sameDay
    ? pad2(start.dayOfMonth)
    : `${pad2(start.dayOfMonth)}–${pad2(end.dayOfMonth)}`;

  const weekday = sameDay
    ? WEEKDAYS_LONG[weekdayIndex(start)].toUpperCase()
    : `${WEEKDAYS_SHORT[weekdayIndex(start)]} – ${WEEKDAYS_SHORT[weekdayIndex(end)]}`.toUpperCase();

  let monthLine;
  if (sameDay || (start.year === end.year && start.month === end.month)) {
    monthLine = `${MONTHS_LONG[start.month - 1]} ${start.year}`;
  } else if (start.year === end.year) {
    monthLine = `${MONTHS_SHORT[start.month - 1]} ${start.dayOfMonth} – `
      + `${MONTHS_SHORT[end.month - 1]} ${end.dayOfMonth}, ${start.year}`;
  } else {
    monthLine = `${MONTHS_SHORT[start.month - 1]} ${start.dayOfMonth}, ${start.year} – `
      + `${MONTHS_SHORT[end.month - 1]} ${end.dayOfMonth}, ${end.year}`;
  }

  // A single-photo event, or one whose photos share a minute, shows one time.
  const startT = formatTime12(start);
  const endT = formatTime12(end);
  const timeLine = startT === endT ? startT : `${startT} – ${endT}`;

  return { dayNumber, weekday, monthLine, timeLine };
}

const EventDateCard = ({ event, onModify }) => {
  const d = describeEventDates(event?.start_time, event?.end_time);

  return (
    <CCard className="h-100">
      <CCardHeader className="d-flex align-items-center gap-2">
        <CIcon icon={cilCalendar} style={{ width: 18, height: 18, color: '#1a73e8' }} />
        <strong style={{ fontSize: '1rem', color: '#212121' }}>Date</strong>
      </CCardHeader>
      <CCardBody className="d-flex flex-column">
        {d ? (
          <div className="d-flex align-items-start" style={{ gap: 12 }}>
            <span
              data-testid="event-date-day"
              style={{ fontSize: '2.4rem', fontWeight: 800, color: '#212121', lineHeight: 1 }}
            >
              {d.dayNumber}
            </span>
            <div style={{ minWidth: 0 }}>
              <div
                data-testid="event-date-weekday"
                style={{
                  fontSize: '1.1rem', fontWeight: 800, letterSpacing: '0.5px',
                  color: '#212121', lineHeight: 1.2,
                }}
              >
                {d.weekday}
              </div>
              <div data-testid="event-date-month" style={{ fontSize: '0.9rem', color: '#666' }}>
                {d.monthLine}
              </div>
              <div data-testid="event-date-time" style={{ fontSize: '0.85rem', color: '#666' }}>
                {d.timeLine}
              </div>
            </div>
          </div>
        ) : (
          <div className="text-muted" style={{ fontSize: '0.85rem' }}>This event has no date.</div>
        )}

        {/* No edit-rights check: the event page has none today (the Details
            card's Edit button is shown to everyone), so neither does this. */}
        <div className="mt-auto pt-3">
          <CButton color="primary" variant="outline" size="sm" className="w-100" onClick={onModify}>
            <CIcon icon={cilCalendar} style={{ width: 13, height: 13 }} className="me-1" />
            Modify dates
          </CButton>
        </div>
      </CCardBody>
    </CCard>
  );
};

export default EventDateCard;
