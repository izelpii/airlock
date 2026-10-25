import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter, Routes, Route, useLocation, useNavigate } from 'react-router-dom';
import Photos from './Photos';

// The Videos page opened the oldest video for every link (Videos.test.js).
// Photos chooses its first photo differently - one function sets the day, the
// list and the selection together - and these pin that it opens what it is
// asked for.

// Newest first, as GET /photos returns them.
const PHOTOS = [
  { id: 30, file_name: 'newest.jpg', master_datetime: '2026-09-25T18:34:15-04:00' },
  { id: 20, file_name: 'utc-late.jpg', master_datetime: '2026-09-20T04:16:13Z' },
  { id: 21, file_name: 'same-day.jpg', master_datetime: '2026-09-20T03:00:00Z' },
  { id: 10, file_name: 'oldest.jpg', master_datetime: '2025-09-05T16:52:43-06:00' },
];

// Catalog 8 numbers its files independently and also has an id 10 - an old
// photo that is NOT its newest.
const PHOTOS_C8 = [
  { id: 99, file_name: 'c8-newest.jpg', master_datetime: '2024-01-02T10:00:00Z' },
  { id: 10, file_name: 'c8-old.jpg', master_datetime: '2020-01-01T10:00:00Z' },
];

// Every getScope request, so a test can say what was FETCHED - the point of
// paging - and not only what ended up on screen.
const mockScopeCalls = [];
// When set, the calendar waits on it: a load still in flight when the user leaves.
const mockHold = { calendar: null };

// Plain functions, not jest.fn: CRA's resetMocks clears jest.fn implementations
// before every test.
jest.mock('../services/api', () => {
  const ok = (data) => () => Promise.resolve({ data });
  const photosFor = (catalogId) => (catalogId === 8 ? PHOTOS_C8 : PHOTOS);
  // Newest day first, as the real endpoint returns it.
  const calendarFor = (photos) => {
    const seen = new Map();
    for (const p of photos) {
      const key = p.master_datetime.slice(0, 10);
      seen.set(key, (seen.get(key) ?? 0) + 1);
    }
    return [...seen.entries()]
      .sort((a, b) => (a[0] < b[0] ? 1 : -1))
      .map(([key, n]) => [...key.split('-').map(Number), n]);
  };
  return {
    __esModule: true,
    default: { get: ok([]), post: ok({}), put: ok({}), delete: ok({}) },
    photoAPI: {
      // The gallery no longer fetches the catalog; it fetches a calendar and
      // then one day. These mocks derive both from the same fixtures, so a day
      // the calendar offers is a day getScope can answer - which is the
      // invariant the real endpoints share through _PHOTO_DAY_SQL.
      getAll: (catalogId) => Promise.resolve({ data: photosFor(catalogId) }),
      calendar: async (catalogId) => {
        if (mockHold.calendar) await mockHold.calendar;
        return { data: { days: calendarFor(photosFor(catalogId)) } };
      },
      // Paged as the real endpoint pages: oldest first by (date, id), and
      // ?around= answers with the page holding that photo and its position.
      getScope: (catalogId, params = {}) => {
        mockScopeCalls.push(params);
        const inScope = photosFor(catalogId).filter((p) => {
          const [y, m, d] = p.master_datetime.slice(0, 10).split('-').map(Number);
          if (params.year != null && Number(params.year) !== y) return false;
          if (params.month != null && Number(params.month) !== m) return false;
          if (params.day != null && Number(params.day) !== d) return false;
          return true;
        });
        if (params.limit == null) return Promise.resolve({ data: inScope });
        const ordered = [...inScope].sort((a, b) => (
          a.master_datetime === b.master_datetime ? a.id - b.id
            : (a.master_datetime < b.master_datetime ? -1 : 1)));
        const limit = Number(params.limit);
        let offset = Number(params.offset ?? 0);
        const body = { total: ordered.length, limit };
        if (params.around != null) {
          const at = ordered.findIndex((p) => p.id === Number(params.around));
          body.position = at < 0 ? null : at;
          if (at >= 0) offset = Math.floor(at / limit) * limit;
        }
        body.offset = offset;
        body.photos = ordered.slice(offset, offset + limit);
        return Promise.resolve({ data: body });
      },
      dayOf: (catalogId, fileId) => {
        const p = photosFor(catalogId).find((x) => x.id === Number(fileId));
        if (!p) return Promise.reject(new Error('not found'));
        const [year, month, day] = p.master_datetime.slice(0, 10)
          .split('-').map(Number);
        return Promise.resolve({ data: { year, month, day } });
      },
      getById: ok(null),
    },
    faceDetectionAPI: new Proxy({}, { get: () => ok([]) }),
  };
});
// One object for the whole run: Photos reloads whenever activeCatalog changes
// identity, as it does in the app only when the user switches catalogs.
jest.mock('../context/CatalogContext', () => {
  const React = require('react');
  const ctx = React.createContext({ activeCatalog: { id: 7, name: 'Test' } });
  return { useCatalog: () => React.useContext(ctx), TestCatalogContext: ctx };
});
jest.mock('../components/Filmstrip', () => ({ Filmstrip: () => null }));

