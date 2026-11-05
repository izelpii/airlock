import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import SavedSearch from './SavedSearch';

// A saved search's page with its tool panel (saved-search-tools-spec.md
// sections 3 to 5): the panel beside the grid, removed files left out of every
// selection, Select all across pages, and a date batch followed to its end.

global.IS_REACT_ACT_ENVIRONMENT = true;

const mockState = {};

const file = (id, extra = {}) => ({
  file_id: id, file_name: `${id}.jpg`, file_type: '.jpg', taken: '2019-03-03T09:41:07-05:00',
  removed_at: null, thumbnail_url: `/t/${id}`, ...extra,
});

jest.mock('../services/api', () => ({
  savedSearchAPI: {
    get: () => Promise.resolve({ data: mockState.search }),
    files: (c, id, params) => Promise.resolve({
      data: { files: mockState.pages[params.offset] || [], total: mockState.total },
    }),
    fileIds: () => Promise.resolve({ data: mockState.allIds }),
    removeFiles: () => Promise.resolve({ data: { removed: 0 } }),
    update: () => Promise.resolve({}),
    remove: () => Promise.resolve({}),
  },
  photoAPI: {
    latestDateBatchForSavedSearch: () => (mockState.latest
      ? Promise.resolve({ data: mockState.latest })
      : Promise.reject({ response: { status: 404 } })),
    getDateBatch: () => Promise.resolve({ data: mockState.batch }),
    updateDates: () => Promise.resolve({ data: { batch_id: 9, file_count: 2 } }),
  },
}));
jest.mock('../services/socket', () => ({
  __esModule: true,
  default: { connect: () => {}, onDateBatchProgress: () => () => {} },
}));
jest.mock('../context/CatalogContext', () => ({
  useCatalog: () => ({ activeCatalog: mockState.catalog }),
}));
jest.mock('./SavedSearches', () => ({ describeQuery: (q) => q }));

let container;
let root;

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
      <MemoryRouter initialEntries={['/saved-searches/3']}>
        <Routes><Route path="/saved-searches/:id" element={<SavedSearch />} /></Routes>
      </MemoryRouter>,
    );
  });
  await settle();
}

const buttonNamed = (re) => [...container.querySelectorAll('button')].find((b) => re.test(b.textContent.trim()));
const checkboxes = () => [...container.querySelectorAll('[data-testid="saved-search-grid"] input[type="checkbox"]')];

beforeEach(() => {
  Object.keys(mockState).forEach((k) => delete mockState[k]);
  mockState.catalog = { id: 7, name: 'Test', role: 'editor', ingest_mode: 'copy' };
  mockState.search = {
    id: 3, name: 'To fix', query: 'q=x', status: 'open', file_count: 63, removed_count: 1, note: null,
  };
  const first = Array.from({ length: 60 }, (_, i) => file(i + 1));
  first[1] = file(2, { removed_at: '2026-10-01' });
  mockState.pages = { 0: first, 60: [file(61), file(62), file(63, { taken: '2021-07-09T10:00:00-04:00' })] };
  mockState.total = 63;
  mockState.allIds = {
    files: [...first.filter((f) => !f.removed_at), ...mockState.pages[60]]
      .map((f) => ({ file_id: f.file_id, taken: f.taken, file_type: f.file_type })),
    total: 62, truncated: false,
  };
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
  document.body.innerHTML = '';
});

it('shows the tool panel beside the grid', async () => {
  await open();
  expect(container.querySelector('[data-testid="selection-card"]').textContent)
    .toContain('Select photos and videos in the grid');
  expect(container.querySelector('[data-testid="change-date-tool"]')).not.toBeNull();
});

it('leaves removed files out of every selection', async () => {
  await open();
  expect(checkboxes()).toHaveLength(59);                  // file 2 has none
  await act(async () => { buttonNamed(/^Select page$/).click(); });
  expect(container.querySelector('[data-testid="selected-count"]').textContent).toContain('59 selected');
});

it('selects all across pages, and says how many are on other pages', async () => {
  await open();
  await act(async () => { buttonNamed(/^Select all 62$/).click(); });
  await settle();
  expect(container.querySelector('[data-testid="selected-count"]').textContent)
    .toBe('62 selected (3 on other pages)');
  expect(container.querySelector('[data-testid="selection-card"]').textContent)
    .toContain('Mar 3, 2019 – Jul 9, 2021');               // file 63's date, from another page
  await act(async () => { buttonNamed(/^Clear$/).click(); });
  expect(container.querySelector('[data-testid="selected-count"]')).toBeNull();
});

it('keeps a selection across pages', async () => {
  await open();
  await act(async () => { checkboxes()[0].click(); });
  await act(async () => { buttonNamed(/^→$/).click(); });
  await settle();
  expect(container.querySelector('[data-testid="selected-count"]').textContent)
    .toBe('1 selected (1 on other pages)');
});

it('follows a running batch from the page opening to its result, then reloads', async () => {
  mockState.latest = { id: 9, status: 'running', done: 1, total: 2 };
  mockState.batch = { id: 9, status: 'running', done: 1, total: 2 };
  await open();
  const banner = () => container.querySelector('[data-testid="date-batch-banner"]').textContent;
  expect(banner()).toContain('Changing the dates of 2 photos and videos (1/2)');
  expect(buttonNamed(/^Change date/).disabled).toBe(true);

  mockState.batch = {
    id: 9, status: 'completed', done: 2, total: 2,
    result: {
      updated: [{ file_id: 1 }],
      failed: [{ file_id: 5, error: 'file_move_conflict', message: 'exists' }],
      rebuild_status: 'scheduled',
      rebuild_at: '2026-11-06T03:00:00-05:00',
    },
  };
  await act(async () => { await new Promise((r) => setTimeout(r, 2100)); });
  await settle();
  expect(banner()).toContain('Done. 1 date changed.');
  expect(banner()).toContain('1 file could not be changed');
  expect(banner()).toContain('Events will be rebuilt tonight');
});

it('shows nothing for a batch that finished long ago', async () => {
  mockState.latest = { id: 4, status: 'completed', finished_at: '2026-01-01 10:00:00' };
  await open();
  expect(container.querySelector('[data-testid="date-batch-banner"]')).toBeNull();
});
