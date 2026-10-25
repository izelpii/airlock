import React, { useState, useEffect, useCallback, useMemo, useRef } from 'react';
import { CSpinner, CAlert, CButton } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilZoomIn, cilZoomOut, cilFullscreen, cilMediaPlay } from '@coreui/icons';
import { useNavigate } from 'react-router-dom';
import { workflowAPI, backupAPI } from '../services/api';
import { STATES as BACKUP_STATES, typeOf, scheduleText, when, ago } from './BackupCards';

// ─── The pipeline, drawn ──────────────────────────────────────────────────────
// An alternative to the workflow card list: the same workflows, arranged by the
// order they actually run in.
//
// The layout is NOT decided here. The backend projects workflow_triggers'
// DEPENDS_ON - the map the queue itself schedules from - onto this catalog's
// workflows and returns a layer per node. So the picture cannot drift from the
// behaviour: change the dependency map and the drawing follows.
//
// The catalog's BACKUPS are drawn here too, in a row of their own under the
// pipeline and connected to nothing - because nothing is: a backup is configured
// on the Catalogs page and run by the backend's own backup scheduler, never by
// the queue, and no workflow waits for one. Drawing them as cards of the same
// shape answers "what runs when" in one picture without pretending they are
// workflows; the dashed border and the missing queue and remove actions say
// which kind of card is which.
//
// Drawn in plain SVG rather than with a graph library. The graph is a dozen
// nodes in half a dozen layers, the layout arrives already computed, and a
// library would bring its own styling to argue with the rest of the app for
// something a transform and a few beziers do.

const NODE_W = 208;
const NODE_H = 78;   // name, type, and the action row along the bottom
const GAP_X = 96;      // between layers
const GAP_Y = 26;      // between nodes in a layer
const PAD = 32;

const STATUS = {
  running: { fill: '#e7f1ff', stroke: '#0d6efd', dot: '#0d6efd', label: 'running' },
  queued:  { fill: '#f8f9fa', stroke: '#6c757d', dot: '#6c757d', label: 'queued' },
  blocked: { fill: '#fff8e1', stroke: '#f0ad4e', dot: '#f0ad4e', label: 'waiting' },
};
const IDLE = { fill: '#ffffff', stroke: '#dee2e6', dot: '#ced4da' };
const UNCONFIGURED = { fill: '#fff5f5', stroke: '#e9a2a2', dot: '#c62828' };

const LEGEND = [
  ['running', '#0d6efd'],
  ['waiting', '#f0ad4e'],
  ['queued', '#6c757d'],
  ['needs a source', '#c62828'],
];

const TYPE_TINT = {
  Import: '#0d6efd',
  'Create Events': '#6f42c1',
  'AI / ML': '#20c997',
};

// Backups: their own stripe colour, and a row of their own under the pipeline.
const BACKUP_TINT = '#795548';
const LANE_GAP = 56;     // pipeline to the Backups row, room for its label
const BACKUP_GAP = 26;   // between backup cards in the row

// A backup's card colours, from its state on the backup card (BackupCards
// STATES): blue while it runs, red when it failed, white otherwise. The dot
// carries the state's own colour, so "backup needed" is the same orange here
// as on the Catalogs page.
const backupStyle = (state) => {
  const dot = (BACKUP_STATES[state] || BACKUP_STATES.needed).color;
  if (state === 'running') return { ...STATUS.running, dot };
  if (state === 'failed') return { ...UNCONFIGURED, dot };
  return { ...IDLE, dot };
};

