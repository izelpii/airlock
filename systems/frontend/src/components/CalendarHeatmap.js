import React, { useEffect, useMemo, useRef, useState } from 'react';
import { select } from 'd3-selection';
import { scaleSequentialSymlog } from 'd3-scale';
import { utcMonday, utcDay, utcDays, utcMonths } from 'd3-time';
import { utcFormat } from 'd3-time-format';

// ─── A calendar heatmap: photos and videos per day ────────────────────────────
// documentation/architecture/calendar-view-spec.md section 5, after
// https://observablehq.com/@d3/calendar/2.
//
// Blocks of days drawn as weeks (columns) by weekday (rows, MONDAY at the top -
// the owner's choice), one block per year in Calendar view, one rolling year on
// the Dashboard. ONE CONTINUOUS BLOCK of weeks, with no lines or gaps between
// months (owner, 2026-10-18): each month's label sits over the column of its
// first Monday, and the days of the month before that Monday fall in the
// column to its left. React owns the data; D3 owns the SVG and redraws it only when
// the data, the blocks or the width change.
//
// Colour: from near-white at 0 to the app's photo blue (#2a78d6, the
// Dashboard's PHOTO) at the catalog's busiest day (owner, 2026-10-18), on a
// symmetric-log scale - logarithmic, because a few days hold thousands and
// most hold tens, but defined at 0, so a day with one photograph is already
// faintly blue and never mistaken for an empty one. The top is the CATALOG'S
// busiest day (passed in), so a colour means the same number on every page
// and on the Dashboard.

// The two ends. ZERO is the near-white the previous scheme had at its centre;
// a day with nothing takes it too.
export const ZERO_COLOR = '#fafafa';
export const TOP_COLOR = '#2a78d6';
export const EMPTY_FILL = ZERO_COLOR;
const HOVER_STROKE = '#212529';                  // the hovered day's outline

const hex = (c) => [1, 3, 5].map((i) => parseInt(c.slice(i, i + 2), 16));
const [Z, T] = [hex(ZERO_COLOR), hex(TOP_COLOR)];
const zeroToTop = (t) => `rgb(${Z.map((z, i) => Math.round(z + (T[i] - z) * t)).join(', ')})`;
const LEFT = 46;                                 // the year label and weekday labels
const TOP = 18;                                  // month labels

export const colorScale = (busiest) => scaleSequentialSymlog(
  [0, Math.max(1, busiest || 1)], zeroToTop).clamp(true);

const countDay = (d) => (d.getUTCDay() + 6) % 7;       // Monday = 0
const formatDay = utcFormat('%A, %-d %B %Y');
const formatMonth = utcFormat('%b');
const keyOf = (d) => d.toISOString().slice(0, 10);

// A block's columns: its weeks, Monday-first.
export const weeksOf = (b) => utcMonday.count(utcMonday.floor(b.start), utcDay.offset(b.end, -1)) + 1;

const filesText = (n) => (n === 1 ? '1 photo or video' : `${n.toLocaleString()} photos and videos`);

/**
 * days     [[y, m, d, photos, videos, in_event], ...]
 * blocks   [{key, label, start: Date (UTC midnight), end: Date (exclusive), total}]
 * busiest  the catalog's busiest day, the colour domain's top
 * cell     preferred square size; shrinks to fit the width, never below minCell
 */
