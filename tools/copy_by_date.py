#!/usr/bin/env python3
"""
Copy every file created on one date out of a directory tree, using rsync.

    ./copy_by_date.py SRC TARGET 2026-09-21

Walks SRC recursively, keeps the files whose creation date is that day, and
hands each one to rsync so permissions, timestamps and ownership survive the
copy.

-- Layout -------------------------------------------------------------------
By default everything lands FLAT in the root of TARGET, however deep it sat in
SRC. That is the whole point of the default: one day's files, in one place, no
tree to click through.

    --preserve-tree   recreate the source subdirectories under TARGET instead

Flattening is also what makes name collisions likely, so collisions are the
part of this script with real logic in it. --preserve-tree mostly removes them
- two files in one source directory cannot share a name - but the same MD5
rules still run, because TARGET may already hold a file at that same relative
path from an earlier run.

-- Collisions ---------------------------------------------------------------
Two files can want the same name in TARGET, either because two subdirectories
each hold an IMG_0001.jpg, or because TARGET already holds one from an earlier
run. The two are then compared by MD5:

    same bytes       -> only one copy is kept, and the skip is logged
    different bytes  -> both are kept, the second as name_1.ext, also logged

The comparison walks every suffixed variant already present, so a third file
whose bytes match name_1.ext is recognised as a duplicate of it rather than
being copied again as name_2.ext.

-- Why rsync, and which flags -----------------------------------------------
Metadata, which is the whole point of the spec. shutil.copy2 carries mtime and
mode; rsync -a also carries owner, group and symlink/device semantics, and
adds -H (hard links), -A (ACLs) and -X (extended attributes) where they exist.

Which of those exist depends on the binary AND on the filesystem, and neither
one can be read off a version string. Ubuntu ships full rsync 3.x and takes
-aHAX. macOS now ships openrsync, which calls itself "rsync version 2.6.9
compatible", takes -aH, and rejects -A and -X. So the script does not guess:

    1. it probes at startup by really copying a scratch file INTO THE TARGET,
       walking -aHAX -> -aHA -> -aH -> -a until one works, so the answer
       reflects the destination filesystem and not just the binary;
    2. if a copy still fails mid-run with the rich set - an ACL or xattr that
       the destination refuses - it retries that file with plain -a, says so
       loudly in the log, and stays on -a for the rest of the run.

On Ubuntu this normally settles on -aHAX and carries everything; on macOS it
settles on -aH. Use --flags to pin a set by hand and skip the probe entirely.

-- What the log holds -------------------------------------------------------
Every copy, duplicate and rename, and also every file the date filter passed
over, named with the time it was judged on and whether that time was a real
creation time or an mtime standing in for one. Those pass-over lines go to the
log file only - on a large tree they would bury the copies on screen.

-- Creation date ------------------------------------------------------------
Asked in order, best answer first:

  1. the time inside the file - EXIF DateTimeOriginal for photos, the
     QuickTime creation atoms for video. This is the only one that survives
     being copied, and it is the reason the other two are not enough: a
     filesystem birth time says when the file reached THIS disk, so on a
     server that photos are rsynced onto, every file looks created on import
     day. That is the bug this ordering fixes.
  2. the filesystem birth time - st_birthtime on macOS; on Linux, where
     Python does not expose it, GNU stat %W in batches.
  3. the modification time, announced in the log whenever it is used.

Photos and videos are opened for step 1 with exiftool when it is installed
(it reads HEIC, CR3 and RAW too, in batches). Without it, built-in stdlib
parsers still read JPEG, TIFF-based RAW and MOV/MP4, and the log names what
they could not open. --no-metadata turns step 1 off; --date-field modified
goes straight to step 3.
"""

import argparse
import collections
import hashlib
import json
import logging
import os
import re
import shutil
import struct
import subprocess
import sys
import tempfile
from datetime import datetime

LOG = logging.getLogger('copy_by_date')

# Flag sets from richest to poorest, tried in order against the real target.
# -a archive (recurse, symlinks, perms, times, group, owner), -H hard links,
# -A ACLs, -X extended attributes. Ubuntu's rsync 3.x takes the first.
RSYNC_FLAG_SETS = [
    ['-a', '-H', '-A', '-X'],
    ['-a', '-H', '-A'],
    ['-a', '-H'],
    ['-a'],
]
MINIMAL_FLAGS = ['-a']

DATE_FORMATS = ['%Y-%m-%d', '%Y/%m/%d', '%Y%m%d', '%d-%m-%Y', '%m/%d/%Y']

STAT_BATCH = 400          # paths per GNU stat call
READ_CHUNK = 1024 * 1024  # md5 read size

# Marks a log record as belonging in the file but not on screen.
FILE_ONLY = {'file_only': True}


# When a file was made, and whether that time is the real creation time or the
# modification time standing in for one. Both halves get logged.
Stamp = collections.namedtuple('Stamp', 'when source')


def describe_stamp(stamp):
    """'created 2026-09-21 14:33:02', or 'modified ...' when that is the truth."""
    if stamp is None:
        return 'time unknown'
    return '%s %s' % (stamp.source, stamp.when.strftime('%Y-%m-%d %H:%M:%S'))


