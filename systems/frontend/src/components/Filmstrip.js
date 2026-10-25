import React, { useState, useEffect, useRef, useCallback } from 'react';
import CIcon from '@coreui/icons-react';
import { cilImage } from '@coreui/icons';

// ─── The day's filmstrip ──────────────────────────────────────────────────────
// THE strip of thumbnails under the Photos and Videos date rows, drawn the same
// way in both, because it is the same thing: the scope you are browsing, one
// tile per item, the current one marked.
//
// Its job is to stay cheap no matter how much is in scope, and there are two
// separate costs to keep down. Both are paid here so neither page has to think
// about it.
//
//   IMAGES.  Every tile used to carry a src, so opening a day asked for every
//            thumbnail at once - a real burst of 67 requests in one second in
//            the gateway log, through a single-worker backend. An
//            IntersectionObserver over the strip now reveals a tile's image
//            only when it comes within LOOKAHEAD_PX of the visible edge.
//
//   TILES.   With "All" in the date rows the scope is the whole catalog, so the
//            strip would hold one DOM node per photograph - thousands of them,
//            all laid out and all re-styled whenever the selection moves. Only
//            the tiles near the visible window are rendered; the rest are two
//            spacer divs holding exactly the width the missing tiles would have
//            occupied, so the scrollbar, the scroll offsets and every index are
//            identical to a fully rendered strip.
//
// The two overlap on purpose. The window is wider than the image look-ahead, so
// a tile can be rendered and still be waiting for its picture - which is what
// makes scrolling show boxes that fill in rather than gaps that pop.

const TILE = 72;                  // the tile's box
const GAP = 6;                    // the space after it
const PITCH = TILE + GAP;         // ...so tile i starts at i * PITCH
const OVERSCAN = 12;              // tiles rendered beyond each edge of the view
const LOOKAHEAD_PX = 400;         // ...of which this much also loads its image

export const THUMB_SIZE = TILE;

// Layout arithmetic, not decoration: the spacers below are sized in PITCH, so
// the tile must occupy exactly PITCH and never flex. The container therefore
// carries no `gap` - that would add width the spacers do not know about.
const tileStyle = (isActive) => ({
  width: TILE, height: TILE, minWidth: TILE, marginRight: GAP,
  flexShrink: 0, flexGrow: 0,
  borderRadius: 4,
  cursor: 'pointer',
  background: '#f1f3f4',
  overflow: 'hidden',
  display: 'flex', alignItems: 'center', justifyContent: 'center',
  border: isActive ? '2px solid #1a73e8' : '2px solid transparent',
  outline: isActive ? '2px solid #a8c4f8' : 'none',
  opacity: isActive ? 1 : 0.75,
  transition: 'border 0.15s, opacity 0.15s',
});

/**
 * One tile.
 *
 *   src  the thumbnail, or null when the item has none to show. NULL IS NOT A
 *        DETAIL: the thumbnail endpoint redirects to the full file when no
 *        thumbnail exists, so a video without a poster would pull an entire
 *        .MOV into an <img>. The caller decides.
 */
const FilmstripThumb = ({ src, title, isActive, onClick, register }) => {
  const [show, setShow] = useState(
    // eager for the selected tile: arrow-key browsing scrolls it into view and
    // waiting for the observer would show a grey box first. And eager for
    // everything if the browser has no IntersectionObserver - then this is the
    // old behaviour plus loading="lazy".
    () => isActive || typeof IntersectionObserver === 'undefined');
  const boxRef = useRef(null);

  useEffect(() => {
    if (show || !boxRef.current) return undefined;
    return register(boxRef.current, () => setShow(true));
  }, [register, show]);

  useEffect(() => { if (isActive) setShow(true); }, [isActive]);

  return (
    <div ref={boxRef} onClick={onClick} title={title} style={tileStyle(isActive)}>
      {show && src && (
        <img
          src={src}
          alt={title || ''}
          loading="lazy"
          decoding="async"
          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
        />
      )}
      {/* No thumbnail to show - a video whose poster frame has not been made
          yet. The grey box alone reads as "still loading", which it is not. */}
      {!src && <CIcon icon={cilImage} style={{ width: 18, height: 18, color: '#c4c8cc' }} />}
    </div>
  );
};

/**
 * The strip.
 *
 *   items     everything in scope, in the order it should be browsed. May have
 *             HOLES - positions not fetched yet, when the caller pages the
 *             scope - which draw as empty tiles of the same size.
 *   activeId  id of the item on screen; the strip keeps it centred
 *   srcFor    item -> thumbnail URL, or null for "nothing to show"
 *   onSelect  item -> void, when a tile is clicked
 *   activeIndex  optional: where activeId is, when the caller knows. Saves a
 *             search of `items`, which under "All" is the whole catalog.
 *   onRange   optional: (start, end) -> void whenever the rendered window
 *             moves, so a paging caller can fetch what is about to show.
 */
