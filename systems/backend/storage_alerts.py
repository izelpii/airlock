"""
storage_alerts.py — watch the disks catalogs write to, and raise alerts.

On 2026-10-04 oldserver's catalog RAID filled to 100% during an import: the import
died ("database or disk is full"), 2,672 previews failed, and every catalog on
that disk stopped working with "disk I/O error" - and nothing said why until
someone ran df. This module is the "why":

  * disk_state(path)       how full the filesystem holding `path` is, and
                           whether that is a warning or critical;
  * check(catalogs, conn)  every filesystem the catalogs write to, measured
                           once each, turned into alerts in the application
                           database: raised, updated while the condition lasts,
                           resolved when space comes back;
  * list_alerts / acknowledge for the Alerts page.

One alert per FILESYSTEM, not per catalog: two catalogs on one full disk are
one problem, and the alert lists which catalogs (and which parts of each) live
there. A catalog spans several filesystems on oldserver - its database and previews
on the SSD RAID, its originals bind-mounted from a USB drive. Grouped by DEVICE,
not mount point: inside the container each catalog's Images, Videos and Others
is its own bind mount of the same USB drive, and by mount point that one drive
was six "disks" and would have been six alerts.

Dependency-free (sqlite3, shutil): the import workflow imports disk_state to
stop cleanly before the disk is full, instead of failing file by file.
"""

import json
import os
import shutil
import sqlite3
from datetime import datetime, timezone

GB = 1024 ** 3


def _env_number(name, default):
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return float(default)


# Either test alone is enough. A percentage alone misses a small disk where 10%
# is a few hundred MB; free space alone misses a 16 TB drive at 99%. Critical is
# where imports STOP, so it is set where trouble is near, not merely possible:
# 97% stopped imports on a 460 GB laptop disk with 13 GB still free.
WARN_PERCENT = _env_number('AIRLOCK_STORAGE_WARN_PERCENT', 90)
CRITICAL_PERCENT = _env_number('AIRLOCK_STORAGE_CRITICAL_PERCENT', 99)
WARN_FREE_GB = _env_number('AIRLOCK_STORAGE_WARN_FREE_GB', 20)
CRITICAL_FREE_GB = _env_number('AIRLOCK_STORAGE_CRITICAL_FREE_GB', 5)

# The parts of a catalog worth naming in an alert, by the folder they live in.
_CATALOG_PARTS = (
    ('database and previews', ''),       # the catalog directory itself
    ('previews', 'cache'),
    ('originals (Images)', 'Images'),
    ('originals (Videos)', 'Videos'),
    ('other files', 'Others'),
)


def _now():
    return datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')


