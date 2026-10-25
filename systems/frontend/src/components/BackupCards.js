import React, { useCallback, useEffect, useRef, useState } from 'react';
import {
  CAlert, CBadge, CButton, CCard, CCardBody, CCardHeader, CCol, CForm, CFormCheck,
  CFormInput, CFormLabel, CFormSelect, CModal, CModalBody, CModalFooter, CModalHeader,
  CModalTitle, CProgress, CProgressBar, CRow, CSpinner,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilPencil, cilPlus, cilTrash, cilCloudUpload, cilCheckCircle, cilHistory, cilMediaStop, cilCopy,
  cilCheckAlt,
} from '@coreui/icons';
import { backupAPI } from '../services/api';

// ─── The open catalog's backups, as cards ─────────────────────────────────────
// documentation/specs/backup-backrest-spec.md, section 5. One card per
// destination; a dashed card per type to add one, as Storage routes does.
// restic, BorgBackup and rsync, all run in the backend. restic reaches a
// repository on another machine over SFTP, borg over SSH with `borg serve`
// there, rsync over SSH into a plain folder (rrsync there) - each with its own
// Airlock SSH key, shown in the form with the line to add there. One form serves both: TYPES
// holds what differs (documentation/features/backups.md).
//
// Only the catalog's owner sees the buttons: the API refuses everyone else
// anyway (auth.py), and offering an action that would 403 helps nobody.

const CARD_WIDTH = 340;
const POLL_MS = 5000;

export const STATES = {
  up_to_date: { color: '#2e7d32', label: 'Up to date' },
  needed: { color: '#ed6c02', label: 'Backup needed' },
  running: { color: '#1976d2', label: 'Running' },
  failed: { color: '#c62828', label: 'Failed' },
};

export const RUNNING = { backup: 'Backing up', verify: 'Verifying', restore: 'Restoring' };

// What differs between the backup types, for the card and the form.
const TYPES = {
  restic: {
    label: 'restic', secret: 'Password',
    placeholder: 'sftp:izelpi@192.168.1.16:/media/usb1/backup',
    hint: <><code>sftp:user@host:/path</code> for another machine, or a folder this backend can write.</>,
    stop: 'restic stops cleanly; the next backup reuses what was already sent',
    verify: 'restic check: is the repository whole?',
    retention: 'After each backup Airlock forgets this card\'s snapshots beyond these. Reclaiming the '
      + 'space (prune) is left to the repository\'s machine.',
  },
  borg: {
    label: 'borg', secret: 'Passphrase',
    placeholder: 'ssh://izelpi@192.168.1.16/media/usb1/borg/family',
    hint: <><code>ssh://user@host/path</code> for another machine with borg installed, or a folder this backend can write.</>,
    stop: 'borg saves a checkpoint and stops; the next backup reuses what was already sent',
    verify: 'borg check: is the repository whole, and every archive readable?',
    retention: 'After each backup Airlock prunes this card\'s archives beyond these, then compacts '
      + 'the repository to free the space.',
  },
};
TYPES.rsync = {
  label: 'rsync', secret: null, plain: true,
  placeholder: 'izelpi@192.168.1.16:/media/usb1/rsync/family',
  hint: <><code>user@host:/path</code> for another machine with rsync, or a folder this backend can write.
    A plain copy - not encrypted: anyone who can open that folder can see the photos.</>,
  stop: 'rsync stops; the next backup carries on from what was already copied',
  verify: 'Compares the copy with the catalog, file by file, by content',
  retention: null,
};
export const typeOf = (kind) => TYPES[kind] || TYPES.restic;

const SCOPES = {
  originals: 'Only originals + the catalog database',
  everything: 'Everything in the catalog',
};

const DAYS = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'];

export const fmtBytes = (b) => {
  if (b === null || b === undefined) return '–';
  const u = ['B', 'KB', 'MB', 'GB', 'TB'];
  let v = b;
  let i = 0;
  while (v >= 1000 && i < u.length - 1) { v /= 1000; i += 1; }
  return `${v.toFixed(v >= 100 || i === 0 ? 0 : 1)} ${u[i]}`;
};

export const ago = (iso) => {
  if (!iso) return null;
  const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
  if (s < 90) return 'just now';
  if (s < 3600) return `${Math.round(s / 60)} min ago`;
  if (s < 86400) return `${Math.round(s / 3600)} h ago`;
  return `${Math.round(s / 86400)} d ago`;
};

export const remaining = (s) => (s >= 3600 ? `${Math.floor(s / 3600)} h ${Math.round((s % 3600) / 60)} min`
  : `${Math.max(1, Math.round(s / 60))} min`);

// "12 s ago" - how fresh the last progress report is, so a quiet stretch (a
// big file, the database copy) still visibly belongs to a live run.
export const secondsAgo = (iso) => {
  if (!iso) return null;
  const s = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
  return s < 90 ? `${s} s ago` : `${Math.round(s / 60)} min ago`;
};

// The file being read, shortened from the left: its name is the useful end.
const shortPath = (p, max = 46) => (!p ? '' : p.length <= max ? p : `…${p.slice(-(max - 1))}`);

export const when = (iso) => (iso ? new Date(iso).toLocaleString(undefined, {
  weekday: 'short', month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit',
}) : null);

