import React, { useState, useEffect } from 'react';
import { CAlert } from '@coreui/react';
import { catalogAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';

// ─── Source availability banner ───────────────────────────────────────────────
// A catalog that indexes files in place (file-ingestion-spec.md) depends on a
// folder this application does not own. When that folder is not there — an
// external drive unmounted, or a host path never mounted into the container —
// the catalog still works for browsing, because every preview and thumbnail
// lives in the catalog folder. Only the full-size originals are missing.
//
// So this is a banner, not an error page: it names what is unreachable and lets
// the user carry on. Section 5.3.

const SourceStatusBanner = () => {
  const { activeCatalog } = useCatalog();
  const [status, setStatus] = useState(null);

  useEffect(() => {
    if (!activeCatalog?.id) { setStatus(null); return; }
    let cancelled = false;
    catalogAPI.sourceStatus(activeCatalog.id)
      .then((res) => { if (!cancelled) setStatus(res.data); })
      // Silent on failure: an unreachable status endpoint is not itself worth a
      // banner, and the pages below have their own error handling.
      .catch(() => { if (!cancelled) setStatus(null); });
    return () => { cancelled = true; };
  }, [activeCatalog?.id]);

  // 'ok' and copy-mode catalogs render nothing at all.
  if (!status || status.state === 'ok' || status.mode !== 'reference') return null;

  const color = status.state === 'unset' ? 'info' : 'warning';

  return (
    <CAlert color={color} className="mb-0 rounded-0 py-2 px-4" style={{ fontSize: '0.85rem' }}>
      <strong>
        {status.state === 'unset' ? 'Source folder not set' : 'Originals unavailable'}
      </strong>
      {' — '}
      {status.message}
      <div className="text-muted mt-1" style={{ fontSize: '0.78rem' }}>
        Browsing, people, events and albums are unaffected — those live in the catalog
        folder. Downloads and face detection need the originals.
      </div>
    </CAlert>
  );
};

export default SourceStatusBanner;
