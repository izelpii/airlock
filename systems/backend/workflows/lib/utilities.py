"""
Common utility functions for workflow processing
Shared by the ingestion workflow and the AI workflows.
"""

import os
import json
import hashlib
import shutil
from datetime import datetime, timezone, timedelta
from PIL import Image
from PIL.ExifTags import TAGS, GPSTAGS
from PIL import ImageFile

# A JPEG cut short - an incomplete copy, often at a round size like exactly
# 1 MB - is rendered as far as its data goes, the rest grey, instead of failing
# outright. 85 of oldserver's family photos (2026-10-04) had no preview at all for
# want of this; a partial picture is how the user finds out the file is damaged.
ImageFile.LOAD_TRUNCATED_IMAGES = True
import exiftool
import pillow_heif

# Video previews: decided and made by one module, shared with backend-transcode.
try:
    from lib import video_proxy
except ImportError:          # imported as a top-level module from lib/
    import video_proxy


def parse_gps_coordinate(coord_str, ref):
    """
    Parse GPS coordinate from DMS string format and apply direction sign.
    
    Args:
        coord_str: String like "77 deg 33' 57.77\" W" or numeric value
        ref: Reference direction ('N', 'S', 'E', 'W')
    
    Returns:
        float: Signed decimal degrees (negative for S/W)
    """
    # If already numeric, just apply sign based on reference
    if isinstance(coord_str, (int, float)):
        value = float(coord_str)
        # Apply negative sign for South or West
        if ref in ('S', 'W'):
            return -abs(value)
        return abs(value)
    
    # Parse DMS string format: "77 deg 33' 57.77" W"
    import re
    match = re.match(r"(\d+(?:\.\d+)?)\s*deg\s*(\d+(?:\.\d+)?)'?\s*(\d+(?:\.\d+)?)", str(coord_str))
    if match:
        degrees = float(match.group(1))
        minutes = float(match.group(2))
        seconds = float(match.group(3))
        
        # Convert to decimal degrees
        decimal = degrees + (minutes / 60.0) + (seconds / 3600.0)
        
        # Apply negative sign for South or West
        if ref in ('S', 'W'):
            return -decimal
        return decimal
    
    # If we can't parse it, return None
    return None


def _quicktime_local_datetime(value):
    """
    'YYYY:MM:DD HH:MM:SS+HH:MM' -> 'YYYY:MM:DD HH:MM:SS', keeping the LOCAL time.

    QuickTime:CreationDate is the only date tag in a .MOV that says what the
    clock read where the camera was standing. The offset is dropped rather than
    applied, because everything downstream - the filename timestamp, the dated
    folder, master_datetime, event grouping - works in local wall-clock, exactly
    as EXIF:DateTimeOriginal does for a photo. Applying the offset here would
    convert to UTC and reintroduce the bug this fixes.

    Returns None for anything that is not a usable date, so the caller's `or`
    chain simply moves on to the next tag.
    """
    if not value or not isinstance(value, str):
        return None
    text = value.strip()
    # Trim a trailing offset: '+HH:MM', '-HH:MM' or 'Z'.
    if text.endswith('Z'):
        text = text[:-1]
    elif len(text) > 6 and text[-6] in '+-' and text[-3] == ':':
        text = text[:-6]
    text = text.strip()
    # exiftool writes 0000:00:00 00:00:00 for an empty tag.
    if not text or text.startswith('0000'):
        return None
    return text


def extract_exif_data_with_exiftool(file_path, et=None):
    """
    Extract EXIF data from a file using ExifTool (supports CR3 and other RAW formats)
    Returns a dictionary with EXIF information or None if extraction fails

    `et` is an already-running ExifToolHelper to reuse. Without one, a process
    is started and stopped for this one file - Perl start-up paid per file,
    which the import's probe workers avoid by passing their own.
    """
    try:
        if et is None:
            with exiftool.ExifToolHelper() as own:
                metadata = own.get_metadata(file_path)
        else:
            metadata = et.get_metadata(file_path)
        if not metadata or len(metadata) == 0:
            return None
        # ExifTool returns a list with one dict per file
        return exif_from_exiftool_metadata(metadata[0])
    except Exception as e:
        print(f"Error extracting EXIF with ExifTool from {file_path}: {e}")
        return None


def exif_from_exiftool_metadata(exif_dict):
    """
    Map one file's exiftool metadata dict to the exif_data row shape.

    Pure: the half of extract_exif_data_with_exiftool that does not start a
    process, so a caller that read many files in one exiftool call can map each.
    """
    
    # Parse GPS coordinates - ExifTool returns signed decimal degrees
    # Composite fields already have the correct sign applied (negative for S/W)
    latitude = exif_dict.get('Composite:GPSLatitude') or exif_dict.get('EXIF:GPSLatitude')
    longitude = exif_dict.get('Composite:GPSLongitude') or exif_dict.get('EXIF:GPSLongitude')
    altitude = exif_dict.get('EXIF:GPSAltitude') or exif_dict.get('Composite:GPSAltitude')
    
    # If we got string format (e.g., "77 deg 33' 57.77\" W"), we need to parse and apply sign
    if latitude and isinstance(latitude, str):
        lat_ref = exif_dict.get('EXIF:GPSLatitudeRef', 'N')
        latitude = parse_gps_coordinate(latitude, lat_ref)
    
    if longitude and isinstance(longitude, str):
        lon_ref = exif_dict.get('EXIF:GPSLongitudeRef', 'E')
        longitude = parse_gps_coordinate(longitude, lon_ref)
    
    # Extract common fields - try multiple possible tag names.
    #
    # The QuickTime fallbacks are what make VIDEO work. A .MOV carries no
    # EXIF: block at all, so every EXIF: lookup above returns None and a
    # video used to fall through to the filesystem date - the date it was
    # imported, not the date it was filmed.
    #
    # QuickTime:CreationDate is preferred over QuickTime:CreateDate, and
    # the order matters more than it looks:
    #
    #   CreateDate    is UTC. Verified against unedited clips, where it
    #                 equals CreationDate converted to UTC to the second.
    #   CreationDate  is Apple's tag: LOCAL wall-clock plus the offset it
    #                 was filmed at.
    #
    # Everything downstream stores local wall-clock - that is what
    # EXIF:DateTimeOriginal is for a photo - so taking CreateDate would
    # shift every video by the UTC offset and push anything filmed after
    # about 8pm onto the following day.
    creation_date = (exif_dict.get('EXIF:DateTimeOriginal') or
                   exif_dict.get('EXIF:CreateDate') or
                   exif_dict.get('EXIF:DateTime') or
                   _quicktime_local_datetime(exif_dict.get('QuickTime:CreationDate')) or
                   exif_dict.get('QuickTime:CreateDate') or
                   exif_dict.get('QuickTime:MediaCreateDate') or
                   exif_dict.get('QuickTime:TrackCreateDate'))

    camera_make = (exif_dict.get('EXIF:Make') or exif_dict.get('IFD0:Make')
                   or exif_dict.get('QuickTime:Make'))
    camera_model = (exif_dict.get('EXIF:Model') or exif_dict.get('IFD0:Model')
                    or exif_dict.get('QuickTime:Model'))
    lens_model = (exif_dict.get('EXIF:LensModel') or exif_dict.get('Composite:LensID')
                  or exif_dict.get('QuickTime:LensModel'))
    
    # Format focal length
    focal_length = None
    fl = exif_dict.get('EXIF:FocalLength') or exif_dict.get('Composite:FocalLength')
    if fl:
        # ExifTool returns focal length as string like "70.0 mm"
        if isinstance(fl, str):
            focal_length = fl.replace(' ', '')
        else:
            focal_length = f"{fl}mm"
    
    # Format aperture
    aperture = None
    fn = exif_dict.get('EXIF:FNumber') or exif_dict.get('Composite:Aperture')
    if fn:
        if isinstance(fn, str):
            aperture = fn if fn.startswith('f/') else f"f/{fn}"
        else:
            aperture = f"f/{fn}"
    
    # ISO
    iso = str(exif_dict.get('EXIF:ISO') or exif_dict.get('IFD0:ISO') or '')
    
    # Shutter speed
    shutter_speed = None
    et_val = exif_dict.get('EXIF:ExposureTime') or exif_dict.get('Composite:ShutterSpeed')
    if et_val:
        if isinstance(et_val, str):
            shutter_speed = et_val if 's' in et_val else f"{et_val}s"
        else:
            shutter_speed = f"{et_val}s"
    
    # Flash
    flash = str(exif_dict.get('EXIF:Flash') or '')
    
    # Orientation
    orientation = exif_dict.get('EXIF:Orientation') or exif_dict.get('IFD0:Orientation')
    
    # Image dimensions
    width = (exif_dict.get('EXIF:ImageWidth') or exif_dict.get('File:ImageWidth')
             or exif_dict.get('QuickTime:ImageWidth')
             or exif_dict.get('QuickTime:SourceImageWidth'))
    height = (exif_dict.get('EXIF:ImageHeight') or exif_dict.get('File:ImageHeight')
              or exif_dict.get('QuickTime:ImageHeight')
              or exif_dict.get('QuickTime:SourceImageHeight'))
    
    return {
        'creation_date': creation_date,
        'latitude': latitude,
        'longitude': longitude,
        'altitude': altitude,
        'camera_make': camera_make,
        'camera_model': camera_model,
        'lens_model': lens_model,
        'focal_length': focal_length,
        'aperture': aperture,
        'iso': iso,
        'shutter_speed': shutter_speed,
        'flash': flash,
        'orientation': orientation,
        'width': width,
        'height': height,
        'raw_exif_json': json.dumps(exif_dict)
    }


