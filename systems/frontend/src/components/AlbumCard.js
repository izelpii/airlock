import React from 'react';
import {
  CCard, CCardBody, CCardHeader, CCol, CBadge, CFormLabel,
} from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilImage } from '@coreui/icons';
import { galleryThumbUrl, GALLERY_THUMB_SIZE } from '../config';
import CardBand, { CARD_TOP_CORNERS } from './CardBand';

// ─── One album, as a card ─────────────────────────────────────────────────────
// THE album card. An album is one concept, so it has one card, drawn the same
// way wherever it appears: beside the deployment that publishes it, and in the
// list of albums that can be sent to another machine. Two spellings of the same
// card is how the two pages drift apart, and there is nothing about a page that
// makes an album a different kind of thing.
//
// What a page contributes is its CONTROLS, never the chrome:
//
//   headerActions  the ghost icon buttons at the top right (edit, delete, …)
//   badges         extra badges beside the photo count (e.g. "transferring")
//   children       a full-width call to action pinned to the bottom
//   catalogId      which catalog to fetch the cover thumbnail from
//
// On top, the same band as catalog and deployment cards (CardBand.js), with
// "album" on it and a picture from public/theme3/ (owner, 2026-10-20). Albums
// store no pick, so it is derived from the album's id - stepping by 7 through
// the 20, so neighbouring albums do not get neighbouring pictures, and an album
// keeps its picture for good.
//
// The cover at the top of the body is a PHOTO FROM THE ALBUM, resolved by the
// albums endpoint (app._album_cover_file_id): the fastest way to tell two albums
// apart, and the same 320 thumbnail the event and section cards already draw.

const DECOR_THEME = 'theme3';
const DECOR_COUNT = 20;
export const albumDecorator = (album) =>
  `${DECOR_THEME}/cardDeco${((Number(album?.id) || 0) * 7) % DECOR_COUNT + 1}.png`;

// As wide as catalog and deployment cards.
export const CARD_WIDTH = 360;
// A floor, not a fixed height: every card in a row lines up whether or not it
// has a description, and a long one still grows the card rather than being cut.
// Includes the band and the cover below.
export const CARD_MIN_HEIGHT = 420;

// How an album was built. Here rather than in either page, because the card is
// what shows it and both pages show the card.
export const ALBUM_MODE_OPTIONS = [
  {
    value: 'words',
    title: 'Create album with words',
    short: 'Words',
    description: 'Build the album with natural-language filters.',
    route: 'words',
  },
  {
    value: 'tools',
    title: 'Create album with tools',
    short: 'Tools',
    description: 'Build the album with tools.',
    route: 'tools',
  },
];

const modeOf = (album) =>
  ALBUM_MODE_OPTIONS.find((o) => o.value === album?.mode) || ALBUM_MODE_OPTIONS[0];

export function getAlbumEditorRoute(album) {
  return `/albums/${modeOf(album).route}/${album.id}`;
}

export const formatDate = (value) => {
  if (!value) return '—';
  const d = new Date(String(value).includes('T') ? value : String(value).replace(' ', 'T'));
  return Number.isNaN(d.getTime()) ? value : d.toLocaleDateString();
};

const Field = ({ label, children }) => (
  <div className="mb-2">
    <CFormLabel className="small fw-semibold mb-1">{label}</CFormLabel>
    <div style={{ fontSize: '0.82rem', color: '#495057', wordBreak: 'break-word' }}>
      {children}
    </div>
  </div>
);

// Fixed height, full card width: a banner rather than a giant square, and every
// card in a row keeps the same one whether or not its album has a cover. It
// opens the BODY, under the header - the name and the controls are what the eye
// needs first, and the picture reads as the album's contents rather than as a
// title bar behind them.
export const COVER_HEIGHT = 120;

const AlbumCover = ({ album, catalogId }) => {
  const url = album?.cover_file_id && catalogId
    ? galleryThumbUrl(catalogId, album.cover_file_id, GALLERY_THUMB_SIZE)
    : null;
  return (
    <div style={{
      width: '100%', height: COVER_HEIGHT, flexShrink: 0, background: '#f1f3f4',
      display: 'flex', alignItems: 'center', justifyContent: 'center',
      overflow: 'hidden',
    }}>
      {url ? (
        <img
          src={url}
          alt=""
          loading="lazy"
          // A square thumbnail in a wide box: objectFit crops it rather than
          // stretching it, and the onError leaves the grey box behind if the
          // thumbnail has gone missing since the endpoint resolved it.
          style={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
          onError={(e) => { e.currentTarget.style.display = 'none'; }}
        />
      ) : (
        // A real case, not a defensive branch: a new album has chosen no
        // photographs yet, and a photo has no thumbnail until that workflow
        // has run.
        <CIcon icon={cilImage} style={{ width: 32, height: 32, color: '#c4c8cc' }} />
      )}
    </div>
  );
};

const AlbumCard = ({ album, badges, headerActions, children, catalogId }) => (
  <CCol xs="auto">
    <CCard className="shadow-sm d-flex flex-column"
           style={{ width: CARD_WIDTH, minHeight: CARD_MIN_HEIGHT, overflow: 'hidden',
                    ...CARD_TOP_CORNERS }}>
      <CardBand image={albumDecorator(album)} label="album" />
      <CCardHeader
        className="d-flex justify-content-between align-items-start"
        style={{ minHeight: '64px' }}
      >
        <div className="me-2 overflow-hidden">
          <div className="fw-semibold text-truncate">{album.name}</div>
          <div className="mt-1 d-flex gap-1 flex-wrap align-items-center">
            {album.photo_count != null && (
              <CBadge color="secondary" style={{ fontSize: '0.7rem' }}>
                <CIcon icon={cilImage} style={{ width: 10, height: 10 }} className="me-1" />
                {album.photo_count}
              </CBadge>
            )}
            <CBadge color="light" className="text-dark" style={{ fontSize: '0.7rem' }}>
              {modeOf(album).short}
            </CBadge>
            {badges}
          </div>
        </div>
        {headerActions && <div className="d-flex flex-shrink-0">{headerActions}</div>}
      </CCardHeader>

      {/* The cover opens the body, edge to edge - hence the padding on the
          fields below it rather than on the body itself, which would inset the
          picture by three quarters of a rem on every side. */}
      <CCardBody className="d-flex flex-column flex-grow-1" style={{ padding: 0 }}>
        <AlbumCover album={album} catalogId={catalogId} />
        <div className="d-flex flex-column flex-grow-1" style={{ padding: '0.75rem' }}>
          <Field label="Created">{formatDate(album.created_at)}</Field>
          <Field label="Description">
            {album.description
              ? <span className="text-truncate d-block">{album.description}</span>
              : <span className="text-muted">—</span>}
          </Field>
          {/* mt-auto: whatever the page puts here sits on the bottom edge, so a
              row of cards has its buttons on one line however tall each card
              is. */}
          {children && <div className="mt-auto">{children}</div>}
        </div>
      </CCardBody>
    </CCard>
  </CCol>
);

export default AlbumCard;
