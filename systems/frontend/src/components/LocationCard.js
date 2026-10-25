import React, { useState } from 'react';
import CIcon from '@coreui/icons-react';
// Only the two the card itself draws. Everything a caller puts in `actions` or
// `badge` it imports for itself - which is what keeps this component ignorant
// of whether it is showing a proposal or a named location.
import { cilMap, cilImage } from '@coreui/icons';
import { galleryThumbUrl, GALLERY_THUMB_SIZE } from '../config';

// ─── One card for a location, proposed or named ───────────────────────────────
// documentation/specs/location-spec.md section 8.1.
//
// There were two cards: a 290px proposal with a photo and counts, and a 400px
// named location with a map. They showed the same kind of thing in two shapes,
// and the only real difference between them was WHICH SIDE the picture was on -
// so this is one card with both: the photo on the front, the map on the back,
// and a button to turn it over.
//
// The flip is the People cards' own: a perspective container, a preserve-3d
// inner, two absolutely positioned faces with backfaceVisibility hidden. Same
// 290px width and the same easing, so a catalog's cards all behave alike.

// The Event cards' width, and for their reason: two thirds of
// GALLERY_THUMB_SIZE. The cover is the SAME stored 320px asset every gallery
// requests, painted into a smaller box - downscaling 320 to 213 costs nothing
// and stays sharp. This card asked /photos/<id>/thumbnail without a size and
// painted whatever came back into 290px, which upscaled it.
const CARD_WIDTH = Math.round(GALLERY_THUMB_SIZE * 2 / 3);   // 213
// The square cover plus everything stacked under it. Added up rather than
// guessed, because Face clips what does not fit and the first thing to go would
// be the Actions row - buttons that are simply not there:
//
//   header         ~46  (two lines of text, 8px padding each side)
//   cover          213  (CARD_WIDTH, square)
//   coordinates     26  (front only, under the map)
//   figures         48
//   actions         42
//   borders          3
//                 ----
//                  378
//
// Erring high on purpose: a card a few pixels taller than its contents leaves a
// thin gap, and one a few pixels shorter loses a button.
const CARD_HEIGHT = CARD_WIDTH + 170;   // 383

const count = (n) => (n == null ? '—' : Number(n).toLocaleString());

// "2014 – 2019", or one year where somewhere was visited once. The date range
// is what makes a location recognisable: a person remembers when far better
// than they remember a coordinate.
export const years = (first, last) => {
  const y = (v) => (v ? String(v).slice(0, 4) : null);
  const a = y(first);
  const b = y(last);
  if (!a && !b) return null;
  if (!a || !b || a === b) return a || b;
  return `${a} – ${b}`;
};

const metres = (v) => {
  if (v == null) return null;
  return v >= 1000 ? `${(v / 1000).toFixed(1)} km across` : `${Math.round(v)} m across`;
};

const coord = (v) => (v == null ? '—' : Number(v).toFixed(5));

const Face = ({ children, back = false }) => (
  <div style={{
    position: 'absolute', width: '100%', height: '100%',
    backfaceVisibility: 'hidden', WebkitBackfaceVisibility: 'hidden',
    transform: back ? 'rotateY(180deg)' : 'none',
  }}>
    <div style={{
      height: '100%', background: '#fff', border: '1px solid #e0e0e0',
      // Square at the top, round at the bottom.
      borderRadius: '0 0 10px 10px', overflow: 'hidden', display: 'flex',
      flexDirection: 'column',
      boxShadow: '0 1px 3px rgba(0,0,0,0.06)',
    }}>
      {children}
    </div>
  </div>
);