def extract_exif_data(file_path):
    """
    Extract EXIF data from an image file
    Returns a dictionary with EXIF information or None if not an image
    """
    try:
        image = Image.open(file_path)
        exif_data = image._getexif()
        
        if not exif_data:
            return None
        
        exif_dict = {}
        gps_info = {}
        
        # Extract basic EXIF data
        for tag_id, value in exif_data.items():
            tag = TAGS.get(tag_id, tag_id)
            
            # Handle GPS data separately
            if tag == 'GPSInfo':
                for gps_tag_id in value:
                    gps_tag = GPSTAGS.get(gps_tag_id, gps_tag_id)
                    gps_info[gps_tag] = value[gps_tag_id]
            else:
                # Convert value to string if it's not JSON serializable
                try:
                    json.dumps(value)
                    exif_dict[tag] = value
                except (TypeError, ValueError):
                    exif_dict[tag] = str(value)
        
        # Parse GPS coordinates
        latitude = None
        longitude = None
        altitude = None
        
        if gps_info:
            # Parse latitude
            if 'GPSLatitude' in gps_info and 'GPSLatitudeRef' in gps_info:
                lat = gps_info['GPSLatitude']
                lat_ref = gps_info['GPSLatitudeRef']
                latitude = convert_to_degrees(lat)
                if lat_ref == 'S':
                    latitude = -latitude
            
            # Parse longitude
            if 'GPSLongitude' in gps_info and 'GPSLongitudeRef' in gps_info:
                lon = gps_info['GPSLongitude']
                lon_ref = gps_info['GPSLongitudeRef']
                longitude = convert_to_degrees(lon)
                if lon_ref == 'W':
                    longitude = -longitude
            
            # Parse altitude
            if 'GPSAltitude' in gps_info:
                altitude = float(gps_info['GPSAltitude'])
        
        # Extract common fields
        creation_date = exif_dict.get('DateTime') or exif_dict.get('DateTimeOriginal')
        camera_make = exif_dict.get('Make')
        camera_model = exif_dict.get('Model')
        lens_model = exif_dict.get('LensModel')
        
        # Format focal length
        focal_length = None
        if 'FocalLength' in exif_dict:
            fl = exif_dict['FocalLength']
            if isinstance(fl, tuple):
                focal_length = f"{fl[0]/fl[1]:.1f}mm"
            else:
                focal_length = f"{fl}mm"
        
        # Format aperture
        aperture = None
        if 'FNumber' in exif_dict:
            fn = exif_dict['FNumber']
            if isinstance(fn, tuple):
                aperture = f"f/{fn[0]/fn[1]:.1f}"
            else:
                aperture = f"f/{fn}"
        
        # ISO
        iso = str(exif_dict.get('ISOSpeedRatings', ''))
        
        # Shutter speed
        shutter_speed = None
        if 'ExposureTime' in exif_dict:
            et = exif_dict['ExposureTime']
            if isinstance(et, tuple):
                shutter_speed = f"{et[0]}/{et[1]}s"
            else:
                shutter_speed = f"{et}s"
        
        # Flash
        flash = str(exif_dict.get('Flash', ''))
        
        # Orientation
        orientation = exif_dict.get('Orientation')
        
        # Image dimensions
        width = image.width
        height = image.height
        
        return {
            'creation_date': creation_date,
            'latitude': latitude,
            'longitude': longitude,
            'altitude': altitude,
            'camera_make': camera_make,
            'camera_model': camera_model,
            'lens_model': lens_model,
            'focal_length': focal_length,
            'aperture': aperture,
            'iso': iso,
            'shutter_speed': shutter_speed,
            'flash': flash,
            'orientation': orientation,
            'width': width,
            'height': height,
            'raw_exif_json': json.dumps(exif_dict)
        }
        
    except Exception as e:
        print(f"Error extracting EXIF from {file_path}: {e}")
        return None


def convert_to_degrees(value):
    """Convert GPS coordinates to degrees"""
    d = float(value[0])
    m = float(value[1])
    s = float(value[2])
    return d + (m / 60.0) + (s / 3600.0)


def calculate_sha256(file_path):
    """Calculate SHA256 hash of a file"""
    # hashlib.file_digest (3.11+) hashes in C with a large buffer and releases
    # the GIL while it does, which is what lets the import's probe threads hash
    # in parallel. The 4 KB read loop it replaces spent most of its time in the
    # interpreter (documentation/features/parallelism.md, P5).
    try:
        with open(file_path, "rb") as f:
            if hasattr(hashlib, 'file_digest'):
                return hashlib.file_digest(f, 'sha256').hexdigest()
            sha256_hash = hashlib.sha256()
            for byte_block in iter(lambda: f.read(1 << 20), b""):
                sha256_hash.update(byte_block)
            return sha256_hash.hexdigest()
    except Exception as e:
        print(f"Error calculating SHA256 for {file_path}: {e}")
        return None


