import { useState, useRef, useCallback, useEffect } from 'react';

// ─── Rectangle ("marquee") selection over a container's tiles ────────────────
//
// mass-edit-event-photo-dates-spec.md section 4.2. Press in the container and
// drag: a rectangle is drawn, the tiles it touches preview their new state,
// and on release they are handed to `onCommit` with the mode the modifiers
// asked for:
//
//   plain drag   → 'toggle'
//   Shift + drag → 'check'
//   Alt + drag   → 'uncheck'
//
// It knows nothing about dates, so face clusters can adopt it later: a tile
// is any element under the container carrying `data-marquee-id`, and the ids
// come back as the attribute's strings.
//
// Rules the spec fixes:
// - A press that moves less than `threshold` px (5) is a click. Nothing is
//   committed and the browser's own click event reaches the tile.
// - The marquee stays inside its container: only that container's tiles are
//   considered and the drawn box is clipped to it. A selection is always
//   "this group, minus some", so a rectangle never spans two cards.
// - Dragging within `edge` px (40) of the window's top or bottom edge scrolls
//   the page, and the rectangle grows with the scroll.
// - Touch has no marquee (pointerType 'touch' is ignored), so a tap toggles.

const modeOf = (e) => {
  if (e.altKey) return 'uncheck';
  if (e.shiftKey) return 'check';
  return 'toggle';
};

const intersects = (a, b) => (
  a.left <= b.right && a.right >= b.left && a.top <= b.bottom && a.bottom >= b.top
);

export default function useMarquee({
  containerRef,
  onCommit,
  threshold = 5,
  edge = 40,
  itemSelector = '[data-marquee-id]',
} = {}) {
  // `box` is in the container's own coordinates, ready for an absolutely
  // positioned overlay. `preview` is {ids: Set<string>, mode} while dragging.
  const [box, setBox] = useState(null);
  const [preview, setPreview] = useState(null);

  // Mutable drag state lives in a ref: the window listeners read it between
  // renders, and setting it must not re-render on every pointer move.
  const drag = useRef(null);
  // The click-swallowing trap a finished drag leaves armed: {container, fn}.
  const trap = useRef(null);
  const onCommitRef = useRef(onCommit);
  onCommitRef.current = onCommit;

  const disarm = () => {
    if (trap.current) {
      trap.current.container.removeEventListener('click', trap.current.fn, { capture: true });
      trap.current = null;
    }
  };

  const stopScroll = () => {
    if (drag.current?.scrollTimer) {
      clearInterval(drag.current.scrollTimer);
      drag.current.scrollTimer = null;
    }
  };

  // Recompute the rectangle and the touched tiles from the last pointer
  // position. The start is kept in PAGE coordinates, so a scroll between two
  // moves stretches the rectangle rather than dragging it along.
  const update = useCallback(() => {
    const d = drag.current;
    const container = containerRef?.current;
    if (!d || !d.active || !container) return;
    const sx = window.scrollX || 0;
    const sy = window.scrollY || 0;
    const startX = d.startPageX - sx;
    const startY = d.startPageY - sy;
    const c = container.getBoundingClientRect();
    const sel = {
      left: Math.max(Math.min(startX, d.x), c.left),
      right: Math.min(Math.max(startX, d.x), c.right),
      top: Math.max(Math.min(startY, d.y), c.top),
      bottom: Math.min(Math.max(startY, d.y), c.bottom),
    };
    const ids = new Set();
    container.querySelectorAll(itemSelector).forEach((el) => {
      if (intersects(sel, el.getBoundingClientRect())) {
        ids.add(el.getAttribute('data-marquee-id'));
      }
    });
    d.ids = ids;
    setBox({
      left: sel.left - c.left,
      top: sel.top - c.top,
      width: Math.max(0, sel.right - sel.left),
      height: Math.max(0, sel.bottom - sel.top),
    });
    setPreview({ ids, mode: d.mode });
  }, [containerRef, itemSelector]);

  const end = useCallback((commit) => {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    if (d.scrollTimer) clearInterval(d.scrollTimer);
    window.removeEventListener('pointermove', d.onMove);
    window.removeEventListener('pointerup', d.onUp);
    window.removeEventListener('pointercancel', d.onCancel);
    setBox(null);
    setPreview(null);
    if (!d.active) return;

    // The release ends a drag, not a click. The browser still fires `click`
    // on the element under both the press and the release, which would toggle
    // that tile a second time, so the next click inside the container is
    // swallowed. The trap stays armed until the next press rather than for a
    // fixed time: a click with no press before it can only be the drag's own,
    // and every genuine click starts with a press, which disarms it. A timer
    // raced the click it was meant to catch.
    const container = containerRef?.current;
    disarm();
    if (container) {
      const fn = (ev) => {
        ev.stopPropagation();
        ev.preventDefault();
        trap.current = null;
      };
      container.addEventListener('click', fn, { capture: true, once: true });
      trap.current = { container, fn };
    }
    if (commit && d.ids && d.ids.size > 0 && onCommitRef.current) {
      onCommitRef.current([...d.ids], d.mode);
    }
  }, [containerRef]);

  const onPointerDown = useCallback((e) => {
    disarm();
    if (e.pointerType === 'touch') return;
    if (e.button !== undefined && e.button !== 0) return;
    if (drag.current) end(false);

    const d = {
      startPageX: e.clientX + (window.scrollX || 0),
      startPageY: e.clientY + (window.scrollY || 0),
      startClientX: e.clientX,
      startClientY: e.clientY,
      x: e.clientX,
      y: e.clientY,
      mode: modeOf(e),
      active: false,
      ids: null,
      scrollTimer: null,
    };

    d.onMove = (ev) => {
      d.x = ev.clientX;
      d.y = ev.clientY;
      d.mode = modeOf(ev);
      if (!d.active) {
        const dist = Math.hypot(ev.clientX - d.startClientX, ev.clientY - d.startClientY);
        if (dist < threshold) return;
        d.active = true;
      }
      // Keep the browser from selecting text or dragging the image.
      if (ev.cancelable) ev.preventDefault();

      // Window-edge auto-scroll. One interval per drag, started on entering
      // the band and stopped on leaving it; each tick re-measures, so the
      // rectangle and the preview follow the content.
      const vh = window.innerHeight;
      const dir = ev.clientY < edge ? -1 : (ev.clientY > vh - edge ? 1 : 0);
      if (dir === 0) {
        stopScroll();
      } else if (!d.scrollTimer) {
        d.scrollTimer = setInterval(() => {
          const now = d.y < edge ? -1 : (d.y > window.innerHeight - edge ? 1 : 0);
          if (now === 0) { stopScroll(); return; }
          window.scrollBy(0, now * 12);
          update();
        }, 16);
      }
      update();
    };
    d.onUp = () => end(true);
    d.onCancel = () => end(false);

    drag.current = d;
    window.addEventListener('pointermove', d.onMove);
    window.addEventListener('pointerup', d.onUp);
    window.addEventListener('pointercancel', d.onCancel);
  }, [threshold, edge, update, end]);

  // A card unmounting mid-drag (its last photo applied away) must not leave
  // window listeners or a scroll timer behind.
  useEffect(() => () => { end(false); disarm(); }, [end]);

  return { onPointerDown, box, preview, dragging: box !== null };
}

/**
 * The state a tile shows: its real state, or the preview's while a marquee
 * touches it.
 */
export function previewChecked(id, checked, preview) {
  if (!preview || !preview.ids.has(String(id))) return checked;
  if (preview.mode === 'check') return true;
  if (preview.mode === 'uncheck') return false;
  return !checked;
}
