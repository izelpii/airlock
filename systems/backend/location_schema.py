#!/usr/bin/env python3
"""
The single definition of the location clustering and resolution schema.

Written as one module for the reason face_recognition_schema.py exists: there
were three definitions of `face_similarity_scores` and they had drifted, so a
catalog whose table happened to be created by a workflow run was permanently
missing a column that models.py knew about. These tables are created by
models.py on every catalog open AND by two workflows that can each be the first
thing to touch a catalog database, which is three paths again. One list here,
and adding a column means editing one list.

`ensure_location_schema()` both creates and upgrades. It is idempotent and safe
to call on every startup and every workflow run.

See documentation/specs/location-spec.md section 5.
"""

import sqlite3


# ── The resolved answer, per file ────────────────────────────────────────────
# TWO facts, TWO provenance columns, because they resolve from different ladders
# and a query may trust one and not the other:
#
#   coordinates:  manual > exif > event*    (* off by default, spec 4.5c)
#   place:        manual > cluster > a person's tag on the event >
#                 the event's member vote
#
# INVARIANT: the clustering pass reads ONLY rows with coord_source = 'exif'.
# Feeding it a derived coordinate makes run n+1 cluster run n's inventions.
FILE_LOCATION_COLUMNS = (
    ('file_id',        'INTEGER PRIMARY KEY'),
    ('latitude',       'REAL'),
    ('longitude',      'REAL'),
    ('coord_source',   'TEXT'),       # 'exif' | 'manual' | 'event' | NULL
    ('coord_radius_m', 'REAL'),       # 0 for 'exif'; the event's spread otherwise
    ('location_id',    'INTEGER'),
    ('location_source',   'TEXT'),       # 'cluster' | 'manual' | 'event' | NULL
    # Set only when location_source='event'. Auto event ids do not survive a Create
    # Events re-run, so this is for the UI ("from event Easter 2019"), never for
    # durable linkage.
    ('location_event_id', 'INTEGER'),
    # NULL when a person tagged the event, a fraction when its members voted.
    # This is what tells the two apart.
    ('location_share',    'REAL'),
    ('updated_at',     'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
)

# ── A proposal: photos taken close together, named by nobody yet ─────────────
LOCATION_CLUSTER_COLUMNS = (
    ('id',             'INTEGER PRIMARY KEY AUTOINCREMENT'),
    # The geohash prefix this grew from, and how many characters that was.
    # Unique together, so a re-run UPDATES its cluster rather than proposing it
    # again - which is what lets an accepted or rejected decision survive.
    ('geohash_prefix', 'TEXT NOT NULL'),
    ('precision',      'INTEGER NOT NULL'),
    # Centroid of the members, not the first member's coordinate.
    ('latitude',       'REAL NOT NULL'),
    ('longitude',      'REAL NOT NULL'),
    ('radius_metres',  'REAL'),
    ('file_count',     'INTEGER NOT NULL DEFAULT 0'),
    # MIN/MAX of the members' master_datetime: "we were here in 2014 and again
    # in 2019" is what makes a cluster recognisable to a person.
    ('first_seen',     'TEXT'),
    ('last_seen',      'TEXT'),
    # Geocoder suggestions as JSON, never as a name (spec 4.4). Cached so the
    # page needs no round trip per cluster, and so a service that is down costs
    # suggestions rather than the workflow.
    ('suggestions',    'TEXT'),
    # 'proposed' | 'accepted' | 'rejected'. A rejected cluster is KEPT so the
    # next run does not propose it again.
    ('status',         "TEXT NOT NULL DEFAULT 'proposed'"),
    ('location_id',    'INTEGER'),
    ('created_at',     'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
    ('updated_at',     'TIMESTAMP DEFAULT CURRENT_TIMESTAMP'),
)

LOCATION_CLUSTER_FILE_COLUMNS = (
    ('cluster_id',      'INTEGER NOT NULL'),
    ('file_id',         'INTEGER NOT NULL'),
    ('distance_metres', 'REAL'),
)

# No REFERENCES clauses anywhere above, for the reason models.py records about
# image_groups: this application never enables PRAGMA foreign_keys, so an
# ON DELETE clause would be inert and reading it as live would be worse than not
# writing it. Deleting a location clears file_locations.location_id explicitly -
# see LocationModel.delete_location.

_INDEXES = (
    ('idx_location_clusters_prefix',
     'CREATE UNIQUE INDEX IF NOT EXISTS idx_location_clusters_prefix '
     'ON location_clusters (geohash_prefix, precision)'),
    ('idx_location_clusters_status',
     'CREATE INDEX IF NOT EXISTS idx_location_clusters_status '
     'ON location_clusters (status)'),
    ('idx_location_cluster_files_file',
     'CREATE INDEX IF NOT EXISTS idx_location_cluster_files_file '
     'ON location_cluster_files (file_id)'),
    ('idx_file_locations_location',
     'CREATE INDEX IF NOT EXISTS idx_file_locations_location '
     'ON file_locations (location_id) WHERE location_id IS NOT NULL'),
    ('idx_file_locations_location_source',
     'CREATE INDEX IF NOT EXISTS idx_file_locations_location_source '
     'ON file_locations (location_source)'),
)


def _table_exists(cur, name):
    return cur.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
        (name,)).fetchone() is not None


def _columns(cur, table):
    return {r[1] for r in cur.execute(f'PRAGMA table_info({table})')}


def _ensure_table(cur, name, columns, extra='', note=lambda m: None):
    if not _table_exists(cur, name):
        body = ',\n                '.join(f'{n} {d}' for n, d in columns)
        cur.execute(f'CREATE TABLE {name} (\n                {body}{extra}\n            )')
        note(f'created {name}')
        return
    have = _columns(cur, name)
    for col, decl in columns:
        if col in have:
            continue
        # A PRIMARY KEY or NOT NULL column cannot be added to a populated table
        # without a default, and none of these ever need to be: new columns in
        # this schema are nullable by design so that an upgrade is one ALTER.
        decl_add = decl.replace('PRIMARY KEY AUTOINCREMENT', '').replace(
            'PRIMARY KEY', '').replace('NOT NULL', '').strip()
        try:
            cur.execute(f'ALTER TABLE {name} ADD COLUMN {col} {decl_add}')
            note(f'added {name}.{col}')
        except sqlite3.Error:
            pass


def ensure_location_schema(db, verbose=False):
    """
    Create or upgrade every table the location feature needs.

    `db` is a path or an open sqlite3.Connection, matching
    ensure_face_recognition_schema. A connection passed in is committed to but
    not closed - this function does not close what it did not open.

    Also creates the partial index on exif_data.geohash, which is what makes
    bucketing one indexed pass (spec 4.3). Skipped when exif_data does not
    exist yet, because a catalog with no imports has nothing to index.

    Returns a list of the changes made, empty when everything was already there.
    """
    owns = not isinstance(db, sqlite3.Connection)
    conn = sqlite3.connect(db, timeout=60) if owns else db
    cur = conn.cursor()
    changed = []

    def note(msg):
        changed.append(msg)
        if verbose:
            print(f'  + {msg}')

    try:
        # ── Vocabulary migration ──────────────────────────────────────────
        # The application had been using "place" and "location" for the same
        # concept, and only one of the two is a word the interface uses. These
        # columns were written before that was settled; any catalog opened
        # between then and now has the old names. RENAME COLUMN needs SQLite
        # 3.25+, which both the container (3.46) and the development Macs
        # (3.39) are well past.
        if _table_exists(cur, 'file_locations'):
            have = _columns(cur, 'file_locations')
            for old_name, new_name in (('place_source', 'location_source'),
                                       ('place_event_id', 'location_event_id'),
                                       ('place_share', 'location_share')):
                if old_name in have and new_name not in have:
                    try:
                        cur.execute(f'ALTER TABLE file_locations '
                                    f'RENAME COLUMN {old_name} TO {new_name}')
                        note(f'renamed file_locations.{old_name} -> {new_name}')
                    except sqlite3.Error:
                        pass

        _ensure_table(cur, 'location_clusters', LOCATION_CLUSTER_COLUMNS,
                      note=note)
        _ensure_table(cur, 'location_cluster_files', LOCATION_CLUSTER_FILE_COLUMNS,
                      extra=',\n                PRIMARY KEY (cluster_id, file_id)',
                      note=note)
        _ensure_table(cur, 'file_locations', FILE_LOCATION_COLUMNS, note=note)

        # The index was named for the old column. Dropped rather than left:
        # an index nothing knows about is one nobody will think to maintain.
        cur.execute('DROP INDEX IF EXISTS idx_file_locations_place_source')

        for name, sql in _INDEXES:
            cur.execute(sql)

        if _table_exists(cur, 'exif_data'):
            cur.execute('CREATE INDEX IF NOT EXISTS idx_exif_geohash '
                        'ON exif_data (geohash) WHERE geohash IS NOT NULL')

        conn.commit()
    finally:
        if owns:
            conn.close()

    return changed
