import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter } from 'react-router-dom';
import TransferBatch, { timeLeft } from './TransferBatch';
import MassTransferModal from './MassTransferModal';

// Sending one album to several people (album-mass-transfer-spec.md section 7),
// driven as a person would against a mocked API.

const mockState = { batch: null, calls: [], startError: null };

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {},
  transferAPI: {
    batch: () => Promise.resolve({ data: mockState.batch }),
    massStart: (c, albumId, recipients, ttl) => {
      mockState.calls.push(['massStart', albumId, recipients, ttl]);
      if (mockState.startError) return Promise.reject(mockState.startError);
      return Promise.resolve({ data: { batch_id: 12, missing_files: [] } });
    },
    renew: (c, b, ttl, ids) => {
      mockState.calls.push(['renew', b, ttl, ids]);
      return Promise.resolve({ data: {} });
    },
    addPerson: (c, b, label) => {
      mockState.calls.push(['addPerson', b, label]);
      return Promise.resolve({ data: {} });
    },
    cancel: (c, id) => { mockState.calls.push(['cancel', id]); return Promise.resolve({}); },
    cancelBatch: () => Promise.resolve({ data: {} }),
  },
}));

jest.mock('../services/socket', () => ({
  __esModule: true,
  default: {
    connect: () => {},
    onTransferProgress: () => () => {},
    onTransferDone: () => () => {},
    onTransferFile: () => () => {},
    onTransferFilesReady: () => () => {},
  },
}));

jest.mock('../context/CatalogContext', () => ({
  useCatalog: () => ({ activeCatalog: { id: 7 } }),
}));

const HOUR = 3600 * 1000;
const iso = (ms) => new Date(Date.now() + ms).toISOString().replace(/\.\d+Z$/, 'Z');

const member = (id, order, label, status, extra = {}) => ({
  id, recipient_order: order, recipient_label: label,
  label: label || `Person ${order}`, status, wormhole_code: null,
  done_bytes: 0, total_bytes: 1000, error_message: null, ...extra,
});

const BATCH = (expiresIn) => ({
  id: 12, album_id: 3, album_name: 'Summer 2019', expires_at: iso(expiresIn),
  cancelled_at: null, max_people: 20,
  members: [
    member(1, 1, 'Mom', 'waiting_peer', { wormhole_code: '7-revenue-bookseller' }),
    member(2, 2, 'Ana', 'transferring', { done_bytes: 620 }),
    member(3, 3, '', 'completed'),
    member(4, 4, '', 'waiting_peer', { wormhole_code: '12-crossover-tiger' }),
    member(5, 5, '', 'expired'),
  ],
  counts: { waiting_peer: 2, transferring: 1, completed: 1, expired: 1 },
});

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
beforeEach(() => { mockState.batch = null; mockState.calls = []; mockState.startError = null; });

const settle = async () => {
  for (let i = 0; i < 5; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
};

async function mount(element) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => { root.render(<MemoryRouter>{element}</MemoryRouter>); });
  await settle();
  const text = () => document.body.textContent;
  const button = (label) => [...document.body.querySelectorAll('button')]
    .find((b) => b.textContent.trim() === label);
  const click = async (el) => { await act(async () => { el.click(); }); await settle(); };
  return { root, text, button, click, container };
}

const setValue = async (el, value) => {
  const proto = el.tagName === 'SELECT' ? window.HTMLSelectElement : window.HTMLInputElement;
  const setter = Object.getOwnPropertyDescriptor(proto.prototype, 'value').set;
  await act(async () => {
    setter.call(el, String(value));
    el.dispatchEvent(new Event(el.tagName === 'SELECT' ? 'change' : 'input', { bubbles: true }));
  });
};

// ── the dialog ───────────────────────────────────────────────────────────────

test('how many people, their labels, and when the codes expire', async () => {
  const created = [];
  const { root, text, button, click } = await mount(
    <MassTransferModal visible album={{ id: 3, name: 'Summer 2019' }} catalogId={7}
      onClose={() => {}} onCreated={(...a) => created.push(a)} />,
  );
  const labels = () => [...document.body.querySelectorAll('input[aria-label^="Label for person"]')];
  expect(labels()).toHaveLength(2);
  expect(labels().map((l) => l.placeholder)).toEqual(['Person 1', 'Person 2']);

  await setValue(labels()[0], 'Mom');
  await setValue(document.getElementById('mt-count'), 4);
  expect(labels()).toHaveLength(4);
  expect(labels()[0].value).toBe('Mom');                 // kept when the count changes

  await setValue(labels()[2], 'mom');
  expect(text()).toContain('Two people are called');
  await setValue(labels()[2], 'Ana');
  expect(text()).not.toContain('Two people are called');

  // Three days is the default and the most on offer.
  const ttl = document.getElementById('mt-ttl');
  expect(ttl.value).toBe('72');
  expect([...ttl.options].map((o) => Number(o.value))).toEqual([1, 12, 24, 48, 72]);
  await setValue(ttl, 24);

  await click(button('Create 4 codes'));
  expect(mockState.calls[0]).toEqual(['massStart', 3,
    [{ label: 'Mom' }, { label: '' }, { label: 'Ana' }, { label: '' }], 24]);
  expect(created).toEqual([[12, []]]);
  act(() => root.unmount());
});

