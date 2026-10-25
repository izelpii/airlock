import axios from 'axios';
import { API_BASE_URL, API_HOST } from '../config';

const api = axios.create({
  baseURL: API_BASE_URL,
  headers: {
    'Content-Type': 'application/json',
  },
  // Authentication is a session cookie, not a bearer token, because <img src>
  // cannot carry an Authorization header. The browser attaches it to XHR only
  // when this is set, and the dev server on :3000 is cross-origin against the
  // backend. See user-authentication-spec.md section 5.1.
  withCredentials: true,
});

// 401 means "not authenticated" -> show the login view.
// 403 means "authenticated but not permitted" -> show a message and STAY, since
// bouncing a signed-in user to a login page they already passed is a loop.
// See user-authentication-spec.md section 7.
let onUnauthenticated = null;
export const setUnauthenticatedHandler = (fn) => { onUnauthenticated = fn; };

api.interceptors.response.use(
  (response) => response,
  (error) => {
    const status = error?.response?.status;
    const url = error?.config?.url || '';
    // The boot probe expects a 401; treating it as a session loss would loop.
    const isProbe = url.includes('/auth/session');
    if (status === 401 && !isProbe && onUnauthenticated) {
      onUnauthenticated();
    }
    return Promise.reject(error);
  }
);

// ── The build ─────────────────────────────────────────────────────────────────
// Unauthenticated, like the endpoint: which code is running is the first thing
// anybody asks when a deployment misbehaves, and needing a session to find out
// makes a bad moment worse.
export const embeddingModelAPI = {
  // Which model a catalog's face embeddings come from, with that model's
  // thresholds and a count of what is actually stored per model - which is how
  // a half-migrated catalog is told from a migrated one.
  get: (catalogId) => api.get(`/catalogs/${catalogId}/embedding-model`),
};

export const systemAPI = {
  version: () => api.get('/version'),
};

export const authAPI = {
  session: () => api.get('/auth/session'),
  setupRequired: () => api.get('/auth/setup-required'),
  setup: (payload) => api.post('/auth/setup', payload),
  login: (username, password) => api.post('/auth/login', { username, password }),
  logout: () => api.post('/auth/logout'),
  logoutAll: () => api.post('/auth/logout-all'),
  sessions: () => api.get('/auth/sessions'),
  changePassword: (current, next) => api.post('/auth/password', { current, new: next }),
};

export const userAPI = {
  getAll: () => api.get('/users'),
  // Profile fields (address, city, country, birthdate, home coordinates) live in
  // user_settings, not on the users row. Addressed by id so an administrator can
  // edit somebody else; /user/settings is the self-service equivalent.
  getProfile: (id) => api.get(`/users/${id}/profile`),
  updateProfile: (id, payload) => api.put(`/users/${id}/profile`, payload),
  create: (payload) => api.post('/users', payload),
  update: (id, payload) => api.patch(`/users/${id}`, payload),
  delete: (id) => api.delete(`/users/${id}`),
  // An administrator sets someone else's password; they choose their own at the
  // next sign in. Your own is changed with the current one (authAPI.changePassword).
  setPassword: (id, next) => api.post(`/users/${id}/password`, { new: next }),
  // Signed-in devices, by account: the account holder or an administrator.
  sessions: (id) => api.get(`/users/${id}/sessions`),
  signOutEverywhere: (id) => api.delete(`/users/${id}/sessions`),
};

// A user profile can borrow a person's details and face. The link is a
// (catalog, person) pair because people.id is unique only within a catalog.
// Storage alerts (storage_alerts.py): disks the catalogs write to that are
// filling up. Counts come with every response, for the top-bar badge.
export const alertsAPI = {
  // { includeResolved, limit, offset, recent } - recent: newest first, open or
  // not, which is what the bell's menu lists.
  list: ({ includeResolved = false, limit, offset, recent = false } = {}) =>
    api.get('/alerts', { params: {
      ...(includeResolved ? { include_resolved: 1 } : {}),
      ...(limit ? { limit } : {}),
      ...(offset ? { offset } : {}),
      ...(recent ? { recent: 1 } : {}),
    } }),
  checkNow: () => api.post('/alerts/check'),
  acknowledge: (id) => api.post(`/alerts/${id}/acknowledge`),
};

export const personLinkAPI = {
  linkable: () => api.get('/people/linkable'),
  get: (userId) => api.get(`/users/${userId}/person`),
  set: (userId, catalogId, personId) =>
    api.patch(`/users/${userId}`, { person_catalog_id: catalogId, person_id: personId }),
  clear: (userId) => api.patch(`/users/${userId}`, { person_id: null }),
  // Not fetched through axios: it is an <img src>, so the session cookie
  // carries it. The cache-buster forces a refetch after the link changes.
  avatarUrl: (userId, bust) =>
    `${API_HOST}/api/users/${userId}/avatar${bust ? `?v=${bust}` : ''}`,
};

export const catalogAccessAPI = {
  list: (catalogId) => api.get(`/catalogs/${catalogId}/access`),
  set: (catalogId, userId, role) =>
    api.post(`/catalogs/${catalogId}/access`, { user_id: userId, role }),
  revoke: (catalogId, userId) => api.delete(`/catalogs/${catalogId}/access/${userId}`),
};

