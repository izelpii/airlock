import { applyTool, cssTransform, delta, summary, swaps, TOOLS } from './orientation';

// The same properties as backend test_photo_edits.py: the two sides must agree,
// or the preview shows one thing and Save stores another.

const ALL = [1, 2, 3, 4, 5, 6, 7, 8];

test('four turns, or one flip twice, come back to the start', () => {
  ALL.forEach((v) => {
    let r = v;
    for (let i = 0; i < 4; i += 1) r = applyTool(r, 'rotate_right');
    expect(r).toBe(v);
    expect(applyTool(applyTool(v, 'flip_horizontal'), 'flip_horizontal')).toBe(v);
    expect(applyTool(applyTool(v, 'flip_vertical'), 'flip_vertical')).toBe(v);
    expect(applyTool(applyTool(v, 'rotate_left'), 'rotate_right')).toBe(v);
  });
  expect(applyTool(applyTool(1, 'flip_horizontal'), 'flip_vertical')).toBe(3);
});

test('the values match the backend', () => {
  expect(applyTool(1, 'rotate_right')).toBe(6);
  expect(applyTool(1, 'rotate_left')).toBe(8);
  expect(applyTool(6, 'flip_horizontal')).toBe(5);
  expect(applyTool(8, 'flip_horizontal')).toBe(7);
  expect(summary(6)).toBe('Rotated 90° right');
  expect(swaps(6) && swaps(5) && !swaps(3)).toBe(true);
});

test('delta takes one edit to another', () => {
  ALL.forEach((a) => ALL.forEach((b) => {
    const d = delta(a, b);
    // "a, then d" is b: apply d's tools after a. Composing by tools, d is
    // reached from 1 the same way, so check through the arithmetic directly.
    let viaTools = a;
    const path = { 1: [], 2: ['flip_horizontal'], 3: ['rotate_right', 'rotate_right'],
      4: ['flip_vertical'], 5: ['rotate_right', 'flip_horizontal'], 6: ['rotate_right'],
      7: ['rotate_left', 'flip_horizontal'], 8: ['rotate_left'] }[d];
    path.forEach((t) => { viaTools = applyTool(viaTools, t); });
    expect(viaTools).toBe(b);
  }));
});

test('the preview transform mirrors first, then turns', () => {
  expect(cssTransform(1)).toBe('none');
  expect(cssTransform(6)).toBe('rotate(90deg)');
  expect(cssTransform(2)).toBe('scaleX(-1)');
  expect(cssTransform(7, 0.5)).toBe('rotate(90deg) scale(0.5) scaleX(-1)');
  expect(TOOLS).toHaveLength(4);
});
