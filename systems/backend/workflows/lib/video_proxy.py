"""
Video previews: probe a video, decide what it needs, and make it browser-playable.

Dependency-free (os, json, struct, subprocess, shutil, time) on purpose: it is
used from two places that must decide identically -

    the import          lib/utilities.create_video_proxy / create_video_poster,
                        when no transcode service answers (local fallback)
    backend-transcode   the service that owns ffmpeg and the hardware encoders
                        (documentation/specs/accelerated-services-spec.md)

The service image vendors this file; nothing here may import from the backend.

The ladder (video-page-spec.md section 4):
    passthrough   already H.264/AAC, small enough, faststart: nothing written
    remux         right codecs, wrong layout: copy the streams, move the index
    transcode     anything else: scale the long edge to max_edge, H.264 8-bit

Encoders, tried in the order given; the software one is always last:
    h264_videotoolbox   Apple (macOS, native only)
    h264_nvenc          NVIDIA, proprietary driver (native, or a container with
                        the GPU). NOT nouveau: NVENC is in the closed driver
                        only, so an open-driver machine has no hardware encoder
                        however capable the card is.
    h264_qsv            Intel Quick Sync (Linux/Windows), through /dev/dri
    h264_vaapi          Intel or AMD through VA-API, the generic Linux path
    h264_v4l2m2m        Raspberry Pi 4's hardware encoder (/dev/video11)
    libx264             software, everywhere

Every one of them is PROVED with a one-frame encode before use (usable_encoders)
and the probe runs the same command shape as the real encode - device flags,
filter chain and all. Being listed by `ffmpeg -encoders` means nothing: Debian's
build lists h264_nvenc, h264_qsv and h264_vaapi on a machine with no GPU at all,
and nouveau's render node answers "No VA display found" only when something
actually tries to open it.
"""

import glob
import json
import os
import shutil
import struct
import subprocess
import time

PROXY_MAX_EDGE = 1280
PROXY_CRF = 23
POSTER_SIZE = 320
# Frame 0 is very often black on phone video, so seek in a little first.
POSTER_SEEK_SECONDS = 1.0

SOFTWARE = 'libx264'
# Best first, and the order encodes two judgements. QSV before VAAPI because on
# Intel both work and QSV's rate control is better; VAAPI before v4l2m2m
# because the Pi's encoder is the weakest of them and only it needs a bitrate
# target instead of a quality target.
HARDWARE = ('h264_videotoolbox', 'h264_nvenc', 'h264_qsv', 'h264_vaapi',
            'h264_v4l2m2m')

# VA-API needs a DRM render node by path. Discovered rather than assumed: a
# machine can have several (two GPUs, or an iGPU beside a card), and on a
# hybrid-graphics laptop the first one is often the one that cannot encode.
VAAPI_DEVICE = os.environ.get('TRANSCODE_VAAPI_DEVICE', '') or None

# VA-API's constant-quality mode is CQP, whose qp is close enough to x264's crf
# to use the same number; raise it if VA-API output looks heavier than x264's.
VAAPI_QP = int(os.environ.get('TRANSCODE_VAAPI_QP', '0') or 0) or None

# The Pi's V4L2 encoder has no quality mode at all - only a bitrate. 2.5 Mbit
# for a 1280-edge preview is roughly crf 23 for phone video.
V4L2_BITRATE = os.environ.get('TRANSCODE_V4L2_BITRATE', '2500k')

# VideoToolbox has no CRF. On Apple Silicon it takes a constant-quality value
# (-q:v, 1-100). Measured against libx264 CRF 23 on iPhone 1080p HDR footage
# (M3 Pro, 2026-10-02): q55 matches its SSIM within 0.005 at ~1.6x the size;
# q60 matches it exactly at ~2x. The gain is CPU, not time: a 3-minute clip took
# 13.8 s and 46 CPU-seconds in hardware against 15.6 s and 165 in software.
# See accelerated-services-spec.md section 9.1.
VT_QUALITY = int(os.environ.get('TRANSCODE_VT_QUALITY', '55') or 55)


