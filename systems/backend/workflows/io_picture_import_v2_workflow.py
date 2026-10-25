#!/usr/bin/env python3
"""
Streaming picture import - the second ingestion script.

Same job, same requirements, same database as io_picture_import_workflow.py:
the dates are decided by the same code from the same metadata, duplicates by
the same SHA-256, and every row it writes has the same shape. What changes is
how the work is scheduled, which is where the time went
(documentation/features/ingestion-architecture.md):

1. Storage-aware reading (§5.1). The source is classified - spinning disk,
   SSD, network, unknown - and read with as many streams as suits it: ONE or
   two on a spinning disk, where eight interleaved streams made the head seek
   on every request. The count then tunes itself to measured throughput.
2. One read per file (§5.2). A single sequential pass hashes the file and
   writes its copy into the catalog. The metadata that decides the date is read
   straight afterwards, from the same path, while its bytes are in the page
   cache - so exiftool and PIL see exactly what they always saw, at no disk cost.
3. Duplicates without a second read (§5.3). A file whose size matches nothing
   in the catalog cannot be a duplicate of it, so it is copied while hashed. One
   whose size does match is hashed first and copied only when new, so a
   duplicate is never written.
4. No barriers (§5.4). Each file is committed as soon as it is known -
   duplicate check, final name, rename out of staging, database rows - so photos
   appear during the import. Render runs alongside, in the background.
5. Render once (§5.5). Every size from one decode; RAW files from their
   embedded preview when it is large enough (lib/render_once.py).

It records its progress in the same five import_phases rows, so the Import
Progress page needs no change; here they run at the same time rather than one
after another:

    scan     walk and stat, as before
    probe    read: hash, copy, metadata, date
    resolve  duplicate decisions, file by file
    place    commits
    render   derivatives, in the background

Selected per workflow: a workflow whose python_script is this file runs it.
"""

import glob
import os
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, FIRST_COMPLETED, wait

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import io_picture_import_workflow as v1                       # noqa: E402
from io_picture_import_workflow import (                      # noqa: E402
    _ImportRun, _PLACED_STATES, PHASES, render_video_task, _creation_from_ts,
    _thread_exiftool, _thread_reader, _close_thread_resources, _batch_match_keys,
    _batch_exiftool_metadata, _mark_running_phases, generate_new_filename,
    extract_exif_data, resolve_master_datetime, is_image_file, is_video_file,
    is_raw_file, find_groups, record_stopped, stop_message,
)
from catalog import Catalog                                   # noqa: E402
from lib.import_pipeline import Progress, now_iso             # noqa: E402
from media_types import sniff                                 # noqa: E402
from lib import storage_profile as sp                         # noqa: E402
from lib.ingest_io import hash_file, hash_and_copy            # noqa: E402
from lib.render_once import render_image_once                 # noqa: E402
from lib.utilities import _cache_dir_for                      # noqa: E402
import json                                                   # noqa: E402


# How often the videos being converted are reported (_StreamRun._report_videos).
VIDEO_REPORT_SECONDS = 5.0


def render_image_task_v2(task):
    """Module-level, so a process pool can pickle it (IMPORT_RENDER_EXECUTOR=process)."""
    return render_image_once(task, _creation_from_ts)


class _AllPhasesProgress(Progress):
    """
    The same counters, but every RUNNING phase is written on each flush: here
    probe, resolve, place and render advance together, where the phased import
    only ever had one current phase.
    """

    def write_snapshot(self, conn):
        self._last_flush = time.monotonic()
        self.dirty = False
        if self.execution_id is None:
            return
        for phase in PHASES:
            if self.phases[phase]['status'] == 'running':
                self.current = phase
                self._update_rate()
                self.write_phase(conn, phase)
        conn.execute('UPDATE workflow_executions SET processed_files=?, total_files=? '
                     'WHERE id=?',
                     (self.processed_files(), self.total_files, self.execution_id))


def _env_int(name, default):
    try:
        v = int(os.environ.get(name, '') or 0)
    except ValueError:
        return default
    return v if v > 0 else default


