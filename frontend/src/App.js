import React, { useState } from 'react';
import { BrowserRouter as Router, Routes, Route } from 'react-router-dom';
import '@coreui/coreui/dist/css/coreui.min.css';
import './App.css';
import { CContainer } from '@coreui/react';
import Sidebar from './components/Sidebar';
import Dashboard from './views/Dashboard';
import Storage from './views/Storage';
import Catalogs from './views/Catalogs';
import Operations from './views/Operations';

function App() {
  const [sidebarVisible, setSidebarVisible] = useState(true);

  return (
    <Router>
      <div className="d-flex">
        <Sidebar visible={sidebarVisible} setVisible={setSidebarVisible} />
        <div className="wrapper d-flex flex-column min-vh-100 bg-light" style={{ marginLeft: sidebarVisible ? '256px' : '0', transition: 'margin-left 0.3s' }}>
          <div className="body flex-grow-1">
            <Routes>
              <Route path="/" element={<Dashboard />} />
              <Route path="/storage" element={<Storage />} />
              <Route path="/catalogs" element={<Catalogs />} />
              <Route path="/operations" element={<Operations />} />
            </Routes>
          </div>
        </div>
      </div>
    </Router>
  );
}

export default App;

// Made with Bob
