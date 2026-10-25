import React, { useState, useEffect, useCallback } from 'react';
import {
  CCard, CCardBody, CCardHeader, CContainer, CButton, CModal, CModalHeader,
  CModalTitle, CModalBody, CModalFooter, CForm, CFormInput, CFormSelect,
  CFormTextarea, CFormLabel, CSpinner, CAlert, CBadge,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPlus, cilPencil, cilX, cilSave, cilPaw } from '@coreui/icons';
import { useNavigate } from 'react-router-dom';
import CreatableSelect from 'react-select/creatable';
import { petAPI, peopleCategoryAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { API_HOST } from '../config';

// ─── Pets ─────────────────────────────────────────────────────────────────────
//
// One card per NAMED pet, and nothing else. Unknown animals are never cards:
// they are groups of detections on Identify pets, and naming a group is what
// creates the row this page lists. A row in `people` means somebody said who
// this is, which is why a detector cannot put one here.
//
// A pet is a `people` row with kind='pet', so the fields are a person's fields
// plus `species`, and the CATEGORY VOCABULARY IS SHARED - a pet sits in Family
// beside its owners, because category answers "whose household" where kind
// answers "what species".
//
// See documentation/specs/pet-recognition-spec.md section 8.1.

const SPECIES = [
  { value: '', label: '(unknown)' },
  { value: 'dog', label: 'Dog' },
  { value: 'cat', label: 'Cat' },
  { value: 'other', label: 'Other' },
];

const emptyPet = { name: '', species: 'dog', category: 'Family',
                   date_of_birth: '', description: '' };

const Pets = () => {
  const navigate = useNavigate();
  const { activeCatalog } = useCatalog();

  const [pets, setPets] = useState([]);
  const [settings, setSettings] = useState(null);
  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);

  const [showModal, setShowModal] = useState(false);
  const [editing, setEditing] = useState(null);     // null = creating
  const [form, setForm] = useState(emptyPet);
  const [saving, setSaving] = useState(false);

  const catalogId = activeCatalog?.id;

  const load = useCallback(async () => {
    if (!catalogId) { setLoading(false); return; }
    setLoading(true);
    setError(null);
    try {
      const [p, s, c] = await Promise.all([
        petAPI.getAll(catalogId),
        petAPI.settings(catalogId),
        peopleCategoryAPI.getAll(catalogId).catch(() => ({ data: [] })),
      ]);
      setPets(p.data || []);
      setSettings(s.data || null);
      setCategories((c.data || []).map((x) => (typeof x === 'string' ? x : x.name)));
    } catch (e) {
      setError(e.response?.data?.error || e.message || 'Could not load pets');
    } finally {
      setLoading(false);
    }
  }, [catalogId]);

  useEffect(() => { load(); }, [load]);


  const openCreate = () => { setEditing(null); setForm(emptyPet); setShowModal(true); };
  const openEdit = (pet) => {
    setEditing(pet);
    setForm({
      name: pet.name || '',
      species: pet.species || '',
      category: pet.category || 'Family',
      date_of_birth: pet.date_of_birth || '',
      description: pet.description || '',
    });
    setShowModal(true);
  };

  const save = async () => {
    if (!form.name.trim()) { setError('A name is required'); return; }
    setSaving(true);
    try {
      if (editing) {
        await petAPI.update(catalogId, editing.id, form);
      } else {
        await petAPI.create(catalogId, form);
      }
      setShowModal(false);
      load();
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    } finally {
      setSaving(false);
    }
  };

  if (!catalogId) {
    return (
      <CContainer className="py-4">
        <CAlert color="info">Choose a catalog to see its pets.</CAlert>
      </CContainer>
    );
  }

  const enabled = settings?.enabled;
  const waiting = settings?.unresolved_groups || 0;

  return (
    <CContainer fluid className="py-3">
      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}
      {notice && <CAlert color="info" dismissible onClose={() => setNotice(null)}>{notice}</CAlert>}

      {/* What this catalog knows about its animals.
          The on/off switch is NOT here any more: it belongs to the catalog,
          alongside the face recognition metric, and is set when the catalog is
          created. Two places to turn one thing on is one place too many. */}
      <CCard className="mb-3">
        <CCardBody className="d-flex flex-wrap align-items-center gap-4">
          <div className="d-flex align-items-center gap-2">
            <CIcon icon={cilPaw} style={{ width: 22, height: 22, color: '#6c757d' }} />
            <h5 className="mb-0 fw-bold">Pets</h5>
          </div>

          {settings && (
            <div className="d-flex flex-wrap gap-3 text-body-secondary" style={{ fontSize: 13 }}>
              <span><strong>{(settings.detections || 0).toLocaleString()}</strong> animals found</span>
              <span><strong>{settings.pets || 0}</strong> named</span>
              <span><strong>{(settings.embedded || 0).toLocaleString()}</strong> measured</span>
              {Object.entries(settings.species || {}).map(([k, v]) => (
                <span key={k}>{v.toLocaleString()} {k}</span>
              ))}
            </div>
          )}

          <div className="ms-auto d-flex gap-2">
            {waiting > 0 && (
              <CButton color="warning" variant="outline" size="sm"
                       onClick={() => navigate('/identify-pets')}>
                {waiting} group{waiting === 1 ? '' : 's'} to name
              </CButton>
            )}
            <CButton color="primary" size="sm" onClick={openCreate}>
              <CIcon icon={cilPlus} className="me-1" style={{ width: 14, height: 14 }} />
              Add a pet
            </CButton>
          </div>
        </CCardBody>
      </CCard>

      {!enabled && (
        <CAlert color="secondary">
          Pet detection is off for this catalog, so nothing looks for animals and
          no models load. You can still add a pet by hand and tag its photographs
          yourself. To have them found automatically, turn{' '}
          <strong>Pets</strong> on in{' '}
          <CButton color="link" className="p-0 align-baseline"
                   onClick={() => navigate('/catalogs')}>this catalog&apos;s settings</CButton>
          {' '}— it adds two workflows that run after Find Faces, which is
          where animals are found.
        </CAlert>
      )}

      {loading ? (
        <div className="text-center py-5"><CSpinner /></div>
      ) : pets.length === 0 ? (
        <CCard><CCardBody className="text-center text-body-secondary py-5">
          No pets yet. {enabled
            ? 'Once Find Faces has run, animals it finds are grouped on Identify pets, and naming a group creates a pet here.'
            : 'Add one by hand, or turn pet detection on in the catalog settings.'}
        </CCardBody></CCard>
      ) : (
        <div className="d-flex flex-wrap gap-3">
          {pets.map((pet) => (
            <CCard key={pet.id} style={{ width: 230 }}>
              <CCardHeader className="d-flex align-items-start justify-content-between py-2">
                <div style={{ minWidth: 0 }}>
                  <div className="fw-semibold text-truncate" title={pet.name}>{pet.name}</div>
                  <div className="d-flex gap-1 mt-1">
                    {pet.species && <CBadge color="secondary">{pet.species}</CBadge>}
                    {pet.category && <CBadge color="light" className="text-body">{pet.category}</CBadge>}
                  </div>
                </div>
                <button
                  onClick={(e) => { e.stopPropagation(); openEdit(pet); }}
                  title="Edit pet"
                  style={{ background: 'none', border: '1px solid #e0e0e0', borderRadius: 4,
                           padding: '2px 6px', cursor: 'pointer', color: '#666', lineHeight: 1 }}
                >
                  <CIcon icon={cilPencil} style={{ width: 13, height: 13 }} />
                </button>
              </CCardHeader>

              <div
                onClick={() => navigate(`/pets/${catalogId}/${pet.id}`)}
                style={{ height: 150, background: '#f5f5f5', cursor: 'pointer',
                         display: 'flex', alignItems: 'center', justifyContent: 'center',
                         overflow: 'hidden' }}
              >
                {/* The crop the identify pass already wrote, not the whole
                    photograph. cover_crop_url is the starred one if somebody
                    picked a favourite and the most confident one otherwise, so
                    a card has a picture from the moment its group is named. */}
                {pet.cover_crop_url ? (
                  <img
                    alt={pet.name}
                    src={`${API_HOST}${pet.cover_crop_url}`}
                    style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                  />
                ) : pet.profile_photo_display_path ? (
                  <img
                    alt={pet.name}
                    src={`${API_HOST}/api/catalogs/files?path=${encodeURIComponent(pet.profile_photo_display_path)}`}
                    style={{ width: '100%', height: '100%', objectFit: 'cover' }}
                  />
                ) : (
                  <CIcon icon={cilPaw} style={{ width: 40, height: 40, color: '#c7c7c7' }} />
                )}
              </div>

              <CCardBody className="py-2" style={{ fontSize: 12 }}>
                {pet.description
                  ? <div className="text-body-secondary text-truncate" title={pet.description}>{pet.description}</div>
                  : <div className="text-body-secondary fst-italic">No description</div>}
                {pet.photo_count > 0 && (
                  <div className="text-body-secondary mt-1">
                    {pet.photo_count.toLocaleString()} photograph{pet.photo_count === 1 ? '' : 's'}
                  </div>
                )}
              </CCardBody>
            </CCard>
          ))}
        </div>
      )}

      <CModal visible={showModal} onClose={() => setShowModal(false)} alignment="center">
        <CModalHeader>
          <CModalTitle>{editing ? `Edit ${editing.name}` : 'Add a pet'}</CModalTitle>
        </CModalHeader>
        <CModalBody>
          <CForm>
            <div className="mb-3">
              <CFormLabel>Name</CFormLabel>
              <CFormInput value={form.name} autoFocus
                          onChange={(e) => setForm({ ...form, name: e.target.value })} />
            </div>

            <div className="mb-3">
              <CFormLabel>Species</CFormLabel>
              <CFormSelect value={form.species}
                           onChange={(e) => setForm({ ...form, species: e.target.value })}>
                {SPECIES.map((s) => <option key={s.value} value={s.value}>{s.label}</option>)}
              </CFormSelect>
            </div>

            {/* The SAME vocabulary the People page uses, on purpose: a pet
                belongs to a household, so Rex sits in Family beside his
                owners. Not a separate list of pet categories. */}
            <div className="mb-3">
              <CFormLabel>Household</CFormLabel>
              <CreatableSelect
                isClearable
                value={form.category ? { value: form.category, label: form.category } : null}
                options={categories.map((c) => ({ value: c, label: c }))}
                onChange={(opt) => setForm({ ...form, category: opt ? opt.value : '' })}
                placeholder="Family, or whoever this pet belongs to"
              />
            </div>

            <div className="mb-3">
              <CFormLabel>Born <span className="text-body-secondary">(optional)</span></CFormLabel>
              <CFormInput value={form.date_of_birth} placeholder="2019, 2019-04-02, --04-02"
                          onChange={(e) => setForm({ ...form, date_of_birth: e.target.value })} />
            </div>

            <div className="mb-1">
              <CFormLabel>Description <span className="text-body-secondary">(optional)</span></CFormLabel>
              <CFormTextarea rows={2} value={form.description}
                             onChange={(e) => setForm({ ...form, description: e.target.value })} />
            </div>
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setShowModal(false)}>
            <CIcon icon={cilX} className="me-1" style={{ width: 14, height: 14 }} />Cancel
          </CButton>
          <CButton color="primary" onClick={save} disabled={saving}>
            <CIcon icon={cilSave} className="me-1" style={{ width: 14, height: 14 }} />
            {saving ? 'Saving…' : 'Save'}
          </CButton>
        </CModalFooter>
      </CModal>
    </CContainer>
  );
};

export default Pets;
