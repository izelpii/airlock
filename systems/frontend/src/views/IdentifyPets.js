import React, { useState, useEffect, useCallback } from 'react';
import {
  CCard, CCardBody, CCardHeader, CContainer, CButton, CBadge, CSpinner, CAlert,
  CFormRange, CModal, CModalHeader, CModalTitle, CModalBody, CModalFooter,
  CForm, CFormInput, CFormSelect, CFormLabel,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPaw, cilCheck, cilBan } from '@coreui/icons';
import CreatableSelect from 'react-select/creatable';
import { petAPI, peopleCategoryAPI } from '../services/api';
import { useCatalog } from '../context/CatalogContext';
import { API_HOST } from '../config';

// ─── Identify pets ────────────────────────────────────────────────────────────
//
// Groups of animals nobody has named yet. NOT cards, and not pets: no `people`
// row exists for any of these. A row in `people` means somebody said who this
// is, which is why naming a group here is the only thing that creates one.
//
// Naming is one request, and it does the whole promotion: creates the pet,
// resolves the group, and writes person_id to every member detection. That is
// the feature - name the dog once and five hundred photographs start answering
// "photos with Rex", with nobody tagging anything by hand.
//
// The slider is the same idea as Identify groups': single-link grouping can
// chain two animals together through an intermediate crop, so a loose group is
// the one to distrust. `max_member_distance` is what it filters on.
//
// See documentation/specs/pet-recognition-spec.md section 8.3.

const SPECIES = [
  { value: '', label: '(unknown)' },
  { value: 'dog', label: 'Dog' },
  { value: 'cat', label: 'Cat' },
  { value: 'other', label: 'Other' },
];

