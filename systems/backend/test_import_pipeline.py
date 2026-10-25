"""
The phased picture import (documentation/features/parallelism.md section 10.4).

Self-contained: builds its own small source tree with PIL, so it runs anywhere
the backend's requirements are installed.

    python -m pytest test_import_pipeline.py
"""

import os
import shutil
import sqlite3
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

from lib.import_pipeline import Progress, Writer, connect  # noqa: E402
from lib.import_schema import (PHASES, ensure_import_schema,  # noqa: E402
                               PLACED_STATES, release_placed_metadata,
                               prune_old_items, reclaimable_bytes)


# ─── Writer ──────────────────────────────────────────────────────────────────

def _db(tmp_path):
    path = str(tmp_path / 'w.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE t (v INTEGER UNIQUE)')
    conn.execute('CREATE TABLE workflow_executions (id INTEGER PRIMARY KEY, '
                 'processed_files INTEGER, total_files INTEGER)')
    ensure_import_schema(conn.cursor())
    conn.commit()
    conn.close()
    return path


def test_writer_isolates_a_failing_write(tmp_path):
    path = _db(tmp_path)
    progress = Progress(None, 0)
    writer = Writer(path, progress, batch=100)
    errors = []
    for v in (1, 2, 2, 3):          # the second 2 violates UNIQUE
        writer.submit(lambda conn, v=v: conn.execute('INSERT INTO t VALUES (?)', (v,)),
                      on_error=lambda conn, exc: errors.append(str(exc)))
    writer.flush()
    writer.close()
    rows = [r[0] for r in sqlite3.connect(path).execute('SELECT v FROM t ORDER BY v')]
    assert rows == [1, 2, 3]
    assert len(errors) == 1 and 'UNIQUE' in errors[0]


def test_writer_batches_commits(tmp_path):
    path = _db(tmp_path)
    progress = Progress(None, 0)
    writer = Writer(path, progress, batch=500)
    commits = []
    real = writer._commit

    def counting(buf, force_snapshot=False):
        if buf:
            commits.append(len(buf))
        return real(buf, force_snapshot)
    writer._commit = counting
    for v in range(1000):
        writer.submit(lambda conn, v=v: conn.execute('INSERT INTO t VALUES (?)', (v,)))
    writer.flush()
    writer.close()
    # 1000 writes, a handful of transactions - not 1000.
    assert sum(commits) == 1000
    assert len(commits) <= 4


def test_writer_surfaces_a_fatal_error(tmp_path):
    progress = Progress(None, 0)
    writer = Writer(str(tmp_path / 'no' / 'such' / 'dir' / 'x.db'), progress)
    with pytest.raises(Exception):
        writer.submit(lambda conn: None)
        writer.flush()


# ─── Progress ────────────────────────────────────────────────────────────────

def test_processed_files_is_monotonic_across_phases():
    p = Progress(1, 2)
    p.total_files = 1000
    seen = []
    for phase in PHASES:
        p.phases[phase]['status'] = 'running'
        p.phases[phase]['total'] = 1000
        for _ in range(10):
            p.bump(phase, done=100)
            seen.append(p.processed_files())
        p.phases[phase]['status'] = 'completed'
        seen.append(p.processed_files())
    assert seen == sorted(seen)
    assert seen[-1] == 1000


# ─── End to end, on a synthetic tree ─────────────────────────────────────────

def _jpeg(path, colour, size=(1600, 1200)):
    from PIL import Image
    os.makedirs(os.path.dirname(path), exist_ok=True)
    Image.new('RGB', size, colour).save(path, 'JPEG', quality=80)


@pytest.fixture
def tree(tmp_path):
    src = tmp_path / 'src'
    _jpeg(str(src / '2020' / '06' / 'a.jpg'), (200, 10, 10))
    _jpeg(str(src / '2020' / '06' / 'b.jpg'), (10, 200, 10))
    os.makedirs(src / 'copies')
    shutil.copy(src / '2020' / '06' / 'a.jpg', src / 'copies' / 'a_again.jpg')
    (src / 'notes.txt').write_text('not a photo')
    (src / '2020' / '06' / 'a.AAE').write_text('<sidecar/>')
    return src