export const workflowAPI = {
  // Get available workflow scripts (global — filesystem scan, enriched from
  // workflows/defaults.json where a script is listed there)
  getScripts: () => api.get('/workflows/scripts'),

  // The default-workflow manifest, and seeding an existing catalog with any it
  // is missing. A catalog with no workflows has no automation at all: trigger
  // chains skip scripts that are not registered for it.
  getDefaults: () => api.get('/workflows/defaults'),
  seedDefaults: (catalogId) =>
    api.post(`/catalogs/${catalogId}/workflows/seed-defaults`),

  // Get all workflows for a catalog
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/workflows`),

  // Get a specific workflow
  getById: (catalogId, id) => api.get(`/catalogs/${catalogId}/workflows/${id}`),

  // Create a new workflow inside a catalog
  create: (catalogId, workflow) => api.post(`/catalogs/${catalogId}/workflows`, workflow),

  // Update a workflow
  update: (catalogId, id, workflow) => api.put(`/catalogs/${catalogId}/workflows/${id}`, workflow),

  // Delete a workflow
  delete: (catalogId, id) => api.delete(`/catalogs/${catalogId}/workflows/${id}`),

  // Execute a workflow
  execute: (catalogId, id, executionConfig = {}) =>
    api.post(`/catalogs/${catalogId}/workflows/${id}/execute`, executionConfig),

  // This catalog's workflows as a graph: nodes, dependency edges with the
  // reason for each, and a layer per node. Laid out on the backend from
  // workflow_triggers.DEPENDS_ON - the same map the queue schedules from - so
  // the drawing cannot drift from the behaviour.
  graph: (catalogId) => api.get(`/catalogs/${catalogId}/workflow-graph`),

  // Get files for a specific execution
  getExecutionFiles: (catalogId, executionId) =>
    api.get(`/catalogs/${catalogId}/executions/${executionId}/files`),

  // Ingestion runs with their file counts, newest first:
  // { total, executions: [{ ..., files_analysed, files_new, files_duplicate,
  //                         files_ignored, counts_from }] }
  // Paged because the report shows one row of cards and steps back through the
  // rest - `total` comes back with every page so the control can size itself.
  getIngestions: (catalogId, { limit = 12, offset = 0 } = {}) =>
    api.get(`/catalogs/${catalogId}/executions/ingestions`, { params: { limit, offset } }),
};

// ─── Workflow queue ───────────────────────────────────────────────────────────
// See documentation/specs/workflow-scheduling-spec.md section 7.
export const queueAPI = {
  get: (catalogId, includeFinished = false) =>
    api.get(`/catalogs/${catalogId}/queue`,
      { params: includeFinished ? { include_finished: 1 } : {} }),

  // Add one workflow. The backend folds this into an existing queued entry for
  // the same workflow rather than queueing it twice.
  // `source` is either { source_directory } for a literal path, or
  // { source_route_id, source_subpath } for a storage route — the backend
  // resolves the route and refuses at this point if it is unavailable.
  enqueue: (catalogId, workflowId, source, runNow = false) =>
    api.post(`/catalogs/${catalogId}/queue`, {
      workflow_id: workflowId,
      ...(typeof source === 'string' ? { source_directory: source } : (source || {})),
      run_now: runNow,
    }),

  // Enqueue a whole named chain, e.g. 'import_completed'.
  enqueueChain: (catalogId, trigger) =>
    api.post(`/catalogs/${catalogId}/queue/chain`, { trigger }),

  cancel: (catalogId, queueId) =>
    api.delete(`/catalogs/${catalogId}/queue/${queueId}`),

  move: (catalogId, queueId, position) =>
    api.put(`/catalogs/${catalogId}/queue/${queueId}/position`, { position }),

  // Skip an item's quiet period.
  runNow: (catalogId, queueId) =>
    api.post(`/catalogs/${catalogId}/queue/${queueId}/run-now`),

  pause: (catalogId) => api.post(`/catalogs/${catalogId}/queue/pause`),
  resume: (catalogId) => api.post(`/catalogs/${catalogId}/queue/resume`),
  status: (catalogId) => api.get(`/catalogs/${catalogId}/queue/status`),

  // Stop a RUNNING workflow. Distinct from cancel(), which only removes work
  // that has not started. The workflow leaves at its next item boundary, so the
  // response means "stopping", not "stopped".
  //
  // remove: true ends the queue row as 'cancelled' with no resume offered.
  // Neither verb un-imports anything the run already did.
  stop: (catalogId, queueId, remove = false) =>
    api.post(`/catalogs/${catalogId}/queue/${queueId}/stop`, { remove }),

  // Queue a stopped workflow again. There is no partial state to reload: every
  // workflow skips what an earlier run finished.
  resumeItem: (catalogId, queueId) =>
    api.post(`/catalogs/${catalogId}/queue/${queueId}/resume`),
};

export const executionAPI = {
  // Get execution status
  getById: (id) => api.get(`/executions/${id}`),

  // Get execution status by catalog and execution id
  getStatus: (catalogId, executionId) =>
    api.get(`/catalogs/${catalogId}/executions/${executionId}`),

  // Get all currently-running executions for a catalog (for page-revisit recovery)
  getRunning: (catalogId) =>
    api.get(`/catalogs/${catalogId}/executions/running`),
};

// ─── Storage routes ───────────────────────────────────────────────────────────
// Named folders catalogs draw input files from. A route is a subpath of the one
// storage root mounted into the backend container; see
// documentation/specs/local-storage-spec.md.
export const storageRouteAPI = {
  // Returns { enabled, message, storage_root_host, routes: [...] }. `enabled`
  // is false when no storage root is mounted, and `message` names the variable
  // to set — so a misconfigured deployment explains itself rather than showing
  // an empty list.
  getAll: () => api.get('/storage-routes'),

  // Created INSIDE a catalog and owned by it from that moment. There is no
  // separate step that attaches a route, and no way to attach one to a second
  // catalog: see documentation/specs/local-storage-spec.md.
  create: (catalogId, route) =>
    api.post(`/catalogs/${catalogId}/storage-routes`, route),
  update: (id, route) => api.put(`/storage-routes/${id}`, route),
  remove: (id) => api.delete(`/storage-routes/${id}`),

  // Subfolders inside one route, for the picker. Directories only.
  browse: (id, path = '') =>
    api.get(`/storage-routes/${id}/browse`, { params: { path } }),

  // Which routes a catalog may draw from.
  forCatalog: (catalogId) => api.get(`/catalogs/${catalogId}/storage-routes`),
};

export const catalogAPI = {
  // Get all catalogs
  getAll: () => api.get('/catalogs'),
  
  // Get a specific catalog, with its workflow executions. The file list only
  // with { include: 'files' }; only the catalog detail view needs it.
  getById: (id, { include } = {}) =>
    api.get(`/catalogs/${id}`, include ? { params: { include } } : undefined),

  // One page of a catalog's files, newest first:
  // { total, page, per_page, files }. WITHOUT raw EXIF - that is 22 KB a file
  // and only the expanded row shows it, so it comes from getFileExif below.
  getFiles: (id, page = 1, perPage = 25) =>
    api.get(`/catalogs/${id}/files`, { params: { page, per_page: perPage } }),

  // One file's full EXIF, for the row a person expanded.
  getFileExif: (id, fileId) => api.get(`/catalogs/${id}/files/${fileId}/exif`),
  
  // Create a new catalog
  create: (catalog) => api.post('/catalogs', catalog),
  
  // Update a catalog
  update: (id, catalog) => api.put(`/catalogs/${id}`, catalog),
  
  // Delete a catalog
  delete: (id) => api.delete(`/catalogs/${id}`),

  // Whether a catalog that indexes files in place can currently reach them.
  // Cheap and sampled — safe to call whenever the active catalog changes.
  sourceStatus: (id) => api.get(`/catalogs/${id}/source-status`),
  
  // Add a file to catalog
  addFile: (id, file) => api.post(`/catalogs/${id}/files`, file),
  
  // Add an operation to catalog
  addOperation: (id, operation) => api.post(`/catalogs/${id}/operations`, operation),
  
  // Add a workflow execution to catalog
  addWorkflowExecution: (id, execution) => api.post(`/catalogs/${id}/workflow-executions`, execution),
  
  // Verify catalog integrity
  verify: (id) => api.post(`/catalogs/${id}/verify`),
  // A move-mode import's leftovers: DUPS/, left in place, not yet deleted.
  leftInSource: (id, executionId) =>
    api.get(`/catalogs/${id}/left-in-source`,
            { params: executionId ? { execution_id: executionId } : {} }),
  
  // Get content by time report
  // What is in a catalog, and what is missing from it. Every section in one
  // reply, cached server-side on a fingerprint of the catalog - see
  // documentation/specs/contents-spec.md. refresh=true recomputes.
  getContentReport: (id, { refresh = false } = {}) =>
    api.get(`/catalogs/${id}/content-report`, { params: refresh ? { refresh: 1 } : {} }),

  getContentByTime: (id, startYear, endYear) => {
    const params = {};
    if (startYear) params.start_year = startYear;
    if (endYear) params.end_year = endYear;
    return api.get(`/catalogs/${id}/content-by-time`, { params });
  },

  // Import Progress report: recent picture imports, and one import's phases.
  getImports: (id, limit = 20) => api.get(`/catalogs/${id}/imports`, { params: { limit } }),
  getImportProgress: (id, executionId) =>
    api.get(`/catalogs/${id}/imports/${executionId}/progress`),
  stopExecution: (id, executionId) => api.post(`/catalogs/${id}/executions/${executionId}/stop`),
};

export const faceDetectionAPI = {
  // Detect faces in an image
  detectFaces: (catalogId, fileId) => api.post(`/catalogs/${catalogId}/images/${fileId}/detect-faces`),
  
  // Get faces for an image
  getFaces: (catalogId, fileId) => api.get(`/catalogs/${catalogId}/images/${fileId}/faces`),
  
  // Create a manual face detection
  createManualFace: (catalogId, fileId, faceData) => api.post(`/catalogs/${catalogId}/images/${fileId}/faces/manual`, faceData),
  
  // Tag a face with a person
  tagFace: (catalogId, faceId, personId) => api.put(`/catalogs/${catalogId}/faces/${faceId}/tag`, { person_id: personId }),

  // Assign many faces to one person in a single request. Faces that already
  // have a person are skipped server-side, never reassigned.
  tagFacesBulk: (catalogId, personId, faceIds) =>
    api.post(`/catalogs/${catalogId}/faces/tag-bulk`, {
      person_id: personId,
      face_ids: faceIds,
    }),
  
  // Untag a face (remove person association)
  untagFace: (catalogId, faceId) => api.put(`/catalogs/${catalogId}/faces/${faceId}/tag`, { person_id: null }),
  
  // Delete a face detection
  deleteFace: (catalogId, faceId) => api.delete(`/catalogs/${catalogId}/faces/${faceId}`),

  // Get alternative JPG version for a HEIC/CR3 file
  getAlternativeJpg: (catalogId, fileId) => api.get(`/catalogs/${catalogId}/images/${fileId}/alternative-jpg`),

  // Get available years, months, and days for images in a catalog
  getImagesYearsMonths: (catalogId) => api.get(`/catalogs/${catalogId}/images/years-months`),

  // Get photos where a person was detected (face_detections) for a given year
  getPersonDetectedPhotos: (catalogId, personId, year) => api.get(`/catalogs/${catalogId}/people/${personId}/detected-photos/${year}`),

  // One page of them across every year, or {year} one, oldest first:
  // {photos, total, limit, offset}.
  getPersonDetectedPhotosPage: (catalogId, personId, { year, limit, offset } = {}) =>
    api.get(`/catalogs/${catalogId}/people/${personId}/detected-photos`,
            { params: { ...(year ? { year } : {}), limit, offset } }),

  // Set the profile photo for a person using a catalog file_id
  setProfilePhoto: (catalogId, personId, fileId) => api.put(`/catalogs/${catalogId}/people/${personId}/profile-photo`, { file_id: fileId }),

  // Get face contents (annotated face images) for a person, optionally filtered by year/month
  getPersonFaceContents: (catalogId, personId, year, month) => {
    const params = {};
    if (year) params.year = year;
    if (month) params.month = month;
    return api.get(`/catalogs/${catalogId}/people/${personId}/faces`, { params });
  },
};

export const peopleAPI = {
  // Get all people in a catalog
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/people`),
};

