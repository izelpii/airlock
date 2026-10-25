import React from 'react';
import { CContainer, CAlert } from '@coreui/react';
import { useCatalog } from '../context/CatalogContext';
import CatalogDetail from '../components/CatalogDetail';

const Contents = () => {
  const { activeCatalog } = useCatalog();

  if (!activeCatalog) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="info">
          No catalog selected. Please open a catalog from the <strong>Catalogs</strong> page first.
        </CAlert>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="px-4 py-4">
      <CatalogDetail catalogId={activeCatalog.id} onClose={null} />
    </CContainer>
  );
};

export default Contents;
