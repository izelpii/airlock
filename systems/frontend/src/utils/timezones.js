// ─── Timezones for the date forms ────────────────────────────────────────────
//
// Moved out of Photos.js, Videos.js and PhotoDetail.js, which each built the
// same list inline, so the event date editor offers exactly the same choices
// (mass-edit-event-photo-dates-spec.md section 4.1).
//
// The application stores no timezone for the user, so the browser's is the
// only one there is (section 2.3). Every date form defaults to it.

// For older browsers without Intl.supportedValuesOf. PhotoDetail's longer list
// was the most complete of the three inline copies, so it is the one kept.
const FALLBACK_TIMEZONES = [
  'UTC',
  'America/New_York', 'America/Chicago', 'America/Denver', 'America/Los_Angeles',
  'America/Sao_Paulo', 'Europe/London', 'Europe/Paris', 'Europe/Berlin',
  'Europe/Moscow', 'Asia/Dubai', 'Asia/Kolkata', 'Asia/Bangkok', 'Asia/Tokyo',
  'Asia/Shanghai', 'Australia/Sydney', 'Pacific/Auckland',
];

/** Sorted IANA timezone names, or a short curated list on older browsers. */
export function getTimezoneOptions() {
  try {
    return Intl.supportedValuesOf('timeZone');
  } catch {
    return FALLBACK_TIMEZONES;
  }
}

/** The browser's IANA timezone, which every date form starts on. */
export function getBrowserTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC';
}
