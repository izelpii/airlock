import React, { createContext, useContext, useEffect, useState, useCallback } from 'react';
import { authAPI, setUnauthenticatedHandler } from '../services/api';

/**
 * Who is signed in, for the whole application.
 *
 * The gate this provides is a usability redirect, not the security boundary:
 * nginx serves index.html and the compiled bundle before any API call happens,
 * so the bundle is not a secret. The enforcement is the API returning 401.
 * See documentation/specs/user-authentication-spec.md sections 3 and 8.1.
 */
const AuthContext = createContext(null);

export const useAuth = () => {
  const ctx = useContext(AuthContext);
  if (!ctx) throw new Error('useAuth must be used inside <AuthProvider>');
  return ctx;
};

export const AuthProvider = ({ children }) => {
  const [user, setUser] = useState(null);
  const [loading, setLoading] = useState(true);
  const [setupRequired, setSetupRequired] = useState(false);
  // Set when we could not ask the server whether setup is needed. Without this
  // the failure was invisible: the catch below fell back to `false`, and an
  // unreachable backend presented a password prompt for an account that does
  // not exist - an unwinnable screen that says nothing about what is wrong.
  const [reachError, setReachError] = useState(null);

  const probe = useCallback(async () => {
    try {
      const { data } = await authAPI.session();
      setUser(data.user);
      setSetupRequired(false);
    } catch (err) {
      // A 401 here is the expected "not signed in" answer, not a failure.
      setUser(null);
      try {
        const { data } = await authAPI.setupRequired();
        // A 200 IS NOT ENOUGH. Loaded from a port that serves only the SPA -
        // :3000, where nginx has no /api proxy - this request returns
        // index.html with status 200. `data.setup_required` is then undefined,
        // Boolean(undefined) is false, and the user gets a sign-in form for an
        // account that does not exist, with nothing reported because nothing
        // threw. Demand the actual answer.
        if (typeof data?.setup_required !== 'boolean') {
          setSetupRequired(false);
          setReachError('The server replied, but not with an answer this app '
            + 'understands - it looks like the page was loaded from somewhere '
            + 'that does not serve the API. Open the address the gateway '
            + 'provides (https://airlock.lan) rather than a service port.');
          return;
        }
        setSetupRequired(data.setup_required);
        setReachError(null);
      } catch (probeErr) {
        // Still false - there is no evidence setup is needed - but say so.
        setSetupRequired(false);
        const status = probeErr?.response?.status;
        setReachError(status
          ? `The server answered ${status} when asked whether an account exists.`
          : 'The server could not be reached. It may still be starting, or the '
            + 'gateway may not be routing /api yet.');
      }
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => { probe(); }, [probe]);

  // Any 401 from a non-probe request means the session went away underneath us
  // — it expired, or someone signed this device out from another one.
  useEffect(() => {
    setUnauthenticatedHandler(() => setUser(null));
    return () => setUnauthenticatedHandler(null);
  }, []);

  const login = useCallback(async (username, password) => {
    const { data } = await authAPI.login(username, password);
    setUser(data.user);
    return data.user;
  }, []);

  const completeSetup = useCallback(async (payload) => {
    const { data } = await authAPI.setup(payload);
    setUser(data.user);
    setSetupRequired(false);
    return data.user;
  }, []);

  const logout = useCallback(async () => {
    try {
      await authAPI.logout();
    } finally {
      setUser(null);
    }
  }, []);

  const value = {
    user,
    loading,
    setupRequired,
    reachError,
    login,
    logout,
    completeSetup,
    refresh: probe,
    setUser,
    isAdmin: Boolean(user?.is_admin),
  };

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
};

export default AuthContext;
