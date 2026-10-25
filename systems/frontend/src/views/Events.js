import React, { useState, useEffect, useCallback, useRef, useMemo } from 'react';
import { useNavigate, useSearchParams } from 'react-router-dom';
import {
  CCard,
  CCardBody,
  CCardHeader,
  CContainer,
  CButton,
  CRow,
  CCol,
  CForm,
  CFormInput,
  CFormTextarea,
  CSpinner,
  CAlert,
  CBadge,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import {
  cilCalendar,
  cilPencil,
  cilX,
  cilCheck,
  cilLocationPin,
  cilPlus,
  cilTrash,
  cilPeople,
  cilImage,
} from '@coreui/icons';
import { eventAPI, userSettingsAPI, calendarAPI } from '../services/api';
import { longDate, neighbours } from '../utils/calendarDays';
import { galleryThumbUrl, GALLERY_THUMB_SIZE } from '../config';
import { useCatalog } from '../context/CatalogContext';

// ─── Helpers ──────────────────────────────────────────────────────────────────

const MONTH_NAMES = [
  'January', 'February', 'March', 'April', 'May', 'June',
  'July', 'August', 'September', 'October', 'November', 'December',
];

function formatDateTime(dt) {
  if (!dt) return '—';
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

function toInputDatetime(dt) {
  if (!dt) return '';
  try {
    const d = new Date(dt);
    if (isNaN(d.getTime())) return '';
    const pad = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
  } catch {
    return '';
  }
}

/** Group events by year then month */
function groupEventsByYearMonth(events) {
  const grouped = {};
  events.forEach((ev) => {
    const dt = ev.start_time ? new Date(ev.start_time) : null;
    const year = dt && !isNaN(dt.getTime()) ? dt.getFullYear() : 'Unknown';
    const month = dt && !isNaN(dt.getTime()) ? dt.getMonth() : -1; // 0-based
    if (!grouped[year]) grouped[year] = {};
    if (!grouped[year][month]) grouped[year][month] = [];
    grouped[year][month].push(ev);
  });

  Object.keys(grouped).forEach((year) => {
    Object.keys(grouped[year]).forEach((month) => {
      grouped[year][month].sort((a, b) => {
        const aDate = a.start_time ? new Date(a.start_time) : null;
        const bDate = b.start_time ? new Date(b.start_time) : null;
        const aValid = aDate && !isNaN(aDate.getTime());
        const bValid = bDate && !isNaN(bDate.getTime());

        if (!aValid && !bValid) return 0;
        if (!aValid) return 1;
        if (!bValid) return -1;

        return aDate.getDate() - bDate.getDate();
      });
    });
  });

  return grouped;
}

// ─── Add Location Panel ───────────────────────────────────────────────────────

const AddLocationPanel = ({ catalogId, eventId, eventLat, eventLong, userLat, userLong, onSaved, onCancel }) => {
  // Default to user settings first, then fall back to event coordinates
  const getDefaultLat = () => {
    if (userLat != null) return String(userLat);
    if (eventLat != null) return String(eventLat);
    return '';
  };
  
  const getDefaultLong = () => {
    if (userLong != null) return String(userLong);
    if (eventLong != null) return String(eventLong);
    return '';
  };
  
  const [form, setForm] = useState({
    name: '',
    lat: getDefaultLat(),
    long: getDefaultLong(),
    description: '',
    source: '',
  });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);

  const handleChange = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));

  const handleSave = async (e) => {
    e.stopPropagation();
    if (!form.name.trim()) { setSaveError('Name is required'); return; }
    setSaving(true);
    setSaveError(null);
    try {
      const payload = {
        name: form.name.trim(),
        lat: form.lat !== '' ? parseFloat(form.lat) : null,
        long: form.long !== '' ? parseFloat(form.long) : null,
        description: form.description.trim() || null,
        source: form.source.trim() || null,
      };
      await eventAPI.addLocation(catalogId, eventId, payload);
      onSaved();
    } catch (err) {
      setSaveError(err.response?.data?.error || 'Failed to save location');
      setSaving(false);
    }
  };

  const labelStyle = {
    fontSize: '0.72rem', fontWeight: 600, color: '#555',
    textTransform: 'uppercase', letterSpacing: '0.4px',
    display: 'block', marginBottom: 3,
  };

  return (
    <CCard
      style={{
        border: '1.5px solid #28a745',
        borderRadius: '0 0 10px 10px',     // square at the top, round at the bottom
        boxShadow: '0 4px 16px rgba(40,167,69,0.15)',
        height: '100%',
      }}
    >
      <CCardHeader
        style={{
          background: 'linear-gradient(135deg, #f0fff4 0%, #e8f5e9 100%)',
          borderBottom: '1px solid #c3e6cb',
          borderRadius: 0,
          padding: '0.75rem 1rem',
        }}
      >
        <div className="d-flex justify-content-between align-items-center">
          <span style={{ fontWeight: 600, fontSize: '0.9rem', color: '#155724' }}>
            <CIcon icon={cilLocationPin} style={{ width: 14, height: 14, marginRight: 6 }} />
            Add Location
          </span>
          <button
            onClick={onCancel}
            style={{
              background: 'none', border: 'none', cursor: 'pointer',
              padding: '2px 4px', borderRadius: 4, color: '#888',
              display: 'flex', alignItems: 'center',
            }}
            title="Cancel"
          >
            <CIcon icon={cilX} style={{ width: 14, height: 14 }} />
          </button>
        </div>
      </CCardHeader>
      <CCardBody style={{ padding: '1rem' }}>
        {saveError && (
          <div style={{
            background: '#fdecea', border: '1px solid #f5c6cb', borderRadius: 6,
            padding: '0.4rem 0.6rem', fontSize: '0.78rem', color: '#721c24', marginBottom: '0.75rem',
          }}>
            {saveError}
          </div>
        )}

        <CForm>
          {/* Name */}
          <div className="mb-2">
            <label style={labelStyle}>Name *</label>
            <CFormInput
              size="sm"
              value={form.name}
              onChange={handleChange('name')}
              placeholder="Location name"
              onClick={(e) => e.stopPropagation()}
            />
          </div>

          {/* Lat / Long */}
          <CRow className="mb-2 g-2">
            <CCol xs={6}>
              <label style={labelStyle}>Latitude</label>
              <CFormInput
                size="sm"
                type="text"
                value={form.lat}
                onChange={handleChange('lat')}
                placeholder="e.g. 40.7128"
                onClick={(e) => e.stopPropagation()}
              />
            </CCol>
            <CCol xs={6}>
              <label style={labelStyle}>Longitude</label>
              <CFormInput
                size="sm"
                type="text"
                value={form.long}
                onChange={handleChange('long')}
                placeholder="e.g. -74.0060"
                onClick={(e) => e.stopPropagation()}
              />
            </CCol>
          </CRow>

          {/* Description */}
          <div className="mb-2">
            <label style={labelStyle}>Description</label>
            <CFormTextarea
              size="sm"
              rows={2}
              value={form.description}
              onChange={handleChange('description')}
              placeholder="Optional description"
              onClick={(e) => e.stopPropagation()}
              style={{ fontSize: '0.82rem' }}
            />
          </div>

          {/* Source */}
          <div className="mb-3">
            <label style={labelStyle}>Source</label>
            <CFormInput
              size="sm"
              value={form.source}
              onChange={handleChange('source')}
              placeholder="e.g. GPS, Manual, OpenStreetMap"
              onClick={(e) => e.stopPropagation()}
            />
          </div>

          {/* Action buttons */}
          <div className="d-flex gap-2">
            <CButton
              size="sm"
              color="success"
              onClick={handleSave}
              disabled={saving}
              style={{ flex: 1 }}
            >
              {saving
                ? <><CSpinner size="sm" className="me-1" />Saving…</>
                : <><CIcon icon={cilCheck} style={{ width: 13, height: 13 }} className="me-1" />Save Location</>
              }
            </CButton>
            <CButton
              size="sm"
              color="secondary"
              variant="outline"
              onClick={onCancel}
              disabled={saving}
            >
              Cancel
            </CButton>
          </div>
        </CForm>
      </CCardBody>
    </CCard>
  );
};