/** Places every node: x from its layer, y stacked within it; backups in a row below. */
function layout(nodes, backups = []) {
  const byLayer = new Map();
  nodes.forEach((n) => {
    const l = n.layer || 0;
    if (!byLayer.has(l)) byLayer.set(l, []);
    byLayer.get(l).push(n);
  });
  const layers = [...byLayer.keys()].sort((a, b) => a - b);
  const tallest = Math.max(...layers.map((l) => byLayer.get(l).length), 1);
  const height = tallest * NODE_H + (tallest - 1) * GAP_Y;

  const placed = new Map();
  layers.forEach((l, col) => {
    const group = byLayer.get(l);
    const groupH = group.length * NODE_H + (group.length - 1) * GAP_Y;
    // Centred in its column, so a short layer sits opposite the middle of a
    // long one instead of hugging the top.
    const top = PAD + (height - groupH) / 2;
    group.forEach((n, row) => {
      placed.set(n.id, {
        ...n,
        x: PAD + col * (NODE_W + GAP_X),
        y: top + row * (NODE_H + GAP_Y),
      });
    });
  });

  let width = PAD * 2 + layers.length * NODE_W + Math.max(0, layers.length - 1) * GAP_X;
  let total = (nodes.length ? height : 0) + PAD * 2;
  let lane = null;
  if (backups.length) {
    const top = nodes.length ? PAD + height + LANE_GAP : PAD + 24;
    backups.forEach((b, i) => {
      const id = `backup-${b.id}`;
      placed.set(id, {
        ...b, id, backupId: b.id, kind: 'backup',
        x: PAD + i * (NODE_W + BACKUP_GAP), y: top,
      });
    });
    width = Math.max(width,
      PAD * 2 + backups.length * NODE_W + (backups.length - 1) * BACKUP_GAP);
    lane = { y: top - 30 };
    total = top + NODE_H + PAD;
  }

  return { placed, width, height: total, lane };
}

/** A horizontal bezier from the right edge of one node to the left of another. */
function edgePath(a, b) {
  const x1 = a.x + NODE_W;
  const y1 = a.y + NODE_H / 2;
  const x2 = b.x;
  const y2 = b.y + NODE_H / 2;
  const dx = Math.max(40, (x2 - x1) / 2);
  return `M ${x1} ${y1} C ${x1 + dx} ${y1}, ${x2 - dx} ${y2}, ${x2} ${y2}`;
}

// An action on a node: the glyph only, with a soft disc appearing under it on
// hover. The grey buttons this replaced put four boxes on a card 208px wide,
// which read as chrome competing with the name rather than as controls.
//
// The hit area stays a comfortable 22px square even though the glyph is 20px,
// so a thin icon is not a thin target.
const ICON = 20;

const NodeAction = ({ x, y, title, accent, disabled, onClick, children }) => {
  const [over, setOver] = useState(false);
  return (
    <g
      transform={`translate(${x}, ${y})`}
      style={{ cursor: disabled ? 'not-allowed' : 'pointer', opacity: disabled ? 0.3 : 1 }}
      // Stops the node underneath from starting a drag, and the canvas from
      // panning: a press on a control is a press on the control.
      onMouseDown={(e) => e.stopPropagation()}
      onClick={(e) => { e.stopPropagation(); if (!disabled) onClick(); }}
      onMouseEnter={() => setOver(true)}
      onMouseLeave={() => setOver(false)}
    >
      <title>{title}</title>
      <circle cx={ICON / 2} cy={ICON / 2} r={13}
              fill={over && !disabled ? accent : 'transparent'}
              opacity={over && !disabled ? 0.12 : 0} />
      <g stroke={over && !disabled ? accent : '#8a94a0'}
         fill="none" strokeWidth="1.25"
         strokeLinecap="round" strokeLinejoin="round"
         style={{ transition: 'stroke 0.12s' }}>
        {children}
      </g>
      {/* A square hit area, so the target does not shrink with the glyph. */}
      <rect x="-1" y="-1" width={ICON + 2} height={ICON + 2} fill="transparent" />
    </g>
  );
};

// The line in a backup card's bottom strip, between its two actions.
const backupLine = (b) => {
  const st = b.status || {};
  if (st.state === 'running') {
    const p = st.progress?.percent;
    return p != null ? `${p}%` : 'running…';
  }
  if (st.state === 'failed') return 'failed';
  if (b.next_run) return `next ${when(b.next_run)}`;
  return (BACKUP_STATES[st.state] || BACKUP_STATES.needed).label;
};

