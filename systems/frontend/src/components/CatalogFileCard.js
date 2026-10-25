import React, { useState } from 'react';
import {
  CCard, CCardHeader, CCardBody, CButton, CSpinner, CBadge,
  CModal, CModalHeader, CModalTitle, CModalBody, CModalFooter,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilDescription, cilTrash } from '@coreui/icons';
import { photoAPI } from '../services/api';

// ─── The catalog's own file, and removing it ─────────────────────────────────
// Shared by Photos and Videos. Names the original file(s) this item is in the
// catalog - both halves of a RAW+JPG pair - and where they sit in the catalog
// directory, which is what the user needs to find them to delete by hand.
//
// Removal (POST /files/<id>/remove) never touches the original. It deletes what
// the application made from it and takes it out of events and albums; the file
// then appears on the Removed files page until it is deleted from disk.

const CatalogFileCard = ({ catalogId, detail, kind = 'photo', onRemoved }) => {
  const [confirming, setConfirming] = useState(false);
  const [removing, setRemoving] = useState(false);
  const [error, setError] = useState(null);

  if (!detail) return null;

  const files = detail.image_group?.files?.length
    ? detail.image_group.files.map((f) => ({ id: f.id, name: f.file_name, primary: f.is_primary }))
    : [{ id: detail.id, name: detail.file_name, primary: true }];

  const remove = async () => {
    setRemoving(true);
    setError(null);
    try {
      const res = await photoAPI.removeFromCatalog(catalogId, detail.id);
      setConfirming(false);
      onRemoved?.(res.data);
    } catch (e) {
      setError(e?.response?.data?.error || 'Could not remove it from the catalog.');
    } finally {
      setRemoving(false);
    }
  };

  return (
    <>
      <CCard>
        <CCardHeader className="d-flex align-items-center py-2">
          <CIcon icon={cilDescription} className="me-2" />
          <strong>File</strong>
          {files.length > 1 && (
            <CBadge color="info" className="ms-2" style={{ fontSize: '0.65rem' }}>RAW + JPG</CBadge>
          )}
        </CCardHeader>
        <CCardBody className="py-2">
          {files.map((f) => (
            <div key={f.id} className="mb-1"
                 style={{ fontSize: '0.8rem', fontWeight: f.primary ? 500 : 400, wordBreak: 'break-all' }}>
              {f.name}
            </div>
          ))}
          {detail.file_path && (
            <div className="mb-2" style={{ fontSize: '0.7rem', color: '#888', wordBreak: 'break-all' }}
                 title="Location inside the catalog">
              {detail.file_path.includes('/')
                ? detail.file_path.slice(0, detail.file_path.lastIndexOf('/') + 1)
                : detail.file_path}
            </div>
          )}
          {error && <div className="mb-2" style={{ fontSize: '0.72rem', color: '#c00' }}>{error}</div>}
          <CButton size="sm" color="danger" variant="outline" className="w-100"
                   onClick={() => { setError(null); setConfirming(true); }}>
            <CIcon icon={cilTrash} className="me-1" />Remove from catalog
          </CButton>
        </CCardBody>
      </CCard>

      <CModal visible={confirming} onClose={removing ? undefined : () => setConfirming(false)}
              backdrop="static">
        <CModalHeader closeButton={!removing}>
          <CModalTitle>Remove this {kind} from the catalog?</CModalTitle>
        </CModalHeader>
        <CModalBody style={{ fontSize: '0.9rem' }}>
          <p className="mb-2">
            {files.length > 1 ? 'Both files stay' : 'The file stays'} on disk:
          </p>
          <ul className="mb-3" style={{ fontSize: '0.8rem', wordBreak: 'break-all' }}>
            {files.map((f) => <li key={f.id}>{f.name}</li>)}
          </ul>
          <p className="mb-2">What removal does:</p>
          <ul className="mb-3" style={{ fontSize: '0.85rem' }}>
            <li>deletes every size, thumbnail{kind === 'video' ? ', poster and playable copy' : ''} made from it</li>
            <li>takes it out of its events and albums</li>
            <li>hides it everywhere in the catalog; face tags are kept but hidden</li>
          </ul>
          <p className="mb-0" style={{ fontSize: '0.85rem', color: '#666' }}>
            It will be listed on <strong>Removed files</strong> until you delete the original by
            hand. You can restore it from there while the original is still on disk.
          </p>
          {error && <div className="mt-2" style={{ fontSize: '0.8rem', color: '#c00' }}>{error}</div>}
        </CModalBody>
        <CModalFooter>
          <CButton color="secondary" variant="ghost" onClick={() => setConfirming(false)}
                   disabled={removing}>
            Cancel
          </CButton>
          <CButton color="danger" onClick={remove} disabled={removing}>
            {removing ? <CSpinner size="sm" /> : 'Remove from catalog'}
          </CButton>
        </CModalFooter>
      </CModal>
    </>
  );
};

export default CatalogFileCard;
