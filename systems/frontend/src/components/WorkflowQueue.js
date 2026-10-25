import React, { useState, useEffect, useCallback, useRef } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CBadge,
  CButton,
  CSpinner,
  CProgress,
  CProgressBar,
} from '@coreui/react';
import { queueAPI } from '../services/api';
import socketService from '../services/socket';

// ─── Workflow queue panel ─────────────────────────────────────────────────────
// The right-hand rail on the Workflow page. Shows what is waiting, what is
// running, and - the part that earns its place - WHY each item is there.
//
// A user who confirms faces and sees "Face Recognition - 12 x faces confirmed"
// learns the causal rule from the UI. Before this existed, confirming a face
// changed nothing until someone remembered to re-run a workflow by hand, and
// there was no way to discover that from the interface.
//
// See documentation/specs/workflow-scheduling-spec.md section 8.

const STATUS_STYLE = {
  running:   { color: 'primary',   label: 'running' },
  queued:    { color: 'secondary', label: 'queued' },
  blocked:   { color: 'warning',   label: 'waiting' },
  completed: { color: 'success',   label: 'done' },
  failed:    { color: 'danger',    label: 'failed' },
  // Its own colour, not danger: a stop is something the user did and can undo
  // by resuming, not a problem to investigate.
  stopped:   { color: 'warning',   label: 'stopped' },
  cancelled: { color: 'dark',      label: 'cancelled' },
  superseded:{ color: 'dark',      label: 'superseded' },
};

const formatWait = (seconds) => {
  if (seconds >= 3600) {
    const h = Math.floor(seconds / 3600);
    const m = Math.floor((seconds % 3600) / 60);
    return `${h}h ${String(m).padStart(2, '0')}m`;
  }
  if (seconds >= 60) {
    const m = Math.floor(seconds / 60);
    const s = seconds % 60;
    return `${m}m ${String(s).padStart(2, '0')}s`;
  }
  return `${seconds}s`;
};

