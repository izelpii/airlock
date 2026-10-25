import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import EventDateEditor, { buildChanges } from './EventDateEditor';

// The event date editor (mass-edit-event-photo-dates-spec.md section 4):
// grouping by day, Apply staging and moving photos between cards, Undo,
// Discard, the leave guard and Confirm.

const mockEvent = { id: 12, name: 'Event 2026-10-02 23:50 (#12)', start_time: '2026-10-02 23:50:00' };
// Three photos of one evening, dated by the file system.
const mockFiles = [1, 2, 3].map((id) => ({
  id,
  file_name: `${id}.jpg`,
  master_datetime: `2026-10-02T23:5${id}:00-04:00`,
  master_datetime_source: 'filesystem',
  filename_datetime: '2003-01-01T12:00:00',
  filename_has_time: false,
  alternative_file_path: null,
  is_video: false,
}));

const mockPosts = [];
let mockPostResult;

// Plain functions, not jest.fn: CRA's resetMocks clears jest.fn implementations.
jest.mock('../services/api', () => ({
  eventAPI: {
    getAll: () => Promise.resolve({ data: [mockEvent] }),
    getFiles: () => Promise.resolve({ data: mockFiles }),
  },
  photoAPI: {
    updateDates: (...args) => {
      mockPosts.push(args);
      return mockPostResult();
    },
  },
}));

let location;
const Probe = () => { location = useLocation(); return null; };

let container;
let root;
let confirms;
let confirmAnswer;

async function settle() {
  for (let i = 0; i < 5; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
}

async function open() {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/events/7/12/dates']}>
        <Routes>
          <Route path="/events/:catalogId/:eventId/dates" element={<><EventDateEditor /><Probe /></>} />
          <Route path="/events/:catalogId/:eventId" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
  });
  await settle();
}

const cards = () => [...document.querySelectorAll('[data-testid="date-group"]')];
const card = (day) => document.querySelector(`[data-testid="date-group"][data-day="${day}"]`);
const tilesOf = (el) => [...el.querySelectorAll('[data-marquee-id]')];
const byTestId = (id, scope = document) => scope.querySelector(`[data-testid="${id}"]`);

function type(input, value) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