def _run(src, target, monkeypatch, phases=None, **env):
    from models import CatalogDatabase, ExecutionModel
    from catalog import Catalog
    import importlib
    # Not about the disk the suite runs on (storage_alerts, the import's guard).
    monkeypatch.setenv('AIRLOCK_IMPORT_DISK_GUARD', '0')
    for k, v in env.items():
        monkeypatch.setenv(k, str(v))
    os.makedirs(target, exist_ok=True)
    db = CatalogDatabase(str(target))
    eid = ExecutionModel(db).create(1, 'Import Pictures')
    wf = importlib.import_module('io_picture_import_workflow')
    catalog = Catalog(name='t', target_directory=str(target))
    result = wf.execute(source_dir=str(src), target_dir=str(target), files=None,
                        progress_callback=None, catalog=catalog, execution_id=eid,
                        phases=phases)
    return eid, result


@pytest.mark.parametrize('workers', [1, 4])
def test_import_dedups_ignores_and_renders(tree, tmp_path, monkeypatch, workers):
    target = tmp_path / f'cat{workers}'
    env = dict(IMPORT_PROBE_CONCURRENCY=workers, IMPORT_PLACE_CONCURRENCY=workers,
               IMPORT_RENDER_CONCURRENCY=workers)
    eid, result = _run(tree, target, monkeypatch, **env)
    assert result['new_files'] == 3            # a, b, notes.txt
    assert result['skipped_files'] == 1        # a_again: same bytes as a
    assert result['ignored_files'] == 1        # the .AAE sidecar
    assert result['files_failed'] == 0

    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    names = sorted(r[0] for r in conn.execute('SELECT file_name FROM file_contents'))
    # The in-run winner is the smallest source path: 2020/06/a.jpg, not copies/.
    assert any(n.endswith('-a.jpg') for n in names)
    assert not any('a_again' in n for n in names)
    rungs = {r[0] for r in conn.execute('SELECT rung FROM alternative_versions')}
    assert {'1200', 'thumb72', 'thumb320'} <= rungs
    phases = dict(conn.execute('SELECT phase, status FROM import_phases WHERE execution_id=?',
                               (eid,)))
    assert phases == {p: 'completed' for p in PHASES}
    ops = dict(conn.execute("SELECT status, COUNT(*) FROM file_operations GROUP BY status"))
    assert ops == {'success': 3, 'skipped': 1}

    # A second import of the same tree skips everything, hashing nothing.
    eid2, again = _run(tree, target, monkeypatch, **env)
    assert again['new_files'] == 0
    assert again['skipped_files'] == 4
    assert conn.execute('SELECT COUNT(*) FROM file_contents').fetchone()[0] == 3


# ─── Copy while hashing (ingestion-performance-oldserver.md) ─────────────────────

def _count_copies(monkeypatch):
    """Record every copy place makes from the SOURCE - what staging removes."""
    import importlib
    wf = importlib.import_module('io_picture_import_workflow')
    real = wf.copy_file_with_catalog
    calls = []

    def counting(source_path, target_path, catalog):
        calls.append(source_path)
        return real(source_path, target_path, catalog)
    monkeypatch.setattr(wf, 'copy_file_with_catalog', counting)
    return calls


def _staged_files(target):
    """Every staged copy, wherever it is: each originals folder has its own area."""
    return [f for root, _, fs in os.walk(str(target)) if '.import-staging' in root
            for f in fs]


def _staging_dirs(target):
    return [root for root, dirs, _ in os.walk(str(target)) if root.endswith('.import-staging')]


