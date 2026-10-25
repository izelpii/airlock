// ─── Grouping a flat media list by year / month / day ────────────────────────
//
// Extracted from Photos.js so the Videos page can group identically
// (video-page-spec.md section 6.2). It is date logic, not photo logic: it reads
// only `master_datetime` and `creation_date`, both of which every media row has.
//
// MOVED VERBATIM. The sentinel values below are load-bearing — Photos.js filters
// on `!== -1` in eleven places and on `!== 'Unknown'` when listing years — so
// this was not the moment to "improve" how unparseable dates are handled.

/**
 * Group media into { year: { monthIdx: { day: [item, …] } } }.
 *
 * Items with no usable date land under year `'Unknown'`, month `-1`, day `-1`.
 * Callers filter those sentinels out rather than this dropping the items:
 * a photo with a broken date should still be reachable somewhere.
 */
// The day a photograph belongs to: the date written in its own timestamp.
//
// NOT `new Date(x)`, which converts to the BROWSER'S timezone. 39 % of the
// reference catalog's timestamps carry a zone (56,846 end in Z, 89,073 an
// offset), and converting moves 3,408 of them across midnight - so a
// photograph taken at 23:00 in Tokyo would be filed on the previous day
// because it is being looked at from New York.
//
// Two reasons for the literal date. It is more correct for a library - a
// picture belongs to the day it was taken, where it was taken, and that does
// not change when you fly home. And it is the only reading the SERVER can
// share: /photos/calendar groups 370,751 rows in SQL and has no idea what
// timezone a browser is in, so any other rule gives a day that is listed with
// one count and opens with another.
function dayPartsOf(dateSrc) {
  if (!dateSrc) return null;
  // 'YYYY-MM-DD...' - the shape of every stored timestamp, ISO with or
  // without a zone.
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(dateSrc));
  if (m) {
    return { year: Number(m[1]), monthIdx: Number(m[2]) - 1, day: Number(m[3]) };
  }
  // Anything else: fall back to parsing, which is better than 'Unknown'.
  const d = new Date(dateSrc);
  if (isNaN(d.getTime())) return null;
  return { year: d.getFullYear(), monthIdx: d.getMonth(), day: d.getDate() };
}

export function groupByYearMonthDay(items) {
  const grouped = {};
  for (const item of items) {
    let year = 'Unknown';
    let monthIdx = -1;
    let day = -1;
    const parts = dayPartsOf(item.master_datetime);
    if (parts) {
      year = parts.year;
      monthIdx = parts.monthIdx;
      day = parts.day;
    }
    if (!grouped[String(year)]) grouped[String(year)] = {};
    if (!grouped[String(year)][monthIdx]) grouped[String(year)][monthIdx] = {};
    if (!grouped[String(year)][monthIdx][day]) grouped[String(year)][monthIdx][day] = [];
    grouped[String(year)][monthIdx][day].push(item);
  }
  return grouped;
}

export const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

/**
 * Every item inside the chosen scope, oldest first.
 *
 * The scope is however much of the date the user has picked, and each level may
 * be null — which is what the "All" buttons on the Photos and Videos date cards
 * produce:
 *
 *   (null, null, null)   everything in the catalog
 *   (2026, null, null)   All 2026
 *   (2026, 7,    null)   All August 2026
 *   (2026, 7,    10)     one day
 *
 * One function for both pages and for both consumers on each page — the
 * filmstrip and the viewer's own prev/next list — because a strip that shows a
 * different set, or the same set in a different order, from the list the arrow
 * keys walk is the kind of mismatch nobody reports and everybody feels.
 *
 * Undated items (the 'Unknown' / -1 sentinels) are inside "everything": they
 * are in the catalog, and the whole point of the sentinel is that they stay
 * reachable.
 */
export function itemsInScope(grouped, year, month, day) {
  const out = [];
  const pushDays = (days) => {
    Object.values(days ?? {}).forEach((list) => out.push(...list));
  };

  if (year == null) {
    Object.values(grouped).forEach((months) => Object.values(months).forEach(pushDays));
  } else if (month == null) {
    Object.values(grouped[String(year)] ?? {}).forEach(pushDays);
  } else if (day == null) {
    pushDays(grouped[String(year)]?.[month]);
  } else {
    out.push(...(grouped[String(year)]?.[month]?.[day] ?? []));
  }

  return out.sort((a, b) => {
    const ta = new Date(a.master_datetime).getTime();
    const tb = new Date(b.master_datetime).getTime();
    return ta - tb;
  });
}
