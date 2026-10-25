import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useParams, useNavigate, useLocation, Link } from 'react-router-dom';
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
  // DEAD CODE — MARKED FOR DELETION: used only by the commented-out face
  // tagging modal below. See the banner on that region.
  // CModal,
  // CModalHeader,
  // CModalTitle,
  // CModalBody,
  // CModalFooter,
  // CListGroup,
  // CListGroupItem,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import EventMergeCard from '../components/EventMergeCard';
import EventDateCard from '../components/EventDateCard';
import {
  cilArrowLeft,
  cilCalendar,
  cilPencil,
  cilX,
  cilLocationPin,
  cilImage,
  cilCheck,
  cilTrash,
  cilMap,
  cilStar,
  // DEAD CODE — MARKED FOR DELETION: used only by the commented-out face
  // tagging modal below. See the banner on that region.
  // cilPlus,
  // cilFace,
  cilExternalLink,
  cilChevronLeft,
  cilChevronRight,
} from '@coreui/icons';
import CreatableSelect from 'react-select/creatable';
import { MapContainer, TileLayer, Marker, useMapEvents } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { eventAPI, locationAPI, photoAPI, userSettingsAPI } from '../services/api';
import socketService from '../services/socket';
import { API_HOST, galleryThumbUrl } from '../config';
// DEAD CODE — MARKED FOR DELETION: the default `api` client, faceDetectionAPI,
// VISION_HOST and PEOPLE_PHOTOS_BASE_URL are reached only from the commented-out
// face tagging modal below. See the banner on that region.
// import api, { faceDetectionAPI } from '../services/api';
// import { VISION_HOST, PEOPLE_PHOTOS_BASE_URL } from '../config';

// Fix leaflet default marker icon issue with webpack
delete L.Icon.Default.prototype._getIconUrl;
L.Icon.Default.mergeOptions({
  iconRetinaUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png',
  iconUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png',
  shadowUrl: 'https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png',
});

// Component that handles map click events to drop a pin
const MapClickHandler = ({ onMapClick }) => {
  useMapEvents({
    click(e) {
      onMapClick(e.latlng.lat, e.latlng.lng);
    },
  });
  return null;
};

// ─── Helpers ──────────────────────────────────────────────────────────────────

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

const DISPLAYABLE_IMAGE_EXTS = new Set([
  'jpg', 'jpeg', 'png', 'gif', 'webp', 'bmp', 'tiff', 'tif', 'svg', 'avif',
]);
const NEEDS_ALTERNATIVE_EXTS = new Set(['heic', 'heif', 'cr3', 'cr2', 'nef', 'arw', 'dng', 'raf', 'orf', 'rw2']);

function getFileExtension(fileName) {
  if (!fileName) return '';
  const parts = fileName.split('.');
  return parts.length > 1 ? parts[parts.length - 1].toLowerCase() : '';
}

function isDisplayableImage(fileName) {
  return DISPLAYABLE_IMAGE_EXTS.has(getFileExtension(fileName));
}

function needsAlternative(fileName) {
  return NEEDS_ALTERNATIVE_EXTS.has(getFileExtension(fileName));
}

// GRID_PAGE_SIZE is now computed dynamically from available space

// ─── EventDetail ──────────────────────────────────────────────────────────────

