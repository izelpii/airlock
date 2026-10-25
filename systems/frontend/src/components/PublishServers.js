import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  CAlert, CBadge, CButton, CForm, CFormInput, CFormLabel, CModal, CModalBody,
  CModalHeader, CModalTitle, CProgress, CProgressBar, CSpinner,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilCloudUpload, cilCopy, cilMediaStop, cilPencil, cilTrash } from '@coreui/icons';
import { publishServerAPI } from '../services/api';

// ─── Publish a deployment to a server ─────────────────────────────────────────
// documentation/specs/deployment-publish-to-server-spec.md section 12.
//
// ServerLine is the one line a deployment card gains: the server, its last
// publish, and Publish. Everything else is in ServersModal: adding a server
// (its key line to paste on the server, then Connect - the host key is shown
// and confirmed, never trusted silently), Preview / Publish / Set up, and a
// run panel shaped like the backup cards'.
//
// Owner only: the routes are, and the card does not show the line otherwise.

const POLL_MS = 2000;
const RUNNING = new Set(['queued', 'running']);

const fmtBytes = (b) => {
  if (b === null || b === undefined) return '–';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = b;
  let i = 0;
  while (v >= 1024 && i < u.length - 1) { v /= 1024; i += 1; }
  return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${u[i]}`;
};

const ago = (iso) => {
  if (!iso) return '';
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return 'just now';
  if (s < 5400) return `${Math.round(s / 60)} min ago`;
  if (s < 172800) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} days ago`;
};

const secondsAgo = (iso) => {
  if (!iso) return '';
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 90 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
};

const KIND = { preview: 'Preview', publish: 'Publish', setup: 'Set up' };

/** What a finished run comes to, in a few words. */
const outcome = (run) => {
  if (!run) return null;
  if (RUNNING.has(run.status)) return { color: 'primary', text: run.phase || 'Working' };
  const r = run.result || {};
  if (run.status === 'succeeded') {
    if (run.kind === 'preview') {
      return { color: 'success', text: r.up_to_date ? 'Up to date' : 'Preview ready' };
    }
    if (r.applied === false) return { color: 'success', text: 'Up to date' };
    return { color: 'success', text: run.kind === 'setup' ? 'Set up' : 'Published' };
  }
  if (run.status === 'stopped') return { color: 'secondary', text: 'Stopped' };
  if (run.status === 'interrupted') return { color: 'warning', text: 'Interrupted by a restart' };
  if (r.needs_confirmation) return { color: 'warning', text: 'Waiting for your confirmation' };
  if (r.needs_setup) return { color: 'warning', text: 'Needs set up' };
  return { color: 'danger', text: 'Failed' };
};

// This machine's gallery version, from its own /health (spec 9).
const useLocalRevision = () => {
  const [rev, setRev] = useState(null);
  useEffect(() => {
    let live = true;
    fetch('/gallery-health').then((r) => (r.ok ? r.json() : null))
      .then((h) => { if (live) setRev(h?.revision || null); }).catch(() => {});
    return () => { live = false; };
  }, []);
  return rev;
};

// ─── The line on the card ─────────────────────────────────────────────────────

export const ServerLine = ({ catalogId, deployment, onOpen, refreshKey }) => {
  const [servers, setServers] = useState(null);
  useEffect(() => {
    let live = true;
    publishServerAPI.list(catalogId, deployment.id)
      .then((r) => { if (live) setServers(r.data.servers || []); })
      .catch(() => { if (live) setServers([]); });
    return () => { live = false; };
  }, [catalogId, deployment.id, refreshKey]);

  if (servers === null) return null;
  const server = servers[0];
  const look = server && outcome(server.last_run);
  // A server, but not connected yet: the button opens the window to finish that.
  const ready = server?.host_key && !RUNNING.has(server.last_run?.status);
  return (
    <div className="d-flex align-items-center gap-2" style={{ fontSize: '0.78rem' }}>
      {server && (
        <button type="button" className="btn btn-link p-0 text-truncate text-start"
                style={{ fontSize: '0.78rem', minWidth: 0 }} onClick={() => onOpen(server.id)}
                title="Servers, previews and publishing">
          {server.name}
          {servers.length > 1 ? ` +${servers.length - 1}` : ''}
          {look && (
            <span className="text-body-secondary">
              {' · '}{look.text}
              {server.last_run?.finished_at ? ` ${ago(server.last_run.finished_at)}` : ''}
            </span>
          )}
        </button>
      )}
      {/* The same button as the other cards' (small, outlined, blue), lower right. */}
      <CButton color="primary" variant="outline" size="sm" className="ms-auto flex-shrink-0"
               disabled={server ? RUNNING.has(server.last_run?.status) : false}
               onClick={() => (server ? onOpen(server.id, ready ? 'publish' : undefined) : onOpen(null))}
               title={!server ? 'Add a server to publish this deployment to'
                 : ready ? 'Send what changed to this server' : 'Finish connecting to the server'}>
        <CIcon icon={cilCloudUpload} className="me-1" style={{ width: 13, height: 13 }} />
        Publish to AWS
      </CButton>
    </div>
  );
};