class CopyError(Exception):
    """Something that must stop the run, phrased for a person."""


# ---------------------------------------------------------------- arguments

def parse_date(text):
    """Accept the date spellings a person actually types."""
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(text.strip(), fmt).date()
        except ValueError:
            continue
    raise argparse.ArgumentTypeError(
        'unrecognised date %r - use YYYY-MM-DD (e.g. 2026-09-21)' % text)


def parse_flags(text):
    """--flags 'aHAX' or '-a -H' or '-aHAX', all meaning the same thing."""
    parts = text.split()
    flags = []
    for part in parts:
        flags.append(part if part.startswith('-') else '-' + part)
    if not flags:
        raise argparse.ArgumentTypeError('--flags needs at least one flag')
    return flags


def parse_args(argv):
    parser = argparse.ArgumentParser(
        prog='copy_by_date.py',
        description='rsync every file created on DATE from SRC into TARGET, flat.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='examples:\n'
               '  ./copy_by_date.py ~/Pictures/import ~/Desktop/day 2026-09-21\n'
               '  ./copy_by_date.py /srv/in /srv/out 2026-09-21 --flags aHAX\n'
               '  ./copy_by_date.py /srv/in /srv/out 2026-09-21 --preserve-tree\n')
    parser.add_argument('src', help='source directory, searched recursively')
    parser.add_argument('target', help='target directory, created if missing')
    parser.add_argument('date', type=parse_date,
                        help='the creation date to select, YYYY-MM-DD')
    parser.add_argument('--log', metavar='PATH',
                        help='log file (default: copy_by_date_<date>_<runtime>.log '
                             'in the current directory)')
    parser.add_argument('--date-field', choices=['created', 'modified'],
                        default='created',
                        help="which timestamp counts as the file's date "
                             '(default: created)')
    parser.add_argument('--rsync', default='rsync', metavar='PATH',
                        help='rsync binary to use (default: the one on PATH)')
    parser.add_argument('--flags', type=parse_flags, metavar='FLAGS',
                        help="pin rsync flags instead of probing, e.g. --flags aHAX")
    parser.add_argument('--no-metadata', action='store_true',
                        help="do not look inside photos and videos; judge every "
                             "file by its filesystem timestamp alone")
    parser.add_argument('--exiftool', default='exiftool', metavar='PATH',
                        help='exiftool binary to use when it is installed '
                             '(default: the one on PATH)')
    parser.add_argument('--preserve-tree', action='store_true',
                        help="recreate the source's subdirectory structure under "
                             'TARGET; by default every file lands flat in the '
                             'root of TARGET')
    parser.add_argument('--dry-run', action='store_true',
                        help='decide and log everything, but copy nothing')
    return parser.parse_args(argv)


# ------------------------------------------------------------------ logging

def setup_logging(log_path, day):
    if not log_path:
        stamp = datetime.now().strftime('%Y%m%d-%H%M%S')
        log_path = 'copy_by_date_%s_%s.log' % (day.isoformat(), stamp)
    log_path = os.path.abspath(log_path)

    parent = os.path.dirname(log_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent, exist_ok=True)

    LOG.setLevel(logging.INFO)
    LOG.propagate = False

    to_file = logging.FileHandler(log_path, encoding='utf-8')
    to_file.setFormatter(logging.Formatter('%(asctime)s  %(levelname)-7s %(message)s'))
    LOG.addHandler(to_file)

    to_screen = logging.StreamHandler(sys.stdout)
    to_screen.setFormatter(logging.Formatter('%(message)s'))
    # A big tree can hold thousands of files that simply are not from the day
    # asked for. Naming each one belongs in the log; on screen it would bury
    # the copies, so those records are marked file_only and filtered here.
    to_screen.addFilter(lambda record: not getattr(record, 'file_only', False))
    LOG.addHandler(to_screen)

    return log_path


# -------------------------------------------------------------------- rsync