def test_staging_places_by_rename_not_a_second_read(tree, tmp_path, monkeypatch):
    copies = _count_copies(monkeypatch)
    target = tmp_path / 'cat'
    eid, result = _run(tree, target, monkeypatch)
    assert result['new_files'] == 3 and result['skipped_files'] == 1
    # Every file arrived from its staged copy; none was read again to copy it.
    assert copies == []
    # Nothing left behind, including the duplicate's copy.
    assert _staged_files(target) == []
    assert _staging_dirs(target) == []

    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    for src_rel, in [('2020/06/a.jpg',), ('2020/06/b.jpg',), ('notes.txt',)]:
        src = os.path.join(str(tree), src_rel)
        placed = conn.execute(
            'SELECT target_path FROM import_items WHERE execution_id=? AND source_path=?',
            (eid, src)).fetchone()[0]
        with open(src, 'rb') as a, open(placed, 'rb') as b:
            assert a.read() == b.read()
        # What shutil.copy2 would have kept.
        assert int(os.stat(placed).st_mtime) == int(os.stat(src).st_mtime)
    assert conn.execute('SELECT COUNT(*) FROM import_items WHERE staged_path IS NOT NULL'
                        ).fetchone()[0] == 0


def test_staging_off_copies_from_the_source(tree, tmp_path, monkeypatch):
    copies = _count_copies(monkeypatch)
    target = tmp_path / 'cat'
    _, result = _run(tree, target, monkeypatch, IMPORT_STAGE_COPIES=0)
    assert result['new_files'] == 3
    assert len(copies) == 3
    assert _staging_dirs(target) == []


def test_resume_reuses_a_stopped_runs_staged_copies(tree, tmp_path, monkeypatch):
    copies = _count_copies(monkeypatch)
    target = tmp_path / 'cat'
    # A run that got as far as probe, then stopped: its copies stay for a resume.
    _run(tree, target, monkeypatch, phases=('scan', 'probe'))
    staged = _staged_files(target)
    assert len(staged) == 4                  # a, b, notes.txt and a_again
    # Each beside the folder it is going to, so place is a rename even when
    # Images/ is a separate mount.
    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    where = dict(conn.execute('SELECT source_path, staged_path FROM import_items '
                              'WHERE staged_path IS NOT NULL'))
    for src, rel in where.items():
        top = 'Others' if src.endswith('.txt') else 'Images'
        assert rel.startswith(f'{top}/.import-staging/'), (src, rel)
    conn.close()

    # One copy has gone missing in between: that file must come from the source.
    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    lost = conn.execute("SELECT staged_path FROM import_items "
                        "WHERE source_path LIKE '%b.jpg'").fetchone()[0]
    os.remove(os.path.join(str(target), lost))

    _, result = _run(tree, target, monkeypatch)
    assert result['new_files'] == 3 and result['files_failed'] == 0
    assert [os.path.basename(p) for p in copies] == ['b.jpg']
    assert _staged_files(target) == []



# ─── Housekeeping: the duplicated metadata ───────────────────────────────────
# metadata_json is the exiftool dump an item carries between PROBE and PLACE.
# _record_placed copies it into exif_raw.raw_exif_json and clears it in the
# same statement; these cover the two sweeps that collect what that cannot -
# rows placed by an older version, and runs that ended before the end.
#
# The bug being pinned down: the end-of-run trim used to be skipped when a run
# was stopped, so every interrupted import kept its whole metadata copy. One
# 340k-file catalog carried 15.9 GB of it beside the 7.2 GB in exif_data.

def _items_db(tmp_path):
    path = str(tmp_path / 'items.db')
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE workflow_executions (id INTEGER PRIMARY KEY, '
                 'workflow_id INT, status TEXT, started_at TEXT, completed_at TEXT)')
    ensure_import_schema(conn.cursor())
    return conn


def _add(conn, execution_id, state, metadata='{"raw_exif_json": "xxxx"}'):
    cur = conn.execute(
        'INSERT INTO import_items (execution_id, source_path, size, mtime_ns, kind, '
        'state, metadata_json, match_keys_json) VALUES (?,?,?,?,?,?,?,?)',
        (execution_id, '/src/%s-%s.jpg' % (execution_id, state), 100, 1, 'image',
         state, metadata, '{"k":1}'))
    return cur.lastrowid


