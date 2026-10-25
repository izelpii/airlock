import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import BasicEditorTools from './BasicEditorTools';

beforeAll(() => { global.IS_REACT_ACT_ENVIRONMENT = true; });

const render = async (props) => {
  const container = document.createElement('div');
  const root = createRoot(container);
  const calls = [];
  const all = {
    value: 1, saved: 1, saving: false, error: null,
    onTool: (t) => calls.push(t), onReset: () => calls.push('reset'),
    onSave: () => calls.push('save'), onCancel: () => calls.push('cancel'),
    ...props,
  };
  await act(async () => { root.render(<BasicEditorTools {...all} />); });
  const button = (text) => [...container.querySelectorAll('button')]
    .find((b) => b.textContent.includes(text));
  return { container, calls, button, root };
};

test('each tool reports itself, and Save waits for a change', async () => {
  const { calls, button, root } = await render({});
  ['Rotate left', 'Rotate right', 'Flip horizontally', 'Flip vertically']
    .forEach((t) => act(() => { button(t).click(); }));
  expect(calls).toEqual(['rotate_left', 'rotate_right', 'flip_horizontal', 'flip_vertical']);
  expect(button('Save').disabled).toBe(true);
  expect(button('Reset to original').disabled).toBe(true);
  act(() => root.unmount());
});

test('a pending edit can be saved, reset or cancelled', async () => {
  const { calls, button, container, root } = await render({ value: 6 });
  expect(container.textContent).toContain('Rotated 90° right');
  act(() => { button('Save').click(); });
  act(() => { button('Reset to original').click(); });
  act(() => { button('Cancel').click(); });
  expect(calls).toEqual(['save', 'reset', 'cancel']);
  act(() => root.unmount());
});

test('while saving nothing can be pressed', async () => {
  const { button, container, root } = await render({ value: 6, saving: true });
  expect(button('Rotate left').disabled).toBe(true);
  expect(button('Cancel').disabled).toBe(true);
  expect(container.textContent).toContain('Rebuilding');
  act(() => root.unmount());
});
