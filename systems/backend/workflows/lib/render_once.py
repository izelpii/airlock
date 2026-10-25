"""
Derivatives from ONE decode of each original.

The first renderer (utilities.create_alternate_version, called once per rung)
opened and decoded the original for every display size, and developed a RAW
file in full each time. Measured per file: 196 + 231 ms for a JPEG's 1200 and
2048 rungs against 209 ms decoding once; 1,115 ms per rung for a CR3 against
67 ms from its embedded preview (documentation/features/ingestion-architecture.md
§3.4).

The OUTPUT is the same as create_alternate_version's: the same cache folder,
names, JPEG quality, widths and heights (computed from the original's true
dimensions, so a reduced decode never changes a stored size), the same
'characteristic' text, and the same rows for the writer. Pixels differ only by
the decode path: a JPEG may be decoded at a reduced scale when every rung is
smaller than it, and a RAW's derivatives come from the camera's own embedded
JPEG when that is large enough, rather than from a LibRaw development.
"""

import io
import math
import os
from datetime import datetime

from PIL import Image, ImageOps

try:
    import pillow_heif
    pillow_heif.register_heif_opener()
except Exception:                      # pragma: no cover - HEIC simply fails to open
    pillow_heif = None

from .utilities import IMAGE_RUNGS, MANDATORY_RUNGS, THUMBNAIL_SIZES, rungs_for, create_thumbnail
from .orientation import apply_user, swaps

_ORIENT_SWAPS = {5, 6, 7, 8}
# LibRaw's `flip` (sizes.flip) to the PIL transpose that undoes it.
_LIBRAW_FLIP = {3: Image.Transpose.ROTATE_180, 5: Image.Transpose.ROTATE_90,
                6: Image.Transpose.ROTATE_270}


def _is_raw(path):
    try:
        import media_types
        return media_types.is_raw_original(path)
    except ImportError:            # pragma: no cover
        return os.path.splitext(path)[1].lower() in {'.cr2', '.cr3', '.nef', '.arw', '.dng'}


def _widest(rungs):
    """The largest capped width wanted, or None when an uncapped (full) rung is."""
    widths = [IMAGE_RUNGS[r]['max_width'] for r in rungs]
    return None if any(w is None for w in widths) else max(widths)


def _load_raw(path, need_width, log):
    """
    (image, (true_w, true_h)) for a RAW file, oriented, using the cheapest source
    that still covers `need_width` (None = the full size is wanted).
    """
    import rawpy
    with rawpy.imread(path) as raw:
        sizes = raw.sizes
        flip = sizes.flip
        true_w, true_h = sizes.width, sizes.height
        if flip in (5, 6):
            true_w, true_h = true_h, true_w

        # 1. The camera's embedded JPEG, when big enough. Most modern RAW files
        #    carry one at (or near) full size.
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                img = Image.open(io.BytesIO(thumb.data))
                img.load()
                # The preview is stored unrotated; the RAW's own flip says how
                # to turn it. Some cameras also tag the preview itself.
                if flip in _LIBRAW_FLIP:
                    img = img.transpose(_LIBRAW_FLIP[flip])
                else:
                    img = ImageOps.exif_transpose(img) or img
                enough = (need_width is None and img.width >= true_w * 0.95) or \
                         (need_width is not None and img.width >= min(need_width, true_w))
                if enough:
                    # Its pixels stand in for the sensor's: size decisions use
                    # the sensor's true dimensions, the preview is resampled.
                    return img, (true_w, true_h), 'embedded preview'
        except Exception as exc:          # no preview, or an unreadable one
            log.append(f'  no usable embedded preview ({exc}); developing the RAW')

        # 2. Half-size development when that still covers the widest rung.
        half = need_width is not None and true_w / 2 >= need_width
        rgb = raw.postprocess(use_camera_wb=True, half_size=half, no_auto_bright=False,
                              output_bps=8)
    return Image.fromarray(rgb), (true_w, true_h), 'half-size RAW' if half else 'full RAW'


def _load_image(path, need_width):
    """(image, (true_w, true_h)) for anything PIL opens, oriented, decoded once."""
    img = Image.open(path)
    w, h = img.size
    try:
        orientation = img.getexif().get(0x0112, 1)
    except Exception:
        orientation = 1
    true_w, true_h = (h, w) if orientation in _ORIENT_SWAPS else (w, h)
    if need_width is not None and true_w > need_width and img.format == 'JPEG':
        # JPEG's DCT scaling: decode at 1/2, 1/4 or 1/8 while still at least as
        # large as needed. Requested in the file's own (unrotated) axes.
        scale = need_width / true_w
        img.draft('RGB', (math.ceil(w * scale), math.ceil(h * scale)))
    img = ImageOps.exif_transpose(img) or img
    img.load()
    return img, (true_w, true_h), 'reduced decode' if img.width < true_w else 'decode'


