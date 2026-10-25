"""
The streaming import (workflows/io_picture_import_v2_workflow.py).

The first thing these tests pin down is that it is the SAME import: run over
the same files, it must decide the same dates, names and duplicates, and record
the same EXIF, as io_picture_import_workflow.py. Everything else it does is only
worth having on that condition.

    python -m pytest test_import_v2.py
"""

import hashlib
import importlib
import os
import shutil
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

from lib.import_schema import PHASES  # noqa: E402


# ─── A tree with the cases that matter ───────────────────────────────────────

def _jpeg(path, colour, size=(1600, 1200), exif_date=None, orientation=None):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    img = Image.new('RGB', size, colour)
    exif = Image.Exif()
    if exif_date:
        exif[0x8769] = {0x9003: exif_date}         # ExifIFD: DateTimeOriginal
        exif[0x0132] = exif_date                   # DateTime
    if orientation:
        exif[0x0112] = orientation
    img.save(path, 'JPEG', quality=80, exif=exif)


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / 'src'
    _jpeg(str(src / '2020' / '06' / 'a.jpg'), (200, 10, 10), exif_date='2020:06:14 10:20:30')
    _jpeg(str(src / '2020' / '06' / 'b.jpg'), (10, 200, 10))                   # no EXIF date
    _jpeg(str(src / '2021' / 'portrait.jpg'), (10, 10, 200), size=(2400, 1600),
          exif_date='2021:03:01 08:00:00', orientation=6)
    os.makedirs(src / 'copies')
    shutil.copy(src / '2020' / '06' / 'a.jpg', src / 'copies' / 'a_again.jpg')  # in-run duplicate
    (src / 'notes.txt').write_text('not a photo')
    (src / '2020' / '06' / 'a.AAE').write_text('<sidecar/>')                   # ignored
    return src


def _run(module, src, target, monkeypatch, phases=None, **env):
    from models import CatalogDatabase, ExecutionModel
    from catalog import Catalog
    # Off unless a test asks for it: these tests are about what an import
    # decides, not about the disk the test suite happens to run on.
    monkeypatch.setenv('AIRLOCK_IMPORT_DISK_GUARD', '0')
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    os.makedirs(target, exist_ok=True)
    db = CatalogDatabase(str(target))
    eid = ExecutionModel(db).create(1, 'Import Pictures')
    wf = importlib.import_module(module)
    catalog = Catalog(name='t', target_directory=str(target))
    result = wf.execute(source_dir=str(src), target_dir=str(target), files=None,
                        progress_callback=None, catalog=catalog, execution_id=eid,
                        phases=phases)
    return eid, result


V1, V2 = 'io_picture_import_workflow', 'io_picture_import_v2_workflow'


def _catalog(target, src):
    """What an import decided, in a form two runs can be compared on."""
    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    files = {}
    for r in conn.execute(
            '''SELECT fo.source_path, fc.file_name, fc.file_path, fc.sha256_hash, fc.file_size,
                      fc.master_datetime, fc.master_datetime_source,
                      e.creation_date, e.camera_make, e.width, e.height, e.orientation
                 FROM file_operations fo JOIN file_contents fc ON fc.id = fo.file_id
                 LEFT JOIN exif_data e ON e.id = fc.exif_data_id
                WHERE fo.status = 'success' '''):
        files[os.path.relpath(r['source_path'], str(src))] = {
            k: r[k] for k in r.keys() if k != 'source_path'}
    skipped = sorted(os.path.relpath(r[0], str(src)) for r in conn.execute(
        "SELECT source_path FROM file_operations WHERE status='skipped'"))
    rungs = sorted((r[0], r[1], r[2], r[3], r[4]) for r in conn.execute(
        'SELECT fc.file_name, av.rung, av.width, av.height, av.alternative_file_name '
        'FROM alternative_versions av JOIN file_contents fc ON fc.id = av.original_file_id'))
    groups = conn.execute('SELECT COUNT(*) FROM image_groups').fetchone()[0]
    conn.close()
    return files, skipped, rungs, groups


# ─── The same import ─────────────────────────────────────────────────────────

def test_same_decisions_as_the_phased_import(tree, tmp_path, monkeypatch):
    _, r1 = _run(V1, tree, tmp_path / 'v1', monkeypatch)
    _, r2 = _run(V2, tree, tmp_path / 'v2', monkeypatch)
    for key in ('new_files', 'skipped_files', 'ignored_files', 'files_failed'):
        assert r1[key] == r2[key], key
    f1, s1, g1_rungs, grp1 = _catalog(tmp_path / 'v1', tree)
    f2, s2, g2_rungs, grp2 = _catalog(tmp_path / 'v2', tree)
    # Dates, names, hashes, EXIF: identical, file by file.
    assert f1 == f2
    # The same file is the duplicate.
    assert s1 == s2 == ['copies/a_again.jpg']
    # Derivatives: the same sizes, dimensions and names (pixels may differ).
    assert g1_rungs == g2_rungs
    assert grp1 == grp2


