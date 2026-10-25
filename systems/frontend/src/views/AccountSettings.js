import React, { useCallback, useEffect, useState } from 'react';
import {
  CCard, CCardBody, CCardHeader, CButton, CForm, CFormInput, CFormLabel,
  CFormSelect, CAlert, CSpinner, CBadge,
} from '@coreui/react';
import { useAuth } from '../context/AuthContext';
import UserAvatar from '../components/UserAvatar';
import { authAPI, personLinkAPI, userAPI } from '../services/api';

/**
 * The cards of an account's page (views/UserSettings.js, /users/:userId):
 * password, signed-in devices, and the person the account is linked to.
 *
 * Each takes the account it is about. On your own page they behave as the old
 * Settings page did; on someone else's - an administrator's view from Admin >
 * Users - the password card SETS a new one (they choose their own at the next
 * sign in) and the devices card lists and signs out theirs.
 * See user-authentication-spec.md section 8.3.
 */

/**
 * A standalone card, for the parts that are about other people or other
 * catalogs rather than about the signed-in user.
 */
const Section = ({ title, children, note, className = 'mb-4' }) => (
  <CCard className={className}>
    <CCardHeader><strong>{title}</strong></CCardHeader>
    <CCardBody>
      {note && <p className="text-muted" style={{ fontSize: '0.85rem' }}>{note}</p>}
      {children}
    </CCardBody>
  </CCard>
);

// ─── Password ─────────────────────────────────────────────────────────────────