// ─── Event Card (with flip-to-edit) ──────────────────────────────────────────

// Two thirds of the 320px the card used to be. Still the same stored asset -
// GALLERY_THUMB_SIZE, the one every other gallery requests - now painted into a
// smaller box. Downscaling a 320 file to 213 costs nothing and stays sharp; it
// is upscaling that would have needed a new thumbnail size.
export const CARD_WIDTH = Math.round(GALLERY_THUMB_SIZE * 2 / 3);   // 213
const COVER_SIZE = CARD_WIDTH;

// Header plus the one-line detail strip, at the reduced type sizes below. Used
// for the card's fixed front height and, on the page, to work out how many rows
// of cards fit the viewport.
const CARD_CHROME = 80;
export const CARD_HEIGHT = COVER_SIZE + CARD_CHROME;                // 293

const EventCard = ({
  event, isSelected, onClick, isFlipped, onFlip, onCancelFlip,
  catalogId, onSaved, showAddLocation, onToggleAddLocation, onLocationSaved,
  locationReloadTrigger,
}) => {
  const [form, setForm] = useState({
    name: '',
    start_time: '',
    end_time: '',
    description: '',
    latitude: '',
    longitude: '',
  });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState(null);
  const [locations, setLocations] = useState([]);
  const [loadingLocations, setLoadingLocations] = useState(false);
  const [removingLocationId, setRemovingLocationId] = useState(null);
  const [people, setPeople] = useState([]);
  const [loadingPeople, setLoadingPeople] = useState(false);

  const loadLocations = useCallback(async () => {
    if (!catalogId || !event?.id) return;
    setLoadingLocations(true);
    try {
      const res = await eventAPI.getLocations(catalogId, event.id);
      setLocations(res.data);
    } catch {
      // ignore
    } finally {
      setLoadingLocations(false);
    }
  }, [catalogId, event?.id]);

  const loadPeople = useCallback(async () => {
    if (!catalogId || !event?.id) return;
    setLoadingPeople(true);
    try {
      const res = await eventAPI.getPeople(catalogId, event.id);
      setPeople(res.data);
    } catch {
      // ignore
    } finally {
      setLoadingPeople(false);
    }
  }, [catalogId, event?.id]);

  // Load people and locations when component mounts
  useEffect(() => {
    loadPeople();
    loadLocations();
  }, [loadPeople, loadLocations]);

  // Populate form when card flips to edit side
  useEffect(() => {
    if (isFlipped && event) {
      setForm({
        name: event.name || '',
        start_time: toInputDatetime(event.start_time),
        end_time: toInputDatetime(event.end_time),
        description: event.description || '',
        latitude: event.latitude != null ? String(event.latitude) : '',
        longitude: event.longitude != null ? String(event.longitude) : '',
      });
      setSaveError(null);
      loadLocations();
    }
  }, [isFlipped, event]);

  // Reload locations when a new location is saved from the panel
  useEffect(() => {
    if (isFlipped && locationReloadTrigger) {
      loadLocations();
    }
  }, [locationReloadTrigger]);

  const handleChange = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));

  const handleSave = async (e) => {
    e.stopPropagation();
    if (!form.name.trim()) { setSaveError('Name is required'); return; }
    if (!form.start_time) { setSaveError('Start time is required'); return; }
    setSaving(true);
    setSaveError(null);
    try {
      const payload = {
        name: form.name.trim(),
        start_time: form.start_time,
        end_time: form.end_time || form.start_time,
        description: form.description.trim() || null,
        latitude: form.latitude !== '' ? parseFloat(form.latitude) : null,
        longitude: form.longitude !== '' ? parseFloat(form.longitude) : null,
      };
      await eventAPI.update(catalogId, event.id, payload);
      onSaved();
      onCancelFlip();
    } catch (err) {
      setSaveError(err.response?.data?.error || 'Failed to save event');
    } finally {
      setSaving(false);
    }
  };

  const handleCancel = (e) => {
    e.stopPropagation();
    onCancelFlip();
  };

  const handleFlipClick = (e) => {
    e.stopPropagation();
    onFlip();
  };

  const handleRemoveLocation = async (e, locationId) => {
    e.stopPropagation();
    setRemovingLocationId(locationId);
    try {
      await eventAPI.removeLocation(catalogId, event.id, locationId);
      setLocations((prev) => prev.filter((l) => l.id !== locationId));
    } catch {
      // ignore
    } finally {
      setRemovingLocationId(null);
    }
  };

  const handleLocationSaved = () => {
    loadLocations();
    if (onLocationSaved) onLocationSaved();
  };

  // The cover comes resolved from GET /events: the backend picks one at random
  // the first time and stores it, so it is the same photo on every reload
  // (event-cards-ui-spec.md section 3.2). cover_file_id null means the event
  // has nothing renderable, and the placeholder shows instead.
  const coverUrl = event?.cover_file_id && catalogId
    ? galleryThumbUrl(catalogId, event.cover_file_id, GALLERY_THUMB_SIZE)
    : null;
  const locationLabel = locations.map((loc) => loc.name).join(', ');

  const labelStyle = {
    fontSize: '0.72rem', fontWeight: 600, color: '#555',
    textTransform: 'uppercase', letterSpacing: '0.4px',
    display: 'block', marginBottom: 3,
  };

  return (
    <div
      style={{
        perspective: '1000px',
        height: isFlipped ? 'auto' : '100%',
        minHeight: isFlipped ? CARD_WIDTH : undefined,
      }}
    >
      <div
        style={{
          position: 'relative',
          transformStyle: 'preserve-3d',
          transition: 'transform 0.45s cubic-bezier(0.4, 0.2, 0.2, 1)',
          transform: isFlipped ? 'rotateY(180deg)' : 'rotateY(0deg)',
          height: isFlipped ? undefined : '100%',
          minHeight: isFlipped ? CARD_WIDTH : 80,
        }}
      >
        {/* ── FRONT ── */}
        <div
          onClick={onClick}
          style={{
            backfaceVisibility: 'hidden',
            WebkitBackfaceVisibility: 'hidden',
            position: isFlipped ? 'absolute' : 'relative',
            top: 0,
            left: 0,
            right: 0,
            height: isFlipped ? undefined : '100%',
            background: isSelected ? '#e3eaf8' : '#fff',
            border: isSelected ? '1.5px solid #1a73e8' : '1px solid #e0e0e0',
            borderRadius: '0 0 10px 10px',     // square at the top, round at the bottom
            // No padding here any more: the cover runs edge to edge, so the
            // header and the detail strip carry their own.
            padding: 0,
            overflow: 'hidden',
            cursor: 'pointer',
            transition: 'all 0.15s ease',
            boxShadow: isSelected ? '0 2px 8px rgba(26,115,232,0.15)' : '0 1px 3px rgba(0,0,0,0.06)',
            boxSizing: 'border-box',
            display: isFlipped ? undefined : 'flex',
            flexDirection: 'column',
            // Fixed front height so a row of cards is level: header + cover +
            // the one-line strip.
            minHeight: isFlipped ? undefined : CARD_HEIGHT,
          }}
        >
          <div style={{ padding: '0.6rem 0.7rem 0.5rem' }}>
          {(() => {
            const _d = event.start_time ? new Date(event.start_time) : null;
            const _day = _d && !isNaN(_d.getTime()) ? _d.getDate() : null;
            const _suffix = _d && !isNaN(_d.getTime())
              ? _d.toLocaleString('en-US', { weekday: 'short' }).toUpperCase()
              : null;
            return (
          <div className="d-flex justify-content-between align-items-start">
            {/* Day-of-month badge */}
            {_day !== null && (
              <div style={{
                display: 'flex',
                flexDirection: 'column',
                alignItems: 'center',
                justifyContent: 'center',
                marginRight: 10,
                flexShrink: 0,
                lineHeight: 1,
              }}>
                <span style={{
                  fontSize: '1.5rem',
                  fontWeight: 800,
                  color: isSelected ? '#1a73e8' : '#212121',
                  lineHeight: 1,
                }}>
                  {String(_day).padStart(2, '0')}
                </span>
                <span style={{
                  fontSize: '0.65rem',
                  fontWeight: 700,
                  color: isSelected ? '#1a73e8' : '#888',
                  textTransform: 'uppercase',
                  letterSpacing: '0.5px',
                  marginTop: 1,
                }}>
                  {_suffix}
                </span>
              </div>
            )}
            <div style={{ flex: 1, minWidth: 0 }}>
              <div
                style={{
                  fontWeight: 600,
                  fontSize: '0.82rem',
                  color: isSelected ? '#1a73e8' : '#212121',
                  whiteSpace: 'nowrap',
                  overflow: 'hidden',
                  textOverflow: 'ellipsis',
                }}
              >
                {event.name}
              </div>
              {event.type === 'Manual' && (
                <CBadge
                  color="success"
                  style={{ fontSize: '0.68rem', marginTop: 4, display: 'inline-block' }}
                >
                  ✓ Verified
                </CBadge>
              )}
              <div style={{ fontSize: '0.72rem', color: '#666', marginTop: 2 }}>
                <CIcon icon={cilCalendar} style={{ width: 11, height: 11, marginRight: 3 }} />
                {formatDateTime(event.start_time)}
              </div>
            </div>
            <div className="d-flex align-items-center gap-1">
              {event.file_count > 0 && (
                <CBadge
                  color={isSelected ? 'primary' : 'secondary'}
                  style={{ fontSize: '0.72rem', flexShrink: 0 }}
                >
                  {event.file_count}
                </CBadge>
              )}
              <button
                onClick={handleFlipClick}
                title="Edit event"
                style={{
                  background: 'none',
                  border: 'none',
                  cursor: 'pointer',
                  padding: '2px 4px',
                  borderRadius: 4,
                  color: '#888',
                  display: 'flex',
                  alignItems: 'center',
                  flexShrink: 0,
                }}
                onMouseEnter={(e) => { e.currentTarget.style.color = '#1a73e8'; e.currentTarget.style.background = '#e8f0fe'; }}
                onMouseLeave={(e) => { e.currentTarget.style.color = '#888'; e.currentTarget.style.background = 'none'; }}
              >
                <CIcon icon={cilPencil} style={{ width: 13, height: 13 }} />
              </button>
            </div>
          </div>
            );
          })()}
          {event.description && (
            <div
              style={{
                fontSize: '0.72rem', color: '#888', marginTop: 4,
                whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis',
              }}
            >
              {event.description}
            </div>
          )}
          </div>

          {/* ── Cover ──
              The 320 thumbnail the galleries already use, at its native size:
              no new image size is produced by this card, and a video resolves
              to its poster frame through the same route. Height is fixed and
              width follows the card, so the full-width layout (both the edit
              form and add-location open) gets a banner rather than a giant
              square. */}
          <div
            style={{
              width: '100%',
              height: COVER_SIZE,
              flexShrink: 0,
              background: '#f1f3f4',
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'center',
              overflow: 'hidden',
            }}
          >
            {coverUrl ? (
              <img
                src={coverUrl}
                alt=""
                loading="lazy"
                // A square source in a square box cannot distort; objectFit is
                // the safety net for the wide layout and for any non-square
                // fallback the thumbnail route may serve.
                style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                onError={(e) => { e.currentTarget.style.display = 'none'; }}
              />
            ) : (
              // A real case, not a defensive branch: an event can hold nothing
              // renderable at all (the importer files stray .DS_Store entries),
              // and a photo has no thumbnail until that workflow has run.
              <CIcon icon={cilImage} style={{ width: 40, height: 40, color: '#c4c8cc' }} />
            )}
          </div>

          {/* ── Detail strip ──
              One line. The back face already carries the full detail; this is
              only enough to tell two events apart at a glance. */}
          <div
            style={{
              // Pinned to the bottom: cards in a row stretch to the tallest, and
              // the strip should sit on the card's edge rather than float above
              // whatever slack that stretch created.
              marginTop: 'auto',
              padding: '0.4rem 0.6rem',
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontSize: '0.7rem',
              color: '#666',
              borderTop: '1px solid #eceff1',
              whiteSpace: 'nowrap',
              overflow: 'hidden',
              minWidth: 0,
            }}
          >
            <span
              title={locationLabel || undefined}
              style={{
                display: 'flex', alignItems: 'center', gap: 4,
                flex: 1, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis',
              }}
            >
              <CIcon icon={cilLocationPin} style={{ width: 12, height: 12, flexShrink: 0 }} />
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>
                {locationLabel || '\u2014'}
              </span>
            </span>
            <span
              title={people.map((pp) => pp.name).join(', ') || undefined}
              style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}
            >
              <CIcon icon={cilPeople} style={{ width: 12, height: 12 }} />
              {people.length}
            </span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
              <CIcon icon={cilImage} style={{ width: 12, height: 12 }} />
              {event.file_count || 0}
            </span>
          </div>
        </div>

        {/* ── BACK (Edit Form) ── */}
        <div
          style={{
            backfaceVisibility: 'hidden',
            WebkitBackfaceVisibility: 'hidden',
            transform: 'rotateY(180deg)',
            position: isFlipped ? 'relative' : 'absolute',
            top: 0,
            left: 0,
            right: 0,
            background: '#fff',
            border: '1.5px solid #1a73e8',
            borderRadius: '0 0 10px 10px',     // square at the top, round at the bottom
            padding: '0.875rem 1rem',
            boxShadow: '0 4px 16px rgba(26,115,232,0.15)',
          }}
        >
          {/* Back header */}
          <div className="d-flex justify-content-between align-items-center mb-3">
            <span style={{ fontWeight: 600, fontSize: '0.85rem', color: '#1a73e8' }}>
              <CIcon icon={cilPencil} style={{ width: 13, height: 13, marginRight: 5 }} />
              Edit Event
            </span>
            <button
              onClick={handleCancel}
              style={{
                background: 'none', border: 'none', cursor: 'pointer',
                padding: '2px 4px', borderRadius: 4, color: '#888',
                display: 'flex', alignItems: 'center',
              }}
              title="Cancel"
            >
              <CIcon icon={cilX} style={{ width: 14, height: 14 }} />
            </button>
          </div>

          {saveError && (
            <div style={{
              background: '#fdecea', border: '1px solid #f5c6cb', borderRadius: 6,
              padding: '0.4rem 0.6rem', fontSize: '0.78rem', color: '#721c24', marginBottom: '0.75rem',
            }}>
              {saveError}
            </div>
          )}

          <CForm>
            {/* Name */}
            <div className="mb-2">
              <label style={labelStyle}>Name *</label>
              <CFormInput
                size="sm"
                value={form.name}
                onChange={handleChange('name')}
                placeholder="Event name"
                onClick={(e) => e.stopPropagation()}
              />
            </div>

            {/* Start / End */}
            <CRow className="mb-2 g-2">
              <CCol xs={6}>
                <label style={labelStyle}>Start *</label>
                <CFormInput
                  size="sm"
                  type="datetime-local"
                  value={form.start_time}
                  onChange={handleChange('start_time')}
                  onClick={(e) => e.stopPropagation()}
                />
              </CCol>
              <CCol xs={6}>
                <label style={labelStyle}>End</label>
                <CFormInput
                  size="sm"
                  type="datetime-local"
                  value={form.end_time}
                  onChange={handleChange('end_time')}
                  onClick={(e) => e.stopPropagation()}
                />
              </CCol>
            </CRow>

            {/* Description */}
            <div className="mb-2">
              <label style={labelStyle}>Description</label>
              <CFormTextarea
                size="sm"
                rows={2}
                value={form.description}
                onChange={handleChange('description')}
                placeholder="Optional description"
                onClick={(e) => e.stopPropagation()}
                style={{ fontSize: '0.82rem' }}
              />
            </div>

            {/* Lat / Lng */}
            <CRow className="mb-3 g-2">
              <CCol xs={6}>
                <label style={labelStyle}>Latitude</label>
                <CFormInput
                  size="sm"
                  type="number"
                  step="any"
                  value={form.latitude}
                  onChange={handleChange('latitude')}
                  placeholder="e.g. 40.7128"
                  onClick={(e) => e.stopPropagation()}
                />
              </CCol>
              <CCol xs={6}>
                <label style={labelStyle}>Longitude</label>
                <CFormInput
                  size="sm"
                  type="number"
                  step="any"
                  value={form.longitude}
                  onChange={handleChange('longitude')}
                  placeholder="e.g. -74.0060"
                  onClick={(e) => e.stopPropagation()}
                />
              </CCol>
            </CRow>

            {/* ── Locations section ── */}
            <div className="mb-3">
              <div className="d-flex justify-content-between align-items-center mb-2">
                <label style={{ ...labelStyle, marginBottom: 0 }}>
                  <CIcon icon={cilLocationPin} style={{ width: 11, height: 11, marginRight: 4 }} />
                  Locations
                </label>
                {loadingLocations && <CSpinner size="sm" style={{ width: 12, height: 12 }} />}
              </div>

              {/* Location list */}
              {locations.length === 0 && !loadingLocations && (
                <div style={{ fontSize: '0.75rem', color: '#aaa', fontStyle: 'italic', marginBottom: 6 }}>
                  No locations associated with this event.
                </div>
              )}
              {locations.map((loc) => (
                <div
                  key={loc.id}
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    justifyContent: 'space-between',
                    background: '#f8f9fa',
                    border: '1px solid #e9ecef',
                    borderRadius: 6,
                    padding: '0.4rem 0.6rem',
                    marginBottom: 4,
                    fontSize: '0.78rem',
                  }}
                  onClick={(e) => e.stopPropagation()}
                >
                  <div style={{ flex: 1, minWidth: 0 }}>
                    <div style={{ fontWeight: 600, color: '#333', marginBottom: 1 }}>{loc.name}</div>
                    {(loc.lat != null || loc.long != null) && (
                      <div style={{ color: '#888', fontSize: '0.72rem' }}>
                        {loc.lat != null ? loc.lat.toFixed(5) : '—'}, {loc.long != null ? loc.long.toFixed(5) : '—'}
                      </div>
                    )}
                    {loc.description && (
                      <div style={{ color: '#999', fontSize: '0.72rem', marginTop: 1 }}>{loc.description}</div>
                    )}
                    {loc.source && (
                      <div style={{ color: '#bbb', fontSize: '0.68rem', marginTop: 1 }}>Source: {loc.source}</div>
                    )}
                  </div>
                  <button
                    onClick={(e) => handleRemoveLocation(e, loc.id)}
                    disabled={removingLocationId === loc.id}
                    title="Remove location from event"
                    style={{
                      background: 'none', border: 'none', cursor: 'pointer',
                      padding: '2px 4px', borderRadius: 4, color: '#dc3545',
                      display: 'flex', alignItems: 'center', flexShrink: 0, marginLeft: 4,
                    }}
                  >
                    {removingLocationId === loc.id
                      ? <CSpinner size="sm" style={{ width: 11, height: 11 }} />
                      : <CIcon icon={cilTrash} style={{ width: 12, height: 12 }} />
                    }
                  </button>
                </div>
              ))}
            </div>

            {/* Action buttons */}
            <div className="d-flex gap-2 mb-2">
              <CButton
                size="sm"
                color="primary"
                onClick={handleSave}
                disabled={saving}
                style={{ flex: 1 }}
              >
                {saving
                  ? <><CSpinner size="sm" className="me-1" />Saving…</>
                  : <><CIcon icon={cilCheck} style={{ width: 13, height: 13 }} className="me-1" />Save</>
                }
              </CButton>
              <CButton
                size="sm"
                color="secondary"
                variant="outline"
                onClick={handleCancel}
                disabled={saving}
              >
                Cancel
              </CButton>
            </div>

            {/* Add Location button */}
            <CButton
              size="sm"
              color="success"
              variant="outline"
              onClick={(e) => { e.stopPropagation(); onToggleAddLocation(); }}
              style={{ width: '100%' }}
            >
              <CIcon icon={cilPlus} style={{ width: 12, height: 12, marginRight: 5 }} />
              Add Location
            </CButton>
          </CForm>
        </div>
      </div>
    </div>
  );
};

