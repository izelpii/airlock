import React, { useState, useEffect, useCallback } from 'react';
import { CBadge, CButton, CSpinner, CAlert } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilChevronLeft, cilChevronRight } from '@coreui/icons';
import { workflowAPI } from '../services/api';

// ─── Ingestion runs, as a row of cards ────────────────────────────────────────
// The top row of the Workflow Executions report: what each import run actually
// did, in four numbers. Read-only - there is nothing to click on a card, which
// is why it has none of the flip or edit machinery the People cards carry while
// keeping their shape, border and header.
//
// ONE ROW, NEWEST LEFT. A catalog accumulates import runs (twelve on the
// family catalog after one week), and a report that wrapped them into six rows
// of cards would bury the one that matters - the last one. So the row is a
// fixed page and the control steps backwards through time.

const CARD_WIDTH = 290;          // the People cards' width
const PAGE_SIZE = 4;

// What each number means, in the import's own vocabulary
// (io_picture_import_workflow.summary). The tooltips are not decoration: "new"
// and "skipped" and "duplicates" are each one plausible reading of the others,
// and the card is too small to spell them out in the label.
const FIGURES = [
  { key: 'files_analysed', label: 'Analysed', color: '#212121',
    title: 'Every file this run looked at' },
  { key: 'files_new', label: 'New', color: '#2e7d32',
    title: 'Added to the catalog' },
  { key: 'files_duplicate', label: 'Duplicates', color: '#ef6c00',
    title: 'Content the catalog already had' },
  { key: 'files_ignored', label: 'Skipped', color: '#757575',
    title: 'Not photos or videos - sidecars, operating-system files' },
];

const STATUS_COLOR = {
  completed: 'success', failed: 'danger', running: 'info',
  stopped: 'secondary', interrupted: 'warning',
};

const formatWhen = (value) => {
  if (!value) return '—';
  const d = new Date(String(value).includes('T') ? value : String(value).replace(' ', 'T'));
  if (Number.isNaN(d.getTime())) return value;
  return d.toLocaleString('en-US', { month: 'short', day: 'numeric',
                                     hour: '2-digit', minute: '2-digit' });
};

const count = (n) => (n == null ? '—' : Number(n).toLocaleString());

const IngestionCard = ({ run }) => (
  <div style={{
    flexShrink: 0, width: CARD_WIDTH, background: '#fff',
    border: '1px solid #e0e0e0', borderRadius: 10, overflow: 'hidden',
    display: 'flex', flexDirection: 'column',
  }}>
    <div style={{
      display: 'flex', alignItems: 'center', gap: 6, padding: '8px 10px',
      borderBottom: '1px solid #eee',
    }}>
      <div style={{
        fontWeight: 600, fontSize: '0.86rem', whiteSpace: 'nowrap',
        overflow: 'hidden', textOverflow: 'ellipsis', flex: 1, minWidth: 0,
      }} title={run.workflow_name}>
        {run.workflow_name}
      </div>
      <CBadge color={STATUS_COLOR[run.status] || 'light'}
              style={{ fontSize: '0.68rem', flexShrink: 0 }}>
        {run.status}
      </CBadge>
    </div>

    <div style={{ padding: '6px 10px', fontSize: '0.74rem', color: '#888' }}>
      {formatWhen(run.started_at)}
      {run.completed_at && <> → {formatWhen(run.completed_at)}</>}
    </div>

    {/* Two by two rather than a row of four: at 290px four numbers in a line
        would each get 70px, and these run to six figures. */}
    <div style={{
      display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 1,
      background: '#eee', borderTop: '1px solid #eee',
    }}>
      {FIGURES.map(({ key, label, color, title }) => (
        <div key={key} title={title}
             style={{ background: '#fff', padding: '10px' }}>
          <div style={{ fontSize: '1.15rem', fontWeight: 600, color,
                        lineHeight: 1.1 }}>
            {count(run[key])}
          </div>
          <div style={{ fontSize: '0.7rem', color: '#888', marginTop: 2 }}>
            {label}
          </div>
        </div>
      ))}
    </div>

    {/* Said on the card, not in a footnote: these numbers were counted from
        the run's item rows rather than read from what it recorded, and
        retention deletes those rows - so an old run can read low or zero and
        the card should not pass that off as fact. */}
    {run.counts_from === 'items' && (
      <div style={{ padding: '6px 10px', fontSize: '0.68rem', color: '#b26500',
                    background: '#fff8e1', borderTop: '1px solid #eee' }}
           title="This run finished before the counts were recorded, so they are
                  counted from its remaining item rows. Retention removes those
                  in time, after which the figures read low.">
        counted from items, may be incomplete
      </div>
    )}
  </div>
);

const IngestionCards = ({ catalogId }) => {
  const [state, setState] = useState({ total: 0, executions: [] });
  const [offset, setOffset] = useState(0);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  const load = useCallback(async () => {
    // setLoading(false), not a bare return: `loading` starts true, so leaving
    // it alone here would spin forever on a page with no catalog open.
    if (!catalogId) { setLoading(false); return; }
    setLoading(true);
    try {
      const res = await workflowAPI.getIngestions(catalogId,
        { limit: PAGE_SIZE, offset });
      setState(res.data || { total: 0, executions: [] });
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load the ingestion runs.');
    } finally {
      setLoading(false);
    }
  }, [catalogId, offset]);

  useEffect(() => { load(); }, [load]);

  const { total, executions } = state;
  const shown = executions.length;
  const first = total === 0 ? 0 : offset + 1;

  if (loading && !shown) {
    return <div className="text-center py-4"><CSpinner size="sm" color="primary" /></div>;
  }
  if (error) {
    return <CAlert color="danger" style={{ fontSize: '0.85rem' }}>{error}</CAlert>;
  }
  if (!total) {
    return (
      <div className="text-muted py-3" style={{ fontSize: '0.85rem' }}>
        No ingestion runs yet. Importing files will add one here.
      </div>
    );
  }

  return (
    <>
      <div className="d-flex align-items-center justify-content-between mb-2">
        <div style={{ fontSize: '0.8rem', color: '#888' }}>
          {/* Said plainly, because "1-4 of 12" with the newest on the left
              reads backwards until you know which way time runs here. */}
          Showing {first}–{offset + shown} of {total}, most recent first
        </div>
        {total > PAGE_SIZE && (
          <div className="d-flex gap-1">
            <CButton color="secondary" variant="outline" size="sm"
                     disabled={offset === 0 || loading}
                     title="More recent"
                     onClick={() => setOffset((o) => Math.max(0, o - PAGE_SIZE))}>
              <CIcon icon={cilChevronLeft} />
            </CButton>
            <CButton color="secondary" variant="outline" size="sm"
                     disabled={offset + shown >= total || loading}
                     title="Older"
                     onClick={() => setOffset((o) => o + PAGE_SIZE)}>
              <CIcon icon={cilChevronRight} />
            </CButton>
          </div>
        )}
      </div>

      {/* nowrap with overflow-x: the row must stay a row. Below about 1250px
          four 290px cards do not fit, and scrolling the strip is the honest
          answer - wrapping would make it two rows, which is the thing this
          layout exists to avoid. */}
      <div style={{ display: 'flex', gap: '1rem', flexWrap: 'nowrap',
                    overflowX: 'auto', paddingBottom: 4, opacity: loading ? 0.6 : 1 }}>
        {executions.map((run) => <IngestionCard key={run.id} run={run} />)}
      </div>
    </>
  );
};

export default IngestionCards;