export const scheduleText = (s) => {
  if (!s || s.kind === 'manual') return 'Manual only';
  return s.kind === 'weekly' ? `Weekly, ${DAYS[s.weekday]} ${s.time}` : `Daily at ${s.time}`;
};

const Row = ({ label, children, mono }) => (
  <div className="d-flex justify-content-between gap-2 py-1"
       style={{ fontSize: '0.82rem', borderBottom: '1px solid #f1f3f5' }}>
    <span className="text-body-secondary text-nowrap">{label}</span>
    <span className="text-end" style={{ minWidth: 0, wordBreak: 'break-all',
                                        fontFamily: mono ? 'monospace' : undefined,
                                        fontSize: mono ? '0.74rem' : undefined }}>
      {children}
    </span>
  </div>
);

// ── a backup card ────────────────────────────────────────────────────────────

const BackupCard = ({ card, canManage, busy, onRun, onVerify, onRestore, onStop, onEdit, onDelete }) => {
  const st = card.status || {};
  const tt = typeOf(card.type);
  const look = STATES[st.state] || STATES.needed;
  const lr = card.last_restore;
  const lb = card.last_backup || {};
  const running = st.state === 'running';
  return (
    <CCol xs="auto">
      <CCard className="shadow-sm" style={{ width: CARD_WIDTH }}>
        <CCardHeader className="d-flex justify-content-between align-items-start" style={{ minHeight: 64 }}>
          <div className="me-2 overflow-hidden">
            <div className="fw-semibold text-truncate">{card.name}</div>
            <div className="mt-1 d-flex gap-1 flex-wrap">
              <CBadge color="secondary" style={{ fontSize: '0.7rem' }}>{tt.label}</CBadge>
              <CBadge color="light" className="text-dark" style={{ fontSize: '0.7rem' }}>
                {card.scope === 'everything' ? 'everything' : 'originals + library'}
              </CBadge>
            </div>
          </div>
          {canManage && (
            <div className="d-flex flex-shrink-0">
              <CButton color="danger" variant="ghost" size="sm" className="me-1"
                       title="Remove this backup (the stored backups stay)" onClick={() => onDelete(card)}>
                <CIcon icon={cilTrash} />
              </CButton>
              <CButton color="info" variant="ghost" size="sm" title="Edit" onClick={() => onEdit(card)}>
                <CIcon icon={cilPencil} />
              </CButton>
            </div>
          )}
        </CCardHeader>
        <CCardBody style={{ padding: '0.75rem' }}>
          {/* The status line: colour, an icon-free label and the reason - never
              colour alone. */}
          <div className="d-flex align-items-center gap-2 mb-1">
            {running ? (
              // Turning while the backend works - the dot is for states at rest.
              <CSpinner size="sm" color="primary" style={{ width: 12, height: 12, borderWidth: 2, flexShrink: 0 }}
                        aria-label="Working" />
            ) : (
              <span style={{ width: 10, height: 10, borderRadius: '50%', background: look.color, flexShrink: 0 }} />
            )}
            <strong style={{ color: look.color, fontSize: '0.9rem' }}>
              {running ? RUNNING[st.kind] || look.label : look.label}
            </strong>
          </div>
          {st.reason && <div className="text-body-secondary mb-2" style={{ fontSize: '0.8rem' }}>{st.reason}</div>}
          {running && !st.progress && (
            // A phase with no numbers yet (starting, retention): still moving.
            <div className="mb-2">
              {/* The fade on CProgress: CoreUI 5.13's CProgressBar drops its
                  width when given a style. */}
              <CProgress height={8} style={{ opacity: 0.45 }}>
                <CProgressBar value={100} animated variant="striped" color="primary" />
              </CProgress>
            </div>
          )}
          {running && st.progress && (
            <div className="mb-2">
              <CProgress height={8}>
                {/* borg's backup does not know its total: a moving bar, and what it has read. */}
                <CProgressBar value={st.progress.percent ?? 100} animated variant="striped" color="primary" />
              </CProgress>
              {st.progress.current && (
                <div className="text-body-secondary mt-1 text-truncate"
                     style={{ fontSize: '0.72rem', fontFamily: 'monospace' }} title={st.progress.current}>
                  Reading {shortPath(st.progress.current)}
                </div>
              )}
              <div className="text-body-secondary mt-1" style={{ fontSize: '0.74rem' }}>
                {st.progress.what === 'copy' && st.progress.percent != null ? 'about ' : ''}
                {st.progress.percent != null ? `${st.progress.percent}%` : 'Working'}
                {st.progress.bytes_total ? ` · ${fmtBytes(st.progress.bytes_done)} of ${fmtBytes(st.progress.bytes_total)}`
                  : st.progress.bytes_done ? ` · ${fmtBytes(st.progress.bytes_done)} read` : ''}
                {st.progress.files_total ? ` · ${(st.progress.files_done || 0).toLocaleString()} of ${st.progress.files_total.toLocaleString()} files`
                  : st.progress.files_done ? ` · ${st.progress.files_done.toLocaleString()} files` : ''}
                {st.progress.seconds_remaining ? ` · ${remaining(st.progress.seconds_remaining)} left` : ''}
                {st.progress.at ? ` · updated ${secondsAgo(st.progress.at)}` : ''}
              </div>
            </div>
          )}
          <Row label="Last backup">
            {lb.at ? (
              <span title={when(lb.at)}>
                {ago(lb.at)}
                {lb.files_new != null && ` · ${lb.files_new.toLocaleString()} new files`}
                {lb.data_added != null && ` · ${fmtBytes(lb.data_added)} added`}
              </span>
            ) : 'never'}
          </Row>
          <Row label="Schedule">{scheduleText(card.schedule)}</Row>
          {card.next_run && <Row label="Next">{when(card.next_run)}</Row>}
          <Row label="Verified">
            {card.last_verify?.at ? (
              <span title={card.last_verify.error || when(card.last_verify.at)}>
                {ago(card.last_verify.at)} {card.last_verify.status === 'success' ? '✓ whole' : '✗ problems found'}
              </span>
            ) : 'not yet'}
          </Row>
          {lr && (
            <Row label="Last restore">
              <span title={lr.error || lr.target}>
                {ago(lr.at)} · {lr.status === 'success' ? 'done' : lr.status}
              </span>
            </Row>
          )}
          <Row label="Repository" mono>{card.repo_uri}</Row>

          {canManage && (
            <div className="d-flex gap-2 mt-3 flex-wrap">
              {running && (
                <CButton color="danger" variant="outline" size="sm" disabled={busy} onClick={() => onStop(card)}
                         title={tt.stop}>
                  <CIcon icon={cilMediaStop} className="me-1" style={{ width: 13, height: 13 }} />
                  Stop
                </CButton>
              )}
              <CButton color="primary" size="sm" disabled={running || busy} onClick={() => onRun(card)}>
                <CIcon icon={cilCloudUpload} className="me-1" style={{ width: 13, height: 13 }} />
                Back up now
              </CButton>
              <CButton color="secondary" variant="outline" size="sm" disabled={running || busy}
                       onClick={() => onVerify(card)} title={tt.verify}>
                <CIcon icon={cilCheckCircle} className="me-1" style={{ width: 13, height: 13 }} />
                Verify
              </CButton>
              <CButton color="secondary" variant="outline" size="sm" disabled={running || busy}
                       onClick={() => onRestore(card)}>
                <CIcon icon={cilHistory} className="me-1" style={{ width: 13, height: 13 }} />
                Restore…
              </CButton>
            </div>
          )}
        </CCardBody>
      </CCard>
    </CCol>
  );
};

