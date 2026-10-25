import React, { useState } from 'react';
import { Link, useLocation, useParams } from 'react-router-dom';
import { CAlert } from '@coreui/react';
import TransferBatch from '../components/TransferBatch';

// ─── One album, several people, full page ─────────────────────────────────────
// /albums/transfers/batches/:batchId — where creating a group lands, and where
// a history entry for a group leads (album-mass-transfer-spec.md 7.3).
//
// The screen is TransferBatch, which the Transfers page also draws; this is the
// wrapper: the id from the URL, and the notice the album card passes along when
// some originals are missing on disk.
const AlbumTransferBatch = () => {
  const { batchId } = useParams();
  const location = useLocation();
  const [notice, setNotice] = useState(location.state?.notice || null);
  return (
    <div className="p-4">
      <div className="small mb-3">
        <Link to="/albums/transfers">‹ Transfers</Link>
      </div>
      {notice && (
        <CAlert color="warning" dismissible onClose={() => setNotice(null)}>{notice}</CAlert>
      )}
      <TransferBatch batchId={batchId} />
    </div>
  );
};

export default AlbumTransferBatch;