class Rsync:
    """The rsync binary, the flag set proven to work, and the fallback."""

    def __init__(self, binary, flags):
        self.binary = binary
        self.flags = flags
        self.degraded = False

    @classmethod
    def probe(cls, binary, target, pinned=None):
        """Settle on a flag set by really copying a scratch file into target.

        A --dry-run probe only tests whether the BINARY parses the flag; a real
        copy also tests whether the FILESYSTEM accepts it, which is the part
        that differs between an ext4 server, a NAS mount and exFAT.
        """
        resolved = shutil.which(binary)
        if resolved is None and not os.path.isfile(binary):
            raise CopyError('rsync not found as %r; install it (apt install rsync) '
                            'or pass --rsync /path/to/rsync' % binary)

        if pinned:
            LOG.info('rsync flags pinned by --flags; skipping the probe')
            return cls(binary, pinned)

        # Probe inside target when it exists, so the destination filesystem is
        # the one being tested; fall back to the system temp dir if it is not
        # writable yet (a dry run against a target that does not exist).
        parent = target if os.path.isdir(target) else None
        try:
            scratch = tempfile.mkdtemp(prefix='.copy_by_date_probe_', dir=parent)
        except OSError:
            scratch = tempfile.mkdtemp(prefix='copy_by_date_probe_')

        try:
            probe_src = os.path.join(scratch, 'probe')
            probe_dst = os.path.join(scratch, 'out')
            with open(probe_src, 'w') as handle:
                handle.write('probe\n')
            os.makedirs(probe_dst)

            for flags in RSYNC_FLAG_SETS:
                done = subprocess.run([binary] + flags + ['--', probe_src, probe_dst],
                                      capture_output=True, text=True)
                if done.returncode == 0:
                    return cls(binary, flags)
                LOG.debug('rsync rejected %s: %s', ' '.join(flags),
                          (done.stderr or '').strip())
        finally:
            shutil.rmtree(scratch, ignore_errors=True)

        raise CopyError("%s rejected even plain 'rsync -a'; it is not usable here"
                        % binary)

    def _attempt(self, flags, src_path, dest_path):
        done = subprocess.run([self.binary] + flags + ['--', src_path, dest_path],
                              capture_output=True, text=True)
        if done.returncode != 0:
            # rsync answers a bad flag with its whole usage block. Keep the
            # first line - the actual complaint - so the log stays readable.
            output = (done.stderr or done.stdout or '').strip()
            detail = output.splitlines()[0][:200] if output else '(no output)'
            raise CopyError('rsync exited %d: %s' % (done.returncode, detail))

    def copy(self, src_path, dest_path):
        """Copy one file, dropping to plain -a once if the rich set is refused.

        A destination that cannot store an ACL or an xattr fails per file, not
        at probe time - an NFS or exFAT mount under an otherwise capable
        server. Losing that metadata is better than losing the file, but it is
        a real downgrade, so it is logged and it sticks for the rest of the run.
        """
        try:
            self._attempt(self.flags, src_path, dest_path)
            return
        except CopyError as first:
            if self.flags == MINIMAL_FLAGS:
                raise
            LOG.warning('WARN    rsync %s failed on %s (%s)',
                        ' '.join(self.flags), src_path, first)
            self._attempt(MINIMAL_FLAGS, src_path, dest_path)
            LOG.warning('WARN    retry with plain -a succeeded; the rest of this run '
                        'uses -a, so ACLs and extended attributes are NOT carried')
            self.flags = MINIMAL_FLAGS
            self.degraded = True


# ------------------------------------------------------------- created dates

