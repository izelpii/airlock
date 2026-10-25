import React from 'react';
import { CButton } from '@coreui/react';

// ─── A two-handle year range, with the data's own shape behind it ─────────────
// location-map-spec.md section 6.1.
//
// Two native range inputs rather than a dependency: CoreUI has no two-handle
// slider, and one control on one page is not worth a package.
//
// The histogram is the point of it. This catalog holds one location in 2011 and
// 235 in 2024, so a bare slider invites dragging into years that contain
// nothing; the bars make the empty stretches visible before you drag.

// `dark` because the overlay this lives in is dark now, and a light grey bar
// on a dark panel reads as a gap rather than as data.
const YearRange = ({ min, max, from, to, counts = {}, onChange, onReset,
                     dark = false }) => {
  if (min == null || max == null || max < min) return null;

  const ink = dark ? 'rgba(255,255,255,0.92)' : '#212121';
  const faint = dark ? 'rgba(255,255,255,0.55)' : '#888';
  const barOn = dark ? '#6ea8fe' : '#1a73e8';
  const barOff = dark ? 'rgba(255,255,255,0.18)' : '#dfe3e8';

  const years = [];
  for (let y = min; y <= max; y += 1) years.push(y);
  const peak = Math.max(1, ...years.map((y) => counts[String(y)] || 0));
  const all = from === min && to === max;

  // Dragging one handle past the other would invert the range, so each is
  // clamped against the other rather than being allowed to cross.
  const setFrom = (v) => onChange(Math.min(Number(v), to), to);
  const setTo = (v) => onChange(from, Math.max(Number(v), from));

  return (
    <div>
      <div className="d-flex justify-content-between align-items-baseline mb-1">
        <span style={{ fontSize: '0.78rem', fontWeight: 600, color: ink }}>
          {all ? 'All years' : from === to ? from : `${from} – ${to}`}
        </span>
        {!all && (
          <CButton color="link" size="sm" className="p-0"
                   style={{ fontSize: '0.74rem' }} onClick={onReset}>
            All years
          </CButton>
        )}
      </div>

      {/* Bars, one per year, shaded when inside the selection. Heights are
          sqrt-scaled: 2011 has 1 location and 2024 has 235, and on a linear
          scale every year but the largest would be a flat line. */}
      <div className="d-flex align-items-end gap-1" style={{ height: 38 }}>
        {years.map((y) => {
          const n = counts[String(y)] || 0;
          const inRange = y >= from && y <= to;
          return (
            <div key={y} title={`${y}: ${n.toLocaleString()} location${n === 1 ? '' : 's'}`}
                 style={{
                   flex: 1,
                   height: `${Math.max(n ? 8 : 2, Math.sqrt(n / peak) * 100)}%`,
                   background: inRange ? barOn : barOff,
                   borderRadius: 1,
                 }} />
          );
        })}
      </div>

      <input type="range" className="form-range" min={min} max={max} value={from}
             aria-label="First year"
             onChange={(e) => setFrom(e.target.value)} />
      <input type="range" className="form-range mt-n2" min={min} max={max} value={to}
             aria-label="Last year"
             onChange={(e) => setTo(e.target.value)} />

      <div className="d-flex justify-content-between"
           style={{ fontSize: '0.7rem', color: faint }}>
        <span>{min}</span>
        <span>{max}</span>
      </div>
    </div>
  );
};

export default YearRange;
