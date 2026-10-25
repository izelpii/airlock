#!/usr/bin/env python3
"""
backend-transcode: video previews as a service.

documentation/specs/accelerated-services-spec.md, section 3.2.

The one heavy job in airlock that had no service boundary. Inside the backend
container ffmpeg can never reach VideoToolbox (macOS) or an NVIDIA card's
NVENC/NVDEC, so this owns ffmpeg instead, and runs wherever the hardware is:
natively on a Mac (start-host.sh), in a container elsewhere (start-podman.sh),
with the GPU passed in when there is one.

It works on FILES, by catalog-relative path, never on uploaded bytes: a video
is gigabytes, and streaming it here and back would double the disk work this
exists to save. Every path is resolved under CATALOGS_ROOT and refused if it
escapes its catalog; outputs may only be written under that catalog's cache/.

The decisions - passthrough, remux, transcode, the scale, 8-bit H.264 - come
from lib/video_proxy.py, the same module the import uses when this service is
not running, so a preview does not depend on which of the two made it.

    POST /transcode  {"catalog", "source", "proxy", "max_edge"?, "crf"?}
    POST /poster     {"catalog", "source", "poster", "size"?}
    GET  /health
"""

import os
import sys
import threading
import time

from flask import Flask, jsonify, request

HERE = os.path.dirname(os.path.abspath(__file__))
# The shared module: the backend's own copy when run natively from the
# repository (never a stale vendored one), or the copy vendored into the image.
for candidate in (os.path.join(HERE, '..', 'backend', 'workflows', 'lib'), HERE):
    if os.path.exists(os.path.join(candidate, 'video_proxy.py')):
        sys.path.insert(0, os.path.abspath(candidate))
        break
import video_proxy as vp  # noqa: E402

app = Flask(__name__)

CATALOGS_ROOT = os.path.realpath(os.environ.get('CATALOGS_ROOT', '/catalogs'))
MODE = os.environ.get('TRANSCODE_MODE', 'container')
# How many encodes run at once. Consumer NVIDIA cards limit concurrent NVENC
# sessions, and a software encode already uses every core.
JOBS = max(1, int(os.environ.get('TRANSCODE_JOBS', '2') or 2))
# Software encodes run below everything else on the machine.
NICE = int(os.environ.get('TRANSCODE_NICE', '10') or 10)
PRESET = os.environ.get('TRANSCODE_PRESET', 'veryfast')
# 'auto' = the best hardware encoder that works here, then software. Naming one
# forces it: libx264, h264_videotoolbox (macOS), h264_nvenc (NVIDIA's closed
# driver), h264_qsv (Intel Quick Sync), h264_vaapi (Intel/AMD), h264_v4l2m2m
# (Raspberry Pi 4). A named hardware encoder is still PROVED before use, and
# still falls back to software per request if it fails on a given file.
WANT = os.environ.get('TRANSCODE_ENCODER', 'auto')

_slots = threading.BoundedSemaphore(JOBS)
_started = time.time()
_stats = {'transcodes': 0, 'posters': 0, 'failures': 0, 'by_encoder': {}}
_stats_lock = threading.Lock()


def _probe_encoders():
    if not vp.available():
        return []
    if WANT == 'auto':
        return vp.usable_encoders()
    if WANT == vp.SOFTWARE:
        return []
    return vp.usable_encoders((WANT,))


HARDWARE = _probe_encoders()
print(f'backend-transcode: mode {MODE}, catalogs {CATALOGS_ROOT}, '
      f'encoders {HARDWARE + [vp.SOFTWARE]}, {JOBS} job(s) at once', flush=True)


class PathError(Exception):
    pass