def mount_point(path):
    """The mount point holding `path` (the path itself may be one)."""
    path = os.path.realpath(path)
    while not os.path.ismount(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return path


def disk_state(path):
    """
    How full the filesystem holding `path` is:
      {'mount', 'total', 'used', 'free', 'percent', 'severity'}
    severity is None, 'warning' or 'critical'. None when `path` is unreadable.

    `free` is what an ordinary process may still write (f_bavail), and the
    percentage is used / (used + free), as df prints it: ext4 keeps 5% for root,
    and a disk df shows at 100% has nothing left for the backend even while
    statvfs still counts those reserved blocks as free.
    """
    try:
        usage = shutil.disk_usage(path)
    except OSError:
        return None
    used, free = usage.used, usage.free
    percent = 100.0 * used / (used + free) if (used + free) else 0.0
    if percent >= CRITICAL_PERCENT or free < CRITICAL_FREE_GB * GB:
        severity = 'critical'
    elif percent >= WARN_PERCENT or free < WARN_FREE_GB * GB:
        severity = 'warning'
    else:
        severity = None
    return {'mount': mount_point(path), 'total': usage.total, 'used': used,
            'free': free, 'percent': round(percent, 1), 'severity': severity}


def _catalog_paths(catalog):
    """(part label, path) for every place this catalog writes or reads files."""
    target = getattr(catalog, 'target_directory', None)
    out = []
    if target and os.path.isdir(target):
        for label, sub in _CATALOG_PARTS:
            p = os.path.join(target, sub) if sub else target
            if os.path.isdir(p):
                out.append((label, p))
    if getattr(catalog, 'ingest_mode', 'copy') == 'reference':
        root = getattr(catalog, 'source_root', None)
        if root and os.path.isdir(root):
            out.append(('originals (reference library)', root))
    return out


def _device(path):
    """The filesystem's device id - shared by every bind mount of one drive."""
    try:
        return os.stat(path).st_dev
    except OSError:
        return None


def measure(catalogs, extra=()):
    """
    One entry per filesystem: its state plus who uses it.
    `extra` is (label, path) pairs that belong to no catalog - the
    application's own database directory.
    """
    by_device = {}
    pairs = [(c, label, p) for c in catalogs for label, p in _catalog_paths(c)]
    pairs += [(None, label, p) for label, p in extra]
    for catalog, label, path in pairs:
        state = disk_state(path)
        if state is None:
            continue
        device = _device(path)
        if device is None:
            continue
        entry = by_device.setdefault(device, dict(state, users={}, mounts=[]))
        if state['mount'] not in entry['mounts']:
            entry['mounts'].append(state['mount'])
            # Named by its shortest mount path - "/catalogs" rather than
            # "/catalogs/family/Images" - which is also what keys its alert.
            entry['mount'] = min(entry['mounts'], key=len)
        who = (f"{catalog.name}" if catalog is not None else 'Airlock Photos')
        entry['users'].setdefault(who, {'catalog_id': getattr(catalog, 'id', None),
                                        'parts': []})
        if label not in entry['users'][who]['parts']:
            entry['users'][who]['parts'].append(label)
    return list(by_device.values())


# ── Alerts in the application database ───────────────────────────────────────

def ensure_schema(conn):
    conn.execute('''
        CREATE TABLE IF NOT EXISTS alerts (
            id               INTEGER PRIMARY KEY AUTOINCREMENT,
            -- What the alert is about, stable across checks: "storage:<mount>".
            alert_key        TEXT NOT NULL,
            kind             TEXT NOT NULL,
            -- 'warning' | 'critical'
            severity         TEXT NOT NULL,
            title            TEXT NOT NULL,
            message          TEXT NOT NULL,
            details          TEXT,
            first_seen       TIMESTAMP NOT NULL,
            last_seen        TIMESTAMP NOT NULL,
            resolved_at      TIMESTAMP,
            acknowledged_at  TIMESTAMP,
            acknowledged_by  INTEGER
        )
    ''')
    # At most one OPEN alert per key; resolved ones are history.
    conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS idx_alerts_open_key '
                 'ON alerts (alert_key) WHERE resolved_at IS NULL')
    conn.commit()


def _describe(entry):
    pct, free = entry['percent'], entry['free'] / GB
    users = '; '.join(f"{name} ({', '.join(u['parts'])})"
                      for name, u in sorted(entry['users'].items()))
    if entry['severity'] == 'critical':
        title = f"Disk almost full: {entry['mount']} at {pct:.0f}%"
        advice = ('Imports stop on their own before it fills; free space or move '
                  'previews to a bigger disk, then run them again.')
    else:
        title = f"Disk filling up: {entry['mount']} at {pct:.0f}%"
        advice = 'Free some space before it fills.'
    message = (f"{free:.1f} GB free of {entry['total'] / GB:.0f} GB. Used by: {users}. "
               f"{advice}")
    return title, message


