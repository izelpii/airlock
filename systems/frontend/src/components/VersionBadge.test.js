import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import VersionBadge from './VersionBadge';

// Beside the logo: the version, and - only without a valid license - a red
// "Unlicensed - No commercial use". Words only; nothing is disabled.

const mockState = { license: undefined };

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {},
  systemAPI: {
    version: () => Promise.resolve({ data: {
      version: '6.4.0', release: '6.4.0', source: 'build',
      ...(mockState.license !== undefined ? { license: mockState.license } : {}),
    } }),
  },
}));

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });

async function mount() {
  const container = document.createElement('div');
  const root = createRoot(container);
  await act(async () => { root.render(<VersionBadge />); });
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  const notice = () => container.querySelector('[data-testid="unlicensed-notice"]');
  return { root, container, notice };
}

test('licensed: the version only, and the holder on the hover', async () => {
  mockState.license = { status: 'licensed', name: 'izelpi', edition: 'Perpetual' };
  const { root, container, notice } = await mount();
  expect(container.textContent).toBe('v6.4');
  expect(notice()).toBeNull();
  expect(container.querySelector('span').title)
    .toContain('Licensed to izelpi — Perpetual');
  act(() => root.unmount());
});

test('no license, or one that does not verify: the red notice', async () => {
  for (const license of [{ status: 'unlicensed' },
    { status: 'invalid', reason: 'the signature does not match' }]) {
    mockState.license = license;
    // eslint-disable-next-line no-await-in-loop
    const { root, notice } = await mount();
    expect(notice().textContent).toBe('Unlicensed - No commercial use');
    expect(notice().style.color).toBe('rgb(198, 40, 40)');
    act(() => root.unmount());
  }
});

test('a backend too old to report a license shows no notice', async () => {
  mockState.license = undefined;
  const { root, notice } = await mount();
  expect(notice()).toBeNull();
  act(() => root.unmount());
});
