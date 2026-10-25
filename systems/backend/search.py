"""
Search: the box, the filters, one list. documentation/architecture/search-spec.md.

  parse(args)            the request -> criteria (the box's words and key:value
                         keywords, and the filter parameters)
  run(conn, c, ...)      one page of results, keyset-paged, with a capped count
  file_ids(conn, c)      every matching catalog file, for "add all N results"

Results are of two kinds:

  file       a photograph or video in the catalog
  duplicate  a copy an import turned away (file_operations): a SEARCH HIT, not a
             file - which file was used or tried to be used - pointing to the
             catalog file it copies (owner, decision 2). It appears when its own
             name or path matched, or its hash did; every other criterion is
             applied to the file it points to.

What counts as a file is what Photos and Videos list: photo and video types,
not cache/ derivatives, one per image group (a RAW+JPG pair is one), not
removed unless asked.
"""

import base64
import json
import math
import re
import shlex
import time
from datetime import date

import search_index
from media_types import RAW_EXTENSIONS, VIDEO_EXTENSIONS

PAGE = 50
MAX_PAGE = 200
COUNT_CAP = 10000
ADD_CAP = 10000
TIME_BUDGET = 3.0
SIMILAR_BITS = 10                     # perceptual-hash Hamming distance for "more like this"

SORTS = {'date': "COALESCE(fc.master_datetime, '')", 'name': 'LOWER(fc.file_name)',
         'size': 'COALESCE(fc.file_size, 0)'}

KEYWORDS = {'person', 'pet', 'date', 'from', 'to', 'on', 'place', 'near', 'event', 'album',
            'sha', 'type', 'camera', 'is', 'has', 'name', 'description'}


class SearchError(ValueError):
    pass


def _types_sql(types):
    return '(' + ','.join(f"'{t}'" for t in sorted(types)) + ')'


# ── parsing ──────────────────────────────────────────────────────────────────

def _ids(value):
    try:
        return [int(x) for x in str(value or '').split(',') if x.strip()]
    except ValueError:
        raise SearchError('ids must be numbers')


def parse(args, photo_types):
    """The request's parameters and its box, as one criteria dict."""
    a = {k: (v.strip() if isinstance(v, str) else v) for k, v in args.items()}
    c = {
        'words': [], 'names': [], 'people': _ids(a.get('person')), 'people_names': [],
        'people_mode': 'any' if a.get('person_mode') == 'any' else 'all',
        'pets': _ids(a.get('pet')), 'pet_names': [],
        'unnamed_faces': a.get('unnamed_faces') == '1', 'faces': a.get('faces') or None,
        'date': a.get('date') or None, 'from': a.get('from') or None, 'to': a.get('to') or None,
        'on': a.get('on') or None, 'undated': a.get('undated') == '1',
        'locations': _ids(a.get('location')), 'place_names': [],
        'near': a.get('near') or None, 'radius': a.get('radius') or None,
        'has_gps': a.get('has_gps') if a.get('has_gps') in ('0', '1') else None,
        'events': _ids(a.get('event')), 'event_names': [], 'albums': _ids(a.get('album')),
        'album_names': [], 'sha': (a.get('sha') or '').lower() or None,
        'similar_to': int(a['similar_to']) if str(a.get('similar_to') or '').isdigit() else None,
        'type': a.get('type') or None, 'camera': a.get('camera') or None,
        'min_size': a.get('min_size') or None, 'max_size': a.get('max_size') or None,
        'duration_min': a.get('duration_min') or None, 'orientation': a.get('orientation') or None,
        'description': a.get('description') or None, 'date_source': a.get('date_source') or None,
        'added_from': a.get('added_from') or None, 'added_to': a.get('added_to') or None,
        'duplicates': a.get('duplicates') != '0', 'only_duplicates': False,
        'removed': a.get('removed') == '1',
        'sort': a.get('sort') if a.get('sort') in SORTS else 'date',
        'dir': 'asc' if a.get('dir') == 'asc' else 'desc',
        'photo_types': photo_types,
    }
    if a.get('name'):
        c['names'].append(a['name'])
    _parse_box(a.get('q') or '', c)
    _validate(c)
    return c


