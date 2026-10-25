import React, { useState, useEffect, useRef } from 'react';
import { CButton, CBadge, CRow, CCol } from '@coreui/react';
import CIcon from '@coreui/icons-react';
import { cilPencil } from '@coreui/icons';
import { galleryThumbUrl } from '../config';
import useMarquee, { previewChecked } from '../hooks/useMarquee';
import {
  parseWallTime, formatDayTitle, formatDayShort, formatTime12, daysBetween,
} from '../utils/wallTime';

// ─── One date group in the event date editor ─────────────────────────────────
//
// mass-edit-event-photo-dates-spec.md sections 4.1-4.3. Laid out like
// ClusterCard on Identify Groups (components/FaceClusters.js), on purpose:
// there, faces judged to be one person are named in one action, minus the
// faces the reviewer unchecks. Here, photos that share a day are re-dated in
// one action, minus the photos the user unchecks. A user who has named face
// groups already knows this card.

// Dates the import had to guess: when the file reached the disk, or when the
// import ran. They look as trustworthy as any other date and are not, which is
// why a group holding them is flagged like a mixed face cluster.
export const GUESSED_SOURCES = new Set(['filesystem', 'system']);

const SOURCE_LABELS = {
  exif: 'from the camera',
  directory: 'from the folder',
  filename: 'from the file name',
  filesystem: 'from the file system',
  system: 'from the import',
  manual: 'set by hand',
};

// members_truncated's limit on Identify Groups. A card draws at most this many
// tiles; Apply still covers every member.
export const MAX_SHOWN = 200;

// A staged photo is about to become 'manual', so it no longer counts as
// guessed: the user has just said when it was taken.
export const isGuessed = (f) => !f.staged && GUESSED_SOURCES.has(f.source);

/**
 * The "Looks like <date>" hint for a group (section 4.3), or null.
 *
 * 1. The most common file-name date among the members, when the group is
 *    guessed or that date is not the group's day. The time comes with it only
 *    when every agreeing name carried the same time of day.
 * 2. Otherwise, for a guessed group, the nearest day held by a photo of this
 *    event whose date was not guessed.
 *
 * `day` is the group's "YYYY-MM-DD" (or null for undated photos), `members`
 * its photos and `eventFiles` every photo in the editor.
 */
export function computeSuggestion(day, members, eventFiles = []) {
  const guessed = members.some(isGuessed);

  const byDay = new Map();
  members.forEach((f) => {
    const p = parseWallTime(f.filename_datetime);
    if (!p) return;
    if (!byDay.has(p.day)) byDay.set(p.day, []);
    byDay.get(p.day).push({ f, p });
  });

  if (byDay.size > 0) {
    // Most common; a tie goes to the earlier date so the hint is stable.
    const [bestDay, agreeing] = [...byDay.entries()].sort(
      (a, b) => (b[1].length - a[1].length) || (a[0] < b[0] ? -1 : 1),
    )[0];
    if (!guessed && bestDay === day) return null;
    const times = new Set(agreeing.map(({ p }) => p.minuteKey.slice(11)));
    const time = agreeing.every(({ f }) => f.filename_has_time) && times.size === 1
      ? [...times][0]
      : '';
    return { date: bestDay, time, count: agreeing.length, from: 'filenames' };
  }

  if (!guessed) return null;

  // The nearest day among the event's trustworthy photos outside this group.
  let best = null;
  eventFiles.forEach((f) => {
    if (isGuessed(f) || !f.current || f.current.day === day) return;
    const dist = day ? Math.abs(daysBetween(day, f.current.day)) : 0;
    if (!best || dist < best.dist || (dist === best.dist && f.current.day < best.date)) {
      best = { date: f.current.day, dist };
    }
  });
  return best ? { date: best.date, time: '', count: null, from: 'event' } : null;
}

// The group's "where the dates came from", from the photos not yet changed.
function sourceLine(members) {
  const counts = new Map();
  members.forEach((f) => {
    if (f.staged) return;
    const label = SOURCE_LABELS[f.source] || 'from an unknown source';
    counts.set(label, (counts.get(label) || 0) + 1);
  });
  if (counts.size === 0) return 'changed here';
  const [top] = [...counts.entries()].sort((a, b) => b[1] - a[1])[0];
  return counts.size === 1 ? top : `mostly ${top}`;
}

