import React, { useState, useEffect } from 'react';
import { useParams, useNavigate } from 'react-router-dom';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CContainer,
  CButton,
  CRow,
  CCol,
  CSpinner,
  CAlert,
  CBadge,
  CListGroup,
  CListGroupItem,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilArrowLeft,
  cilCalendar,
  cilLocationPin,
  cilFace,
  cilCamera,
  cilPencil,
} from '@coreui/icons';
import { photoAPI } from '../services/api';
import { API_HOST } from '../config';
import { getTimezoneOptions, getBrowserTimezone } from '../utils/timezones';

// ─── Helpers ──────────────────────────────────────────────────────────────────

function formatDateTime(dt) {
  if (!dt) return '—';
  try {
    const d = new Date(dt);
    if (isNaN(d.getTime())) return dt;
    return d.toLocaleString('en-US', {
      year: 'numeric', month: 'long', day: 'numeric',
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch {
    return dt;
  }
}

function formatDate(dt) {
  if (!dt) return '—';
  try {
    const d = new Date(dt);
    if (isNaN(d.getTime())) return dt;
    return d.toLocaleString('en-US', {
      year: 'numeric', month: 'long', day: 'numeric',
    });
  } catch {
    return dt;
  }
}

function formatTime(dt) {
  if (!dt) return '—';
  try {
    const d = new Date(dt);
    if (isNaN(d.getTime())) return dt;
    return d.toLocaleString('en-US', {
      hour: '2-digit', minute: '2-digit', second: '2-digit',
    });
  } catch {
    return dt;
  }
}

function formatFileSize(bytes) {
  if (!bytes) return '—';
  const kb = bytes / 1024;
  if (kb < 1024) return `${kb.toFixed(1)} KB`;
  const mb = kb / 1024;
  if (mb < 1024) return `${mb.toFixed(1)} MB`;
  const gb = mb / 1024;
  return `${gb.toFixed(2)} GB`;
}

// ─── Main Component ───────────────────────────────────────────────────────────

const PhotoDetail = () => {
  const { catalogId, photoId } = useParams();
  const navigate = useNavigate();
  const [photo, setPhoto] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);

  // ── Date-edit state ──────────────────────────────────────────────────────────
  const [editingDate, setEditingDate]   = useState(false);
  const [dateInput,   setDateInput]     = useState('');   // "YYYY-MM-DD"
  const [timeInput,   setTimeInput]     = useState('');   // "HH:MM:SS"
  const [tzInput,     setTzInput]       = useState(getBrowserTimezone);
  const [dateSaving,  setDateSaving]    = useState(false);
  const [dateError,   setDateError]     = useState(null);
  const [dateSuccess, setDateSuccess]   = useState(null);

  useEffect(() => {
    loadPhoto();
  }, [catalogId, photoId]);

  const loadPhoto = async () => {
    try {
      setLoading(true);
      setError(null);
      const response = await photoAPI.getById(catalogId, photoId);
      setPhoto(response.data);
    } catch (err) {
      setError(err.response?.data?.error || 'Failed to load photo details');
    } finally {
      setLoading(false);
    }
  };

  const handleBack = () => {
    navigate(`/catalogs/${catalogId}/photos`);
  };

  // ── Date-edit handlers ───────────────────────────────────────────────────────

  const handleEditDate = () => {
    const src = photo.master_datetime;
    const d   = src ? new Date(src) : new Date();
    // Use local-time components so the pre-populated value matches what the
    // user sees in the read-only display, not an offset-adjusted UTC value.
    const pad = (n) => String(n).padStart(2, '0');
    setDateInput(
      `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`
    );
    setTimeInput(
      `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
    );
    setDateError(null);
    setEditingDate(true);
  };

  const handleSaveDate = async () => {
    setDateSaving(true);
    setDateError(null);
    try {
      const isoLocal = `${dateInput}T${timeInput}`;   // "YYYY-MM-DDTHH:MM:SS"
      await photoAPI.updateDate(catalogId, photoId, isoLocal, tzInput);
      await loadPhoto();                               // re-fetch fresh photo data
      setEditingDate(false);
      setDateSuccess('Date updated. File has been renamed and moved.');
    } catch (err) {
      setDateError(err.response?.data?.error || 'Failed to update date');
    } finally {
      setDateSaving(false);
    }
  };

  // ── Loading / error guards ────────────────────────────────────────────────────

  if (loading) {
    return (
      <CContainer fluid className="py-4 px-4">
        <div className="text-center py-5">
          <CSpinner color="primary" />
          <div className="mt-2 text-muted">Loading photo details…</div>
        </div>
      </CContainer>
    );
  }

  if (error) {
    return (
      <CContainer fluid className="py-4 px-4">
        <CAlert color="danger">{error}</CAlert>
        <CButton color="secondary" onClick={handleBack}>
          <CIcon icon={cilArrowLeft} className="me-2" />
          Back to Photos
        </CButton>
      </CContainer>
    );
  }

  if (!photo) {
    return (
      <CContainer fluid className="py-4 px-4">
        <CAlert color="warning">Photo not found</CAlert>
        <CButton color="secondary" onClick={handleBack}>
          <CIcon icon={cilArrowLeft} className="me-2" />
          Back to Photos
        </CButton>
      </CContainer>
    );
  }

  // Use the HTTP endpoint to serve the image
  const imageUrl = `${API_HOST}/api/catalogs/${catalogId}/photos/${photo.id}/image`;

  const tzOptions = getTimezoneOptions();

  return (
    <CContainer fluid className="py-4 px-4">
      {/* Header with back button */}
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <CButton
            color="secondary"
            variant="ghost"
            onClick={handleBack}
            className="mb-2"
          >
            <CIcon icon={cilArrowLeft} className="me-2" />
            Back to Photos
          </CButton>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>
            {photo.file_name}
            {photo.alternative_file_name && (
              <div style={{ fontSize: '0.75rem', color: '#888', fontWeight: 'normal', marginTop: '0.25rem' }}>
                Displaying: {photo.alternative_file_name}
              </div>
            )}
          </h4>
        </div>
      </div>

      {/* Success banner (date update) */}
      {dateSuccess && (
        <CAlert color="success" dismissible onClose={() => setDateSuccess(null)} className="mb-3">
          {dateSuccess}
        </CAlert>
      )}

      {/* First row: Description and People */}
      <CRow className="mb-3">
        {/* Left column: Description */}
        <CCol md={6}>
          <CCard className="h-100">
            <CCardHeader className="d-flex align-items-center py-2">
              <strong>Description</strong>
            </CCardHeader>
            <CCardBody className="py-3">
              {photo.description ? (
                <div style={{ fontSize: '0.95rem', lineHeight: '1.6' }}>
                  {photo.description}
                </div>
              ) : (
                <div style={{ fontSize: '0.85rem', color: '#888', fontStyle: 'italic' }}>
                  No description available
                </div>
              )}
            </CCardBody>
          </CCard>
        </CCol>

        {/* Right column: People */}
        <CCol md={6}>
          <CCard className="h-100">
            <CCardHeader className="d-flex align-items-center py-2">
              <CIcon icon={cilFace} className="me-2" />
              <strong>People</strong>
              {photo.people && photo.people.length > 0 && (
                <CBadge color="primary" className="ms-2">{photo.people.length}</CBadge>
              )}
            </CCardHeader>
            <CCardBody className="py-3">
              {photo.people && photo.people.length > 0 ? (
                <div className="d-flex flex-wrap gap-2">
                  {photo.people.map((person) => (
                    <CBadge
                      key={person.id}
                      color="info"
                      style={{ fontSize: '0.9rem', padding: '0.5rem 0.75rem', cursor: 'pointer' }}
                      onClick={() => navigate(`/people/${catalogId}/${person.id}`)}
                    >
                      {person.name}
                    </CBadge>
                  ))}
                </div>
              ) : (
                <div style={{ fontSize: '0.85rem', color: '#888', fontStyle: 'italic' }}>
                  No people tagged
                </div>
              )}
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>

      {/* Second row: Date/Time, Events, and Locations */}
      <CRow className="mb-3">
        {/* Left column: Date and Time (with inline edit) */}
        <CCol md={4}>
          <CCard className="h-100">
            <CCardBody className="py-3">
              {/* Card header row with edit button */}
              <div className="d-flex justify-content-between align-items-center mb-2">
                <div className="d-flex align-items-center">
                  <CIcon icon={cilCalendar} className="me-2" style={{ color: '#1a73e8' }} />
                  <strong style={{ fontSize: '0.85rem' }}>Date &amp; Time</strong>
                </div>
                {!editingDate && (
                  <CButton
                    size="sm"
                    color="secondary"
                    variant="ghost"
                    onClick={handleEditDate}
                    title="Edit date"
                  >
                    <CIcon icon={cilPencil} />
                  </CButton>
                )}
              </div>

              {editingDate ? (
                <>
                  <div className="mb-2">
                    <label style={{ fontSize: '0.75rem', color: '#888' }}>Date</label>
                    <input
                      type="date"
                      className="form-control form-control-sm"
                      value={dateInput}
                      onChange={(e) => setDateInput(e.target.value)}
                    />
                  </div>
                  <div className="mb-2">
                    <label style={{ fontSize: '0.75rem', color: '#888' }}>Time</label>
                    <input
                      type="time"
                      step="1"
                      className="form-control form-control-sm"
                      value={timeInput}
                      onChange={(e) => setTimeInput(e.target.value)}
                    />
                  </div>
                  <div className="mb-3">
                    <label style={{ fontSize: '0.75rem', color: '#888' }}>Time Zone</label>
                    <select
                      className="form-select form-select-sm"
                      value={tzInput}
                      onChange={(e) => setTzInput(e.target.value)}
                    >
                      {tzOptions.map((tz) => (
                        <option key={tz} value={tz}>{tz}</option>
                      ))}
                    </select>
                  </div>
                  {dateError && (
                    <div style={{ fontSize: '0.8rem', color: '#c00' }} className="mb-2">
                      {dateError}
                    </div>
                  )}
                  <div className="d-flex gap-2">
                    <CButton
                      size="sm"
                      color="primary"
                      onClick={handleSaveDate}
                      disabled={dateSaving || !dateInput || !timeInput}
                    >
                      {dateSaving ? <CSpinner size="sm" /> : 'Save'}
                    </CButton>
                    <CButton
                      size="sm"
                      color="secondary"
                      variant="ghost"
                      onClick={() => { setEditingDate(false); setDateError(null); }}
                      disabled={dateSaving}
                    >
                      Cancel
                    </CButton>
                  </div>
                </>
              ) : (
                <>
                  <div className="mb-2">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>Date</div>
                    <div style={{ fontSize: '0.95rem', fontWeight: 500 }}>
                      {formatDate(photo.master_datetime)}
                    </div>
                  </div>
                  <div>
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>Time</div>
                    <div style={{ fontSize: '0.95rem', fontWeight: 500 }}>
                      {formatTime(photo.master_datetime)}
                    </div>
                  </div>
                </>
              )}
            </CCardBody>
          </CCard>
        </CCol>

        {/* Middle column: Events */}
        <CCol md={4}>
          <CCard className="h-100">
            <CCardHeader className="d-flex align-items-center py-2">
              <CIcon icon={cilCalendar} className="me-2" />
              <strong>Events</strong>
              {photo.events && photo.events.length > 0 && (
                <CBadge color="primary" className="ms-2">{photo.events.length}</CBadge>
              )}
            </CCardHeader>
            <CCardBody className="py-3" style={{ maxHeight: '200px', overflowY: 'auto' }}>
              {photo.events && photo.events.length > 0 ? (
                <CListGroup flush>
                  {photo.events.map((event) => (
                    <CListGroupItem
                      key={event.id}
                      className="px-0 py-2 border-0"
                    >
                      <div
                        role="button"
                        tabIndex={0}
                        onClick={() => navigate(`/events/${catalogId}/${event.id}`)}
                        onKeyDown={(e) => e.key === 'Enter' && navigate(`/events/${catalogId}/${event.id}`)}
                        style={{
                          fontSize: '0.9rem', fontWeight: 500, color: '#1a73e8',
                          cursor: 'pointer', textDecoration: 'underline',
                          display: 'inline',
                        }}
                      >
                        {event.name}
                      </div>
                      {event.event_type && (
                        <CBadge color="secondary" className="me-2" style={{ fontSize: '0.65rem' }}>
                          {event.event_type}
                        </CBadge>
                      )}
                      {event.start_time && (
                        <div style={{ fontSize: '0.75rem', color: '#666' }}>
                          {formatDate(event.start_time)}
                        </div>
                      )}
                    </CListGroupItem>
                  ))}
                </CListGroup>
              ) : (
                <div style={{ fontSize: '0.85rem', color: '#888', fontStyle: 'italic' }}>
                  No events tagged
                </div>
              )}
            </CCardBody>
          </CCard>
        </CCol>

        {/* Right column: Locations */}
        <CCol md={4}>
          <CCard className="h-100">
            <CCardHeader className="d-flex align-items-center py-2">
              <CIcon icon={cilLocationPin} className="me-2" />
              <strong>Locations</strong>
              {photo.locations && photo.locations.length > 0 && (
                <CBadge color="primary" className="ms-2">{photo.locations.length}</CBadge>
              )}
            </CCardHeader>
            <CCardBody className="py-3" style={{ maxHeight: '200px', overflowY: 'auto' }}>
              {photo.locations && photo.locations.length > 0 ? (
                <CListGroup flush>
                  {photo.locations.map((location) => (
                    <CListGroupItem
                      key={location.id}
                      className="px-0 py-2 border-0"
                      style={{ cursor: 'pointer' }}
                      onClick={() => navigate(`/locations/${catalogId}/${location.id}`)}
                    >
                      <div style={{ fontSize: '0.9rem', fontWeight: 500, color: '#1a73e8' }}>
                        {location.name}
                      </div>
                      {(location.latitude && location.longitude) && (
                        <div style={{ fontSize: '0.75rem', color: '#888' }}>
                          {location.latitude.toFixed(4)}, {location.longitude.toFixed(4)}
                        </div>
                      )}
                    </CListGroupItem>
                  ))}
                </CListGroup>
              ) : (
                <div style={{ fontSize: '0.85rem', color: '#888', fontStyle: 'italic' }}>
                  No locations tagged
                </div>
              )}
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>

      {/* Image and EXIF data in two columns */}
      <CRow className="mb-3">
        {/* Left column: Image */}
        <CCol lg={8}>
          <CCard>
            <CCardBody className="p-0">
              <div style={{ position: 'relative', width: '100%', background: '#000' }}>
                <img
                  src={imageUrl}
                  alt={photo.file_name}
                  style={{
                    width: '100%',
                    height: 'auto',
                    display: 'block',
                  }}
                  onError={(e) => {
                    e.target.style.display = 'none';
                    e.target.parentElement.innerHTML = '<div style="padding: 4rem; text-align: center; color: #999;"><div style="font-size: 3rem;">📷</div><div style="margin-top: 1rem;">Image not available</div></div>';
                  }}
                />
              </div>
            </CCardBody>
          </CCard>
        </CCol>

        {/* Right column: EXIF data */}
        <CCol lg={4}>
          <CCard>
            <CCardHeader className="d-flex align-items-center">
              <CIcon icon={cilCamera} className="me-2" />
              <strong>Photo Information</strong>
            </CCardHeader>
            <CCardBody>
              <CListGroup flush>
                {photo.camera_make && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>Camera Make</div>
                    <div style={{ fontSize: '0.9rem' }}>{photo.camera_make}</div>
                  </CListGroupItem>
                )}
                {photo.camera_model && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>Camera Model</div>
                    <div style={{ fontSize: '0.9rem' }}>{photo.camera_model}</div>
                  </CListGroupItem>
                )}
                {photo.lens_model && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>Lens</div>
                    <div style={{ fontSize: '0.9rem' }}>{photo.lens_model}</div>
                  </CListGroupItem>
                )}
                {photo.focal_length && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>Focal Length</div>
                    <div style={{ fontSize: '0.9rem' }}>{photo.focal_length}mm</div>
                  </CListGroupItem>
                )}
                {photo.aperture && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>Aperture</div>
                    <div style={{ fontSize: '0.9rem' }}>f/{photo.aperture}</div>
                  </CListGroupItem>
                )}
                {photo.shutter_speed && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>Shutter Speed</div>
                    <div style={{ fontSize: '0.9rem' }}>{photo.shutter_speed}s</div>
                  </CListGroupItem>
                )}
                {photo.iso && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>ISO</div>
                    <div style={{ fontSize: '0.9rem' }}>{photo.iso}</div>
                  </CListGroupItem>
                )}
                {photo.width && photo.height && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>Dimensions</div>
                    <div style={{ fontSize: '0.9rem' }}>{photo.width} × {photo.height}</div>
                  </CListGroupItem>
                )}
                {photo.file_size && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>File Size</div>
                    <div style={{ fontSize: '0.9rem' }}>{formatFileSize(photo.file_size)}</div>
                  </CListGroupItem>
                )}
                {photo.file_type && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>File Type</div>
                    <div style={{ fontSize: '0.9rem' }}>{photo.file_type.toUpperCase()}</div>
                  </CListGroupItem>
                )}
                {(photo.latitude && photo.longitude) && (
                  <CListGroupItem className="px-0 py-2 border-0">
                    <div style={{ fontSize: '0.75rem', color: '#888' }}>GPS Coordinates</div>
                    <div style={{ fontSize: '0.9rem' }}>
                      {photo.latitude.toFixed(6)}, {photo.longitude.toFixed(6)}
                    </div>
                  </CListGroupItem>
                )}
              </CListGroup>
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>


    </CContainer>
  );
};

export default PhotoDetail;