def _parse_box(q, c):
    """Plain words, and key:value keywords - each also a filter chip."""
    try:
        tokens = shlex.split(q)
    except ValueError:                          # an unbalanced quote: take it literally
        tokens = q.split()
    for tok in tokens:
        key, sep, value = tok.partition(':')
        key = key.lower()
        if not sep or key not in KEYWORDS or not value:
            c['words'].append(tok)
            continue
        if key == 'person':
            c['people_names'].append(value)
        elif key == 'pet':
            c['pet_names'].append(value)
        elif key in ('date', 'from', 'to', 'on', 'camera', 'type', 'description'):
            c[key] = value
        elif key == 'place':
            c['place_names'].append(value)
        elif key == 'near':
            parts = value.split(',')
            c['near'] = ','.join(parts[:2])
            if len(parts) > 2:
                c['radius'] = parts[2]
        elif key == 'event':
            c['event_names'].append(value)
        elif key == 'album':
            c['album_names'].append(value)
        elif key == 'sha':
            c['sha'] = value.lower()
        elif key == 'name':
            c['names'].append(value)
        elif key == 'is':
            if value == 'duplicate':
                c['only_duplicates'] = True
            elif value == 'removed':
                c['removed'] = True
        elif key == 'has':
            if value == 'gps':
                c['has_gps'] = '1'
            elif value in ('no-gps', 'nogps'):
                c['has_gps'] = '0'
            elif value in ('unnamed-face', 'unnamed-faces'):
                c['unnamed_faces'] = True


def _validate(c):
    for w in c['words'] + c['names']:
        if len(w) < search_index.MIN_CHARS and not w.isdigit():
            raise SearchError(f'"{w}" is too short: search for at least '
                              f'{search_index.MIN_CHARS} characters')
    if c['sha'] and (len(c['sha']) < 8 or not re.fullmatch(r'[0-9a-f]+', c['sha'])):
        raise SearchError('A hash search needs at least 8 hexadecimal characters')
    if c['date'] and not re.fullmatch(r'\d{4}(-\d{2}(-\d{2})?)?', c['date']):
        raise SearchError('date is a year, a month or a day: 2019, 2019-07, 2019-07-04')
    for k in ('from', 'to'):
        if c[k] and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', c[k]):
            raise SearchError(f'{k} is a day: 2019-07-04')
    if c['on'] and not re.fullmatch(r'\d{2}-\d{2}', c['on']):
        raise SearchError('on is a month and day: 07-04')
    if c['near']:
        try:
            lat, lng = (float(x) for x in c['near'].split(','))
        except ValueError:
            raise SearchError('near is latitude,longitude')
        c['near'] = (lat, lng)
        c['radius'] = _radius(c['radius'])


def _radius(value):
    if not value:
        return 1000.0
    m = re.fullmatch(r'(\d+(?:\.\d+)?)\s*(km|m)?', str(value).strip().lower())
    if not m:
        raise SearchError('radius is metres, or 1km')
    n = float(m[1])
    return n * 1000 if m[2] == 'km' else n


# ── geohash, for "near" ──────────────────────────────────────────────────────

_B32 = '0123456789bcdefghjkmnpqrstuvwxyz'


def geohash(lat, lng, precision):
    lat_r, lng_r, bits, bit, ch, out, even = [-90.0, 90.0], [-180.0, 180.0], 0, 0, 0, [], True
    while len(out) < precision:
        rng, val = (lng_r, lng) if even else (lat_r, lat)
        mid = (rng[0] + rng[1]) / 2
        if val >= mid:
            ch |= 1 << (4 - bit)
            rng[0] = mid
        else:
            rng[1] = mid
        even = not even
        bit += 1
        if bit == 5:
            out.append(_B32[ch])
            bit, ch = 0, 0
    return ''.join(out)


def _cell_size(precision):
    """(lat degrees, lng degrees) of one cell."""
    lng_bits = math.ceil(precision * 5 / 2)
    lat_bits = math.floor(precision * 5 / 2)
    return 180.0 / 2 ** lat_bits, 360.0 / 2 ** lng_bits


def near_cells(lat, lng, radius_m):
    """The geohash cells covering a circle: the point's cell and its neighbours,
    at the finest precision whose cell is at least the radius."""
    precision = 1
    for p in range(9, 0, -1):
        dlat, _ = _cell_size(p)
        if dlat * 111_000 >= radius_m:
            precision = p
            break
    dlat, dlng = _cell_size(precision)
    cells = set()
    for i in (-1, 0, 1):
        for j in (-1, 0, 1):
            la = max(-89.999, min(89.999, lat + i * dlat))
            lo = ((lng + j * dlng + 180) % 360) - 180
            cells.add(geohash(la, lo, precision))
    return sorted(cells)


