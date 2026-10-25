import React, { useState, useEffect, useCallback, useRef } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CContainer,
  CButton,
  CRow,
  CCol,
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CModalFooter,
  CForm,
  CFormInput,
  CFormLabel,
  CFormTextarea,
  CFormSelect,
  CSpinner,
  CAlert,
  CBadge,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilArrowLeft, cilPencil, cilTrash, cilX, cilCheck, cilBolt, cilMap, cilReload } from '@coreui/icons';
import { MapContainer, TileLayer, Marker, useMapEvents } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { locationAPI } from '../services/api';
import { galleryThumbUrl } from '../config';

// Fix leaflet default marker icon issue with webpack
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png',
  iconUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png',
  shadowUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png',
});

const MONTH_NAMES = [
  '', 'Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun',
  'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec',
];


// Component that handles map click events to drop a pin
const MapClickHandler = ({ onMapClick }) => {
  useMapEvents({
    click(e) {
      onMapClick(e.latlng.lat, e.latlng.lng);
    },
  });
  return null;
};

// Same shape as Events.js's formatter, so two pages do not render a timestamp
// two ways. Returns the raw string rather than "Invalid Date" when it cannot
// parse: the value is more use to whoever has to work out why.
function formatDateTime(dt) {
  if (!dt) return null;
  try {
    const d = new Date(dt);
    if (isNaN(d.getTime())) return dt;
    return d.toLocaleString('en-US', {
      year: 'numeric', month: 'short', day: 'numeric',
      hour: '2-digit', minute: '2-digit',
    });
  } catch {
    return dt;
  }
}

// One row of the details list. An empty value prints a dash rather than being
// omitted: "there is no description" and "this location has no description
// field" look identical when the row simply is not there, and the card's job
// is to say what is known.
const Detail = ({ label, children }) => {
  const empty = children === null || children === undefined || children === ''
    || (Array.isArray(children) && children.length === 0);
  return (
    <div className="d-flex gap-2 py-1" style={{ borderBottom: '1px solid #f3f3f3' }}>
      <dt className="text-muted fw-normal" style={{ flex: '0 0 42%', fontSize: '0.78rem' }}>
        {label}
      </dt>
      <dd className="mb-0" style={{ flex: 1, minWidth: 0, wordBreak: 'break-word' }}>
        {empty ? <span className="text-muted">—</span> : children}
      </dd>
    </div>
  );
};

