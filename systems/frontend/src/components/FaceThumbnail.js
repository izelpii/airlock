import React, { useState } from 'react';
import { FILES_BASE_URL } from '../config';

const IMAGE_BASE = FILES_BASE_URL;

// ─── Face crop thumbnail ──────────────────────────────────────────────────────
// Priority 1: use the pre-saved AI crop JPEG from ai_face_contents (always a
//             displayable file, works for HEIC/CR3 originals too).
// Priority 2: CSS-clip the original photo (only works for displayable formats).

const FaceThumbnail = ({ face, size = 96 }) => {
  const [cropError, setCropError] = useState(false);
  const [origError, setOrigError] = useState(false);

  // Use the pre-saved crop when available and not yet errored
  const hasCrop = !!face.ai_crop_path && !cropError;

  if (hasCrop) {
    const cropSrc = `${IMAGE_BASE}${encodeURIComponent(face.ai_crop_path)}`;
    return (
      <img
        src={cropSrc}
        alt="face"
        style={{ width: size, height: size, objectFit: 'cover', borderRadius: 6, flexShrink: 0 }}
        onError={() => setCropError(true)}
      />
    );
  }

  // Fallback: CSS-clip from the original photo
  const origSrc = `${IMAGE_BASE}${encodeURIComponent(face.file_path)}`;

  if (!face.image_width || !face.image_height || origError) {
    return (
      <img
        src={origSrc}
        alt="face"
        style={{ width: size, height: size, objectFit: 'cover', borderRadius: 6, flexShrink: 0 }}
        onError={() => setOrigError(true)}
      />
    );
  }

  const scale = Math.min(size / face.w, size / face.h);
  const renderedW = face.image_width * scale;
  const renderedH = face.image_height * scale;
  const offsetX = -(face.x * scale);
  const offsetY = -(face.y * scale);

  return (
    <div style={{ width: size, height: size, overflow: 'hidden', borderRadius: 6, flexShrink: 0 }}>
      <img
        src={origSrc}
        alt="face"
        style={{ width: renderedW, height: renderedH, marginLeft: offsetX, marginTop: offsetY, display: 'block' }}
        onError={() => setOrigError(true)}
      />
    </div>
  );
};

export default FaceThumbnail;
