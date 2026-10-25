import React, { useEffect, useState } from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import { Sidebar, Menu, MenuItem, SubMenu } from 'react-pro-sidebar';
import { useCatalog } from '../context/CatalogContext';
import { useAuth } from '../context/AuthContext';
import { systemAPI } from '../services/api';
import CIcon from '@coreui/icons-react';
import {
  cilImage,
  cilSpeedometer,
  cilBell,
  cilBook,
  cilCog,
  cilTask,
  cilSitemap,
  cilPeople,
  cilPaw,
  cilCalendar,
  cilLocationPin,
  cilMap,
  cilSearch,
  cilBookmark,
  cilSettings,
  cilList,
  cilMenu,
  cilChart,
  cilApps,
  cilUser,
  cilGroup,
  cilShareAlt,
  cilMediaPlay,
  cilTrash,
  cilFace,
  cilSwapHorizontal,
  cilLayers,
} from '@coreui/icons';
import './Sidebar.css';

const AppSidebar = ({ visible, setVisible }) => {
  const navigate = useNavigate();
  const location = useLocation();
  const { activeCatalog } = useCatalog();
  const { isAdmin } = useAuth();

  const handleNavigation = (path) => {
    navigate(path);
  };

  // Who this copy is licensed to, for the line at the foot of the panel. From
  // /api/version, as the version badge reads it (airlock_license.py). Only a
  // VALID license is named here; without one the red notice beside the logo
  // says so, and this line is simply absent.
  const [licensee, setLicensee] = useState(null);
  useEffect(() => {
    let cancelled = false;
    systemAPI.version()
      .then((res) => {
        const lic = res.data?.license;
        if (!cancelled && lic?.status === 'licensed' && lic.name) setLicensee(lic.name);
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, []);

  return (
    <Sidebar
      collapsed={!visible}
      backgroundColor="#fff"
      width="280px"
      style={{
        height: '100vh',
        borderRight: '1px solid #d8dbe0',
        position: 'fixed',
        top: 0,
        left: 0,
        zIndex: 1040,
      }}
    >
      {/* A column, so the license line can sit at the foot of the panel: pushed
          down by margin-top:auto under a short menu, after the last item under
          a long one. */}
      <div style={{ display: 'flex', flexDirection: 'column', minHeight: '100%' }}>
        <Menu
          menuItemStyles={{
            button: ({ level, active }) => {
              return {
                backgroundColor: active ? '#e3f2fd' : undefined,
                color: active ? '#1976d2' : '#5c6873',
                fontWeight: active ? 600 : 400,
                '&:hover': {
                  backgroundColor: '#f5f5f5',
                  color: '#1976d2',
                },
                paddingLeft: level === 0 ? '20px' : '40px',
              };
            },
          }}
        >
          <div
            style={{
              padding: '20px 20px 10px 20px',
              display: 'flex',
              alignItems: 'center',
              cursor: 'pointer',
            }}
            onClick={() => setVisible(!visible)}
          >
            <CIcon icon={cilMenu} size="xl" style={{ color: '#5c6873' }} />
          </div>

          {/* Home: the open catalog at a glance (dashboard-specs.md). */}
          <MenuItem
            active={location.pathname === '/'}
            icon={<CIcon icon={cilSpeedometer} style={{ width: '20px', height: '20px' }} />}
            onClick={() => handleNavigation('/')}
          >
            Dashboard
          </MenuItem>

          <SubMenu
            label="Admin"
            icon={<CIcon icon={cilSettings} style={{ width: '20px', height: '20px' }} />}
            defaultOpen={location.pathname.startsWith('/catalogs')
                         || location.pathname.startsWith('/users')
                         || location.pathname.startsWith('/storage')}
          >
            <MenuItem
              active={location.pathname === '/catalogs'}
              icon={<CIcon icon={cilBook} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/catalogs')}
            >
              Catalogs
            </MenuItem>
            {/* No Storage entry. A storage route belongs to exactly one
                catalog, so the routes are drawn beside the open catalog on the
                Catalogs page - a separate page listing them was a second place
                to look for something that only exists in one place. */}
            {/* Same gate, same reason: /api/users is @admin_required. */}
            {isAdmin && (
              <MenuItem
                active={location.pathname === '/users'}
                icon={<CIcon icon={cilUser} style={{ width: '18px', height: '18px' }} />}
                onClick={() => handleNavigation('/users')}
              >
                Users
              </MenuItem>
            )}
          </SubMenu>

          {activeCatalog && (
            <>
          <SubMenu
            label="Albums"
            icon={<CIcon icon={cilImage} style={{ width: '20px', height: '20px' }} />}
            defaultOpen={location.pathname.startsWith('/albums')}
          >
            {/* One card per album, whatever publishes it. Deployments, which
                lists what an album is published IN, is under Systems. */}
            <MenuItem
              active={location.pathname === '/albums'}
              icon={<CIcon icon={cilLayers} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/albums')}
            >
              Albums
            </MenuItem>
            {/* Both directions in one place. Sending starts from an album
                card, so this is where receiving begins and where the history of
                either direction lives. */}
            <MenuItem
              active={location.pathname.startsWith('/albums/transfer')}
              icon={<CIcon icon={cilSwapHorizontal} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/albums/transfers')}
            >
              Transfers
            </MenuItem>
          </SubMenu>

          <SubMenu
            label="Content"
            icon={<CIcon icon={cilList} style={{ width: '20px', height: '20px' }} />}
            defaultOpen={
              location.pathname.startsWith('/people') ||
              location.pathname.startsWith('/pets') ||
              location.pathname.startsWith('/events') ||
              location.pathname.startsWith('/photos') ||
              location.pathname.startsWith('/videos') ||
              location.pathname === '/locations' ||
              location.pathname.startsWith('/locations/')
            }
          >
            <MenuItem
              active={location.pathname === '/photos'}
              icon={<CIcon icon={cilImage} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/photos')}
            >
              Photos
            </MenuItem>
            <MenuItem
              active={location.pathname.startsWith('/videos')}
              icon={<CIcon icon={cilMediaPlay} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/videos')}
            >
              Videos
            </MenuItem>
            <MenuItem
              active={location.pathname === '/people'}
              icon={<CIcon icon={cilPeople} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/people')}
            >
              People
            </MenuItem>
            <MenuItem
              active={location.pathname.startsWith('/pets')}
              icon={<CIcon icon={cilPaw} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/pets')}
            >
              Pets
            </MenuItem>
            <MenuItem
              active={location.pathname === '/events'}
              icon={<CIcon icon={cilCalendar} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/events')}
            >
              Events
            </MenuItem>
            <MenuItem
              active={location.pathname === '/locations'}
              icon={<CIcon icon={cilLocationPin} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/locations')}
            >
              Locations
            </MenuItem>
          </SubMenu>

          {/* Search: finding anything in the catalog, and the saved searches
              kept from it (search-spec.md). Its own group, below Content. */}
          <SubMenu
            label="Search"
            icon={<CIcon icon={cilSearch} style={{ width: '20px', height: '20px' }} />}
            defaultOpen={location.pathname.startsWith('/search')
              || location.pathname.startsWith('/saved-searches')}
          >
            <MenuItem
              active={location.pathname === '/search'}
              icon={<CIcon icon={cilSearch} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/search')}
            >
              Search
            </MenuItem>
            <MenuItem
              active={location.pathname.startsWith('/saved-searches')}
              icon={<CIcon icon={cilBookmark} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/saved-searches')}
            >
              Saved searches
            </MenuItem>
          </SubMenu>

          {/* Views: other ways of looking at the same content. Locations is
              back under Content, with the other things a catalog holds. */}
          <SubMenu
            label="Views"
            icon={<CIcon icon={cilMap} style={{ width: '20px', height: '20px' }} />}
            defaultOpen={location.pathname.startsWith('/locations-map')
              || location.pathname.startsWith('/calendar-view')}
          >
            <MenuItem
              active={location.pathname === '/locations-map'}
              icon={<CIcon icon={cilMap} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/locations-map')}
            >
              Map view
            </MenuItem>
            <MenuItem
              active={location.pathname === '/calendar-view'}
              icon={<CIcon icon={cilCalendar} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/calendar-view')}
            >
              Calendar view
            </MenuItem>
          </SubMenu>

          <SubMenu
            label="Recognition"
            icon={<CIcon icon={cilFace} style={{ width: '20px', height: '20px' }} />}
            defaultOpen={
              location.pathname.startsWith('/identify-people') ||
              location.pathname.startsWith('/identify-groups') ||
              location.pathname.startsWith('/identify-pets')
            }
          >
            <MenuItem
              active={location.pathname === '/identify-people'}
              icon={<CIcon icon={cilPeople} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/identify-people')}
            >
              Identify people
            </MenuItem>
            <MenuItem
              active={location.pathname === '/identify-groups'}
              icon={<CIcon icon={cilGroup} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/identify-groups')}
            >
              Identify groups
            </MenuItem>
            <MenuItem
              active={location.pathname === '/identify-pets'}
              icon={<CIcon icon={cilPaw} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/identify-pets')}
            >
              Identify pets
            </MenuItem>
          </SubMenu>

          {/* Systems is a group, not a page: the two things it holds - what
              runs and where it is deployed - are the same subject seen twice. */}
          <SubMenu
            label="Systems"
            icon={<CIcon icon={cilApps} style={{ width: '20px', height: '20px' }} />}
            defaultOpen={
              location.pathname === '/workflows' ||
              location.pathname.startsWith('/albums/deployments')
            }
          >
            <MenuItem
              active={location.pathname === '/albums/deployments'}
              icon={<CIcon icon={cilShareAlt} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/albums/deployments')}
            >
              Deployments
            </MenuItem>
            <MenuItem
              active={location.pathname === '/workflows'}
              icon={<CIcon icon={cilSitemap} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/workflows')}
            >
              Workflows
            </MenuItem>
          </SubMenu>

          <SubMenu
            label="Reports"
            icon={<CIcon icon={cilChart} style={{ width: '20px', height: '20px' }} />}
            defaultOpen={
              location.pathname.startsWith('/operations') ||
              location.pathname.startsWith('/alerts') ||
              location.pathname.startsWith('/content-report') ||
              location.pathname.startsWith('/removed-files') ||
              location.pathname.startsWith('/import-progress') ||
              location.pathname.startsWith('/catalog-files') ||
              location.pathname.startsWith('/workflow-executions')
            }
          >            <MenuItem
              active={location.pathname === '/content-report'}
              icon={<CIcon icon={cilChart} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/content-report')}
            >
              Content report
            </MenuItem>
            {/* The catalog's files, as a table with paths, permissions and
                per-file EXIF. Named "Files" rather than "Contents": the latter
                is what the Content report below gets called, and two entries
                a line apart answering to the same word is how someone ends up
                on the wrong page. /contents redirects to that report. */}
            <MenuItem
              active={location.pathname === '/catalog-files'}
              icon={<CIcon icon={cilList} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/catalog-files')}
            >
              Files
            </MenuItem>
            <MenuItem
              active={location.pathname === '/workflow-executions'}
              icon={<CIcon icon={cilTask} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/workflow-executions')}
            >
              Workflow Executions
            </MenuItem>
            <MenuItem
              active={location.pathname === '/import-progress'}
              icon={<CIcon icon={cilTask} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/import-progress')}
            >
              Import progress
            </MenuItem>
            <MenuItem
              active={location.pathname === '/operations'}
              icon={<CIcon icon={cilCog} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/operations')}
            >
              Catalog report
            </MenuItem>
            <MenuItem
              active={location.pathname === '/removed-files'}
              icon={<CIcon icon={cilTrash} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/removed-files')}
            >
              Removed files
            </MenuItem>

            <MenuItem
              active={location.pathname === '/alerts'}
              icon={<CIcon icon={cilBell} style={{ width: '18px', height: '18px' }} />}
              onClick={() => handleNavigation('/alerts')}
            >
              Alerts
            </MenuItem>
          </SubMenu>
            </>
          )}
        </Menu>

        {/* Text only - hidden when the panel is collapsed to its icons. */}
        {visible && licensee && (
          <div
            data-testid="sidebar-licensee"
            style={{
              marginTop: 'auto',
              padding: '14px 20px 16px',
              borderTop: '1px solid #eef0f2',
              fontSize: '0.75rem',
              color: '#5c6873',
              lineHeight: 1.4,
              overflowWrap: 'anywhere',
            }}
          >
            Licensed to: {licensee}
          </div>
        )}
      </div>
    </Sidebar>
  );
};

export default AppSidebar;