export const Filmstrip = ({ items, activeId, srcFor, onSelect, activeIndex, onRange }) => {
  const stripRef = useRef(null);
  const [range, setRange] = useState({ start: 0, end: OVERSCAN * 2 });

  // ── Which tiles exist ───────────────────────────────────────────────────────
  const measure = useCallback(() => {
    const el = stripRef.current;
    if (!el) return;
    const first = Math.floor(el.scrollLeft / PITCH);
    const across = Math.ceil(el.clientWidth / PITCH);
    const start = Math.max(0, first - OVERSCAN);
    const end = Math.min(items.length, first + across + OVERSCAN);
    // Same window, same object: re-rendering every tile on every scroll event
    // is the cost this whole mechanism exists to avoid.
    setRange((prev) => (prev.start === start && prev.end === end
      ? prev : { start, end }));
  }, [items.length]);

  useEffect(() => { measure(); }, [measure]);

  useEffect(() => {
    if (onRange) onRange(range.start, range.end);
  }, [range, onRange]);

  useEffect(() => {
    const el = stripRef.current;
    if (!el) return undefined;
    // Coalesced to one measurement per frame: a scroll event fires far more
    // often than the window can actually change.
    let frame = null;
    const onScroll = () => {
      if (frame !== null) return;
      frame = requestAnimationFrame(() => { frame = null; measure(); });
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    const ro = typeof ResizeObserver !== 'undefined'
      ? new ResizeObserver(onScroll) : null;
    ro?.observe(el);
    return () => {
      el.removeEventListener('scroll', onScroll);
      ro?.disconnect();
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [measure]);

  // ── Which images load ───────────────────────────────────────────────────────
  const pending = useRef(new Map());        // element -> reveal callback
  const observer = useRef(null);

  // Called from a tile's own effect, which runs BEFORE this component's effects
  // on the same commit - so a tile that registers before the observer exists
  // waits in the map and is picked up when it is built.
  const register = useCallback((el, reveal) => {
    if (!el) return undefined;
    pending.current.set(el, reveal);
    observer.current?.observe(el);
    return () => {
      pending.current.delete(el);
      observer.current?.unobserve(el);
    };
  }, []);

  useEffect(() => {
    const root = stripRef.current;
    if (!root || typeof IntersectionObserver === 'undefined') return undefined;
    // root is the STRIP, not the viewport. rootMargin only expands the root's
    // own rectangle, so with the viewport as root a tile scrolled off the side
    // of the strip is clipped and no margin can reach it; with the strip as
    // root the margin is exactly what it should be - look-ahead along the strip.
    const io = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        const reveal = pending.current.get(entry.target);
        if (reveal) reveal();
        io.unobserve(entry.target);          // nothing left to watch for
        pending.current.delete(entry.target);
      });
    }, { root, rootMargin: `0px ${LOOKAHEAD_PX}px` });
    observer.current = io;
    pending.current.forEach((_, el) => io.observe(el));
    return () => { io.disconnect(); observer.current = null; };
  }, []);

  // ── Keeping the selected tile in view ───────────────────────────────────────
  // Computed from the INDEX rather than scrollIntoView on the element, because
  // under windowing the selected tile may not be rendered yet - which is
  // precisely when the strip most needs to move.
  useEffect(() => {
    const el = stripRef.current;
    if (!el) return;
    const idx = activeIndex ?? items.findIndex((item) => item?.id === activeId);
    if (idx == null || idx < 0) return;
    const target = Math.max(0, (idx * PITCH) + (TILE / 2) - (el.clientWidth / 2));
    // Smooth for a step along the strip; instant for a jump of several screens,
    // which is what widening the scope or landing on a deep link does - and
    // smooth-scrolling a thousand tiles is a long animation over nothing.
    const far = Math.abs(el.scrollLeft - target) > el.clientWidth * 3;
    el.scrollTo({ left: target, behavior: far ? 'auto' : 'smooth' });
  }, [activeId, activeIndex, items]);

  const { start, end } = range;
  const lead = start * PITCH;
  const tail = Math.max(0, (items.length - end)) * PITCH;

  // By index, not items.slice().map(): map skips holes, which would slide
  // every later tile left of the offset its index says it is at.
  const tiles = [];
  for (let i = start; i < Math.min(end, items.length); i += 1) {
    const item = items[i];
    tiles.push(item ? (
      <FilmstripThumb
        key={item.id}
        title={item.file_name}
        isActive={item.id === activeId}
        register={register}
        src={srcFor(item)}
        onClick={() => onSelect(item)}
      />
    ) : (
      <div key={`pending-${i}`} style={tileStyle(false)} />
    ));
  }

  return (
    <div
      ref={stripRef}
      style={{
        display: 'flex',
        overflowX: 'auto',
        padding: '0 4px',
        scrollbarWidth: 'thin',
      }}
    >
      {/* The tiles that are not rendered, as width. This is what keeps the
          scrollbar honest and every tile at the offset its index says. */}
      {lead > 0 && <div style={{ width: lead, flexShrink: 0 }} />}
      {tiles}
      {tail > 0 && <div style={{ width: tail, flexShrink: 0 }} />}
    </div>
  );
};

export default Filmstrip;