// ── Pets ─────────────────────────────────────────────────────────────────────
//
// A pet IS a `people` row with kind='pet', so creating, renaming, categorising,
// tagging and photographing one go through the people endpoints with that kind.
// This wrapper exists so the pet pages never write the word "people" and never
// forget the parameter - forgetting it would list humans on the Pets page, and
// on the People side the default protects the face tagger.
//
// See documentation/specs/pet-recognition-spec.md section 7.
export const petAPI = {
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/people?kind=pet`),
  getById: (catalogId, petId) => api.get(`/catalogs/${catalogId}/people/${petId}`),
  create: (catalogId, body) =>
    api.post(`/catalogs/${catalogId}/people`, { ...body, kind: 'pet' }),
  // `kind` is immutable server-side, so it is deliberately not sent here.
  update: (catalogId, petId, body) =>
    api.put(`/catalogs/${catalogId}/people/${petId}`, body),
  remove: (catalogId, petId) => api.delete(`/catalogs/${catalogId}/people/${petId}`),

  // Whether this catalog looks for pets, and what it has found so far.
  settings: (catalogId) => api.get(`/catalogs/${catalogId}/pets/settings`),

  // One page of a pet's photographs, newest first:
  // {detections, total, limit, offset, years: {year: count}}.
  detections: (catalogId, petId, { year, limit, offset } = {}) =>
    api.get(`/catalogs/${catalogId}/pets/${petId}/detections`,
            { params: { ...(year ? { year } : {}), limit, offset } }),
  // Every animal found in one photograph, for the viewer's overlay. The pet
  // half of faceDetectionAPI.getFaces, and the same shape, because both are
  // drawn on one canvas in one coordinate space.
  imageDetections: (catalogId, fileId) =>
    api.get(`/catalogs/${catalogId}/images/${fileId}/pet-detections`),

  // Star one of a pet's photographs as its card picture. Takes the DETECTION
  // the gallery has in hand; the backend stores that detection's file, which
  // is what survives the next detection run.
  setProfilePhoto: (catalogId, petId, detectionId) =>
    api.put(`/catalogs/${catalogId}/pets/${petId}/profile-photo`,
            { detection_id: detectionId }),

  assignDetection: (catalogId, detectionId, personId) =>
    api.post(`/catalogs/${catalogId}/pet-detections/${detectionId}/assign`,
             { person_id: personId }),
  unassignDetection: (catalogId, detectionId) =>
    api.delete(`/catalogs/${catalogId}/pet-detections/${detectionId}/assign`),

  // Groups of unnamed animals, and the two things one can do with a group.
  clusters: (catalogId, params) =>
    api.get(`/catalogs/${catalogId}/pets/clusters`, { params: params || {} }),
  // Takes either {person_id} or {name, species, category} - one request, so a
  // failure cannot leave a half-named animal.
  nameCluster: (catalogId, clusterId, body) =>
    api.post(`/catalogs/${catalogId}/pets/clusters/${clusterId}/assign`, body),
  rejectCluster: (catalogId, clusterId) =>
    api.post(`/catalogs/${catalogId}/pets/clusters/${clusterId}/reject`),
};

// Catalog backups (documentation/specs/backup-backrest-spec.md).
export const backupAPI = {
  list: (catalogId) => api.get(`/catalogs/${catalogId}/backups`),
  test: (catalogId, body) => api.post(`/catalogs/${catalogId}/backups/test`, body),
  create: (catalogId, body) => api.post(`/catalogs/${catalogId}/backups`, body),
  update: (catalogId, id, body) => api.put(`/catalogs/${catalogId}/backups/${id}`, body),
  remove: (catalogId, id) => api.delete(`/catalogs/${catalogId}/backups/${id}`),
  run: (catalogId, id) => api.post(`/catalogs/${catalogId}/backups/${id}/run`),
  verify: (catalogId, id) => api.post(`/catalogs/${catalogId}/backups/${id}/verify`),
  snapshots: (catalogId, id) => api.get(`/catalogs/${catalogId}/backups/${id}/snapshots`),
  restore: (catalogId, id, body) => api.post(`/catalogs/${catalogId}/backups/${id}/restore`, body),
  cancel: (catalogId, id) => api.post(`/catalogs/${catalogId}/backups/${id}/cancel`),
};

// The Dashboard (documentation/specs/dashboard-specs.md): the page in one request,
// and "Refresh stats", which recounts now.
export const dashboardAPI = {
  get: (catalogId) => api.get(`/catalogs/${catalogId}/dashboard`),
  refresh: (catalogId) => api.post(`/catalogs/${catalogId}/dashboard/refresh`),
};

// Photos and videos per day, cached (documentation/architecture/calendar-view-spec.md).
export const calendarAPI = {
  get: (catalogId) => api.get(`/catalogs/${catalogId}/calendar`),
  refresh: (catalogId) => api.post(`/catalogs/${catalogId}/calendar/refresh`),
};

// Search (documentation/architecture/search-spec.md): one box and filters, and
// saved searches - a query plus the photos and videos chosen from it.
export const searchAPI = {
  search: (catalogId, params) => api.get(`/catalogs/${catalogId}/search`, { params }),
  rebuildIndex: (catalogId) => api.post(`/catalogs/${catalogId}/search/index`),
};

export const savedSearchAPI = {
  list: (catalogId, status) =>
    api.get(`/catalogs/${catalogId}/saved-searches`, status ? { params: { status } } : undefined),
  create: (catalogId, body) => api.post(`/catalogs/${catalogId}/saved-searches`, body),
  get: (catalogId, id, { run = false } = {}) =>
    api.get(`/catalogs/${catalogId}/saved-searches/${id}`, run ? { params: { run: 1 } } : undefined),
  update: (catalogId, id, body) => api.patch(`/catalogs/${catalogId}/saved-searches/${id}`, body),
  keep: (catalogId, id) => api.patch(`/catalogs/${catalogId}/saved-searches/${id}`, {}),
  remove: (catalogId, id) => api.delete(`/catalogs/${catalogId}/saved-searches/${id}`),
  files: (catalogId, id, params) =>
    api.get(`/catalogs/${catalogId}/saved-searches/${id}/files`, { params }),
  addFiles: (catalogId, id, body) => api.post(`/catalogs/${catalogId}/saved-searches/${id}/files`, body),
  removeFiles: (catalogId, id, fileIds) =>
    api.delete(`/catalogs/${catalogId}/saved-searches/${id}/files`, { data: { file_ids: fileIds } }),
};

export const peopleCategoryAPI = {
  // Get all categories for a catalog
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/people-categories`),

  // Create a new category
  create: (catalogId, name) => api.post(`/catalogs/${catalogId}/people-categories`, { name }),

  // Rename a category and move everyone in it. Names go in the body: they are
  // free text and may contain a slash.
  rename: (catalogId, from, to) =>
    api.put(`/catalogs/${catalogId}/people-categories/rename`, { from, to }),
};

