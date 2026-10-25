"""
Media type classification, in one place.

Implements documentation/specs/video-spec.md section 8.1.

Two lists of video extensions used to exist independently: catalog.py's
getFileType(), which decides whether a file is filed under Videos/, and a
proposed second list in workflows/lib/utilities.py used to decide whether a file
gets a playable proxy. They disagreed on four extensions (.3g2, .gifv, .ogv,
.vob), and a file in that gap would be filed as a video and then never
recognised as one — invisible in every gallery, with nothing logged.

One definition, imported by both, so they cannot drift again.

Deliberately dependency-free: this is imported from the backend root (catalog.py)
and from workflow subprocesses (workflows/lib/utilities.py), which have different
sys.path setups.
"""

import os
import re

# Filed under Videos/ by Catalog.getFileType(), and given a proxy + poster by the
# ingestion workflows. Keep the two uses in step by keeping the set here.
VIDEO_EXTENSIONS = {
    '.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm',
    '.m4v', '.mpg', '.mpeg', '.3gp', '.3g2', '.mts', '.m2ts',
    '.vob', '.ogv', '.gifv',
}


# True camera RAW formats — a sensor dump, not a picture a browser can show.
#
# Deliberately NARROWER than workflows/lib/utilities.py's is_raw_file(), which
# also counts .heic/.heif because those need ExifTool to read. That function
# answers "how do I extract metadata from this"; this set answers "is this a RAW
# original", which is a different question with a different answer for HEIC.
#
# Used to label an original (ORIGINAL RAW vs ORIGINAL JPG) and to decide which
# half of a RAW+JPG pair is the primary — see file-unity-spec.md section 4.3.
RAW_EXTENSIONS = {
    '.cr3', '.cr2', '.crw',        # Canon
    '.nef', '.nrw',                # Nikon
    '.arw', '.srf', '.sr2',        # Sony
    '.raf',                        # Fujifilm
    '.orf',                        # Olympus
    '.rw2',                        # Panasonic
    '.pef',                        # Pentax
    '.dng',                        # Adobe / generic
    '.raw', '.rwl', '.srw', '.x3f',
}


# Every image the importer probes and renders (workflows/lib/utilities.py
# is_image_file reads this set). Together with VIDEO_EXTENSIONS: what counts as
# a photo or video for the parts of the application that want only those -
# event extraction, for one.
IMAGE_EXTENSIONS = {
    '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif', '.webp',
    '.heic', '.heif', '.cr3', '.cr2', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2',
}


def is_media_file(file_path: str) -> bool:
    """True for a photo or a video - not an 'Others' file."""
    if not file_path:
        return False
    ext = os.path.splitext(file_path)[1].lower()
    return ext in IMAGE_EXTENSIONS or ext in VIDEO_EXTENSIONS


def is_raw_original(file_path: str) -> bool:
    """True for a camera RAW file. HEIC is NOT raw — see RAW_EXTENSIONS."""
    if not file_path:
        return False
    return os.path.splitext(file_path)[1].lower() in RAW_EXTENSIONS


def original_kind(file_path: str) -> str:
    """
    'ORIGINAL RAW' or 'ORIGINAL JPG' — the label from file-unity-spec.md 4.4.

    Derived from the extension rather than stored: the extension already answers
    it, and a stored copy would be a second source of truth to keep in step.
    """
    return 'ORIGINAL RAW' if is_raw_original(file_path) else 'ORIGINAL JPG'


def is_video_file(file_path: str) -> bool:
    """True when the extension is a recognised video format."""
    if not file_path:
        return False
    return os.path.splitext(file_path)[1].lower() in VIDEO_EXTENSIONS


# ── Files that are never media ────────────────────────────────────────────────
#
# The importer walks a folder and takes what it finds, so a camera roll copied
# off a phone brings its housekeeping with it. Those files became rows in
# file_contents: they have no date, no EXIF and nothing to show, so they padded
# the counts, appeared as blanks in galleries, and one of them was picked as an
# event cover once (event-cards-ui-spec.md section 3.3 had to work around it).
#
# The list is deliberately a DENY list, not an allow list of media extensions.
# An allow list would silently drop any format nobody thought of - a new camera
# raw, an odd container - and the failure would look identical to the file not
# being there. A file that is merely unrecognised should still be imported and
# visible; only things known to be noise are dropped.

# Sidecars and companion files written BESIDE a photo or video. Each one belongs
# to a real file that is imported on its own, so dropping them loses nothing.
IGNORED_EXTENSIONS = {
    '.aae',            # iOS "edit recipe" - the edit is already baked into IMG_E*.
    '.thm',            # camera thumbnail sidecar for a video
    '.ctg', '.ind',    # camera index/catalog files
    '.modd', '.moff',  # Sony camera sidecars
    '.xmp',            # Adobe/darktable sidecar; the raw beside it carries the image
    '.pp3', '.dop',    # RawTherapee / DxO edit sidecars
    '.lrv',            # iOS low-res proxy - we build our own
    '.sec',            # some Android camera temp
}

# Operating-system and filesystem housekeeping, matched on the WHOLE name.
IGNORED_FILENAMES = {
    '.ds_store',          # macOS Finder
    '.localized',         # macOS
    'thumbs.db',          # Windows Explorer
    'ehthumbs.db',
    'desktop.ini',        # Windows
    'picasa.ini', '.picasa.ini',
    'albumart.ini',
    'zbthumbnail.info',   # some camcorders
}