def hash_and_stage(source_path, staged_path, chunk=4 << 20):
    """
    SHA256 of source_path, copying it to staged_path in the same single read.

    What lets the picture import read each file ONCE: probe needs every byte for
    the hash, and place used to read every byte again to copy it. On a spinning
    disk that second pass was as long as the first (documentation/features/
    ingestion-performance-oldserver.md). Place now renames the staged copy instead.

    The copy is written under a temporary name and renamed when complete, so a
    crash or a stop never leaves a truncated file under the name place trusts.
    shutil.copystat afterwards makes it what shutil.copy2 would have produced:
    same bytes, same mode and times.

    Unlike calculate_sha256 this RAISES on failure, because the caller has two
    different fallbacks for "cannot read the source" and "cannot write the copy".
    """
    partial = staged_path + '.part'
    os.makedirs(os.path.dirname(staged_path), exist_ok=True)
    sha = hashlib.sha256()
    try:
        # Large blocks: hashlib and file I/O both release the GIL on them, so
        # probe threads still hash in parallel, as with file_digest.
        with open(source_path, 'rb') as src, open(partial, 'wb') as dst:
            for block in iter(lambda: src.read(chunk), b''):
                sha.update(block)
                dst.write(block)
        shutil.copystat(source_path, partial)
        os.replace(partial, staged_path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    return sha.hexdigest()


def preserve_source_attributes(source_path, full_target_path):
    """
    Owner and extended attributes from source to target, best effort.

    What copy_file_with_catalog does after a local copy, shared with the import's
    staged copies so a renamed file ends up exactly like a copied one.
    """
    try:
        stat_info = os.stat(source_path)
        os.chown(full_target_path, stat_info.st_uid, stat_info.st_gid)
    except (OSError, AttributeError) as e:
        # chown may fail if not running as root or on Windows
        print(f"Warning: Could not preserve ownership for {full_target_path}: {e}")

    # Preserve extended attributes if available
    try:
        # This works on macOS and some Linux systems
        if hasattr(os, 'listxattr') and hasattr(os, 'getxattr') and hasattr(os, 'setxattr'):
            for attr in os.listxattr(source_path):
                try:
                    value = os.getxattr(source_path, attr)
                    os.setxattr(full_target_path, attr, value)
                except OSError:
                    pass  # Some attributes may not be copyable
    except (OSError, AttributeError):
        pass  # Extended attributes not supported on this system


def get_file_timestamps(file_path):
    """
    Extract all filesystem timestamps from a file.
    Returns a dictionary with modification_time, access_time, change_time, and creation_time.
    
    Returns:
        dict: Dictionary containing timestamp information as ISO format strings
    """
    try:
        stat_info = os.stat(file_path)
        
        # Modification time (mtime) - when file content was last modified
        modification_time = datetime.fromtimestamp(stat_info.st_mtime).isoformat()
        
        # Access time (atime) - when file was last accessed
        access_time = datetime.fromtimestamp(stat_info.st_atime).isoformat()
        
        # Change time (ctime) - when file metadata was last changed (Unix) or creation time (Windows)
        change_time = datetime.fromtimestamp(stat_info.st_ctime).isoformat()
        
        # Creation time (birthtime) - available on macOS and Windows
        creation_time = None
        if hasattr(stat_info, 'st_birthtime'):
            creation_time = datetime.fromtimestamp(stat_info.st_birthtime).isoformat()
        else:
            # On Linux, st_birthtime is not available, use ctime as fallback
            creation_time = change_time
        
        return {
            'modification_time': modification_time,
            'access_time': access_time,
            'change_time': change_time,
            'creation_time': creation_time
        }
    except Exception as e:
        print(f"Error extracting timestamps from {file_path}: {e}")
        return {
            'modification_time': None,
            'access_time': None,
            'change_time': None,
            'creation_time': None
        }


def is_image_file(file_path):
    """Check if file is an image based on extension (media_types.IMAGE_EXTENSIONS)."""
    ext = os.path.splitext(file_path)[1].lower()
    return ext in IMAGE_EXTENSIONS


def is_raw_file(file_path):
    """Check if file is a RAW image format or HEIC that requires ExifTool"""
    # Include HEIC/HEIF formats as they need ExifTool for proper EXIF extraction
    raw_extensions = {'.cr3', '.cr2', '.nef', '.arw', '.dng', '.raf', '.orf', '.rw2', '.heic', '.heif'}
    ext = os.path.splitext(file_path)[1].lower()
    return ext in raw_extensions


def is_heic_file(file_path):
    """Check if file is a HEIC/HEIF image"""
    heic_extensions = {'.heic', '.heif'}
    ext = os.path.splitext(file_path)[1].lower()
    return ext in heic_extensions


def is_cr3_file(file_path):
    """Check if file is a Canon CR3 RAW image"""
    return os.path.splitext(file_path)[1].lower() == '.cr3'


def is_jpg_or_png_file(file_path):
    """Check if file is a JPG or PNG image"""
    jpg_png_extensions = {'.jpg', '.jpeg', '.png'}
    ext = os.path.splitext(file_path)[1].lower()
    return ext in jpg_png_extensions


def create_alternate_version(original_path, target_dir, file_id, max_width=1200,
                             creation_date=None, name_stem=None, suffix='_alt',
                             user_orientation=1):
    """
    Create an alternate JPG version of an image, resized to max_width if needed.
    Always converts to JPG format and resizes if image width exceeds max_width.
    Supports HEIC, CR3, and standard image formats.

    Args:
        original_path: Path to the original image file
        target_dir: Target directory where alternate version will be saved
        file_id: Database ID of the original file
        max_width: Maximum width in pixels (default 1200). Caps WIDTH, not the
                   long edge - see IMAGE_RUNGS for why that distinction matters.
                   None means NO CAP: the JPG comes out at the original's own
                   dimensions, which is what the full-resolution rung is.
        creation_date: datetime object used to build the cache subdirectory
        name_stem:    Base name for the derivative, WITHOUT extension. Defaults to the
                      original's own stem, which is unique when the catalog owns the
                      file (its name carries a timestamp and a hash). A catalog that
                      indexes files in place has no such guarantee - two folders in a
                      user's library can both hold "IMG_0001.jpg" - so the ingestion
                      workflow passes the collision-proof name it would have used
                      (file-ingestion-spec.md section 3.4).
                       (cache/Images/YYYY/MM/DD/).  Defaults to today when None.
        suffix:    Name suffix identifying the rung, e.g. '_alt' (the mandatory
                   1200px base) or '_alt_2048'. The base keeps the bare '_alt'
                   it has always had, so no path already on disk is rewritten
                   and no stored face coordinate is invalidated.
        user_orientation: the Basic Editor's turn or flip (1-8, EXIF numbering),
                   applied after the EXIF orientation (basic-photo-editing.md).

    Returns:
        dict: Information about the alternate version (path, size, dimensions) or None if failed
    """
    try:
        # Import ImageOps for orientation handling
        from PIL import ImageOps
        
        # Register HEIF opener for PIL to handle HEIC files
        pillow_heif.register_heif_opener()
        
        # Check if this is a CR3 file and handle it specially
        if is_cr3_file(original_path):
            import rawpy
            # Open CR3 with rawpy and post-process to an 8-bit RGB array
            with rawpy.imread(original_path) as raw:
                rgb = raw.postprocess(
                    use_camera_wb=True,
                    half_size=False,
                    no_auto_bright=False,
                    output_bps=8,
                )
            # Convert numpy array to Pillow Image
            img = Image.fromarray(rgb)
            
            # Apply EXIF orientation if present
            oriented_image = ImageOps.exif_transpose(img)
            if oriented_image is not None:
                img = oriented_image
        else:
            # Open the image normally for other formats
            img = Image.open(original_path)
            
            # Apply EXIF orientation correction for all image types
            oriented_image = ImageOps.exif_transpose(img)
            if oriented_image is not None:
                img = oriented_image
        
        if user_orientation and user_orientation != 1:
            try:
                from .orientation import apply_user
            except ImportError:          # imported as a top-level module
                from orientation import apply_user
            img = apply_user(img, user_orientation)

        # Get original dimensions
        original_width, original_height = img.size
        
        # Determine if resizing is needed. An uncapped rung never resizes, and
        # an original narrower than the cap never does either - both come out
        # as a plain "TYPE JPG" below, because that is what they are.
        needs_resize = max_width is not None and original_width > max_width
        
        # Calculate new dimensions if resizing
        if needs_resize:
            ratio = max_width / original_width
            new_width = max_width
            new_height = int(original_height * ratio)
        else:
            new_width = original_width
            new_height = original_height
        
        # Convert to RGB if necessary (for PNG with transparency, HEIC, etc.)
        if img.mode in ('RGBA', 'LA', 'P'):
            # Create white background
            background = Image.new('RGB', img.size, (255, 255, 255))
            if img.mode == 'P':
                img = img.convert('RGBA')
            background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
            img = background
        elif img.mode != 'RGB':
            img = img.convert('RGB')
        
        # Resize if needed
        if needs_resize:
            img = img.resize((new_width, new_height), Image.Resampling.LANCZOS)
        
        # Generate alternate file path in cache subdirectory (cache/Images/YYYY/MM/DD/)
        _date = creation_date if creation_date is not None else datetime.today()
        year  = f"{_date.year:04d}"
        month = f"{_date.month:02d}"
        day   = f"{_date.day:02d}"
        cache_dir = os.path.join(target_dir, 'cache', 'Images', year, month, day)
        os.makedirs(cache_dir, exist_ok=True)

        original_name = os.path.basename(original_path)
        name_without_ext = name_stem or os.path.splitext(original_name)[0]
        alternate_name = f"{name_without_ext}{suffix}.jpg"
        alternate_path = os.path.join(cache_dir, alternate_name)
        
        # Save as JPG with high quality
        img.save(alternate_path, 'JPEG', quality=90, optimize=True)
        
        # Get file size
        alternate_size = os.path.getsize(alternate_path)
        
        # Build characteristic description
        characteristics = []
        characteristics.append("TYPE JPG")
        if needs_resize:
            characteristics.append(f"TYPE SCALE {max_width}")
        characteristic = " ".join(characteristics)
        
        return {
            'path': alternate_path,
            'name': alternate_name,
            'size': alternate_size,
            'width': new_width,
            'height': new_height,
            'characteristic': characteristic
        }
        
    except Exception as e:
        print(f"Error creating alternate version for {original_path}: {e}")
        return None


# ─── Image display rungs ──────────────────────────────────────────────────────
#
# See documentation/specs/file-sizes-spec.md.
#
# `max_width` caps WIDTH, not the long edge. Capping the long edge is the
# obvious-looking alternative and it is a trap: a portrait photo capped at a
# 2048 long edge is 1152x2048, which is NARROWER than the 1200 rung's 1200x2134.
# A "larger" rung that is sometimes smaller than the base cannot be reasoned
# about (spec 4.3).
#
# The 1200 rung is MANDATORY and keeps the bare `_alt.jpg` name it has always
# had. Every stored face-detection coordinate is in its pixel space, and it is
# what the application itself displays: it is the floor, not a default (spec 2.3,
# 4.4).
#
# `max_width: None` is the FULL-RESOLUTION rung: a JPG at the original's own
# pixel dimensions, never scaled. It is a cap of "no cap", not a cap of some
# very large number - a sentinel width would be a figure this table asserts and
# nothing honours, and every comparison against it would read as a real limit.
# It is still a JPG, which is the point: originals are HEIC and CR3 as often as
# they are JPG, and a full-resolution size a browser cannot open is not a size.
IMAGE_RUNGS = {
    '1200': {'max_width': 1200, 'suffix': '_alt',      'mandatory': True},
    '1600': {'max_width': 1600, 'suffix': '_alt_1600', 'mandatory': False},
    '2048': {'max_width': 2048, 'suffix': '_alt_2048', 'mandatory': False},
    '2560': {'max_width': 2560, 'suffix': '_alt_2560', 'mandatory': False},
    'full': {'max_width': None, 'suffix': '_alt_full', 'mandatory': False},
}

# Always generated, never selectable, never removable.
MANDATORY_RUNGS = ('1200',)


def rung_width(rung):
    """
    A rung's width as a SORTABLE number, widest last.

    The uncapped rung sorts above every capped one, which is what it is: no
    original is wider than "the original". Everywhere that orders rungs goes
    through this, so `max_width: None` cannot reach a comparison that would
    raise on it.
    """
    width = IMAGE_RUNGS[rung]['max_width']
    return float('inf') if width is None else width


# The rungs a user may add, widest last.
OPTIONAL_RUNGS = tuple(
    k for k in sorted(IMAGE_RUNGS, key=rung_width)
    if not IMAGE_RUNGS[k]['mandatory'])


def rungs_for(extras):
    """
    The full rung list to generate, narrowest first.

    `extras` is a catalog's chosen optional rungs (Catalog.image_rungs). The
    mandatory floor is added here and nowhere else, so no caller can produce a
    file set without the base - which section 2.3 shows would not degrade the
    application but break it.

    Unknown names are dropped rather than raising: a rung removed from
    IMAGE_RUNGS in a future version must not make every existing catalog
    unopenable.
    """
    chosen = list(MANDATORY_RUNGS) + [r for r in (extras or []) if r in IMAGE_RUNGS]
    seen, out = set(), []
    for r in sorted(chosen, key=rung_width):
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# Square thumbnail sizes generated for every imported image.
#   72  - the filmstrip in Photos
#   320 - the 160x160 gallery cards (People / Locations / Events), at 2x for HiDPI
THUMBNAIL_SIZES = (72, 320)


def create_thumbnail(alt_path, alt_name, target_dir, size=72):
    """
    Create a square thumbnail from an existing _alt.jpg file.
    The thumbnail is saved next to the alt file with the same base name.

    Naming: size 72 keeps the historic _thumb.jpg suffix so the thumbnails
    created before multiple sizes existed stay valid; every other size uses
    _thumb_<size>.jpg so the sizes never overwrite each other.

    Args:
        alt_path:   Absolute path to the _alt.jpg file produced by create_alternate_version.
        alt_name:   File name of the alt file (e.g. "20240101120000-abc_alt.jpg").
        target_dir: Catalog target directory (used to compute the relative path for DB storage).
        size:       Square pixel size for the thumbnail (default 72).

    Returns:
        dict with keys path, name, size, width, height, characteristic — or None on failure.
    """
    try:
        cache_dir = os.path.dirname(alt_path)
        name_without_ext = os.path.splitext(alt_name)[0]
        # Strip trailing _alt suffix to get the clean base name
        if name_without_ext.endswith('_alt'):
            name_without_ext = name_without_ext[:-4]
        suffix = '_thumb' if size == 72 else f'_thumb_{size}'
        thumb_name = f"{name_without_ext}{suffix}.jpg"
        thumb_path = os.path.join(cache_dir, thumb_name)

        with Image.open(alt_path) as img:
            if img.mode != 'RGB':
                img = img.convert('RGB')
            # thumbnail() keeps aspect ratio within the box; ImageOps.fit() fills the box
            from PIL import ImageOps as _ImageOps
            thumb = _ImageOps.fit(img, (size, size), Image.Resampling.LANCZOS)

        thumb.save(thumb_path, 'JPEG', quality=80, optimize=True)

        return {
            'path': thumb_path,
            'name': thumb_name,
            'size': os.path.getsize(thumb_path),
            'width': size,
            'height': size,
            'characteristic': f'TYPE THUMBNAIL {size}',
        }
    except Exception as e:
        print(f"Error creating thumbnail for {alt_path}: {e}")
        return None


# ─── Video ────────────────────────────────────────────────────────────────────
#
# See documentation/specs/video-spec.md.
#
# Ingested videos previously got no derivative at all and were invisible in every
# gallery. Two artefacts fix that:
#
#   a PROXY  - a browser-playable MP4, produced only when the source is not
#              already playable (section 4);
#   a POSTER - a square still, registered as TYPE THUMBNAIL 320 so every existing
#              thumbnail path works for videos with no special casing.

# Recognised video extensions live in media_types, shared with Catalog's
# Images/Videos/Others routing so a file cannot be filed as a video and then not
# recognised as one.
#
# is_ignored_file / ignore_reason ride along on the same import: the deny list
# of sidecars and operating-system files belongs beside the other "what kind of
# file is this" answers, and re-exporting here keeps the workflows importing
# from one place.
try:
    from media_types import (  # noqa: F401
        VIDEO_EXTENSIONS, IMAGE_EXTENSIONS, is_video_file, is_ignored_file,
        ignore_reason, is_ignored_directory, is_raw_original, original_kind,
    )
except ImportError:  # pragma: no cover - direct import when the root is not on the path
    import sys as _sys
    _sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(
        os.path.abspath(__file__)))))
    from media_types import (  # noqa: F401
        VIDEO_EXTENSIONS, IMAGE_EXTENSIONS, is_video_file, is_ignored_file,
        ignore_reason, is_ignored_directory, is_raw_original, original_kind,
    )

