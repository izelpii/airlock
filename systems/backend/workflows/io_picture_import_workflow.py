## Import Pictures from Directory

import errno
import os
import shutil
import sys
import sqlite3
import re
import stat
import json
import threading
import time
from datetime import datetime

# Add parent directory to path to import catalog module
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from catalog import Catalog

# Add workflows directory to path to import lib module
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Import common utilities
from lib.utilities import (
    extract_exif_data_with_exiftool,
    exif_from_exiftool_metadata,
    extract_exif_data,
    convert_to_degrees,
    calculate_sha256,
    get_file_timestamps,
    is_image_file,
    is_raw_file,
    is_heic_file,
    is_cr3_file,
    is_jpg_or_png_file,
    create_alternate_version,
    create_thumbnail,
    THUMBNAIL_SIZES,
    IMAGE_RUNGS,
    MANDATORY_RUNGS,
    rungs_for,
    is_video_file,
    is_ignored_file,
    ignore_reason,
    is_ignored_directory,
    create_video_proxy,
    create_video_poster,
    get_next_alternate_sequence,
    convert_heic_to_jpg,
    convert_cr3_to_jpg,
    resize_image_to_width,
    get_creation_date,
    format_creation_timestamp,
    copy_file_with_catalog,
    hash_and_stage,
    preserve_source_attributes,
    compute_geohash,
    resolve_master_datetime,
    usable_exif_datetime,
    _is_valid_year,
)
# Cooperative stop: read a flag, leave between two units of work. Nothing to
# checkpoint - import_items records how far every file got.
from lib.stopping import stop_requested, record_stopped, stop_message
# Deciding when several files are one photograph - see file-unity-spec.md.
from lib.grouping import read_match_keys, find_groups, match_keys, MATCH_TAGS
# The phased engine: settings, single writer, progress, pool runner
# (documentation/features/parallelism.md).
from lib.import_schema import (ensure_import_schema, PHASES, ITEM_RETENTION_DAYS,
                               release_placed_metadata, prune_old_items,
                               reclaimable_bytes)
from lib.import_pipeline import (
    Progress, Writer, run_pool, paged, chunked, connect, load_settings, now_iso,
)
# Move mode: what an import does to its source afterwards (ingest-move-spec.md).
from lib.import_move import MoveMode, OP_MOVE, PENDING_DELETE, PENDING_MOVE


# ─── Subprocess progress helpers ──────────────────────────────────────────────

def _wal_pragmas(conn, timeout_ms: int = 30000):
    """
    Best-effort connection pragmas for a catalog database.

    journal_mode is deliberately NOT allowed to raise. It is PERSISTENT - written
    into the database header when the file is created and inherited by every
    later connection - so setting it again here is redundant. It is also the
    statement most likely to fail under contention, because switching journal
    mode needs an exclusive lock and a header read. Letting it throw is how a
    transient fault killed workflows that had already done their work, with
    "disk I/O error" and "file is not a database" both raised from this one line.

    busy_timeout is set first and matters more: it makes a concurrent writer
    wait rather than fail.
    """
    try:
        conn.execute('PRAGMA busy_timeout=%d' % timeout_ms)
    except sqlite3.Error:
        pass
    try:
        return conn.execute('PRAGMA journal_mode=WAL').fetchone()
    except sqlite3.Error:
        return None


def _db_update_progress(catalog_db_path: str, execution_id: int,
                        processed: int, total: int):
    """Write progress directly to workflow_executions (used when running as subprocess)."""
    # NEVER raises. This runs from the progress callback inside the main loop,
    # so an exception here kills a workflow that is otherwise working - which is
    # exactly what happened three times: runs died on the pragma above after
    # finishing their real work. Progress reporting is not worth a job.
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
            conn.execute(
                'UPDATE workflow_executions SET processed_files=?, total_files=? WHERE id=?',
                (processed, total, execution_id)
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[progress] could not record {processed}/{total}: {exc}', file=sys.stderr)


def _db_complete(catalog_db_path: str, execution_id: int, status: str,
                 error_message: str = None, skipped_files: int = 0):
    """Mark execution finished in the DB (used when running as subprocess)."""
    # Never raises either. This is usually called FROM an exception handler, so
    # throwing here replaces the real error with a database one and loses the
    # original - and leaves the execution stuck at 'running' for good, holding a
    # concurrency slot that nothing ever frees.
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
            conn.execute(
                '''UPDATE workflow_executions
                   SET status=?, completed_at=CURRENT_TIMESTAMP,
                       error_message=?, skipped_files=?
                   WHERE id=?''',
                (status, error_message, skipped_files, execution_id)
            )
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[complete] could not mark execution {execution_id} as {status}: {exc}',
              file=sys.stderr)


# Override generate_new_filename to use organized structure
def generate_new_filename(file_path, sha256_hash, exif_data=None, source_dir=None):
    """
    Generate new filename with format: YYYYMMDDHHmmss-SHA256-original_filename.ext
    Date is extracted from directory structure, filename, EXIF, or filesystem
    
    Args:
        file_path: Original file path
        sha256_hash: Full SHA256 hash of the file
        exif_data: Optional EXIF data dictionary
        source_dir: Base source directory for extracting date from path (optional)
    
    Returns:
        Tuple of (new_filename, creation_timestamp_14digit, date_source, creation_date_obj)
        creation_date_obj is the resolved datetime, used by resolve_master_datetime.
    """
    # Get creation date using organized structure if source_dir provided
    if source_dir:
        creation_date = get_creation_date_from_organized_structure(file_path, source_dir, exif_data)
        # Determine date source for tracking.
        #
        # usable_exif_datetime(), not a truthiness check. An EXIF block whose
        # DateTimeOriginal reads '0000:00:00 00:00:00' is a non-empty string, so
        # this used to label the source "exif" for a date that does not exist -
        # and that label is what stopped resolve_master_datetime from using the
        # directory date it had already worked out on the line above. The
        # filename got 2005 and master_datetime got the day of the import.
        if usable_exif_datetime(exif_data):
            date_source = "exif"
        elif extract_date_from_directory_path(file_path, source_dir):
            date_source = "directory"
        elif extract_date_from_filename(os.path.basename(file_path)):
            date_source = "filename"
        else:
            date_source = "filesystem"
    else:
        # Fall back to original logic
        creation_date = get_creation_date(file_path, exif_data)
        date_source = "exif" if usable_exif_datetime(exif_data) else "filesystem"
    
    # Format date as YYYYMMDDHHmmss (14 digits)
    timestamp_str = format_creation_timestamp(creation_date)
    
    # Get last 10 characters of SHA256 hash
    hash_suffix = sha256_hash[-10:] if sha256_hash else '0000000000'
    
    # Get original filename with extension
    original_filename = os.path.basename(file_path)
    
    # Construct new filename with 14-digit timestamp
    new_filename = f"{timestamp_str}-{hash_suffix}-{original_filename}"
    
    return new_filename, timestamp_str, date_source, creation_date

# Structure-aware date extraction: the directory and filename tiers that the
# shared helpers in lib/utilities.py do not provide.
def extract_date_from_directory_path(file_path, source_dir):
    """
    Extract year, month, and day from directory structure
    Expected structure: source_dir/YYYY/MM/filename or source_dir/YYYY/MM/DD/filename
    
    Args:
        file_path: Full path to the file
        source_dir: Base source directory
    
    Returns:
        dict: {'year': str, 'month': str, 'day': str} or None if not found
    """
    try:
        # Get relative path from source directory
        rel_path = os.path.relpath(file_path, source_dir)
        path_parts = rel_path.split(os.sep)
        
        # Remove filename from parts
        dir_parts = path_parts[:-1]
        
        year = None
        month = None
        day = None
        
        # Find year/month[/day] folders. Every part is checked: a four-digit
        # folder name is not a year just for being four digits - an album id
        # such as "1086" would otherwise file its photos in the year 1086.
        for i, part in enumerate(dir_parts):
            if re.match(r'^\d{4}$', part) and _is_valid_year(int(part)):
                # Check if next part is a month
                if i + 1 < len(dir_parts):
                    next_part = dir_parts[i + 1]
                    if re.match(r'^\d{1,2}$', next_part) and 1 <= int(next_part) <= 12:
                        year = part
                        month = next_part.zfill(2)  # Ensure 2 digits
                        # Check if there's a day after month
                        if i + 2 < len(dir_parts):
                            day_part = dir_parts[i + 2]
                            if re.match(r'^\d{1,2}$', day_part) and 1 <= int(day_part) <= 31:
                                day = day_part.zfill(2)
                        break
        
        if year and month:
            return {'year': year, 'month': month, 'day': day}
        
        return None
    except Exception as e:
        print(f"Error extracting date from directory path {file_path}: {e}")
        return None

# A date in a file name, optionally followed by a time of day. The date is
# YYYYMMDD or YYYY-MM-DD (any one of - _ . used for both separators); the time
# is HHMM or HHMMSS, glued on or separated, with optional milliseconds:
#
#   200301010600            camera / scanner YYYYMMDDHHMM
#   20190815143022          YYYYMMDDHHMMSS
#   IMG_20190815_143022     Android
#   PXL_20230101_123456789  Pixel (HHMMSS + milliseconds)
#   2019-08-15 14.30.22     Dropbox camera uploads
#   Screenshot 2023-01-01 at 10.30.00
#
# Whole numbers only - (?<!\d) and the (?!\d) at the end - so a digit run that
# is neither 8, 12, 14 nor 17 digits long is not read as a date at all.
_FILENAME_DATETIME = re.compile(
    r'(?<!\d)(?P<y>\d{4})(?P<ds>[-_.]?)(?P<m>\d{2})(?P=ds)(?P<d>\d{2})'
    r'(?:(?P<ts>[-_ T.]|\s+at\s+)?'
    r'(?P<H>\d{2})(?P<tsep>[-_.:]?)(?P<M>\d{2})(?:(?P=tsep)(?P<S>\d{2}))?(?:\d{3})?)?'
    r'(?!\d)')
_FILENAME_YEAR_MONTH = re.compile(r'(?<!\d)(\d{4})[-_](\d{2})(?!\d)')


def extract_date_from_filename(filename):
    """
    Extract a date, and the time of day when the name carries one, from a
    file name. See _FILENAME_DATETIME for the shapes recognised; a bare
    YYYY-MM / YYYY_MM is the last resort.

    Args:
        filename: Name of the file

    Returns:
        dict: {'year', 'month', 'day', 'hour', 'minute', 'second'} as strings,
        'day' and the time fields None when absent - or None if no date found.
    """
    try:
        name_no_ext = os.path.splitext(filename)[0]

        # Every match is tried, not just the first, and the year must be
        # plausible by the same rule the stored date uses
        # (resolve_master_datetime), so the two cannot disagree. Without that a
        # Facebook download "12189981_10156158419245024_..._n.jpg" matched
        # "9981_10" as YYYY_MM and was filed in the year 9981 (oldserver, family
        # import, 2026-10-01).
        for match in _FILENAME_DATETIME.finditer(name_no_ext):
            year, month, day = match.group('y'), match.group('m'), match.group('d')
            if not _is_valid_year(int(year)):
                continue
            try:
                datetime(int(year), int(month), int(day))
            except ValueError:
                continue
            hour, minute, second = match.group('H'), match.group('M'), match.group('S')
            if hour is not None and not (int(hour) <= 23 and int(minute) <= 59
                                         and int(second or 0) <= 59):
                # Glued straight onto a compact date, an impossible time means
                # the digit run was never a timestamp: 200301019999 is not a
                # date. Behind a separator it is just a number that follows
                # one, and the date stands on its own.
                if not match.group('ds') and not match.group('ts'):
                    continue
                hour = minute = second = None
            return {'year': year, 'month': month, 'day': day,
                    'hour': hour, 'minute': minute, 'second': second}

        for match in _FILENAME_YEAR_MONTH.finditer(name_no_ext):
            year, month = match.group(1), match.group(2)
            if _is_valid_year(int(year)) and 1 <= int(month) <= 12:
                return {'year': year, 'month': month, 'day': None,
                        'hour': None, 'minute': None, 'second': None}

        return None
    except Exception as e:
        print(f"Error extracting date from filename {filename}: {e}")
        return None

