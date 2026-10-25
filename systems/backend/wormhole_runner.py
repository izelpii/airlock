"""
wormhole_runner.py — the process that actually moves the bytes.

See documentation/specs/wormhole-album-transfer-spec.md.

Run as a SUBPROCESS, never imported by the backend. magic-wormhole is Twisted and
the backend is gunicorn with gevent monkey-patching; a Twisted reactor inside a
gevent-patched process is a fight with no upside. The reference application at
/Users/izelpi/Personal/Development/wormhole-ui reached the same isolation by shelling
out to the `wormhole` CLI - this reaches it with our own script, which is what lets
the protocol carry a manifest and a resume offset (spec 3.1, 3.2).

This script TOUCHES NO DATABASE. It reports on stdout, one JSON object per line, and
the parent owns all state. That keeps SQLite single-writer, and means a killed runner
can never leave a half-written row.

    python wormhole_runner.py --mode send    --transfer-id 7 --manifest /tmp/m.json
    python wormhole_runner.py --mode receive --transfer-id 8 --code 7-foo-bar \
                              --dest /catalogs/family/incoming/summer

The manifest arrives as a FILE, not on the command line: an album of thousands of
files exceeds ARG_MAX, and a process listing should not carry the whole album.
"""

import argparse
import hashlib
import json
import os
import re
import shutil
import sys
import time

from twisted.internet import reactor, defer
from twisted.internet.protocol import Factory, Protocol
from twisted.internet.task import react

import wormhole

# A wire name, like SUBPROTOCOL below: both ends must agree. It kept the shoebox
# name through the rename to airlock (v5.6) and changed in v6.3.1, deliberately
# and as a clean break: an install on v6.3.0 or earlier and one on v6.3.1 or later
# cannot exchange albums, and neither reports an error - each waits for a peer
# under a name the other never registers. The rendezvous server keeps a separate
# list of codes per APPID, which is also why the standard `wormhole receive`
# client can never meet an Airlock sender.
APPID = 'airlock.app/album-transfer'
RELAY = os.environ.get('WORMHOLE_RELAY_URL',
                       'ws://relay.magic-wormhole.io:4000/v1')

# THE TRANSIT RELAY, AND WHY IT IS NOT OPTIONAL HERE.
#
# The rendezvous server above only carries the code exchange. The bulk data
# rides a dilated connection, and dilation is given its relay as an ARGUMENT:
# wormhole/_dilation/connector.py does
#
#     if self._transit_relay_location: ... else: self._transit_relays = []
#
# so calling w.dilate() without one leaves dilation with NO relay and only
# direct connections to try. Two peers on one host find each other that way and
# everything looks fine; two peers on different machines - each inside a
# rootless container advertising an address only its own host can route to -
# never connect, and dilation retries silently rather than failing. The symptom
# is a transfer that sits at "waiting for the other side" forever with no error
# on either end, which reads as a hung UI rather than a network dead end.
TRANSIT_RELAY = (os.environ.get('WORMHOLE_TRANSIT_RELAY', '').strip()
                 or 'tcp:transit.magic-wormhole.io:4001')
# A wire name both ends must agree on, renamed with APPID in v6.3.1 - one break
# rather than two.
SUBPROTOCOL = 'airlock-album-transfer'
PROTOCOL_VERSION = 1

# Large enough that per-write overhead disappears, small enough that progress stays
# smooth and a cancel is felt immediately.
CHUNK = 64 * 1024

# How long the sender waits for the receiver to confirm it has the whole album
# before closing the wormhole, and how long the receiver waits for files that
# are still in flight once the peer says it has sent everything. Both exist to
# bound a wait that would otherwise be forever; neither is reached in a healthy
# transfer.
ACK_TIMEOUT = 120.0
INFLIGHT_GRACE = 60.0

# How long a sender may take to close its wormhole once its code has expired,
# before the reactor is stopped regardless. Closing tells the rendezvous server to
# free the nameplate now rather than prune it 11 minutes later; it is a courtesy,
# and an unreachable server must not keep an expired code's process alive.
EXPIRE_CLOSE_GRACE = 15.0