export const tagAPI = {
  // Get all tags in a catalog (optionally filtered by search query)
  getAll: (catalogId, query) => {
    const params = query ? { q: query } : {};
    return api.get(`/catalogs/${catalogId}/tags`, { params });
  },

  // Get tags for a specific person
  getPersonTags: (catalogId, personId) => api.get(`/catalogs/${catalogId}/people/${personId}/tags`),

  // Set (replace) all tags for a person
  setPersonTags: (catalogId, personId, tags) =>
    api.put(`/catalogs/${catalogId}/people/${personId}/tags`, { tags }),
};

export const eventAPI = {
  // Get all events for a catalog
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/events`),
  // One day: the events with a file that day, and how many files are in none
  // (calendar-view-spec.md section 6).
  getDay: (catalogId, date) => api.get(`/catalogs/${catalogId}/events`, { params: { date } }),

  // Get a specific event
  getById: (catalogId, eventId) => api.get(`/catalogs/${catalogId}/events/${eventId}`),

  // Create a new event
  create: (catalogId, event) => api.post(`/catalogs/${catalogId}/events`, event),

  // Update an event
  update: (catalogId, eventId, event) => api.put(`/catalogs/${catalogId}/events/${eventId}`, event),

  // Delete an event
  delete: (catalogId, eventId) => api.delete(`/catalogs/${catalogId}/events/${eventId}`),

  // Get files associated with an event
  getFiles: (catalogId, eventId) => api.get(`/catalogs/${catalogId}/events/${eventId}/files`),

  // Get people tagged in photos associated with an event
  getPeople: (catalogId, eventId) => api.get(`/catalogs/${catalogId}/events/${eventId}/people`),

  // Get locations associated with an event
  getLocations: (catalogId, eventId) => api.get(`/catalogs/${catalogId}/events/${eventId}/locations`),

  // Add a new location to an event (creates location + association)
  addLocation: (catalogId, eventId, location) => api.post(`/catalogs/${catalogId}/events/${eventId}/locations`, location),

  // Remove a location association from an event
  removeLocation: (catalogId, eventId, locationId) => api.delete(`/catalogs/${catalogId}/events/${eventId}/locations/${locationId}`),

  // Set the photo shown on this event's card. The backend refuses a file that
  // is not in the event (event-cards-ui-spec.md section 4).
  setCover: (catalogId, eventId, fileId) =>
    api.put(`/catalogs/${catalogId}/events/${eventId}/cover`, { file_id: fileId }),

  // Clear it; the next read of the events list rolls a fresh one and stores it.
  clearCover: (catalogId, eventId) => api.delete(`/catalogs/${catalogId}/events/${eventId}/cover`),

  // Events this one can be merged into: the same day, the day before or the
  // day after, each with its `relation` and live photo count.
  getMergeCandidates: (catalogId, eventId) =>
    api.get(`/catalogs/${catalogId}/events/${eventId}/merge-candidates`),

  // Move this event's photos into another and delete this one. The target keeps
  // its name, description, cover and locations.
  merge: (catalogId, eventId, intoEventId) =>
    api.post(`/catalogs/${catalogId}/events/${eventId}/merge`, { into_event_id: intoEventId }),

  // The events each file belongs to now: {"<file_id>": [{id, name, start_time,
  // type}]}. After a date batch, Create Events has given Auto events new ids,
  // so this is how the event page finds where the photos went
  // (mass-edit-event-photo-dates-spec.md section 5.3).
  locate: (catalogId, fileIds) =>
    api.get(`/catalogs/${catalogId}/events/locate`, { params: { file_ids: fileIds.join(',') } }),
};

export const locationAPI = {
  // Get all locations in a catalog. confirmedOnly is what the event-tagging
  // picker asks for: the clustering workflow writes an unconfirmed location per
  // cluster, and offering a thousand guesses when somebody wants to tag one
  // event would bury the handful they named.
  getAll: (catalogId, { confirmedOnly = false } = {}) =>
    api.get(`/catalogs/${catalogId}/locations`,
            confirmedOnly ? { params: { confirmed: 1 } } : undefined),

  // One page of locations, for the Locations page: {locations, total, limit,
  // offset}. Every card fetches a map tile and a thumbnail, so a page of cards
  // is a page of requests - which is the point, far more than the JSON size.
  getPage: (catalogId, { offset = 0, limit = 24, confirmedOnly = false } = {}) =>
    api.get(`/catalogs/${catalogId}/locations`, {
      params: { offset, limit, ...(confirmedOnly ? { confirmed: 1 } : {}) },
    }),

  // Confirm a location, renaming it in the same step. Both are the same act -
  // a person has looked at this and says it is a location.
  confirm: (catalogId, locationId, name) =>
    api.post(`/catalogs/${catalogId}/locations/${locationId}/confirm`,
             name ? { name } : {}),

  unconfirm: (catalogId, locationId) =>
    api.post(`/catalogs/${catalogId}/locations/${locationId}/unconfirm`),

  // Get a specific location
  getById: (catalogId, locationId) => api.get(`/catalogs/${catalogId}/locations/${locationId}`),

  // Create a new location
  create: (catalogId, location) => api.post(`/catalogs/${catalogId}/locations`, location),

  // Update a location
  update: (catalogId, locationId, location) => api.put(`/catalogs/${catalogId}/locations/${locationId}`, location),

  // Delete a location
  delete: (catalogId, locationId) => api.delete(`/catalogs/${catalogId}/locations/${locationId}`),

  // Link an existing location to an event
  linkToEvent: (catalogId, eventId, locationId) =>
    api.put(`/catalogs/${catalogId}/events/${eventId}/locations/${locationId}`),

  // Get photos associated with a location (by GPS proximity), optionally filtered by year/month.
  // With {limit, offset} it is one page, {photos, total, limit, offset, years_months};
  // without, every photo of the location.
  getPhotos: (catalogId, locationId, year, month, { limit, offset } = {}) => {
    const params = {};
    if (year) params.year = year;
    if (month) params.month = month;
    if (limit != null) params.limit = limit;
    if (offset != null) params.offset = offset;
    return api.get(`/catalogs/${catalogId}/locations/${locationId}/photos`, { params });
  },

  // Get the URL for a location's static map image
  getMapImageUrl: (catalogId, locationId) =>
    `${API_BASE_URL}/catalogs/${catalogId}/locations/${locationId}/map-image`,

  // ── Clusters (location-spec.md 8.1) ───────────────────────────────────────
  // The geohash bookkeeping behind an unconfirmed location. The page works on
  // locations, not clusters; these remain because a REJECTION lives on the
  // cluster, so that the next clustering run does not propose it again.
  getClusters: (catalogId, { status = 'proposed', limit = 60, offset = 0 } = {}) =>
    api.get(`/catalogs/${catalogId}/location-clusters`,
            { params: { status, limit, offset } }),

  // No acceptCluster: naming a proposal is confirm() on the LOCATION now, which
  // the clustering workflow has already created. Two ways to turn a cluster
  // into a named location would be one too many.
  rejectCluster: (catalogId, clusterId) =>
    api.post(`/catalogs/${catalogId}/location-clusters/${clusterId}/reject`),

  reopenCluster: (catalogId, clusterId) =>
    api.post(`/catalogs/${catalogId}/location-clusters/${clusterId}/reopen`),

  // Every location grouped for the map, with how much happened at each, plus
  // the whole year range the slider can select. `zoom` picks the grouping
  // server-side; see location-map-spec.md section 4.
  map: (catalogId, { zoom, from, to } = {}) => {
    const params = {};
    if (zoom != null) params.zoom = zoom;
    if (from != null) params.from = from;
    if (to != null) params.to = to;
    return api.get(`/catalogs/${catalogId}/locations/map`, { params });
  },

  // Nearby points of interest, for naming a location.
  //
  // Through the BACKEND, not straight at backend-geoloc as this used to be.
  // The browser needs no route to an internal service, the answer can be
  // cached server-side, and Nominatim's etiquette limit is enforced once
  // rather than once per open tab. See location-spec.md 7.3.
  geocode: (catalogId, lat, lon, { limit = 5, dist = 500 } = {}) =>
    api.get(`/catalogs/${catalogId}/geocode`, { params: { lat, lon, limit, dist } }),

  // Tag one photo with a location by hand, or clear it with location_id: null.
  // Recorded as location_source 'manual', which no workflow overwrites.
  setPhotoLocation: (catalogId, photoId, locationId) =>
    api.put(`/catalogs/${catalogId}/photos/${photoId}/location`,
            { location_id: locationId }),
};

export const photoAPI = {
  // Get all photos from a catalog
  // The whole catalog. 301,195 rows and 190 MB on `family`, so nothing in the
  // UI asks for it any more - kept for callers that predate the scoping.
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/photos`),

  // Which days hold photographs, and how many: the gallery's navigation, 154 KB
  // where getAll is 190 MB.
  calendar: (catalogId) => api.get(`/catalogs/${catalogId}/photos/calendar`),

  // One day, month or year of photographs. params: {year, month, day}, months
  // 1-BASED here - the UI's are 0-based and the caller converts. With
  // {limit, offset} or {limit, around} it is one page, oldest first:
  // {photos, total, limit, offset, position?} - see hooks/usePhotoScope.
  getScope: (catalogId, params) =>
    api.get(`/catalogs/${catalogId}/photos`, { params }),

  // Which day one photograph is on, so a deep link opens without loading the
  // catalog to find out.
  dayOf: (catalogId, fileId) =>
    api.get(`/catalogs/${catalogId}/photos/${fileId}/day`),
  
  // Get detailed information about a specific photo
  getById: (catalogId, photoId) => api.get(`/catalogs/${catalogId}/photos/${photoId}`),

  // Update the date/time of a photo (moves file + cache, updates DB, re-assigns event)
  updateDate: (catalogId, photoId, datetime, timezone) =>
    api.put(`/catalogs/${catalogId}/photos/${photoId}/date`, { datetime, timezone }),

  // Re-date many files at once (mass-edit-event-photo-dates-spec.md section
  // 5.1). `changes` is [{file_ids, datetime: "YYYY-MM-DDTHH:MM", timezone}],
  // one entry per target date. Answers 202 {batch_id, file_count} at once and
  // does the work in the background; 409 catalog_busy while an import runs.
  // `eventId` is only recorded on the batch, so the event page can find it
  // again after Create Events has replaced the event.
  updateDates: (catalogId, eventId, changes, rebuildEvents = true) =>
    api.post(`/catalogs/${catalogId}/photos/dates`, {
      event_id: Number(eventId), changes, rebuild_events: rebuildEvents,
    }),

  // One date batch: status (running | rebuilding | completed | interrupted |
  // failed), done/total, file_ids and, once finished, its result.
  getDateBatch: (catalogId, batchId) =>
    api.get(`/catalogs/${catalogId}/photos/dates/batches/${batchId}`),

  // The newest batch opened on an event, or 404. Used when an event id has
  // vanished, to follow its photos to the event that holds them now.
  latestDateBatch: (catalogId, eventId) =>
    api.get(`/catalogs/${catalogId}/photos/dates/batches`, {
      params: { event_id: eventId, latest: 1 },
    }),

  // Edit a file's description. PUT, and /files/ rather than /photos/ — the
  // endpoint is file-generic, which is why videos reuse it unchanged.
  updateDescription: (catalogId, fileId, description) =>
    api.put(`/catalogs/${catalogId}/files/${fileId}/description`, { description }),

  // Remove a photo or video (and its RAW+JPG partner) from the catalog. The
  // original stays on disk for the user to delete by hand; everything made
  // from it is deleted, and it leaves events and albums.
  removeFromCatalog: (catalogId, fileId) =>
    api.post(`/catalogs/${catalogId}/files/${fileId}/remove`),
};