const LocationDetail = () => {
  const { catalogId, locationId } = useParams();
  const navigate = useNavigate();

  const [location, setLocation] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);

  // Gallery state. `photos` is ONE PAGE - the server pages it - and
  // `photosTotal` is how many the location holds under the current filter.
  const [photos, setPhotos] = useState([]);
  const [photosTotal, setPhotosTotal] = useState(0);
  const [yearsMonths, setYearsMonths] = useState({});
  const [selectedYear, setSelectedYear] = useState(null);
  const [selectedMonth, setSelectedMonth] = useState(null);
  const [photosLoading, setPhotosLoading] = useState(false);

  // Gallery pagination
  const [galleryPage, setGalleryPage] = useState(0);
  const galleryBodyRef = useRef(null);
  const topSectionRef = useRef(null);
  const [pageSize, setPageSize] = useState(12);
  const [galleryBodyH, setGalleryBodyH] = useState(400);

  // Edit state
  const [isEditing, setIsEditing] = useState(false);
  // The details card turns over to show what is nearby.
  const [detailsFlipped, setDetailsFlipped] = useState(false);
  const [saving, setSaving] = useState(false);
  const [showDeleteConfirmModal, setShowDeleteConfirmModal] = useState(false);
  const [formData, setFormData] = useState({
    name: '',
    lat: '',
    long: '',
    description: '',
    source: '',
  });

  // AI / GeoLoc state
  const [aiLoading, setAiLoading] = useState(false);
  const [aiError, setAiError] = useState(null);
  const [aiPois, setAiPois] = useState([]);
  const [selectedPoiIndex, setSelectedPoiIndex] = useState(0);

  // Map state
  const [pinPosition, setPinPosition] = useState(null); // { lat, lng }

  // Cache-busting timestamp for the location map image (bumped after save)

  /**
   * `quiet` refreshes the data WITHOUT blanking the page.
   *
   * A loud reload sets `loading`, and the early return at the top of this
   * component then replaces everything with a spinner - which unmounts the
   * details card. That is why the flip back after "Apply All Fields" was
   * instant instead of animated: the card was destroyed mid-transition and
   * remounted already turned, so there was nothing to animate. It also made a
   * normal save blink the whole page for one request.
   *
   * Only the first load, from the effect below, should be loud. Everything
   * after it is a refresh of something already on screen.
   */
  const loadLocation = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    setError(null);
    try {
      const response = await locationAPI.getById(catalogId, locationId);
      setLocation(response.data);
      setFormData({
        name: response.data.name || '',
        lat: response.data.latitude != null ? String(response.data.latitude) : '',
        long: response.data.longitude != null ? String(response.data.longitude) : '',
        description: response.data.description || '',
        source: response.data.source || '',
      });
    } catch (err) {
      setError('Failed to load location details');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [catalogId, locationId]);

  // One page at a time. This used to fetch every photo of the location and
  // slice it here: 9,257 rows on location 347 of `family` to draw twelve.
  //
  // `photosRequest` numbers the requests so only the latest one lands - two
  // quick clicks on Next must not let the slower, older page overwrite the
  // newer one.
  const photosRequest = useRef(0);
  const loadPhotos = useCallback(async () => {
    const seq = ++photosRequest.current;
    setPhotosLoading(true);
    try {
      const response = await locationAPI.getPhotos(
        catalogId,
        locationId,
        selectedYear,
        selectedMonth,
        { limit: pageSize, offset: galleryPage * pageSize },
      );
      if (seq !== photosRequest.current) return;
      setPhotos(response.data.photos || []);
      setPhotosTotal(response.data.total ?? (response.data.photos || []).length);
      if (!selectedYear && !selectedMonth) {
        setYearsMonths(response.data.years_months || {});
      }
    } catch (err) {
      if (seq !== photosRequest.current) return;
      setPhotos([]);
      setPhotosTotal(0);
    } finally {
      if (seq === photosRequest.current) setPhotosLoading(false);
    }
  }, [catalogId, locationId, selectedYear, selectedMonth, pageSize, galleryPage]);

  useEffect(() => {
    loadLocation();
  }, [loadLocation]);

  useEffect(() => {
    if (location) {
      loadPhotos();
    }
  }, [location, loadPhotos]);

  // Back to the first page when the filter or the location changes. Not on
  // [photos] as it was: a page arriving would now send the gallery back to
  // page one.
  useEffect(() => {
    setGalleryPage(0);
  }, [catalogId, locationId, selectedYear, selectedMonth]);

  // When the window resizes and a page holds a different number of tiles,
  // stay on the page that holds the first tile you were looking at.
  const prevPageSize = useRef(pageSize);
  useEffect(() => {
    const prev = prevPageSize.current;
    prevPageSize.current = pageSize;
    if (prev !== pageSize) {
      setGalleryPage((p) => Math.floor((p * prev) / pageSize));
    }
  }, [pageSize]);

  // Compute gallery body height and page size — mirrors EventDetail
  useEffect(() => {
    const TOP_NAV_H = 60;
    const CONTAINER_PT = 24;
    const CONTAINER_PB = 24;
    const CARD_HEADER_H = 57;
    const CARD_BODY_P = 24;
    const CARD_W = 160;
    const CARD_H = 206; // image 160 + label ~34 + border/padding ~12
    const GAP = 12;

    const compute = () => {
      const topEl = topSectionRef.current;
      const topH = topEl ? topEl.offsetHeight : 0;
      const avail = window.innerHeight
        - TOP_NAV_H
        - CONTAINER_PT
        - topH
        - CARD_HEADER_H
        - CARD_BODY_P
        - CONTAINER_PB;
      const bodyEl = galleryBodyRef.current;
      const bodyW = bodyEl ? bodyEl.clientWidth : window.innerWidth - 340;
      const cols = Math.max(1, Math.floor((bodyW + GAP) / (CARD_W + GAP)));
      const rows = Math.max(1, Math.floor((Math.max(CARD_H, avail) + GAP) / (CARD_H + GAP)) - 1);
      setGalleryBodyH(rows * (CARD_H + GAP) - GAP);
      setPageSize(cols * rows);
    };

    compute();
    window.addEventListener('resize', compute);
    const ro = new ResizeObserver(compute);
    if (topSectionRef.current)  ro.observe(topSectionRef.current);
    if (galleryBodyRef.current) ro.observe(galleryBodyRef.current);
    return () => {
      window.removeEventListener('resize', compute);
      ro.disconnect();
    };
  }, []);

  // Sync pin position when formData lat/long changes externally
  useEffect(() => {
    const lat = parseFloat(formData.lat);
    const lng = parseFloat(formData.long);
    if (!isNaN(lat) && !isNaN(lng)) {
      setPinPosition({ lat, lng });
    } else {
      setPinPosition(null);
    }
  }, [formData.lat, formData.long]);

  const handleStartEditing = () => {
    setFormData({
      name: location?.name || '',
      lat: location?.latitude != null ? String(location.latitude) : '',
      long: location?.longitude != null ? String(location.longitude) : '',
      description: location?.description || '',
      source: location?.source || '',
    });
    setIsEditing(true);
  };

  const handleCancelEditing = () => {
    setIsEditing(false);
    setError(null);
    // The suggestions go with the edit they were fetched for; the cards
    // themselves are always on screen now, so there is no panel to close.
    setAiPois([]);
    setAiError(null);
  };

  /**
   * Save, from the values GIVEN rather than from state.
   *
   * That is the whole reason this takes an argument: setFormData is
   * asynchronous, so "apply these fields then save" would send the state as it
   * was BEFORE the apply. Applying and saving is one action to a person, and it
   * has to be one payload here too.
   *
   * Returns true when it saved, so a caller can decide what to do next - the
   * card only turns back over if the save actually worked.
   */
  const saveLocation = async (data) => {
    if (!String(data.name || '').trim()) { setError('Name is required'); return false; }
    setSaving(true);
    try {
      await locationAPI.update(catalogId, locationId, {
        name: data.name,
        latitude: data.lat !== '' ? parseFloat(data.lat) : null,
        longitude: data.long !== '' ? parseFloat(data.long) : null,
        description: data.description || null,
        source: data.source || null,
      });
      setIsEditing(false);
      // The nearby results are NOT cleared here. They were, and it showed:
      // clearing them empties the back face, which is still the visible one
      // for the first half of the turn, so the names flashed away before the
      // card moved. They are cleared when a new lookup starts and when an edit
      // is cancelled, which is soon enough - and leaving the names that were
      // just applied on the back is better feedback than an empty panel.
      setAiError(null);
      // Quiet: this is a refresh of a card already on screen. A loud one would
      // unmount it, and the flip back from "Apply All Fields" would have
      // nothing left to animate.
      loadLocation(true);
      return true;
    } catch (err) {
      // The server's own words, not a fixed sentence. "Failed to update
      // location" was all a real failure ever produced, which made one report
      // of it impossible to act on: the cause turned out to be the backend
      // being briefly unresponsive, and the message said nothing about that.
      setError(err?.response?.data?.error
               || err?.message
               || 'Failed to update location');
      return false;
    } finally {
      setSaving(false);
    }
  };

  const handleUpdateLocation = () => saveLocation(formData);

  const handleDeleteLocation = async () => {
    try {
      await locationAPI.delete(catalogId, locationId);
      setShowDeleteConfirmModal(false);
      navigate('/locations');
    } catch (err) {
      setError('Failed to delete location');
      setShowDeleteConfirmModal(false);
    }
  };

  const handleDetectNearby = async () => {
    const lat = location?.latitude;
    const lon = location?.longitude;

    if (lat == null || lon == null) {
      setAiError('This location has no coordinates. Please add Latitude and Longitude first.');
      setDetailsFlipped(true);     // or the message lands on a face nobody sees
      return;
    }

    // Make sure we're in edit mode
    if (!isEditing) {
      handleStartEditing();
    }

    setAiLoading(true);
    setAiError(null);
    setAiPois([]);
    // Turned over NOW, not when the answer lands. The lookup can take tens of
    // seconds, and a button that shows nothing for that long reads as one that
    // did not work; the back has its own spinner.
    setDetailsFlipped(true);

    try {
      // Through the backend, not straight at backend-geoloc as this used to
      // be. The browser needs no route to an internal service, the answer can
      // be cached server-side, and Nominatim's etiquette limit is enforced
      // once rather than once per open tab. See location-spec.md 7.3.
      const response = await locationAPI.geocode(catalogId, lat, lon);
      // Alphabetical. The geocoder returns them in its own order -
      // combine_pois() in backend-geoloc merges an OSMnx list into a Nominatim
      // one, so the order is really "which upstream found it first", which is
      // no order at all to someone scanning for a name. localeCompare rather
      // than <, so an accented name lands where a reader expects instead of
      // where its code point falls.
      const pois = [...(response.data?.pois || [])].sort(
        (a, b) => String(a.name || '').localeCompare(String(b.name || ''),
                                                     undefined, { sensitivity: 'base' }));
      setAiPois(pois);
      setSelectedPoiIndex(0);
      if (pois.length === 0) {
        // "nothing is named here" is a real answer about a field in the middle
        // of nowhere; the backend answers 503 when it could not ask at all,
        // which lands in the catch below instead.
        setAiError('Nothing named near these coordinates.');
      }
    } catch (err) {
      const detail = err?.response?.data?.error || err.message;
      setAiError(`Could not look up nearby names: ${detail}`);
    } finally {
      setAiLoading(false);
    }
  };

  const handleMapClick = useCallback((lat, lng) => {
    // Enter edit mode, because a dropped pin writes into the edit form and
    // without this the coordinate goes somewhere invisible and is lost on the
    // next load. handleUseMap used to do it before opening the panel; with the
    // map always mounted there is no "open" to hook, so the click takes over.
    //
    // setIsEditing(true) directly, NOT handleStartEditing(): that reseeds the
    // whole form from the loaded location, which would discard a name typed
    // before the pin was dropped. loadLocation already seeds formData, so
    // there is nothing to seed - and setting true when already true is a
    // no-op, which is why this needs no `isEditing` in the dependency array
    // below. Reading it there would have been a stale `false` captured on the
    // first render, so every drop would have reset the form.
    setIsEditing(true);
    const latStr = lat.toFixed(6);
    const lngStr = lng.toFixed(6);
    setPinPosition({ lat, lng });
    setFormData((prev) => ({ ...prev, lat: latStr, long: lngStr }));
  }, []);

  // Apply a single field from the selected POI into the edit form
  const applyPoiField = (field, value) => {
    setFormData((prev) => ({ ...prev, [field]: value }));
  };

  /**
   * Take everything from the chosen nearby name, save it, and turn the card
   * back over.
   *
   * One action, so one payload: `next` is built here and handed to
   * saveLocation rather than written to state and read back, which would have
   * saved the values from before the apply.
   *
   * Only turns back over on success - a failure leaves the card on this face
   * with its error visible, which is where it can be acted on. And saving a
   * name is what marks a location verified (location-spec.md 14.8): choosing
   * one of these names is a person deciding, so that is correct rather than
   * incidental.
   */
  const applyAllPoiFields = async () => {
    const poi = aiPois[selectedPoiIndex];
    if (!poi) return;
    const next = {
      ...formData,
      name: poi.name || formData.name,
      description: poi.address || formData.description,
      source: poi.source || formData.source,
    };
    setFormData(next);
    if (await saveLocation(next)) {
      setDetailsFlipped(false);
    }
  };

  const formatCoord = (val) => {
    if (val == null) return null;
    return Number(val).toFixed(5);
  };

  const getMapsUrl = (lat, long) => {
    if (lat == null || long == null) return null;
    return `https://www.google.com/maps?q=${lat},${long}`;
  };

  // Sorted years descending
  const sortedYears = Object.keys(yearsMonths)
    .map(Number)
    .sort((a, b) => b - a);

  // Months available for the selected year
  const availableMonths = selectedYear ? (yearsMonths[selectedYear] || []) : [];

  const selectedPoi = aiPois[selectedPoiIndex] || null;

  // Column widths are FIXED - 3 / 3 / 6 of twelve, so Edit details and Nearby
  // share the left half and the map takes the right. They used to be computed
  // from which panels happened to be open (4/4/4, or 6/6, or 12), which meant
  // the page reflowed under you as you worked. See
  // documentation/specs/location-redesign-spec.md section 2.
  //
  // The row's HEIGHT is this one number. The three cards are h-100 inside a
  // flex row, so the row takes the height of its tallest card and the other
  // two stretch to match - which means the map alone decides it. Setting a
  // height on the row instead would have to be undone below md, where the
  // columns stack and would each claim a third of it.
  const MAP_HEIGHT = 480;        // 80% of the 600 it was

  // Determine map center: use pin position, or formData coords, or location coords, or default
  const getMapCenter = () => {
    if (pinPosition) return [pinPosition.lat, pinPosition.lng];
    const lat = parseFloat(formData.lat);
    const lng = parseFloat(formData.long);
    if (!isNaN(lat) && !isNaN(lng)) return [lat, lng];
    if (location?.latitude != null && location?.longitude != null) return [location.latitude, location.longitude];
    return [40.7128, -74.0060]; // Nothing to place it by yet: a neutral starting view
  };

  const getMapZoom = () => {
    if (pinPosition) return 13;
    const lat = parseFloat(formData.lat);
    const lng = parseFloat(formData.long);
    if (!isNaN(lat) && !isNaN(lng)) return 13;
    if (location?.latitude != null && location?.longitude != null) return 13;
    return 2;
  };

  if (loading) {
    return (
      <CContainer fluid className="px-4 py-4">
        <div className="text-center py-5">
          <CSpinner color="primary" />
        </div>
      </CContainer>
    );
  }

  if (!location) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="danger">Location not found</CAlert>
      </CContainer>
    );
  }

  const mapsUrl = getMapsUrl(location.latitude, location.longitude);
  const totalGalleryPages = Math.ceil(photosTotal / pageSize);
  const pagePhotos = photos;

  return (
    <CContainer fluid className="px-4 py-4">
      <div ref={topSectionRef}>
      <CButton color="link" onClick={() => navigate('/locations')} className="mb-3">
        <CIcon icon={cilArrowLeft} className="me-2" />
        Back to Locations
      </CButton>

      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}

      {/* TWO columns. The nearby-places card is gone from the row: it is
          the BACK of the details card now, which is where it belongs -
          looking up what is near a location is something you do TO that
          location, not a third pane competing with it for width. That is
          what bought the map three quarters of the row. */}
      <CRow className="mb-4">
        {/* Details, front and back. A flip needs an explicit height, and
            MAP_HEIGHT is the one the map beside it already sets. */}
        <CCol xs={12} md={3}>
          <div style={{ perspective: 1200, height: MAP_HEIGHT }}>
            <div style={{
              position: 'relative', width: '100%', height: '100%',
              transformStyle: 'preserve-3d',
              transition: 'transform 0.55s cubic-bezier(0.4, 0.2, 0.2, 1)',
              transform: detailsFlipped ? 'rotateY(180deg)' : 'rotateY(0deg)',
            }}>

              {/* ── FRONT: the fields ── */}
              <div style={{
                position: 'absolute', width: '100%', height: '100%',
                backfaceVisibility: 'hidden', WebkitBackfaceVisibility: 'hidden',
              }}>
                <CCard className="h-100" style={{ overflow: 'hidden' }}>
                  <CCardHeader className="d-flex justify-content-between align-items-center">
                    <strong>{location.name}</strong>
                    <div className="d-flex gap-2 flex-wrap">
                      {/* Detect Nearby, then Edit. The results are the BACK of
                          this card, so the button that fetches them belongs on
                          its front - and pressing it turns the card over. */}
                      <CButton
                        color="success"
                        size="sm"
                        onClick={handleDetectNearby}
                        disabled={aiLoading}
                        title="Look up what is named near these coordinates"
                      >
                        {aiLoading
                          ? <><CSpinner size="sm" className="me-1" />Looking…</>
                          : <><CIcon icon={cilBolt} style={{ width: 13, height: 13 }} className="me-1" />Detect Nearby</>
                        }
                      </CButton>

                      <CButton
                        color="primary"
                        size="sm"
                        onClick={isEditing ? handleCancelEditing : handleStartEditing}
                      >
                        {isEditing
                          ? <><CIcon icon={cilX} style={{ width: 13, height: 13 }} className="me-1" />Cancel</>
                          : <><CIcon icon={cilPencil} style={{ width: 13, height: 13 }} className="me-1" />Edit</>
                        }
                      </CButton>
                    </div>
                  </CCardHeader>
                  <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
                    <CCardBody>
                      {!isEditing ? (
                        /* ── View mode ── */
                        /* EVERY field, including the empty ones.
                           Description and Source were rendered only when set,
                           so an empty one was indistinguishable from a field
                           that does not exist - and this card's whole job is
                           to say what is known about a location. A dash is an
                           answer; absence is not.

                           No map here either: there is a real one in the
                           column beside this, and the static image this card
                           used to carry was the less useful of the two since
                           it cannot be dragged to set a coordinate. */
                        <dl className="mb-0" style={{ fontSize: '0.86rem' }}>
                          <Detail label="Name">{location.name}</Detail>

                          <Detail label="Verified">
                            {location.confirmed
                              ? <CBadge color="success" style={{ fontSize: '0.68rem' }}>✓ Verified</CBadge>
                              : <span className="text-muted">Not yet</span>}
                          </Detail>

                          <Detail label="Coordinates">
                            {location.latitude != null && location.longitude != null ? (
                              <>
                                <span style={{ fontFamily: 'monospace' }}>
                                  {formatCoord(location.latitude)}°, {formatCoord(location.longitude)}°
                                </span>
                                {mapsUrl && (
                                  <a href={mapsUrl} target="_blank" rel="noopener noreferrer"
                                     className="ms-2" style={{ fontSize: '0.8rem' }}>
                                    open ↗
                                  </a>
                                )}
                              </>
                            ) : null}
                          </Detail>

                          <Detail label="Description">{location.description}</Detail>
                          <Detail label="Source">{location.source}</Detail>

                          {/* The gallery's total rather than a field: the
                              detail endpoint returns the locations row alone,
                              and the gallery below has already counted them. */}
                          <Detail label="Photos">
                            {photosTotal ? photosTotal.toLocaleString() : null}
                          </Detail>

                          <Detail label="Added">{formatDateTime(location.created_at)}</Detail>
                          <Detail label="Updated">{formatDateTime(location.updated_at)}</Detail>
                        </dl>
                      ) : (
                        /* ── Edit mode ── */
                        <CForm>
                          <div className="mb-3">
                            <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Name *</CFormLabel>
                            <CFormInput
                              value={formData.name}
                              onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                              placeholder="Enter location name"
                            />
                          </div>
                          <div className="mb-3">
                            <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Latitude</CFormLabel>
                            <CFormInput
                              type="number"
                              step="any"
                              value={formData.lat}
                              onChange={(e) => setFormData({ ...formData, lat: e.target.value })}
                              placeholder="e.g. 48.8566"
                            />
                          </div>
                          <div className="mb-3">
                            <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Longitude</CFormLabel>
                            <CFormInput
                              type="number"
                              step="any"
                              value={formData.long}
                              onChange={(e) => setFormData({ ...formData, long: e.target.value })}
                              placeholder="e.g. 2.3522"
                            />
                          </div>
                          <div className="mb-3">
                            <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Description</CFormLabel>
                            <CFormTextarea
                              rows={3}
                              value={formData.description}
                              onChange={(e) => setFormData({ ...formData, description: e.target.value })}
                              placeholder="Enter description"
                            />
                          </div>
                          <div className="mb-3">
                            <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Source</CFormLabel>
                            <CFormInput
                              value={formData.source}
                              onChange={(e) => setFormData({ ...formData, source: e.target.value })}
                              placeholder="e.g. GPS, Manual, OpenStreetMap"
                            />
                          </div>
                          <div className="d-flex justify-content-between align-items-center mt-3">
                            <CButton
                              color="danger"
                              variant="outline"
                              size="sm"
                              onClick={() => setShowDeleteConfirmModal(true)}
                              disabled={saving}
                            >
                              <CIcon icon={cilTrash} className="me-1" style={{ width: 13, height: 13 }} />
                              Delete Location
                            </CButton>
                            <div className="d-flex gap-2">
                              <CButton color="secondary" variant="outline" onClick={handleCancelEditing} disabled={saving}>
                                Cancel
                              </CButton>
                              <CButton color="primary" onClick={handleUpdateLocation} disabled={!formData.name || saving}>
                                {saving
                                  ? <><CSpinner size="sm" className="me-1" />Saving…</>
                                  : <><CIcon icon={cilCheck} style={{ width: 14, height: 14 }} className="me-1" />Save Changes</>
                                }
                              </CButton>
                            </div>
                          </div>
                        </CForm>
                      )}
                    </CCardBody>
                  </div>
                </CCard>
              </div>

              {/* ── BACK: what is nearby ── */}
              <div style={{
                position: 'absolute', width: '100%', height: '100%',
                backfaceVisibility: 'hidden', WebkitBackfaceVisibility: 'hidden',
                transform: 'rotateY(180deg)',
              }}>
                <CCard className="h-100 border-success" style={{ overflow: 'hidden' }}>
                  <CCardHeader className="d-flex justify-content-between align-items-center bg-success bg-opacity-10">
                    <div className="d-flex align-items-center gap-2">
                      <CIcon icon={cilBolt} style={{ width: 14, height: 14, color: '#198754' }} />
                      {/* Not "AI Suggestions". This is a query to a GIS -
                          Nominatim and OSMnx, through backend-geoloc - and
                          calling it AI both oversells it and misleads about
                          where the names come from. */}
                      <strong style={{ color: '#198754' }}>Nearby</strong>
                      {aiPois.length > 0 && (
                        <CBadge color="success">{aiPois.length} found</CBadge>
                      )}
                    </div>
                    <CButton color="secondary" variant="outline" size="sm"
                             onClick={() => setDetailsFlipped(false)}
                             title="Back to this location's details">
                      <CIcon icon={cilArrowLeft} style={{ width: 13, height: 13 }} className="me-1" />
                      Details
                    </CButton>
                  </CCardHeader>
                  <div style={{ overflowY: 'auto', flex: 1, minHeight: 0 }}>
                    <CCardBody>
                      {aiLoading && (
                        <div className="text-center py-5">
                          <CSpinner color="success" />
                          <p className="mt-3 text-muted">Looking up what is nearby…</p>
                        </div>
                      )}

                      {aiError && !aiLoading && (
                        <>
                          <CAlert color="warning" className="mb-2">{aiError}</CAlert>
                          {/* Retry, because a failure here is usually temporary
                              and a second attempt is usually FASTER: the slow
                              path is a public geocoder, and backend-geoloc
                              caches a region to disk once it has seen it - the
                              same coordinate was measured at 63.8s and then
                              5.7s. Worth saying so on the button, or a retry
                              looks like a wish rather than a plan.

                              Offered only when there is a coordinate to look
                              up. Without one no number of retries can work, and
                              a button that always fails is worse than none. */}
                          {location.latitude != null && location.longitude != null && (
                            <div className="d-flex justify-content-end">
                              <CButton
                                color="success"
                                variant="outline"
                                size="sm"
                                onClick={handleDetectNearby}
                                title="Ask again. The geocoder is usually quicker the second time, having cached the area."
                              >
                                <CIcon icon={cilReload} style={{ width: 13, height: 13 }} className="me-1" />
                                Retry
                              </CButton>
                            </div>
                          )}
                        </>
                      )}

                      {!aiLoading && aiPois.length > 0 && (
                        <>
                          {/* Which of the nearby names to read fields from */}
                          {aiPois.length > 1 && (
                            <div className="mb-3">
                              <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>
                                Choose one ({aiPois.length} found)
                              </CFormLabel>
                              <CFormSelect
                                value={selectedPoiIndex}
                                onChange={(e) => setSelectedPoiIndex(Number(e.target.value))}
                                size="sm"
                              >
                                {aiPois.map((poi, idx) => (
                                  <option key={idx} value={idx}>
                                    {poi.name} {poi.source ? `(${poi.source})` : ''}
                                  </option>
                                ))}
                              </CFormSelect>
                            </div>
                          )}

                          {/* Each field is clickable, and copies into the edit form */}
                          {selectedPoi && (
                            <>
                              <p className="text-muted small mb-2">
                                Click any field below to copy it into the edit form.
                              </p>

                              {/* Name */}
                              <div className="mb-3">
                                <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Name</CFormLabel>
                                <div
                                  className="form-control form-control-sm"
                                  style={{
                                    cursor: 'pointer',
                                    backgroundColor: '#f8fff8',
                                    borderColor: '#198754',
                                    transition: 'background-color 0.15s',
                                  }}
                                  title="Click to use this name"
                                  onClick={() => applyPoiField('name', selectedPoi.name)}
                                  onMouseEnter={(e) => e.currentTarget.style.backgroundColor = '#d1f0d1'}
                                  onMouseLeave={(e) => e.currentTarget.style.backgroundColor = '#f8fff8'}
                                >
                                  {selectedPoi.name || <span className="text-muted">—</span>}
                                </div>
                              </div>

                              {/* Address → Description */}
                              <div className="mb-3">
                                <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>
                                  Address <span className="text-muted fw-normal">(→ Description)</span>
                                </CFormLabel>
                                <div
                                  className="form-control form-control-sm"
                                  style={{
                                    cursor: 'pointer',
                                    backgroundColor: '#f8fff8',
                                    borderColor: '#198754',
                                    minHeight: '60px',
                                    whiteSpace: 'pre-wrap',
                                    wordBreak: 'break-word',
                                    transition: 'background-color 0.15s',
                                  }}
                                  title="Click to use as description"
                                  onClick={() => applyPoiField('description', selectedPoi.address)}
                                  onMouseEnter={(e) => e.currentTarget.style.backgroundColor = '#d1f0d1'}
                                  onMouseLeave={(e) => e.currentTarget.style.backgroundColor = '#f8fff8'}
                                >
                                  {selectedPoi.address || <span className="text-muted">—</span>}
                                </div>
                              </div>

                              {/* Source */}
                              <div className="mb-3">
                                <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Source</CFormLabel>
                                <div
                                  className="form-control form-control-sm"
                                  style={{
                                    cursor: 'pointer',
                                    backgroundColor: '#f8fff8',
                                    borderColor: '#198754',
                                    transition: 'background-color 0.15s',
                                  }}
                                  title="Click to use this source"
                                  onClick={() => applyPoiField('source', selectedPoi.source)}
                                  onMouseEnter={(e) => e.currentTarget.style.backgroundColor = '#d1f0d1'}
                                  onMouseLeave={(e) => e.currentTarget.style.backgroundColor = '#f8fff8'}
                                >
                                  {selectedPoi.source || <span className="text-muted">—</span>}
                                </div>
                              </div>

                              {/* Apply all button */}
                              <div className="d-flex justify-content-end mt-3">
                                <CButton
                                  color="success"
                                  size="sm"
                                  onClick={applyAllPoiFields}
                                >
                                  <CIcon icon={cilCheck} style={{ width: 13, height: 13 }} className="me-1" />
                                  Apply All Fields
                                </CButton>
                              </div>
                            </>
                          )}
                        </>
                      )}
                    </CCardBody>
                  </div>
                </CCard>
              </div>

            </div>
          </div>
        </CCol>

        {/* The map, always mounted. It is a pin-dropper, not a map to explore -
        whole-catalog mapping is deferred (location-spec.md 10). */}
        <CCol xs={12} md={9}>
            <CCard className="h-100 border-info">
              <CCardHeader className="d-flex justify-content-between align-items-center bg-info bg-opacity-10">
                <div className="d-flex align-items-center gap-2">
                  <CIcon icon={cilMap} style={{ width: 14, height: 14, color: '#0dcaf0' }} />
                  <strong style={{ color: '#055160' }}>Map</strong>
                  {pinPosition && (
                    <CBadge color="info" style={{ fontSize: '0.7rem' }}>
                      📍 {pinPosition.lat.toFixed(5)}°, {pinPosition.lng.toFixed(5)}°
                    </CBadge>
                  )}
                </div>
              </CCardHeader>
              <CCardBody className="p-0" style={{ position: 'relative' }}>
                <div
                  className="text-muted small px-3 py-2"
                  style={{ backgroundColor: '#f0f9ff', borderBottom: '1px solid #bee3f8' }}
                >
                  Click anywhere on the map to drop a pin and set the coordinates.
                </div>
                <MapContainer
                  center={getMapCenter()}
                  zoom={getMapZoom()}
                  style={{ height: MAP_HEIGHT, width: '100%', cursor: 'crosshair' }}
                  scrollWheelZoom={true}
                >
                  <TileLayer
                    attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
                    url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
                  />
                  <MapClickHandler onMapClick={handleMapClick} />
                  {pinPosition && (
                    <Marker
                      position={[pinPosition.lat, pinPosition.lng]}
                      draggable={true}
                      eventHandlers={{
                        dragend(e) {
                          const { lat, lng } = e.target.getLatLng();
                          handleMapClick(lat, lng);
                        },
                      }}
                    />
                  )}
                </MapContainer>
              </CCardBody>
            </CCard>
        </CCol>
      </CRow>
      </div>{/* end topSectionRef */}

      {/* Gallery Card */}
      <CCard>
        <CCardHeader className="d-flex align-items-center flex-wrap gap-2">
          {/* Year / month filter buttons */}
          <div className="d-flex gap-1 flex-wrap align-items-center">
            <button
              className={`btn btn-sm ${selectedYear === null ? 'btn-primary' : 'btn-outline-secondary'}`}
              onClick={() => { setSelectedYear(null); setSelectedMonth(null); }}
            >
              All
            </button>
            {sortedYears.map((year) => (
              <button
                key={year}
                className={`btn btn-sm ${selectedYear === year ? 'btn-primary' : 'btn-outline-secondary'}`}
                onClick={() => { setSelectedYear(year); setSelectedMonth(null); }}
              >
                {year}
              </button>
            ))}
            {selectedYear !== null && availableMonths.length > 0 && (
              <>
                <span className="text-muted mx-1" style={{ fontSize: '0.8rem' }}>|</span>
                <button
                  className={`btn btn-sm ${selectedMonth === null ? 'btn-primary' : 'btn-outline-secondary'}`}
                  onClick={() => setSelectedMonth(null)}
                >
                  All months
                </button>
                {availableMonths.map((month) => (
                  <button
                    key={month}
                    className={`btn btn-sm ${selectedMonth === month ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={() => setSelectedMonth(month)}
                  >
                    {MONTH_NAMES[month]}
                  </button>
                ))}
              </>
            )}
            {photosTotal > 0 && (
              <CBadge color="secondary" className="ms-1">{photosTotal}</CBadge>
            )}
          </div>
          {/* Pagination controls */}
          {totalGalleryPages > 1 && (
            <div className="d-flex align-items-center gap-2 ms-auto">
              <CButton
                color="secondary"
                size="sm"
                onClick={() => setGalleryPage((p) => p - 1)}
                disabled={galleryPage === 0}
              >
                ← Previous
              </CButton>
              <span style={{ fontSize: '0.85rem', color: '#666', whiteSpace: 'nowrap' }}>
                Page {galleryPage + 1} of {totalGalleryPages}
              </span>
              <CButton
                color="secondary"
                size="sm"
                onClick={() => setGalleryPage((p) => p + 1)}
                disabled={galleryPage === totalGalleryPages - 1}
              >
                Next →
              </CButton>
            </div>
          )}
        </CCardHeader>
        <CCardBody ref={galleryBodyRef} style={{ height: galleryBodyH, overflow: 'hidden' }}>
          {photosLoading ? (
            <div className="text-center py-5">
              <CSpinner color="primary" />
            </div>
          ) : photosTotal === 0 ? (
            <div className="text-center py-5 text-muted">
              <p>No photos found for this location{selectedYear ? ` for ${selectedYear}` : ''}{selectedMonth ? ` / ${MONTH_NAMES[selectedMonth]}` : ''}. Photos are shown when they belong to an event associated with this location.</p>
            </div>
          ) : (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px' }}>
              {pagePhotos.map((photo) => (
                <div key={photo.file_id} style={{ width: 160, flexShrink: 0 }}>
                  <CCard
                    style={{ cursor: 'pointer', transition: 'transform 0.2s, box-shadow 0.2s' }}
                    onMouseEnter={(e) => {
                      e.currentTarget.style.transform = 'translateY(-4px)';
                      e.currentTarget.style.boxShadow = '0 4px 12px rgba(0,0,0,0.15)';
                    }}
                    onMouseLeave={(e) => {
                      e.currentTarget.style.transform = 'translateY(0)';
                      e.currentTarget.style.boxShadow = '';
                    }}
                    onClick={() => navigate(`/photos/${catalogId}/${photo.file_id}`)}
                  >
                    <div
                      style={{
                        height: 160,
                        background: '#f0f0f0',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        overflow: 'hidden',
                      }}
                    >
                      <img
                        src={galleryThumbUrl(catalogId, photo.file_id)}
                        alt={photo.display_name}
                        loading="lazy"
                        style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                        title={photo.display_name}
                      />
                    </div>
                    <CCardBody className="p-2">
                      <small className="text-muted d-block text-truncate" title={photo.display_name}>
                        {photo.display_name}
                      </small>
                      {photo.creation_date && (
                        <small className="text-muted d-block mt-1" style={{ fontSize: '0.72rem' }}>
                          {photo.creation_date.substring(0, 10)}
                        </small>
                      )}
                    </CCardBody>
                  </CCard>
                </div>
              ))}
            </div>
          )}
        </CCardBody>
      </CCard>

      {/* Delete Confirmation Modal */}
      <CModal visible={showDeleteConfirmModal} onClose={() => setShowDeleteConfirmModal(false)}>
        <CModalHeader>
          <CModalTitle>Delete Location</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p>Are you sure you want to delete <strong>{location?.name}</strong>?</p>
          <p className="text-muted small">This action cannot be undone.</p>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowDeleteConfirmModal(false)}>
            Cancel
          </CButton>
          <CButton color="danger" onClick={handleDeleteLocation}>
            <CIcon icon={cilTrash} className="me-2" />
            Delete Location
          </CButton>
        </CModalFooter>
      </CModal>
    </CContainer>
  );
};

export default LocationDetail;