# ── event reporting ───────────────────────────────────────────────────────────

def emit(event, **fields):
    """One JSON object per line on stdout. The parent's watcher reads these."""
    fields['event'] = event
    sys.stdout.write(json.dumps(fields) + '\n')
    sys.stdout.flush()


def fail(message):
    emit('error', message=str(message))


# ── framing ───────────────────────────────────────────────────────────────────
#
# Every subchannel opens with one JSON header terminated by \n. Control messages
# are JSON lines; a file subchannel is a header line then raw bytes until the
# sender closes its write side.

class LineAndBytes(Protocol):
    """
    Reads newline-terminated JSON headers, then hands the rest over as raw bytes.

    Framing has to be explicit because a subchannel is a stream: the header and the
    first bytes of a 25 MB photo can and do arrive in the same packet.
    """

    def __init__(self):
        self._buf = b''
        self._header_done = False

    def dataReceived(self, data):
        if self._header_done:
            self.rawReceived(data)
            return
        self._buf += data
        if b'\n' not in self._buf:
            return
        line, rest = self._buf.split(b'\n', 1)
        self._buf = b''
        self._header_done = True
        try:
            header = json.loads(line.decode('utf-8'))
        except ValueError as exc:
            self.transport.loseConnection()
            fail(f'unreadable header: {exc}')
            return
        self.headerReceived(header)
        if rest:
            self.rawReceived(rest)

    def headerReceived(self, header):
        pass

    def rawReceived(self, data):
        pass


def send_json(transport, obj):
    transport.write((json.dumps(obj) + '\n').encode('utf-8'))


# ── sender ────────────────────────────────────────────────────────────────────

class FileProducer:
    """
    Streams one file to a subchannel with backpressure.

    A push producer, not a loop of transport.write(): a 4 GB album written as fast
    as the disk can read it would queue in memory until the process died. SubChannel
    implements pauseProducing/resumeProducing, so this is the flow control the
    transport already expects (spec 2.2).
    """

    def __init__(self, path, offset, length, transport, on_chunk, on_done):
        self._fh = open(path, 'rb')
        self._fh.seek(offset)
        self._remaining = length
        self._transport = transport
        self._on_chunk = on_chunk
        self._on_done = on_done
        self._paused = False
        self._finished = False

    def start(self):
        self._transport.registerProducer(self, True)
        self.resumeProducing()

    def resumeProducing(self):
        self._paused = False
        while not self._paused and not self._finished:
            if self._remaining <= 0:
                self._complete()
                return
            data = self._fh.read(min(CHUNK, self._remaining))
            if not data:
                # The file is shorter than the manifest said. Stop here and let
                # the receiver's hash check be the thing that objects - a short
                # write with a matching hash is impossible, so nothing silently
                # lands wrong.
                self._complete()
                return
            self._remaining -= len(data)
            self._transport.write(data)
            self._on_chunk(len(data))

    def pauseProducing(self):
        self._paused = True

    def stopProducing(self):
        self._finished = True
        self._close()

    def _complete(self):
        if self._finished:
            return
        self._finished = True
        self._close()
        try:
            self._transport.unregisterProducer()
        except Exception:
            pass
        self._on_done()

    def _close(self):
        try:
            self._fh.close()
        except Exception:
            pass