const isPlaceholder = (f) => !f.is_video && !f.alternative_file_path && f.needsAlternative;

// One thumbnail. Every file is shown, including the ones EventDetail skips for
// lack of an alternative version: a file the user cannot see cannot be
// re-dated, so it gets a file-type placeholder instead of being left out.
const Tile = ({ file, catalogId, checked, onToggle }) => {
  const [broken, setBroken] = useState(false);
  const ext = (file.file_name || '').split('.').pop().toUpperCase();
  const showImage = !broken && !isPlaceholder(file);
  return (
    <div
      data-marquee-id={file.id}
      data-checked={checked ? 'true' : 'false'}
      onClick={() => onToggle(file.id)}
      title={file.file_name}
      style={{
        cursor: 'pointer', textAlign: 'center', position: 'relative',
        opacity: checked ? 1 : 0.35,
        border: `2px solid ${checked ? 'transparent' : '#dc3545'}`,
        borderRadius: 6, padding: 2,
      }}
    >
      {showImage ? (
        <img
          src={galleryThumbUrl(catalogId, file.id)}
          alt={file.file_name}
          draggable={false}
          loading="lazy"
          onError={() => setBroken(true)}
          style={{ width: 96, height: 96, objectFit: 'cover', borderRadius: 4, display: 'block' }}
        />
      ) : (
        <div
          data-testid="tile-placeholder"
          style={{
            width: 96, height: 96, borderRadius: 4, background: '#e9ecef', color: '#6c757d',
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: '0.8rem', fontWeight: 700,
          }}
        >
          {ext || 'FILE'}
        </div>
      )}
      {file.is_video && (
        <span style={{
          position: 'absolute', left: 6, bottom: 22, background: 'rgba(0,0,0,0.6)',
          color: '#fff', borderRadius: 3, fontSize: '0.6rem', padding: '0 4px',
        }}
        >
          ▶
        </span>
      )}
      {file.staged && (
        <span
          data-testid="staged-badge"
          title="Date changed here; not saved yet"
          style={{
            position: 'absolute', right: 6, top: 6, background: '#0d6efd', color: '#fff',
            borderRadius: '50%', width: 18, height: 18,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
          }}
        >
          <CIcon icon={cilPencil} style={{ width: 10, height: 10 }} />
        </span>
      )}
      <div style={{ fontSize: '0.65rem', color: '#888' }}>
        {file.current ? formatTime12(file.current) : '—'}
      </div>
    </div>
  );
};

const linkStyle = {
  background: 'none', border: 'none', padding: 0, color: '#0d6efd',
  textDecoration: 'underline', cursor: 'pointer', fontSize: '0.72rem',
};