const CalendarHeatmap = ({ days, blocks, busiest, cell = 17, minCell = 9, onDayClick,
                           showTotals = true, ariaLabel = 'Photos and videos per day' }) => {
  const wrapRef = useRef(null);
  const svgRef = useRef(null);
  const [width, setWidth] = useState(0);
  const [tip, setTip] = useState(null);

  const byDay = useMemo(() => {
    const m = new Map();
    for (const r of days || []) {
      m.set(`${r[0]}-${String(r[1]).padStart(2, '0')}-${String(r[2]).padStart(2, '0')}`, r);
    }
    return m;
  }, [days]);

  useEffect(() => {
    const el = wrapRef.current;
    if (!el) return undefined;
    // No ResizeObserver (jsdom, old browsers): measure once, assume a desktop width.
    if (typeof ResizeObserver === 'undefined') { setWidth(el.clientWidth || 960); return undefined; }
    const ro = new ResizeObserver(() => setWidth(el.clientWidth));
    ro.observe(el);
    setWidth(el.clientWidth);
    return () => ro.disconnect();
  }, []);

  const right = showTotals ? 120 : 8;
  const weeksMax = Math.max(1, ...blocks.map(weeksOf));
  const size = Math.max(minCell, Math.min(cell, Math.floor((width - LEFT - right) / weeksMax) || cell));
  const blockH = size * 7 + TOP + 14;
  const svgW = LEFT + weeksMax * size + right;
  const svgH = blocks.length * blockH;

  useEffect(() => {
    const svg = select(svgRef.current);
    svg.selectAll('*').remove();
    if (!width || !blocks.length) return;
    const color = colorScale(busiest);

    const g = svg.selectAll('g.block').data(blocks).join('g')
      .attr('class', 'block')
      .attr('transform', (_, i) => `translate(${LEFT + 0.5},${i * blockH + TOP + 0.5})`);

    // The block's label (the year) above the weekday labels, as in the example.
    g.append('text')
      .attr('x', -6).attr('y', -6).attr('text-anchor', 'end')
      .attr('font-weight', 600).attr('font-size', 12).attr('fill', '#212529')
      .text((b) => b.label);

    g.append('g').attr('font-size', 9).attr('fill', '#6c757d').attr('text-anchor', 'end')
      .selectAll('text').data(['Mon', 'Wed', 'Fri']).join('text')
      .attr('x', -6).attr('y', (_, i) => (i * 2 + 0.5) * size).attr('dy', '0.31em')
      .text((t) => (size >= 11 ? t : t[0]));

    // A day's column: its week, counted from the block's first Monday.
    const colOf = (b, d) => utcMonday.count(utcMonday.floor(b.start), d);

    g.append('g').selectAll('rect')
      .data((b) => utcDays(b.start, b.end).map((d) => ({ b, d })))
      .join('rect')
      .attr('width', size - 1.5).attr('height', size - 1.5)
      .attr('x', ({ b, d }) => colOf(b, d) * size)
      .attr('y', ({ d }) => countDay(d) * size)
      .each(function each({ d }) {
        const row = byDay.get(keyOf(d));
        const n = row ? row[3] + row[4] : 0;
        select(this)
          .attr('fill', n ? color(n) : EMPTY_FILL)
          .style('cursor', n && onDayClick ? 'pointer' : 'default');
      })
      .on('mouseenter', function enter(event, { d }) {
        const row = byDay.get(keyOf(d));
        const n = row ? row[3] + row[4] : 0;
        select(this).attr('stroke', HOVER_STROKE).attr('stroke-width', 1.5);
        const box = wrapRef.current.getBoundingClientRect();
        setTip({
          x: event.clientX - box.left, y: event.clientY - box.top,
          title: formatDay(d),
          text: n ? `${filesText(n)}${row[3] && row[4] ? ` — ${row[3].toLocaleString()} photos, ${row[4].toLocaleString()} videos` : ''}`
            : 'Nothing taken',
        });
      })
      .on('mouseleave', function leave() {
        select(this).attr('stroke', null).attr('stroke-width', null);
        setTip(null);
      })
      .on('click', (_, { d }) => {
        const row = byDay.get(keyOf(d));
        if (row && onDayClick) onDayClick(row);
      });

    // Month labels over the column of each month's first Monday. Labels are
    // dropped where the next would overlap (a sliver of a month at the start of
    // the rolling year).
    g.append('g').selectAll('text')
      .data((b) => [b.start, ...utcMonths(utcDay.offset(b.start, 1), b.end)]
        .map((m) => ({ m, col: colOf(b, utcMonday.ceil(m)) }))
        .filter((x, i, all) => x.col < weeksOf(b)
          && (i + 1 === all.length || all[i + 1].col - x.col >= 2)))
      .join('text')
      .attr('x', (x) => x.col * size).attr('y', -5)
      .attr('font-size', 10).attr('fill', '#6c757d')
      .text((x) => formatMonth(x.m));

    if (showTotals) {
      g.append('text')
        .attr('x', (b) => weeksOf(b) * size + 8)
        .attr('y', 3.5 * size).attr('dy', '0.31em')
        .attr('font-size', 11).attr('fill', '#495057')
        .text((b) => (b.total ? `${b.total.toLocaleString()}` : ''));
    }
  }, [blocks, byDay, busiest, width, size, blockH, onDayClick, showTotals]);

  return (
    <div ref={wrapRef} style={{ position: 'relative', width: '100%', overflowX: 'hidden' }}>
      <svg ref={svgRef} width={svgW} height={svgH} role="img" aria-label={ariaLabel}
           style={{ display: 'block', maxWidth: '100%' }} />
      {tip && (
        <div style={{
          position: 'absolute', left: Math.min(tip.x + 12, Math.max(0, width - 240)), top: tip.y + 14,
          background: '#fff', border: '1px solid #dee2e6', borderRadius: 6,
          boxShadow: '0 2px 8px rgba(0,0,0,0.12)', padding: '6px 9px', fontSize: 12,
          pointerEvents: 'none', whiteSpace: 'nowrap', zIndex: 5, color: '#212529',
        }}>
          <div style={{ fontWeight: 600 }}>{tip.title}</div>
          <div style={{ color: '#495057' }}>{tip.text}</div>
        </div>
      )}
    </div>
  );
};