async function applyTo(el, date, time) {
  await act(async () => { type(byTestId('apply-date', el), date); });
  if (time) await act(async () => { type(byTestId('apply-time', el), time); });
  await act(async () => { byTestId('apply', el).click(); });
}

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
beforeEach(() => {
  mockPosts.length = 0;
  confirms = [];
  confirmAnswer = true;
  window.confirm = (msg) => { confirms.push(msg); return confirmAnswer; };
  mockPostResult = () => Promise.resolve({ status: 202, data: { batch_id: 17, file_count: 2 } });
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('an event whose photos share one day opens as one group, every photo checked', async () => {
  await open();
  expect(cards()).toHaveLength(1);
  expect(card('2026-10-02')).not.toBeNull();
  expect(tilesOf(card('2026-10-02')).every((t) => t.getAttribute('data-checked') === 'true')).toBe(true);
  expect(byTestId('confirm').textContent).toBe('Confirm (0)');
});

test('Apply moves the checked photos into the new date\'s card; the unchecked stay', async () => {
  await open();
  await act(async () => { tilesOf(card('2026-10-02'))[2].click(); }); // uncheck photo 3
  await applyTo(card('2026-10-02'), '2003-01-01');

  expect(cards().map((c) => c.getAttribute('data-day'))).toEqual(['2003-01-01', '2026-10-02']);
  const moved = card('2003-01-01');
  expect(tilesOf(moved).map((t) => t.getAttribute('data-marquee-id'))).toEqual(['1', '2']);
  expect(moved.querySelectorAll('[data-testid="staged-badge"]')).toHaveLength(2);
  // An empty time is noon.
  expect(moved.textContent).toContain('12:00 PM');
  expect(byTestId('changed-from', moved).textContent).toBe('2 changed from Oct 2, 2026 · Undo');
  expect(tilesOf(card('2026-10-02')).map((t) => t.getAttribute('data-marquee-id'))).toEqual(['3']);
  expect(byTestId('confirm').textContent).toBe('Confirm (2)');
});

test('a card emptied by Apply disappears, and joining an existing date reuses its card', async () => {
  await open();
  await act(async () => { tilesOf(card('2026-10-02'))[2].click(); });
  await applyTo(card('2026-10-02'), '2003-01-01');
  await applyTo(card('2026-10-02'), '2003-01-01', '18:00');
  expect(cards().map((c) => c.getAttribute('data-day'))).toEqual(['2003-01-01']);
  expect(tilesOf(card('2003-01-01'))).toHaveLength(3);
  expect(byTestId('confirm').textContent).toBe('Confirm (3)');
});

test('Undo returns the photos to their original date\'s card', async () => {
  await open();
  await applyTo(card('2026-10-02'), '2003-01-01');
  expect(cards()).toHaveLength(1);
  const undo = [...card('2003-01-01').querySelectorAll('button')].find((b) => b.textContent === 'Undo');
  await act(async () => { undo.click(); });
  expect(cards().map((c) => c.getAttribute('data-day'))).toEqual(['2026-10-02']);
  expect(byTestId('confirm').textContent).toBe('Confirm (0)');
});

test('Discard clears every staged change after confirming', async () => {
  await open();
  await applyTo(card('2026-10-02'), '2003-01-01');
  confirmAnswer = false;
  await act(async () => { byTestId('discard').click(); });
  expect(byTestId('confirm').textContent).toBe('Confirm (3)');
  confirmAnswer = true;
  await act(async () => { byTestId('discard').click(); });
  expect(byTestId('confirm').textContent).toBe('Confirm (0)');
  expect(card('2026-10-02')).not.toBeNull();
});

test('leaving with staged changes asks first', async () => {
  await open();
  await applyTo(card('2026-10-02'), '2003-01-01');

  const unload = new Event('beforeunload', { cancelable: true });
  window.dispatchEvent(unload);
  expect(unload.defaultPrevented).toBe(true);

  confirmAnswer = false;
  await act(async () => { byTestId('back-to-event').click(); });
  expect(confirms).toHaveLength(1);
  expect(location.pathname).toBe('/events/7/12/dates');

  confirmAnswer = true;
  await act(async () => { byTestId('back-to-event').click(); });
  expect(location.pathname).toBe('/events/7/12');
});

test('leaving with nothing staged does not ask', async () => {
  await open();
  await act(async () => { byTestId('back-to-event').click(); });
  expect(confirms).toEqual([]);
  expect(location.pathname).toBe('/events/7/12');
});

test('Confirm summarises, posts the batch, and redirects with the batch id without a warning', async () => {
  await open();
  await act(async () => { tilesOf(card('2026-10-02'))[2].click(); });
  await applyTo(card('2026-10-02'), '2003-01-01');
  await applyTo(card('2026-10-02'), '2003-01-01', '18:00');
  await act(async () => { byTestId('confirm').click(); });
  await settle();
  const summary = byTestId('confirm-summary').textContent;
  expect(summary).toContain('Change the date of 3 photos.');
  expect(summary).toContain('2 to Jan 1, 2003 12:00 PM, 1 to Jan 1, 2003 6:00 PM');

  await act(async () => { byTestId('confirm-send').click(); });
  await settle();

  expect(mockPosts).toHaveLength(1);
  const [catalogId, eventId, changes, rebuild] = mockPosts[0];
  expect([catalogId, eventId, rebuild]).toEqual(['7', '12', true]);
  expect(changes.map(({ file_ids: ids, datetime }) => [ids, datetime])).toEqual([
    [[1, 2], '2003-01-01T12:00'],
    [[3], '2003-01-01T18:00'],
  ]);
  expect(confirms).toEqual([]);
  expect(location.pathname).toBe('/events/7/12');
  expect(location.state).toEqual({ dateBatchId: 17 });
});

test('a busy catalog keeps the staged changes and the editor open', async () => {
  mockPostResult = () => Promise.reject({
    response: { status: 409, data: { error: 'catalog_busy', message: 'An import is running.' } },
  });
  await open();
  await applyTo(card('2026-10-02'), '2003-01-01');
  await act(async () => { byTestId('confirm').click(); });
  await settle();
  await act(async () => { byTestId('confirm-send').click(); });
  await settle();
  expect(location.pathname).toBe('/events/7/12/dates');
  expect(document.body.textContent).toContain('An import is running.');
  expect(byTestId('confirm').textContent).toBe('Confirm (3)');
});

test('buildChanges groups the staged map by target time, with the page timezone', () => {
  expect(buildChanges({ 5: '2003-01-01T12:00', 4: '2003-01-01T12:00', 6: '2002-12-31T09:00' }, 'Europe/Paris'))
    .toEqual([
      { file_ids: [6], datetime: '2002-12-31T09:00', timezone: 'Europe/Paris' },
      { file_ids: [4, 5], datetime: '2003-01-01T12:00', timezone: 'Europe/Paris' },
    ]);
});
