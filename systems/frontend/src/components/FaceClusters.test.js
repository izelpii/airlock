import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import ClustersPanel from './FaceClusters';

// Identify groups said "0 groups" for most of a minute on `family`: the panel
// drew its header while the whole 9,479-group answer (15.5 MB) was still on
// its way. It now asks for a page and says nothing about counts until it has
// one.

const mockCalls = [];
const mockGate = { hold: null };

jest.mock('../services/api', () => ({
  __esModule: true,
  faceRecognitionAPI: {
    getClusters: async (catalogId, params) => {
      mockCalls.push(params);
      if (mockGate.hold) await mockGate.hold;
      const offset = Number(params.offset || 0);
      const clusters = Array.from({ length: Math.min(params.limit, 120 - offset) }, (_, i) => ({
        id: offset + i + 1, member_count: 3, cohesion: 0.1, members: [],
        resolved_person_id: null,
      }));
      return { data: { clusters, total: 120, total_faces: 360, limit: params.limit, offset } };
    },
  },
}));
jest.mock('./FaceThumbnail', () => () => null);

let container;
let root;
const settle = async () => {
  for (let i = 0; i < 10; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
};
const render = async () => {
  await act(async () => {
    root.render(<ClustersPanel catalogId={9} people={[]} emptyMessage={<p>none</p>} />);
  });
};

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
beforeEach(() => {
  mockCalls.length = 0;
  mockGate.hold = null;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

test('while the first page is on its way it says nothing about counts', async () => {
  let release;
  mockGate.hold = new Promise((r) => { release = r; });
  await render();
  expect(container.textContent).not.toMatch(/0 group/);
  expect(container.textContent).not.toMatch(/none/);
  await act(async () => { release(); });
  await settle();
  expect(container.textContent).toMatch(/360 unidentified faces in 120 groups/);
});

test('it asks for a page, never for everything', async () => {
  await render();
  await settle();
  expect(mockCalls).toEqual([{ limit: 50, offset: 0 }]);
});

test('next asks for the following page', async () => {
  await render();
  await settle();
  const next = [...container.querySelectorAll('button')].find((b) => b.textContent.includes('Next'));
  await act(async () => { next.click(); });
  await settle();
  expect(mockCalls[mockCalls.length - 1]).toEqual({ limit: 50, offset: 50 });
});
