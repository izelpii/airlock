"""
iPhone Live Photos as one image (image_groups kind 'live_photo'), and video
previews rendered after every photo's.

    python -m pytest test_live_photos.py
"""

import json
import os
import queue
import sqlite3
import sys
import threading
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

from lib.grouping import find_groups, find_live_photos, match_keys  # noqa: E402
from lib.utilities import is_image_file, is_raw_file, is_video_file  # noqa: E402

from test_photo_dates_batch import app_module, catalog, client  # noqa: E402,F401  (fixtures)

UUID = '5B0E5B1C-1F2A-4C55-9D7E-0123456789AB'


# ─── Matching ─────────────────────────────────────────────────────────────────

def test_the_identifier_is_read_from_a_still_and_from_a_clip():
    assert match_keys({'MakerNotes:ContentIdentifier': UUID})['content_id'] == UUID
    assert match_keys({'QuickTime:ContentIdentifier': UUID})['content_id'] == UUID
    assert match_keys({'EXIF:Make': 'Apple'})['content_id'] is None


def test_a_still_and_its_clip_become_one_live_photo():
    keys = {'/a/IMG_0112.HEIC': {'content_id': UUID}, '/a/IMG_0112.MOV': {'content_id': UUID}}
    groups = find_groups(list(keys), keys, is_raw_file, is_image_file, is_video=is_video_file)
    assert groups == [{
        'primary': '/a/IMG_0112.HEIC', 'secondary': '/a/IMG_0112.MOV', 'kind': 'live_photo',
        'match_rule': 'content_identifier', 'match_evidence': {'content_identifier': UUID},
    }]


def test_without_is_video_find_groups_is_unchanged():
    keys = {'/a/IMG_0112.HEIC': {'content_id': UUID}, '/a/IMG_0112.MOV': {'content_id': UUID}}
    assert find_groups(list(keys), keys, is_raw_file, is_image_file) == []


def test_a_shared_name_is_never_enough():
    # The 4 in 300 on oldserver: a real video that happens to share a still's name.
    keys = {'/a/IMG_0107.JPG': {}, '/a/IMG_0107.MOV': {}}
    assert find_live_photos(list(keys), keys, is_image_file, is_video_file) == []
    keys = {'/a/IMG_0107.JPG': {'content_id': UUID}, '/a/IMG_0107.MOV': {'content_id': 'other'}}
    assert find_live_photos(list(keys), keys, is_image_file, is_video_file) == []


def test_an_identifier_shared_by_more_than_a_pair_is_left_alone():
    keys = {'/a/IMG_1.HEIC': {'content_id': UUID}, '/b/IMG_1.HEIC': {'content_id': UUID},
            '/a/IMG_1.MOV': {'content_id': UUID}}
    logged = []
    assert find_live_photos(list(keys), keys, is_image_file, is_video_file,
                            log=logged.append) == []
    assert logged and 'ungrouped' in logged[0]


def test_a_still_already_in_a_raw_pair_keeps_its_clip_ungrouped():
    keys = {
        '/a/IMG_1.DNG': {'subsec': '2023:03:09 18:58:26.123', 'datetime': '2023:03:09 18:58:26'},
        '/a/IMG_1.JPG': {'subsec': '2023:03:09 18:58:26.123', 'datetime': '2023:03:09 18:58:26',
                         'content_id': UUID},
        '/a/IMG_1.MOV': {'content_id': UUID},
    }
    groups = find_groups(list(keys), keys, is_raw_file, is_image_file, is_video=is_video_file)
    assert [g['kind'] for g in groups] == ['raw_jpeg']


# ─── Render order ─────────────────────────────────────────────────────────────