def _resolve(catalog, rel, must_exist=False, under_cache=False):
    """
    An absolute path for `rel` inside catalog `catalog`, or PathError.
    Containment is checked on the REAL path, so a symlink cannot lead out; the
    catalog's own originals folders may be bind mounts from another disk, which
    realpath does not follow out of, so they stay inside.
    """
    if not isinstance(catalog, str) or not catalog or catalog.startswith(('/', '.')) \
            or '..' in catalog.split('/'):
        raise PathError('catalog must be a folder name under the catalogs root')
    if not isinstance(rel, str) or not rel or os.path.isabs(rel) or '..' in rel.split('/'):
        raise PathError('paths must be relative to the catalog, without ..')
    root = os.path.realpath(os.path.join(CATALOGS_ROOT, catalog))
    if not root.startswith(CATALOGS_ROOT + os.sep) or not os.path.isdir(root):
        raise PathError(f'no catalog {catalog!r} here')
    full = os.path.join(root, rel)
    real = os.path.realpath(full if must_exist else os.path.dirname(full))
    if real != root and not real.startswith(root + os.sep):
        raise PathError('path is outside the catalog')
    if under_cache:
        cache = os.path.join(root, 'cache') + os.sep
        if not os.path.realpath(os.path.dirname(full)).startswith(cache) \
                and not os.path.abspath(full).startswith(cache):
            raise PathError('outputs are written under cache/ only')
    if must_exist and not os.path.isfile(full):
        raise PathError('source not found')
    return full


def _count(key, encoder=None):
    with _stats_lock:
        _stats[key] += 1
        if encoder:
            _stats['by_encoder'][encoder] = _stats['by_encoder'].get(encoder, 0) + 1


@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        'status': 'ok' if vp.available() else 'no ffmpeg',
        'mode': MODE,
        'encoders': HARDWARE + [vp.SOFTWARE],
        'hardware': HARDWARE,
        # Which DRM node the VA-API encoder opened, when it is one of them.
        # Worth reporting: a machine with two render nodes gets exactly one
        # that can encode, and "which one" is otherwise unknowable from
        # outside - see video_proxy.vaapi_device.
        'vaapi_device': vp.vaapi_device() if 'h264_vaapi' in HARDWARE else None,
        'jobs': JOBS,
        'catalogs_root': CATALOGS_ROOT,
        'uptime_seconds': int(time.time() - _started),
        'stats': _stats,
    })


@app.route('/transcode', methods=['POST'])
def transcode():
    data = request.get_json(silent=True) or {}
    try:
        src = _resolve(data.get('catalog'), data.get('source'), must_exist=True)
        dst = _resolve(data.get('catalog'), data.get('proxy'), under_cache=True)
        max_edge = int(data.get('max_edge') or vp.PROXY_MAX_EDGE)
        crf = int(data.get('crf') or vp.PROXY_CRF)
        if not (64 <= max_edge <= 4096) or not (0 <= crf <= 51):
            raise PathError('max_edge or crf out of range')
    except (PathError, ValueError, TypeError) as exc:
        return jsonify({'error': str(exc)}), 400
    if not vp.available():
        return jsonify({'error': 'ffmpeg is not installed here'}), 503

    os.makedirs(os.path.dirname(dst), exist_ok=True)
    stem, ext = os.path.splitext(dst)
    temp = f'{stem}.partial.t{os.getpid()}-{threading.get_ident()}{ext}'
    with _slots:
        result = vp.make_proxy(src, dst, temp, encoders=HARDWARE, crf=crf, preset=PRESET,
                               max_edge=max_edge, nice=NICE)
    if result is None:
        _count('failures')
        return jsonify({'error': 'not a readable video'}), 422
    if result.get('action') is None:
        _count('failures')
        return jsonify({'error': result.get('error'), 'tried': result.get('tried')}), 500
    _count('transcodes', result.get('encoder'))
    return jsonify(result)


@app.route('/poster', methods=['POST'])
def poster():
    data = request.get_json(silent=True) or {}
    try:
        src = _resolve(data.get('catalog'), data.get('source'), must_exist=True)
        dst = _resolve(data.get('catalog'), data.get('poster'), under_cache=True)
        size = int(data.get('size') or vp.POSTER_SIZE)
        if not (32 <= size <= 2048):
            raise PathError('size out of range')
    except (PathError, ValueError, TypeError) as exc:
        return jsonify({'error': str(exc)}), 400
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    stem, ext = os.path.splitext(dst)
    temp = f'{stem}.partial.t{os.getpid()}-{threading.get_ident()}{ext}'
    if not vp.make_poster(src, dst, temp, size=size, nice=NICE):
        _count('failures')
        return jsonify({'error': 'poster failed'}), 500
    _count('posters')
    return jsonify({'size': os.path.getsize(dst), 'width': size, 'height': size})


if __name__ == '__main__':
    app.run(host='127.0.0.1', port=int(os.environ.get('PORT', '5004')))
