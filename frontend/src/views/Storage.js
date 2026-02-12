import React from 'react';
import { CCard, CCardBody, CCardHeader, CContainer } from '@coreui/react';

const Storage = () => {
  return (
    <CContainer fluid className="px-4 py-4">
      <CCard>
        <CCardHeader>
          <strong>Storage</strong>
        </CCardHeader>
        <CCardBody>
          <p className="text-muted">Storage management coming soon...</p>
        </CCardBody>
      </CCard>
    </CContainer>
  );
};

export default Storage;

// Made with Bob