def _render_run(monkeypatch, tasks, videos_last=True):
    """Drive _StreamRun._render_lane with stand-ins; return the order things ran."""
    import io_picture_import_v2_workflow as v2
    order, lock = [], threading.Lock()

    def _record(task):
        with lock:
            order.append(task['target_path'])
        return {}
    monkeypatch.setattr(v2, 'render_video_task', _record)
    q = queue.Queue()
    for t in tasks:
        q.put(t)
    q.put(None)
    done = threading.Event()
    done.set()
    fake = SimpleNamespace(
        same_spindle=False, settings={'render_workers': 2, 'video_workers': 1},
        _render_q=q, stopped=False, _ingest_done=done, stop_check=lambda: False,
        defer_videos=True, videos_last=videos_last,
        image_render_task=_record, _on_render=lambda r: None,
        # Looked up at call time, so the monkeypatched render_video_task runs.
        _video_task=lambda t: v2.render_video_task(t),
        _report_videos=lambda force=False: None)
    v2._StreamRun._render_lane(fake)
    return order


def test_every_photo_preview_comes_before_any_video(monkeypatch):
    tasks = [{'target_path': p} for p in ('a.mov', 'b.jpg', 'c.mp4', 'd.heic', 'e.jpg')]
    order = _render_run(monkeypatch, tasks)
    assert sorted(order[:3]) == ['b.jpg', 'd.heic', 'e.jpg']
    assert order[3:] == ['a.mov', 'c.mp4']


def test_videos_last_can_be_turned_off(monkeypatch):
    tasks = [{'target_path': p} for p in ('a.mov', 'b.jpg')]
    order = _render_run(monkeypatch, tasks, videos_last=False)
    assert sorted(order) == ['a.mov', 'b.jpg']     # interleaved: video not held back


# ─── The backfill ─────────────────────────────────────────────────────────────