def check(catalogs, conn, extra=()):
    """
    Measure, then bring the alerts table up to date. Returns
    (alerts changed?, the measurements).

    A condition that persists updates its open alert; one that gets WORSE
    (warning -> critical) clears the acknowledgement, so a problem someone
    dismissed at 91% is raised again at 98%. A filesystem back under the limits
    resolves its alert.
    """
    ensure_schema(conn)
    entries = measure(catalogs, extra)
    now = _now()
    changed = False
    flagged = set()
    for e in entries:
        if not e['severity']:
            continue
        key = f"storage:{e['mount']}"
        flagged.add(key)
        title, message = _describe(e)
        details = json.dumps({k: e[k] for k in ('mount', 'mounts', 'total', 'used',
                                                 'free', 'percent', 'users')})
        row = conn.execute('SELECT id, severity FROM alerts '
                           'WHERE alert_key = ? AND resolved_at IS NULL', (key,)).fetchone()
        if row is None:
            conn.execute('INSERT INTO alerts (alert_key, kind, severity, title, message, '
                         'details, first_seen, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                         (key, 'storage', e['severity'], title, message, details, now, now))
            changed = True
        else:
            worse = row[1] == 'warning' and e['severity'] == 'critical'
            if row[1] != e['severity']:
                changed = True
            conn.execute(
                'UPDATE alerts SET severity = ?, title = ?, message = ?, details = ?, '
                'last_seen = ?' + (', acknowledged_at = NULL, acknowledged_by = NULL'
                                   if worse else '') + ' WHERE id = ?',
                (e['severity'], title, message, details, now, row[0]))
    measured = {f"storage:{e['mount']}" for e in entries}
    for (aid, key) in conn.execute("SELECT id, alert_key FROM alerts WHERE kind = 'storage' "
                                   'AND resolved_at IS NULL').fetchall():
        # Only resolve what was measured this time: a catalog that could not be
        # read is not evidence that its disk recovered.
        if key in measured and key not in flagged:
            conn.execute('UPDATE alerts SET resolved_at = ? WHERE id = ?', (now, aid))
            changed = True
    conn.commit()
    return changed, entries


def _row_dict(r):
    d = dict(r)
    try:
        d['details'] = json.loads(d['details']) if d.get('details') else None
    except ValueError:
        d['details'] = None
    d['active'] = d['resolved_at'] is None
    return d


def list_alerts(conn, include_resolved=False, limit=200, offset=0, newest_first=False):
    """
    Alerts for the page and the bell. The page orders open before resolved and
    critical before warning; the bell (newest_first) is simply the most recent,
    open or not - "what happened lately".
    """
    ensure_schema(conn)
    conn.row_factory = sqlite3.Row
    sql = 'SELECT * FROM alerts'
    if not include_resolved:
        sql += ' WHERE resolved_at IS NULL'
    if newest_first:
        sql += ' ORDER BY last_seen DESC, id DESC'
    else:
        sql += (" ORDER BY resolved_at IS NOT NULL, "
                "CASE severity WHEN 'critical' THEN 0 ELSE 1 END, last_seen DESC, id DESC")
    sql += ' LIMIT ? OFFSET ?'
    return [_row_dict(r) for r in conn.execute(sql, (limit, max(0, offset)))]


def count_alerts(conn, include_resolved=False):
    ensure_schema(conn)
    sql = 'SELECT COUNT(*) FROM alerts'
    if not include_resolved:
        sql += ' WHERE resolved_at IS NULL'
    return conn.execute(sql).fetchone()[0]


def summary(conn):
    """Counts the top bar shows: open alerts, and how many are critical and
    not yet acknowledged."""
    ensure_schema(conn)
    rows = conn.execute('SELECT severity, acknowledged_at FROM alerts '
                        'WHERE resolved_at IS NULL').fetchall()
    return {
        'active': len(rows),
        'critical': sum(1 for s, _ in rows if s == 'critical'),
        'unacknowledged': sum(1 for _, a in rows if a is None),
    }


def acknowledge(conn, alert_id, user_id=None):
    ensure_schema(conn)
    cur = conn.execute('UPDATE alerts SET acknowledged_at = ?, acknowledged_by = ? '
                       'WHERE id = ? AND resolved_at IS NULL AND acknowledged_at IS NULL',
                       (_now(), user_id, alert_id))
    conn.commit()
    return cur.rowcount > 0
