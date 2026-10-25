import React, { useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import useMarquee, { previewChecked } from './useMarquee';

// The rectangle selection of mass-edit-event-photo-dates-spec.md section 4.2:
// toggle / Shift = check / Alt = uncheck, a 5px click threshold, one container
// only, and window-edge auto-scroll. jsdom has no layout, so every element's
// box is given by hand: tiles are 96px squares on a 100px pitch.

// jsdom 16 has no PointerEvent. A MouseEvent of the same type, with
// pointerType added, is what React and the hook read.
function pointer(target, type, { x, y, pointerType = 'mouse', ...mods } = {}) {
  const ev = new MouseEvent(type, {
    bubbles: true, cancelable: true, clientX: x, clientY: y, button: 0, ...mods,
  });
  Object.defineProperty(ev, 'pointerType', { value: pointerType });
  target.dispatchEvent(ev);
}

const rect = (left, top, width, height) => () => ({
  left, top, width, height, right: left + width, bottom: top + height, x: left, y: top,
});

// Two "cards", each its own marquee container. Card A spans y 0-300 and card B
// y 400-700; each has three tiles in a row.
const Card = ({ name, top, onCommit, onClickTile, previews }) => {
  const ref = useRef(null);
  const m = useMarquee({ containerRef: ref, onCommit: (ids, mode) => onCommit(name, ids, mode) });
  previews[name] = m.preview;
  return (
    <div
      ref={ref} data-card={name} data-top={top} onPointerDown={m.onPointerDown}
    >
      {[1, 2, 3].map((i) => {
        const id = `${name}${i}`;
        return (
          <div key={id} data-marquee-id={id} onClick={() => onClickTile(id)}>{id}</div>
        );
      })}
      {m.box && <div data-testid={`box-${name}`} />}
    </div>
  );
};

let container;
let root;
let commits;
let clicks;
let previews;

async function mount() {
  commits = [];
  clicks = [];
  previews = {};
  container = document.createElement('div');
  document.body.appendChild(container);
  root = createRoot(container);
  const onCommit = (name, ids, mode) => commits.push({ name, ids: [...ids].sort(), mode });
  await act(async () => {
    root.render(
      <>
        <Card name="a" top={0} onCommit={onCommit} onClickTile={(id) => clicks.push(id)} previews={previews} />
        <Card name="b" top={400} onCommit={onCommit} onClickTile={(id) => clicks.push(id)} previews={previews} />
      </>,
    );
  });
  container.querySelectorAll('[data-card]').forEach((card) => {
    const top = Number(card.getAttribute('data-top'));
    card.getBoundingClientRect = rect(0, top, 1000, 300);
    card.querySelectorAll('[data-marquee-id]').forEach((tile, i) => {
      tile.getBoundingClientRect = rect(i * 100, top, 96, 96);
    });
  });
}

const tile = (id) => container.querySelector(`[data-marquee-id="${id}"]`);

async function drag(from, to, mods = {}) {
  await act(async () => { pointer(from.el, 'pointerdown', { x: from.x, y: from.y, ...mods }); });
  await act(async () => { pointer(window, 'pointermove', { x: to.x, y: to.y, ...mods }); });
  await act(async () => { pointer(window, 'pointerup', { x: to.x, y: to.y, ...mods }); });
}

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });
beforeEach(mount);
afterEach(() => {
  act(() => root.unmount());
  container.remove();
});

test('a plain drag toggles the tiles it touches', async () => {
  await drag({ el: tile('a2'), x: 150, y: 50 }, { x: 260, y: 60 });
  expect(commits).toEqual([{ name: 'a', ids: ['a2', 'a3'], mode: 'toggle' }]);
});

test('Shift + drag checks and Alt + drag unchecks', async () => {
  await drag({ el: tile('a1'), x: 10, y: 10 }, { x: 150, y: 20 }, { shiftKey: true });
  await drag({ el: tile('a1'), x: 10, y: 10 }, { x: 150, y: 20 }, { altKey: true });
  expect(commits).toEqual([
    { name: 'a', ids: ['a1', 'a2'], mode: 'check' },
    { name: 'a', ids: ['a1', 'a2'], mode: 'uncheck' },
  ]);
});

test('the touched tiles preview while dragging, and the preview ends on release', async () => {
  await act(async () => { pointer(tile('a1'), 'pointerdown', { x: 10, y: 10 }); });
  await act(async () => { pointer(window, 'pointermove', { x: 120, y: 20 }); });
  expect([...previews.a.ids].sort()).toEqual(['a1', 'a2']);
  expect(container.querySelector('[data-testid="box-a"]')).not.toBeNull();
  expect(previewChecked('a1', true, previews.a)).toBe(false);
  expect(previewChecked('a3', true, previews.a)).toBe(true);
  await act(async () => { pointer(window, 'pointerup', { x: 120, y: 20 }); });
  expect(previews.a).toBeNull();
  expect(container.querySelector('[data-testid="box-a"]')).toBeNull();
});

test('a press that moves less than 5px is a click, not a marquee', async () => {
  await drag({ el: tile('a1'), x: 10, y: 10 }, { x: 13, y: 12 });
  await act(async () => { tile('a1').click(); });
  expect(commits).toEqual([]);
  expect(clicks).toEqual(['a1']);
});

test('the click that ends a drag does not toggle the tile a second time', async () => {
  await drag({ el: tile('a1'), x: 10, y: 10 }, { x: 60, y: 20 });
  await act(async () => { tile('a1').click(); });
  expect(commits).toHaveLength(1);
  expect(clicks).toEqual([]);
});

test('a marquee does not cross into another card', async () => {
  // From card A down past card B's tiles.
  await drag({ el: tile('a1'), x: 10, y: 10 }, { x: 250, y: 600 });
  expect(commits).toEqual([{ name: 'a', ids: ['a1', 'a2', 'a3'], mode: 'toggle' }]);
});

test('touch has no marquee', async () => {
  await drag({ el: tile('a1'), x: 10, y: 10 }, { x: 250, y: 60 }, { pointerType: 'touch' });
  expect(commits).toEqual([]);
});

test('dragging near the bottom of the window scrolls the page', async () => {
  const calls = [];
  const original = window.scrollBy;
  window.scrollBy = (x, y) => calls.push(y);
  try {
    await act(async () => { pointer(tile('a1'), 'pointerdown', { x: 10, y: 10 }); });
    await act(async () => { pointer(window, 'pointermove', { x: 50, y: window.innerHeight - 10 }); });
    await act(async () => { await new Promise((r) => setTimeout(r, 60)); });
    await act(async () => { pointer(window, 'pointerup', { x: 50, y: window.innerHeight - 10 }); });
  } finally {
    window.scrollBy = original;
  }
  expect(calls.length).toBeGreaterThan(0);
  expect(calls.every((y) => y > 0)).toBe(true);
});