@pytest.mark.parametrize('readers', [1, 4])
def test_streaming_import_end_to_end(tree, tmp_path, monkeypatch, readers):
    target = tmp_path / f'cat{readers}'
    eid, result = _run(V2, tree, target, monkeypatch, IMPORT_READERS=readers)
    assert result['new_files'] == 4            # a, b, portrait, notes.txt
    assert result['skipped_files'] == 1        # a_again
    assert result['ignored_files'] == 1
    assert result['files_failed'] == 0 and not result['stopped']

    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    phases = dict(conn.execute('SELECT phase, status FROM import_phases WHERE execution_id=?',
                               (eid,)))
    assert phases == {p: 'completed' for p in PHASES}
    # The portrait is stored upright: EXIF orientation 6 turns 2400x1600 into
    # a 1600-wide image, and the 1200 rung is 1200x1800.
    dims = dict(conn.execute(
        "SELECT av.rung, av.width || 'x' || av.height FROM alternative_versions av "
        "JOIN file_contents fc ON fc.id = av.original_file_id "
        "WHERE fc.file_name LIKE '%portrait%'"))
    assert dims['1200'] == '1200x1800'
    # Nothing staged is left behind, in any originals folder.
    assert not [r for r, _, _ in os.walk(str(target)) if r.endswith('.import-staging')]
    # A second run of the same tree changes nothing.
    _, again = _run(V2, tree, target, monkeypatch, IMPORT_READERS=readers)
    assert again['new_files'] == 0 and again['skipped_files'] == 5


def test_a_duplicate_of_the_catalog_is_never_written(tree, tmp_path, monkeypatch):
    target = tmp_path / 'cat'
    _run(V2, tree, target, monkeypatch)
    # A new folder holding a renamed copy of a photo already in the catalog.
    more = tmp_path / 'more'
    os.makedirs(more)
    shutil.copy(tree / '2020' / '06' / 'b.jpg', more / 'renamed.jpg')

    import lib.ingest_io as io_mod
    wf = importlib.import_module(V2)
    copies = []
    real = io_mod.hash_and_copy

    def counting(src, *a, **k):
        copies.append(os.path.basename(src))
        return real(src, *a, **k)
    monkeypatch.setattr(wf, 'hash_and_copy', counting)
    _, result = _run(V2, more, target, monkeypatch)
    assert result['new_files'] == 0 and result['skipped_files'] == 1
    assert copies == []                      # same size as b.jpg: hashed first, not copied


def test_new_files_are_read_once(tree, tmp_path, monkeypatch):
    wf = importlib.import_module(V2)
    reads = []
    for name in ('hash_file', 'hash_and_copy'):
        real = getattr(wf, name)

        def spy(path, *a, _real=real, _name=name, **k):
            reads.append((_name, os.path.basename(path)))
            return _real(path, *a, **k)
        monkeypatch.setattr(wf, name, spy)
    _run(V2, tree, tmp_path / 'cat', monkeypatch, IMPORT_READERS=1)
    by_file = {}
    for kind, f in reads:
        by_file.setdefault(f, []).append(kind)
    # Unique sizes: one pass each, hashing and copying together.
    for f in ('b.jpg', 'portrait.jpg', 'notes.txt'):
        assert by_file[f] == ['hash_and_copy'], f
    # a.jpg and a_again.jpg share a size and content. However the two reads
    # interleave, the duplicate is never copied twice and never placed.
    pair = [k for k in by_file if k.startswith('a')]
    assert sum(len(by_file[k]) for k in pair) <= 3
    assert by_file['a.jpg'][-1] == 'hash_and_copy'      # the winner is the one copied


def test_resume_adopts_a_stopped_runs_work(tree, tmp_path, monkeypatch):
    target = tmp_path / 'cat'
    wf = importlib.import_module(V2)
    # Stop after the first commit.
    calls = {'n': 0}
    real_stop = wf._StreamRun.stop_check

    def stop_after_one(self):
        calls['n'] += 1
        if calls['n'] > 1:
            self.stopped = True
        return self.stopped or real_stop(self)
    monkeypatch.setattr(wf._StreamRun, 'stop_check', stop_after_one)
    _, first = _run(V2, tree, target, monkeypatch, IMPORT_READERS=1)
    assert first['stopped']
    monkeypatch.setattr(wf._StreamRun, 'stop_check', real_stop)

    _, second = _run(V2, tree, target, monkeypatch, IMPORT_READERS=1)
    assert not second['stopped'] and second['files_failed'] == 0
    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    assert conn.execute('SELECT COUNT(*) FROM file_contents').fetchone()[0] == 4
    assert not [r for r, _, _ in os.walk(str(target)) if r.endswith('.import-staging')]


# ─── Building blocks ─────────────────────────────────────────────────────────

def test_hash_and_copy_is_one_faithful_pass(tmp_path):
    from lib.ingest_io import hash_and_copy, hash_file
    src = tmp_path / 'big.bin'
    data = os.urandom(3 * 1024 * 1024 + 17)
    src.write_bytes(data)
    os.utime(src, (1_600_000_000, 1_600_000_000))
    want = hashlib.sha256(data).hexdigest()
    for burst in (None, 1 << 20):
        dst = tmp_path / f'staged-{burst}.bin'
        assert hash_and_copy(str(src), str(dst), 256 * 1024, burst=burst) == want
        assert dst.read_bytes() == data
        assert int(os.stat(dst).st_mtime) == 1_600_000_000
        assert not os.path.exists(str(dst) + '.part')
    assert hash_file(str(src), 64 * 1024) == want