const WorkflowCanvas = ({
  catalogId, onRun, onQueue, onEdit, onDelete, canManageBackups = false,
}) => {
  const navigate = useNavigate();
  const [graph, setGraph] = useState({ nodes: [], edges: [] });
  const [backups, setBackups] = useState([]);
  const [backupBusy, setBackupBusy] = useState(null);
  const [backupError, setBackupError] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [hover, setHover] = useState(null);      // {edge} or {node}
  const [zoom, setZoom] = useState(1);
  const [pan, setPan] = useState({ x: 0, y: 0 });
  const drag = useRef(null);          // panning the canvas
  const nodeDrag = useRef(null);      // moving one node
  const viewport = useRef(null);
  const fitted = useRef(false);
  // Where the user has put a node, relative to its computed place. The layout
  // stays authoritative - this is an offset on top of it, so re-fetching the
  // graph never throws away an arrangement someone made.
  const [offsets, setOffsets] = useState({});

  const load = useCallback(async () => {
    if (!catalogId) return;
    try {
      // The backups alongside, and never at the graph's expense: a catalog whose
      // backup list cannot be read still shows its pipeline.
      const [res, bk] = await Promise.all([
        workflowAPI.graph(catalogId),
        backupAPI.list(catalogId).catch(() => null),
      ]);
      setGraph(res.data);
      if (bk) setBackups(bk.data?.backups || []);
      setError(null);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not load the workflow graph.');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);
  // The status colours come from the queue, which moves on its own.
  useEffect(() => {
    const t = setInterval(load, 5000);
    return () => clearInterval(t);
  }, [load]);

  const base = useMemo(() => layout(graph.nodes || [], backups), [graph, backups]);
  const { width, height, lane } = base;
  const placed = useMemo(() => {
    const out = new Map();
    base.placed.forEach((n, id) => {
      const o = offsets[id];
      out.set(id, o ? { ...n, x: n.x + o.dx, y: n.y + o.dy } : n);
    });
    return out;
  }, [base, offsets]);

  // Fit the whole pipeline into view, centred. Called once when the graph
  // arrives and again from the reset button - not on every poll, or the view
  // would snap back under the user every five seconds.
  const fit = useCallback(() => {
    const box = viewport.current;
    if (!box || !width || !height) return;
    const z = Math.min(1, (box.clientWidth - 24) / width,
                       (box.clientHeight - 24) / height);
    setZoom(z);
    setPan({
      x: (box.clientWidth - width * z) / 2,
      y: (box.clientHeight - height * z) / 2,
    });
  }, [width, height]);

  useEffect(() => {
    if (fitted.current || (!graph.nodes.length && !backups.length)) return;
    fitted.current = true;
    fit();
  }, [graph, backups, fit]);

  // Back up now - the same action as the backup card's button.
  const backupNow = async (b) => {
    setBackupBusy(b.backupId);
    setBackupError(null);
    try {
      await backupAPI.run(catalogId, b.backupId);
      await load();
    } catch (e) {
      setBackupError(e?.response?.data?.error || `Could not start "${b.name}".`);
    } finally {
      setBackupBusy(null);
    }
  };

  const startDrag = (e) => {
    drag.current = { x: e.clientX, y: e.clientY, pan: { ...pan } };
  };

  const startNodeDrag = (e, id) => {
    // Not the canvas: a press that lands on a node moves that node.
    e.stopPropagation();
    const o = offsets[id] || { dx: 0, dy: 0 };
    nodeDrag.current = { id, x: e.clientX, y: e.clientY, from: o, moved: false };
  };

  const onMove = (e) => {
    if (nodeDrag.current) {
      const d = nodeDrag.current;
      // Divided by zoom, or a node would run away from the pointer when the
      // canvas is scaled.
      const dx = d.from.dx + (e.clientX - d.x) / zoom;
      const dy = d.from.dy + (e.clientY - d.y) / zoom;
      if (Math.abs(e.clientX - d.x) > 2 || Math.abs(e.clientY - d.y) > 2) d.moved = true;
      setOffsets((prev) => ({ ...prev, [d.id]: { dx, dy } }));
      return;
    }
    if (!drag.current) return;
    setPan({
      x: drag.current.pan.x + (e.clientX - drag.current.x),
      y: drag.current.pan.y + (e.clientY - drag.current.y),
    });
  };

  const endDrag = () => { drag.current = null; nodeDrag.current = null; };
  // Puts everything back, including nodes the user has moved: one control for
  // "I have lost the shape of this".
  const reset = () => { setOffsets({}); fit(); };

  if (loading) return <div className="text-center py-5"><CSpinner color="primary" /></div>;
  if (error) return <CAlert color="danger">{error}</CAlert>;
  if (!graph.nodes.length && !backups.length) {
    return (
      <div className="text-center py-5 text-muted">
        No workflows in this catalog yet.
      </div>
    );
  }

  return (
    <div style={{ position: 'relative', border: '1px solid #e9ecef', borderRadius: 12,
                  background: '#fbfcfd', overflow: 'hidden' }}>
      {/* Controls */}
      <div style={{ position: 'absolute', top: 10, right: 10, zIndex: 3,
                    display: 'flex', gap: 4 }}>
        <CButton size="sm" color="light" title="Zoom out"
                 onClick={() => setZoom((z) => Math.max(0.4, z - 0.15))}>
          <CIcon icon={cilZoomOut} style={{ width: 14, height: 14 }} />
        </CButton>
        <CButton size="sm" color="light" title="Zoom in"
                 onClick={() => setZoom((z) => Math.min(2, z + 0.15))}>
          <CIcon icon={cilZoomIn} style={{ width: 14, height: 14 }} />
        </CButton>
        <CButton size="sm" color="light" title="Reset view" onClick={reset}>
          <CIcon icon={cilFullscreen} style={{ width: 14, height: 14 }} />
        </CButton>
      </div>

      <div
        ref={viewport}
        style={{ height: 540, cursor: drag.current ? 'grabbing' : 'grab' }}
        onMouseDown={startDrag}
        onMouseMove={onMove}
        onMouseUp={endDrag}
        onMouseLeave={endDrag}
      >
        <svg width="100%" height="100%" style={{ display: 'block', userSelect: 'none' }}>
          <defs>
            <marker id="wf-arrow" viewBox="0 0 10 10" refX="9" refY="5"
                    markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#c3ccd6" />
            </marker>
            <marker id="wf-arrow-lit" viewBox="0 0 10 10" refX="9" refY="5"
                    markerWidth="6" markerHeight="6" orient="auto-start-reverse">
              <path d="M 0 0 L 10 5 L 0 10 z" fill="#0d6efd" />
            </marker>
            <pattern id="wf-grid" width="24" height="24" patternUnits="userSpaceOnUse">
              <circle cx="1" cy="1" r="1" fill="#e9edf2" />
            </pattern>
          </defs>

          <rect width="100%" height="100%" fill="url(#wf-grid)" />

          <g transform={`translate(${pan.x}, ${pan.y}) scale(${zoom})`}>
            {/* Edges first, so a node always sits on top of the lines. */}
            {(graph.edges || []).map((e, i) => {
              const a = placed.get(e.from);
              const b = placed.get(e.to);
              if (!a || !b) return null;
              const lit = hover?.type === 'edge' && hover.i === i;
              const near = hover?.type === 'node'
                && (hover.id === e.from || hover.id === e.to);
              return (
                <path
                  key={i}
                  d={edgePath(a, b)}
                  fill="none"
                  stroke={lit || near ? '#0d6efd' : '#c3ccd6'}
                  strokeWidth={lit || near ? 2.2 : 1.4}
                  markerEnd={`url(#${lit || near ? 'wf-arrow-lit' : 'wf-arrow'})`}
                  style={{ cursor: 'help', transition: 'stroke 0.15s' }}
                  onMouseEnter={() => setHover({ type: 'edge', i })}
                  onMouseLeave={() => setHover(null)}
                >
                  <title>{e.reason}</title>
                </path>
              );
            })}

            {/* The Backups row's label and rule: a lane of its own, so the
                cards in it read as a separate kind rather than as workflows
                that lost their connectors. */}
            {lane && (
              <g>
                <text x={PAD} y={lane.y} style={{ fontSize: 12, fontWeight: 600, fill: '#6c757d' }}>
                  Backups
                </text>
                <line x1={PAD + 62} y1={lane.y - 4} x2={width - PAD} y2={lane.y - 4}
                      stroke="#dee2e6" strokeDasharray="4 4" />
                <text x={PAD} y={lane.y + 15} style={{ fontSize: 10.5, fill: '#adb5bd' }}>
                  on their own schedule - not in the queue
                </text>
              </g>
            )}

            {[...placed.values()].filter((n) => n.kind === 'backup').map((b) => {
              const st = b.status || {};
              const running = st.state === 'running';
              const style = backupStyle(st.state);
              const pct = running ? st.progress?.percent : null;
              const type = typeOf(b.type).label;
              const typeLine = `Backup · ${type} · ${scheduleText(b.schedule)}`;
              return (
                <g
                  key={b.id}
                  transform={`translate(${b.x}, ${b.y})`}
                  onMouseEnter={() => setHover({ type: 'backup', id: b.id })}
                  onMouseLeave={() => setHover(null)}
                  onMouseDown={(e) => startNodeDrag(e, b.id)}
                  style={{ cursor: 'grab' }}
                  data-testid="backup-node"
                >
                  <title>
                    {[
                      b.name,
                      `${type} backup, ${scheduleText(b.schedule)}`,
                      `last ${b.last_backup?.at ? ago(b.last_backup.at) : 'never'}`,
                      b.next_run && !running ? `next ${when(b.next_run)}` : null,
                      st.reason,
                    ].filter(Boolean).join(' — ')}
                  </title>
                  <rect
                    width={NODE_W} height={NODE_H} rx="10"
                    fill={style.fill} stroke={style.stroke} strokeDasharray="5 3"
                    strokeWidth={hover?.type === 'backup' && hover.id === b.id ? 2 : 1.2}
                  />
                  <rect width="4" height={NODE_H} rx="2" fill={BACKUP_TINT} />
                  <circle cx={NODE_W - 16} cy="16" r="4.5" fill={style.dot} />
                  <text x="16" y="27" style={{ fontSize: 13, fontWeight: 600, fill: '#212529' }}>
                    {b.name && b.name.length > 22 ? `${b.name.slice(0, 21)}…` : b.name}
                  </text>
                  <text x="16" y="45" style={{ fontSize: 11, fill: '#6c757d' }}>
                    {typeLine.length > 34 ? `${typeLine.slice(0, 33)}…` : typeLine}
                  </text>
                  {/* Between the two actions: when it runs next, or how far it
                      has got. */}
                  <text x={NODE_W / 2} y={NODE_H - 12} textAnchor="middle"
                        style={{ fontSize: 10.5,
                                 fill: st.state === 'failed' ? '#c62828' : '#6c757d' }}>
                    {backupLine(b)}
                  </text>
                  {running && (
                    <g>
                      <rect x="14" y={NODE_H - 6} width={NODE_W - 28} height="3" rx="1.5"
                            fill="#d0e2ff" />
                      <rect x="14" y={NODE_H - 6} rx="1.5" height="3" fill="#0d6efd"
                            width={(NODE_W - 28) * Math.min(100, pct ?? 100) / 100}
                            opacity={pct == null ? 0.45 : 1} />
                    </g>
                  )}

                  <NodeAction
                    x={14} y={NODE_H - 26}
                    title="Backup settings - on the Catalogs page, Backup tab"
                    accent="#0d6efd"
                    onClick={() => navigate('/catalogs?tab=backup')}
                  >
                    <path d="M 4 16 v -3.2 L 12.6 4.2 l 3.2 3.2 L 7 16 Z" />
                    <path d="M 11 5.8 L 14.2 9" />
                  </NodeAction>
                  <NodeAction
                    x={NODE_W - 28} y={NODE_H - 26}
                    title={!canManageBackups ? 'Only an owner can start a backup'
                      : running ? 'Already running' : 'Back up now'}
                    accent="#198754"
                    disabled={!canManageBackups || running || backupBusy === b.backupId}
                    onClick={() => backupNow(b)}
                  >
                    <path d="M 6.6 4.6 L 15.8 10 L 6.6 15.4 Z"
                          fill="#198754" stroke="#198754" strokeWidth="1.2" />
                  </NodeAction>
                </g>
              );
            })}

            {[...placed.values()].filter((n) => n.kind !== 'backup').map((n) => {
              const style = n.needs_configuration ? UNCONFIGURED
                : STATUS[n.status] || IDLE;
              const tint = TYPE_TINT[n.type] || '#adb5bd';
              return (
                <g
                  key={n.id}
                  transform={`translate(${n.x}, ${n.y})`}
                  onMouseEnter={() => setHover({ type: 'node', id: n.id })}
                  onMouseLeave={() => setHover(null)}
                  onMouseDown={(e) => startNodeDrag(e, n.id)}
                  style={{ cursor: 'grab' }}
                >
                  {/* The script is here rather than on the card. It is the
                      one thing on a node nobody reads at a glance and everybody
                      wants occasionally, which is what a tooltip is for. */}
                  <title>
                    {[
                      n.name,
                      n.needs_configuration
                        ? 'no source configured, so it would import nothing'
                        : n.status && STATUS[n.status].label,
                      n.script,
                    ].filter(Boolean).join(' — ')}
                  </title>
                  <rect
                    width={NODE_W} height={NODE_H} rx="10"
                    fill={style.fill} stroke={style.stroke}
                    strokeWidth={hover?.type === 'node' && hover.id === n.id ? 2 : 1.2}
                  />
                  {/* Type stripe: the pipeline stage at a glance. */}
                  <rect width="4" height={NODE_H} rx="2" fill={tint} />
                  <circle cx={NODE_W - 16} cy="16" r="4.5" fill={style.dot} />
                  <text x="16" y="27" style={{ fontSize: 13, fontWeight: 600, fill: '#212529' }}>
                    {n.name && n.name.length > 22 ? `${n.name.slice(0, 21)}…` : n.name}
                  </text>
                  {/* Type and the one warning share a line, which leaves the
                      whole bottom strip for the actions. */}
                  <text x="16" y="45" style={{ fontSize: 11, fill: '#6c757d' }}>
                    {n.type || 'workflow'}
                    {n.needs_configuration && (
                      <tspan fill="#c62828"> · needs a source</tspan>
                    )}
                  </text>

                  {/* Two on the left, two on the right. The pair that CHANGES
                      the workflow sits away from the pair that RUNS it, so a
                      slip lands on the wrong action of the same kind rather
                      than deleting something you meant to start. */}
                  <NodeAction
                    x={14} y={NODE_H - 26}
                    title="Edit this workflow"
                    accent="#0d6efd"
                    onClick={() => onEdit && onEdit(n)}
                  >
                    <path d="M 4 16 v -3.2 L 12.6 4.2 l 3.2 3.2 L 7 16 Z" />
                    <path d="M 11 5.8 L 14.2 9" />
                  </NodeAction>
                  <NodeAction
                    x={42} y={NODE_H - 26}
                    title="Remove this workflow"
                    accent="#c62828"
                    onClick={() => onDelete && onDelete(n)}
                  >
                    <path d="M 4.4 6 H 15.6" />
                    <path d="M 6.6 6 L 7.3 15.6 h 5.4 L 13.4 6" />
                    <path d="M 8.4 6 V 4.4 h 3.2 V 6" />
                    <path d="M 9 8.6 v 4.6 M 11 8.6 v 4.6" />
                  </NodeAction>

                  <NodeAction
                    x={NODE_W - 56} y={NODE_H - 26}
                    title={n.needs_configuration
                      ? 'No source configured — give it a storage route first'
                      : 'Add to the queue in the ordinary position'}
                    accent="#0d6efd"
                    disabled={n.needs_configuration}
                    onClick={() => onQueue && onQueue(n)}
                  >
                    <path d="M 4 6.4 H 12 M 4 10 H 12 M 4 13.6 H 9" />
                    <path d="M 13.4 12.4 H 17.6 M 15.5 10.3 V 14.5" />
                  </NodeAction>
                  <NodeAction
                    x={NODE_W - 28} y={NODE_H - 26}
                    title={n.needs_configuration
                      ? 'No source configured — give it a storage route first'
                      : 'Run now — front of the queue, starts on this pass'}
                    accent="#198754"
                    disabled={n.needs_configuration}
                    onClick={() => onRun && onRun(n)}
                  >
                    {/* Filled, unlike the outlined three: this is the action the
                        page exists for, and weight is how that gets said. */}
                    <path d="M 6.6 4.6 L 15.8 10 L 6.6 15.4 Z"
                          fill="#198754" stroke="#198754" strokeWidth="1.2" />
                  </NodeAction>
                </g>
              );
            })}
          </g>
        </svg>
      </div>

      {/* One flat bar along the bottom: what the pointer is on, and the key.
          The legend used to be floated over this strip at bottom:10, which put
          it on top of this text - two things written in the same place. */}
      <div style={{ borderTop: '1px solid #e9ecef', padding: '6px 12px',
                    fontSize: '0.75rem', color: '#6c757d', minHeight: 30,
                    background: '#fff', display: 'flex', flexWrap: 'wrap',
                    alignItems: 'center', gap: '6px 18px' }}>
        <div style={{ flex: '1 1 260px', minWidth: 0 }}>
        {backupError
          ? <span className="text-danger">{backupError}</span>
          : hover?.type === 'edge' && graph.edges[hover.i]
          ? <span><strong>Depends on:</strong> {graph.edges[hover.i].reason}</span>
          : hover?.type === 'backup'
          ? <span>A backup, not a workflow: it runs on its own schedule. Settings
              on the left, back up now on the right</span>
          : hover?.type === 'node'
            ? <span><CIcon icon={cilMediaPlay} style={{ width: 11, height: 11 }} />
                {' '}Edit · remove on the left, queue · run on the right — or drag
                the card to move it</span>
            : <span>Drag a card to move it · drag the background to pan · hover a
                connector to see why the order is what it is</span>}
        </div>

        {/* The key, on the same line. Kept flat and inline - it is four words
            and four dots, not a panel. */}
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: 12,
                      fontSize: '0.72rem', flexShrink: 0 }}>
          {LEGEND.map(([label, colour]) => (
            <span key={label} className="d-flex align-items-center gap-1">
              <span style={{ width: 8, height: 8, borderRadius: '50%',
                             background: colour, flexShrink: 0 }} />
              {label}
            </span>
          ))}
          {backups.length > 0 && (
            <span className="d-flex align-items-center gap-1">
              <span style={{ width: 12, height: 8, borderRadius: 2, flexShrink: 0,
                             border: `1px dashed ${BACKUP_TINT}` }} />
              backup
            </span>
          )}
        </div>
      </div>
    </div>
  );
};

export default WorkflowCanvas;
