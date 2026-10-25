import React, { useState, useEffect, useCallback } from 'react';
import {
  CCard, CCardBody, CCardHeader, CBadge, CFormSelect, CSpinner, CTable, CTableHead,
  CTableRow, CTableHeaderCell, CTableBody, CTableDataCell, CButton,
} from '@coreui/react';
import { catalogAPI } from '../services/api';

// ─── "Left in the source" (ingest-move-spec.md section 7.3) ──────────────────
// A catalog in move mode empties the folders it imports from, so after a run
// the source holds only what was NOT imported. This is that list, for one run:
// duplicates set aside in DUPS/, files that are not photos or videos (left
// where they were), files the catalog holds but whose source was not deleted
// yet, and failures. It is the list a person goes through by hand.

const KIND = {
  duplicate_moved: { label: 'In DUPS/', color: 'warning' },
  left_in_place: { label: 'Left in place', color: 'secondary' },
  move: { label: 'Not deleted', color: 'info' },
  failed: { label: 'Failed', color: 'danger' },
  // The source is gone and the catalog copy is missing or does not match.
  // Re-checked by every import, so it clears if the copy turns up again.
  missing: { label: 'Missing from both places', color: 'danger' },
};

const PAGE = 25;

const formatSize = (bytes) => {
  if (!bytes) return '';
  const k = 1024;
  const units = ['B', 'KB', 'MB', 'GB'];
  const i = Math.min(units.length - 1, Math.floor(Math.log(bytes) / Math.log(k)));
  return `${Math.round((bytes / k ** i) * 10) / 10} ${units[i]}`;
};

const kindOf = (f) => (f.status === 'failed' ? KIND.failed
  : f.status === 'missing' ? KIND.missing : KIND[f.operation_type]);

const LeftInSource = ({ catalogId }) => {
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [shown, setShown] = useState(PAGE);

  const load = useCallback(async (executionId) => {
    setLoading(true);
    try {
      const r = await catalogAPI.leftInSource(catalogId, executionId);
      setData(r.data);
      setShown(PAGE);
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load what was left in the source.');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  if (loading && !data) {
    return <div className="py-3"><CSpinner size="sm" color="primary" /></div>;
  }
  if (error) return <div className="text-danger small mb-3">{error}</div>;
  if (!data || data.ingest_mode !== 'move') return null;

  const run = data.runs.find((r) => r.execution_id === data.execution_id);
  const files = data.files || [];

  return (
    <CCard className="mb-4">
      <CCardHeader className="d-flex justify-content-between align-items-center flex-wrap gap-2">
        <strong>Left in the source</strong>
        {data.runs.length > 0 && (
          <CFormSelect
            size="sm" style={{ maxWidth: 360 }}
            value={data.execution_id || ''}
            onChange={(e) => load(Number(e.target.value))}
            aria-label="Import run"
          >
            {data.runs.map((r) => (
              <option key={r.execution_id} value={r.execution_id}>
                Run {r.execution_id}
                {r.started ? ` · ${String(r.started).slice(0, 16).replace('T', ' ')}` : ''}
                {` · ${r.moved || 0} moved`}
              </option>
            ))}
          </CFormSelect>
        )}
      </CCardHeader>
      <CCardBody>
        {data.missing_total > 0 && (
          <div className="alert alert-danger py-2 small">
            <strong>{data.missing_total} file{data.missing_total === 1 ? ' is' : 's are'} missing
            from both places</strong> - the source was deleted and the catalog copy is missing
            or does not match. Check that the catalog&apos;s Images, Videos and Others folders
            are mounted where they should be; the next import re-checks them.
          </div>
        )}
        {!run ? (
          <div className="text-muted small">
            No import has moved files into this catalog yet. After one does, what it did
            not take - duplicates gathered in <code>DUPS/</code> and anything that is not a
            photo or video - is listed here.
          </div>
        ) : (
          <>
            {run.source_directory && (
              <div className="small text-muted mb-2">
                From <code>{run.source_directory}</code>
              </div>
            )}
            <div className="d-flex flex-wrap gap-3 small mb-3">
              <span><strong>{run.moved || 0}</strong> moved into the catalog</span>
              <span><strong>{run.duplicates || 0}</strong> duplicates in DUPS/</span>
              <span><strong>{run.left_in_place || 0}</strong> left in place</span>
              {run.pending_delete > 0 && (
                <span className="text-info">
                  <strong>{run.pending_delete}</strong> in the catalog, source not yet deleted
                </span>
              )}
              {run.failed > 0 && (
                <span className="text-danger"><strong>{run.failed}</strong> failed</span>
              )}
              {run.missing > 0 && (
                <span className="text-danger">
                  <strong>{run.missing}</strong> missing from both places
                </span>
              )}
            </div>
            {files.length === 0 ? (
              <div className="text-muted small">Nothing was left behind by this run.</div>
            ) : (
              <>
                <CTable small hover responsive>
                  <CTableHead>
                    <CTableRow>
                      <CTableHeaderCell style={{ width: 120 }}>What</CTableHeaderCell>
                      <CTableHeaderCell>Where it is now</CTableHeaderCell>
                      <CTableHeaderCell>Why</CTableHeaderCell>
                      <CTableHeaderCell style={{ width: 90 }}>Size</CTableHeaderCell>
                    </CTableRow>
                  </CTableHead>
                  <CTableBody>
                    {files.slice(0, shown).map((f) => {
                      const k = kindOf(f) || { label: f.operation_type, color: 'light' };
                      // A duplicate's place is now in DUPS/; everything else is
                      // still at its original path.
                      const where = f.operation_type === 'duplicate_moved' && f.target_path
                        ? f.target_path : f.source_path;
                      return (
                        <CTableRow key={f.id}>
                          <CTableDataCell><CBadge color={k.color}>{k.label}</CBadge></CTableDataCell>
                          <CTableDataCell style={{ wordBreak: 'break-all' }}>
                            <small>{where}</small>
                          </CTableDataCell>
                          <CTableDataCell><small>{(f.details || '').split('; SHA256')[0]}</small></CTableDataCell>
                          <CTableDataCell><small>{formatSize(f.file_size)}</small></CTableDataCell>
                        </CTableRow>
                      );
                    })}
                  </CTableBody>
                </CTable>
                {files.length > shown && (
                  <CButton color="secondary" variant="outline" size="sm"
                           onClick={() => setShown(shown + PAGE * 4)}>
                    Show more ({files.length - shown} left)
                  </CButton>
                )}
              </>
            )}
          </>
        )}
      </CCardBody>
    </CCard>
  );
};

export default LeftInSource;
