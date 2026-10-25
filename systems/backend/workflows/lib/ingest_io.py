"""
The one read an import makes of each source file.

Everything an import needs from a file's bytes - the SHA-256 for duplicates and
the copy into the catalog - comes out of a single sequential pass here. The
metadata that decides the date is read straight afterwards from the same path,
while those bytes are still in the page cache, so it costs no second trip to a
spinning disk (documentation/features/ingestion-architecture.md §5.2).
"""

import hashlib
import os
import shutil


def _advise_sequential(fd):
    # Doubles the kernel's read-ahead for this file on Linux. Harmless elsewhere.
    if hasattr(os, 'posix_fadvise'):
        try:
            os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_SEQUENTIAL)
        except OSError:
            pass


def hash_file(path, chunk):
    """SHA-256 of `path`, read sequentially in `chunk`-sized blocks. Raises on error."""
    sha = hashlib.sha256()
    with open(path, 'rb', buffering=0) as src:
        _advise_sequential(src.fileno())
        buf = bytearray(chunk)
        view = memoryview(buf)
        while True:
            n = src.readinto(buf)
            if not n:
                break
            sha.update(view[:n])
    return sha.hexdigest()


def hash_and_copy(source_path, staged_path, chunk, burst=None):
    """
    SHA-256 of source_path and a copy of it at staged_path, from one read.

    Written as `<staged>.part` and renamed when complete, then copystat, so the
    result is what shutil.copy2 would have made and a crash never leaves a
    truncated file under the final name.

    `burst`, when set (source and catalog on the same spinning disk), reads that
    many bytes before writing any of them, so the head alternates between two
    long runs instead of moving on every block.

    Returns the hex digest. Raises on error, having removed the partial file.
    """
    partial = staged_path + '.part'
    os.makedirs(os.path.dirname(staged_path), exist_ok=True)
    sha = hashlib.sha256()
    step = max(chunk, burst or 0)
    try:
        with open(source_path, 'rb', buffering=0) as src, \
                open(partial, 'wb', buffering=0) as dst:
            _advise_sequential(src.fileno())
            buf = bytearray(step)
            view = memoryview(buf)
            while True:
                n = 0
                # Fill the whole step (a burst may need several reads).
                while n < step:
                    got = src.readinto(view[n:])
                    if not got:
                        break
                    n += got
                if not n:
                    break
                sha.update(view[:n])
                written = 0
                while written < n:
                    written += dst.write(view[written:n])
                if n < step:
                    break
        shutil.copystat(source_path, partial)
        os.replace(partial, staged_path)
    except BaseException:
        try:
            os.remove(partial)
        except OSError:
            pass
        raise
    return sha.hexdigest()
