"""
Move mode (documentation/specs/ingest-move-spec.md), through both importers.

    python -m pytest test_import_move.py
"""

import importlib
import os
import shutil
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

from test_import_v2 import _jpeg  # noqa: E402

V1, V2 = 'io_picture_import_workflow', 'io_picture_import_v2_workflow'
BOTH = pytest.mark.parametrize('module', [V1, V2])


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / 'src'
    _jpeg(str(src / '2020' / '06' / 'a.jpg'), (200, 10, 10), exif_date='2020:06:14 10:20:30')
    _jpeg(str(src / '2020' / '06' / 'b.jpg'), (10, 200, 10))
    _jpeg(str(src / '2021' / 'deep' / 'c.jpg'), (10, 10, 200), exif_date='2021:03:01 08:00:00')
    os.makedirs(src / 'copies')
    shutil.copy(src / '2020' / '06' / 'a.jpg', src / 'copies' / 'a_again.jpg')  # in-run duplicate
    (src / 'notes.txt').write_text('not a photo')
    (src / '2020' / '06' / 'a.AAE').write_text('<sidecar/>')                   # sidecar
    return src


def _run(module, src, target, mode='move'):
    # Not about the disk the suite runs on (storage_alerts, the import's guard).
    os.environ['AIRLOCK_IMPORT_DISK_GUARD'] = '0'
    from models import CatalogDatabase, ExecutionModel
    from catalog import Catalog
    os.makedirs(target, exist_ok=True)
    db = CatalogDatabase(str(target))
    eid = ExecutionModel(db).create(1, 'Import Pictures')
    wf = importlib.import_module(module)
    catalog = Catalog(name='t', target_directory=str(target), ingest_mode=mode)
    result = wf.execute(source_dir=str(src), target_dir=str(target), files=None,
                        progress_callback=None, catalog=catalog, execution_id=eid)
    return eid, result


def _ops(target, eid=None):
    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(
        'SELECT * FROM file_operations' + (' WHERE execution_id=?' if eid else '')
        + ' ORDER BY id', (eid,) if eid else ())]
    conn.close()
    return rows


def _files(root):
    return sorted(os.path.relpath(os.path.join(d, f), root)
                  for d, _, fs in os.walk(root) for f in fs)


def _catalog_files(target):
    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    rows = conn.execute('SELECT file_path, sha256_hash FROM file_contents').fetchall()
    conn.close()
    return rows


# ─── The whole run ───────────────────────────────────────────────────────────

@BOTH
def test_a_move_leaves_only_what_was_not_imported(tree, tmp_path, module):
    target = tmp_path / 'cat'
    mtime = os.stat(tree / '2020' / '06' / 'a.jpg').st_mtime
    eid, result = _run(module, tree, target)

    # The source holds exactly the duplicate (in DUPS/) and what is not media.
    assert _files(tree) == ['2020/06/a.AAE', 'DUPS/copies/a_again.jpg', 'notes.txt']
    # Emptied folders are gone; the one holding a sidecar stays.
    assert not (tree / '2021').exists()
    assert not (tree / 'copies').exists()
    assert (tree / '2020' / '06').is_dir()

    # The catalog has the three photos, each file as it was.
    stored = _catalog_files(target)
    assert len(stored) == 3
    for rel, _ in stored:
        assert os.path.exists(target / rel)
    a = next(rel for rel, _ in stored if rel.startswith('Images/2020/06/14'))
    assert abs(os.stat(target / a).st_mtime - mtime) < 0.01      # §2.3

    ops = _ops(target, eid)
    moves = [o for o in ops if o['operation_type'] == 'move']
    assert len(moves) == 3 and {o['status'] for o in moves} == {'moved'}
    dup = [o for o in ops if o['operation_type'] == 'duplicate_moved']
    assert len(dup) == 1 and dup[0]['status'] == 'moved'
    assert dup[0]['target_path'] == str(tree / 'DUPS' / 'copies' / 'a_again.jpg')
    assert dup[0]['details'].startswith('duplicate of ')
    left = sorted(os.path.basename(o['source_path']) for o in ops
                  if o['operation_type'] == 'left_in_place')
    assert left == ['a.AAE', 'notes.txt']

    assert result['moved_files'] == 3
    assert result['duplicates_moved'] == 1
    assert result['left_in_place'] == 2
    assert result['folders_removed'] >= 3      # 2021/deep, 2021, copies