def _db(cat):
    conn = sqlite3.connect(os.path.join(cat.target_directory, 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    return conn


def _file(cat, name, day='20230309'):
    stored = f'{day}185826-44bc2cd185-{name}'
    rel = os.path.join('Videos' if is_video_file(name) else 'Images', day[:4], day[4:6], day[6:], stored)
    path = os.path.join(cat.target_directory, rel)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    open(path, 'wb').close()
    conn = _db(cat)
    fid = conn.execute("INSERT INTO file_contents (file_path, file_name, file_type) VALUES (?, ?, ?)",
                       (rel, stored, os.path.splitext(name)[1].lower())).lastrowid
    conn.commit()
    conn.close()
    return fid, path


@pytest.fixture
def live_catalog(catalog):
    """A still and its clip in one event; a lone video; an unrendered import item."""
    still, still_path = _file(catalog, 'IMG_0112.HEIC')
    clip, clip_path = _file(catalog, 'IMG_0112.MOV')
    other, other_path = _file(catalog, 'IMG_0107.MOV')
    _file(catalog, 'IMG_0107.JPG')                      # same name, no identifier
    conn = _db(catalog)
    eid = conn.execute("INSERT INTO events (name, start_time, end_time, file_count, type) "
                       "VALUES ('E', '2023-03-09 18:58:26', '2023-03-09 18:58:26', 2, 'Auto')").lastrowid
    conn.executemany('INSERT INTO event_files (event_id, file_id) VALUES (?, ?)',
                     [(eid, still), (eid, clip)])
    conn.execute("INSERT INTO import_items (execution_id, source_path, kind, state, file_id) "
                 "VALUES (7, '/src/IMG_0112.MOV', 'video', 'placed', ?)", (clip,))
    conn.commit()
    conn.close()
    keys = {still_path: {'content_id': UUID}, clip_path: {'content_id': UUID},
            other_path: {}}
    return SimpleNamespace(catalog=catalog, still=still, clip=clip, other=other,
                           event=eid, keys=keys)


def _migrate(monkeypatch, live, **kw):
    import migrate_group_live_photos as m
    monkeypatch.setattr(m, 'read_match_keys',
                        lambda paths, log=None: {p: live.keys.get(p, {}) for p in paths})
    return m.migrate_catalog(live.catalog, log=lambda *a: None, **kw)


def test_the_backfill_groups_moves_links_and_skips_the_clip_render(monkeypatch, live_catalog):
    live = live_catalog
    summary = _migrate(monkeypatch, live)
    assert summary['groups'] == 1
    assert summary['renders_skipped'] == 1

    conn = _db(live.catalog)
    g = conn.execute('SELECT * FROM image_groups').fetchone()
    assert (g['kind'], g['primary_file_id'], g['match_rule']) == ('live_photo', live.still, 'content_identifier')
    assert json.loads(g['match_evidence']) == {'content_identifier': UUID}
    members = {r[0] for r in conn.execute('SELECT id FROM file_contents WHERE image_group_id = ?', (g['id'],))}
    assert members == {live.still, live.clip}
    assert [r[0] for r in conn.execute('SELECT file_id FROM event_files')] == [live.still]
    assert conn.execute('SELECT file_count FROM events').fetchone()[0] == 1
    item = conn.execute('SELECT state, group_role FROM import_items').fetchone()
    assert (item['state'], item['group_role']) == ('render_skipped', 'secondary')
    conn.close()


def test_the_backfill_is_idempotent_and_dry_run_changes_nothing(monkeypatch, live_catalog):
    assert _migrate(monkeypatch, live_catalog, dry_run=True)['groups'] == 1
    conn = _db(live_catalog.catalog)
    assert conn.execute('SELECT COUNT(*) FROM image_groups').fetchone()[0] == 0
    conn.close()
    _migrate(monkeypatch, live_catalog)
    assert _migrate(monkeypatch, live_catalog)['groups'] == 0


def test_the_backfill_refuses_while_an_import_runs(monkeypatch, live_catalog):
    conn = _db(live_catalog.catalog)
    wid = conn.execute("INSERT INTO workflows (name, description, python_script) "
                       "VALUES ('Import', '', 'workflows/io_picture_import_v2_workflow.py')").lastrowid
    conn.execute("INSERT INTO workflow_executions (workflow_id, workflow_name, status) "
                 "VALUES (?, 'Import', 'running')", (wid,))
    conn.commit()
    conn.close()
    summary = _migrate(monkeypatch, live_catalog)
    assert (summary['busy'], summary['groups']) == (1, 0)


# ─── Pages and events ─────────────────────────────────────────────────────────

def test_the_clip_is_not_listed_as_a_video(monkeypatch, live_catalog, client):
    _migrate(monkeypatch, live_catalog)
    ids = {v['id'] for v in client.get('/api/catalogs/1/videos').get_json()}
    assert live_catalog.clip not in ids
    assert live_catalog.other in ids


def test_create_events_counts_one_member_per_image(monkeypatch, live_catalog):
    _migrate(monkeypatch, live_catalog)
    import ev_extract_events as ev
    # No get_user_location to stub out any more: an event whose members carry no
    # GPS now gets NULL coordinates rather than the owner's home address. See
    # location-spec.md 9.1.
    ev.execute(None, live_catalog.catalog.target_directory, [], lambda *a, **k: None)
    conn = _db(live_catalog.catalog)
    members = {r[0] for r in conn.execute('SELECT file_id FROM event_files')}
    conn.close()
    assert live_catalog.still in members
    assert live_catalog.clip not in members
def test_one_unreadable_file_does_not_lose_its_whole_batch(monkeypatch, live_catalog):
    import migrate_group_live_photos as m

    def _read(paths, log=None):
        if len(paths) > 1:
            return {}                     # exiftool failed for the batch
        return {paths[0]: live_catalog.keys.get(paths[0], {})}
    monkeypatch.setattr(m, 'read_match_keys', _read)
    assert m.migrate_catalog(live_catalog.catalog, log=lambda *a: None)['groups'] == 1


def test_a_keys_cache_spares_the_second_read(monkeypatch, live_catalog, tmp_path):
    import migrate_group_live_photos as m
    reads = []

    def _read(paths, log=None):
        reads.extend(paths)
        return {p: live_catalog.keys.get(p, {}) for p in paths}
    monkeypatch.setattr(m, 'read_match_keys', _read)
    cache = str(tmp_path / 'keys.json')
    m.migrate_catalog(live_catalog.catalog, dry_run=True, log=lambda *a: None, keys_cache=cache)
    first = len(reads)
    assert first > 0 and os.path.exists(cache)
    summary = m.migrate_catalog(live_catalog.catalog, log=lambda *a: None, keys_cache=cache)
    assert len(reads) == first                     # nothing read twice
    assert summary['groups'] == 1
    conn = _db(live_catalog.catalog)
    assert 'idx_ii_file_id' in [r[1] for r in conn.execute('PRAGMA index_list(import_items)')]
    conn.close()


@pytest.mark.parametrize('videos_last', [False, True])
def test_a_stop_cancels_previews_that_have_not_started(monkeypatch, videos_last):
    """
    oldserver, 2026-10-03: a stop let the ~30 previews already queued in the pools
    run first - an hour of video. Only the preview in hand may finish now.
    videos_last=False is the interleaved path oldserver was on.
    """
    import time
    import io_picture_import_v2_workflow as v2
    started, lock = [], threading.Lock()

    def _slow(task):
        with lock:
            started.append(task['target_path'])
        time.sleep(0.3)
        return {}
    monkeypatch.setattr(v2, 'render_video_task', _slow)

    q = queue.Queue()
    for i in range(10):
        q.put({'target_path': f'v{i}.mov'})
    q.put(None)
    done = threading.Event()
    done.set()
    fake = SimpleNamespace(
        same_spindle=False, settings={'render_workers': 4, 'video_workers': 1},
        _render_q=q, stopped=False, _ingest_done=done,
        defer_videos=True, videos_last=videos_last,
        image_render_task=_slow, _on_render=lambda r: None,
        _video_task=lambda t: v2.render_video_task(t),
        _report_videos=lambda force=False: None)

    def _stop_check():
        # The stop arrives once the first preview is under way.
        if started:
            fake.stopped = True
        return fake.stopped
    fake.stop_check = _stop_check

    v2._StreamRun._render_lane(fake)
    # The one in hand, and at most one more that started before the lane
    # woke to ask - never the whole queue.
    assert 1 <= len(started) <= 2, started


def test_a_heic_and_its_jpeg_copy_pair_the_clip_with_the_heic():
    # 1,055 of oldserver's shared identifiers look like this.
    keys = {p: {'content_id': UUID} for p in
            ('/a/IMG_4596.HEIC', '/a/IMG_4596.jpeg', '/a/IMG_4596.MOV')}
    groups = find_live_photos(list(keys), keys, is_image_file, is_video_file)
    assert [(g['primary'], g['secondary'], g['match_rule']) for g in groups] == [
        ('/a/IMG_4596.HEIC', '/a/IMG_4596.MOV', 'content_identifier+name')]


def test_an_original_and_an_edit_each_keep_their_own_clip():
    keys = {p: {'content_id': UUID} for p in
            ('/a/IMG_6033.HEIC', '/a/IMG_E6033.HEIC', '/a/IMG_6033.MOV', '/a/IMG_E6033.MOV')}
    pairs = {(g['primary'], g['secondary'])
             for g in find_live_photos(list(keys), keys, is_image_file, is_video_file)}
    assert pairs == {('/a/IMG_6033.HEIC', '/a/IMG_6033.MOV'),
                     ('/a/IMG_E6033.HEIC', '/a/IMG_E6033.MOV')}


def test_names_also_work_on_catalogued_files():
    keys = {p: {'content_id': UUID} for p in (
        '/c/Images/20171020210544-3d8dab8b00-IMG_4596.JPG',
        '/c/Images/20171020210544-affc3b6154-IMG_4599.JPG',
        '/c/Videos/20171020210542-0b0fec50cb-IMG_4596.MOV')}
    groups = find_live_photos(list(keys), keys, is_image_file, is_video_file)
    assert [os.path.basename(g['primary']) for g in groups] == [
        '20171020210544-3d8dab8b00-IMG_4596.JPG']


def test_two_copies_of_the_same_jpeg_still_settle_nothing():
    keys = {p: {'content_id': UUID} for p in
            ('/a/IMG_E6033.JPG', '/b/IMG_E6033.JPG', '/a/IMG_E6033.MOV')}
    assert find_live_photos(list(keys), keys, is_image_file, is_video_file,
                            log=lambda *a: None) == []
