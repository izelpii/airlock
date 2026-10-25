import React from 'react';
import { CBadge } from '@coreui/react';

// ─── The report idiom ─────────────────────────────────────────────────────────
// The shapes every section of the Content report is built from, in one place so
// nine sections cannot drift into nine designs. Same card as People and
// Ingestion Runs: 290 px, white, 1px #e0e0e0, radius 10, number over label.

export const CARD_WIDTH = 290;

export const n = (value) => (value == null ? '—' : Number(value).toLocaleString());

export const bytes = (value) => {
  if (value == null) return '—';
  let x = Number(value);
  for (const unit of ['B', 'KB', 'MB', 'GB', 'TB']) {
    if (Math.abs(x) < 1000 || unit === 'TB') return `${x.toFixed(x < 10 ? 1 : 0)} ${unit}`;
    x /= 1000;
  }
  return `${x} TB`;
};

// One big number with its label. `title` is not decoration: most of these words
// have a second plausible meaning and the card is too small to spell it out.
export const Figure = ({ value, label, title, color = '#212121', sub }) => (
  <div title={title} style={{
    flexShrink: 0, minWidth: 130, background: '#fff',
    border: '1px solid #e0e0e0', borderRadius: 10, padding: '12px 14px',
  }}>
    <div style={{ fontSize: '1.4rem', fontWeight: 600, color, lineHeight: 1.1 }}>
      {value}
    </div>
    <div style={{ fontSize: '0.72rem', color: '#888', marginTop: 3 }}>{label}</div>
    {sub && <div style={{ fontSize: '0.68rem', color: '#aaa', marginTop: 2 }}>{sub}</div>}
  </div>
);

export const FigureRow = ({ children }) => (
  <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap' }}>{children}</div>
);

// A proportion drawn as one bar. Used for coverage and for date provenance,
// where the SHAPE is the message and the exact pixel is not.
export const Meter = ({ covered, total, color = '#2e7d32', height = 8 }) => {
  const pct = total > 0 ? Math.min(100, (covered / total) * 100) : 0;
  return (
    <div style={{ background: '#eee', borderRadius: height, height, overflow: 'hidden' }}>
      <div style={{ width: `${pct}%`, height: '100%', background: color }} />
    </div>
  );
};

// A coverage line: what is covered, and what is not. The second number is the
// point - see contents-spec.md section 5.7.
export const CoverageRow = ({ what, covered, of, used, detail, href }) => {
  const missing = Math.max(0, (of || 0) - (covered || 0));
  const pct = of > 0 ? Math.round((covered / of) * 100) : 0;
  return (
    <div style={{ padding: '10px 0', borderBottom: '1px solid #f0f0f0' }}>
      <div className="d-flex align-items-baseline justify-content-between mb-1">
        <div style={{ fontSize: '0.86rem', fontWeight: 500 }}>{what}</div>
        {/* An unused feature is reported as unused, never as a failure: albums,
            people, locations and tags are all empty on a real catalog, so this
            is the common case and red would invent a problem. */}
        {used ? (
          <div style={{ fontSize: '0.8rem', color: '#555' }}>
            {n(covered)} <span style={{ color: '#aaa' }}>of {n(of)}</span>
            {missing > 0 && (
              <span style={{ color: '#ef6c00' }}> · {n(missing)} not</span>
            )}
          </div>
        ) : (
          <CBadge color="light" className="text-dark" style={{ fontSize: '0.68rem' }}>
            not used yet
          </CBadge>
        )}
      </div>
      {used && <Meter covered={covered} total={of} />}
      <div style={{ fontSize: '0.72rem', color: '#999', marginTop: 4 }}>
        {used ? `${pct}% · ${detail}` : detail}
        {href && used && missing > 0 && <> · <a href={href}>fix</a></>}
      </div>
    </div>
  );
};

// A short table. Every section that is a distribution uses this one.
export const MiniTable = ({ columns, rows }) => (
  <table className="table table-sm mb-0" style={{ fontSize: '0.8rem' }}>
    <thead>
      <tr>
        {columns.map((c) => (
          <th key={c.key} style={{ textAlign: c.numeric ? 'right' : 'left',
                                    fontWeight: 600, color: '#666',
                                    borderTop: 0, whiteSpace: 'nowrap' }}>
            {c.label}
          </th>
        ))}
      </tr>
    </thead>
    <tbody>
      {rows.map((row, i) => (
        <tr key={row.key || i}>
          {columns.map((c) => (
            <td key={c.key} style={{ textAlign: c.numeric ? 'right' : 'left',
                                      whiteSpace: 'nowrap' }}>
              {c.render ? c.render(row) : row[c.key]}
            </td>
          ))}
        </tr>
      ))}
    </tbody>
  </table>
);

export const Section = ({ title, hint, children, right }) => (
  <div style={{ background: '#fff', border: '1px solid #e0e0e0', borderRadius: 10,
                padding: '14px 16px', marginBottom: '1rem' }}>
    <div className="d-flex align-items-start justify-content-between mb-2">
      <div>
        <div style={{ fontWeight: 600, fontSize: '0.95rem' }}>{title}</div>
        {hint && <div style={{ fontSize: '0.75rem', color: '#999', marginTop: 2 }}>{hint}</div>}
      </div>
      {right}
    </div>
    {children}
  </div>
);