def get_creation_date_from_organized_structure(file_path, source_dir, exif_data=None):
    """
    Get creation date from organized directory structure and filename
    Priority:
    1. EXIF data (if available and valid)
    2. Directory structure (year/month/day)
    3. Filename pattern
    4. File system timestamps
    
    Args:
        file_path: Full path to the file
        source_dir: Base source directory
        exif_data: EXIF data dictionary (optional)
    
    Returns:
        datetime object or None
    """
    # Try EXIF first - but only a date that is actually there. A placeholder
    # '0000:00:00 00:00:00' must fall through to the directory tier below.
    exif_dt = usable_exif_datetime(exif_data)
    if exif_dt is not None:
        return exif_dt
    
    # Try directory structure
    dir_date = extract_date_from_directory_path(file_path, source_dir)
    if dir_date and dir_date.get('year') and dir_date.get('month'):
        year = int(dir_date['year'])
        month = int(dir_date['month'])
        day = int(dir_date['day']) if dir_date.get('day') else 1
        try:
            return datetime(year, month, day, 12, 0, 0)  # Default to noon
        except ValueError:
            pass
    
    # Try filename
    filename = os.path.basename(file_path)
    file_date = extract_date_from_filename(filename)
    if file_date and file_date.get('year') and file_date.get('month'):
        year = int(file_date['year'])
        month = int(file_date['month'])
        day = int(file_date['day']) if file_date.get('day') else 1
        # The name's own time of day when it has one; noon otherwise.
        if file_date.get('hour') is not None:
            hms = (int(file_date['hour']), int(file_date['minute']),
                   int(file_date.get('second') or 0))
        else:
            hms = (12, 0, 0)
        try:
            return datetime(year, month, day, *hms)
        except ValueError:
            pass
    
    # Fall back to file system timestamp
    try:
        stat_info = os.stat(file_path)
        if hasattr(stat_info, 'st_birthtime'):
            return datetime.fromtimestamp(stat_info.st_birthtime)
        else:
            return datetime.fromtimestamp(stat_info.st_ctime)
    except:
        return datetime.now()

        return None, None, None, None

def get_creation_date(file_path, exif_data=None):
    """
    Get creation date from EXIF data or filesystem.
    Returns datetime object.
    
    Priority:
    1. EXIF creation date (for images)
    2. Filesystem creation date (birth time on macOS/Windows, ctime on Linux)
    """
    # Try EXIF data first if available
    if exif_data and exif_data.get('creation_date'):
        exif_date_str = exif_data['creation_date']
        try:
            # EXIF date format is typically "YYYY:MM:DD HH:MM:SS"
            # Parse the EXIF date
            return datetime.strptime(exif_date_str, '%Y:%m:%d %H:%M:%S')
        except (ValueError, TypeError) as e:
            print(f"Warning: Could not parse EXIF date '{exif_date_str}': {e}")
    
    # Fall back to filesystem creation date
    try:
        stat_info = os.stat(file_path)
        # Try to get birth time (creation time) - available on macOS and Windows
        if hasattr(stat_info, 'st_birthtime'):
            return datetime.fromtimestamp(stat_info.st_birthtime)
        else:
            # Fall back to ctime (metadata change time on Unix, creation time on Windows)
            return datetime.fromtimestamp(stat_info.st_ctime)
    except Exception as e:
        print(f"Warning: Could not get filesystem date for {file_path}: {e}")
        # Last resort: use current time
        return datetime.now()

def format_creation_timestamp(creation_date):
    """
    Format creation date as 14-digit timestamp: YYYYMMDDHHmmss
    
    Args:
        creation_date: datetime object
    
    Returns:
        14-digit string timestamp
    """
    return creation_date.strftime('%Y%m%d%H%M%S')

# copy_file_with_catalog lives in lib/utilities.py and is imported above. A
# byte-identical copy used to be defined here, shadowing that import - so a
# fix to one would silently miss the other (file-ingestion-spec.md 2.1).


# ═══════════════════════════════════════════════════════════════════════════════
# The phased import (documentation/features/parallelism.md)
#
#   SCAN → PROBE → RESOLVE → PLACE → RENDER
#
# Each phase moves import_items rows from one state to the next, and the next
# phase starts only when the previous one has finished for every item. Worker
# threads do the per-file work and never write to SQLite; the one Writer thread
# turns their results into batched transactions (lib/import_pipeline.py).
# ═══════════════════════════════════════════════════════════════════════════════

_PLACED_STATES = ('placed', 'rendered', 'render_skipped', 'render_failed')

# Where probe leaves the copy it makes while hashing, so that place can RENAME
# it into position. Hidden, and never a catalogue path: its files are named by
# import_items id, one folder per execution
# (documentation/features/ingestion-performance-oldserver.md).
#
# One per top-level originals folder - <catalog>/Images/.import-staging and so
# on - never one for the whole catalog: Images/ or Videos/ may be a separate
# mount (catalog_layout.py), and Linux refuses a rename across mounts even on
# the same disk. Staged under the folder a file is going to, it always stays a
# rename.
STAGING_DIRNAME = '.import-staging'
_ORIGINALS_DIRS = ('Images', 'Videos', 'Others')


def _ensure_results_schema(conn):
    """The catalogue tables this workflow writes, for a database the backend never opened."""
    results_cursor = conn.cursor()
    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_operations (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            execution_id INTEGER,
            file_id INTEGER,
            operation_type TEXT NOT NULL,
            source_path TEXT,
            target_path TEXT,
            file_size INTEGER,
            status TEXT NOT NULL,
            details TEXT,
            operation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES file_contents(id)
        )
    ''')

    # Resuming a stopped import walks the same source directory again. Without
    # this index the resume fast path is a table scan per file, which is worse
    # than the hash it exists to avoid.
    results_cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_file_ops_source '
        'ON file_operations(source_path)')

    # Ensure execution_id column exists (for backward compatibility)
    try:
        results_cursor.execute('SELECT execution_id FROM file_operations LIMIT 1')
    except sqlite3.OperationalError:
        results_cursor.execute('ALTER TABLE file_operations ADD COLUMN execution_id INTEGER')
        conn.commit()

    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS file_contents (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_path TEXT NOT NULL,
            file_name TEXT NOT NULL,
            file_size INTEGER,
            file_type TEXT,
            file_permissions TEXT,
            sha256_hash TEXT,
            file_id INTEGER,
            exif_data_id INTEGER,
            modification_time TEXT,
            access_time TEXT,
            change_time TEXT,
            creation_time TEXT,
            master_datetime TEXT,
            master_datetime_source TEXT,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (exif_data_id) REFERENCES exif_data(id)
        )
    ''')

    # The duplicate checks are `WHERE sha256_hash = ?`. Unindexed they scan
    # file_contents, so the cost of an import grows with the size of the
    # catalog it is importing into rather than with the number of files being
    # imported.
    #
    # Created here as well as in models.py because this workflow runs as its
    # own process against a database it creates itself: a catalog imported into
    # before the backend has ever opened it would otherwise do the scan.
    results_cursor.execute(
        'CREATE INDEX IF NOT EXISTS idx_fc_sha256 '
        'ON file_contents(sha256_hash)')

    # Migration: add master_datetime columns if not present in existing databases.
    # removed_at too: phase_render filters on it, and this can run before the
    # backend (models.CatalogDatabase) has ever opened the catalog.
    for _col in ('master_datetime', 'master_datetime_source', 'removed_at'):
        try:
            results_cursor.execute(f'ALTER TABLE file_contents ADD COLUMN {_col} TEXT')
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS exif_data (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER NOT NULL,
            file_name TEXT,
            creation_date TEXT,
            latitude REAL,
            longitude REAL,
            altitude REAL,
            camera_make TEXT,
            camera_model TEXT,
            lens_model TEXT,
            focal_length TEXT,
            aperture TEXT,
            iso TEXT,
            shutter_speed TEXT,
            flash TEXT,
            orientation INTEGER,
            width INTEGER,
            height INTEGER,
            raw_exif_json TEXT,
            geohash TEXT,
            extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (file_id) REFERENCES file_contents(id) ON DELETE CASCADE
        )
    ''')

    # The raw exiftool dump, in its own table, because _record_placed writes
    # it there rather than into exif_data - see the INSERT in _record_placed
    # and migrate_split_raw_exif.py. Declared here as well as in
    # CatalogDatabase.init_db() for the same reason exif_data is: an import
    # must work against a catalog database the backend has not opened.
    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS exif_raw (
            exif_id       INTEGER PRIMARY KEY,
            raw_exif_json TEXT,
            FOREIGN KEY (exif_id) REFERENCES exif_data(id) ON DELETE CASCADE
        )
    ''')

    # Migration: add geohash column if not present in existing databases
    try:
        results_cursor.execute('SELECT geohash FROM exif_data LIMIT 1')
    except Exception:
        try:
            results_cursor.execute('ALTER TABLE exif_data ADD COLUMN geohash TEXT')
            conn.commit()
        except Exception:
            pass

    results_cursor.execute('''
        CREATE TABLE IF NOT EXISTS alternative_versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            original_file_id INTEGER NOT NULL,
            alternative_file_path TEXT NOT NULL,
            alternative_file_name TEXT NOT NULL,
            alternative_file_size INTEGER,
            characteristic TEXT NOT NULL,
            width INTEGER,
            height INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (original_file_id) REFERENCES file_contents(id) ON DELETE CASCADE
        )
    ''')

    # Ensure width and height columns exist (for backward compatibility with older catalogs)
    try:
        results_cursor.execute('SELECT width, height FROM alternative_versions LIMIT 1')
    except sqlite3.OperationalError:
        try:
            results_cursor.execute('ALTER TABLE alternative_versions ADD COLUMN width INTEGER')
        except sqlite3.OperationalError:
            pass  # Column already exists
        try:
            results_cursor.execute('ALTER TABLE alternative_versions ADD COLUMN height INTEGER')
        except sqlite3.OperationalError:
            pass  # Column already exists
        conn.commit()

    ensure_import_schema(results_cursor)
    conn.commit()


def _kind(path):
    """What an item is, for choosing how to probe and whether to render."""
    if is_ignored_file(path):
        return 'ignored'
    if is_image_file(path):
        # is_raw_file, not media_types.is_raw_original: it is the test that has
        # always decided "read this with exiftool" (it includes HEIC), and the
        # grouping below is called with it exactly as PASS 1 used to be.
        return 'raw' if is_raw_file(path) else 'image'
    if is_video_file(path):
        return 'video'
    return 'other'


def _walk(source_dir, skip=(), guard=None):
    """
    Every non-directory entry under source_dir, with its stat, streamed.

    The same set os.walk reported as filenames (symlinked directories are listed
    but not followed), without first building a 300k-element list. Sorted per
    directory, so two walks of the same tree visit it in the same order.

    Hidden and system folders below the source (.fseventsd, .Spotlight-V100,
    .Trashes, $RECYCLE.BIN - media_types.is_ignored_directory) are not entered:
    everything in them is housekeeping, under names the per-file deny list
    cannot recognise. The source folder itself is always walked, whatever its
    name - the user chose it.

    `skip` names folders not to enter (move mode's DUPS/), and `guard(path,
    entry)` sees every folder before it is entered and may raise to stop the
    walk.
    """
    skip = {os.path.normpath(p) for p in skip}
    stack = [source_dir]
    while stack:
        d = stack.pop()
        try:
            with os.scandir(d) as it:
                entries = sorted(it, key=lambda e: e.name)
        except OSError as exc:
            print(f'Cannot read directory {d}: {exc}')
            continue
        yield ('dir', d, None)
        subdirs = []
        for e in entries:
            try:
                is_dir = e.is_dir()
            except OSError:
                is_dir = False
            if is_dir:
                if is_ignored_directory(e.name):
                    print(f'Skipping {e.path}: hidden or system folder')
                elif os.path.normpath(e.path) in skip:
                    print(f'Skipping {e.path}: duplicates set aside by an earlier import')
                elif not e.is_symlink():
                    if guard:
                        guard(e.path, e)
                    subdirs.append(e.path)
                continue
            try:
                st = e.stat()
            except OSError:
                st = None
            yield ('file', e.path, st)
        stack.extend(reversed(subdirs))


def _unwritable(folder):
    """
    Why nothing can be written in `folder`, or None when a file can be.

    Checked by writing one, because a folder can look fine and still refuse: on
    oldserver a catalog's Images folder was a bind mount of a directory deleted
    from the USB drive, so it listed as an empty folder and every write failed
    with "No such file or directory".
    """
    probe = os.path.join(folder, f'.airlock-write-check-{os.getpid()}')
    try:
        os.makedirs(folder, exist_ok=True)
        with open(probe, 'w'):
            pass
        os.remove(probe)
        return None
    except OSError as exc:
        why = exc.strerror or str(exc)
        if exc.errno == errno.ENOENT and os.path.isdir(folder):
            why += (' - the folder is there but cannot be written to. If it is a mount, '
                    'the directory it points at may have been deleted or moved: '
                    'remount it, then restart the backend container')
        return why


def _first_file(source_dir):
    """True as soon as the source holds any file - the empty-source check, without a full walk."""
    for kind, _, _ in _walk(source_dir):
        if kind == 'file':
            return True
    return False


# ── Per-thread resources ─────────────────────────────────────────────────────
#
# One exiftool process and one read-only connection per worker thread, never
# shared: ExifToolHelper is a pipe to one Perl process, and a sqlite3
# connection belongs to the thread that uses it.

_tls = threading.local()
_opened = []
_opened_lock = threading.Lock()
# Bumped by _close_thread_resources. A thread-local handle from an earlier
# generation was closed from another thread and must not be reused - the main
# thread in particular outlives every pool, and renders on the image lane.
_generation = 0


