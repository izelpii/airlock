"""
Storage alerts, and an import that stops before the disk is full.

    python -m pytest test_storage_alerts.py

2026-10-04: oldserver's catalog RAID filled to 100%; the import failed 2,672
previews one by one and then itself, and every catalog on the disk answered
"disk I/O error" with nothing saying why.
"""

import os
import sqlite3
import sys
from collections import namedtuple
from types import SimpleNamespace

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
sys.path.insert(0, os.path.join(HERE, 'workflows'))

import storage_alerts as sa  # noqa: E402

GB = sa.GB
Usage = namedtuple('Usage', 'total used free')


def _fake_disk(monkeypatch, by_prefix):
    """by_prefix: {path prefix: (total_gb, used_gb, free_gb)}; mounts are the prefixes."""
    # Resolved like the real statvfs: a symlinked Images/ is measured where it
    # points, not where the link sits.
    by_prefix = {os.path.realpath(k): v for k, v in by_prefix.items()}

    def usage(path):
        path = os.path.realpath(path)
        for prefix, (t, u, f) in sorted(by_prefix.items(), key=lambda kv: -len(kv[0])):
            if path.startswith(prefix):
                return Usage(int(t * GB), int(u * GB), int(f * GB))
        raise OSError('unknown path')

    def mount(path):
        path = os.path.realpath(path)
        for prefix in sorted(by_prefix, key=len, reverse=True):
            if path.startswith(prefix):
                return prefix
        return '/'
    monkeypatch.setattr(sa.shutil, 'disk_usage', usage)
    monkeypatch.setattr(sa, 'mount_point', mount)
    # Each fake disk is its own device, as each real one would be.
    monkeypatch.setattr(sa, '_device', lambda path: mount(path))


@pytest.mark.parametrize('used, free, expected', [
    (500, 500, None),            # half full
    (910, 90, 'warning'),        # 91%
    (980, 20, 'warning'),        # 98%, 20 GB free: near, not yet critical
    (995, 5.5, 'critical'),      # 99.5%
    (40, 15, 'warning'),         # small disk, 27% but only 15 GB free
    (40, 3, 'critical'),         # under 5 GB free
    (447, 13, 'warning'),        # the laptop disk: 97%, 13 GB free - warn, don't stop
])
def test_severity(monkeypatch, tmp_path, used, free, expected):
    _fake_disk(monkeypatch, {str(tmp_path): (used + free, used, free)})
    assert sa.disk_state(str(tmp_path))['severity'] == expected


def _catalog(tmp_path, name, cid, originals_elsewhere=None):
    target = tmp_path / name
    os.makedirs(target / 'cache')
    images = target / 'Images'
    if originals_elsewhere:
        os.makedirs(originals_elsewhere, exist_ok=True)
        os.symlink(originals_elsewhere, images)
    else:
        os.makedirs(images)
    return SimpleNamespace(id=cid, name=name, target_directory=str(target), ingest_mode='copy')


@pytest.fixture
def conn(tmp_path):
    c = sqlite3.connect(str(tmp_path / 'app.db'))
    yield c
    c.close()


def test_one_alert_per_disk_naming_every_catalog_on_it(monkeypatch, tmp_path, conn):
    usb = str(tmp_path / 'usb')
    family = _catalog(tmp_path, 'family', 9, originals_elsewhere=usb)
    band = _catalog(tmp_path, 'band', 7)
    # The catalogs' own folders on a full "RAID"; the originals on a roomy "USB".
    _fake_disk(monkeypatch, {str(tmp_path): (916, 912, 4), usb: (16000, 9000, 7000)})

    changed, entries = sa.check([family, band], conn)
    assert changed
    alerts = sa.list_alerts(conn)
    assert len(alerts) == 1
    a = alerts[0]
    assert a['severity'] == 'critical' and a['active']
    assert set(a['details']['users']) == {'family', 'band'}
    assert 'database and previews' in a['details']['users']['family']['parts']
    assert 'Disk almost full' in a['title']
    # The USB disk is measured too, and is fine.
    assert {e['mount'] for e in entries} == {os.path.realpath(tmp_path), os.path.realpath(usb)}


def test_an_alert_updates_escalates_and_resolves(monkeypatch, tmp_path, conn):
    cat = _catalog(tmp_path, 'family', 9)
    _fake_disk(monkeypatch, {str(tmp_path): (1000, 920, 80)})            # warning
    sa.check([cat], conn)
    aid = sa.list_alerts(conn)[0]['id']
    assert sa.acknowledge(conn, aid)
    assert sa.summary(conn) == {'active': 1, 'critical': 0, 'unacknowledged': 0}

    _fake_disk(monkeypatch, {str(tmp_path): (1000, 990, 10)})            # critical
    sa.check([cat], conn)
    a = sa.list_alerts(conn)[0]
    assert a['id'] == aid and a['severity'] == 'critical'
    assert a['acknowledged_at'] is None                    # worse: raised again
    assert sa.summary(conn)['unacknowledged'] == 1

    _fake_disk(monkeypatch, {str(tmp_path): (1000, 500, 500)})           # space freed
    changed, _ = sa.check([cat], conn)
    assert changed and sa.list_alerts(conn) == []
    history = sa.list_alerts(conn, include_resolved=True)
    assert len(history) == 1 and history[0]['resolved_at']