def available():
    """True when ffmpeg and ffprobe are both on PATH."""
    return bool(shutil.which('ffmpeg') and shutil.which('ffprobe'))


def probe(path):
    """{width, height, duration, vcodec, acodec}, or None when ffprobe cannot read it."""
    try:
        result = subprocess.run(
            ['ffprobe', '-v', 'error',
             '-show_entries', 'stream=width,height,codec_name,codec_type',
             '-show_entries', 'format=duration',
             '-of', 'json', path],
            capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or '{}')
    except Exception as exc:
        print(f'[video] ffprobe failed for {os.path.basename(path)}: {exc}')
        return None
    info = {'width': 0, 'height': 0, 'duration': 0.0, 'vcodec': None, 'acodec': None}
    for stream in data.get('streams', []):
        if stream.get('codec_type') == 'video' and not info['vcodec']:
            info['vcodec'] = stream.get('codec_name')
            info['width'] = int(stream.get('width') or 0)
            info['height'] = int(stream.get('height') or 0)
        elif stream.get('codec_type') == 'audio' and not info['acodec']:
            info['acodec'] = stream.get('codec_name')
    try:
        info['duration'] = float(data.get('format', {}).get('duration') or 0.0)
    except (TypeError, ValueError):
        info['duration'] = 0.0
    return info


def is_faststart(path):
    """True when the moov atom precedes mdat - the file plays before it is all downloaded."""
    try:
        total = os.path.getsize(path)
        with open(path, 'rb') as fh:
            offset = 0
            while offset < total:
                fh.seek(offset)
                header = fh.read(8)
                if len(header) < 8:
                    return False
                size = struct.unpack('>I', header[:4])[0]
                kind = header[4:8]
                if size == 1:                       # 64-bit extended size
                    size = struct.unpack('>Q', fh.read(8))[0]
                if size <= 0:
                    return False
                if kind == b'moov':
                    return True
                if kind == b'mdat':
                    return False
                offset += size
    except Exception:
        return False
    return False


def plan(info, path, max_edge=PROXY_MAX_EDGE):
    """'passthrough', 'remux' or 'transcode' for a probed video."""
    long_edge = max(info['width'], info['height'])
    browser_codecs = (info['vcodec'] in ('h264', 'avc1')
                      and info['acodec'] in ('aac', None))
    # The original is used as-is only when it is no bigger than the copy would be.
    small_enough = bool(long_edge) and long_edge <= max_edge
    if browser_codecs and small_enough:
        return 'passthrough' if is_faststart(path) else 'remux'
    return 'transcode'


def scale_filter(info, max_edge=PROXY_MAX_EDGE):
    """
    Scale the LONG edge to max_edge, never upscale; -2 keeps the aspect ratio
    AND rounds to an even number, which H.264 requires. (min() replaced
    force_original_aspect_ratio=decrease, which lost the even rounding and made
    every 1920x1080 source fail with "height not divisible by 2".)
    """
    if info['height'] >= info['width']:
        return f"scale=-2:'min({max_edge},ih)'"     # portrait: height is long
    return f"scale='min({max_edge},iw)':-2"         # landscape: width is long


def target_size(info, max_edge=PROXY_MAX_EDGE):
    """
    The same decision scale_filter makes, as NUMBERS.

    A hardware scaler cannot be handed scale's min()/-2 expressions:
    scale_vaapi and scale_qsv take integers. Computing the pair here keeps one
    definition of "long edge to max_edge, never upscale, even numbers" instead
    of one for software and a subtly different one per device.
    """
    width, height = int(info['width']), int(info['height'])
    longest = max(width, height)
    factor = 1.0 if longest <= max_edge else max_edge / float(longest)
    out = []
    for edge in (width, height):
        value = int(round(edge * factor))
        value -= value % 2            # H.264 needs even dimensions
        out.append(max(2, value))
    return out[0], out[1]


_vaapi_cache = []        # [] not yet looked, [None] looked and found nothing


