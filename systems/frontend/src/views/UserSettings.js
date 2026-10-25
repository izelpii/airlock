import React, { useState, useEffect, useCallback } from 'react';
import {
  CContainer,
  CRow,
  CCol,
  CCard,
  CCardBody,
  CCardHeader,
  CForm,
  CFormLabel,
  CFormInput,
  CButton,
  CAlert,
  CSpinner,
  CModal,
  CModalHeader,
  CModalTitle,
  CModalBody,
  CBadge
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilUser, cilSave, cilMap, cilArrowLeft } from '@coreui/icons';
import { useNavigate, useParams } from 'react-router-dom';
import { MapContainer, TileLayer, Marker, useMapEvents } from 'react-leaflet';
import L from 'leaflet';
import 'leaflet/dist/leaflet.css';
import { PasswordCard, SessionsCard, LinkedPersonCard } from './AccountSettings';
import { useAuth } from '../context/AuthContext';
import { userAPI } from '../services/api';

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

// ─── An account's page ───────────────────────────────────────────────────────
// /users/:userId - opened by the pencil on a card in Admin > Users, or by your
// own name in the top bar. Everything the old Account Settings held: the
// profile, the linked person, the password and the signed-in devices. An
// administrator can open anyone's; everyone can open their own (the API
// enforces both). The gear's page is for site settings.

