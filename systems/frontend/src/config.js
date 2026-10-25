import { editVersionOf } from './utils/editVersions';

// ─── Service endpoints ────────────────────────────────────────────────────────
//
// Every backend URL the frontend talks to, in one place and overridable at build
// time. These were previously hardcoded as `http://localhost:5000/...` in 32
// places across 13 files, baked into the static bundle by `npm run build`.
//
// That works only when the browser runs on the same machine as the services. It
// breaks the moment the UI is opened from a phone or another computer on the
// network, or a port changes.
//
// The defaults are now RELATIVE, and that is the whole point.
//
// Create React App inlines any REACT_APP_* variable at BUILD time. An absolute
// URL here is therefore compiled into the JavaScript bundle, and changing the
// address later requires rebuilding the frontend image - a step that fails
// silently, because the source is correct and the build succeeds. This project
// has been caught by exactly that more than once.
//
// The gateway (systems/gateway) serves this bundle and the API from one origin,
// so the browser can address everything relatively and the backend's location
// stops being a build-time fact:
//
//     /api/...        -> backend        (airlock.lan/api/)
//     /socket.io/...  -> backend
//     /vision/...     -> backend-vision
//     /geoloc/...     -> backend-geoloc
//
// THE RULE: if a URL ends up inside this bundle, it must be relative. Setting
// these to an absolute host still works - for a dev server talking to services
// started by hand, say - but it reintroduces rebuild-to-readdress, so treat it
// as a local override rather than a deployment mechanism.
//
//   REACT_APP_API_HOST=http://localhost:5000 npm start
//
// See documentation/specs/redesign-ports-routes-spec.md section 5.

const host = (name, fallback) => {
  const value = process.env[name];
  // EMPTY COUNTS AS UNSET, and that is the whole point of this line.
  //
  // The Dockerfile declares `ARG REACT_APP_GEOLOC_HOST=` and passes it to ENV.
  // Docker's ENV sets the variable to the EMPTY STRING rather than leaving it
  // unset, so Create React App bakes REACT_APP_GEOLOC_HOST:"" into the bundle
  // and `undefined` is never seen here. The fallback was therefore dead for
  // every service, and /geoloc/poi went out as /poi - which the gateway routes
  // to the SPA, so the browser parsed index.html as JSON and reported
  // "unexpected character at line 1 column 1".
  //
  // API_HOST hid it: its fallback IS '', so the empty value was right by
  // accident and only the prefixed services broke.
  //
  // Nothing is lost by the coalesce. A service whose fallback is a PATH cannot
  // usefully be set to '' - that would address the page's own origin with no
  // prefix, which is the frontend - and API_HOST arrives at '' either way.
  return value === undefined || value === '' ? fallback : value;
};

/** Main catalog/workflow backend. */
export const API_HOST = host('REACT_APP_API_HOST', '');

/** Vision (image description) service. */
export const VISION_HOST = host('REACT_APP_VISION_HOST', '/vision');

/** Geolocation service. */
export const GEOLOC_HOST = host('REACT_APP_GEOLOC_HOST', '/geoloc');

/** REST base. Relative by default: '/api'. */
export const API_BASE_URL = `${API_HOST}/api`;

/** Serves any catalog file by absolute path. Append an encoded path. */
export const FILES_BASE_URL = `${API_HOST}/api/catalogs/files?path=`;

/**
 * Square size, in pixels, of the prebuilt thumbnail the galleries request.
 *
 * The People / Locations / Events cards paint a 160x160 box, so 320 keeps them
 * crisp on a 2x (HiDPI) display. The backend serves the nearest prebuilt size
 * that is at least this large, so changing this only picks a different
 * prebuilt file - nothing is ever resized per request.
 */
export const GALLERY_THUMB_SIZE = 320;

/**
 * URL of a photo's prebuilt gallery thumbnail, by catalog and file id.
 *
 * It carries the photo's edit version (utils/editVersions.js): the backend
 * caches a versioned thumbnail for a year, so an edit has to change the URL
 * (basic-photo-editing.md section 7.2). Every thumbnail URL is built here.
 */
export const galleryThumbUrl = (catalogId, fileId, size = GALLERY_THUMB_SIZE, version) => {
  const v = version ?? editVersionOf(catalogId, fileId);
  return `${API_HOST}/api/catalogs/${catalogId}/photos/${fileId}/thumbnail?size=${size}`
    + (v === null || v === undefined ? '' : `&v=${v}`);
};

/**
 * The stamp of this frontend build (Dockerfile: REACT_APP_BUILD_ID), or null in
 * development. public/'s images are baked into the build, so they can only
 * change when it does.
 */
export const BUILD_ID = process.env.REACT_APP_BUILD_ID || null;

/**
 * URL of a file in public/ (a card decorator, the logo), carrying the build's
 * stamp. nginx lets browsers keep a stamped URL for a year without asking
 * again; a rebuild is a new stamp, so a replaced image is fetched once and then
 * kept. Unstamped (development), it is revalidated on every use instead.
 */
export const assetUrl = (path) => {
  const url = `${process.env.PUBLIC_URL || ''}/${String(path).replace(/^\//, '')}`;
  return BUILD_ID ? `${url}?v=${BUILD_ID}` : url;
};

/** Serves a person's photo by relative path. */
export const PEOPLE_PHOTOS_BASE_URL = `${API_HOST}/api/people/photos/`;

/**
 * Socket.IO endpoint for live workflow progress.
 *
 * Empty means "this origin", which is what socket.io-client does with an empty
 * URL - it connects back to the page's own host. The gateway forwards
 * /socket.io/ to the backend with buffering off, because the backend runs
 * gthread and therefore long-polls rather than using websockets.
 */
export const SOCKET_URL = API_HOST;
