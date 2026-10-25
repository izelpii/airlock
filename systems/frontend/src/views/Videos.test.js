import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import Videos from './Videos';

// A deep link must open the video it names. It used to open the OLDEST video in
// the catalog instead: before a day is picked the "day in view" is every video,
// oldest first, and the day-sync effect - running in the same commit as the
// deep-link effect, while `selected` was still null - replaced the linked video
// with the first of those.

// Newest first, as GET /videos returns them.
const VIDEOS = [
  { id: 17537, file_name: 'newest.mov', master_datetime: '2026-09-25T18:34:15-04:00' },
  { id: 17270, file_name: 'awards.mov', master_datetime: '2026-09-20T04:16:13Z' },
  { id: 7855, file_name: 'middle.mp4', master_datetime: '2025-10-30T16:21:07-06:00' },
  { id: 3337, file_name: 'oldest.mp4', master_datetime: '2025-09-05T16:52:43-06:00' },
];

// Plain functions, not jest.fn: CRA's resetMocks clears jest.fn implementations
// before every test.
jest.mock('../services/api', () => ({
  videoAPI: { getAll: () => Promise.resolve({ data: VIDEOS }) },
  photoAPI: { getById: () => Promise.resolve({ data: null }) },
}));
jest.mock('../context/CatalogContext', () => ({
  useCatalog: () => ({ activeCatalog: { id: 7, name: 'Test' } }),
}));
// Layout only; jsdom has no scrollIntoView for it to call.
jest.mock('../components/Filmstrip', () => ({ Filmstrip: () => null }));

let location;
const Probe = () => { location = useLocation(); return null; };

async function open(path) {
  const container = document.createElement('div');
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/videos" element={<><Videos /><Probe /></>} />
          <Route path="/videos/:catalogId/:fileId" element={<><Videos /><Probe /></>} />
        </Routes>
      </MemoryRouter>,
    );
  });
  // Let the list resolve and every effect it triggers settle.
  for (let i = 0; i < 10; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
  const text = container.textContent;
  root.unmount();
  return text;
}

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });

test('a deep link opens the linked video, not the oldest one', async () => {
  await open('/videos/7/17270');
  expect(location.pathname).toBe('/videos/7/17270');
});

test('with no video in the link, the newest video opens', async () => {
  await open('/videos');
  expect(location.pathname).toBe('/videos/7/17537');
});