const Header = ({ title, subtitle, badge, flipped, onFlip, hasMap }) => (
  <div style={{
    display: 'flex', alignItems: 'center', gap: 6, padding: '8px 10px',
    borderBottom: '1px solid #eee', flexShrink: 0,
  }}>
    <div style={{ flex: 1, minWidth: 0 }}>
      <div style={{
        fontWeight: 600, fontSize: '0.86rem', whiteSpace: 'nowrap',
        overflow: 'hidden', textOverflow: 'ellipsis', color: '#212121',
      }} title={title}>
        {title}
      </div>
      {subtitle && (
        <div style={{ fontSize: '0.7rem', color: '#888', whiteSpace: 'nowrap',
                      overflow: 'hidden', textOverflow: 'ellipsis' }}>
          {subtitle}
        </div>
      )}
      {/* A line of its own under the name, not a chip beside it. Beside it, a
          badge competes with the name for a width that is already short enough
          to be eliding - the Event cards put theirs on its own line for the
          same reason. */}
      {badge && <div style={{ marginTop: 4 }}>{badge}</div>}
    </div>
    {/* Show Map / Show Photo, the one control on both faces. Disabled rather
        than hidden when there is no coordinate: a button that comes and goes
        is harder to find than one that explains itself. */}
    <button
      onClick={(e) => { e.stopPropagation(); onFlip(); }}
      disabled={!hasMap}
      title={!hasMap ? 'No coordinates, so there is no map - this is its photo'
                     : flipped ? 'Show the map' : 'Show a photo'}
      style={{
        background: 'none', border: '1px solid #e0e0e0', borderRadius: 4,
        padding: '3px 6px', cursor: hasMap ? 'pointer' : 'not-allowed',
        opacity: hasMap ? 1 : 0.4, flexShrink: 0, lineHeight: 1,
      }}
    >
      {/* The icon names what pressing it WILL show, not what is showing: the
          front is the map now, so the button offers a photo. */}
      <CIcon icon={flipped ? cilMap : cilImage}
             style={{ width: 14, height: 14, color: '#555' }} />
    </button>
  </div>
);

const Picture = ({ src, alt, empty }) => {
  // A thumbnail that has not been built now answers 404 rather than redirecting
  // to the full-size original, so the <img> fails - and failing quietly is the
  // point. Without this the card shows a broken-image glyph, which reads as
  // something going wrong rather than as a derivative nobody has made yet.
  const [failed, setFailed] = React.useState(false);
  React.useEffect(() => { setFailed(false); }, [src]);

  return (
    <div style={{ width: '100%', height: CARD_WIDTH, flexShrink: 0,
                  background: '#f5f5f5', overflow: 'hidden',
                  display: 'flex', alignItems: 'center', justifyContent: 'center' }}>
      {src && !failed ? (
        <img src={src} alt={alt} loading="lazy"
             onError={() => setFailed(true)}
             style={{ width: '100%', height: '100%', objectFit: 'cover' }} />
      ) : (
        <span style={{ fontSize: '0.75rem', color: '#aaa' }}>{empty}</span>
      )}
    </div>
  );
};

const Figures = ({ left, leftLabel, right, rightLabel }) => (
  <div style={{ display: 'flex', gap: 1, background: '#eee', flexShrink: 0 }}>
    <div style={{ background: '#fff', padding: '8px 10px', flex: 1 }}>
      <div style={{ fontSize: '1.05rem', fontWeight: 600, color: '#212121',
                    lineHeight: 1.1 }}>{left}</div>
      <div style={{ fontSize: '0.7rem', color: '#888', marginTop: 2 }}>{leftLabel}</div>
    </div>
    <div style={{ background: '#fff', padding: '8px 10px', flex: 1 }}>
      <div style={{ fontSize: '1.05rem', fontWeight: 600, color: '#212121',
                    lineHeight: 1.1 }}>{right}</div>
      <div style={{ fontSize: '0.7rem', color: '#888', marginTop: 2 }}>{rightLabel}</div>
    </div>
  </div>
);

// Bottom right, and it swallows the click: the whole card opens the location
// now, so a button inside it has to stop the event or confirming would also
// navigate away from the thing being confirmed.
const Actions = ({ children }) => (
  <div style={{ padding: '8px 10px', borderTop: '1px solid #f0f0f0',
                display: 'flex', gap: 6, flexShrink: 0, flexWrap: 'wrap',
                justifyContent: 'flex-end', marginTop: 'auto' }}
       onClick={(e) => e.stopPropagation()}>
    {children}
  </div>
);

