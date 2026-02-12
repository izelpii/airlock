import React from 'react';
import { CCard, CCardBody, CCardHeader, CContainer } from '@coreui/react';

const Operations = () => {
  return (
    <CContainer fluid className="px-4 py-4">
      <CCard>
        <CCardHeader>
          <strong>Operations</strong>
        </CCardHeader>
        <CCardBody>
          <p className="text-muted">Operations management coming soon...</p>
        </CCardBody>
      </CCard>
    </CContainer>
  );
};

export default Operations;

// Made with Bob