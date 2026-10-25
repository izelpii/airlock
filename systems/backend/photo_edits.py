"""
Basic photo editing: a photo's turn or flip, kept in the catalog database and
never written to the original (documentation/specs/basic-photo-editing.md).

What lives here is everything that needs no Flask: the schema, saving an edit
(with the face and pet boxes moved in the same transaction), and rebuilding a
photo's derivatives and crops in place. app.py owns the routes and the two
rebuild steps that use its own helpers (named-face crops, profile photos).
"""

import hashlib
import os
import shutil
import uuid
from datetime import datetime, timezone

from workflows.lib import orientation as orient

# The derivatives a rebuild replaces: every display rung and both thumbnails.
IMAGE_RUNG_NAMES = ('1200', '1600', '2048', '2560', 'full', 'thumb72', 'thumb320')

# The detection image is the 1200 rung, its long edge capped at this
# (ai_auto_detect_faces_workflow.DETECTION_MAX_DIM). Boxes are rescaled into
# that space after a rebuild so the detector's overlap check compares like with
# like when it looks at the photo again.
DETECTION_MAX_DIM = int(os.environ.get('DETECTION_MAX_DIM', '1600'))

# Same padding as the two crop writers (extract_and_save_ai_face, crop_one).
FACE_CROP_PAD = 0.20
PET_CROP_PAD = float(os.environ.get('PET_CROP_PAD', '0.12'))


class EditError(Exception):
    def __init__(self, code, message, status=400):
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status


