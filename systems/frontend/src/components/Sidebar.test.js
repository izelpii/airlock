import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter } from 'react-router-dom';
import AppSidebar from './Sidebar';

// The foot of the left panel: "Licensed to: <name>" with a valid license, and
// nothing otherwise (the red notice beside the logo covers that case).

const mockState = { license: null };

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {},
  systemAPI: {
    version: () => Promise.resolve({ data: { version: '6.4.1', license: mockState.license } }),
  },
}));
jest.mock('../context/CatalogContext', () => ({ useCatalog: () => ({ activeCatalog: null }) }));
jest.mock('../context/AuthContext', () => ({ useAuth: () => ({ isAdmin: false }) }));

beforeAll(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  // react-pro-sidebar watches its own size; jsdom has no ResizeObserver.
  global.ResizeObserver = global.ResizeObserver || class {
    observe() {}

    unobserve() {}

    disconnect() {}
  };
});

async function mount(visible = true) {
  const container = document.createElement('div');
  const root = createRoot(container);
  await act(async () => {
    root.render(<MemoryRouter><AppSidebar visible={visible} setVisible={() => {}} /></MemoryRouter>);
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  return { root, line: () => container.querySelector('[data-testid="sidebar-licensee"]') };
}

test('a valid license is named at the foot of the panel', async () => {
  mockState.license = { status: 'licensed', name: 'izelpi', edition: 'Perpetual' };
  const { root, line } = await mount();
  expect(line().textContent).toBe('Licensed to: izelpi');
  expect(line().style.color).toBe('rgb(92, 104, 115)');      // the menu's own colour
  act(() => root.unmount());
});

test('no line without a valid license, or with the panel collapsed', async () => {
  for (const license of [{ status: 'unlicensed' }, { status: 'invalid' }, undefined]) {
    mockState.license = license;
    // eslint-disable-next-line no-await-in-loop
    const { root, line } = await mount();
    expect(line()).toBeNull();
    act(() => root.unmount());
  }
  mockState.license = { status: 'licensed', name: 'izelpi' };
  const { root, line } = await mount(false);
  expect(line()).toBeNull();
  act(() => root.unmount());
});
