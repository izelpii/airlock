// Which photos have been edited, and how many times: {fileId: version}.
//
// A thumbnail is rewritten in place when its photo is edited, and the backend
// lets a browser keep a thumbnail for a year only when the URL names the edit
// version (basic-photo-editing.md section 7.2). galleryThumbUrl reads this
// map so every grid asks for the current version without each endpoint having
// to return it. Loaded per catalog by CatalogContext; until it has loaded,
// URLs carry no version and the backend revalidates them instead.

let current = { catalogId: null, versions: null };
// Versions saved in this tab. Kept apart from the loaded map so a save still
// changes its URLs when the map never loaded - without claiming v=0 for
// every other photo, which could name a stale year-long cache entry.
const saved = {};

export const setEditVersions = (catalogId, versions) => {
  current = { catalogId: String(catalogId), versions: { ...(versions || {}) } };
};

export const bumpEditVersion = (catalogId, fileId, version) => {
  saved[`${catalogId}:${fileId}`] = version;
};

/** The version to put in a URL, or null while the map is not loaded. */
export const editVersionOf = (catalogId, fileId) => {
  const mine = saved[`${catalogId}:${fileId}`];
  if (mine !== undefined) return mine;
  if (current.catalogId !== String(catalogId) || !current.versions) return null;
  return current.versions[String(fileId)] || 0;
};
