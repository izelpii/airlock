import React, { useState, useEffect, useRef } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { catalogAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';

// Routes that do not require a catalog — navigating to them when closing is fine
const CATALOG_FREE_ROUTES = ['/catalogs', '/admin', '/settings'];

const isCatalogDependent = (pathname) =>
  !CATALOG_FREE_ROUTES.some((r) => pathname === r || pathname.startsWith(r + '/'));

const CatalogSelector = () => {
  const { activeCatalog, setActiveCatalog, clearActiveCatalog } = useCatalog();
  const [catalogs, setCatalogs] = useState([]);
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    catalogAPI.getAll()
      .then((res) => {
        const list = res.data || [];
        setCatalogs(list);
        // Reconcile the selection against what actually exists.
        //
        // A catalog deleted and re-created gets a NEW id, and until this the app
        // kept pointing at the old one for the life of the tab: every
        // catalog-scoped call answered 403 (deliberately - the API will not say
        // whether a catalog is missing or merely someone else's), and every page
        // showed its own version of "failed to load". Clearing it here puts the
        // user back on "Select catalog…", which is the truth.
        if (activeCatalog && !list.some((c) => c.id === activeCatalog.id)) {
          clearActiveCatalog();
          if (isCatalogDependent(location.pathname)) navigate('/catalogs');
        }
      })
      .catch(() => {});
    // activeCatalog is deliberately not a dependency: this runs once per mount
    // to validate whatever was restored, and re-running it on every selection
    // would refetch the list each time a catalog is picked.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Close dropdown when clicking outside
  useEffect(() => {
    const handler = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  const handleSelect = (catalog) => {
    setActiveCatalog(catalog);
    setOpen(false);
  };

  const handleClose = () => {
    clearActiveCatalog();
    setOpen(false);
    if (isCatalogDependent(location.pathname)) {
      navigate('/catalogs');
    }
  };

  const label = activeCatalog ? activeCatalog.name : 'Select catalog…';

  return (
    <div ref={ref} style={{ position: 'relative', marginRight: 12 }}>
      <button
        onClick={() => setOpen((o) => !o)}
        style={{
          display: 'flex',
          alignItems: 'center',
          gap: 6,
          padding: '5px 12px',
          border: '1px solid #d0d7de',
          borderRadius: 6,
          background: activeCatalog ? '#f0f6ff' : '#f6f8fa',
          color: activeCatalog ? '#1976d2' : '#57606a',
          fontSize: '0.85rem',
          fontWeight: activeCatalog ? 600 : 400,
          cursor: 'pointer',
          whiteSpace: 'nowrap',
          maxWidth: 220,
          overflow: 'hidden',
          textOverflow: 'ellipsis',
        }}
        title={label}
      >
        <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{label}</span>
        <span style={{ flexShrink: 0, marginLeft: 2, opacity: 0.6, fontSize: '0.7rem' }}>▾</span>
      </button>

      {open && (
        <div
          style={{
            position: 'absolute',
            top: 'calc(100% + 4px)',
            right: 0,
            minWidth: 220,
            background: '#fff',
            border: '1px solid #d0d7de',
            borderRadius: 8,
            boxShadow: '0 8px 24px rgba(0,0,0,0.12)',
            zIndex: 9999,
            overflow: 'hidden',
          }}
        >
          {catalogs.length === 0 && (
            <div style={{ padding: '10px 14px', color: '#888', fontSize: '0.85rem' }}>
              No catalogs available
            </div>
          )}
          {catalogs.map((c) => (
            <button
              key={c.id}
              onClick={() => handleSelect(c)}
              style={{
                display: 'block',
                width: '100%',
                textAlign: 'left',
                padding: '8px 14px',
                border: 'none',
                background: activeCatalog?.id === c.id ? '#e3f2fd' : 'transparent',
                color: activeCatalog?.id === c.id ? '#1976d2' : '#1f2328',
                fontWeight: activeCatalog?.id === c.id ? 600 : 400,
                fontSize: '0.875rem',
                cursor: 'pointer',
              }}
              onMouseEnter={(e) => { e.currentTarget.style.background = '#f0f6ff'; }}
              onMouseLeave={(e) => {
                e.currentTarget.style.background = activeCatalog?.id === c.id ? '#e3f2fd' : 'transparent';
              }}
            >
              {c.name}
            </button>
          ))}
          {/* Divider + close option */}
          <div style={{ borderTop: '1px solid #e5e7eb', margin: '4px 0' }} />
          <button
            onClick={handleClose}
            disabled={!activeCatalog}
            style={{
              display: 'block',
              width: '100%',
              textAlign: 'left',
              padding: '8px 14px',
              border: 'none',
              background: 'transparent',
              color: activeCatalog ? '#d32f2f' : '#aaa',
              fontSize: '0.875rem',
              cursor: activeCatalog ? 'pointer' : 'default',
            }}
            onMouseEnter={(e) => {
              if (activeCatalog) e.currentTarget.style.background = '#fff3f3';
            }}
            onMouseLeave={(e) => { e.currentTarget.style.background = 'transparent'; }}
          >
            Close catalog
          </button>
        </div>
      )}
    </div>
  );
};

export default CatalogSelector;
