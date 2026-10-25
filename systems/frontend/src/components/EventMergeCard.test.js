import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import EventMergeCard from './EventMergeCard';

// The card lists what the backend offers, merges into the chosen event only
// after confirmation, and hands the result back so the page can follow it.

const calls = [];
const CANDIDATES = [
  { id: 11, name: 'Evening', start_time: '2026-09-20 20:00:00', relation: 'same day', photos: 12 },
  { id: 12, name: 'Awards', start_time: '2026-09-21 09:00:00', relation: 'day after', photos: 1 },
];

// Plain functions, not jest.fn: CRA's resetMocks clears jest.fn implementations.
jest.mock('../services/api', () => ({
  eventAPI: {
    getMergeCandidates: (catalogId, eventId) => {
      calls.push(['candidates', catalogId, eventId]);
      return Promise.resolve({ data: eventId === 99 ? [] : CANDIDATES });
    },
    merge: (catalogId, eventId, into) => {
      calls.push(['merge', catalogId, eventId, into]);
      return Promise.resolve({ data: { merged_event_id: eventId, into_event_id: into } });
    },
  },
}));

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
beforeEach(() => { calls.length = 0; });

async function render(props) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => { root.render(<EventMergeCard catalogId="7" {...props} />); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  return { container, root };
}

function choose(container, value) {
  const select = container.querySelector('select');
  // React tracks the value through the native setter.
  Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, 'value').set.call(select, value);
  select.dispatchEvent(new Event('change', { bubbles: true }));
}

const mergeButton = (container) =>
  [...container.querySelectorAll('button')].find((b) => b.textContent.includes('Merge'));

test('lists the nearby events with when they are and how many photos', async () => {
  const { container, root } = await render({ event: { id: 5, name: 'Afternoon' } });
  const options = [...container.querySelectorAll('option')].map((o) => o.textContent);
  expect(options[1]).toMatch(/^Same day · .* · Evening \(12 photos\)$/);
  expect(options[2]).toMatch(/^Day after · .* · Awards \(1 photo\)$/);
  expect(mergeButton(container).disabled).toBe(true);
  act(() => root.unmount());
});

test('merges into the chosen event only after confirming, then reports it', async () => {
  const merged = [];
  const { container, root } = await render({
    event: { id: 5, name: 'Afternoon' }, onMerged: (r) => merged.push(r),
  });
  await act(async () => { choose(container, '12'); });

  window.confirm = () => false;
  await act(async () => { mergeButton(container).click(); });
  expect(calls.filter((c) => c[0] === 'merge')).toHaveLength(0);

  window.confirm = () => true;
  await act(async () => { mergeButton(container).click(); });
  await act(async () => { await new Promise((r) => setTimeout(r, 0)); });
  expect(calls.filter((c) => c[0] === 'merge')).toEqual([['merge', '7', 5, 12]]);
  expect(merged).toEqual([{ merged_event_id: 5, into_event_id: 12 }]);
  act(() => root.unmount());
});

test('says so when there is nothing nearby to merge with', async () => {
  const { container, root } = await render({ event: { id: 99, name: 'Lonely' } });
  expect(container.textContent).toContain('No other event on the same day, the day before or the day after.');
  expect(container.querySelector('select')).toBeNull();
  act(() => root.unmount());
});
