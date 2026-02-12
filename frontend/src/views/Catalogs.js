import React, { useState } from 'react';
import { CContainer } from '@coreui/react';
import CatalogList from '../components/CatalogList';
import CatalogForm from '../components/CatalogForm';
import CatalogDetail from '../components/CatalogDetail';

const Catalogs = () => {
  const [showForm, setShowForm] = useState(false);
  const [showDetail, setShowDetail] = useState(false);
  const [selectedCatalog, setSelectedCatalog] = useState(null);
  const [selectedCatalogId, setSelectedCatalogId] = useState(null);
  const [refreshKey, setRefreshKey] = useState(0);

  const handleCreate = () => {
    setSelectedCatalog(null);
    setShowForm(true);
  };

  const handleEdit = (catalog) => {
    setSelectedCatalog(catalog);
    setShowForm(true);
  };

  const handleView = (catalog) => {
    setSelectedCatalogId(catalog.id);
    setShowDetail(true);
  };

  const handleFormClose = () => {
    setShowForm(false);
    setSelectedCatalog(null);
  };

  const handleDetailClose = () => {
    setShowDetail(false);
    setSelectedCatalogId(null);
  };

  const handleSuccess = () => {
    setRefreshKey((prev) => prev + 1);
  };

  return (
    <CContainer fluid className="px-4 py-4">
      {!showDetail ? (
        <CatalogList
          key={refreshKey}
          onCreate={handleCreate}
          onEdit={handleEdit}
          onView={handleView}
        />
      ) : (
        <CatalogDetail
          catalogId={selectedCatalogId}
          onClose={handleDetailClose}
        />
      )}
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

// Made with Bob