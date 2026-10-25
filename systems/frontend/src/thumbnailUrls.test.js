import fs from 'fs';
import path from 'path';
import { galleryThumbUrl } from './config';
import { bumpEditVersion, setEditVersions } from './utils/editVersions';

// basic-photo-editing.md section 7.2: thumbnails are rewritten in place when a
// photo is edited, and a browser keeps a versioned one for a year - so every
// thumbnail URL must be built by galleryThumbUrl, which adds the version.

const walk = (dir) => fs.readdirSync(dir, { withFileTypes: true }).flatMap((e) => {
  const p = path.join(dir, e.name);
  if (e.isDirectory()) return walk(p);
  return /\.js$/.test(e.name) && !/\.test\.js$/.test(e.name) ? [p] : [];
});

test('no file but config.js builds a thumbnail URL by hand', () => {
  const offenders = walk(__dirname)
    .filter((f) => path.basename(f) !== 'config.js')
    .filter((f) => /\/thumbnail[?`'"]/.test(fs.readFileSync(f, 'utf8')));
  expect(offenders).toEqual([]);
});

test('the URL carries the edit version once the versions are known', () => {
  expect(galleryThumbUrl(3, 5)).not.toMatch(/[?&]v=/);
  setEditVersions(3, { 5: 2 });
  expect(galleryThumbUrl(3, 5)).toMatch(/size=320&v=2$/);
  expect(galleryThumbUrl(3, 6)).toMatch(/&v=0$/);
  bumpEditVersion(3, 6, 1);
  expect(galleryThumbUrl(3, 6, 72)).toMatch(/size=72&v=1$/);
  expect(galleryThumbUrl(4, 6)).not.toMatch(/[?&]v=/);
  // A save counts even where the map never loaded.
  bumpEditVersion(4, 6, 3);
  expect(galleryThumbUrl(4, 6)).toMatch(/&v=3$/);
  expect(galleryThumbUrl(4, 7)).not.toMatch(/[?&]v=/);
});

test('public images are addressed with the build stamp', () => {
  jest.isolateModules(() => {
    process.env.REACT_APP_BUILD_ID = '20261021235959';
    // eslint-disable-next-line global-require
    const { assetUrl } = require('./config');
    expect(assetUrl('theme2/cardDeco7.png')).toBe('/theme2/cardDeco7.png?v=20261021235959');
    expect(assetUrl('/logo.png')).toBe('/logo.png?v=20261021235959');
    delete process.env.REACT_APP_BUILD_ID;
  });
  jest.isolateModules(() => {
    // eslint-disable-next-line global-require
    const { assetUrl } = require('./config');
    expect(assetUrl('logo.png')).toBe('/logo.png');          // development: no stamp
  });
});