def _distance_m(lat1, lng1, lat2, lng2):
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lng2 - lng1)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 6_371_000 * 2 * math.asin(min(1.0, math.sqrt(h)))


# ── building the query ───────────────────────────────────────────────────────

class _Q:
    """WHERE clauses and their parameters, plus temp tables of ids."""

    def __init__(self, conn):
        self.conn, self.where, self.params, self.n = conn, [], [], 0
        self.selective = False

    def add(self, sql, *params):
        self.where.append(sql)
        self.params.extend(params)

    def temp(self, ids):
        self.n += 1
        name = f'temp.s{self.n}'
        self.conn.execute(f'DROP TABLE IF EXISTS {name}')
        self.conn.execute(f'CREATE TEMP TABLE s{self.n} (id INTEGER PRIMARY KEY)')
        self.conn.executemany(f'INSERT OR IGNORE INTO {name} (id) VALUES (?)', ((i,) for i in ids))
        return name

    def only(self, sql, *params):
        """Restrict to the files an indexed query names. Selective criteria
        are answered this way - people through face_detections(person_id),
        places through file_locations(location_id), events through event_files -
        so a search starts from the few files they name rather than testing
        every file in the catalog."""
        self.selective = True
        ids = [r[0] for r in self.conn.execute(sql, params)]
        self.add(f'fc.id IN (SELECT id FROM {self.temp(ids)})')
        return ids


def _people_ids(conn, names, kind):
    ids = []
    for n in names:
        found = [r[0] for r in conn.execute(
            'SELECT id FROM people WHERE name LIKE ? AND COALESCE(kind, \'person\') = ?',
            ('%' + n + '%', kind))]
        if not found:
            raise SearchError(f'No {"pet" if kind == "pet" else "person"} called "{n}"')
        ids.append(found)
    return ids


def _base(c, q):
    """What counts as a file, and removed files."""
    types = set(c['photo_types']) | set(VIDEO_EXTENSIONS)
    q.add(f'LOWER(fc.file_type) IN {_types_sql(types)}')
    q.add("fc.file_path NOT LIKE 'cache/%'")
    # One per image group (a RAW+JPG pair is one): a primary-key lookup per row,
    # not a 55,000-row NOT IN set built for every query.
    q.add('(fc.image_group_id IS NULL OR fc.id = (SELECT g.primary_file_id FROM image_groups g '
          'WHERE g.id = fc.image_group_id))')
    if not c['removed']:
        q.add('fc.removed_at IS NULL')


def _date_range(value):
    lo = value
    return lo, lo + '~'


