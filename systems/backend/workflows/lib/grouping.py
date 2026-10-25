"""
grouping.py — deciding when two files are one photograph.

Implements documentation/specs/file-unity-spec.md sections 4.2 and 5.1.

A camera set to RAW+JPG writes two files for one shutter actuation. They are one
image: one gallery tile, one event member, one set of derivatives. An iPhone
Live Photo is the same idea - a still and a ~3 s .MOV - and is grouped the same
way (kind 'live_photo'). This module decides which files pair up, and nothing
else — it does not touch the database, copy anything, or generate anything.

WHY IT IS THIS CAUTIOUS
-----------------------
A missed pair costs a duplicate tile: visible, harmless, fixable by hand. A wrong
merge silently hides one photograph behind another and is very hard to notice. So
several independent signals must agree, and a timestamp alone is NEVER enough.

Measured against the real files (spec section 2.2), on 8 CR3+JPG pairs:

    body serial + ShutterCount ......... 8 groups of 2   correct
    SubSecDateTimeOriginal ............. 8 groups of 2   correct
    DateTimeOriginal, second precision . 5 groups        WRONG - merges bursts
    ImageUniqueID ...................... 16 groups of 1  WRONG - it is per FILE

The second-precision failure is not a corner case: frames shot inside one second
during a burst are different photographs. And 17 files in the family catalog share
the placeholder '0000:00:00 00:00:00', so a timestamp rule would fuse all of them.

Dependency-free apart from exiftool: imported from the ingestion workflow and from
a migration script at the backend root, which have different sys.path setups.
"""

import os
import re

# Read for the whole batch in ONE exiftool call. Six tags, not the full metadata
# dict: this pass runs before anything is copied or converted, and its whole
# justification is that it is cheap (36 ms/file, against 670 ms to convert one
# CR3 that turns out not to be needed).
MATCH_TAGS = [
    'SerialNumber',
    'ShutterCount',
    'SubSecDateTimeOriginal',
    'DateTimeOriginal',
    'Make',
    'Model',
    # Live Photos: Apple writes one UUID into both the still (MakerNotes) and
    # the motion clip (QuickTime). Measured on oldserver's family import
    # (2026-10-03): 296 of 300 same-name still+MOV pairs carry it, all equal;
    # the 4 without were real, longer videos that merely shared a name - which
    # is why a name is never enough to pair a still with a video.
    'ContentIdentifier',
]

# 'YYYY:MM:DD HH:MM:SS' with an optional fractional part and offset.
_DATE_RE = re.compile(r'^(\d{4}):(\d{2}):(\d{2})[ T](\d{2}):(\d{2}):(\d{2})')

# Years that mean "no date was set". Mirrors _is_valid_year in utilities.py.
_BAD_YEARS = {0, 1970, 2000}