@BOTH
def test_names_and_dates_match_a_copy_mode_import(tree, tmp_path, module):
    copy_src = tmp_path / 'copy_src'
    shutil.copytree(tree, copy_src)
    _run(module, copy_src, tmp_path / 'copycat', mode='copy')
    _run(module, tree, tmp_path / 'movecat')
    # Photos only: copy mode also files notes.txt under Others/; move mode leaves it.
    photos = lambda t: sorted(r for r in _catalog_files(t) if r[0].startswith('Images'))  # noqa: E731
    assert photos(tmp_path / 'copycat') == sorted(_catalog_files(tmp_path / 'movecat'))


# ─── Duplicates (§5) ─────────────────────────────────────────────────────────

@BOTH
def test_duplicates_of_the_catalog_and_of_removed_files_go_to_dups(tmp_path, module):
    src, target = tmp_path / 'src', tmp_path / 'cat'
    _jpeg(str(src / 'x' / 'one.jpg'), (1, 2, 3))
    _jpeg(str(src / 'x' / 'two.jpg'), (4, 5, 6))
    keep = tmp_path / 'keep'
    shutil.copytree(src, keep)
    _run(module, src, target)
    assert _files(src) == []

    # Remove one from the catalog, then bring both back.
    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    conn.execute("UPDATE file_contents SET removed_at=CURRENT_TIMESTAMP "
                 "WHERE file_name LIKE '%' AND id=(SELECT MIN(id) FROM file_contents)")
    removed_name = conn.execute('SELECT file_name FROM file_contents WHERE removed_at IS NOT NULL'
                                ).fetchone()[0]
    conn.commit()
    conn.close()
    shutil.copytree(keep / 'x', src / 'x')
    eid, _ = _run(module, src, target)
    assert _files(src) == ['DUPS/x/one.jpg', 'DUPS/x/two.jpg']
    details = sorted(o['details'] for o in _ops(target, eid)
                     if o['operation_type'] == 'duplicate_moved')
    assert any(d.startswith(f'removed from the catalog: {removed_name}') for d in details)
    assert any(d.startswith('duplicate of ') for d in details)
    assert len(_catalog_files(target)) == 2       # nothing re-imported

    # A third time: DUPS/ already holds those names, so the new ones are suffixed,
    # and DUPS/ itself is not walked (its files are not duplicates of themselves).
    shutil.copytree(keep / 'x', src / 'x')
    eid, _ = _run(module, src, target)
    assert _files(src) == ['DUPS/x/one (2).jpg', 'DUPS/x/one.jpg',
                           'DUPS/x/two (2).jpg', 'DUPS/x/two.jpg']
    ops = [o for o in _ops(target, eid) if o['operation_type'] == 'duplicate_moved']
    assert len(ops) == 2 and all('renamed' in o['details'] for o in ops)


# ─── Verify, then delete (§4) ────────────────────────────────────────────────

@BOTH
def test_a_copy_that_does_not_verify_never_deletes_the_source(tree, tmp_path, module,
                                                              monkeypatch):
    import lib.import_move as im
    monkeypatch.setenv('IMPORT_MOVE_RENAME', '0')       # the copy path has the check
    monkeypatch.setattr(im, 'calculate_sha256', lambda p: 'not-the-hash')
    target = tmp_path / 'cat'
    before = _files(tree)
    eid, result = _run(module, tree, target)
    assert _files(tree) == before                       # every source still there
    assert _catalog_files(target) == []
    images = target / 'Images'
    assert _files(images) == [] if images.exists() else True
    failed = [o for o in _ops(target, eid) if o['operation_type'] == 'move']
    assert failed and {o['status'] for o in failed} == {'failed'}
    assert 'does not match' in failed[0]['details']