const UserSettings = () => {
  const navigate = useNavigate();
  const { userId: param } = useParams();
  const { user: me, isAdmin, refresh } = useAuth();
  const userId = Number(param) || me?.id;
  const own = me && userId === me.id;
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState(null);
  const [success, setSuccess] = useState(false);
  const [formData, setFormData] = useState({
    name: '',
    email: '',
    birthdate: '',
    address: '',
    city: '',
    state: '',
    country: '',
    lat: '',
    long: ''
  });
  
  // Map state
  const [mapModalVisible, setMapModalVisible] = useState(false);
  const [pinPosition, setPinPosition] = useState(null); // { lat, lng }

  useEffect(() => {
    fetchUserSettings();
  // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [userId]);

  const fromProfile = (data) => ({
    name: data.name || '',
    email: data.email || '',
    birthdate: data.birthdate || '',
    address: data.address || '',
    city: data.city || '',
    state: data.state || '',
    country: data.country || '',
    lat: data.lat != null ? String(data.lat) : '',
    long: data.long != null ? String(data.long) : '',
  });

  const fetchUserSettings = async () => {
    if (!userId) return;
    try {
      setLoading(true);
      setError(null);
      const { data } = await userAPI.getProfile(userId);
      setFormData(fromProfile(data));
    } catch (err) {
      setError(err?.response?.data?.error || 'Could not load this account.');
    } finally {
      setLoading(false);
    }
  };

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

  const handleInputChange = (e) => {
    const { name, value } = e.target;
    setFormData(prev => ({
      ...prev,
      [name]: value
    }));
    // Clear success message when user starts editing
    if (success) setSuccess(false);
  };

  const handleUseMap = () => {
    setMapModalVisible(true);
  };

  const handleCloseMapModal = () => {
    setMapModalVisible(false);
  };

  const handleMapClick = useCallback((lat, lng) => {
    const latStr = lat.toFixed(6);
    const lngStr = lng.toFixed(6);
    setPinPosition({ lat, lng });
    setFormData((prev) => ({ ...prev, lat: latStr, long: lngStr }));
  }, []);

  // Determine map center: use pin position, or formData coords, or default
  const getMapCenter = () => {
    if (pinPosition) return [pinPosition.lat, pinPosition.lng];
    const lat = parseFloat(formData.lat);
    const lng = parseFloat(formData.long);
    if (!isNaN(lat) && !isNaN(lng)) return [lat, lng];
    return [40.7128, -74.0060]; // Nothing to place it by yet: a neutral starting view
  };

  const getMapZoom = () => {
    if (pinPosition) return 13;
    const lat = parseFloat(formData.lat);
    const lng = parseFloat(formData.long);
    if (!isNaN(lat) && !isNaN(lng)) return 13;
    return 2;
  };

  const handleSubmit = async (e) => {
    e.preventDefault();
    
    if (!formData.name.trim()) {
      setError('Name is required');
      return;
    }

    try {
      setSaving(true);
      setError(null);
      setSuccess(false);

      // Two rows, as Admin > Users always saved them: the name and email the
      // account signs in as, and the profile.
      await userAPI.update(userId, { display_name: formData.name.trim(), email: formData.email.trim() });
      const { data } = await userAPI.updateProfile(userId, formData);
      setFormData(fromProfile(data));
      if (own) await refresh();       // the top bar shows your own name
      setSuccess(true);
      
      // Auto-hide success message after 3 seconds
      setTimeout(() => setSuccess(false), 3000);
    } catch (err) {
      setError(err?.response?.data?.error || err.message);
    } finally {
      setSaving(false);
    }
  };

  if (loading) {
    return (
      <CContainer fluid className="mt-4 px-4">
        <div className="text-center">
          <CSpinner color="primary" />
          <p className="mt-2">Loading the account...</p>
        </div>
      </CContainer>
    );
  }

  return (
    <CContainer fluid className="mt-4 px-4">
      <div className="d-flex align-items-center gap-2 mb-3">
        {isAdmin && (
          <CButton color="secondary" variant="ghost" size="sm" onClick={() => navigate('/users')}>
            <CIcon icon={cilArrowLeft} className="me-1" />Users
          </CButton>
        )}
        <h4 className="mb-0">{formData.name || 'Account'}{own ? ' (you)' : ''}</h4>
      </div>
      {/* Three equal columns: profile | linked person | password + devices. */}
      <CRow>
        <CCol xs={12} lg={4}>
          <CCard className="h-100">
            <CCardHeader className="d-flex align-items-center">
              <CIcon icon={cilUser} size="lg" className="me-2" />
              <h5 className="mb-0">User Settings</h5>
            </CCardHeader>
            <CCardBody>
              {error && (
                <CAlert color="danger" dismissible onClose={() => setError(null)}>
                  {error}
                </CAlert>
              )}
              
              {success && (
                <CAlert color="success" dismissible onClose={() => setSuccess(false)}>
                  Settings updated successfully!
                </CAlert>
              )}

              <CForm onSubmit={handleSubmit}>
                <div className="mb-3">
                  <CFormLabel htmlFor="name">Name *</CFormLabel>
                  <CFormInput
                    type="text"
                    id="name"
                    name="name"
                    value={formData.name}
                    onChange={handleInputChange}
                    placeholder="Enter your name"
                    required
                  />
                </div>

                <div className="mb-3">
                  <CFormLabel htmlFor="email">Email</CFormLabel>
                  <CFormInput
                    type="email"
                    id="email"
                    name="email"
                    value={formData.email}
                    onChange={handleInputChange}
                    placeholder="Enter your email"
                  />
                </div>

                <div className="mb-3">
                  <CFormLabel htmlFor="birthdate">Birthdate</CFormLabel>
                  <CFormInput
                    type="date"
                    id="birthdate"
                    name="birthdate"
                    value={formData.birthdate}
                    onChange={handleInputChange}
                  />
                </div>

                <div className="mb-3">
                  <CFormLabel htmlFor="address">Address</CFormLabel>
                  <CFormInput
                    type="text"
                    id="address"
                    name="address"
                    value={formData.address}
                    onChange={handleInputChange}
                    placeholder="Enter your address"
                  />
                </div>

                <div className="mb-3">
                  <CFormLabel htmlFor="city">City</CFormLabel>
                  <CFormInput
                    type="text"
                    id="city"
                    name="city"
                    value={formData.city}
                    onChange={handleInputChange}
                    placeholder="Enter your city"
                  />
                </div>

                <div className="mb-3">
                  <CFormLabel htmlFor="state">State</CFormLabel>
                  <CFormInput
                    type="text"
                    id="state"
                    name="state"
                    value={formData.state}
                    onChange={handleInputChange}
                    placeholder="Enter your state"
                  />
                </div>

                <div className="mb-3">
                  <CFormLabel htmlFor="country">Country</CFormLabel>
                  <CFormInput
                    type="text"
                    id="country"
                    name="country"
                    value={formData.country}
                    onChange={handleInputChange}
                    placeholder="Enter your country"
                  />
                </div>

                <div className="mb-3">
                  <CFormLabel htmlFor="lat">Latitude</CFormLabel>
                  <div className="d-flex gap-2">
                    <CFormInput
                      type="number"
                      step="any"
                      id="lat"
                      name="lat"
                      value={formData.lat}
                      onChange={handleInputChange}
                      placeholder="e.g. 48.8566"
                      style={{ flex: 1 }}
                    />
                    <CButton
                      color="info"
                      size="sm"
                      onClick={handleUseMap}
                      title="Open map to drop a pin and set coordinates"
                    >
                      <CIcon icon={cilMap} className="me-1" style={{ width: 13, height: 13 }} />
                      Use Map
                    </CButton>
                  </div>
                </div>

                <div className="mb-3">
                  <CFormLabel htmlFor="long">Longitude</CFormLabel>
                  <CFormInput
                    type="number"
                    step="any"
                    id="long"
                    name="long"
                    value={formData.long}
                    onChange={handleInputChange}
                    placeholder="e.g. 2.3522"
                  />
                </div>

                <div className="d-grid gap-2 d-md-flex justify-content-md-end">
                  <CButton
                    color="primary"
                    type="submit"
                    disabled={saving}
                  >
                    {saving ? (
                      <>
                        <CSpinner size="sm" className="me-2" />
                        Saving...
                      </>
                    ) : (
                      <>
                        <CIcon icon={cilSave} className="me-2" />
                        Save Settings
                      </>
                    )}
                  </CButton>
                </div>
              </CForm>
            </CCardBody>
          </CCard>
        </CCol>

        {/* Column 2: the person this account borrows its details from. */}
        <CCol xs={12} lg={4}>
          <LinkedPersonCard userId={userId} />
        </CCol>

        {/* Column 3 stacks two cards, so neither takes h-100 — that would make
            each try to fill the column and leave them mis-sized. */}
        <CCol xs={12} lg={4}>
          <PasswordCard userId={userId} />
          <SessionsCard userId={userId} />
        </CCol>
      </CRow>

      {/* Map Modal */}
      <CModal visible={mapModalVisible} onClose={handleCloseMapModal} size="lg">
        <CModalHeader>
          <CModalTitle>
            Select Location on Map
            {pinPosition && (
              <CBadge color="info" className="ms-2" style={{ fontSize: '0.7rem' }}>
                📍 {pinPosition.lat.toFixed(5)}°, {pinPosition.lng.toFixed(5)}°
              </CBadge>
            )}
          </CModalTitle>
        </CModalHeader>
        <CModalBody className="p-0" style={{ minHeight: '500px', position: 'relative' }}>
          <div
            className="text-muted small px-3 py-2"
            style={{ backgroundColor: '#f0f9ff', borderBottom: '1px solid #bee3f8' }}
          >
            Click anywhere on the map to drop a pin and set the coordinates.
          </div>
          <MapContainer
            key={`map-${mapModalVisible}`}
            center={getMapCenter()}
            zoom={getMapZoom()}
            style={{ height: '500px', width: '100%', cursor: 'crosshair' }}
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
        </CModalBody>
      </CModal>

    </CContainer>
  );
};

export default UserSettings;
