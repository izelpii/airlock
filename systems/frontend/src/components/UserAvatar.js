import React, { useState, useEffect } from 'react';
import { personLinkAPI } from '../services/api';

/**
 * A user's face, from the person they are linked to.
 *
 * Falls back to initials rather than a broken image: most users will not be
 * linked to a person, and the avatar endpoint answers 404 for them by design.
 *
 * The image is a plain <img src> so the session cookie authenticates it — the
 * same reason authentication is a cookie rather than a bearer token, since an
 * img tag cannot set an Authorization header.
 */
const initialsOf = (name = '') =>
  name
    .split(/\s+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((w) => w[0])
    .join('')
    .toUpperCase() || '?';

const UserAvatar = ({ userId, name, size = 32, version, className = '', style = {} }) => {
  const [failed, setFailed] = useState(false);

  // A changed link must refetch, and the browser would otherwise reuse the
  // cached 404 or the previous face.
  useEffect(() => { setFailed(false); }, [userId, version]);

  // Shared box. flexShrink so a flex parent cannot squeeze it narrower than it
  // is tall — that would distort a circle into an ellipse.
  const base = {
    width: size,
    height: size,
    minWidth: size,
    minHeight: size,
    borderRadius: '50%',
    flexShrink: 0,
    overflow: 'hidden',
    ...style,
  };

  if (!userId || failed) {
    return (
      <span
        className={className}
        style={{
          ...base,
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          background: '#e2e8f0',
          color: '#475569',
          fontSize: Math.max(10, Math.round(size * 0.4)),
          fontWeight: 600,
          letterSpacing: '0.02em',
        }}
        title={name || ''}
      >
        {initialsOf(name)}
      </span>
    );
  }

  return (
    <img
      className={className}
      src={personLinkAPI.avatarUrl(userId, version)}
      alt={name ? `${name}'s photo` : 'Profile photo'}
      title={name || ''}
      onError={() => setFailed(true)}
      // objectFit: cover crops rather than stretches. The source is a square
      // crop built for this purpose, so it is square-into-square and nothing is
      // cropped either — the property is here so a non-square source (a legacy
      // face_contents fallback) degrades by cropping instead of distorting.
      style={{ ...base, display: 'block', objectFit: 'cover', background: '#e2e8f0' }}
    />
  );
};

export default UserAvatar;