# The web-friendly copy of a video (create_video_proxy): its long edge, and the
# x264 quality it is encoded at (CRF: lower is better and larger; 23 is x264's
# own default).
#
# Was 720 and CRF 28, sized for a LAN gallery. On real camera footage that came
# out visibly poor: a 1920x1080, 30 Mbps clip became 720x406 at ~280 kbps.
# 1280 is 720p for 16:9 footage (so the 'VIDEO PROXY MP4 720P' label is now
# accurate), and CRF 23 keeps it sharp - roughly 4-6x the old file size.
# Existing copies are rebuilt with:
#     python backend/migrate_add_video_proxies.py --refresh-proxies
VIDEO_PROXY_MAX_EDGE = 1280
VIDEO_PROXY_CRF = 23
# x264's speed/size trade-off. Was 'slow': on oldserver a 6-minute 4K HEVC iPhone
# clip took ~9 minutes on all 8 cores. Measured there on 20 s of that footage:
# slow 64 s (5.2 MB), veryfast 42 s (4.3 MB), and decoding alone 30 s - for 4K
# HEVC the software decode is the floor (its i7-4940MX and GTX 880M cannot
# decode HEVC in hardware), so the encoder preset is what is left to save.
VIDEO_PROXY_PRESET = os.environ.get('IMPORT_VIDEO_PRESET') or 'veryfast'
# ffmpeg runs at this niceness, so an import's own work (reading, hashing,
# metadata, moving files) gets the CPU first and previews take what is left.
# 0 turns it off.
try:
    VIDEO_FFMPEG_NICE = int(os.environ.get('IMPORT_VIDEO_NICE', '10'))