const EventDetail = () => {
  const { catalogId, eventId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();

  const [event, setEvent] = useState(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  // Bumped to re-read the event and its photos in place, when a date batch
  // finishes and this event survived the rebuild.
  const [reloadKey, setReloadKey] = useState(0);

  // ── Date batch (mass-edit-event-photo-dates-spec.md section 4.5) ──
  // The editor redirects here with {dateBatchId} while the server re-dates the
  // photos in the background. `batch` is its latest known state. Once it ends,
  // the page lands on the event that holds the photos and carries
  // {dateBatchResult} in router state, so the "Done" banner survives the
  // redirect to a successor event.
  const [batchId, setBatchId] = useState(location.state?.dateBatchId ?? null);
  const [batch, setBatch] = useState(null);
  const [batchResult, setBatchResult] = useState(location.state?.dateBatchResult ?? null);
  const [showBatchFailures, setShowBatchFailures] = useState(false);

  // Photo gallery state
  const [displayFiles, setDisplayFiles] = useState([]);
  // Index into displayFiles of the file being previewed, or null. An INDEX
  // rather than the file itself, so the overlay's arrows can walk the whole
  // event rather than just the page the user happened to click from.
  const [previewIndex, setPreviewIndex] = useState(null);
  const [photosLoading, setPhotosLoading] = useState(false);
  const [photosError, setPhotosError] = useState(null);
  const [page, setPage] = useState(0);
  const loadingForRef = useRef(null);

  // Dynamic grid sizing
  const galleryBodyRef = useRef(null);
  const topSectionRef = useRef(null);
  const [pageSize, setPageSize] = useState(12);
  const [galleryBodyH, setGalleryBodyH] = useState(400);

  // Edit form state
  const [isEditing, setIsEditing] = useState(false);
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

  // Location state
  const [isAddingLocation, setIsAddingLocation] = useState(false);
  const [locationForm, setLocationForm] = useState({
    name: '',
    lat: '',
    long: '',
    description: '',
  });
  const [locationSaving, setLocationSaving] = useState(false);
  const [locationSaveError, setLocationSaveError] = useState(null);
  const [eventLocations, setEventLocations] = useState([]);
  const [locationsLoading, setLocationsLoading] = useState(false);
  const [removingLocationId, setRemovingLocationId] = useState(null);

  // Edit location state
  const [isEditingLocation, setIsEditingLocation] = useState(false);
  const [editingLocationId, setEditingLocationId] = useState(null);
  const [editLocationForm, setEditLocationForm] = useState({
    name: '',
    lat: '',
    long: '',
    description: '',
  });
  const [editLocationSaving, setEditLocationSaving] = useState(false);
  const [editLocationSaveError, setEditLocationSaveError] = useState(null);

  // All catalog locations (for the CreatableSelect dropdown)
  const [allLocations, setAllLocations] = useState([]);
  const [locationSelectLoading, setLocationSelectLoading] = useState(false);

  // User settings state
  const [userSettings, setUserSettings] = useState(null);

  // Map state for Add Location
  const [mapPanelVisible, setMapPanelVisible] = useState(false);
  const [pinPosition, setPinPosition] = useState(null); // { lat, lng }

  // ─── DEAD CODE — MARKED FOR DELETION ─────────────────────────────
  // Unreachable since face tagging moved to the Photos page: the only entry point
  // was handleOpenImageDetail(), which nothing calls, and the "Tag faces" overlay
  // that suggested it was removed in event-cards-ui-spec.md section 2.4.
  // Kept commented for one release so the change is reviewable. Delete wholesale —
  // see the checklist item in event-cards-ui-spec.md section 8.
  // The description editor below is part of the same modal and dies with it.
  /*
  // Face tagging state
  const [showImageDetail, setShowImageDetail] = useState(false);
  const [selectedImageForTagging, setSelectedImageForTagging] = useState(null);
  const [detectedFaces, setDetectedFaces] = useState([]);
  const [detectingFaces, setDetectingFaces] = useState(false);
  const [selectedFace, setSelectedFace] = useState(null);
  const [showTagModal, setShowTagModal] = useState(false);
  const [showAddPersonModal, setShowAddPersonModal] = useState(false);
  const [newPersonName, setNewPersonName] = useState('');
  const [people, setPeople] = useState([]);
  const [hoveredFaceId, setHoveredFaceId] = useState(null);
  const [shouldReopenTagModal, setShouldReopenTagModal] = useState(false);
  const [hoveredRemoveTagId, setHoveredRemoveTagId] = useState(null);
  const [isDrawing, setIsDrawing] = useState(false);
  const [drawStart, setDrawStart] = useState(null);
  const [drawPreview, setDrawPreview] = useState(null);
  const imageRefTagging = useRef(null);
  const canvasRefTagging = useRef(null);
  const removeTagAreasRef = useRef([]);
  const isDrawingRef = useRef(false);
  const drawStartRef = useRef(null);

  // Image description state
  const [imageDescription, setImageDescription] = useState('');
  const [isEditingDescription, setIsEditingDescription] = useState(false);
  const [savingDescription, setSavingDescription] = useState(false);
  const [generatingDescription, setGeneratingDescription] = useState(false);
  */

  // Load user settings
  useEffect(() => {
    userSettingsAPI.get()
      .then((res) => setUserSettings(res.data))
      .catch(() => setUserSettings(null));
  }, []);

  // Router state is per history entry. The component stays mounted when it
  // navigates from one event to another, so it is re-read on every entry.
  useEffect(() => {
    const st = location.state || {};
    if (st.dateBatchId) {
      setBatchId(st.dateBatchId);
      setBatch(null);
      setBatchResult(null);
    } else if (st.dateBatchResult) {
      setBatchId(null);
      setBatch(null);
      setBatchResult(st.dateBatchResult);
      setShowBatchFailures(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [location.key]);

  // A finished batch: find the event now holding most of its photos, and land
  // there. Create Events gives Auto events new ids, so this event being gone
  // is the common case, not an error.
  const landOnBatch = useCallback(async (b) => {
    const ids = b.file_ids || [];
    let located = {};
    try {
      located = ids.length ? ((await eventAPI.locate(catalogId, ids)).data || {}) : {};
    } catch {
      located = {};
    }
    const tally = new Map();
    Object.values(located).forEach((evs) => (evs || []).forEach((ev) => {
      const t = tally.get(ev.id) || { event: ev, count: 0 };
      t.count += 1;
      tally.set(ev.id, t);
    }));
    const best = [...tally.values()].sort((a, c) => c.count - a.count)[0] || null;

    let stillHere = false;
    try {
      const r = await eventAPI.getAll(catalogId);
      stillHere = (r.data || []).some((e) => String(e.id) === String(eventId));
    } catch {
      stillHere = false;
    }

    const result = {
      batchId: b.id,
      status: b.status,
      done: b.done,
      total: b.total,
      moved: best ? best.count : 0,
      target: best ? { id: best.event.id, name: best.event.name } : null,
      failed: b.result?.failed || [],
      rebuildStatus: b.result?.rebuild_status || null,
    };
    const state = { dateBatchResult: result };
    if (stillHere) {
      // Same event: reload its photos and Date card in place.
      setReloadKey((k) => k + 1);
      navigate(`/events/${catalogId}/${eventId}`, { replace: true, state });
    } else if (best) {
      navigate(`/events/${catalogId}/${best.event.id}`, { replace: true, state });
    } else {
      navigate('/events', { replace: true, state });
    }
  }, [catalogId, eventId, navigate]);
  const landRef = useRef(landOnBatch);
  landRef.current = landOnBatch;

  // Follow the batch: Socket.IO for prompt progress, and a 2 s poll as well,
  // because a missed socket event must not leave the banner stuck.
  useEffect(() => {
    if (!batchId || !catalogId) return undefined;
    let stopped = false;
    let landing = false;
    let timer = null;
    const terminal = (st) => st === 'completed' || st === 'interrupted' || st === 'failed';
    const settle = (b) => {
      if (stopped || !b) return;
      setBatch(b);
      if (terminal(b.status) && !landing) {
        landing = true;
        clearInterval(timer);
        landRef.current(b);
      }
    };
    const fetchBatch = () => photoAPI.getDateBatch(catalogId, batchId)
      .then((r) => settle(r.data))
      .catch(() => {});
    fetchBatch();
    timer = setInterval(fetchBatch, 2000);
    socketService.connect();
    const off = socketService.onDateBatchProgress((d) => {
      if (String(d.batch_id) !== String(batchId)) return;
      if (d.catalog_id != null && String(d.catalog_id) !== String(catalogId)) return;
      setBatch((prev) => ({ ...(prev || {}), done: d.done, total: d.total, status: d.status }));
      // The socket carries no file ids or result; the row does.
      if (terminal(d.status)) fetchBatch();
    });
    return () => {
      stopped = true;
      clearInterval(timer);
      off();
    };
  }, [batchId, catalogId]);

  // Load event details
  useEffect(() => {
    if (!catalogId || !eventId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    eventAPI.getAll(catalogId)
      .then(async (res) => {
        if (cancelled) return;
        const found = res.data.find((e) => String(e.id) === String(eventId));
        if (found) {
          setEvent(found);
          return;
        }
        setEvent(null);
        // Gone. A date batch opened on this event may have replaced it: a
        // reload, or a link followed later, has lost the router state, so ask
        // for the latest batch and follow its photos (section 4.5).
        let latest = null;
        try {
          latest = (await photoAPI.latestDateBatch(catalogId, eventId)).data;
        } catch {
          latest = null;
        }
        if (cancelled) return;
        if (!latest) {
          setError('Event not found');
        } else if (latest.status === 'running' || latest.status === 'rebuilding') {
          setBatchId(latest.id);
        } else {
          await landRef.current(latest);
        }
      })
      .catch(() => { if (!cancelled) setError('Failed to load event'); })
      .finally(() => { if (!cancelled) setLoading(false); });
    return () => { cancelled = true; };
  }, [catalogId, eventId, reloadKey]);

  // ── Event cover ──
  // The card on the Events page shows one of this event's photos. It is picked
  // at random the first time the event is listed and stored, so the value here
  // is normally already set (event-cards-ui-spec.md section 3.2); the star lets
  // the user replace it.
  const [coverFileId, setCoverFileId] = useState(null);
  const [coverMessage, setCoverMessage] = useState(null);

  useEffect(() => {
    setCoverFileId(event?.cover_file_id ?? null);
  }, [event?.cover_file_id]);

  const handleSetCover = useCallback(async (fileId) => {
    if (!catalogId || !eventId || fileId === coverFileId) return;
    const previous = coverFileId;
    // Optimistic: the star fills immediately and rolls back if the write fails.
    setCoverFileId(fileId);
    // Replaced, never appended: clicking through several photos in a row must
    // not stack a message per click.
    setCoverMessage({ color: 'success', text: 'Event cover updated.' });
    try {
      await eventAPI.setCover(catalogId, eventId, fileId);
      setEvent((prev) => (prev ? { ...prev, cover_file_id: fileId } : prev));
    } catch {
      setCoverFileId(previous);
      setCoverMessage({ color: 'danger', text: 'Could not set the event cover.' });
    }
  }, [catalogId, eventId, coverFileId]);

  // Load existing locations for this event
  useEffect(() => {
    if (!catalogId || !eventId) return;
    setLocationsLoading(true);
    eventAPI.getLocations(catalogId, eventId)
      .then((res) => setEventLocations(res.data || []))
      .catch(() => setEventLocations([]))
      .finally(() => setLocationsLoading(false));
  }, [catalogId, eventId]);

  // Load photos when event is available
  useEffect(() => {
    if (!catalogId || !event?.id) return;
    setPhotosLoading(true);
    setPhotosError(null);
    setPage(0);
    setDisplayFiles([]);
    loadingForRef.current = event.id;

    eventAPI.getFiles(catalogId, event.id)
      .then((res) => {
        if (loadingForRef.current !== event.id) return;
        const allFiles = res.data;
        const resolved = [];
        
        for (const f of allFiles) {
          // Determine which path to use for display
          // Priority: alternative_file_path > file_path
          const displayPath = f.alternative_file_path || f.file_path;
          
          // Include files that:
          // 1. Have an alternative version (for HEIC/CR3/RAW files), OR
          // 2. Are directly displayable in browsers (JPG, PNG, etc.)
          // Skip files that need alternatives but don't have them yet (CR3, HEIC, etc. without alternatives)
          const hasAlternative = f.alternative_file_path != null;
          const isDirectlyDisplayable = isDisplayableImage(f.file_name);
          const requiresAlternative = needsAlternative(f.file_name);
          
          // Videos are included on their own terms: a proxy is optional, so
          // requiring an alternative would hide every already-playable video.
          if (displayPath && (f.is_video || hasAlternative
                              || (isDirectlyDisplayable && !requiresAlternative))) {
            resolved.push({
              id: f.id,
              file_name: f.file_name,
              file_path: f.file_path,
              alternative_file_path: f.alternative_file_path,
              displayName: f.file_name,
              // A video with no proxy took the passthrough branch: its own
              // file streams fine, so displayPath is the original. See
              // video-spec.md section 4.
              isVideo: Boolean(f.is_video),
              imageUrl: f.is_video
                ? null
                : `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(displayPath)}`,
              videoUrl: f.is_video
                ? `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(displayPath)}`
                : null,
              // For a video this resolves to the poster frame, which is stored
              // as an ordinary TYPE THUMBNAIL row.
              thumbUrl: galleryThumbUrl(catalogId, f.id),
              description: f.description || '',
            });
          }
          // Skip files that need alternatives but don't have them (CR3, HEIC, etc.)
          // Skip files that are not images (videos, documents, etc.)
        }
        
        if (loadingForRef.current === event.id) {
          setDisplayFiles(resolved);
        }
      })
      .catch(() => {
        if (loadingForRef.current === event.id) setPhotosError('Failed to load event images');
      })
      .finally(() => {
        if (loadingForRef.current === event.id) setPhotosLoading(false);
      });
  }, [catalogId, event?.id, reloadKey]);

  // Populate edit form when editing starts
  useEffect(() => {
    if (isEditing && event) {
      setForm({
        name: event.name || '',
        start_time: toInputDatetime(event.start_time),
        end_time: toInputDatetime(event.end_time),
        description: event.description || '',
        latitude: event.latitude != null ? String(event.latitude) : '',
        longitude: event.longitude != null ? String(event.longitude) : '',
      });
      setSaveError(null);
    }
  }, [isEditing, event]);

  // Load the catalog's CONFIRMED locations for the CreatableSelect.
  //
  // Confirmed only: the clustering workflow writes an unconfirmed location per
  // cluster, named from a geocoder guess - over a thousand on a real catalog -
  // and a picker offering those would bury the handful somebody has actually
  // named. Confirming one on the Locations page is what puts it here.
  useEffect(() => {
    if (!catalogId) return;
    setLocationSelectLoading(true);
    locationAPI.getAll(catalogId, { confirmedOnly: true })
      .then((res) => setAllLocations(res.data || []))
      .catch(() => setAllLocations([]))
      .finally(() => setLocationSelectLoading(false));
  }, [catalogId]);

  // Reset location form error when opening (but don't reset name — it may be pre-filled from CreatableSelect)
  useEffect(() => {
    if (isAddingLocation) {
      setLocationSaveError(null);
    }
  }, [isAddingLocation]);

  const handleChange = (field) => (e) => setForm((f) => ({ ...f, [field]: e.target.value }));
  const handleLocationChange = (field) => (e) => setLocationForm((f) => ({ ...f, [field]: e.target.value }));
  const handleEditLocationChange = (field) => (e) => setEditLocationForm((f) => ({ ...f, [field]: e.target.value }));

  // Handle CreatableSelect change for locations
  const handleLocationSelectChange = async (option, actionMeta) => {
    if (!option) return;

    if (option.__isNew__) {
      // New location typed by user → pre-fill name and open the Add Location panel
      // Default to user settings first, then fall back to event coordinates
      let defaultLat = '';
      let defaultLong = '';
      
      if (userSettings?.lat != null && userSettings?.long != null) {
        defaultLat = String(userSettings.lat);
        defaultLong = String(userSettings.long);
      } else if (event?.latitude != null && event?.longitude != null) {
        defaultLat = String(event.latitude);
        defaultLong = String(event.longitude);
      }
      
      setLocationForm({
        name: option.value,
        lat: defaultLat,
        long: defaultLong,
        description: '',
      });
      setLocationSaveError(null);
      setIsAddingLocation(true);
    } else {
      // Existing location selected → link it to the event
      const alreadyLinked = eventLocations.some((l) => l.id === option.value);
      if (alreadyLinked) return;
      try {
        await locationAPI.linkToEvent(catalogId, eventId, option.value);
        const locRes = await eventAPI.getLocations(catalogId, eventId);
        setEventLocations(locRes.data || []);
      } catch {
        // silently ignore
      }
    }
  };

  const handleSave = async () => {
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
      // Reload event data
      const res = await eventAPI.getAll(catalogId);
      const updated = res.data.find((e) => String(e.id) === String(eventId));
      if (updated) setEvent(updated);
      setIsEditing(false);
    } catch (err) {
      setSaveError(err.response?.data?.error || 'Failed to save event');
    } finally {
      setSaving(false);
    }
  };

  const handleSaveLocation = async () => {
    if (!locationForm.name.trim()) { setLocationSaveError('Location name is required'); return; }
    setLocationSaving(true);
    setLocationSaveError(null);
    try {
      const payload = {
        name: locationForm.name.trim(),
        lat: locationForm.lat !== '' ? parseFloat(locationForm.lat) : null,
        long: locationForm.long !== '' ? parseFloat(locationForm.long) : null,
        description: locationForm.description.trim() || null,
        source: 'Manual',
      };
      await eventAPI.addLocation(catalogId, eventId, payload);
      // Refresh locations list and all-locations list
      const [locRes, allLocRes] = await Promise.all([
        eventAPI.getLocations(catalogId, eventId),
        locationAPI.getAll(catalogId, { confirmedOnly: true }),
      ]);
      setEventLocations(locRes.data || []);
      setAllLocations(allLocRes.data || []);
      setLocationForm({ name: '', lat: '', long: '', description: '' });
      setIsAddingLocation(false);
    } catch (err) {
      setLocationSaveError(err.response?.data?.error || 'Failed to save location');
    } finally {
      setLocationSaving(false);
    }
  };

  const handleRemoveLocation = async (locationId) => {
    setRemovingLocationId(locationId);
    try {
      await eventAPI.removeLocation(catalogId, eventId, locationId);
      setEventLocations((prev) => prev.filter((l) => l.id !== locationId));
    } catch {
      // silently ignore
    } finally {
      setRemovingLocationId(null);
    }
  };

  // Map handlers for Add Location
  const handleUseMap = () => {
    // When opening the map for a new location, default to user settings lat/long if available
    // and the location form doesn't already have coordinates
    if (userSettings && userSettings.lat != null && userSettings.long != null) {
      if (locationForm.lat === '' && locationForm.long === '') {
        setLocationForm((f) => ({
          ...f,
          lat: String(userSettings.lat),
          long: String(userSettings.long),
        }));
      }
    }
    setMapPanelVisible(true);
  };

  const handleCloseMapPanel = () => {
    setMapPanelVisible(false);
  };

  const handleMapClick = useCallback((lat, lng) => {
    const latStr = lat.toFixed(6);
    const lngStr = lng.toFixed(6);
    setPinPosition({ lat, lng });
    setLocationForm((prev) => ({ ...prev, lat: latStr, long: lngStr }));
  }, []);

  const getMapCenter = () => {
    if (pinPosition) return [pinPosition.lat, pinPosition.lng];
    if (locationForm.lat && locationForm.long) {
      return [parseFloat(locationForm.lat), parseFloat(locationForm.long)];
    }
    if (event?.latitude != null && event?.longitude != null) {
      return [event.latitude, event.longitude];
    }
    return [40.7128, -74.0060]; // Nothing to place it by yet: a neutral starting view
  };

  const getMapZoom = () => {
    if (pinPosition || (locationForm.lat && locationForm.long)) return 13;
    if (event?.latitude != null && event?.longitude != null) return 10;
    return 8;
  };

  const handleOpenEditLocation = (loc) => {
    setIsAddingLocation(false);
    setEditingLocationId(loc.id);
    setEditLocationForm({
      name: loc.name || '',
      lat: loc.lat != null ? String(loc.lat) : '',
      long: loc.long != null ? String(loc.long) : '',
      description: loc.description || '',
    });
    setEditLocationSaveError(null);
    setIsEditingLocation(true);
  };

  const handleSaveEditLocation = async () => {
    if (!editLocationForm.name.trim()) { setEditLocationSaveError('Location name is required'); return; }
    setEditLocationSaving(true);
    setEditLocationSaveError(null);
    try {
      const payload = {
        name: editLocationForm.name.trim(),
        lat: editLocationForm.lat !== '' ? parseFloat(editLocationForm.lat) : null,
        long: editLocationForm.long !== '' ? parseFloat(editLocationForm.long) : null,
        description: editLocationForm.description.trim() || null,
      };
      await locationAPI.update(catalogId, editingLocationId, payload);
      // Refresh locations list
      const locRes = await eventAPI.getLocations(catalogId, eventId);
      setEventLocations(locRes.data || []);
      setIsEditingLocation(false);
      setEditingLocationId(null);
      setEditLocationForm({ name: '', lat: '', long: '', description: '' });
    } catch (err) {
      setEditLocationSaveError(err.response?.data?.error || 'Failed to save location');
    } finally {
      setEditLocationSaving(false);
    }
  };

  // ─── DEAD CODE — MARKED FOR DELETION ─────────────────────────────
  // Unreachable since face tagging moved to the Photos page: the only entry point
  // was handleOpenImageDetail(), which nothing calls, and the "Tag faces" overlay
  // that suggested it was removed in event-cards-ui-spec.md section 2.4.
  // Kept commented for one release so the change is reviewable. Delete wholesale —
  // see the checklist item in event-cards-ui-spec.md section 8.
  /*
  // Load people when image detail view opens
  useEffect(() => {
    if (showImageDetail && catalogId) {
      api.get(`/catalogs/${catalogId}/people`)
        .then(res => setPeople(res.data || []))
        .catch(() => setPeople([]));
    }
  }, [showImageDetail, catalogId]);

  // Reopen tag modal after people list is updated (when a new person is added)
  useEffect(() => {
    if (shouldReopenTagModal && people.length > 0) {
      setShowTagModal(true);
      setShouldReopenTagModal(false);
    }
  }, [people, shouldReopenTagModal]);

  // Face tagging functions
  const handleOpenImageDetail = (imageFile) => {
    setSelectedImageForTagging(imageFile);
    setShowImageDetail(true);
    setDetectedFaces([]);
    setSelectedFace(null);
  };

  const handleCloseImageDetail = () => {
    setShowImageDetail(false);
    setSelectedImageForTagging(null);
    setDetectedFaces([]);
    setSelectedFace(null);
    setIsDrawing(false);
    setDrawStart(null);
    setDrawPreview(null);
    isDrawingRef.current = false;
    drawStartRef.current = null;
  };

  const scaleFaceCoordinates = (faces, detectionWidth, detectionHeight) => {
    // If no detection dimensions provided or no image loaded, return faces as-is
    if (!detectionWidth || !detectionHeight || !imageRefTagging.current) {
      return faces;
    }
    
    const displayedWidth = imageRefTagging.current.naturalWidth;
    const displayedHeight = imageRefTagging.current.naturalHeight;
    
    // If dimensions match, no scaling needed
    if (displayedWidth === detectionWidth && displayedHeight === detectionHeight) {
      return faces;
    }
    
    // Calculate scale factors
    const scaleX = displayedWidth / detectionWidth;
    const scaleY = displayedHeight / detectionHeight;
    
    // Scale all face coordinates
    return faces.map(face => ({
      ...face,
      x: Math.round(face.x * scaleX),
      y: Math.round(face.y * scaleY),
      w: Math.round(face.w * scaleX),
      h: Math.round(face.h * scaleY)
    }));
  };

  const handleDetectFaces = async () => {
    if (!selectedImageForTagging) return;
    
    setDetectingFaces(true);
    setError(null);
    try {
      const response = await faceDetectionAPI.detectFaces(catalogId, selectedImageForTagging.id);
      if (response.data.success) {
        const faces = response.data.faces || [];
        const detectionWidth = response.data.detection_width;
        const detectionHeight = response.data.detection_height;
        
        // Scale face coordinates if detection dimensions differ from displayed image
        const scaledFaces = scaleFaceCoordinates(faces, detectionWidth, detectionHeight);
        
        setDetectedFaces(scaledFaces);
        drawFacesOnCanvas(scaledFaces);
      } else {
        setError('Face detection failed: ' + (response.data.error || 'Unknown error'));
      }
    } catch (err) {
      console.error('Failed to detect faces:', err);
      const errorMsg = err.response?.data?.error || err.message || 'Failed to detect faces';
      setError(errorMsg);
    } finally {
      setDetectingFaces(false);
    }
  };

  const loadExistingFaces = async (fileId) => {
    try {
      const response = await faceDetectionAPI.getFaces(catalogId, fileId);
      if (response.data.faces && response.data.faces.length > 0) {
        // Scale face coordinates if they were detected on a different image size
        const scaledFaces = response.data.faces.map(face => {
          // If face has stored image dimensions and they differ from current display
          if (face.image_width && face.image_height && imageRefTagging.current) {
            const displayWidth = imageRefTagging.current.naturalWidth;
            const displayHeight = imageRefTagging.current.naturalHeight;
            
            // Only scale if dimensions differ
            if (face.image_width !== displayWidth || face.image_height !== displayHeight) {
              const scaleX = displayWidth / face.image_width;
              const scaleY = displayHeight / face.image_height;
              
              return {
                ...face,
                x: Math.round(face.x * scaleX),
                y: Math.round(face.y * scaleY),
                w: Math.round(face.w * scaleX),
                h: Math.round(face.h * scaleY)
              };
            }
          }
          return face;
        });
        
        setDetectedFaces(scaledFaces);
        drawFacesOnCanvas(scaledFaces);
      } else {
        setDetectedFaces([]);
        if (canvasRefTagging.current) {
          const ctx = canvasRefTagging.current.getContext('2d');
          ctx.clearRect(0, 0, canvasRefTagging.current.width, canvasRefTagging.current.height);
        }
      }
    } catch (err) {
      setDetectedFaces([]);
    }
  };

  const drawFacesOnCanvas = (faces, preview = null) => {
    if (!imageRefTagging.current || !canvasRefTagging.current) return;
    
    const canvas = canvasRefTagging.current;
    const ctx = canvas.getContext('2d');
    const img = imageRefTagging.current;
    
    canvas.width = img.naturalWidth;
    canvas.height = img.naturalHeight;
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    removeTagAreasRef.current = [];
    
    faces.forEach((face, index) => {
      const isHovered = hoveredFaceId === face.id;
      const isSelected = selectedFace?.id === face.id;
      const isRemoveTagHovered = hoveredRemoveTagId === face.id;
      
      if (isSelected) {
        ctx.strokeStyle = '#ffc107';
        ctx.lineWidth = 5;
      } else if (isHovered) {
        ctx.strokeStyle = face.person_id ? '#28a745' : '#007bff';
        ctx.lineWidth = 5;
      } else {
        ctx.strokeStyle = face.person_id ? '#28a745' : '#007bff';
        ctx.lineWidth = 3;
      }
      
      ctx.strokeRect(face.x, face.y, face.w, face.h);
      
      const label = face.person_name || `Face ${index + 1}`;
      ctx.font = '16px Arial';
      const textWidth = ctx.measureText(label).width;
      
      ctx.fillStyle = isSelected ? '#ffc107' : (face.person_id ? '#28a745' : '#007bff');
      ctx.fillRect(face.x, face.y - 25, textWidth + 10, 25);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(label, face.x + 5, face.y - 7);
      
      if (face.person_id) {
        const removeLabel = '✕';
        ctx.font = 'bold 16px Arial';
        const removeButtonSize = 20;
        const removeButtonX = face.x + face.w - removeButtonSize;
        const removeButtonY = face.y;
        
        removeTagAreasRef.current.push({
          faceId: face.id,
          x: removeButtonX,
          y: removeButtonY,
          w: removeButtonSize,
          h: removeButtonSize,
        });
        
        ctx.fillStyle = isRemoveTagHovered ? '#c82333' : '#dc3545';
        ctx.fillRect(removeButtonX, removeButtonY, removeButtonSize, removeButtonSize);
        ctx.fillStyle = '#ffffff';
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(removeLabel, removeButtonX + removeButtonSize / 2, removeButtonY + removeButtonSize / 2);
        ctx.textAlign = 'left';
        ctx.textBaseline = 'alphabetic';
      }
    });

    if (preview && preview.size > 0) {
      ctx.strokeStyle = '#ff6600';
      ctx.lineWidth = 2;
      ctx.setLineDash([8, 4]);
      ctx.strokeRect(preview.x, preview.y, preview.size, preview.size);
      ctx.setLineDash([]);
      ctx.font = '14px Arial';
      const previewLabel = 'New area';
      const previewTextWidth = ctx.measureText(previewLabel).width;
      ctx.fillStyle = '#ff6600';
      ctx.fillRect(preview.x, preview.y - 22, previewTextWidth + 10, 22);
      ctx.fillStyle = '#ffffff';
      ctx.fillText(previewLabel, preview.x + 5, preview.y - 5);
    }
  };

  const getCanvasCoords = (event) => {
    const canvas = canvasRefTagging.current;
    const img = imageRefTagging.current;
    const rect = canvas.getBoundingClientRect();
    const scaleX = img.naturalWidth / rect.width;
    const scaleY = img.naturalHeight / rect.height;
    return {
      x: (event.clientX - rect.left) * scaleX,
      y: (event.clientY - rect.top) * scaleY,
    };
  };

  const pointInAnyFace = (x, y, faces) => {
    for (const face of faces) {
      if (x >= face.x && x <= face.x + face.w && y >= face.y && y <= face.y + face.h) {
        return face;
      }
    }
    return null;
  };

  const handleCanvasClick = (event) => {
    if (!imageRefTagging.current || !canvasRefTagging.current) return;
    
    // Don't process clicks if we were drawing
    if (isDrawingRef.current) return;
    
    const { x, y } = getCanvasCoords(event);

    // Check if clicking on remove tag button
    for (const area of removeTagAreasRef.current) {
      if (x >= area.x && x <= area.x + area.w && y >= area.y && y <= area.y + area.h) {
        const face = detectedFaces.find(f => f.id === area.faceId);
        if (face) {
          event.preventDefault();
          event.stopPropagation();
          handleRemoveTag(face);
          return;
        }
      }
    }

    // Check if clicking on a face area
    const clickedFace = pointInAnyFace(x, y, detectedFaces);
    if (clickedFace) {
      event.preventDefault();
      event.stopPropagation();
      setSelectedFace(clickedFace);
      drawFacesOnCanvas(detectedFaces);
      // Open the tag modal when clicking on any face
      setShowTagModal(true);
      console.log('Opening tag modal for face:', clickedFace);
      return;
    }
  };

  const handleCanvasMouseDown = (event) => {
    if (!imageRefTagging.current || !canvasRefTagging.current) return;
    const { x, y } = getCanvasCoords(event);

    // Check if clicking on remove tag button - handle immediately
    for (const area of removeTagAreasRef.current) {
      if (x >= area.x && x <= area.x + area.w && y >= area.y && y <= area.y + area.h) {
        const face = detectedFaces.find(f => f.id === area.faceId);
        if (face) {
          event.preventDefault();
          event.stopPropagation();
          handleRemoveTag(face);
          return;
        }
      }
    }

    // Check if clicking on an existing face - don't start drawing
    const clickedFace = pointInAnyFace(x, y, detectedFaces);
    if (clickedFace) {
      event.preventDefault();
      event.stopPropagation();
      // Don't start drawing, just select the face
      setSelectedFace(clickedFace);
      drawFacesOnCanvas(detectedFaces);
      return;
    }

    // Start drawing a new face area
    isDrawingRef.current = true;
    drawStartRef.current = { x, y };
    setIsDrawing(true);
    setDrawStart({ x, y });
    setDrawPreview(null);
    setSelectedFace(null);
    event.preventDefault();
  };

  const handleCanvasMouseMove = (event) => {
    if (!imageRefTagging.current || !canvasRefTagging.current) return;
    const { x, y } = getCanvasCoords(event);
    const canvas = canvasRefTagging.current;

    if (isDrawingRef.current && drawStartRef.current) {
      const dx = x - drawStartRef.current.x;
      const dy = y - drawStartRef.current.y;
      const size = Math.max(Math.abs(dx), Math.abs(dy));
      const previewX = dx >= 0 ? drawStartRef.current.x : drawStartRef.current.x - size;
      const previewY = dy >= 0 ? drawStartRef.current.y : drawStartRef.current.y - size;
      const preview = { x: previewX, y: previewY, size };
      setDrawPreview(preview);
      drawFacesOnCanvas(detectedFaces, preview);
      canvas.style.cursor = 'crosshair';
      return;
    }

    if (detectedFaces.length === 0) {
      canvas.style.cursor = 'crosshair';
      return;
    }

    let foundRemoveTag = null;
    for (const area of removeTagAreasRef.current) {
      if (x >= area.x && x <= area.x + area.w && y >= area.y && y <= area.y + area.h) {
        foundRemoveTag = area.faceId;
        break;
      }
    }

    if (foundRemoveTag) {
      if (hoveredRemoveTagId !== foundRemoveTag) {
        setHoveredRemoveTagId(foundRemoveTag);
        setHoveredFaceId(null);
        canvas.style.cursor = 'pointer';
        drawFacesOnCanvas(detectedFaces);
      }
      return;
    } else if (hoveredRemoveTagId !== null) {
      setHoveredRemoveTagId(null);
      drawFacesOnCanvas(detectedFaces);
    }

    const foundFace = pointInAnyFace(x, y, detectedFaces);
    if (foundFace) {
      if (hoveredFaceId !== foundFace.id) {
        setHoveredFaceId(foundFace.id);
        canvas.style.cursor = 'pointer';
        drawFacesOnCanvas(detectedFaces);
      }
    } else {
      if (hoveredFaceId !== null) {
        setHoveredFaceId(null);
        drawFacesOnCanvas(detectedFaces);
      }
      canvas.style.cursor = 'crosshair';
    }
  };

  const handleCanvasMouseUp = async (event) => {
    if (!isDrawingRef.current || !drawStartRef.current) return;
    const { x, y } = getCanvasCoords(event);
    const startX = drawStartRef.current.x;
    const startY = drawStartRef.current.y;
    const dx = x - startX;
    const dy = y - startY;
    const size = Math.max(Math.abs(dx), Math.abs(dy));

    isDrawingRef.current = false;
    drawStartRef.current = null;
    setIsDrawing(false);
    setDrawStart(null);
    setDrawPreview(null);

    if (size < 10) {
      drawFacesOnCanvas(detectedFaces);
      return;
    }

    const squareX = Math.round(dx >= 0 ? startX : startX - size);
    const squareY = Math.round(dy >= 0 ? startY : startY - size);
    const squareSize = Math.round(size);

    if (!selectedImageForTagging || !catalogId) return;

    try {
      // Get the displayed image dimensions
      const imageWidth = imageRefTagging.current?.naturalWidth;
      const imageHeight = imageRefTagging.current?.naturalHeight;

      const response = await faceDetectionAPI.createManualFace(
        catalogId,
        selectedImageForTagging.id,
        {
          x: squareX,
          y: squareY,
          w: squareSize,
          h: squareSize,
          image_width: imageWidth,
          image_height: imageHeight
        }
      );

      const newFace = {
        id: response.data.face_id,
        x: squareX,
        y: squareY,
        w: squareSize,
        h: squareSize,
        confidence: null,
        person_id: null,
        person_name: null,
        source: 'Manual',
        image_width: imageWidth,
        image_height: imageHeight
      };

      const updatedFaces = [...detectedFaces, newFace];
      setDetectedFaces(updatedFaces);
      drawFacesOnCanvas(updatedFaces);
      setSelectedFace(newFace);
      // Automatically open the tag modal when a new face area is drawn
      setShowTagModal(true);
    } catch (err) {
      console.error('Failed to create tagging area:', err);
      drawFacesOnCanvas(detectedFaces);
    }
  };

  const handleCanvasMouseLeave = () => {
    if (isDrawingRef.current) {
      isDrawingRef.current = false;
      drawStartRef.current = null;
      setIsDrawing(false);
      setDrawStart(null);
      setDrawPreview(null);
      drawFacesOnCanvas(detectedFaces);
    }
  };

  const handleTagFace = async (personId) => {
    if (!selectedFace) return;
    
    try {
      await faceDetectionAPI.tagFace(catalogId, selectedFace.id, personId);
      const updatedFaces = detectedFaces.map(f => {
        if (f.id === selectedFace.id) {
          const person = people.find(p => p.id === personId);
          return { ...f, person_id: personId, person_name: person?.name || null };
        }
        return f;
      });
      
      setDetectedFaces(updatedFaces);
      drawFacesOnCanvas(updatedFaces);
      setShowTagModal(false);
      setSelectedFace(null);
    } catch (err) {
      console.error('Failed to tag face:', err);
    }
  };

  const handleRemoveTag = async (face) => {
    if (!face || !face.person_id) return;
    
    try {
      await faceDetectionAPI.untagFace(catalogId, face.id);
      const updatedFaces = detectedFaces.map(f => {
        if (f.id === face.id) {
          return { ...f, person_id: null, person_name: null };
        }
        return f;
      });
      
      setDetectedFaces(updatedFaces);
      drawFacesOnCanvas(updatedFaces);
      setSelectedFace(null);
    } catch (err) {
      console.error('Failed to remove tag:', err);
    }
  };

  const handleDeleteArea = async (face) => {
    if (!face) return;
    
    try {
      await faceDetectionAPI.deleteFace(catalogId, face.id);
      const updatedFaces = detectedFaces.filter(f => f.id !== face.id);
      setDetectedFaces(updatedFaces);
      drawFacesOnCanvas(updatedFaces);
      setSelectedFace(null);
    } catch (err) {
      console.error('Failed to delete area:', err);
    }
  };

  const handleAddNewPerson = async () => {
    if (!newPersonName.trim()) return;
    
    try {
      const response = await api.post(`/catalogs/${catalogId}/people`, {
        name: newPersonName.trim()
      });
      
      const newPerson = response.data;
      
      // Reload people list from backend to ensure state is properly updated
      const peopleResponse = await api.get(`/catalogs/${catalogId}/people`);
      setPeople(peopleResponse.data || []);
      
      if (selectedFace && newPerson.id) {
        const updatedFaces = detectedFaces.map(f => {
          if (f.id === selectedFace.id) {
            return { ...f, person_id: newPerson.id, person_name: newPersonName.trim() };
          }
          return f;
        });
        
        await faceDetectionAPI.tagFace(catalogId, selectedFace.id, newPerson.id);
        setDetectedFaces(updatedFaces);
        drawFacesOnCanvas(updatedFaces);
        setSelectedFace(null);
        setShowTagModal(false);
      }
      
      setShowAddPersonModal(false);
      setNewPersonName('');
      
      // If no face is selected, set flag to reopen the tag modal after people state updates
      if (!selectedFace) {
        setShouldReopenTagModal(true);
      }
    } catch (err) {
      console.error('Failed to add person:', err);
    }
  };

  const handleImageLoadTagging = async () => {
    if (selectedImageForTagging) {
      await loadExistingFaces(selectedImageForTagging.id);
      // Load description if available
      setImageDescription(selectedImageForTagging.description || '');
    }
  };

  const handleSaveDescription = async () => {
    if (!selectedImageForTagging) return;
    
    setSavingDescription(true);
    try {
      await api.put(
        `/catalogs/${catalogId}/files/${selectedImageForTagging.id}/description`,
        { description: imageDescription }
      );
      
      // Update the file in displayFiles
      setDisplayFiles(prev => prev.map(f =>
        f.id === selectedImageForTagging.id
          ? { ...f, description: imageDescription }
          : f
      ));
      
      // Update selectedImageForTagging
      setSelectedImageForTagging(prev => ({
        ...prev,
        description: imageDescription
      }));
      
      setIsEditingDescription(false);
    } catch (err) {
      console.error('Failed to save description:', err);
      alert('Failed to save description');
    } finally {
      setSavingDescription(false);
    }
  };

  const handleGenerateDescription = async () => {
    if (!selectedImageForTagging) return;
    
    setGeneratingDescription(true);
    try {
      // Get people names from detected faces
      const peopleNames = detectedFaces
        .filter(f => f.person_name)
        .map(f => f.person_name);
      
      // Get the image path - use alternative_file_path if available (for HEIC files),
      // otherwise use file_path
      const imagePath = selectedImageForTagging.alternative_file_path || selectedImageForTagging.file_path;
      
      if (!imagePath) {
        throw new Error('Image path not available');
      }
      
      // Get location name from event locations (use first location if multiple exist)
      const locationName = eventLocations.length > 0 ? eventLocations[0].name : null;
      
      console.log('Generating description for image:', {
        file_name: selectedImageForTagging.file_name,
        file_path: selectedImageForTagging.file_path,
        alternative_file_path: selectedImageForTagging.alternative_file_path,
        using_path: imagePath,
        people_names: peopleNames,
        location_name: locationName
      });
      
      // Call vision backend to generate description
      const response = await fetch(`${VISION_HOST}/describe`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
        },
        body: JSON.stringify({
          image_path: imagePath,
          people_names: peopleNames.length > 0 ? peopleNames : undefined,
          location_name: locationName
        })
      });
      
      const data = await response.json();
      
      if (data.success && data.description) {
        setImageDescription(data.description);
        setIsEditingDescription(true);
      } else {
        throw new Error(data.error || 'Failed to generate description');
      }
    } catch (err) {
      console.error('Failed to generate description:', err);
      const errorMsg = err.message || 'Unknown error';
      alert(`Failed to generate description: ${errorMsg}\n\nMake sure the vision backend is running and the image file is accessible.\n\nImage: ${selectedImageForTagging?.file_name || 'unknown'}\nPath: ${selectedImageForTagging?.alternative_file_path || selectedImageForTagging?.file_path || 'unknown'}`);
    } finally {
      setGeneratingDescription(false);
    }
  };

  useEffect(() => {
    if (detectedFaces.length > 0) {
      drawFacesOnCanvas(detectedFaces);
    }
  }, [hoveredFaceId, selectedFace, hoveredRemoveTagId]);
  */

  // Compute the gallery body height so it fills the remaining viewport space,
  // then derive how many fixed-size cards fit (no stretching).
  useEffect(() => {
    const TOP_NAV_H = 60;    // .top-menu min-height
    const CONTAINER_PT = 24; // CContainer py-4 top padding (Bootstrap rem at 16px base)
    const CONTAINER_PB = 24; // CContainer py-4 bottom padding
    const CARD_HEADER_H = 57; // gallery CCardHeader ~57px
    const CARD_BODY_P = 24;  // CCardBody default padding top+bottom (12px each)
    const CARD_W = 160;
    const CARD_H = 206;      // image 160 + label ~34 + border/padding ~12
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

      // cols from the gallery body element width (available after it renders)
      const bodyEl = galleryBodyRef.current;
      const bodyW = bodyEl ? bodyEl.clientWidth : window.innerWidth - 340; // 280 sidebar + 60 padding
      const cols = Math.max(1, Math.floor((bodyW + GAP) / (CARD_W + GAP)));
      const rows = Math.max(1, Math.floor((Math.max(CARD_H, avail) + GAP) / (CARD_H + GAP)) - 1);
      // Snap body height to exactly what the rows need — no leftover space
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

  // Reset to first page when displayFiles changes
  useEffect(() => {
    setPage(0);
  }, [displayFiles]);

  // ─── Preview overlay ──────────────────────────────────────────────────
  //
  // A plain click on a tile shows the photo or video here rather than leaving
  // the event. Opening the dedicated page is the "Open" control on each tile.

  const openPreview = useCallback((file) => {
    const idx = displayFiles.findIndex((x) => x.id === file.id);
    setPreviewIndex(idx === -1 ? null : idx);
  }, [displayFiles]);

  const closePreview = useCallback(() => setPreviewIndex(null), []);

  const stepPreview = useCallback((delta) => {
    setPreviewIndex((i) => {
      if (i === null || displayFiles.length === 0) return i;
      // Clamped, not wrapped: reaching the end of an event and silently
      // starting again reads as "the arrows stopped working".
      const next = i + delta;
      return next < 0 || next >= displayFiles.length ? i : next;
    });
  }, [displayFiles.length]);

  const previewFile = previewIndex === null ? null : displayFiles[previewIndex];

  // Escape closes, arrows step. Bound only while the overlay is open, so the
  // page keeps its own key handling the rest of the time.
  useEffect(() => {
    if (previewIndex === null) return undefined;
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); closePreview(); }
      else if (e.key === 'ArrowRight') { e.preventDefault(); stepPreview(1); }
      else if (e.key === 'ArrowLeft') { e.preventDefault(); stepPreview(-1); }
    };
    window.addEventListener('keydown', onKey);
    // The page behind must not scroll while a full-screen overlay is up.
    const prevOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    return () => {
      window.removeEventListener('keydown', onKey);
      document.body.style.overflow = prevOverflow;
    };
  }, [previewIndex, closePreview, stepPreview]);

  const totalPages = Math.ceil(displayFiles.length / pageSize);
  const pageFiles = displayFiles.slice(page * pageSize, (page + 1) * pageSize);

  // ─── Date batch banner (mass-edit-event-photo-dates-spec.md section 4.5) ──
  const REBUILD_PROBLEMS = {
    failed: 'Create Events failed',
    cancelled: 'Create Events was cancelled',
    stopped: 'Create Events was stopped',
    no_create_events_workflow: 'this catalog has no Create Events workflow',
  };
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  const renderBatchBanner = () => {
    if (batchId) {
      const b = batch;
      let text;
      if (!b || b.status === 'running') {
        text = b && b.total
          ? `Updating the dates of ${plural(b.total, 'photo')} (${b.done || 0}/${b.total})… `
            + 'Events will be rebuilt when this finishes.'
          : 'Updating the dates of the photos… Events will be rebuilt when this finishes.';
      } else if (b.status === 'rebuilding') {
        text = 'Rebuilding events…';
      } else {
        text = 'Finding where the photos went…';
      }
      return (
        <CAlert color="info" className="d-flex align-items-center gap-2" data-testid="date-batch-banner">
          <CSpinner size="sm" />
          <span>{text}</span>
        </CAlert>
      );
    }
    if (!batchResult) return null;

    const r = batchResult;
    const failed = r.failed || [];
    const rebuildProblem = REBUILD_PROBLEMS[r.rebuildStatus] || null;
    const ok = r.status === 'completed' && failed.length === 0 && !rebuildProblem;
    // The failed files may have left this page's list; their names are shown
    // when they are still here, the id always.
    const nameOf = (id) => displayFiles.find((f) => f.id === id)?.file_name;
    return (
      <CAlert
        color={ok ? 'success' : 'warning'} dismissible
        onClose={() => setBatchResult(null)} data-testid="date-batch-banner"
      >
        {r.status === 'interrupted' && (
          <div>
            The date change was interrupted after {r.done} of {plural(r.total, 'photo')}.
            {' '}The others keep their old dates.
          </div>
        )}
        {r.status === 'failed' && (
          <div>The date change could not be completed.</div>
        )}
        {r.status === 'completed' && (
          <div>
            Done.
            {r.target && r.moved > 0 && (
              <>
                {' '}{plural(r.moved, 'photo')} moved to{' '}
                <Link to={`/events/${catalogId}/${r.target.id}`}><strong>{r.target.name}</strong></Link>.
              </>
            )}
          </div>
        )}
        {rebuildProblem && (
          <div>Events were not rebuilt: {rebuildProblem}.</div>
        )}
        {failed.length > 0 && (
          <div>
            {plural(failed.length, 'photo')} could not be changed.{' '}
            <button
              type="button"
              onClick={() => setShowBatchFailures((v) => !v)}
              style={{
                background: 'none', border: 'none', padding: 0, color: 'inherit',
                textDecoration: 'underline', cursor: 'pointer',
              }}
            >
              {showBatchFailures ? 'Hide details' : 'Details'}
            </button>
            {showBatchFailures && (
              <ul className="mb-0 mt-1" style={{ fontSize: '0.82rem' }}>
                {failed.map((f) => (
                  <li key={f.file_id}>
                    #{f.file_id}{nameOf(f.file_id) ? ` ${nameOf(f.file_id)}` : ''}: {f.error}
                    {f.message ? ` — ${f.message}` : ''}
                  </li>
                ))}
              </ul>
            )}
          </div>
        )}
      </CAlert>
    );
  };

  // ─── Top row ──
  // Details, Date, whatever location cards are open, and Merge, in ONE row of
  // equal columns: each CCol is `md` with no number, so Bootstrap shares the
  // row evenly however many cards there are (three or four). Below md they
  // stack in that order.
  //
  // Merge is left out while a location is being added or edited, as it already
  // is while the event is edited: it has nothing to do with that task, and
  // beside the form and the map it would make five cards and leave the map too
  // narrow to use.
  const showMergeCard = !isEditing && !isAddingLocation && !isEditingLocation;
  const showAddLocationCard = !isEditing && eventLocations.length === 0
    && !isAddingLocation && !isEditingLocation;
  const showMapPanel = mapPanelVisible && isAddingLocation;

  if (loading) {
    return (
      <CContainer fluid className="px-4 py-4">
        <div className="text-center py-5">
          <CSpinner color="primary" />
        </div>
      </CContainer>
    );
  }

  // The event is gone and a batch that replaced it is still running: show
  // its progress until it lands on the successor.
  if (!event && batchId) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CButton color="link" onClick={() => navigate('/events')} className="mb-3">
          <CIcon icon={cilArrowLeft} className="me-2" />
          Back to Events
        </CButton>
        {renderBatchBanner()}
      </CContainer>
    );
  }

  if (error && !event) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CButton color="link" onClick={() => navigate('/events')} className="mb-3">
          <CIcon icon={cilArrowLeft} className="me-2" />
          Back to Events
        </CButton>
        <CAlert color="danger">{error}</CAlert>
      </CContainer>
    );
  }

  // No event and no error: a finished batch is redirecting to its successor.
  if (!event) {
    return (
      <CContainer fluid className="px-4 py-4">
        <div className="text-center py-5">
          <CSpinner color="primary" />
        </div>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="px-4 py-4">
      <div ref={topSectionRef}>
      {/* Back button */}
      <CButton color="link" onClick={() => navigate('/events')} className="mb-3">
        <CIcon icon={cilArrowLeft} className="me-2" />
        Back to Events
      </CButton>

      {renderBatchBanner()}

      {/* Top row: Event Info Card + Location Card (side by side when adding/editing location OR when no locations in view mode) */}
      <CRow className="mb-4 g-3">
        {/* Event Info Card */}
        <CCol md>
          <CCard className="h-100">
            <CCardHeader className="d-flex justify-content-between align-items-center">
              <div className="d-flex align-items-center gap-2">
                <CIcon icon={cilCalendar} style={{ width: 18, height: 18, color: '#1a73e8' }} />
                <strong style={{ fontSize: '1rem', color: '#212121' }}>{event?.name}</strong>
                {event?.type === 'Manual' && (
                  <CBadge color="success" style={{ fontSize: '0.7rem' }}>✓ Verified</CBadge>
                )}
              </div>
              <div className="d-flex gap-2">
                <CButton
                  color="primary"
                  size="sm"
                  onClick={() => setIsEditing((v) => !v)}
                >
                  {isEditing
                    ? <><CIcon icon={cilX} style={{ width: 13, height: 13 }} className="me-1" />Cancel</>
                    : <><CIcon icon={cilPencil} style={{ width: 13, height: 13 }} className="me-1" />Edit</>
                  }
                </CButton>
              </div>
            </CCardHeader>

            <CCardBody>
              {!isEditing ? (
                /* ── View mode ── */
                <div>
                  <div className="mb-2">
                    <span className="text-muted" style={{ fontSize: '0.82rem' }}>Start:</span>{' '}
                    <span style={{ fontSize: '0.9rem' }}>{formatDateTime(event?.start_time)}</span>
                  </div>
                  {event?.end_time && event.end_time !== event.start_time && (
                    <div className="mb-2">
                      <span className="text-muted" style={{ fontSize: '0.82rem' }}>End:</span>{' '}
                      <span style={{ fontSize: '0.9rem' }}>{formatDateTime(event.end_time)}</span>
                    </div>
                  )}
                  {event?.description && (
                    <div className="mb-2">
                      <span className="text-muted" style={{ fontSize: '0.82rem' }}>Description:</span>{' '}
                      <span style={{ fontSize: '0.9rem' }}>{event.description}</span>
                    </div>
                  )}
                  {(event?.latitude != null || event?.longitude != null) && (
                    <div className="mb-2">
                      <CIcon icon={cilLocationPin} style={{ width: 13, height: 13, marginRight: 4, color: '#888' }} />
                      <span style={{ fontSize: '0.9rem', color: '#555' }}>
                        {event.latitude?.toFixed(6)}, {event.longitude?.toFixed(6)}
                      </span>
                    </div>
                  )}
                  {displayFiles.length > 0 && (
                    <div className="mt-2 text-muted" style={{ fontSize: '0.82rem' }}>
                      {displayFiles.length} photo{displayFiles.length !== 1 ? 's' : ''}
                    </div>
                  )}

                  {/* Linked Locations - shown at bottom when they exist */}
                  {eventLocations.length > 0 && (
                    <div className="mt-4 pt-3" style={{ borderTop: '1px solid #e9ecef' }}>
                      <div className="fw-semibold mb-2" style={{ fontSize: '0.82rem', color: '#555' }}>
                        <CIcon icon={cilLocationPin} style={{ width: 13, height: 13, marginRight: 4 }} />
                        Locations
                      </div>
                      {locationsLoading ? (
                        <CSpinner size="sm" />
                      ) : (
                        <div className="d-flex flex-column gap-2">
                          {eventLocations.map((loc) => (
                            <div
                              key={loc.id}
                              className="d-flex align-items-start justify-content-between p-2 rounded"
                              style={{
                                background: editingLocationId === loc.id ? '#f0f4ff' : '#f8f9fa',
                                border: `1px solid ${editingLocationId === loc.id ? '#b8c8f0' : '#e9ecef'}`,
                              }}
                            >
                              <div>
                                <div style={{ fontSize: '0.88rem', fontWeight: 600 }}>{loc.name}</div>
                                {loc.description && (
                                  <div style={{ fontSize: '0.78rem', color: '#666' }}>{loc.description}</div>
                                )}
                                {(loc.lat != null || loc.long != null) && (
                                  <div style={{ fontSize: '0.78rem', color: '#888' }}>
                                    {loc.lat?.toFixed(6)}, {loc.long?.toFixed(6)}
                                  </div>
                                )}
                              </div>
                              <div className="d-flex gap-1">
                                <CButton
                                  color="primary"
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => handleOpenEditLocation(loc)}
                                  title="Edit location"
                                >
                                  <CIcon icon={cilPencil} style={{ width: 13, height: 13 }} />
                                </CButton>
                                <CButton
                                  color="danger"
                                  variant="ghost"
                                  size="sm"
                                  onClick={() => handleRemoveLocation(loc.id)}
                                  disabled={removingLocationId === loc.id}
                                  title="Remove location"
                                >
                                  {removingLocationId === loc.id
                                    ? <CSpinner size="sm" />
                                    : <CIcon icon={cilTrash} style={{ width: 13, height: 13 }} />
                                  }
                                </CButton>
                              </div>
                            </div>
                          ))}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              ) : (
                /* ── Edit mode ── */
                <CForm>
                  {saveError && (
                    <CAlert color="danger" className="py-2 mb-3" style={{ fontSize: '0.85rem' }}>
                      {saveError}
                    </CAlert>
                  )}

                  {/* Name */}
                  <div className="mb-3">
                    <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Name *</label>
                    <CFormInput
                      value={form.name}
                      onChange={handleChange('name')}
                      placeholder="Event name"
                    />
                  </div>

                  {/* Start / End */}
                  <CRow className="mb-3 g-3">
                    <CCol md={6}>
                      <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Start *</label>
                      <CFormInput
                        type="datetime-local"
                        value={form.start_time}
                        onChange={handleChange('start_time')}
                      />
                    </CCol>
                    <CCol md={6}>
                      <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>End</label>
                      <CFormInput
                        type="datetime-local"
                        value={form.end_time}
                        onChange={handleChange('end_time')}
                      />
                    </CCol>
                  </CRow>

                  {/* Description */}
                  <div className="mb-3">
                    <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Description</label>
                    <CFormTextarea
                      rows={3}
                      value={form.description}
                      onChange={handleChange('description')}
                      placeholder="Optional description"
                    />
                  </div>

                  {/* Lat / Lng */}
                  <CRow className="mb-3 g-3">
                    <CCol md={6}>
                      <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Latitude</label>
                      <CFormInput
                        type="number"
                        step="any"
                        value={form.latitude}
                        onChange={handleChange('latitude')}
                        placeholder="e.g. 40.7128"
                      />
                    </CCol>
                    <CCol md={6}>
                      <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Longitude</label>
                      <CFormInput
                        type="number"
                        step="any"
                        value={form.longitude}
                        onChange={handleChange('longitude')}
                        placeholder="e.g. -74.0060"
                      />
                    </CCol>
                  </CRow>

                  {/* Locations CreatableSelect */}
                  <div className="mb-3">
                    <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>
                      <CIcon icon={cilLocationPin} style={{ width: 13, height: 13, marginRight: 4 }} />
                      Add Location
                    </label>
                    <CreatableSelect
                      isClearable
                      isLoading={locationSelectLoading}
                      options={allLocations
                        .filter((loc) => !eventLocations.some((el) => el.id === loc.id))
                        .map((loc) => ({ value: loc.id, label: loc.name }))}
                      onChange={handleLocationSelectChange}
                      value={null}
                      placeholder="Select existing or type to create new…"
                      formatCreateLabel={(inputValue) => `Create "${inputValue}"`}
                      styles={{
                        control: (base) => ({ ...base, fontSize: '0.88rem', minHeight: 36 }),
                        option: (base) => ({ ...base, fontSize: '0.88rem' }),
                        placeholder: (base) => ({ ...base, fontSize: '0.88rem', color: '#aaa' }),
                      }}
                    />
                    {/* Currently linked locations (shown in edit mode too) */}
                    {eventLocations.length > 0 && (
                      <div className="d-flex flex-column gap-1 mt-2">
                        {eventLocations.map((loc) => (
                          <div
                            key={loc.id}
                            className="d-flex align-items-center justify-content-between px-2 py-1 rounded"
                            style={{ background: '#f0faf4', border: '1px solid #c3e6cb', fontSize: '0.82rem' }}
                          >
                            <span style={{ fontWeight: 600 }}>{loc.name}</span>
                            <CButton
                              color="danger"
                              variant="ghost"
                              size="sm"
                              onClick={() => handleRemoveLocation(loc.id)}
                              disabled={removingLocationId === loc.id}
                              title="Remove location"
                              style={{ padding: '0 4px' }}
                            >
                              {removingLocationId === loc.id
                                ? <CSpinner size="sm" />
                                : <CIcon icon={cilX} style={{ width: 12, height: 12 }} />
                              }
                            </CButton>
                          </div>
                        ))}
                      </div>
                    )}
                  </div>

                  <div className="d-flex gap-2">
                    <CButton color="primary" onClick={handleSave} disabled={saving}>
                      {saving
                        ? <><CSpinner size="sm" className="me-1" />Saving…</>
                        : <><CIcon icon={cilCheck} style={{ width: 14, height: 14 }} className="me-1" />Save Changes</>
                      }
                    </CButton>
                    <CButton color="secondary" variant="outline" onClick={() => setIsEditing(false)} disabled={saving}>
                      Cancel
                    </CButton>
                  </div>
                </CForm>
              )}
            </CCardBody>
          </CCard>
        </CCol>

        {/* Date Card - directly after Details, so it sits between Details and
            any location card (section 3.1). */}
        <CCol md>
          <EventDateCard
            event={event}
            onModify={() => navigate(`/events/${catalogId}/${eventId}/dates`)}
          />
        </CCol>

        {/* Add Location Card - shown in view mode when no locations exist */}
        {showAddLocationCard && (
          <CCol md>
            <CCard className="h-100" style={{ borderColor: '#198754' }}>
              <CCardHeader
                className="d-flex justify-content-between align-items-center"
                style={{ background: '#f0faf4', borderBottomColor: '#198754' }}
              >
                <div className="d-flex align-items-center gap-2">
                  <CIcon icon={cilLocationPin} style={{ width: 18, height: 18, color: '#198754' }} />
                  <strong style={{ fontSize: '1rem', color: '#198754' }}>Add Location</strong>
                </div>
              </CCardHeader>

              <CCardBody>
                <p className="text-muted mb-3" style={{ fontSize: '0.85rem' }}>
                  This event doesn't have any locations yet. Select an existing location or create a new one.
                </p>
                
                <div className="mb-3">
                  <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>
                    <CIcon icon={cilLocationPin} style={{ width: 13, height: 13, marginRight: 4 }} />
                    Location
                  </label>
                  <CreatableSelect
                    isClearable
                    isLoading={locationSelectLoading}
                    options={allLocations.map((loc) => ({ value: loc.id, label: loc.name }))}
                    onChange={handleLocationSelectChange}
                    value={null}
                    placeholder="Select existing or type to create new…"
                    formatCreateLabel={(inputValue) => `Create "${inputValue}"`}
                    styles={{
                      control: (base) => ({ ...base, fontSize: '0.88rem', minHeight: 36 }),
                      option: (base) => ({ ...base, fontSize: '0.88rem' }),
                      placeholder: (base) => ({ ...base, fontSize: '0.88rem', color: '#aaa' }),
                    }}
                  />
                </div>
              </CCardBody>
            </CCard>
          </CCol>
        )}

        {/* Add Location Card - shown when adding a new location */}
        {isAddingLocation && (
          <CCol md>
            <CCard className="h-100" style={{ borderColor: '#198754' }}>
              <CCardHeader
                className="d-flex justify-content-between align-items-center"
                style={{ background: '#f0faf4', borderBottomColor: '#198754' }}
              >
                <div className="d-flex align-items-center gap-2">
                  <CIcon icon={cilLocationPin} style={{ width: 18, height: 18, color: '#198754' }} />
                  <strong style={{ fontSize: '1rem', color: '#198754' }}>Add Location</strong>
                </div>
                <CButton
                  color="secondary"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setIsAddingLocation(false);
                    setLocationForm({ name: '', lat: '', long: '', description: '' });
                  }}
                  title="Cancel"
                >
                  <CIcon icon={cilX} style={{ width: 13, height: 13 }} />
                </CButton>
              </CCardHeader>

              <CCardBody>
                <CForm>
                  {locationSaveError && (
                    <CAlert color="danger" className="py-2 mb-3" style={{ fontSize: '0.85rem' }}>
                      {locationSaveError}
                    </CAlert>
                  )}

                  {/* Name */}
                  <div className="mb-3">
                    <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Name *</label>
                    <CFormInput
                      value={locationForm.name}
                      onChange={handleLocationChange('name')}
                      placeholder="e.g. Central Park"
                    />
                  </div>

                  {/* Description */}
                  <div className="mb-3">
                    <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Description</label>
                    <CFormTextarea
                      rows={3}
                      value={locationForm.description}
                      onChange={handleLocationChange('description')}
                      placeholder="Optional description"
                    />
                  </div>

                  {/* Lat / Long */}
                  <CRow className="mb-3 g-3">
                    <CCol md={6}>
                      <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Latitude</label>
                      <CFormInput
                        type="text"
                        value={locationForm.lat}
                        onChange={handleLocationChange('lat')}
                        placeholder="e.g. 40.7851"
                      />
                    </CCol>
                    <CCol md={6}>
                      <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Longitude</label>
                      <CFormInput
                        type="text"
                        value={locationForm.long}
                        onChange={handleLocationChange('long')}
                        placeholder="e.g. -73.9683"
                      />
                    </CCol>
                  </CRow>

                  <div className="d-flex gap-2 flex-wrap">
                    <CButton
                      color="info"
                      size="sm"
                      onClick={mapPanelVisible ? handleCloseMapPanel : handleUseMap}
                      title="Open map to browse and drop a pin to set coordinates"
                    >
                      <CIcon icon={cilMap} style={{ width: 13, height: 13 }} className="me-1" />
                      {mapPanelVisible ? 'Close Map' : 'Use Map'}
                    </CButton>
                    <div className="flex-grow-1"></div>
                    <CButton color="success" onClick={handleSaveLocation} disabled={locationSaving}>
                      {locationSaving
                        ? <><CSpinner size="sm" className="me-1" />Saving…</>
                        : <><CIcon icon={cilCheck} style={{ width: 14, height: 14 }} className="me-1" />Save Location</>
                      }
                    </CButton>
                    <CButton
                      color="secondary"
                      variant="outline"
                      onClick={() => {
                        setIsAddingLocation(false);
                        setLocationForm({ name: '', lat: '', long: '', description: '' });
                        setMapPanelVisible(false);
                        setPinPosition(null);
                      }}
                      disabled={locationSaving}
                    >
                      Cancel
                    </CButton>
                  </div>
                </CForm>
              </CCardBody>
            </CCard>
          </CCol>
        )}

        {/* Map Panel - shown when "Use Map" is clicked in Add Location */}
        {showMapPanel && (
          <CCol md>
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
                <CButton
                  color="secondary"
                  variant="ghost"
                  size="sm"
                  onClick={handleCloseMapPanel}
                  title="Close map panel"
                >
                  <CIcon icon={cilX} style={{ width: 13, height: 13 }} />
                </CButton>
              </CCardHeader>
              <CCardBody className="p-0" style={{ minHeight: '400px', position: 'relative' }}>
                <div
                  className="text-muted small px-3 py-2"
                  style={{ backgroundColor: '#f0f9ff', borderBottom: '1px solid #bee3f8' }}
                >
                  Click anywhere on the map to drop a pin and set the coordinates.
                </div>
                <MapContainer
                  key={`map-${mapPanelVisible}`}
                  center={getMapCenter()}
                  zoom={getMapZoom()}
                  style={{ height: '420px', width: '100%', cursor: 'crosshair' }}
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
        )}

        {/* Edit Location Card */}
        {isEditingLocation && (
          <CCol md>
            <CCard className="h-100" style={{ borderColor: '#198754' }}>
              <CCardHeader
                className="d-flex justify-content-between align-items-center"
                style={{ background: '#f0faf4', borderBottomColor: '#198754' }}
              >
                <div className="d-flex align-items-center gap-2">
                  <CIcon icon={cilLocationPin} style={{ width: 18, height: 18, color: '#198754' }} />
                  <strong style={{ fontSize: '1rem', color: '#198754' }}>Edit Location</strong>
                </div>
                <CButton
                  color="secondary"
                  variant="ghost"
                  size="sm"
                  onClick={() => {
                    setIsEditingLocation(false);
                    setEditingLocationId(null);
                    setEditLocationForm({ name: '', lat: '', long: '', description: '' });
                  }}
                  title="Cancel"
                >
                  <CIcon icon={cilX} style={{ width: 13, height: 13 }} />
                </CButton>
              </CCardHeader>

              <CCardBody>
                <CForm>
                  {editLocationSaveError && (
                    <CAlert color="danger" className="py-2 mb-3" style={{ fontSize: '0.85rem' }}>
                      {editLocationSaveError}
                    </CAlert>
                  )}

                  {/* Name */}
                  <div className="mb-3">
                    <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Name *</label>
                    <CFormInput
                      value={editLocationForm.name}
                      onChange={handleEditLocationChange('name')}
                      placeholder="e.g. Central Park"
                    />
                  </div>

                  {/* Description */}
                  <div className="mb-3">
                    <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Description</label>
                    <CFormTextarea
                      rows={3}
                      value={editLocationForm.description}
                      onChange={handleEditLocationChange('description')}
                      placeholder="Optional description"
                    />
                  </div>

                  {/* Lat / Long */}
                  <CRow className="mb-3 g-3">
                    <CCol md={6}>
                      <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Latitude</label>
                      <CFormInput
                        type="number"
                        step="any"
                        value={editLocationForm.lat}
                        onChange={handleEditLocationChange('lat')}
                        placeholder="e.g. 40.7851"
                      />
                    </CCol>
                    <CCol md={6}>
                      <label className="form-label fw-semibold" style={{ fontSize: '0.82rem' }}>Longitude</label>
                      <CFormInput
                        type="number"
                        step="any"
                        value={editLocationForm.long}
                        onChange={handleEditLocationChange('long')}
                        placeholder="e.g. -73.9683"
                      />
                    </CCol>
                  </CRow>

                  <div className="d-flex gap-2">
                    <CButton color="success" onClick={handleSaveEditLocation} disabled={editLocationSaving}>
                      {editLocationSaving
                        ? <><CSpinner size="sm" className="me-1" />Saving…</>
                        : <><CIcon icon={cilCheck} style={{ width: 14, height: 14 }} className="me-1" />Save Changes</>
                      }
                    </CButton>
                    <CButton
                      color="secondary"
                      variant="outline"
                      onClick={() => {
                        setIsEditingLocation(false);
                        setEditingLocationId(null);
                        setEditLocationForm({ name: '', lat: '', long: '', description: '' });
                      }}
                      disabled={editLocationSaving}
                    >
                      Cancel
                    </CButton>
                  </div>
                </CForm>
              </CCardBody>
            </CCard>
          </CCol>
        )}

        {/* Merge into an event on the same day, the day before or the day
            after: the row's last card. Inside the measured top section, so the
            gallery below sizes itself around it. */}
        {showMergeCard && event && (
          <CCol md>
            <EventMergeCard
              catalogId={catalogId}
              event={event}
              onMerged={(result) => navigate(`/events/${catalogId}/${result.into_event_id}`)}
            />
          </CCol>
        )}
      </CRow>
      </div>{/* end topSectionRef */}

      {/* Photo Gallery Card */}
        <CCard>
          <CCardHeader className="d-flex align-items-center">
            <strong>Photos</strong>
            {displayFiles.length > 0 && (
              <CBadge color="secondary" className="ms-2">{displayFiles.length}</CBadge>
            )}
            {/* Pagination in header */}
            {!photosLoading && totalPages > 1 && (
              <div className="d-flex align-items-center gap-2 ms-auto">
                <CButton
                  color="secondary"
                  size="sm"
                  onClick={() => setPage((p) => p - 1)}
                  disabled={page === 0}
                >
                  ← Previous
                </CButton>
                <span style={{ fontSize: '0.85rem', color: '#666', whiteSpace: 'nowrap' }}>
                  Page {page + 1} of {totalPages}
                </span>
                <CButton
                  color="secondary"
                  size="sm"
                  onClick={() => setPage((p) => p + 1)}
                  disabled={page === totalPages - 1}
                >
                  Next →
                </CButton>
              </div>
            )}
          </CCardHeader>
          <CCardBody ref={galleryBodyRef} style={{ height: galleryBodyH, overflow: 'hidden' }}>
          {/* Loading */}
          {photosLoading && (
            <div className="text-center py-5">
              <CSpinner color="primary" />
              <div className="mt-2 text-muted" style={{ fontSize: '0.9rem' }}>Loading images…</div>
            </div>
          )}

          {/* Error */}
          {photosError && <CAlert color="danger">{photosError}</CAlert>}

          {/* One slot, overwritten on each cover click rather than appended. */}
          {coverMessage && (
            <CAlert
              color={coverMessage.color}
              dismissible
              onClose={() => setCoverMessage(null)}
              className="py-2"
            >
              {coverMessage.text}
            </CAlert>
          )}

          {/* Empty state */}
          {!photosLoading && !photosError && displayFiles.length === 0 && (
            <div className="text-center py-5 text-muted">
              <CIcon icon={cilImage} style={{ width: 48, height: 48, opacity: 0.25 }} />
              <div className="mt-3" style={{ fontSize: '1rem' }}>No images associated with this event.</div>
            </div>
          )}

          {/* Photo grid */}
          {!photosLoading && pageFiles.length > 0 && (
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px' }}>
              {pageFiles.map((f) => (
                <div
                  key={f.id}
                  style={{ width: 160, flexShrink: 0 }}
                >
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
                    // The plain click PREVIEWS, in an overlay. Opening the
                    // full Photos/Videos page is the "Open" control in the
                    // corner - browsing an event is mostly looking, and making
                    // the common action a page navigation meant leaving the
                    // event to see one picture and then coming back.
                    onClick={() => openPreview(f)}
                  >
                    <div
                      style={{
                        height: 160,
                        background: '#f0f0f0',
                        display: 'flex',
                        alignItems: 'center',
                        justifyContent: 'center',
                        position: 'relative',
                        overflow: 'hidden',
                      }}
                    >
                      {f.isVideo ? (
                        <>
                          {/* preload="none": with a poster there is nothing to
                              gain from fetching metadata, and "metadata" would
                              start a request per card on every page turn. */}
                          <video
                            src={f.videoUrl}
                            poster={f.thumbUrl}
                            preload="none"
                            muted
                            loop
                            playsInline
                            title={f.displayName}
                            style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                            onMouseEnter={(e) => { e.currentTarget.play().catch(() => {}); }}
                            onMouseLeave={(e) => {
                              e.currentTarget.pause();
                              e.currentTarget.currentTime = 0;
                            }}
                          />
                          {/* Hover-to-play is not discoverable on a touch
                              screen, so a video reads as a video at rest. */}
                          <div
                            style={{
                              position: 'absolute', bottom: 6, left: 6,
                              display: 'flex', alignItems: 'center', gap: 4,
                              background: 'rgba(0,0,0,0.6)', color: '#fff',
                              borderRadius: 4, padding: '1px 6px',
                              fontSize: '0.7rem', pointerEvents: 'none',
                            }}
                          >
                            <span style={{ fontSize: '0.75rem', lineHeight: 1 }}>▶</span>
                            Video
                          </div>
                        </>
                      ) : f.imageUrl ? (
                        <img
                          src={f.thumbUrl || f.imageUrl}
                          alt={f.displayName}
                          loading="lazy"
                          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                          title={f.displayName}
                        />
                      ) : (
                        <span style={{ color: '#aaa', fontSize: '0.8rem' }}>N/A</span>
                      )}
                      {/* Sets this photo as the event's cover. It stands where
                          the "Tag faces" badge used to: that badge had no
                          onClick and the only function that would have opened
                          the tagging modal was never called — tagging lives on
                          the Photos page now (spec section 2.4).

                          Not offered on videos yet. A video cover would be its
                          poster frame, which does exist, but a still is the
                          safer default (spec section 6). */}
                      {!f.isVideo && (
                        <button
                          type="button"
                          title={coverFileId === f.id
                            ? 'This is the event cover'
                            : 'Use as event cover'}
                          aria-label={coverFileId === f.id
                            ? 'This is the event cover'
                            : 'Use as event cover'}
                          aria-pressed={coverFileId === f.id}
                          onClick={(e) => {
                            // The card itself opens the preview; setting a
                            // cover must not also do that.
                            e.stopPropagation();
                            e.preventDefault();
                            handleSetCover(f.id);
                          }}
                          style={{
                            position: 'absolute',
                            top: 8,
                            // LEFT: the top-right corner now belongs to Open.
                            // Two icon buttons in one corner is a coin toss for
                            // the user; one per side is not.
                            left: 8,
                            background: 'rgba(0,0,0,0.55)',
                            border: 'none',
                            color: coverFileId === f.id ? '#ffc107' : 'rgba(255,255,255,0.85)',
                            padding: '4px 6px',
                            borderRadius: '4px',
                            lineHeight: 0,
                            cursor: 'pointer',
                            display: 'flex',
                            alignItems: 'center',
                          }}
                          onMouseEnter={(e) => { e.currentTarget.style.color = '#ffc107'; }}
                          onMouseLeave={(e) => {
                            e.currentTarget.style.color =
                              coverFileId === f.id ? '#ffc107' : 'rgba(255,255,255,0.85)';
                          }}
                        >
                          <CIcon icon={cilStar} style={{ width: 14, height: 14 }} />
                        </button>
                      )}
                      {/* Open — the old behaviour of the whole card, now an
                          explicit control. On every tile, photo and video
                          alike, and it is the ONLY thing here that leaves the
                          event. */}
                      <button
                        type="button"
                        title={f.isVideo ? 'Open this video' : 'Open this photo'}
                        aria-label={f.isVideo ? 'Open this video' : 'Open this photo'}
                        onClick={(e) => {
                          e.stopPropagation();
                          e.preventDefault();
                          navigate(f.isVideo
                            ? `/videos/${catalogId}/${f.id}`
                            : `/photos/${catalogId}/${f.id}`);
                        }}
                        style={{
                          position: 'absolute',
                          top: 8,
                          right: 8,
                          background: 'rgba(0,0,0,0.55)',
                          border: 'none',
                          color: 'rgba(255,255,255,0.85)',
                          padding: '4px 6px',
                          borderRadius: '4px',
                          lineHeight: 0,
                          cursor: 'pointer',
                          display: 'flex',
                          alignItems: 'center',
                        }}
                        onMouseEnter={(e) => { e.currentTarget.style.color = '#fff'; }}
                        onMouseLeave={(e) => { e.currentTarget.style.color = 'rgba(255,255,255,0.85)'; }}
                      >
                        <CIcon icon={cilExternalLink} style={{ width: 14, height: 14 }} />
                      </button>
                    </div>
                    <CCardBody className="p-2">
                      <small className="text-muted d-block text-truncate" title={f.displayName}>
                        {f.displayName}
                      </small>
                    </CCardBody>
                  </CCard>
                </div>
              ))}
            </div>
          )}
        </CCardBody>
      </CCard>

      {/* ─── Preview overlay ──────────────────────────────────────────────
          What a plain click on a tile now does. Deliberately not a CModal:
          a preview wants the media at full size on a dark ground, not a white
          panel with a header and a footer. */}
      {previewFile && (
        <div
          role="dialog"
          aria-modal="true"
          aria-label={previewFile.displayName}
          // Clicking the backdrop closes. The check keeps a click that
          // started on the image itself from counting as a backdrop click.
          onClick={(e) => { if (e.target === e.currentTarget) closePreview(); }}
          style={{
            position: 'fixed', inset: 0, zIndex: 2000,
            background: 'rgba(0,0,0,0.88)',
            display: 'flex', flexDirection: 'column',
            alignItems: 'center', justifyContent: 'center',
            padding: '48px 64px',
          }}
        >
          {/* Close */}
          <button
            type="button"
            title="Close (Esc)"
            aria-label="Close preview"
            onClick={closePreview}
            style={{
              position: 'absolute', top: 16, right: 16,
              background: 'rgba(255,255,255,0.12)', border: 'none',
              color: '#fff', borderRadius: 6, padding: '8px 10px',
              lineHeight: 0, cursor: 'pointer',
            }}
          >
            <CIcon icon={cilX} style={{ width: 18, height: 18 }} />
          </button>

          {/* Open the dedicated page — the same destination as the tile's
              corner control, repeated here so the overlay is not a dead end. */}
          <CButton
            color="light"
            size="sm"
            onClick={(e) => {
              e.stopPropagation();
              navigate(previewFile.isVideo
                ? `/videos/${catalogId}/${previewFile.id}`
                : `/photos/${catalogId}/${previewFile.id}`);
            }}
            style={{ position: 'absolute', top: 16, left: 16 }}
          >
            <CIcon icon={cilExternalLink} className="me-1" style={{ width: 14, height: 14 }} />
            Open {previewFile.isVideo ? 'video' : 'photo'} page
          </CButton>

          {/* Previous / next across the WHOLE event, not just this page. */}
          {previewIndex > 0 && (
            <button
              type="button"
              title="Previous (←)"
              aria-label="Previous"
              onClick={(e) => { e.stopPropagation(); stepPreview(-1); }}
              style={{
                position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)',
                background: 'rgba(255,255,255,0.12)', border: 'none', color: '#fff',
                borderRadius: 6, padding: '12px 8px', lineHeight: 0, cursor: 'pointer',
              }}
            >
              <CIcon icon={cilChevronLeft} style={{ width: 20, height: 20 }} />
            </button>
          )}
          {previewIndex < displayFiles.length - 1 && (
            <button
              type="button"
              title="Next (→)"
              aria-label="Next"
              onClick={(e) => { e.stopPropagation(); stepPreview(1); }}
              style={{
                position: 'absolute', right: 12, top: '50%', transform: 'translateY(-50%)',
                background: 'rgba(255,255,255,0.12)', border: 'none', color: '#fff',
                borderRadius: 6, padding: '12px 8px', lineHeight: 0, cursor: 'pointer',
              }}
            >
              <CIcon icon={cilChevronRight} style={{ width: 20, height: 20 }} />
            </button>
          )}

          {/* The media. key={id} so React REPLACES the element when the arrows
              move: mutating src on a playing <video> leaves the old frame up. */}
          {previewFile.isVideo ? (
            <video
              key={previewFile.id}
              src={previewFile.videoUrl}
              poster={previewFile.thumbUrl}
              controls
              autoPlay
              playsInline
              onClick={(e) => e.stopPropagation()}
              style={{ maxWidth: '100%', maxHeight: 'calc(100vh - 160px)', outline: 'none' }}
            />
          ) : previewFile.imageUrl ? (
            <img
              key={previewFile.id}
              src={previewFile.imageUrl}
              alt={previewFile.displayName}
              onClick={(e) => e.stopPropagation()}
              style={{ maxWidth: '100%', maxHeight: 'calc(100vh - 160px)', objectFit: 'contain' }}
            />
          ) : (
            <div style={{ color: '#bbb' }}>This file cannot be previewed.</div>
          )}

          {/* Caption */}
          <div
            onClick={(e) => e.stopPropagation()}
            style={{ marginTop: 12, color: '#ddd', textAlign: 'center', maxWidth: '80%' }}
          >
            <div style={{ fontSize: '0.9rem' }}>{previewFile.displayName}</div>
            {previewFile.description && (
              <div style={{ fontSize: '0.8rem', color: '#999', marginTop: 4 }}>
                {previewFile.description}
              </div>
            )}
            <div style={{ fontSize: '0.75rem', color: '#777', marginTop: 4 }}>
              {previewIndex + 1} of {displayFiles.length}
            </div>
          </div>
        </div>
      )}

      {/* ─── DEAD CODE — MARKED FOR DELETION ───────────────────────
          Unreachable since face tagging moved to the Photos page: the only
          entry point was handleOpenImageDetail(), which nothing calls, and the
          "Tag faces" overlay that suggested it was removed in
          event-cards-ui-spec.md section 2.4. Kept commented for one release so
          the change is reviewable. Delete wholesale — see the checklist item in
          event-cards-ui-spec.md section 8.

          A comment terminator inside the region is written with a space in it
          below, because a JSX comment is a block comment and the first real
          terminator would close this one early.
      {/* Image Detail View - Full Page * /}
      {showImageDetail && selectedImageForTagging && (
        <>
          {/* Top Card: Image Title and Description * /}
          <CCard className="mb-3">
            <CCardHeader className="d-flex justify-content-between align-items-center">
              <div>
                <strong>Image Details - {selectedImageForTagging.displayName}</strong>
              </div>
              <CButton
                color="secondary"
                size="sm"
                onClick={handleCloseImageDetail}
              >
                <CIcon icon={cilX} className="me-2" />
                Back to Gallery
              </CButton>
            </CCardHeader>
            <CCardBody>
              {/* Image Description Section * /}
              <div>
                <h6 className="mb-2">Description</h6>
                {isEditingDescription ? (
                  <>
                    <CFormTextarea
                      rows={4}
                      value={imageDescription}
                      onChange={(e) => setImageDescription(e.target.value)}
                      placeholder="Enter image description..."
                      className="mb-2"
                    />
                    <div className="d-flex gap-2">
                      <CButton
                        color="primary"
                        size="sm"
                        onClick={handleSaveDescription}
                        disabled={savingDescription}
                      >
                        {savingDescription ? (
                          <>
                            <CSpinner size="sm" className="me-1" />
                            Saving...
                          </>
                        ) : (
                          <>
                            <CIcon icon={cilCheck} className="me-1" />
                            Save
                          </>
                        )}
                      </CButton>
                      <CButton
                        color="secondary"
                        size="sm"
                        variant="outline"
                        onClick={() => {
                          setIsEditingDescription(false);
                          setImageDescription(selectedImageForTagging?.description || '');
                        }}
                        disabled={savingDescription}
                      >
                        Cancel
                      </CButton>
                    </div>
                  </>
                ) : (
                  <>
                    <div
                      className="mb-2 p-2"
                      style={{
                        minHeight: '60px',
                        backgroundColor: '#f8f9fa',
                        borderRadius: '4px',
                        fontSize: '0.9rem',
                        color: imageDescription ? '#212529' : '#6c757d'
                      }}
                    >
                      {imageDescription || 'No description yet'}
                    </div>
                    <div className="d-flex gap-2">
                      <CButton
                        color="primary"
                        size="sm"
                        variant="outline"
                        onClick={() => setIsEditingDescription(true)}
                      >
                        <CIcon icon={cilPencil} className="me-1" />
                        Edit
                      </CButton>
                      <CButton
                        color="success"
                        size="sm"
                        onClick={handleGenerateDescription}
                        disabled={generatingDescription}
                      >
                        {generatingDescription ? (
                          <>
                            <CSpinner size="sm" className="me-1" />
                            Generating...
                          </>
                        ) : (
                          <>✨ Generate with AI</>
                        )}
                      </CButton>
                    </div>
                  </>
                )}
              </div>
            </CCardBody>
          </CCard>

          {/* Bottom Card: Image and Face Tagging * /}
          <CCard>
            <CCardBody>
              <CRow className="g-4">
                {/* Main image area with canvas for face tagging * /}
                <CCol md={8}>
                <div className="mb-3 d-flex justify-content-between align-items-center">
                  <h5 className="mb-0"><strong>Image and Face Tagging</strong></h5>
                  <CButton
                    color="primary"
                    onClick={handleDetectFaces}
                    disabled={detectingFaces}
                    size="sm"
                  >
                    {detectingFaces ? (
                      <>
                        <CSpinner size="sm" className="me-2" />
                        Detecting...
                      </>
                    ) : (
                      <>
                        <CIcon icon={cilFace} className="me-2" />
                        Auto-detect Faces
                      </>
                    )}
                  </CButton>
                </div>
                <div style={{ position: 'relative', width: '100%' }}>
                  {selectedImageForTagging?.imageUrl && (
                    <>
                      <img
                        ref={imageRefTagging}
                        src={selectedImageForTagging.imageUrl}
                        alt={selectedImageForTagging.displayName}
                        onLoad={handleImageLoadTagging}
                        style={{
                          width: '100%',
                          height: 'auto',
                          display: 'block',
                          borderRadius: '8px',
                        }}
                      />
                      <canvas
                        ref={canvasRefTagging}
                        onClick={handleCanvasClick}
                        onMouseDown={handleCanvasMouseDown}
                        onMouseMove={handleCanvasMouseMove}
                        onMouseUp={handleCanvasMouseUp}
                        onMouseLeave={handleCanvasMouseLeave}
                        style={{
                          position: 'absolute',
                          top: 0,
                          left: 0,
                          width: '100%',
                          height: '100%',
                          cursor: 'crosshair',
                        }}
                      />
                    </>
                  )}
                </div>
                <div className="mt-3">
                  <small className="text-muted">
                    {detectedFaces.length > 0
                      ? 'Click on a face to tag it, or drag on an empty area to create a new tagging square'
                      : 'Click "Auto-detect Faces" or drag on the image to manually create a tagging area'}
                  </small>
                  {error && (
                    <CAlert color="danger" className="mt-3" dismissible onClose={() => setError(null)}>
                      {error}
                    </CAlert>
                  )}
                </div>
              </CCol>

              {/* Side panel with detected faces * /}
              <CCol md={4}>
                <div style={{ maxHeight: '70vh', overflowY: 'auto' }}>
                  <h6 className="mb-3">
                    Detected Faces
                    {detectedFaces.length > 0 && (
                      <CBadge color="primary" className="ms-2">{detectedFaces.length}</CBadge>
                    )}
                  </h6>
                  {detectedFaces.length === 0 ? (
                    <p className="text-muted small">No faces detected yet. Use auto-detect or draw manually.</p>
                  ) : (
                    <CListGroup>
                      {detectedFaces.map((face, idx) => (
                        <CListGroupItem
                          key={face.id}
                          className="d-flex justify-content-between align-items-center"
                          style={{
                            cursor: 'pointer',
                            backgroundColor: selectedFace?.id === face.id ? '#e7f3ff' : 'transparent',
                          }}
                          onClick={() => {
                            setSelectedFace(face);
                            // Automatically open tag modal for any face (tagged or untagged)
                            setShowTagModal(true);
                          }}
                        >
                          <div>
                            <div style={{ fontWeight: 600, fontSize: '0.9rem' }}>
                              {face.person_name || `Face ${idx + 1}`}
                            </div>
                            {face.source && (
                              <small className="text-muted">{face.source}</small>
                            )}
                          </div>
                          <div className="d-flex gap-1">
                            {face.person_id && (
                              <CButton
                                color="danger"
                                variant="ghost"
                                size="sm"
                                onClick={(e) => {
                                  e.stopPropagation();
                                  handleRemoveTag(face);
                                }}
                                title="Remove tag"
                              >
                                <CIcon icon={cilX} style={{ width: 14, height: 14 }} />
                              </CButton>
                            )}
                            <CButton
                              color="danger"
                              variant="ghost"
                              size="sm"
                              onClick={(e) => {
                                e.stopPropagation();
                                handleDeleteArea(face);
                              }}
                              title="Delete area"
                            >
                              <CIcon icon={cilTrash} style={{ width: 14, height: 14 }} />
                            </CButton>
                          </div>
                        </CListGroupItem>
                      ))}
                    </CListGroup>
                  )}
                </div>
              </CCol>
            </CRow>
          </CCardBody>
        </CCard>
        </>
      )}


      {/* Tag Person Modal * /}
      <CModal visible={showTagModal} onClose={() => setShowTagModal(false)} size="lg">
        <CModalHeader>
          <CModalTitle>Tag Face</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p className="mb-3">Select a person to tag this face, or add a new person:</p>
          <div className="d-flex flex-wrap gap-3 mb-3">
            {people.map((person) => {
              // Check if person has a profile photo
              const hasProfilePhoto = person.profile_photo_display_path || person.profile_photo_path;
              
              // Determine image source for profile photo
              let imgSrc = null;
              if (hasProfilePhoto) {
                // If profile_photo_path is an absolute path, it's a pre-cropped face image
                const isCroppedFace = person.profile_photo_path && person.profile_photo_path.startsWith('/');
                if (isCroppedFace) {
                  imgSrc = `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_path)}`;
                } else if (person.profile_photo_display_path) {
                  imgSrc = `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_display_path)}`;
                } else if (person.profile_photo_path) {
                  imgSrc = `${PEOPLE_PHOTOS_BASE_URL}${person.profile_photo_path}`;
                }
              }
              
              const fb = person.profile_photo_face_box;

              return (
                <div
                  key={person.id}
                  onClick={() => handleTagFace(person.id)}
                  style={{
                    cursor: 'pointer',
                    border: '2px solid #d8dbe0',
                    borderRadius: '8px',
                    overflow: 'hidden',
                    width: '120px',
                    transition: 'all 0.2s',
                    backgroundColor: '#fff',
                  }}
                  onMouseEnter={(e) => {
                    e.currentTarget.style.borderColor = '#321fdb';
                    e.currentTarget.style.boxShadow = '0 2px 8px rgba(50, 31, 219, 0.2)';
                  }}
                  onMouseLeave={(e) => {
                    e.currentTarget.style.borderColor = '#d8dbe0';
                    e.currentTarget.style.boxShadow = 'none';
                  }}
                >
                  {/* Profile Photo * /}
                  <div
                    style={{
                      width: '120px',
                      height: '120px',
                      background: '#f5f5f5',
                      display: 'flex',
                      alignItems: 'center',
                      justifyContent: 'center',
                      overflow: 'hidden',
                      position: 'relative',
                    }}
                  >
                    {imgSrc ? (() => {
                      // If it's a cropped face or no face box data, display image directly
                      const isCroppedFace = person.profile_photo_path && person.profile_photo_path.startsWith('/');
                      if (isCroppedFace || !fb || !fb.img_width || !fb.img_height) {
                        return (
                          <img
                            src={imgSrc}
                            alt={person.name}
                            style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                          />
                        );
                      }
                      
                      // Crop to face region with padding using CSS background-image
                      const pad = Math.max(fb.w, fb.h) * 0.5;
                      const cropX = Math.max(0, fb.x - pad);
                      const cropY = Math.max(0, fb.y - pad);
                      const cropW = Math.min(fb.img_width - cropX, fb.w + pad * 2);
                      const cropH = Math.min(fb.img_height - cropY, fb.h + pad * 2);
                      const scale = fb.img_width / cropW;
                      const bgSize = `${scale * 100}%`;
                      const bgPosX = cropX === 0 ? '0%' : `${(cropX / (fb.img_width - cropW)) * 100}%`;
                      const bgPosY = cropY === 0 ? '0%' : `${(cropY / (fb.img_height - cropH)) * 100}%`;
                      
                      return (
                        <div
                          style={{
                            width: '100%',
                            height: '100%',
                            backgroundImage: `url(${imgSrc})`,
                            backgroundSize: bgSize,
                            backgroundPosition: `${bgPosX} ${bgPosY}`,
                            backgroundRepeat: 'no-repeat',
                          }}
                        />
                      );
                    })() : (
                      <div style={{ fontSize: '2.5rem', opacity: 0.35 }}>👤</div>
                    )}
                  </div>
                  
                  {/* Person Name * /}
                  <div
                    style={{
                      padding: '8px',
                      textAlign: 'center',
                      fontSize: '0.85rem',
                      fontWeight: 500,
                      color: '#333',
                      whiteSpace: 'nowrap',
                      overflow: 'hidden',
                      textOverflow: 'ellipsis',
                    }}
                  >
                    {person.name}
                  </div>
                </div>
              );
            })}
          </div>
          <div className="border-top pt-3">
            <CButton
              color="success"
              onClick={() => {
                setShowTagModal(false);
                setShowAddPersonModal(true);
              }}
            >
              <CIcon icon={cilPlus} className="me-2" />
              Add New Person
            </CButton>
          </div>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowTagModal(false)}>
            Cancel
          </CButton>
        </CModalFooter>
      </CModal>

      {/* Add New Person Modal * /}
      <CModal visible={showAddPersonModal} onClose={() => setShowAddPersonModal(false)}>
        <CModalHeader>
          <CModalTitle>Add New Person</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <CForm>
            <div className="mb-3">
              <label className="form-label">Name *</label>
              <CFormInput
                value={newPersonName}
                onChange={(e) => setNewPersonName(e.target.value)}
                placeholder="Enter person's name"
                onKeyPress={(e) => {
                  if (e.key === 'Enter') {
                    e.preventDefault();
                    handleAddNewPerson();
                  }
                }}
              />
            </div>
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowAddPersonModal(false)}>
            Cancel
          </CButton>
          <CButton
            color="primary"
            onClick={handleAddNewPerson}
            disabled={!newPersonName.trim()}
          >
            Add Person
          </CButton>
        </CModalFooter>
      </CModal>
      */}
    </CContainer>
  );
};

export default EventDetail;
