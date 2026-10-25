import React, { useState, useEffect, useRef, useCallback } from 'react';
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
  CSpinner,
  CAlert,
  CFormTextarea,
  CBadge,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilArrowLeft, cilTrash, cilStar, cilX, cilPencil, cilCheck } from '@coreui/icons';
import CreatableSelect from 'react-select/creatable';
import api, { faceDetectionAPI, tagAPI, peopleCategoryAPI } from '../services/api';
import { API_HOST, PEOPLE_PHOTOS_BASE_URL, galleryThumbUrl } from '../config';

// ── Birthday helpers ──────────────────────────────────────────────────────────
// Stored format options: 'YYYY-MM-DD', 'YYYY', '--MM-DD', or ''
const parseDateOfBirth = (dob) => {
  if (!dob) return { year: '', month: '', day: '' };
  if (/^--(\d{2})-(\d{2})$/.test(dob)) {
    const [, m, d] = dob.match(/^--(\d{2})-(\d{2})$/);
    return { year: '', month: m, day: d };
  }
  if (/^\d{4}-(\d{2})-(\d{2})$/.test(dob)) {
    const [, y, m, d] = dob.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return { year: y, month: m, day: d };
  }
  if (/^\d{4}$/.test(dob)) return { year: dob, month: '', day: '' };
  return { year: '', month: '', day: '' };
};

const composeDateOfBirth = (year, month, day) => {
  const y = year.trim();
  const m = month.trim();
  const d = day.trim();
  if (y && m && d) return `${y}-${m}-${d}`;
  if (y) return y;
  if (m && d) return `--${m}-${d}`;
  return '';
};

const formatDateOfBirthDisplay = (dob) => {
  if (!dob) return null;
  const months = ['January','February','March','April','May','June','July','August','September','October','November','December'];
  if (/^--(\d{2})-(\d{2})$/.test(dob)) {
    const [, m, d] = dob.match(/^--(\d{2})-(\d{2})$/);
    return `${months[parseInt(m, 10) - 1]} ${parseInt(d, 10)}`;
  }
  if (/^\d{4}-(\d{2})-(\d{2})$/.test(dob)) {
    const [, y, m, d] = dob.match(/^(\d{4})-(\d{2})-(\d{2})$/);
    return `${months[parseInt(m, 10) - 1]} ${parseInt(d, 10)}, ${y}`;
  }
  if (/^\d{4}$/.test(dob)) return dob;
  return dob;
};

const MONTHS = [
  { value: '01', label: 'January' }, { value: '02', label: 'February' },
  { value: '03', label: 'March' },   { value: '04', label: 'April' },
  { value: '05', label: 'May' },     { value: '06', label: 'June' },
  { value: '07', label: 'July' },    { value: '08', label: 'August' },
  { value: '09', label: 'September'},{ value: '10', label: 'October' },
  { value: '11', label: 'November' },{ value: '12', label: 'December' },
];

// The card's turn, as on the People page's cards.
const FLIP_MS = 550;
const FLIP_EASE = 'cubic-bezier(0.4, 0.2, 0.2, 1)';

// The person's picture, by the People page's rules: a pre-cropped face (an
// absolute profile_photo_path) as it is; otherwise the profile photograph,
// cropped to the face's box when there is one; otherwise a placeholder.
const PersonPicture = ({ person }) => {
  const cropped = person.profile_photo_path && person.profile_photo_path.startsWith('/');
  if (cropped) {
    return (
      <img alt={person.name}
           src={`${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_path)}`}
           style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
    );
  }
  if (!person.profile_photo_display_path && !person.profile_photo_path) {
    return <div style={{ fontSize: '3.5rem', opacity: 0.35 }}>👤</div>;
  }
  const src = person.profile_photo_display_path
    ? `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_display_path)}`
    : `${PEOPLE_PHOTOS_BASE_URL}${person.profile_photo_path}`;
  const fb = person.profile_photo_face_box;
  if (fb && fb.img_width && fb.img_height) {
    // A SQUARE around the face - the frame is square - twice the face's
    // larger side, centred on it and kept inside the photograph. A crop the
    // shape of the face's box would be cut top and bottom in a square frame.
    const side = Math.min(Math.max(fb.w, fb.h) * 2, fb.img_width, fb.img_height);
    const clamp = (v, max) => Math.max(0, Math.min(v, max));
    const cropX = clamp(fb.x + fb.w / 2 - side / 2, fb.img_width - side);
    const cropY = clamp(fb.y + fb.h / 2 - side / 2, fb.img_height - side);
    const at = (crop, room) => (room > 0 ? `${(crop / room) * 100}%` : '50%');
    return (
      <div role="img" aria-label={person.name} style={{
        width: '100%', height: '100%', backgroundImage: `url(${src})`,
        backgroundSize: `${(fb.img_width / side) * 100}%`,
        backgroundPosition: `${at(cropX, fb.img_width - side)} ${at(cropY, fb.img_height - side)}`,
        backgroundRepeat: 'no-repeat',
      }} />
    );
  }
  return (
    <img alt={person.name} src={src}
         style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }} />
  );
};

