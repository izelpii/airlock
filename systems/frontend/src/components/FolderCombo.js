import React, { useState, useEffect, useCallback, useRef } from 'react';
import CreatableSelect from 'react-select/creatable';
import { storageRouteAPI } from '../services/api';

// ─── Folder autocomplete inside a storage route ───────────────────────────────
// A browser cannot pick a folder on the SERVER: <input type="file"
// webkitdirectory> returns File objects from the user's own machine with no
// usable absolute path, and the backend is the process that has to open the
// directory. So this browses server-side instead, through
// GET /storage-routes/<id>/browse — which lists directories only and is
// contained by realpath, so a typed ".." cannot walk out of the route.
//
// Creatable rather than a plain select: a folder six levels down is faster to
// type than to click to, and a path that exists but is not offered (a race with
// the filesystem, a permission quirk) must still be usable.

// One request per directory level, cached for the life of the component. Typing
// walks back and forth across the same few levels, and re-fetching each keypress
// would make the control feel broken on a slow disk.
const useDirectoryCache = (routeId) => {
  const cache = useRef({});
  useEffect(() => { cache.current = {}; }, [routeId]);
  return cache;
};

/** 'a/b/c' -> 'a/b'   ·   'a' -> ''   ·   'a/' -> 'a' */
const parentOf = (value) => {
  const i = (value || '').lastIndexOf('/');
  return i === -1 ? '' : value.slice(0, i);
};

const FolderCombo = ({ routeId, value, onChange, disabled, placeholder }) => {
  const [options, setOptions] = useState([]);
  const [loading, setLoading] = useState(false);
  const [typed, setTyped] = useState('');
  const cache = useDirectoryCache(routeId);

  // Which level to offer depends on what has been typed so far: the children of
  // the last complete segment. Typing "2019/su" offers the contents of "2019".
  const level = parentOf(typed || value || '');

  const loadLevel = useCallback(async (path) => {
    if (!routeId) { setOptions([]); return; }
    if (cache.current[path]) { setOptions(cache.current[path]); return; }
    setLoading(true);
    try {
      const res = await storageRouteAPI.browse(routeId, path);
      const dirs = (res.data.directories || []).map((d) => ({
        value: path ? `${path}/${d}` : d,
        label: path ? `${path}/${d}` : d,
      }));
      cache.current[path] = dirs;
      setOptions(dirs);
    } catch {
      // An unreadable level is not an error worth showing here — the route's
      // own availability is reported on the Storage page and on the card.
      setOptions([]);
    } finally {
      setLoading(false);
    }
  }, [routeId, cache]);

  useEffect(() => { loadLevel(level); }, [level, loadLevel]);

  const selected = value ? { value, label: value } : null;

  return (
    <CreatableSelect
      isClearable
      isDisabled={disabled || !routeId}
      isLoading={loading}
      options={options}
      value={selected}
      inputValue={typed}
      onInputChange={(v, meta) => {
        // react-select clears the input on blur and on select; keeping those
        // would reset the level and refetch the root every time.
        if (meta.action === 'input-change') setTyped(v);
      }}
      onChange={(opt) => { onChange(opt ? opt.value : ''); setTyped(''); }}
      placeholder={placeholder || 'Whole route (or type a subfolder)'}
      formatCreateLabel={(v) => `Use "${v}"`}
      noOptionsMessage={() => (routeId ? 'No subfolders here' : 'Pick a route first')}
      menuPortalTarget={typeof document !== 'undefined' ? document.body : null}
      styles={{
        // The card and the modal both clip their content; without a portal and
        // a raised z-index the menu opens behind them.
        menuPortal: (base) => ({ ...base, zIndex: 2000 }),
        control: (base) => ({ ...base, minHeight: 31, fontSize: '0.82rem' }),
        dropdownIndicator: (base) => ({ ...base, padding: 4 }),
        clearIndicator: (base) => ({ ...base, padding: 4 }),
        menu: (base) => ({ ...base, fontSize: '0.82rem' }),
      }}
    />
  );
};

export default FolderCombo;
