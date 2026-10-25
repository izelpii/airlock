import React, { useState, useEffect } from 'react';
import { systemAPI } from '../services/api';

// Who this copy is licensed to, on a line of its own under the version. A
// notice only (airlock_license.py): nothing in the application reads it.
export const licenseLine = (license) => {
  if (!license) return '';
  if (license.status === 'licensed') {
    return `\nLicensed to ${license.name}${license.edition ? ` — ${license.edition}` : ''}`;
  }
  if (license.status === 'invalid') {
    return `\nLicense not valid: ${license.reason || 'it could not be checked'}`;
  }
  return '\nUnlicensed copy';
};

// The visible notice beside the logo when no valid license is found: none, or
// one that does not verify. Words only - nothing is disabled (airlock_license.py).
// A backend too old to report a license says nothing here rather than claim
// a licensed copy is unlicensed.
export const UNLICENSED_TEXT = 'Unlicensed - No commercial use';
export const showUnlicensed = (license) => !!license && license.status !== 'licensed';

/**
 * The running version, beside the logo.
 *
 * Shows the major and minor only - "5.0" - because that is the part somebody
 * recognises at a glance and the patch number rarely answers a question on its
 * own. The full version, including how far past the release this build is, is
 * on the hover, which is where you look once the glance has raised a doubt.
 *
 * Renders nothing until it has an answer. A placeholder dash next to the logo
 * reads as a broken page rather than as a pending request.
 */
const VersionBadge = () => {
  // Asked once. A running build's version does not change underneath it, and
  // restarting the backend reloads this page anyway.
  const [info, setInfo] = useState(null);
  useEffect(() => {
    let cancelled = false;
    systemAPI.version()
      .then((res) => { if (!cancelled) setInfo(res.data); })
      .catch(() => {});      // a missing version is not worth an error here
    return () => { cancelled = true; };
  }, []);

  if (!info || !info.release) return null;

  // From `release`, never `version`: a version past its release carries
  // '+12.abc1234', and splitting that on '.' would put build detail where the
  // minor belongs.
  const short = info.release.split('.').slice(0, 2).join('.');

  const source = {
    build: 'baked into this image',
    git: 'read from the checkout',
    file: 'from the VERSION file; no git here',
  }[info.source];

  // At an exact release `version` already IS the release, so naming it again
  // would read "Airlock Photos 5.0.0 - release v5.0.0". The clause earns its place
  // only when the build has moved past the tag.
  const hover = [
    `Airlock Photos ${info.version}`,
    info.commits_since_release
      ? `${info.commits_since_release} commit${info.commits_since_release === 1 ? '' : 's'} since v${info.release}`
      : null,
    source,
  ].filter(Boolean).join(' — ') + licenseLine(info.license);

  return (
    <>
      <span
        title={hover}
        style={{
          fontSize: '0.75rem',
          fontWeight: 500,
          color: '#64748b',
          letterSpacing: '0.02em',
          whiteSpace: 'nowrap',
          cursor: 'default',
          alignSelf: 'center',
          paddingTop: '2px',   // sits on the logo's optical centre, not its box
        }}
      >
        v{short}
      </span>
      {showUnlicensed(info.license) && (
        <span
          title={licenseLine(info.license).trim()}
          data-testid="unlicensed-notice"
          style={{
            fontSize: '0.75rem',
            fontWeight: 600,
            color: '#c62828',
            whiteSpace: 'nowrap',
            cursor: 'default',
            alignSelf: 'center',
            paddingTop: '2px',
          }}
        >
          {UNLICENSED_TEXT}
        </span>
      )}
    </>
  );
};

export default VersionBadge;