def test_adaptive_limiter_climbs_while_it_helps_and_turns_back(monkeypatch):
    from lib import storage_profile as sp
    clock = [0.0]
    monkeypatch.setattr(sp.time, 'monotonic', lambda: clock[0])
    lim = sp.AdaptiveLimiter(1, 4, window=1.0, gain=0.05, log=lambda *_: None)
    # Throughput per reader count: rises to 3, falls at 4.
    speed = {1: 100, 2: 180, 3: 230, 4: 200}
    for _ in range(12):
        lim.acquire()
        clock[0] += 1.0
        lim.release(speed[lim.limit])
    assert lim.limit in (2, 3)             # settles around the best, never at 4 for long
    assert max(n for n, _ in lim.history) >= 3

    fixed = sp.AdaptiveLimiter(2, 2, window=1.0, fixed=True)
    for _ in range(5):
        fixed.acquire()
        clock[0] += 1.0
        fixed.release(10)
    assert fixed.limit == 2


def test_classify_never_fails(tmp_path):
    from lib import storage_profile as sp
    info = sp.classify(str(tmp_path))
    assert info['kind'] in (sp.HDD, sp.SSD, sp.NETWORK, sp.UNKNOWN)
    assert sp.classify(str(tmp_path / 'missing'))['kind'] == sp.UNKNOWN
    assert sp.same_device(str(tmp_path), str(tmp_path))


def test_render_once_matches_the_per_rung_renderer(tmp_path):
    from lib.render_once import render_image_once
    from lib.utilities import create_alternate_version
    from io_picture_import_workflow import _creation_from_ts
    src = tmp_path / 'p.jpg'
    _jpeg(str(src), (90, 120, 30), size=(3000, 2000), orientation=6)
    task = {'id': 1, 'file_id': 7, 'target_path': str(src), 'target_dir': str(tmp_path / 'a'),
            'creation_ts': '20210301080000', 'derivative_stem': None,
            'rungs': ['2048'], 'existing': {}}
    new = render_image_once(task, _creation_from_ts)
    assert new['ok'], new['error']
    got = {r[7]: (r[2], r[5], r[6], r[4]) for r in new['rows']}
    for rung, width in (('1200', 1200), ('2048', 2048)):
        old = create_alternate_version(str(src), str(tmp_path / 'b'), 7, max_width=width,
                                       creation_date=_creation_from_ts('20210301080000'),
                                       suffix='_alt' if rung == '1200' else '_alt_2048')
        assert got[rung] == (old['name'], old['width'], old['height'], old['characteristic'])
    assert {'thumb72', 'thumb320'} <= set(got)


# ─── Catalog layout (catalog_layout.py) ──────────────────────────────────────

def test_a_new_catalog_gets_its_whole_tree(tmp_path):
    import catalog_layout as cl
    made = cl.ensure_catalog_dirs(str(tmp_path / 'cat'))
    for rel in ('Images', 'Videos', 'Others', 'cache/Images', 'cache/Videos',
                'cache/Albums', 'cache/Deployments', 'transfers'):
        assert os.path.isdir(tmp_path / 'cat' / rel), rel
    assert set(made) == set(cl.catalog_dirs('copy'))
    # Idempotent, and never disturbs what is there (a mount point, say).
    (tmp_path / 'cat' / 'Images' / 'keep.txt').write_text('x')
    assert cl.ensure_catalog_dirs(str(tmp_path / 'cat')) == []
    assert (tmp_path / 'cat' / 'Images' / 'keep.txt').read_text() == 'x'


def test_a_reference_catalog_gets_no_originals_folders(tmp_path):
    import catalog_layout as cl
    cl.ensure_catalog_dirs(str(tmp_path / 'ref'), 'reference')
    assert not os.path.exists(tmp_path / 'ref' / 'Images')
    assert os.path.isdir(tmp_path / 'ref' / 'cache' / 'Images')



def test_a_rerun_does_not_copy_a_duplicate_it_already_found(tree, tmp_path, monkeypatch):
    """
    a_again.jpg is a duplicate of a.jpg within the tree. The first run hashes
    it and records it as a duplicate; a re-run adopts that hash - and used to
    copy the whole file again before discarding it, because the duplicate check
    only ran for files whose hash was NOT already known.
    """
    target = tmp_path / 'cat'
    _run(V2, tree, target, monkeypatch, IMPORT_READERS=1)

    wf = importlib.import_module(V2)
    copies = []
    real = wf.hash_and_copy

    def counting(src, *a, **k):
        copies.append(os.path.basename(src))
        return real(src, *a, **k)
    monkeypatch.setattr(wf, 'hash_and_copy', counting)
    _, again = _run(V2, tree, target, monkeypatch, IMPORT_READERS=1)
    assert again['new_files'] == 0 and again['files_failed'] == 0
    assert 'a_again.jpg' not in copies
