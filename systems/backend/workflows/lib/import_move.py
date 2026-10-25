"""
Move mode for the picture imports (documentation/specs/ingest-move-spec.md).

A mixin for _ImportRun, so the phased import and the streaming one (which
subclasses it) share every rule here. Move mode is copy mode for everything the
catalog stores; what it adds is what happens to the SOURCE afterwards:

    new photo or video   RENAMED into the catalog when the catalog's folders
                         can be reached through the source's own mount, else
                         copied in (as copy mode), verified, and deleted (§4)
    duplicate            moved to <source root>/DUPS/<relative path>     (§5)
    anything else        left where it is, and logged                    (§6)
    emptied folders      removed at the end of the run                   (§6)

The one rule everything else serves: a source file leaves only after the
transaction recording it in the catalog has committed. The delete (or rename)
therefore runs in the writer's after-commit hook, never in the step that
records the file. A copy is checked (size and SHA-256) before it can be
recorded; a rename needs no check - the bytes never move, and rename() is
atomic, so the file is always in exactly one of the two places.

Why both. Linux refuses rename() across mount points (EXDEV) even on one
filesystem, and a container sees the source and the catalog's originals
through different mounts. On oldserver both are the same USB disk, and the
catalog's Images folder is ALSO reachable under the source's mount
(/storage/usb1/shoebox-storage/family/Images): renaming through that alias
moves a file without writing a byte. _find_rename_aliases() works the alias
out from /proc/self/mountinfo and proves it with a real rename first.
"""

import errno
import os
import shutil
import threading

from lib.utilities import calculate_sha256

DUPS_DIRNAME = 'DUPS'

# file_operations.operation_type values this mode writes (§7.1).
OP_MOVE = 'move'
OP_DUPLICATE_MOVED = 'duplicate_moved'
OP_LEFT_IN_PLACE = 'left_in_place'

# file_operations.status values for OP_MOVE: recorded, then source deleted
# (a copy) or renamed into the catalog (a rename).
PENDING_DELETE = 'pending_delete'
PENDING_MOVE = 'pending_move'
MOVED = 'moved'
PENDING = (PENDING_DELETE, PENDING_MOVE)
# Neither the source nor a matching catalog copy exists: a real loss, or a
# catalog folder that is not where it should be. Never acted on, only shown -
# and re-checked by every later run, so it clears itself if the copy reappears
# (a remounted disk, say).
MISSING = 'missing'


def _unescape(field):
    """mountinfo escapes space, tab, newline and backslash as \\ooo."""
    out, i = [], 0
    while i < len(field):
        if field[i] == '\\' and field[i + 1:i + 4].isdigit():
            out.append(chr(int(field[i + 1:i + 4], 8)))
            i += 4
        else:
            out.append(field[i])
            i += 1
    return ''.join(out)


def _mountinfo():
    """[(mount id, 'major:minor', root within the fs, mount point)], or []."""
    try:
        with open('/proc/self/mountinfo', encoding='utf-8', errors='replace') as fh:
            lines = fh.read().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        f = line.split(' - ')[0].split()
        if len(f) >= 5:
            rows.append((f[0], f[2], _unescape(f[3]), _unescape(f[4])))
    return rows


def _mount_of(path, rows):
    """The mount holding `path`: longest mount point, the later of two on one point."""
    real = os.path.realpath(path)
    best = None
    for row in rows:
        mp = row[3]
        if real == mp or real.startswith(mp.rstrip('/') + '/') or mp == '/':
            if best is None or len(mp) >= len(best[3]):
                best = row
    return best


def rename_alias(source_dir, folder, rows=None):
    """
    `folder` as a path inside the mount that holds `source_dir`, or None.

    A rename from the source to that path stays within one mount, so the kernel
    allows it. None when they are different filesystems, or the folder is not
    reachable under the source's mount at all.
    """
    rows = _mountinfo() if rows is None else rows
    try:
        if os.stat(source_dir).st_dev != os.stat(folder).st_dev:
            return None
    except OSError:
        return None
    if not rows:
        return folder               # no mount table (macOS): one device, one mount
    alias = alias_path(os.path.realpath(source_dir), os.path.realpath(folder), rows)
    if alias is None or alias == os.path.realpath(folder):
        return alias and folder
    try:
        a, f = os.stat(alias), os.stat(folder)
    except OSError:
        return None
    return alias if (a.st_dev, a.st_ino) == (f.st_dev, f.st_ino) else None