@BOTH
@pytest.mark.parametrize('rename', ['1', '0'])
def test_resume_finishes_a_recorded_move_and_does_not_call_it_a_duplicate(
        tree, tmp_path, module, monkeypatch, rename):
    import lib.import_move as im
    target = tmp_path / 'cat'
    # A run that records every move but is "stopped" before any source leaves.
    monkeypatch.setenv('IMPORT_MOVE_RENAME', rename)
    monkeypatch.setattr(im.MoveMode, '_delete_committed_sources', lambda self: None)
    eid1, _ = _run(module, tree, target)
    assert 'copies/a_again.jpg' not in _files(tree)          # dups still go to DUPS
    assert {o['status'] for o in _ops(target, eid1) if o['operation_type'] == 'move'} \
        == {'pending_move' if rename == '1' else 'pending_delete'}
    assert (tree / '2021' / 'deep' / 'c.jpg').exists()
    monkeypatch.undo()
    monkeypatch.setenv('IMPORT_MOVE_RENAME', rename)

    eid2, _ = _run(module, tree, target)
    assert _files(tree) == ['2020/06/a.AAE', 'DUPS/copies/a_again.jpg', 'notes.txt']
    assert {o['status'] for o in _ops(target, eid1)
            if o['operation_type'] == 'move'} == {'moved'}
    assert not [o for o in _ops(target, eid2) if o['operation_type'] == 'duplicate_moved']
    assert len(_catalog_files(target)) == 3
    for rel, _ in _catalog_files(target):
        assert (target / rel).exists()


# ─── Safety (§8) ─────────────────────────────────────────────────────────────

@BOTH
def test_a_source_inside_the_catalog_is_refused(tmp_path, module):
    target = tmp_path / 'cat'
    inside = target / 'Images' / 'drop'
    _jpeg(str(inside / 'p.jpg'), (9, 9, 9))
    with pytest.raises(Exception, match='inside this catalog'):
        _run(module, inside, target)
    assert (inside / 'p.jpg').exists()


@BOTH
def test_a_source_containing_the_catalog_is_refused(tmp_path, module):
    src = tmp_path / 'src'
    target = src / 'cat'
    _jpeg(str(src / 'p.jpg'), (9, 9, 9))
    with pytest.raises(Exception, match='folder of this catalog'):
        _run(module, src, target)
    assert (src / 'p.jpg').exists()


@BOTH
def test_not_enough_space_stops_before_anything_moves(tree, tmp_path, module, monkeypatch):
    import lib.import_move as im

    class _Usage:
        free = 10
    monkeypatch.setattr(im.shutil, 'disk_usage', lambda p: _Usage)
    before = _files(tree)
    with pytest.raises(Exception, match='Not enough space'):
        _run(module, tree, tmp_path / 'cat')
    assert _files(tree) == before


def test_copy_mode_still_never_touches_the_source(tree, tmp_path):
    before = _files(tree)
    _run(V2, tree, tmp_path / 'cat', mode='copy')
    assert _files(tree) == before


def test_a_route_must_be_marked_before_files_are_moved(tmp_path, monkeypatch):
    import storage_routes as sr
    route = {'id': 1, 'name': 'Camera', 'container_path': str(tmp_path), 'read_only': 1}
    assert 'read-only' in sr.move_refusal(route)
    route['read_only'] = 0
    assert sr.move_refusal(route) is None
    monkeypatch.setattr(sr.os, 'access', lambda p, m: False)
    assert 'cannot write' in sr.move_refusal(route)


@BOTH
def test_an_unwritable_catalog_stops_before_the_scan(tree, tmp_path, module):
    # As on oldserver: the catalog's Images folder could not be written, and every
    # file failed at place with the same bare error. Now the run refuses first.
    target = tmp_path / 'cat'
    os.makedirs(target / 'Images')
    os.chmod(target / 'Images', 0o500)
    before = _files(tree)
    try:
        with pytest.raises(Exception, match='cannot store files.*Images'):
            _run(module, tree, target, mode='copy')
        with pytest.raises(Exception, match='cannot store files'):
            _run(module, tree, target)
    finally:
        os.chmod(target / 'Images', 0o700)
    assert _files(tree) == before