class ControlSender(LineAndBytes):
    """Sends the manifest, waits for the peer's `have` reply, then its ack."""

    def __init__(self, manifest, on_have, on_complete):
        LineAndBytes.__init__(self)
        self._manifest = manifest
        self._on_have = on_have
        self._on_complete = on_complete
        self._line_buf = b''

    def connectionMade(self):
        send_json(self.transport, {
            'type': 'manifest',
            'version': PROTOCOL_VERSION,
            'album': self._manifest['album'],
            'files': self._manifest['files'],
        })

    def headerReceived(self, header):
        if header.get('type') == 'have':
            self._on_have(header.get('files') or [])

    def rawReceived(self, data):
        # Everything after the `have` header is JSON lines. Previously this was
        # not implemented at all, so anything the receiver said after `have` was
        # silently discarded - which is why the sender had no way to know
        # whether the album had landed.
        self._line_buf += data
        while b'\n' in self._line_buf:
            line, self._line_buf = self._line_buf.split(b'\n', 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode('utf-8'))
            except ValueError:
                continue
            if msg.get('type') == 'complete':
                self._on_complete(msg)


def read_deadline(path):
    """The unix time in an expiry file, or None if it cannot be read."""
    try:
        with open(path, encoding='utf-8') as fh:
            return float(fh.read().strip())
    except (OSError, ValueError):
        return None


# Set once a code has expired, so the errback that closing the wormhole provokes
# is not reported as a failure: an expired code is an outcome, not an error.
EXPIRED = {'value': False}


class Deadline:
    """
    Ends a code nobody has used by its deadline (album-mass-transfer-spec.md 3.5).

    The deadline lives in a FILE, not an argument, because it can move: new codes
    for a group move the group's one deadline, and members still waiting on an
    old code adopt it (spec 3.6). The file is read again when the timer fires; a
    later time there reschedules instead of expiring.

    Disarmed the moment the peer connects. From then on the code has been used,
    and a transfer in progress is never cut off by a clock (spec 3.4).
    """

    def __init__(self, path, w):
        self.path = path
        self.w = w
        self.call = None
        self.deadline = read_deadline(path)

    def arm(self):
        if self.deadline is None:
            # An unreadable file at start is a bug in the parent, not a reason
            # to wait forever silently.
            emit('note', message=f'expiry file unreadable: {self.path}; '
                                 'this code has no deadline')
            return
        self.call = reactor.callLater(max(0.0, self.deadline - time.time()), self.fire)

    def disarm(self):
        if self.call is not None and self.call.active():
            self.call.cancel()
        self.call = None

    def fire(self):
        self.call = None
        latest = read_deadline(self.path)
        if latest is not None and latest > time.time() + 1:
            self.deadline = latest
            self.call = reactor.callLater(latest - time.time(), self.fire)
            return
        EXPIRED['value'] = True
        emit('expired')
        d = self.w.close()
        d.addBoth(lambda _: reactor.stop() if reactor.running else None)
        reactor.callLater(EXPIRE_CLOSE_GRACE,
                          lambda: reactor.stop() if reactor.running else None)


