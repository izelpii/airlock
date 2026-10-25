"""
Files whose names lie about what they are (media_types.sniff), in the import
and in the repair of a catalog imported before.

    python -m pytest test_misidentified_files.py

From oldserver's family import (2026-10-04): 2,269 macOS AppleDouble files with
rewritten names, 721 QuickTime clips named .JPG, 85 JPEGs cut short - 3,094
files without a preview.
"""

import os
import shutil
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

from media_types import sniff  # noqa: E402
from test_import_v2 import _jpeg, _run, V2  # noqa: E402

APPLEDOUBLE = b'\x00\x05\x16\x07\x00\x02\x00\x00Mac OS X        ' + b'\x00' * 200
HAVE_FFMPEG = shutil.which('ffmpeg') is not None


def _quicktime(path):
    """A real one-second QuickTime clip, at whatever name it is given."""
    subprocess.run(['ffmpeg', '-v', 'error', '-y', '-f', 'lavfi', '-i',
                    'color=c=red:s=64x64:d=1', '-c:v', 'libx264', '-f', 'mov', path],
                   check=True)


def _truncated_jpeg(path):
    _jpeg(path, (30, 160, 60))
    data = open(path, 'rb').read()
    with open(path, 'wb') as f:
        f.write(data[:len(data) * 3 // 5])


# ─── sniff ────────────────────────────────────────────────────────────────────

@pytest.mark.parametrize('head, expected', [
    (APPLEDOUBLE[:16], ('appledouble', None)),
    (b'\x00\x00\x00\x14ftypqt  \x00\x00\x00\x00', ('video', '.mov')),
    (b'\x00\x00\x00\x18ftypisom\x00\x00\x02\x00', ('video', '.mp4')),
    (b'\x00\x00\x00\x18ftypheic\x00\x00\x00\x00', (None, None)),     # a HEIC is a still
    (b'\x00\x00\x00\x18ftypmif1\x00\x00\x00\x00', (None, None)),
    (b'\xff\xd8\xff\xe0\x00\x10JFIF\x00', (None, None)),
])
def test_sniff(tmp_path, head, expected):
    p = tmp_path / 'f.jpg'
    p.write_bytes(head)
    assert sniff(str(p)) == expected


def test_sniff_never_raises(tmp_path):
    assert sniff(str(tmp_path / 'missing.jpg')) == (None, None)


# ─── The import ───────────────────────────────────────────────────────────────

@pytest.mark.skipif(not HAVE_FFMPEG, reason='needs ffmpeg to make a real clip')
def test_the_import_reads_what_files_are(tmp_path, monkeypatch):
    src = tmp_path / 'src'
    os.makedirs(src / '2018' / '08')
    _jpeg(str(src / '2018' / '08' / 'good.jpg'), (200, 10, 10))
    (src / '2018' / '08' / '20121212._2012-12-01 15.04.17.jpg').write_bytes(APPLEDOUBLE)
    _quicktime(str(src / '2018' / '08' / 'EGRU4349.JPG'))
    _truncated_jpeg(str(src / '2018' / '08' / 'cut.jpg'))
    target = tmp_path / 'catalog'

    _run(V2, src, target, monkeypatch)

    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    items = {os.path.basename(r['source_path']): dict(r) for r in conn.execute(
        'SELECT source_path, kind, state, phase_error FROM import_items')}
    files = {r['file_name'].split('-', 2)[-1]: dict(r) for r in conn.execute(
        'SELECT id, file_name, file_path, file_type FROM file_contents')}

    # AppleDouble: never a catalogue row, recorded as ignored with the reason.
    ad = items['20121212._2012-12-01 15.04.17.jpg']
    assert (ad['state'], ad['kind']) == ('ignored', 'ignored')
    assert 'AppleDouble' in ad['phase_error']
    assert not any('._2012' in n for n in files)

    # The clip: filed and named as the video it is, and given a preview.
    clip = files['EGRU4349.mov']
    assert clip['file_path'].startswith('Videos' + os.sep)
    assert items['EGRU4349.JPG']['kind'] == 'video'
    assert items['EGRU4349.JPG']['state'] == 'rendered', items['EGRU4349.JPG']

    # The damaged JPEG: rendered as far as it goes, not failed.
    assert items['cut.jpg']['state'] == 'rendered', items['cut.jpg']
    assert conn.execute('SELECT COUNT(*) FROM alternative_versions WHERE original_file_id = ?',
                        (files['cut.jpg']['id'],)).fetchone()[0] > 0
    assert items['good.jpg']['state'] == 'rendered'
    conn.close()


# ─── The repair of a catalog imported before ──────────────────────────────────

@pytest.fixture
def damaged_catalog(tmp_path, monkeypatch):
    """A catalog as the old import left it: three files that failed to render."""
    from models import CatalogDatabase
    target = tmp_path / 'catalog'
    CatalogDatabase(str(target))
    day = os.path.join('Images', '2018', '08', '13')
    os.makedirs(target / day)
    made = {}
    for name, write in (
            ('20180813173612-7a9f633e59-EGRU4349.JPG', _quicktime if HAVE_FFMPEG else None),
            ('20121201120000-5e2df500af-20121212._2012-12-01 15.04.17.jpg',
             lambda p: open(p, 'wb').write(APPLEDOUBLE)),
            ('20180813173612-1111111111-cut.jpg', _truncated_jpeg)):
        if write is None:
            continue
        abs_path = str(target / day / name)
        write(abs_path)
        made[name] = abs_path
    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    for name, abs_path in made.items():
        fid = conn.execute('INSERT INTO file_contents (file_path, file_name, file_type) '
                           'VALUES (?, ?, ?)', (os.path.join(day, name), name,
                                                os.path.splitext(name)[1].lower())).lastrowid
        error = ('image file is truncated (0 bytes not processed)' if 'cut' in name
                 else f"cannot identify image file '{abs_path}'")
        conn.execute("INSERT INTO import_items (execution_id, source_path, kind, state, "
                     "file_id, target_path, phase_error) VALUES (7, ?, 'image', "
                     "'render_failed', ?, ?, ?)", ('/src/' + name, fid, abs_path, error))
    eid = conn.execute("INSERT INTO events (name, start_time, end_time, file_count, type) "
                       "VALUES ('E', '2018-08-13', '2018-08-13', 1, 'Auto')").lastrowid
    ad_id = conn.execute("SELECT id FROM file_contents WHERE file_name LIKE '%._2012%'").fetchone()[0]
    conn.execute('INSERT INTO event_files (event_id, file_id) VALUES (?, ?)', (eid, ad_id))
    conn.commit()
    conn.close()
    return SimpleNamespace(id=9, name='Family', target_directory=str(target),
                           ingest_mode='copy', source_root=None)


def _rows(cat):
    conn = sqlite3.connect(os.path.join(cat.target_directory, 'workflow_results.db'))
    conn.row_factory = sqlite3.Row
    out = {r['file_name']: dict(r) for r in conn.execute(
        'SELECT fc.id, fc.file_name, fc.file_path, fc.file_type, fc.removed_at, '
        '       i.state, i.kind, i.target_path '
        'FROM file_contents fc JOIN import_items i ON i.file_id = fc.id')}
    events = conn.execute('SELECT COUNT(*) FROM event_files').fetchone()[0]
    conn.close()
    return out, events


def test_the_repair_dry_run_changes_nothing(damaged_catalog):
    import migrate_repair_misidentified as m
    before = _rows(damaged_catalog)
    s = m.migrate_catalog(damaged_catalog, dry_run=True, log=lambda *a: None)
    assert s['appledouble_removed'] == 1 and s['truncated_requeued'] == 1
    assert _rows(damaged_catalog) == before


@pytest.mark.skipif(not HAVE_FFMPEG, reason='needs ffmpeg to make a real clip')
def test_the_repair_fixes_each_kind(damaged_catalog):
    import migrate_repair_misidentified as m
    s = m.migrate_catalog(damaged_catalog, log=lambda *a: None)
    assert (s['appledouble_removed'], s['video_renamed'], s['truncated_requeued']) == (1, 1, 1)
    rows, events = _rows(damaged_catalog)

    ad = rows['20121201120000-5e2df500af-20121212._2012-12-01 15.04.17.jpg']
    assert ad['removed_at'] and ad['state'] == 'ignored'
    assert events == 0                                   # its event link dropped

    clip = rows['20180813173612-7a9f633e59-EGRU4349.mov']
    assert clip['file_path'] == os.path.join('Videos', '2018', '08', '13', clip['file_name'])
    assert clip['file_type'] == '.mov' and clip['kind'] == 'video' and clip['state'] == 'placed'
    assert os.path.isfile(os.path.join(damaged_catalog.target_directory, clip['file_path']))
    assert clip['target_path'].endswith(clip['file_path'])
    assert not os.path.exists(os.path.join(damaged_catalog.target_directory, 'Images', '2018',
                                           '08', '13', '20180813173612-7a9f633e59-EGRU4349.JPG'))

    assert rows['20180813173612-1111111111-cut.jpg']['state'] == 'placed'
    # Run again: nothing left to do.
    again = m.migrate_catalog(damaged_catalog, log=lambda *a: None)
    assert sum(again[k] for k in ('appledouble_removed', 'video_renamed',
                                  'truncated_requeued')) == 0


def test_the_repair_refuses_while_an_import_runs(damaged_catalog):
    import migrate_repair_misidentified as m
    conn = sqlite3.connect(os.path.join(damaged_catalog.target_directory, 'workflow_results.db'))
    wid = conn.execute("INSERT INTO workflows (name, description, python_script) VALUES "
                       "('Import', '', 'workflows/io_picture_import_v2_workflow.py')").lastrowid
    conn.execute("INSERT INTO workflow_executions (workflow_id, workflow_name, status) "
                 "VALUES (?, 'Import', 'running')", (wid,))
    conn.commit()
    conn.close()
    assert m.migrate_catalog(damaged_catalog, log=lambda *a: None)['busy'] == 1


def test_the_repair_requeues_previews_that_failed_for_want_of_space(damaged_catalog):
    """2026-10-04: oldserver's RAID filled and 2,672 video previews failed."""
    import migrate_repair_misidentified as m
    target = damaged_catalog.target_directory
    conn = sqlite3.connect(os.path.join(target, 'workflow_results.db'))
    for i, error in enumerate(("[Errno 28] No space left on device: '/catalogs/x_proxy.mp4'",
                               'no poster frame could be made')):
        rel = os.path.join('Videos', '2024', '05', f'clip{i}.MOV')
        os.makedirs(os.path.join(target, os.path.dirname(rel)), exist_ok=True)
        open(os.path.join(target, rel), 'wb').write(b'\x00\x00\x00\x14ftypqt  ' + b'\x00' * 64)
        fid = conn.execute("INSERT INTO file_contents (file_path, file_name, file_type) "
                           "VALUES (?, ?, '.mov')", (rel, f'clip{i}.MOV')).lastrowid
        conn.execute("INSERT INTO import_items (execution_id, source_path, kind, state, file_id, "
                     "target_path, phase_error) VALUES (9, ?, 'video', 'render_failed', ?, ?, ?)",
                     (f'/src/clip{i}.MOV', fid, os.path.join(target, rel), error))
    conn.commit()
    conn.close()
    s = m.migrate_catalog(damaged_catalog, log=lambda *a: None)
    assert s['disk_full_requeued'] == 2
    conn = sqlite3.connect(os.path.join(target, 'workflow_results.db'))
    states = [r[0] for r in conn.execute(
        "SELECT state FROM import_items WHERE source_path LIKE '/src/clip%'")]
    conn.close()
    assert states == ['placed', 'placed']
