// A photo's turn or flip in the Basic Editor
// (documentation/specs/basic-photo-editing.md section 4.1). The same
// arithmetic as backend workflows/lib/orientation.py: values use the EXIF
// numbering 1-8, each a pair (m, k) - mirror left-right when m is 1, then k
// quarter turns clockwise - applied on top of the photo's own EXIF tag.

const STATE = { 1: [0, 0], 6: [0, 1], 3: [0, 2], 8: [0, 3], 2: [1, 0], 7: [1, 1], 4: [1, 2], 5: [1, 3] };
const VALUE = Object.fromEntries(Object.entries(STATE).map(([v, [m, k]]) => [`${m},${k}`, Number(v)]));

export const TOOLS = ['rotate_left', 'rotate_right', 'flip_horizontal', 'flip_vertical'];

const mod4 = (n) => ((n % 4) + 4) % 4;

/** The orientation after applying `tool` to a photo shown at `value`. */
export const applyTool = (value, tool) => {
  let [m, k] = STATE[value] || STATE[1];
  if (tool === 'rotate_right') k += 1;
  else if (tool === 'rotate_left') k -= 1;
  else if (tool === 'flip_horizontal') { m = 1 - m; k = -k; }
  else if (tool === 'flip_vertical') { m = 1 - m; k = 2 - k; }
  else throw new Error(`unknown tool ${tool}`);
  return VALUE[`${m},${mod4(k)}`];
};

/** True when width and height trade places. */
export const swaps = (value) => (STATE[value] || STATE[1])[1] % 2 === 1;

const SUMMARY = {
  1: 'As imported',
  2: 'Flipped horizontally',
  3: 'Rotated 180°',
  4: 'Flipped vertically',
  5: 'Rotated 90° right and flipped horizontally',
  6: 'Rotated 90° right',
  7: 'Rotated 90° left and flipped horizontally',
  8: 'Rotated 90° left',
};

export const summary = (value) => SUMMARY[value] || SUMMARY[1];

/**
 * The CSS transform previewing `delta` on an image already shown upright.
 * CSS applies the list right to left: the mirror first, then the turn - the
 * (m, k) order. `fit` shrinks a quarter-turned image back into its box.
 */
export const cssTransform = (delta, fit = 1) => {
  const [m, k] = STATE[delta] || STATE[1];
  const parts = [];
  if (k) parts.push(`rotate(${k * 90}deg)`);
  if (fit !== 1) parts.push(`scale(${fit})`);
  if (m) parts.push('scaleX(-1)');
  return parts.join(' ') || 'none';
};

// The (m, k) group product, for the preview's delta from the saved edit.
const mul = ([ma, ka], [mb, kb]) => [(ma + mb) % 2, mod4(ka + (ma ? -kb : kb))];
const inv = ([m, k]) => (m ? [m, k] : [0, mod4(-k)]);

/** The orientation d with "old, then d" equal to `next`. */
export const delta = (old, next) => {
  const [m, k] = mul(STATE[next] || STATE[1], inv(STATE[old] || STATE[1]));
  return VALUE[`${m},${k}`];
};