def _clean(value):
    """exiftool gives ints, floats, empty strings and None. Normalise to str|None."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def usable_timestamp(raw):
    """
    The timestamp as a comparable string, or None when it is not a real date.

    This is the guard that keeps the 17 files sharing '0000:00:00 00:00:00' from
    collapsing into one image, and it is why the caller must never compare
    raw tag values directly.
    """
    text = _clean(raw)
    if not text:
        return None
    m = _DATE_RE.match(text)
    if not m:
        return None
    year = int(m.group(1))
    if year in _BAD_YEARS or year < 1971:
        return None
    return text


def match_keys(metadata):
    """
    Reduce one exiftool result to the few values the rule compares.

    `metadata` is a dict as exiftool returns it, with or without group prefixes;
    both spellings are accepted because ExifToolHelper's output depends on how it
    was asked.
    """
    def tag(*names):
        for n in names:
            for key in (n, f'EXIF:{n}', f'MakerNotes:{n}', f'Composite:{n}',
                        f'QuickTime:{n}', f'Keys:{n}'):
                if key in metadata:
                    v = _clean(metadata[key])
                    if v is not None:
                        return v
        return None

    subsec = usable_timestamp(tag('SubSecDateTimeOriginal'))
    return {
        'serial': tag('SerialNumber', 'InternalSerialNumber'),
        'shutter_count': tag('ShutterCount'),
        # Only a SUB-second timestamp counts as signal B. A value with no
        # fractional part is just DateTimeOriginal wearing another name, and
        # second precision is exactly what merges bursts.
        'subsec': subsec if (subsec and '.' in subsec) else None,
        'datetime': usable_timestamp(tag('DateTimeOriginal', 'CreateDate')),
        'make': tag('Make'),
        'model': tag('Model'),
        'content_id': tag('ContentIdentifier'),
    }


def _stem(path):
    return os.path.splitext(os.path.basename(path or ''))[0].lower()


# A catalogued file's name is "<14 digits>-<10 hex>-<original name>".
_CATALOG_PREFIX = re.compile(r'^\d{14}-[0-9a-fA-F]{10}-')

# Camera originals, preferred over a converted copy of the same still.
_ORIGINAL_STILL_EXTS = ('.heic', '.heif')


def _original_stem(path):
    """The stem of the name the file arrived with, in a source tree or a catalog."""
    return _stem(_CATALOG_PREFIX.sub('', os.path.basename(path or ''), count=1))


def _ext(path):
    return os.path.splitext(path or '')[1].lower()


def pair_reason(a_path, a_keys, b_path, b_keys, is_raw):
    """
    Why these two files are one image, or None when they are not.

    Returns (rule_name, evidence_dict) or None. `is_raw` is a callable, and it
    must be the PRECISE sense - media_types.is_raw_original, not utilities'
    is_raw_file, which also counts HEIC because HEIC needs ExifTool. A HEIC is
    not a RAW original, and treating it as one would pair iPhone stills with
    their exports as though a camera had written both.

    The structural conditions come first because they are free; the metadata
    signals are only consulted for candidates that already look like a pair.
    """
    # 1. different files, different extensions - never two .JPG
    if a_path == b_path:
        return None
    a_ext, b_ext = _ext(a_path), _ext(b_path)
    if a_ext == b_ext:
        return None

    # 2. exactly one RAW. This is the raw_jpeg kind and the only kind implemented;
    #    two JPGs, or two RAWs, are not a pair.
    if is_raw(a_path) == is_raw(b_path):
        return None

    # 3. the camera must agree, when both files say
    for field in ('make', 'model'):
        if a_keys.get(field) and b_keys.get(field) and a_keys[field] != b_keys[field]:
            return None

    # ── Signal A: the same shutter actuation on the same body ────────────────
    # The strongest evidence there is. A shutter count is unique per body, so two
    # files carrying the same one came from the same press of the button.
    if (a_keys.get('shutter_count') and a_keys.get('serial')
            and a_keys['shutter_count'] == b_keys.get('shutter_count')
            and a_keys['serial'] == b_keys.get('serial')):
        return 'serial+shuttercount', {
            'serial': a_keys['serial'],
            'shutter_count': a_keys['shutter_count'],
        }

    # ── Signal B: identical to the sub-second, offset included ───────────────
    if a_keys.get('subsec') and a_keys['subsec'] == b_keys.get('subsec'):
        return 'subsec_datetime', {'subsec_datetime': a_keys['subsec']}

    # ── Signal C: the filename convention, and only WITH a matching second ───
    # The brief's original rule. Demoted to a fallback: a camera writing RAW and
    # JPG to separate cards numbers them independently, and a stem on its own can
    # collide between unrelated sources.
    if (_stem(a_path) == _stem(b_path)
            and a_keys.get('datetime')
            and a_keys['datetime'] == b_keys.get('datetime')):
        return 'stem+datetime', {
            'stem': _stem(a_path),
            'datetime': a_keys['datetime'],
        }

    return None


def _blocking_keys(path, keys):
    """The equality keys pair_reason's three signals compare, one per signal."""
    out = []
    if keys.get('shutter_count') and keys.get('serial'):
        out.append(('A', keys['serial'], keys['shutter_count']))
    if keys.get('subsec'):
        out.append(('B', keys['subsec']))
    if keys.get('datetime'):
        out.append(('C', _stem(path), keys['datetime']))
    return out


def find_live_photos(paths, keys_by_path, is_image, is_video, exclude=(), log=print):
    """
    Pair each Live Photo still with its motion clip.

    The still and the video must carry the same Apple ContentIdentifier. A
    UUID is not a coincidence the way a file name is - see MATCH_TAGS for the
    measurement that ruled names out as the rule.

    When one identifier covers more than one still or clip (a JPEG copy of the
    HEIC, an edited IMG_E beside the original), names decide WITHIN it: a clip
    pairs with the one still of its own name, the HEIC winning a tie with its
    copy. Anything that does not settle is left alone and reported, as
    find_groups does.

    `exclude` is files already in another group: a file belongs to one group,
    so a ProRAW Live Photo whose still is half of a RAW pair keeps its clip
    ungrouped rather than guessing which group wins.

    The PRIMARY is the still: it is what the gallery shows, and the clip needs
    no preview of its own.
    """
    by_id = {}
    for p in sorted(paths):
        if p in exclude:
            continue
        cid = (keys_by_path.get(p) or {}).get('content_id')
        if not cid:
            continue
        if is_video(p):
            by_id.setdefault(cid, ([], []))[1].append(p)
        elif is_image(p):
            by_id.setdefault(cid, ([], []))[0].append(p)

    groups = []
    for cid, (stills, videos) in sorted(by_id.items()):
        if not stills or not videos:
            continue
        if len(stills) == 1 and len(videos) == 1:
            groups.append(_live_group(stills[0], videos[0], cid, 'content_identifier'))
            continue
        # More than one still or clip under one identifier. Measured on oldserver
        # (2,315 such identifiers): nearly all are the original HEIC plus a
        # JPEG copy of it, or an edited IMG_E still beside the original, each
        # with its clip. Within the identifier, a clip's NAME says which still
        # it belongs to; a tie between a HEIC and its copy goes to the HEIC.
        # Whatever that does not settle is left alone, as before.
        claims = {}
        for v in videos:
            same = [s_ for s_ in stills if _original_stem(s_) == _original_stem(v)]
            if len(same) > 1:
                originals = [s_ for s_ in same if _ext(s_) in _ORIGINAL_STILL_EXTS]
                same = originals if len(originals) == 1 else []
            if len(same) == 1:
                claims.setdefault(same[0], []).append(v)
        paired = [(still, vs[0]) for still, vs in sorted(claims.items()) if len(vs) == 1]
        if not paired:
            log(f'  [group] Live Photo {cid}: {len(stills)} still(s) and '
                f'{len(videos)} video(s) share it - leaving all of them ungrouped')
            continue
        for still, video in paired:
            groups.append(_live_group(still, video, cid, 'content_identifier+name'))
    return groups