class _StreamRun(_ImportRun):
    """The phased run's writer, recording, grouping and resume - streamed."""

    image_render_task = staticmethod(render_image_task_v2)

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.progress = _AllPhasesProgress(self.eid, self.settings['progress_flush_seconds'],
                                           self.progress.progress_callback)
        self.source_profile = sp.classify(self.source_dir)
        # Where the files actually land: the originals folders, which may be
        # mounts on another disk than the catalog folder itself. On oldserver the
        # catalog is on the SSD array but Images/Videos/Others are bind mounts
        # of the USB drive the source is on, and comparing against the catalog
        # folder missed that every byte was read and written on one disk.
        originals = [os.path.join(self.target_dir, d) for d in self.ORIGINALS_DIRS
                     if os.path.isdir(os.path.join(self.target_dir, d))]
        self.target_profile = sp.classify(originals[0] if originals else self.target_dir)
        kind = self.source_profile['kind']
        start, maximum = sp.READERS[kind]
        fixed = os.environ.get('IMPORT_READERS')
        if fixed:
            start = maximum = _env_int('IMPORT_READERS', start)
        self.limiter = sp.AdaptiveLimiter(start, maximum, fixed=bool(fixed))
        self.chunk = _env_int('IMPORT_READ_CHUNK_MB', sp.CHUNK[kind] >> 20) << 20
        # Source and catalog on one spinning disk: long read/write bursts.
        same = any(sp.same_device(self.source_dir, d) for d in originals or [self.target_dir])
        self.same_spindle = same and kind == sp.HDD
        self.burst = sp.SAME_DISK_BURST if self.same_spindle else None
        # CPU-side workers: metadata after each read, placement of copies.
        self.meta_workers = _env_int('IMPORT_META_CONCURRENCY', min(4, os.cpu_count() or 2))
        # Video previews wait until every file is in (IMPORT_DEFER_VIDEO_RENDER,
        # on by default). Transcoding is the slowest thing an import does and
        # takes every core it is given; done alongside, it halved how fast files
        # were moved on oldserver. Photos still render as they arrive.
        self.defer_videos = bool(_env_int('IMPORT_DEFER_VIDEO_RENDER', 1))
        # Every photo's preview before any video's. One ffmpeg takes most of
        # the cores; interleaved, it starved the photo lane, and the catalog
        # stayed unbrowsable for days behind a terabyte of transcoding (oldserver,
        # 2026-10-03). Videos wait until the last photo is done.
        # IMPORT_VIDEOS_LAST=0 restores the interleaving.
        self.videos_last = bool(_env_int('IMPORT_VIDEOS_LAST', 1))

        self._lock = threading.Lock()
        self._sizes_seen = set()       # sizes this run has started to read
        self._hashed = {}              # sha -> smallest source path hashed so far
        self._winners = {}             # sha -> item id, the first of each content
        self._render_q = queue.Queue()
        self._ingest_done = threading.Event()
        # Videos being converted right now, for the Import Progress page: one
        # can take ten minutes without the render counter moving (homeserver,
        # 2026-10-12), which read as a hang.
        self._video_lock = threading.Lock()
        self._videos_now = {}          # item id -> (name, path, started, partial glob)
        self._videos_reported_at = 0.0
        self._videos_reported_any = False

    # ── the whole run ────────────────────────────────────────────────────────

    def run(self, files, phases):
        self.open()
        print(f"Source: {self.source_profile['kind']} ({self.source_profile['fstype']}); "
              f"catalog: {self.target_profile['kind']}"
              + ('; same spinning disk - reading in 64 MB bursts' if self.same_spindle else '')
              + f"; readers {self.limiter.limit} (max {self.limiter.maximum}"
              + (', fixed' if self.limiter.fixed else ', adaptive') + ')'
              + f'; metadata workers {self.meta_workers}')
        if 'scan' in phases:
            self.phase_scan(files)
        if self.stopped:
            self.mark_remaining('stopped')
            return self.summary()
        if set(phases) - {'scan'} <= {'render'} and 'render' in phases:
            # A re-render of what is already placed (--phases render).
            self.phase_render()
        elif set(phases) - {'scan'}:
            self.phase_stream()
        self.mark_remaining('stopped' if self.stopped else 'skipped')
        # UNCONDITIONAL, and that is the fix. This used to be skipped when the
        # run was stopped, on the reasoning that a stopped run is the one most
        # likely to be resumed and should keep its cache. That is true of its
        # UNPLACED items - and they are not what this touches. A placed item's
        # metadata is already in exif_data, its file is in the catalog, and
        # _adopt_probe refuses to read it; keeping it only meant that every
        # interrupted import left its whole metadata copy behind for good.
        #
        # Most of the work is already done by then: _record_placed clears each
        # item as it lands. This is the backstop for rows placed before that
        # existed, and for any path that reaches a placed state another way.
        if self.eid is not None:
            def _trim(conn):
                conn.execute(
                    'UPDATE import_items SET metadata_json=NULL, match_keys_json=NULL '
                    f"WHERE execution_id=? AND state IN ({','.join('?' * len(_PLACED_STATES))})",
                    (self.eid_key, *_PLACED_STATES))
            self.writer.call(_trim)
        if self.limiter.history:
            print('Read throughput (readers, MB/s) per window: '
                  + ', '.join(f'{n}:{r}' for n, r in self.limiter.history[-12:]))
        return self.summary()

    # ── ingest: read, describe, decide, commit ───────────────────────────────

    def phase_stream(self):
        # Source-path order: a folder's files stay together (what a disk wants),
        # and committing in this order makes the FIRST copy of any content the
        # one with the smallest path - the phased import's duplicate rule.
        rows = [dict(r) for r in self.read.execute(
            'SELECT id, source_path, size, mtime_ns, kind FROM import_items '
            "WHERE execution_id=? AND state='discovered'", (self.eid_key,))]
        rows.sort(key=lambda r: (r['source_path'], r['id']))
        total = len(rows)
        self._catalog_sizes = {r[0] for r in self.read.execute(
            'SELECT DISTINCT file_size FROM file_contents WHERE file_size IS NOT NULL')}
        pool_size = self.limiter.maximum + self.meta_workers
        for phase, workers in (('probe', self.limiter.limit), ('resolve', 1),
                               ('place', 1)):
            self.begin(phase, total=total, workers=workers)
        self.progress.phases['render']['total'] = 0
        self.begin('render', total=0,
                   workers=self.settings['render_workers'] + self.settings['video_workers'])

        # Folders finish in scan order; when one does, its pairs are known and
        # its photos can go to render.
        remaining = {}
        for r in rows:
            folder = os.path.dirname(r['source_path'])
            remaining[folder] = remaining.get(folder, 0) + 1
        folder_ids = {}

        renderer = threading.Thread(target=self._render_lane, name='import-render',
                                    daemon=True)
        renderer.start()
        self._enqueue_leftovers()

        stopped = False
        with ThreadPoolExecutor(max_workers=pool_size, thread_name_prefix='ingest') as pool:
            # Files are read in parallel but COMMITTED in order: results wait in
            # `ready` until every earlier file is committed. The look-ahead is
            # bounded on what is being read, not on what is waiting, so one big
            # video does not leave the disk idle behind it.
            in_flight, ready = {}, {}
            next_submit = next_commit = 0
            while True:
                while (not stopped and next_submit < total
                       and len(in_flight) < pool_size * 2 and len(ready) < 5000):
                    in_flight[pool.submit(self._ingest_one, rows[next_submit])] = next_submit
                    next_submit += 1
                if not in_flight and not ready:
                    break
                if in_flight:
                    done, _ = wait(in_flight, return_when=FIRST_COMPLETED)
                    for fut in done:
                        ready[in_flight.pop(fut)] = fut.result()
                while next_commit in ready:
                    item = rows[next_commit]
                    self._commit(item, ready.pop(next_commit))
                    next_commit += 1
                    folder = os.path.dirname(item['source_path'])
                    folder_ids.setdefault(folder, []).append(item['id'])
                    remaining[folder] -= 1
                    if remaining[folder] == 0:
                        self._folder_done(folder_ids.pop(folder))
                if not stopped and self.stop_check():
                    stopped = True
        # Folders cut short by a stop still render what they placed.
        for ids in folder_ids.values():
            self._folder_done(ids)
        self.writer.flush()
        _close_thread_resources()

        if not stopped:
            revived = self._revive_orphaned_duplicates()
            if revived:
                self.progress.phases['place']['total'] += len(revived)
                self._place_ids(revived, 1)
                self._folder_done(revived)
            self._write_duplicate_ops(catalogue=False)
        n_groups = self._plan_groups_placed()
        self._write_groups()
        self.writer.flush()
        if not stopped:
            self._clean_staging()
        for phase in ('probe', 'resolve', 'place'):
            self.finish(phase, 'stopped' if stopped else 'completed')

        # Render keeps going until its queue is empty (or the stop reaches it).
        self._ingest_done.set()
        self._render_q.put(None)
        renderer.join()
        self.writer.flush()
        self.finish('render', 'stopped' if self.stopped else 'completed')
        if n_groups:
            print(f'Grouping: {n_groups} image(s) arrive as two files each.')

    def _ingest_one(self, item):
        """Worker: the one read of a file, then its metadata. Never raises."""
        path = item['source_path']
        try:
            reader = _thread_reader(self.db_path)
            adopted = None
            try:
                adopted = self._adopt(reader, item)
            except Exception:
                adopted = None
            staged, sha, nbytes, dup = None, None, 0, None
            if adopted:
                sha = adopted['sha256']
                staged = self._usable_staged(adopted.get('staged_path'), item['size'])
            if not staged and not (adopted and not self.staging):
                sha, staged, nbytes, dup = self._read(item, known_sha=sha)
            result = {'ok': True, 'sha': sha, 'staged_path': staged, 'bytes': nbytes,
                      'adopted': bool(adopted), 'catalog_duplicate': dup}
            # What the bytes say, when the name lies (media_types.sniff). Read
            # now, while the file is still in the page cache.
            found, real_ext = sniff(path)
            if found == 'appledouble':
                result['skip_reason'] = 'macOS metadata (AppleDouble), not a photo or video'
                return result
            if found == 'video' and item['kind'] != 'video':
                # A video named like a photo - a Live Photo clip exported as
                # .JPG. Read, named and filed as the video it is.
                item = dict(item, kind='video')
                result.update(kind='video', rename_ext=real_ext)
            if adopted and not result.get('kind'):
                result.update(metadata_json=adopted['metadata_json'],
                              match_keys_json=adopted['match_keys_json'])
                exif = json.loads(adopted['metadata_json']) if adopted['metadata_json'] else None
            else:
                exif, result['metadata_json'], result['match_keys_json'] = \
                    self._describe(item)
            new_filename, creation_ts, date_source, creation_obj = generate_new_filename(
                path, sha, exif, self.source_dir)
            if result.get('rename_ext'):
                new_filename = os.path.splitext(new_filename)[0] + result['rename_ext']
            master_dt, master_src = resolve_master_datetime(
                path, exif, source_dir=self.source_dir, date_source_hint=date_source,
                precomputed_datetime=creation_obj)
            result.update(new_filename=new_filename, creation_ts=creation_ts,
                          date_source=date_source, master_datetime=master_dt,
                          master_dt_source=master_src)
            return result
        except Exception as exc:
            return {'ok': False, 'error': str(exc), 'source_path': path}

    def _read(self, item, known_sha=None):
        """
        The read (§5.3). Returns (sha, staged_rel or None, bytes read,
        catalog duplicate row or None).
        """
        path, size = item['source_path'], item['size']
        with self._lock:
            size_known = size in self._catalog_sizes or size in self._sizes_seen
            self._sizes_seen.add(size)
        staged_abs = None
        if self.staging:
            ext = os.path.splitext(path)[1].lower()
            staged_abs = os.path.join(self._staging_dir_for(path), f"{item['id']}{ext}")

        def _timed(fn, *a):
            self.limiter.acquire()
            try:
                return fn(*a)
            finally:
                self.limiter.release(size or 0)

        if not self.staging:
            # Reference mode (nothing is copied) or a remote catalog (place
            # copies with the catalog's strategy): the read is the hash.
            return _timed(hash_file, path, self.chunk), None, size or 0, None
        if known_sha:
            # Hashed by an earlier run of this same, unchanged file (adopted:
            # same path, size and mtime). Asked the same two questions as a
            # freshly hashed file before anything is copied - it used to go
            # straight to hash_and_copy, so every duplicate a stopped or
            # finished run had found was read and copied again on each re-run,
            # then discarded at commit, contrary to "a duplicate is never
            # written" above. Nothing is read here, so no bytes are counted.
            dup = self._catalog_row(known_sha)
            if dup:
                return known_sha, None, 0, dup
            if self._claim(known_sha, path):
                return known_sha, None, 0, None
        if size_known and not known_sha:
            # Could be a duplicate: hash first, write only if the content is new.
            sha = _timed(hash_file, path, self.chunk)
            dup = self._catalog_row(sha)
            if dup:
                return sha, None, size or 0, dup
            if self._claim(sha, path):
                # An earlier path has this content and will be placed first:
                # this one is a duplicate within the run. Nothing to copy.
                return sha, None, size or 0, None
        try:
            sha = _timed(hash_and_copy, path, staged_abs, self.chunk, self.burst)
        except OSError as exc:
            if not os.path.exists(path):
                raise
            # The source read, the copy did not (a full catalog disk, say): hash
            # only; place copies it the old way.
            print(f'Could not stage {os.path.basename(path)} ({exc}); '
                  f'it will be copied at place instead')
            return _timed(hash_file, path, self.chunk), None, size or 0, None
        self._claim(sha, path)
        return sha, os.path.relpath(staged_abs, self.target_dir), (size or 0) * (
            2 if size_known else 1), None

    def _claim(self, sha, path):
        """Record that `path` has this content; True when a smaller path already did."""
        with self._lock:
            first = self._hashed.get(sha)
            if first is None or path < first:
                self._hashed[sha] = path
                return False
            return first < path

    def _catalog_row(self, sha):
        if not sha:
            return None
        row = _thread_reader(self.db_path).execute(
            'SELECT id, file_name, removed_at FROM file_contents WHERE sha256_hash=? '
            'ORDER BY id LIMIT 1',
            (sha,)).fetchone()
        return dict(row) if row else None

    def _describe(self, item):
        """
        Metadata, exactly as the phased import reads it - the same helpers on the
        same path - but now straight after the read, from the page cache.
        Returns (exif, metadata_json, match_keys_json).
        """
        path = item['source_path']
        et = _thread_exiftool()
        # Videos too: a Live Photo's clip is matched to its still by the
        # ContentIdentifier both carry (lib/grouping.py, find_live_photos).
        mk = (_batch_match_keys(et, [path]).get(path)
              if is_image_file(path) or is_video_file(path) else None)
        if item['kind'] in ('raw', 'video'):
            exif = _batch_exiftool_metadata(et, [path]).get(path)
        elif item['kind'] == 'image':
            exif = extract_exif_data(path)
        else:
            exif = None
        metadata_json = json.dumps(exif, default=str) if exif else None
        exif = json.loads(metadata_json) if metadata_json else None
        return exif, metadata_json, (json.dumps(mk) if mk is not None else None)

    def _commit(self, item, r):
        """Orchestrator thread: record the probe, decide, place - in source-path order."""
        if not r['ok']:
            print(f"Error processing {r['source_path']}: {r['error']}")

            def _fail(conn, r=r):
                conn.execute("UPDATE import_items SET state='probe_failed', phase_error=?, "
                             'updated_at=CURRENT_TIMESTAMP WHERE id=?', (r['error'], item['id']))
                self._file_op(conn, None, 'index' if self.reference_mode
                              else 'move' if self.move_mode else 'copy',
                              r['source_path'], '', 'failed', r['error'])
                self.progress.bump('probe', failed=1)
            self.writer.submit(_fail)
            return

        sha = r['sha']
        staged = r['staged_path']

        if r.get('skip_reason'):
            # Not a photo or video whatever its name says (media_types.sniff):
            # left where it is, recorded like any ignored file.
            self._discard(staged)
            print(f"Ignored ({r['skip_reason']}): {os.path.basename(item['source_path'])}")

            def _ignored(conn, r=r):
                conn.execute("UPDATE import_items SET state='ignored', kind='ignored', "
                             'staged_path=NULL, phase_error=?, updated_at=CURRENT_TIMESTAMP '
                             'WHERE id=?', (r['skip_reason'], item['id']))
                if self.move_mode:
                    conn.execute(
                        'INSERT INTO file_operations (execution_id, file_id, '
                        'operation_type, source_path, target_path, file_size, status, '
                        'details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        self._left_in_place_op(item['source_path'], r['skip_reason'],
                                               item['size']))
                self.progress.bump('probe', skipped=1)
            self.writer.submit(_ignored)
            return

        def _probed(conn, r=r):
            conn.execute(
                '''UPDATE import_items SET state='probed', probed=1, sha256=?, staged_path=?,
                          metadata_json=?, match_keys_json=?, new_filename=?, creation_ts=?,
                          date_source=?, master_datetime=?, master_dt_source=?,
                          kind=COALESCE(?, kind),
                          updated_at=CURRENT_TIMESTAMP WHERE id=?''',
                (sha, staged, r['metadata_json'], r['match_keys_json'], r['new_filename'],
                 r['creation_ts'], r['date_source'], r['master_datetime'],
                 r['master_dt_source'], r.get('kind'), item['id']))
            self.progress.bump('probe', done=1, bytes_done=r['bytes'],
                               adopted=1 if r['adopted'] else 0)
        self.writer.submit(_probed)

        # Duplicates: commits come in source-path order, so the first of each
        # content is the smallest path - the phased import's rule exactly.
        winner = self._winners.get(sha) if sha else None
        dup = r['catalog_duplicate'] or (None if winner else self._catalog_row(sha))
        if winner or dup:
            self._discard(staged)
            row = {'source_path': item['source_path'], 'creation_ts': r['creation_ts'],
                   'new_filename': r['new_filename']}
            # Move mode sets a catalogue duplicate aside in DUPS/ now. One
            # repeated within this run waits for the end of the run
            # (_write_duplicate_ops), in case the copy it repeats fails to place
            # and it has to be imported instead.
            moved_op = (self._duplicate_moved_op(
                dup['id'], item['source_path'], sha, dup['file_name'], dup.get('removed_at'),
                item['size']) if self.move_mode and dup and not winner else None)

            def _duplicate(conn, winner=winner, dup=dup, row=row, moved_op=moved_op):
                if winner:
                    conn.execute("UPDATE import_items SET state='duplicate', duplicate_item=?, "
                                 'staged_path=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                                 (winner, item['id']))
                    self.progress.bump('resolve', skipped=1, in_run_duplicates=1)
                else:
                    conn.execute("UPDATE import_items SET state='duplicate', duplicate_of=?, "
                                 'staged_path=NULL, updated_at=CURRENT_TIMESTAMP WHERE id=?',
                                 (dup['id'], item['id']))
                    if moved_op:
                        conn.execute(
                            'INSERT INTO file_operations (execution_id, file_id, '
                            'operation_type, source_path, target_path, file_size, status, '
                            'details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)', moved_op)
                        self.progress.bump('resolve', skipped=1, catalogue_duplicates=1)
                        return
                    self._file_op(conn, dup['id'], 'index' if self.reference_mode else 'skip',
                                  item['source_path'], self._would_be_target(row), 'skipped',
                                  f'Duplicate hash: {sha}' + (
                                      f"; already indexed as file_contents id {dup['id']}"
                                      if self.reference_mode else ''))
                    self.progress.bump('resolve', skipped=1, catalogue_duplicates=1)
            self.writer.submit(_duplicate)
            if not moved_op:
                print(f"Skipping duplicate file: {os.path.basename(item['source_path'])}"
                      + (f" (same as {dup['file_name']})" if dup else ''))
            return

        if sha:
            self._winners[sha] = item['id']
        with self._lock:
            self._catalog_sizes.add(item['size'])

        def _new(conn):
            conn.execute("UPDATE import_items SET state='to_place' WHERE id=?", (item['id'],))
            self.progress.bump('resolve', done=1)
        self.writer.submit(_new)

        placed = self._place_one({
            'id': item['id'], 'source_path': item['source_path'], 'size': item['size'],
            'mtime_ns': item.get('mtime_ns'), 'sha256': sha, 'staged_path': staged, 'metadata_json': r['metadata_json'],
            'new_filename': r['new_filename'], 'creation_ts': r['creation_ts'],
            'date_source': r['date_source'], 'master_datetime': r['master_datetime'],
            'master_dt_source': r['master_dt_source']})
        self._on_place(placed)

    def _discard(self, staged_rel):
        if staged_rel:
            try:
                os.remove(os.path.join(self.target_dir, staged_rel))
            except OSError:
                pass

    # ── pairs and render ─────────────────────────────────────────────────────

    def _folder_done(self, ids):
        """
        A folder is fully read: its RAW+JPEG pairs and Live Photos are known
        (they sit side by side), so its photos can go to render, secondaries
        excepted - a RAW behind its JPG, a Live Photo's clip behind its still.
        The definitive grouping of the whole run is recorded at the end.
        """
        if not ids:
            return
        self.writer.flush()
        marks = ','.join('?' * len(ids))
        rows = [dict(r) for r in self.read.execute(
            f'SELECT id, source_path, match_keys_json, kind, file_id, target_path, '
            f'creation_ts, derivative_stem FROM import_items '
            f"WHERE id IN ({marks}) AND state='placed'", ids)]
        if not rows:
            return
        secondaries = set()
        members = [r for r in rows if r['kind'] in ('image', 'raw', 'video')]
        if len(members) > 1:
            try:
                keys = {r['source_path']: (json.loads(r['match_keys_json'])
                                           if r['match_keys_json'] else {}) for r in members}
                for g in find_groups([r['source_path'] for r in members], keys,
                                     is_raw_file, is_image_file, log=lambda *_: None,
                                     is_video=is_video_file):
                    secondaries.add(g['secondary'])
            except Exception as exc:
                print(f'Grouping skipped for a folder ({exc}); rendering every file.')
        tasks, skip = [], []
        for r in rows:
            tp = r['target_path'] or ''
            if r['source_path'] in secondaries or not r['file_id'] or not (
                    is_image_file(tp) or is_video_file(tp)):
                skip.append(r['id'])
            else:
                tasks.append(r)
        if skip:
            def _skip(conn, skip=skip):
                conn.executemany("UPDATE import_items SET state='render_skipped', "
                                 'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                                 [(i,) for i in skip])
                self.progress.bump('render', skipped=len(skip))
            self.progress.phases['render']['total'] += len(skip)
            self.writer.submit(_skip)
        if tasks:
            self.progress.phases['render']['total'] += len(tasks)
            for task in self._render_tasks(tasks):
                self._render_q.put(task)

    def _enqueue_leftovers(self):
        """Photos an earlier, interrupted run placed but never rendered."""
        finished = self._finished_import_executions()
        if not finished:
            return
        marks = ','.join('?' * len(finished))
        rows = [dict(r) for r in self.read.execute(
            f"SELECT id, file_id, target_path, group_role, creation_ts, derivative_stem "
            f"FROM import_items WHERE state='placed' AND execution_id IN ({marks}) "
            f'ORDER BY master_datetime DESC, id', finished)]
        todo = [r for r in rows if r['group_role'] != 'secondary' and r['file_id']
                and (is_image_file(r['target_path'] or '') or is_video_file(r['target_path'] or ''))]
        if todo:
            print(f'Rendering {len(todo)} file(s) an earlier, interrupted import placed '
                  f'but did not finish.')
            self.progress.phases['render']['total'] += len(todo)
            self.progress.phases['render']['detail']['from_earlier_runs'] = len(todo)
            for task in self._render_tasks(todo):
                self._render_q.put(task)

    def _video_task(self, task):
        """render_video_task, announced while it runs (see _report_videos)."""
        path = task['target_path'] or ''
        stem = task['derivative_stem'] or os.path.splitext(os.path.basename(path))[0]
        # Where the proxy is being written: beside its final name, with a
        # .partial suffix (backend-transcode/app.py), in the dated cache folder
        # create_video_proxy picks (lib/utilities.py _cache_dir_for).
        partial = os.path.join(
            _cache_dir_for(task['target_dir'], _creation_from_ts(task['creation_ts'])),
            glob.escape(stem) + '_proxy.partial*')
        with self._video_lock:
            self._videos_now[task['id']] = (os.path.basename(path), path,
                                            time.monotonic(), partial)
        try:
            return render_video_task(task)
        finally:
            with self._video_lock:
                self._videos_now.pop(task['id'], None)

    def _report_videos(self, force=False):
        """
        Put the videos being converted into the render phase's detail, every
        VIDEO_REPORT_SECONDS: name, size, time so far and bytes of proxy written.
        The counter only moves when a video finishes; this shows it is working.
        Called from the render lane while it waits; the write happens on the
        writer thread, which owns the Progress counters.
        """
        now = time.monotonic()
        if not force and now - self._videos_reported_at < VIDEO_REPORT_SECONDS:
            return
        self._videos_reported_at = now
        with self._video_lock:
            current = list(self._videos_now.values())
        if not current and not self._videos_reported_any:
            return
        self._videos_reported_any = bool(current)
        videos = []
        for name, path, started, partial in current:
            try:
                size = os.path.getsize(path)
            except OSError:
                size = None
            written = 0
            for p in glob.glob(partial):
                try:
                    written += os.path.getsize(p)
                except OSError:
                    pass
            videos.append({'name': name, 'size': size, 'written': written,
                           'seconds': int(now - started)})

        def _fn(conn, videos=videos):
            detail = self.progress.phases['render']['detail']
            if videos:
                detail['videos_now'] = videos
            else:
                detail.pop('videos_now', None)
            self.progress.dirty = True
        self.writer.submit(_fn)

    def _render_lane(self):
        """
        Background: images on `render_workers` threads, videos on their own lane.

        Waits for ingest to finish when source and catalog are one spinning disk
        - render reads the catalog, and would drag the head away from the
        source - and otherwise overlaps with it: ingest is disk-bound, render
        CPU-bound (§5.5).
        """
        # IMPORT_RENDER_DURING_INGEST=1 overrides, for when previews during a
        # long import matter more than how fast it finishes.
        if self.same_spindle and not _env_int('IMPORT_RENDER_DURING_INGEST', 0):
            print('Source and catalog are one spinning disk: previews will be made '
                  'once every file is in.')
            self._ingest_done.wait()
        img_pool = ThreadPoolExecutor(max_workers=max(1, self.settings['render_workers']),
                                      thread_name_prefix='render')
        vid_pool = ThreadPoolExecutor(max_workers=max(1, self.settings['video_workers']),
                                      thread_name_prefix='video')
        pending = set()
        deferred = []
        last_check = 0.0

        def _done(f):
            if not f.cancelled():
                self._on_render(f.result())

        def _cancel_queued(futures):
            """
            On a stop, drop every task handed to a pool but not yet started.

            Only the tasks in hand finish. Before this, everything already
            queued in the pools ran first - up to 4 x (workers + 1) of them, and
            when those are videos that took an hour on oldserver (2026-10-03). A
            cancelled task stays 'placed', so the next run renders it.
            """
            cancelled = sum(1 for f in futures if f.cancel())
            if cancelled:
                print(f'Stop: {cancelled} queued preview(s) left for the next run.')
            return {f for f in futures if not f.cancelled()}

        def _wait_below(futures, limit):
            """
            Wait until fewer than `limit` tasks are outstanding - the bounded
            look-ahead - or, with limit 1, until all are done.

            A plain wait() here was where a stop went unnoticed: the lane sat in
            it while the pools worked through everything queued. It now wakes
            every two seconds to ask, and a stop cancels the queue.
            """
            while len(futures) >= limit:
                _, futures = wait(futures, timeout=2.0, return_when=FIRST_COMPLETED)
                if self.stop_check():
                    futures = _cancel_queued(futures)
                self._report_videos()
            return futures

        try:
            while True:
                try:
                    # A timeout, not a plain get(): a video may be converting
                    # while the lane waits for ingest, and it must be reported.
                    task = self._render_q.get(timeout=2.0)
                except queue.Empty:
                    self._report_videos()
                    continue
                if task is None:
                    # Ingest is done: now the videos held back, unless stopped
                    # (they stay 'placed' and the next run renders them). With
                    # videos_last, only once every photo preview is finished.
                    if deferred and self.videos_last:
                        pending = _wait_below(pending, 1)
                    if deferred and not self.stopped:
                        print(f'All photos are done; making {len(deferred)} video preview(s).'
                              if self.videos_last else
                              f'All files are in; making {len(deferred)} video preview(s).')
                        for t in deferred:
                            if self.stop_check():
                                pending = _cancel_queued(pending)
                                break
                            fut = vid_pool.submit(self._video_task, t)
                            fut.add_done_callback(_done)
                            pending.add(fut)
                            pending = {f for f in pending if not f.done()}
                            pending = _wait_below(
                                pending, 4 * (self.settings['video_workers'] + 1))
                    break
                # Ingest checks for a stop between files; once it has finished,
                # this lane is all that is left to notice one. Throttled: it is a
                # database read.
                if (not self.stopped and self._ingest_done.is_set()
                        and time.monotonic() - last_check > 2.0):
                    last_check = time.monotonic()
                    self.stop_check()
                if self.stopped:
                    if pending:
                        pending = _cancel_queued(pending)
                    continue
                is_video = is_video_file(task['target_path'] or '')
                if is_video and (self.videos_last or (
                        self.defer_videos and not self._ingest_done.is_set())):
                    deferred.append(task)
                    continue
                fn = self._video_task if is_video else self.image_render_task
                fut = (vid_pool if is_video else img_pool).submit(fn, task)
                fut.add_done_callback(_done)
                pending.add(fut)
                pending = {f for f in pending if not f.done()}
                # Bounded look-ahead: never hold more than a few pages of tasks.
                pending = _wait_below(pending, 4 * (self.settings['render_workers'] + 1))
            _wait_below(pending, 1)
            self._report_videos(force=True)
        finally:
            img_pool.shutdown(wait=True)
            vid_pool.shutdown(wait=True)

    def _plan_groups_placed(self):
        """The run's definitive pairs, over what was actually placed (spec P8)."""
        rows = self.read.execute(
            f"SELECT id, source_path, match_keys_json FROM import_items "
            f"WHERE execution_id=? AND kind IN ('image', 'raw', 'video') "
            f"AND state IN ({','.join('?' * len(_PLACED_STATES))})",
            (self.eid_key, *_PLACED_STATES)).fetchall()
        if len(rows) < 2:
            return 0
        try:
            ids = {r['source_path']: r['id'] for r in rows}
            keys = {r['source_path']: (json.loads(r['match_keys_json'])
                                       if r['match_keys_json'] else {}) for r in rows}
            found = find_groups(list(ids), keys, is_raw_file, is_image_file,
                                log=lambda *_: None, is_video=is_video_file)
        except Exception as exc:
            print(f'Grouping skipped ({exc}); importing every file as its own image.')
            return 0
        updates = []
        for g in found:
            key = str(ids[g['primary']])
            info = json.dumps({'kind': g['kind'], 'match_rule': g['match_rule'],
                               'match_evidence': g['match_evidence']})
            updates.append((key, 'primary', info, ids[g['primary']]))
            updates.append((key, 'secondary', None, ids[g['secondary']]))
        if updates:
            def _fn(conn):
                conn.executemany('UPDATE import_items SET group_key=?, group_role=?, '
                                 'group_info=? WHERE id=?', updates)
                self.progress.bump('resolve', groups=len(found))
            self.writer.call(_fn)
        return len(found)


def execute(source_dir, target_dir, files, progress_callback, catalog_id=None, catalog=None,
            execution_id=None, phases=None):
    """Same contract as io_picture_import_workflow.execute."""
    if catalog is None and catalog_id is not None:
        catalog = Catalog.get_by_id(catalog_id)
        if catalog is None:
            raise ValueError(f'Catalog with ID {catalog_id} not found')
    if catalog is None:
        raise ValueError('Either catalog or catalog_id must be provided')

    phases = tuple(phases) if phases else PHASES
    run = _StreamRun(source_dir, target_dir, catalog, execution_id, progress_callback)
    try:
        result = run.run(files, phases)
    except BaseException:
        _mark_running_phases(run.db_path, execution_id, 'failed')
        raise
    finally:
        run.close()

    if result['stopped'] and execution_id:
        record_stopped(run.db_path, execution_id,
                       processed_files=run.progress.processed_files(),
                       skipped_files=result['skipped_files'],
                       message=stop_message(run.progress.processed_files(),
                                            run.progress.total_files,
                                            getattr(run, 'stop_reason', None)))
    return result


if __name__ == '__main__':
    v1.main(execute_fn=execute, description='Streaming picture import (subprocess mode)')
