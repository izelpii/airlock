import React, { useState, useEffect, useRef } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  CContainer,
  CButton,
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CModalFooter,
  CForm,
  CFormInput,
  CFormTextarea,
  CFormLabel,
  CSpinner,
  CAlert,
  CBadge,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPlus, cilCheckAlt, cilBan } from '@coreui/icons';
import { locationAPI } from '../services/api';
import LocationCard, { CARD_WIDTH, CARD_HEIGHT } from '../components/LocationCard';
import { useCatalog } from '../context/CatalogContext';

const Locations = () => {
  const navigate = useNavigate();
  const { activeCatalog } = useCatalog();
  const [locations, setLocations] = useState([]);
  const [total, setTotal] = useState(0);
  const [page, setPage] = useState(0);
  // How many cards fit the window. Measured, not guessed - the same approach
  // the Events page uses, and for the same reason: a fixed number is wrong on
  // every screen that is not the one it was chosen on.
  const [pageSize, setPageSize] = useState(24);
  const gridRef = useRef(null);
  const topRef = useRef(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showAddModal, setShowAddModal] = useState(false);
  const [notice, setNotice] = useState(null);
  const [busy, setBusy] = useState(false);
  const [confirming, setConfirming] = useState(null);
  const [confirmName, setConfirmName] = useState('');
  const [formData, setFormData] = useState({
    name: '',
    lat: '',
    long: '',
    description: '',
    source: '',
  });

  // How many cards fit between the header and the bottom of the window.
  //
  // The same calculation as the Events page. Measured rather than assumed,
  // and re-measured on resize, so the grid holds whole rows on whatever
  // screen it is on.
  useEffect(() => {
    const TOP_NAV_H = 60;       // .top-menu min-height
    const CONTAINER_PT = 24;    // CContainer py-4
    const CONTAINER_PB = 24;
    const GAP = 16;             // the grid's 1rem gap

    const compute = () => {
      const topH = topRef.current ? topRef.current.offsetHeight : 0;
      const avail = window.innerHeight - TOP_NAV_H - CONTAINER_PT - topH
                    - CONTAINER_PB;
      const el = gridRef.current;
      // 280 sidebar + 60 padding, only before the first render has a width.
      const bodyW = el ? el.clientWidth : window.innerWidth - 340;
      const cols = Math.max(1, Math.floor((bodyW + GAP) / (CARD_WIDTH + GAP)));
      const rows = Math.max(1, Math.floor((avail + GAP) / (CARD_HEIGHT + GAP)));
      setPageSize(cols * rows);
    };

    compute();
    window.addEventListener('resize', compute);
    const ro = new ResizeObserver(compute);
    if (topRef.current) ro.observe(topRef.current);
    if (gridRef.current) ro.observe(gridRef.current);
    return () => {
      window.removeEventListener('resize', compute);
      ro.disconnect();
    };
    // Not on `loading`: the measured element no longer comes and goes with
    // it, and re-measuring on every load is how a page-size change fed back
    // into another load.
  }, [activeCatalog]);

  useEffect(() => {
    if (activeCatalog) {
      loadLocations();
    } else {
      setLocations([]);
      setTotal(0);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [activeCatalog, page, pageSize]);

  // Keep `page` inside the list.
  //
  // The pager shows a clamped page but the fetch uses the raw one, so without
  // this they drift apart in two ordinary cases: a window resized to fit more
  // cards, and rejecting the last proposal on the final page. Both would
  // request an offset past the end and show an empty grid under "Page 3 of 2".
  useEffect(() => {
    const last = Math.max(0, Math.ceil(total / Math.max(1, pageSize)) - 1);
    if (page > last) setPage(last);
  }, [total, pageSize, page]);

  // A catalog with different locations starts at its own beginning.
  useEffect(() => { setPage(0); }, [activeCatalog]);

  // ONE PAGE, not the catalog.
  //
  // This fetched every location and drew a card for each. The JSON was never
  // the problem - 1,041 locations is 337 KB in 0.27 s on `family` - but every
  // card fetches a MAP TILE and a thumbnail, so one click meant a thousand
  // cards and some two thousand image requests. A page of cards is a page of
  // requests.
  const loadLocations = async () => {
    if (!activeCatalog) return;
    setLoading(true);
    setError(null);
    try {
      const response = await locationAPI.getPage(activeCatalog.id, {
        offset: page * pageSize,
        limit: pageSize,
      });
      const body = response.data;
      // The endpoint answers with an object when paged and a bare array when
      // not. Reading a page as the whole list would show a thousand locations
      // as a handful and look like data loss, so handle both shapes here.
      if (Array.isArray(body)) {
        setLocations(body);
        setTotal(body.length);
      } else {
        setLocations(body.locations ?? []);
        setTotal(body.total ?? 0);
      }
    } catch (err) {
      setError('Failed to load locations');
    } finally {
      setLoading(false);
    }
  };

  const handleAddLocation = async () => {
    try {
      await locationAPI.create(activeCatalog.id, {
        name: formData.name,
        latitude: formData.lat !== '' ? parseFloat(formData.lat) : null,
        longitude: formData.long !== '' ? parseFloat(formData.long) : null,
        description: formData.description || null,
        source: formData.source || null,
      });
      setShowAddModal(false);
      resetForm();
      loadLocations();
    } catch (err) {
      setError('Failed to add location');
    }
  };

  const openConfirm = (location) => {
    setConfirming(location);
    // Pre-filled with what it is called now, so confirming as-is is one click
    // and renaming is one edit.
    setConfirmName(location.name || '');
  };

  const doConfirm = async () => {
    if (!confirming) return;
    setBusy(true);
    setError(null);
    try {
      await locationAPI.confirm(activeCatalog.id, confirming.id,
                                confirmName.trim());
      setNotice(`"${confirmName.trim()}" is verified. It can be used to tag `
                + 'events now.');
      setConfirming(null);
      await loadLocations();
    } catch (err) {
      setError(err?.response?.data?.error || 'Could not confirm that location.');
    } finally {
      setBusy(false);
    }
  };

  const dismiss = async (location) => {
    if (!location.cluster_id) return;
    setBusy(true);
    setError(null);
    try {
      await locationAPI.rejectCluster(activeCatalog.id, location.cluster_id);
      setNotice(`"${location.name}" is no longer a location. It will not be `
                + 'proposed again.');
      await loadLocations();
    } catch (err) {
      setError(err?.response?.data?.error || 'Could not dismiss that location.');
    } finally {
      setBusy(false);
    }
  };

  const resetForm = () => {
    setFormData({ name: '', lat: '', long: '', description: '', source: '' });
  };



  const handleCardClick = (location) => {
    navigate(`/locations/${activeCatalog.id}/${location.id}`);
  };

  const totalPages = Math.max(1, Math.ceil(total / Math.max(1, pageSize)));
  // Clamped rather than reset: a resize that fits more cards per page can
  // leave `page` past the end, and sending somebody back to page 1 for
  // resizing their window would be its own bug. The same reasoning, and the
  // same two lines, as the Events page.
  const safePage = Math.min(page, totalPages - 1);

  if (!activeCatalog) {
    return (
      <CContainer fluid className="py-4 px-4">
        <div className="text-center py-5 text-muted">
          <strong>No catalog selected</strong>
          <div style={{ marginTop: 8, fontSize: '0.9rem' }}>
            Select a catalog from the dropdown in the top menu to get started.
          </div>
        </div>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="py-4 px-4">
      {/* Header */}
      <div ref={topRef}
           className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Locations</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Where your photos were taken. Confirm one to use it on an event.
            {total > 0 && (
              <span> · {total.toLocaleString()} in all</span>
            )}
          </div>
        </div>
        <div className="d-flex align-items-center gap-2">
          {/* The Events page's pager, the same markup and the same wording,
              because it is the same control doing the same job. The only
              difference is that this one turns a page on the SERVER: Events
              has its events in hand and slices them, while a thousand
              location cards would be a thousand map tiles. */}
          {totalPages > 1 && (
            <div className="d-flex align-items-center gap-2 me-2">
              <CButton color="secondary" size="sm"
                       onClick={() => setPage((p) => Math.max(0, p - 1))}
                       disabled={safePage === 0 || loading}>
                ← Previous
              </CButton>
              <span style={{ fontSize: '0.85rem', color: '#666',
                             whiteSpace: 'nowrap' }}>
                Page {safePage + 1} of {totalPages}
              </span>
              <CButton color="secondary" size="sm"
                       onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                       disabled={safePage === totalPages - 1 || loading}>
                Next →
              </CButton>
            </div>
          )}
          <CButton color="primary" onClick={() => { resetForm(); setShowAddModal(true); }}>
            <CIcon icon={cilPlus} className="me-2" />
            Add Location
          </CButton>
        </div>
      </div>

      {error && <CAlert color="danger">{error}</CAlert>}
      {notice && <CAlert color="info" dismissible onClose={() => setNotice(null)}
                         style={{ fontSize: '0.85rem' }}>{notice}</CAlert>}

      {/* ONE list, unfiltered. There were two sections - "Proposed places" from
          the clustering workflow and "Places" that a person had named -
          showing one concept as two kinds of thing. A location is now a
          location, and the Verified badge is the ONLY difference between
          them, which is the whole point of having one list. A filter whose
          default is "everything" would only put that difference back behind a
          control.

          No map here either: the cards each carry one on the back, and a
          whole-catalog map is a separate job. */}
      {/* What is MEASURED, and it is always here. The ref was on the grid,
          which the spinner replaces, so every load measured the window's
          fallback width instead - 14 cards where the grid holds 12 - and the
          new page size started a load, which removed the grid again. 654
          requests for page one in a few hours, and a page that blinked. */}
      <div ref={gridRef}>
      {loading ? (
        <div className="text-center py-5">
          <CSpinner color="primary" />
        </div>
      ) : locations.length === 0 ? (
        <div className="text-center py-5 text-muted" style={{ fontSize: '0.9rem' }}>
          <p className="mb-1">No locations yet.</p>
          <p className="mb-0">
            Run <strong>Extract Locations</strong> to group the photos that
            carry GPS coordinates, or click "Add Location" to enter one by hand.
          </p>
        </div>
      ) : (
        <div style={{ display: 'flex', flexWrap: 'wrap', gap: '1rem' }}>
          {locations.map((location) => (
            <LocationCard
              key={location.id}
              catalogId={activeCatalog.id}
              id={location.id}
              name={location.name}
              subtitle={location.description || null}
              /* "Verified", the word the Event cards use for the same idea -
                 a person has looked at this and vouched for it. The ACTION is
                 still Confirm, because that is the verb; this is the state it
                 leaves behind. */
              badge={location.confirmed ? (
                <CBadge color="success"
                        style={{ fontSize: '0.68rem', display: 'inline-block' }}>
                  ✓ Verified
                </CBadge>
              ) : null}
              coverFileId={location.cover_file_id}
              latitude={location.latitude}
              longitude={location.longitude}
              fileCount={location.file_count}
              mapUrl={locationAPI.getMapImageUrl(activeCatalog.id, location.id)}
              onOpen={() => handleCardClick(location)}
              /* No Open button: the card itself opens the location. Only the
                 two decisions live here, and only while there is a decision
                 to make - a confirmed location has no buttons at all.

                 Dismissing is offered for an unconfirmed location only.
                 Rejecting the grouping that suggested a name is not the same
                 as deleting a location somebody named, which is done on its
                 own page. */
              actions={!location.confirmed ? (
                <>
                  <CButton color="success" variant="outline" size="sm"
                           disabled={busy}
                           style={{ fontSize: '0.74rem' }}
                           title="Confirm this location, changing its name if you like"
                           onClick={() => openConfirm(location)}>
                    <CIcon icon={cilCheckAlt} className="me-1" /> Confirm
                  </CButton>
                  {location.cluster_id && (
                    <CButton color="secondary" variant="outline" size="sm"
                             disabled={busy}
                             style={{ fontSize: '0.74rem' }}
                             title="Not a location. Kept as a rejection so it is not proposed again."
                             onClick={() => dismiss(location)}>
                      <CIcon icon={cilBan} />
                    </CButton>
                  )}
                </>
              ) : null}
            />
          ))}
        </div>
      )}
      </div>

      {/* Confirming, with the name editable in the same step: somebody who
          changes the geocoder's guess has plainly decided this is a location,
          and asking them to then press Confirm would be asking twice. */}
      <CModal visible={!!confirming} onClose={() => setConfirming(null)}>
        <CModalHeader>
          <CModalTitle>Confirm this location</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <div style={{ fontSize: '0.85rem', color: '#888', marginBottom: 12 }}>
            {(confirming?.file_count || 0).toLocaleString()} photo(s)
            {confirming?.latitude != null && (
              <> · {Number(confirming.latitude).toFixed(5)},{' '}
                {Number(confirming.longitude).toFixed(5)}</>
            )}
          </div>
          <CForm>
            <CFormLabel>Name</CFormLabel>
            <CFormInput value={confirmName} autoFocus
                        placeholder="Abuela's house"
                        onChange={(e) => setConfirmName(e.target.value)} />
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline"
                   onClick={() => setConfirming(null)}>Cancel</CButton>
          <CButton color="success" disabled={busy || !confirmName.trim()}
                   onClick={doConfirm}>Confirm</CButton>
        </CModalFooter>
      </CModal>

      {/* Add Location Modal */}
      <CModal visible={showAddModal} onClose={() => setShowAddModal(false)}>
        <CModalHeader>
          <CModalTitle>Add New Location</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <CForm>
            <div className="mb-3">
              <CFormLabel>Name *</CFormLabel>
              <CFormInput
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                placeholder="Enter location name"
              />
            </div>
            <div className="mb-3">
              <CFormLabel>Latitude</CFormLabel>
              <CFormInput
                type="number"
                step="any"
                value={formData.lat}
                onChange={(e) => setFormData({ ...formData, lat: e.target.value })}
                placeholder="e.g. 48.8566"
              />
            </div>
            <div className="mb-3">
              <CFormLabel>Longitude</CFormLabel>
              <CFormInput
                type="number"
                step="any"
                value={formData.long}
                onChange={(e) => setFormData({ ...formData, long: e.target.value })}
                placeholder="e.g. 2.3522"
              />
            </div>
            <div className="mb-3">
              <CFormLabel>Description</CFormLabel>
              <CFormTextarea
                rows={3}
                value={formData.description}
                onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                placeholder="Enter description"
              />
            </div>
            <div className="mb-3">
              <CFormLabel>Source</CFormLabel>
              <CFormInput
                value={formData.source}
                onChange={(e) => setFormData({ ...formData, source: e.target.value })}
                placeholder="e.g. GPS, Manual, OpenStreetMap"
              />
            </div>
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowAddModal(false)}>
            Cancel
          </CButton>
          <CButton color="primary" onClick={handleAddLocation} disabled={!formData.name}>
            Add Location
          </CButton>
        </CModalFooter>
      </CModal>
    </CContainer>
  );
};

export default Locations;