const QueueItem = ({ item, catalogId, progress, onChanged, isFirstPending }) => {
  const [busy, setBusy] = useState(false);
  // Counted down in the browser from the value the server sent. Ticking this
  // over the socket would mean an event per second per item, to render a number
  // the client can work out on its own.
  const [waiting, setWaiting] = useState(item.waiting_seconds || 0);
  const style = STATUS_STYLE[item.status] || STATUS_STYLE.queued;

  useEffect(() => { setWaiting(item.waiting_seconds || 0); }, [item.waiting_seconds]);

  useEffect(() => {
    if (waiting <= 0) return undefined;
    const t = setInterval(() => setWaiting((w) => Math.max(0, w - 1)), 1000);
    return () => clearInterval(t);
  }, [waiting > 0]);   // eslint-disable-line react-hooks/exhaustive-deps

  const startNow = async () => {
    setBusy(true);
    try {
      await queueAPI.runNow(catalogId, item.id);
      setWaiting(0);
      onChanged();
    } catch (e) {
      onChanged(e?.response?.data?.error || 'Could not start that item.');
    } finally {
      setBusy(false);
    }
  };

  const cancel = async () => {
    setBusy(true);
    try {
      await queueAPI.cancel(catalogId, item.id);
      onChanged();
    } catch {
      /* the socket refresh will correct the list */
    } finally {
      setBusy(false);
    }
  };

  // Stopping a running workflow. Separate from cancel(): that one only removes
  // work that has not begun.
  const stop = async (remove) => {
    if (remove && !window.confirm(
      `Stop "${item.workflow_name}" and remove it from the queue?\n\n` +
      'Anything it has already done stays — files already imported are not ' +
      'removed from the catalog.')) return;
    setBusy(true);
    try {
      await queueAPI.stop(catalogId, item.id, remove);
      onChanged();
    } catch (e) {
      onChanged(e?.response?.data?.error || 'Could not stop that workflow.');
    } finally {
      setBusy(false);
    }
  };

  const resume = async () => {
    setBusy(true);
    try {
      await queueAPI.resumeItem(catalogId, item.id);
      onChanged();
    } catch (e) {
      onChanged(e?.response?.data?.error || 'Could not resume that workflow.');
    } finally {
      setBusy(false);
    }
  };

  const moveUp = async () => {
    setBusy(true);
    try {
      await queueAPI.move(catalogId, item.id, Math.max(0, (item.position || 1) - 2));
      onChanged();
    } catch (e) {
      onChanged(e?.response?.data?.error);
    } finally {
      setBusy(false);
    }
  };

  // THE QUEUE ROW ALREADY CARRIES THE NUMBERS.
  //
  // list_queue LEFT JOINs workflow_executions, so every row arrives with
  // processed_files and total_files on it. This used to read only the
  // socket-delivered `progress` object, which made the bar depend entirely on
  // workflow_progress events: if those did not arrive, no bar ever appeared -
  // even though the counts were sitting in the payload that had just rendered
  // the row.
  //
  // The row is the floor and the socket is the refinement. Sockets make it
  // smooth between queue updates; without them it still moves, one queue
  // refresh at a time, which is what a progress bar is for.
  const rowProcessed = item.processed_files ?? 0;
  const rowTotal = item.total_files ?? 0;
  // Whichever source has seen more. A socket event can arrive before the queue
  // is re-fetched, and a queue refresh can arrive after a missed event.
  const processed = Math.max(progress?.processed ?? 0, rowProcessed);
  const total = Math.max(progress?.total ?? 0, rowTotal);
  const pct = total > 0 ? Math.min(100, Math.round((processed / total) * 100))
                        : (progress?.percentage ?? 0);
  const showBar = item.status === 'running';
  // Derived, not stored: the flag is set but the workflow has not reached its
  // next item boundary yet. After a restart nothing is stopping, because nothing
  // is running.
  const stopping = item.status === 'running' && item.stop_requested;

  return (
    <div style={{
      borderLeft: `3px solid var(--cui-${style.color})`,
      background: item.status === 'running' ? 'rgba(13,110,253,0.04)' : 'transparent',
      padding: '8px 10px',
      marginBottom: 6,
      borderRadius: 4,
    }}>
      <div className="d-flex justify-content-between align-items-start gap-2">
        <div style={{ minWidth: 0, flex: 1 }}>
          <div className="fw-semibold text-truncate" style={{ fontSize: '0.85rem' }}>
            {item.workflow_name}
          </div>
          <div style={{ fontSize: '0.72rem', color: '#888' }}>
            queued by: {item.trigger_label || item.trigger_source}
          </div>
          {waiting > 0 && item.status !== 'running' && (
            <div style={{ fontSize: '0.72rem', color: '#0d6efd' }}>
              {item.trigger_detail?.nightly
                // Tagging queues Match Faces by Appearance for the night: it
                // runs for hours (workflow_triggers.NIGHTLY).
                ? <>runs tonight at {new Date(Date.now() + waiting * 1000)
                  .toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}
                  {' '}(in {formatWait(waiting)}) — or press now</>
                : <>starts in {formatWait(waiting)} — resets while you keep working</>}
            </div>
          )}
          {item.status === 'blocked' && (
            <div style={{ fontSize: '0.72rem', color: '#a97a00' }}>
              waiting for {item.depends_on.length} earlier workflow
              {item.depends_on.length === 1 ? '' : 's'}
            </div>
          )}
          {stopping && (
            <div style={{ fontSize: '0.72rem', color: '#a97a00' }}>
              finishing the current item…
            </div>
          )}
          {item.status === 'stopped' && (
            <div style={{ fontSize: '0.72rem', color: '#a97a00' }}>
              resuming skips what is already done
            </div>
          )}
          {item.error_message && (
            <div style={{
              fontSize: '0.72rem',
              // A stopped run's message explains where it got to; it is not an
              // error and should not be red.
              color: item.status === 'stopped' ? '#888' : '#dc3545',
            }}>
              {item.error_message}
            </div>
          )}
        </div>
        <div className="d-flex flex-column align-items-end gap-1">
          <CBadge color={stopping ? 'warning' : style.color}>
            {stopping ? 'stopping' : style.label}
          </CBadge>
          {(item.status === 'queued' || item.status === 'blocked') && (
            <div className="d-flex gap-1">
              {waiting > 0 && (
                <CButton size="sm" color="link" className="p-0"
                         style={{ fontSize: '0.7rem' }}
                         disabled={busy} onClick={startNow}
                         title="Do not wait for the quiet period">
                  now
                </CButton>
              )}
              {!isFirstPending && (
                <CButton size="sm" color="link" className="p-0"
                         style={{ fontSize: '0.7rem' }}
                         disabled={busy} onClick={moveUp} title="Move up">
                  up
                </CButton>
              )}
              <CButton size="sm" color="link" className="p-0 text-danger"
                       style={{ fontSize: '0.7rem' }}
                       disabled={busy} onClick={cancel} title="Remove from queue">
                {busy ? <CSpinner size="sm" /> : 'cancel'}
              </CButton>
            </div>
          )}
          {/* Only offered where it can actually be honoured. item.stoppable is
              false for the workflows that rebuild their results in one pass and
              for anything that runs in-process — showing a control and then
              rejecting it would be worse than not showing one. */}
          {item.status === 'running' && item.stoppable && !stopping && (
            // Real buttons rather than the text links the other rows use:
            // stopping a workflow that is under way is a decision, and it should
            // look like one. Allowed to wrap — the rail is narrow and "Stop and
            // remove" is long, and a wrapped pair reads better than two buttons
            // squeezed past legibility.
            <div className="d-flex flex-wrap gap-1 justify-content-end">
              <CButton size="sm" color="secondary" variant="outline"
                       style={{ fontSize: '0.7rem', padding: '2px 8px' }}
                       disabled={busy} onClick={() => stop(false)}
                       title="Stop after the current item; run it again to continue">
                {busy ? <CSpinner size="sm" /> : 'Stop'}
              </CButton>
              <CButton size="sm" color="danger" variant="outline"
                       style={{ fontSize: '0.7rem', padding: '2px 8px' }}
                       disabled={busy} onClick={() => stop(true)}
                       title="Stop and remove from the queue">
                Stop and remove
              </CButton>
            </div>
          )}
          {item.status === 'stopped' && (
            <div className="d-flex gap-1">
              <CButton size="sm" color="link" className="p-0"
                       style={{ fontSize: '0.7rem' }}
                       disabled={busy} onClick={resume}
                       title="Queue it again; work already done is skipped">
                {busy ? <CSpinner size="sm" /> : 'resume'}
              </CButton>
              {/* A stopped row stays on screen to carry the resume link, so it
                  also needs a way to say "no, leave it" and clear itself. */}
              <CButton size="sm" color="link" className="p-0 text-muted"
                       style={{ fontSize: '0.7rem' }}
                       disabled={busy} onClick={cancel} title="Clear this from the queue">
                dismiss
              </CButton>
            </div>
          )}
        </div>
      </div>

      {showBar && (
        <CProgress height={10} className="mt-2">
          <CProgressBar value={pct} color="primary">
            {pct > 12 ? `${pct}%` : ''}
          </CProgressBar>
        </CProgress>
      )}
      {showBar && total > 0 && (
        <div style={{ fontSize: '0.7rem', color: '#888', marginTop: 2 }}>
          {processed} / {total}
        </div>
      )}
    </div>
  );
};

