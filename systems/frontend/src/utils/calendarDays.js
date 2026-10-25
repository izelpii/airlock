// Days in the calendar counts (GET /calendar: [year, month, day, photos,
// videos, in_an_event], newest first) and where a click on one goes.
// documentation/architecture/calendar-view-spec.md sections 6 and 7.

const pad = (n) => String(n).padStart(2, '0');

export const isoOf = (row) => `${row[0]}-${pad(row[1])}-${pad(row[2])}`;

export const totalOf = (row) => row[3] + row[4];

// Events when any of the day's files is in an event; otherwise Photos, or
// Videos for a day of videos only (owner, 2026-10-18).
export const linkFor = (row) => {
  const iso = isoOf(row);
  if (row[5] > 0) return `/events?date=${iso}`;
  if (row[3] > 0) return `/photos?date=${iso}`;
  return `/videos?date=${iso}`;
};

// The days with files either side of `iso`, from the newest-first list.
export const neighbours = (days, iso) => {
  const keys = (days || []).map(isoOf);
  const later = [...keys].reverse().find((k) => k > iso) || null;
  const earlier = keys.find((k) => k < iso) || null;
  return { earlier, later };
};

export const longDate = (iso) => new Date(`${iso}T12:00:00`).toLocaleDateString(undefined, {
  weekday: 'long', day: 'numeric', month: 'long', year: 'numeric',
});