def test_an_unreadable_catalog_does_not_resolve_its_alert(monkeypatch, tmp_path, conn):
    cat = _catalog(tmp_path, 'family', 9)
    _fake_disk(monkeypatch, {str(tmp_path): (1000, 990, 10)})
    sa.check([cat], conn)
    _fake_disk(monkeypatch, {})                                          # nothing readable
    sa.check([cat], conn)
    assert len(sa.list_alerts(conn)) == 1


# ─── The API ──────────────────────────────────────────────────────────────────

from test_photo_dates_batch import app_module, catalog, client  # noqa: E402,F401


def test_the_alerts_api(monkeypatch, app_module, catalog, client, tmp_path):
    _fake_disk(monkeypatch, {catalog.target_directory: (916, 910, 6),
                             os.path.dirname(app_module.__file__): (500, 100, 400)})
    monkeypatch.setattr(app_module.Catalog, 'get_all', classmethod(lambda cls: [catalog]))
    emitted = []
    monkeypatch.setattr(app_module.socketio, 'emit', lambda ev, data=None, **k: emitted.append(ev))

    body = client.post('/api/alerts/check').get_json()
    assert body['critical'] == 1 and len(body['alerts']) == 1
    assert body['thresholds']['critical_percent'] == sa.CRITICAL_PERCENT
    assert 'alerts_updated' in emitted
    aid = body['alerts'][0]['id']

    assert client.get('/api/alerts').get_json()['unacknowledged'] == 1
    r = client.post(f'/api/alerts/{aid}/acknowledge')
    assert r.status_code == 200 and r.get_json()['unacknowledged'] == 0
    assert client.post(f'/api/alerts/{aid}/acknowledge').status_code == 404


# ─── The import stops before the disk is full ─────────────────────────────────

def test_an_import_stops_cleanly_on_a_full_disk(tmp_path, monkeypatch):
    from test_import_v2 import _jpeg, _run, V2
    src = tmp_path / 'src'
    for i in range(6):
        _jpeg(str(src / '2020' / '06' / f'p{i}.jpg'), (20 * i, 40, 90))
    target = tmp_path / 'catalog'

    calls = []

    def full(path):
        calls.append(path)
        return {'mount': '/', 'total': 100 * GB, 'used': 99 * GB, 'free': 1 * GB,
                'percent': 99.0, 'severity': 'critical'}
    monkeypatch.setattr(sa, 'disk_state', full)

    eid, result = _run(V2, src, target, monkeypatch, AIRLOCK_IMPORT_DISK_GUARD=1)
    assert result['stopped'] and calls
    conn = sqlite3.connect(str(target / 'workflow_results.db'))
    status, message = conn.execute('SELECT status, error_message FROM workflow_executions '
                                   'WHERE id = ?', (eid,)).fetchone()
    failed = conn.execute("SELECT COUNT(*) FROM import_items WHERE state LIKE '%failed'").fetchone()[0]
    conn.close()
    assert status == 'stopped'
    assert "disk is almost full" in message and 'Running this workflow again continues' in message
    assert failed == 0                                   # nothing failed: it all waits


def test_bind_mounts_of_one_drive_are_one_disk(monkeypatch, tmp_path, conn):
    """oldserver: Images, Videos and Others of two catalogs are six bind mounts of one
    USB drive; that is one disk and, full, one alert."""
    a = _catalog(tmp_path, 'family', 9)
    b = _catalog(tmp_path, 'band', 7)
    mounts = {str(tmp_path / n / 'Images'): f'/catalogs/{n}/Images' for n in ('family', 'band')}
    monkeypatch.setattr(sa, 'disk_state', lambda p: {
        'mount': mounts.get(str(p), '/catalogs'), 'total': 100 * GB, 'used': 99.5 * GB,
        'free': 0.5 * GB, 'percent': 99.5, 'severity': 'critical'})
    # One device for everything - what st_dev says for bind mounts of one drive.
    monkeypatch.setattr(sa, '_device', lambda path: 42)
    entries = sa.measure([a, b])
    assert len(entries) == 1
    assert entries[0]['mount'] == '/catalogs'
    assert set(entries[0]['mounts']) >= {'/catalogs', '/catalogs/family/Images', '/catalogs/band/Images'}
    sa.check([a, b], conn)
    assert len(sa.list_alerts(conn)) == 1


def test_alerts_page_through_history_and_the_bell_sees_the_newest(conn):
    sa.ensure_schema(conn)
    for i in range(30):
        conn.execute("INSERT INTO alerts (alert_key, kind, severity, title, message, first_seen, "
                     "last_seen, resolved_at) VALUES (?, 'storage', ?, ?, '', ?, ?, ?)",
                     (f'k{i}', 'critical' if i % 3 == 0 else 'warning', f'alert {i}',
                      f'2026-10-05 01:{i:02d}:00', f'2026-10-05 01:{i:02d}:00',
                      None if i >= 25 else f'2026-10-05 02:{i:02d}:00'))
    conn.commit()
    assert sa.count_alerts(conn) == 5 and sa.count_alerts(conn, include_resolved=True) == 30
    page1 = sa.list_alerts(conn, include_resolved=True, limit=25)
    page2 = sa.list_alerts(conn, include_resolved=True, limit=25, offset=25)
    assert len(page1) == 25 and len(page2) == 5
    assert {a['id'] for a in page1}.isdisjoint({a['id'] for a in page2})
    assert all(a['active'] for a in page1[:5])                     # open ones first
    recent = sa.list_alerts(conn, include_resolved=True, limit=10, newest_first=True)
    assert [a['title'] for a in recent] == [f'alert {i}' for i in range(29, 19, -1)]
