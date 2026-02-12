import React from 'react';
import { useNavigate, useLocation } from 'react-router-dom';
import {
  CSidebar,
  CSidebarBrand,
  CSidebarNav,
  CNavItem,
  CNavTitle,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilStorage,
  cilBook,
  cilCog,
  cilTask,
} from '@coreui/icons';

const Sidebar = ({ visible, setVisible }) => {
  const navigate = useNavigate();
  const location = useLocation();

  const menuItems = [
    {
      name: 'Storage',
      path: '/storage',
      icon: cilStorage,
    },
    {
      name: 'Catalogs',
      path: '/catalogs',
      icon: cilBook,
    },
    {
      name: 'Operations',
      path: '/operations',
      icon: cilCog,
    },
    {
      name: 'Workflows',
      path: '/',
      icon: cilTask,
    },
  ];

  return (
    <CSidebar
      position="fixed"
      visible={visible}
      onVisibleChange={(val) => setVisible(val)}
      className="sidebar border-end"
      colorScheme="dark"
    >
      <CSidebarBrand className="d-none d-md-flex">
        <div className="sidebar-brand-full px-3 py-2">
          <h5 className="mb-0">Workflow Manager</h5>
        </div>
      </CSidebarBrand>
      <CSidebarNav className="sidebar-nav">
        <CNavTitle className="nav-title">Menu</CNavTitle>
        {menuItems.map((item) => (
          <CNavItem
            key={item.path}
            href="#"
            active={location.pathname === item.path}
            onClick={(e) => {
              e.preventDefault();
              navigate(item.path);
            }}
            className="nav-item"
          >
            <CIcon icon={item.icon} customClassName="nav-icon" />
            {item.name}
          </CNavItem>
        ))}
      </CSidebarNav>
    </CSidebar>
  );
};

export default Sidebar;

// Made with Bob