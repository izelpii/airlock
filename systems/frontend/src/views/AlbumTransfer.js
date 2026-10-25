import React from 'react';
import { useParams } from 'react-router-dom';
import TransferPanel from '../components/TransferPanel';

// ─── One transfer, full page ──────────────────────────────────────────────────
// /albums/transfers/:transferId — the route a history row's Open button leads
// to, and the one in links people already have.
//
// The screen itself is TransferPanel, which the Transfers page also draws for
// whatever is running right now (transfer-spec.md section 3.6). This is the
// wrapper: the id from the URL, and the roomier of the two page sizes, since
// here the file table has the whole window and nothing sits underneath it.
//
// A page rather than a modal: a multi-gigabyte transfer outlives the user's
// interest in the album list.
const AlbumTransfer = () => {
  const { transferId } = useParams();
  return (
    <div className="p-4">
      <TransferPanel transferId={transferId} perPage={25} />
    </div>
  );
};

export default AlbumTransfer;