def ensure_schema(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS photo_edits (
        file_id        INTEGER PRIMARY KEY REFERENCES file_contents(id) ON DELETE CASCADE,
        orientation    INTEGER NOT NULL DEFAULT 1 CHECK (orientation BETWEEN 1 AND 8),
        version        INTEGER NOT NULL DEFAULT 1,
        render_status  TEXT    NOT NULL DEFAULT 'pending',
        render_error   TEXT,
        updated_by     INTEGER,
        updated_at     TEXT    NOT NULL)''')
    conn.execute('''CREATE TABLE IF NOT EXISTS photo_edit_history (
        id                 INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id            INTEGER NOT NULL,
        before_orientation INTEGER NOT NULL,
        after_orientation  INTEGER NOT NULL,
        user_id            INTEGER,
        created_at         TEXT NOT NULL)''')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_photo_edit_history_file '
                 'ON photo_edit_history(file_id)')


def _now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get(conn, file_id):
    """
    The edit as the API returns it; orientation 1 when there is none.
    updated_by is a users.id - the users table is in catalogs.db, so the
    caller adds the name.
    """
    row = conn.execute('SELECT * FROM photo_edits WHERE file_id = ?', (file_id,)).fetchone()
    if row:
        row = dict(row)
        return {'orientation': row['orientation'], 'version': row['version'],
                'render_status': row['render_status'], 'render_error': row['render_error'],
                'updated_by': row['updated_by'], 'updated_at': row['updated_at'],
                'summary': orient.summary(row['orientation'])}
    # A finished reset deletes the row, but the version must keep climbing, or
    # the next edit would reuse a thumbnail URL a browser holds for a year.
    last = conn.execute('SELECT COUNT(*) FROM photo_edit_history WHERE file_id = ?',
                        (file_id,)).fetchone()[0]
    return {'orientation': 1, 'version': last, 'render_status': 'ready',
            'render_error': None, 'updated_by': None, 'updated_at': None,
            'summary': orient.summary(1)}


def versions(conn, file_ids=None):
    """{file_id: version} for every file that has ever been edited."""
    sql = ('SELECT file_id, COUNT(*) AS v FROM photo_edit_history '
           + ('WHERE file_id IN (%s) ' % ','.join('?' * len(file_ids)) if file_ids else '')
           + 'GROUP BY file_id')
    try:
        return {r[0]: r[1] for r in conn.execute(sql, list(file_ids or []))}
    except Exception:
        return {}


# ─── Saving ──────────────────────────────────────────────────────────────────

def save(conn, file_id, value, base_version, user_id):
    """
    Record the edit and move the file's boxes, in one transaction. Returns
    (changed, edit). The caller starts the rebuild when `changed`.

    The version is the number of saves ever made (history rows), so it only
    ever rises - through resets too. It is what thumbnail URLs carry.
    """
    if not orient.valid(value):
        raise EditError('bad_orientation', 'orientation must be a whole number from 1 to 8')
    current = get(conn, file_id)
    if current['render_status'] in ('rendering', 'pending') and \
            conn.execute('SELECT 1 FROM photo_edits WHERE file_id = ?', (file_id,)).fetchone():
        raise EditError('rendering', 'This photo is still being rebuilt from the last edit.', 409)
    if base_version is not None and int(base_version) != current['version']:
        raise EditError('stale', 'Someone saved an edit to this photo first. '
                                 'Reload it to see theirs.', 409)
    old = current['orientation']
    if value == old:
        if current['render_status'] != 'failed':
            return False, current
        # The same edit again after a failed rebuild: try the rebuild again.
        conn.execute("UPDATE photo_edits SET render_status = 'rendering', render_error = NULL "
                     "WHERE file_id = ?", (file_id,))
        conn.commit()
        return True, get(conn, file_id)

    version = current['version'] + 1
    now = _now()
    conn.execute('INSERT INTO photo_edit_history (file_id, before_orientation, after_orientation, '
                 'user_id, created_at) VALUES (?, ?, ?, ?, ?)',
                 (file_id, old, value, user_id, now))
    # The row exists until the rebuild finishes, even for a reset: it carries
    # the 'rendering' status. finish() deletes it when the edit is 1.
    conn.execute('INSERT INTO photo_edits (file_id, orientation, version, render_status, '
                 'render_error, updated_by, updated_at) VALUES (?, ?, ?, ?, NULL, ?, ?) '
                 'ON CONFLICT(file_id) DO UPDATE SET orientation = excluded.orientation, '
                 'version = excluded.version, render_status = excluded.render_status, '
                 'render_error = NULL, updated_by = excluded.updated_by, '
                 'updated_at = excluded.updated_at',
                 (file_id, value, version, 'rendering', user_id, now))
    transform_boxes(conn, file_id, orient.delta(old, value))
    conn.commit()
    return True, get(conn, file_id)


def _base_size(conn, file_id):
    row = conn.execute("SELECT width, height FROM alternative_versions "
                       "WHERE original_file_id = ? AND rung = '1200' "
                       "ORDER BY id LIMIT 1", (file_id,)).fetchone()
    return (row[0], row[1]) if row and row[0] and row[1] else (None, None)


def transform_boxes(conn, file_id, d):
    """Every face and pet box on the file, through d (spec section 6.1)."""
    if d == 1:
        return 0
    bw, bh = _base_size(conn, file_id)
    moved = 0
    for table in ('face_detections', 'pet_detections'):
        try:
            rows = conn.execute(f'SELECT id, x, y, w, h, image_width, image_height '
                                f'FROM {table} WHERE file_id = ?', (file_id,)).fetchall()
        except Exception:            # pet_detections before the pet schema exists
            continue
        for r in rows:
            W, H = r[5] or bw, r[6] or bh
            if not W or not H:
                continue             # no idea what space it is in; leave it
            x, y, w, h, W2, H2 = orient.transform_box(r[1], r[2], r[3], r[4], W, H, d)
            conn.execute(f'UPDATE {table} SET x=?, y=?, w=?, h=?, image_width=?, '
                         f'image_height=? WHERE id=?', (x, y, w, h, W2, H2, r[0]))
            moved += 1
    return moved


# ─── Rebuilding ──────────────────────────────────────────────────────────────

def _creation_from_ts(ts):
    try:
        return datetime.strptime(ts, '%Y%m%d%H%M%S') if ts else None
    except ValueError:
        return None


def rerender_derivatives(conn, target_dir, file_id, original_path, creation_ts, stem,
                         rungs, value):
    """
    Render every rung and both thumbnails with `value` applied, into a private
    folder, then move each over the file it replaces (spec section 5.3).

    Paths stay what they were: published albums symlink them, and the rows do
    not move. Rungs the photo gains (a turn changes the width a rung caps) are
    added; rungs it no longer warrants are removed. On any failure the old
    files are untouched.
    """
    from workflows.lib.render_once import render_image_once

    tmp = os.path.join(target_dir, 'cache', 'tmp', f'edit-{file_id}-{uuid.uuid4().hex[:8]}')
    os.makedirs(tmp, exist_ok=True)
    try:
        task = {'id': file_id, 'file_id': file_id, 'target_path': original_path,
                'target_dir': tmp, 'creation_ts': creation_ts, 'derivative_stem': stem,
                'existing': {}, 'rungs': rungs, 'user_orientation': value}
        result = render_image_once(task, _creation_from_ts)
        if not result['ok']:
            raise EditError('render_failed', result['error'] or 'the photo could not be rendered', 500)

        marks = ','.join('?' * len(IMAGE_RUNG_NAMES))
        existing = {r['rung']: dict(r) for r in conn.execute(
            f'SELECT id, rung, alternative_file_path FROM alternative_versions '
            f'WHERE original_file_id = ? AND rung IN ({marks}) ORDER BY id',
            [file_id, *IMAGE_RUNG_NAMES])}

        moves, produced = [], set()
        for (_fid, rel, name, size, characteristic, w, h, rung) in result['rows']:
            produced.add(rung)
            src = os.path.join(tmp, rel)
            if rung in existing:
                dest_rel = existing[rung]['alternative_file_path']
            else:
                dest_rel = rel
            moves.append((src, dest_rel, name, size, characteristic, w, h, rung))

        for src, dest_rel, *_ in moves:
            dest = dest_rel if os.path.isabs(dest_rel) else os.path.join(target_dir, dest_rel)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            os.replace(src, dest)

        for src, dest_rel, name, size, characteristic, w, h, rung in moves:
            if rung in existing:
                conn.execute('UPDATE alternative_versions SET alternative_file_size = ?, '
                             'characteristic = ?, width = ?, height = ? WHERE id = ?',
                             (size, characteristic, w, h, existing[rung]['id']))
            else:
                conn.execute('INSERT INTO alternative_versions (original_file_id, '
                             'alternative_file_path, alternative_file_name, '
                             'alternative_file_size, characteristic, width, height, rung) '
                             'VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                             (file_id, dest_rel, name, size, characteristic, w, h, rung))
        for rung, row in existing.items():
            if rung in produced:
                continue
            path = row['alternative_file_path']
            path = path if os.path.isabs(path) else os.path.join(target_dir, path)
            try:
                os.remove(path)
            except OSError:
                pass
            conn.execute('DELETE FROM alternative_versions WHERE id = ?', (row['id'],))
        conn.commit()
        return result['logs']
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _detection_size(w, h):
    if DETECTION_MAX_DIM > 0 and max(w, h) > DETECTION_MAX_DIM:
        s = DETECTION_MAX_DIM / float(max(w, h))
        return max(1, int(round(w * s))), max(1, int(round(h * s)))
    return w, h


def rescale_boxes(conn, file_id):
    """
    Boxes into the new detection image's space. A quarter turn changes the
    scale of the 1200 rung (it caps width, not the long edge), so the boxes
    moved by transform_boxes are right in shape but not in scale.
    """
    bw, bh = _base_size(conn, file_id)
    if not bw:
        return
    dw, dh = _detection_size(bw, bh)
    for table in ('face_detections', 'pet_detections'):
        try:
            rows = conn.execute(f'SELECT id, x, y, w, h, image_width, image_height '
                                f'FROM {table} WHERE file_id = ?', (file_id,)).fetchall()
        except Exception:
            continue
        for r in rows:
            W, H = r[5], r[6]
            if not W or not H or (W, H) == (dw, dh):
                continue
            sx, sy = dw / float(W), dh / float(H)
            conn.execute(f'UPDATE {table} SET x=?, y=?, w=?, h=?, image_width=?, '
                         f'image_height=? WHERE id=?',
                         (int(round(r[1] * sx)), int(round(r[2] * sy)),
                          max(1, int(round(r[3] * sx))), max(1, int(round(r[4] * sy))),
                          dw, dh, r[0]))
    conn.commit()


def _base_image(conn, target_dir, file_id):
    from PIL import Image
    row = conn.execute("SELECT alternative_file_path FROM alternative_versions "
                       "WHERE original_file_id = ? AND rung = '1200' ORDER BY id LIMIT 1",
                       (file_id,)).fetchone()
    if not row:
        return None
    path = row[0] if os.path.isabs(row[0]) else os.path.join(target_dir, row[0])
    if not os.path.exists(path):
        return None
    img = Image.open(path)
    img.load()
    return img.convert('RGB') if img.mode != 'RGB' else img


def _scaled(img, r):
    sx = img.width / float(r['image_width'] or img.width)
    sy = img.height / float(r['image_height'] or img.height)
    return r['x'] * sx, r['y'] * sy, r['w'] * sx, r['h'] * sy


def _write(img, box, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    part = f'{path}.{uuid.uuid4().hex[:6]}.tmp'
    img.crop(box).save(part, 'JPEG', quality=92, optimize=True)
    os.replace(part, path)
    with open(path, 'rb') as f:
        return hashlib.sha256(f.read()).hexdigest(), os.path.getsize(path)


def rebuild_crops(conn, target_dir, file_id):
    """
    The automatic face crops and the pet crops, cut again from the new 1200
    rung at the paths they already have. Their embeddings are cleared so the
    embedding passes compute them again from upright pixels. Returns counts.
    """
    img = _base_image(conn, target_dir, file_id)
    if img is None:
        return {'faces': 0, 'pets': 0}
    faces = pets = 0

    for r in conn.execute(
            'SELECT fd.id, fd.x, fd.y, fd.w, fd.h, fd.image_width, fd.image_height, '
            '       afc.id AS crop_id, afc.file_path '
            'FROM face_detections fd JOIN ai_face_contents afc ON afc.face_detection_id = fd.id '
            'WHERE fd.file_id = ?', (file_id,)).fetchall():
        r = dict(r)
        x, y, w, h = _scaled(img, r)
        px, py = int(w * FACE_CROP_PAD), int(h * FACE_CROP_PAD)
        box = (max(0, int(x) - px), max(0, int(y) - py),
               min(img.width, int(x + w) + px), min(img.height, int(y + h) + py))
        if box[2] <= box[0] or box[3] <= box[1]:
            continue
        path = r['file_path'] if os.path.isabs(r['file_path']) \
            else os.path.join(target_dir, r['file_path'])
        sha, size = _write(img, box, path)
        conn.execute('UPDATE ai_face_contents SET sha256_hash = ?, file_size = ?, '
                     'embedding = NULL WHERE id = ?', (sha, size, r['crop_id']))
        faces += 1

    try:
        pet_rows = conn.execute(
            'SELECT pd.id, pd.x, pd.y, pd.w, pd.h, pd.image_width, pd.image_height, '
            '       apc.id AS crop_id, apc.file_path '
            'FROM pet_detections pd JOIN ai_pet_contents apc ON apc.pet_detection_id = pd.id '
            'WHERE pd.file_id = ?', (file_id,)).fetchall()
    except Exception:
        pet_rows = []
    for r in pet_rows:
        r = dict(r)
        x, y, w, h = _scaled(img, r)
        pad = PET_CROP_PAD * max(w, h)
        box = (max(0, int(x - pad)), max(0, int(y - pad)),
               min(img.width, int(x + w + pad)), min(img.height, int(y + h + pad)))
        if box[2] - box[0] < 8 or box[3] - box[1] < 8:
            continue
        path = r['file_path'] if os.path.isabs(r['file_path']) \
            else os.path.join(target_dir, r['file_path'])
        sha, _size = _write(img, box, path)
        conn.execute('UPDATE ai_pet_contents SET sha256_hash = ?, embedding = NULL '
                     'WHERE id = ?', (sha, r['crop_id']))
        pets += 1
    conn.commit()
    img.close()
    return {'faces': faces, 'pets': pets}


def rescan(conn, file_id):
    """Face and pet detection look at the photo again on their next run (6.3)."""
    for column in ('face_detection_scanned_at', 'pets_scanned_at'):
        try:
            conn.execute(f'UPDATE file_contents SET {column} = NULL WHERE id = ?', (file_id,))
        except Exception:
            pass                       # the column arrives with its workflow
    conn.commit()


def finish(conn, file_id, error=None):
    """The rebuild's outcome. A finished reset leaves no row behind."""
    if error:
        conn.execute("UPDATE photo_edits SET render_status = 'failed', render_error = ? "
                     "WHERE file_id = ?", (str(error)[:500], file_id))
    else:
        conn.execute("DELETE FROM photo_edits WHERE file_id = ? AND orientation = 1",
                     (file_id,))
        conn.execute("UPDATE photo_edits SET render_status = 'ready', render_error = NULL "
                     "WHERE file_id = ?", (file_id,))
    conn.commit()


def interrupted(conn):
    """Edits left mid-rebuild by a restart, set back to pending; their ids."""
    try:
        conn.execute("UPDATE photo_edits SET render_status = 'pending' "
                     "WHERE render_status = 'rendering'")
        conn.commit()
        return [r[0] for r in conn.execute(
            "SELECT file_id FROM photo_edits WHERE render_status = 'pending'")]
    except Exception:
        return []
