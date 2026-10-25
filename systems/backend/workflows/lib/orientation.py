"""
A photo's own orientation, as the user set it in the Basic Editor
(documentation/specs/basic-photo-editing.md section 4.1).

The value is numbered as the EXIF Orientation tag is (1-8) and applied AFTER the
tag: what is shown is user(exif(original)). The original file is never touched;
the value lives in photo_edits, in the catalog database.

Each value is a pair (m, k): mirror left-right when m is 1, then turn k quarter
turns clockwise. That makes composing tools and inverting an edit arithmetic,
and the PIL table below is the same one ImageOps.exif_transpose uses, so one
mapping serves the tag and the edit alike.

Kept free of app and database imports beyond sqlite3: app.py, the import
workflows and the face and pet workflows all import it.
"""

from PIL import Image

STATE = {1: (0, 0), 6: (0, 1), 3: (0, 2), 8: (0, 3),
         2: (1, 0), 7: (1, 1), 4: (1, 2), 5: (1, 3)}
VALUE = {s: v for v, s in STATE.items()}

USER_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT,
    3: Image.Transpose.ROTATE_180,
    4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE,
    6: Image.Transpose.ROTATE_270,
    7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}

TOOLS = ('rotate_left', 'rotate_right', 'flip_horizontal', 'flip_vertical')


def valid(value):
    return isinstance(value, int) and not isinstance(value, bool) and value in STATE


def swaps(value):
    """True when width and height trade places."""
    return STATE.get(value or 1, (0, 0))[1] % 2 == 1


def compose(value, tool):
    """The orientation after applying `tool` to a photo shown at `value`."""
    m, k = STATE[value]
    if tool == 'rotate_right':
        k += 1
    elif tool == 'rotate_left':
        k -= 1
    elif tool == 'flip_horizontal':
        m, k = 1 - m, -k
    elif tool == 'flip_vertical':
        m, k = 1 - m, 2 - k
    else:
        raise ValueError(f'unknown tool {tool!r}')
    return VALUE[(m, k % 4)]


def _mul(a, b):
    """a after b, as (m, k) pairs: R^ka F^ma R^kb F^mb."""
    (ma, ka), (mb, kb) = a, b
    k = ka + (-kb if ma else kb)
    return ((ma + mb) % 2, k % 4)


def _inv(s):
    m, k = s
    return (m, k) if m else (0, (-k) % 4)


def then(first, second):
    """The single orientation equal to applying `first` and then `second`."""
    return VALUE[_mul(STATE[second], STATE[first])]


def delta(old, new):
    """The orientation d with then(old, d) == new."""
    return VALUE[_mul(STATE[new], _inv(STATE[old]))]


def apply_user(img, value):
    """`img`, already EXIF-oriented, turned the way the user set it."""
    if not value or value == 1:
        return img
    return img.transpose(USER_TRANSPOSE[value])


def transform_box(x, y, w, h, W, H, d):
    """
    A box (x, y, w, h) in a W x H image, after the image is transformed by d.
    Returns (x, y, w, h, W, H) in the new image. Exact in integer pixels.
    """
    if d == 1:
        return x, y, w, h, W, H
    if d == 2:
        return W - x - w, y, w, h, W, H
    if d == 3:
        return W - x - w, H - y - h, w, h, W, H
    if d == 4:
        return x, H - y - h, w, h, W, H
    if d == 5:
        return y, x, h, w, H, W
    if d == 6:
        return H - y - h, x, h, w, H, W
    if d == 7:
        return H - y - h, W - x - w, h, w, H, W
    if d == 8:
        return y, W - x - w, h, w, H, W
    raise ValueError(f'not an orientation: {d!r}')


def summary(value):
    """How the edit reads on the photo page."""
    return {
        1: 'As imported',
        2: 'Flipped horizontally',
        3: 'Rotated 180°',
        4: 'Flipped vertically',
        5: 'Rotated 90° right and flipped horizontally',
        6: 'Rotated 90° right',
        7: 'Rotated 90° left and flipped horizontally',
        8: 'Rotated 90° left',
    }.get(value or 1, 'As imported')


def user_orientation(conn, file_id):
    """The stored edit for a file, or 1 (none). Safe before the table exists."""
    try:
        row = conn.execute('SELECT orientation FROM photo_edits WHERE file_id = ?',
                           (file_id,)).fetchone()
    except Exception:
        return 1
    return (row[0] if row else 1) or 1