let location;
let nav;
const Probe = () => { location = useLocation(); nav = useNavigate(); return null; };

async function open(path) {
  const container = document.createElement('div');
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/photos" element={<><Photos /><Probe /></>} />
          <Route path="/photos/:catalogId/:fileId" element={<><Photos /><Probe /></>} />
        </Routes>
      </MemoryRouter>,
    );
  });
  for (let i = 0; i < 10; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
  root.unmount();
}

beforeAll(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  // jsdom has no canvas; the face overlay only needs something to call.
  HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => {} });
});

test('a deep link opens the linked photo', async () => {
  await open('/photos/7/10');
  expect(location.pathname).toBe('/photos/7/10');
});

test('a deep link to a photo that is not first in its day opens that photo', async () => {
  await open('/photos/7/21');
  expect(location.pathname).toBe('/photos/7/21');
});

test('with no photo in the link, the newest photo opens', async () => {
  await open('/photos');
  expect(location.pathname).toBe('/photos/7/30');
});

test('switching catalogs after a deep link opens the new catalog\'s newest photo', async () => {
  // eslint-disable-next-line global-require
  const { TestCatalogContext } = require('../context/CatalogContext');
  const catalog7 = { activeCatalog: { id: 7, name: 'Seven' } };
  const catalog8 = { activeCatalog: { id: 8, name: 'Eight' } };
  const container = document.createElement('div');
  const root = createRoot(container);
  const tree = (value) => (
    <TestCatalogContext.Provider value={value}>
      <MemoryRouter initialEntries={['/photos/7/10']}>
        <Routes>
          <Route path="/photos/:catalogId/:fileId" element={<><Photos /><Probe /></>} />
        </Routes>
      </MemoryRouter>
    </TestCatalogContext.Provider>
  );
  const settle = async () => {
    for (let i = 0; i < 10; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
    }
  };
  await act(async () => { root.render(tree(catalog7)); });
  await settle();
  expect(location.pathname).toBe('/photos/7/10');
  await act(async () => { root.render(tree(catalog8)); });
  await settle();
  root.unmount();
  expect(location.pathname).toBe('/photos/8/99');
});

test('"All" keeps the photo on screen and fetches one page, not the catalog', async () => {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  const settle = async () => {
    for (let i = 0; i < 10; i += 1) {
      // eslint-disable-next-line no-await-in-loop
      await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
    }
  };
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/photos/7/21']}>
        <Routes>
          <Route path="/photos/:catalogId/:fileId" element={<><Photos /><Probe /></>} />
        </Routes>
      </MemoryRouter>,
    );
  });
  await settle();
  mockScopeCalls.length = 0;

  const all = [...container.querySelectorAll('button')]
    .find((b) => b.textContent.trim() === 'All');
  expect(all).toBeTruthy();
  await act(async () => { all.click(); });
  await settle();

  expect(location.pathname).toBe('/photos/7/21');
  // One request, paged, asking for the page around the photo on screen - and
  // no year, because "All" is the whole catalog.
  expect(mockScopeCalls.length).toBeGreaterThan(0);
  expect(mockScopeCalls[0]).toMatchObject({ around: 21, limit: expect.any(Number) });
  expect(mockScopeCalls[0].year).toBeUndefined();
  expect(mockScopeCalls.every((c) => c.limit != null)).toBe(true);
  root.unmount();
  container.remove();
});

test('leaving Photos before it has loaded does not pull you back to it', async () => {
  let release;
  mockHold.calendar = new Promise((r) => { release = r; });
  const container = document.createElement('div');
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/photos']}>
        <Routes>
          <Route path="/photos" element={<><Photos /><Probe /></>} />
          <Route path="/photos/:catalogId/:fileId" element={<><Photos /><Probe /></>} />
          <Route path="/identify-pets" element={<Probe />} />
        </Routes>
      </MemoryRouter>,
    );
  });
  await act(async () => { nav('/identify-pets'); });
  mockHold.calendar = null;
  await act(async () => { release(); });
  for (let i = 0; i < 10; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
  expect(location.pathname).toBe('/identify-pets');
  root.unmount();
});
