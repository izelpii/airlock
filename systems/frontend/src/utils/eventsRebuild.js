// A date change queues Create Events for the night rather than running it at
// once (owner, 2026-11-05): the backend answers with
//   { status: 'scheduled', run_at: ISO }  - waiting for tonight's run
//   { status: 'queued',    run_at: null } - joined a run that is already due
// or with nothing when the catalog has no Create Events workflow.

/**
 * One sentence for the person who changed the date, or '' when there is
 * nothing to say.
 */
export function describeEventsRebuild(status, runAt) {
  if (status === 'scheduled') {
    const at = runAt ? new Date(runAt) : null;
    const when = at && !Number.isNaN(at.getTime())
      ? at.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
      : null;
    return `Events will be rebuilt tonight${when ? ` at ${when}` : ''}. `
      + 'To rebuild them now, run Create Events from the Workflows page.';
  }
  if (status === 'queued') {
    return 'Events will be rebuilt shortly: Create Events was already waiting to run.';
  }
  return '';
}

/** The same, from a single-photo PUT /photos/<id>/date answer. */
export function describeRebuildOf(response) {
  const r = response?.events_rebuild;
  return r ? describeEventsRebuild(r.status, r.run_at) : '';
}
