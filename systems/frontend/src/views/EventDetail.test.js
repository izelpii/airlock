import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import EventDetail from './EventDetail';

// The event page after a date batch (mass-edit-event-photo-dates-spec.md
// section 4.5): the banner follows the batch and then the rebuild, and when
// Create Events has replaced the event the page lands on its successor. A
// vanished event id opened later finds its latest batch and lands too.

// State the mocks read, rewritten per test. `mock` prefix: jest.mock
// factories may only reach variables named that way.
const mockState = {
  events: [],
  batch: null,
  latest: null,
  located: {},
  socket: null,
};

jest.mock('../services/api', () => {
  const ok = (data) => Promise.resolve({ data });
  return {
    eventAPI: {
      getAll: () => ok(mockState.events),
      getFiles: () => ok([]),
      getLocations: () => ok([]),
      getMergeCandidates: () => ok([]),
      locate: () => ok(mockState.located),
    },
    locationAPI: { getAll: () => ok([]) },
    userSettingsAPI: { get: () => ok({}) },
    photoAPI: {
      getDateBatch: () => ok(mockState.batch),
      latestDateBatch: () => (mockState.latest
        ? ok(mockState.latest)
        : Promise.reject({ response: { status: 404 } })),
    },
  };
});
jest.mock('../services/socket', () => ({
  __esModule: true,
  default: {
    connect: () => {},
    onDateBatchProgress: (cb) => { mockState.socket = cb; return () => { mockState.socket = null; }; },
  },
}));
// react-leaflet ships ES modules only, which CRA's jest does not transform.
// The map is never opened here.
jest.mock('react-leaflet', () => ({
  MapContainer: () => null, TileLayer: () => null, Marker: () => null, useMapEvents: () => null,
}));

let location;
const Probe = () => { location = useLocation(); return null; };

let container;
let root;

async function settle(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
}

async function open(entry) {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[entry]}>
        <Routes>
          <Route path="/events" element={<Probe />} />
          <Route path="/events/:catalogId/:eventId" element={<><EventDetail /><Probe /></>} />
        </Routes>
      </MemoryRouter>,
    );
  });
  await settle();
}

const banner = () => container.querySelector('[data-testid="date-batch-banner"]');
const ev = (id, name) => ({ id, name, start_time: '2003-01-01 12:00:00', end_time: '2003-01-01 12:00:00', type: 'Auto' });
const batchRow = (over) => ({
  id: 17, event_id: 12, status: 'running', total: 3, done: 1, file_ids: [1, 2, 3], result: null, ...over,
});

beforeAll(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  global.ResizeObserver = class { observe() {} disconnect() {} };
});
beforeEach(() => {
  mockState.events = [ev(12, 'Event 2026-10-02 23:50 (#12)')];
  mockState.batch = batchRow();
  mockState.latest = null;
  mockState.located = {};
  mockState.socket = null;
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('Details, Date, Add Location and Merge share one row in equal columns', async () => {
  await open('/events/7/12');
  const rows = container.querySelectorAll('.row.mb-4');
  expect(rows).toHaveLength(1);
  const cols = [...rows[0].children];
  // Every card is an auto column (`col-md`, no number): Bootstrap splits the
  // row evenly. The event has no location, so Add Location is open.
  expect(cols.map((c) => c.className.split(' ').filter((k) => k.startsWith('col-')))).toEqual(
    [['col-md'], ['col-md'], ['col-md'], ['col-md']]);
  expect(cols[1].textContent).toContain('Modify dates');
  expect(cols[3].textContent).toContain('Merge event');
});

test('the banner follows the batch, then the rebuild, then lands on the successor event', async () => {
  await open({ pathname: '/events/7/12', state: { dateBatchId: 17 } });
  expect(banner().textContent).toContain('Updating the dates of 3 photos (1/3)');

  await act(async () => { mockState.socket({ catalog_id: 7, batch_id: 17, done: 3, total: 3, status: 'rebuilding' }); });
  expect(banner().textContent).toContain('Rebuilding events');

  // Create Events replaced event 12: its photos are now in 30 (two) and 31.
  mockState.events = [ev(30, 'Event 2003-01-01 12:00 (#30)'), ev(31, 'Other')];
  mockState.located = { 1: [ev(30, 'Event 2003-01-01 12:00 (#30)')], 2: [ev(30, 'x')], 3: [ev(31, 'Other')] };
  mockState.batch = batchRow({
    status: 'completed', done: 3,
    result: { updated: [], failed: [], detached_from_events: [12], rebuild_status: 'completed' },
  });
  await act(async () => { mockState.socket({ catalog_id: 7, batch_id: 17, done: 3, total: 3, status: 'completed' }); });
  await settle();

  expect(location.pathname).toBe('/events/7/30');
  expect(banner().textContent).toContain('Done. 2 photos moved to Event 2003-01-01 12:00 (#30).');
  expect(banner().querySelector('a').getAttribute('href')).toBe('/events/7/30');
});

test('a batch with failures keeps a warning with the details', async () => {
  mockState.located = { 1: [ev(12, 'Event 2026-10-02 23:50 (#12)')] };
  mockState.batch = batchRow({
    status: 'completed', done: 3,
    result: {
      updated: [], detached_from_events: [], rebuild_status: 'completed',
      failed: [{ file_id: 3, error: 'file_move_conflict', message: 'A file of that name exists.' }],
    },
  });
  await open({ pathname: '/events/7/12', state: { dateBatchId: 17 } });
  await settle();
  expect(location.pathname).toBe('/events/7/12');
  expect(banner().className).toContain('alert-warning');
  expect(banner().textContent).toContain('1 photo could not be changed.');
  const details = [...banner().querySelectorAll('button')].find((b) => b.textContent === 'Details');
  await act(async () => { details.click(); });
  expect(banner().textContent).toContain('#3: file_move_conflict — A file of that name exists.');
});

test('opening a vanished event later follows its latest batch to the successor', async () => {
  mockState.events = [ev(30, 'Event 2003-01-01 12:00 (#30)')];
  mockState.latest = batchRow({ status: 'completed', done: 3, result: { updated: [], failed: [] } });
  mockState.located = { 1: [ev(30, 'Event 2003-01-01 12:00 (#30)')] };
  await open('/events/7/12');
  await settle();
  expect(location.pathname).toBe('/events/7/30');
  expect(banner().textContent).toContain('Done. 1 photo moved to');
});

test('a vanished event with no batch is still "not found"', async () => {
  mockState.events = [];
  await open('/events/7/12');
  expect(container.textContent).toContain('Event not found');
});

test('when no event holds the photos, the page goes to the Events list', async () => {
  mockState.events = [];
  mockState.latest = batchRow({ status: 'completed', done: 3, result: { updated: [], failed: [] } });
  await open('/events/7/12');
  await settle();
  expect(location.pathname).toBe('/events');
});
