import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter, Routes, Route } from 'react-router-dom';
import PetDetail from './PetDetail';

// A pet's page asked for every photograph - capped, silently, at 500 - so
// Luna's ~5,000 drew 500 thumbnails at once and said "500". It now asks for
// a page and shows the real count.

const mockCalls = [];
const mockTotal = 130;

jest.mock('../services/api', () => ({
  __esModule: true,
  petAPI: {
    getById: async () => ({ data: { id: 31, name: 'Luna', species: 'dog' } }),
    detections: async (catalogId, petId, params) => {
      mockCalls.push(params);
      const offset = params.offset || 0;
      const n = Math.max(0, Math.min(params.limit, mockTotal - offset));
      return {
        data: {
          detections: Array.from({ length: n }, (_, i) => ({
            id: offset + i + 1, file_id: offset + i + 1, master_datetime: '2021-05-05T10:00:00',
            source: 'Automatic', confidence: 0.9,
          })),
          total: mockTotal, limit: params.limit, offset, years: { 2021: mockTotal },
        },
      };
    },
    setProfilePhoto: async () => ({}),
    unassignDetection: async () => ({}),
  },
}));

let container;
let root;
const settle = async () => {
  for (let i = 0; i < 10; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
};

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
beforeEach(async () => {
  mockCalls.length = 0;
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/pets/9/31']}>
        <Routes><Route path="/pets/:catalogId/:petId" element={<PetDetail />} /></Routes>
      </MemoryRouter>,
    );
  });
  await settle();
});
afterEach(() => { act(() => root.unmount()); container.remove(); });

test('it asks for one page and says how many there are in all', () => {
  expect(mockCalls).toEqual([{ limit: 60, offset: 0 }]);
  expect(container.querySelectorAll('img')).toHaveLength(0);   // no crop_url in the mock
  expect(container.textContent).toMatch(/1–60 of 130/);
  expect(container.textContent).toMatch(/2021 \(130\)/);
});

test('next asks for the following page', async () => {
  const next = [...container.querySelectorAll('button')].find((b) => b.textContent.includes('Next'));
  await act(async () => { next.click(); });
  await settle();
  expect(mockCalls[mockCalls.length - 1]).toEqual({ limit: 60, offset: 60 });
  expect(container.textContent).toMatch(/61–120 of 130/);
});