except ValueError:
    VIDEO_FFMPEG_NICE = 10
VIDEO_POSTER_SIZE = 320
# Frame 0 is very often black on phone video, so seek in a little first.
VIDEO_POSTER_SEEK_SECONDS = 1.0

_FFMPEG_WARNED = False


def _lower_priority():
    """preexec_fn for ffmpeg: run below the import itself (VIDEO_FFMPEG_NICE)."""
    if VIDEO_FFMPEG_NICE > 0:
        try:
            os.nice(VIDEO_FFMPEG_NICE)
        except OSError:
            pass


def _ffmpeg_available():
    """
    True when ffmpeg and ffprobe are both on PATH. Warns once per process rather
    than once per file, or a large import fills the log with the same line.
    """
    global _FFMPEG_WARNED
    ok = video_proxy.available()
    if not ok and not _FFMPEG_WARNED:
        _FFMPEG_WARNED = True
        print('[video] ffmpeg/ffprobe not on PATH - videos will be ingested '
              'without a playable proxy or poster')
    return ok


def _ffprobe_streams(path):
    """{width, height, duration, vcodec, acodec} or None (lib/video_proxy.probe)."""
    return video_proxy.probe(path)


def _is_faststart(path):
    return video_proxy.is_faststart(path)


def _video_timeout(duration_seconds):
    """
    A transcode budget proportional to the material. A flat cap kills long videos
    mid-way and leaves a truncated file behind. Twice as generous when ffmpeg
    runs niced, since it may wait for the CPU behind the import itself.
    """
    return video_proxy.timeout_for(duration_seconds, 6 if VIDEO_FFMPEG_NICE > 0 else 3)


# ── The transcode service (accelerated-services-spec.md section 3.2) ──────────
# When TRANSCODE_URL answers, video previews are made there - natively on a Mac
# (VideoToolbox), with the GPU on an NVIDIA machine - and the import only waits.
# When it does not, they are made here exactly as before. A service that is down
# is not asked again for a minute, so a stopped service costs one refused
# connection, not one per video.
_TRANSCODE_DOWN_UNTIL = 0.0
# Where the catalogs are mounted in THIS process; the service gets paths
# relative to it, and resolves them under its own root.
_CATALOGS_MOUNT = os.environ.get('TRANSCODE_CATALOGS_MOUNT', '/catalogs')


def _transcode_request(endpoint, payload, timeout):
    """
    POST to the service. Returns (status, body) or (None, reason) when it could
    not be reached, so the caller falls back to doing the work itself.
    """
    global _TRANSCODE_DOWN_UNTIL
    import time
    import urllib.error
    import urllib.request
    base = (os.environ.get('TRANSCODE_URL') or '').rstrip('/')
    if not base or time.monotonic() < _TRANSCODE_DOWN_UNTIL:
        return None, 'no service'
    req = urllib.request.Request(f'{base}/{endpoint}', data=json.dumps(payload).encode(),
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read() or b'{}')
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read() or b'{}')
        except Exception:
            body = {}
        return exc.code, body
    except (urllib.error.URLError, OSError, ValueError) as exc:
        _TRANSCODE_DOWN_UNTIL = time.monotonic() + 60
        print(f'[video] transcode service not reachable at {base} ({exc}); '
              f'making previews here for the next minute')
        return None, str(exc)


def _catalog_relative(target_dir, *paths):
    """
    (catalog, [paths relative to it]) when the service can address these files,
    else None: the catalog must sit under the catalogs mount, and every path
    inside it (a reference catalog's originals live elsewhere).
    """
    root = os.path.realpath(_CATALOGS_MOUNT)
    cat = os.path.realpath(target_dir)
    if not cat.startswith(root + os.sep):
        return None
    rels = []
    for p in paths:
        real = os.path.realpath(os.path.dirname(p))
        if real != cat and not real.startswith(cat + os.sep):
            return None
        rels.append(os.path.relpath(os.path.join(real, os.path.basename(p)), cat))
    return os.path.relpath(cat, root), rels


def _cache_dir_for(target_dir, creation_date):
    _date = creation_date if creation_date is not None else datetime.today()
    return os.path.join(target_dir, 'cache', 'Videos',
                        f'{_date.year:04d}', f'{_date.month:02d}', f'{_date.day:02d}')