def _live_group(still, video, cid, rule):
    return {
        'primary': still,
        'secondary': video,
        'kind': 'live_photo',
        'match_rule': rule,
        'match_evidence': {'content_identifier': cid},
    }


def find_groups(paths, keys_by_path, is_raw, is_image, log=print, is_video=None):
    """
    Group a batch of files into logical images.

    Returns a list of dicts:
        {'primary': path, 'secondary': path, 'kind': 'raw_jpeg',
         'match_rule': str, 'match_evidence': dict}

    With `is_video`, Live Photos are paired too (find_live_photos), after the
    RAW pairs and never overlapping them: kind 'live_photo', the still primary.
    Without it the result is RAW pairs only, exactly as before.

    Files that pair with nothing are simply absent from the result — they are
    their own image and need no row.

    The PRIMARY is the non-RAW file. It is the one every browser, thumbnailer and
    face detector reads directly, and the cheap one to render: converting the CR3
    instead costs six times as much for a visually identical result.
    """
    candidates = [p for p in paths if is_image(p)]
    used = set()
    groups = []

    # Deterministic order, so the same batch always groups the same way whatever
    # order the filesystem walk produced.
    candidates.sort()

    # Blocking. pair_reason can only say yes when at least one of its three
    # signals holds, and each signal is an equality - so two files can only pair
    # if they share a bucket below. Comparing within buckets instead of against
    # every later file gives exactly the same answer at O(n) instead of O(n^2),
    # which at 300k files is the difference between seconds and never
    # (documentation/features/parallelism.md section 5.3.1).
    buckets = {}
    for idx, p in enumerate(candidates):
        for key in _blocking_keys(p, keys_by_path.get(p, {})):
            buckets.setdefault(key, []).append(idx)

    for i, a in enumerate(candidates):
        if a in used:
            continue
        # Later candidates sharing any bucket with `a`, in candidate order - the
        # same order the exhaustive scan visited them in.
        near = sorted({j for key in _blocking_keys(a, keys_by_path.get(a, {}))
                       for j in buckets.get(key, ()) if j > i})
        matches = []
        for b in (candidates[j] for j in near):
            if b in used:
                continue
            reason = pair_reason(a, keys_by_path.get(a, {}), b,
                                 keys_by_path.get(b, {}), is_raw)
            if reason:
                matches.append((b, reason))

        if not matches:
            continue
        if len(matches) > 1:
            # Three or more files matching is not an error, but it is not
            # something this rule was designed for - a burst with an odd
            # configuration, or a duplicate copy. Left alone and reported, rather
            # than guessed at.
            log(f'  [group] {os.path.basename(a)} matched '
                f'{len(matches)} files ({", ".join(os.path.basename(m) for m, _ in matches)}) '
                f'- leaving all of them ungrouped')
            continue

        b, (rule, evidence) = matches[0]
        primary, secondary = (b, a) if is_raw(a) else (a, b)
        used.add(a)
        used.add(b)
        groups.append({
            'primary': primary,
            'secondary': secondary,
            'kind': 'raw_jpeg',
            'match_rule': rule,
            'match_evidence': evidence,
        })

    if is_video is not None:
        groups.extend(find_live_photos(paths, keys_by_path, is_image, is_video,
                                       exclude=used, log=log))
    return groups


def read_match_keys(paths, log=print):
    """
    MATCH_TAGS for every path, in ONE exiftool invocation.

    One process for the whole batch rather than one per file: that is the
    difference between this pass being free and being a second import. Returns
    {path: keys}; a file exiftool cannot read simply gets empty keys and pairs
    with nothing.
    """
    if not paths:
        return {}
    try:
        import exiftool
    except ImportError:
        log('  [group] exiftool bindings unavailable - no grouping this run')
        return {}

    out = {}
    try:
        with exiftool.ExifToolHelper() as et:
            for meta in et.get_tags(list(paths), tags=MATCH_TAGS):
                source = meta.get('SourceFile')
                if source:
                    out[source] = match_keys(meta)
    except Exception as exc:
        log(f'  [group] could not read match keys ({exc}) - no grouping this run')
        return {}

    for p in paths:
        out.setdefault(p, {})
    return out