@defer.inlineCallbacks
def run_send(manifest_path, expires_file=None):
    manifest = json.load(open(manifest_path, encoding='utf-8'))
    files = manifest['files']
    by_hash = {f['sha256']: f for f in files}

    w = wormhole.create(APPID, RELAY, reactor, dilation=True)
    w.allocate_code()
    code = yield w.get_code()
    # The user needs this before anything else can happen, so it goes out the
    # instant it exists rather than after the peer connects.
    emit('code', code=code)

    deadline = None
    if expires_file:
        deadline = Deadline(expires_file, w)
        deadline.arm()

    dw = w.dilate(transit_relay_location=TRANSIT_RELAY,
                  expected_subprotocols=[SUBPROTOCOL])
    yield dw.when_dilated()
    if deadline is not None:
        deadline.disarm()
    emit('connected')

    connector = dw.connector_for(SUBPROTOCOL)

    have_d = defer.Deferred()
    complete_d = defer.Deferred()

    def on_complete(msg):
        if not complete_d.called:
            complete_d.callback(msg)

    control = yield connector.connect(
        Factory.forProtocol(
            lambda: ControlSender(manifest, have_d.callback, on_complete)))
    have = yield have_d

    have_by_hash = {h['sha256']: h for h in have}

    for entry in files:
        sha = entry['sha256']
        size = int(entry['size'] or 0)
        peer = have_by_hash.get(sha) or {}
        offset = int(peer.get('bytes') or 0)

        if peer.get('complete') and offset >= size:
            emit('file_skipped', sha256=sha, name=entry['name'], size=size)
            continue
        if offset > size:
            offset = 0          # the peer has something we did not send; resend

        path = entry['path']
        if not os.path.exists(path):
            emit('file_failed', sha256=sha, name=entry['name'],
                 message=f'original missing on disk: {path}')
            continue

        emit('file_start', sha256=sha, name=entry['name'], size=size,
             offset=offset)

        done_d = defer.Deferred()
        proto = yield connector.connect(Factory.forProtocol(Protocol))
        send_json(proto.transport, {
            'type': 'file', 'sha256': sha, 'name': entry['name'],
            'size': size, 'offset': offset, 'length': size - offset,
        })

        sent = [offset]

        def on_chunk(n, _sha=sha, _sent=sent, _size=size):
            _sent[0] += n
            emit('file_progress', sha256=_sha, sent_bytes=_sent[0], size=_size)

        producer = FileProducer(
            path, offset, size - offset, proto.transport,
            on_chunk, lambda: done_d.callback(None))
        producer.start()
        yield done_d
        # Closing the write side is the end-of-file marker: the receiver has no
        # other way to know a stream that arrived in pieces is finished.
        proto.transport.loseConnection()
        emit('file_done', sha256=sha, name=entry['name'], size=size)

    send_json(control.transport, {'type': 'finished'})

    # WAIT FOR THE RECEIVER BEFORE CLOSING.
    #
    # w.close() tears down the dilated connection, and anything still in flight
    # goes with it. The last file's loseConnection() is issued microseconds
    # earlier on a different subchannel, so closing here raced it: the sender
    # reported every file sent while the receiver sat forever at N-1, never
    # having seen the close that completes a file. Waiting for the peer's ack
    # removes the race entirely - the connection stays up until the album is
    # published on the other side.
    #
    # The timeout is for a peer that cannot answer (an older build, or one that
    # died). Closing anyway is right there: the bytes are sent, and the receiver
    # keeps its staged copy to resume from.
    complete_d.addTimeout(ACK_TIMEOUT, reactor)
    try:
        ack = yield complete_d
    except Exception:
        emit('note', message='peer did not acknowledge within '
                             f'{int(ACK_TIMEOUT)}s; closing anyway')
    else:
        if not ack.get('ok'):
            emit('note', message=f"peer is missing {ack.get('missing')} file(s); "
                                 'they stay staged for a resume')
    emit('finished')
    yield w.close()


# ── receiver ──────────────────────────────────────────────────────────────────

_SAFE = re.compile(r'[^A-Za-z0-9._()\-]')


def safe_name(name):
    """
    A peer is not trusted to name a file into another directory.

    basename() first, then a character filter, then a fallback: '..' and '' both
    reduce to nothing usable, and a file with no name must still land somewhere
    inspectable rather than aborting the transfer.
    """
    base = os.path.basename(name or '')
    base = _SAFE.sub('_', base).strip()
    if not base or base in ('.', '..'):
        base = 'received.bin'
    return base[:200]


def unique_path(directory, name):
    """`photo.jpg`, then `photo (2).jpg` - never an overwrite."""
    candidate = os.path.join(directory, name)
    if not os.path.exists(candidate):
        return candidate
    stem, ext = os.path.splitext(name)
    n = 2
    while True:
        candidate = os.path.join(directory, f'{stem} ({n}){ext}')
        if not os.path.exists(candidate):
            return candidate
        n += 1


