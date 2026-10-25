import React, { useCallback, useEffect, useState } from 'react';
import {
  CContainer, CCard, CCardBody, CRow, CCol, CButton, CFormLabel,
  CFormRange, CSpinner, CAlert,
} from '@coreui/react';
import { peopleAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { defaultMinSimilarity } from '../utils/similarity';
import ClustersPanel from '../components/FaceClusters';

/**
 * Identify groups — faces the contextual workflow judged to be the same person.
 *
 * This was a collapsed panel on the Identify people page, behind a Review
 * button, sharing that page with the per-person candidate queue. Here the groups
 * are the whole page, so the panel is always open.
 */

const IdentifyGroups = () => {
  const { activeCatalog } = useCatalog();
  const [people, setPeople] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // The floor for showing a suggested person. Same default and same 0-100
  // scale as the slider on Identify people, so a number means the same
  // thing on both pages.
  const [minSimilarity, setMinSimilarity] = useState(() => defaultMinSimilarity());
  // Bumping this remounts the panel, which is how Reload re-runs its fetch.
  const [reloadKey, setReloadKey] = useState(0);

  const loadPeople = useCallback(async (catalogId) => {
    setLoading(true);
    setError(null);
    try {
      const r = await peopleAPI.getAll(catalogId);
      setPeople(r.data || []);
    } catch (err) {
      setError('Could not load people. Group assignment needs the people list.');
      setPeople([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    if (activeCatalog?.id) loadPeople(activeCatalog.id);
  }, [activeCatalog, loadPeople]);

  if (!activeCatalog) {
    return (
      <CContainer fluid className="py-4 px-4">
        <CAlert color="info">Select a catalog to review face groups.</CAlert>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="py-4 px-4">
      {/* Page header */}
      <div className="mb-4">
        <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Identify groups</h4>
        <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
          Faces grouped as the same person — naming a group labels every face in it
        </div>
      </div>

      {/* Controls, in the same position as the slider on Identify people */}
      <CCard className="mb-3">
        <CCardBody className="py-3">
          <CRow className="align-items-end g-3">
            <CCol md={5}>
              <CFormLabel className="mb-1 fw-semibold" style={{ fontSize: '0.85rem' }}>
                Minimum similarity: <strong>{minSimilarity}</strong>
                <span style={{ fontWeight: 'normal', color: '#888', marginLeft: 8 }}>
                  (hides weaker suggestions)
                </span>
              </CFormLabel>
              <CFormRange
                min={0} max={100} step={1}
                value={minSimilarity}
                onChange={(e) => setMinSimilarity(parseInt(e.target.value, 10))}
              />
            </CCol>
            <CCol md="auto">
              <CButton
                color="secondary"
                variant="outline"
                onClick={() => {
                  loadPeople(activeCatalog.id);
                  setReloadKey((k) => k + 1);
                }}
                disabled={loading}
              >
                {loading ? <CSpinner size="sm" className="me-1" /> : null}
                Reload
              </CButton>
            </CCol>
          </CRow>
        </CCardBody>
      </CCard>

      {error && <CAlert color="danger">{error}</CAlert>}

      {loading ? (
        <div className="text-center py-5"><CSpinner color="primary" /></div>
      ) : (
        <ClustersPanel
          key={`${activeCatalog.id}-${reloadKey}`}
          catalogId={activeCatalog.id}
          people={people}
          minSimilarity={minSimilarity}
          onAssigned={() => loadPeople(activeCatalog.id)}
          emptyMessage={(
            <CCard className="mb-3">
              <CCardBody className="text-center text-muted py-5">
                <div className="mb-1">No face groups in this catalog.</div>
                <div style={{ fontSize: '0.85rem' }}>
                  Groups are produced by the contextual recognition workflow — run it
                  to populate this page.
                </div>
              </CCardBody>
            </CCard>
          )}
        />
      )}
    </CContainer>
  );
};

export default IdentifyGroups;
