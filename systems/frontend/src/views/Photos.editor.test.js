import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import Photos from './Photos';

// The Basic Editor on the photo page (basic-photo-editing.md sections 3 and 9).

const PHOTOS = [{ id: 30, file_name: 'p.jpg', master_datetime: '2026-09-25T18:34:15Z',
  alternative_file_path: 'cache/Images/2026/09/25/p_alt.jpg' }];
const mockCalls = { save: [], get: 0 };
const mockDetail = { current: null };

jest.mock('../services/api', () => {
  const ok = (data) => () => Promise.resolve({ data });
  return {
    __esModule: true,
    default: { get: ok([]), post: ok({}), put: ok({}), delete: ok({}) },
    photoAPI: {
      calendar: ok({ days: [[2026, 9, 25, 1]] }),
      getScope: (catalogId, params = {}) => Promise.resolve({
        data: params.limit == null ? PHOTOS
          : { total: 1, limit: Number(params.limit), offset: 0, position: 0, photos: PHOTOS },
      }),
      dayOf: ok({ year: 2026, month: 9, day: 25 }),
      getById: () => Promise.resolve({ data: mockDetail.current }),
    },
    faceDetectionAPI: new Proxy({}, { get: () => ok([]) }),
    petAPI: new Proxy({}, { get: () => ok({ pets: [] }) }),
    peopleCategoryAPI: new Proxy({}, { get: () => ok([]) }),
    photoEditAPI: {
      save: (catalogId, fileId, orientation, base) => {
        mockCalls.save.push([catalogId, fileId, orientation, base]);
        return Promise.resolve({ data: { file_id: fileId, orientation, version: base + 1,
          render_status: 'rendering', published_albums: [{ id: 1, name: 'Summer' }] } });
      },
      reset: ok({}),
      get: () => {
        mockCalls.get += 1;
        return Promise.resolve({ data: { orientation: 6, version: 1, render_status: 'ready' } });
      },
      versions: ok({ versions: {} }),
    },
  };
});
// One object for the whole run: Photos reloads whenever activeCatalog changes
// identity (Photos.test.js).
jest.mock('../context/CatalogContext', () => {
  const value = { activeCatalog: { id: 7, name: 'Test' } };
  return { useCatalog: () => value };
});
jest.mock('../components/Filmstrip', () => ({ Filmstrip: () => null }));

beforeAll(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  HTMLCanvasElement.prototype.getContext = () => new Proxy({}, { get: () => () => {} });
});

const settle = async (ms = 10, times = 10) => {
  for (let i = 0; i < times; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, ms)); });
  }
};

async function mount() {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/photos/7/30']}>
        <Routes><Route path="/photos/:catalogId/:fileId" element={<Photos />} /></Routes>
      </MemoryRouter>,
    );
  });
  await settle();
  const img = container.querySelector('img[alt="p.jpg"]');
  await act(async () => { img.dispatchEvent(new Event('load')); });
  await settle();
  const button = (text) => [...container.querySelectorAll('button')]
    .find((b) => b.textContent.trim() === text || b.textContent.includes(text));
  return { container, root, button, img };
}

beforeEach(() => {
  mockCalls.save = [];
  mockCalls.get = 0;
  mockDetail.current = { id: 30, file_name: 'p.jpg', edit: { orientation: 1, version: 0 } };
});

test('Open shows only the tools, previews a turn, and Save waits for the rebuild', async () => {
  const { container, root, button, img } = await mount();
  expect(container.textContent).toContain('Basic Editor');
  expect(container.textContent).toContain('Detected Faces');

  await act(async () => { button('Open').click(); });
  expect(container.textContent).not.toContain('Detected Faces');
  expect(container.querySelector('canvas').style.display).toBe('none');

  await act(async () => { button('Rotate right').click(); });
  expect(img.style.transform).toContain('rotate(90deg)');
  expect(container.textContent).toContain('Rotated 90° right');

  await act(async () => { button('Save').click(); });
  await settle(100, 15);
  expect(mockCalls.save).toEqual([[7, 30, 6, 0]]);
  expect(mockCalls.get).toBeGreaterThan(0);
  expect(container.textContent).toContain('The original file is unchanged');
  expect(container.textContent).toContain('re-publish “Summer”');
  expect(container.textContent).toContain('Detected Faces');
  expect(img.getAttribute('src')).toMatch(/&v=1$/);
  act(() => root.unmount());
});

test('Cancel discards the turn', async () => {
  const { container, root, button, img } = await mount();
  await act(async () => { button('Open').click(); });
  await act(async () => { button('Rotate left').click(); });
  await act(async () => { button('Cancel').click(); });
  expect(img.style.transform || 'none').toBe('none');
  expect(mockCalls.save).toEqual([]);
  expect(container.textContent).toContain('Detected Faces');
  act(() => root.unmount());
});

test('a removed photo has no editor', async () => {
  mockDetail.current = { id: 30, file_name: 'p.jpg', removed_at: '2026-10-01', edit: null };
  const { container, root } = await mount();
  expect(container.textContent).not.toContain('Basic Editor');
  act(() => root.unmount());
});