// TagInput component - token-style input for tags
const TagInput = ({ catalogId, value, onChange }) => {
  const [inputValue, setInputValue] = useState('');
  const [suggestions, setSuggestions] = useState([]);
  const [showSuggestions, setShowSuggestions] = useState(false);
  const [activeSuggestion, setActiveSuggestion] = useState(-1);
  const inputRef = useRef(null);
  const suggestionsRef = useRef(null);
  const debounceRef = useRef(null);

  const fetchSuggestions = useCallback(async (query) => {
    if (!query.trim()) {
      setSuggestions([]);
      setShowSuggestions(false);
      return;
    }
    try {
      const response = await tagAPI.getAll(catalogId, query);
      // Filter out already-selected tags
      const existing = value.map(t => t.name.toLowerCase());
      const filtered = response.data.filter(t => !existing.includes(t.name.toLowerCase()));
      setSuggestions(filtered);
      setShowSuggestions(true);
      setActiveSuggestion(-1);
    } catch {
      setSuggestions([]);
    }
  }, [catalogId, value]);

  const handleInputChange = (e) => {
    const val = e.target.value;
    setInputValue(val);
    clearTimeout(debounceRef.current);
    debounceRef.current = setTimeout(() => fetchSuggestions(val), 200);
  };

  const addTag = (tagName) => {
    const name = tagName.trim();
    if (!name) return;
    // Avoid duplicates
    if (value.some(t => t.name.toLowerCase() === name.toLowerCase())) {
      setInputValue('');
      setShowSuggestions(false);
      return;
    }
    onChange([...value, { name }]);
    setInputValue('');
    setSuggestions([]);
    setShowSuggestions(false);
    setActiveSuggestion(-1);
    inputRef.current?.focus();
  };

  const removeTag = (tagName) => {
    onChange(value.filter(t => t.name !== tagName));
  };

  const handleKeyDown = (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      if (activeSuggestion >= 0 && suggestions[activeSuggestion]) {
        addTag(suggestions[activeSuggestion].name);
      } else if (inputValue.trim()) {
        addTag(inputValue);
      }
    } else if (e.key === 'ArrowDown') {
      e.preventDefault();
      setActiveSuggestion(prev => Math.min(prev + 1, suggestions.length - 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setActiveSuggestion(prev => Math.max(prev - 1, -1));
    } else if (e.key === 'Escape') {
      setShowSuggestions(false);
      setActiveSuggestion(-1);
    } else if (e.key === 'Backspace' && !inputValue && value.length > 0) {
      // Remove last tag on backspace when input is empty
      onChange(value.slice(0, -1));
    }
  };

  // Close suggestions when clicking outside
  useEffect(() => {
    const handleClickOutside = (e) => {
      if (suggestionsRef.current && !suggestionsRef.current.contains(e.target) &&
          inputRef.current && !inputRef.current.contains(e.target)) {
        setShowSuggestions(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  return (
    <div style={{ position: 'relative' }}>
      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '4px',
          padding: '6px 8px',
          border: '1px solid #ced4da',
          borderRadius: '4px',
          backgroundColor: '#fff',
          cursor: 'text',
          minHeight: '38px',
          alignItems: 'center',
        }}
        onClick={() => inputRef.current?.focus()}
      >
        {value.map((tag) => (
          <span
            key={tag.name}
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: '4px',
              backgroundColor: '#321fdb',
              color: '#fff',
              borderRadius: '3px',
              padding: '2px 6px',
              fontSize: '0.8rem',
              fontWeight: 500,
            }}
          >
            {tag.name}
            <span
              style={{ cursor: 'pointer', lineHeight: 1, opacity: 0.8 }}
              onClick={(e) => { e.stopPropagation(); removeTag(tag.name); }}
              title="Remove tag"
            >
              ×
            </span>
          </span>
        ))}
        <input
          ref={inputRef}
          value={inputValue}
          onChange={handleInputChange}
          onKeyDown={handleKeyDown}
          onFocus={() => inputValue && fetchSuggestions(inputValue)}
          placeholder={value.length === 0 ? 'Add tags...' : ''}
          style={{
            border: 'none',
            outline: 'none',
            flex: '1 1 80px',
            minWidth: '80px',
            fontSize: '0.875rem',
            padding: '0',
            backgroundColor: 'transparent',
          }}
        />
      </div>
      {showSuggestions && suggestions.length > 0 && (
        <ul
          ref={suggestionsRef}
          style={{
            position: 'absolute',
            top: '100%',
            left: 0,
            right: 0,
            zIndex: 1000,
            listStyle: 'none',
            margin: 0,
            padding: '4px 0',
            backgroundColor: '#fff',
            border: '1px solid #ced4da',
            borderRadius: '4px',
            boxShadow: '0 2px 8px rgba(0,0,0,0.15)',
            maxHeight: '200px',
            overflowY: 'auto',
          }}
        >
          {suggestions.map((tag, idx) => (
            <li
              key={tag.id}
              onMouseDown={(e) => { e.preventDefault(); addTag(tag.name); }}
              style={{
                padding: '6px 12px',
                cursor: 'pointer',
                backgroundColor: idx === activeSuggestion ? '#f0f0f0' : 'transparent',
                fontSize: '0.875rem',
              }}
              onMouseEnter={() => setActiveSuggestion(idx)}
            >
              {tag.name}
            </li>
          ))}
          {inputValue.trim() && !suggestions.some(t => t.name.toLowerCase() === inputValue.trim().toLowerCase()) && (
            <li
              onMouseDown={(e) => { e.preventDefault(); addTag(inputValue); }}
              style={{
                padding: '6px 12px',
                cursor: 'pointer',
                backgroundColor: activeSuggestion === suggestions.length ? '#f0f0f0' : 'transparent',
                fontSize: '0.875rem',
                borderTop: suggestions.length > 0 ? '1px solid #eee' : 'none',
                color: '#321fdb',
              }}
            >
              + Create "<strong>{inputValue.trim()}</strong>"
            </li>
          )}
        </ul>
      )}
      {showSuggestions && suggestions.length === 0 && inputValue.trim() && (
        <ul
          ref={suggestionsRef}
          style={{
            position: 'absolute',
            top: '100%',
            left: 0,
            right: 0,
            zIndex: 1000,
            listStyle: 'none',
            margin: 0,
            padding: '4px 0',
            backgroundColor: '#fff',
            border: '1px solid #ced4da',
            borderRadius: '4px',
            boxShadow: '0 2px 8px rgba(0,0,0,0.15)',
          }}
        >
          <li
            onMouseDown={(e) => { e.preventDefault(); addTag(inputValue); }}
            style={{
              padding: '6px 12px',
              cursor: 'pointer',
              fontSize: '0.875rem',
              color: '#321fdb',
            }}
          >
            + Create "<strong>{inputValue.trim()}</strong>"
          </li>
        </ul>
      )}
      <small className="text-muted">Type to search or create tags. Press Enter or comma to add.</small>
    </div>
  );
};

const PersonDetail = () => {
  const { catalogId, personId } = useParams();
  const navigate = useNavigate();
  const [person, setPerson] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [successMessage, setSuccessMessage] = useState(null);
  const [settingProfilePhoto, setSettingProfilePhoto] = useState(null);
  const [selectedYear, setSelectedYear] = useState(null);
  // The gallery's detected photographs: ONE PAGE, from the server, with the
  // total across the selected year or all of them. The uploaded photographs
  // (people_photos) are few and come whole with the person record.
  const [detectedPage, setDetectedPage] = useState({ photos: [], total: 0 });
  const [galleryLoading, setGalleryLoading] = useState(false);
  // The card on the left flips to its edit side: isEditing turns it, showBack
  // keeps the edit side mounted while it turns either way.
  const [isEditing, setIsEditing] = useState(false);
  const [showBack, setShowBack] = useState(false);
  const flipTimer = useRef(null);
  // Photographs across every year, for the card: the gallery's own total
  // follows the selected year.
  const [allYearsTotal, setAllYearsTotal] = useState(null);
  const [saving, setSaving] = useState(false);
  const [showDeleteConfirmModal, setShowDeleteConfirmModal] = useState(false);

  // Gallery pagination state
  const [galleryPage, setGalleryPage] = useState(0);
  const galleryBodyRef = useRef(null);
  const topSectionRef = useRef(null);
  const [pageSize, setPageSize] = useState(12);
  const [galleryBodyH, setGalleryBodyH] = useState(400);
  const [formData, setFormData] = useState({
    name: '',
    birth_year: '',
    birth_month: '',
    birth_day: '',
    description: '',
    category: 'Other',
  });
  const [editTags, setEditTags] = useState([]);
  const [categories, setCategories] = useState(['Family', 'Other']);

  const loadCategories = useCallback(async () => {
    try {
      const response = await peopleCategoryAPI.getAll(catalogId);
      setCategories(response.data.length > 0 ? response.data : ['Family', 'Other']);
    } catch {
      setCategories(['Family', 'Other']);
    }
  }, [catalogId]);

  // `quiet` reloads without the full-page spinner, which would unmount the card
  // mid-flip: after a save, a new profile picture, a deleted photograph.
  const loadPerson = useCallback(async (quiet = false) => {
    if (!quiet) setLoading(true);
    setError(null);
    try {
      const response = await api.get(`/catalogs/${catalogId}/people/${personId}`);
      setPerson(response.data);
      const parsed = parseDateOfBirth(response.data.date_of_birth || '');
      setFormData({
        name: response.data.name,
        birth_year: parsed.year,
        birth_month: parsed.month,
        birth_day: parsed.day,
        description: response.data.description || '',
        category: response.data.category || 'Other',
      });
      setEditTags(response.data.tags || []);
    } catch (err) {
      setError('Failed to load person details');
    } finally {
      if (!quiet) setLoading(false);
    }
  }, [catalogId, personId]);

  useEffect(() => {
    loadPerson();
    loadCategories();
  }, [loadPerson, loadCategories]);

  // Uploaded photographs for the selected year, or all of them.
  const uploadedPhotos = (person?.photos || [])
    .filter((p) => selectedYear == null || p.year === selectedYear)
    .sort((x, y) => (x.year - y.year) || ((x.photo_order ?? 0) - (y.photo_order ?? 0)));

  // One page of the gallery. It asked the per-year route once per year and
  // merged every answer here to show a page of twelve: a person photographed
  // across twenty years was twenty requests for every photograph of them.
  // The detected photographs come first and are paged by the server; the
  // uploaded ones follow them, on whatever pages come after.
  const galleryRequest = useRef(0);
  useEffect(() => {
    if (!person) return;
    const start = galleryPage * pageSize;
    const seq = ++galleryRequest.current;
    setGalleryLoading(true);
    faceDetectionAPI.getPersonDetectedPhotosPage(catalogId, personId, {
      year: selectedYear, limit: pageSize, offset: start,
    }).then((r) => {
      if (seq !== galleryRequest.current) return;
      setDetectedPage({ photos: r.data.photos || [], total: r.data.total || 0 });
      if (selectedYear == null) setAllYearsTotal(r.data.total || 0);
    }).catch(() => {
      if (seq !== galleryRequest.current) return;
      setDetectedPage({ photos: [], total: 0 });
    }).finally(() => {
      if (seq === galleryRequest.current) setGalleryLoading(false);
    });
  }, [catalogId, personId, person, selectedYear, galleryPage, pageSize]);

  // A different year starts at the first page.
  useEffect(() => { setGalleryPage(0); }, [selectedYear]);

  // Compute gallery body height and page size — mirrors EventDetail
  useEffect(() => {
    const TOP_NAV_H = 60;
    const CONTAINER_PT = 24;
    const CONTAINER_PB = 24;
    const CARD_HEADER_H = 57;
    const CARD_BODY_P = 24;
    const CARD_W = 160;
    const CARD_H = 240; // image 160 + label + action button + padding
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
      // The card body's own padding. clientWidth includes it, and the height
      // set below is border-box: left out, the grid had 32 px less height
      // than its rows and the last row was cut, and its width could fit one
      // card more than the row really holds - which then wrapped, and was cut.
      const cs = bodyEl ? window.getComputedStyle(bodyEl) : null;
      const padX = cs ? parseFloat(cs.paddingLeft) + parseFloat(cs.paddingRight) : 0;
      const padY = cs ? parseFloat(cs.paddingTop) + parseFloat(cs.paddingBottom) : 0;
      const bodyW = bodyEl ? bodyEl.clientWidth - padX : window.innerWidth - 340;
      const cols = Math.max(1, Math.floor((bodyW + GAP) / (CARD_W + GAP)));
      const rows = Math.max(1, Math.floor((Math.max(CARD_H, avail) + GAP) / (CARD_H + GAP)) - 1);
      // Room for exactly the rows, plus the padding around them.
      setGalleryBodyH(rows * (CARD_H + GAP) - GAP + padY);
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

  const handleDeletePhoto = async (photoId) => {
    if (!window.confirm('Are you sure you want to delete this photo?')) {
      return;
    }

    try {
      await api.delete(`/catalogs/${catalogId}/people/photos/${photoId}`);
      loadPerson(true);
    } catch (err) {
      setError('Failed to delete photo');
    }
  };

  const handleUpdatePerson = async () => {
    if (!formData.name.trim()) { setError('Name is required'); return; }
    setSaving(true);
    try {
      await api.put(`/catalogs/${catalogId}/people/${personId}`, {
        name: formData.name,
        date_of_birth: composeDateOfBirth(formData.birth_year, formData.birth_month, formData.birth_day),
        description: formData.description,
        category: formData.category || 'Other',
      });
      // Persist any newly-typed category
      if (formData.category && !categories.includes(formData.category)) {
        await peopleCategoryAPI.create(catalogId, formData.category);
        loadCategories();
      }
      // Save tags separately
      await tagAPI.setPersonTags(catalogId, personId, editTags.map(t => t.name));
      await loadPerson(true);
      closeEditor();
    } catch (err) {
      setError('Failed to update person');
    } finally {
      setSaving(false);
    }
  };

  const handleStartEditing = () => {
    // Reset form from current person data when entering edit mode
    const parsed = parseDateOfBirth(person?.date_of_birth || '');
    setFormData({
      name: person?.name || '',
      birth_year: parsed.year,
      birth_month: parsed.month,
      birth_day: parsed.day,
      description: person?.description || '',
      category: person?.category || 'Other',
    });
    setEditTags(person?.tags || []);
    clearTimeout(flipTimer.current);
    setShowBack(true);
    // Mounted first, turned on the next frame, so the turn is animated.
    requestAnimationFrame(() => setIsEditing(true));
  };

  // Turn back to the details; the edit side unmounts once it is out of sight.
  const closeEditor = () => {
    setIsEditing(false);
    clearTimeout(flipTimer.current);
    flipTimer.current = setTimeout(() => setShowBack(false), FLIP_MS);
  };
  useEffect(() => () => clearTimeout(flipTimer.current), []);

  const handleCancelEditing = () => {
    closeEditor();
    setError(null);
  };

  const handleDeletePerson = async () => {
    try {
      await api.delete(`/catalogs/${catalogId}/people/${personId}`);
      setShowDeleteConfirmModal(false);
      setIsEditing(false);
      navigate('/people');
    } catch (err) {
      setError('Failed to delete person');
      setShowDeleteConfirmModal(false);
    }
  };

  const handleSetProfilePhoto = async (fileId) => {
    setSettingProfilePhoto(fileId);
    setError(null);
    setSuccessMessage(null);
    try {
      await faceDetectionAPI.setProfilePhoto(catalogId, personId, fileId);
      setSuccessMessage('Profile picture updated successfully!');
      loadPerson(true);
    } catch (err) {
      setError('Failed to set profile picture');
    } finally {
      setSettingProfilePhoto(null);
    }
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

  if (!person) {
    return (
      <CContainer fluid className="px-4 py-4">
        <CAlert color="danger">Person not found</CAlert>
      </CContainer>
    );
  }

  // This page: the server's page of detected photographs, then - once those
  // run out - the uploaded ones, continuing from where the detected ended.
  const detectedTotal = detectedPage.total;
  const galleryTotal = detectedTotal + uploadedPhotos.length;
  const pageStart = galleryPage * pageSize;
  const uploadedFrom = Math.max(0, pageStart - detectedTotal);
  const uploadedHere = uploadedPhotos.slice(
    uploadedFrom, uploadedFrom + (pageSize - detectedPage.photos.length));
  const pagePhotos = [
    ...detectedPage.photos.map(p => ({
      key: `det-${p.file_id}`,
      src: `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(p.display_path)}`,
      thumbSrc: galleryThumbUrl(catalogId, p.file_id),
      name: p.display_name,
      isProfilePhoto: person?.profile_photo_file_id === p.file_id,
      fileId: p.file_id,
      characteristic: p.characteristic,
      isDetected: true,
    })),
    ...uploadedHere.map(p => ({
      key: `yr-${p.id}`,
      src: `${PEOPLE_PHOTOS_BASE_URL}${p.photo_path}`,
      name: `${person.name}${selectedYear ? ` – ${selectedYear}` : ''}`,
      isProfilePhoto: false,
      photoId: p.id,
      isDetected: false,
    })),
  ];
  const totalGalleryPages = Math.max(1, Math.ceil(galleryTotal / pageSize));

  const yearsSorted = (person.years || []).slice().sort((x, y) => x - y);
  const yearSpan = yearsSorted.length === 0 ? null
    : yearsSorted.length === 1 ? String(yearsSorted[0])
    : `${yearsSorted[0]} – ${yearsSorted[yearsSorted.length - 1]}`;
  const photographs = allYearsTotal == null ? null
    : allYearsTotal + (person.photos?.length || 0);

  // Pets' shape (PetDetail.js): the person on the left, a column of md 3 with a
  // large picture and the details under it; their photographs on the right,
  // md 9. The card turns over to edit, and back on save or cancel.
  return (
    <CContainer fluid className="py-3">
      <div ref={topSectionRef}>
        {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}
        {successMessage && <CAlert color="success" dismissible onClose={() => setSuccessMessage(null)}>{successMessage}</CAlert>}

        <div className="d-flex align-items-center gap-3 mb-3">
          <CButton color="secondary" variant="outline" size="sm" onClick={() => navigate('/people')}
                   title="Back to People">
            <CIcon icon={cilArrowLeft} style={{ width: 14, height: 14 }} />
          </CButton>
          <h4 className="mb-0 fw-bold">{person.name}</h4>
          {person.category && <CBadge color="light" className="text-body">{person.category}</CBadge>}
        </div>
      </div>{/* end topSectionRef */}

      <CRow className="g-3">
        <CCol md={3}>
          <div style={{ perspective: '1200px' }}>
            {/* Both sides share one grid cell, so the card is as tall as the
                side being shown (the edit side is mounted only around a turn). */}
            <div style={{
              display: 'grid',
              transformStyle: 'preserve-3d',
              transition: `transform ${FLIP_MS}ms ${FLIP_EASE}`,
              transform: isEditing ? 'rotateY(180deg)' : 'rotateY(0deg)',
            }}>
              {/* FRONT: the person */}
              <div style={{ gridArea: '1 / 1', alignSelf: 'start', backfaceVisibility: 'hidden', WebkitBackfaceVisibility: 'hidden', transform: 'rotateY(0deg)' }}>
                <CCard>
                  {/* Square, as wide as the column: the card grows taller
                      rather than cutting the face to a wide strip. */}
                  <div style={{ aspectRatio: '1 / 1', width: '100%', background: '#f5f5f5', display: 'flex',
                                alignItems: 'center', justifyContent: 'center', overflow: 'hidden' }}>
                    <PersonPicture person={person} />
                  </div>
                  <CCardBody style={{ fontSize: 13 }}>
                    <dl className="mb-0">
                      <dt className="text-body-secondary fw-normal">Category</dt>
                      <dd>{person.category || '—'}</dd>
                      <dt className="text-body-secondary fw-normal">Born</dt>
                      <dd>{formatDateOfBirthDisplay(person.date_of_birth) || '—'}</dd>
                      <dt className="text-body-secondary fw-normal">Photographs</dt>
                      <dd>{photographs == null ? '…' : photographs.toLocaleString()}</dd>
                      <dt className="text-body-secondary fw-normal">Years</dt>
                      <dd>{yearSpan || '—'}</dd>
                    </dl>
                    {person.description && (
                      <p className="mb-0 mt-2 text-body-secondary">{person.description}</p>
                    )}
                    {person.tags && person.tags.length > 0 && (
                      <div className="d-flex flex-wrap gap-1 mt-2">
                        {person.tags.map(tag => (
                          <CBadge key={tag.id || tag.name} color="primary" style={{ fontSize: '0.75rem' }}>
                            {tag.name}
                          </CBadge>
                        ))}
                      </div>
                    )}
                    <CButton color="primary" size="sm" className="w-100 mt-3" onClick={handleStartEditing}
                             disabled={showBack}>
                      <CIcon icon={cilPencil} style={{ width: 13, height: 13 }} className="me-1" />Edit
                    </CButton>
                  </CCardBody>
                </CCard>
              </div>

              {/* BACK: every detail, editable */}
              {showBack && (
                <div style={{ gridArea: '1 / 1', alignSelf: 'start', backfaceVisibility: 'hidden', WebkitBackfaceVisibility: 'hidden', transform: 'rotateY(180deg)' }}>
                  <CCard className="border-primary">
                    <CCardHeader className="bg-primary text-white d-flex justify-content-between align-items-center py-2">
                      <strong style={{ fontSize: '0.9rem' }}>Edit person</strong>
                      <CButton color="link" size="sm" className="p-0 text-white" onClick={handleCancelEditing}
                               disabled={saving} title="Cancel">
                        <CIcon icon={cilX} />
                      </CButton>
                    </CCardHeader>
                    <CCardBody>
                      <CForm>
                        <div className="mb-3">
                          <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Name *</CFormLabel>
                          <CFormInput
                            value={formData.name}
                            onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                            placeholder="Enter name"
                          />
                        </div>
                        <div className="mb-3">
                          <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Birth Year <span className="text-muted fw-normal">(optional)</span></CFormLabel>
                          <CFormInput
                            type="number"
                            min="1900"
                            max={new Date().getFullYear()}
                            value={formData.birth_year}
                            onChange={(e) => setFormData({ ...formData, birth_year: e.target.value })}
                            placeholder="e.g. 1985"
                          />
                        </div>
                        <div className="mb-3">
                          <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Day &amp; Month <span className="text-muted fw-normal">(optional)</span></CFormLabel>
                          <div className="d-flex gap-2">
                            <select
                              className="form-select"
                              value={formData.birth_month}
                              onChange={(e) => setFormData({ ...formData, birth_month: e.target.value })}
                            >
                              <option value="">Month</option>
                              {MONTHS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
                            </select>
                            <select
                              className="form-select"
                              value={formData.birth_day}
                              onChange={(e) => setFormData({ ...formData, birth_day: e.target.value })}
                            >
                              <option value="">Day</option>
                              {Array.from({ length: 31 }, (_, i) => {
                                const d = String(i + 1).padStart(2, '0');
                                return <option key={d} value={d}>{i + 1}</option>;
                              })}
                            </select>
                          </div>
                        </div>
                        <div className="mb-3">
                          <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Category</CFormLabel>
                          <CreatableSelect
                            isClearable={false}
                            value={formData.category ? { value: formData.category, label: formData.category } : { value: 'Other', label: 'Other' }}
                            onChange={(selected) => setFormData({ ...formData, category: selected ? selected.value : 'Other' })}
                            options={categories.map(c => ({ value: c, label: c }))}
                            placeholder="Select or type category..."
                            formatCreateLabel={(v) => `New category: "${v}"`}
                            menuPortalTarget={document.body}
                            styles={{
                              control: (base) => ({ ...base, fontSize: '0.875rem' }),
                              menuPortal: (base) => ({ ...base, zIndex: 9999 }),
                            }}
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
                          <CFormLabel className="fw-semibold" style={{ fontSize: '0.82rem' }}>Tags</CFormLabel>
                          <TagInput
                            catalogId={catalogId}
                            value={editTags}
                            onChange={setEditTags}
                          />
                        </div>
                        <div className="d-flex gap-2 mt-3">
                          <CButton color="primary" className="flex-grow-1" onClick={handleUpdatePerson} disabled={!formData.name || saving}>
                            {saving
                              ? <><CSpinner size="sm" className="me-1" />Saving…</>
                              : <><CIcon icon={cilCheck} style={{ width: 14, height: 14 }} className="me-1" />Save</>
                            }
                          </CButton>
                          <CButton color="secondary" variant="outline" onClick={handleCancelEditing} disabled={saving}>
                            Cancel
                          </CButton>
                        </div>
                        <CButton
                          color="danger"
                          variant="outline"
                          size="sm"
                          className="w-100 mt-3"
                          onClick={() => setShowDeleteConfirmModal(true)}
                          disabled={saving}
                        >
                          <CIcon icon={cilTrash} className="me-1" style={{ width: 13, height: 13 }} />
                          Delete Person
                        </CButton>
                      </CForm>
                    </CCardBody>
                  </CCard>
                </div>
              )}
            </div>
          </div>
        </CCol>

        <CCol md={9}>
          <CCard>
            <CCardHeader className="d-flex align-items-center flex-wrap gap-2">
              {/* Year filter buttons */}
              <div className="d-flex gap-1 flex-wrap align-items-center">
                <button
                  className={`btn btn-sm ${selectedYear === null ? 'btn-primary' : 'btn-outline-secondary'}`}
                  onClick={() => setSelectedYear(null)}
                >
                  All
                </button>
                {(person?.years || []).slice().sort((a, b) => b - a).map((year) => (
                  <button
                    key={year}
                    className={`btn btn-sm ${selectedYear === year ? 'btn-primary' : 'btn-outline-secondary'}`}
                    onClick={() => setSelectedYear(year)}
                  >
                    {year}
                  </button>
                ))}
                {galleryTotal > 0 && (
                  <CBadge color="secondary" className="ms-1">{galleryTotal.toLocaleString()}</CBadge>
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
            <CCardBody ref={galleryBodyRef} style={{ minHeight: galleryBodyH }}>
              {galleryLoading && pagePhotos.length === 0 && (
                <div className="text-center py-5"><CSpinner color="primary" /></div>
              )}
              {/* Empty state */}
              {!galleryLoading && galleryTotal === 0 && (
                <div className="text-center py-5 text-muted">
                  <p>No photos{selectedYear ? ` for ${selectedYear}` : ''}. Tag this person in face detections to add photos.</p>
                </div>
              )}

              {/* Photo grid */}
              {pagePhotos.length > 0 && (
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '12px' }}>
                  {pagePhotos.map((photo) => (
                    <div key={photo.key} style={{ width: 160, flexShrink: 0 }}>
                      <CCard
                        style={{
                          cursor: photo.isDetected ? 'pointer' : 'default',
                          transition: 'transform 0.2s, box-shadow 0.2s',
                          outline: photo.isProfilePhoto ? '2px solid #f9a825' : 'none',
                        }}
                        onMouseEnter={(e) => {
                          e.currentTarget.style.transform = 'translateY(-4px)';
                          e.currentTarget.style.boxShadow = '0 4px 12px rgba(0,0,0,0.15)';
                        }}
                        onMouseLeave={(e) => {
                          e.currentTarget.style.transform = 'translateY(0)';
                          e.currentTarget.style.boxShadow = '';
                        }}
                        onClick={() => photo.isDetected && navigate(`/photos/${catalogId}/${photo.fileId}`)}
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
                          <img
                            src={photo.thumbSrc || photo.src}
                            alt={photo.name}
                            loading="lazy"
                            style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                            title={photo.name}
                          />
                          {photo.isProfilePhoto && (
                            <div style={{
                              position: 'absolute', top: 8, left: 8,
                              background: 'rgba(249,168,37,0.9)', color: '#000',
                              padding: '2px 6px', borderRadius: 4,
                              fontSize: '0.7rem', display: 'flex', alignItems: 'center', gap: 3,
                            }}>
                              <CIcon icon={cilStar} style={{ width: 10, height: 10 }} />
                              Profile
                            </div>
                          )}
                        </div>
                        <CCardBody className="p-2">
                          <small className="text-muted d-block text-truncate" title={photo.name}>
                            {photo.name}
                          </small>
                          {photo.isDetected && !photo.isProfilePhoto && (
                            <CButton
                              color="warning"
                              variant="outline"
                              size="sm"
                              className="w-100 mt-1"
                              disabled={settingProfilePhoto === photo.fileId}
                              onClick={() => handleSetProfilePhoto(photo.fileId)}
                            >
                              {settingProfilePhoto === photo.fileId
                                ? <CSpinner size="sm" />
                                : <><CIcon icon={cilStar} style={{ width: 11, height: 11 }} className="me-1" />Set profile</>
                              }
                            </CButton>
                          )}
                          {!photo.isDetected && (
                            <CButton
                              color="danger"
                              variant="outline"
                              size="sm"
                              className="w-100 mt-1"
                              onClick={() => handleDeletePhoto(photo.photoId)}
                            >
                              <CIcon icon={cilTrash} style={{ width: 11, height: 11 }} className="me-1" />Delete
                            </CButton>
                          )}
                        </CCardBody>
                      </CCard>
                    </div>
                  ))}
                </div>
              )}
            </CCardBody>
          </CCard>
        </CCol>
      </CRow>

      {/* Delete Confirmation Modal */}
      <CModal visible={showDeleteConfirmModal} onClose={() => setShowDeleteConfirmModal(false)}>
        <CModalHeader>
          <CModalTitle>Delete Person</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p>Are you sure you want to delete <strong>{person?.name}</strong> and all their photos?</p>
          <p className="text-muted small">This action cannot be undone.</p>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowDeleteConfirmModal(false)}>
            Cancel
          </CButton>
          <CButton color="danger" onClick={handleDeletePerson}>
            <CIcon icon={cilTrash} className="me-2" />
            Delete Person
          </CButton>
        </CModalFooter>
      </CModal>
    </CContainer>
  );
};

export default PersonDetail;
