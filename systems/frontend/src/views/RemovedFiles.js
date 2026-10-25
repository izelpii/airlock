import React, { useState, useEffect, useCallback } from 'react';
import {
  CContainer, CCard, CCardBody, CButton, CAlert, CSpinner, CBadge,
  CTable, CTableHead, CTableRow, CTableHeaderCell, CTableBody, CTableDataCell,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilActionUndo, cilTrash, cilReload } from '@coreui/icons';
import { removedFilesAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';

// ─── Removed files ────────────────────────────────────────────────────────────
// Photos and videos removed from the catalog (the File card on Photos and
// Videos). Removal leaves the original where it is, so this is the to-do list of
// originals to delete by hand - and, once they are gone, the place to purge
// them from the catalog for good.
//
//   Still on disk  delete it by hand, or Restore it
//   Gone           Purge: the catalog forgets the file entirely

function formatBytes(bytes) {
  if (!bytes) return '—';
  const mb = bytes / (1024 * 1024);
  return mb < 1 ? `${(bytes / 1024).toFixed(0)} KB` : `${mb.toFixed(1)} MB`;
}

function formatWhen(value) {
  if (!value) return '—';
  // removed_at is SQLite CURRENT_TIMESTAMP: UTC, with no zone marker.
  const d = new Date(`${value.replace(' ', 'T')}Z`);
  return isNaN(d.getTime()) ? value : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' });
}

const RemovedFiles = () => {
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;
  // Purge is owner-only on the server (auth._OWNER_RULE_FRAGMENTS).
  const isOwner = !activeCatalog?.role || activeCatalog.role === 'owner';

  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busyId, setBusyId] = useState(null);     // row being restored/purged, or 'all'
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const load = useCallback(async () => {
    if (!catalogId) return;
    setLoading(true);
    try {
      const res = await removedFilesAPI.getAll(catalogId);
      setData(res.data);
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load removed files.');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  const restore = async (file) => {
    setBusyId(file.id);
    try {
      await removedFilesAPI.restore(catalogId, file.id);
      setNotice(`Restored ${file.file_name}. Its sizes and thumbnails are being rebuilt; `
        + 'run event extraction to place it in an event again.');
      await load();
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not restore it.');
    } finally {
      setBusyId(null);
    }
  };

  const purge = async (fileIds) => {
    const what = fileIds ? 'this file' : 'every removed file that is no longer on disk';
    if (!window.confirm(`Purge ${what} from the catalog? The catalog forgets it completely, `
      + 'and importing the same file again would bring it back as new.')) return;
    setBusyId(fileIds ? fileIds[0] : 'all');
    try {
      const res = await removedFilesAPI.purge(catalogId, fileIds);
      const n = res.data.purged.length;
      const kept = res.data.still_on_disk.length;
      setNotice(`Purged ${n} file${n === 1 ? '' : 's'} from the catalog.`
        + (kept ? ` ${kept} still on disk and left alone.` : ''));
      await load();
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not purge.');
    } finally {
      setBusyId(null);
    }
  };

  if (!activeCatalog) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="info">Select a catalog to see its removed files.</CAlert>
      </CContainer>
    );
  }

  const files = data?.files ?? [];
  const gone = files.filter((f) => !f.on_disk).length;

  return (
    <CContainer fluid className="px-4 py-4">
      <div className="d-flex justify-content-between align-items-center mb-3">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Removed files</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            {data
              ? `${data.total} removed from ${activeCatalog.name} · ${data.on_disk} still on disk`
              : 'Loading…'}
          </div>
        </div>
        <div className="d-flex gap-2">
          <CButton color="secondary" variant="outline" size="sm" onClick={load} disabled={loading}>
            <CIcon icon={cilReload} className="me-1" />Refresh
          </CButton>
          <CButton color="danger" size="sm" onClick={() => purge(null)}
                   disabled={!isOwner || gone === 0 || busyId !== null}
                   title={isOwner ? 'Purge every removed file whose original is gone'
                     : 'Only the catalog owner can purge'}>
            {busyId === 'all' ? <CSpinner size="sm" />
              : <><CIcon icon={cilTrash} className="me-1" />Purge all missing ({gone})</>}
          </CButton>
        </div>
      </div>

      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}
      {notice && <CAlert color="success" dismissible onClose={() => setNotice(null)}>{notice}</CAlert>}

      <CAlert color="light" className="py-2" style={{ fontSize: '0.85rem' }}>
        {data?.ingest_mode === 'reference'
          ? 'This catalog indexes your own library in place: the paths below are in that library. '
          : 'The paths below are inside the catalog directory. '}
        Delete a <strong>Still on disk</strong> file by hand, then Refresh - it becomes{' '}
        <strong>Gone</strong> and can be purged.
      </CAlert>

      <CCard>
        <CCardBody className="p-0">
          {loading && !data ? (
            <div className="text-center py-5"><CSpinner color="primary" /></div>
          ) : files.length === 0 ? (
            <div className="text-center py-5 text-muted" style={{ fontSize: '0.9rem' }}>
              Nothing has been removed from this catalog.
            </div>
          ) : (
            <CTable hover responsive className="mb-0" style={{ fontSize: '0.85rem' }}>
              <CTableHead>
                <CTableRow>
                  <CTableHeaderCell>File</CTableHeaderCell>
                  <CTableHeaderCell>Path</CTableHeaderCell>
                  <CTableHeaderCell>Size</CTableHeaderCell>
                  <CTableHeaderCell>Removed</CTableHeaderCell>
                  <CTableHeaderCell>Status</CTableHeaderCell>
                  <CTableHeaderCell />
                </CTableRow>
              </CTableHead>
              <CTableBody>
                {files.map((f) => (
                  <CTableRow key={f.id}>
                    <CTableDataCell style={{ wordBreak: 'break-all' }}>
                      {f.file_name}
                      <CBadge color="light" className="text-dark ms-2">{f.kind}</CBadge>
                    </CTableDataCell>
                    <CTableDataCell style={{ wordBreak: 'break-all', color: '#666' }}
                                    title={f.absolute_path}>
                      {f.file_path}
                    </CTableDataCell>
                    <CTableDataCell style={{ whiteSpace: 'nowrap' }}>{formatBytes(f.file_size)}</CTableDataCell>
                    <CTableDataCell style={{ whiteSpace: 'nowrap' }}>{formatWhen(f.removed_at)}</CTableDataCell>
                    <CTableDataCell>
                      {f.on_disk
                        ? <CBadge color="warning" className="text-dark">Still on disk</CBadge>
                        : <CBadge color="secondary">Gone</CBadge>}
                    </CTableDataCell>
                    <CTableDataCell className="text-end" style={{ whiteSpace: 'nowrap' }}>
                      {f.on_disk ? (
                        <CButton size="sm" color="secondary" variant="outline"
                                 onClick={() => restore(f)} disabled={busyId !== null}>
                          {busyId === f.id ? <CSpinner size="sm" />
                            : <><CIcon icon={cilActionUndo} className="me-1" />Restore</>}
                        </CButton>
                      ) : (
                        <CButton size="sm" color="danger" variant="outline"
                                 onClick={() => purge([f.id])}
                                 disabled={!isOwner || busyId !== null}
                                 title={isOwner ? undefined : 'Only the catalog owner can purge'}>
                          {busyId === f.id ? <CSpinner size="sm" />
                            : <><CIcon icon={cilTrash} className="me-1" />Purge</>}
                        </CButton>
                      )}
                    </CTableDataCell>
                  </CTableRow>
                ))}
              </CTableBody>
            </CTable>
          )}
        </CCardBody>
      </CCard>
    </CContainer>
  );
};

export default RemovedFiles;