const IdentifyPets = () => {
  const { activeCatalog } = useCatalog();
  const catalogId = activeCatalog?.id;

  const [clusters, setClusters] = useState([]);
  const [pets, setPets] = useState([]);
  const [categories, setCategories] = useState([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [notice, setNotice] = useState(null);
  const [minMembers, setMinMembers] = useState(3);

  const [naming, setNaming] = useState(null);     // the cluster being named
  const [form, setForm] = useState({ name: '', species: '', category: 'Family',
                                     person_id: '' });
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    if (!catalogId) { setLoading(false); return; }
    setLoading(true);
    try {
      const [c, p, cat] = await Promise.all([
        petAPI.clusters(catalogId, { min_members: minMembers }),
        petAPI.getAll(catalogId),
        peopleCategoryAPI.getAll(catalogId).catch(() => ({ data: [] })),
      ]);
      setClusters(c.data || []);
      setPets(p.data || []);
      setCategories((cat.data || []).map((x) => (typeof x === 'string' ? x : x.name)));
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    } finally {
      setLoading(false);
    }
  }, [catalogId, minMembers]);

  useEffect(() => { load(); }, [load]);

  const openNaming = (cluster) => {
    setNaming(cluster);
    setForm({ name: '', species: cluster.species || '', category: 'Family',
              person_id: '' });
  };

  const submitName = async () => {
    if (!form.person_id && !form.name.trim()) {
      setError('Pick an existing pet, or give a name for a new one');
      return;
    }
    setSaving(true);
    try {
      const body = form.person_id
        ? { person_id: Number(form.person_id) }
        : { name: form.name.trim(), species: form.species, category: form.category };
      const r = await petAPI.nameCluster(catalogId, naming.id, body);
      const n = r.data?.detections_named ?? 0;
      setNaming(null);
      setClusters((cs) => cs.filter((c) => c.id !== naming.id));
      setError(null);
      setNotice(`Named ${n} photograph${n === 1 ? '' : 's'}.`);
      petAPI.getAll(catalogId).then((p) => setPets(p.data || [])).catch(() => {});
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    } finally {
      setSaving(false);
    }
  };

  const reject = async (cluster) => {
    try {
      await petAPI.rejectCluster(catalogId, cluster.id);
      // Rejected groups stay out of future offers without the detections being
      // deleted - they are still a measurement, and the grouping pass must not
      // simply find them again next run.
      setClusters((cs) => cs.filter((c) => c.id !== cluster.id));
    } catch (e) {
      setError(e.response?.data?.error || e.message);
    }
  };

  if (!catalogId) {
    return (
      <CContainer className="py-4">
        <CAlert color="info">Choose a catalog to review its animals.</CAlert>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="py-3">
      {error && <CAlert color="danger" dismissible onClose={() => setError(null)}>{error}</CAlert>}
      {notice && <CAlert color="success" dismissible onClose={() => setNotice(null)}>{notice}</CAlert>}

      <CCard className="mb-3">
        <CCardBody className="d-flex flex-wrap align-items-center gap-4">
          <div className="d-flex align-items-center gap-2">
            <CIcon icon={cilPaw} style={{ width: 22, height: 22, color: '#6c757d' }} />
            <h5 className="mb-0 fw-bold">Identify pets</h5>
          </div>
          <div style={{ minWidth: 260 }}>
            <label className="text-body-secondary" style={{ fontSize: 12 }}>
              At least {minMembers} photograph{minMembers === 1 ? '' : 's'} per group
            </label>
            <CFormRange min={1} max={12} value={minMembers}
                        onChange={(e) => setMinMembers(Number(e.target.value))} />
          </div>
          <div className="text-body-secondary" style={{ fontSize: 13 }}>
            {clusters.length} group{clusters.length === 1 ? '' : 's'} waiting
          </div>
        </CCardBody>
      </CCard>

      {loading ? (
        <div className="text-center py-5"><CSpinner /></div>
      ) : clusters.length === 0 ? (
        <CCard><CCardBody className="text-center text-body-secondary py-5">
          Nothing to name. Animals are found during Find Faces, measured by
          Measure and Match Pets, and grouped by Group Unnamed Pets — groups of {minMembers}
          or more appear here.
        </CCardBody></CCard>
      ) : clusters.map((cluster) => (
        <CCard key={cluster.id} className="mb-3">
          <CCardHeader className="d-flex align-items-center gap-3 py-2">
            <strong>{cluster.member_count} photograph{cluster.member_count === 1 ? '' : 's'}</strong>
            {cluster.species && <CBadge color="secondary">{cluster.species}</CBadge>}
            {/* A loose group is two animals chained through an intermediate
                crop. Saying so is more use than hiding it. */}
            {cluster.max_member_distance > 0.3 && (
              <CBadge color="warning">loose — may be more than one animal</CBadge>
            )}
            <span className="text-body-secondary" style={{ fontSize: 12 }}>
              cohesion {Number(cluster.cohesion || 0).toFixed(3)}
            </span>
            <div className="ms-auto d-flex gap-2">
              <CButton color="primary" size="sm" onClick={() => openNaming(cluster)}>
                <CIcon icon={cilCheck} className="me-1" style={{ width: 13, height: 13 }} />
                Name this one
              </CButton>
              <CButton color="secondary" variant="outline" size="sm"
                       onClick={() => reject(cluster)}
                       title="Not an animal, or not worth naming">
                <CIcon icon={cilBan} className="me-1" style={{ width: 13, height: 13 }} />
                Not a pet
              </CButton>
            </div>
          </CCardHeader>
          <CCardBody>
            <div className="d-flex flex-wrap gap-2">
              {(cluster.members || []).map((m) => (
                <div key={m.detection_id} style={{ width: 104 }}>
                  <div style={{ height: 104, background: '#f0f0f0', borderRadius: 4,
                                overflow: 'hidden', display: 'flex',
                                alignItems: 'center', justifyContent: 'center' }}>
                    {m.crop_url ? (
                      <img alt="" src={`${API_HOST}${m.crop_url}`}
                           style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
                    ) : (
                      <CIcon icon={cilPaw} style={{ width: 22, height: 22, color: '#c7c7c7' }} />
                    )}
                  </div>
                  <div className="text-body-secondary text-center" style={{ fontSize: 10 }}>
                    {Number(m.distance_to_centroid || 0).toFixed(3)}
                  </div>
                </div>
              ))}
            </div>
          </CCardBody>
        </CCard>
      ))}

      <CModal visible={!!naming} onClose={() => setNaming(null)} alignment="center">
        <CModalHeader>
          <CModalTitle>
            Name {naming?.member_count} photograph{naming?.member_count === 1 ? '' : 's'}
          </CModalTitle>
        </CModalHeader>
        <CModalBody>
          <p className="text-body-secondary" style={{ fontSize: 13 }}>
            Naming this group links every photograph in it to that pet at once.
          </p>
          <CForm>
            {pets.length > 0 && (
              <div className="mb-3">
                <CFormLabel>A pet you already have</CFormLabel>
                <CFormSelect value={form.person_id}
                             onChange={(e) => setForm({ ...form, person_id: e.target.value })}>
                  <option value="">— or create a new one below —</option>
                  {pets.map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}{p.species ? ` (${p.species})` : ''}
                    </option>
                  ))}
                </CFormSelect>
              </div>
            )}

            {!form.person_id && (
              <>
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
                <div className="mb-1">
                  <CFormLabel>Household</CFormLabel>
                  <CreatableSelect
                    isClearable
                    value={form.category ? { value: form.category, label: form.category } : null}
                    options={categories.map((c) => ({ value: c, label: c }))}
                    onChange={(opt) => setForm({ ...form, category: opt ? opt.value : '' })}
                    placeholder="Family, or whoever this pet belongs to"
                  />
                </div>
              </>
            )}
          </CForm>
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="outline" onClick={() => setNaming(null)}>
            Cancel
          </CButton>
          <CButton color="primary" onClick={submitName} disabled={saving}>
            {saving ? 'Naming…' : 'Name them'}
          </CButton>
        </CModalFooter>
      </CModal>
    </CContainer>
  );
};

export default IdentifyPets;
