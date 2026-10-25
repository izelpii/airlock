"""
What kind of storage a path lives on, and how many reads it wants at once.

The picture import used to read with a fixed number of threads. That is right
for an SSD and the worst case for a spinning disk: eight files read at once
from a disk that serves one read at a time makes it seek between them on every
request (documentation/features/ingestion-architecture.md §3.2). This module
lets the import choose from the storage instead, and then adapt.

Linux only for classification; anything it cannot identify is 'unknown' and
handled by adapting from measurements, never by failing.
"""

import os
import threading
import time

HDD, SSD, NETWORK, UNKNOWN = 'hdd', 'ssd', 'network', 'unknown'

_NETWORK_FS = {'nfs', 'nfs4', 'cifs', 'smb3', 'smbfs', 'fuse.sshfs', '9p', 'afpfs',
               'ceph', 'glusterfs', 'fuse.rclone', 'davfs'}

# (start, maximum) concurrent reads for each class. A spinning disk may try a
# second stream - with large files and a big read-ahead two can beat one - but
# never more; SSDs and network shares hide latency with more requests in flight.
READERS = {
    HDD: (1, 2),
    SSD: (min(8, os.cpu_count() or 4), 16),
    NETWORK: (4, 16),
    UNKNOWN: (2, 8),
}

# Bytes per read. Large enough that a spinning disk spends its time transferring
# rather than seeking. Used as the copy block too.
CHUNK = {HDD: 8 << 20, SSD: 4 << 20, NETWORK: 4 << 20, UNKNOWN: 4 << 20}

# When the source and the catalog are the same spinning disk, alternate long
# bursts: read this much, then write it, so the head is not dragged between the
# two areas of the disk on every block.
SAME_DISK_BURST = 64 << 20


def _mounts():
    """(mount point, fs type, device) from /proc/mounts, longest mount point first."""
    try:
        with open('/proc/mounts', encoding='utf-8', errors='replace') as fh:
            rows = [line.split()[:3] for line in fh if line.strip()]
    except OSError:
        return []
    out = [(r[1].replace('\\040', ' '), r[2], r[0]) for r in rows if len(r) >= 3]
    return sorted(out, key=lambda r: len(r[0]), reverse=True)


def filesystem_type(path):
    """The fs type of the mount holding `path`, or None."""
    real = os.path.realpath(path)
    for mount, fstype, _dev in _mounts():
        if real == mount or real.startswith(mount.rstrip('/') + '/'):
            return fstype
    return None


def _block_queue(dev):
    """/sys/.../queue for st_dev, walking from a partition up to its disk."""
    sysdir = f'/sys/dev/block/{os.major(dev)}:{os.minor(dev)}'
    if os.path.exists(sysdir):
        real = os.path.realpath(sysdir)
        for candidate in (real, os.path.dirname(real)):
            q = os.path.join(candidate, 'queue')
            if os.path.isdir(q):
                return q
    return _block_queue_by_name(dev)


def _block_queue_by_name(dev):
    """
    The same answer by NAME, for when /sys/dev/block is empty - as it is inside
    a rootless podman container, where /sys/block still lists the disks. Without
    this, oldserver's USB hard drive classified as 'unknown' and was read with up to
    eight concurrent streams instead of two.

    /proc/partitions maps major:minor to a name (sdd1); the queue is that
    device's own (/sys/block/md126) or its disk's (/sys/block/sdd/sdd1 -> sdd).
    """
    name = None
    try:
        with open('/proc/partitions', encoding='ascii') as fh:
            for line in fh:
                parts = line.split()
                if (len(parts) == 4 and parts[0].isdigit() and parts[1].isdigit()
                        and int(parts[0]) == os.major(dev) and int(parts[1]) == os.minor(dev)):
                    name = parts[3]
                    break
    except OSError:
        return None
    if not name:
        return None
    own = os.path.join('/sys/block', name, 'queue')
    if os.path.isdir(own):
        return own
    try:
        disks = os.listdir('/sys/block')
    except OSError:
        return None
    for disk in disks:
        if os.path.isdir(os.path.join('/sys/block', disk, name)):
            q = os.path.join('/sys/block', disk, 'queue')
            return q if os.path.isdir(q) else None
    return None