def _tls_get(name):
    held = getattr(_tls, name, None)
    if held is not None and held[0] == _generation:
        return held[1]
    return None


def _tls_put(name, obj):
    setattr(_tls, name, (_generation, obj))
    if obj is not None:
        with _opened_lock:
            _opened.append(obj)


def _thread_exiftool():
    if getattr(_tls, 'et_failed', False):
        return None
    et = _tls_get('et')
    if et is None:
        try:
            import exiftool
            et = exiftool.ExifToolHelper()
            et.run()
        except Exception as exc:
            print(f'  exiftool unavailable in this worker ({exc}); '
                  f'falling back to one process per file')
            _tls.et_failed = True
            return None
        _tls_put('et', et)
    return et


def _thread_reader(db_path):
    conn = _tls_get('reader')
    if conn is None:
        conn = connect(db_path)
        _tls_put('reader', conn)
    return conn


def _close_thread_resources():
    global _generation
    with _opened_lock:
        items = list(_opened)
        _opened.clear()
        _generation += 1
    for obj in items:
        try:
            if hasattr(obj, 'terminate'):
                obj.terminate()
            else:
                obj.close()
        except Exception:
            pass


def _batch_match_keys(et, paths):
    """
    MATCH_TAGS for `paths` in one exiftool call - {path: keys}.

    A failing batch is retried a file at a time. exiftool exits non-zero when
    ANY file in the batch is unreadable, and PASS 1 used to lose the grouping
    for the entire import to one such file.
    """
    if not paths:
        return {}
    if et is None:
        return read_match_keys(paths, log=lambda *a, **k: None)
    try:
        return _map_results(paths, et.get_tags(list(paths), tags=MATCH_TAGS), match_keys)
    except Exception:
        out = {}
        for p in paths:
            try:
                out.update(_map_results([p], et.get_tags([p], tags=MATCH_TAGS), match_keys))
            except Exception:
                out[p] = {}
        return out


def _batch_exiftool_metadata(et, paths):
    """The exif_data dict for each of `paths` from one exiftool call, per-file on failure."""
    if not paths:
        return {}
    if et is None:
        return {p: extract_exif_data_with_exiftool(p) for p in paths}

    def mapped(meta):
        try:
            return exif_from_exiftool_metadata(meta)
        except Exception as exc:
            print(f"Error extracting EXIF with ExifTool from {meta.get('SourceFile')}: {exc}")
            return None
    try:
        return _map_results(paths, et.get_metadata(list(paths)), mapped)
    except Exception:
        return {p: extract_exif_data_with_exiftool(p, et=et) for p in paths}


def _map_results(paths, results, fn):
    """exiftool answers in input order; SourceFile is the check, not the key."""
    out = {}
    if results and len(results) == len(paths):
        for p, meta in zip(paths, results):
            out[p] = fn(meta)
    else:
        by_source = {m.get('SourceFile'): m for m in (results or [])}
        for p in paths:
            out[p] = fn(by_source[p]) if p in by_source else None
    for p in paths:
        if out.get(p) is None and fn is match_keys:
            out[p] = {}
    return out


# ── Render workers: module-level and plain-data in, plain-data out, so the
#    same functions run in a thread or a process (IMPORT_RENDER_EXECUTOR). ──

def _creation_from_ts(creation_ts):
    try:
        return datetime.strptime(creation_ts, '%Y%m%d%H%M%S') if creation_ts else None
    except ValueError:
        return None


def render_image_task(task):
    """
    The display derivatives for one image (the body of the old loop, lines
    959-1016 before the phases, unchanged in what it produces).

    The 1200px base is mandatory and comes first; the catalog's optional rungs
    (documentation/specs/file-sizes-spec.md section 7) follow, widest last:

      * EVERY rung is rendered from the ORIGINAL, never from the base.
        Rendering 2048 from a 1200px file is upscaling;
      * the base is written first and unconditionally, so a failure in a later
        rung leaves a fully usable file;
      * thumbnails come from the base.

    Rungs already recorded for this file (`existing`, from a run that stopped
    part-way) are skipped - the output names are deterministic, so what is on
    disk is what would be written again.
    """
    target_path = task['target_path']
    target_dir = task['target_dir']
    file_id = task['file_id']
    existing = task['existing']          # rung -> [rel_path, name, width]
    name = os.path.basename(target_path)
    rows, logs = [], []
    try:
        creation = _creation_from_ts(task['creation_ts'])
        alternate_info = None
        if '1200' in existing:
            rel, alt_name, _w = existing['1200']
            alternate_info = {'path': os.path.join(target_dir, rel), 'name': alt_name}
        produced = max([w or 0 for _, _, w in existing.values()] or [0])

        for rung in rungs_for(task['rungs']):
            if rung in existing:
                continue
            spec = IMAGE_RUNGS[rung]
            info = create_alternate_version(
                target_path, target_dir, file_id,
                max_width=spec['max_width'], creation_date=creation,
                name_stem=task['derivative_stem'], suffix=spec['suffix'],
                user_orientation=task.get('user_orientation') or 1)
            if not info:
                continue
            # Never store two rungs holding the same pixels. An original
            # narrower than this rung produces a file identical to the one
            # below it, so drop it rather than shipping it twice (spec 4.9).
            if rung not in MANDATORY_RUNGS and info['width'] <= produced:
                try:
                    os.remove(info['path'])
                except OSError:
                    pass
                logs.append(f'  {name}: rung {rung} would duplicate {produced}px - not stored')
                continue
            rows.append((file_id, os.path.relpath(info['path'], target_dir), info['name'],
                         info['size'], info['characteristic'], info['width'],
                         info['height'], rung))
            produced = max(produced, info['width'])
            if rung in MANDATORY_RUNGS:
                alternate_info = info
            logs.append(f"Created alternate version (rung {rung}): {info['name']} "
                        f"({info['characteristic']}) - {info['width']}x{info['height']}px")

        if alternate_info:
            for size in THUMBNAIL_SIZES:
                if f'thumb{size}' in existing:
                    continue
                thumb = create_thumbnail(alternate_info['path'], alternate_info['name'],
                                         target_dir, size=size)
                if thumb:
                    rows.append((file_id, os.path.relpath(thumb['path'], target_dir),
                                 thumb['name'], thumb['size'], thumb['characteristic'],
                                 thumb['width'], thumb['height'], f'thumb{size}'))
                    logs.append(f"Created thumbnail: {thumb['name']} ({thumb['characteristic']}) "
                                f"- {thumb['width']}x{thumb['height']}px")
        ok = alternate_info is not None
        return {'id': task['id'], 'ok': ok, 'rows': rows, 'logs': logs,
                'duration': None,
                'error': None if ok else 'no 1200px rendering could be made'}
    except Exception as exc:
        return {'id': task['id'], 'ok': False, 'rows': rows, 'logs': logs,
                'duration': None, 'error': str(exc)}


def render_video_task(task):
    """
    A playable proxy and a poster frame (documentation/specs/video-spec.md).

    create_video_proxy returns {'action': 'passthrough'} with no path when the
    source already streams in a browser, in which case there is nothing to
    record and the frontend plays the original. The poster is registered as
    TYPE THUMBNAIL so every existing thumbnail path works for videos.
    """
    target_path = task['target_path']
    target_dir = task['target_dir']
    file_id = task['file_id']
    existing = task['existing']
    rows, logs, duration = [], [], None
    try:
        creation = _creation_from_ts(task['creation_ts'])
        if 'video_proxy' not in existing:
            proxy = create_video_proxy(target_path, target_dir, creation_date=creation,
                                       name_stem=task['derivative_stem'])
            # Recorded from the dict create_video_proxy already built, because
            # ffprobe has run by this point (video-page-spec.md section 2.7).
            if proxy and proxy.get('duration'):
                try:
                    duration = float(proxy['duration'])
                except (TypeError, ValueError):
                    pass
            if proxy and proxy.get('path'):
                rows.append((file_id, os.path.relpath(proxy['path'], target_dir), proxy['name'],
                             proxy['size'], proxy['characteristic'], proxy['width'],
                             proxy['height'], 'video_proxy'))
                logs.append(f"Created video {proxy['action']}: {proxy['name']} "
                            f"({proxy['width']}x{proxy['height']}px, {proxy['size'] // 1024} KB)")
            elif proxy:
                logs.append(f'Video already browser-playable, no proxy needed: '
                            f'{os.path.basename(target_path)}')
        poster_ok = 'video_poster' in existing
        if not poster_ok:
            poster = create_video_poster(target_path, target_dir, creation_date=creation,
                                         name_stem=task['derivative_stem'])
            if poster:
                poster_ok = True
                rows.append((file_id, os.path.relpath(poster['path'], target_dir), poster['name'],
                             poster['size'], poster['characteristic'], poster['width'],
                             poster['height'], 'video_poster'))
                logs.append(f"Created video poster: {poster['name']} ({poster['characteristic']})")
        return {'id': task['id'], 'ok': poster_ok, 'rows': rows, 'logs': logs,
                'duration': duration,
                'error': None if poster_ok else 'no poster frame could be made'}
    except Exception as exc:
        return {'id': task['id'], 'ok': False, 'rows': rows, 'logs': logs,
                'duration': duration, 'error': str(exc)}