def _metadata(conn, item_id):
    return conn.execute('SELECT metadata_json FROM import_items WHERE id=?',
                        (item_id,)).fetchone()[0]


@pytest.mark.parametrize('state', PLACED_STATES)
def test_release_clears_every_placed_state(tmp_path, state):
    """A file in the catalog has its metadata in exif_data; the item's copy goes."""
    conn = _items_db(tmp_path)
    item = _add(conn, 1, state)
    assert release_placed_metadata(conn) == 1
    assert _metadata(conn, item) is None


@pytest.mark.parametrize('state', ['discovered', 'probed', 'to_place', 'duplicate'])
def test_release_keeps_the_resume_cache(tmp_path, state):
    """
    An UNPLACED item's metadata is what makes resuming a stopped import cheap
    (_adopt_probe, spec 7.2) - it is not a duplicate of anything yet, and
    clearing it would mean re-reading and re-exiftooling the file.
    """
    conn = _items_db(tmp_path)
    item = _add(conn, 1, state)
    assert release_placed_metadata(conn) == 0
    assert _metadata(conn, item) is not None


def test_release_does_not_care_how_the_run_ended(tmp_path):
    """The point of the fix: stopped and failed runs are swept like any other."""
    conn = _items_db(tmp_path)
    items = {}
    for eid, status in ((1, 'completed'), (2, 'stopped'), (3, 'failed'),
                        (4, 'interrupted'), (5, 'running')):
        conn.execute('INSERT INTO workflow_executions (id, workflow_id, status, '
                     'started_at, completed_at) VALUES (?,7,?,?,?)',
                     (eid, status, '2026-10-01', '2026-10-02'))
        items[status] = _add(conn, eid, 'rendered')
    assert release_placed_metadata(conn) == 5
    for status, item in items.items():
        assert _metadata(conn, item) is None, status


def test_release_is_idempotent(tmp_path):
    """Runs on every import, so the second time must cost nothing."""
    conn = _items_db(tmp_path)
    _add(conn, 1, 'rendered')
    assert release_placed_metadata(conn) == 1
    assert release_placed_metadata(conn) == 0


def test_prune_keeps_running_and_recent_imports(tmp_path):
    """
    Retention deletes items whose run ended long enough ago - and NEVER those
    of a running execution, whatever its dates say.
    """
    conn = _items_db(tmp_path)
    rows = {}
    for eid, status, when in (
            (1, 'completed', '2000-01-01'),      # ancient, completed  -> gone
            (2, 'stopped', '2000-01-01'),        # ancient, abandoned  -> gone
            (3, 'completed', 'now'),             # recent              -> kept
            (4, 'stopped', 'now'),               # recent abandoned    -> kept
            (5, 'running', '2000-01-01')):       # running             -> kept
        stamp = None if when == 'now' else when
        conn.execute('INSERT INTO workflow_executions (id, workflow_id, status, '
                     'started_at, completed_at) VALUES (?,7,?,?,?)',
                     (eid, status, stamp or '2026-10-06', stamp))
        rows[eid] = _add(conn, eid, 'probed')
    prune_old_items(conn)
    left = {r[0] for r in conn.execute('SELECT DISTINCT execution_id FROM import_items')}
    assert left == {3, 4, 5}, left


def test_reclaimable_bytes_grows_after_a_release(tmp_path):
    """
    SQLite does not shrink a file when rows are cleared; the pages go on the
    freelist. This is the number reclaim_catalog_db.py reports, and what a
    VACUUM would hand back.
    """
    conn = _items_db(tmp_path)
    big = '{"raw_exif_json": "%s"}' % ('x' * 40000)
    for _ in range(40):
        _add(conn, 1, 'rendered', metadata=big)
    conn.commit()
    assert reclaimable_bytes(conn) == 0
    release_placed_metadata(conn)
    conn.commit()
    assert reclaimable_bytes(conn) > 0