// ─── The run panel ────────────────────────────────────────────────────────────

const RunPanel = ({ catalogId, deploymentId, runId, localRevision, onFinished, onAgain }) => {
  const [run, setRun] = useState(null);
  const [showLog, setShowLog] = useState(false);
  const timer = useRef(null);

  const load = useCallback(async () => {
    try {
      const r = await publishServerAPI.run(catalogId, deploymentId, runId);
      setRun(r.data);
      if (!RUNNING.has(r.data.status)) onFinished?.(r.data);
    } catch { /* the next poll tries again */ }
  }, [catalogId, deploymentId, runId, onFinished]);

  useEffect(() => { setRun(null); load(); }, [load]);
  useEffect(() => {
    clearTimeout(timer.current);
    if (run && RUNNING.has(run.status)) timer.current = setTimeout(load, POLL_MS);
    return () => clearTimeout(timer.current);
  }, [run, load]);

  if (!run) return <div className="py-2"><CSpinner size="sm" color="primary" /></div>;
  const running = RUNNING.has(run.status);
  const look = outcome(run);
  const r = run.result || {};
  const p = run.progress;

  return (
    <div className="border rounded p-2 mb-2" style={{ fontSize: '0.82rem' }}>
      <div className="d-flex align-items-center gap-2 mb-1">
        {running
          ? <CSpinner size="sm" color="primary" style={{ width: 12, height: 12, borderWidth: 2 }} />
          : <CBadge color={look.color}>{look.text}</CBadge>}
        <strong>{KIND[run.kind]}</strong>
        {running && <span>{run.phase}</span>}
        {running && (
          <CButton color="danger" variant="outline" size="sm" className="ms-auto py-0"
                   onClick={() => publishServerAPI.stop(catalogId, deploymentId, run.id).then(load)}>
            <CIcon icon={cilMediaStop} className="me-1" style={{ width: 12, height: 12 }} />Stop
          </CButton>
        )}
      </div>

      {running && (
        <div className="mb-1">
          {/* The fade goes on CProgress: CoreUI 5.13's CProgressBar drops its width when given a style. */}
          <CProgress height={8} style={p ? undefined : { opacity: 0.45 }}>
            <CProgressBar value={p?.percent ?? 100} animated variant="striped" color="primary" />
          </CProgress>
          {p && (
            <div className="text-body-secondary mt-1" style={{ fontSize: '0.74rem' }}>
              {p.percent != null ? `${p.percent}%` : ''}
              {p.bytes_done ? ` · ${fmtBytes(p.bytes_done)} sent` : ''}
              {p.at ? ` · updated ${secondsAgo(p.at)}` : ''}
            </div>
          )}
        </div>
      )}

      {!running && run.error && <div className="text-danger mb-1">{run.error}</div>}

      {r.plan && (
        <div className="mb-1">
          {r.up_to_date ? 'Nothing to send: the server already has this gallery.'
            : `${r.applied ? 'Sent' : 'Would send'} ${r.plan.new + r.plan.changed} file(s) `
              + `(${fmtBytes(r.applied ? r.bytes_sent : r.plan.bytes)}), `
              + `${r.applied ? 'deleted' : 'would delete'} ${r.plan.deleted}.`}
          {r.checksum && !r.applied && ' Compared by content, as on a first publish.'}
        </div>
      )}

      {(r.warnings || []).length > 0 && (
        <ul className="mb-1 ps-3" style={{ color: '#8a6d1b' }}>
          {r.warnings.map((w) => <li key={w}>{w}</li>)}
        </ul>
      )}

      {r.deletions?.length > 0 && !r.applied && (
        <details className="mb-1">
          <summary>{r.deletions.length} file(s) to delete</summary>
          <div style={{ maxHeight: 140, overflowY: 'auto', fontFamily: 'monospace', fontSize: '0.72rem' }}>
            {r.deletions.map((d) => <div key={d}>{d}</div>)}
          </div>
        </details>
      )}

      {r.checks && (
        <div className="mb-1">
          {r.checks.map((c) => (
            <div key={c.check} style={{ color: c.ok ? '#2e7d32' : '#c62828' }} title={c.detail}>
              {c.ok ? '✓' : '✗'} {c.check}{c.ok ? '' : ` — ${c.detail}`}
            </div>
          ))}
        </div>
      )}

      {r.server_revision && localRevision && r.server_revision !== localRevision && (
        <CAlert color="warning" className="py-1 px-2 mb-1" style={{ fontSize: '0.78rem' }}>
          The server runs gallery {r.server_revision}, this machine {localRevision}. The data
          was sent; to update the gallery program itself, see the guide, section 3.
        </CAlert>
      )}

      {r.needs_confirmation && (
        <div className="d-flex justify-content-end gap-2 mb-1">
          <CButton color="danger" variant="outline" size="sm"
                   onClick={() => onAgain('publish', { confirm_deletions: true })}>
            Publish and delete {r.plan?.deleted} file(s)
          </CButton>
        </div>
      )}
      {r.needs_setup && (
        <div className="d-flex justify-content-end mb-1">
          <CButton color="primary" variant="outline" size="sm" onClick={() => onAgain('setup')}>
            Set up this server
          </CButton>
        </div>
      )}
      {run.kind === 'preview' && run.status === 'succeeded' && !r.up_to_date && !r.stop
        && !r.needs_setup && (
        <div className="d-flex justify-content-end mb-1">
          <CButton color="primary" variant="outline" size="sm"
                   onClick={() => onAgain('publish', r.confirm ? { confirm_deletions: true } : {})}>
            {r.confirm ? `Publish and delete ${r.plan.deleted} file(s)` : 'Publish this'}
          </CButton>
        </div>
      )}

      {run.log && (
        <details open={showLog} onToggle={(e) => setShowLog(e.target.open)}>
          <summary className="text-body-secondary" style={{ fontSize: '0.74rem' }}>Log</summary>
          <pre className="mb-0" style={{ maxHeight: 180, overflowY: 'auto', fontSize: '0.7rem' }}>
            {run.log}
          </pre>
        </details>
      )}
    </div>
  );
};

