import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import CalendarHeatmap, { yearBlocks, rollingYearBlock, colorScale, EMPTY_FILL, weeksOf, ZERO_COLOR } from './CalendarHeatmap';

// [year, month, day, photos, videos, in_an_event]
const days = [[2024, 2, 29, 40, 2, 42], [2024, 1, 1, 1, 0, 0]];

let container;
let root;
beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const draw = (props) => act(() => {
  root.render(<CalendarHeatmap days={days} blocks={yearBlocks([2024], { 2024: 43 })} busiest={4530} {...props} />);
});
const rects = () => container.querySelectorAll('rect');
const fire = (el, type) => act(() => {
  el.dispatchEvent(new MouseEvent(type, { bubbles: true, clientX: 10, clientY: 10 }));
});

test('one square per day of the year, Monday first, coloured by count', () => {
  draw();
  expect(rects().length).toBe(366);                     // a leap year
  // 1 January 2024 was a Monday: top row, first column.
  expect(rects()[0].getAttribute('y')).toBe('0');
  expect(rects()[0].getAttribute('x')).toBe('0');
  expect(rects()[59].getAttribute('fill')).toBe(colorScale(4530)(42));   // 29 February
  expect(rects()[1].getAttribute('fill')).toBe(EMPTY_FILL);             // 2 January: nothing
});

test('one continuous block: no gaps or lines, months labelled at their first Monday', () => {
  draw();
  expect(container.querySelectorAll('path').length).toBe(0);
  const size = Number(rects()[0].getAttribute('width')) + 1.5;
  // 31 January (Wednesday) and 1 February (Thursday) share a column, one row apart.
  const jan31 = rects()[30];
  const feb1 = rects()[31];
  expect(feb1.getAttribute('x')).toBe(jan31.getAttribute('x'));
  expect(Number(feb1.getAttribute('y')) - Number(jan31.getAttribute('y'))).toBeCloseTo(size, 5);
  // 2024 has 53 week-columns.
  expect(weeksOf(yearBlocks([2024], {})[0])).toBe(53);
  // "Feb" sits over 5 February, the month's first Monday: the sixth column.
  const feb = [...container.querySelectorAll('text')].find((t) => t.textContent === 'Feb');
  expect(Number(feb.getAttribute('x'))).toBeCloseTo(5 * size, 5);
});

test('near-white at zero, the app blue at the busiest day, one photo already tinted', () => {
  const color = colorScale(4530);
  expect(color(0)).toBe('rgb(250, 250, 250)');
  expect(ZERO_COLOR).toBe('#fafafa');
  expect(color(4530)).toBe('rgb(42, 120, 214)');            // #2a78d6
  expect(color(1)).not.toBe(color(0));
  const blue = (c) => Number(c.match(/\d+/g)[2]) - Number(c.match(/\d+/g)[0]);
  expect(blue(color(100))).toBeGreaterThan(blue(color(10)));  // more is bluer
});

test('a click on a day with files hands back its row; an empty day does nothing', () => {
  const clicked = [];
  draw({ onDayClick: (row) => clicked.push(row) });
  fire(rects()[59], 'click');
  fire(rects()[1], 'click');
  expect(clicked).toEqual([days[0]]);
});

test('hover says the day and its count', () => {
  draw();
  fire(rects()[59], 'mouseover');          // d3's mouseenter listens for this in jsdom
  fire(rects()[59], 'mouseenter');
  expect(container.textContent).toContain('Thursday, 29 February 2024');
  expect(container.textContent).toContain('42 photos and videos — 40 photos, 2 videos');
});

test('the rolling year is 53 weeks ending today, starting on a Monday', () => {
  const [b] = rollingYearBlock(new Date(2026, 9, 18));
  expect(b.start.getUTCDay()).toBe(1);
  expect(b.end.toISOString().slice(0, 10)).toBe('2026-10-19');
  expect((b.end - b.start) / 86400000).toBeGreaterThan(364);
});