def _criteria(conn, c, q, matched):
    """Every criterion but the words, on file_contents fc. `matched` collects,
    per file id, the text that made it match (for the Name column)."""
    if c['date']:
        lo, hi = _date_range(c['date'])
        q.add('fc.master_datetime >= ? AND fc.master_datetime < ?', lo, hi)
    if c['from']:
        q.add('fc.master_datetime >= ?', c['from'])
    if c['to']:
        q.add('fc.master_datetime < ?', c['to'] + '~')
    if c['on']:
        q.add('substr(fc.master_datetime, 6, 5) = ?', c['on'])
    if c['undated']:
        q.add("COALESCE(fc.master_datetime, '') = ''")

    person_groups = [[p] for p in c['people']] + _people_ids(conn, c['people_names'], 'person')
    if person_groups:
        groups = ([sorted({i for g in person_groups for i in g})] if c['people_mode'] == 'any'
                  else person_groups)
        for g in groups:
            q.only(f'SELECT DISTINCT file_id FROM face_detections WHERE person_id IN ({_in(g)})', *g)
    for g in [[p] for p in c['pets']] + _people_ids(conn, c['pet_names'], 'pet'):
        q.only(f'SELECT DISTINCT file_id FROM pet_detections WHERE person_id IN ({_in(g)})', *g)
    if c['unnamed_faces']:
        q.add('EXISTS (SELECT 1 FROM face_detections fd WHERE fd.file_id = fc.id '
              'AND fd.person_id IS NULL)')
    if c['faces'] in ('alone', 'few', 'group'):
        lo, hi = {'alone': (1, 1), 'few': (2, 3), 'group': (4, 100000)}[c['faces']]
        q.add('(SELECT COUNT(*) FROM face_detections fd WHERE fd.file_id = fc.id) BETWEEN ? AND ?',
              lo, hi)

    locs = list(c['locations'])
    for n in c['place_names']:
        found = [r[0] for r in conn.execute('SELECT id FROM locations WHERE name LIKE ?',
                                            ('%' + n + '%',))]
        if not found:
            raise SearchError(f'No place called "{n}"')
        locs += found
    if locs:
        q.only(f'SELECT DISTINCT file_id FROM file_locations WHERE location_id IN ({_in(locs)})', *locs)
    if c['near']:
        lat, lng = c['near']
        ids = []
        for cell in near_cells(lat, lng, c['radius']):
            for fid, la, lo in conn.execute(
                    'SELECT file_id, latitude, longitude FROM exif_data '
                    'WHERE geohash >= ? AND geohash < ?', (cell, cell + '~')):
                if la is not None and lo is not None and \
                        _distance_m(lat, lng, la, lo) <= c['radius']:
                    ids.append(fid)
        q.selective = True
        q.add(f'fc.id IN (SELECT id FROM {q.temp(ids)})')
    if c['has_gps'] == '1':
        q.add('EXISTS (SELECT 1 FROM exif_data e WHERE e.file_id = fc.id AND e.latitude IS NOT NULL)')
    elif c['has_gps'] == '0':
        q.add('NOT EXISTS (SELECT 1 FROM exif_data e WHERE e.file_id = fc.id AND e.latitude IS NOT NULL)')

    events = list(c['events'])
    for n in c['event_names']:
        found = [r[0] for r in conn.execute('SELECT id FROM events WHERE name LIKE ?', ('%' + n + '%',))]
        if not found:
            raise SearchError(f'No event called "{n}"')
        events += found
    if events:
        q.only(f'SELECT DISTINCT file_id FROM event_files WHERE event_id IN ({_in(events)})', *events)
    albums = list(c['albums'])
    for n in c['album_names']:
        found = [r[0] for r in conn.execute('SELECT id FROM albums WHERE name LIKE ?', ('%' + n + '%',))]
        if not found:
            raise SearchError(f'No album called "{n}"')
        albums += found
    if albums:
        q.only(f'SELECT DISTINCT sp.file_id FROM section_photos sp JOIN album_sections s '
               f'ON s.id = sp.section_id WHERE s.album_id IN ({_in(albums)})', *albums)

    if c['sha']:
        q.only('SELECT id FROM file_contents WHERE sha256_hash >= ? AND sha256_hash < ?',
               c['sha'], c['sha'] + 'g')
        q.sha_table = q.where[-1]                 # 'fc.id IN (SELECT id FROM temp.sN)'
    if c['similar_to'] is not None:
        row = conn.execute('SELECT perceptual_hash FROM file_contents WHERE id = ?',
                           (c['similar_to'],)).fetchone()
        if not row or not row[0]:
            raise SearchError('That file has no perceptual hash to compare')
        ref = int(row[0], 16)
        ids = [fid for fid, h in conn.execute(
            'SELECT id, perceptual_hash FROM file_contents WHERE perceptual_hash IS NOT NULL')
            if h and bin(int(h, 16) ^ ref).count('1') <= SIMILAR_BITS]
        q.selective = True
        q.add(f'fc.id IN (SELECT id FROM {q.temp(ids)})')

    t = c['type']
    if t == 'photo':
        q.add(f'LOWER(fc.file_type) IN {_types_sql(c["photo_types"])}')
    elif t == 'video':
        q.add(f'LOWER(fc.file_type) IN {_types_sql(VIDEO_EXTENSIONS)}')
    elif t == 'raw':
        q.add(f'(LOWER(fc.file_type) IN {_types_sql(RAW_EXTENSIONS)} OR EXISTS (SELECT 1 FROM '
              "image_groups g WHERE g.id = fc.image_group_id AND g.kind = 'raw_jpeg'))")
    elif t == 'live':
        q.add("EXISTS (SELECT 1 FROM image_groups g WHERE g.id = fc.image_group_id "
              "AND g.kind = 'live_photo')")
    elif t:
        raise SearchError('type is photo, video, raw or live')
    if c['camera']:
        q.add("EXISTS (SELECT 1 FROM exif_data e WHERE e.file_id = fc.id AND "
              "(COALESCE(e.camera_make, '') || ' ' || COALESCE(e.camera_model, '')) LIKE ?)",
              '%' + c['camera'] + '%')
    if c['orientation'] in ('portrait', 'landscape', 'square'):
        op = {'portrait': '<', 'landscape': '>', 'square': '='}[c['orientation']]
        q.add(f'EXISTS (SELECT 1 FROM exif_data e WHERE e.file_id = fc.id AND e.width {op} e.height)')
    for key, sql in (('min_size', 'fc.file_size >= ?'), ('max_size', 'fc.file_size <= ?'),
                     ('duration_min', 'fc.duration_seconds >= ?')):
        if c[key]:
            try:
                q.add(sql, float(c[key]))
            except ValueError:
                raise SearchError(f'{key} must be a number')
    if c['description']:
        q.add('fc.description LIKE ?', '%' + c['description'] + '%')
    if c['date_source']:
        q.add('fc.master_datetime_source = ?', c['date_source'])
    if c['added_from']:
        q.add('fc.added_at >= ?', c['added_from'])
    if c['added_to']:
        q.add('fc.added_at < ?', c['added_to'] + '~')


