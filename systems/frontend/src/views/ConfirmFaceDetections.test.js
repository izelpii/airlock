import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import ConfirmFaceDetections from './ConfirmFaceDetections';

// Identify people counts and pages its candidates on the server. It used to
// download every unidentified face in the catalog - 469,839 faces and 188 MB
// on `family` - to show one person's matches a page at a time. These pin that
// it never asks for anything but counts and pages.

const mockCalls = [];
// Ana has 150 candidates on the server; each page is cut from that list.
const mockAna = Array.from({ length: 150 }, (_, i) => ({
  face_id: 1000 + i, file_id: 1, x: 0, y: 0, w: 10, h: 10,
  image_width: 100, image_height: 100, file_path: 'Images/a.jpg',
  file_name: 'a.jpg', master_datetime: '2021-05-05T10:00:00', ai_crop_path: null,
  best_match: { person_id: 1, person_name: 'Ana', distance: 0.2, metric: 'cosine',
                metric_distance: 0.2 },
}));
const mockDone = new Set();

jest.mock('../services/api', () => {
  const ok = (data) => Promise.resolve({ data });
  return {
    __esModule: true,
    default: {
      get: (url) => {
        mockCalls.push({ url });
        return ok(url.endsWith('/people')
          ? [{ id: 1, name: 'Ana' }, { id: 2, name: 'Ben' }] : []);
      },
      post: () => ok({}),
    },
    faceRecognitionAPI: {
      getUnidentifiedSummary: (catalogId, params) => {
        mockCalls.push({ url: 'summary', params });
        const left = mockAna.filter((f) => !mockDone.has(f.face_id)).length;
        return ok({ people: { 1: { candidates: left, matched: left } },
                    has_scores: true, has_unidentified: true });
      },
      getUnidentifiedFaces: (catalogId, params) => {
        mockCalls.push({ url: 'faces', params });
        const left = mockAna.filter((f) => !mockDone.has(f.face_id));
        const offset = Number(params.offset || 0);
        return ok({ faces: left.slice(offset, offset + Number(params.limit)),
                    total: left.length, limit: params.limit, offset });
      },
      getAutoTags: () => ok({ auto_tags: [] }),
      rejectCandidate: () => ok({}),
    },
    faceDetectionAPI: {
      tagFace: (catalogId, faceId) => { mockDone.add(faceId); return ok({}); },
      tagFacesBulk: () => ok({ assigned: 0, assigned_face_ids: [] }),
    },
  };
});
// One object for the whole run, as the app's context is: a new one per render
// would read as a catalog switch on every render.
jest.mock('../context/CatalogContext', () => {
  const value = { activeCatalog: { id: 7, name: 'Test', similarity_metric: 'cosine' } };
  return { useCatalog: () => value };
});
jest.mock('../components/FaceThumbnail', () => () => null);

let container;
let root;
const settle = async () => {
  for (let i = 0; i < 10; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
};
const click = async (el) => { await act(async () => { el.click(); }); await settle(); };
const personRow = (name) => [...container.querySelectorAll('div')]
  .find((d) => d.style.cursor === 'pointer' && d.textContent.includes(name));
const badgeOf = (name) => personRow(name).querySelector('.badge')?.textContent;
const cards = () => [...container.querySelectorAll('button')].filter((b) => b.textContent === '✓');

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
beforeEach(async () => {
  mockCalls.length = 0;
  mockDone.clear();
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => { root.render(<ConfirmFaceDetections />); });
  await settle();
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

test('opening the page asks for counts, never for a list of faces', () => {
  expect(mockCalls.some((c) => c.url === 'summary')).toBe(true);
  expect(mockCalls.some((c) => c.url === 'faces')).toBe(false);
  expect(badgeOf('Ana')).toBe('150');
});

test('choosing a person fetches one page of theirs', async () => {
  await click(personRow('Ana'));
  const asked = mockCalls.filter((c) => c.url === 'faces');
  expect(asked).toHaveLength(1);
  expect(asked[0].params).toMatchObject({ person_id: 1, limit: 100, offset: 0 });
  expect(cards()).toHaveLength(100);
});

test('confirming a face takes its card away and its person\'s badge down', async () => {
  await click(personRow('Ana'));
  await click(cards()[0]);
  expect(cards()).toHaveLength(99);
  expect(badgeOf('Ana')).toBe('149');
});

test('next continues where the server\'s list now continues', async () => {
  await click(personRow('Ana'));
  await click(cards()[0]);                 // one of the first hundred is gone
  const next = [...container.querySelectorAll('button')]
    .find((b) => b.textContent.includes('Next'));
  await click(next);
  const asked = mockCalls.filter((c) => c.url === 'faces');
  // 99 left on page one, so the server's 100th remaining face starts page two.
  expect(asked[asked.length - 1].params.offset).toBe(99);
  expect(cards()[0]).toBeTruthy();
  expect(cards()).toHaveLength(50);        // 149 remaining - 99 on page one
});