def vaapi_device():
    """
    A render node whose VA-API driver can actually encode H.264, or None.

    Looked for once per process, lazily - never at import time, because this
    module is imported by the backend on every workflow and probing spawns
    ffmpeg.

    Each candidate is opened for real. This is the whole reason the function
    exists: on a machine with NVIDIA cards and the open nouveau driver,
    /dev/dri/renderD128 exists, is readable, and answers

        [AVHWDeviceContext] No VA display found for device /dev/dri/renderD128.

    the moment anything tries to use it (verified on oldserver, 2026-10-05) -
    because NVENC lives in the closed driver and nouveau exposes no VA-API
    encoder. A node that exists is not a node that encodes.
    """
    if _vaapi_cache:
        return _vaapi_cache[0]
    candidates = [VAAPI_DEVICE] if VAAPI_DEVICE else sorted(
        glob.glob('/dev/dri/renderD*'))
    for node in candidates:
        if not node or not os.path.exists(node):
            continue
        if _probe_once('h264_vaapi', device=node):
            _vaapi_cache.append(node)
            return node
    _vaapi_cache.append(None)
    return None


def init_args(encoder):
    """Device set-up that must come BEFORE -i, or nothing after it can use it."""
    if encoder == 'h264_vaapi':
        device = vaapi_device()
        return ['-vaapi_device', device] if device else []
    return []


def filter_args(encoder, info, max_edge=PROXY_MAX_EDGE):
    """
    The -vf chain. Software scales on the CPU; VA-API scales on the GPU, which
    means the frames have to get there first.

    format=nv12,hwupload,scale_vaapi is deliberately plain: the frames are
    decoded in software, uploaded, then scaled and encoded on the device. The
    faster shape - `-hwaccel vaapi -hwaccel_output_format vaapi`, decoding on
    the GPU too - fails outright on any source the device cannot decode (10-bit
    HEVC on older Intel, VP9 on anything pre-Kaby Lake), and failing the whole
    command loses the encode as well. One extra upload per frame buys a path
    that works for every source.

    QSV and V4L2 both accept system-memory frames and upload internally, so
    they need no special chain.
    """
    if encoder == 'h264_vaapi':
        width, height = target_size(info, max_edge)
        return f'format=nv12,hwupload,scale_vaapi={width}:{height}'
    return scale_filter(info, max_edge)


def encoder_args(encoder, crf=PROXY_CRF, preset='veryfast'):
    """The video-encoder half of the command. Always 8-bit 4:2:0 H.264.

    -pix_fmt yuv420p matters: iPhone HDR is 10-bit HEVC, and without it libx264
    keeps 10 bits and writes H.264 "High 10" - which Firefox and Chrome cannot
    play (found on oldserver, 2026-10-02). HLG, the iPhone's HDR curve, is made to
    look right on a standard display, so 8 bits are enough for a preview.
    """
    if encoder == 'h264_videotoolbox':
        return ['-c:v', 'h264_videotoolbox', '-q:v', str(VT_QUALITY),
                '-profile:v', 'high', '-pix_fmt', 'yuv420p']
    if encoder == 'h264_nvenc':
        return ['-c:v', 'h264_nvenc', '-preset', 'p5', '-rc', 'vbr', '-cq', str(crf),
                '-b:v', '0', '-profile:v', 'high', '-pix_fmt', 'yuv420p']
    if encoder == 'h264_qsv':
        # Intel's own encoder. -global_quality is its constant-quality knob and
        # tracks crf closely; nv12 because QSV's surfaces are nv12 and asking
        # for yuv420p adds a conversion for nothing.
        return ['-c:v', 'h264_qsv', '-global_quality', str(crf),
                '-preset', 'veryfast', '-profile:v', 'high', '-pix_fmt', 'nv12']
    if encoder == 'h264_vaapi':
        # NO -pix_fmt: after hwupload the frames are device surfaces, and
        # naming a software pixel format forces them back to the CPU, which
        # throws away the point of the device.
        return ['-c:v', 'h264_vaapi', '-rc_mode', 'CQP',
                '-qp', str(VAAPI_QP or crf), '-profile:v', 'high']
    if encoder == 'h264_v4l2m2m':
        # The Pi. Bitrate only - this encoder has no constant-quality mode -
        # and no -profile:v, which its wrapper rejects.
        return ['-c:v', 'h264_v4l2m2m', '-b:v', V4L2_BITRATE,
                '-pix_fmt', 'yuv420p']
    return ['-c:v', 'libx264', '-crf', str(crf), '-preset', preset,
            '-profile:v', 'high', '-pix_fmt', 'yuv420p']