def _read(path):
    try:
        with open(path, encoding='ascii') as fh:
            return fh.read().strip()
    except OSError:
        return None


def classify(path):
    """
    {'kind': hdd|ssd|network|unknown, 'device': st_dev, 'fstype': str|None,
     'rotational': bool|None}

    A software RAID or device-mapper volume reports the rotational flag of its
    own queue, which the kernel derives from its members: a RAID 0 of two SSDs
    is correctly 'ssd'.
    """
    try:
        dev = os.stat(path).st_dev
    except OSError:
        return {'kind': UNKNOWN, 'device': None, 'fstype': None, 'rotational': None}
    fstype = filesystem_type(path)
    if fstype and (fstype in _NETWORK_FS or fstype.startswith('nfs')):
        return {'kind': NETWORK, 'device': dev, 'fstype': fstype, 'rotational': None}
    queue = _block_queue(dev) if os.major(dev) else None
    rot = _read(os.path.join(queue, 'rotational')) if queue else None
    if rot == '1':
        kind = HDD
    elif rot == '0':
        kind = SSD
    else:
        kind = UNKNOWN
    return {'kind': kind, 'device': dev, 'fstype': fstype,
            'rotational': None if rot is None else rot == '1'}


def same_device(a, b):
    try:
        return os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


class AdaptiveLimiter:
    """
    A semaphore whose size tunes itself to throughput.

    Readers call acquire() before reading a file and release(nbytes) after.
    Every `window` seconds the limiter compares the bytes read in the window
    with the previous one and moves one step at a time, hill-climbing: keep
    going while throughput rises by more than `gain`, turn round when it falls.
    On a spinning disk this settles at one or two readers; on an SSD it climbs.

    fixed=True turns the tuning off (an explicit IMPORT_READERS setting).
    """

    def __init__(self, start, maximum, minimum=1, window=6.0, gain=0.08, fixed=False,
                 log=print):
        self.minimum, self.maximum = minimum, max(minimum, maximum)
        self.limit = max(self.minimum, min(start, self.maximum))
        self.window, self.gain, self.fixed, self.log = window, gain, fixed, log
        self._active = 0
        self._cond = threading.Condition()
        self._bytes = 0
        self._window_start = time.monotonic()
        self._last_rate = None
        self._direction = +1
        self.history = []          # (limit, MB/s) per window, for the report

    def acquire(self):
        with self._cond:
            while self._active >= self.limit:
                self._cond.wait()
            self._active += 1

    def release(self, nbytes=0):
        with self._cond:
            self._active -= 1
            self._bytes += nbytes
            self._maybe_adapt()
            self._cond.notify_all()

    def _maybe_adapt(self):
        now = time.monotonic()
        elapsed = now - self._window_start
        if elapsed < self.window:
            return
        rate = self._bytes / elapsed
        self.history.append((self.limit, round(rate / 1e6, 1)))
        self._bytes, self._window_start = 0, now
        if self.fixed or rate <= 0:
            return
        prev, self._last_rate = self._last_rate, rate
        if prev is None:
            step = self._direction
        elif rate > prev * (1 + self.gain):
            step = self._direction                 # it helped: keep going
        elif rate < prev * (1 - self.gain):
            self._direction = -self._direction     # it hurt: go back
            step = self._direction
        else:
            return                                 # flat: stay
        new = max(self.minimum, min(self.maximum, self.limit + step))
        if new != self.limit:
            self.log(f'  readers: {self.limit} -> {new} ({rate / 1e6:.1f} MB/s)')
            self.limit = new