/**
 * `kind` is 'proposed' or 'named', and it changes only what the card SAYS -
 * never its shape. That is the point: the two used to be different components
 * and drifted apart in width, in what they showed and in whether they had a
 * map at all.
 */
const LocationCard = ({
  kind, catalogId, id, name, subtitle, badge,
  coverFileId, latitude, longitude, fileCount, firstSeen, lastSeen,
  radiusMetres, mapUrl, onOpen, actions,
}) => {
  const [flipped, setFlipped] = useState(false);
  const hasMap = latitude != null && longitude != null;
  const thumb = coverFileId
    ? galleryThumbUrl(catalogId, coverFileId, GALLERY_THUMB_SIZE)
    : null;
  const span = years(firstSeen, lastSeen);

  const header = (
    <Header title={name} subtitle={subtitle} badge={badge}
            flipped={flipped} hasMap={hasMap}
            onFlip={() => setFlipped((f) => !f)} />
  );

  return (
    // The whole card opens the location. There was an Open button doing that
    // job from inside a card that was already the obvious thing to click, so
    // the button was a second way to do one thing - and it crowded two
    // controls that are not the same kind of action at all.
    <div style={{ perspective: '1000px', width: CARD_WIDTH, height: CARD_HEIGHT,
                  flexShrink: 0, cursor: onOpen ? 'pointer' : 'default' }}
         onClick={onOpen}>
      <div style={{
        position: 'relative', width: '100%', height: '100%',
        transformStyle: 'preserve-3d',
        transition: 'transform 0.55s cubic-bezier(0.4, 0.2, 0.2, 1)',
        transform: flipped ? 'rotateY(180deg)' : 'rotateY(0deg)',
      }}>
        {/* THE MAP LEADS. A location is a where, and the map says where at a
            glance; the photo says which one of your photos happens to be
            first, which is a weaker answer to the same question.

            A location with no coordinates has no map to lead with, so it shows
            its photo on the front and does not flip at all - rather than
            leading with an empty grey box and hiding the one thing it has. */}
        <Face>
          {header}
          {hasMap ? (
            <>
              {/* A rendered snapshot, not a live map: a wall of cards each
                  running its own Leaflet instance is a great deal of work for
                  a picture nobody pans. loading="lazy" matters more now that
                  this is the front - the browser fetches only what comes near
                  the viewport, and the backend renders each one once and
                  caches it. */}
              <Picture src={mapUrl} alt={`Map of ${name}`}
                       empty="loading the map…" />
              <div style={{ padding: '6px 10px', fontSize: '0.75rem',
                            fontFamily: 'monospace', color: '#495057',
                            flexShrink: 0 }}>
                {coord(latitude)}, {coord(longitude)}
              </div>
            </>
          ) : (
            <Picture src={thumb} alt={name} empty="no photo yet" />
          )}
          <Figures
            left={count(fileCount)} leftLabel="photos"
            right={span || '—'}
            rightLabel={metres(radiusMetres) || (span ? 'when' : '—')} />
          {actions && <Actions>{actions}</Actions>}
        </Face>

        <Face back>
          {header}
          {/* Only painted once the card has been turned: a thumbnail is
              cheap, but a page of them that nobody asked to see is not. */}
          <Picture src={flipped ? thumb : null} alt={name}
                   empty="no photo yet" />
          <Figures
            left={count(fileCount)} leftLabel="photos"
            right={span || '—'}
            rightLabel={metres(radiusMetres) || (span ? 'when' : '—')} />
          {actions && <Actions>{actions}</Actions>}
        </Face>
      </div>
    </div>
  );
};

export { CARD_WIDTH, CARD_HEIGHT, count, coord, metres };
export default LocationCard;
