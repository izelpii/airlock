// ─── Wall-clock dates, read literally ────────────────────────────────────────
//
// Event times and master_datetime are wall-clock values: what the camera's
// clock said, in whatever zone it was set to. They are read here as strings,
// never through `new Date(string)`. A trailing `Z`, or no offset at all, would
// make the browser shift the shown time by the viewer's zone, the bug fixed
// for photos in resolve_master_datetime
// (mass-edit-event-photo-dates-spec.md sections 3.2 and 5.5).
//
// Weekdays are computed with Date.UTC, which is pure calendar arithmetic and
// so does not depend on the viewer's zone either.

export const MONTHS_LONG = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];
export const MONTHS_SHORT = MONTHS_LONG.map((m) => m.slice(0, 3));
export const WEEKDAYS_LONG = [
  'Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday',
];
export const WEEKDAYS_SHORT = WEEKDAYS_LONG.map((d) => d.slice(0, 3));

const WALL_RE = /^(\d{4})-(\d{2})-(\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?/;

/**
 * Parse "YYYY-MM-DD[( |T)HH:MM[:SS]]" with anything after it (fractions, `Z`,
 * an offset) ignored. Returns null when the string does not start that way.
 *
 * `day` is "YYYY-MM-DD" and `minute` "YYYY-MM-DDTHH:MM", the two keys the
 * date editor groups and stages by.
 */
export function parseWallTime(value) {
  if (!value || typeof value !== 'string') return null;
  const m = WALL_RE.exec(value.trim());
  if (!m) return null;
  const [, y, mo, d, hh = '00', mi = '00', ss = '00'] = m;
  const year = Number(y);
  const month = Number(mo);
  const dayNum = Number(d);
  if (month < 1 || month > 12 || dayNum < 1 || dayNum > 31) return null;
  return {
    year, month, dayOfMonth: dayNum,
    hour: Number(hh), minute: Number(mi), second: Number(ss),
    day: `${y}-${mo}-${d}`,
    minuteKey: `${y}-${mo}-${d}T${hh}:${mi}`,
    sortKey: `${y}-${mo}-${d}T${hh}:${mi}:${ss}`,
    hasTime: m[4] !== undefined,
  };
}

/** 0 = Sunday, for a parsed wall time or a "YYYY-MM-DD" string. */
export function weekdayIndex(w) {
  const p = typeof w === 'string' ? parseWallTime(w) : w;
  if (!p) return null;
  return new Date(Date.UTC(p.year, p.month - 1, p.dayOfMonth)).getUTCDay();
}

/** "6:00 AM" */
export function formatTime12(w) {
  const p = typeof w === 'string' ? parseWallTime(w) : w;
  if (!p) return '';
  const h12 = p.hour % 12 === 0 ? 12 : p.hour % 12;
  return `${h12}:${String(p.minute).padStart(2, '0')} ${p.hour < 12 ? 'AM' : 'PM'}`;
}

/** "Thu, Oct 2, 2026" — a date group's title. */
export function formatDayTitle(w) {
  const p = typeof w === 'string' ? parseWallTime(w) : w;
  if (!p) return 'No date';
  return `${WEEKDAYS_SHORT[weekdayIndex(p)]}, ${MONTHS_SHORT[p.month - 1]} ${p.dayOfMonth}, ${p.year}`;
}

/** "Oct 2, 2026" */
export function formatDayShort(w) {
  const p = typeof w === 'string' ? parseWallTime(w) : w;
  if (!p) return 'no date';
  return `${MONTHS_SHORT[p.month - 1]} ${p.dayOfMonth}, ${p.year}`;
}

/** Whole days from day `a` to day `b`, both "YYYY-MM-DD". */
export function daysBetween(a, b) {
  const pa = parseWallTime(a);
  const pb = parseWallTime(b);
  if (!pa || !pb) return Infinity;
  return Math.round(
    (Date.UTC(pb.year, pb.month - 1, pb.dayOfMonth)
     - Date.UTC(pa.year, pa.month - 1, pa.dayOfMonth)) / 86400000,
  );
}
