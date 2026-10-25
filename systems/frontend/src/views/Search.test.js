import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter } from 'react-router-dom';
import Search from './Search';

// Selecting results: "Select all" then untick some, and ranges.

const mockResults = [1, 2, 3, 4, 5].map((id) => ({
  kind: 'file', file_id: id, name: `p${id}.jpg`, thumbnail_url: `/t/${id}`, people: [], pets: [],
}));
const mockAdds = [];

jest.mock('../services/api', () => {
  const ok = (data) => () => Promise.resolve({ data });
  return {
    __esModule: true,
    searchAPI: { search: () => Promise.resolve({ data: { results: mockResults, total: 40, total_capped: false, next_cursor: null } }) },
    savedSearchAPI: {
      list: ok({ saved_searches: [{ id: 9, name: 'Trip', file_count: 0 }] }),
      addFiles: (catalogId, sid, body) => {
        mockAdds.push(sid === 9 ? body : { sid, ...body });
        return Promise.resolve({ data: { added: 1, already: 0 } });
      },
      get: ok({}),
    },
    peopleAPI: { getAll: ok([]) },
    petAPI: { getAll: ok([]) },
    locationAPI: { getAll: ok([]) },
  };
});
jest.mock('../context/CatalogContext', () => {
  const value = { activeCatalog: { id: 7 } };
  return { useCatalog: () => value };
});

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });

async function mount(path = '/search?q=beach') {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(<MemoryRouter initialEntries={[path]}><Search /></MemoryRouter>);
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 20)); });
  const button = (text) => [...container.querySelectorAll('button')].find((b) => b.textContent.startsWith(text));
  const boxes = () => [...container.querySelectorAll('tbody input[type="checkbox"]')];
  const click = async (el, init = {}) => {
    await act(async () => { el.dispatchEvent(new MouseEvent('click', { bubbles: true, ...init })); });
  };
  return { container, root, button, boxes, click };
}

beforeEach(() => { mockAdds.length = 0; });

test('select all, then untick one: the rest stay selected and the search is sent less it', async () => {
  const { container, root, button, boxes, click } = await mount();
  await click(button('Select all 40'));
  expect(boxes().every((b) => b.checked && !b.disabled)).toBe(true);
  expect(container.textContent).toContain('40 selected');

  await click(boxes()[1]);
  expect(boxes().map((b) => b.checked)).toEqual([true, false, true, true, true]);
  expect(container.textContent).toContain('39 selected');

  await click(button('Add to saved search'));
  await click([...container.querySelectorAll('.dropdown-item')].find((i) => i.textContent === 'Trip'));
  expect(mockAdds).toEqual([{ query: 'q=beach', exclude_ids: [2] }]);
  act(() => root.unmount());
});

test('a range follows its first click, selecting or unselecting', async () => {
  const { container, root, button, boxes, click } = await mount();
  await click(button('Select range'));
  await click(boxes()[0]);
  expect(container.textContent).toContain('Now click the last photo to select');
  await click(boxes()[3]);
  expect(boxes().map((b) => b.checked)).toEqual([true, true, true, true, false]);
  expect(container.textContent).toContain('4 selected');

  // Shift-click unselects a range when its first click unselected.
  await click(boxes()[1]);
  await click(boxes()[3], { shiftKey: true });
  expect(boxes().map((b) => b.checked)).toEqual([true, false, false, false, false]);

  // Under "all", a range unticks a subgroup.
  await click(button('Select all 40'));
  await click(button('Select range'));
  await click(boxes()[4]);
  await click(boxes()[2]);
  expect(boxes().map((b) => b.checked)).toEqual([true, true, false, false, false]);
  expect(container.textContent).toContain('37 selected');
  act(() => root.unmount());
});

test('after Run again, the selection goes into that saved search - no name asked', async () => {
  const { container, root, button, boxes, click } = await mount('/search?q=beach&ss=9');
  expect(button('Save this search')).toBeUndefined();
  expect(button('Add to “Trip”').disabled).toBe(true);
  await click(boxes()[0]);
  await click(boxes()[2]);
  await click(button('Add 2 selected to “Trip”'));
  expect(mockAdds).toEqual([{ file_ids: [1, 3], added_from: 'q=beach' }]);
  expect(document.body.textContent).not.toContain("Keeps this search's query");
  expect(container.textContent).toContain('Trip: added 1');
  act(() => root.unmount());
});