def decoder_args(encoder):
    """Hardware decoding to go with a hardware encoder; frames come back to the
    CPU for the scale filter, which is what keeps one command for every path."""
    if encoder == 'h264_videotoolbox':
        return ['-hwaccel', 'videotoolbox']
    if encoder == 'h264_nvenc':
        return ['-hwaccel', 'cuda']
    # Deliberately nothing for qsv, vaapi and v4l2m2m. Hardware DECODE on those
    # is all-or-nothing within one command: a source the device cannot decode
    # (10-bit HEVC on pre-Icelake Intel, anything exotic on a Pi) fails the
    # command and costs the encode too. Software decode plus hardware encode
    # still moves the expensive half onto the device.
    return []


def proxy_command(src, dst, info, action, encoder=SOFTWARE, crf=PROXY_CRF,
                  preset='veryfast', max_edge=PROXY_MAX_EDGE):
    if action == 'remux':
        return ['ffmpeg', '-y', '-i', src, '-c', 'copy', '-movflags', '+faststart', dst]
    return (['ffmpeg', '-y'] + init_args(encoder) + decoder_args(encoder)
            + ['-i', src, '-vf', filter_args(encoder, info, max_edge)]
            + encoder_args(encoder, crf, preset)
            + ['-movflags', '+faststart',
               '-c:a', 'aac', '-b:a', '128k', '-ac', '2', dst])


def timeout_for(duration_seconds, factor=3):
    """A budget proportional to the material; a flat cap kills long videos mid-way."""
    return int(max(300, (duration_seconds or 0) * factor))


def _preexec(nice):
    if os.name != 'posix' or not nice or nice <= 0:
        return None

    def _lower():
        try:
            os.nice(nice)
        except OSError:
            pass
    return _lower


def _unlink(path):
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def make_proxy(src, proxy_path, temp_path, encoders=(SOFTWARE,), crf=PROXY_CRF,
               preset='veryfast', max_edge=PROXY_MAX_EDGE, nice=0, timeout_factor=3,
               info=None):
    """
    Make the proxy of `src` at `proxy_path`, writing to `temp_path` first (a
    killed ffmpeg must not leave a file that looks finished).

    Returns
        {'action': 'passthrough', 'duration': ...}
        {'action': 'remux'|'transcode', 'encoder': ..., 'seconds': ..., 'size': ...,
         'width': ..., 'height': ..., 'duration': ..., 'tried': [...]}
        {'action': None, 'error': ..., 'tried': [...]}     every encoder failed
        None                                                not a readable video

    `encoders` is tried in order, each once; a hardware encoder that fails (an
    unsupported profile, a 10-bit source on an old card) falls through to the
    next, and the software encoder is always appended last.
    """
    info = info or probe(src)
    if info is None:
        return None
    action = plan(info, src, max_edge)
    if action == 'passthrough':
        return {'action': 'passthrough', 'duration': info.get('duration') or None}

    # Only encoders this module knows how to drive; anything else would quietly
    # run as libx264 under another name.
    order = [e for e in encoders if e in HARDWARE] + [SOFTWARE]
    if action == 'remux':
        order = ['copy']
    tried, last_error = [], None
    for encoder in order:
        cmd = proxy_command(src, temp_path, info, action,
                            SOFTWARE if encoder == 'copy' else encoder, crf, preset, max_edge)
        started = time.monotonic()
        try:
            result = subprocess.run(cmd, capture_output=True, text=True,
                                    timeout=timeout_for(info['duration'], timeout_factor),
                                    preexec_fn=_preexec(nice))
            ok = result.returncode == 0 and os.path.exists(temp_path)
            err = (result.stderr or '').strip()[-400:]
        except subprocess.TimeoutExpired:
            ok, err = False, 'timed out'
        tried.append(encoder)
        if ok:
            os.replace(temp_path, proxy_path)
            out = probe(proxy_path) or {}
            return {
                'action': action, 'encoder': encoder,
                'seconds': round(time.monotonic() - started, 1),
                'size': os.path.getsize(proxy_path),
                'width': out.get('width') or info['width'],
                'height': out.get('height') or info['height'],
                # From the SOURCE: a transcode can land a frame short, and the
                # number shown should describe the video the user gave us.
                'duration': info.get('duration') or out.get('duration') or None,
                'tried': tried,
            }
        _unlink(temp_path)
        last_error = f'{encoder}: {err}'
        print(f'[video] {action} with {encoder} failed for {os.path.basename(src)}: {err}')
    return {'action': None, 'error': last_error, 'tried': tried}