class DateReader:
    """Answers 'what date was this file created', as well as the OS allows.

    Three sources, in order of honesty:
      birthtime  - os.stat().st_birthtime, what macOS and the BSDs give.
      gnu-stat   - GNU stat %W, which Linux exposes via statx on ext4/xfs/btrfs
                   even though Python does not surface it there.
      mtime      - last resort, and always announced.
    """

    def __init__(self, field, sample):
        self.fallbacks = 0
        if field == 'modified':
            self.mode = 'mtime'
        elif sample is not None and hasattr(os.stat(sample), 'st_birthtime'):
            self.mode = 'birthtime'
        elif sample is not None and self._gnu_stat_works(sample):
            self.mode = 'gnu-stat'
        else:
            self.mode = 'mtime'

    @staticmethod
    def _gnu_stat_works(sample):
        """True only if GNU stat exists AND this filesystem records birth time."""
        if sys.platform == 'darwin' or shutil.which('stat') is None:
            return False
        try:
            done = subprocess.run(['stat', '--printf=%W\n', '--', sample],
                                  capture_output=True, text=True, timeout=10)
        except (OSError, subprocess.SubprocessError):
            return False
        if done.returncode != 0:
            return False
        value = done.stdout.strip()
        return value.isdigit() and int(value) > 0

    def _birth_times(self, paths):
        """GNU stat over a batch. Returns a list aligned with paths, 0 = unknown.

        If stat drops a line - an unreadable file, a newline in a name - the
        alignment is gone, so the batch is redone one path at a time rather
        than silently pairing a time with the wrong file.
        """
        try:
            done = subprocess.run(['stat', '--printf=%W\n', '--'] + paths,
                                  capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            return [0] * len(paths)

        lines = done.stdout.splitlines()
        if len(lines) != len(paths):
            if len(paths) == 1:
                return [0]
            out = []
            for path in paths:
                out.extend(self._birth_times([path]))
            return out
        return [int(line) if line.strip().isdigit() else 0 for line in lines]

    def stamps_for(self, paths):
        """{path: Stamp} for every readable path, batching where that pays.

        The whole datetime is kept, not just the date, because the log names
        the time of every file it passes over - and each Stamp carries where
        its time came from, so a line never says "created" about an mtime.
        """
        found = {}
        unreadable = []

        if self.mode == 'gnu-stat':
            for start in range(0, len(paths), STAT_BATCH):
                chunk = paths[start:start + STAT_BATCH]
                for path, seconds in zip(chunk, self._birth_times(chunk)):
                    if seconds > 0:
                        found[path] = Stamp(datetime.fromtimestamp(seconds),
                                            'created')
            leftover = [p for p in paths if p not in found]
        else:
            leftover = list(paths)

        for path in leftover:
            try:
                info = os.stat(path)
            except OSError as problem:
                unreadable.append((path, str(problem)))
                continue
            birth = getattr(info, 'st_birthtime', None)
            if self.mode == 'birthtime' and birth is not None:
                found[path] = Stamp(datetime.fromtimestamp(birth), 'created')
            else:
                if self.mode != 'mtime':
                    self.fallbacks += 1
                found[path] = Stamp(datetime.fromtimestamp(info.st_mtime),
                                    'modified')

        return found, unreadable

    def describe(self):
        return {
            'birthtime': 'creation time (st_birthtime)',
            'gnu-stat': 'creation time (GNU stat %W)',
            'mtime': 'modification time',
        }[self.mode]


# ----------------------------------------------------- time inside the file

# What a camera writes INSIDE the file. Extensions are only a filter for which
# files are worth opening - the parsers check real magic bytes before trusting
# anything.
JPEG_SUFFIXES = {'.jpg', '.jpeg', '.jpe'}
TIFF_SUFFIXES = {'.tif', '.tiff', '.dng', '.nef', '.cr2', '.arw', '.orf',
                 '.rw2', '.pef', '.srw', '.raf'}
BMFF_SUFFIXES = {'.mov', '.mp4', '.m4v', '.3gp', '.3g2', '.qt'}
# Real media that the built-in parsers cannot open: HEIC keeps its EXIF inside
# a meta box, CR3 is Canon's own ISO-BMFF dialect, and MTS/AVI/MKV are other
# containers entirely. exiftool reads all of them.
# Containers whose EXIF block the scanner above can find without walking the
# format properly: HEIC and AVIF park it in mdat, CR3 keeps it in a CMT1 box.
SCANNABLE_SUFFIXES = {'.heic', '.heif', '.avif', '.cr3', '.png', '.webp'}
# Formats with no EXIF block to find at all. Only exiftool reads these.
EXIFTOOL_ONLY_SUFFIXES = {'.mts', '.m2ts', '.avi', '.mkv', '.webm', '.gif',
                          '.mpg', '.mpeg', '.wmv', '.flv'}
MEDIA_SUFFIXES = (JPEG_SUFFIXES | TIFF_SUFFIXES | BMFF_SUFFIXES
                  | SCANNABLE_SUFFIXES | EXIFTOOL_ONLY_SUFFIXES)
BUILTIN_SUFFIXES = (JPEG_SUFFIXES | TIFF_SUFFIXES | BMFF_SUFFIXES
                    | SCANNABLE_SUFFIXES)

# Tags to ask exiftool for, best first. DateTimeOriginal is when the shutter
# fired. CreationDate is QuickTime's Keys atom, which carries a UTC offset and
# so beats CreateDate, which does not.
EXIFTOOL_TAGS = [
    ('DateTimeOriginal', 'exif:DateTimeOriginal'),
    ('CreationDate', 'quicktime:CreationDate'),
    ('CreateDate', 'metadata:CreateDate'),
    ('MediaCreateDate', 'quicktime:MediaCreateDate'),
]

EXIF_HEADER = b'Exif\x00\x00'
BMFF_EPOCH_OFFSET = 2082844800     # 1904-01-01 -> 1970-01-01, in seconds
IMAGE_HEADER_BYTES = 1024 * 1024   # enough for an APP1 segment and then some
EXIFTOOL_BATCH = 200

# A capture time outside this window is a broken field, not a fact: cameras
# with a dead clock love 1970, and a zeroed QuickTime atom reads as 1904.
EARLIEST_SANE = datetime(1980, 1, 1)

DATE_IN_TEXT = re.compile(
    r'(\d{4})[-:](\d{2})[-:](\d{2})[T ](\d{2}):(\d{2}):(\d{2})')


def is_media(path):
    return os.path.splitext(path)[1].lower() in MEDIA_SUFFIXES


def sane_capture_time(when):
    """Guard against zeroed and garbage clock fields."""
    if when is None:
        return None
    if when < EARLIEST_SANE:
        return None
    if when > datetime.now().replace(hour=23, minute=59, second=59):
        return None
    return when


def parse_capture_text(text):
    """'2024:01:15 10:30:00' and its ISO cousins -> datetime, or None.

    Any zone offset on the end is deliberately ignored: a photo belongs to the
    day it was taken where it was taken, which is the wall clock already in
    front of the offset.
    """
    if not text:
        return None
    match = DATE_IN_TEXT.search(text)
    if not match:
        return None
    try:
        return sane_capture_time(datetime(*(int(part) for part in match.groups())))
    except ValueError:
        return None


def read_tiff_datetime(blob, base):
    """Walk a TIFF IFD chain for the best capture tag. Used for JPEG and RAW."""
    if len(blob) < base + 8:
        return None
    order = blob[base:base + 2]
    if order == b'II':
        endian = '<'
    elif order == b'MM':
        endian = '>'
    else:
        return None
    magic, first_ifd = struct.unpack(endian + 'HI', blob[base + 2:base + 8])
    if magic != 42:
        return None

    found = {}

    def walk(offset, depth):
        pos = base + offset
        if depth > 2 or pos + 2 > len(blob):
            return
        entries = struct.unpack(endian + 'H', blob[pos:pos + 2])[0]
        pos += 2
        for _ in range(entries):
            entry = blob[pos:pos + 12]
            pos += 12
            if len(entry) < 12:
                return
            tag, kind, count = struct.unpack(endian + 'HHI', entry[:8])
            raw = entry[8:12]
            if tag in (0x9003, 0x9004, 0x0132) and kind == 2 and count:
                if count <= 4:
                    value = raw[:count]
                else:
                    at = struct.unpack(endian + 'I', raw)[0] + base
                    value = blob[at:at + count]
                found.setdefault(tag, value.split(b'\x00')[0]
                                 .decode('ascii', 'ignore'))
            elif tag == 0x8769 and kind == 4:
                walk(struct.unpack(endian + 'I', raw)[0], depth + 1)

    walk(first_ifd, 0)

    for tag, label in ((0x9003, 'exif:DateTimeOriginal'),
                       (0x9004, 'exif:DateTimeDigitized'),
                       (0x0132, 'exif:DateTime')):
        when = parse_capture_text(found.get(tag))
        if when:
            return when, label
    return None


def read_jpeg_datetime(path):
    """Find the APP1 EXIF segment, then read it as TIFF."""
    with open(path, 'rb') as handle:
        blob = handle.read(IMAGE_HEADER_BYTES)
    if not blob.startswith(b'\xff\xd8'):
        return None

    pos = 2
    while pos + 4 <= len(blob):
        if blob[pos] != 0xFF:
            pos += 1
            continue
        marker = blob[pos + 1]
        if marker == 0xDA:          # start of scan: pixels from here on
            return None
        if marker in (0x01, 0xD8) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        length = struct.unpack('>H', blob[pos + 2:pos + 4])[0]
        if marker == 0xE1:
            segment = blob[pos + 4:pos + 2 + length]
            if segment.startswith(EXIF_HEADER):
                return read_tiff_datetime(segment, len(EXIF_HEADER))
        pos += 2 + length
    return None


def read_tiff_file_datetime(path):
    with open(path, 'rb') as handle:
        blob = handle.read(IMAGE_HEADER_BYTES)
    if not blob.startswith((b'II*\x00', b'MM\x00*')):
        return None
    return read_tiff_datetime(blob, 0)


def iter_boxes(handle, start, end):
    """Yield (type, payload_start, box_end) for ISO-BMFF boxes in a range."""
    pos = start
    while pos + 8 <= end:
        handle.seek(pos)
        header = handle.read(8)
        if len(header) < 8:
            return
        size, kind = struct.unpack('>I4s', header)
        head = 8
        if size == 1:                       # 64-bit size follows the type
            extended = handle.read(8)
            if len(extended) < 8:
                return
            size = struct.unpack('>Q', extended)[0]
            head = 16
        elif size == 0:                     # runs to the end of the file
            size = end - pos
        if size < head or pos + size > end:
            return
        yield kind, pos + head, pos + size
        pos += size


def read_quicktime_datetime(path):
    """Capture time of a .mov/.mp4, preferring the atom that knows its zone.

    udta/meta hold '(c)day' or com.apple.quicktime.creationdate as a real local
    timestamp with an offset. mvhd only has seconds since 1904 in UTC, so it is
    the second choice: converting it back to local time is a guess that goes
    wrong by a day for anything shot near midnight in another zone.
    """
    with open(path, 'rb') as handle:
        end = os.fstat(handle.fileno()).st_size
        moov = None
        for kind, start, stop in iter_boxes(handle, 0, end):
            if kind == b'moov':
                moov = (start, stop)
                break
        if moov is None:
            return None

        mvhd = None
        for kind, start, stop in iter_boxes(handle, *moov):
            if kind in (b'udta', b'meta'):
                handle.seek(start)
                text = handle.read(min(stop - start, 64 * 1024))
                when = parse_capture_text(text.decode('latin-1', 'ignore'))
                if when:
                    return when, 'quicktime:CreationDate'
            elif kind == b'mvhd':
                mvhd = (start, stop)

        if mvhd:
            handle.seek(mvhd[0])
            data = handle.read(min(mvhd[1] - mvhd[0], 32))
            if len(data) >= 12:
                if data[0] == 1 and len(data) >= 20:
                    seconds = struct.unpack('>Q', data[4:12])[0]
                else:
                    seconds = struct.unpack('>I', data[4:8])[0]
                when = sane_capture_time(
                    datetime.fromtimestamp(seconds - BMFF_EPOCH_OFFSET)
                    if seconds > BMFF_EPOCH_OFFSET else None)
                if when:
                    return when, 'quicktime:mvhd'
    return None


def scan_for_exif(path, limit=4 * 1024 * 1024):
    """Last-resort reader: find an EXIF block anywhere in the file's head.

    HEIC, AVIF and CR3 are all ISO-BMFF, but their capture time is not in a
    movie header - it sits in a TIFF block that an iloc/iinf table points at,
    usually parked in mdat. Walking those tables properly is a lot of code for
    one timestamp, so instead this looks for the block itself:

        'Exif\0\0' followed by a real TIFF magic   (HEIC, AVIF, PNG, WebP)
        a 'CMT1' box, which is Canon's bare EXIF IFD (CR3)

    Requiring valid TIFF magic right after the marker is what makes the scan
    safe: four arbitrary bytes almost never spell II*\0 or MM\0*, so a false
    hit would have to be a TIFF header that is not one.
    """
    try:
        with open(path, 'rb') as handle:
            blob = handle.read(limit)
    except OSError:
        return None

    for marker, skip in ((EXIF_HEADER, len(EXIF_HEADER)), (b'CMT1', 4)):
        at = blob.find(marker)
        while at != -1:
            base = at + skip
            if blob[base:base + 4] in (b'II*\x00', b'MM\x00*'):
                answer = read_tiff_datetime(blob, base)
                if answer:
                    return answer
            at = blob.find(marker, at + 1)
    return None


class MetadataReader:
    """The capture time written inside photos and videos.

    This exists because a filesystem timestamp is not the truth about a photo.
    Copying, rsyncing or downloading a file gives it a brand new birth time on
    the receiving disk, so on a server that files arrive at, every photo looks
    created the day it was imported. The camera's own EXIF is the only field
    that still says when the shutter fired.

    Two backends. exiftool if it is installed, because it reads everything
    including HEIC and RAW, and reads it in batches. Otherwise the parsers
    above, which are stdlib-only and cover JPEG, TIFF-based RAW and
    MOV/MP4 - so an Ubuntu box with no extra packages still gets real times
    for the common cases.
    """

    def __init__(self, enabled, exiftool='exiftool'):
        self.enabled = enabled
        self.exiftool = shutil.which(exiftool) if enabled else None
        self.hits = 0
        self.unsupported = 0

    def describe(self):
        if not self.enabled:
            return 'not read (--no-metadata)'
        if self.exiftool:
            return 'exiftool at %s' % self.exiftool
        return ('built-in EXIF/QuickTime reader - install exiftool '
                '(apt install libimage-exiftool-perl) for HEIC, CR3 and MTS')

    def times_for(self, paths):
        """{path: Stamp} for whatever actually carries a capture time."""
        if not self.enabled:
            return {}
        media = [p for p in paths if is_media(p)]
        if not media:
            return {}
        found = self._by_exiftool(media) if self.exiftool else self._by_parsers(media)
        self.hits = len(found)
        return found

    def _by_exiftool(self, paths):
        found = {}
        wanted = ['-' + tag for tag, _ in EXIFTOOL_TAGS]
        for start in range(0, len(paths), EXIFTOOL_BATCH):
            chunk = paths[start:start + EXIFTOOL_BATCH]
            command = ([self.exiftool, '-j', '-q', '-m',
                        '-api', 'QuickTimeUTC=1',
                        '-d', '%Y-%m-%d %H:%M:%S'] + wanted + ['--'] + chunk)
            try:
                done = subprocess.run(command, capture_output=True, text=True,
                                      timeout=300)
                entries = json.loads(done.stdout) if done.stdout.strip() else []
            except (OSError, subprocess.SubprocessError, ValueError) as problem:
                LOG.warning('WARN    exiftool failed on a batch of %d file(s): '
                            '%s', len(chunk), problem)
                continue
            for entry in entries:
                source_file = entry.get('SourceFile')
                if not source_file:
                    continue
                for tag, label in EXIFTOOL_TAGS:
                    when = parse_capture_text(entry.get(tag))
                    if when:
                        found[os.path.abspath(source_file)] = Stamp(when, label)
                        break
        return found

    def _by_parsers(self, paths):
        found = {}
        for path in paths:
            suffix = os.path.splitext(path)[1].lower()
            if suffix not in BUILTIN_SUFFIXES:
                self.unsupported += 1
                continue
            try:
                if suffix in JPEG_SUFFIXES:
                    answer = read_jpeg_datetime(path)
                elif suffix in TIFF_SUFFIXES:
                    answer = read_tiff_file_datetime(path)
                elif suffix in BMFF_SUFFIXES:
                    answer = read_quicktime_datetime(path)
                else:
                    answer = None
                if answer is None and suffix in SCANNABLE_SUFFIXES:
                    answer = scan_for_exif(path)
            except (OSError, struct.error, ValueError):
                answer = None      # an unreadable header is a fallback, not a stop
            if answer:
                found[path] = Stamp(answer[0], answer[1])
        return found


# ------------------------------------------------------------------ scanning

def link_stamp(path):
    """When a symlink itself was made - lstat, so a broken link still answers."""
    try:
        info = os.lstat(path)
    except OSError:
        return None
    birth = getattr(info, 'st_birthtime', None)
    if birth is not None:
        return Stamp(datetime.fromtimestamp(birth), 'created')
    return Stamp(datetime.fromtimestamp(info.st_mtime), 'modified')


def collect_files(src, target):
    """Every regular file under src, sorted, with what was deliberately left out.

    Symlinks are skipped rather than copied: flattening a tree breaks a
    relative link target, so a copied link would point at nothing. Anything
    inside target is skipped too, so a target nested in src cannot feed itself.
    """
    target_real = os.path.realpath(target)
    files, skipped_links = [], []

    for root, dirnames, filenames in os.walk(src):
        if os.path.realpath(root) == target_real:
            dirnames[:] = []
            continue
        dirnames.sort()
        for name in sorted(filenames):
            path = os.path.join(root, name)
            if os.path.islink(path):
                skipped_links.append(path)
            elif os.path.isfile(path):
                files.append(path)

    return files, skipped_links


# ---------------------------------------------------------------- collisions

def md5(path, cache):
    """MD5 of a file, read in chunks and remembered per run."""
    key = os.path.realpath(path)
    if key in cache:
        return cache[key]
    digest = hashlib.md5()
    with open(path, 'rb') as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK), b''):
            digest.update(chunk)
    cache[key] = digest.hexdigest()
    return cache[key]