// The dashed cards that start a new backup, one per type (spec 5.1).
const NewBackupCard = ({ label, hint, disabled, onClick }) => (
  <CCol xs="auto">
    <CCard className="h-auto"
           style={{ width: 220, minHeight: 150, border: '2px dashed #ced4da', background: 'transparent',
                    cursor: disabled ? 'default' : 'pointer', opacity: disabled ? 0.6 : 1 }}
           onClick={disabled ? undefined : onClick}
           role={disabled ? undefined : 'button'}>
      <CCardBody className="d-flex flex-column align-items-center justify-content-center text-center">
        <CIcon icon={cilPlus} style={{ width: 24, height: 24, color: '#adb5bd', marginBottom: 6 }} />
        <div style={{ fontSize: '0.9rem', color: disabled ? '#adb5bd' : '#495057', fontWeight: 600 }}>{label}</div>
        <div style={{ fontSize: '0.74rem', color: '#adb5bd', marginTop: 4 }}>{hint}</div>
      </CCardBody>
    </CCard>
  </CCol>
);

// ── create / edit ────────────────────────────────────────────────────────────

const browserZone = () => {
  try { return Intl.DateTimeFormat().resolvedOptions().timeZone || 'UTC'; } catch { return 'UTC'; }
};

const randomPassword = () => {
  const bytes = new Uint8Array(24);
  window.crypto.getRandomValues(bytes);
  return btoa(String.fromCharCode(...bytes)).replace(/[+/=]/g, '').slice(0, 32);
};

const emptyForm = () => ({
  name: '', scope: 'originals',
  repo_mode: 'existing', repo_uri: '', repo_password: '', remote_path: '/opt/borg/bin/borg', root: '',
  schedule: { kind: 'daily', time: '02:00', weekday: 6 },
  retention: { daily: 7, weekly: 4, monthly: 12, yearly: 3 },
});

const isSftp = (uri) => /^sftp:/.test((uri || '').trim());

