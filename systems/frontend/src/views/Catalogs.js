import React, { useState } from 'react';
import { CContainer } from '@coreui/react';
import CatalogList from '../components/CatalogList';
import CatalogForm from '../components/CatalogForm';

const Catalogs = () => {
  const [showForm, setShowForm] = useState(false);
  const [selectedCatalog, setSelectedCatalog] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const handleCreate = () => {
    setSelectedCatalog(null);
    setShowForm(true);
  };

  const handleEdit = (catalog) => {
    setSelectedCatalog(catalog);
    setShowForm(true);
  };

  const handleFormClose = () => {
    setShowForm(false);
    setSelectedCatalog(null);
  };

  const handleSuccess = () => {
    setRefreshKey((prev) => prev + 1);
  };

  return (
    <CContainer fluid className="px-4 py-4">
      <CatalogList
        key={refreshKey}
        onCreate={handleCreate}
        onEdit={handleEdit}
      />
      <CatalogForm
        visible={showForm}
        onClose={handleFormClose}
        catalog={selectedCatalog}
        onSuccess={handleSuccess}
      />
    </CContainer>
  );
};

export default Catalogs;