// ─── Add / edit a server ──────────────────────────────────────────────────────

const EMPTY = { name: '', host: '', port: 22, ssh_user: 'ubuntu', remote_dir: '',
                public_url: 'https://', gallery_image: 'localhost/album-gallery:amd64' };

const ServerForm = ({ initial, deployment, onSave, onCancel, saving, error }) => {
  const [form, setForm] = useState(() => initial || {
    ...EMPTY, remote_dir: `galleries/${deployment.slug}`,
  });
  const set = (k) => (e) => setForm({ ...form, [k]: e.target.value });
  const row = (key, label, hint, props = {}) => (
    <div className="mb-2">
      <CFormLabel className="mb-0" style={{ fontSize: '0.8rem' }}>{label}</CFormLabel>
      <CFormInput size="sm" value={form[key] ?? ''} onChange={set(key)} {...props} />
      {hint && <div className="text-body-secondary" style={{ fontSize: '0.72rem' }}>{hint}</div>}
    </div>
  );
  return (
    <CForm onSubmit={(e) => { e.preventDefault(); onSave(form); }}>
      {row('name', 'Name', null, { placeholder: 'EC2 photos.example.com' })}
      <div className="d-flex gap-2">
        <div style={{ flex: 3 }}>{row('host', 'Host', 'Its Elastic IP or name', { placeholder: '203.0.113.10' })}</div>
        <div style={{ flex: 1 }}>{row('port', 'SSH port', null, { type: 'number' })}</div>
      </div>
      {row('ssh_user', 'SSH user', 'ubuntu on Ubuntu images')}
      {row('remote_dir', 'Folder on the server', 'Relative to that user\'s home')}
      {row('public_url', 'Public address', 'What visitors open', { placeholder: 'https://photos.example.com/' })}
      {row('gallery_image', 'Gallery image', 'As loaded on the server (guide section 3)')}
      {error && <div className="text-danger mb-2" style={{ fontSize: '0.8rem' }}>{error}</div>}
      <div className="d-flex justify-content-end gap-2">
        <CButton size="sm" color="primary" variant="outline" onClick={onCancel} disabled={saving}>
          Cancel
        </CButton>
        <CButton size="sm" color="primary" variant="outline" type="submit" disabled={saving}>
          {saving ? <CSpinner size="sm" /> : initial ? 'Save' : 'Add server'}
        </CButton>
      </div>
    </CForm>
  );
};