// Files removed from a catalog whose originals may still be on disk.
export const removedFilesAPI = {
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/removed-files`),
  restore: (catalogId, fileId) =>
    api.post(`/catalogs/${catalogId}/files/${fileId}/restore`),
  // fileIds omitted: every removed file whose original is gone from disk.
  // Files still on disk are never purged. Owner only.
  purge: (catalogId, fileIds) =>
    api.post(`/catalogs/${catalogId}/removed-files/purge`, fileIds ? { file_ids: fileIds } : {}),
};

// Videos. Disjoint from photoAPI.getAll by construction: /photos filters to
// image extensions and /videos to media_types.VIDEO_EXTENSIONS, so no file
// appears in both. Everything else about a video — detail, date, description —
// goes through photoAPI, whose endpoints are file-generic.
// See documentation/specs/video-page-spec.md.
export const videoAPI = {
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/videos`),
};

export const userSettingsAPI = {
  // Get current user settings
  get: () => api.get('/user/settings'),
  
  // Update user settings
  update: (settings) => api.put('/user/settings', settings),
};

export const faceRecognitionAPI = {
  // Identify people, counted and paged on the server. A face is a candidate
  // for the one person who is its best match, when its rounded similarity
  // meets min_similarity. metric: 'cosine' (default) | 'contextual'.
  //
  // Per person {candidates, matched}, plus has_scores / has_unidentified.
  // params: {metric, min_similarity}
  getUnidentifiedSummary: (catalogId, params) =>
    api.get(`/catalogs/${catalogId}/faces/unidentified/summary`, { params }),

  // One person's candidates, closest first: {faces, total, limit, offset}.
  // params: {person_id, metric, min_similarity, limit, offset}
  getUnidentifiedFaces: (catalogId, params) =>
    api.get(`/catalogs/${catalogId}/faces/unidentified`, { params }),

  // Clusters of unidentified faces judged to be the same person, so one
  // confirmation can label the whole group.
  getClusters: (catalogId, params) =>
    api.get(`/catalogs/${catalogId}/faces/clusters`, { params }),

  // Faces the model tagged without human review, with the evidence for each.
  getAutoTags: (catalogId, params) =>
    api.get(`/catalogs/${catalogId}/auto-tags`, { params }),

  // Undo a whole auto-tagging run.
  revertAutoTagRun: (catalogId, executionId) =>
    api.post(`/catalogs/${catalogId}/auto-tags/revert`, { execution_id: executionId }),

  // Undo one auto-tagged face, returning it to the review queue.
  revertAutoTag: (catalogId, faceId) =>
    api.delete(`/catalogs/${catalogId}/faces/${faceId}/auto-tag`),

  // Assign every member of a cluster to one person in a single call.
  // excludeFaceIds lets the reviewer drop members that clearly do not belong;
  // faces already carrying a person are skipped server-side, never overwritten.
  assignCluster: (catalogId, clusterId, personId, excludeFaceIds = []) =>
    api.post(`/catalogs/${catalogId}/faces/clusters/${clusterId}/assign`, {
      person_id: personId,
      exclude_face_ids: excludeFaceIds,
    }),

  // Get all similarity scores for a single query face.
  // Both distance columns are always present; metric controls sort and threshold filter.
  getSimilarityScores: (catalogId, faceId, { threshold, metric } = {}) =>
    api.get(`/catalogs/${catalogId}/faces/${faceId}/similarity-scores`, {
      params: {
        ...(threshold !== undefined && { threshold }),
        ...(metric !== undefined && { metric }),
      },
    }),

  // Get a summary of best candidates per unidentified face within a threshold.
  getSimilaritySummary: (catalogId, { threshold, metric } = {}) =>
    api.get(`/catalogs/${catalogId}/faces/similarity-scores/summary`, {
      params: {
        ...(threshold !== undefined && { threshold }),
        ...(metric !== undefined && { metric }),
      },
    }),

  // Remove similarity scores for a (face, person) pair — "not this person" rejection
  rejectCandidate: (catalogId, faceId, personId) =>
    api.delete(`/catalogs/${catalogId}/faces/${faceId}/similarity-scores/${personId}`),
};