def create_video_proxy(original_path, target_dir, creation_date=None, name_stem=None):
    """
    Make a video browser-playable, doing the least work that achieves it.

    Returns one of:
        {'action': 'passthrough', 'duration': ...}     already playable, no file written
        {'action': 'remux'|'transcode', 'path': ..., 'name': ..., 'size': ...,
         'width': ..., 'height': ..., 'duration': ...,
         'characteristic': 'VIDEO PROXY MP4 720P'}
        None                                            ffmpeg missing, or failed

    'duration' is in seconds and present in BOTH success shapes (video-page-spec.md
    section 2.7). The decisions - passthrough, remux, transcode, the scale, 8-bit
    H.264 - are lib/video_proxy.py's, whether the transcode service makes the
    proxy or this process does.
    """
    cache_dir = _cache_dir_for(target_dir, creation_date)
    # name_stem: see create_alternate_version. A referenced original's own name
    # is not unique across a user's library.
    stem = name_stem or os.path.splitext(os.path.basename(original_path))[0]
    proxy_name = f'{stem}_proxy.mp4'
    proxy_path = os.path.join(cache_dir, proxy_name)

    def _shape(r):
        if r.get('action') == 'passthrough':
            return {'action': 'passthrough', 'duration': r.get('duration') or None}
        return {'action': r['action'], 'path': proxy_path, 'name': proxy_name,
                'size': r.get('size') or os.path.getsize(proxy_path),
                'width': r.get('width'), 'height': r.get('height'),
                'duration': r.get('duration'),
                'characteristic': 'VIDEO PROXY MP4 720P',
                'encoder': r.get('encoder')}

    rel = _catalog_relative(target_dir, original_path, proxy_path)
    if rel:
        catalog, (src_rel, proxy_rel) = rel
        status, body = _transcode_request(
            'transcode', {'catalog': catalog, 'source': src_rel, 'proxy': proxy_rel,
                          'max_edge': VIDEO_PROXY_MAX_EDGE, 'crf': VIDEO_PROXY_CRF},
            timeout=_video_timeout(None) + 7200)
        if status == 200:
            return _shape(body)
        if status in (422, 500):
            # The service tried, hardware and software: doing it again here
            # would fail the same way, slower.
            print(f'[video] transcode service could not make a proxy for '
                  f'{os.path.basename(original_path)}: {body.get("error")}')
            return None
        if status is not None:
            print(f'[video] transcode service refused {os.path.basename(original_path)} '
                  f'(HTTP {status}: {body.get("error")}); making it here')

    if not _ffmpeg_available():
        return None
    os.makedirs(cache_dir, exist_ok=True)
    # A temporary name: a killed ffmpeg otherwise leaves a truncated file that
    # looks finished. The real extension stays LAST (ffmpeg picks its muxer from
    # it); the pid keeps two concurrent runs from sharing it.
    temp_path = os.path.join(cache_dir, f'{stem}_proxy.partial.{os.getpid()}.mp4')
    result = video_proxy.make_proxy(
        original_path, proxy_path, temp_path, encoders=(video_proxy.SOFTWARE,),
        crf=VIDEO_PROXY_CRF, preset=VIDEO_PROXY_PRESET, max_edge=VIDEO_PROXY_MAX_EDGE,
        nice=VIDEO_FFMPEG_NICE, timeout_factor=6 if VIDEO_FFMPEG_NICE > 0 else 3)
    if not result or result.get('action') is None:
        return None
    return _shape(result)


def create_video_poster(original_path, target_dir, creation_date=None, name_stem=None,
                        size=VIDEO_POSTER_SIZE):
    """
    A square still from the video, in the shape create_thumbnail() returns.

    Registered by the caller as TYPE THUMBNAIL <size>, which is what lets the
    existing thumbnail endpoint, galleryThumbUrl and nearest-size selection treat
    a video like any other item (spec section 2.5). Cropped to square, never
    squashed.
    """
    cache_dir = _cache_dir_for(target_dir, creation_date)
    stem = name_stem or os.path.splitext(os.path.basename(original_path))[0]
    poster_name = f'{stem}_poster.jpg'
    poster_path = os.path.join(cache_dir, poster_name)
    shaped = {'path': poster_path, 'name': poster_name, 'width': size, 'height': size,
              'characteristic': f'TYPE THUMBNAIL {size}'}

    rel = _catalog_relative(target_dir, original_path, poster_path)
    if rel:
        catalog, (src_rel, poster_rel) = rel
        status, body = _transcode_request(
            'poster', {'catalog': catalog, 'source': src_rel, 'poster': poster_rel,
                       'size': size}, timeout=180)
        if status == 200:
            return {**shaped, 'size': body.get('size') or os.path.getsize(poster_path)}
        if status is not None and status >= 500:
            return None

    if not _ffmpeg_available():
        return None
    os.makedirs(cache_dir, exist_ok=True)
    temp_path = os.path.join(cache_dir, f'{stem}_poster.partial.{os.getpid()}.jpg')
    if not video_proxy.make_poster(original_path, poster_path, temp_path, size=size,
                                   nice=VIDEO_FFMPEG_NICE):
        return None
    return {**shaped, 'size': os.path.getsize(poster_path)}


def _unlink_quietly(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def get_next_alternate_sequence(alt_dir, base_name):
    """
    Get the next available 4-digit sequence number for an alternate version.
    
    Args:
        alt_dir: Directory where alternate versions are stored
        base_name: Base name of the file (without extension)
    
    Returns:
        int: Next available sequence number (1-9999)
    """
    max_seq = 0
    if os.path.exists(alt_dir):
        for fname in os.listdir(alt_dir):
            # Match files like: basename_NNNN.ext
            name_no_ext = os.path.splitext(fname)[0]
            if name_no_ext.startswith(base_name + '_'):
                suffix = name_no_ext[len(base_name) + 1:]
                if suffix.isdigit() and len(suffix) == 4:
                    seq = int(suffix)
                    if seq > max_seq:
                        max_seq = seq
    return max_seq + 1


def convert_heic_to_jpg(heic_path, output_dir, base_name):
    """
    Convert a HEIC image to JPG format and save it in the output directory.
    
    Args:
        heic_path: Path to the source HEIC file
        output_dir: Directory where the JPG will be saved
        base_name: Base name for the output file (without extension)
    
    Returns:
        tuple: (output_path, file_size) if successful, (None, None) if failed
    """
    try:
        # Register HEIF opener with Pillow
        pillow_heif.register_heif_opener()
        
        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)
        
        # Get next sequence number
        seq = get_next_alternate_sequence(output_dir, base_name)
        seq_str = f"{seq:04d}"
        
        # Build output filename: base_name_NNNN.jpg
        output_filename = f"{base_name}_{seq_str}.jpg"
        output_path = os.path.join(output_dir, output_filename)
        
        # Open HEIC and apply EXIF orientation
        image = Image.open(heic_path)
        
        # Apply EXIF orientation to ensure the image is correctly oriented
        from PIL import ImageOps
        image = ImageOps.exif_transpose(image)
        if image is None:
            # If exif_transpose returns None, re-open the image
            image = Image.open(heic_path)
        
        # Convert to RGB if necessary (HEIC may be in other modes)
        if image.mode not in ('RGB', 'L'):
            image = image.convert('RGB')
        
        # Save as JPEG with high quality
        image.save(output_path, 'JPEG', quality=95)
        
        file_size = os.path.getsize(output_path)
        print(f"Converted HEIC to JPG: {output_path} ({file_size} bytes)")
        return output_path, file_size
        
    except Exception as e:
        print(f"Error converting HEIC to JPG for {heic_path}: {e}")
        return None, None


