import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { usePhotoScope, PAGE } from './usePhotoScope';

// A scope of 2.5 pages, ids 1..N, oldest first. Every request is recorded so
// the tests can say what was fetched, not just what came back.
const N = Math.floor(PAGE * 2.5);
const mockN = N;
const mockAll = Array.from({ length: N }, (_, i) => ({ id: i + 1, file_name: `p${i + 1}.jpg` }));
const mockCalls = [];
const mockGate = { hold: null };   // when set, getScope waits on it before answering

jest.mock('../services/api', () => ({
  __esModule: true,
  photoAPI: {
    getScope: async (catalogId, params) => {
      mockCalls.push(params);
      if (mockGate.hold) await mockGate.hold;
      const limit = Number(params.limit);
      let offset = Number(params.offset ?? 0);
      const body = { total: mockN, limit };
      if (params.around != null) {
        const at = mockAll.findIndex((p) => p.id === Number(params.around));
        body.position = at < 0 ? null : at;
        if (at >= 0) offset = Math.floor(at / limit) * limit;
      }
      body.offset = offset;
      body.photos = mockAll.slice(offset, offset + limit);
      return { data: body };
    },
  },
}));

let scope;
const Probe = () => { scope = usePhotoScope(); return null; };

let root;
beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
beforeEach(async () => {
  mockCalls.length = 0;
  mockGate.hold = null;
  root = createRoot(document.createElement('div'));
  await act(async () => { root.render(<Probe />); });
});
afterEach(() => { act(() => root.unmount()); });

test('a scope is as long as its total, with only the first page fetched', async () => {
  await act(async () => { await scope.open(1, { year: 2022 }); });
  expect(scope.total).toBe(N);
  expect(scope.items.length).toBe(N);
  expect(scope.items[0].id).toBe(1);
  expect(scope.items[PAGE]).toBeUndefined();
  expect(mockCalls).toEqual([{ year: 2022, limit: PAGE, offset: 0 }]);
});

test('around opens on the page holding the photo, at its position', async () => {
  let opened;
  await act(async () => { opened = await scope.open(1, {}, { around: PAGE + 7 }); });
  expect(opened.position).toBe(PAGE + 6);
  expect(scope.items[PAGE + 6].id).toBe(PAGE + 7);
  expect(scope.items[0]).toBeUndefined();
  expect(scope.indexOf(PAGE + 7)).toBe(PAGE + 6);
});

test('ensure fetches only the pages it is missing, once each', async () => {
  await act(async () => { await scope.open(1, {}); });
  await act(async () => {
    await Promise.all([scope.ensure(PAGE - 5, PAGE + 5), scope.ensure(PAGE, PAGE + 1)]);
  });
  expect(mockCalls.map((c) => c.offset)).toEqual([0, PAGE]);
  expect(scope.items[PAGE + 4].id).toBe(PAGE + 5);
  expect(scope.indexOf(PAGE + 5)).toBe(PAGE + 4);
});

test('a scope opened later wins over a slower one opened first', async () => {
  let release;
  mockGate.hold = new Promise((r) => { release = r; });
  let first;
  let second;
  await act(async () => {
    first = scope.open(1, { year: 2020 });
    second = scope.open(1, { year: 2022 });
    release();
    [first, second] = await Promise.all([first, second]);
  });
  expect(first).toBeNull();
  expect(second.total).toBe(N);
});

test('a page arriving for a scope that has been replaced is dropped', async () => {
  await act(async () => { await scope.open(1, { year: 2020 }); });
  let release;
  mockGate.hold = new Promise((r) => { release = r; });
  let late;
  await act(async () => { late = scope.ensure(PAGE * 2, PAGE * 2 + 1); });
  mockGate.hold = null;
  await act(async () => { await scope.open(1, { year: 2022 }, { around: 3 }); });
  await act(async () => { release(); await late; });
  expect(scope.items[PAGE * 2]).toBeUndefined();
});