@BOTH
def test_resume_corrects_a_move_whose_source_was_deleted_but_not_recorded(
        tree, tmp_path, module, monkeypatch):
    # As on oldserver: the backend was killed after deleting sources but before
    # writing 'moved'. The sources are gone, the records still say pending.
    import lib.import_move as im
    target = tmp_path / 'cat'
    monkeypatch.setattr(im.MoveMode, '_write_delete_results', lambda self, conn: None)
    eid1, _ = _run(module, tree, target)
    assert not (tree / '2021' / 'deep' / 'c.jpg').exists()
    assert {o['status'] for o in _ops(target, eid1) if o['operation_type'] == 'move'} \
        <= {'pending_delete', 'pending_move'}     # whichever way it moved them
    monkeypatch.undo()

    _run(module, tree, target)
    moves = [o for o in _ops(target, eid1) if o['operation_type'] == 'move']
    assert {o['status'] for o in moves} == {'moved'}
    assert all('already deleted' in o['details'] for o in moves)


# ─── Rename instead of copy ──────────────────────────────────────────────────

@BOTH
def test_one_disk_moves_by_rename_and_writes_nothing(tree, tmp_path, module, monkeypatch):
    # Same disk, same mount: the file is renamed into the catalog - no copy is
    # made, so the catalog file IS the source file (same inode).
    import lib.import_move as im
    inode = os.stat(tree / '2021' / 'deep' / 'c.jpg').st_ino
    copies = []
    monkeypatch.setattr(im, 'calculate_sha256', lambda p: copies.append(p) or 'x')
    target = tmp_path / 'cat'
    eid, result = _run(module, tree, target)
    assert _files(tree) == ['2020/06/a.AAE', 'DUPS/copies/a_again.jpg', 'notes.txt']
    c = next(rel for rel, _ in _catalog_files(target) if rel.startswith('Images/2021'))
    assert os.stat(target / c).st_ino == inode
    assert copies == []                         # nothing to verify: no copy was made
    moves = [o for o in _ops(target, eid) if o['operation_type'] == 'move']
    assert len(moves) == 3 and {o['status'] for o in moves} == {'moved'}
    assert not os.path.exists(target / 'Images' / '.import-staging' / str(eid))


@BOTH
def test_a_refused_rename_keeps_the_source_and_takes_back_the_record(
        tree, tmp_path, module, monkeypatch):
    import lib.import_move as im
    real_rename = os.rename

    def refuse(src, dst):
        if '.airlock-rename-check' in str(src) or 'DUPS' in str(dst):
            return real_rename(src, dst)        # the probe and duplicates still work
        raise OSError(30, 'Read-only file system')
    monkeypatch.setattr(im.os, 'rename', refuse)
    target = tmp_path / 'cat'
    eid, result = _run(module, tree, target)
    monkeypatch.undo()
    # Every photo is still in the source; the catalog holds nothing for them.
    assert (tree / '2020' / '06' / 'a.jpg').exists()
    assert (tree / '2021' / 'deep' / 'c.jpg').exists()
    assert _catalog_files(target) == []
    moves = [o for o in _ops(target, eid) if o['operation_type'] == 'move']
    assert moves and {o['status'] for o in moves} == {'failed'}
    assert all('Read-only' in o['details'] for o in moves)
    # And the next run imports them normally.
    _run(module, tree, target)
    assert len(_catalog_files(target)) == 3


