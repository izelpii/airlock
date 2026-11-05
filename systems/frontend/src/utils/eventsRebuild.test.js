import { describeEventsRebuild, describeRebuildOf } from './eventsRebuild';

it('says when tonight\'s rebuild runs, and how to run it now', () => {
  const text = describeEventsRebuild('scheduled', '2026-11-06T03:00:00-05:00');
  expect(text).toMatch(/^Events will be rebuilt tonight at \d{1,2}:00/);
  expect(text).toContain('run Create Events from the Workflows page');
});

it('says "shortly" when the request joined a run already due', () => {
  expect(describeEventsRebuild('queued', null)).toContain('shortly');
});

it('says nothing when there is no rebuild, or the problem is reported elsewhere', () => {
  expect(describeEventsRebuild(null, null)).toBe('');
  expect(describeEventsRebuild('no_create_events_workflow', null)).toBe('');
  expect(describeRebuildOf({ events_rebuild: null })).toBe('');
  expect(describeRebuildOf({ events_rebuild: { status: 'scheduled', run_at: null } }))
    .toMatch(/^Events will be rebuilt tonight\. /);
});
