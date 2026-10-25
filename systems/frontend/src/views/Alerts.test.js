import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { MemoryRouter } from 'react-router-dom';
import Alerts from './Alerts';
import AlertsBell from '../components/AlertsBell';

// The storage alerts page and the top-bar bell (storage_alerts.py). Plain
// functions in the mocks, not jest.fn: the test config resets mocks before
// every test, which would leave them returning undefined.

const mockState = { alerts: [], counts: { active: 0, critical: 0, unacknowledged: 0 },
                    acknowledged: [], checks: 0, calls: [] };

jest.mock('../services/api', () => ({
  alertsAPI: {
    // Pages and the bell's "recent" are served from mockState.alerts like the
    // backend would: open only unless includeResolved, newest first if recent.
    list: ({ includeResolved = false, limit = 50, offset = 0, recent = false } = {}) => {
      mockState.calls.push({ includeResolved, limit, offset, recent });
      let rows = mockState.alerts.filter((a) => includeResolved || a.active);
      if (recent) rows = [...rows].sort((x, y) => (x.last_seen < y.last_seen ? 1 : -1));
      return Promise.resolve({ data: {
        alerts: rows.slice(offset, offset + limit), total: rows.length, ...mockState.counts,
        thresholds: { warn_percent: 90, critical_percent: 99, warn_free_gb: 20, critical_free_gb: 5 },
      } });
    },
    checkNow: () => { mockState.checks += 1; return Promise.resolve({ data: {} }); },
    acknowledge: (id) => {
      mockState.acknowledged.push(id);
      mockState.alerts = mockState.alerts.map((a) => (a.id === id
        ? { ...a, acknowledged_at: '2026-10-05 01:30:00' } : a));
      mockState.counts = { ...mockState.counts, unacknowledged: 0 };
      return Promise.resolve({ data: {} });
    },
  },
}));

jest.mock('../services/socket', () => ({
  __esModule: true,
  default: { connect: () => {}, onAlertsUpdated: () => () => {} },
}));

const GB = 1024 ** 3;
const fullRaid = {
  id: 3, alert_key: 'storage:/catalogs', kind: 'storage', severity: 'critical', active: true,
  title: 'Disk almost full: /catalogs at 100%',
  message: '0.0 GB free of 916 GB. Used by: Family (database and previews, previews).',
  first_seen: '2026-10-04 23:30:00', last_seen: '2026-10-04 23:35:00',
  resolved_at: null, acknowledged_at: null,
  details: { mount: '/catalogs', total: 916 * GB, used: 916 * GB, free: 0, percent: 100,
             users: { Family: { catalog_id: 9, parts: ['database and previews', 'previews'] },
                      'second-catalog': { catalog_id: 7, parts: ['database and previews'] } } },
};

let container;
let root;
beforeEach(() => {
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  mockState.alerts = [];
  mockState.counts = { active: 0, critical: 0, unacknowledged: 0 };
  mockState.acknowledged = [];
  mockState.checks = 0;
  mockState.calls = [];
});
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

const render = async (el) => {
  await act(async () => { root.render(<MemoryRouter>{el}</MemoryRouter>); });
  await act(async () => { await Promise.resolve(); });
};

const history = (n) => Array.from({ length: n }, (_, i) => ({
  ...fullRaid, id: 100 + i, title: `alert ${i}`, active: i >= n - 2,
  severity: i % 2 ? 'warning' : 'critical',
  last_seen: `2026-10-05 ${String(Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}:00`,
  resolved_at: i >= n - 2 ? null : '2026-10-05 09:00:00',
}));

test('says so when every disk has room', async () => {
  await render(<Alerts />);
  expect(container.querySelector('[data-testid="alerts-none"]')).not.toBeNull();
  expect(container.querySelectorAll('[data-testid="alert-row"]')).toHaveLength(0);
});

test('a full disk shows its numbers, the catalogs on it, and can be acknowledged', async () => {
  mockState.alerts = [fullRaid];
  mockState.counts = { active: 1, critical: 1, unacknowledged: 1 };
  await render(<Alerts />);
  const rows = container.querySelectorAll('[data-testid="alert-row"]');
  expect(rows).toHaveLength(1);
  const text = rows[0].textContent;
  expect(text).toContain('critical');
  expect(text).toContain('/catalogs');
  expect(text).toContain('100%');
  expect(text).toContain('Family');
  expect(text).toContain('second-catalog');

  const ack = [...rows[0].querySelectorAll('button')].find((b) => b.textContent === 'Acknowledge');
  await act(async () => { ack.click(); });
  await act(async () => { await Promise.resolve(); });
  expect(mockState.acknowledged).toEqual([3]);
  expect(container.textContent).toContain('acknowledged');
});

test('check now measures again', async () => {
  await render(<Alerts />);
  const btn = [...container.querySelectorAll('button')].find((b) => b.textContent.includes('Check now'));
  await act(async () => { btn.click(); });
  expect(mockState.checks).toBe(1);
});

test('the page pages through every alert, open and resolved', async () => {
  mockState.alerts = history(60);
  mockState.counts = { active: 2, critical: 1, unacknowledged: 2 };
  await render(<Alerts />);
  expect(container.querySelectorAll('[data-testid="alert-row"]')).toHaveLength(25);
  expect(container.querySelector('[data-testid="alerts-range"]').textContent).toBe('1–25 of 60');
  await act(async () => { container.querySelector('[data-testid="alerts-next"]').click(); });
  await act(async () => { await Promise.resolve(); });
  expect(mockState.calls.at(-1)).toMatchObject({ includeResolved: true, offset: 25, limit: 25 });
  expect(container.querySelector('[data-testid="alerts-range"]').textContent).toBe('26–50 of 60');
});

test('open only shows just the open alerts', async () => {
  mockState.alerts = history(60);
  await render(<Alerts />);
  const btn = [...container.querySelectorAll('button')].find((b) => b.textContent === 'Open only');
  await act(async () => { btn.click(); });
  await act(async () => { await Promise.resolve(); });
  expect(mockState.calls.at(-1)).toMatchObject({ includeResolved: false, offset: 0 });
  expect(container.querySelectorAll('[data-testid="alert-row"]')).toHaveLength(2);
});

test('the bell shows unacknowledged alerts, red when one is critical', async () => {
  mockState.counts = { active: 2, critical: 1, unacknowledged: 2 };
  await render(<AlertsBell />);
  const badge = container.querySelector('[data-testid="alerts-badge"]');
  expect(badge.textContent).toBe('2');
  expect(badge.className).toContain('danger');
});

test('the bell has no badge when nothing needs attention', async () => {
  mockState.counts = { active: 1, critical: 0, unacknowledged: 0 };
  await render(<AlertsBell />);
  expect(container.querySelector('[data-testid="alerts-badge"]')).toBeNull();
});

test("the bell's menu lists the ten most recent alerts, a line each", async () => {
  mockState.alerts = history(30);
  mockState.counts = { active: 2, critical: 1, unacknowledged: 2 };
  await render(<AlertsBell />);
  const toggle = container.querySelector('[data-testid="alerts-toggle"]');
  await act(async () => { toggle.click(); });
  await act(async () => { await Promise.resolve(); });
  expect(mockState.calls.at(-1)).toMatchObject({ includeResolved: true, recent: true, limit: 10 });
  const items = document.querySelectorAll('[data-testid="alerts-menu-item"]');
  expect(items).toHaveLength(10);
  expect(items[0].textContent).toContain('alert 29');          // newest first
  expect(document.body.textContent).toContain('View all alerts');
});
