import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import { ServerLine, ServersModal } from './PublishServers';

// The Servers panel (deployment-publish-to-server-spec.md section 12), driven
// as a person would against a mocked API.

const mockState = { servers: [], runs: {}, calls: [] };

jest.mock('../services/api', () => ({
  __esModule: true,
  default: {},
  publishServerAPI: {
    list: () => Promise.resolve({ data: { servers: mockState.servers } }),
    add: (c, d, form) => {
      mockState.calls.push(['add', form]);
      const s = { id: 5, ...form, port: 22, host_key: null, last_run: null };
      mockState.servers = [s];
      return Promise.resolve({ data: { ...s, authorized_keys_line: 'ssh-ed25519 AAAAkey airlock-publish' } });
    },
    key: () => Promise.resolve({ data: { authorized_keys_line: 'ssh-ed25519 AAAAkey airlock-publish' } }),
    pin: (c, d, sid, fingerprint) => {
      mockState.calls.push(['pin', fingerprint || null]);
      if (fingerprint) mockState.servers = mockState.servers.map((s) => ({ ...s, host_key: fingerprint }));
      return Promise.resolve({ data: fingerprint ? { pinned: fingerprint }
        : { fingerprint: 'SHA256:abc', pinned: null } });
    },
    start: (c, d, sid, kind, options) => {
      mockState.calls.push([kind, options]);
      const id = 100 + mockState.calls.length;
      mockState.runs[id] = kind === 'preview'
        ? { id, kind, status: 'succeeded', result: { plan: { new: 2, changed: 1, deleted: 3, bytes: 2048 },
            warnings: ['Album "Summer" needs rebuilding: a photo in it was turned or flipped.'],
            deletions: ['a.jpg', 'b.jpg', 'c.jpg'], confirm: 'It would delete 3 of the 10 files.' } }
        : { id, kind, status: 'succeeded', result: { applied: true, bytes_sent: 2048,
            plan: { new: 2, changed: 1, deleted: 3 }, server_revision: 'old1',
            checks: [{ check: 'The gallery answers', ok: true, detail: '' }] } };
      return Promise.resolve({ data: { run_id: id } });
    },
    run: (c, d, id) => Promise.resolve({ data: mockState.runs[id] }),
    stop: () => Promise.resolve({ data: {} }),
  },
}));

beforeAll(() => {
  global.IS_REACT_ACT_ENVIRONMENT = true;
  global.fetch = () => Promise.resolve({ ok: true, json: () => Promise.resolve({ revision: 'new2' }) });
});
beforeEach(() => { mockState.servers = []; mockState.runs = {}; mockState.calls = []; });

const DEPLOYMENT = { id: 1, name: 'Album 2026', slug: 'album2026' };

const settle = async () => {
  for (let i = 0; i < 5; i += 1) {
    // eslint-disable-next-line no-await-in-loop
    await act(async () => { await new Promise((r) => setTimeout(r, 10)); });
  }
};

async function mount(element) {
  const container = document.createElement('div');
  document.body.appendChild(container);
  const root = createRoot(container);
  await act(async () => { root.render(element); });
  await settle();
  const text = () => document.body.textContent;
  const button = (label) => [...document.body.querySelectorAll('button')]
    .find((b) => b.textContent.trim() === label);
  const click = async (el) => { await act(async () => { el.click(); }); await settle(); };
  return { root, text, button, click };
}

const type = async (label, value) => {
  const lab = [...document.body.querySelectorAll('label')].find((l) => l.textContent === label);
  const input = lab.parentElement.querySelector('input');
  const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
  await act(async () => {
    setter.call(input, value);
    input.dispatchEvent(new Event('input', { bubbles: true }));
  });
};

test('a new server: its key line, then connect and confirm its identity, then preview and publish',
  async () => {
    const { root, text, button, click } = await mount(
      <ServersModal catalogId={7} deployment={DEPLOYMENT} visible onClose={() => {}} />,
    );
    expect(text()).toContain('The server must already exist');
    await type('Name', 'EC2');
    await type('Host', '203.0.113.10');
    await type('Public address', 'https://photos.example.com/');
    await click(button('Add server'));
    expect(mockState.calls[0][1]).toMatchObject({ name: 'EC2', host: '203.0.113.10',
      remote_dir: 'galleries/album2026', ssh_user: 'ubuntu' });
    expect(text()).toContain('ssh-ed25519 AAAAkey airlock-publish');
    expect(button('Publish')).toBeUndefined();          // not before it is confirmed

    await click(button('Connect'));
    expect(text()).toContain('SHA256:abc');
    await click(button('It matches - confirm'));
    expect(mockState.calls).toContainEqual(['pin', 'SHA256:abc']);

    await click(button('Preview'));
    expect(text()).toContain('Would send 3 file(s) (2.0 KB), would delete 3.');
    expect(text()).toContain('needs rebuilding');
    expect(text()).toContain('3 file(s) to delete');

    await click(button('Publish and delete 3 file(s)'));
    expect(mockState.calls).toContainEqual(['publish', { confirm_deletions: true }]);
    expect(text()).toContain('Published');
    expect(text()).toContain('✓ The gallery answers');
    expect(text()).toContain('The server runs gallery old1, this machine new2');
    act(() => root.unmount());
  });

test('the card line names the server and starts a publish', async () => {
  mockState.servers = [{ id: 5, name: 'EC2', host_key: 'SHA256:abc',
    last_run: { id: 1, kind: 'publish', status: 'succeeded', finished_at: new Date().toISOString(),
      result: { applied: true } } }];
  const opened = [];
  const { root, text, button, click } = await mount(
    <ServerLine catalogId={7} deployment={DEPLOYMENT} onOpen={(...a) => opened.push(a)} />,
  );
  expect(text()).toContain('EC2');
  expect(text()).toContain('Published just now');
  await click(button('Publish to AWS'));
  expect(opened).toEqual([[5, 'publish']]);
  act(() => root.unmount());
});

test('without a server the button opens the window to add one', async () => {
  const opened = [];
  const { root, button, click } = await mount(
    <ServerLine catalogId={7} deployment={DEPLOYMENT} onOpen={(...a) => opened.push(a)} />,
  );
  await click(button('Publish to AWS'));
  expect(opened).toEqual([[null]]);
  act(() => root.unmount());
});

test('Cancel closes the window when there is no server yet, and goes back when there is', async () => {
  let closed = 0;
  const { root, text, button, click } = await mount(
    <ServersModal catalogId={7} deployment={DEPLOYMENT} visible onClose={() => { closed += 1; }} />,
  );
  await click(button('Cancel'));
  expect(closed).toBe(1);
  act(() => root.unmount());

  mockState.servers = [{ id: 5, name: 'EC2', host: '203.0.113.10', port: 22, ssh_user: 'ubuntu',
    remote_dir: 'galleries/album2026', public_url: 'https://photos.example.com/',
    host_key: 'SHA256:abc', last_run: null }];
  const second = await mount(
    <ServersModal catalogId={7} deployment={DEPLOYMENT} visible onClose={() => { closed += 1; }} />,
  );
  await second.click(second.button('+ Add a server'));
  expect(second.text()).toContain('The server must already exist');
  await second.click(second.button('Cancel'));
  expect(closed).toBe(1);
  expect(second.text()).not.toContain('The server must already exist');
  expect(second.button('Publish')).toBeDefined();
  act(() => second.root.unmount());
});