def _words(conn, c, q, matched, dup_ops):
    """Each word (and name:) must match: a name or path the file had, its
    description, or the name of a person, pet, place or event in it. A name of
    a turned-away duplicate also yields a DUPLICATE row (dup_ops)."""
    first = True
    for w in c['words'] + c['names']:
        name_only = w in c['names']
        hits = search_index.name_hits(conn, w) if len(w) >= search_index.MIN_CHARS else []
        files = set()
        ops = set()
        for h in hits:
            if h['kind'] == 'duplicate':
                ops.add(h['op_id'])
                matched.setdefault(('d', h['op_id']), (h['kind'], h['text']))
            else:
                files.add(h['file_id'])
                matched.setdefault(('f', h['file_id']), (h['kind'], h['text']))
        dup_ops[:] = list(ops if first else set(dup_ops) & ops)
        first = False
        if not name_only:
            # Everything else a word can be, each through an index - never a
            # subquery tested against every file in the catalog.
            like = '%' + w + '%'
            for sql in (
                    'SELECT DISTINCT fd.file_id FROM face_detections fd WHERE fd.person_id IN '
                    '(SELECT id FROM people WHERE name LIKE ? AND COALESCE(kind, \'person\') = \'person\')',
                    'SELECT DISTINCT pd.file_id FROM pet_detections pd WHERE pd.person_id IN '
                    '(SELECT id FROM people WHERE name LIKE ?)',
                    'SELECT DISTINCT fl.file_id FROM file_locations fl WHERE fl.location_id IN '
                    '(SELECT id FROM locations WHERE name LIKE ?)',
                    'SELECT DISTINCT ef.file_id FROM event_files ef WHERE ef.event_id IN '
                    '(SELECT id FROM events WHERE name LIKE ?)',
                    'SELECT id FROM file_contents WHERE description LIKE ?'):
                files.update(r[0] for r in conn.execute(sql, (like,)))
        q.selective = True
        q.add(f'fc.id IN (SELECT id FROM {q.temp(files)})')
    return bool(c['words'] or c['names'])


