import { useCallback, useMemo, useRef, useState } from 'react';
import { photoAPI } from '../services/api';

// ─── A photo scope, fetched a page at a time ─────────────────────────────────
//
// What the Photos page browses: a day, a month, a year or the whole catalog.
// It used to arrive whole, and "All" of 2022 on `family` is 29,680 photographs
// and 19 MB - "All" of the catalog 301,195 and 190 MB - for a strip that shows
// a dozen tiles and a viewer that shows one photo.
//
// So `items` is an array as long as the scope, with holes where nothing has
// been fetched yet. Indexes are positions in the scope, the same on every page
// of it: the server orders it oldest first by (date, id), which is what makes
// "position 4,812" mean the same photo on every request.
//
//   open(catalogId, {year, month, day}, {around})
//            a new scope. With `around`, the page holding that photo arrives
//            first and its position comes back, so widening to "All" keeps the
//            photo on screen without fetching anything before it.
//   ensure(start, end)
//            fetch whatever pages of [start, end) are not in hand. Resolves to
//            the items once they are.
//   indexOf(id)
//            position of a loaded photo, or -1. A Map, not findIndex: under
//            "All" the array is 300,000 long.
//
// Months here are 1-BASED, as the server's are. The page's own months are
// 0-based (Date.getMonth) and it converts.

export const PAGE = 200;

const empty = () => ({
  token: 0,
  catalogId: null,
  params: null,
  total: null,
  items: [],
  indexById: new Map(),
  loaded: new Set(),
  inflight: new Map(),
});

export function usePhotoScope() {
  // The scope lives in a ref so that a page arriving late can tell whether it
  // still belongs to the scope on screen; `version` re-renders when it changes.
  const ref = useRef(empty());
  const [, setVersion] = useState(0);
  const bump = useCallback(() => setVersion((v) => v + 1), []);

  const fill = (s, offset, photos) => {
    // A new array each time, so anything memoised on `items` sees the change.
    s.items = s.items.slice();
    photos.forEach((p, i) => {
      s.items[offset + i] = p;
      s.indexById.set(p.id, offset + i);
    });
    s.loaded.add(Math.floor(offset / PAGE));
  };

  const reset = useCallback(() => {
    ref.current = { ...empty(), token: ref.current.token + 1 };
    bump();
  }, [bump]);

  const open = useCallback(async (catalogId, params, { around = null } = {}) => {
    const token = ref.current.token + 1;
    ref.current = { ...ref.current, token };
    const query = { ...params, limit: PAGE };
    if (around != null) query.around = around;
    else query.offset = 0;
    const r = await photoAPI.getScope(catalogId, query);
    // Superseded by a later open: a slow "All" must not land over the day the
    // user picked after it.
    if (ref.current.token !== token) return null;
    const { photos = [], total = 0, offset = 0, position = null } = r.data;
    const s = {
      ...empty(), token, catalogId, params, total, items: new Array(total),
    };
    fill(s, offset, photos);
    ref.current = s;
    bump();
    return { items: s.items, total, position };
  }, [bump]);

  const ensure = useCallback((start, end) => {
    const s = ref.current;
    if (s.total == null || s.total === 0) return Promise.resolve(s.items);
    const lo = Math.max(0, Math.floor(start / PAGE));
    const hi = Math.min(Math.ceil(s.total / PAGE) - 1,
                        Math.floor((Math.max(start + 1, end) - 1) / PAGE));
    const waits = [];
    for (let page = lo; page <= hi; page += 1) {
      if (s.loaded.has(page)) continue;
      if (!s.inflight.has(page)) {
        const p = photoAPI.getScope(s.catalogId, {
          ...s.params, limit: PAGE, offset: page * PAGE,
        }).then((r) => {
          if (ref.current !== s) return;     // the scope changed meanwhile
          fill(s, page * PAGE, r.data.photos || []);
          bump();
        }).catch(() => {
          // Left unloaded: the next ensure over it tries again.
        }).finally(() => s.inflight.delete(page));
        s.inflight.set(page, p);
      }
      waits.push(s.inflight.get(page));
    }
    return Promise.all(waits).then(() => ref.current.items);
  }, [bump]);

  const indexOf = useCallback(
    (id) => (id == null ? -1 : (ref.current.indexById.get(id) ?? -1)), []);

  const s = ref.current;
  return useMemo(() => ({
    items: s.items,
    total: s.total ?? 0,
    loaded: s.total != null,
    open,
    ensure,
    reset,
    indexOf,
  }), [s.items, s.total, open, ensure, reset, indexOf]);
}

export default usePhotoScope;