class ReceiveSide:
    """
    Shared state for one receiving session.

    NOTHING is written into `dest` until the whole transfer is complete. A
    receive stages everything under `state_dir`, which lives in the catalog's
    cache and which no workflow walks:

        <state>/parts/    partial blobs, named by sha256, plus completed.json
        <state>/ready/    files that arrived whole and verified, final names
        dest              created by ONE atomic rename of ready/, at the end

    Per-file safety was already there before this staging existed - a file only
    left parts/ once its full-file digest matched. What staging adds is
    ALBUM-level atomicity: a transfer that fails, or is abandoned half way,
    leaves nothing at all in the folder a workflow ingests, rather than leaving
    a partial album that looks finished.

    The rename is the reason ready/ is a directory rather than a list of moves:
    moving N files one at a time is not atomic, and a crash midway through would
    reintroduce exactly the half-published state this is here to prevent.
    """

    def __init__(self, dest, state_dir=None):
        self.dest = dest
        base = state_dir or os.path.join(os.path.dirname(dest),
                                         '.staging-' + os.path.basename(dest))
        self.partial_dir = os.path.join(base, 'parts')
        self.ready_dir = os.path.join(base, 'ready')
        self.base_dir = base
        os.makedirs(self.partial_dir, exist_ok=True)
        os.makedirs(self.ready_dir, exist_ok=True)
        self.manifest = {}
        self.by_hash = {}
        self.finished = defer.Deferred()
        # Completion is not a single moment on this side. The peer's `finished`
        # and the close of the last file subchannel are separate events on
        # separate streams, and they DO arrive in either order - see try_finish.
        self.peer_finished = False
        self.published = False
        self.control = None
        self._grace = None

    def partial_path(self, sha):
        return os.path.join(self.partial_dir, sha)

    def scan(self, files):
        """
        What we already have, as the `have` reply.

        A file counts as complete only when the record says so AND it is still
        staged in ready/. The two can disagree - a published transfer takes
        ready/ away - and believing the record alone would answer "I have it"
        about a file that is no longer there.
        """
        out = []
        done_marker = os.path.join(self.partial_dir, 'completed.json')
        completed = {}
        if os.path.exists(done_marker):
            try:
                completed = json.load(open(done_marker, encoding='utf-8'))
            except ValueError:
                completed = {}
        self._completed = completed
        self._done_marker = done_marker
        for entry in files:
            sha = entry['sha256']
            staged = completed.get(sha)
            if staged and os.path.exists(os.path.join(self.ready_dir, staged)):
                out.append({'sha256': sha, 'bytes': int(entry['size'] or 0),
                            'complete': True})
                continue
            p = self.partial_path(sha)
            if os.path.exists(p):
                out.append({'sha256': sha, 'bytes': os.path.getsize(p),
                            'complete': False})
        return out

    def missing(self):
        """Manifest files that are not staged and verified. Empty means publish."""
        out = []
        for sha, entry in self.by_hash.items():
            staged = getattr(self, '_completed', {}).get(sha)
            if not (staged and os.path.exists(os.path.join(self.ready_dir, staged))):
                out.append(entry.get('name') or sha)
        return out

    def publish(self):
        """
        Move the finished album into place, all at once.

        One rename when the destination does not exist yet, which is the normal
        case and is genuinely atomic on a single filesystem - ready/ and dest are
        both inside the catalog directory. The per-file fallback exists only for
        a destination that somehow already exists; it is not atomic, and says so.
        """
        parent = os.path.dirname(self.dest)
        if parent:
            os.makedirs(parent, exist_ok=True)
        if not os.path.exists(self.dest):
            os.replace(self.ready_dir, self.dest)
            atomic = True
        else:
            for name in sorted(os.listdir(self.ready_dir)):
                os.replace(os.path.join(self.ready_dir, name),
                           unique_path(self.dest, name))
            try:
                os.rmdir(self.ready_dir)
            except OSError:
                pass
            atomic = False
        # The record described what was staged; the staging is gone.
        shutil.rmtree(self.base_dir, ignore_errors=True)
        return atomic

    def try_finish(self):
        """
        Publish once the peer has sent everything AND everything has landed.

        WHY THIS IS NOT A SINGLE CHECK IN THE `finished` HANDLER. A file is only
        complete on this side when its subchannel CLOSES, and the control
        channel's `finished` travels on a different stream. The last file's
        close and `finished` are therefore racing, and the loser is usually the
        file: the sender calls loseConnection() and immediately writes
        `finished`. Checking only when `finished` arrives declared that file
        missing and left a 16-file album staged at 15.

        So both events call this, and whichever happens last does the work.
        """
        if self.published or not self.peer_finished:
            return
        if self.missing():
            return                      # a subchannel close will call back here
        self.published = True
        if self._grace and self._grace.active():
            self._grace.cancel()
        atomic = self.publish()
        emit('published', destination=self.dest, atomic=atomic)
        # Tell the peer before it tears the connection down. Without this the
        # sender closes the wormhole the instant it has written the last byte,
        # which is what made the last file's close unreliable in the first place.
        if self.control is not None:
            send_json(self.control, {'type': 'complete', 'ok': True})
        emit('finished')
        if not self.finished.called:
            self.finished.callback(None)

    def give_up(self):
        """The grace period expired with files still missing: stay staged."""
        if self.published or self.finished.called:
            return
        missing = self.missing()
        emit('incomplete', missing=len(missing), examples=missing[:5],
             staged=self.base_dir)
        if self.control is not None:
            send_json(self.control, {'type': 'complete', 'ok': False,
                                     'missing': len(missing)})
        emit('finished')
        self.finished.callback(None)

    def mark_completed(self, sha, final_path):
        self._completed[sha] = os.path.basename(final_path)
        tmp = self._done_marker + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(self._completed, fh)
        os.replace(tmp, self._done_marker)