// ─── Main Events View ─────────────────────────────────────────────────────────

const filesText = (n) => (n === 1 ? '1 photo or video' : `${n.toLocaleString()} photos and videos`);

const Events = () => {
  const navigate = useNavigate();
  const { activeCatalog } = useCatalog();
  // ?date=YYYY-MM-DD: one day's events, from Calendar view or the Dashboard's
  // mini calendar (calendar-view-spec.md section 6). Without it, the list.
  const [searchParams] = useSearchParams();
  const dayParam = /^\d{4}-\d{2}-\d{2}$/.test(searchParams.get('date') || '')
    ? searchParams.get('date') : null;
  const [day, setDay] = useState(null);           // {date, total, not_in_an_event, events}
  const [calendarDays, setCalendarDays] = useState(null);
  // "All events" from a day returns to the list at that day's year and month.
  const returnTo = useRef(null);
  const [events, setEvents] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [flippedEventId, setFlippedEventId] = useState(null);
  // Track which event has the "Add Location" panel open
  const [addLocationEventId, setAddLocationEventId] = useState(null);
  // Increment to trigger location list reload in EventCard
  const [locationReloadTrigger, setLocationReloadTrigger] = useState(0);
  // Year/month navigation state. Both null means "everything", which is what
  // the page opens on - the pills narrow the list rather than defining it.
  const [selectedYear, setSelectedYear] = useState(null);
  const [selectedMonth, setSelectedMonth] = useState(null);
  // User settings state
  const [userSettings, setUserSettings] = useState(null);

  // Paging. The grid shows whole rows only, as many as fit the window, so the
  // page never scrolls vertically - the same arrangement the photo galleries
  // use. pageSize is derived from the measured grid, not configured.
  const [page, setPage] = useState(0);
  const [pageSize, setPageSize] = useState(12);
  // The grid itself, so a real card can be measured instead of assumed.
  const gridRef = useRef(null);
  // Whether a card is flipped open, readable from inside compute() without
  // making the layout effect depend on it. See the guard there.
  const cardOpenRef = useRef(false);
  const topSectionRef = useRef(null);
  const gridBodyRef = useRef(null);

  // Load user settings on mount
  useEffect(() => {
    userSettingsAPI.get()
      .then((res) => setUserSettings(res.data))
      .catch(() => setUserSettings(null));
  }, []);

  // One day's events. The calendar counts (cached, milliseconds) give the
  // previous and next days that have anything.
  const loadDay = useCallback(() => {
    if (!activeCatalog || !dayParam) return;
    setLoading(true);
    setError(null);
    setFlippedEventId(null);
    setAddLocationEventId(null);
    eventAPI.getDay(activeCatalog.id, dayParam)
      .then((res) => setDay(res.data))
      .catch(() => setError('Failed to load that day'))
      .finally(() => setLoading(false));
  }, [activeCatalog, dayParam]);

  useEffect(() => { loadDay(); }, [loadDay]);

  useEffect(() => {
    if (!activeCatalog || !dayParam) return;
    calendarAPI.get(activeCatalog.id)
      .then((res) => setCalendarDays(res.data.days || []))
      .catch(() => setCalendarDays([]));
  }, [activeCatalog, dayParam]);

  // Load events when active catalog changes - not in the day view, which has
  // its own few; the full list is 15,000 events on a large catalog.
  useEffect(() => {
    if (dayParam) return;
    if (!activeCatalog) { setEvents([]); return; }
    setLoading(true);
    setError(null);
    setFlippedEventId(null);
    setAddLocationEventId(null);
    const back = returnTo.current;
    returnTo.current = null;
    setSelectedYear(back ? back.year : null);
    setSelectedMonth(back ? back.month : null);
    setPage(0);
    eventAPI.getAll(activeCatalog.id)
      .then((res) => setEvents(res.data))
      .catch(() => setError('Failed to load events'))
      .finally(() => setLoading(false));
  }, [activeCatalog, dayParam]);

  const handleEventClick = (ev) => {
    // Don't navigate if this card is in edit mode
    if (flippedEventId === ev.id) return;
    navigate(`/events/${activeCatalog.id}/${ev.id}`);
  };

  const handleFlip = (ev) => {
    setFlippedEventId(ev.id);
    // Close add-location panel if switching to a different event
    if (addLocationEventId !== ev.id) setAddLocationEventId(null);
  };

  const handleCancelFlip = () => {
    setFlippedEventId(null);
    setAddLocationEventId(null);
  };

  const handleSaved = useCallback(() => {
    if (!activeCatalog) return;
    if (dayParam) { loadDay(); return; }
    eventAPI.getAll(activeCatalog.id).then((res) => {
      setEvents(res.data);
    });
  }, [activeCatalog, dayParam, loadDay]);

  const handleToggleAddLocation = (eventId) => {
    setAddLocationEventId((prev) => (prev === eventId ? null : eventId));
  };

  const handleLocationSaved = () => {
    setAddLocationEventId(null);
    setLocationReloadTrigger((n) => n + 1);
  };

  // A year pill narrows to that year and shows the whole year - it does not
  // also pick a month. Clicking the active one clears it, so there is always a
  // way back to everything without hunting for a reset control.
  const handleYearChange = (year) => {
    setSelectedYear((prev) => (prev === year ? null : year));
    setSelectedMonth(null);
    setPage(0);
  };

  const handleMonthChange = (month) => {
    setSelectedMonth((prev) => (prev === month ? null : month));
    setPage(0);
  };

  const clearFilters = () => {
    setSelectedYear(null);
    setSelectedMonth(null);
    setPage(0);
  };

  const grouped = groupEventsByYearMonth(events);
  const allYears = Object.keys(grouped).sort((a, b) => {
    if (a === 'Unknown') return 1;
    if (b === 'Unknown') return -1;
    return Number(b) - Number(a);
  });

  // Derive available years and months from grouped data
  const availableYearsMonths = {};
  allYears.forEach((year) => {
    availableYearsMonths[year] = Object.keys(grouped[year]).map(Number).sort((a, b) => a - b);
  });

  // One flat list, newest first, rather than the year/month sections this page
  // used to render. A section header is an unpredictable amount of vertical
  // space, and the grid below has to know exactly how many rows it can fit.
  // The pills do the grouping's job now, and they do it without hiding anything
  // by default.
  const visibleEvents = useMemo(() => {
    const inRange = events.filter((ev) => {
      if (selectedYear == null) return true;
      const dt = ev.start_time ? new Date(ev.start_time) : null;
      const valid = dt && !isNaN(dt.getTime());
      const year = valid ? dt.getFullYear() : 'Unknown';
      if (String(year) !== String(selectedYear)) return false;
      if (selectedMonth == null) return true;
      return (valid ? dt.getMonth() : -1) === selectedMonth;
    });
    return inRange.sort((a, b) => {
      const at = a.start_time ? new Date(a.start_time).getTime() : NaN;
      const bt = b.start_time ? new Date(b.start_time).getTime() : NaN;
      if (isNaN(at) && isNaN(bt)) return 0;
      if (isNaN(at)) return 1;      // undated events sink to the end
      if (isNaN(bt)) return -1;
      return bt - at;               // newest first
    });
  }, [events, selectedYear, selectedMonth]);

  // How many whole rows of cards fit between the filters and the bottom of the
  // window. Measured rather than assumed: the filter card grows a second row of
  // pills when a year is picked, and the count has to follow it.
  //
  // This used to ALSO pin the body to `rows * (CARD_HEIGHT + GAP) - GAP` with
  // overflow:hidden, to leave no dead space under the last row. That is what
  // cut the bottom row off. CARD_HEIGHT is an assumption - COVER_SIZE plus a
  // CARD_CHROME constant of 80 - and the card only takes it as a minHeight, so
  // any card that grows past it (a title that wraps to two lines, a location
  // line) makes its whole row taller than the pinned container, and the hidden
  // overflow slices it.
  //
  // The height is no longer pinned at all. pageSize already puts exactly
  // cols x rows cards in the body, so a body that sizes to its content has no
  // dead space BY CONSTRUCTION and cannot clip. The measured card height below
  // then keeps the row count honest, so the last row still lands on the fold
  // rather than under it.
  useEffect(() => {
    const TOP_NAV_H = 60;      // .top-menu min-height
    const CONTAINER_PT = 24;   // CContainer py-4
    const CONTAINER_PB = 24;
    const CARD_HEADER_H = 57;  // the grid card's own header
    const CARD_BODY_P = 24;    // CCardBody padding, top + bottom
    const GAP = 8;             // the grid's gap, below

    const compute = () => {
      // Not while a card is open. An open card carries an edit form and is far
      // taller than a browsing one, so measuring it would collapse the page to
      // a single row - and repaginating under somebody who is mid-edit would
      // be its own bug. The page size that was right before they opened it is
      // still right.
      if (cardOpenRef.current) return;

      const topH = topSectionRef.current ? topSectionRef.current.offsetHeight : 0;
      const avail = window.innerHeight - TOP_NAV_H - CONTAINER_PT - topH
                    - CARD_HEADER_H - CARD_BODY_P - CONTAINER_PB;
      const bodyEl = gridBodyRef.current;
      // 280 sidebar + 60 padding, only until the element has been laid out once.
      const bodyW = bodyEl ? bodyEl.clientWidth : window.innerWidth - 340;
      const cols = Math.max(1, Math.floor((bodyW + GAP) / (CARD_WIDTH + GAP)));

      // What a card on this page ACTUALLY is, not what the constant says. The
      // first one in the grid is representative: they stretch to the tallest
      // in their row, so measuring one measures the row. CARD_HEIGHT is the
      // fallback for the first pass, before anything has been laid out.
      const firstCard = gridRef.current && gridRef.current.firstElementChild;
      const cardH = (firstCard && firstCard.offsetHeight) || CARD_HEIGHT;

      const rows = Math.max(1, Math.floor((avail + GAP) / (cardH + GAP)));
      setPageSize(cols * rows);
    };

    compute();
    window.addEventListener('resize', compute);
    const ro = new ResizeObserver(compute);
    if (topSectionRef.current) ro.observe(topSectionRef.current);
    if (gridBodyRef.current) ro.observe(gridBodyRef.current);
    return () => {
      window.removeEventListener('resize', compute);
      ro.disconnect();
    };
    // Re-attached when the content that carries these refs appears. On the
    // first mount there may be no catalog and no grid to observe, and an
    // observer bound to nothing would leave the page on its default page size
    // forever.
  }, [activeCatalog, loading, events.length]);

  const totalPages = Math.max(1, Math.ceil(visibleEvents.length / pageSize));
  // Clamped rather than reset: a window resize that fits more cards per page can
  // leave `page` past the end, and snapping the user back to page 1 for resizing
  // their window would be its own bug.
  const safePage = Math.min(page, totalPages - 1);
  const pageEvents = visibleEvents.slice(safePage * pageSize, (safePage + 1) * pageSize);

  // A flipped card is far taller than a browsing one. Nothing is clipped by
  // that any more - the grid body is no longer pinned to a height - but it
  // must not be MEASURED either, or one open card would decide the page size
  // for all of them. Kept in a ref so the layout effect above need not
  // re-attach its observers every time a card opens.
  cardOpenRef.current = flippedEventId != null || addLocationEventId != null;

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

  // ── One day (calendar-view-spec.md 6.2) ──────────────────────────────────
  if (dayParam) {
    const { earlier, later } = neighbours(calendarDays, dayParam);
    const toDay = (iso) => navigate(`/events?date=${iso}`);
    const backToList = () => {
      const [y, m] = dayParam.split('-').map(Number);
      returnTo.current = { year: y, month: m - 1 };
      navigate('/events');
    };
    const renderCard = (ev) => (
      <div key={ev.id} style={{ width: CARD_WIDTH, flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
        <EventCard
          event={ev}
          isSelected={false}
          onClick={() => handleEventClick(ev)}
          isFlipped={flippedEventId === ev.id}
          onFlip={() => handleFlip(ev)}
          onCancelFlip={handleCancelFlip}
          catalogId={activeCatalog.id}
          onSaved={handleSaved}
          showAddLocation={addLocationEventId === ev.id}
          onToggleAddLocation={() => handleToggleAddLocation(ev.id)}
          onLocationSaved={handleLocationSaved}
          locationReloadTrigger={locationReloadTrigger}
        />
      </div>
    );
    return (
      <CContainer fluid className="py-4 px-4">
        <div className="d-flex justify-content-between align-items-start mb-3 flex-wrap gap-2">
          <div>
            <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>{longDate(dayParam)}</h4>
            <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
              {day && day.date === dayParam
                ? `${filesText(day.total)} · `
                  + `${day.events.length} event${day.events.length === 1 ? '' : 's'}`
                : 'Events of one day'}
            </div>
          </div>
          <div className="d-flex gap-2 align-items-center">
            <CButton color="secondary" variant="outline" size="sm" disabled={!earlier}
                     onClick={() => toDay(earlier)} title={earlier ? longDate(earlier) : 'No earlier day'}>
              ← Previous day
            </CButton>
            <CButton color="secondary" variant="outline" size="sm" disabled={!later}
                     onClick={() => toDay(later)} title={later ? longDate(later) : 'No later day'}>
              Next day →
            </CButton>
            <CButton color="primary" variant="outline" size="sm" onClick={backToList}>
              All events
            </CButton>
          </div>
        </div>

        {error && <CAlert color="danger" className="mb-3">{error}</CAlert>}
        {loading && (
          <div className="text-center py-5"><CSpinner color="primary" /></div>
        )}

        {!loading && day && day.date === dayParam && (
          <>
            {day.events.length === 0 ? (
              <div className="text-center py-4 text-muted">
                <CIcon icon={cilCalendar} style={{ width: 40, height: 40, opacity: 0.25 }} />
                <div className="mt-2">No event holds this day&apos;s photos and videos.</div>
              </div>
            ) : (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'stretch' }}>
                {day.events.map(renderCard)}
              </div>
            )}
            {day.not_in_an_event > 0 && (
              <CAlert color="light" className="mt-3 mb-0 d-flex flex-wrap align-items-center gap-2"
                      style={{ fontSize: '0.88rem' }}>
                <span>
                  {filesText(day.not_in_an_event)} this day
                  {day.not_in_an_event === 1 ? ' is' : ' are'} in no event.
                </span>
                <CButton color="link" size="sm" className="p-0"
                         onClick={() => navigate(`/photos?date=${dayParam}`)}>Open in Photos</CButton>
                <span className="text-body-secondary">·</span>
                <CButton color="link" size="sm" className="p-0"
                         onClick={() => navigate(`/videos?date=${dayParam}`)}>Open in Videos</CButton>
              </CAlert>
            )}
          </>
        )}
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="py-4 px-4">
      {/* Everything above the grid is measured, not estimated: the filter card
          grows a second row of pills when a year is picked, and the number of
          card rows that fit has to follow it. */}
      <div ref={topSectionRef}>
        {/* Header */}
        <div className="d-flex justify-content-between align-items-center mb-3">
          <div>
            <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>Events</h4>
            <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
              Browse and manage events from your catalog
            </div>
          </div>
        </div>

        <CCard className="mb-3">
          <CCardBody className="py-2">
            {/* Year / Month filters.
                These narrow the list; they do not define it. The page opens on
                every event, and "All" is the active pill until someone picks a
                year - which is also why clicking the selected year clears it. */}
            {!loading && events.length > 0 && (
              <div className="d-flex flex-column gap-2">
                <div className="d-flex gap-1 flex-wrap">
                  <button
                    className={`btn btn-sm ${selectedYear == null ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={clearFilters}
                  >
                    All
                  </button>
                  {allYears.filter((y) => y !== 'Unknown').map((year) => (
                    <button
                      key={year}
                      className={`btn btn-sm ${selectedYear === Number(year) ? 'btn-primary' : 'btn-outline-secondary'}`}
                      onClick={() => handleYearChange(Number(year))}
                    >
                      {year}
                    </button>
                  ))}
                  {availableYearsMonths['Unknown'] && (
                    <button
                      className={`btn btn-sm ${selectedYear === 'Unknown' ? 'btn-primary' : 'btn-outline-secondary'}`}
                      onClick={() => handleYearChange('Unknown')}
                    >
                      Unknown
                    </button>
                  )}
                </div>

                {selectedYear != null && availableYearsMonths[String(selectedYear)] && (
                  <div className="d-flex gap-1 flex-wrap">
                    {/* The whole year, which is what picking a year already
                        gives you - shown so the month row has a visible
                        "no month" state rather than an implied one. */}
                    <button
                      className={`btn btn-sm ${selectedMonth == null ? 'btn-primary' : 'btn-outline-secondary'}`}
                      onClick={() => { setSelectedMonth(null); setPage(0); }}
                    >
                      All {selectedYear}
                    </button>
                    {availableYearsMonths[String(selectedYear)]
                      .filter((m) => m !== -1)
                      .map((monthIdx) => (
                        <button
                          key={monthIdx}
                          className={`btn btn-sm ${selectedMonth === monthIdx ? 'btn-primary' : 'btn-outline-secondary'}`}
                          onClick={() => handleMonthChange(monthIdx)}
                        >
                          {MONTH_NAMES[monthIdx].slice(0, 3)}
                        </button>
                      ))}
                    {availableYearsMonths[String(selectedYear)].includes(-1) && (
                      <button
                        className={`btn btn-sm ${selectedMonth === -1 ? 'btn-primary' : 'btn-outline-secondary'}`}
                        onClick={() => handleMonthChange(-1)}
                      >
                        Unknown
                      </button>
                    )}
                  </div>
                )}
              </div>
            )}
          </CCardBody>
        </CCard>

        {/* Alerts */}
        {error && <CAlert color="danger" className="mb-3">{error}</CAlert>}
      </div>

      {/* Loading */}
      {loading && (
        <div className="text-center py-5">
          <CSpinner color="primary" />
          <div className="mt-2 text-muted">Loading events…</div>
        </div>
      )}

      {/* Empty state */}
      {!loading && !error && events.length === 0 && (
        <div className="text-center py-5 text-muted">
          <CIcon icon={cilCalendar} style={{ width: 48, height: 48, opacity: 0.25 }} />
          <div className="mt-3" style={{ fontSize: '1rem' }}>No events found in this catalog.</div>
          <div style={{ fontSize: '0.85rem', marginTop: 4 }}>Events are extracted automatically during workflow processing.</div>
        </div>
      )}

      {/* The grid.
          One flat page of cards, not year/month sections: the page shows as
          many whole rows as fit the window and pages through the rest, so it
          never scrolls vertically. Filtering is the pills' job. */}
      {!loading && !error && events.length > 0 && (
        <CCard>
          <CCardHeader className="d-flex align-items-center py-2">
            <strong style={{ fontSize: '0.9rem' }}>Events</strong>
            <CBadge color="secondary" className="ms-2">
              {visibleEvents.length === events.length
                ? visibleEvents.length
                : `${visibleEvents.length} of ${events.length}`}
            </CBadge>
            {selectedYear != null && (
              <span style={{ fontSize: '0.78rem', color: '#888', marginLeft: 8 }}>
                {selectedMonth != null && selectedMonth !== -1
                  ? `${MONTH_NAMES[selectedMonth]} ${selectedYear}`
                  : selectedMonth === -1 ? `${selectedYear}, no month`
                  : selectedYear}
              </span>
            )}
            {totalPages > 1 && (
              <div className="d-flex align-items-center gap-2 ms-auto">
                <CButton color="secondary" size="sm"
                         onClick={() => setPage((p) => Math.max(0, p - 1))}
                         disabled={safePage === 0}>
                  ← Previous
                </CButton>
                <span style={{ fontSize: '0.85rem', color: '#666', whiteSpace: 'nowrap' }}>
                  Page {safePage + 1} of {totalPages}
                </span>
                <CButton color="secondary" size="sm"
                         onClick={() => setPage((p) => Math.min(totalPages - 1, p + 1))}
                         disabled={safePage === totalPages - 1}>
                  Next →
                </CButton>
              </div>
            )}
          </CCardHeader>
          <CCardBody
            ref={gridBodyRef}
            style={{
              // Neither pinned nor clipped. It holds exactly cols x rows
              // cards, so it is already the right height; forcing a pixel
              // value computed from an assumed card height is what cut the
              // bottom row off. See the row calculation above.
              overflow: 'visible',
            }}
          >
            {visibleEvents.length === 0 ? (
              <div className="text-center py-5 text-muted">
                <CIcon icon={cilCalendar} style={{ width: 40, height: 40, opacity: 0.25 }} />
                <div className="mt-3" style={{ fontSize: '0.95rem' }}>
                  No events in this period.
                </div>
                <CButton color="link" size="sm" onClick={clearFilters}>
                  Show all events
                </CButton>
              </div>
            ) : (
              <div ref={gridRef}
                   style={{ display: 'flex', flexWrap: 'wrap', gap: '8px', alignItems: 'stretch' }}>
                {pageEvents.map((ev) => {
                  const isFlipped = flippedEventId === ev.id;
                  const showAddLoc = addLocationEventId === ev.id;

                  // Edit form and add-location panel both open: the pair spans
                  // the full width and sits side by side.
                  if (isFlipped && showAddLoc) {
                    return (
                      <div key={ev.id} style={{ width: '100%' }}>
                        <div style={{ display: 'flex', gap: '8px', alignItems: 'flex-start', flexWrap: 'wrap' }}>
                          <div style={{ flex: '1 1 280px', minWidth: 0 }}>
                            <EventCard
                              event={ev}
                              isSelected={false}
                              onClick={() => handleEventClick(ev)}
                              isFlipped={isFlipped}
                              onFlip={() => handleFlip(ev)}
                              onCancelFlip={handleCancelFlip}
                              catalogId={activeCatalog.id}
                              onSaved={handleSaved}
                              showAddLocation={showAddLoc}
                              onToggleAddLocation={() => handleToggleAddLocation(ev.id)}
                              onLocationSaved={handleLocationSaved}
                              locationReloadTrigger={locationReloadTrigger}
                            />
                          </div>
                          <div style={{ flex: '1 1 280px', minWidth: 0 }}>
                            <AddLocationPanel
                              catalogId={activeCatalog.id}
                              eventId={ev.id}
                              eventLat={ev.latitude}
                              eventLong={ev.longitude}
                              userLat={userSettings?.lat}
                              userLong={userSettings?.long}
                              onSaved={handleLocationSaved}
                              onCancel={() => setAddLocationEventId(null)}
                            />
                          </div>
                        </div>
                      </div>
                    );
                  }

                  return (
                    <div key={ev.id}
                         style={{ width: CARD_WIDTH, flexShrink: 0, display: 'flex', flexDirection: 'column' }}>
                      <EventCard
                        event={ev}
                        isSelected={false}
                        onClick={() => handleEventClick(ev)}
                        isFlipped={isFlipped}
                        onFlip={() => handleFlip(ev)}
                        onCancelFlip={handleCancelFlip}
                        catalogId={activeCatalog.id}
                        onSaved={handleSaved}
                        showAddLocation={showAddLoc}
                        onToggleAddLocation={() => handleToggleAddLocation(ev.id)}
                        onLocationSaved={handleLocationSaved}
                        locationReloadTrigger={locationReloadTrigger}
                      />
                    </div>
                  );
                })}
              </div>
            )}
          </CCardBody>
        </CCard>
      )}

    </CContainer>
  );
};

export default Events;
