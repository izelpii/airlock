import React from 'react';
import { createRoot } from 'react-dom/client';
import { act } from 'react-dom/test-utils';
import ChangeDateTool, { buildDateChanges, dateSpan, describeSpan, MAX_FILES } from './ChangeDateTool';

// The Change date tool (saved-search-tools-spec.md section 5): which date and
// time each file gets, the card's disabled states, the confirmation and what
// is sent.

const mockPosts = [];
let mockPostResult;

global.IS_REACT_ACT_ENVIRONMENT = true;

// Plain functions, not jest.fn: CRA's resetMocks clears jest.fn implementations.
jest.mock('../services/api', () => ({
  photoAPI: {
    updateDates: (...args) => {
      mockPosts.push(args);
      return mockPostResult();
    },
  },
}));

const taken = new Map([
  [1, '2019-03-03T09:41:07-05:00'],
  [2, '2019-03-03T15:02:55-05:00'],
  [3, '2021-07-09T09:41:07-04:00'],
  [4, null],
]);

describe('buildDateChanges', () => {
  it('keeps each file\'s own time of day when no time is given', () => {
    const changes = buildDateChanges([1, 2, 3, 4], taken, '2019-06-01', '', 'America/New_York');
    expect(changes).toEqual([
      { file_ids: [1, 3], datetime: '2019-06-01T09:41:07', timezone: 'America/New_York' },
      { file_ids: [2], datetime: '2019-06-01T15:02:55', timezone: 'America/New_York' },
      { file_ids: [4], datetime: '2019-06-01T12:00:00', timezone: 'America/New_York' },
    ]);
  });

  it('gives every file the same instant when a time is given', () => {
    expect(buildDateChanges([1, 2], taken, '2019-06-01', '15:15', 'UTC')).toEqual([
      { file_ids: [1, 2], datetime: '2019-06-01T15:15:00', timezone: 'UTC' },
    ]);
    expect(buildDateChanges([1], taken, '2019-06-01', '15:15:05', 'UTC')[0].datetime)
      .toBe('2019-06-01T15:15:05');
  });
});

describe('dateSpan', () => {
  it('spans the selection\'s days, ignoring files without a date', () => {
    expect(describeSpan(dateSpan([1, 3, 4], taken))).toBe('Mar 3, 2019 – Jul 9, 2021');
    expect(describeSpan(dateSpan([1, 2], taken))).toBe('Mar 3, 2019');
    expect(dateSpan([4], taken)).toBeNull();
  });
});

describe('the card', () => {
  let container;
  let root;
  let started;

  const render = async (props = {}) => {
    started = [];
    await act(async () => {
      root.render(
        <ChangeDateTool catalogId={7} savedSearchId={3} selectedIds={[1, 2]} takenById={taken}
                        canEdit indexInPlace={false} busy={false}
                        onStarted={(...a) => started.push(a)} {...props} />,
      );
    });
  };
  const button = () => [...document.querySelectorAll('button')].find((b) => /^Change date/.test(b.textContent));
  const confirmButton = () => [...document.querySelectorAll('button')].find((b) => /^Change \d+ dates?$/.test(b.textContent.trim()));
  const setInput = async (id, value) => {
    const el = container.querySelector(`#${id}`);
    const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
    await act(async () => {
      setter.call(el, value);
      el.dispatchEvent(new Event('input', { bubbles: true }));
    });
  };

  beforeEach(() => {
    mockPosts.length = 0;
    mockPostResult = () => Promise.resolve({ data: { batch_id: 41, file_count: 2 } });
    container = document.createElement('div');
    document.body.appendChild(container);
    root = createRoot(container);
  });
  afterEach(() => {
    act(() => root.unmount());
    container.remove();
    document.body.innerHTML = '';
  });

  it('starts on the shared day and leaves mixed days empty', async () => {
    await render();
    expect(container.querySelector('#ss-date').value).toBe('2019-03-03');
    await render({ selectedIds: [1, 3] });
    expect(container.querySelector('#ss-date').value).toBe('');
    expect(button().disabled).toBe(true);                 // no date yet
  });

  it('is disabled with nothing selected, for a viewer, while busy, and over the cap', async () => {
    await render({ selectedIds: [] });
    expect(button().disabled).toBe(true);
    await render({ canEdit: false });
    expect(button().disabled).toBe(true);
    expect(container.textContent).toContain('Changing dates needs editor access.');
    await render({ busy: true });
    expect(button().disabled).toBe(true);
    expect(container.textContent).toContain('A date change is running');
    const many = Array.from({ length: MAX_FILES + 1 }, (_, i) => i + 10);
    await render({ selectedIds: many });
    expect(button().disabled).toBe(true);
    expect(container.textContent).toContain('At most 5,000 at a time');
  });

  it('asks before sending, then sends the saved search and each file\'s own time', async () => {
    await render();
    await setInput('ss-date', '2019-06-01');
    await act(async () => { button().click(); });
    expect(document.body.textContent).toContain('Change the date of 2 photos and videos?');
    expect(document.body.textContent).toContain('each keeping its own time of day');
    expect(document.body.textContent).toContain('Create Events then runs for the catalog');
    expect(mockPosts).toHaveLength(0);                    // nothing before the confirmation

    await act(async () => { confirmButton().click(); });
    expect(mockPosts).toHaveLength(1);
    const [catalogId, eventId, changes, rebuild, savedSearchId] = mockPosts[0];
    expect([catalogId, eventId, rebuild, savedSearchId]).toEqual([7, null, true, 3]);
    expect(changes.map((c) => c.datetime)).toEqual(['2019-06-01T09:41:07', '2019-06-01T15:02:55']);
    expect(started).toEqual([[41, 2]]);
  });

  it('says so in an index-in-place catalog, and when a time is set', async () => {
    await render({ indexInPlace: true });
    await setInput('ss-time', '15:15:00');
    await act(async () => { button().click(); });
    expect(document.body.textContent).toContain('the original stays where it is');
    expect(document.body.textContent).toContain('They will all be dated');
  });

  it('keeps the selection and shows why when the catalog is busy', async () => {
    mockPostResult = () => Promise.reject({
      response: { status: 409, data: { error: 'catalog_busy', message: 'An import is running in this catalog.' } },
    });
    await render();
    await act(async () => { button().click(); });
    await act(async () => { confirmButton().click(); });
    expect(started).toEqual([]);
    expect(container.textContent).toContain('An import is running in this catalog.');
    expect(container.querySelector('#ss-date').value).toBe('2019-03-03');
  });
});