const WorkflowQueue = ({ catalogId }) => {
  const [state, setState] = useState({ queue: [], paused: false,
                                       running_count: 0, queued_count: 0 });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [progress, setProgress] = useState({});
  const catalogRef = useRef(catalogId);
  catalogRef.current = catalogId;

  const load = useCallback(async (message) => {
    if (message) setError(message);
    if (!catalogId) return;
    try {
      const r = await queueAPI.get(catalogId);
      setState(r.data);
      if (!message) setError(null);
    } catch (e) {
      setError('Could not load the queue.');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);

  // The server pushes the whole queue on every change, so replace rather than
  // reconcile. Progress arrives separately, per execution, on workflow_progress.
  useEffect(() => {
    socketService.connect();
    const offQueue = socketService.onQueueUpdated((data) => {
      if (!data || data.catalog_id !== catalogRef.current) return;
      if (data.queue) setState(data);
      else load();
    });
    const offProgress = socketService.onWorkflowProgress((data) => {
      if (!data?.execution_id) return;
      setProgress((prev) => ({ ...prev, [data.execution_id]: data }));
    });
    return () => { offQueue(); offProgress(); };
  }, [load]);

  // A SLOW POLL, so the queue is never wrong for long.
  //
  // Sockets are the fast path, not the only path. When they do not arrive - a
  // proxy that will not upgrade, a certificate the browser distrusts, a network
  // that drops long polls - the list simply froze: a workflow would start,
  // finish and disappear between two socket events that never came, and the
  // page showed "Nothing queued" throughout. The data was one HTTP call away
  // the whole time.
  //
  // Five seconds is cheap - one small JSON payload - and it means the worst a
  // dead socket can do is make the UI five seconds late instead of blank.
  useEffect(() => {
    const id = setInterval(load, 5000);
    return () => clearInterval(id);
  }, [load]);

  const togglePause = async () => {
    try {
      if (state.paused) await queueAPI.resume(catalogId);
      else await queueAPI.pause(catalogId);
      load();
    } catch {
      setError('Could not change the queue state.');
    }
  };

  // 'running' rows are shown separately above the pending list; a stopped row
  // sits with the pending ones so its resume link is where the user is looking.
  const pending = state.queue.filter((i) => i.status !== 'running');
  const firstPendingId = pending.length ? pending[0].id : null;

  return (
    <CCard className="mb-3">
      <CCardHeader className="d-flex justify-content-between align-items-center py-2">
        <div>
          <strong style={{ fontSize: '0.9rem' }}>Queue</strong>
          {state.running_count > 0 && (
            <CBadge color="primary" className="ms-2">{state.running_count} running</CBadge>
          )}
          {state.queued_count > 0 && (
            <CBadge color="secondary" className="ms-1">{state.queued_count} waiting</CBadge>
          )}
        </div>
        <CButton size="sm" color={state.paused ? 'warning' : 'secondary'}
                 variant={state.paused ? undefined : 'outline'}
                 onClick={togglePause}>
          {state.paused ? 'Resume' : 'Pause'}
        </CButton>
      </CCardHeader>
      <CCardBody className="py-2" style={{ maxHeight: '70vh', overflowY: 'auto' }}>
        {!state.paused && state.queue.some((i) => (i.waiting_seconds || 0) > 0
                                                && !i.trigger_detail?.nightly) && (
          <div style={{ fontSize: '0.75rem', color: '#0d6efd', marginBottom: 8 }}>
            Waiting for you to finish. Each change pushes the start time back, so
            a tagging session runs the chain once at the end rather than after
            every photo.
          </div>
        )}
        {state.paused && (
          <div style={{ fontSize: '0.75rem', color: '#a97a00', marginBottom: 8 }}>
            Paused. Workflows already running will finish; nothing new will start.
          </div>
        )}
        {error && (
          <div style={{ fontSize: '0.75rem', color: '#dc3545', marginBottom: 8 }}>{error}</div>
        )}

        {loading ? (
          <div className="text-center py-3"><CSpinner size="sm" /></div>
        ) : state.queue.length === 0 ? (
          <div style={{ fontSize: '0.8rem', color: '#888', padding: '12px 4px' }}>
            Nothing queued. Workflows are added here automatically after you import
            photos or confirm faces, and you can queue one by hand from its card.
          </div>
        ) : (
          state.queue.map((item) => (
            <QueueItem
              key={item.id}
              item={item}
              catalogId={catalogId}
              progress={progress[item.execution_id]}
              isFirstPending={item.id === firstPendingId}
              onChanged={load}
            />
          ))
        )}
      </CCardBody>
    </CCard>
  );
};

export default WorkflowQueue;