test('an album that already has a group opens it instead', async () => {
  mockState.startError = { response: { status: 409, data: { batch_id: 9 } } };
  const created = [];
  const { root, button, click } = await mount(
    <MassTransferModal visible album={{ id: 3, name: 'Summer 2019' }} catalogId={7}
      onClose={() => {}} onCreated={(...a) => created.push(a)} />,
  );
  await click(button('Create 2 codes'));
  expect(created).toEqual([[9, []]]);
  act(() => root.unmount());
});

// ── the group view ───────────────────────────────────────────────────────────

test('one row per person, codes only while waiting, and the deadline', async () => {
  mockState.batch = BATCH(2 * 24 * HOUR + 23 * HOUR + 30 * 60000);
  const { root, text } = await mount(<TransferBatch batchId={12} />);
  const rows = [...document.body.querySelectorAll('[data-testid="batch-member"]')];
  expect(rows.map((r) => r.cells[0].textContent)).toEqual(
    ['Mom', 'Ana', 'Person 3', 'Person 4', 'Person 5']);
  expect(rows[0].textContent).toContain('7-revenue-bookseller');
  expect(rows[1].textContent).toContain('62%');
  expect(rows[2].textContent).toContain('done');
  expect(rows[4].textContent).toContain('code expired');
  expect(text()).toContain('Summer 2019 · 5 people');
  expect(text()).toContain('2 d 23 h left');
  expect(text()).toContain('2 waiting · 1 sending · 1 done · 1 expired');
  act(() => root.unmount());
});

test('Copy all codes copies the waiting ones as Label: code', async () => {
  mockState.batch = BATCH(HOUR);
  const copied = [];
  Object.assign(navigator, { clipboard: { writeText: (t) => copied.push(t) } });
  const { root, button, click } = await mount(<TransferBatch batchId={12} />);
  await click(button('Copy all codes'));
  expect(copied).toEqual(['Mom: 7-revenue-bookseller\nPerson 4: 12-crossover-tiger']);
  act(() => root.unmount());
});

test('a new code for one person asks for the deadline, then renews that person', async () => {
  mockState.batch = BATCH(HOUR);
  const { root, text, button, click } = await mount(<TransferBatch batchId={12} />);
  await click(button('New code'));
  expect(text()).toContain('A new code for Person 5');
  expect(text()).toContain('The deadline moves for the whole group');
  await setValue(document.querySelector('select[aria-label="New deadline"]'), 48);
  await click(button('Make codes'));
  expect(mockState.calls).toContainEqual(['renew', 12, 48, [5]]);
  act(() => root.unmount());
});

test('after the deadline: says so, and new codes are the way on', async () => {
  mockState.batch = BATCH(-HOUR);
  const { root, text, button, click } = await mount(<TransferBatch batchId={12} />);
  expect(text()).toContain('Codes expired');
  expect(button('Add person').disabled).toBe(true);
  await click(button('New codes for unfinished'));
  expect(text()).toContain('New codes for 1 person');
  await click(button('Make codes'));
  expect(mockState.calls).toContainEqual(['renew', 12, 72, undefined]);
  act(() => root.unmount());
});

test('adding a person and stopping one', async () => {
  mockState.batch = BATCH(HOUR);
  const { root, button, click } = await mount(<TransferBatch batchId={12} />);
  await click(button('Add person'));
  const input = document.body.querySelector('input[aria-label="Label for the new person"]');
  expect(input.placeholder).toBe('Person 6');
  await setValue(input, 'Uncle Rui');
  await click(button('Add'));
  expect(mockState.calls).toContainEqual(['addPerson', 12, 'Uncle Rui']);
  await click([...document.body.querySelectorAll('button')]
    .find((b) => b.title === "Stop Mom's code"));
  expect(mockState.calls).toContainEqual(['cancel', 1]);
  act(() => root.unmount());
});

test('time left reads in days, hours or minutes, and ends', () => {
  const now = Date.now();
  expect(timeLeft(new Date(now + 3 * 24 * HOUR), now)).toBe('3 d 0 h left');
  expect(timeLeft(new Date(now + 5 * HOUR + 12 * 60000), now)).toBe('5 h 12 min left');
  expect(timeLeft(new Date(now + 30000), now)).toBe('1 min left');
  expect(timeLeft(new Date(now - 1), now)).toBeNull();
});