def test_rename_alias_through_another_mount():
    # oldserver: the source's mount is the whole USB disk; the catalog's Images
    # folder is a second mount of a folder on that same disk.
    from lib.import_move import alias_path
    rows = [('1', '259:0', '/', '/'),
            ('5', '259:0', '/opt/shoebox/catalogs', '/catalogs'),
            ('6', '8:49', '/shoebox-storage/family/Images', '/catalogs/family/Images'),
            ('7', '8:33', '/media', '/storage'),
            ('8', '8:49', '/', '/storage/usb1'),
            ('9', '8:65', '/', '/storage/usb2')]
    # The family photos: the same disk, reachable under the source's mount.
    assert alias_path('/storage/usb1/photos', '/catalogs/family/Images', rows) \
        == '/storage/usb1/shoebox-storage/family/Images'
    # A source on another disk: no rename, copy instead.
    assert alias_path('/storage/usb2/pics', '/catalogs/family/Images', rows) is None
    # A catalog folder on the SSD array, not the USB disk: copy.
    assert alias_path('/storage/usb1/photos', '/catalogs/other', rows) is None
    # Source and catalog on one mount: rename directly.
    assert alias_path('/storage/usb1/photos', '/storage/usb1/cat/Images', rows) \
        == '/storage/usb1/cat/Images'
    # The disk's folder is mounted narrower than the catalog folder: not visible.
    narrow = rows[:4] + [('8', '8:49', '/photos', '/storage/usb1')]
    assert alias_path('/storage/usb1/x', '/catalogs/family/Images', narrow) is None


@BOTH
def test_a_file_gone_from_both_places_is_flagged_and_clears_when_it_returns(
        tree, tmp_path, module, monkeypatch):
    import lib.import_move as im
    target = tmp_path / 'cat'
    # Moves recorded, the files moved, but the 'moved' status never written...
    monkeypatch.setattr(im.MoveMode, '_write_delete_results', lambda self, conn: None)
    eid1, _ = _run(module, tree, target)
    monkeypatch.undo()
    # ...and then the catalog copies vanish (a folder mounted somewhere else).
    stored = _catalog_files(target)
    aside = tmp_path / 'aside'
    for rel, _ in stored:
        os.makedirs((aside / rel).parent, exist_ok=True)
        shutil.move(target / rel, aside / rel)

    _, result = _run(module, tree, target)
    moves = [o for o in _ops(target, eid1) if o['operation_type'] == 'move']
    assert {o['status'] for o in moves} == {'missing'}
    assert all('MISSING FROM BOTH PLACES' in o['details'] for o in moves)
    assert result['moves_missing'] == 3

    # The copies come back: the next run sees them and clears the flag.
    for rel, _ in stored:
        shutil.move(aside / rel, target / rel)
    _, result = _run(module, tree, target)
    assert {o['status'] for o in _ops(target, eid1) if o['operation_type'] == 'move'} \
        == {'moved'}
    assert result['moves_missing'] == 0


@BOTH
@pytest.mark.parametrize('mode', ['move', 'copy'])
def test_empty_files_are_noted_and_left_in_place(tree, tmp_path, module, mode):
    # Two empty "media" files: never imported, never each other's duplicate
    # (they share the SHA-256 of nothing), and still where they were.
    (tree / 'phone').mkdir()
    (tree / 'phone' / 'IMG_3388.MOV').write_bytes(b'')
    (tree / 'phone' / 'IMG_4418.HEIC').write_bytes(b'')
    target = tmp_path / 'cat'
    eid, result = _run(module, tree, target, mode=mode)
    assert (tree / 'phone' / 'IMG_3388.MOV').exists()
    assert (tree / 'phone' / 'IMG_4418.HEIC').exists()
    assert not [r for r in _catalog_files(target) if r[1] == EMPTY_SHA]
    ops = _ops(target, eid)
    empties = sorted(os.path.basename(o['source_path']) for o in ops
                     if o['details'] == 'empty file (0 bytes)')
    assert empties == ['IMG_3388.MOV', 'IMG_4418.HEIC']
    assert all(o['operation_type'] == 'left_in_place' for o in ops
               if o['details'] == 'empty file (0 bytes)')
    assert not [o for o in ops if 'IMG_4418' in (o['source_path'] or '')
                and o['operation_type'] in ('skip', 'duplicate_moved')]


EMPTY_SHA = 'e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855'
