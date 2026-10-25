// A zone far from UTC, so a parse through `new Date()` would visibly shift the
// times. Set before anything reads the clock.
process.env.TZ = 'America/Los_Angeles';

/* eslint-disable import/first */
import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import EventDateCard, { describeEventDates } from './EventDateCard';
/* eslint-enable import/first */

// The Date card on the event page (mass-edit-event-photo-dates-spec.md
// section 3.2). Event times are wall-clock strings, shown as stored.

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });

test('a one-day event shows its day, full weekday, month and time range', () => {
  expect(describeEventDates('2003-01-01 06:00:00', '2003-01-01 21:14:00')).toEqual({
    dayNumber: '01', weekday: 'WEDNESDAY', monthLine: 'January 2003', timeLine: '6:00 AM – 9:14 PM',
  });
});

test('a single-photo event shows one time', () => {
  expect(describeEventDates('2003-01-01 18:00', '2003-01-01 18:00').timeLine).toBe('6:00 PM');
  expect(describeEventDates('2003-01-01 18:00', null).timeLine).toBe('6:00 PM');
});

test('a multi-day event shows ranges, and both months when they differ', () => {
  expect(describeEventDates('2003-01-01 10:00:00', '2003-01-03 12:00:00')).toMatchObject({
    dayNumber: '01–03', weekday: 'WED – FRI', monthLine: 'January 2003',
  });
  expect(describeEventDates('2003-01-30 10:00:00', '2003-02-02 12:00:00').monthLine)
    .toBe('Jan 30 – Feb 2, 2003');
  expect(describeEventDates('2002-12-30T10:00:00', '2003-01-02T12:00:00').monthLine)
    .toBe('Dec 30, 2002 – Jan 2, 2003');
});

test('a trailing Z or offset is ignored, not converted to the browser zone', () => {
  // Through `new Date()` in Los Angeles, 06:00Z would read as 10:00 PM on
  // Dec 31, a Tuesday.
  expect(describeEventDates('2003-01-01T06:00:00Z', '2003-01-01T21:14:00+09:00')).toEqual({
    dayNumber: '01', weekday: 'WEDNESDAY', monthLine: 'January 2003', timeLine: '6:00 AM – 9:14 PM',
  });
});

test('the card renders the literal time and its button opens the editor', async () => {
  const container = document.createElement('div');
  const root = createRoot(container);
  let opened = 0;
  await act(async () => {
    root.render(
      <EventDateCard
        event={{ start_time: '2003-01-01T06:00:00Z', end_time: '2003-01-01T06:00:00Z' }}
        onModify={() => { opened += 1; }}
      />,
    );
  });
  expect(container.querySelector('[data-testid="event-date-day"]').textContent).toBe('01');
  expect(container.querySelector('[data-testid="event-date-weekday"]').textContent).toBe('WEDNESDAY');
  expect(container.querySelector('[data-testid="event-date-time"]').textContent).toBe('6:00 AM');
  const button = [...container.querySelectorAll('button')].find((b) => b.textContent.includes('Modify dates'));
  await act(async () => { button.click(); });
  expect(opened).toBe(1);
  act(() => root.unmount());
});