def convert_cr3_to_jpg(cr3_path, output_dir, base_name):
    """
    Convert a Canon CR3 RAW image to JPG format and save it in the output directory.
    Uses rawpy to decode the RAW data and Pillow to save as JPEG.
    
    Args:
        cr3_path: Path to the source CR3 file
        output_dir: Directory where the JPG will be saved
        base_name: Base name for the output file (without extension)
    
    Returns:
        tuple: (output_path, file_size) if successful, (None, None) if failed
    """
    try:
        import rawpy

        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)

        # Get next sequence number
        seq = get_next_alternate_sequence(output_dir, base_name)
        seq_str = f"{seq:04d}"

        # Build output filename: base_name_NNNN.jpg
        output_filename = f"{base_name}_{seq_str}.jpg"
        output_path = os.path.join(output_dir, output_filename)

        # Open CR3 with rawpy and post-process to an 8-bit RGB array
        with rawpy.imread(cr3_path) as raw:
            rgb = raw.postprocess(
                use_camera_wb=True,
                half_size=False,
                no_auto_bright=False,
                output_bps=8,
            )

        # Convert numpy array to Pillow Image
        image = Image.fromarray(rgb)
        
        # Apply EXIF orientation if present in the original CR3
        # Note: rawpy may already handle orientation, but we apply it to be safe
        from PIL import ImageOps
        oriented_image = ImageOps.exif_transpose(image)
        if oriented_image is not None:
            image = oriented_image
        
        image.save(output_path, 'JPEG', quality=95)

        file_size = os.path.getsize(output_path)
        print(f"Converted CR3 to JPG: {output_path} ({file_size} bytes)")
        return output_path, file_size

    except Exception as e:
        print(f"Error converting CR3 to JPG for {cr3_path}: {e}")
        return None, None


def resize_image_to_width(source_path, output_dir, base_name, max_width=1200):
    """
    Resize an image proportionally so its width equals max_width pixels.
    Only resizes if the image width exceeds max_width.
    Saves the resized image in the output directory.
    
    Args:
        source_path: Path to the source image file
        output_dir: Directory where the resized image will be saved
        base_name: Base name for the output file (without extension)
        max_width: Maximum width in pixels (default: 1200)
    
    Returns:
        tuple: (output_path, file_size, original_width, new_width) if resized,
               (None, None, original_width, original_width) if no resize needed,
               (None, None, None, None) if failed
    """
    try:
        # Register HEIF opener in case source is HEIC
        pillow_heif.register_heif_opener()
        
        # Open the image and apply EXIF orientation
        image = Image.open(source_path)
        
        # Apply EXIF orientation to ensure the image is correctly oriented
        from PIL import ImageOps
        image = ImageOps.exif_transpose(image)
        if image is None:
            # If exif_transpose returns None, re-open the image
            image = Image.open(source_path)
        
        original_width, original_height = image.size
        
        # Only resize if width exceeds max_width
        if original_width <= max_width:
            print(f"Image {os.path.basename(source_path)} width ({original_width}px) does not exceed {max_width}px, skipping resize")
            return None, None, original_width, original_width
        
        # Calculate proportional new height
        ratio = max_width / original_width
        new_height = int(original_height * ratio)
        new_size = (max_width, new_height)
        
        # Ensure output directory exists
        os.makedirs(output_dir, exist_ok=True)
        
        # Get next sequence number
        seq = get_next_alternate_sequence(output_dir, base_name)
        seq_str = f"{seq:04d}"
        
        # Determine output format: keep original extension if it's a standard format,
        # otherwise default to JPG
        src_ext = os.path.splitext(source_path)[1].lower()
        if src_ext in ('.jpg', '.jpeg'):
            out_ext = '.jpg'
            save_format = 'JPEG'
            save_kwargs = {'quality': 95}
        elif src_ext == '.png':
            out_ext = '.png'
            save_format = 'PNG'
            save_kwargs = {}
        else:
            # For HEIC, RAW, and other formats, output as JPG
            out_ext = '.jpg'
            save_format = 'JPEG'
            save_kwargs = {'quality': 95}
        
        output_filename = f"{base_name}_{seq_str}{out_ext}"
        output_path = os.path.join(output_dir, output_filename)
        
        # Resize using high-quality Lanczos resampling
        resized_image = image.resize(new_size, Image.Resampling.LANCZOS)
        
        # Convert to RGB if saving as JPEG and image is not in RGB/L mode
        if save_format == 'JPEG' and resized_image.mode not in ('RGB', 'L'):
            resized_image = resized_image.convert('RGB')
        
        resized_image.save(output_path, save_format, **save_kwargs)
        
        file_size = os.path.getsize(output_path)
        print(f"Resized image from {original_width}x{original_height} to {max_width}x{new_height}: {output_path} ({file_size} bytes)")
        return output_path, file_size, original_width, max_width
        
    except Exception as e:
        print(f"Error resizing image {source_path}: {e}")
        return None, None, None, None


# ─── Epoch / corrupted date guard ────────────────────────────────────────────
# Years that indicate a camera clock was never set (reset to 1970-01-01 or 2000-01-01)
_EPOCH_YEARS_INVALID = {1970, 2000, 0}


def usable_exif_datetime(exif_data):
    """
    The EXIF/QuickTime creation date as a datetime, or None when there is not a
    real one.

    A blank date is NOT absent. Scanners, phone exports and re-encoders write
    EXIF blocks whose DateTimeOriginal is literally '0000:00:00 00:00:00', and a
    non-empty string is truthy, so `if exif_data.get('creation_date')` treats
    that placeholder as a genuine date. It then fails to parse, and the caller
    falls all the way through to the filesystem - the date the file was copied.

    That is how photos from 2003 came to be filed under 2026: the directory tier
    had the right answer and never got asked, because the placeholder had already
    claimed the answer was EXIF's.

    Rejects anything that does not parse, and anything whose year is implausible
    (see _is_valid_year), so one check covers both shapes of bad data.
    """
    if not exif_data:
        return None
    raw = exif_data.get('creation_date')
    if not raw or not isinstance(raw, str):
        return None
    raw = raw.strip()
    for fmt in ('%Y:%m:%d %H:%M:%S', '%Y-%m-%d %H:%M:%S'):
        try:
            dt = datetime.strptime(raw, fmt)
        except (ValueError, TypeError):
            continue
        return dt if _is_valid_year(dt.year) else None
    return None


def _is_valid_year(year_int):
    """Return True if year_int is plausible (> 1970, not a known-bad epoch, ≤ current year + 2)."""
    return year_int > 1970 and year_int not in _EPOCH_YEARS_INVALID and year_int <= datetime.now().year + 2