// ─── Key line, and Connect (pin) ──────────────────────────────────────────────

const KeyAndConnect = ({ catalogId, deploymentId, server, keyLine, onPinned }) => {
  const [line, setLine] = useState(keyLine || null);
  const [scan, setScan] = useState(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (line || !server) return;
    publishServerAPI.key(catalogId, deploymentId, server.id)
      .then((r) => setLine(r.data.authorized_keys_line)).catch(() => {});
  }, [catalogId, deploymentId, server, line]);

  const connect = async () => {
    setBusy(true); setError(null);
    try {
      setScan((await publishServerAPI.pin(catalogId, deploymentId, server.id)).data);
    } catch (e) { setError(e.response?.data?.error || 'Could not reach the server.'); }
    setBusy(false);
  };
  const confirm = async () => {
    setBusy(true); setError(null);
    try {
      await publishServerAPI.pin(catalogId, deploymentId, server.id, scan.fingerprint);
      setScan(null);
      onPinned();
    } catch (e) { setError(e.response?.data?.error || 'Could not confirm the server.'); }
    setBusy(false);
  };

  return (
    <div className="border rounded p-2 mb-2" style={{ fontSize: '0.8rem' }}>
      <div className="mb-1">
        <strong>1.</strong> On the server, add this line to <code>~/.ssh/authorized_keys</code>
        {' '}of <code>{server.ssh_user}</code>:
      </div>
      <div className="d-flex gap-1 align-items-start mb-2">
        <code className="flex-grow-1 p-1 bg-body-tertiary rounded"
              style={{ wordBreak: 'break-all', fontSize: '0.7rem' }}>{line || '…'}</code>
        <CButton size="sm" color="primary" variant="outline" disabled={!line}
                 onClick={() => {
                   navigator.clipboard?.writeText(line);
                   setCopied(true);
                   setTimeout(() => setCopied(false), 1500);
                 }}>
          <CIcon icon={cilCopy} style={{ width: 12, height: 12 }} />{copied ? ' Copied' : ''}
        </CButton>
      </div>
      <div className="text-body-secondary mb-2" style={{ fontSize: '0.74rem' }}>
        For example, from your own computer:
        <code className="d-block" style={{ fontSize: '0.7rem' }}>
          {`ssh -i your-key.pem ${server.ssh_user}@${server.host} 'cat >> ~/.ssh/authorized_keys'`}
        </code>
        then paste the line and press Ctrl-D.
      </div>
      <div className="mb-1"><strong>2.</strong> Connect, and check the server is the one you expect.</div>
      {server.host_key && !scan && (
        <div className="mb-1" style={{ color: '#2e7d32' }}>
          ✓ Connected. Its key: <code style={{ fontSize: '0.7rem' }}>{server.host_key}</code>
        </div>
      )}
      {scan && (
        <div className="mb-1">
          The server presents <code style={{ fontSize: '0.7rem' }}>{scan.fingerprint}</code>.
          {scan.pinned && scan.pinned !== scan.fingerprint && (
            <div className="text-danger">
              That is NOT the key confirmed before ({scan.pinned}). Only confirm if you replaced
              the server.
            </div>
          )}
          <div className="text-body-secondary" style={{ fontSize: '0.72rem' }}>
            To check it on the server: <code>ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub</code>
          </div>
        </div>
      )}
      {error && <div className="text-danger mb-1">{error}</div>}
      <div className="d-flex justify-content-end gap-2">
        {scan ? (
          <CButton size="sm" color="primary" variant="outline" onClick={confirm} disabled={busy}>
            {busy ? <CSpinner size="sm" /> : 'It matches - confirm'}
          </CButton>
        ) : (
          <CButton size="sm" color="primary" variant="outline" onClick={connect} disabled={busy}>
            {busy ? <CSpinner size="sm" /> : server.host_key ? 'Check again' : 'Connect'}
          </CButton>
        )}
      </div>
    </div>
  );
};