def resolve_destination(src_path, dest_dir, cache):
    """Where this file goes, or None if an identical copy is already there.

    dest_dir is the target root when flattening, or the mirrored subdirectory
    under --preserve-tree; the collision rules are the same either way.

    Returns (dest_or_None, twin_or_None, names_collided_with). The twin is the
    existing file the source turned out to be identical to, which is what makes
    the duplicate line in the log worth reading.
    """
    base = os.path.basename(src_path)
    stem, ext = os.path.splitext(base)
    index = 0
    collisions = []

    while True:
        name = base if index == 0 else '%s_%d%s' % (stem, index, ext)
        dest = os.path.join(dest_dir, name)

        if not os.path.lexists(dest):
            return dest, None, collisions

        if os.path.isdir(dest) or os.path.islink(dest):
            collisions.append(dest)
            index += 1
            continue

        if md5(src_path, cache) == md5(dest, cache):
            return None, dest, collisions

        collisions.append(dest)
        index += 1


# ---------------------------------------------------------------------- run

def run(args):
    src = os.path.abspath(os.path.expanduser(args.src))
    target = os.path.abspath(os.path.expanduser(args.target))

    if not os.path.isdir(src):
        raise CopyError('source is not a directory: %s' % src)
    if os.path.realpath(src) == os.path.realpath(target):
        raise CopyError('source and target are the same directory: %s' % src)
    if os.path.exists(target) and not os.path.isdir(target):
        raise CopyError('target exists but is not a directory: %s' % target)

    if not os.path.isdir(target):
        if args.dry_run:
            LOG.info('would create target directory %s', target)
        else:
            os.makedirs(target, exist_ok=True)

    rsync = Rsync.probe(args.rsync, target, args.flags)

    LOG.info('=' * 78)
    LOG.info('copy_by_date  started %s', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))
    LOG.info('host        : %s (%s)', os.uname().nodename, sys.platform)
    LOG.info('source      : %s', src)
    LOG.info('target      : %s', target)
    LOG.info('date        : %s', args.date.isoformat())
    LOG.info('rsync       : %s %s', rsync.binary, ' '.join(rsync.flags))
    LOG.info('layout      : %s', 'mirroring the source subdirectories'
             if args.preserve_tree else 'flat - every file in the root of target')
    if args.dry_run:
        LOG.info('mode        : DRY RUN - nothing will be written')
    LOG.info('=' * 78)

    candidates, skipped_links = collect_files(src, target)
    reader = DateReader(args.date_field, candidates[0] if candidates else None)
    LOG.info('dated by    : %s', reader.describe())

    metadata = MetadataReader(not args.no_metadata, args.exiftool)
    LOG.info('metadata    : %s', metadata.describe())

    stamps, unreadable = reader.stamps_for(candidates)

    # The camera beats the filesystem. A birth time only records when the file
    # reached THIS disk, so on a machine that photos are copied onto it is the
    # import date; EXIF still holds the day the shutter fired. Only 'created'
    # mode does this - asking for --date-field modified means asking for the
    # filesystem's answer.
    from_metadata = {}
    if args.date_field == 'created':
        from_metadata = metadata.times_for(candidates)
        stamps.update(from_metadata)

    matches = sorted(p for p, stamp in stamps.items()
                     if stamp.when.date() == args.date)
    passed_over = sorted(p for p, stamp in stamps.items()
                         if stamp.when.date() != args.date)

    if reader.fallbacks:
        LOG.warning('NOTE    %d file(s) had no creation time recorded; their '
                    'modification time was used instead', reader.fallbacks)
    if args.date_field == 'created' and reader.mode == 'mtime':
        LOG.warning('NOTE    this system does not expose a creation time here, so '
                    'MODIFICATION time was used for every file')
    for path in skipped_links:
        LOG.warning('SKIP    symlink, not copied: %s  [%s]',
                    path, describe_stamp(link_stamp(path)))
    for path, problem in unreadable:
        LOG.error('ERROR   could not read %s: %s', path, problem)

    if from_metadata:
        LOG.info('%d file(s) dated from their own metadata, %d from the '
                 'filesystem', len(from_metadata),
                 len(stamps) - len(from_metadata))
    if metadata.unsupported:
        LOG.warning('NOTE    %d media file(s) need exiftool to read their '
                    'embedded time (HEIC, CR3, MTS and the like); the built-in '
                    'reader could not, so their filesystem time was used',
                    metadata.unsupported)

    LOG.info('scanned %d file(s); %d match %s',
             len(candidates), len(matches), args.date.isoformat())

    # Every file the date filter passed over, with the time it was judged on.
    # This is what makes an empty run readable: it answers "then what ARE the
    # dates in there?" without a second trip to the filesystem.
    if passed_over:
        LOG.info('%d file(s) skipped as not from %s, each named below with its '
                 'time (log file only)', len(passed_over), args.date.isoformat())
        for path in passed_over:
            LOG.info('NOMATCH %s  [%s]', path, describe_stamp(stamps[path]),
                     extra=FILE_ONLY)

    LOG.info('-' * 78)

    cache = {}
    copied = duplicates = renamed = failed = 0
    made_dirs = {}

    for src_path in matches:
        dest_dir = target
        if args.preserve_tree:
            relative = os.path.relpath(os.path.dirname(src_path), src)
            if relative != '.':
                dest_dir = os.path.join(target, relative)
                made_dirs[dest_dir] = os.path.dirname(src_path)

        try:
            dest, twin, collisions = resolve_destination(src_path, dest_dir, cache)
        except OSError as problem:
            failed += 1
            LOG.error('ERROR   could not compare %s: %s', src_path, problem)
            continue

        if dest is None:
            duplicates += 1
            LOG.info('DUP     %s  [%s]', src_path,
                     describe_stamp(stamps.get(src_path)))
            LOG.info('        identical (md5 %s) to %s - not copied again',
                     cache[os.path.realpath(twin)], twin)
            continue

        if collisions:
            LOG.info('RENAME  %s', src_path)
            LOG.info('        name taken by %s with different content - saving as %s',
                     ', '.join(os.path.basename(p) for p in collisions),
                     os.path.basename(dest))

        if args.dry_run:
            copied += 1
            renamed += 1 if collisions else 0
            LOG.info('WOULD   %s -> %s', src_path, dest)
            continue

        try:
            # rsync will not create the parent of a file destination.
            os.makedirs(dest_dir, exist_ok=True)
            rsync.copy(src_path, dest)
        except OSError as problem:
            failed += 1
            LOG.error('ERROR   could not create %s: %s', dest_dir, problem)
            continue
        except CopyError as problem:
            failed += 1
            LOG.error('ERROR   %s -> %s: %s', src_path, dest, problem)
            continue

        copied += 1
        renamed += 1 if collisions else 0
        LOG.info('COPIED  %s -> %s', src_path, dest)

    # rsync carried the metadata of every FILE, but the directories here were
    # made by os.makedirs and wear this moment's timestamp. Copy their mode and
    # times across too, deepest first so that fixing a parent does not re-dirty
    # the mtime of a child. Ownership is left alone: rsync -a only carries it
    # when running as root, so it is not something to half-do here.
    if args.preserve_tree and not args.dry_run and made_dirs:
        adjusted = 0
        for dest_dir in sorted(made_dirs, key=len, reverse=True):
            try:
                shutil.copystat(made_dirs[dest_dir], dest_dir)
                adjusted += 1
            except OSError as problem:
                LOG.warning('WARN    could not carry metadata onto %s: %s',
                            dest_dir, problem)
        LOG.info('carried mode and timestamps onto %d directory(ies)', adjusted)

    LOG.info('-' * 78)
    LOG.info('%s %d file(s)%s', 'would copy' if args.dry_run else 'copied',
             copied, ' (%d under a new name)' % renamed if renamed else '')
    LOG.info('skipped %d duplicate(s) by md5', duplicates)
    if passed_over:
        LOG.info('skipped %d file(s) not from %s (times are in the log file)',
                 len(passed_over), args.date.isoformat())
    if skipped_links:
        LOG.info('skipped %d symlink(s)', len(skipped_links))
    if rsync.degraded:
        LOG.warning('metadata downgraded mid-run: finished on plain -a')
    if failed:
        LOG.info('FAILED  %d file(s) - see the ERROR lines above', failed)
    LOG.info('finished %s', datetime.now().strftime('%Y-%m-%d %H:%M:%S'))

    return 1 if failed else 0


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    log_path = setup_logging(args.log, args.date)
    try:
        code = run(args)
    except CopyError as problem:
        LOG.error('ERROR   %s', problem)
        code = 2
    except KeyboardInterrupt:
        LOG.error('interrupted by user')
        code = 130
    LOG.info('log written to %s', log_path)
    return code


if __name__ == '__main__':
    sys.exit(main())