class ControlReceiver(LineAndBytes):
    def __init__(self, side):
        LineAndBytes.__init__(self)
        self._side = side
        self._rest = b''

    def headerReceived(self, header):
        if header.get('type') != 'manifest':
            return
        side = self._side
        # Kept so the album can be acknowledged on this same channel later.
        side.control = self.transport
        side.manifest = header
        side.by_hash = {f['sha256']: f for f in header.get('files') or []}
        emit('manifest',
             album=header.get('album') or {},
             files=[{'sha256': f['sha256'], 'name': f['name'],
                     'size': f.get('size') or 0}
                    for f in header.get('files') or []])
        have = side.scan(header.get('files') or [])
        send_json(self.transport, {'type': 'have', 'files': have})
        for h in have:
            if h['complete']:
                entry = side.by_hash.get(h['sha256']) or {}
                emit('file_skipped', sha256=h['sha256'],
                     name=entry.get('name', ''), size=entry.get('size', 0))

    def rawReceived(self, data):
        # Control carries JSON lines after the manifest; `finished` is the only one.
        self._rest += data
        while b'\n' in self._rest:
            line, self._rest = self._rest.split(b'\n', 1)
            if not line.strip():
                continue
            try:
                msg = json.loads(line.decode('utf-8'))
            except ValueError:
                continue
            if msg.get('type') == 'finished':
                side = self._side
                side.peer_finished = True
                # Files may still be closing. try_finish publishes if they are
                # all in, and the grace timer is the backstop for the case where
                # one genuinely never arrives - it stays staged and resumable.
                side.try_finish()
                if not side.finished.called and side._grace is None:
                    side._grace = reactor.callLater(INFLIGHT_GRACE, side.give_up)