def resolve_master_datetime(file_path, exif_data=None, source_dir=None,
                             date_source_hint=None, precomputed_datetime=None):
    """
    Resolve the canonical date/time for a file and return it as an ISO 8601
    string plus a source label.

    The string carries a zone only when one is actually KNOWN:
      * a photo's EXIF time with an OffsetTime* tag  -> "...-04:00"
      * a photo's EXIF time WITHOUT one, and a date read from a folder or file
        name                                         -> "2026-09-25T20:46:10",
        no zone: local time as the camera (or the person naming the folder)
        recorded it. These used to be written as "...Z", which declares them
        UTC; a browser then shifted every Canon EOS 6D photo by the viewer's
        offset - 20:46 shown as 16:46 in New York.
      * a video's metadata time, a filesystem time, the system time -> "...Z":
        QuickTime records UTC, and the other two are instants.

    Resolution tiers (per spec §3):
      1. EXIF creation_date  (with OffsetTimeOriginal/OffsetTime/OffsetTimeDigitized)
      2. Directory path structure  [via precomputed_datetime from the caller]
      3. Filename date pattern     [via precomputed_datetime from the caller]
      4. Filesystem birthtime / ctime
      5. System time (datetime.now(UTC))

    Any date whose year falls outside [1970 … now+2] is skipped to the next tier.

    Args:
        file_path:            Path to the source file.
        exif_data:            Dict returned by extract_exif_data / extract_exif_data_with_exiftool.
        source_dir:           Base source directory (enables dir/filename tiers).
        date_source_hint:     Pre-computed source label from generate_new_filename(); when
                              supplied together with precomputed_datetime, the result is
                              returned immediately without re-running extraction logic.
        precomputed_datetime: datetime object already resolved by the caller (e.g., the
                              result of get_creation_date_from_organized_structure in
                              picture_import_workflow).  Used with date_source_hint to
                              avoid re-running the same extraction work.

    Returns:
        Tuple[str, str]:  (iso8601_string, source_label)
                          source_label ∈ {"exif","directory","filename","filesystem","system"}
    """

    # ── Fast path: caller already resolved everything ─────────────────────────
    # picture_import_workflow passes date_source_hint + precomputed_datetime to
    # avoid re-running directory/filename regex extraction.
    if date_source_hint in ('directory', 'filename') and precomputed_datetime is not None:
        dt = precomputed_datetime
        if _is_valid_year(dt.year):
            return dt.strftime('%Y-%m-%dT%H:%M:%S'), date_source_hint

    # ── Tier 1: EXIF creation_date ────────────────────────────────────────────
    # usable_exif_datetime(), not a truthiness check: '0000:00:00 00:00:00' is a
    # non-empty string and used to reach the parse below, fail it, and take the
    # whole resolution down to the filesystem tier.
    dt = usable_exif_datetime(exif_data)
    if dt is not None:
        # Attempt to extract a timezone offset from raw EXIF JSON
        tz_offset = None
        raw_json = exif_data.get('raw_exif_json')
        if raw_json:
            try:
                raw_dict = json.loads(raw_json) if isinstance(raw_json, str) else raw_json
                tz_offset = (
                    raw_dict.get('OffsetTimeOriginal') or
                    raw_dict.get('EXIF:OffsetTimeOriginal') or
                    raw_dict.get('OffsetTime') or
                    raw_dict.get('EXIF:OffsetTime') or
                    raw_dict.get('OffsetTimeDigitized') or
                    raw_dict.get('EXIF:OffsetTimeDigitized')
                )
            except Exception:
                pass

        if tz_offset:
            # tz_offset is like "+02:00" or "-05:00"
            iso_str = dt.strftime('%Y-%m-%dT%H:%M:%S') + tz_offset
        elif is_video_file(file_path):
            # QuickTime/MP4 metadata times are UTC by specification.
            iso_str = dt.strftime('%Y-%m-%dT%H:%M:%SZ')
        else:
            # A still camera's clock is local time, and with no offset tag the
            # zone is unknown - so none is claimed (see the docstring).
            iso_str = dt.strftime('%Y-%m-%dT%H:%M:%S')
        return iso_str, 'exif'

    # ── Tiers 2 & 3: directory structure / filename pattern ───────────────────
    # The caller resolved these already - picture_import_workflow works them out
    # to build the filename - and hands the result over as precomputed_datetime.
    # This is where it gets used when the hint was not one of the fast-path
    # labels, which is exactly the case a blank EXIF date used to create: the
    # date was known, correct, and thrown away.
    #
    # The docstring promised these two tiers and the code went straight from
    # Tier 1 to Tier 4, so a directory date only ever survived via the fast path
    # at the top.
    if precomputed_datetime is not None and _is_valid_year(precomputed_datetime.year):
        label = date_source_hint if date_source_hint in ('directory', 'filename') else 'directory'
        return precomputed_datetime.strftime('%Y-%m-%dT%H:%M:%S'), label

    # ── Tier 4: filesystem birthtime / ctime ──────────────────────────────────
    try:
        stat_info = os.stat(file_path)
        if hasattr(stat_info, 'st_birthtime'):
            ts = stat_info.st_birthtime
        else:
            ts = stat_info.st_ctime
        # In UTC explicitly: the "Z" below was only true when the importer
        # happened to run in a UTC container.
        dt = datetime.fromtimestamp(ts, timezone.utc)
        if _is_valid_year(dt.year):
            return dt.strftime('%Y-%m-%dT%H:%M:%SZ'), 'filesystem'
    except Exception:
        pass

    # ── Tier 5: system time (final fallback) ──────────────────────────────────
    dt = datetime.now(timezone.utc)
    return dt.strftime('%Y-%m-%dT%H:%M:%SZ'), 'system'


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


def generate_new_filename(file_path, sha256_hash, exif_data=None):
    """
    Generate new filename with format: YYYYMMDDHHmmss-SHA256-original_filename.ext
    
    Args:
        file_path: Original file path
        sha256_hash: Full SHA256 hash of the file
        exif_data: Optional EXIF data dictionary
    
    Returns:
        Tuple of (new_filename, creation_timestamp_14digit)
    """
    # Get creation date
    creation_date = get_creation_date(file_path, exif_data)
    
    # Format date as YYYYMMDDHHmmss (14 digits)
    timestamp_str = format_creation_timestamp(creation_date)
    
    # Get last 10 characters of SHA256 hash
    hash_suffix = sha256_hash[-10:] if sha256_hash else '0000000000'
    
    # Get original filename with extension
    original_filename = os.path.basename(file_path)
    
    # Construct new filename with 14-digit timestamp
    new_filename = f"{timestamp_str}-{hash_suffix}-{original_filename}"
    
    return new_filename, timestamp_str


def copy_file_with_catalog(source_path, target_path, catalog):
    """
    Copy file using Catalog's file write strategy.
    
    Args:
        source_path: Source file path
        target_path: Target file path (relative to catalog's target_directory)
        catalog: Catalog instance to use for file operations
        
    Returns:
        bool: True if successful, False otherwise
    """
    try:
        # Use the catalog's write_file method with its configured strategy
        # The target_path should be relative to the catalog's target_directory
        # since the catalog's FileManager already has base_folder set
        success = catalog.write_file(source_path, target_path)
        
        if success:
            # Try to preserve ownership and extended attributes. This only
            # works for local file strategies.
            if catalog.file_strategy == 'local':
                preserve_source_attributes(
                    source_path, os.path.join(catalog.target_directory, target_path))

        return success
    except Exception as e:
        print(f"Error copying file with catalog: {e}")
        return False

def compute_geohash(latitude, longitude, precision=10):
    """
    Compute a geohash string for the given latitude/longitude coordinates.

    Args:
        latitude: Latitude in decimal degrees (float)
        longitude: Longitude in decimal degrees (float)
        precision: Number of geohash characters (default 10, ~1.2m accuracy)

    Returns:
        str: Geohash string, or None if coordinates are invalid / library unavailable
    """
    if latitude is None or longitude is None:
        return None
    try:
        import pygeohash as pgh
        return pgh.encode(float(latitude), float(longitude), precision=precision)
    except Exception as e:
        print(f"Warning: Could not compute geohash for ({latitude}, {longitude}): {e}")
        return None
