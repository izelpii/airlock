import React, { useState } from 'react';
import {
  CCard,
  CCardBody,
  CButton,
  CForm,
  CFormInput,
  CFormLabel,
  CAlert,
  CSpinner,
} from '@coreui/react';
import { useAuth } from '../context/AuthContext';
import { authAPI } from '../services/api';

/**
 * The sign-in screen, and the first-run screen when no account exists yet.
 *
 * There is deliberately no registration link: accounts are created by an
 * administrator. The error message never distinguishes an unknown username from
 * a wrong password, matching the API, so this page cannot be used to find out
 * which accounts exist. See user-authentication-spec.md section 8.2.
 */
const Shell = ({ title, subtitle, children }) => (
  <div
    style={{
      minHeight: '100vh',
      display: 'flex',
      alignItems: 'center',
      justifyContent: 'center',
      background: '#ebedef',
      padding: 24,
    }}
  >
    <CCard style={{ width: '100%', maxWidth: 420 }}>
      <CCardBody className="p-4">
        <h3 className="mb-1">{title}</h3>
        {subtitle && <p className="text-muted" style={{ fontSize: '0.9rem' }}>{subtitle}</p>}
        {children}
      </CCardBody>
    </CCard>
  </div>
);

const Login = () => {
  const { login, setupRequired, completeSetup, user, setUser, reachError } = useAuth();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [displayName, setDisplayName] = useState('');
  const [email, setEmail] = useState('');
  const [confirm, setConfirm] = useState('');
  const [currentPassword, setCurrentPassword] = useState('');
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  // A password minted by the migration or by an administrator must be replaced
  // before the app is usable, so this screen also handles the forced change.
  const mustChange = Boolean(user?.must_change_password);

  const submitLogin = async (e) => {
    e.preventDefault();
    setError(null);
    setBusy(true);
    try {
      await login(username.trim(), password);
    } catch (err) {
      setError(err?.response?.data?.error || 'Sign in failed. Please try again.');
    } finally {
      setBusy(false);
    }
  };

  const submitSetup = async (e) => {
    e.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError('The two passwords do not match.');
      return;
    }
    setBusy(true);
    try {
      await completeSetup({
        username: username.trim(),
        password,
        display_name: displayName.trim() || username.trim(),
        email: email.trim() || null,
      });
    } catch (err) {
      setError(err?.response?.data?.error || 'Could not create the account.');
    } finally {
      setBusy(false);
    }
  };

  const submitPasswordChange = async (e) => {
    e.preventDefault();
    setError(null);
    if (password !== confirm) {
      setError('The two new passwords do not match.');
      return;
    }
    setBusy(true);
    try {
      const { data } = await authAPI.changePassword(currentPassword, password);
      setUser(data.user);
    } catch (err) {
      setError(err?.response?.data?.error || 'Could not change the password.');
    } finally {
      setBusy(false);
    }
  };

  if (mustChange) {
    return (
      <Shell
        title="Choose a new password"
        subtitle="This account uses a password that was generated for it. Pick your own to continue."
      >
        <CForm onSubmit={submitPasswordChange}>
          {error && <CAlert color="danger" className="py-2">{error}</CAlert>}
          <div className="mb-3">
            <CFormLabel>Current password</CFormLabel>
            <CFormInput type="password" value={currentPassword} autoComplete="current-password"
                        onChange={(e) => setCurrentPassword(e.target.value)} required />
          </div>
          <div className="mb-3">
            <CFormLabel>New password</CFormLabel>
            <CFormInput type="password" value={password} autoComplete="new-password"
                        onChange={(e) => setPassword(e.target.value)} required />
            <small className="text-muted">At least 12 characters.</small>
          </div>
          <div className="mb-4">
            <CFormLabel>Confirm new password</CFormLabel>
            <CFormInput type="password" value={confirm} autoComplete="new-password"
                        onChange={(e) => setConfirm(e.target.value)} required />
          </div>
          <CButton color="primary" type="submit" className="w-100" disabled={busy}>
            {busy ? <CSpinner size="sm" /> : 'Set password'}
          </CButton>
        </CForm>
      </Shell>
    );
  }

  if (setupRequired) {
    return (
      <Shell
        title="Welcome to Airlock Photos"
        subtitle="No account exists yet. Create the administrator account to begin."
      >
        <CForm onSubmit={submitSetup}>
          {error && <CAlert color="danger" className="py-2">{error}</CAlert>}
          <div className="mb-3">
            <CFormLabel>Username</CFormLabel>
            <CFormInput value={username} onChange={(e) => setUsername(e.target.value)}
                        autoFocus required />
          </div>
          <div className="mb-3">
            <CFormLabel>Display name</CFormLabel>
            <CFormInput value={displayName} onChange={(e) => setDisplayName(e.target.value)} />
          </div>
          <div className="mb-3">
            <CFormLabel>Email <span className="text-muted">(optional)</span></CFormLabel>
            <CFormInput type="email" value={email} onChange={(e) => setEmail(e.target.value)} />
          </div>
          <div className="mb-3">
            <CFormLabel>Password</CFormLabel>
            <CFormInput type="password" value={password} autoComplete="new-password"
                        onChange={(e) => setPassword(e.target.value)} required />
            <small className="text-muted">At least 12 characters.</small>
          </div>
          <div className="mb-4">
            <CFormLabel>Confirm password</CFormLabel>
            <CFormInput type="password" value={confirm} autoComplete="new-password"
                        onChange={(e) => setConfirm(e.target.value)} required />
          </div>
          <CButton color="primary" type="submit" className="w-100" disabled={busy}>
            {busy ? <CSpinner size="sm" /> : 'Create account'}
          </CButton>
        </CForm>
      </Shell>
    );
  }

  return (
    <Shell title="Airlock Photos" subtitle="Sign in to continue.">
      <CForm onSubmit={submitLogin}>
        {/* A server that cannot be reached is not a wrong password, and saying
            so is the difference between "try again" and knowing where to look.
            Landing here on a NEW installation means the first-run setup screen
            could not be shown, which looks identical to a forgotten password. */}
        {reachError && !error && (
          <CAlert color="warning" className="py-2">
            {reachError}
            <div className="small mt-1">
              On a new installation the first-run setup screen cannot appear
              until the server answers.
            </div>
          </CAlert>
        )}
        {error && <CAlert color="danger" className="py-2">{error}</CAlert>}
        <div className="mb-3">
          <CFormLabel>Username</CFormLabel>
          <CFormInput value={username} onChange={(e) => setUsername(e.target.value)}
                      autoComplete="username" autoFocus required />
        </div>
        <div className="mb-4">
          <CFormLabel>Password</CFormLabel>
          <CFormInput type="password" value={password} autoComplete="current-password"
                      onChange={(e) => setPassword(e.target.value)} required />
        </div>
        <CButton color="primary" type="submit" className="w-100" disabled={busy}>
          {busy ? <CSpinner size="sm" /> : 'Sign in'}
        </CButton>
      </CForm>
    </Shell>
  );
};

export default Login;
