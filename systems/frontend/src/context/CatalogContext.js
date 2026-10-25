import React, { createContext, useContext, useState, useEffect } from 'react';
import { catalogAPI, embeddingModelAPI, photoEditAPI } from '../services/api';
import { applyEmbeddingModel } from '../utils/similarity';
import { setEditVersions } from '../utils/editVersions';

const CatalogContext = createContext(null);

const STORAGE_KEY = 'activeCatalogId';

export const CatalogProvider = ({ children }) => {
  const [activeCatalog, setActiveCatalogState] = useState(null);
  const [initialized, setInitialized] = useState(false);

  // On mount, try to restore the previously selected catalog from localStorage
  useEffect(() => {
    const savedId = localStorage.getItem(STORAGE_KEY);
    if (!savedId) {
      setInitialized(true);
      return;
    }
    catalogAPI.getById(savedId)
      .then((res) => setActiveCatalogState(res.data))
      .catch(() => localStorage.removeItem(STORAGE_KEY))
      .finally(() => setInitialized(true));
  }, []);

  // Which model this catalog's face embeddings came from, and therefore which
  // thresholds the review pages should use. Fetched here because this is the
  // one place that knows the catalog, and applied into utils/similarity so the
  // pages do not each have to ask. A failure is silent on purpose: the
  // fallback is VGG-Face's measured threshold, which is correct for every
  // catalog that has not migrated.
  const [embeddingModel, setEmbeddingModel] = useState(null);
  useEffect(() => {
    if (!activeCatalog?.id) {
      setEmbeddingModel(null);
      return;
    }
    let cancelled = false;
    embeddingModelAPI.get(activeCatalog.id)
      .then((res) => {
        if (cancelled) return;
        setEmbeddingModel(applyEmbeddingModel(res.data));
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [activeCatalog?.id]);

  // Which photos have been edited, for the thumbnail URLs
  // (utils/editVersions.js, basic-photo-editing.md section 7.2). Until this
  // answers, URLs carry no version and are revalidated instead.
  useEffect(() => {
    if (!activeCatalog?.id) return;
    photoEditAPI.versions(activeCatalog.id)
      .then((res) => setEditVersions(activeCatalog.id, res.data.versions))
      .catch(() => {});
  }, [activeCatalog?.id]);

  const setActiveCatalog = (catalog) => {
    setActiveCatalogState(catalog);
    if (catalog) {
      localStorage.setItem(STORAGE_KEY, String(catalog.id));
    } else {
      localStorage.removeItem(STORAGE_KEY);
    }
  };

  const clearActiveCatalog = () => {
    setActiveCatalogState(null);
    localStorage.removeItem(STORAGE_KEY);
  };

  return (
    <CatalogContext.Provider value={{ activeCatalog, setActiveCatalog, clearActiveCatalog, initialized, embeddingModel }}>
      {children}
    </CatalogContext.Provider>
  );
};

export const useCatalog = () => {
  const ctx = useContext(CatalogContext);
  if (!ctx) throw new Error('useCatalog must be used inside CatalogProvider');
  return ctx;
};

export default CatalogContext;