const DateGroupCard = ({
  group, catalogId, eventFiles = [], onApply, onUndo, flashKey = 0, disabled = false,
}) => {
  const members = group.files;
  const [excluded, setExcluded] = useState(() => new Set());
  const [date, setDate] = useState('');
  const [time, setTime] = useState('');
  const [flashing, setFlashing] = useState(false);
  const rootRef = useRef(null);
  const facesRef = useRef(null);

  // A photo that arrives here from another card starts checked, like every
  // photo. A stale id in `excluded` for a photo that left is harmless.
  const isChecked = (id) => !excluded.has(id);

  const toggle = (id) => {
    setExcluded((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id); else next.add(id);
      return next;
    });
  };

  const marquee = useMarquee({
    containerRef: facesRef,
    onCommit: (ids, mode) => {
      setExcluded((prev) => {
        const next = new Set(prev);
        ids.forEach((raw) => {
          const id = Number(raw);
          const on = !next.has(id);
          const want = mode === 'check' ? true : mode === 'uncheck' ? false : !on;
          if (want) next.delete(id); else next.add(id);
        });
        return next;
      });
    },
  });

  // Applying elsewhere sent photos here: bring the card into view and flash
  // it, so the user sees where they went (section 4.3).
  useEffect(() => {
    if (!flashKey) return undefined;
    const el = rootRef.current;
    if (el && typeof el.scrollIntoView === 'function') {
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
    }
    setFlashing(true);
    const t = setTimeout(() => setFlashing(false), 1200);
    return () => clearTimeout(t);
  }, [flashKey]);

  const willApply = members.filter((f) => isChecked(f.id)).length;
  const guessedCount = members.filter(isGuessed).length;
  const suggestion = computeSuggestion(group.day, members, eventFiles);

  const sorted = members.filter((f) => f.current).map((f) => f.current.sortKey).sort();
  const first = sorted.length ? formatTime12(sorted[0]) : null;
  const last = sorted.length ? formatTime12(sorted[sorted.length - 1]) : null;
  const timeRange = first === last ? first : `${first} – ${last}`;

  // "44 changed from Oct 2, 2026 · Undo", one line per day they came from.
  const changedFrom = new Map();
  members.forEach((f) => {
    if (!f.staged) return;
    const from = f.original ? f.original.day : null;
    if (!changedFrom.has(from)) changedFrom.set(from, []);
    changedFrom.get(from).push(f.id);
  });

  const apply = () => {
    if (!date || willApply === 0) return;
    const ids = members.filter((f) => isChecked(f.id)).map((f) => f.id);
    // The brief's default: a date without a time is noon.
    onApply(ids, `${date}T${time || '12:00'}`);
    // What is left in this card is a fresh group: every photo starts checked.
    setExcluded(new Set());
    setDate('');
    setTime('');
  };

  const shown = members.slice(0, MAX_SHOWN);

  return (
    <div
      ref={rootRef}
      data-testid="date-group"
      data-day={group.day || 'none'}
      data-guessed={guessedCount > 0 ? 'true' : 'false'}
      style={{
        border: `1px solid ${guessedCount > 0 ? '#e6a817' : '#dee2e6'}`,
        borderRadius: 8, padding: 12, marginBottom: 12,
        background: flashing ? '#eef5ff' : '#fff',
        boxShadow: flashing ? '0 0 0 3px rgba(13,110,253,0.35)' : 'none',
        transition: 'background 0.4s, box-shadow 0.4s',
      }}
    >
      {/* Two columns, from .cluster-info-col / .cluster-faces-col in App.css,
          reused rather than copied (section 4.1). xs={12} stacks them on a
          narrow screen. */}
      <CRow className="g-3" style={{ alignItems: 'flex-start' }}>
        <CCol xs={12} className="cluster-info-col">
          <CRow className="g-2">
            {/* What the group IS on the left two thirds ... */}
            <CCol xs={12} sm={8}>
              <div>
                <strong style={{ fontSize: '0.85rem' }} data-testid="group-title">
                  {group.day ? formatDayTitle(group.day) : 'No date'}
                </strong>
                <CBadge color="secondary" className="ms-2">
                  {members.length} photo{members.length === 1 ? '' : 's'}
                </CBadge>
              </div>
              <div style={{ fontSize: '0.75rem', color: '#888', marginTop: 2 }}>
                {timeRange && <>{timeRange} · </>}{sourceLine(members)}
              </div>

              {[...changedFrom.entries()].map(([from, ids]) => (
                <div key={from || 'none'} style={{ fontSize: '0.75rem', color: '#0d6efd', marginTop: 2 }}
                     data-testid="changed-from">
                  {ids.length} changed from {from ? formatDayShort(from) : 'no date'}
                  {' · '}
                  <button type="button" style={linkStyle} disabled={disabled}
                          onClick={() => onUndo(ids)}>
                    Undo
                  </button>
                </div>
              ))}

              {/* Offered, not applied, as on ClusterCard: clicking fills the
                  inputs and the user still presses Apply. */}
              {suggestion && (
                <div style={{ fontSize: '0.78rem', marginTop: 6 }}>
                  <span style={{ color: '#888' }}>Looks like </span>
                  <button
                    type="button"
                    data-testid="suggestion"
                    onClick={() => { setDate(suggestion.date); setTime(suggestion.time); }}
                    disabled={disabled}
                    title="Use this suggestion"
                    style={{
                      background: 'none', border: 'none', padding: 0, color: '#0d6efd',
                      fontWeight: 600, textDecoration: 'underline', cursor: 'pointer',
                      textAlign: 'left',
                    }}
                  >
                    {formatDayShort(suggestion.date)}
                    {suggestion.time ? ` ${formatTime12(`${suggestion.date}T${suggestion.time}`)}` : ''}
                  </button>
                  <span style={{ color: '#888' }}>
                    {' '}
                    {suggestion.from === 'filenames'
                      ? `(from ${suggestion.count} file name${suggestion.count === 1 ? '' : 's'})`
                      : '(from the other photos in this event)'}
                  </span>
                </div>
              )}
            </CCol>

            {/* ... and what you DO with it on the right third. */}
            <CCol xs={12} sm={4}>
              <div style={{ fontSize: '0.72rem', color: '#888' }}>Set all to…</div>
              <input
                type="date" required className="form-control form-control-sm"
                aria-label="New date" data-testid="apply-date"
                value={date} onChange={(e) => setDate(e.target.value)} disabled={disabled}
              />
              <input
                type="time" className="form-control form-control-sm mt-1"
                aria-label="New time (optional)" data-testid="apply-time"
                placeholder="12:00 PM if empty" title="Optional. 12:00 PM if empty."
                value={time} onChange={(e) => setTime(e.target.value)} disabled={disabled}
              />
              {!time && (
                <div style={{ fontSize: '0.65rem', color: '#aaa' }}>12:00 PM if empty</div>
              )}
              <CButton
                size="sm" color="primary" className="mt-2 w-100" data-testid="apply"
                disabled={disabled || !date || willApply === 0}
                onClick={apply}
              >
                Apply {willApply}
              </CButton>
            </CCol>
          </CRow>

          {guessedCount > 0 && (
            <div style={{ fontSize: '0.75rem', color: '#a97a00', marginTop: 8 }} data-testid="guessed-note">
              {guessedCount === members.length
                ? 'These dates were guessed'
                : `${guessedCount} of ${members.length} dates were guessed`}
              {' '}by the import from the file system. They are probably when the
              files were copied, not when the photos were taken.
            </div>
          )}
        </CCol>

        <CCol xs={12} className="cluster-faces-col">
          <div style={{ fontSize: '0.72rem', marginBottom: 4 }}>
            <button type="button" style={linkStyle} onClick={() => setExcluded(new Set())}>All</button>
            {' / '}
            <button
              type="button" style={linkStyle}
              onClick={() => setExcluded(new Set(members.map((f) => f.id)))}
            >
              None
            </button>
          </div>
          {/* The marquee's container. Relative, so its rectangle can be drawn
              in the container's coordinates; no text selection, because a drag
              here is always a selection of photos. */}
          <div
            ref={facesRef}
            data-testid="group-thumbs"
            onPointerDown={marquee.onPointerDown}
            style={{
              display: 'flex', flexWrap: 'wrap', gap: 8,
              position: 'relative', userSelect: 'none', WebkitUserSelect: 'none',
            }}
          >
            {shown.map((f) => (
              <Tile
                key={f.id}
                file={f}
                catalogId={catalogId}
                checked={previewChecked(f.id, isChecked(f.id), marquee.preview)}
                onToggle={toggle}
              />
            ))}
            {marquee.box && (
              <div
                data-testid="marquee-box"
                style={{
                  position: 'absolute', left: marquee.box.left, top: marquee.box.top,
                  width: marquee.box.width, height: marquee.box.height,
                  border: '1px solid #0d6efd', background: 'rgba(13,110,253,0.12)',
                  pointerEvents: 'none',
                }}
              />
            )}
          </div>

          {members.length > MAX_SHOWN && (
            <div style={{ fontSize: '0.72rem', color: '#888', marginTop: 4 }}>
              Showing {MAX_SHOWN} of {members.length}. Applying covers all
              {' '}{members.length}, minus anything unchecked above.
            </div>
          )}
        </CCol>
      </CRow>
    </div>
  );
};

export default DateGroupCard;
