import React from 'react';
import { useNavigate } from 'react-router-dom';
import { CButton, CCard, CCardBody, CContainer } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilSettings, cilUser } from '@coreui/icons';
import { useAuth } from '../context/AuthContext';

// ─── Site settings (the gear in the top bar) ─────────────────────────────────
// Settings of this installation. Its content comes later (owner, 2026-10-19).
// What was here - your profile, linked person, password and signed-in devices -
// is now your account's page, /users/:userId (views/UserSettings.js), opened by
// your name in the top bar or from Admin > Users.

const SiteSettings = () => {
  const navigate = useNavigate();
  const { user } = useAuth();
  return (
    <CContainer fluid className="px-4 py-4">
      <h4 className="mb-3 fw-bold" style={{ color: '#212121' }}>
        <CIcon icon={cilSettings} className="me-2" style={{ width: 22, height: 22 }} />
        Site settings
      </h4>
      <CCard style={{ maxWidth: 640 }}>
        <CCardBody>
          <p className="mb-2">Settings for this Airlock installation will be here.</p>
          <p className="text-body-secondary mb-3" style={{ fontSize: '0.9rem' }}>
            Your own details - profile, linked person, password and signed-in devices - are on
            your account page.
          </p>
          {user && (
            <CButton color="primary" variant="outline" size="sm" onClick={() => navigate(`/users/${user.id}`)}>
              <CIcon icon={cilUser} className="me-1" />Your account
            </CButton>
          )}
        </CCardBody>
      </CCard>
    </CContainer>
  );
};

export default SiteSettings;