# Name prefixes that mark a file as not-content.
#   '._'  AppleDouble resource forks, created whenever a Mac writes to a
#         non-HFS volume - a USB stick or an SD card, which is exactly where
#         photos are copied from.
#   '.'   any other hidden file. Photographs are not named this way.
IGNORED_PREFIXES = ('._', '.')

# Whole names matched as patterns, for housekeeping whose names vary.
#   fseventsd-uuid, fseventsd-uuid_1   the id file macOS keeps in .fseventsd/
#   000000000662ad3b                   its event-log pages: exactly 16 hex
#                                      digits, no extension
# Normally inside the hidden .fseventsd/ folder, which the walk skips (see
# is_ignored_directory), but they turn up loose when a card's contents are
# copied out wholesale - oldserver's catalog 7 imported six of them and made an
# "event" of them. The optional _<n> is a copy tool's de-duplicating suffix.
IGNORED_NAME_PATTERNS = (
    re.compile(r'^fseventsd-uuid(_\d+)?$'),
    re.compile(r'^[0-9a-f]{16}(_\d+)?$'),
)

# Folders whose contents are never media, matched on the folder's own name.
# Any HIDDEN folder (a leading dot) covers the macOS ones - .fseventsd,
# .Spotlight-V100, .Trashes, .TemporaryItems, .DocumentRevisions-V100 - and
# this importer's own .import-staging. The rest are Windows.
IGNORED_DIRECTORY_NAMES = {
    '$recycle.bin', 'recycler', 'system volume information',
}


def is_ignored_directory(dir_name: str) -> bool:
    """True when a folder met while walking a source should not be entered."""
    lowered = (dir_name or '').lower()
    return lowered.startswith('.') or lowered in IGNORED_DIRECTORY_NAMES


def is_ignored_file(file_path: str) -> bool:
    """
    True when a file should never be imported.

    Checked before hashing, copying or metadata extraction, so an ignored file
    costs nothing and never reaches file_contents.
    """
    if not file_path:
        return True
    name = os.path.basename(file_path)
    if not name:
        return True
    lowered = name.lower()
    if lowered in IGNORED_FILENAMES:
        return True
    if any(lowered.startswith(p) for p in IGNORED_PREFIXES):
        return True
    if any(p.match(lowered) for p in IGNORED_NAME_PATTERNS):
        return True
    return os.path.splitext(lowered)[1] in IGNORED_EXTENSIONS


def ignore_reason(file_path: str) -> str:
    """A short phrase for the log, so a skipped file says why."""
    name = os.path.basename(file_path or '')
    lowered = name.lower()
    if lowered in IGNORED_FILENAMES:
        return 'operating-system file'
    if lowered.startswith('._'):
        return 'macOS resource fork'
    if lowered.startswith('.'):
        return 'hidden file'
    if any(p.match(lowered) for p in IGNORED_NAME_PATTERNS):
        return 'macOS file-system event log'
    ext = os.path.splitext(lowered)[1]
    if ext in IGNORED_EXTENSIONS:
        return f'{ext} sidecar file'
    return 'ignored'


# ── What a file really is ────────────────────────────────────────────────────
# Everything above decides by NAME, which is right for nearly every file and
# costs nothing. sniff() looks at the first bytes instead, for the files whose
# name lies. Measured on oldserver's family import (2026-10-04), where 3,094 files
# failed to render:
#   2,269  macOS AppleDouble metadata whose names had been rewritten, so the
#          '._' rule above never saw them ("20121212._2012-12-01 15.04.17.jpg")
#     721  QuickTime videos named .JPG (Live Photo clips from an export)
# Called by the import right after it has read the file, so the bytes come from
# the page cache, not another seek.

_APPLEDOUBLE_MAGIC = b'\x00\x05\x16\x07'

# ISO base media files (the 'ftyp' box) are photos OR videos: the brand says
# which. HEIC/AVIF are stills; QuickTime and MP4 are not.
_STILL_BRANDS = {'heic', 'heix', 'hevc', 'hevx', 'heim', 'heis', 'hevm', 'hevs',
                 'mif1', 'msf1', 'avif', 'avis'}
_VIDEO_BRAND_EXT = {'qt  ': '.mov', 'm4v ': '.m4v', '3gp4': '.3gp', '3gp5': '.3gp',
                    '3gp6': '.3gp', '3g2a': '.3g2'}


def sniff(path):
    """
    What the file's first bytes say it is, when that contradicts a likely name:

      ('appledouble', None)  macOS metadata, never a photo or video
      ('video', '.mov')      a video, with the extension it should have
      (None, None)           nothing to say - trust the name

    Never raises: an unreadable file is the read's problem, not this one's.
    """
    try:
        with open(path, 'rb') as f:
            head = f.read(16)
    except OSError:
        return None, None
    if head[:4] == _APPLEDOUBLE_MAGIC:
        return 'appledouble', None
    if len(head) >= 12 and head[4:8] == b'ftyp':
        brand = head[8:12].decode('latin-1').lower()
        if brand in _STILL_BRANDS:
            return None, None
        return 'video', _VIDEO_BRAND_EXT.get(brand, '.mp4')
    return None, None