// ─── The modal ────────────────────────────────────────────────────────────────

export const ServersModal = ({ catalogId, deployment, visible, focusServer, startKind,
                               onClose, onChanged }) => {
  const [servers, setServers] = useState([]);
  const [selected, setSelected] = useState(null);
  const [mode, setMode] = useState(null);           // 'add' | 'edit' | null
  const [keyLine, setKeyLine] = useState(null);
  const [showKey, setShowKey] = useState(false);
  const [runId, setRunId] = useState(null);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [carry, setCarry] = useState('');
  const localRevision = useLocalRevision();
  const started = useRef(false);

  // By id, not by the deployment object: the page replaces that object every
  // time it reloads, and the panel must not reset when a run finishes.
  const deploymentId = deployment?.id;
  const load = useCallback(async () => {
    if (!deploymentId) return [];
    const r = await publishServerAPI.list(catalogId, deploymentId);
    setServers(r.data.servers || []);
    return r.data.servers || [];
  }, [catalogId, deploymentId]);

  const start = useCallback(async (serverId, kind, options = {}) => {
    setError(null);
    try {
      const r = await publishServerAPI.start(catalogId, deploymentId, serverId, kind, options);
      setRunId(r.data.run_id);
    } catch (e) {
      setError(e.response?.data?.error || `Could not start ${KIND[kind]}.`);
    }
  }, [catalogId, deploymentId]);

  // Opening: pick the server asked for (or the first), show its last run, and
  // start a run when the card's Publish asked for one.
  useEffect(() => {
    if (!visible) { started.current = false; return; }
    setMode(null); setKeyLine(null); setShowKey(false); setError(null); setRunId(null); setCarry('');
    load().then((list) => {
      const s = list.find((x) => x.id === focusServer) || list[0] || null;
      setSelected(s?.id ?? null);
      if (!s) { setMode('add'); return; }
      if (startKind && !started.current) {
        started.current = true;
        start(s.id, startKind);
      } else if (s.last_run) {
        setRunId(s.last_run.id);
      }
    }).catch(() => setError('Could not load the servers.'));
  }, [visible, focusServer, startKind, deploymentId]); // eslint-disable-line react-hooks/exhaustive-deps

  const server = servers.find((s) => s.id === selected) || null;
  const running = server && RUNNING.has(server.last_run?.status);

  const save = async (form) => {
    setSaving(true); setError(null);
    try {
      if (mode === 'add') {
        const r = await publishServerAPI.add(catalogId, deployment.id, form);
        setKeyLine(r.data.authorized_keys_line);
        await load();
        setSelected(r.data.id);
      } else {
        await publishServerAPI.update(catalogId, deployment.id, server.id, form);
        await load();
      }
      setMode(null);
      onChanged?.();
    } catch (e) {
      setError(e.response?.data?.error || 'Could not save the server.');
    }
    setSaving(false);
  };

  const forget = async () => {
    if (!window.confirm(`Forget "${server.name}"? Its key is deleted here; remove its line from `
      + `~/.ssh/authorized_keys on the server too. The gallery on the server is not touched.`)) return;
    await publishServerAPI.forget(catalogId, deployment.id, server.id);
    const list = await load();
    setSelected(list[0]?.id ?? null);
    setRunId(null);
    if (!list.length) setMode('add');
    onChanged?.();
  };

  const finished = useCallback(() => { load(); onChanged?.(); }, [load, onChanged]);

  if (!deployment) return null;
  return (
    <CModal visible={visible} onClose={onClose} size="lg">
      <CModalHeader>
        <CModalTitle>Publish “{deployment.name}” to a server</CModalTitle>
      </CModalHeader>
      <CModalBody>
        {servers.length > 0 && mode !== 'add' && (
          <div className="d-flex gap-2 flex-wrap mb-2">
            {servers.map((s) => (
              <CButton key={s.id} size="sm" color="primary"
                       variant={s.id === selected ? undefined : 'outline'}
                       onClick={() => {
                         setSelected(s.id); setRunId(s.last_run?.id ?? null);
                         setMode(null); setKeyLine(null); setShowKey(false);
                       }}>
                {s.name}
              </CButton>
            ))}
            <CButton size="sm" color="link" onClick={() => { setMode('add'); setKeyLine(null); }}>
              + Add a server
            </CButton>
          </div>
        )}

        {mode && (
          <>
            {mode === 'add' && (
              <div className="text-body-secondary mb-2" style={{ fontSize: '0.8rem' }}>
                The server must already exist, with Docker installed - see the guide,
                {' '}<code>documentation/guides/PUBLISH_TO_EC2.md</code>. Airlock makes a key for
                it; nothing is sent until you confirm the server.
              </div>
            )}
            <ServerForm initial={mode === 'edit' ? server : null} deployment={deployment}
                        onSave={save} onCancel={() => setMode(servers.length ? null : 'add')}
                        saving={saving} error={error} />
          </>
        )}

        {!mode && server && (
          <>
            <div className="d-flex align-items-start gap-2 mb-2" style={{ fontSize: '0.82rem' }}>
              <div className="flex-grow-1" style={{ minWidth: 0 }}>
                <div><a href={server.public_url} target="_blank" rel="noreferrer">{server.public_url}</a></div>
                <div className="text-body-secondary">
                  {server.ssh_user}@{server.host}{server.port !== 22 ? `:${server.port}` : ''}
                  {' · '}<code>{server.remote_dir}</code>
                </div>
              </div>
              <CButton size="sm" color="primary" variant="outline" title="Edit" onClick={() => setMode('edit')}>
                <CIcon icon={cilPencil} style={{ width: 12, height: 12 }} />
              </CButton>
              <CButton size="sm" color="danger" variant="outline" title="Forget this server" onClick={forget}>
                <CIcon icon={cilTrash} style={{ width: 12, height: 12 }} />
              </CButton>
            </div>

            {(server.key_missing || server.key_changed) && (
              <CAlert color="danger" className="py-1 px-2" style={{ fontSize: '0.8rem' }}>
                {server.key_missing
                  ? 'This server\'s key is missing on this machine. Forget the server and add it again.'
                  : 'This machine\'s key for the server changed. Add the line below to the server again.'}
              </CAlert>
            )}

            {(!server.host_key || keyLine || showKey || server.key_changed) && (
              <KeyAndConnect key={server.id} catalogId={catalogId} deploymentId={deployment.id}
                             server={server} keyLine={keyLine}
                             onPinned={() => { setKeyLine(null); setShowKey(false); load(); }} />
            )}

            {server.host_key && (
              <div className="d-flex justify-content-end align-items-center gap-2 flex-wrap mb-2">
                {!keyLine && !showKey && (
                  <CButton size="sm" color="link" className="me-auto p-0"
                           onClick={() => setShowKey(true)}>Key and identity</CButton>
                )}
                <CButton size="sm" color="primary" variant="outline" disabled={running}
                         onClick={() => start(server.id, 'preview')}>Preview</CButton>
                <CButton size="sm" color="primary" variant="outline" disabled={running}
                         onClick={() => start(server.id, 'publish')}>Publish</CButton>
              </div>
            )}

            {server.host_key && server.last_run?.result?.needs_setup && (
              <div className="border rounded p-2 mb-2" style={{ fontSize: '0.8rem' }}>
                <div className="mb-1">
                  <strong>Set up</strong> sends the first copy, writes the server's session secret
                  and starts the gallery.
                </div>
                <CFormLabel className="mb-0" style={{ fontSize: '0.76rem' }}>
                  Carry certificates over from another gallery's Docker volume (optional)
                </CFormLabel>
                <div className="d-flex gap-2">
                  <CFormInput size="sm" placeholder="e.g. second-catalog_caddy_data" value={carry}
                              onChange={(e) => setCarry(e.target.value)} />
                  <CButton size="sm" color="primary" variant="outline" disabled={running}
                           onClick={() => start(server.id, 'setup',
                             carry.trim() ? { carry_certificates_from: carry.trim() } : {})}>
                    Set up
                  </CButton>
                </div>
              </div>
            )}

            {error && <CAlert color="danger" className="py-1 px-2" style={{ fontSize: '0.8rem' }}>{error}</CAlert>}

            {runId && (
              <RunPanel catalogId={catalogId} deploymentId={deployment.id} runId={runId}
                        localRevision={localRevision} onFinished={finished}
                        onAgain={(kind, options) => start(server.id, kind, options)} />
            )}
          </>
        )}
      </CModalBody>
    </CModal>
  );
};
