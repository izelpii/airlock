import React, { useCallback, useEffect, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  CBadge, CDropdown, CDropdownDivider, CDropdownHeader, CDropdownItem, CDropdownMenu,
  CDropdownToggle, CSpinner,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilBell } from '@coreui/icons';
import { alertsAPI } from '../services/api';
import socketService from '../services/socket';

// The top bar's bell: the central place alerts reach the user. The badge counts
// open alerts nobody has acknowledged, red when one is critical; the menu lists
// the ten most recent, a line each, with the full list one click away on
// Reports > Alerts. Pushed by the backend (alerts_updated) and polled once a
// minute too - a missed socket event must not leave a full disk unannounced.
const POLL_MS = 60000;
const RECENT = 10;

// Timestamps are stored in UTC without a zone.
const parseUtc = (s) => (s ? new Date(`${s.replace(' ', 'T')}Z`) : null);

export const timeAgo = (s, now = Date.now()) => {
  const d = parseUtc(s);
  if (!d || Number.isNaN(d.getTime())) return '';
  const mins = Math.max(0, Math.round((now - d.getTime()) / 60000));
  if (mins < 1) return 'just now';
  if (mins < 60) return `${mins} min ago`;
  const hours = Math.round(mins / 60);
  if (hours < 24) return `${hours} h ago`;
  return d.toLocaleDateString();
};

const dotColour = (a) => (!a.active ? '#adb5bd' : a.severity === 'critical' ? '#dc3545' : '#f0ad4e');

const AlertsBell = () => {
  const navigate = useNavigate();
  const [counts, setCounts] = useState({ active: 0, critical: 0, unacknowledged: 0 });
  const [recent, setRecent] = useState(null);     // null until the menu first opens

  const loadCounts = useCallback(async () => {
    try {
      const { data } = await alertsAPI.list({ limit: 1 });
      setCounts({ active: data.active, critical: data.critical,
                  unacknowledged: data.unacknowledged });
    } catch {
      // Signed out, or the backend is down: no badge rather than a wrong one.
    }
  }, []);

  const loadRecent = useCallback(async () => {
    try {
      const { data } = await alertsAPI.list({ includeResolved: true, recent: true, limit: RECENT });
      setRecent(data.alerts || []);
      setCounts({ active: data.active, critical: data.critical,
                  unacknowledged: data.unacknowledged });
    } catch {
      setRecent([]);
    }
  }, []);

  useEffect(() => {
    loadCounts();
    const timer = setInterval(loadCounts, POLL_MS);
    socketService.connect();
    const off = socketService.onAlertsUpdated((c) => {
      if (c) setCounts(c);
      setRecent(null);                            // stale: refetched on next open
    });
    return () => { clearInterval(timer); if (typeof off === 'function') off(); };
  }, [loadCounts]);

  const shown = counts.unacknowledged;
  const label = counts.active
    ? `${counts.active} open alert${counts.active === 1 ? '' : 's'}`
      + (counts.critical ? `, ${counts.critical} critical` : '')
    : 'No open alerts';

  return (
    <CDropdown variant="nav-item" alignment="end" onShow={loadRecent}>
      <CDropdownToggle
        caret={false}
        className="top-menu-icon position-relative"
        title={label}
        aria-label={label}
        data-testid="alerts-toggle"
      >
        <CIcon icon={cilBell} size="lg" />
        {shown > 0 && (
          <CBadge
            color={counts.critical ? 'danger' : 'warning'}
            shape="rounded-pill"
            className="position-absolute top-0 start-100 translate-middle"
            style={{ fontSize: '0.6rem' }}
            data-testid="alerts-badge"
          >
            {shown}
          </CBadge>
        )}
      </CDropdownToggle>
      <CDropdownMenu className="pt-0" style={{ width: 360, maxWidth: '90vw' }}
                     data-testid="alerts-menu">
        <CDropdownHeader className="bg-light fw-semibold py-2 d-flex justify-content-between">
          <span>Alerts</span>
          <span className="text-muted fw-normal">{label}</span>
        </CDropdownHeader>
        {recent === null && (
          <div className="text-center py-3"><CSpinner size="sm" /></div>
        )}
        {recent && recent.length === 0 && (
          <div className="text-muted px-3 py-3" style={{ fontSize: '0.85rem' }}>
            Nothing to report.
          </div>
        )}
        {recent && recent.map((a) => (
          <CDropdownItem
            key={a.id}
            href="#"
            onClick={(e) => { e.preventDefault(); navigate('/alerts'); }}
            className="d-flex align-items-center gap-2 py-2"
            style={{ fontSize: '0.83rem', whiteSpace: 'nowrap' }}
            data-testid="alerts-menu-item"
            title={a.message}
          >
            <span style={{ width: 8, height: 8, borderRadius: '50%', flexShrink: 0,
                           background: dotColour(a) }} />
            <span className="text-truncate"
                  style={{ flex: 1, minWidth: 0, color: a.active ? undefined : '#6c757d' }}>
              {a.title}
            </span>
            <span className="text-muted" style={{ fontSize: '0.72rem', flexShrink: 0 }}>
              {a.active ? timeAgo(a.last_seen) : 'resolved'}
            </span>
          </CDropdownItem>
        ))}
        <CDropdownDivider className="my-0" />
        <CDropdownItem
          href="#"
          onClick={(e) => { e.preventDefault(); navigate('/alerts'); }}
          className="text-center py-2"
          style={{ fontSize: '0.83rem' }}
        >
          View all alerts
        </CDropdownItem>
      </CDropdownMenu>
    </CDropdown>
  );
};

export default AlertsBell;