class _ImportRun(MoveMode):
    """One execution of the phased import. Owns the writer, the counters and the phases."""

    ORIGINALS_DIRS = _ORIGINALS_DIRS

    # The image renderer phase_render hands each photo to. A module-level
    # function, so it still works with IMPORT_RENDER_EXECUTOR=process; the
    # streaming import (io_picture_import_v2_workflow.py) swaps in its own.
    image_render_task = staticmethod(render_image_task)

    def __init__(self, source_dir, target_dir, catalog, execution_id, progress_callback):
        self.source_dir = source_dir
        self.target_dir = target_dir
        self.catalog = catalog
        self.eid = execution_id
        self.db_path = os.path.join(target_dir, 'workflow_results.db')
        self.settings = load_settings()
        self.progress = Progress(execution_id, self.settings['progress_flush_seconds'],
                                 progress_callback)
        self.writer = None
        self.read = None
        self.stopped = False
        self.groups_written = 0

        # Reference mode: index the files where they are (file-ingestion-spec.md).
        # Nothing is copied, moved or renamed; only derivatives are written, and
        # they still go into the catalog.
        self.reference_mode = getattr(catalog, 'ingest_mode', 'copy') == 'reference'
        self.source_root = getattr(catalog, 'source_root', None) if self.reference_mode else None
        if self.reference_mode:
            if not self.source_root:
                raise Exception('This catalog indexes files in place but has no source '
                                'folder set, so there is nothing to make its paths '
                                'relative to.')
            self.source_root = os.path.abspath(self.source_root)
            print(f'Indexing in place: originals stay under {self.source_root}; '
                  f'previews and thumbnails go to {target_dir}')

        # Copy while hashing. Only when place would copy to the local disk:
        # reference mode copies nothing, and a remote strategy (scp, s3, ...)
        # cannot take a file by renaming it.
        self.staging = (bool(self.settings['stage_copies']) and not self.reference_mode
                        and getattr(catalog, 'file_strategy', 'local') == 'local')
        # Every place a staged copy can be: one per originals folder, plus the
        # catalog root, where runs before 2026-09-27 staged theirs.
        self.staging_roots = [os.path.join(target_dir, d, STAGING_DIRNAME)
                              for d in _ORIGINALS_DIRS] + [
                                  os.path.join(target_dir, STAGING_DIRNAME)]
        # Move mode: copy mode, then the source is emptied (lib/import_move.py).
        self._init_move_mode()

    # ── lifecycle ────────────────────────────────────────────────────────────

    def open(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        try:
            _wal_pragmas(conn, 30000)
            _ensure_results_schema(conn)
        finally:
            conn.close()
        self.writer = Writer(self.db_path, self.progress, batch=self.settings['commit_batch'])
        self._install_move_hooks()
        self.read = connect(self.db_path)
        if self.eid is None:
            # A direct call without an execution (scripts, tests) files its
            # items under 0; a previous such run's leftovers are not this run's.
            self.writer.call(lambda conn: conn.execute(
                'DELETE FROM import_items WHERE execution_id = 0'))
        else:
            # The phase rows are committed on their own, before housekeeping:
            # until they exist the Import Progress page cannot show this run,
            # and housekeeping took six minutes on homeserver's family catalog
            # after 3.6 M items had expired (2026-10-12). The scan row says
            # what is happening meanwhile.
            def _start(conn):
                self.progress.insert_rows(conn)
                conn.execute('UPDATE workflow_executions SET source_directory=? WHERE id=?',
                             (self.source_dir, self.eid))
                self.progress.phases['scan']['detail']['housekeeping'] = True
                self.progress.write_phase(conn, 'scan')
            self.writer.call(_start)

            def _housekeeping(conn):
                # ── Housekeeping, every run, before any work ──────────────
                # Two different jobs, and the first is the one that matters:
                #
                #   release  every item ALREADY IN THE CATALOG drops its
                #            metadata copy, whatever run placed it and however
                #            that run ended. This is the sweep that collects
                #            what older versions left behind - a stopped import
                #            used to keep the lot - and it is safe for exactly
                #            the reason a placed row is never adopted.
                #   prune    items of imports that ended long enough ago go
                #            entirely (retention, spec section 4.1).
                #
                # Both are indexed, and no-ops once there is nothing left to
                # collect - but the first run after a long backlog can take
                # minutes, which is why the scan row announces it.
                released = release_placed_metadata(conn)
                pruned = prune_old_items(conn)
                if released or pruned:
                    print(f'Housekeeping: released metadata on {released:,} placed '
                          f'item(s), pruned {pruned:,} expired item(s).')
                    free = reclaimable_bytes(conn)
                    if free > 1 << 30:
                        print(f'  {free / 1e9:.1f} GB of the database file is now free '
                              f'space it will reuse. To hand it back to the disk, run '
                              f'tools/reclaim_catalog_db.py while nothing '
                              f'else is using this catalog.')
                self.progress.phases['scan']['detail'].pop('housekeeping', None)
                self.progress.write_phase(conn, 'scan')
            self.writer.call(_housekeeping)
        print(f'Import settings: probe×{self.settings["probe_workers"]} '
              f'place×{self.settings["place_workers"]} '
              f'render×{self.settings["render_workers"]} '
              f'video×{self.settings["video_workers"]} '
              f'batch {self.settings["commit_batch"]}'
              + (' - copying while hashing' if self.staging else ''))

    def close(self):
        """Never raises: on the failure path it runs in a `finally`, and must not
        replace the real error with a writer one."""
        _close_thread_resources()
        try:
            if self.writer:
                self.writer.close()
        except Exception as exc:
            print(f'[writer] {exc}', file=sys.stderr)
        finally:
            if self.read:
                self.read.close()

    def stop_check(self):
        # The stop check sits between two units of work, where leaving costs
        # nothing: everything handed out is drained and committed first.
        if not self.stopped and self.eid and stop_requested(self.db_path, self.eid):
            print('\nStop requested - finishing the work in hand, then leaving.')
            self.stopped = True
        if not self.stopped:
            self._disk_check()
        return self.stopped

    def _disk_check(self):
        """
        Stop cleanly while the catalog's disk still has room, rather than run
        into it. A full disk failed 2,672 previews one by one and then the run
        itself (oldserver, 2026-10-04); stopped here, every unfinished file stays
        queued and the next run carries on. Throttled: a statvfs every 30 s.
        """
        if os.environ.get('AIRLOCK_IMPORT_DISK_GUARD', '1') == '0':
            return
        now = time.monotonic()
        if now - getattr(self, '_last_disk_check', 0.0) < 30.0:
            return
        self._last_disk_check = now
        try:
            import storage_alerts
            state = storage_alerts.disk_state(self.target_dir)
        except Exception:
            return
        if state and state['severity'] == 'critical':
            self.stop_reason = (f"the catalog's disk is almost full "
                                f"({state['free'] / storage_alerts.GB:.1f} GB free, "
                                f"{state['percent']:.0f}% used) - free space, then run "
                                f'it again')
            print(f'\nStopping: {self.stop_reason}.')
            self.stopped = True

    def begin(self, phase, total=None, workers=None):
        def _fn(conn):
            ph = self.progress.phases[phase]
            ph.update(status='running', started_at=now_iso(), total=total, workers=workers)
            self.progress.current = phase
            self.progress.dirty = True
            if self.eid is not None:
                self.progress.write_phase(conn, phase)
        self.writer.call(_fn)
        print(f'\n── Phase {PHASES.index(phase) + 1}/5: {phase}'
              + (f' - {total} item(s)' if total is not None else '')
              + (f', {workers} worker(s)' if workers else ''))

    def finish(self, phase, status='completed'):
        def _fn(conn):
            ph = self.progress.phases[phase]
            ph['status'] = status
            ph['finished_at'] = now_iso()
            if self.eid is not None:
                self.progress.write_phase(conn, phase)
                self.progress.write_snapshot(conn)
        self.writer.call(_fn)

    def mark_remaining(self, status):
        """Every phase that never ran is recorded as not having run, and why."""
        def _fn(conn):
            for p in PHASES:
                ph = self.progress.phases[p]
                if ph['status'] in ('pending', 'running'):
                    ph['status'] = status
                    if self.eid is not None:
                        self.progress.write_phase(conn, p)
        self.writer.call(_fn)

    def ids_in_state(self, state, order='id'):
        return [r[0] for r in self.read.execute(
            f'SELECT id FROM import_items WHERE execution_id=? AND state=? ORDER BY {order}',
            (self.eid_key, state))]

    @property
    def eid_key(self):
        # import_items needs an execution id; a run without one (a direct call
        # from a script) uses 0, which no real execution has.
        return self.eid if self.eid is not None else 0

    def state_counts(self):
        return {r[0]: r[1] for r in self.read.execute(
            'SELECT state, COUNT(*) FROM import_items WHERE execution_id=? GROUP BY state',
            (self.eid_key,))}

    def _file_op(self, conn, file_id, operation_type, source_path, target_path,
                 status, details, file_size=None):
        return conn.execute(
            'INSERT INTO file_operations (execution_id, file_id, operation_type, source_path, '
            'target_path, file_size, status, details) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
            (self.eid, file_id, operation_type, source_path, target_path, file_size,
             status, details)).lastrowid

    # ── Phase 1: SCAN ────────────────────────────────────────────────────────

    def _check_originals_writable(self):
        """
        Refuse to start when the catalog cannot store originals, rather than
        failing every file at place with the same error (6,150 of them on oldserver,
        2026-10-02).
        """
        if self.reference_mode or getattr(self.catalog, 'file_strategy', 'local') != 'local':
            return
        problems = [f'{os.path.join(self.target_dir, d)}: {why}'
                    for d in _ORIGINALS_DIRS
                    if (why := _unwritable(os.path.join(self.target_dir, d)))]
        if problems:
            raise Exception('This catalog cannot store files, so nothing was imported. '
                            + '; '.join(problems))

    def phase_scan(self, files):
        self.begin('scan', workers=1)
        self._check_originals_writable()
        # Move mode refuses here, before anything is read (spec section 8).
        self._move_preflight_source()
        batch = []
        left = []
        found = ignored = empty = 0

        def _insert(rows, n_ignored, n_dirs, left_ops):
            def _fn(conn):
                conn.executemany(
                    'INSERT INTO import_items (execution_id, source_path, size, mtime_ns, '
                    'kind, state) VALUES (?, ?, ?, ?, ?, ?)', rows)
                if left_ops:
                    conn.executemany(
                        'INSERT INTO file_operations (execution_id, file_id, operation_type, '
                        'source_path, target_path, file_size, status, details) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', left_ops)
                self.progress.bump('scan', done=len(rows) - n_ignored, skipped=n_ignored,
                                   dirs=n_dirs, ignored=n_ignored,
                                   **({'left_in_place': len(left_ops)} if left_ops else {}))
            self.writer.submit(_fn)

        if files is None:
            entries = (_walk(self.source_dir, skip=(self.dups_root,),
                             guard=self._check_walk_dir) if self.move_mode
                       else _walk(self.source_dir))
        else:
            def _given():
                for p in files:
                    try:
                        st = os.stat(p)
                    except OSError:
                        st = None
                    yield ('file', p, st)
            entries = _given()

        dirs = 0
        for kind, path, st in entries:
            if kind == 'dir':
                dirs += 1
                continue
            item_kind = _kind(path)
            if st is not None and st.st_size == 0 and item_kind != 'ignored':
                # An empty file is not a photo or video, whatever its name says:
                # nothing to date, render or play. Imported, it became a video
                # that never plays; deduplicated, every empty file looked like a
                # "duplicate" of every other (they share one SHA-256). Noted and
                # left where it is, in every mode.
                ignored += 1
                empty += 1
                print(f'Left in place (empty file, 0 bytes): {path}')
                left.append((self.eid, None, 'left_in_place', path, None, 0, 'skipped',
                             'empty file (0 bytes)'))
                item_kind = 'empty'
            elif item_kind == 'ignored':
                # Sidecars and operating-system files are dropped before any work -
                # before hashing, copying or metadata extraction - so they cost
                # nothing and never become a catalogue row (media_types.IGNORED_*).
                ignored += 1
                print(f'Ignored ({ignore_reason(path)}): {os.path.basename(path)}')
            elif item_kind == 'other' and self.move_mode:
                # Move mode takes photos and videos only; everything else stays
                # exactly where it is (spec section 6), where copy mode would
                # have filed it under Others/.
                item_kind = 'ignored'
                ignored += 1
                print(f'Left in place (not a photo or video): {os.path.basename(path)}')
            if item_kind == 'empty':
                item_kind = 'ignored'       # noted above; stored like any ignored file
            elif self.move_mode and item_kind == 'ignored':
                left.append(self._left_in_place_op(
                    path, 'not a photo or video' if _kind(path) == 'other'
                    else ignore_reason(path), st.st_size if st else None))
            found += 1
            batch.append((self.eid_key, path, st.st_size if st else None,
                          st.st_mtime_ns if st else None, item_kind,
                          'ignored' if item_kind == 'ignored' else 'discovered'))
            if len(batch) >= 1000:
                n_ign = sum(1 for r in batch if r[5] == 'ignored')
                _insert(batch, n_ign, dirs, left)
                batch, dirs, left = [], 0, []
                if self.stop_check():
                    break
        if batch or dirs:
            _insert(batch, sum(1 for r in batch if r[5] == 'ignored'), dirs, left)
        self.writer.flush()
        print(f'Found {found} file(s) under {self.source_dir}')
        if empty:
            print(f'{empty} empty file(s) (0 bytes) left in place - see the operation log.')

        if self.stopped:
            self.finish('scan', 'stopped')
            return

        # Resume fast path, as one statement instead of one query per file.
        #
        # file_operations records every source path this catalog has copied or
        # indexed, with its size. A match on both means "we did this file" -
        # size is the guard against a different file having taken the same
        # path since. The hash check in RESOLVE stays the authority for
        # everything else; this is a shortcut, not a replacement.
        def _resume(conn):
            n = conn.execute(
                '''UPDATE import_items SET state='already_imported'
                    WHERE execution_id=? AND state='discovered'
                      AND EXISTS (SELECT 1 FROM file_operations fo
                                   WHERE fo.source_path = import_items.source_path
                                     AND fo.file_size = import_items.size
                                     AND fo.status = 'success'
                                     AND fo.operation_type IN ('copy', 'index'))''',
                (self.eid_key,)).rowcount
            if n:
                self.progress.bump('scan', done=-n, skipped=n, already_imported=n)
            return n
        # Move mode: a source path the catalog already holds is gone from the
        # source once moved, so the shortcut above never matches a finished move
        # ('move' is not in its list on purpose). What can match is a move a
        # stopped run recorded but did not get to delete (spec section 4.1).
        resumed = 0 if self.move_mode else self.writer.call(_resume)
        if resumed:
            print(f'Already imported from these paths, skipping: {resumed} file(s)')
        self._finish_interrupted_moves()
        self._move_preflight_space()

        self.progress.total_files = found
        self.progress.phases['scan']['total'] = found
        self.finish('scan')

    # ── Phase 2: PROBE ───────────────────────────────────────────────────────

    def phase_probe(self):
        ids = self.ids_in_state('discovered')
        workers = self.settings['probe_workers']
        self.begin('probe', total=len(ids), workers=workers)
        rows = paged(self.read,
                     'SELECT id, source_path, size, mtime_ns, kind FROM import_items '
                     'WHERE id IN ({ids})', (), ids, self.settings['page_size'])
        units = chunked((dict(r) for r in rows), self.settings['probe_chunk'])
        stopped = run_pool(units, self._probe_chunk, self._on_probe_results,
                           workers, self.stop_check)
        self.writer.flush()
        _close_thread_resources()
        self.finish('probe', 'stopped' if stopped else 'completed')

    def _adopt(self, reader, item):
        """
        Probe results from an earlier run over the same, unchanged file.

        What makes resuming a stopped 300k import cheap: nothing that was
        hashed is hashed again (spec section 7.2). Placed rows are not adopted -
        their metadata is cleared once it is in exif_data, and the resume fast
        path in SCAN has already skipped them.
        """
        if item['size'] is None or item['mtime_ns'] is None:
            return None
        row = reader.execute(
            f'''SELECT sha256, metadata_json, match_keys_json, staged_path FROM import_items
                 WHERE source_path=? AND size=? AND mtime_ns=? AND probed=1
                   AND execution_id != ? AND sha256 IS NOT NULL
                   AND state NOT IN ({','.join('?' * len(_PLACED_STATES))})
                 ORDER BY id DESC LIMIT 1''',
            (item['source_path'], item['size'], item['mtime_ns'], self.eid_key,
             *_PLACED_STATES)).fetchone()
        return dict(row) if row else None

    def _probe_chunk(self, items):
        """Worker: hash, metadata and dates for a chunk. Never raises."""
        results = []
        try:
            reader = _thread_reader(self.db_path)
            fresh = []
            for it in items:
                try:
                    adopted = self._adopt(reader, it)
                except sqlite3.Error:
                    adopted = None
                it['adopted'] = adopted
                if not adopted:
                    fresh.append(it)

            # Two exiftool calls for the whole chunk, on this thread's own
            # long-lived process: grouping keys for every image, and full
            # metadata for the files PIL cannot read (RAW, HEIC, video).
            et = _thread_exiftool() if fresh else None
            keys = _batch_match_keys(
                et, [it['source_path'] for it in fresh if is_image_file(it['source_path'])])
            metas = _batch_exiftool_metadata(
                et, [it['source_path'] for it in fresh if it['kind'] in ('raw', 'video')])
        except Exception as exc:
            return [{'id': it['id'], 'ok': False, 'error': f'probe failed: {exc}',
                     'source_path': it['source_path']} for it in items]

        for it in items:
            path = it['source_path']
            try:
                adopted = it['adopted']
                staged = None
                if adopted:
                    sha = adopted['sha256']
                    metadata_json = adopted['metadata_json']
                    match_keys_json = adopted['match_keys_json']
                    exif = json.loads(metadata_json) if metadata_json else None
                    hashed = 0
                    # A stopped run's copy is as good as a new one, so resuming
                    # keeps the single read. Checked, not trusted: it may have
                    # been cleaned up or be from a file since changed.
                    staged = self._usable_staged(adopted.get('staged_path'), it['size'])
                else:
                    sha, staged = self._hash(it)
                    if not sha:
                        print(f'Warning: Could not calculate hash for {os.path.basename(path)}, '
                              f'skipping duplicate check')
                    if it['kind'] in ('raw', 'video'):
                        # VIDEO GOES THROUGH EXIFTOOL TOO: PIL cannot open a video
                        # and exiftool reads QuickTime tags natively
                        # (video-page-spec.md).
                        exif = metas.get(path)
                    elif it['kind'] == 'image':
                        exif = extract_exif_data(path)
                    else:
                        exif = None
                    metadata_json = json.dumps(exif, default=str) if exif else None
                    # Round-trip now, so a fresh probe and an adopted one hand
                    # PLACE exactly the same values.
                    exif = json.loads(metadata_json) if metadata_json else None
                    mk = keys.get(path) if is_image_file(path) else None
                    match_keys_json = json.dumps(mk) if mk is not None else None
                    hashed = (it['size'] or 0) if sha else 0

                # The date decides the filename, the dated folder it is filed
                # under, and master_datetime.
                new_filename, creation_ts, date_source, creation_obj = generate_new_filename(
                    path, sha, exif, self.source_dir)
                master_dt, master_src = resolve_master_datetime(
                    path, exif, source_dir=self.source_dir, date_source_hint=date_source,
                    precomputed_datetime=creation_obj)
                results.append({
                    'id': it['id'], 'ok': True, 'sha': sha, 'staged_path': staged,
                    'metadata_json': metadata_json,
                    'match_keys_json': match_keys_json, 'new_filename': new_filename,
                    'creation_ts': creation_ts, 'date_source': date_source,
                    'master_datetime': master_dt, 'master_dt_source': master_src,
                    'adopted': bool(adopted), 'bytes': hashed})
            except Exception as exc:
                results.append({'id': it['id'], 'ok': False, 'error': str(exc),
                                'source_path': path})
        return results

    def _staging_dir_for(self, source_path):
        """
        This execution's staging folder under the originals folder `source_path`
        will be filed in - found with the same rule calculateDir uses, so the
        staged copy and its destination are always on the same mount.
        """
        try:
            category = self.catalog._get_file_type(source_path)
        except Exception:
            category = None
        if category not in _ORIGINALS_DIRS:
            category = 'Others'
        return os.path.join(self.target_dir, category, STAGING_DIRNAME, str(self.eid_key))

    def _hash(self, item):
        """
        (sha256, staged_path) for a fresh item. staged_path is relative to the
        catalog, or None when nothing was staged.

        Staging is an optimisation, never a requirement: if the copy cannot be
        written (a full disk, say), the file is hashed the old way and place
        copies it from the source, exactly as before this existed.
        """
        path = item['source_path']
        if not self.staging:
            return calculate_sha256(path), None
        ext = os.path.splitext(path)[1].lower()
        staged_abs = os.path.join(self._staging_dir_for(path), f"{item['id']}{ext}")
        try:
            sha = hash_and_stage(path, staged_abs)
        except OSError as exc:
            print(f'Could not stage {os.path.basename(path)} ({exc}); '
                  f'it will be copied at place instead')
            return calculate_sha256(path), None
        return sha, os.path.relpath(staged_abs, self.target_dir)

    def _usable_staged(self, staged_rel, size):
        """staged_rel when that copy still exists with the source's size, else None."""
        if not staged_rel or size is None:
            return None
        try:
            return staged_rel if os.path.getsize(
                os.path.join(self.target_dir, staged_rel)) == size else None
        except OSError:
            return None

    def _on_probe_results(self, results):
        for r in results:
            if r['ok']:
                def _fn(conn, r=r):
                    conn.execute(
                        '''UPDATE import_items SET state='probed', probed=1, sha256=?,
                                  staged_path=?, metadata_json=?, match_keys_json=?,
                                  new_filename=?, creation_ts=?, date_source=?,
                                  master_datetime=?, master_dt_source=?,
                                  updated_at=CURRENT_TIMESTAMP
                            WHERE id=?''',
                        (r['sha'], r['staged_path'], r['metadata_json'], r['match_keys_json'],
                         r['new_filename'], r['creation_ts'], r['date_source'],
                         r['master_datetime'], r['master_dt_source'], r['id']))
                    self.progress.bump('probe', done=1, bytes_done=r['bytes'],
                                       adopted=1 if r['adopted'] else 0)
                self.writer.submit(_fn)
            else:
                print(f"Error processing {r['source_path']}: {r['error']}")

                def _fail(conn, r=r):
                    conn.execute(
                        "UPDATE import_items SET state='probe_failed', phase_error=?, "
                        'updated_at=CURRENT_TIMESTAMP WHERE id=?', (r['error'], r['id']))
                    self._file_op(conn, None, 'index' if self.reference_mode else 'copy',
                                  r['source_path'], '', 'failed', r['error'])
                    self.progress.bump('probe', failed=1)
                self.writer.submit(_fail)

    # ── Phase 3: RESOLVE ─────────────────────────────────────────────────────

    def phase_resolve(self):
        total = len(self.ids_in_state('probed'))
        self.begin('resolve', total=total, workers=1)
        eid = self.eid_key

        def _dedup(conn):
            # 1. Already in the catalogue.
            n_cat = conn.execute(
                '''UPDATE import_items SET state='duplicate',
                          duplicate_of = (SELECT fc.id FROM file_contents fc
                                           WHERE fc.sha256_hash = import_items.sha256
                                           ORDER BY fc.id LIMIT 1)
                    WHERE execution_id=? AND state='probed' AND sha256 IS NOT NULL
                      AND EXISTS (SELECT 1 FROM file_contents fc
                                   WHERE fc.sha256_hash = import_items.sha256)''',
                (eid,)).rowcount
            # 2. Twice in this run. The winner is the smallest source path -
            #    deterministic, where the old loop kept whichever os.walk met
            #    first.
            conn.execute('DROP TABLE IF EXISTS temp._import_dups')
            conn.execute(
                '''CREATE TEMP TABLE _import_dups AS
                   SELECT id, FIRST_VALUE(id) OVER w AS keep,
                          ROW_NUMBER() OVER w AS rn
                     FROM import_items
                    WHERE execution_id=? AND state='probed' AND sha256 IS NOT NULL
                   WINDOW w AS (PARTITION BY sha256 ORDER BY source_path, id)''', (eid,))
            n_run = conn.execute(
                '''UPDATE import_items SET state='duplicate', duplicate_item=d.keep
                     FROM temp._import_dups d
                    WHERE import_items.id = d.id AND d.rn > 1''').rowcount
            conn.execute('DROP TABLE temp._import_dups')
            # 3. Everything left is new. A NULL hash (unreadable while hashing)
            #    goes on too, as it always has: it skips the duplicate check.
            n_new = conn.execute(
                "UPDATE import_items SET state='to_place' WHERE execution_id=? AND state='probed'",
                (eid,)).rowcount
            self.progress.bump('resolve', done=n_new, skipped=n_cat + n_run,
                               catalogue_duplicates=n_cat, in_run_duplicates=n_run)
            return n_cat, n_run
        n_cat, n_run = self.writer.call(_dedup)
        if n_cat or n_run:
            print(f'Duplicates: {n_cat} already in the catalogue, {n_run} repeated in this import')
            # Their staged copies will never be placed. A duplicate revived at
            # place (its winner failed) is copied from the source instead.
            self._discard_staged("state='duplicate'")

        # 4. Grouping, over what will actually be placed. AFTER dedup, so a RAW
        #    whose JPG turned out to be a duplicate is not left as a secondary
        #    with nobody to render for it (spec P8).
        n_groups = self._plan_groups()
        # 5. The audit rows for duplicates of the catalogue. Duplicates within
        #    this run are written after PLACE, when the winner has a file id.
        self._write_duplicate_ops(catalogue=True)
        self.writer.flush()
        self.finish('resolve')
        return n_groups

    def _discard_staged_file(self, staged_rel):
        try:
            os.remove(os.path.join(self.target_dir, staged_rel))
        except OSError:
            pass

    def _discard_staged(self, where):
        """Delete the staged copies of this execution's items matching `where`."""
        rows = self.read.execute(
            f'SELECT id, staged_path FROM import_items '
            f'WHERE execution_id=? AND staged_path IS NOT NULL AND {where}',
            (self.eid_key,)).fetchall()
        if not rows:
            return 0
        for r in rows:
            try:
                os.remove(os.path.join(self.target_dir, r['staged_path']))
            except OSError:
                pass
        ids = [(r['id'],) for r in rows]
        self.writer.call(lambda conn: conn.executemany(
            'UPDATE import_items SET staged_path=NULL WHERE id=?', ids))
        return len(rows)

    def _clean_staging(self):
        """
        Remove staged copies nothing will place any more, once place has finished.

        What is left in this execution's folder belongs to items that failed.
        Folders of OTHER executions are left over from stopped or failed runs,
        and are removed too - unless that execution is still running (a second
        import into this catalog), because its place may yet want them. Before
        this point they are kept deliberately: a resume adopts them (_adopt).
        """
        roots = [r for r in self.staging_roots if os.path.isdir(r)]
        if not roots:
            return
        try:
            running = {str(r[0]) for r in self.read.execute(
                "SELECT id FROM workflow_executions WHERE status='running' AND id != ?",
                (self.eid_key,))}
        except sqlite3.Error:
            running = set()     # no executions table: a direct call, nothing else runs
        removed = 0
        for root in roots:
            for name in os.listdir(root):
                if name in running:
                    continue
                folder = os.path.join(root, name)
                try:
                    removed += sum(len(files) for _, _, files in os.walk(folder))
                except OSError:
                    pass
                shutil.rmtree(folder, ignore_errors=True)
            try:
                os.rmdir(root)      # only when nothing else is in it
            except OSError:
                pass
        self.writer.call(lambda conn: conn.execute(
            'UPDATE import_items SET staged_path=NULL '
            'WHERE execution_id=? AND staged_path IS NOT NULL', (self.eid_key,)))
        if removed:
            print(f'Removed {removed} staged cop{"y" if removed == 1 else "ies"} '
                  f'that will not be placed.')

    def _plan_groups(self):
        """
        Which files are the same photograph (file-unity-spec.md).

        Known before anything is rendered, because the answer decides who
        generates derivatives: a RAW+JPG pair needs ONE 1200px rendering, and
        making it from the CR3 costs six times what making it from the JPG does.
        """
        rows = self.read.execute(
            "SELECT id, source_path, match_keys_json FROM import_items "
            "WHERE execution_id=? AND state='to_place' AND kind IN ('image', 'raw')",
            (self.eid_key,)).fetchall()
        if not rows:
            return 0
        try:
            paths = [r['source_path'] for r in rows]
            ids = {r['source_path']: r['id'] for r in rows}
            keys = {r['source_path']: (json.loads(r['match_keys_json'])
                                       if r['match_keys_json'] else {}) for r in rows}
            found = find_groups(paths, keys, is_raw_file, is_image_file)
        except Exception as exc:
            # Never fatal. Without a plan every file is its own image, which is
            # exactly the behaviour before this feature existed.
            print(f'Grouping skipped ({exc}); importing every file as its own image.')
            return 0
        if not found:
            return 0
        updates = []
        for g in found:
            key = str(ids[g['primary']])
            info = json.dumps({'kind': g['kind'], 'match_rule': g['match_rule'],
                               'match_evidence': g['match_evidence']})
            updates.append((key, 'primary', info, ids[g['primary']]))
            updates.append((key, 'secondary', None, ids[g['secondary']]))

        def _fn(conn):
            conn.executemany('UPDATE import_items SET group_key=?, group_role=?, '
                             'group_info=? WHERE id=?', updates)
            self.progress.bump('resolve', groups=len(found))
        self.writer.submit(_fn)
        print(f'Grouping: {len(found)} image(s) arrive as two files each; '
              f'derivatives will be made once per image.')
        for g in found:
            print(f"  {os.path.basename(g['primary'])} + "
                  f"{os.path.basename(g['secondary'])}  [{g['match_rule']}]")
        return len(found)

    def _would_be_target(self, row):
        """Where a duplicate would have gone - what the skip row has always recorded."""
        if self.reference_mode:
            return row['source_path']
        try:
            return os.path.join(self.catalog.calculateDir(row['source_path'], row['creation_ts'],
                                                          type_from=row['new_filename']),
                                row['new_filename'])
        except Exception:
            return None

    def _write_duplicate_ops(self, catalogue):
        if catalogue:
            sql = ('SELECT i.id, i.source_path, i.size, i.sha256, i.new_filename, i.creation_ts, '
                   'i.duplicate_of AS existing_id, fc.file_name AS existing_name, '
                   'fc.removed_at AS existing_removed '
                   'FROM import_items i LEFT JOIN file_contents fc ON fc.id = i.duplicate_of '
                   "WHERE i.execution_id=? AND i.state='duplicate' AND i.duplicate_of IS NOT NULL")
        else:
            sql = ('SELECT i.id, i.source_path, i.size, i.sha256, i.new_filename, i.creation_ts, '
                   'w.file_id AS existing_id, w.new_filename AS existing_name, '
                   'NULL AS existing_removed '
                   'FROM import_items i JOIN import_items w ON w.id = i.duplicate_item '
                   "WHERE i.execution_id=? AND i.state='duplicate' AND i.duplicate_item IS NOT NULL")
        rows = self.read.execute(sql, (self.eid_key,)).fetchall()
        op_type = 'index' if self.reference_mode else 'skip'
        batch = []
        for r in rows:
            if self.move_mode:
                # Set aside in DUPS/ rather than left in the source (spec section 5).
                batch.append(self._duplicate_moved_op(
                    r['existing_id'], r['source_path'], r['sha256'], r['existing_name'],
                    r['existing_removed'], r['size']))
                if len(batch) >= 500:
                    self._submit_ops(batch)
                    batch = []
                continue
            print(f"Skipping duplicate file: {os.path.basename(r['source_path'])} "
                  f"(same as {r['existing_name']})")
            if self.reference_mode:
                # In reference mode the consequence differs and is worth
                # recording: the file at this path really exists and is NOT in
                # the catalog, because an identical one already is. In copy mode
                # nothing was lost - it was going to be a second copy.
                details = (f"Duplicate hash: {r['sha256']}; already indexed as "
                           f"file_contents id {r['existing_id']}")
            else:
                details = f"Duplicate hash: {r['sha256']}"
            batch.append((self.eid, r['existing_id'], op_type, r['source_path'],
                          self._would_be_target(r), None, 'skipped', details))
            if len(batch) >= 500:
                self._submit_ops(batch)
                batch = []
        if batch:
            self._submit_ops(batch)

    def _submit_ops(self, rows):
        def _fn(conn):
            conn.executemany(
                'INSERT INTO file_operations (execution_id, file_id, operation_type, '
                'source_path, target_path, file_size, status, details) '
                'VALUES (?, ?, ?, ?, ?, ?, ?, ?)', rows)
        self.writer.submit(_fn)

    # ── Phase 4: PLACE ───────────────────────────────────────────────────────

    def phase_place(self):
        ids = self.ids_in_state('to_place')
        workers = 1 if self.reference_mode else self.settings['place_workers']
        self.begin('place', total=len(ids), workers=workers)
        stopped = self._place_ids(ids, workers)

        # A duplicate whose winner failed to place is not a duplicate of
        # anything in the catalogue. The old loop imported the next copy in
        # that case; so does this, by promoting one and placing it.
        for _ in range(5):
            if stopped:
                break
            revived = self._revive_orphaned_duplicates()
            if not revived:
                break
            self.progress.phases['place']['total'] += len(revived)
            stopped = self._place_ids(revived, workers)

        if not stopped:
            self._write_duplicate_ops(catalogue=False)
        # Even after a stop: a pair whose two files are both in is a pair.
        self._write_groups()
        self.writer.flush()
        if not stopped:
            # After a stop they stay: the resume adopts them.
            self._clean_staging()
        self.finish('place', 'stopped' if stopped else 'completed')

    def _place_ids(self, ids, workers):
        rows = paged(self.read,
                     'SELECT id, source_path, size, mtime_ns, sha256, staged_path, metadata_json, '
                     'new_filename, creation_ts, date_source, master_datetime, '
                     'master_dt_source FROM import_items '
                     'WHERE id IN ({ids})', (), ids, self.settings['page_size'])
        stopped = run_pool((dict(r) for r in rows), self._place_one, self._on_place,
                           workers, self.stop_check)
        self.writer.flush()
        return stopped

    def _place_one(self, item):
        """Worker: copy (or, in reference mode, just locate) one file. Never raises."""
        path = item['source_path']
        target_path = None
        operation_type = ('index' if self.reference_mode
                          else OP_MOVE if self.move_mode else 'copy')
        source_stat = None
        rename_to = None
        try:
            if self.reference_mode:
                # No placement at all. `target_path` becomes the ORIGINAL, which
                # is what makes RENDER work unchanged: every derivative is built
                # from `target_path`, and in this mode that is the user's file.
                target_path = path
                # Stored relative to source_root, not to target_dir, so the
                # library can move by updating one field (spec section 3.2).
                stored_rel_path = os.path.relpath(path, self.source_root)
                if stored_rel_path.startswith(os.pardir + os.sep) or stored_rel_path == os.pardir:
                    # A path that escapes the root breaks containment and the
                    # movable property at once (spec 4.2).
                    raise Exception(
                        f'{path} is outside this catalog\'s source folder '
                        f'({self.source_root}). Set the workflow\'s source directory to a '
                        f'folder inside it.')
                # The user's name, untouched. Derivatives, however, DO get the
                # collision-proof name: two folders can both hold IMG_0001.jpg,
                # and their thumbnails land in the same dated cache directory.
                stored_name = os.path.basename(path)
                derivative_stem = os.path.splitext(item['new_filename'])[0]
            else:
                # target_directory/FileType/YYYY/MM/DD, from the resolved date.
                calculated_dir = self.catalog.calculateDir(
                    path, item['creation_ts'], type_from=item['new_filename'])
                target_path = os.path.join(calculated_dir, item['new_filename'])
                stored_rel_path = os.path.relpath(target_path, self.target_dir)
                stored_name = item['new_filename']
                derivative_stem = None      # the copy's own name is already unique
                rename_to = self._rename_target(target_path) if self.move_mode else None
                if rename_to:
                    # Move mode, by rename: nothing is written now. The file is
                    # recorded, and renamed into place once that commits
                    # (lib/import_move.py). Only checks here.
                    if os.path.lexists(target_path):
                        raise Exception(f'a file is already at {target_path}')
                    st = os.stat(path)
                    if item.get('mtime_ns') is not None and (
                            st.st_size, st.st_mtime_ns) != (item['size'], item['mtime_ns']):
                        raise Exception('the file changed after it was read; it was left '
                                        'in place and will be read again next time')
                    os.makedirs(os.path.dirname(rename_to), exist_ok=True)
                    if item.get('staged_path'):
                        self._discard_staged_file(item['staged_path'])
                    source_stat = (st.st_size, st.st_mtime_ns)
                elif not self._place_staged(item, target_path):
                    if not copy_file_with_catalog(path, stored_rel_path, self.catalog):
                        # Say why when it can be found out: the bare sentence
                        # hid a deleted mount behind 6,150 identical failures.
                        why = (_unwritable(os.path.dirname(target_path))
                               if getattr(self.catalog, 'file_strategy', 'local') == 'local'
                               else None)
                        raise Exception('Failed to copy file using catalog strategy'
                                        + (f': cannot write to {os.path.dirname(target_path)} '
                                           f'({why})' if why else ''))
                if self.move_mode and not rename_to:
                    # Step 3 of a move: the copy is checked before anything can
                    # delete the source (ingest-move-spec.md section 4).
                    source_stat = self._verify_placed(item, target_path)

            try:
                file_permissions = oct(os.stat(path).st_mode)[-3:]
            except OSError:
                file_permissions = None
            return {
                'ok': True, 'item': item, 'target_path': target_path,
                'stored_rel_path': stored_rel_path, 'stored_name': stored_name,
                'derivative_stem': derivative_stem, 'operation_type': operation_type,
                'file_size': os.path.getsize(path),
                'file_type': os.path.splitext(stored_name)[1].lower(),
                'file_permissions': file_permissions,
                'timestamps': get_file_timestamps(path),
                'source_stat': source_stat,
                'rename_to': rename_to if not self.reference_mode else None,
            }
        except Exception as exc:
            return {'ok': False, 'item': item, 'target_path': target_path,
                    'operation_type': operation_type, 'error': str(exc)}

    def _place_staged(self, item, target_path):
        """
        Move probe's copy of this file to target_path. False means "copy it
        from the source instead": there is no usable copy, or moving it failed.

        The copy is checked against the source's CURRENT size, so a file that
        changed since probe is copied afresh rather than filed as the old one.
        """
        path = item['source_path']
        staged_rel = item.get('staged_path')
        if not staged_rel:
            return False
        try:
            size_now = os.path.getsize(path)
        except OSError:
            size_now = None
        if not self._usable_staged(staged_rel, size_now):
            return False
        staged_abs = os.path.join(self.target_dir, staged_rel)
        try:
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            try:
                # Same filesystem: a rename, no bytes moved. Overwrites like
                # shutil.copy2 would.
                os.replace(staged_abs, target_path)
            except OSError as exc:
                if exc.errno != errno.EXDEV:
                    raise
                # The catalog spans filesystems (a mount point under it):
                # copy then delete, keeping times like copy2.
                shutil.move(staged_abs, target_path)
        except OSError as exc:
            print(f'Could not move the staged copy of {os.path.basename(path)} ({exc}); '
                  f'copying from the source instead')
            return False
        preserve_source_attributes(path, target_path)
        return True

    def _on_place(self, r):
        item = r['item']

        def _fail(conn, exc=None):
            error = str(exc) if exc is not None else r['error']
            print(f"Error processing {item['source_path']}: {error}")
            conn.execute("UPDATE import_items SET state='place_failed', phase_error=?, "
                         'updated_at=CURRENT_TIMESTAMP WHERE id=?', (error, item['id']))
            self._file_op(conn, None, r['operation_type'], item['source_path'],
                          r.get('target_path') or '', 'failed', error)
            self.progress.bump('place', failed=1)

        if not r['ok']:
            self.writer.submit(_fail)
            return
        self.writer.submit(lambda conn: self._record_placed(conn, r), on_error=_fail)

    def _record_placed(self, conn, r):
        item = r['item']
        sha = item['sha256']
        # Backstop: something else (the upload endpoint, another import) may
        # have added this content since RESOLVE looked.
        if sha:
            existing = conn.execute('SELECT id, file_path, file_name FROM file_contents '
                                    'WHERE sha256_hash=? ORDER BY id LIMIT 1', (sha,)).fetchone()
            if existing:
                if not self.reference_mode and existing['file_path'] != r['stored_rel_path']:
                    # The copy just made is an orphan. Never delete when the
                    # existing row IS this path - that file is the catalogue's.
                    try:
                        os.remove(r['target_path'])
                    except OSError:
                        pass
                conn.execute("UPDATE import_items SET state='duplicate', duplicate_of=?, "
                             'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                             (existing['id'], item['id']))
                if self.move_mode:
                    removed = conn.execute('SELECT removed_at FROM file_contents WHERE id=?',
                                           (existing['id'],)).fetchone()[0]
                    conn.execute(
                        'INSERT INTO file_operations (execution_id, file_id, operation_type, '
                        'source_path, target_path, file_size, status, details) '
                        'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                        self._duplicate_moved_op(existing['id'], item['source_path'], sha,
                                                 existing['file_name'], removed,
                                                 r.get('file_size')))
                else:
                    self._file_op(conn, existing['id'],
                                  'index' if self.reference_mode else 'skip',
                                  item['source_path'], r['target_path'], 'skipped',
                                  f'Duplicate hash: {sha}')
                print(f"Skipping duplicate file: {os.path.basename(item['source_path'])} "
                      f"(same as {existing['file_name']})")
                self.progress.bump('place', skipped=1, late_duplicates=1)
                return

        ts = r['timestamps']
        # file_path is relative either way - to target_dir when the catalog owns
        # a copy, to source_root when it indexes in place - so no absolute path
        # ever enters workflow_results.db (spec section 3.0).
        cur = conn.execute(
            '''INSERT INTO file_contents
               (file_path, file_name, file_size, file_type, file_permissions, sha256_hash, file_id,
                modification_time, access_time, change_time, creation_time,
                master_datetime, master_datetime_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
            (r['stored_rel_path'], r['stored_name'], r['file_size'], r['file_type'],
             r['file_permissions'], sha, None,
             ts['modification_time'], ts['access_time'], ts['change_time'], ts['creation_time'],
             item['master_datetime'], item['master_dt_source']))
        file_id = cur.lastrowid
        conn.execute('UPDATE file_contents SET file_id = ? WHERE id = ?', (file_id, file_id))

        exif = json.loads(item['metadata_json']) if item['metadata_json'] else None
        if exif:
            geohash_value = compute_geohash(exif.get('latitude'), exif.get('longitude'))
            # The raw dump goes to exif_raw, NOT to exif_data.raw_exif_json.
            # It averages 22 KB a row, and inside exif_data it is paid for by
            # every query that reads a whole row - which is why CREATE INDEX
            # on exif_data(file_id) could not finish on a 361 K-file catalog.
            # One row per exif_data row, keyed on its id.
            cur = conn.execute(
                '''INSERT INTO exif_data
                   (file_id, file_name, creation_date, latitude, longitude,
                    altitude, camera_make, camera_model, lens_model, focal_length,
                    aperture, iso, shutter_speed, flash, orientation, width, height,
                    geohash)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                (file_id, r['stored_name'], exif.get('creation_date'), exif.get('latitude'),
                 exif.get('longitude'), exif.get('altitude'), exif.get('camera_make'),
                 exif.get('camera_model'), exif.get('lens_model'), exif.get('focal_length'),
                 exif.get('aperture'), exif.get('iso'), exif.get('shutter_speed'),
                 exif.get('flash'), exif.get('orientation'), exif.get('width'),
                 exif.get('height'), geohash_value))
            exif_row_id = cur.lastrowid
            if exif.get('raw_exif_json'):
                conn.execute(
                    'INSERT OR REPLACE INTO exif_raw (exif_id, raw_exif_json) '
                    'VALUES (?, ?)', (exif_row_id, exif.get('raw_exif_json')))
            conn.execute('UPDATE file_contents SET exif_data_id = ? WHERE id = ?',
                         (exif_row_id, file_id))

        date_source = item['date_source']
        op_id = self._file_op(
            conn, file_id, r['operation_type'], item['source_path'], r['target_path'],
            # Move mode: recorded now, 'moved' once the source is deleted or
            # renamed in - which only happens after this transaction commits
            # (spec section 4).
            (PENDING_MOVE if r.get('rename_to') else PENDING_DELETE) if self.move_mode
            else 'success',
            f'Date source: {date_source}, SHA256: {sha}' if sha
            else f'Date source: {date_source}',
            file_size=r['file_size'])
        # metadata_json=NULL IN THE SAME STATEMENT that marks the item placed.
        #
        # Ten lines above, this transaction copied that exact JSON into
        # exif_raw.raw_exif_json, which is where it lives for good. Holding it
        # on the item as well is a second copy of the same bytes, and the
        # docstring on _adopt_probe already promises it is "cleared once it is
        # in exif_data" - this is the line that makes that true.
        #
        # Here rather than at the end of the run, because an import that is
        # stopped, killed -9 or still going never reaches the end: one 340k-file
        # import left 15.9 GB of released-in-theory metadata behind it. Clearing
        # it per item caps what the database holds mid-run at the work actually
        # in flight.
        #
        # NOT match_keys_json: the grouping pass runs after place and reads it.
        conn.execute("UPDATE import_items SET state='placed', file_id=?, target_path=?, "
                     'derivative_stem=?, staged_path=NULL, metadata_json=NULL, '
                     'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                     (file_id, r['target_path'], r['derivative_stem'], item['id']))
        self.progress.bump('place', done=1, bytes_done=0 if self.reference_mode else r['file_size'])
        print(f"{'Indexed' if self.reference_mode else 'Moved' if self.move_mode else 'Processed'}"
              f": {r['stored_name']} (date from {date_source})")
        if self.move_mode:
            # Last, so nothing after it can roll this record back.
            self._queue_source_delete(op_id, r, file_id)

    def _revive_orphaned_duplicates(self):
        rows = self.read.execute(
            '''SELECT d.id, d.sha256 FROM import_items d
                 JOIN import_items w ON w.id = d.duplicate_item
                WHERE d.execution_id=? AND d.state='duplicate' AND w.state='place_failed'
                ORDER BY d.sha256, d.source_path, d.id''', (self.eid_key,)).fetchall()
        if not rows:
            return []
        by_sha = {}
        for r in rows:
            by_sha.setdefault(r['sha256'], []).append(r['id'])
        promoted = [ids[0] for ids in by_sha.values()]

        def _fn(conn):
            for ids in by_sha.values():
                conn.execute("UPDATE import_items SET state='to_place', duplicate_item=NULL "
                             'WHERE id=?', (ids[0],))
                conn.executemany('UPDATE import_items SET duplicate_item=? WHERE id=?',
                                 [(ids[0], i) for i in ids[1:]])
            self.progress.bump('resolve', done=len(promoted), skipped=-len(promoted),
                               in_run_duplicates=-len(promoted))
        self.writer.call(_fn)
        return promoted

    def _write_groups(self):
        """
        The image_groups rows, now that both members have a file id.

        A pair where one member was a duplicate, or failed, is simply not
        recorded - one file is a whole image on its own.

        Includes pairs an earlier, interrupted run placed but never got to
        record: this run's scan skips those files as already imported, so
        nothing else would ever group them.
        """
        runs = (self.eid_key, *self._finished_import_executions(_PLACED_STATES))
        rows = self.read.execute(
            f"""SELECT i.group_key, i.group_role, i.file_id, i.group_info
                  FROM import_items i JOIN file_contents fc ON fc.id = i.file_id
                 WHERE i.execution_id IN ({','.join('?' * len(runs))})
                   AND i.group_key IS NOT NULL AND fc.image_group_id IS NULL
                   AND i.state IN ({','.join('?' * len(_PLACED_STATES))})""",
            (*runs, *_PLACED_STATES)).fetchall()
        pairs = {}
        for r in rows:
            pairs.setdefault(r['group_key'], {})[r['group_role']] = r
        complete = [p for p in pairs.values() if 'primary' in p and 'secondary' in p]
        if not complete:
            return

        def _fn(conn):
            for p in complete:
                info = json.loads(p['primary']['group_info'] or '{}')
                cur = conn.execute(
                    'INSERT INTO image_groups (primary_file_id, kind, match_rule, match_evidence) '
                    'VALUES (?, ?, ?, ?)',
                    (p['primary']['file_id'], info.get('kind', 'raw_jpeg'),
                     info.get('match_rule'), json.dumps(info.get('match_evidence'))))
                conn.execute('UPDATE file_contents SET image_group_id = ? WHERE id IN (?, ?)',
                             (cur.lastrowid, p['primary']['file_id'],
                              p['secondary']['file_id']))
            self.groups_written += len(complete)
            self.progress.bump('place', groups=len(complete))
        # Never fatal: an ungrouped pair is two tiles, which is visibly wrong
        # rather than silently wrong.
        self.writer.submit(_fn, on_error=lambda conn, exc: print(
            f'  could not record groups: {exc}'))
        self.writer.flush()
        print(f'\nGrouped {self.groups_written} image(s) from two files each.')

    # ── Phase 5: RENDER ──────────────────────────────────────────────────────

    def phase_render(self):
        # Newest first: the gallery's default order, so the first page a user
        # opens during a long import fills first.
        #
        # Includes files an EARLIER run placed but never rendered - it was
        # stopped or killed between the two phases. This run's scan rightly
        # treats those files as already imported, so nothing else would ever
        # make their thumbnails. Only runs that are no longer running: a live
        # one is rendering its own.
        finished = self._finished_import_executions()
        ids = [r[0] for r in self.read.execute(
            f'''SELECT id FROM import_items
                 WHERE state = 'placed'
                   AND execution_id IN ({','.join('?' * (len(finished) + 1))})
                   -- A file the user removed from the catalog is never
                   -- rendered again: removal deleted its derivatives on purpose.
                   AND file_id NOT IN (SELECT id FROM file_contents
                                        WHERE removed_at IS NOT NULL)
                 ORDER BY master_datetime DESC, id''', (self.eid_key, *finished))]
        total = len(ids)
        carried = self.read.execute(
            f'''SELECT COUNT(*) FROM import_items WHERE state = 'placed'
                  AND execution_id IN ({','.join('?' * (len(finished) + 1))})''',
            (-1, *finished)).fetchone()[0]
        if carried:
            print(f'Rendering {carried} file(s) an earlier, interrupted import placed '
                  f'but did not finish.')
            self.progress.phases['render']['detail']['from_earlier_runs'] = carried
        rows = []
        for r in paged(self.read,
                       'SELECT id, file_id, target_path, group_role, creation_ts, '
                       'derivative_stem FROM import_items WHERE id IN ({ids})', (),
                       ids, self.settings['page_size']):
            rows.append(dict(r))
        image_items, video_items, skip_ids = [], [], []
        for r in rows:
            tp = r['target_path'] or ''
            if r['group_role'] == 'secondary':
                # A SECONDARY generates nothing. Its group's single set of
                # derivatives comes from the primary, the JPG (spec section 6.2).
                skip_ids.append(r['id'])
            elif is_image_file(tp) and r['file_id']:
                image_items.append(r)
            elif is_video_file(tp) and r['file_id']:
                video_items.append(r)
            else:
                skip_ids.append(r['id'])
        del rows

        img_workers = self.settings['render_workers']
        vid_workers = self.settings['video_workers']
        self.begin('render', total=total, workers=img_workers + vid_workers)
        if skip_ids:
            def _skip(conn):
                conn.executemany("UPDATE import_items SET state='render_skipped', "
                                 'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                                 [(i,) for i in skip_ids])
                self.progress.bump('render', skipped=len(skip_ids))
            self.writer.submit(_skip)

        executor = None
        if self.settings['render_executor'] == 'process':
            from concurrent.futures import ProcessPoolExecutor
            executor = ProcessPoolExecutor

        results = {}

        def lane(items, fn, workers, name):
            try:
                results[name] = run_pool(self._render_tasks(items), fn, self._on_render,
                                         workers, self.stop_check, executor=executor)
            except Exception as exc:          # surfaced after join
                results[name] = exc

        video_thread = threading.Thread(
            target=lane, args=(video_items, render_video_task, vid_workers, 'video'),
            name='import-video-lane', daemon=True)
        video_thread.start()
        lane(image_items, self.image_render_task, img_workers, 'image')
        video_thread.join()
        self.writer.flush()
        for v in results.values():
            if isinstance(v, Exception):
                raise v
        stopped = any(v is True for v in results.values())
        self.finish('render', 'stopped' if stopped else 'completed')

    def _finished_import_executions(self, states=('placed',)):
        """
        Earlier executions, holding items in `states`, that are over.

        Over means a terminal status, or 'running' with a process that is
        confirmed gone: a workflow killed while the backend stayed up keeps its
        'running' row until the next backend start. Unknown and foreign stay
        untouched - rendering alongside a live run would do its work twice.
        """
        rows = self.read.execute(
            "SELECT DISTINCT e.id, e.status, e.pid FROM import_items i "
            "JOIN workflow_executions e ON e.id = i.execution_id "
            f"WHERE i.state IN ({','.join('?' * len(states))}) AND i.execution_id != ?",
            (*states, self.eid_key)).fetchall()
        finished = [r['id'] for r in rows if r['status'] != 'running']
        running = [r for r in rows if r['status'] == 'running']
        if running:
            try:
                sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                import workflow_scheduler as _ws
                owner_col = ('owner_instance' if _ws.has_owner_column(self.read)
                             else 'NULL AS owner_instance')
                for r in running:
                    owner = self.read.execute(
                        f'SELECT {owner_col} FROM workflow_executions WHERE id=?',
                        (r['id'],)).fetchone()[0]
                    if _ws.execution_state(r['pid'], r['id'], owner) == _ws.EXEC_GONE:
                        finished.append(r['id'])
            except Exception as exc:
                print(f'  could not check earlier runs ({exc}); leaving their files for later')
        return finished

    def _render_tasks(self, items):
        """Plain-dict tasks, with the rungs each file already has, read a page at a time."""
        rungs = getattr(self.catalog, 'image_rungs', None)
        rungs = list(rungs) if rungs else None
        for page in chunked(items, 500):
            existing = {}
            marks = ','.join('?' * len(page))
            for row in _thread_reader(self.db_path).execute(
                    f'SELECT original_file_id, rung, alternative_file_path, '
                    f'alternative_file_name, width FROM alternative_versions '
                    f'WHERE original_file_id IN ({marks}) AND rung IS NOT NULL',
                    [p['file_id'] for p in page]):
                existing.setdefault(row[0], {})[row[1]] = [row[2], row[3], row[4]]
            for it in page:
                yield {'id': it['id'], 'file_id': it['file_id'],
                       'target_path': it['target_path'], 'target_dir': self.target_dir,
                       'creation_ts': it['creation_ts'],
                       'derivative_stem': it['derivative_stem'], 'rungs': rungs,
                       'existing': existing.get(it['file_id'], {})}

    def _on_render(self, r):
        for line in r['logs']:
            print(line)
        if not r['ok']:
            print(f"Render failed for item {r['id']}: {r['error']}")

        def _fn(conn):
            if r['rows']:
                conn.executemany(
                    '''INSERT INTO alternative_versions
                       (original_file_id, alternative_file_path, alternative_file_name,
                        alternative_file_size, characteristic, width, height, rung)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?)''', r['rows'])
            if r['duration']:
                # Never fatal: a video with no duration shows a dash.
                try:
                    conn.execute('UPDATE file_contents SET duration_seconds = ? WHERE id = '
                                 '(SELECT file_id FROM import_items WHERE id = ?)',
                                 (r['duration'], r['id']))
                except sqlite3.Error as exc:
                    print(f'Could not record duration: {exc}')
            conn.execute('UPDATE import_items SET state=?, phase_error=?, '
                         'updated_at=CURRENT_TIMESTAMP WHERE id=?',
                         ('rendered' if r['ok'] else 'render_failed', r['error'], r['id']))
            if r['ok']:
                self.progress.bump('render', done=1)
            else:
                self.progress.bump('render', failed=1)
        self.writer.submit(_fn)

    # ── the whole run ────────────────────────────────────────────────────────

    def run(self, files, phases):
        self.open()
        if 'scan' not in phases:
            # A partial re-run (--phases) of an execution whose items exist.
            self.progress.total_files = sum(self.state_counts().values())
        for phase in PHASES:
            if self.stopped:
                break
            if phase not in phases:
                continue
            if phase == 'scan':
                self.phase_scan(files)
            else:
                getattr(self, f'phase_{phase}')()
        self.mark_remaining('stopped' if self.stopped else 'skipped')

        if not self.stopped and self.eid is not None:
            # Placed rows have their metadata in exif_data now; keep the rest
            # for the report and for adoption by a later run (spec 4.1).
            def _trim(conn):
                conn.execute(
                    'UPDATE import_items SET metadata_json=NULL, match_keys_json=NULL '
                    f"WHERE execution_id=? AND state IN ({','.join('?' * len(_PLACED_STATES))})",
                    (self.eid_key, *_PLACED_STATES))
            self.writer.call(_trim)
        return self.summary()

    def summary(self):
        # Move mode's end of run, whichever way the run ended: commit, delete
        # the sources those commits cover, then tidy the folders they emptied.
        self._settle_moves()
        self._remove_emptied_folders()
        moved = self._move_summary()
        counts = self.state_counts()

        def n(*states):
            return sum(counts.get(s, 0) for s in states)
        total = sum(counts.values())
        failed = n('probe_failed', 'place_failed')
        unfinished = n('discovered', 'probed', 'to_place')
        processed = total - unfinished
        ignored = n('ignored')
        if ignored:
            print(f'\nIgnored {ignored} non-media file(s) (sidecars, operating-system files).')
        print(f"Import summary: {n(*_PLACED_STATES)} new, {n('already_imported', 'duplicate')} "
              f"skipped, {failed} failed, {n('render_failed')} without derivatives")
        return {
            'files_processed': processed - failed,
            'files_failed': failed,
            'new_files': n(*_PLACED_STATES),
            'skipped_files': n('already_imported', 'duplicate'),
            'ignored_files': ignored,
            'grouped_images': self.groups_written,
            'render_failed': n('render_failed'),
            'stopped': self.stopped,
            **moved,
        }


def execute(source_dir, target_dir, files, progress_callback, catalog_id=None, catalog=None,
            execution_id=None, phases=None):
    """
    Import pictures from a directory: scan, probe, resolve, place, render
    (documentation/features/parallelism.md).

    Copy files from an organized directory structure (or index them in place),
    extract date from EXIF/directory/filename, extract EXIF data, and record
    everything in the catalog database.

    Expected directory structure: source_dir/YYYY/MM/filename or source_dir/YYYY/MM/DD/filename
    Date extraction priority: EXIF > Directory structure > Filename pattern > Filesystem

    Args:
        source_dir: Source directory path (organized by year/month)
        target_dir: Target directory path (catalog target directory)
        files: List of file paths to process, or None to walk source_dir
        progress_callback: Optional callback(processed, total). Called at most
            once per progress flush, never per file. The subprocess entry point
            passes None: progress is written to workflow_executions directly.
        catalog_id: ID of the catalog (deprecated, use catalog parameter)
        catalog: Catalog instance to use for file operations (recommended)
        execution_id: ID of the workflow execution (for tracking)
        phases: Phases to run (default all). A subset re-runs part of an
            existing execution, e.g. ('render',).

    Returns:
        dict: Statistics about the execution including files_processed and files_failed
    """
    # If catalog is not provided, create one from catalog_id
    if catalog is None and catalog_id is not None:
        catalog = Catalog.get_by_id(catalog_id)
        if catalog is None:
            raise ValueError(f"Catalog with ID {catalog_id} not found")
    if catalog is None:
        raise ValueError("Either catalog or catalog_id must be provided")

    phases = tuple(phases) if phases else PHASES
    run = _ImportRun(source_dir, target_dir, catalog, execution_id, progress_callback)
    try:
        result = run.run(files, phases)
    except BaseException:
        # Whatever phase was running did not finish; say so, then let the
        # caller record the failure as it always has. Written on a fresh
        # connection because the writer may be the thing that died.
        _mark_running_phases(run.db_path, execution_id, 'failed')
        raise
    finally:
        run.close()

    if result['stopped'] and execution_id:
        # Written here rather than by the caller: only this function knows how
        # far it got, and the caller's _db_complete would record 'completed'.
        record_stopped(run.db_path, execution_id,
                       processed_files=run.progress.processed_files(),
                       skipped_files=result['skipped_files'],
                       message=stop_message(run.progress.processed_files(),
                                            run.progress.total_files,
                                            getattr(run, 'stop_reason', None)))
    return result


def _mark_running_phases(db_path, execution_id, status):
    if execution_id is None:
        return
    try:
        conn = sqlite3.connect(db_path, timeout=30)
        try:
            conn.execute('UPDATE import_phases SET status=?, finished_at=? '
                         "WHERE execution_id=? AND status IN ('running', 'pending')",
                         (status, now_iso(), execution_id))
            conn.commit()
        finally:
            conn.close()
    except sqlite3.Error as exc:
        print(f'[phases] could not mark execution {execution_id} as {status}: {exc}',
              file=sys.stderr)


# ─── Standalone subprocess entry point ────────────────────────────────────────

def main(execute_fn=None, description='Picture import workflow (subprocess mode)'):
    """
    The subprocess entry point, shared with io_picture_import_v2_workflow.py,
    which passes its own execute_fn: the route re-check, the empty-source check
    and how completion and stops are recorded must not differ between the two.

    When launched as a subprocess by the backend, arguments are:
        --execution-id  <int>   workflow_executions.id
        --source-dir    <path>  source directory to import from
        --target-dir    <path>  catalog target directory (contains workflow_results.db)
        --catalog-id    <int>   catalog id
        --phases        <list>  optional, e.g. "render" to re-render an execution's files
    """
    import argparse

    parser = argparse.ArgumentParser(description=description)
    parser.add_argument('--execution-id', type=int, required=True)
    parser.add_argument('--source-dir', type=str, required=True)
    parser.add_argument('--target-dir', type=str, required=True)
    parser.add_argument('--catalog-id', type=int, required=True)
    parser.add_argument('--route-id', type=int, default=None,
                        help='storage route this source came from. Re-checked here '
                             'so an unplugged drive fails loudly, and named in any '
                             'error rather than shown as a raw path.')
    parser.add_argument('--phases', type=str, default=None,
                        help='comma-separated subset of ' + ','.join(PHASES) +
                             '; default all. Not exposed in the UI.')
    args = parser.parse_args()

    catalog_db_path = os.path.join(args.target_dir, 'workflow_results.db')

    # ── Storage route re-check ────────────────────────────────────────────────
    # The launcher already resolved this route when it built the command. It is
    # checked AGAIN here because a queue item can wait minutes behind other work,
    # and a drive unplugged in between would otherwise walk an empty (or absent)
    # directory and report success - the exact failure storage routes exist to
    # remove (local-storage-spec.md section 6.5.2). One stat.
    route_label = None
    if args.route_id:
        try:
            sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
            import storage_routes as _routes
            _resolved, _route_err = _routes.resolve_source(args.route_id)
            if _route_err:
                print(f'ERROR: {_route_err}', file=sys.stderr)
                _db_complete(catalog_db_path, args.execution_id, 'failed', _route_err)
                sys.exit(1)
            _route = _routes.get_route(args.route_id)
            route_label = _route.get('name') if _route else None
            # The launcher's path wins if they differ - it is what the rest of
            # this run was set up with - but a mismatch means the route moved
            # between enqueue and now, which is worth saying out loud.
            if _resolved != args.source_dir and not args.source_dir.startswith(_resolved):
                print(f'Note: storage route "{route_label}" now resolves to {_resolved}, '
                      f'but this run was started against {args.source_dir}.')
        except ImportError as exc:
            print(f'WARNING: could not check the storage route ({exc}); '
                  f'continuing with {args.source_dir}')

    if not os.path.exists(args.source_dir):
        print(f'ERROR: Source directory does not exist: {args.source_dir}', file=sys.stderr)
        _db_complete(catalog_db_path, args.execution_id, 'failed',
                     f'Source directory does not exist: {args.source_dir}')
        sys.exit(1)

    catalog = Catalog.get_by_id(args.catalog_id)
    if catalog is None:
        print(f'ERROR: Catalog {args.catalog_id} not found', file=sys.stderr)
        _db_complete(catalog_db_path, args.execution_id, 'failed',
                     f'Catalog {args.catalog_id} not found')
        sys.exit(1)

    run_phases = tuple(p.strip() for p in args.phases.split(',')) if args.phases else None

    # An empty source is almost always a mount problem rather than a real empty
    # folder, and silently "succeeding" with 0 files hides it. A bind mount
    # resolves once at container start, so if the host directory was repointed
    # after the container started, the container still sees the old one.
    #
    # Checked by walking only as far as the FIRST file: the full walk is the
    # scan phase's job, and it streams rather than listing.
    if (run_phases is None or 'scan' in run_phases) and not _first_file(args.source_dir):
        if route_label:
            # Everything is known here: which route, where the user thinks it
            # is, and that the folder is readable but empty. Say that, rather
            # than a paragraph about bind mounts the user did not configure.
            _r = _routes.get_route(args.route_id) or {}
            msg = (f'Storage route "{route_label}" contains no files. '
                   f'It points at {_r.get("host_path") or args.source_dir}'
                   + (f' (subfolder "{_r.get("source_subpath")}")'
                      if _r.get('source_subpath') else '')
                   + '. The folder is readable but empty - check that it is the one '
                     'you meant, and that the files are not in a subfolder.')
        else:
            msg = (f'Source directory contains no files: {args.source_dir}. '
                   f'If this path is a mount, check that the host directory is correct '
                   f'and recreate the container - bind mounts do not follow changes '
                   f'made to the host path after startup.')
        print(f'ERROR: {msg}', file=sys.stderr)
        _db_complete(catalog_db_path, args.execution_id, 'failed', msg)
        sys.exit(1)

    try:
        result = (execute_fn or execute)(
            source_dir=args.source_dir,
            target_dir=args.target_dir,
            files=None,
            progress_callback=None,
            catalog_id=args.catalog_id,
            catalog=catalog,
            execution_id=args.execution_id,
            phases=run_phases,
        )
        if isinstance(result, dict) and result.get('stopped'):
            # execute() already wrote the 'stopped' row. Calling _db_complete
            # here would overwrite it with 'completed' and the run would fire
            # its whole downstream chain on a half-imported catalog.
            print(f'Subprocess execution {args.execution_id} stopped by request.')
        else:
            _db_complete(catalog_db_path, args.execution_id, 'completed',
                         skipped_files=result.get('skipped_files', 0) if isinstance(result, dict) else 0)
            print(f'Subprocess execution {args.execution_id} completed.')
    except Exception as exc:
        import traceback
        traceback.print_exc()
        _db_complete(catalog_db_path, args.execution_id, 'failed', str(exc))
        sys.exit(1)


if __name__ == '__main__':
    main()