def _union(conn, c, matched):
    """(sql, params) of every result row: (sk, rank, file_id, op_id, kind)."""
    files_q = _Q(conn)
    _base(c, files_q)
    _criteria(conn, c, files_q, matched)
    # Criteria applied to a duplicate's catalog file: everything but the words.
    dup_where, dup_params = list(files_q.where), list(files_q.params)
    dup_ops = []
    has_words = _words(conn, c, files_q, matched, dup_ops)

    sk = SORTS[c['sort']]
    parts, params = [], []
    if not c['only_duplicates']:
        parts.append(f"SELECT {sk} AS sk, 1 AS rank, fc.id AS file_id, 0 AS op_id "
                     f"FROM file_contents fc WHERE {' AND '.join(files_q.where)}")
        params += files_q.params
    if c['duplicates']:
        # A duplicate is listed when its own name or path matched (the words),
        # or - with no words - when its hash did: it has the same SHA-256 as the
        # catalog file it copies, so every turned-away copy of a matching file.
        which = None
        if has_words:
            which = f'fo.id IN (SELECT id FROM {files_q.temp(dup_ops)})'
        elif c['sha']:
            which = (files_q.sha_table.replace('fc.id', 'fo.file_id') +
                     " AND (fo.operation_type = 'duplicate_moved' OR (fo.operation_type = 'skip' "
                     "AND fo.details LIKE 'Duplicate hash:%'))")
        if which:
            parts.append(
                f"SELECT {sk} AS sk, 0 AS rank, fc.id AS file_id, fo.id AS op_id "
                f"FROM file_operations fo JOIN file_contents fc ON fc.id = fo.file_id "
                f"WHERE {which} AND {' AND '.join(dup_where)}")
            params += dup_params
    if not parts:
        return None, [], None
    single = ' AND '.join(files_q.where) if len(parts) == 1 and not c['only_duplicates'] else None
    return ' UNION ALL '.join(parts), params, single


def _cursor_encode(row):
    return base64.urlsafe_b64encode(json.dumps(list(row)).encode()).decode().rstrip('=')


def _cursor_decode(value):
    try:
        pad = '=' * (-len(value) % 4)
        sk, rank, fid, op = json.loads(base64.urlsafe_b64decode(value + pad))
        return sk, int(rank), int(fid), int(op)
    except Exception:
        raise SearchError('That page link is no longer valid - search again')


def run(conn, c, after=None, limit=PAGE):
    """One page: {results, total, total_capped, next_cursor}."""
    started = time.monotonic()
    matched = {}
    union, params, single = _union(conn, c, matched)
    if union is None:
        return {'results': [], 'total': 0, 'total_capped': False, 'next_cursor': None}
    order = 'DESC' if c['dir'] == 'desc' else 'ASC'
    cmp = '<' if c['dir'] == 'desc' else '>'
    page_params = list(params)
    if single:
        # No duplicate rows: straight from file_contents, so the date sort walks
        # idx_fc_sort_date and stops after one page instead of sorting the lot.
        sk = SORTS[c['sort']]
        page_sql = f'SELECT {sk} AS sk, 1, fc.id, 0 FROM file_contents fc WHERE {single}'
        if after:
            a_sk, _, a_fid, _ = _cursor_decode(after)
            page_sql += f' AND ({sk}, fc.id) {cmp} (?, ?)'
            page_params += [a_sk, a_fid]
        page_sql += f' ORDER BY {sk} {order}, fc.id {order} LIMIT ?'
    else:
        page_sql = f'SELECT sk, rank, file_id, op_id FROM ({union}) u'
        if after:
            page_sql += f' WHERE (sk, rank, file_id, op_id) {cmp} (?, ?, ?, ?)'
            page_params += list(_cursor_decode(after))
        page_sql += f' ORDER BY sk {order}, rank {order}, file_id {order}, op_id {order} LIMIT ?'
    rows = conn.execute(page_sql, (*page_params, limit + 1)).fetchall()
    more = len(rows) > limit
    rows = rows[:limit]
    total = conn.execute(f'SELECT COUNT(*) FROM (SELECT 1 FROM ({union}) LIMIT {COUNT_CAP + 1})',
                         params).fetchone()[0]
    return {
        'results': describe(conn, rows, matched),
        'total': min(total, COUNT_CAP),
        'total_capped': total > COUNT_CAP,
        'next_cursor': _cursor_encode(rows[-1]) if more and rows else None,
        'took_ms': round((time.monotonic() - started) * 1000),
    }


def file_ids(conn, c, cap=ADD_CAP):
    """The catalog files a search finds - duplicates as the file they point to -
    for "add all N results" to a saved search."""
    matched = {}
    union, params, _ = _union(conn, c, matched)
    if union is None:
        return []
    return [r[0] for r in conn.execute(
        f'SELECT DISTINCT file_id FROM ({union}) LIMIT ?', (*params, cap))]


# ── describing a page ────────────────────────────────────────────────────────

def _in(ids):
    return ','.join('?' * len(ids))