// Airlock's public key, for the backup host's authorized_keys - restricted to
// file transfer, so the key can do nothing else there.
const SshKeyPanel = ({ ssh, uri }) => {
  const [copied, setCopied] = useState(false);
  if (!ssh?.authorized_keys_line) {
    return <CAlert color="warning" className="small">{ssh?.error || 'This backend has no SSH key yet.'}</CAlert>;
  }
  const m = /^sftp:(?:\/\/)?(?:([^@:/]+)@)?([^:/]+)/.exec((uri || '').trim());
  const where = m ? `${m[1] ? `${m[1]}'s` : 'the user\'s'} ~/.ssh/authorized_keys on ${m[2]}` : 'the backup host\'s ~/.ssh/authorized_keys';
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(ssh.authorized_keys_line);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* the text is selectable anyway */ }
  };
  return (
    <div className="mb-3 p-2 rounded" style={{ background: '#f8f9fa', border: '1px solid #e9ecef' }}>
      <div className="small mb-1">
        Airlock reaches the repository with its own SSH key. Add this line, once, to <strong>{where}</strong>:
      </div>
      <div className="d-flex gap-1 align-items-start">
        <code className="flex-grow-1 p-1 bg-white border rounded"
              style={{ fontSize: '0.7rem', wordBreak: 'break-all', userSelect: 'all' }}>
          {ssh.authorized_keys_line}
        </code>
        <CButton color="secondary" variant="outline" size="sm" onClick={copy} title="Copy">
          <CIcon icon={copied ? cilCheckAlt : cilCopy} />
        </CButton>
      </div>
      <div className="text-body-secondary mt-1" style={{ fontSize: '0.72rem' }}>
        <code>restrict</code> and <code>sftp-server</code> limit it to file transfer: no shell, no forwarding.
      </div>
    </div>
  );
};

const isBorgSsh = (uri) => /^ssh:\/\//.test((uri || '').trim());
const RSYNC_SSH = /^(?:([^@:/\s]+)@)?([^:/\s]+):(\/\S*)$/;
const parentOf = (path) => (path.replace(/\/+$/, '').replace(/\/[^/]*$/, '') || '/');

// rsync's key, and its line: rsync only, only inside one folder (rrsync).
const RsyncKeyPanel = ({ ssh, uri, root }) => {
  const [copied, setCopied] = useState(false);
  const key = ssh?.rsync;
  if (!key?.public_key) {
    return <CAlert color="warning" className="small">{key?.error || 'This backend has no rsync SSH key yet.'}</CAlert>;
  }
  const m = RSYNC_SSH.exec((uri || '').trim());
  const folder = root || (m ? parentOf(m[3]) : '/');
  const line = `command="rrsync ${folder}",restrict ${key.public_key}`;
  const where = m ? `${m[1] ? `${m[1]}'s` : 'the user\'s'} ~/.ssh/authorized_keys on ${m[2]}` : 'the backup host\'s ~/.ssh/authorized_keys';
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(line);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* the text is selectable anyway */ }
  };
  return (
    <div className="mb-3 p-2 rounded" style={{ background: '#f8f9fa', border: '1px solid #e9ecef' }}>
      <div className="small mb-1">
        Airlock copies with its own <strong>rsync</strong> SSH key. Add this line, once,
        to <strong>{where}</strong>:
      </div>
      <div className="d-flex gap-1 align-items-start">
        <code className="flex-grow-1 p-1 bg-white border rounded"
              style={{ fontSize: '0.7rem', wordBreak: 'break-all', userSelect: 'all' }}>
          {line}
        </code>
        <CButton color="secondary" variant="outline" size="sm" onClick={copy} title="Copy">
          <CIcon icon={copied ? cilCheckAlt : cilCopy} />
        </CButton>
      </div>
      <div className="text-body-secondary mt-1" style={{ fontSize: '0.72rem' }}>
        <code>rrsync</code> (part of rsync) lets the key copy files inside <code>{folder}</code> and nothing else.
      </div>
    </div>
  );
};

// borg's key, and its line: `borg serve` only, only inside the repository's
// parent folder - so other repositories beside it work with the same line.
const BorgKeyPanel = ({ ssh, uri, remotePath }) => {
  const [copied, setCopied] = useState(false);
  const borg = ssh?.borg;
  if (!borg?.public_key) {
    return <CAlert color="warning" className="small">{borg?.error || 'This backend has no borg SSH key yet.'}</CAlert>;
  }
  const m = /^ssh:\/\/(?:([^@:/]+)@)?([^:/]+)(?::\d+)?(\/.*)$/.exec((uri || '').trim());
  const parent = m ? (m[3].replace(/\/+$/, '').replace(/\/[^/]*$/, '') || '/') : '/';
  const line = `command="${remotePath || borg.default_remote_path} serve --restrict-to-path ${parent}",restrict ${borg.public_key}`;
  const where = m ? `${m[1] ? `${m[1]}'s` : 'the user\'s'} ~/.ssh/authorized_keys on ${m[2]}` : 'the backup host\'s ~/.ssh/authorized_keys';
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(line);
      setCopied(true);
      setTimeout(() => setCopied(false), 1500);
    } catch { /* the text is selectable anyway */ }
  };
  return (
    <div className="mb-3 p-2 rounded" style={{ background: '#f8f9fa', border: '1px solid #e9ecef' }}>
      <div className="small mb-1">
        Airlock reaches borg with its own <strong>borg</strong> SSH key (not the restic one). Add this
        line, once, to <strong>{where}</strong>:
      </div>
      <div className="d-flex gap-1 align-items-start">
        <code className="flex-grow-1 p-1 bg-white border rounded"
              style={{ fontSize: '0.7rem', wordBreak: 'break-all', userSelect: 'all' }}>
          {line}
        </code>
        <CButton color="secondary" variant="outline" size="sm" onClick={copy} title="Copy">
          <CIcon icon={copied ? cilCheckAlt : cilCopy} />
        </CButton>
      </div>
      <div className="text-body-secondary mt-1" style={{ fontSize: '0.72rem' }}>
        The key may only run <code>borg serve</code>, and only inside <code>{parent}</code>: no shell, no forwarding.
      </div>
    </div>
  );
};

const BackupForm = ({ visible, catalogId, editing, kind: newKind, ssh, sizes, onClose, onSaved }) => {
  const kind = editing ? (editing.type || 'restic') : (newKind || 'restic');
  const tt = typeOf(kind);
  const isBorg = kind === 'borg';
  const isRsync = kind === 'rsync';
  const [f, setF] = useState(emptyForm());
  const [test, setTest] = useState(null);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);

  useEffect(() => {
    if (!visible) return;
    setError(null);
    setTest(null);
    if (editing) {
      setF({ ...emptyForm(), name: editing.name, scope: editing.scope, repo_uri: editing.repo_uri,
             schedule: editing.schedule, retention: editing.retention,
             remote_path: editing.options?.remote_path || '/opt/borg/bin/borg',
             root: editing.options?.root || '' });
    } else {
      setF(emptyForm());
    }
  }, [visible, editing]);

  const set = (k, v) => {
    setF((x) => ({ ...x, [k]: v }));
    if (['repo_uri', 'repo_password', 'repo_mode', 'remote_path', 'root'].includes(k)) setTest(null);
  };
  const creating = !editing && f.repo_mode === 'new';
  // Once Create has made the repository, its password is that one: changing
  // it here would only make a card that cannot open what was just created.
  const created = creating && !!test?.created;
  const repoChanged = editing && (f.repo_uri.trim() !== editing.repo_uri || f.repo_password);

  const runTest = async () => {
    setTesting(true);
    setError(null);
    try {
      const r = await backupAPI.test(catalogId, {
        type: kind, ...(isBorg ? { options: { remote_path: f.remote_path.trim() } } : {}),
        ...(isRsync ? { options: { root: f.root.trim() } } : {}),
        repo_uri: f.repo_uri.trim(), repo_password: f.repo_password || null,
        backup_id: editing?.id, create: creating,
      });
      setTest(r.data);
    } catch (e) {
      setTest({ ok: false, error: e.response?.data?.error || e.message });
    } finally {
      setTesting(false);
    }
  };

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      const body = {
        name: f.name, scope: f.scope, schedule: f.schedule, retention: f.retention,
        schedule_tz: browserZone(),
        ...(isBorg ? { options: { remote_path: f.remote_path.trim() } } : {}),
        ...(isRsync ? { options: { root: f.root.trim() } } : {}),
      };
      if (editing) {
        await backupAPI.update(catalogId, editing.id, {
          ...body,
          ...(f.repo_uri.trim() !== editing.repo_uri ? { repo_uri: f.repo_uri.trim() } : {}),
          ...(f.repo_password ? { repo_password: f.repo_password } : {}),
        });
      } else {
        await backupAPI.create(catalogId, {
          ...body, type: kind, repo_uri: f.repo_uri.trim(), repo_password: f.repo_password,
        });
      }
      onSaved();
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    } finally {
      setSaving(false);
    }
  };

  // A new card needs a repository that opens: Test connection proves it (and,
  // for a new one, creates it). An edit that changes the repository is
  // checked by the server when saved.
  const canSave = f.name.trim() && f.repo_uri.trim()
    && (editing || ((f.repo_password || isRsync) && test?.ok));

  const testColor = !test ? null : !test.ok ? (test.kind === 'missing' ? 'warning' : 'danger') : 'success';

  return (
    <CModal visible={visible} onClose={onClose} size="lg" backdrop="static">
      <CModalHeader>
        <CModalTitle>{editing ? `Edit ${editing.name}` : `New backup · ${tt.label}`}</CModalTitle>
      </CModalHeader>
      <CModalBody>
        {error && <CAlert color="danger" style={{ fontSize: '0.85rem' }}>{error}</CAlert>}
        <CForm>
          <h6 className="fw-semibold">About the backup</h6>
          <CRow className="g-2 mb-3">
            <CCol md={6}>
              <CFormLabel className="small">Name *</CFormLabel>
              <CFormInput value={f.name} onChange={(e) => set('name', e.target.value)}
                          placeholder="backup-host nightly" />
            </CCol>
            <CCol md={6}>
              <CFormLabel className="small">What to back up</CFormLabel>
              {Object.entries(SCOPES).map(([k, label]) => (
                <CFormCheck key={k} type="radio" name="scope" id={`scope-${k}`} checked={f.scope === k}
                            onChange={() => set('scope', k)}
                            label={<span style={{ fontSize: '0.85rem' }}>{label}
                              {sizes?.[k] ? <span className="text-body-secondary"> · {fmtBytes(sizes[k])}</span> : null}</span>} />
              ))}
            </CCol>
          </CRow>

          <h6 className="fw-semibold">{isRsync ? 'Destination' : 'Repository'} <span className="text-body-secondary fw-normal small">- where the backup is stored</span></h6>
          {!editing && (
            <div className="d-flex gap-4 mb-2">
              <CFormCheck type="radio" name="repo_mode" id="repo-existing" label={isRsync ? 'Use an existing folder' : 'Use an existing repository'}
                          checked={f.repo_mode === 'existing'} onChange={() => set('repo_mode', 'existing')} />
              <CFormCheck type="radio" name="repo_mode" id="repo-new" label="Create a new one"
                          checked={f.repo_mode === 'new'} onChange={() => set('repo_mode', 'new')} />
            </div>
          )}
          <CRow className="g-2 mb-2">
            <CCol md={7}>
              <CFormLabel className="small">{isRsync ? 'Folder' : 'Repository'} *</CFormLabel>
              <CFormInput value={f.repo_uri} onChange={(e) => set('repo_uri', e.target.value)}
                          style={{ fontFamily: 'monospace', fontSize: '0.82rem' }}
                          placeholder={tt.placeholder} />
            </CCol>
            {!isRsync && (
            <CCol md={5}>
              <CFormLabel className="small">
                {tt.secret} {creating ? '* (8+ characters)' : editing ? '' : '*'}
              </CFormLabel>
              <div className="d-flex gap-1">
                <CFormInput type={creating ? 'text' : 'password'} value={f.repo_password}
                            readOnly={created} autoComplete="new-password"
                            placeholder={editing?.has_repo_password ? '(unchanged)' : ''}
                            onChange={(e) => set('repo_password', e.target.value)}
                            style={{ fontFamily: 'monospace', fontSize: '0.8rem' }} />
                {creating && (
                  <CButton color="secondary" variant="outline" size="sm" disabled={created}
                           title={created ? 'The repository was created with this password' : undefined}
                           onClick={() => set('repo_password', randomPassword())}>Generate</CButton>
                )}
              </div>
            </CCol>
            )}
          </CRow>
          <div className="text-body-secondary mb-2" style={{ fontSize: '0.74rem' }}>
            {tt.hint}
            {!isRsync && ' One repository can hold many catalogs\' backups - each card keeps to its own snapshots.'}
          </div>
          {creating && !isRsync && (
            <div className="small mb-2" style={{ color: '#c62828' }}>
              Store this {tt.secret.toLowerCase()} somewhere else. Without it nobody can read these backups - not even you.
            </div>
          )}
          {isBorg && (
            <CRow className="g-2 mb-2">
              <CCol md={7}>
                <CFormLabel className="small">borg on the server</CFormLabel>
                <CFormInput value={f.remote_path} onChange={(e) => set('remote_path', e.target.value)}
                            style={{ fontFamily: 'monospace', fontSize: '0.82rem' }}
                            placeholder="/opt/borg/bin/borg" />
              </CCol>
              <CCol md={5} className="d-flex align-items-end">
                <div className="text-body-secondary" style={{ fontSize: '0.72rem' }}>
                  Where the backup host has borg 1.x. On backup-host: <code>/opt/borg/bin/borg</code>.
                </div>
              </CCol>
            </CRow>
          )}
          {!isBorg && isSftp(f.repo_uri) && <SshKeyPanel ssh={ssh} uri={f.repo_uri} />}
          {isBorg && isBorgSsh(f.repo_uri) && <BorgKeyPanel ssh={ssh} uri={f.repo_uri} remotePath={f.remote_path.trim()} />}
          {isRsync && RSYNC_SSH.test(f.repo_uri.trim()) && (
            <>
              <CRow className="g-2 mb-2">
                <CCol md={7}>
                  <CFormLabel className="small">Folder the key is locked to</CFormLabel>
                  <CFormInput value={f.root} onChange={(e) => set('root', e.target.value)}
                              style={{ fontFamily: 'monospace', fontSize: '0.82rem' }}
                              placeholder={parentOf(RSYNC_SSH.exec(f.repo_uri.trim())[3])} />
                </CCol>
                <CCol md={5} className="d-flex align-items-end">
                  <div className="text-body-secondary" style={{ fontSize: '0.72rem' }}>
                    Empty: the destination&apos;s parent, so one line covers every catalog copied there.
                  </div>
                </CCol>
              </CRow>
              <RsyncKeyPanel ssh={ssh} uri={f.repo_uri} root={f.root.trim()} />
            </>
          )}
          <div className="d-flex align-items-center gap-2 mb-2">
            <CButton color="secondary" variant="outline" size="sm" onClick={runTest}
                     disabled={testing || created || !f.repo_uri.trim() || (!f.repo_password && !editing && !isRsync)}>
              {testing ? <CSpinner size="sm" /> : creating ? (created ? 'Created' : (isRsync ? 'Create folder' : 'Create repository')) : 'Test connection'}
            </CButton>
            {!editing && !test?.ok && (
              <span className="text-body-secondary" style={{ fontSize: '0.76rem' }}>
                {creating ? 'Creates it, empty, and checks it opens.' : 'Checks the repository opens with this password.'}
              </span>
            )}
          </div>
          {test && (
            <CAlert color={testColor} style={{ fontSize: '0.82rem' }}>
              {test.ok ? (
                <>
                  {isRsync ? (test.created ? 'Created the folder' : 'The folder is reachable')
                    : (test.created ? 'Created the repository' : 'The repository opens')}
                  {test.repo_id && <> · id <code>{test.repo_id.slice(0, 12)}</code></>}.
                </>
              ) : (
                <>
                  {test.error}
                  {test.kind === 'missing' && !editing && f.repo_mode === 'existing' && (
                    <div>To start one there, choose <strong>Create a new one</strong>.</div>
                  )}
                </>
              )}
              {test.host_fingerprint && (
                <div className="text-body-secondary" style={{ fontSize: '0.74rem' }}>
                  Host key {test.host_fingerprint} - pinned; Airlock will refuse the host if it changes.
                </div>
              )}
            </CAlert>
          )}
          {repoChanged && (
            <div className="small text-body-secondary mb-2">
              A different repository is checked when you save; the card starts over there.
            </div>
          )}

          <h6 className="fw-semibold mt-3">{isRsync ? 'When' : 'When, and how long to keep'}</h6>
          <CRow className="g-2">
            <CCol md={4}>
              <CFormLabel className="small">Schedule</CFormLabel>
              <CFormSelect value={f.schedule.kind}
                           onChange={(e) => set('schedule', { ...f.schedule, kind: e.target.value })}>
                <option value="manual">Manual only</option>
                <option value="daily">Daily</option>
                <option value="weekly">Weekly</option>
              </CFormSelect>
            </CCol>
            {f.schedule.kind === 'weekly' && (
              <CCol md={4}>
                <CFormLabel className="small">Day</CFormLabel>
                <CFormSelect value={f.schedule.weekday}
                             onChange={(e) => set('schedule', { ...f.schedule, weekday: Number(e.target.value) })}>
                  {DAYS.map((d, i) => <option key={d} value={i}>{d}</option>)}
                </CFormSelect>
              </CCol>
            )}
            {f.schedule.kind !== 'manual' && (
              <CCol md={4}>
                <CFormLabel className="small">Time ({browserZone()})</CFormLabel>
                <CFormInput type="time" value={f.schedule.time}
                            onChange={(e) => set('schedule', { ...f.schedule, time: e.target.value })} />
              </CCol>
            )}
          </CRow>
          {isRsync ? (
            <div className="text-body-secondary mt-2" style={{ fontSize: '0.74rem' }}>
              rsync keeps one copy, brought up to date by each backup - no versions to keep. Files
              deleted or replaced in the catalog are not lost from the copy: they are moved into
              its <code>.airlock-replaced/</code> folder, by date, to clear by hand.
            </div>
          ) : (
          <>
          <CRow className="g-2 mt-1">
            {['daily', 'weekly', 'monthly', 'yearly'].map((k) => (
              <CCol key={k} xs={6} md={3}>
                <CFormLabel className="small">Keep {k}</CFormLabel>
                <CFormInput type="number" min={0} value={f.retention[k]}
                            onChange={(e) => set('retention', { ...f.retention, [k]: Number(e.target.value) })} />
              </CCol>
            ))}
          </CRow>
          <div className="text-body-secondary mt-1" style={{ fontSize: '0.74rem' }}>
            {tt.retention}
          </div>
          </>
          )}
        </CForm>
      </CModalBody>
      <CModalFooter>
        <CButton color="secondary" variant="outline" onClick={onClose} disabled={saving}>Cancel</CButton>
        <CButton color="primary" onClick={save} disabled={!canSave || saving}
                 title={!editing && !test?.ok ? 'Test the connection first' : undefined}>
          {saving ? <CSpinner size="sm" /> : editing ? 'Save' : 'Create backup'}
        </CButton>
      </CModalFooter>
    </CModal>
  );
};

// ── restore ──────────────────────────────────────────────────────────────────

const RestoreModal = ({ visible, catalogId, card, onClose, onStarted }) => {
  const [snaps, setSnaps] = useState(null);
  const [root, setRoot] = useState('');
  const [catalogPath, setCatalogPath] = useState('');
  const [snapshot, setSnapshot] = useState('');
  const [path, setPath] = useState('');
  const [folder, setFolder] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!visible || !card) return;
    setSnaps(null);
    setError(null);
    setPath('');
    setFolder('');
    backupAPI.snapshots(catalogId, card.id).then((r) => {
      setSnaps(r.data.snapshots);
      setRoot(r.data.restore_root_host || r.data.restore_root);
      setCatalogPath(r.data.catalog_path);
      setSnapshot(r.data.snapshots[0]?.id || '');
    }).catch((e) => setError(e.response?.data?.error || e.message));
  }, [visible, card, catalogId]);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      const r = await backupAPI.restore(catalogId, card.id, { snapshot_id: snapshot, path, folder });
      onStarted(r.data.target_host || r.data.target);
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <CModal visible={visible} onClose={onClose} size="lg">
      <CModalHeader><CModalTitle>Restore from {card?.name}</CModalTitle></CModalHeader>
      <CModalBody>
        {error && <CAlert color="danger" style={{ fontSize: '0.85rem' }}>{error}</CAlert>}
        <p className="small text-body-secondary">
          Restores into a new folder, <code>{root}/…</code>, on the machine Airlock runs on - never
          over the live catalog. Bringing a whole catalog back is a separate procedure (spec section 7).
        </p>
        {snaps === null && !error ? <CSpinner size="sm" /> : snaps && snaps.length === 0 ? (
          <div className="text-body-secondary">No snapshots yet - run a backup first.</div>
        ) : snaps && (
          <CForm>
            <CFormLabel className="small">Snapshot</CFormLabel>
            <CFormSelect value={snapshot} onChange={(e) => setSnapshot(e.target.value)} className="mb-2">
              {snaps.map((s) => (
                <option key={s.id} value={s.id}>
                  {when(s.time)} · {s.short_id} · {s.files ? `${s.files.toLocaleString()} files, ` : ''}{fmtBytes(s.bytes)}
                </option>
              ))}
            </CFormSelect>
            <CFormLabel className="small">What to restore (a path inside the snapshot; empty for all of it)</CFormLabel>
            <CFormInput value={path} onChange={(e) => setPath(e.target.value)} className="mb-2"
                        placeholder="everything in the snapshot"
                        style={{ fontFamily: 'monospace', fontSize: '0.82rem' }} />
            <div className="small text-body-secondary mb-2">
              e.g. {catalogPath}/Images/2019 for one year.
            </div>
            <CFormLabel className="small">Into folder (optional)</CFormLabel>
            <CFormInput value={folder} onChange={(e) => setFolder(e.target.value)}
                        placeholder="default: catalog name and the time" />
          </CForm>
        )}
      </CModalBody>
      <CModalFooter>
        <CButton color="secondary" variant="outline" onClick={onClose}>Cancel</CButton>
        <CButton color="primary" onClick={start} disabled={busy || !snapshot}>
          {busy ? <CSpinner size="sm" /> : 'Restore'}
        </CButton>
      </CModalFooter>
    </CModal>
  );
};

// ── the tab ──────────────────────────────────────────────────────────────────

const BackupCards = ({ catalogId, catalogName, canManage }) => {
  const [data, setData] = useState(null);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(null);
  const [formOpen, setFormOpen] = useState(false);
  const [formKind, setFormKind] = useState('restic');
  const [editing, setEditing] = useState(null);
  const [restoring, setRestoring] = useState(null);
  const timer = useRef(null);

  const load = useCallback(async () => {
    try {
      const r = await backupAPI.list(catalogId);
      setData(r.data);
      setError(null);
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    }
  }, [catalogId]);

  useEffect(() => { setData(null); load(); }, [load]);

  // While anything runs, ask again every few seconds - only with the tab visible.
  useEffect(() => {
    clearTimeout(timer.current);
    if (data?.backups?.some((b) => b.status?.state === 'running')) {
      timer.current = setTimeout(() => {
        if (document.visibilityState === 'visible') load();
        else setData((d) => ({ ...d }));   // ask again on the next tick
      }, POLL_MS);
    }
    return () => clearTimeout(timer.current);
  }, [data, load]);

  const act = async (card, fn, message) => {
    setBusy(card.id);
    setError(null);
    try {
      await fn();
      setNotice(message);
      await load();
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    } finally {
      setBusy(null);
    }
  };

  const remove = (card) => {
    if (!window.confirm(`Remove the backup "${card.name}"?\n\n`
      + 'The backups already stored stay in the repository.')) return;
    act(card, () => backupAPI.remove(catalogId, card.id), `${card.name} removed; its stored backups were kept.`);
  };

  if (!data) {
    return error ? <CAlert color="danger">{error}</CAlert>
      : <div className="py-3"><CSpinner size="sm" color="primary" /></div>;
  }

  return (
    <>
      {error && <CAlert color="danger" style={{ fontSize: '0.85rem' }}>{error}</CAlert>}
      {notice && <CAlert color="success" dismissible onClose={() => setNotice(null)} style={{ fontSize: '0.85rem' }}>{notice}</CAlert>}
      <CRow className="g-3 align-items-start">
        {data.backups.map((card) => (
          <BackupCard key={card.id} card={card} canManage={canManage} busy={busy === card.id}
                      onRun={(c) => act(c, () => backupAPI.run(catalogId, c.id),
                                        `Backing up ${c.name}: copying the catalog database first, then the files.`)}
                      onVerify={(c) => act(c, () => backupAPI.verify(catalogId, c.id),
                                           `Verifying ${c.name}'s repository.`)}
                      onRestore={setRestoring}
                      onStop={(c) => act(c, () => backupAPI.cancel(catalogId, c.id),
                                         `Stopping ${c.name}…`)}
                      onEdit={(c) => { setEditing(c); setFormOpen(true); }}
                      onDelete={remove} />
        ))}
        {canManage && (
          <>
            <NewBackupCard label="restic" hint="Encrypted, versioned snapshots"
                           onClick={() => { setEditing(null); setFormKind('restic'); setFormOpen(true); }} />
            <NewBackupCard label="BorgBackup" hint="Deduplicated, compressed archives"
                           onClick={() => { setEditing(null); setFormKind('borg'); setFormOpen(true); }} />
            <NewBackupCard label="rsync" hint="A plain copy, browsable anywhere"
                           onClick={() => { setEditing(null); setFormKind('rsync'); setFormOpen(true); }} />
          </>
        )}
        {!canManage && data.backups.length === 0 && (
          <CCol><div className="text-body-secondary small">{catalogName} has no backups. Its owner can add one.</div></CCol>
        )}
      </CRow>

      <BackupForm visible={formOpen} catalogId={catalogId} editing={editing} kind={formKind} ssh={data.ssh}
                  sizes={data.sizes} onClose={() => setFormOpen(false)}
                  onSaved={() => { setFormOpen(false); setNotice(editing ? 'Saved.' : 'Backup created.'); load(); }} />
      <RestoreModal visible={!!restoring} catalogId={catalogId} card={restoring}
                    onClose={() => setRestoring(null)}
                    onStarted={(target) => { setRestoring(null); setNotice(`Restoring into ${target}.`); load(); }} />
    </>
  );
};

export default BackupCards;