export const albumAPI = {
  // List albums for a catalog
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/albums`),

  // Get a single album (includes filters, sections + counts)
  getById: (catalogId, albumId) => api.get(`/catalogs/${catalogId}/albums/${albumId}`),

  // Create a new album
  create: (catalogId, album) => api.post(`/catalogs/${catalogId}/albums`, album),

  // Update album header (name / description)
  update: (catalogId, albumId, album) => api.put(`/catalogs/${catalogId}/albums/${albumId}`, album),

  // Delete an album
  delete: (catalogId, albumId) => api.delete(`/catalogs/${catalogId}/albums/${albumId}`),

  // Add a filter statement
  addFilter: (catalogId, albumId, nlText) =>
    api.post(`/catalogs/${catalogId}/albums/${albumId}/filters`, { nl_text: nlText }),

  // Remove a filter
  deleteFilter: (catalogId, albumId, filterId) =>
    api.delete(`/catalogs/${catalogId}/albums/${albumId}/filters/${filterId}`),

  // Get the final resolved photo list (all sections combined)
  getPhotos: (catalogId, albumId) => api.get(`/catalogs/${catalogId}/albums/${albumId}/photos`),

  // ── Sections ──────────────────────────────────────────────────────────────

  // List sections for an album (each includes photo_count)
  getSections: (catalogId, albumId) =>
    api.get(`/catalogs/${catalogId}/albums/${albumId}/sections`),

  // Add a section to an album
  createSection: (catalogId, albumId, name) =>
    api.post(`/catalogs/${catalogId}/albums/${albumId}/sections`, { name }),

  // Rename a section
  updateSection: (catalogId, albumId, sectionId, name) =>
    api.put(`/catalogs/${catalogId}/albums/${albumId}/sections/${sectionId}`, { name }),

  // Delete a section
  deleteSection: (catalogId, albumId, sectionId) =>
    api.delete(`/catalogs/${catalogId}/albums/${albumId}/sections/${sectionId}`),

  // ── Section photos ────────────────────────────────────────────────────────

  // Get photos assigned to a section
  getSectionPhotos: (catalogId, albumId, sectionId) =>
    api.get(`/catalogs/${catalogId}/albums/${albumId}/sections/${sectionId}/photos`),

  // Add a photo to a section by file_id
  addSectionPhoto: (catalogId, albumId, sectionId, fileId) =>
    api.post(`/catalogs/${catalogId}/albums/${albumId}/sections/${sectionId}/photos`, { file_id: fileId }),

  // Remove a photo from a section
  removeSectionPhoto: (catalogId, albumId, sectionId, fileId) =>
    api.delete(`/catalogs/${catalogId}/albums/${albumId}/sections/${sectionId}/photos/${fileId}`),

  // Import an event's photos into a section (creates or re-syncs the section)
  // categoryId null (or omitted) files the new section at the album root, which
  // is exactly what this did before categories existed.
  // template-albums-spec.md §5: the years a template can use, and the one-step create.
  getYearTemplate: (catalogId) =>
    api.get(`/catalogs/${catalogId}/album-templates/year`),
  createFromTemplate: (catalogId, body) =>
    api.post(`/catalogs/${catalogId}/albums/from-template`, body),
  importEventToSection: (catalogId, albumId, eventId, categoryId = null) =>
    api.post(`/catalogs/${catalogId}/albums/${albumId}/sections/import-event`,
      { event_id: eventId, category_id: categoryId }),

  // ── Categories ────────────────────────────────────────────────────────────
  // See documentation/specs/album-gallery-enhancement-spec.md.
  //
  // The root is not a category: it is the album, and the list endpoint returns
  // its label separately as { root, categories }. A null parent_id or
  // category_id everywhere below means "at the root".

  getCategories: (catalogId, albumId) =>
    api.get(`/catalogs/${catalogId}/albums/${albumId}/categories`),

  createCategory: (catalogId, albumId, name, parentId = null) =>
    api.post(`/catalogs/${catalogId}/albums/${albumId}/categories`,
      { name, parent_id: parentId }),

  // Pass only what changes: omitting parent_id leaves the category where it is,
  // which is not the same as passing null (that would move it to the root).
  updateCategory: (catalogId, albumId, categoryId, patch) =>
    api.put(`/catalogs/${catalogId}/albums/${albumId}/categories/${categoryId}`, patch),

  // mode: undefined refuses a non-empty category with a 409 naming the counts;
  // 'reparent' lifts its children up one level; 'cascade' destroys the subtree
  // and every photo list in it.
  deleteCategory: (catalogId, albumId, categoryId, mode) =>
    api.delete(`/catalogs/${catalogId}/albums/${albumId}/categories/${categoryId}`
      + (mode ? `?mode=${mode}` : '')),

  setSectionCategory: (catalogId, albumId, sectionId, categoryId) =>
    api.put(`/catalogs/${catalogId}/albums/${albumId}/sections/${sectionId}/category`,
      { category_id: categoryId }),

  setSectionCover: (catalogId, albumId, sectionId, fileId) =>
    api.put(`/catalogs/${catalogId}/albums/${albumId}/sections/${sectionId}/cover`,
      { file_id: fileId }),

  clearSectionCover: (catalogId, albumId, sectionId) =>
    api.delete(`/catalogs/${catalogId}/albums/${albumId}/sections/${sectionId}/cover`),

  // Deploy (or re-deploy) an album: rebuild folder + manifest
  deploy: (catalogId, albumId) =>
    api.post(`/catalogs/${catalogId}/albums/${albumId}/deploy`),
};

// Album deployments: named, shareable galleries holding one or more albums.
// See documentation/specs/albums-enhancement-spec.md.
//
// Catalog-scoped like albums, because a deployment belongs to exactly one
// catalog - which is also why the album picker needs no catalog column.
// Album transfer over magic-wormhole.
// See documentation/specs/wormhole-album-transfer-spec.md.
export const transferAPI = {
  start: (catalogId, albumId) =>
    api.post(`/catalogs/${catalogId}/albums/${albumId}/transfer`),
  receive: (catalogId, payload) =>
    api.post(`/catalogs/${catalogId}/transfers/receive`, payload),
  list: (catalogId) => api.get(`/catalogs/${catalogId}/transfers`),
  get: (catalogId, transferId) =>
    api.get(`/catalogs/${catalogId}/transfers/${transferId}`),
  // Paginated on the server: an album of thousands of photos must not send
  // every row to draw one progress bar.
  files: (catalogId, transferId, page = 1, perPage = 50) =>
    api.get(`/catalogs/${catalogId}/transfers/${transferId}/files`,
      { params: { page, per_page: perPage } }),
  cancel: (catalogId, transferId) =>
    api.post(`/catalogs/${catalogId}/transfers/${transferId}/cancel`),
  resume: (catalogId, transferId, payload = {}) =>
    api.post(`/catalogs/${catalogId}/transfers/${transferId}/resume`, payload),
  // Sending one album to several people, one code each, one deadline
  // (album-mass-transfer-spec.md). `recipients` is [{label}], one per person.
  massStart: (catalogId, albumId, recipients, ttlHours) =>
    api.post(`/catalogs/${catalogId}/albums/${albumId}/mass-transfer`,
      { recipients, ttl_hours: ttlHours }),
  batch: (catalogId, batchId) =>
    api.get(`/catalogs/${catalogId}/transfer-batches/${batchId}`),
  addPerson: (catalogId, batchId, label) =>
    api.post(`/catalogs/${catalogId}/transfer-batches/${batchId}/people`, { label }),
  // transferIds omitted = everyone whose code expired, failed or was stopped.
  renew: (catalogId, batchId, ttlHours, transferIds) =>
    api.post(`/catalogs/${catalogId}/transfer-batches/${batchId}/renew`,
      { ttl_hours: ttlHours, ...(transferIds ? { transfer_ids: transferIds } : {}) }),
  cancelBatch: (catalogId, batchId) =>
    api.post(`/catalogs/${catalogId}/transfer-batches/${batchId}/cancel`),
};

export const deploymentAPI = {
  // { catalog_slug, catalog_slug_problem, deployments: [...] }
  getAll: (catalogId) => api.get(`/catalogs/${catalogId}/deployments`),

  // { name, description, slug? } - the slug is derived from the name when absent
  create: (catalogId, deployment) =>
    api.post(`/catalogs/${catalogId}/deployments`, deployment),

  // Rename, re-describe, re-slug, enable or disable
  update: (catalogId, deploymentId, deployment) =>
    api.put(`/catalogs/${catalogId}/deployments/${deploymentId}`, deployment),

  // Removes the published directory. The albums themselves are untouched.
  remove: (catalogId, deploymentId) =>
    api.delete(`/catalogs/${catalogId}/deployments/${deploymentId}`),

  // { password } to lock it, { password: null } to make it public again.
  // Owner-only on the backend: this decides who can see the catalog's
  // photographs from outside the application entirely.
  setPassword: (catalogId, deploymentId, password) =>
    api.put(`/catalogs/${catalogId}/deployments/${deploymentId}/password`, { password }),

  addAlbum: (catalogId, deploymentId, albumId) =>
    api.post(`/catalogs/${catalogId}/deployments/${deploymentId}/albums`,
             { album_id: albumId }),

  removeAlbum: (catalogId, deploymentId, albumId) =>
    api.delete(`/catalogs/${catalogId}/deployments/${deploymentId}/albums/${albumId}`),

  // Rewrite the published directory from the database. Every mutation does this
  // anyway; this is the explicit repair for a directory edited or lost by hand.
  publish: (catalogId, deploymentId) =>
    api.post(`/catalogs/${catalogId}/deployments/${deploymentId}/publish`),
};

// Basic photo editing (documentation/specs/basic-photo-editing.md section 5.2).
// save() answers 202 while the derivatives are rebuilt; poll get() until
// render_status is 'ready' or 'failed'.
export const photoEditAPI = {
  get: (catalogId, fileId) => api.get(`/catalogs/${catalogId}/files/${fileId}/edits`),
  save: (catalogId, fileId, orientation, baseVersion) =>
    api.put(`/catalogs/${catalogId}/files/${fileId}/edits`,
      { orientation, base_version: baseVersion }),
  reset: (catalogId, fileId, baseVersion) =>
    api.delete(`/catalogs/${catalogId}/files/${fileId}/edits`,
      { data: { base_version: baseVersion } }),
  versions: (catalogId) => api.get(`/catalogs/${catalogId}/edits/versions`),
};

// Publishing a deployment to a server (deployment-publish-to-server-spec.md).
// Owner only. preview/publish/setup answer 202 {run_id}; follow it with run().
export const publishServerAPI = {
  list: (catalogId, deploymentId) =>
    api.get(`/catalogs/${catalogId}/deployments/${deploymentId}/servers`),
  add: (catalogId, deploymentId, server) =>
    api.post(`/catalogs/${catalogId}/deployments/${deploymentId}/servers`, server),
  update: (catalogId, deploymentId, serverId, fields) =>
    api.put(`/catalogs/${catalogId}/deployments/${deploymentId}/servers/${serverId}`, fields),
  forget: (catalogId, deploymentId, serverId) =>
    api.delete(`/catalogs/${catalogId}/deployments/${deploymentId}/servers/${serverId}`),
  key: (catalogId, deploymentId, serverId) =>
    api.get(`/catalogs/${catalogId}/deployments/${deploymentId}/servers/${serverId}/key`),
  // Without a fingerprint: read the server's host key for you to confirm.
  // With one: pin it, if the server still shows the same.
  pin: (catalogId, deploymentId, serverId, fingerprint) =>
    api.post(`/catalogs/${catalogId}/deployments/${deploymentId}/servers/${serverId}/pin`,
      fingerprint ? { confirm: true, fingerprint } : {}),
  start: (catalogId, deploymentId, serverId, kind, options = {}) =>
    api.post(`/catalogs/${catalogId}/deployments/${deploymentId}/servers/${serverId}/${kind}`,
      options),
  runs: (catalogId, deploymentId, serverId) =>
    api.get(`/catalogs/${catalogId}/deployments/${deploymentId}/servers/${serverId}/runs`),
  run: (catalogId, deploymentId, runId) =>
    api.get(`/catalogs/${catalogId}/deployments/${deploymentId}/publish-runs/${runId}`),
  stop: (catalogId, deploymentId, runId) =>
    api.post(`/catalogs/${catalogId}/deployments/${deploymentId}/publish-runs/${runId}/stop`),
};

export default api;
