import React from 'react';
import { assetUrl } from '../config';

// ─── The band across the top of a catalog or deployment card ─────────────────
// One look for both pages: a 50px picture (the 720x100 decorators at 2x on a
// 360px card) with the kind of card written on it, bold and white, on the
// right. The card under it takes CARD_TOP_CORNERS: square where the band meets
// the card's top edge, round at the bottom.

export const BAND_HEIGHT = 50;

export const CARD_TOP_CORNERS = { borderTopLeftRadius: 0, borderTopRightRadius: 0 };

const CardBand = ({ image, label }) => (
  <div style={{
    width: '100%', height: BAND_HEIGHT, flexShrink: 0,
    // Stamped with the build, so the browser keeps it for good (config.js).
    backgroundImage: `url(${assetUrl(image)})`,
    backgroundSize: 'cover',
    backgroundPosition: 'center',
    backgroundRepeat: 'no-repeat',
    display: 'flex', alignItems: 'center', justifyContent: 'flex-end',
    padding: '0 16px',
  }}>
    {/* A soft shadow keeps white text readable on a light picture. */}
    <span style={{ color: '#fff', fontWeight: 700, fontSize: '2.1rem', lineHeight: 1,
                   letterSpacing: '0.02em', textShadow: '0 1px 3px rgba(0,0,0,0.45)' }}>
      {label}
    </span>
  </div>
);

export default CardBand;
