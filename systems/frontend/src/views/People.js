import React, { useState, useEffect } from 'react';
import {
  CCard,
  CCardBody,
  CCardHeader,
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
import { cilPlus, cilPencil, cilTrash, cilX, cilSave } from '@coreui/icons';
import CreatableSelect from 'react-select/creatable';
import api, { tagAPI, peopleCategoryAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { API_HOST, PEOPLE_PHOTOS_BASE_URL } from '../config';

// The person card's face side: as wide as a pet card (Pets.js, 230) so the two
// pages line up, and tall enough for the name row plus a SQUARE photo - the
// profile photos are square face crops, and a shorter card cut their bottom off.
const PERSON_CARD_WIDTH = 230;
const PERSON_NAME_ROW_PX = 38;
const PERSON_CARD_HEIGHT = PERSON_CARD_WIDTH + PERSON_NAME_ROW_PX;

// ── Birthday helpers ──────────────────────────────────────────────────────────

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

const MONTHS = [
  { value: '01', label: 'January' }, { value: '02', label: 'February' },
  { value: '03', label: 'March' },   { value: '04', label: 'April' },
  { value: '05', label: 'May' },     { value: '06', label: 'June' },
  { value: '07', label: 'July' },    { value: '08', label: 'August' },
  { value: '09', label: 'September'},{ value: '10', label: 'October' },
  { value: '11', label: 'November' },{ value: '12', label: 'December' },
];

const EMPTY_FORM = { name: '', birth_year: '', birth_month: '', birth_day: '', description: '', category: 'Other', tags: [] };

const People = () => {
  const { activeCatalog } = useCatalog();
  const [people, setPeople] = useState([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [showAddModal, setShowAddModal] = useState(false);
  const [showDeleteConfirmModal, setShowDeleteConfirmModal] = useState(false);
  const [flippedPersonId, setFlippedPersonId] = useState(null);
  const [selectedPerson, setSelectedPerson] = useState(null);
  const [formData, setFormData] = useState(EMPTY_FORM);
  const [tagInput, setTagInput] = useState('');
  const [tagSuggestions, setTagSuggestions] = useState([]);
  const [categories, setCategories] = useState(['Family', 'Other']);
  // Renaming a category from its section heading: { from, to } while editing.
  const [renaming, setRenaming] = useState(null);
  const [renameSaving, setRenameSaving] = useState(false);
  const [renameError, setRenameError] = useState(null);

  useEffect(() => {
    if (activeCatalog) {
      loadPeople();
      loadCategories();
    } else {
      setPeople([]);
    }
  }, [activeCatalog]);

  const loadPeople = async () => {
    if (!activeCatalog) return;
    
    setLoading(true);
    setError(null);
    try {
      const response = await api.get(`/catalogs/${activeCatalog.id}/people`);
      setPeople(response.data);
    } catch (err) {
      setError('Failed to load people');
    } finally {
      setLoading(false);
    }
  };

  const loadCategories = async () => {
    if (!activeCatalog) return;
    try {
      const response = await peopleCategoryAPI.getAll(activeCatalog.id);
      setCategories(response.data.length > 0 ? response.data : ['Family', 'Other']);
    } catch {
      setCategories(['Family', 'Other']);
    }
  };

  // Family and Other are re-created whenever missing, so renaming them would
  // only leave an empty copy behind (PeopleCategoryModel.BUILT_IN).
  const BUILT_IN_CATEGORIES = ['Family', 'Other'];

  const saveCategoryName = async () => {
    const to = renaming.to.trim();
    if (!to || to === renaming.from) {
      setRenaming(null);
      setRenameError(null);
      return;
    }
    setRenameSaving(true);
    setRenameError(null);
    try {
      await peopleCategoryAPI.rename(activeCatalog.id, renaming.from, to);
      setRenaming(null);
      await Promise.all([loadPeople(), loadCategories()]);
    } catch (err) {
      setRenameError(err.response?.data?.error || 'Could not rename the category.');
    } finally {
      setRenameSaving(false);
    }
  };

  const handleAddPerson = async () => {
    try {
      await api.post(`/catalogs/${activeCatalog.id}/people`, {
        name: formData.name,
        date_of_birth: composeDateOfBirth(formData.birth_year, formData.birth_month, formData.birth_day),
        description: formData.description,
        category: formData.category || 'Other',
      });
      // Persist any newly-typed category so it's available for future use
      if (formData.category && !categories.includes(formData.category)) {
        await peopleCategoryAPI.create(activeCatalog.id, formData.category);
        loadCategories();
      }
      setShowAddModal(false);
      setFormData(EMPTY_FORM);
      loadPeople();
    } catch (err) {
      setError('Failed to add person');
    }
  };

  const handleEditPerson = async () => {
    try {
      await api.put(`/catalogs/${activeCatalog.id}/people/${selectedPerson.id}`, {
        name: formData.name,
        date_of_birth: composeDateOfBirth(formData.birth_year, formData.birth_month, formData.birth_day),
        description: formData.description,
        category: formData.category || 'Other',
      });
      // Persist any newly-typed category
      if (formData.category && !categories.includes(formData.category)) {
        await peopleCategoryAPI.create(activeCatalog.id, formData.category);
        loadCategories();
      }
      await tagAPI.setPersonTags(activeCatalog.id, selectedPerson.id, formData.tags);
      setFlippedPersonId(null);
      setTimeout(() => {
        setSelectedPerson(null);
        setFormData(EMPTY_FORM);
        setTagInput('');
        setTagSuggestions([]);
        loadPeople();
      }, 600);
    } catch (err) {
      setError('Failed to update person');
    }
  };

  const handleDeletePerson = async () => {
    try {
      await api.delete(`/catalogs/${activeCatalog.id}/people/${selectedPerson.id}`);
      setShowDeleteConfirmModal(false);
      setFlippedPersonId(null);
      setSelectedPerson(null);
      setFormData(EMPTY_FORM);
      setTagInput('');
      setTagSuggestions([]);
      loadPeople();
    } catch (err) {
      setError('Failed to delete person');
      setShowDeleteConfirmModal(false);
    }
  };

  const openEditModal = async (person) => {
    setSelectedPerson(person);
    let personTags = [];
    try {
      const tagsResponse = await tagAPI.getPersonTags(activeCatalog.id, person.id);
      personTags = tagsResponse.data.map(t => t.name || t);
    } catch (err) {
      console.error('Failed to load person tags', err);
    }
    const parsed = parseDateOfBirth(person.date_of_birth || '');
    setFormData({
      name: person.name,
      birth_year: parsed.year,
      birth_month: parsed.month,
      birth_day: parsed.day,
      description: person.description || '',
      category: person.category || 'Other',
      tags: personTags,
    });
    setTagInput('');
    setTagSuggestions([]);
    setFlippedPersonId(person.id);
  };

  const handleTagInputChange = async (value) => {
    setTagInput(value);
    if (value.trim().length > 0) {
      try {
        const response = await tagAPI.getAll(activeCatalog.id, value.trim());
        const existing = (response.data || []).map(t => t.name || t);
        setTagSuggestions(existing.filter(s => !formData.tags.includes(s)));
      } catch (err) {
        setTagSuggestions([]);
      }
    } else {
      setTagSuggestions([]);
    }
  };

  const addTag = (tag) => {
    const trimmed = tag.trim();
    if (trimmed && !formData.tags.includes(trimmed)) {
      setFormData(prev => ({ ...prev, tags: [...prev.tags, trimmed] }));
    }
    setTagInput('');
    setTagSuggestions([]);
  };

  const removeTag = (tag) => {
    setFormData(prev => ({ ...prev, tags: prev.tags.filter(t => t !== tag) }));
  };

  const handleTagKeyDown = (e) => {
    if (e.key === 'Enter' || e.key === ',') {
      e.preventDefault();
      if (tagInput.trim()) addTag(tagInput);
    } else if (e.key === 'Backspace' && tagInput === '' && formData.tags.length > 0) {
      removeTag(formData.tags[formData.tags.length - 1]);
    }
  };

  const openPersonDetails = (personId) => {
    window.location.href = `/people/${activeCatalog.id}/${personId}`;
  };

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
      <div className="d-flex justify-content-between align-items-center mb-4">
        <div>
          <h4 className="mb-0 fw-bold" style={{ color: '#212121' }}>People</h4>
          <div style={{ fontSize: '0.85rem', color: '#888', marginTop: 2 }}>
            Manage people in your catalog
          </div>
        </div>
        <CButton color="primary" onClick={() => setShowAddModal(true)}>
          <CIcon icon={cilPlus} className="me-2" />
          Add Person
        </CButton>
      </div>

      {error && <CAlert color="danger">{error}</CAlert>}
      
      {loading ? (
        <div className="text-center py-5">
          <CSpinner color="primary" />
        </div>
      ) : people.length === 0 ? (
        <div className="text-center py-5 text-muted">
          <p>No people added yet. Click "Add Person" to get started.</p>
        </div>
      ) : (() => {
        // ── Group people by category, Family always first ────────────────────
        const categoryMap = {};
        const categoryOrder = [];
        people.forEach((person) => {
          const key = (person.category || 'Family').trim();
          if (!categoryMap[key]) {
            categoryMap[key] = [];
            categoryOrder.push(key);
          }
          categoryMap[key].push(person);
        });
        // Family always first, then the rest alphabetically
        const sortedCategories = [
          ...('Family' in categoryMap ? ['Family'] : []),
          ...categoryOrder.filter((k) => k !== 'Family').sort(),
        ];

        const renderPersonCard = (person) => {
          const isFlipped = flippedPersonId === person.id;
          return (
            <div key={person.id} style={{ flexShrink: 0 }}>
                          {/* Flip card container. The face side is 230 wide,
                              as a pet card, with a square photo; the edit side keeps
                              290 x 510, because its form - name, dates,
                              category, tags - does not fit any narrower. */}
                          <div style={{
                            perspective: '1000px',
                            height: isFlipped ? '510px' : `${PERSON_CARD_HEIGHT}px`,
                            width: isFlipped ? '290px' : `${PERSON_CARD_WIDTH}px`,
                            transition: 'height 0.55s cubic-bezier(0.4, 0.2, 0.2, 1), width 0.55s cubic-bezier(0.4, 0.2, 0.2, 1)',
                          }}>
                            <div style={{
                              position: 'relative',
                              width: '100%',
                              height: '100%',
                              transformStyle: 'preserve-3d',
                              transition: 'transform 0.55s cubic-bezier(0.4, 0.2, 0.2, 1)',
                              transform: isFlipped ? 'rotateY(180deg)' : 'rotateY(0deg)',
                            }}>
                              {/* FRONT FACE */}
                              <div style={{
                                position: 'absolute',
                                width: '100%',
                                height: '100%',
                                backfaceVisibility: 'hidden',
                                WebkitBackfaceVisibility: 'hidden',
                              }}>
                                <div style={{
                                  height: '100%',
                                  background: '#fff',
                                  border: '1px solid #e0e0e0',
                                  borderRadius: '0 0 10px 10px',     // square at the top, round at the bottom
                                  overflow: 'hidden',
                                  boxShadow: '0 1px 3px rgba(0,0,0,0.06)',
                                  display: 'flex',
                                  flexDirection: 'column',
                                  transition: 'all 0.15s ease',
                                }}>
                                  {/* Top: Name + Edit button */}
                                  <div
                                    className="d-flex justify-content-between align-items-center"
                                    style={{
                                      // A fixed height, so the photo below it
                                      // is exactly square (PERSON_CARD_HEIGHT).
                                      height: PERSON_NAME_ROW_PX,
                                      boxSizing: 'border-box',
                                      padding: '0 0.65rem',
                                      borderBottom: '1px solid #f0f0f0',
                                      flexShrink: 0,
                                    }}
                                  >
                                    <div
                                      style={{
                                        fontWeight: 600,
                                        fontSize: '0.82rem',
                                        color: '#212121',
                                        whiteSpace: 'nowrap',
                                        overflow: 'hidden',
                                        textOverflow: 'ellipsis',
                                        flex: 1,
                                        minWidth: 0,
                                      }}
                                    >
                                      {person.name}
                                    </div>
                                    <div style={{ display: 'flex', alignItems: 'center', gap: 6, flexShrink: 0 }}>
                                      {person.face_count > 0 && (
                                        <CBadge
                                          color="secondary"
                                          style={{ fontSize: '0.72rem' }}
                                        >
                                          {person.face_count}
                                        </CBadge>
                                      )}
                                      <button
                                        onClick={() => openEditModal(person)}
                                        style={{
                                          background: 'none',
                                          border: '1px solid #e0e0e0',
                                          borderRadius: 4,
                                          padding: '2px 6px',
                                          cursor: 'pointer',
                                          flexShrink: 0,
                                          color: '#666',
                                          lineHeight: 1,
                                        }}
                                        title="Edit person"
                                      >
                                        <CIcon icon={cilPencil} style={{ width: 13, height: 13 }} />
                                      </button>
                                    </div>
                                  </div>

                                  {/* Middle: Profile photo */}
                                  <div
                                    style={{
                                      flex: '1 1 0',
                                      background: '#f5f5f5',
                                      display: 'flex',
                                      alignItems: 'center',
                                      justifyContent: 'center',
                                      cursor: 'pointer',
                                      overflow: 'hidden',
                                      position: 'relative',
                                    }}
                                    onClick={() => openPersonDetails(person.id)}
                                  >
                                    {(person.profile_photo_display_path || person.profile_photo_path) ? (() => {
                                      // If profile_photo_path is an absolute path, it's a pre-cropped face image — use it directly
                                      const isCroppedFace = person.profile_photo_path && person.profile_photo_path.startsWith('/');
                                      if (isCroppedFace) {
                                        const imgSrc = `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_path)}`;
                                        return (
                                          <img
                                            src={imgSrc}
                                            alt={person.name}
                                            style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                                          />
                                        );
                                      }
                                      const imgSrc = person.profile_photo_display_path
                                        ? `${API_HOST}/api/catalogs/files?path=${encodeURIComponent(person.profile_photo_display_path)}`
                                        : `${PEOPLE_PHOTOS_BASE_URL}${person.profile_photo_path}`;
                                      const fb = person.profile_photo_face_box;
                                      if (fb && fb.img_width && fb.img_height) {
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
                                      }
                                      return (
                                        <img
                                          src={imgSrc}
                                          alt={person.name}
                                          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                                        />
                                      );
                                    })() : (
                                      <div style={{ fontSize: '2.4rem', opacity: 0.35 }}>👤</div>
                                    )}
                                  </div>

                                </div>
                              </div>
  
                              {/* BACK FACE (Edit details) */}
                              <div style={{
                                position: 'absolute',
                                width: '100%',
                                height: '100%',
                                backfaceVisibility: 'hidden',
                                WebkitBackfaceVisibility: 'hidden',
                                transform: 'rotateY(180deg)',
                              }}>
                                <CCard className="h-100 shadow-sm border-primary"
                                       style={{ borderTopLeftRadius: 0, borderTopRightRadius: 0 }}>
                                  <CCardHeader className="bg-primary text-white d-flex justify-content-between align-items-center py-2"
                                               style={{ borderTopLeftRadius: 0, borderTopRightRadius: 0 }}>
                                    <strong style={{ fontSize: '0.9rem' }}>Edit Person</strong>
                                    <CButton
                                      color="link"
                                      size="sm"
                                      className="p-0 text-white"
                                      onClick={() => {
                                        setFlippedPersonId(null);
                                        setSelectedPerson(null);
                                        setFormData(EMPTY_FORM);
                                        setTagInput('');
                                        setTagSuggestions([]);
                                      }}
                                    >
                                      <CIcon icon={cilX} />
                                    </CButton>
                                  </CCardHeader>
                                  <CCardBody className="d-flex flex-column" style={{ overflowY: 'auto', padding: '0.75rem' }}>
                                    <div className="flex-grow-1">
                                      <div className="mb-2">
                                        <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Name</CFormLabel>
                                        <CFormInput
                                          size="sm"
                                          value={isFlipped ? formData.name : ''}
                                          onChange={(e) => setFormData(prev => ({ ...prev, name: e.target.value }))}
                                          placeholder="Full name"
                                        />
                                      </div>
                                      <div className="mb-2">
                                        <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Birth Year</CFormLabel>
                                        <CFormInput
                                          size="sm"
                                          type="number"
                                          min="1900"
                                          max={new Date().getFullYear()}
                                          value={isFlipped ? formData.birth_year : ''}
                                          onChange={(e) => setFormData(prev => ({ ...prev, birth_year: e.target.value }))}
                                          placeholder="e.g. 1985"
                                        />
                                      </div>
                                      <div className="mb-2">
                                        <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Day &amp; Month</CFormLabel>
                                        <div className="d-flex gap-1">
                                          <select
                                            className="form-select form-select-sm"
                                            value={isFlipped ? formData.birth_month : ''}
                                            onChange={(e) => setFormData(prev => ({ ...prev, birth_month: e.target.value }))}
                                          >
                                            <option value="">Month</option>
                                            {MONTHS.map(m => <option key={m.value} value={m.value}>{m.label}</option>)}
                                          </select>
                                          <select
                                            className="form-select form-select-sm"
                                            style={{ maxWidth: '80px' }}
                                            value={isFlipped ? formData.birth_day : ''}
                                            onChange={(e) => setFormData(prev => ({ ...prev, birth_day: e.target.value }))}
                                          >
                                            <option value="">Day</option>
                                            {Array.from({ length: 31 }, (_, i) => {
                                              const d = String(i + 1).padStart(2, '0');
                                              return <option key={d} value={d}>{i + 1}</option>;
                                            })}
                                          </select>
                                        </div>
                                      </div>
                                      <div className="mb-2">
                                        <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Category</CFormLabel>
                                        <CreatableSelect
                                          isClearable={false}
                                          value={isFlipped && formData.category ? { value: formData.category, label: formData.category } : { value: 'Other', label: 'Other' }}
                                          onChange={(selected) => setFormData(prev => ({ ...prev, category: selected ? selected.value : 'Other' }))}
                                          options={categories.map(c => ({ value: c, label: c }))}
                                          placeholder="Select or type category..."
                                          formatCreateLabel={(v) => `New category: "${v}"`}
                                          styles={{
                                            control: (base) => ({ ...base, fontSize: '0.82rem', minHeight: '32px' }),
                                            menu: (base) => ({ ...base, fontSize: '0.82rem', zIndex: 9999 }),
                                          }}
                                        />
                                      </div>
                                      <div className="mb-2">
                                        <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Description</CFormLabel>
                                        <CFormTextarea
                                          size="sm"
                                          rows={3}
                                          value={isFlipped ? formData.description : ''}
                                          onChange={(e) => setFormData(prev => ({ ...prev, description: e.target.value }))}
                                          placeholder="Description"
                                          style={{ fontSize: '0.82rem', resize: 'none' }}
                                        />
                                      </div>
                                      <div className="mb-2">
                                        <CFormLabel style={{ fontSize: '0.72rem', textTransform: 'uppercase', letterSpacing: '0.5px', color: '#999', marginBottom: 2 }}>Tags</CFormLabel>
                                        <CreatableSelect
                                          isMulti
                                          isClearable
                                          value={(isFlipped ? formData.tags : []).map(t => ({ value: t, label: t }))}
                                          onChange={(selected) => {
                                            setFormData(prev => ({
                                              ...prev,
                                              tags: (selected || []).map(s => s.value),
                                            }));
                                          }}
                                          onInputChange={(inputValue) => {
                                            if (inputValue.trim().length > 0) {
                                              tagAPI.getAll(activeCatalog.id, inputValue.trim())
                                                .then(response => {
                                                  const existing = (response.data || []).map(t => t.name || t);
                                                  setTagSuggestions(existing);
                                                })
                                                .catch(() => setTagSuggestions([]));
                                            } else {
                                              setTagSuggestions([]);
                                            }
                                            return inputValue;
                                          }}
                                          options={tagSuggestions.map(t => ({ value: t, label: t }))}
                                          placeholder="Add tags..."
                                          formatCreateLabel={(inputValue) => `Create tag "${inputValue}"`}
                                          styles={{
                                            control: (base) => ({ ...base, fontSize: '0.82rem', minHeight: '32px' }),
                                            menu: (base) => ({ ...base, fontSize: '0.82rem', zIndex: 9999 }),
                                            multiValue: (base) => ({ ...base, backgroundColor: '#a8c8f8' }),
                                            multiValueLabel: (base) => ({ ...base, color: '#1a3a5c', fontSize: '0.75rem' }),
                                          }}
                                        />
                                      </div>
                                    </div>
                                    <div className="d-flex gap-2 mt-2 pt-2 border-top">
                                      <CButton
                                        color="primary"
                                        size="sm"
                                        className="flex-grow-1"
                                        onClick={handleEditPerson}
                                      >
                                        <CIcon icon={cilSave} className="me-1" />
                                        Save
                                      </CButton>
                                      <CButton
                                        color="danger"
                                        variant="outline"
                                        size="sm"
                                        onClick={() => {
                                          setShowDeleteConfirmModal(true);
                                        }}
                                      >
                                        <CIcon icon={cilTrash} />
                                      </CButton>
                                    </div>
                                  </CCardBody>
                                </CCard>
                              </div>
                            </div>
                          </div>
            </div>
          );
        };

        return (
          <>
            {sortedCategories.map((category) => (
              <div key={category} className="mb-4">
                <div
                  style={{
                    fontSize: '1.1rem',
                    fontWeight: 700,
                    color: '#1a73e8',
                    borderBottom: '2px solid #e3eaf8',
                    paddingBottom: '0.4rem',
                    marginBottom: '1rem',
                  }}
                >
                  {renaming?.from === category ? (
                    <div className="d-flex align-items-center gap-2 flex-wrap">
                      <CFormInput
                        size="sm"
                        autoFocus
                        style={{ maxWidth: '260px' }}
                        value={renaming.to}
                        disabled={renameSaving}
                        aria-label={`New name for ${category}`}
                        onChange={(e) => setRenaming({ ...renaming, to: e.target.value })}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') saveCategoryName();
                          if (e.key === 'Escape') { setRenaming(null); setRenameError(null); }
                        }}
                      />
                      <CButton size="sm" color="primary" disabled={renameSaving || !renaming.to.trim()}
                               onClick={saveCategoryName} title="Save the new name">
                        {renameSaving ? <CSpinner size="sm" /> : <CIcon icon={cilSave} />}
                      </CButton>
                      <CButton size="sm" color="secondary" variant="outline" disabled={renameSaving}
                               onClick={() => { setRenaming(null); setRenameError(null); }} title="Cancel">
                        <CIcon icon={cilX} />
                      </CButton>
                      {renameError && (
                        <span className="text-danger" style={{ fontSize: '0.85rem', fontWeight: 400 }}>
                          {renameError}
                        </span>
                      )}
                    </div>
                  ) : (
                    <>
                      {category}
                      {!BUILT_IN_CATEGORIES.includes(category) && (
                        <CButton size="sm" color="link" className="ms-2 p-0 align-baseline"
                                 title={`Rename ${category}`} aria-label={`Rename ${category}`}
                                 onClick={() => { setRenaming({ from: category, to: category }); setRenameError(null); }}>
                          <CIcon icon={cilPencil} />
                        </CButton>
                      )}
                    </>
                  )}
                </div>
                <div style={{ display: 'flex', flexWrap: 'wrap', gap: '16px' }}>
                  {categoryMap[category].map((person) => renderPersonCard(person))}
                </div>
              </div>
            ))}
          </>
        );
      })()}

      {/* Add Person Modal */}
      <CModal visible={showAddModal} onClose={() => setShowAddModal(false)}>
        <CModalHeader>
          <CModalTitle>Add New Person</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <CForm>
            <div className="mb-3">
              <CFormLabel>Name *</CFormLabel>
              <CFormInput
                value={formData.name}
                onChange={(e) => setFormData({ ...formData, name: e.target.value })}
                placeholder="Enter name"
              />
            </div>
            <div className="mb-3">
              <CFormLabel>Birth Year <span className="text-muted" style={{ fontWeight: 'normal', fontSize: '0.88em' }}>(optional)</span></CFormLabel>
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
              <CFormLabel>Day &amp; Month <span className="text-muted" style={{ fontWeight: 'normal', fontSize: '0.88em' }}>(optional)</span></CFormLabel>
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
              <CFormLabel>Category</CFormLabel>
              <CreatableSelect
                isClearable={false}
                value={formData.category ? { value: formData.category, label: formData.category } : { value: 'Other', label: 'Other' }}
                onChange={(selected) => setFormData({ ...formData, category: selected ? selected.value : 'Other' })}
                options={categories.map(c => ({ value: c, label: c }))}
                placeholder="Select or type category..."
                formatCreateLabel={(v) => `New category: "${v}"`}
                styles={{
                  control: (base) => ({ ...base, fontSize: '0.875rem' }),
                  menu: (base) => ({ ...base, zIndex: 9999 }),
                }}
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
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" onClick={() => setShowAddModal(false)}>
            Cancel
          </CButton>
          <CButton color="primary" onClick={handleAddPerson} disabled={!formData.name}>
            Add Person
          </CButton>
        </CModalFooter>
      </CModal>


      {/* Delete Person Confirmation Modal */}
      <CModal visible={showDeleteConfirmModal} onClose={() => setShowDeleteConfirmModal(false)}>
        <CModalHeader>
          <CModalTitle>Delete Person</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p>Are you sure you want to delete <strong>{selectedPerson?.name}</strong> and all their photos?</p>
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

export default People;