class FileReceiver(LineAndBytes):
    def __init__(self, side):
        LineAndBytes.__init__(self)
        self._side = side
        self._fh = None
        self._sha = None
        self._name = None
        self._size = 0
        self._written = 0
        self._hasher = None

    def headerReceived(self, header):
        if header.get('type') != 'file':
            self.transport.loseConnection()
            return
        self._sha = header['sha256']
        self._name = safe_name(header.get('name'))
        self._size = int(header.get('size') or 0)
        offset = int(header.get('offset') or 0)
        path = self._side.partial_path(self._sha)

        # Rehash what is already on disk so the digest covers the whole file, not
        # only the part this session carried. Without it a resumed file could not
        # be verified at all, which is the same as not verifying it.
        self._hasher = hashlib.sha256()
        mode = 'r+b' if (offset and os.path.exists(path)) else 'wb'
        self._fh = open(path, mode)
        if mode == 'r+b':
            read = 0
            while read < offset:
                block = self._fh.read(min(CHUNK, offset - read))
                if not block:
                    break
                self._hasher.update(block)
                read += len(block)
            self._fh.seek(read)
            self._written = read
        emit('file_start', sha256=self._sha, name=self._name,
             size=self._size, offset=self._written)

    def rawReceived(self, data):
        if not self._fh:
            return
        self._fh.write(data)
        self._hasher.update(data)
        self._written += len(data)
        emit('file_progress', sha256=self._sha, sent_bytes=self._written,
             size=self._size)

    def connectionLost(self, reason):
        if not self._fh:
            return
        try:
            self._fh.close()
        except Exception:
            pass
        path = self._side.partial_path(self._sha)
        digest = self._hasher.hexdigest()
        if digest != self._sha:
            # A silently corrupt original is the worst possible outcome, so the
            # partial is destroyed rather than left to look resumable.
            try:
                os.remove(path)
            except OSError:
                pass
            emit('file_failed', sha256=self._sha, name=self._name,
                 message=f'checksum mismatch (got {digest[:12]}…)')
            return
        # Into ready/, not into the destination: nothing reaches the folder a
        # workflow ingests until the whole transfer is complete.
        final = unique_path(self._side.ready_dir, self._name)
        os.replace(path, final)
        self._side.mark_completed(self._sha, final)
        emit('file_done', sha256=self._sha, name=os.path.basename(final),
             size=self._size)
        # If this was the file the album was waiting on, finish now.
        self._side.try_finish()


@defer.inlineCallbacks
def run_receive(code, dest, state_dir=None):
    # `dest` is NOT created here. It comes into existence by the rename that
    # publishes the finished album, so its mere presence means "complete".
    side = ReceiveSide(dest, state_dir)

    w = wormhole.create(APPID, RELAY, reactor, dilation=True)
    w.set_code(code)
    dw = w.dilate(transit_relay_location=TRANSIT_RELAY,
                  expected_subprotocols=[SUBPROTOCOL])
    yield dw.when_dilated()
    emit('connected')

    def build():
        # The first subchannel is control; every later one is a file. The peer
        # opens them in that order and nothing else opens any.
        if not getattr(build, 'control_taken', False):
            build.control_taken = True
            return ControlReceiver(side)
        return FileReceiver(side)

    dw.listener_for(SUBPROTOCOL).listen(Factory.forProtocol(build))
    yield side.finished
    yield w.close()


# ── entry point ───────────────────────────────────────────────────────────────

def main(_reactor):
    parser = argparse.ArgumentParser(description='airlock album transfer runner')
    parser.add_argument('--mode', choices=('send', 'receive'), required=True)
    parser.add_argument('--transfer-id', type=int, required=True)
    parser.add_argument('--manifest')
    parser.add_argument('--code')
    parser.add_argument('--dest')
    parser.add_argument('--state-dir',
                        help='where partial files and the completed-file record '
                             'live. Kept OUT of --dest, which a workflow ingests.')
    parser.add_argument('--expires-file',
                        help='send only: a file holding the unix time after which '
                             'an unused code expires. Re-read when it fires, so '
                             'the deadline can be moved while waiting.')
    args = parser.parse_args()

    if args.mode == 'send':
        if not args.manifest:
            fail('--manifest is required for --mode send')
            return defer.succeed(None)
        d = run_send(args.manifest, args.expires_file)
    else:
        if not (args.code and args.dest):
            fail('--code and --dest are required for --mode receive')
            return defer.succeed(None)
        d = run_receive(args.code, args.dest, args.state_dir)

    def on_error(failure):
        if EXPIRED['value']:
            return              # closing an expired code; not a failure
        fail(failure.getErrorMessage())
    d.addErrback(on_error)
    return d


if __name__ == '__main__':
    react(main, [])
