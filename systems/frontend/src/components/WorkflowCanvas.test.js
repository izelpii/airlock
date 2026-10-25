import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter, Routes, Route, useLocation } from 'react-router-dom';
import WorkflowCanvas from './WorkflowCanvas';

// The catalog's backups, drawn in the workflow diagram as cards of their own,
// in a row under the pipeline and connected to nothing.

const mockState = { graph: null, backups: [], runs: [], backupsFail: false };

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {},
  workflowAPI: {
    graph: () => Promise.resolve({ data: mockState.graph }),
  },
  backupAPI: {
    list: () => (mockState.backupsFail
      ? Promise.reject(new Error('no'))
      : Promise.resolve({ data: { backups: mockState.backups } })),
    run: (catalogId, id) => { mockState.runs.push(id); return Promise.resolve({ data: {} }); },
  },
}));

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
beforeEach(() => {
  mockState.runs = [];
  mockState.backupsFail = false;
  mockState.graph = {
    nodes: [
      { id: 1, name: 'Import Photos', type: 'Import', layer: 0 },
      { id: 2, name: 'Thumbnails', type: 'Thumbnails', layer: 1 },
    ],
    edges: [{ from: 1, to: 2, reason: 'needs the imported files' }],
  };
});

const IDLE = {
  id: 1, name: 'Restic at Backup-host', type: 'restic',
  schedule: { kind: 'daily', time: '02:00' },
  next_run: '2026-10-25T06:00:00+00:00',
  last_backup: { at: new Date(Date.now() - 3 * 3600 * 1000).toISOString() },
  status: { state: 'up_to_date' },
};
const RUNNING_CARD = {
  id: 2, name: 'Borg at Backup-host', type: 'borg', schedule: { kind: 'weekly', weekday: 6, time: '03:00' },
  next_run: null, last_backup: { at: null },
  status: { state: 'running', kind: 'backup', reason: 'Backing up', progress: { percent: 42 } },
};

const Where = () => <div data-testid="where">{useLocation().pathname}{useLocation().search}</div>;

async function mount(canManage = true) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      <MemoryRouter initialEntries={['/workflows']}>
        <Routes>
          <Route path="/workflows" element={(
            <WorkflowCanvas catalogId={7} canManageBackups={canManage} />)} />
          <Route path="*" element={<Where />} />
        </Routes>
      </MemoryRouter>,
    );
  });
  await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  const backupNodes = () => [...container.querySelectorAll('[data-testid="backup-node"]')];
  // NodeAction is an SVG <g> whose <title> names it.
  const action = (node, prefix) => [...node.querySelectorAll('g > title')]
    .find((t) => t.textContent.startsWith(prefix)).parentNode;
  const click = async (el) => {
    await act(async () => {
      el.dispatchEvent(new MouseEvent('click', { bubbles: true }));
      await new Promise((r) => setTimeout(r, 10));
    });
  };
  return { root, container, backupNodes, action, click };
}

test('each backup is a card in its own row, connected to nothing', async () => {
  mockState.backups = [IDLE, RUNNING_CARD];
  const { root, container, backupNodes } = await mount();
  const nodes = backupNodes();
  expect(nodes).toHaveLength(2);
  expect(nodes[0].textContent).toContain('Restic at Backup-host');
  expect(nodes[0].textContent).toContain('Backup · restic · Daily at 02:00');
  expect(nodes[0].textContent).toContain('next ');
  expect(nodes[0].querySelector('title').textContent).toContain('last 3 h ago');
  expect(nodes[1].textContent).toContain('42%');
  expect(container.textContent).toContain('Backups');
  expect(container.textContent).toContain('not in the queue');
  // The pipeline's one edge, and no line to or from a backup.
  expect(container.querySelectorAll('path[marker-end]')).toHaveLength(1);
  act(() => root.unmount());
});

test('back up now: idle starts, running cannot, and a viewer cannot at all', async () => {
  mockState.backups = [IDLE, RUNNING_CARD];
  let m = await mount();
  let [idle, running] = m.backupNodes();
  await m.click(m.action(idle, 'Back up now'));
  expect(mockState.runs).toEqual([1]);
  await m.click(m.action(running, 'Already running'));
  expect(mockState.runs).toEqual([1]);
  act(() => m.root.unmount());

  mockState.runs = [];
  m = await mount(false);
  [idle] = m.backupNodes();
  await m.click(m.action(idle, 'Only an owner'));
  expect(mockState.runs).toEqual([]);
  act(() => m.root.unmount());
});

test('settings open the Backup tab on the Catalogs page', async () => {
  mockState.backups = [IDLE];
  const m = await mount();
  await m.click(m.action(m.backupNodes()[0], 'Backup settings'));
  expect(document.body.querySelector('[data-testid="where"]').textContent)
    .toBe('/catalogs?tab=backup');
  act(() => m.root.unmount());
});

test('the pipeline is drawn even when the backups cannot be read', async () => {
  mockState.backupsFail = true;
  const { root, container, backupNodes } = await mount();
  expect(backupNodes()).toHaveLength(0);
  expect(container.textContent).toContain('Import Photos');
  expect(container.textContent).not.toContain('not in the queue');
  act(() => root.unmount());
});
