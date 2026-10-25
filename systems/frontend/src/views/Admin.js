import React from 'react';
import { CContainer, CCard, CCardBody, CCardHeader } from '@coreui/react';

const Admin = () => {
  return (
    <CContainer fluid className="p-4">
      <CCard>
        <CCardHeader>
          <h4 className="mb-0">Admin</h4>
        </CCardHeader>
        <CCardBody>
          <p className="text-muted">Admin content coming soon.</p>
        </CCardBody>
      </CCard>
    </CContainer>
  );
};

export default Admin;