// The colour key: 0 to the busiest day, symmetric-log spaced.
export const CalendarLegend = ({ busiest }) => {
  const color = colorScale(busiest);
  const max = Math.max(1, busiest || 1);
  const pos = (v) => (100 * Math.log1p(v)) / Math.log1p(max);
  const stops = Array.from({ length: 21 }, (_, i) => Math.expm1((Math.log1p(max) * i) / 20));
  const marks = [0, 1, 10, 100, 1000, 10000].filter((v) => v < max * 0.8);
  return (
    <div className="d-flex align-items-center gap-2 flex-wrap" style={{ fontSize: 11, color: '#6c757d' }}>
      <div style={{ position: 'relative', width: 240, paddingBottom: 14 }}>
        <div style={{
          height: 10,
          background: `linear-gradient(to right, ${stops.map((v) => color(v)).join(',')})`,
        }} />
        {[...marks, max].map((v) => (
          <span key={v} style={{ position: 'absolute', left: `${pos(v)}%`, top: 12,
                                 transform: 'translateX(-50%)', whiteSpace: 'nowrap' }}>
            {v.toLocaleString()}
          </span>
        ))}
      </div>
      <span>photos and videos per day</span>
    </div>
  );
};

// Blocks: one per calendar year.
export const yearBlocks = (years, totals) => years.map((y) => ({
  key: String(y), label: String(y),
  start: new Date(Date.UTC(y, 0, 1)), end: new Date(Date.UTC(y + 1, 0, 1)),
  total: totals?.[y] || 0,
}));

// One block: the last 53 weeks, Monday-first, ending today (the Dashboard).
export const rollingYearBlock = (today = new Date(), total = 0) => {
  const end = new Date(Date.UTC(today.getFullYear(), today.getMonth(), today.getDate() + 1));
  const start = utcMonday.offset(utcMonday.floor(utcDay.offset(end, -1)), -52);
  return [{ key: 'recent', label: '', start, end, total }];
};

export default CalendarHeatmap;
