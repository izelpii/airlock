import React from 'react';
import { useNavigate } from 'react-router-dom';
import {
  CHeader,
  CContainer,
  CHeaderNav,
  CNavItem,
  CNavLink,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilSettings,
  cilMenu,
  cilAccountLogout,
} from '@coreui/icons';
import CatalogSelector from './CatalogSelector';
import { useAuth } from '../context/AuthContext';
import UserAvatar from './UserAvatar';
import AlertsBell from './AlertsBell';
import VersionBadge from './VersionBadge';
import { assetUrl } from '../config';

const TopMenu = ({ sidebarVisible, setSidebarVisible }) => {
  const navigate = useNavigate();

  const handleUserSettingsClick = (e) => {
    e.preventDefault();
    navigate('/settings');
  };

  const { user, logout } = useAuth();

  return (
    <CHeader
      position="sticky"
      className="top-menu"
      style={{ marginBottom: 0 }}
    >
      <CContainer fluid style={{ height: '100%', display: 'flex', alignItems: 'center' }}>
        <div
          style={{
            display: 'flex',
            alignItems: 'center',
            gap: '10px',
            padding: '0 20px',
          }}
        >
          <img
            src={assetUrl('logo.png')}
            alt="Airlock Photos"
            style={{
              height: '40px',
              objectFit: 'contain',
            }}
          />
          <VersionBadge />
        </div>
        <CHeaderNav className="ms-auto" style={{ display: 'flex', alignItems: 'center' }}>
          {/* Who is signed in. It sits at the head of the right-hand group -
              ahead of the catalog, notifications, settings and sign-out - so
              everything about the current session reads left to right in one
              place, rather than the identity being stranded beside the logo. */}
          {/* Your name opens your account's page: profile, linked person,
              password, signed-in devices. */}
          {user && (
            <div
              role="link"
              tabIndex={0}
              title="Your account"
              onClick={() => navigate(`/users/${user.id}`)}
              onKeyDown={(e) => { if (e.key === 'Enter') navigate(`/users/${user.id}`); }}
              style={{
                display: 'flex',
                alignItems: 'center',
                gap: '8px',
                marginRight: '16px',
                cursor: 'pointer',
              }}
            >
              <UserAvatar userId={user.id} name={user.display_name} size={32}
                          version={user.person_id || 0} />
              <span
                style={{
                  fontSize: '0.95rem',
                  color: '#1e293b',
                  whiteSpace: 'nowrap',
                }}
              >
                {user.display_name}
              </span>
            </div>
          )}
          {/* Search from anywhere: Enter opens the Search page with the box's
              words (search-spec.md, decision 4). */}
          {user && (
            <form onSubmit={(e) => {
                    e.preventDefault();
                    const q = e.target.elements.q.value.trim();
                    navigate(q ? `/search?q=${encodeURIComponent(q)}` : '/search');
                    e.target.reset();
                  }}
                  style={{ marginRight: 12 }}>
              <input name="q" type="search" placeholder="Search…" aria-label="Search"
                     className="form-control form-control-sm" style={{ width: 200 }} />
            </form>
          )}
          <CatalogSelector />
          <AlertsBell />
          <CNavItem>
            <CNavLink
              href="#"
              className="top-menu-icon"
              onClick={handleUserSettingsClick}
              style={{ cursor: 'pointer' }}
              title="Site settings"
              aria-label="Site settings"
            >
              <CIcon icon={cilSettings} size="lg" />
            </CNavLink>
          </CNavItem>
          {/* The way out. Without a visible sign-out the only way to end a
              session is to wait 30 days for it to expire. */}
          {user && (
            <CNavItem>
              <CNavLink
                href="#"
                className="top-menu-icon"
                onClick={(e) => { e.preventDefault(); logout(); }}
                style={{ cursor: 'pointer' }}
                title="Sign out"
                aria-label="Sign out"
              >
                <CIcon icon={cilAccountLogout} size="lg" />
              </CNavLink>
            </CNavItem>
          )}
        </CHeaderNav>
      </CContainer>
    </CHeader>
  );
};

export default TopMenu;