def make_poster(src, poster_path, temp_path, size=POSTER_SIZE, nice=0, info=None):
    """A square still, cropped (never squashed) to size x size. True on success."""
    info = info or probe(src) or {}
    duration = info.get('duration') or 0.0
    seek = POSTER_SEEK_SECONDS if duration > POSTER_SEEK_SECONDS * 2 else 0.0
    cmd = ['ffmpeg', '-y', '-ss', str(seek), '-i', src, '-frames:v', '1',
           '-vf', (f'scale={size}:{size}:force_original_aspect_ratio=increase,'
                   f'crop={size}:{size}'),
           '-q:v', '3', temp_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                preexec_fn=_preexec(nice))
        if result.returncode != 0 or not os.path.exists(temp_path):
            print(f'[video] poster failed for {os.path.basename(src)}: '
                  f'{(result.stderr or "").strip()[-300:]}')
            _unlink(temp_path)
            return False
        os.replace(temp_path, poster_path)
        return True
    except Exception as exc:
        print(f'[video] poster error for {os.path.basename(src)}: {exc}')
        _unlink(temp_path)
        return False


# The source every probe encodes: one small frame, generated in ffmpeg, so a
# probe needs no sample file on disk and cannot be slow.
_PROBE_INFO = {'width': 256, 'height': 144}


def _probe_once(encoder, device=None):
    """
    One frame through `encoder`, as the real command would run it. True on
    success.

    THE SHAPE MATTERS. This builds the device flags and the filter chain the
    same way proxy_command does, because the failures worth catching are in
    exactly those parts: a VA-API node with no driver behind it, a QSV library
    that loads but finds no device, a Pi encoder whose /dev/video11 is not
    passed into the container. A probe that only tried `-c:v <enc>` would pass
    on all three and every real encode would then fail.

    `device` overrides the discovered VA-API node, which is how vaapi_device()
    tests candidates without recursing into itself.
    """
    init = ['-vaapi_device', device] if (device and encoder == 'h264_vaapi') \
        else init_args(encoder)
    if encoder == 'h264_vaapi' and not init:
        return False          # no usable node, so nothing to probe
    cmd = (['ffmpeg', '-v', 'error'] + init
           + ['-f', 'lavfi', '-i', 'testsrc=size=256x144:rate=1', '-frames:v', '1',
              '-vf', filter_args(encoder, _PROBE_INFO, PROXY_MAX_EDGE)]
           + encoder_args(encoder) + ['-f', 'null', '-'])
    try:
        return subprocess.run(cmd, capture_output=True, timeout=30).returncode == 0
    except Exception:
        return False


def usable_encoders(candidates=HARDWARE):
    """
    The hardware encoders that actually work here, best first.

    Being listed by `ffmpeg -encoders` is not enough - Debian's build lists
    h264_nvenc, h264_qsv and h264_vaapi on a machine with no GPU at all - so
    each one is proved by encoding a frame with it.

    Returning [] is a perfectly good answer and the common one: a machine with
    no usable device encodes in software, which is why nothing here raises.
    """
    return [enc for enc in candidates if _probe_once(enc)]
