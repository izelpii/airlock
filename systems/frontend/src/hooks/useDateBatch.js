import { useEffect, useRef, useState } from 'react';
import { photoAPI } from '../services/api';
import socketService from '../services/socket';

// ─── Follow one date batch ────────────────────────────────────────────────────
// saved-search-tools-spec.md section 5.5, the way the event page follows its
// batch (mass-edit-event-photo-dates-spec.md section 4.5): Socket.IO for prompt
// progress, and a 2 s poll as well, because a missed socket event must not
// leave a banner stuck. Returns the batch row as last read, or null.

export const isTerminal = (st) => st === 'completed' || st === 'interrupted' || st === 'failed';

export default function useDateBatch(catalogId, batchId, onFinished) {
  const [batch, setBatch] = useState(null);
  const onFinishedRef = useRef(onFinished);
  onFinishedRef.current = onFinished;

  useEffect(() => {
    setBatch(null);
    if (!batchId || !catalogId) return undefined;
    let stopped = false;
    let finished = false;
    let timer = null;
    const settle = (b) => {
      if (stopped || !b) return;
      setBatch(b);
      if (isTerminal(b.status) && !finished) {
        finished = true;
        clearInterval(timer);
        if (onFinishedRef.current) onFinishedRef.current(b);
      }
    };
    const fetchBatch = () => photoAPI.getDateBatch(catalogId, batchId)
      .then((r) => settle(r.data))
      .catch(() => {});
    fetchBatch();
    timer = setInterval(fetchBatch, 2000);
    socketService.connect();
    const off = socketService.onDateBatchProgress((d) => {
      if (String(d.batch_id) !== String(batchId)) return;
      if (d.catalog_id != null && String(d.catalog_id) !== String(catalogId)) return;
      setBatch((prev) => ({ ...(prev || {}), done: d.done, total: d.total, status: d.status }));
      // The socket carries no result; the row does.
      if (isTerminal(d.status)) fetchBatch();
    });
    return () => {
      stopped = true;
      clearInterval(timer);
      off();
    };
  }, [catalogId, batchId]); // eslint-disable-line react-hooks/exhaustive-deps

  return batch;
}
