import React, { useEffect, useState } from 'react';
import { CCard, CCardHeader, CCardBody, CButton, CFormSelect, CSpinner } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilFork } from '@coreui/icons';
import { eventAPI } from '../services/api';

// ─── Merge this event into another ──────────────────────────────────────────
// Automatic grouping sometimes splits one occasion in two. This moves the
// event's photos into another event and removes it. Only events on the same
// day, the day before or the day after are offered - the backend enforces the
// same rule (app.py, "Merging events").
//
// The event merged INTO keeps its own name, description, cover and locations;
// it becomes Manual so Create Events does not split the photos apart again.

function formatDay(value) {
  if (!value) return '';
  const d = new Date(String(value).replace(' ', 'T'));
  return isNaN(d.getTime())
    ? String(value).slice(0, 10)
    : d.toLocaleDateString(undefined, { weekday: 'short', month: 'short', day: 'numeric' });
}

const EventMergeCard = ({ catalogId, event, onMerged }) => {
  const [candidates, setCandidates] = useState(null);   // null while loading
  const [targetId, setTargetId] = useState('');
  const [merging, setMerging] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!catalogId || !event?.id) return;
    let cancelled = false;
    setCandidates(null);
    setTargetId('');
    setError(null);
    eventAPI.getMergeCandidates(catalogId, event.id)
      .then((res) => { if (!cancelled) setCandidates(res.data || []); })
      .catch(() => { if (!cancelled) { setCandidates([]); setError('Could not load events to merge with.'); } });
    return () => { cancelled = true; };
  }, [catalogId, event?.id]);

  const target = (candidates || []).find((c) => String(c.id) === targetId);

  const merge = async () => {
    if (!target) return;
    const ok = window.confirm(
      `Merge "${event.name}" into "${target.name}"?\n\n`
      + `Its photos move to "${target.name}", which keeps its own name, description, `
      + `cover and locations. "${event.name}" is then deleted.`);
    if (!ok) return;
    setMerging(true);
    setError(null);
    try {
      const res = await eventAPI.merge(catalogId, event.id, target.id);
      onMerged?.(res.data);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not merge the events.');
      setMerging(false);
    }
  };

  return (
    // h-100: it shares the event page's top row, and matches its neighbours' height.
    <CCard className="h-100">
      <CCardHeader className="d-flex align-items-center gap-2">
        <CIcon icon={cilFork} style={{ width: 16, height: 16, color: '#6f42c1' }} />
        <strong style={{ fontSize: '0.95rem' }}>Merge event</strong>
      </CCardHeader>
      <CCardBody className="py-3">
        {candidates === null ? (
          <div className="text-muted" style={{ fontSize: '0.85rem' }}>
            <CSpinner size="sm" className="me-2" />Looking for nearby events…
          </div>
        ) : candidates.length === 0 ? (
          <div className="text-muted" style={{ fontSize: '0.85rem' }}>
            {error || 'No other event on the same day, the day before or the day after.'}
          </div>
        ) : (
          <>
            <div className="text-muted mb-2" style={{ fontSize: '0.8rem' }}>
              Move this event's photos into another event on the same day, the day before or
              the day after. The event you choose keeps its name, description, cover and
              locations; this one is deleted.
            </div>
            <div className="d-flex gap-2 align-items-center flex-wrap">
              <CFormSelect
                size="sm"
                style={{ maxWidth: 520 }}
                value={targetId}
                onChange={(e) => setTargetId(e.target.value)}
                disabled={merging}
                aria-label="Event to merge into"
              >
                <option value="">Choose an event to merge into…</option>
                {candidates.map((c) => (
                  <option key={c.id} value={c.id}>
                    {`${c.relation[0].toUpperCase()}${c.relation.slice(1)} · ${formatDay(c.start_time)} · `
                      + `${c.name} (${c.photos} photo${c.photos === 1 ? '' : 's'})`}
                  </option>
                ))}
              </CFormSelect>
              <CButton size="sm" color="primary" onClick={merge} disabled={!target || merging}>
                {merging ? <CSpinner size="sm" /> : 'Merge into selected'}
              </CButton>
            </div>
            {error && <div className="mt-2" style={{ fontSize: '0.8rem', color: '#c00' }}>{error}</div>}
          </>
        )}
      </CCardBody>
    </CCard>
  );
};

export default EventMergeCard;
