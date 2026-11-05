import React, { useMemo } from 'react';
import { CCard, CCardBody, CCardHeader } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilCheckCircle } from '@coreui/icons';
import ChangeDateTool, { dateSpan, describeSpan } from './ChangeDateTool';

// ─── A saved search's tool panel ──────────────────────────────────────────────
// saved-search-tools-spec.md sections 3 and 4.3. The left column of a saved
// search's page, as wide as the Photos page's: a Selection card, then one card
// per tool. A later tool (location, people, make an album) is one more card.

export const VIDEO = new Set(['.mov', '.mp4', '.m4v', '.avi', '.mkv', '.mts', '.m2ts', '.3gp', '.wmv', '.mpg', '.mpeg', '.webm']);
export const isVideoType = (t) => VIDEO.has((t || '').toLowerCase());

const plural = (n, one, many) => `${n.toLocaleString()} ${n === 1 ? one : many}`;

export function describeKinds(ids, typeById) {
  const videos = ids.filter((id) => isVideoType(typeById.get(id))).length;
  const photos = ids.length - videos;
  if (!videos || !photos) return null;
  return `${plural(photos, 'photo', 'photos')}, ${plural(videos, 'video', 'videos')}`;
}

const SelectionCard = ({ selectedIds, takenById, typeById }) => {
  const span = useMemo(() => dateSpan(selectedIds, takenById), [selectedIds, takenById]);
  const kinds = describeKinds(selectedIds, typeById);
  return (
    <CCard data-testid="selection-card">
      <CCardHeader className="d-flex align-items-center py-2">
        <CIcon icon={cilCheckCircle} className="me-2" />
        <strong>Selection</strong>
      </CCardHeader>
      <CCardBody className="py-2" style={{ fontSize: '0.85rem' }}>
        {selectedIds.length === 0 ? (
          <span className="text-body-secondary">
            Select photos and videos in the grid to work on them.
          </span>
        ) : (
          <>
            <div className="fw-semibold">{selectedIds.length.toLocaleString()} selected</div>
            {kinds && <div className="text-body-secondary">{kinds}</div>}
            {span && <div className="text-body-secondary">{describeSpan(span)}</div>}
          </>
        )}
      </CCardBody>
    </CCard>
  );
};

const SavedSearchTools = ({
  catalogId, savedSearchId, selectedIds, takenById, typeById, canEdit, indexInPlace,
  busy, onDateChangeStarted,
}) => (
  <div className="d-flex flex-column gap-3"
       style={{ position: 'sticky', top: '1rem', maxHeight: 'calc(100vh - 2rem)', overflowY: 'auto' }}>
    <SelectionCard selectedIds={selectedIds} takenById={takenById} typeById={typeById} />
    <ChangeDateTool
      catalogId={catalogId}
      savedSearchId={savedSearchId}
      selectedIds={selectedIds}
      takenById={takenById}
      canEdit={canEdit}
      indexInPlace={indexInPlace}
      busy={busy}
      onStarted={onDateChangeStarted}
    />
  </div>
);

export default SavedSearchTools;