def describe(conn, rows, matched=None):
    """The page's rows, with what the list shows - a handful of indexed lookups
    for the page's files, never for the whole result."""
    matched = matched or {}
    fids = sorted({r[2] for r in rows})
    if not fids:
        return []
    files = {r[0]: r for r in conn.execute(
        f'SELECT fc.id, fc.file_name, fc.file_type, fc.file_size, fc.master_datetime, '
        f'fc.master_datetime_source, fc.sha256_hash, fc.removed_at, fc.duration_seconds, '
        f'fc.description, g.kind FROM file_contents fc '
        f'LEFT JOIN image_groups g ON g.id = fc.image_group_id WHERE fc.id IN ({_in(fids)})', fids)}
    exif = {r[0]: r for r in conn.execute(
        f'SELECT file_id, camera_make, camera_model, width, height, latitude, longitude '
        f'FROM exif_data WHERE file_id IN ({_in(fids)})', fids)}
    people, pets, unnamed = {}, {}, {}
    for fid, pid, name in conn.execute(
            f'SELECT fd.file_id, fd.person_id, p.name FROM face_detections fd '
            f'LEFT JOIN people p ON p.id = fd.person_id WHERE fd.file_id IN ({_in(fids)})', fids):
        if pid is None:
            unnamed[fid] = unnamed.get(fid, 0) + 1
        elif name and name not in people.setdefault(fid, []):
            people[fid].append(name)
    for fid, name in conn.execute(
            f'SELECT pd.file_id, p.name FROM pet_detections pd JOIN people p ON p.id = pd.person_id '
            f'WHERE pd.file_id IN ({_in(fids)})', fids):
        if name not in pets.setdefault(fid, []):
            pets[fid].append(name)
    places = {r[0]: r[1] for r in conn.execute(
        f'SELECT fl.file_id, l.name FROM file_locations fl JOIN locations l ON l.id = fl.location_id '
        f'WHERE fl.file_id IN ({_in(fids)})', fids)}
    events = {}
    for fid, name in conn.execute(
            f'SELECT ef.file_id, e.name FROM event_files ef JOIN events e ON e.id = ef.event_id '
            f'WHERE ef.file_id IN ({_in(fids)}) ORDER BY e.start_time', fids):
        events.setdefault(fid, name)
    originals = {}
    for fid, path in conn.execute(
            f"SELECT file_id, source_path FROM file_operations WHERE file_id IN ({_in(fids)}) "
            f"AND operation_type IN ('move', 'copy', 'index') ORDER BY id", fids):
        originals.setdefault(fid, path)
    ops = [r[3] for r in rows if r[3]]
    dups = {r[0]: r for r in conn.execute(
        f'SELECT id, source_path, target_path, operation_type, operation_date, execution_id '
        f'FROM file_operations WHERE id IN ({_in(ops)})', ops)} if ops else {}

    out = []
    for sk, rank, fid, op in rows:
        f = files.get(fid)
        if f is None:
            continue
        e = exif.get(fid)
        ext = (f[2] or '').lower()
        kind = ('Live Photo' if f[10] == 'live_photo' else 'RAW+JPG' if f[10] == 'raw_jpeg'
                else 'Video' if ext in VIDEO_EXTENSIONS else 'RAW' if ext in RAW_EXTENSIONS else 'Photo')
        item = {
            'kind': 'duplicate' if op else 'file', 'file_id': fid, 'name': f[1],
            'taken': f[4], 'taken_source': f[5], 'type': kind, 'size': f[3],
            'width': e[3] if e else None, 'height': e[4] if e else None,
            'camera': ' '.join(x for x in (e[1], e[2]) if x) if e else None,
            'people': people.get(fid, []), 'pets': pets.get(fid, []),
            'unnamed_faces': unnamed.get(fid, 0),
            'place': places.get(fid) or (f'{e[5]:.4f}, {e[6]:.4f}' if e and e[5] is not None else None),
            'event': events.get(fid), 'original': originals.get(fid),
            'sha256': f[6], 'removed_at': f[7], 'duration': f[8], 'description': f[9],
            'matched': None, 'duplicate': None,
        }
        hit = matched.get(('d', op)) if op else matched.get(('f', fid))
        if hit:
            item['matched'] = {'on': hit[0], 'text': hit[1]}
        if op and op in dups:
            d = dups[op]
            item['duplicate'] = {'source_path': d[1],
                                 'moved_to': d[2] if d[3] == 'duplicate_moved' else None,
                                 'at': d[4], 'execution_id': d[5], 'op_id': d[0]}
        out.append(item)
    return out