def alias_path(source_real, folder_real, rows):
    """
    The path arithmetic of rename_alias, on resolved paths and a mount table:
    where `folder_real` appears inside the mount holding `source_real`.
    """
    ms, mf = _mount_of(source_real, rows), _mount_of(folder_real, rows)
    if ms is None or mf is None or ms[1] != mf[1]:
        return None                 # different filesystems: only a copy can do it
    if ms[0] == mf[0]:
        return folder_real          # one mount: rename directly
    inside = os.path.relpath(folder_real, mf[3])
    fs_path = os.path.normpath(os.path.join(mf[2], inside))
    rel = os.path.relpath(fs_path, ms[2])
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        return None                 # the folder is outside what that mount shows
    alias = os.path.normpath(os.path.join(ms[3], rel))
    am = _mount_of(alias, rows)
    if am is None or am[0] != ms[0]:
        return None                 # something else is mounted over that path
    return alias


def _under(path, root):
    """True when the realpath of path is root or inside it."""
    real, real_root = os.path.realpath(path), os.path.realpath(root)
    return real == real_root or real.startswith(real_root + os.sep)


def _gb(n):
    return f'{n / 1e9:,.1f} GB'


class MoveMode:
    """Everything move mode adds to an import run. Inert unless move_mode is set."""

    def _init_move_mode(self):
        self.move_mode = getattr(self.catalog, 'ingest_mode', 'copy') == 'move'
        self.dups_root = os.path.join(self.source_dir, DUPS_DIRNAME)
        self._dups_lock = threading.Lock()
        # Written and read on the writer thread only (record, then the hooks).
        self._to_delete = []        # sources whose catalog copy is being committed
        self._delete_results = []   # (op_id, status, note) waiting to be written
        self._writing_results = 0
        # Folders this run took files out of; candidates for removal at the end.
        self._touched_dirs = set()
        self._touched_lock = threading.Lock()
        self.folders_removed = 0
        self._catalog_dir_marks = set()
        # {catalog originals folder: the same folder under the source's mount}.
        # Empty means files are copied (_find_rename_aliases).
        self._rename_aliases = {}

    def _install_move_hooks(self):
        """Called once the writer exists."""
        if not self.move_mode:
            return
        self.writer.before_commit = self._write_delete_results
        self.writer.after_commit = self._delete_committed_sources
        self.writer.has_pending = lambda: bool(self._delete_results)

    # ── before anything moves (§8) ───────────────────────────────────────────

    def _move_preflight_source(self):
        """
        Refuse a run that may not, or must not, take files out of its source.
        Raises with the reason; nothing has been read or moved yet.
        """
        if not self.move_mode:
            return
        refusal = self._move_route_refusal()
        if refusal:
            raise Exception(refusal)
        if os.path.realpath(self.source_dir) == os.path.realpath(self.dups_root):
            raise Exception('The DUPS folder holds duplicates set aside by earlier '
                            'imports; import the folder above it instead.')
        # §8.3: never a source that is, or sits inside, this catalog. Compared by
        # device and inode, not by path: on oldserver the catalog's Images folder is
        # a bind mount of a folder that is ALSO reachable under the storage root,
        # and no path comparison would see that they are the same directory.
        for d in [self.target_dir] + [os.path.join(self.target_dir, x)
                                      for x in self.ORIGINALS_DIRS]:
            try:
                st = os.stat(d)
                self._catalog_dir_marks.add((st.st_dev, st.st_ino))
            except OSError:
                pass
        p = os.path.realpath(self.source_dir)
        while True:
            try:
                st = os.stat(p)
                if (st.st_dev, st.st_ino) in self._catalog_dir_marks:
                    raise Exception(f'{self.source_dir} is inside this catalog. Moving a '
                                    f"catalog's own files into itself would delete them.")
            except OSError:
                pass
            parent = os.path.dirname(p)
            if parent == p:
                break
            p = parent
        print(f'Moving files: originals leave {self.source_dir}; duplicates go to '
              f'{self.dups_root}; anything that is not a photo or video stays.')
        self._find_rename_aliases()

    def _find_rename_aliases(self):
        """
        Use rename instead of copy when every originals folder is reachable
        under the source's mount - proved by renaming a probe file there and
        back out. Then nothing is staged: probe only fingerprints the file.
        IMPORT_MOVE_RENAME=0 keeps copying.
        """
        if os.environ.get('IMPORT_MOVE_RENAME', '1') == '0' or self.reference_mode \
                or getattr(self.catalog, 'file_strategy', 'local') != 'local':
            return
        rows = _mountinfo()
        aliases = {}
        for d in self.ORIGINALS_DIRS:
            folder = os.path.join(self.target_dir, d)
            os.makedirs(folder, exist_ok=True)
            alias = rename_alias(self.source_dir, folder, rows)
            if not alias:
                print(f'Copying files: {folder} cannot be reached by renaming from '
                      f'{self.source_dir} (a different disk or mount).')
                return
            aliases[os.path.realpath(folder)] = alias
        probe = os.path.join(self.source_dir, f'.airlock-rename-check-{os.getpid()}')
        landed = None
        try:
            with open(probe, 'w'):
                pass
            landed = os.path.join(next(iter(aliases.values())), os.path.basename(probe))
            os.rename(probe, landed)
        except OSError as exc:
            print(f'Copying files: a rename into the catalog was refused ({exc}).')
            return
        finally:
            for leftover in (probe, landed):
                if leftover:
                    try:
                        os.remove(leftover)
                    except OSError:
                        pass
        self._rename_aliases = aliases
        self.staging = False        # nothing to copy: probe only fingerprints
        print('Renaming files into the catalog: source and catalog are one disk, so '
              'nothing is copied. Through '
              + ', '.join(sorted(set(aliases.values()))))

    def _rename_target(self, target_path):
        """target_path (under the catalog) as the path to rename to, or None."""
        real = os.path.realpath(os.path.dirname(target_path))
        for folder, alias in self._rename_aliases.items():
            if real == folder or real.startswith(folder + os.sep):
                return os.path.join(alias, os.path.relpath(real, folder),
                                    os.path.basename(target_path))
        return None

    def _move_route_refusal(self):
        """
        Why this source may not be moved from, or None (§8.1, §11.2).

        Under the storage root, it must be inside a route of this catalog that an
        owner marked "imports may move files". Elsewhere (the /input inbox, which
        is mounted read-write for ingestion) it only has to be writable.
        """
        try:
            import storage_routes as routes
        except ImportError:
            routes = None
        real = os.path.realpath(self.source_dir)
        root = routes.STORAGE_ROOT_CONTAINER if routes else None
        if routes and root and os.path.isdir(root) and _under(real, root):
            covering = [r for r in routes.list_routes(catalog_id=getattr(self.catalog, 'id', None))
                        if r.get('container_path') and _under(real, r['container_path'])]
            if not covering:
                return (f'{self.source_dir} is not inside a storage route of this catalog, '
                        f'so nothing has said files may be moved out of it.')
            refusals = [routes.move_refusal(r) for r in covering]
            return None if any(x is None for x in refusals) else refusals[0]
        if not os.access(self.source_dir, os.W_OK):
            return (f'This catalog moves files out of the folders it imports from, but '
                    f'{self.source_dir} is not writable here.')
        return None

    def _check_walk_dir(self, path, entry):
        """_walk's guard in move mode: never descend into this catalog (§8.3)."""
        try:
            st = entry.stat(follow_symlinks=False)
        except OSError:
            return
        if (st.st_dev, st.st_ino) in self._catalog_dir_marks:
            raise Exception(f'{path} is a folder of this catalog, inside the source being '
                            f'imported. Moving it into itself would delete it; choose a '
                            f'source that does not contain the catalog.')

    def _move_preflight_space(self):
        """
        §8.4: stop before the first move rather than part-way through.

        A move is copy-then-delete, so on a different filesystem from the source
        the catalog needs room for everything; on the same one, only for the
        files in flight.
        """
        if not self.move_mode:
            return
        rows = self.read.execute(
            "SELECT source_path, size FROM import_items WHERE execution_id=? "
            "AND state='discovered'", (self.eid_key,)).fetchall()
        if not rows:
            return
        try:
            src_dev = os.stat(self.source_dir).st_dev
        except OSError:
            return
        need = {}
        for r in rows:
            folder = os.path.dirname(self._staging_dir_for(r['source_path']))
            sizes = need.setdefault(folder, [0, 0])
            sizes[0] += r['size'] or 0
            sizes[1] = max(sizes[1], r['size'] or 0)
        in_flight = 2 * max(self.settings.get('place_workers') or 1,
                            self.settings.get('probe_workers') or 1)
        for folder, (total, largest) in need.items():
            probe = folder
            while not os.path.isdir(probe) and os.path.dirname(probe) != probe:
                probe = os.path.dirname(probe)
            try:
                same = os.stat(probe).st_dev == src_dev
                free = shutil.disk_usage(probe).free
            except OSError:
                continue
            wanted = min(total, largest * in_flight) if same else total
            if wanted > free:
                raise Exception(f'Not enough space to move these files into {folder}: '
                                f'needs {_gb(wanted)}, has {_gb(free)}. Nothing was moved.')

    # ── left in place (§6) ───────────────────────────────────────────────────

    def _left_in_place_op(self, path, reason, size):
        return (self.eid, None, OP_LEFT_IN_PLACE, path, None, size, 'skipped', reason)

    # ── a new file: verify, record, delete (§4) ──────────────────────────────

    def _verify_placed(self, item, target_path):
        """
        Step 3. Raises when the catalog copy is not byte-for-byte the file probe
        fingerprinted; the copy is removed and the source is left alone.
        """
        sha = item.get('sha256')
        try:
            if not sha:
                raise Exception('the file could not be fingerprinted, so its copy cannot '
                                'be checked; it was left in the source')
            src_size = os.path.getsize(item['source_path'])
            if os.path.getsize(target_path) != src_size:
                raise Exception('the catalog copy is not the size of the source; the '
                                'source was left in place')
            if calculate_sha256(target_path) != sha:
                raise Exception('the catalog copy does not match the source (SHA-256); '
                                'the source was left in place')
        except Exception:
            try:
                os.remove(target_path)
            except OSError:
                pass
            raise
        st = os.stat(item['source_path'])
        return (st.st_size, st.st_mtime_ns)

    def _queue_source_delete(self, op_id, r, file_id):
        """Step 5, scheduled: runs once the transaction holding step 4 commits.
        Must be the LAST thing _record_placed does, so a rollback of that
        record cannot leave a delete queued behind it."""
        self._to_delete.append((op_id, r['item']['source_path'], r.get('source_stat'),
                                r.get('rename_to'), file_id, r['item']['id']))

    def _delete_committed_sources(self):
        """Writer thread, after COMMIT: the catalog now durably has these files."""
        if not self._to_delete:
            return
        todo, self._to_delete = self._to_delete, []
        for op_id, path, stat_then, rename_to, file_id, item_id in todo:
            status, note, undo = MOVED, None, None
            try:
                if not _under(path, self.source_dir) or _under(path, self.dups_root):
                    raise Exception('outside the folder being imported')
                st = os.stat(path)
                if stat_then and (st.st_size, st.st_mtime_ns) != tuple(stat_then):
                    raise Exception('it changed after it was read')
                if rename_to:
                    # rename() overwrites silently; the catalog path must be free.
                    if os.path.lexists(rename_to):
                        raise Exception(f'a file is already at {rename_to}')
                    os.rename(path, rename_to)
                else:
                    os.remove(path)
                self._touch(path)
            except FileNotFoundError:
                if rename_to:
                    # Nothing reached the catalog: its record must go too.
                    status, note, undo = 'failed', 'the source disappeared before it ' \
                        'could be moved', (file_id, item_id)
                else:
                    note = 'the source was already gone'
            except Exception as exc:
                if rename_to:
                    status, note, undo = 'failed', f'not moved: {exc}', (file_id, item_id)
                else:
                    status, note = PENDING_DELETE, f'source not deleted: {exc}'
                print(f'Kept {path}: {exc}')
            self._delete_results.append((op_id, status, note, undo))

    def _write_delete_results(self, conn):
        """Writer thread, inside the next transaction."""
        if not self._delete_results:
            return
        done, self._delete_results = self._delete_results, []
        moved = 0
        for op_id, status, note, undo in done:
            conn.execute(
                "UPDATE file_operations SET status=?, details=CASE WHEN ? IS NULL THEN details "
                "ELSE COALESCE(details || '; ', '') || ? END WHERE id=?",
                (status, note, note, op_id))
            if undo:
                # A rename that did not happen: the catalog never got the file,
                # so the rows saying it did are removed. The source is untouched.
                file_id, item_id = undo
                conn.execute('DELETE FROM exif_data WHERE file_id=?', (file_id,))
                conn.execute('DELETE FROM file_contents WHERE id=?', (file_id,))
                conn.execute("UPDATE import_items SET state='place_failed', phase_error=?, "
                             'file_id=NULL WHERE id=?', (note, item_id))
            moved += status == MOVED
        if moved:
            self.progress.bump('place', moved=moved)

    def _settle_moves(self):
        """Commit everything, delete what it covered, and record the deletes."""
        if not self.move_mode or not self.writer:
            return
        for _ in range(3):
            self.writer.flush()
            if not self._to_delete and not self._delete_results:
                break

    # ── resume (§4.1) ────────────────────────────────────────────────────────

    def _finish_interrupted_moves(self):
        """
        Files a stopped run recorded in the catalog but did not get to delete.
        Their content IS in the catalog - this import put it there - so they are
        unfinished moves, not duplicates, and must not go to DUPS/.
        """
        if not self.move_mode:
            return 0
        rows = self.read.execute(
            '''SELECT i.id, i.source_path, fo.id AS op_id, fc.file_path, fc.sha256_hash
                 FROM import_items i
                 JOIN file_operations fo ON fo.source_path = i.source_path
                                        AND fo.file_size = i.size
                                        AND fo.operation_type = ? AND fo.status IN (?, ?)
                 JOIN file_contents fc ON fc.id = fo.file_id
                WHERE i.execution_id = ? AND i.state = 'discovered' ''',
            (OP_MOVE, *PENDING, self.eid_key)).fetchall()
        finished = 0
        for r in rows:
            path, sha = r['source_path'], r['sha256_hash']
            try:
                if calculate_sha256(path) != sha:
                    continue        # a different file at that path now: import it
                copy = os.path.join(self.target_dir, r['file_path'])
                if not _under(path, self.source_dir):
                    raise Exception('outside the folder being imported')
                if not os.path.lexists(copy) and self._rename_target(copy):
                    # Recorded, then stopped before the rename: do it now.
                    dest = self._rename_target(copy)
                    os.makedirs(os.path.dirname(dest), exist_ok=True)
                    os.rename(path, dest)
                else:
                    if calculate_sha256(copy) != sha:
                        raise Exception('its catalog copy no longer matches; the source '
                                        'was kept')
                    os.remove(path)
                self._touch(path)
            except Exception as exc:
                print(f'Unfinished move of {path} not completed: {exc}')

                def _fail(conn, r=r, exc=exc):
                    conn.execute("UPDATE import_items SET state='place_failed', phase_error=? "
                                 'WHERE id=?', (str(exc), r['id']))
                self.writer.submit(_fail)
                continue
            finished += 1

            def _done(conn, r=r):
                conn.execute("UPDATE file_operations SET status=? WHERE id=?", (MOVED, r['op_id']))
                conn.execute("UPDATE import_items SET state='already_imported' WHERE id=?",
                             (r['id'],))
                self.progress.bump('scan', done=-1, skipped=1, already_imported=1)
            self.writer.submit(_done)
        self.writer.flush()
        if finished:
            print(f'Finished {finished} move(s) an earlier run recorded but did not complete.')
        finished += self._settle_already_deleted()
        return finished

    def _settle_already_deleted(self):
        """
        Moves whose source IS gone but whose record still says pending_delete:
        the run was killed between deleting the source and writing that down
        (20 of them on oldserver when its backend was stopped, 2026-10-02). The
        catalog copy is checked against its fingerprint first; only then is the
        record corrected. Nothing on disk changes.

        When the copy is missing or does not match either, the record becomes
        MISSING - "missing from both places" in the report and the run summary -
        rather than staying an ordinary-looking pending one.
        """
        rows = self.read.execute(
            '''SELECT fo.id, fo.status, fo.source_path, fc.file_path, fc.sha256_hash
                 FROM file_operations fo JOIN file_contents fc ON fc.id = fo.file_id
                WHERE fo.operation_type = ? AND fo.status IN (?, ?, ?)''',
            (OP_MOVE, *PENDING, MISSING)).fetchall()
        settled, missing = [], []
        for r in rows:
            if os.path.lexists(r['source_path']) or not _under(r['source_path'], self.source_dir):
                continue
            copy = os.path.join(self.target_dir, r['file_path'])
            try:
                ok = calculate_sha256(copy) == r['sha256_hash']
            except OSError as exc:
                ok, why = False, f'the catalog copy is missing ({exc.strerror or exc})'
            else:
                why = 'the catalog copy does not match the original\'s fingerprint'
            if ok:
                settled.append(r['id'])
            elif r['status'] != MISSING:
                missing.append((f'MISSING FROM BOTH PLACES: the source is gone and {why}',
                                r['id']))
                print(f'{r["source_path"]}: missing from both places - the source is gone '
                      f'and {why}. Check {copy}.')
        if settled:
            self.writer.call(lambda conn: conn.executemany(
                "UPDATE file_operations SET status=?, details=COALESCE(details || '; ', '') || "
                "'source was already deleted; record corrected on resume' WHERE id=?",
                [(MOVED, i) for i in settled]))
            print(f'Corrected {len(settled)} move record(s) whose source was already deleted.')
        if missing:
            self.writer.call(lambda conn: conn.executemany(
                "UPDATE file_operations SET status='missing', "
                "details=COALESCE(details || '; ', '') || ? WHERE id=?", missing))
        return len(settled)

    # ── duplicates (§5) ──────────────────────────────────────────────────────

    def _move_to_dups(self, path):
        """
        Rename `path` to DUPS/<its path relative to the source root>. Returns
        (new path, None) or (None, why not). Never overwrites: a name already
        taken there gets " (2)", " (3)", ... before its extension.
        """
        if not _under(path, self.source_dir) or _under(path, self.dups_root):
            return None, 'outside the folder being imported; left in place'
        rel = os.path.relpath(path, self.source_dir)
        dest = os.path.join(self.dups_root, rel)
        base, ext = os.path.splitext(dest)
        with self._dups_lock:
            n = 2
            while os.path.lexists(dest):
                dest = f'{base} ({n}){ext}'
                n += 1
            try:
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                try:
                    os.rename(path, dest)
                except OSError as exc:
                    if exc.errno != errno.EXDEV:     # a mount inside the source
                        raise
                    shutil.move(path, dest)
            except OSError as exc:
                return None, f'could not move it to {DUPS_DIRNAME}/: {exc}'
        self._touch(path)
        return dest, None

    def _duplicate_moved_op(self, file_id, path, sha, existing_name, removed, size=None):
        """(the DUPS move, the file_operations row) for one duplicate."""
        dest, err = self._move_to_dups(path)
        reason = (f'removed from the catalog: {existing_name}' if removed
                  else f'duplicate of {existing_name}')
        if sha:
            reason += f'; SHA256: {sha}'
        if dest is None:
            print(f'Duplicate {os.path.basename(path)} not moved: {err}')
            return (self.eid, file_id, OP_DUPLICATE_MOVED, path, None, size, 'failed',
                    f'{reason}; {err}')
        renamed = os.path.basename(dest) != os.path.basename(path)
        print(f'Duplicate {os.path.basename(path)} -> {os.path.relpath(dest, self.source_dir)}'
              f' ({reason.split(";")[0]})')
        return (self.eid, file_id, OP_DUPLICATE_MOVED, path, dest, size, MOVED,
                reason + ('; renamed, a file of that name was already there' if renamed else ''))

    # ── empty folders (§6, §11.1) ────────────────────────────────────────────

    def _touch(self, path):
        with self._touched_lock:
            self._touched_dirs.add(os.path.dirname(path))

    def _remove_emptied_folders(self):
        """
        Remove the folders this run emptied, deepest first, climbing while each
        parent is empty in turn. Never the source root, never DUPS/, and never a
        folder that still holds anything: os.rmdir refuses those by itself.
        """
        if not self.move_mode or not self._touched_dirs:
            return 0
        root = os.path.realpath(self.source_dir)
        removed = 0
        for d in sorted(self._touched_dirs, key=lambda p: p.count(os.sep), reverse=True):
            while True:
                real = os.path.realpath(d)
                if real == root or not real.startswith(root + os.sep) \
                        or _under(d, self.dups_root):
                    break
                try:
                    os.rmdir(d)
                except OSError:
                    break
                removed += 1
                d = os.path.dirname(d)
        self._touched_dirs.clear()
        self.folders_removed += removed
        if removed:
            print(f'Removed {removed} folder(s) this import emptied.')
            self.writer.call(lambda conn: self.progress.bump('place', folders_removed=removed))
        return removed

    # ── the run summary (§7.2) ───────────────────────────────────────────────

    def _move_summary(self):
        if not self.move_mode or self.eid is None:
            return {}
        rows = self.read.execute(
            'SELECT operation_type, status, COUNT(*), COALESCE(SUM(file_size), 0) '
            'FROM file_operations WHERE execution_id=? GROUP BY operation_type, status',
            (self.eid,)).fetchall()
        c = {(r[0], r[1]): (r[2], r[3]) for r in rows}
        moved, moved_bytes = c.get((OP_MOVE, MOVED), (0, 0))
        pending = sum(c.get((OP_MOVE, st), (0, 0))[0] for st in PENDING)
        dups, dup_bytes = c.get((OP_DUPLICATE_MOVED, MOVED), (0, 0))
        left, _ = c.get((OP_LEFT_IN_PLACE, 'skipped'), (0, 0))
        failed = sum(v[0] for (t, s), v in c.items() if s == 'failed')
        print(f'\nMoved into the catalog     {moved:>7,} files   {_gb(moved_bytes)}'
              + (f'   ({pending} recorded, source not yet deleted)' if pending else ''))
        print(f'Duplicates -> {DUPS_DIRNAME}/          {dups:>7,} files   {_gb(dup_bytes)}')
        print(f'Left in place              {left:>7,} files')
        print(f'Failed                     {failed:>7,}')
        print(f'Folders emptied and removed {self.folders_removed:>6,}')
        # Catalog-wide, not this run's: a record found missing belongs to the
        # run that made it, and it matters until someone deals with it.
        lost = self.read.execute(
            'SELECT COUNT(*) FROM file_operations WHERE operation_type=? AND status=?',
            (OP_MOVE, MISSING)).fetchone()[0]
        if lost:
            print(f'MISSING FROM BOTH PLACES    {lost:>7,}   (see "Left in the source" '
                  f'on the Catalog report)')
        return {'moved_files': moved, 'moved_bytes': moved_bytes, 'moves_pending': pending,
                'duplicates_moved': dups, 'left_in_place': left,
                'folders_removed': self.folders_removed, 'moves_missing': lost}
