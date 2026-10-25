import React, { useState, useEffect, useCallback } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  CCard, CCardBody, CCardHeader, CContainer, CRow, CCol, CButton, CBadge,
  CSpinner, CAlert,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilArrowLeft, cilPaw, cilBan, cilStar } from '@coreui/icons';
import { petAPI } from '../services/api';
import { API_HOST } from '../config';

// ─── One pet ──────────────────────────────────────────────────────────────────
//
// The People detail page's shape, for a `people` row that happens to be an
// animal: the facts on the left, the photographs it appears in on the right.
//
// Its photographs come from pet_detections rather than face_detections, which
// is the only real difference - and the reason those are separate tables is
// that a 512-d animal vector and a face vector cannot meet in one distance.
//
// See documentation/specs/pet-recognition-spec.md section 8.2.
//
// A PAGE at a time, from the server. It fetched every photograph - capped,
// silently, at 500 - so Luna's ~5,000 drew 500 thumbnails at once, the count
// said 500, and the rest could not be reached.
const PAGE = 60;

const PetDetail = () => {
  const { catalogId, petId } = useParams();
  const navigate = useNavigate();

  const [pet, setPet] = useState(null);
  // One page of photographs; `total` and `years` count all of them.
  const [detections, setDetections] = useState([]);
  const [total, setTotal] = useState(0);
  const [years, setYears] = useState({});
  const [offset, setOffset] = useState(0);
  const [pageLoading, setPageLoading] = useState(false);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [starring, setStarring] = useState(null);

  // Star one photograph: it becomes the picture on this animal's card.
  // Mirrors handleSetProfilePhoto on the People detail page, and reloads
  // rather than patching state locally - the backend decides which detection
  // in that file represents the pet, and guessing here could disagree with it.
  const setProfile = async (detectionId) => {
    setStarring(detectionId);
    setError(null);
    try {
      await petAPI.setProfilePhoto(catalogId, petId, detectionId);
      await load(offset);
    } catch (e) {
      setError(e.response?.data?.error || e.message || 'Could not set the picture');
    } finally {
      setStarring(null);
    }
  };

  const showPage = (d) => {
    setDetections(d.detections || []);
    setTotal(d.total || 0);
    setYears(d.years || {});
    setOffset(d.offset || 0);
  };

  const load = useCallback(async (at = 0) => {
    setLoading(true);
    try {
      const [p, d] = await Promise.all([
        petAPI.getById(catalogId, petId),
        petAPI.detections(catalogId, petId, { limit: PAGE, offset: at })
          .catch(() => ({ data: {} })),
      ]);
      setPet(p.data);
      showPage(d.data);
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    } finally {
      setLoading(false);
    }
  }, [catalogId, petId]);

  useEffect(() => { load(0); }, [load]);

  // Every photograph on this page unnamed, more waiting: the server's list now
  // continues at this same offset.
  useEffect(() => {
    if (!loading && !pageLoading && detections.length === 0 && total > offset) {
      goTo(offset);
    }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [detections.length]);

  const goTo = async (at) => {
    setPageLoading(true);
    try {
      const d = await petAPI.detections(catalogId, petId, { limit: PAGE, offset: at });
      showPage(d.data);
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    } finally {
      setPageLoading(false);
    }
  };

  const unname = async (detectionId) => {
    try {
      await petAPI.unassignDetection(catalogId, detectionId);
      // The detection survives - it just has no name again. The animal is
      // still in the photograph; only the judgement is withdrawn.
      const gone = detections.find((d) => d.id === detectionId);
      setDetections((ds) => ds.filter((d) => d.id !== detectionId));
      setTotal((t) => Math.max(0, t - 1));
      if (gone) {
        const y = (gone.master_datetime || '').slice(0, 4) || 'undated';
        setYears((ys) => ({ ...ys, [y]: Math.max(0, (ys[y] || 1) - 1) }));
      }
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    }
  };

  if (loading) {
    return <CContainer className="py-5 text-center"><CSpinner /></CContainer>;
  }
  if (!pet) {
    return (
      <CContainer className="py-4">
        <CAlert color="danger">{error || 'That pet does not exist.'}</CAlert>
        <CButton color="secondary" variant="outline" onClick={() => navigate('/pets')}>
          Back to pets
        </CButton>
      </CContainer>
    );
  }

  // The page's photographs under their years, newest first; each heading
  // counts the whole year, not just the part of it on this page.
  const byYear = detections.reduce((acc, d) => {
    const y = (d.master_datetime || '').slice(0, 4) || 'undated';
    (acc[y] = acc[y] || []).push(d);
    return acc;
  }, {});
  const pageYears = Object.keys(byYear).sort().reverse();

  // Unnamed photographs have left the server's list, so the next page starts
  // where what is left on this one ends.
  const next = offset + detections.length;
  const pager = total > PAGE && (
    <div className="d-flex align-items-center gap-2">
      <CButton size="sm" color="light" disabled={pageLoading || offset === 0}
               onClick={() => goTo(Math.max(0, offset - PAGE))}>
        ‹ Prev
      </CButton>
      <span style={{ fontSize: 12 }} className="text-body-secondary">
        {total === 0 ? 0 : offset + 1}–{next} of {total.toLocaleString()}
      </span>
      <CButton size="sm" color="light" disabled={pageLoading || next >= total}
               onClick={() => goTo(next)}>
        Next ›
      </CButton>
    </div>
  );

  return (
    <CContainer fluid className="py-3">
      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}

      <div className="d-flex align-items-center gap-3 mb-3">
        <CButton color="secondary" variant="outline" size="sm" onClick={() => navigate('/pets')}>
          <CIcon icon={cilArrowLeft} style={{ width: 14, height: 14 }} />
        </CButton>
        <h4 className="mb-0 fw-bold">{pet.name}</h4>
        {pet.species && <CBadge color="secondary">{pet.species}</CBadge>}
        {pet.category && <CBadge color="light" className="text-body">{pet.category}</CBadge>}
      </div>

      <CRow className="g-3">
        <CCol md={3}>
          <CCard>
            <div style={{ height: 220, background: '#f5f5f5', display: 'flex',
                          alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}>
              {pet.profile_photo_display_path ? (
                <img
                  alt={pet.name}
                  src={`${API_HOST}/api/catalogs/files?path=${encodeURIComponent(pet.profile_photo_display_path)}`}
                  style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                />
              ) : (
                <CIcon icon={cilPaw} style={{ width: 56, height: 56, color: '#c7c7c7' }} />
              )}
            </div>
            <CCardBody style={{ fontSize: 13 }}>
              <dl className="mb-0">
                <dt className="text-body-secondary fw-normal">Species</dt>
                <dd>{pet.species || <span className="fst-italic text-body-secondary">unknown</span>}</dd>
                <dt className="text-body-secondary fw-normal">Household</dt>
                <dd>{pet.category || '—'}</dd>
                <dt className="text-body-secondary fw-normal">Born</dt>
                <dd>{pet.date_of_birth || '—'}</dd>
                <dt className="text-body-secondary fw-normal">Photographs</dt>
                <dd>{total.toLocaleString()}</dd>
              </dl>
              {pet.description && (
                <p className="mb-0 mt-2 text-body-secondary">{pet.description}</p>
              )}
            </CCardBody>
          </CCard>
        </CCol>

        <CCol md={9}>
          <CCard>
            <CCardHeader className="fw-semibold d-flex align-items-center justify-content-between">
              <span>Photographs {pet.name} appears in</span>
              {pager}
            </CCardHeader>
            <CCardBody>
              {pageLoading ? (
                <div className="text-center py-5"><CSpinner /></div>
              ) : total === 0 ? (
                <div className="text-body-secondary py-4">
                  None yet. Animals are found during Find Faces, grouped by
                  Group Unnamed Pets, and naming a group links every photograph in
                  it to that pet at once.
                </div>
              ) : pageYears.map((y) => (
                <div key={y} className="mb-4">
                  <div className="fw-semibold mb-2" style={{ fontSize: 13 }}>
                    {y} <span className="text-body-secondary">({(years[y] ?? byYear[y].length).toLocaleString()})</span>
                  </div>
                  <div className="d-flex flex-wrap gap-2">
                    {byYear[y].map((d) => (
                      <div key={d.id} style={{ width: 112, position: 'relative' }}>
                        <div style={{ height: 112, background: '#f0f0f0', borderRadius: 4,
                                      overflow: 'hidden', display: 'flex',
                                      alignItems: 'center', justifyContent: 'center' }}>
                          {d.crop_url ? (
                            <img alt="" src={`${API_HOST}${d.crop_url}`}
                                 style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                          ) : (
                            <CIcon icon={cilPaw} style={{ width: 24, height: 24, color: '#c7c7c7' }} />
                          )}
                          {d.is_profile && (
                            <div style={{
                              position: 'absolute', top: 4, left: 4,
                              background: 'rgba(249,168,37,0.92)', color: '#000',
                              padding: '1px 5px', borderRadius: 3, fontSize: 9,
                              display: 'flex', alignItems: 'center', gap: 3,
                            }}>
                              <CIcon icon={cilStar} style={{ width: 9, height: 9 }} />
                              Profile
                            </div>
                          )}
                        </div>
                        <div className="d-flex align-items-center justify-content-between mt-1"
                             style={{ fontSize: 10 }}>
                          <span className="text-body-secondary">
                            {d.source === 'Manual' ? 'by hand' : `${Math.round((d.confidence || 0) * 100)}%`}
                          </span>
                          <div className="d-flex align-items-center gap-2">
                            {!d.is_profile && d.crop_url && (
                              <button
                                onClick={() => setProfile(d.id)}
                                disabled={starring === d.id}
                                title="Make this the picture on this pet's card"
                                style={{ background: 'none', border: 'none', cursor: 'pointer',
                                         color: '#c79100', padding: 0, lineHeight: 1 }}
                              >
                                {starring === d.id
                                  ? <CSpinner size="sm" style={{ width: 11, height: 11 }} />
                                  : <CIcon icon={cilStar} style={{ width: 12, height: 12 }} />}
                              </button>
                            )}
                          <button
                            onClick={() => unname(d.id)}
                            title="Not this pet - keeps the detection, removes the name"
                            style={{ background: 'none', border: 'none', cursor: 'pointer',
                                     color: '#999', padding: 0, lineHeight: 1 }}
                          >
                            <CIcon icon={cilBan} style={{ width: 12, height: 12 }} />
                          </button>
                          </div>
                        </div>
                      </div>
                    ))}
                  </div>
                </div>
              ))}
              {pager && !pageLoading && (
                <div className="d-flex justify-content-center mt-2">{pager}</div>
              )}
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>
    </CContainer>
  );
};

export default PetDetail;
