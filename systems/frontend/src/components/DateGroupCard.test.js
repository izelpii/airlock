import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import DateGroupCard, { computeSuggestion } from './DateGroupCard';
import { viewFiles, groupByDay } from '../views/EventDateEditor';

// The editor module is imported for its grouping helpers only.
jest.mock('../services/api', () => ({ eventAPI: {}, photoAPI: {} }));

// One date group of the event date editor
// (mass-edit-event-photo-dates-spec.md sections 4.1-4.3): the guessed-date
// flag, the "Looks like" suggestion, selection and Apply.

const row = (id, over = {}) => ({
  id,
  file_name: `${id}.jpg`,
  master_datetime: '2026-10-02T23:50:00-04:00',
  master_datetime_source: 'filesystem',
  filename_datetime: null,
  filename_has_time: false,
  alternative_file_path: null,
  is_video: false,
  ...over,
});

// The card is given a group the way the editor builds it.
const groupOf = (rows, staged = {}) => {
  const viewed = viewFiles(rows, staged);
  return { group: groupByDay(viewed)[0], viewed };
};

let container;
let root;
let applied;

async function render(rows, { staged, eventRows } = {}) {
  applied = [];
  const { group } = groupOf(rows, staged);
  const eventFiles = viewFiles(eventRows || rows, staged || {});
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(
      <DateGroupCard
        group={group} catalogId="7" eventFiles={eventFiles}
        onApply={(ids, dt) => applied.push({ ids, dt })} onUndo={() => {}}
      />,
    );
  });
}

const q = (sel) => container.querySelector(sel);
const tiles = () => [...container.querySelectorAll('[data-marquee-id]')];

function type(input, value) {
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value').set.call(input, value);
  input.dispatchEvent(new Event('input', { bubbles: true }));
}

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('a group with guessed dates gets the amber border and the "N of M" note', async () => {
  await render([row(1), row(2), row(3, { master_datetime_source: 'exif' })]);
  const card = q('[data-testid="date-group"]');
  expect(card.getAttribute('data-guessed')).toBe('true');
  expect(card.style.border).toContain('#e6a817');
  expect(q('[data-testid="guessed-note"]').textContent).toMatch(/^2 of 3 dates were guessed by the import/);
});

test('a camera-dated group has the plain border and no note', async () => {
  await render([row(1, { master_datetime_source: 'exif' })]);
  expect(q('[data-testid="date-group"]').getAttribute('data-guessed')).toBe('false');
  expect(q('[data-testid="guessed-note"]')).toBeNull();
});

test('the suggestion is the most common file-name date, and clicking it fills without applying', async () => {
  await render([
    row(1, { filename_datetime: '2003-01-01T18:00:00', filename_has_time: true }),
    row(2, { filename_datetime: '2003-01-01T18:00:00', filename_has_time: true }),
    row(3, { filename_datetime: '2004-05-05T12:00:00' }),
  ]);
  const s = q('[data-testid="suggestion"]');
  expect(s.textContent).toBe('Jan 1, 2003 6:00 PM');
  expect(s.parentElement.textContent).toContain('(from 2 file names)');
  await act(async () => { s.click(); });
  expect(q('[data-testid="apply-date"]').value).toBe('2003-01-01');
  expect(q('[data-testid="apply-time"]').value).toBe('18:00');
  expect(applied).toEqual([]);
});

test('file names that disagree on the time leave the time empty', () => {
  const { group, viewed } = groupOf([
    row(1, { filename_datetime: '2003-01-01T18:00:00', filename_has_time: true }),
    row(2, { filename_datetime: '2003-01-01T09:30:00', filename_has_time: true }),
  ]);
  expect(computeSuggestion(group.day, group.files, viewed)).toMatchObject({ date: '2003-01-01', time: '' });
});

test('a file-name date without a time of day suggests no time', () => {
  const { group, viewed } = groupOf([row(1, { filename_datetime: '2003-01-01T12:00:00' })]);
  expect(computeSuggestion(group.day, group.files, viewed)).toMatchObject({ date: '2003-01-01', time: '' });
});

test('no suggestion for a trusted group whose file names agree with its day', () => {
  const { group, viewed } = groupOf([row(1, {
    master_datetime_source: 'exif', filename_datetime: '2026-10-02T08:00:00',
  })]);
  expect(computeSuggestion(group.day, group.files, viewed)).toBeNull();
});

test('a guessed group without file-name dates is offered the nearest trusted day of the event', () => {
  const rows = [
    row(1),
    row(2, { master_datetime: '2003-01-01T18:00:00', master_datetime_source: 'exif' }),
    row(3, { master_datetime: '2026-09-28T10:00:00', master_datetime_source: 'exif' }),
  ];
  const viewed = viewFiles(rows, {});
  const guessedGroup = groupByDay(viewed).find((g) => g.day === '2026-10-02');
  expect(computeSuggestion(guessedGroup.day, guessedGroup.files, viewed))
    .toEqual({ date: '2026-09-28', time: '', count: null, from: 'event' });
});

test('every photo starts checked, a click unchecks, and Apply sends only the checked ones at noon', async () => {
  await render([row(1), row(2), row(3)]);
  expect(tiles().every((t) => t.getAttribute('data-checked') === 'true')).toBe(true);
  await act(async () => { tiles()[1].click(); });
  expect(tiles()[1].getAttribute('data-checked')).toBe('false');
  expect(tiles()[1].style.opacity).toBe('0.35');
  const apply = q('[data-testid="apply"]');
  expect(apply.disabled).toBe(true); // no date yet
  await act(async () => { type(q('[data-testid="apply-date"]'), '2003-01-01'); });
  expect(apply.textContent).toBe('Apply 2');
  await act(async () => { apply.click(); });
  expect(applied).toEqual([{ ids: [1, 3], dt: '2003-01-01T12:00' }]);
});

test('"None" and "All" uncheck and check the whole card', async () => {
  await render([row(1), row(2)]);
  const link = (label) => [...container.querySelectorAll('button')].find((b) => b.textContent === label);
  await act(async () => { link('None').click(); });
  expect(q('[data-testid="apply"]').textContent).toBe('Apply 0');
  await act(async () => { link('All').click(); });
  expect(q('[data-testid="apply"]').textContent).toBe('Apply 2');
});

test('a group over 200 photos shows 200 and says Apply covers them all', async () => {
  await render(Array.from({ length: 205 }, (_, i) => row(i + 1)));
  expect(tiles()).toHaveLength(200);
  expect(container.textContent).toContain('Showing 200 of 205. Applying covers all 205, minus anything unchecked above.');
  expect(q('[data-testid="apply"]').textContent).toBe('Apply 205');
});

test('a file with no alternative version yet is shown as a placeholder, not left out', async () => {
  await render([row(1, { file_name: 'IMG_1.CR3' }), row(2)]);
  expect(tiles()).toHaveLength(2);
  expect(container.querySelectorAll('[data-testid="tile-placeholder"]')).toHaveLength(1);
});

test('staged photos carry the pencil badge and the "changed from" line', async () => {
  await render([row(1), row(2)], { staged: { 1: '2003-01-01T12:00', 2: '2003-01-01T12:00' } });
  expect(container.querySelectorAll('[data-testid="staged-badge"]')).toHaveLength(2);
  expect(q('[data-testid="changed-from"]').textContent).toBe('2 changed from Oct 2, 2026 · Undo');
  // A staged photo is no longer a guess.
  expect(q('[data-testid="guessed-note"]')).toBeNull();
});