// Someone else's password, set by an administrator.
const SetPasswordSection = ({ userId, className = 'mb-4' }) => {
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setMsg(null);
    if (next !== confirm) {
      setMsg({ color: 'danger', text: 'The two passwords do not match.' });
      return;
    }
    setBusy(true);
    try {
      await userAPI.setPassword(userId, next);
      setNext(''); setConfirm('');
      setMsg({ color: 'success',
               text: 'Password set. They must choose their own at the next sign in; their devices were signed out.' });
    } catch (err) {
      setMsg({ color: 'danger', text: err?.response?.data?.error || 'Could not set the password.' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Password" className={className}
             note="Set a temporary password. They must choose their own at the next sign in, and are signed out everywhere.">
      <CForm onSubmit={submit}>
        {msg && <CAlert color={msg.color} className="py-2">{msg.text}</CAlert>}
        <div className="mb-3">
          <CFormLabel>New password</CFormLabel>
          <CFormInput type="password" value={next} autoComplete="new-password"
                      onChange={(e) => setNext(e.target.value)} required />
          <small className="text-muted">At least 12 characters.</small>
        </div>
        <div className="mb-3">
          <CFormLabel>Confirm new password</CFormLabel>
          <CFormInput type="password" value={confirm} autoComplete="new-password"
                      onChange={(e) => setConfirm(e.target.value)} required />
        </div>
        <CButton color="primary" type="submit" disabled={busy}>
          {busy ? <CSpinner size="sm" /> : 'Set password'}
        </CButton>
      </CForm>
    </Section>
  );
};

// Your own password, changed with the current one.
const PasswordSection = ({ userId, className = 'mb-4' }) => {
  const { user } = useAuth();
  if (userId != null && user && userId !== user.id) {
    return <SetPasswordSection userId={userId} className={className} />;
  }
  return <OwnPasswordSection className={className} />;
};

const OwnPasswordSection = ({ className = 'mb-4' }) => {
  const [current, setCurrent] = useState('');
  const [next, setNext] = useState('');
  const [confirm, setConfirm] = useState('');
  const [msg, setMsg] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setMsg(null);
    if (next !== confirm) {
      setMsg({ color: 'danger', text: 'The two new passwords do not match.' });
      return;
    }
    setBusy(true);
    try {
      await authAPI.changePassword(current, next);
      setCurrent(''); setNext(''); setConfirm('');
      setMsg({ color: 'success', text: 'Password changed. Your other devices were signed out.' });
    } catch (err) {
      setMsg({ color: 'danger', text: err?.response?.data?.error || 'Could not change the password.' });
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Password" className={className}
             note="Changing your password signs out every other device.">
      <CForm onSubmit={submit}>
        {msg && <CAlert color={msg.color} className="py-2">{msg.text}</CAlert>}
        <div className="mb-3">
          <CFormLabel>Current password</CFormLabel>
          <CFormInput type="password" value={current} autoComplete="current-password"
                      onChange={(e) => setCurrent(e.target.value)} required />
        </div>
        <div className="mb-3">
          <CFormLabel>New password</CFormLabel>
          <CFormInput type="password" value={next} autoComplete="new-password"
                      onChange={(e) => setNext(e.target.value)} required />
          <small className="text-muted">At least 12 characters.</small>
        </div>
        <div className="mb-3">
          <CFormLabel>Confirm new password</CFormLabel>
          <CFormInput type="password" value={confirm} autoComplete="new-password"
                      onChange={(e) => setConfirm(e.target.value)} required />
        </div>
        <CButton color="primary" type="submit" disabled={busy}>
          {busy ? <CSpinner size="sm" /> : 'Change password'}
        </CButton>
      </CForm>
    </Section>
  );
};

/**
 * A short, human name for a session's device.
 *
 * The raw user agent is 100+ characters of boilerplate and does not fit a
 * narrow card; the browser and platform are what identifies a device to the
 * person deciding whether to sign it out.
 */
const describeAgent = (ua = '') => {
  const browser =
    /Edg\//.test(ua) ? 'Edge' :
    /OPR\//.test(ua) ? 'Opera' :
    /Chrome\//.test(ua) ? 'Chrome' :
    /Safari\//.test(ua) ? 'Safari' :
    /Firefox\//.test(ua) ? 'Firefox' :
    /curl\//.test(ua) ? 'curl' : null;
  const platform =
    /iPhone|iPad/.test(ua) ? 'iOS' :
    /Android/.test(ua) ? 'Android' :
    /Mac OS X/.test(ua) ? 'macOS' :
    /Windows/.test(ua) ? 'Windows' :
    /Linux/.test(ua) ? 'Linux' : null;
  const parts = [browser, platform].filter(Boolean);
  return parts.length ? parts.join(' on ') : ua.slice(0, 24);
};

// ─── Sessions ─────────────────────────────────────────────────────────────────

const SessionsSection = ({ userId, className = 'mb-4' }) => {
  const [rows, setRows] = useState([]);
  const [busy, setBusy] = useState(false);
  const [done, setDone] = useState(false);
  const { user, logout } = useAuth();
  const own = userId == null || (user && userId === user.id);

  const load = useCallback(async () => {
    try {
      const { data } = own ? await authAPI.sessions() : await userAPI.sessions(userId);
      setRows(data);
    } catch {
      setRows([]);
    }
  }, [own, userId]);

  useEffect(() => { load(); }, [load]);

  const signOutEverywhere = async () => {
    setBusy(true);
    try {
      if (own) {
        await authAPI.logoutAll();
        await logout();
      } else {
        await userAPI.signOutEverywhere(userId);
        setDone(true);
        await load();
      }
    } finally {
      setBusy(false);
    }
  };

  return (
    <Section title="Signed-in devices" className={className}
             note={own
               ? 'Each row is a live session. Signing out everywhere ends all of them, including this one.'
               : 'Each row is a live session of this account. Signing out everywhere ends all of them.'}>
      {done && <CAlert color="success" className="py-2">Signed out on every device.</CAlert>}
      {/* A list rather than a table: this card is a third of the width, and
          three columns of timestamp, address and user agent do not fit. */}
      {rows.length === 0 ? (
        <p className="text-muted mb-3">{own ? 'No other sessions.' : 'Not signed in anywhere.'}</p>
      ) : (
        <div className="mb-3">
          {rows.map((s) => (
            <div
              key={s.id}
              className="py-2"
              style={{ borderTop: '1px solid #f0f0f0' }}
            >
              <div className="d-flex align-items-center justify-content-between">
                <span style={{ fontSize: '0.9rem' }}>
                  {s.last_seen_at
                    ? String(s.last_seen_at).replace('T', ' ').slice(0, 16)
                    : '—'}
                </span>
                {s.current && <CBadge color="success">this device</CBadge>}
              </div>
              <div className="text-muted" style={{ fontSize: '0.78rem', wordBreak: 'break-word' }}>
                {s.ip || '—'}
                {s.user_agent ? ` · ${describeAgent(s.user_agent)}` : ''}
              </div>
            </div>
          ))}
        </div>
      )}
      <CButton color="danger" variant="outline" onClick={signOutEverywhere} disabled={busy}>
        Sign out everywhere
      </CButton>
    </Section>
  );
};

// ─── Linked person ────────────────────────────────────────────────────────────

/**
 * Links this account to a person in a catalog, so the profile can borrow their
 * details and their face.
 *
 * A user and a person are separate on purpose: a person exists whether or not
 * they can sign in, and an account exists whether or not it appears in any
 * photo. This is the join between them, and it is a (catalog, person) pair
 * because people.id is unique only within a catalog.
 */
const LinkedPersonSection = ({ userId, className = 'mb-4 h-100' }) => {
  const { user: me, refresh } = useAuth();
  // The account this card is about: the page's, else the signed-in one.
  const user = userId != null ? { id: userId } : me;
  const own = me && user && me.id === user.id;
  const [person, setPerson] = useState(null);
  const [dangling, setDangling] = useState(false);
  const [options, setOptions] = useState([]);
  const [loading, setLoading] = useState(true);
  const [msg, setMsg] = useState(null);
  const [version, setVersion] = useState(0);

  const load = useCallback(async () => {
    if (!user) return;
    setLoading(true);
    try {
      const [{ data: linked }, { data: list }] = await Promise.all([
        personLinkAPI.get(user.id),
        personLinkAPI.linkable(),
      ]);
      setPerson(linked.person);
      setDangling(Boolean(linked.dangling));
      setOptions(list || []);
    } catch {
      setPerson(null);
      setOptions([]);
    } finally {
      setLoading(false);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [user?.id]);

  useEffect(() => { load(); }, [load]);

  const choose = async (value) => {
    setMsg(null);
    if (!value) return;
    const [catalogId, personId] = value.split(':').map(Number);
    try {
      await personLinkAPI.set(user.id, catalogId, personId);
      setVersion((v) => v + 1);   // force the avatar to refetch
      await load();
      if (own) await refresh();   // the top bar shows your own face
    } catch (err) {
      setMsg({ color: 'danger', text: err?.response?.data?.error || 'Could not link that person.' });
    }
  };

  const unlink = async () => {
    setMsg(null);
    try {
      await personLinkAPI.clear(user.id);
      setVersion((v) => v + 1);
      await load();
      if (own) await refresh();
    } catch (err) {
      setMsg({ color: 'danger', text: err?.response?.data?.error || 'Could not unlink.' });
    }
  };

  return (
    <Section
      title="Linked person"
      className={className}
      note="Borrow a person's details and face for this account."
    >
      {msg && <CAlert color={msg.color} className="py-2">{msg.text}</CAlert>}
      {dangling && (
        <CAlert color="warning" className="py-2">
          This account points at a person that no longer exists. Pick another, or unlink.
        </CAlert>
      )}

      {loading ? (
        <CSpinner size="sm" />
      ) : (
        <>
          {person ? (
            <div className="d-flex align-items-center gap-3 mb-3">
              <UserAvatar userId={user.id} name={person.name} size={64} version={version} />
              <div style={{ minWidth: 0 }}>
                <div style={{ fontWeight: 600 }}>{person.name}</div>
                <div className="text-muted" style={{ fontSize: '0.82rem' }}>
                  {person.catalog_name}
                  {person.category ? ` · ${person.category}` : ''}
                </div>
                {person.date_of_birth && (
                  <div className="text-muted" style={{ fontSize: '0.82rem' }}>
                    Born {person.date_of_birth}
                  </div>
                )}
                {person.description && (
                  <div className="text-muted" style={{ fontSize: '0.8rem' }}>
                    {person.description}
                  </div>
                )}
                {!person.has_avatar && (
                  <div className="text-muted" style={{ fontSize: '0.78rem' }}>
                    No face picture for this person yet.
                  </div>
                )}
              </div>
            </div>
          ) : (
            <p className="text-muted mb-3">Not linked to anyone.</p>
          )}

          <CFormLabel>{person ? 'Change to' : 'Link to a person'}</CFormLabel>
          <CFormSelect
            size="sm"
            value=""
            className="mb-3"
            onChange={(e) => choose(e.target.value)}
          >
            <option value="">Choose a person…</option>
            {options.map((o) => {
              const taken = o.linked_to && !(person && person.person_id === o.person_id
                                             && person.catalog_id === o.catalog_id);
              return (
                <option
                  key={`${o.catalog_id}:${o.person_id}`}
                  value={`${o.catalog_id}:${o.person_id}`}
                  disabled={Boolean(taken)}
                >
                  {o.name}
                  {options.some((x) => x.catalog_id !== o.catalog_id) ? ` — ${o.catalog_name}` : ''}
                  {taken ? ` (linked to ${o.linked_to})` : ''}
                </option>
              );
            })}
          </CFormSelect>

          {person && (
            <CButton color="secondary" variant="outline" size="sm" onClick={unlink}>
              Unlink
            </CButton>
          )}
        </>
      )}
    </Section>
  );
};

export const PasswordCard = PasswordSection;
export const SessionsCard = SessionsSection;
export const LinkedPersonCard = LinkedPersonSection;

// Sharing is per catalog, on the back of each catalog card
// (components/CatalogSharePanel.js). Account administration is Admin > Users.