def _to_rgb(img):
    # Exactly create_alternate_version's rule: transparency onto white.
    if img.mode in ('RGBA', 'LA', 'P'):
        background = Image.new('RGB', img.size, (255, 255, 255))
        if img.mode == 'P':
            img = img.convert('RGBA')
        background.paste(img, mask=img.split()[-1] if img.mode in ('RGBA', 'LA') else None)
        return background
    return img.convert('RGB') if img.mode != 'RGB' else img


def _save_rung(img, true_size, spec, target_dir, stem, creation):
    true_w, true_h = true_size
    max_width = spec['max_width']
    needs_resize = max_width is not None and true_w > max_width
    if needs_resize:
        new_w, new_h = max_width, int(true_h * (max_width / true_w))
    else:
        new_w, new_h = true_w, true_h
    out = img if img.size == (new_w, new_h) else img.resize((new_w, new_h),
                                                             Image.Resampling.LANCZOS)
    d = creation if creation is not None else datetime.today()
    cache_dir = os.path.join(target_dir, 'cache', 'Images',
                             f'{d.year:04d}', f'{d.month:02d}', f'{d.day:02d}')
    os.makedirs(cache_dir, exist_ok=True)
    name = f"{stem}{spec['suffix']}.jpg"
    path = os.path.join(cache_dir, name)
    out.save(path, 'JPEG', quality=90, optimize=True)
    characteristic = 'TYPE JPG' + (f' TYPE SCALE {max_width}' if needs_resize else '')
    return {'path': path, 'name': name, 'size': os.path.getsize(path),
            'width': new_w, 'height': new_h, 'characteristic': characteristic}


def render_image_once(task, creation_from_ts):
    """
    render_image_task, decoding the original once. Same task in, same result out:
    {'id', 'ok', 'rows', 'logs', 'duration', 'error'}.
    """
    target_path = task['target_path']
    target_dir = task['target_dir']
    file_id = task['file_id']
    existing = task['existing']
    name = os.path.basename(target_path)
    stem = task['derivative_stem'] or os.path.splitext(name)[0]
    rows, logs = [], []
    try:
        creation = creation_from_ts(task['creation_ts'])
        wanted = [r for r in rungs_for(task['rungs']) if r not in existing]
        base = None
        if '1200' in existing:
            rel, alt_name, _w = existing['1200']
            base = {'path': os.path.join(target_dir, rel), 'name': alt_name}
        produced = max([w or 0 for _, _, w in existing.values()] or [0])

        if wanted:
            need = _widest(wanted)
            if _is_raw(target_path):
                img, true_size, how = _load_raw(target_path, need, logs)
            else:
                img, true_size, how = _load_image(target_path, need)
            # The user's own turn or flip, after the EXIF one
            # (basic-photo-editing-spec section 5.1). 1 for every new import.
            user = task.get('user_orientation') or 1
            if user != 1:
                img = apply_user(img, user)
                if swaps(user):
                    true_size = (true_size[1], true_size[0])
                how += f', edited ({user})'
            img = _to_rgb(img)
            logs.append(f'  {name}: {how} {img.width}x{img.height}, '
                        f'{len(wanted)} size(s) from one decode')
            for rung in wanted:
                spec = IMAGE_RUNGS[rung]
                # Never store two rungs holding the same pixels (spec 4.9) - and,
                # unlike before, never write the file only to delete it.
                cap = spec['max_width']
                width = true_size[0] if cap is None or true_size[0] <= cap else cap
                if rung not in MANDATORY_RUNGS and width <= produced:
                    logs.append(f'  {name}: rung {rung} would duplicate {produced}px - not stored')
                    continue
                info = _save_rung(img, true_size, spec, target_dir, stem, creation)
                rows.append((file_id, os.path.relpath(info['path'], target_dir), info['name'],
                             info['size'], info['characteristic'], info['width'],
                             info['height'], rung))
                produced = max(produced, info['width'])
                if rung in MANDATORY_RUNGS:
                    base = info
                logs.append(f"Created alternate version (rung {rung}): {info['name']} "
                            f"({info['characteristic']}) - {info['width']}x{info['height']}px")
            del img

        if base:
            for size in THUMBNAIL_SIZES:
                if f'thumb{size}' in existing:
                    continue
                thumb = create_thumbnail(base['path'], base['name'], target_dir, size=size)
                if thumb:
                    rows.append((file_id, os.path.relpath(thumb['path'], target_dir),
                                 thumb['name'], thumb['size'], thumb['characteristic'],
                                 thumb['width'], thumb['height'], f'thumb{size}'))
        ok = base is not None
        return {'id': task['id'], 'ok': ok, 'rows': rows, 'logs': logs, 'duration': None,
                'error': None if ok else 'no 1200px rendering could be made'}
    except Exception as exc:
        return {'id': task['id'], 'ok': False, 'rows': rows, 'logs': logs,
                'duration': None, 'error': str(exc)}
