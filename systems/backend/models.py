import sqlite3
import json
import os
import threading
from datetime import datetime
from typing import List, Dict, Optional

from face_recognition_schema import ensure_face_recognition_schema
from location_schema import ensure_location_schema
from pet_schema import ensure_pet_schema
from workflow_queue_schema import ensure_workflow_queue_schema

class Database:
    """Main database for workflows and catalog metadata"""
    def __init__(self, db_path: Optional[str] = None):
        # Use absolute path to database directory
        if db_path is None:
            # Database directory sits alongside the app files
            app_dir = os.path.dirname(os.path.abspath(__file__))
            db_path = os.path.join(app_dir, 'database', 'catalogs.db')
        self.db_path = db_path
        # Ensure the directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.init_db()
    
    def get_connection(self):
        # 30 s, not sqlite's default 5: catalogs.db is in rollback-journal mode,
        # where a writer waits for every reader, and the scheduler (60 s) and
        # each request's session check all share it.
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # catalog_meta is NOT created here. catalog.py owns it, and this file
        # used to carry a second, obsolete definition of the same table: `owner
        # TEXT NOT NULL`, no file_strategy, no slug, no owner_user_id.
        #
        # CREATE TABLE IF NOT EXISTS makes that silent. Whichever module ran
        # first won, and the other's definition was skipped without a word. It
        # worked only because importing catalog.py used to create the table as a
        # side effect, before this ran. The moment that import-time side effect
        # was removed, this stub won the race and creating a catalog failed with
        # "table catalog_meta has no column named file_strategy" on a fresh
        # database - while every existing database kept working, because the
        # table was already correct.
        #
        # Two definitions of one table is the bug. See catalog.Catalog.init_db.

        # User settings table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT,
                address TEXT,
                city TEXT,
                state TEXT,
                country TEXT,
                lat REAL,
                long REAL,
                birthdate TEXT,
                user_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Added after the table shipped, so existing installations pick it up on
        # the next start rather than needing a separate migration. Same approach
        # as the person link on users.
        _cols = {r[1] for r in cursor.execute('PRAGMA table_info(user_settings)')}
        if 'birthdate' not in _cols:
            cursor.execute('ALTER TABLE user_settings ADD COLUMN birthdate TEXT')
        # user_id: settings are per user (UserSettingsModel selects by it). It
        # used to be added only by migrate_add_users.py, so an installation
        # that never ran that script - oldserver - failed every settings save with
        # "no such column: user_id".
        if 'user_id' not in _cols:
            cursor.execute('ALTER TABLE user_settings ADD COLUMN user_id INTEGER')
            # The pre-users row belonged to whoever ran the installation, as the
            # migration assumed: give it to the administrator when there is
            # exactly one. The users table may not exist yet on a new install.
            try:
                admins = cursor.execute(
                    'SELECT id FROM users WHERE is_admin = 1').fetchall()
                if len(admins) == 1:
                    cursor.execute('UPDATE user_settings SET user_id = ? WHERE user_id IS NULL',
                                   (admins[0][0],))
            except sqlite3.Error:
                pass

        # Insert default user if none exists
        cursor.execute('SELECT COUNT(*) as count FROM user_settings')
        if cursor.fetchone()['count'] == 0:
            cursor.execute('''
                INSERT INTO user_settings (name, email, address, city, state, country, lat, long)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', ('Default User', '', '', '', '', '', None, None))

        conn.commit()
        conn.close()

# Schemas this process has already ensured: db_path -> the schema_version it
# had when we finished. See CatalogDatabase.__init__.
_SCHEMA_ENSURED = {}
_SCHEMA_ENSURED_LOCK = threading.Lock()
# Database files this process has already put into WAL mode. Guarded by
# _SCHEMA_ENSURED_LOCK; see CatalogDatabase.get_connection.
_WAL_SET = set()



class CatalogDatabase:
    """Catalog-specific database for execution results and file tracking"""

    def __init__(self, target_directory: str):
        self.target_directory = target_directory
        self.db_path = os.path.join(target_directory, 'workflow_results.db')
        # Ensure the directory exists
        os.makedirs(target_directory, exist_ok=True)

        # ── ONCE PER PROCESS, NOT ONCE PER REQUEST ───────────────────────
        #
        # init_db() runs 28 CREATE TABLE, 23 CREATE INDEX, 27 ALTER TABLE and
        # six ensure_* schema modules. It is idempotent, so running it again
        # changes nothing - and it was running on EVERY construction, which is
        # every API call that touches a catalog.
        #
        # Measured on homeserver: 62 ms each. A location with 6,404 photographs
        # asks for 6,404 thumbnails, so that page spent 397 SECONDS re-checking
        # that the tables exist before serving a byte of image. With one worker
        # and eight threads it saturated the backend, the images crawled, and a
        # save queued behind them looked like the server was down.
        #
        # PRAGMA schema_version is the guard: SQLite bumps it on any DDL, so a
        # schema someone else changed is noticed and re-ensured, while the
        # ordinary case is one connection and one integer read.
        # One connection does both halves of this. A connection pays for the
        # schema parse on its first statement - about 1 ms against 28 tables
        # and 23 indexes - so opening two of them doubles the floor.
        conn = self._plain_connection()
        try:
            version = self._schema_version(conn)
            with _SCHEMA_ENSURED_LOCK:
                done = _SCHEMA_ENSURED.get(self.db_path)
            if version is None or done != version:
                conn.close()
                conn = None
                self.init_db()
                conn = self._plain_connection()
                after = self._schema_version(conn)
                with _SCHEMA_ENSURED_LOCK:
                    _SCHEMA_ENSURED[self.db_path] = after

            # Two things in init_db() are NOT schema work and so cannot hide
            # behind that memo: they have to catch rows the running
            # application inserts. They run every time, and they are written
            # to read before they write - see _seed_runtime_rows.
            #
            # PRAGMA data_version looks like the guard for this and is NOT:
            # it resets to 1 whenever a database is opened and only moves for
            # commits a connection did not make itself, so on the fresh
            # connection each request gets it is the same number forever.
            self._seed_runtime_rows(conn)
        finally:
            if conn is not None:
                conn.close()

    def _seed_runtime_rows(self, conn):
        """
        The rows the application needs present, re-checked on every open.

        Everything else init_db() does is keyed to the schema, so the memo in
        __init__ may skip it. These two are keyed to the DATA, and new rows
        arrive while the process runs:

          - the default people categories, which a user can delete and which
            every category picker expects to find;
          - a decorator for every deployment, because a card with none draws
            no artwork at all.

        Both are scans of tables with a handful of rows, and the work is READ
        FIRST: `INSERT OR IGNORE` still opens a write transaction and fsyncs
        the WAL even when it ignores every row, which measured 3.3 ms - and
        3.3 ms once per thumbnail is 21 seconds on a location holding 6,404
        photographs. So nothing here writes unless there is something to
        write. Contrast the five `rung` passes over 1.24 million
        alternative_versions rows, which rightly stay behind the memo.
        """
        from card_decorators import DEPLOYMENT_THEME, random_decorator
        cursor = conn.cursor()
        missing = [n for n in ('Family', 'Other') if not cursor.execute(
            'SELECT 1 FROM people_categories WHERE name = ?', (n,)).fetchone()]
        undecorated = cursor.execute(
            "SELECT id FROM album_deployments "
            "WHERE decorator IS NULL OR decorator = ''").fetchall()
        off_theme = cursor.execute(
            "SELECT id, decorator FROM album_deployments "
            "WHERE decorator NOT LIKE ? || '/%'",
            (DEPLOYMENT_THEME,)).fetchall()
        if not (missing or undecorated or off_theme):
            return                      # the whole point: no transaction

        for name in missing:
            cursor.execute('INSERT OR IGNORE INTO people_categories (name) '
                           'VALUES (?)', (name,))

        for (dep_id,) in undecorated:
            cursor.execute(
                'UPDATE album_deployments SET decorator = ? WHERE id = ?',
                (random_decorator(DEPLOYMENT_THEME), dep_id))
        # Picks made while deployments used another theme move to the
        # current one, keeping their number: the same card, not a re-roll.
        for (dep_id, old_pick) in off_theme:
            name = old_pick.split('/', 1)[-1]
            cursor.execute(
                'UPDATE album_deployments SET decorator = ? WHERE id = ?',
                (f'{DEPLOYMENT_THEME}/{name}', dep_id))
        conn.commit()

    def _plain_connection(self):
        """A connection with no row factory and no PRAGMAs: setup use only."""
        return sqlite3.connect(self.db_path, timeout=30)

    def _schema_version(self, conn):
        """
        SQLite's schema cookie, or None when it cannot be read.

        None means "do not trust the memo" - a database that is missing,
        locked or unreadable must fall through to init_db() rather than be
        skipped on the strength of a value we never got.
        """
        try:
            return conn.execute('PRAGMA schema_version').fetchone()[0]
        except sqlite3.Error:
            return None
    
    def get_connection(self):
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        # WAL is a PERSISTENT property of the database file, written into its
        # header the first time it is set. Asking for it again on an already-WAL
        # database changes nothing - but it is not free: it takes an exclusive
        # lock and fsyncs, measured at 2.26 ms, which was being paid by EVERY
        # connection, which is to say by every query the application makes.
        #
        # So set it once per process per file. The cost of being wrong is small
        # and self-correcting: a database somebody switched back to a rollback
        # journal keeps working, just without WAL's concurrency, until the next
        # restart.
        if self.db_path not in _WAL_SET:
            conn.execute('PRAGMA journal_mode=WAL')
            with _SCHEMA_ENSURED_LOCK:
                _WAL_SET.add(self.db_path)
        return conn
    
    def init_db(self):
        """Initialize catalog-specific tables"""
        conn = self.get_connection()
        cursor = conn.cursor()

        # Workflows table — each catalog owns its own workflow definitions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                source_directory TEXT,
                python_script TEXT NOT NULL,
                workflow_type TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute("PRAGMA table_info(workflows)")
        existing_wf_cols = {row[1] for row in cursor.fetchall()}
        if 'workflow_type' not in existing_wf_cols:
            cursor.execute("ALTER TABLE workflows ADD COLUMN workflow_type TEXT")

        # Which storage route an ingestion workflow reads from
        # (local-storage-spec.md section 6.4). NOTE THE DATABASE: `workflows`
        # lives here, in each catalog's own workflow_results.db, while
        # storage_routes lives in the application database - so this is a
        # reference ACROSS the two files and nothing can enforce it. A dangling
        # route id reads as "unconfigured" and refuses to run; it never crashes.
        _wf_cols = {r[1] for r in cursor.execute('PRAGMA table_info(workflows)')}
        if 'source_route_id' not in _wf_cols:
            cursor.execute('ALTER TABLE workflows ADD COLUMN source_route_id INTEGER')
        if 'source_subpath' not in _wf_cols:
            cursor.execute('ALTER TABLE workflows ADD COLUMN source_subpath TEXT')

        # Catalog properties: facts about THIS CATALOG'S DATA, as opposed to facts
        # about the machine it is running on.
        #
        # The split matters (file-ingestion-spec.md section 3.0). catalog_meta,
        # in the application database, holds where this machine reaches the
        # catalog - target_directory, source_root. Those are mount points and do
        # not survive a move. What lives HERE travels with the directory, so a
        # catalog folder carried to another machine and re-added still knows what
        # its own rows mean.
        #
        # 'ingest_mode' is the one that matters: it decides whether
        # file_contents.file_path is relative to the catalog or to the source
        # library. A directory that arrived without it would resolve its
        # originals against itself - not a 404, but potentially a different file.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS catalog_properties (
                key   TEXT PRIMARY KEY,
                value TEXT
            )
        ''')

        # Workflow executions table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS workflow_executions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                workflow_id INTEGER NOT NULL,
                workflow_name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP,
                total_files INTEGER DEFAULT 0,
                processed_files INTEGER DEFAULT 0,
                skipped_files INTEGER DEFAULT 0,
                -- The import's own breakdown, kept because the thing that can
                -- answer it does not last. These numbers are counts of
                -- import_items rows by state, and those rows are pruned by
                -- retention (import_schema.prune_old_items) - so an ingestion
                -- report computed live goes blank 30 days after the run it
                -- describes. Written once when the run ends, from the dict
                -- io_picture_import_workflow.summary() already builds.
                --
                -- NULL, not 0, for a run that finished before these existed:
                -- the API falls back to counting items for those, and zero
                -- would be indistinguishable from "it really imported nothing".
                new_files INTEGER,
                duplicate_files INTEGER,
                ignored_files INTEGER,
                error_message TEXT,
                -- Set to 1 when the user asks for a stop. The running subprocess
                -- reads its own row and breaks out of its loop at the next item
                -- boundary; see workflows-review-spec.md section 3.2.
                stop_requested INTEGER NOT NULL DEFAULT 0,
                -- The subprocess's pid, so a stop that is ignored can be
                -- signalled. Only meaningful while this backend process lives,
                -- and never signalled without the /proc cmdline check.
                pid INTEGER,
                -- Which machine started this. A pid means nothing without it:
                -- recovery cannot otherwise tell a process that died from one
                -- running on a host whose /proc it cannot read, and guessing
                -- buries live imports as failed. See
                -- workflow_scheduler.instance_id().
                owner_instance TEXT
            )
        ''')

        # The two stop columns arrived after this table did, so existing catalogs
        # get them by ALTER on the next start rather than by a migration script
        # someone has to remember to run.
        _exec_cols = {r[1] for r in cursor.execute('PRAGMA table_info(workflow_executions)')}
        if 'stop_requested' not in _exec_cols:
            cursor.execute('ALTER TABLE workflow_executions '
                           'ADD COLUMN stop_requested INTEGER NOT NULL DEFAULT 0')
        if 'pid' not in _exec_cols:
            cursor.execute('ALTER TABLE workflow_executions ADD COLUMN pid INTEGER')
        if 'owner_instance' not in _exec_cols:
            cursor.execute('ALTER TABLE workflow_executions ADD COLUMN owner_instance TEXT')
        # The import's breakdown. Nullable on purpose - see the CREATE above.
        for _col in ('new_files', 'duplicate_files', 'ignored_files'):
            if _col not in _exec_cols:
                cursor.execute(f'ALTER TABLE workflow_executions ADD COLUMN {_col} INTEGER')

        # File contents table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_contents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER,
                file_type TEXT,
                file_permissions TEXT,
                sha256_hash TEXT,
                file_id INTEGER,
                exif_data_id INTEGER,
                modification_time TEXT,
                access_time TEXT,
                change_time TEXT,
                creation_time TEXT,
                description TEXT,
                master_datetime TEXT,
                master_datetime_source TEXT,
                -- Video length in seconds. NULL for every photo, and for any
                -- video whose duration could not be read. REAL, not INTEGER:
                -- ffprobe reports fractional seconds and rounding a 0.6s clip
                -- to 0 would render as a broken file
                -- (video-page-spec.md section 4.1).
                --
                -- It lives HERE rather than on alternative_versions because a
                -- browser-playable video takes the passthrough branch and gets
                -- no alternative_versions row at all - there would be nothing
                -- to hang it on for the most common case.
                duration_seconds REAL,
                -- Which logical IMAGE this file belongs to (file-unity-spec.md).
                -- One photograph can arrive as several files - a camera writing
                -- a RAW and a JPG from the same shutter actuation - and those
                -- are one image, not two.
                --
                -- NULL means "this file is its own image", which is the state of
                -- every row until grouping actually runs, so adding the column
                -- changes nothing on its own.
                image_group_id INTEGER,
                -- Set when a user removes the file from the catalog
                -- (POST /api/catalogs/<id>/files/<id>/remove). The row stays so
                -- the importer's SHA-256 check still recognises the file and
                -- does not bring it back; everything that lists or processes
                -- files skips it. NULL for every file in the catalog.
                removed_at TIMESTAMP,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (exif_data_id) REFERENCES exif_data (id) ON DELETE SET NULL
            )
        ''')
        
        # File operations table for tracking all file operations
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                execution_id INTEGER,
                file_id INTEGER,
                operation_type TEXT NOT NULL,
                source_path TEXT,
                target_path TEXT,
                file_size INTEGER,
                status TEXT NOT NULL,
                details TEXT,
                operation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (execution_id) REFERENCES workflow_executions (id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES file_contents (id) ON DELETE SET NULL
            )
        ''')
        
        # EXIF data table for image metadata
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS exif_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                file_name TEXT NOT NULL,
                creation_date TEXT,
                latitude REAL,
                longitude REAL,
                altitude REAL,
                camera_make TEXT,
                camera_model TEXT,
                lens_model TEXT,
                focal_length TEXT,
                aperture TEXT,
                iso TEXT,
                shutter_speed TEXT,
                flash TEXT,
                orientation INTEGER,
                width INTEGER,
                height INTEGER,
                raw_exif_json TEXT,
                geohash TEXT,
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (file_id) REFERENCES file_contents (id) ON DELETE CASCADE
            )
        ''')

        # The raw exiftool dump, OUT of exif_data and keyed one-to-one to it.
        #
        # raw_exif_json averages 22 KB a row and 7.2 GB across a 361 K-file
        # catalog (contents-spec section 4), and inside exif_data every query
        # that reads whole rows pays for it whether it wants the blob or not.
        # Measured on homeserver's family catalog: reading exif_data.file_id alone
        # runs at 626,000 rows/s while COUNT(*) over the SAME rows exceeds
        # 360 s - identical row count, the only difference being whether the
        # read has to traverse the blob. It is also why CREATE INDEX on
        # exif_data(file_id) could not finish there, which is the whole reason
        # the location gallery had no index to use.
        #
        # Keyed on exif_data.id, NOT file_id: exif_data.file_id carries no
        # uniqueness constraint, so keying on it would invent one. id is the
        # one column that is already exactly one per row.
        #
        # Nothing is dropped from exif_data here. Readers COALESCE the two, so
        # a catalog whose rows have not been moved yet keeps working, and
        # migrate_split_raw_exif.py does the moving in its own window.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS exif_raw (
                exif_id       INTEGER PRIMARY KEY,
                raw_exif_json TEXT,
                FOREIGN KEY (exif_id) REFERENCES exif_data (id) ON DELETE CASCADE
            )
        ''')

        # People table for family members
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS people (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                date_of_birth TEXT,
                description TEXT,
                profile_photo_path TEXT,
                profile_photo_file_id INTEGER REFERENCES file_contents(id) ON DELETE SET NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # People photos table for yearly photos
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS people_photos (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                person_id INTEGER NOT NULL,
                year INTEGER NOT NULL,
                photo_path TEXT NOT NULL,
                photo_order INTEGER DEFAULT 1,
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (person_id) REFERENCES people (id) ON DELETE CASCADE
            )
        ''')
        
        # Face detections table for storing detected faces in images
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS face_detections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                w INTEGER NOT NULL,
                h INTEGER NOT NULL,
                confidence REAL,
                person_id INTEGER,
                source TEXT DEFAULT 'Automatic',
                image_width INTEGER,
                image_height INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (file_id) REFERENCES file_contents (id) ON DELETE CASCADE,
                FOREIGN KEY (person_id) REFERENCES people (id) ON DELETE SET NULL
            )
        ''')
        # Note: suggested_person_id and recognition_confidence were removed —
        # candidates are stored in face_similarity_scores instead.
        
        # Face contents table for storing extracted face images
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS face_contents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path TEXT NOT NULL,
                file_name TEXT NOT NULL,
                file_size INTEGER,
                sha256_hash TEXT,
                year INTEGER,
                month INTEGER,
                day INTEGER,
                person_id INTEGER NOT NULL,
                face_detection_id INTEGER NOT NULL,
                source_file_id INTEGER NOT NULL,
                embedding TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (person_id) REFERENCES people (id) ON DELETE CASCADE,
                FOREIGN KEY (face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
                FOREIGN KEY (source_file_id) REFERENCES file_contents (id) ON DELETE CASCADE
            )
        ''')
        
        # Create indexes for face_contents table
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_face_contents_person_id 
            ON face_contents(person_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_face_contents_face_detection_id 
            ON face_contents(face_detection_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_face_contents_source_file_id 
            ON face_contents(source_file_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_face_contents_date
            ON face_contents(year, month, day)
        ''')

        # ai_face_contents table — extracted face crops from Automatic detections
        # (separate from face_contents which requires a known person_id)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_face_contents (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                file_path         TEXT    NOT NULL,
                file_name         TEXT    NOT NULL,
                file_size         INTEGER,
                sha256_hash       TEXT,
                year              INTEGER,
                month             INTEGER,
                day               INTEGER,
                face_detection_id INTEGER NOT NULL UNIQUE,
                source_file_id    INTEGER NOT NULL,
                embedding         TEXT,
                created_at        TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (face_detection_id) REFERENCES face_detections (id) ON DELETE CASCADE,
                FOREIGN KEY (source_file_id)    REFERENCES file_contents    (id) ON DELETE CASCADE
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ai_face_contents_face_detection_id
            ON ai_face_contents (face_detection_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ai_face_contents_source_file_id
            ON ai_face_contents (source_file_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_ai_face_contents_date
            ON ai_face_contents (year, month, day)
        ''')

        # Alternative versions table for storing alternative file formats
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS alternative_versions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                original_file_id INTEGER NOT NULL,
                alternative_file_path TEXT NOT NULL,
                alternative_file_name TEXT NOT NULL,
                alternative_file_size INTEGER,
                characteristic TEXT NOT NULL,
                width INTEGER,
                height INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (original_file_id) REFERENCES file_contents (id) ON DELETE CASCADE
            )
        ''')
        

        # ── Which rung is this derivative? ────────────────────────────────────
        # `characteristic` cannot answer that. It is free text, and it does not
        # even describe size reliably: TYPE JPG spans 489..1801px on the long
        # edge, and the SCALE-1200 band spans 1200..2134, because max_width
        # caps WIDTH, not the long edge (file-sizes-spec.md section 2.2).
        #
        # Selection ranks on measured pixels (av.width), never on this column.
        # `rung` answers the different question that packing and the manifest
        # ask - "which rung IS this row" - which pixels cannot answer, because
        # the 2048 rung of a 1500px-wide original is 1500px wide.
        _av_cols = {r[1] for r in cursor.execute('PRAGMA table_info(alternative_versions)')}
        if 'rung' not in _av_cols:
            cursor.execute('ALTER TABLE alternative_versions ADD COLUMN rung TEXT')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_alt_versions_file_rung
            ON alternative_versions (original_file_id, rung)
        ''')

        # Label rows written before the column existed. Here rather than in a
        # script the user must remember to run: every query in file-sizes-spec
        # section 6 filters on `rung`, so an unlabelled row would simply vanish
        # from the application. Only NULLs are touched, so this is idempotent
        # and costs one indexed scan on every subsequent open.
        #
        # ORDER MATTERS. A video poster carries TYPE THUMBNAIL 320, exactly like
        # a photo's gallery thumbnail; only the file name separates them, so the
        # poster must be claimed before the generic 320 rule runs.
        cursor.execute(
            "UPDATE alternative_versions SET rung = 'thumb72' "
            "WHERE rung IS NULL AND UPPER(characteristic) = 'TYPE THUMBNAIL 72'")
        cursor.execute(
            "UPDATE alternative_versions SET rung = 'video_proxy' "
            "WHERE rung IS NULL AND UPPER(characteristic) LIKE 'VIDEO PROXY%'")
        cursor.execute(
            "UPDATE alternative_versions SET rung = 'video_poster' "
            "WHERE rung IS NULL AND UPPER(characteristic) LIKE '%THUMBNAIL 320%' "
            "  AND LOWER(alternative_file_name) LIKE '%poster.jpg'")
        cursor.execute(
            "UPDATE alternative_versions SET rung = 'thumb320' "
            "WHERE rung IS NULL AND UPPER(characteristic) LIKE '%THUMBNAIL 320%'")
        # Everything else is a display derivative, and before this change every
        # display derivative was the 1200px base - no other kind existed.
        cursor.execute(
            "UPDATE alternative_versions SET rung = '1200' WHERE rung IS NULL")

        # Events table for tracking events derived from catalog files
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                start_time TIMESTAMP NOT NULL,
                end_time TIMESTAMP NOT NULL,
                latitude REAL,
                longitude REAL,
                description TEXT,
                file_count INTEGER DEFAULT 0,
                type TEXT DEFAULT 'Auto',
                cover_file_id INTEGER,
                -- Where latitude/longitude came from: 'member' (a file in this
                -- event carried GPS), 'manual', or NULL for "nothing measured".
                -- Never the owner's home address - see location-spec.md 4.8.
                location_source TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # The photo shown for this event. No REFERENCES clause on purpose: this
        # application never enables PRAGMA foreign_keys, so ON DELETE SET NULL
        # would be inert and a deleted photo would leave a dangling id — the same
        # trap that orphaned 31 face_detections rows. Readers treat a cover whose
        # file has gone as "no cover" and re-resolve.
        # Added by ALTER so existing catalogs pick it up on the next start.
        _event_cols = {r[1] for r in cursor.execute('PRAGMA table_info(events)')}
        if 'cover_file_id' not in _event_cols:
            cursor.execute('ALTER TABLE events ADD COLUMN cover_file_id INTEGER')
        
        # Event files mapping table: links events to file_contents
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS event_files (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id INTEGER NOT NULL,
                file_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE CASCADE,
                FOREIGN KEY (file_id) REFERENCES file_contents (id) ON DELETE CASCADE,
                UNIQUE (event_id, file_id)
            )
        ''')

        # People categories table - user-defined categories for people
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS people_categories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Seeded by _seed_runtime_rows(), which runs on every construction.

        # Tags table - catalog-wide tag definitions
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Tag-person mapping table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS tag_person (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tag_id INTEGER NOT NULL,
                person_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (tag_id) REFERENCES tags (id) ON DELETE CASCADE,
                FOREIGN KEY (person_id) REFERENCES people (id) ON DELETE CASCADE,
                UNIQUE (tag_id, person_id)
            )
        ''')

        # Locations table - named places with coordinates
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS locations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                -- 'latitude'/'longitude', matching exif_data, events and
                -- file_locations. They were 'lat'/'long' here alone, and three
                -- endpoints plus LocationModel translated between the two
                -- spellings; nothing does now. See location-spec.md 9.3.
                latitude REAL,
                longitude REAL,
                description TEXT,
                source TEXT,
                map_image_path TEXT,
                -- 0 until a person has confirmed it, which they do by keeping
                -- or changing its name. The clustering workflow creates
                -- locations too, named from a geocoder guess, and an
                -- unconfirmed location is NOT offered when tagging an event -
                -- the same distinction events make with Auto and Manual.
                confirmed INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Location-event mapping table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS location_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                location_id INTEGER NOT NULL,
                event_id INTEGER NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (location_id) REFERENCES locations (id) ON DELETE CASCADE,
                FOREIGN KEY (event_id) REFERENCES events (id) ON DELETE CASCADE,
                UNIQUE (location_id, event_id)
            )
        ''')

        # Location clustering and resolution — one definition, shared by
        # models.py and both location workflows, each of which can be the first
        # thing to touch a catalog database. See location_schema.py for why
        # this is not inlined here (the same reason as the face schema below).
        ensure_location_schema(conn)

        # Face-recognition schema — one definition, shared by every path that
        # can be the first to touch a catalog database. See
        # face_recognition_schema.py for why this is not inlined here.
        ensure_face_recognition_schema(conn)

        # Pet recognition. Same shape and the same reason: four paths can each
        # be the first to touch a catalog database. This also adds people.kind
        # and people.species, and creates the `subject_appearances` view - so
        # it has to run AFTER the face schema, because the view reads
        # face_detections. See pet_schema.py and
        # documentation/specs/pet-recognition-spec.md section 3.
        ensure_pet_schema(conn)

        # Workflow queue. Created here, on every catalog open, rather than by a
        # migration script someone has to remember to run - see
        # documentation/features/CONTAINERIZATION_TRADEOFFS.md for what happened
        # the last time schema arrived only by hand.
        ensure_workflow_queue_schema(conn)

        # Migration: add geohash column to exif_data if not present
        try:
            cursor.execute('ALTER TABLE exif_data ADD COLUMN geohash TEXT')
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Migration: a location is confirmed or it is not.
        #
        # Backfilled to 1 for everything already there, and that is the whole
        # reason this runs here rather than defaulting: every location that
        # existed before this column was typed by a person through the Add
        # Location form. Defaulting them to 0 would present somebody's own work
        # back to them as a machine's guess awaiting approval.
        try:
            cursor.execute('ALTER TABLE locations ADD COLUMN confirmed '
                           'INTEGER NOT NULL DEFAULT 0')
            cursor.execute('UPDATE locations SET confirmed = 1')
            conn.commit()
        except Exception:
            pass  # Column already exists.

        # Migration: one spelling for a coordinate (location-spec.md 9.3).
        # RENAME COLUMN needs SQLite 3.25+; both the container (3.46) and the
        # development Macs (3.39) are well past it. Wrapped because the second
        # open finds the columns already renamed.
        try:
            cursor.execute('ALTER TABLE locations RENAME COLUMN lat TO latitude')
            conn.commit()
        except Exception:
            pass
        try:
            cursor.execute('ALTER TABLE locations RENAME COLUMN long TO longitude')
            conn.commit()
        except Exception:
            pass

        # Migration: say where an event's coordinate came from.
        #
        # 'member' when a member of the event carried GPS, 'manual' when a
        # person set it, NULL when nothing measured. 'home' is deliberately NOT
        # a value: ev_extract_events used to write the catalog owner's home
        # address into events.latitude when no member had GPS, into the same
        # column that otherwise holds a measurement and with nothing recording
        # which of the two a row held. That fallback is gone (location-spec.md
        # 4.8, 9.1); a label for it would only invite its return.
        try:
            cursor.execute('ALTER TABLE events ADD COLUMN location_source TEXT')
            conn.commit()

            # The column is new, so this is the first open after the upgrade and
            # the right moment to clean what the fallback already wrote. Runs
            # once, inside the ALTER's try block, for that reason.
            #
            # Both guards matter. An Auto event whose members all carry GPS at
            # the owner's own house is REAL DATA and must survive - in the
            # family catalog 73 events sit on one coordinate near home, every
            # one of them measured. And a Manual event is never touched: a
            # person may have typed those coordinates deliberately.
            cursor.execute('''
                UPDATE events SET latitude = NULL, longitude = NULL
                 WHERE type = 'Auto'
                   AND latitude IS NOT NULL
                   AND NOT EXISTS (
                       SELECT 1 FROM event_files ef
                         JOIN exif_data e ON e.file_id = ef.file_id
                        WHERE ef.event_id = events.id
                          AND e.latitude IS NOT NULL
                          AND e.longitude IS NOT NULL)
            ''')
            cleared = cursor.rowcount

            # Backfill provenance for the rows that remain.
            cursor.execute('''
                UPDATE events SET location_source = 'member'
                 WHERE latitude IS NOT NULL AND type = 'Auto'
            ''')
            cursor.execute('''
                UPDATE events SET location_source = 'manual'
                 WHERE latitude IS NOT NULL AND type = 'Manual'
            ''')
            conn.commit()
            if cleared:
                print(f"[models] Cleared invented coordinates from {cleared} event(s) "
                      f"with no GPS-bearing member (location-spec.md 9.1)")
        except Exception:
            pass  # Column already exists; the clean-up has already run.

        # Migration: add category column to people if it doesn't exist yet
        try:
            cursor.execute("ALTER TABLE people ADD COLUMN category TEXT DEFAULT 'Other'")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Migration: add master_datetime columns to file_contents if not present
        try:
            cursor.execute('ALTER TABLE file_contents ADD COLUMN master_datetime TEXT')
            conn.commit()
        except Exception:
            pass  # Column already exists
        try:
            cursor.execute('ALTER TABLE file_contents ADD COLUMN master_datetime_source TEXT')
            conn.commit()
        except Exception:
            pass  # Column already exists
        try:
            cursor.execute('ALTER TABLE file_contents ADD COLUMN duration_seconds REAL')
            conn.commit()
        except Exception:
            pass  # Column already exists
        try:
            cursor.execute('ALTER TABLE file_contents ADD COLUMN image_group_id INTEGER')
            conn.commit()
        except Exception:
            pass  # Column already exists
        try:
            cursor.execute('ALTER TABLE file_contents ADD COLUMN removed_at TIMESTAMP')
            conn.commit()
        except Exception:
            pass  # Column already exists
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_file_contents_image_group
            ON file_contents (image_group_id)
        ''')

        # ── Image groups ──────────────────────────────────────────────────────
        # One row per logical image that is represented by MORE THAN ONE file.
        # See documentation/specs/file-unity-spec.md.
        #
        # A group is a LINK, not a merge: both files keep their file_contents
        # row, their id and their URLs. What the group adds is (a) which of them
        # listings should show, and (b) which of them owns the derivatives, so a
        # RAW+JPG pair is rendered once instead of twice.
        #
        # No REFERENCES clause: this application never enables
        # PRAGMA foreign_keys, so it would be inert. Deleting a file clears its
        # membership explicitly and dissolves a group left with one member.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS image_groups (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                -- file_contents.id of the member that listings show and that
                -- owns the single set of derivatives.
                primary_file_id INTEGER NOT NULL,
                -- 'raw_jpeg', or 'live_photo' (a still and its motion clip,
                -- the still primary; file-unity-spec.md section 12).
                kind            TEXT NOT NULL DEFAULT 'raw_jpeg',
                -- Which rule matched, and the values it matched on. Kept so a
                -- wrong merge can be explained rather than guessed at.
                match_rule      TEXT,
                match_evidence  TEXT,
                created_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_image_groups_primary
            ON image_groups (primary_file_id)
        ''')
        conn.commit()

        # A batch of photo date changes from the event date editor
        # (mass-edit-event-photo-dates-spec.md section 5.3). The work runs in
        # the background, so this row is how the event page follows it - and
        # how it finds where the photos went once the event it was opened on
        # has been rebuilt away.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS date_change_batches (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                event_id     INTEGER,
                -- running | rebuilding | completed | interrupted | failed
                status       TEXT NOT NULL,
                file_ids     TEXT NOT NULL,
                total        INTEGER NOT NULL,
                done         INTEGER NOT NULL DEFAULT 0,
                result_json  TEXT,
                queue_id     INTEGER,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                finished_at  TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_date_batches_event
            ON date_change_batches (event_id, id)
        ''')
        conn.commit()

        # Index on master_datetime for fast chronological queries
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fc_master_datetime
            ON file_contents (master_datetime)
        ''')

        # ── The Content report's four slow aggregates ─────────────────────────
        # contents-spec.md section 6 sets a cost ceiling for that page, and
        # without these it cannot be met: measured on family's row counts
        # (1.19 M alternative_versions, 1.35 M file_operations, 361 K files,
        # 2026-10-06) the four queries below cost 4.47 s together and 0.70 s with
        # these indexes - six times faster, for 4.7 s of one-off index build.
        #
        # Each one removes a specific cost the query plan named:
        #
        #   (rung, original_file_id)  the existing index is (original_file_id,
        #                             rung), which covers the columns but leaves
        #                             GROUP BY rung sorting in a temp B-tree
        #   (latitude) partial        COUNT(*) WHERE latitude IS NOT NULL was a
        #                             full scan of exif_data - whose rows carry
        #                             raw_exif_json, so scanning it reads 7.2 GB
        #                             to count 341 K values. Partial, so the
        #                             index holds only the rows being counted
        #   (operation_type, status)  a 1.35 M-row scan plus a temp B-tree
        #   (sha256_hash, removed_at) the duplicate check, made covering: the
        #                             removed_at filter was a row lookup per key
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_alt_rung_file '
                       'ON alternative_versions (rung, original_file_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_exif_latitude '
                       'ON exif_data (latitude) WHERE latitude IS NOT NULL')
        # exif_data.file_id IS NOT INDEXED HERE, AND THAT IS DELIBERATE.
        #
        # It needs the index as much as the three below - it is the column the
        # table is joined on in 16 places in app.py and in most of the
        # workflows, and without it
        #
        #     LEFT JOIN exif_data ed ON fc.id = ed.file_id
        #
        # plans as `SCAN ed LEFT-JOIN`: a full scan of exif_data PER ROW of the
        # outer query. That is what made a location with 9,753 photographs
        # never finish loading.
        #
        # But BUILDING it cannot happen in a request. exif_data carries
        # raw_exif_json, so its rows are mostly blob in overflow pages, and
        # walking all of them to collect one integer column took over an hour
        # on the family catalog (27.8 GB) without finishing - against 3.6 s on
        # the 2.3 GB second-catalog catalog, which is why a small catalog gives
        # no warning of this. Measured: reading file_id alone runs at 626,000
        # rows/s, while COUNT(*) over the same rows exceeds 360 s. The blob is
        # the entire difference.
        #
        # Creating it here would therefore stall the FIRST API call after an
        # upgrade for an unbounded time while holding a write lock, and with
        # eight gunicorn threads the other seven would raise "database is
        # locked" after busy_timeout. A backend that cannot start is worse than
        # a gallery that is slow.
        #
        # So it belongs to migrate_add_file_id_indexes.py, run deliberately
        # with the backend stopped. The lasting fix is to move raw_exif_json
        # into its own table so that exif_data is narrow and this index is
        # cheap to build; until then, this comment is the index's whereabouts.
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_face_det_file '
                       'ON face_detections (file_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_event_files_file '
                       'ON event_files (file_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_file_locations_file '
                       'ON file_locations (file_id)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_fo_type_status '
                       'ON file_operations (operation_type, status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_fc_sha_removed '
                       'ON file_contents (sha256_hash, removed_at)')
        conn.commit()

        # The import's duplicate check is `WHERE sha256_hash = ?`, once per
        # source file (io_picture_import_workflow.py). Without this it is a full
        # scan of file_contents per file, so importing N files into a catalog of
        # M costs N*M row reads - the cost grows with the library, which is the
        # wrong shape for the one check every import runs.
        #
        # Not UNIQUE. The column is nullable, a hash is best-effort (the
        # workflow warns and carries on when it cannot read a file), and two
        # rows legitimately share a hash while a RAW+JPG group is being written.
        # Uniqueness is enforced by the check, not by the schema.
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fc_sha256
            ON file_contents (sha256_hash)
        ''')

        # Backfill master_datetime for existing rows that have NULL values.
        # Uses best available source: EXIF creation_date → modification_time → added_at.
        # Note: epoch/invalid years (1970, 2000, 0000) are skipped to the next tier.
        # SQLite correlated subqueries cannot alias the outer table, so we use
        # scalar subqueries that reference the outer row's id directly.
        #
        # EXIF format is 'YYYY:MM:DD HH:MM:SS' — we must only replace the date
        # colons, not the time colons, so we use SUBSTR to reconstruct the value:
        #   SUBSTR(cd,1,4)||'-'||SUBSTR(cd,6,2)||'-'||SUBSTR(cd,9,2)||'T'||SUBSTR(cd,12,8)||'Z'
        cursor.execute('''
            UPDATE file_contents
            SET
                master_datetime = COALESCE(
                    -- Tier 1: valid EXIF date → ISO 8601 (YYYY-MM-DDTHH:MM:SSZ)
                    CASE
                        WHEN (SELECT creation_date FROM exif_data
                              WHERE file_id = file_contents.id LIMIT 1) IS NOT NULL
                         AND LENGTH((SELECT creation_date FROM exif_data
                                     WHERE file_id = file_contents.id LIMIT 1)) >= 19
                         AND SUBSTR((SELECT creation_date FROM exif_data
                                     WHERE file_id = file_contents.id LIMIT 1), 1, 4)
                             NOT IN ('1970', '2000', '0000')
                        THEN
                            SUBSTR((SELECT creation_date FROM exif_data WHERE file_id = file_contents.id LIMIT 1), 1, 4)
                            || '-'
                            || SUBSTR((SELECT creation_date FROM exif_data WHERE file_id = file_contents.id LIMIT 1), 6, 2)
                            || '-'
                            || SUBSTR((SELECT creation_date FROM exif_data WHERE file_id = file_contents.id LIMIT 1), 9, 2)
                            || 'T'
                            || SUBSTR((SELECT creation_date FROM exif_data WHERE file_id = file_contents.id LIMIT 1), 12, 8)
                            || 'Z'
                    END,
                    -- Tier 2: filesystem modification_time (already ISO-like 'YYYY-MM-DD HH:MM:SS')
                    CASE
                        WHEN modification_time IS NOT NULL
                        THEN REPLACE(modification_time, ' ', 'T') || 'Z'
                    END,
                    -- Tier 3: ingestion timestamp
                    REPLACE(added_at, ' ', 'T') || 'Z'
                ),
                master_datetime_source = CASE
                    WHEN (SELECT creation_date FROM exif_data
                          WHERE file_id = file_contents.id LIMIT 1) IS NOT NULL
                     AND LENGTH((SELECT creation_date FROM exif_data
                                 WHERE file_id = file_contents.id LIMIT 1)) >= 19
                     AND SUBSTR((SELECT creation_date FROM exif_data
                                 WHERE file_id = file_contents.id LIMIT 1), 1, 4)
                         NOT IN ('1970', '2000', '0000')
                    THEN 'exif'
                    WHEN modification_time IS NOT NULL THEN 'filesystem'
                    ELSE 'system'
                END
            WHERE master_datetime IS NULL
        ''')
        conn.commit()

        # Albums table — named, curated photo collections
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS albums (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                name        TEXT    NOT NULL,
                description TEXT,
                mode        TEXT    NOT NULL DEFAULT 'words',
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        try:
            cursor.execute("ALTER TABLE albums ADD COLUMN mode TEXT NOT NULL DEFAULT 'words'")
            conn.commit()
        except Exception:
            pass  # Column already exists

        # Album filters — ordered sequence of NL→SQL filter steps
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS album_filters (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id     INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                position     INTEGER NOT NULL,
                nl_text      TEXT    NOT NULL,
                sql_fragment TEXT    NOT NULL,
                photo_count  INTEGER,
                created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (album_id, position)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_album_filters_album_id
            ON album_filters(album_id)
        ''')

        # Album sections — named subdivisions of an album (1-N per album)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS album_sections (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id    INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                name        TEXT    NOT NULL,
                position    INTEGER NOT NULL DEFAULT 0,
                created_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (album_id, position)
            )
        ''')
        try:
            cursor.execute(
                "ALTER TABLE album_sections ADD COLUMN source_event_id INTEGER REFERENCES events(id) ON DELETE SET NULL"
            )
            conn.commit()
        except Exception:
            pass  # Column already exists
        try:
            cursor.execute("ALTER TABLE album_sections ADD COLUMN description TEXT")
            conn.commit()
        except Exception:
            pass  # Column already exists
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_album_sections_album_id
            ON album_sections(album_id)
        ''')

        # Section photos — photos explicitly assigned to a section
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS section_photos (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                section_id  INTEGER NOT NULL REFERENCES album_sections(id) ON DELETE CASCADE,
                file_id     INTEGER NOT NULL,
                position    INTEGER NOT NULL DEFAULT 0,
                added_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE (section_id, file_id)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_section_photos_section_id
            ON section_photos(section_id)
        ''')

        # ── Album categories ──────────────────────────────────────────────────
        # A hierarchical grouping of sections inside one album
        # (album-gallery-enhancement-spec.md section 4).
        #
        # The ROOT IS NOT A ROW. parent_id NULL means "directly under the album",
        # and the root's label is the album's own name. Three things follow, and
        # all three are the reason it is done this way: renaming the album
        # renames the root for free, the root cannot be deleted, and every
        # section that existed before this table reads as root-level with no
        # backfill (spec section 3.1).
        #
        # The REFERENCES clauses are documentation. This application never
        # enables PRAGMA foreign_keys, so ON DELETE CASCADE will not run -
        # deleting a category removes its subtree in app.py, explicitly.
        #
        # There is deliberately NO UNIQUE(album_id, parent_id, position): SQLite
        # treats NULLs as distinct for uniqueness, so such a constraint would
        # hold everywhere EXCEPT the root - the one level every album has. A
        # constraint that protects half a tree reads as a guarantee and is not
        # one (spec section 2.6). Ordering uses ORDER BY position, id instead.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS album_categories (
                id         INTEGER PRIMARY KEY AUTOINCREMENT,
                album_id   INTEGER NOT NULL REFERENCES albums(id) ON DELETE CASCADE,
                parent_id  INTEGER REFERENCES album_categories(id) ON DELETE CASCADE,
                name       TEXT    NOT NULL,
                position   INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_album_categories_album
            ON album_categories(album_id)
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_album_categories_parent
            ON album_categories(parent_id)
        ''')

        # Which category a section sits in, and which photo fronts its card.
        # Both by ALTER on the next start rather than by a migration script
        # someone has to remember to run - the same reasoning as the two stop
        # columns above. NULL category_id means the root, which is where every
        # pre-existing section already is.
        #
        # cover_file_id has no REFERENCES clause for the same reason
        # events.cover_file_id has none: it would be inert, and an inert
        # constraint reads as a promise. A dangling id is treated as "no cover"
        # and the next candidate is used (spec section 4.5).
        _sec_cols = {r[1] for r in cursor.execute('PRAGMA table_info(album_sections)')}
        if 'category_id' not in _sec_cols:
            try:
                cursor.execute('ALTER TABLE album_sections ADD COLUMN category_id INTEGER '
                               'REFERENCES album_categories(id) ON DELETE SET NULL')
            except Exception as e:
                print(f'  ! could not add album_sections.category_id: {e}')
        if 'cover_file_id' not in _sec_cols:
            try:
                cursor.execute('ALTER TABLE album_sections ADD COLUMN cover_file_id INTEGER')
            except Exception as e:
                print(f'  ! could not add album_sections.cover_file_id: {e}')
        conn.commit()

        # ── Album deployments ─────────────────────────────────────────────────
        # A deployment is a named, shareable gallery holding one or more albums
        # (albums-enhancement-spec.md section 3).
        #
        # NOTE THE DATABASE. These live HERE, in the catalog's own
        # workflow_results.db, beside `albums` - not in the application database
        # with catalog_meta. A deployment belongs to exactly one catalog, which
        # buys three things at once:
        #
        #   * album_id is a reference inside one file, so "which albums are in
        #     this deployment" is one join rather than the cross-file scan
        #     storage_routes needs (local-storage-spec.md section 6.4);
        #   * the catalog directory stays self-contained - carry the folder to
        #     another machine and its deployments travel with it, which is the
        #     property file-ingestion-spec.md section 3.0 establishes;
        #   * the deployment slug only has to be unique HERE. The URL is the
        #     pair <catalog-slug>/<deployment-slug>, and the catalog half is
        #     unique in catalog_meta, so the pair is unique by two ordinary
        #     constraints with nothing to scan.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS album_deployments (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                slug          TEXT NOT NULL UNIQUE,
                name          TEXT NOT NULL,
                description   TEXT,
                -- The same hash format auth.py writes for user passwords. NULL
                -- means the deployment is public. Never leaves this database
                -- except into .auth.json, which is never served.
                password_hash TEXT,
                enabled       INTEGER NOT NULL DEFAULT 1,
                -- users.id, from the APPLICATION database. A record of who made
                -- it, never read to make a decision, so the split costs nothing.
                created_by    INTEGER,
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Which derivative sizes this deployment packs into a bundle, as a JSON
        # list of rung names (documentation/specs/file-sizes-spec.md section 9.2).
        #
        # The DEFAULT is not '[]'. An empty default would silently strip square
        # thumbnails from every existing deployment on its next publish, and a
        # bundle is a one-way copy: nobody at the destination can add them back.
        # This default reproduces today's bundle minus the 72px filmstrip
        # thumbnail, which no gallery would ever show (spec 4.11).
        _dep_cols = {r[1] for r in cursor.execute('PRAGMA table_info(album_deployments)')}
        if 'packed_rungs' not in _dep_cols:
            cursor.execute("ALTER TABLE album_deployments ADD COLUMN "
                           "packed_rungs TEXT NOT NULL DEFAULT '[\"1200\",\"thumb320\"]'")

        # The artwork across the top of the deployment's card, picked at random
        # from card_decorators.DEPLOYMENT_THEME and then kept. Deployments made
        # before this get theirs here, once.
        if 'decorator' not in _dep_cols:
            cursor.execute('ALTER TABLE album_deployments ADD COLUMN decorator TEXT')
        # Filled in by _seed_runtime_rows(), which runs on every construction.

        cursor.execute('''
            CREATE TABLE IF NOT EXISTS album_deployment_albums (
                deployment_id INTEGER NOT NULL,
                album_id      INTEGER NOT NULL,
                position      INTEGER NOT NULL DEFAULT 0,
                added_at      TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (deployment_id, album_id)
            )
        ''')
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_deployment_albums_album
            ON album_deployment_albums(album_id)
        ''')

        # Album transfers over magic-wormhole
        # (documentation/specs/wormhole-album-transfer-spec.md section 5).
        # Here, in the catalog's own database, for the same reason deployments
        # are: a transfer belongs to exactly one catalog, and the catalog
        # directory stays self-contained.
        import wormhole_transfer
        wormhole_transfer.ensure_schema(cursor)

        # The phased picture import's work items and per-phase progress
        # (documentation/features/parallelism.md section 4).
        from workflows.lib.import_schema import ensure_import_schema
        ensure_import_schema(cursor)

        # A photo's turn or flip, never written to the original
        # (documentation/specs/basic-photo-editing.md section 4).
        import photo_edits
        photo_edits.ensure_schema(conn)

        # Publishing a deployment to a server: its servers and runs
        # (documentation/specs/deployment-publish-to-server-spec.md section 5).
        import publish
        publish.ensure_schema(conn)

        conn.commit()
        conn.close()

class WorkflowModel:
    """Catalog-scoped workflow model — operates on a CatalogDatabase."""
    def __init__(self, catalog_db: 'CatalogDatabase'):
        self.catalog_db = catalog_db

    def create(self, name: str, description: str, source_directory: str,
               python_script: str, workflow_type: str = None,
               source_route_id: int = None, source_subpath: str = None) -> int:
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workflows (name, description, source_directory,
                                   python_script, workflow_type,
                                   source_route_id, source_subpath)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (name, description, source_directory, python_script, workflow_type,
              source_route_id, source_subpath))
        workflow_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return workflow_id

    def get_all(self) -> List[Dict]:
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM workflows ORDER BY created_at DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_by_id(self, workflow_id: int) -> Optional[Dict]:
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM workflows WHERE id = ?', (workflow_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def update(self, workflow_id: int, **kwargs):
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        fields = []
        values = []
        for key, value in kwargs.items():
            if key in ['name', 'description', 'source_directory',
                       'python_script', 'workflow_type',
                       'source_route_id', 'source_subpath']:
                fields.append(f"{key} = ?")
                values.append(value)
        if fields:
            values.append(workflow_id)
            query = f"UPDATE workflows SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            cursor.execute(query, values)
            conn.commit()
        conn.close()

    def delete(self, workflow_id: int):
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM workflows WHERE id = ?', (workflow_id,))
        conn.commit()
        conn.close()

# ── Catalog properties ────────────────────────────────────────────────────────
# Free functions rather than a model class: these are read during path
# resolution, before any model exists, and they must work from a bare path.

# Absent means 'copy'. That is what keeps every catalog that predates this
# feature behaving exactly as it does today (spec section 7).
DEFAULT_INGEST_MODE = 'copy'
INGEST_MODES = ('copy', 'reference', 'move')


def get_catalog_property(catalog_db_path: str, key: str, default=None):
    """
    One property from a catalog directory's own database.

    Never raises: this is called while deciding how to resolve a path, and a
    catalog whose database is momentarily locked must degrade to the default
    rather than take the request down with it.
    """
    try:
        conn = sqlite3.connect(catalog_db_path, timeout=10)
        try:
            row = conn.execute(
                'SELECT value FROM catalog_properties WHERE key = ?', (key,)).fetchone()
            return row[0] if row else default
        finally:
            conn.close()
    except sqlite3.Error:
        return default


def set_catalog_property(catalog_db_path: str, key: str, value: str):
    """Write one property into a catalog directory's own database."""
    conn = sqlite3.connect(catalog_db_path, timeout=30)
    try:
        conn.execute('CREATE TABLE IF NOT EXISTS catalog_properties '
                     '(key TEXT PRIMARY KEY, value TEXT)')
        conn.execute('INSERT INTO catalog_properties (key, value) VALUES (?, ?) '
                     'ON CONFLICT(key) DO UPDATE SET value = excluded.value',
                     (key, value))
        conn.commit()
    finally:
        conn.close()


def read_ingest_mode(target_directory: str) -> str:
    """
    The ingest mode recorded in a catalog DIRECTORY, or 'copy'.

    This is the value that travels. It is read when a directory is registered as
    a catalog, to seed catalog_meta - after that the registry copy is what the
    request path reads (spec section 3.1).
    """
    if not target_directory:
        return DEFAULT_INGEST_MODE
    db_path = os.path.join(target_directory, 'workflow_results.db')
    if not os.path.exists(db_path):
        return DEFAULT_INGEST_MODE
    mode = get_catalog_property(db_path, 'ingest_mode', DEFAULT_INGEST_MODE)
    return mode if mode in INGEST_MODES else DEFAULT_INGEST_MODE


def write_ingest_mode(target_directory: str, mode: str):
    """Record the mode with the data, once, at catalog creation."""
    if mode not in INGEST_MODES:
        raise ValueError(f'unknown ingest_mode {mode!r}')
    set_catalog_property(
        os.path.join(target_directory, 'workflow_results.db'), 'ingest_mode', mode)


class ExecutionModel:
    """Model for workflow executions in catalog-specific databases"""
    def __init__(self, catalog_db: CatalogDatabase):
        self.catalog_db = catalog_db
    
    def create(self, workflow_id: int, workflow_name: str,
               owner_instance: str = None) -> int:
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workflow_executions
                (workflow_id, workflow_name, status, owner_instance)
            VALUES (?, ?, 'running', ?)
        ''', (workflow_id, workflow_name, owner_instance))
        execution_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return execution_id
    
    def update_progress(self, execution_id: int, processed_files: int,
                       total_files: int, skipped_files: int = 0):
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE workflow_executions
            SET processed_files = ?, total_files = ?, skipped_files = ?
            WHERE id = ?
        ''', (processed_files, total_files, skipped_files, execution_id))
        conn.commit()
        conn.close()
    
    def complete(self, execution_id: int, status: str = 'completed',
                error_message: Optional[str] = None, skipped_files: int = 0,
                new_files: Optional[int] = None,
                duplicate_files: Optional[int] = None,
                ignored_files: Optional[int] = None):
        """
        Put the run into its terminal state, with the import's breakdown when
        the script returned one.

        The three optional counts are left ALONE when None rather than written
        as 0: most workflows are not imports and have nothing to say about
        files, and a stopped import that never reached a summary should keep
        whatever its last progress update recorded.
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        sets = ['status = ?', 'completed_at = CURRENT_TIMESTAMP',
                'error_message = ?', 'skipped_files = ?']
        args = [status, error_message, skipped_files]
        for column, value in (('new_files', new_files),
                              ('duplicate_files', duplicate_files),
                              ('ignored_files', ignored_files)):
            if value is not None:
                sets.append(f'{column} = ?')
                args.append(value)
        args.append(execution_id)
        cursor.execute(f'UPDATE workflow_executions SET {", ".join(sets)} WHERE id = ?',
                       args)
        conn.commit()
        conn.close()
    
    def set_pid(self, execution_id: int, pid: int):
        """Record the subprocess pid so a stop can be escalated to a signal."""
        conn = self.catalog_db.get_connection()
        conn.execute('UPDATE workflow_executions SET pid = ? WHERE id = ?',
                     (pid, execution_id))
        conn.commit()
        conn.close()

    def request_stop(self, execution_id: int) -> Optional[Dict]:
        """
        Ask a running execution to stop. Returns the row, or None if unknown.

        Only sets the flag - the running script notices it at its next item
        boundary. Nothing is killed here; escalation is a separate, timed step
        (workflows-review-spec.md section 3.3).
        """
        conn = self.catalog_db.get_connection()
        try:
            row = conn.execute('SELECT * FROM workflow_executions WHERE id = ?',
                               (execution_id,)).fetchone()
            if row is None:
                return None
            if row['status'] == 'running':
                conn.execute('UPDATE workflow_executions SET stop_requested = 1 WHERE id = ?',
                             (execution_id,))
                conn.commit()
            return dict(row)
        finally:
            conn.close()

    def force_stopped(self, execution_id: int, message: Optional[str] = None) -> bool:
        """
        Mark a still-running execution as stopped.

        The backstop for a script that never reached its check - and the reason
        the update is conditional on 'running': a script that stopped cleanly a
        moment earlier has already written its own row, and overwriting it would
        replace an accurate record with this one.
        """
        conn = self.catalog_db.get_connection()
        try:
            cur = conn.execute(
                "UPDATE workflow_executions SET status = 'stopped', "
                'completed_at = CURRENT_TIMESTAMP, error_message = ? '
                "WHERE id = ? AND status = 'running'", (message, execution_id))
            conn.commit()
            return cur.rowcount > 0
        finally:
            conn.close()

    def get_by_workflow(self, workflow_id: int) -> List[Dict]:
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM workflow_executions
            WHERE workflow_id = ?
            ORDER BY started_at DESC
        ''', (workflow_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_all(self) -> List[Dict]:
        """Get all executions in this catalog"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM workflow_executions
            ORDER BY started_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    # The scripts that ingest files. Named rather than pattern-matched: a
    # workflow called "Import People" would match a LIKE '%import%' and is not
    # an ingestion.
    INGESTION_SCRIPTS = ('io_picture_import_workflow.py',
                         'io_picture_import_v2_workflow.py')

    def get_ingestions(self, limit: int = 24, offset: int = 0) -> Dict:
        """
        Ingestion runs, newest first, with the four counts the report shows.

        {'total': n, 'executions': [...]}  - total is every ingestion, so the
        caller can page without asking twice.

        Each count comes from the columns the run recorded when it finished.
        Where those are NULL - a run from before they existed, or one stopped
        before it could summarise - they are counted from import_items instead,
        and `counts_from` says which happened. That fallback is best effort by
        nature: retention prunes those rows (import_schema.prune_old_items), so
        an old stopped run can legitimately report zeros.
        """
        conn = self.catalog_db.get_connection()
        try:
            # Which workflows ingest, decided in Python on the BASENAME.
            # python_script is stored as a path - 'workflows/io_picture_
            # import_v2_workflow.py' - so comparing the column to a bare script
            # name matches nothing, and a LIKE '%name' would also match
            # 'my_io_picture_import_workflow.py'. There are a handful of
            # workflows per catalog, so reading them costs nothing.
            ids = [r['id'] for r in conn.execute(
                'SELECT id, python_script FROM workflows')
                if os.path.basename(r['python_script'] or '') in self.INGESTION_SCRIPTS]
            if not ids:
                return {'total': 0, 'executions': []}

            marks = ','.join('?' * len(ids))
            total = conn.execute(
                f'SELECT COUNT(*) FROM workflow_executions '
                f'WHERE workflow_id IN ({marks})', ids).fetchone()[0]
            rows = [dict(r) for r in conn.execute(
                f"""SELECT * FROM workflow_executions
                     WHERE workflow_id IN ({marks})
                     ORDER BY started_at DESC, id DESC
                     LIMIT ? OFFSET ?""", (*ids, limit, offset)).fetchall()]

            for row in rows:
                recorded = row.get('new_files') is not None
                row['counts_from'] = 'recorded' if recorded else 'items'
                if recorded:
                    row['files_new'] = row['new_files'] or 0
                    row['files_duplicate'] = row['duplicate_files'] or 0
                    row['files_ignored'] = row['ignored_files'] or 0
                    # Analysed is every file the run looked at, which is the
                    # three outcomes plus whatever failed or never finished.
                    row['files_analysed'] = max(
                        row.get('total_files') or 0,
                        row['files_new'] + row['files_duplicate'] + row['files_ignored'])
                else:
                    row.update(self._counts_from_items(conn, row['id']))
            return {'total': total, 'executions': rows}
        finally:
            conn.close()

    # The item states behind each number, in the import's own vocabulary
    # (io_picture_import_workflow.summary): placed states are new, duplicate
    # and already_imported are content the catalog already had, and ignored is
    # not media at all - a sidecar or an operating-system file.
    _NEW_STATES = ('placed', 'rendered', 'render_skipped', 'render_failed')
    _DUPLICATE_STATES = ('duplicate', 'already_imported')
    _IGNORED_STATES = ('ignored',)

    def _counts_from_items(self, conn, execution_id: int) -> Dict:
        counts = {}
        try:
            counts = {r[0]: r[1] for r in conn.execute(
                'SELECT state, COUNT(*) FROM import_items WHERE execution_id = ? '
                'GROUP BY state', (execution_id,))}
        except Exception:
            pass          # a catalog with no import schema yet
        total = sum(counts.values())

        def n(states):
            return sum(counts.get(state, 0) for state in states)

        return {'files_new': n(self._NEW_STATES),
                'files_duplicate': n(self._DUPLICATE_STATES),
                'files_ignored': n(self._IGNORED_STATES),
                'files_analysed': total}

    def get_by_id(self, execution_id: int) -> Optional[Dict]:
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM workflow_executions WHERE id = ?',
                      (execution_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None


class CatalogModel:
    """
    Catalog rows, read through the registry database.

    WHAT IS NOT HERE, AND WHY. This class once mirrored `catalog.Catalog`
    (create / update / delete / get_all) and carried its own file and EXIF
    writers as well. None of it was ever called: `app.py` does not import
    CatalogModel at all, the application's catalog CRUD lives in `catalog.py`,
    and the file and EXIF rows are written by the import workflow's own SQL.
    Eleven unreachable methods were deleted rather than maintained.

    What is left is the three reads that have a caller, and the one helper they
    share. Before adding anything back here, check whether `catalog.Catalog` is
    the thing you actually want - it almost certainly is.
    """

    def __init__(self, db: Database):
        self.db = db
    
    def get_catalog_db(self, catalog_id: int) -> Optional[CatalogDatabase]:
        """Get catalog-specific database instance"""
        catalog = self.get_by_id(catalog_id)
        if not catalog:
            return None
        return CatalogDatabase(catalog['target_directory'])
    
    def get_by_id(self, catalog_id: int) -> Optional[Dict]:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM catalog_meta WHERE id = ?', (catalog_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def get_files(self, catalog_id: int) -> List[Dict]:
        """Get all files from catalog-specific database with EXIF data"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            return []
        
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                fc.id,
                fc.file_path,
                fc.file_name,
                fc.file_size,
                fc.file_type,
                fc.file_permissions,
                fc.sha256_hash,
                fc.exif_data_id,
                fc.modification_time,
                fc.access_time,
                fc.change_time,
                fc.creation_time,
                fc.added_at,
                ed.creation_date,
                ed.latitude,
                ed.longitude,
                ed.altitude,
                ed.camera_make,
                ed.camera_model,
                ed.lens_model,
                ed.focal_length,
                ed.aperture,
                ed.iso,
                ed.shutter_speed,
                ed.flash,
                ed.orientation,
                ed.width,
                ed.height,
                -- exif_raw first, the old column as a fallback for rows
                -- migrate_split_raw_exif.py has not moved yet.
                COALESCE(er.raw_exif_json, ed.raw_exif_json) AS raw_exif_json
            FROM file_contents fc
            LEFT JOIN exif_data ed ON fc.id = ed.file_id
            LEFT JOIN exif_raw  er ON er.exif_id = ed.id
            WHERE fc.removed_at IS NULL
            ORDER BY fc.added_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        
        # Convert rows to dictionaries and parse EXIF data
        files = []
        for row in rows:
            file_dict = dict(row)
            
            # Build exif_data object from the joined columns
            exif_data = {}
            exif_fields = [
                'creation_date', 'latitude', 'longitude', 'altitude',
                'camera_make', 'camera_model', 'lens_model', 'focal_length',
                'aperture', 'iso', 'shutter_speed', 'flash', 'orientation',
                'width', 'height'
            ]
            
            for field in exif_fields:
                if field in file_dict and file_dict[field] is not None:
                    exif_data[field] = file_dict[field]
            
            # If we have raw_exif_json, parse it and merge with structured data
            if file_dict.get('raw_exif_json'):
                try:
                    raw_exif = json.loads(file_dict['raw_exif_json'])
                    # Merge raw EXIF data with structured data (structured data takes precedence)
                    exif_data = {**raw_exif, **exif_data}
                except (json.JSONDecodeError, TypeError):
                    pass
            
            # Add exif_data to file_dict if we have any EXIF data
            if exif_data:
                file_dict['exif_data'] = exif_data
            else:
                file_dict['exif_data'] = None
            
            # Remove the individual EXIF fields from the top level
            for field in exif_fields + ['raw_exif_json']:
                file_dict.pop(field, None)
            
            files.append(file_dict)
        
        return files
    
    def get_workflow_executions(self, catalog_id: int) -> List[Dict]:
        """Get workflow executions from catalog-specific database (workflow_executions table)"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            return []
        
        execution_model = ExecutionModel(catalog_db)
        return execution_model.get_all()


class PeopleModel:
    """Model for managing people and their photos in catalog-specific databases"""
    def __init__(self, catalog_db: CatalogDatabase):
        self.catalog_db = catalog_db
    
    def create_person(self, name: str, date_of_birth: Optional[str] = None,
                     description: Optional[str] = None, profile_photo_path: Optional[str] = None,
                     category: Optional[str] = None, kind: str = 'person',
                     species: Optional[str] = None) -> int:
        """
        Create a person - or a pet, which is the same row with kind='pet'.

        `category` is the household either way: a pet sits in Family beside its
        owners. `kind` is what species the row describes. See pet_schema.py.
        """
        if kind not in ('person', 'pet'):
            raise ValueError(f"kind must be 'person' or 'pet', not {kind!r}")
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO people (name, date_of_birth, description, profile_photo_path,
                                category, kind, species)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (name, date_of_birth, description, profile_photo_path,
              category or 'Other', kind, species))
        person_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return person_id
    
    def get_all_people(self, kind: str = 'person') -> List[Dict]:
        """
        People, or pets, or both.

        DEFAULTS TO 'person', and that default is the load-bearing part.

        Every existing caller - the People page, the face tagger, the album
        editor, the user-linking dialog - keeps its present behaviour and,
        crucially, CANNOT offer a pet as an identity for a human face. That one
        filter is what the pet design's isolation rests on at the UI layer: a
        pet acquiring a face_detections row is the single action that would put
        it into the human reference set. A default of 'all' would put the trap
        back. See documentation/specs/pet-recognition-spec.md sections 6 and 7.2.
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        if kind == 'all':
            cursor.execute('SELECT * FROM people ORDER BY name')
        else:
            if kind not in ('person', 'pet'):
                conn.close()
                raise ValueError(f"kind must be 'person', 'pet' or 'all', not {kind!r}")
            # COALESCE, not `= ?`: a catalog upgraded mid-flight could in
            # principle hold a NULL here, and a person is what NULL meant.
            cursor.execute("SELECT * FROM people WHERE COALESCE(kind,'person') = ? "
                           " ORDER BY name", (kind,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_person_by_id(self, person_id: int) -> Optional[Dict]:
        """Get a specific person by ID"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM people WHERE id = ?', (person_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def update_person(self, person_id: int, **kwargs):
        """Update person details"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        
        fields = []
        values = []
        for key, value in kwargs.items():
            if key in ['name', 'date_of_birth', 'description', 'profile_photo_path', 'profile_photo_file_id', 'category']:
                fields.append(f"{key} = ?")
                values.append(value)
        
        if fields:
            values.append(person_id)
            query = f"UPDATE people SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            cursor.execute(query, values)
            conn.commit()
        
        conn.close()
    
    # What each table that references people.id should do when a person goes.
    # These mirror the ON DELETE clauses already declared in the schema — the
    # declarations are correct, they simply never run, because SQLite enforces
    # foreign keys only when PRAGMA foreign_keys is ON and this application never
    # turns it on (see the same note in rescan_faces.py). Rather than flip that
    # globally, which would change insert/delete behaviour across every table at
    # once, delete_person does explicitly what the schema says it wants.
    _PERSON_SET_NULL = (
        # A face whose person is deleted becomes unidentified again, NOT an
        # orphan pointing at a row that no longer exists. Orphans are invisible
        # in the UI but block promotion to a reference face, which inner-joins
        # people and silently skips.
        ('face_detections', 'person_id'),
        ('face_clusters', 'resolved_person_id'),
    )
    _PERSON_CASCADE = (
        ('people_photos', 'person_id'),
        ('face_contents', 'person_id'),
        ('face_similarity_scores', 'person_id'),
        # face_similarity_scores_audit is deliberately absent: it records
        # candidate_person_id as an audit trail and declares no foreign key.
        # An audit entry describing a decision should outlive its subject.
        ('tag_person', 'person_id'),
        ('auto_tag_audit', 'person_id'),
    )

    def delete_person(self, person_id: int) -> Dict[str, int]:
        """
        Delete a person, returning face detections to "unidentified" and removing
        the rows that only existed because of them.

        Returns a count per affected table, so the caller can report what
        happened rather than just "deleted".
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        affected: Dict[str, int] = {}

        def _has(table: str, column: str) -> bool:
            """Catalogs built at different times do not all carry the same
            columns, so check for the column rather than just the table."""
            exists = cursor.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
            ).fetchone() is not None
            if not exists:
                return False
            return any(r[1] == column for r in cursor.execute(f'PRAGMA table_info({table})'))

        try:
            # One transaction: a half-deleted person is worse than either outcome.
            cursor.execute('BEGIN')

            for table, column in self._PERSON_SET_NULL:
                if not _has(table, column):
                    continue
                cursor.execute(
                    f'UPDATE {table} SET {column} = NULL WHERE {column} = ?', (person_id,))
                if cursor.rowcount:
                    affected[table] = cursor.rowcount

            for table, column in self._PERSON_CASCADE:
                if not _has(table, column):
                    continue
                cursor.execute(f'DELETE FROM {table} WHERE {column} = ?', (person_id,))
                if cursor.rowcount:
                    affected[table] = cursor.rowcount

            cursor.execute('DELETE FROM people WHERE id = ?', (person_id,))
            affected['people'] = cursor.rowcount
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

        return affected
    
    def add_photo(self, person_id: int, year: int, photo_path: str, photo_order: int = 1) -> int:
        """Add a photo for a person in a specific year"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO people_photos (person_id, year, photo_path, photo_order)
            VALUES (?, ?, ?, ?)
        ''', (person_id, year, photo_path, photo_order))
        photo_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return photo_id
    
    def get_photos_by_person(self, person_id: int) -> List[Dict]:
        """Get all photos for a person, grouped by year"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM people_photos
            WHERE person_id = ?
            ORDER BY year DESC, photo_order
        ''', (person_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_photos_by_year(self, person_id: int, year: int) -> List[Dict]:
        """Get photos for a person in a specific year"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM people_photos
            WHERE person_id = ? AND year = ?
            ORDER BY photo_order
        ''', (person_id, year))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def delete_photo(self, photo_id: int):
        """Delete a specific photo"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM people_photos WHERE id = ?', (photo_id,))
        conn.commit()
        conn.close()
    
    def get_years_for_person(self, person_id: int) -> List[int]:
        """Get all years that have photos for a person"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT DISTINCT year FROM people_photos
            WHERE person_id = ?
            ORDER BY year DESC
        ''', (person_id,))
        rows = cursor.fetchall()
        conn.close()
        return [row['year'] for row in rows]

class PeopleCategoryModel:
    """Model for managing people categories in catalog-specific databases"""
    def __init__(self, catalog_db: CatalogDatabase):
        self.catalog_db = catalog_db

    def get_all(self) -> List[str]:
        """Return all category names, sorted alphabetically"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT name FROM people_categories ORDER BY name')
        rows = cursor.fetchall()
        conn.close()
        return [row['name'] for row in rows]

    def create(self, name: str) -> str:
        """Create a new category (no-op if already exists). Returns the name."""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('INSERT OR IGNORE INTO people_categories (name) VALUES (?)', (name,))
        conn.commit()
        conn.close()
        return name

    # Re-seeded by _seed_runtime_rows() whenever missing, so a rename would only
    # leave an empty copy behind - and Family is where the Tag Face picker opens.
    BUILT_IN = ('Family', 'Other')

    def rename(self, old: str, new: str) -> int:
        """
        Rename a category, and every person in it, in one transaction.

        A person's category is stored as text on the person, so renaming only
        the people_categories row would leave them under the old name. Raises
        ValueError for a built-in category, an unknown one, or a new name that
        is already taken - two categories are never merged by accident.
        Returns how many people moved.
        """
        if old in self.BUILT_IN:
            raise ValueError(f'"{old}" is built in and cannot be renamed.')
        conn = self.catalog_db.get_connection()
        try:
            cursor = conn.cursor()
            known = cursor.execute('SELECT 1 FROM people_categories WHERE name = ?',
                                   (old,)).fetchone()
            carried = cursor.execute('SELECT 1 FROM people WHERE category = ? LIMIT 1',
                                     (old,)).fetchone()
            if not known and not carried:
                raise LookupError(f'There is no category "{old}".')
            if new != old and (
                    cursor.execute('SELECT 1 FROM people_categories WHERE name = ?',
                                   (new,)).fetchone()
                    or cursor.execute('SELECT 1 FROM people WHERE category = ? LIMIT 1',
                                      (new,)).fetchone()):
                raise ValueError(f'A category named "{new}" already exists.')
            if known:
                cursor.execute('UPDATE people_categories SET name = ? WHERE name = ?',
                               (new, old))
            else:
                # Carried by people but never registered: register it now.
                cursor.execute('INSERT OR IGNORE INTO people_categories (name) VALUES (?)',
                               (new,))
            moved = cursor.execute('UPDATE people SET category = ? WHERE category = ?',
                                   (new, old)).rowcount
            conn.commit()
            return moved
        finally:
            conn.close()

    def delete(self, name: str):
        """Delete a category by name (cannot delete Family)"""
        if name == 'Family':
            return
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM people_categories WHERE name = ?', (name,))
        conn.commit()
        conn.close()


class FaceDetectionModel:
    """Model for managing face detections in catalog-specific databases"""
    def __init__(self, catalog_db: CatalogDatabase):
        self.catalog_db = catalog_db
    
    def add_face_detection(self, file_id: int, x: int, y: int, w: int, h: int,
                          confidence: Optional[float] = None, person_id: Optional[int] = None,
                          source: str = 'Automatic', image_width: Optional[int] = None,
                          image_height: Optional[int] = None) -> int:
        """Add a face detection to an image with the dimensions of the image used for detection"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO face_detections (file_id, x, y, w, h, confidence, person_id, source, image_width, image_height)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (file_id, x, y, w, h, confidence, person_id, source, image_width, image_height))
        face_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return face_id
    
    def get_faces_by_file(self, file_id: int) -> List[Dict]:
        """Get all face detections for a specific file"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT fd.*, p.name as person_name
            FROM face_detections fd
            LEFT JOIN people p ON fd.person_id = p.id
            WHERE fd.file_id = ?
            ORDER BY fd.created_at
        ''', (file_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def update_face_person(self, face_id: int, person_id: Optional[int]) -> bool:
        """Update the person tagged in a face detection"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE face_detections
            SET person_id = ?, source = 'Manual'
            WHERE id = ?
        ''', (person_id, face_id))
        updated = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return updated
    
    def delete_face_detection(self, face_id: int):
        """Delete a face detection"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM face_detections WHERE id = ?', (face_id,))
        conn.commit()
        conn.close()
    
    def delete_faces_by_file(self, file_id: int):
        """Delete all face detections for a file"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM face_detections WHERE file_id = ?', (file_id,))
        conn.commit()
        conn.close()
    
    def delete_automatic_faces_by_file(self, file_id: int):
        """Delete only automatic face detections for a file, preserving manual ones"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM face_detections WHERE file_id = ? AND source = ?', (file_id, 'Automatic'))
        conn.commit()
        conn.close()
    
    def get_manual_faces_by_file(self, file_id: int) -> List[Dict]:
        """Get only manual face detections for a specific file"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT fd.*, p.name as person_name
            FROM face_detections fd
            LEFT JOIN people p ON fd.person_id = p.id
            WHERE fd.file_id = ? AND fd.source = 'Manual'
            ORDER BY fd.created_at
        ''', (file_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_protected_faces_by_file(self, file_id: int) -> List[Dict]:
        """
        Faces that must survive a re-detection pass: anything a person confirmed
        ('Manual') plus anything the contextual model auto-confirmed
        ('Auto-Confirmed').

        Kept separate from get_manual_faces_by_file() because the two questions are
        different. "Which faces are human-verified references?" is Manual only.
        "Which faces must auto-detection not clobber or duplicate?" includes
        auto-confirmed ones - they carry a person_id, so re-detecting a fresh
        Automatic box over the top would leave the same person twice in one photo.
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute("""
            SELECT fd.*, p.name as person_name
            FROM face_detections fd
            LEFT JOIN people p ON fd.person_id = p.id
            WHERE fd.file_id = ? AND fd.source IN ('Manual', 'Auto-Confirmed')
            ORDER BY fd.created_at
        """, (file_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_all_manual_faces_with_files(self) -> List[Dict]:
        """Get all manual face detections with their associated file information"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                fd.id as face_id,
                fd.file_id,
                fd.x,
                fd.y,
                fd.w,
                fd.h,
                fd.confidence,
                fd.person_id,
                fd.image_width,
                fd.image_height,
                fd.created_at,
                p.name as person_name,
                fc.file_path,
                fc.file_name,
                fc.file_type,
                ed.creation_date,
                CAST(strftime('%Y', ed.creation_date) AS INTEGER) as photo_year
            FROM face_detections fd
            LEFT JOIN people p ON fd.person_id = p.id
            INNER JOIN file_contents fc ON fd.file_id = fc.id
            LEFT JOIN exif_data ed ON fc.exif_data_id = ed.id
            WHERE fd.source = 'Manual' AND fc.removed_at IS NULL
            ORDER BY fd.created_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_manual_faces_by_person(self, person_id: int) -> List[Dict]:
        """Get all manual face detections for a specific person with file information"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT
                fd.id as face_id,
                fd.file_id,
                fd.x,
                fd.y,
                fd.w,
                fd.h,
                fd.confidence,
                fd.person_id,
                fd.image_width,
                fd.image_height,
                fd.created_at,
                p.name as person_name,
                fc.file_path,
                fc.file_name,
                fc.file_type,
                ed.creation_date,
                CAST(strftime('%Y', ed.creation_date) AS INTEGER) as photo_year
            FROM face_detections fd
            LEFT JOIN people p ON fd.person_id = p.id
            INNER JOIN file_contents fc ON fd.file_id = fc.id
            LEFT JOIN exif_data ed ON fc.exif_data_id = ed.id
            WHERE fd.source = 'Manual' AND fd.person_id = ? AND fc.removed_at IS NULL
            ORDER BY fd.created_at DESC
        ''', (person_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_files_with_manual_faces(self) -> List[Dict]:
        """Get all files that have manual face detections"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT DISTINCT
                fc.id as file_id,
                fc.file_path,
                fc.file_name,
                fc.file_type,
                COUNT(fd.id) as manual_face_count
            FROM file_contents fc
            INNER JOIN face_detections fd ON fc.id = fd.file_id
            WHERE fd.source = 'Manual' AND fc.removed_at IS NULL
            GROUP BY fc.id, fc.file_path, fc.file_name, fc.file_type
            ORDER BY fc.file_name
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
        return [dict(row) for row in rows]

# The order photos are shown in within an event or an album section: when each
# was taken, as the camera recorded it. Compared as WALL-CLOCK time, ignoring
# the offset suffix: a camera that records no offset (a Canon EOS 6D, say) is
# stored as "...Z", and treating that as real UTC would place its photos hours
# away from those of a camera that does record -04:00, shot at the same moment.
# The camera's own file name breaks ties - burst frames share a second. Stored
# names carry the importer's "YYYYMMDDHHMMSS-<10 hex>-" prefix (26 characters),
# whose hash would otherwise decide: HS8A9120a sorted before HS8A9120. The full
# name is the last resort. `fc` is file_contents.
_STORED_PREFIX_GLOB = '[0-9]' * 14 + '-' + '[0-9a-f]' * 10 + '-*'
TAKEN_ORDER_SQL = (
    "substr(replace(fc.master_datetime, 'T', ' '), 1, 19), "
    f"CASE WHEN fc.file_name GLOB '{_STORED_PREFIX_GLOB}' "
    "THEN substr(fc.file_name, 27) ELSE fc.file_name END, "
    "fc.file_name")


class EventModel:
    """Model for managing events and their file associations in catalog-specific databases"""
    def __init__(self, catalog_db: CatalogDatabase):
        self.catalog_db = catalog_db

    def create_event(self, name: str, start_time: str, end_time: str,
                     latitude: Optional[float] = None, longitude: Optional[float] = None,
                     description: Optional[str] = None, file_count: int = 0,
                     type: str = 'Auto') -> int:
        """Create a new event"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO events (name, start_time, end_time, latitude, longitude, description, file_count, type)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', (name, start_time, end_time, latitude, longitude, description, file_count, type))
        event_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return event_id

    def update_event(self, event_id: int, **kwargs):
        """Update event details"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()

        fields = []
        values = []
        for key, value in kwargs.items():
            if key in ['name', 'start_time', 'end_time', 'latitude', 'longitude',
                       'description', 'file_count', 'type', 'cover_file_id']:
                fields.append(f"{key} = ?")
                values.append(value)

        if fields:
            values.append(event_id)
            query = f"UPDATE events SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            cursor.execute(query, values)
            conn.commit()

        conn.close()

    def get_all_events(self) -> List[Dict]:
        """Get all events ordered by start_time"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM events ORDER BY start_time')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_event_by_id(self, event_id: int) -> Optional[Dict]:
        """Get a specific event by ID"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM events WHERE id = ?', (event_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def delete_event(self, event_id: int):
        """Delete an event and all its file associations"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM events WHERE id = ?', (event_id,))
        conn.commit()
        conn.close()

    def add_event_file(self, event_id: int, file_id: int) -> int:
        """Associate a file with an event (ignore if already exists)"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO event_files (event_id, file_id)
            VALUES (?, ?)
        ''', (event_id, file_id))
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def get_files_for_event(self, event_id: int) -> List[Dict]:
        """Get all file_contents records associated with an event"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT fc.*
            FROM file_contents fc
            JOIN event_files ef ON fc.id = ef.file_id
            WHERE ef.event_id = ? AND fc.removed_at IS NULL
            ORDER BY ''' + TAKEN_ORDER_SQL, (event_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_events_for_file(self, file_id: int) -> List[Dict]:
        """Get all events associated with a specific file"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT e.*
            FROM events e
            JOIN event_files ef ON e.id = ef.event_id
            WHERE ef.file_id = ?
            ORDER BY e.start_time
        ''', (file_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def delete_all_events(self):
        """Delete all events and their file associations (for re-processing)"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM event_files')
        cursor.execute('DELETE FROM events')
        conn.commit()
        conn.close()

    # ------------------------------------------------------------------
    # Event covers  (documentation/specs/event-cards-ui-spec.md section 3)
    #
    # A cover is picked at random ONCE, written to events.cover_file_id, and
    # never re-rolled. Without the write-back the pick would re-roll on every
    # page load: the card would show a different photo each time and the star
    # control would look broken, because a user's choice would appear not to
    # stick.
    # ------------------------------------------------------------------

    # Ranked last when picking a cover: a poster frame is a fine cover but a
    # still is the safer default (spec section 3.3). Mirrors media_types
    # VIDEO_EXTENSIONS; file_contents.file_type stores the dotted, lowercased
    # extension.
    _COVER_VIDEO_TYPES = (
        '.mp4', '.avi', '.mov', '.wmv', '.flv', '.mkv', '.webm',
        '.m4v', '.mpg', '.mpeg', '.3gp', '.3g2', '.mts', '.m2ts',
        '.vob', '.ogv', '.gifv',
    )

    def _pick_cover_file_id(self, cursor, event_id: int) -> Optional[int]:
        """
        One of the event's files, at random, photos first. None if it has none
        that can actually be shown.

        A 320 thumbnail is REQUIRED, not merely preferred. The card renders that
        asset and nothing else, so storing a file without one would pin the
        event to a permanently broken image — and events do contain such files:
        the importer files stray .DS_Store entries alongside the photos. Leaving
        the cover unset instead shows the placeholder and re-resolves on the
        next read, which also covers the window where a photo is imported but
        its thumbnail has not been generated yet.

        A video qualifies through its poster frame, which is stored under the
        same characteristic (video-spec.md section 7), but ranks after photos:
        a still is the safer default (spec section 3.3).
        """
        placeholders = ','.join('?' * len(self._COVER_VIDEO_TYPES))
        cursor.execute(f"""
            SELECT fc.id
            FROM event_files ef
            JOIN file_contents fc ON fc.id = ef.file_id
            WHERE ef.event_id = ? AND fc.removed_at IS NULL
              AND EXISTS (SELECT 1 FROM alternative_versions av
                          WHERE av.original_file_id = fc.id
                            AND UPPER(av.characteristic) LIKE '%THUMBNAIL 320%')
            ORDER BY
                CASE WHEN LOWER(COALESCE(fc.file_type, '')) IN ({placeholders})
                     THEN 1 ELSE 0 END,
                RANDOM()
            LIMIT 1
        """, (event_id, *self._COVER_VIDEO_TYPES))
        row = cursor.fetchone()
        return row['id'] if row else None

    def resolve_covers(self, events: List[Dict]) -> List[Dict]:
        """
        Fill in cover_file_id for a list of events, in ONE pass.

        Resolved in bulk rather than per card on purpose: the events list
        endpoint returns every event, and letting each card fetch its own cover
        is N requests on page load (spec section 4).

        Mutates and returns the dicts. An event with no usable file keeps
        cover_file_id None and the card shows its placeholder.
        """
        if not events:
            return events

        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        try:
            # A stored id can point at a file that has since been deleted: this
            # application never enables PRAGMA foreign_keys, so nothing cleared
            # it. Treat a dangling id as "no cover" and re-roll (spec 3.1).
            stored = [e.get('cover_file_id') for e in events if e.get('cover_file_id')]
            live = set()
            if stored:
                marks = ','.join('?' * len(stored))
                live = {r['id'] for r in cursor.execute(
                    f'SELECT id FROM file_contents WHERE id IN ({marks}) '
                    f'AND removed_at IS NULL', stored)}

            wrote = False
            for event in events:
                current = event.get('cover_file_id')
                if current and current in live:
                    continue
                picked = self._pick_cover_file_id(cursor, event['id'])
                if picked is None:
                    event['cover_file_id'] = None
                    continue
                # Conditional on the value we read, so two cards resolving the
                # same event at once cannot end up with two different covers —
                # the first write wins and the second is a no-op. "IS ?" is
                # SQLite's null-safe comparison, so this covers both the unset
                # case and the repair of a dangling id.
                cursor.execute(
                    'UPDATE events SET cover_file_id = ? WHERE id = ? AND cover_file_id IS ?',
                    (picked, event['id'], current))
                event['cover_file_id'] = picked
                wrote = True

            if wrote:
                conn.commit()
                # Re-read after committing: if another request won the race, the
                # winner's value is what every card must show.
                ids = [e['id'] for e in events]
                marks = ','.join('?' * len(ids))
                actual = {r['id']: r['cover_file_id'] for r in cursor.execute(
                    f'SELECT id, cover_file_id FROM events WHERE id IN ({marks})', ids)}
                for event in events:
                    if event['id'] in actual:
                        event['cover_file_id'] = actual[event['id']]
        finally:
            conn.close()
        return events

    def set_cover(self, event_id: int, file_id: int) -> bool:
        """
        Set the cover explicitly. False when the file is not in this event.

        The membership check is the authorization check too: without it an event
        could be given a cover from another event, or from a catalog the caller
        cannot read (spec section 4).
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                'SELECT 1 FROM event_files WHERE event_id = ? AND file_id = ?',
                (event_id, file_id))
            if not cursor.fetchone():
                return False
            cursor.execute(
                'UPDATE events SET cover_file_id = ?, updated_at = CURRENT_TIMESTAMP '
                'WHERE id = ?', (file_id, event_id))
            conn.commit()
            return True
        finally:
            conn.close()

    def clear_cover(self, event_id: int):
        """Clear the cover; the next read rolls a fresh one and stores it."""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE events SET cover_file_id = NULL, updated_at = CURRENT_TIMESTAMP '
            'WHERE id = ?', (event_id,))
        conn.commit()
        conn.close()

    def get_people_for_event(self, event_id: int) -> List[Dict]:
        """Get all people tagged in photos associated with this event"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT DISTINCT p.id, p.name, p.profile_photo_path, p.profile_photo_file_id
            FROM people p
            JOIN face_detections fd ON p.id = fd.person_id
            JOIN event_files ef ON fd.file_id = ef.file_id
            WHERE ef.event_id = ?
            ORDER BY p.name
        ''', (event_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]


class TagModel:
    """Model for managing tags and their associations with people"""
    def __init__(self, catalog_db: CatalogDatabase):
        self.catalog_db = catalog_db

    def get_all_tags(self) -> List[Dict]:
        """Get all tags in the catalog"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tags ORDER BY name')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def search_tags(self, query: str) -> List[Dict]:
        """Search tags by name (for autocomplete)"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM tags WHERE name LIKE ? ORDER BY name LIMIT 20', (f'%{query}%',))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_or_create_tag(self, name: str) -> int:
        """Get existing tag by name or create a new one. Returns tag id."""
        name = name.strip()
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT id FROM tags WHERE name = ?', (name,))
        row = cursor.fetchone()
        if row:
            tag_id = row['id']
        else:
            cursor.execute('INSERT INTO tags (name) VALUES (?)', (name,))
            tag_id = cursor.lastrowid
            conn.commit()
        conn.close()
        return tag_id

    def get_tags_for_person(self, person_id: int) -> List[Dict]:
        """Get all tags for a specific person"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT t.id, t.name, t.created_at
            FROM tags t
            JOIN tag_person tp ON t.id = tp.tag_id
            WHERE tp.person_id = ?
            ORDER BY t.name
        ''', (person_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def set_tags_for_person(self, person_id: int, tag_names: List[str]):
        """Replace all tags for a person with the given list of tag names"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()

        # Remove all existing tag associations for this person
        cursor.execute('DELETE FROM tag_person WHERE person_id = ?', (person_id,))

        # Add new tags
        for name in tag_names:
            name = name.strip()
            if not name:
                continue
            # Get or create tag
            cursor.execute('SELECT id FROM tags WHERE name = ?', (name,))
            row = cursor.fetchone()
            if row:
                tag_id = row['id']
            else:
                cursor.execute('INSERT INTO tags (name) VALUES (?)', (name,))
                tag_id = cursor.lastrowid

            # Associate tag with person (ignore if already exists)
            cursor.execute('''
                INSERT OR IGNORE INTO tag_person (tag_id, person_id) VALUES (?, ?)
            ''', (tag_id, person_id))

        conn.commit()
        conn.close()

    def add_tag_to_person(self, person_id: int, tag_name: str) -> int:
        """Add a tag to a person. Creates the tag if it doesn't exist. Returns tag id."""
        tag_id = self.get_or_create_tag(tag_name)
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO tag_person (tag_id, person_id) VALUES (?, ?)
        ''', (tag_id, person_id))
        conn.commit()
        conn.close()
        return tag_id

    def remove_tag_from_person(self, person_id: int, tag_id: int):
        """Remove a tag association from a person"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM tag_person WHERE person_id = ? AND tag_id = ?', (person_id, tag_id))
        conn.commit()
        conn.close()


class LocationModel:
    """Model for managing locations and their event associations"""
    def __init__(self, catalog_db: CatalogDatabase):
        self.catalog_db = catalog_db

    def create_location(self, name: str, latitude: Optional[float] = None,
                        longitude: Optional[float] = None,
                        description: Optional[str] = None,
                        source: Optional[str] = None,
                        confirmed: bool = True) -> int:
        """
        Create a new location.

        `confirmed` defaults to True because the caller that has always existed
        is the Add Location form, where a person typed the name. The clustering
        workflow passes False: it supplies a geocoder's guess, which is a
        proposal and not an answer.
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO locations (name, latitude, longitude, description,
                                   source, confirmed)
            VALUES (?, ?, ?, ?, ?, ?)
        ''', (name, latitude, longitude, description, source,
              1 if confirmed else 0))
        location_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return location_id

    def update_location(self, location_id: int, **kwargs):
        """Update location details"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        fields = []
        values = []
        for key, value in kwargs.items():
            if key in ['name', 'latitude', 'longitude', 'description', 'source',
                       'map_image_path', 'confirmed']:
                fields.append(f"{key} = ?")
                values.append(value)
        if fields:
            values.append(location_id)
            query = f"UPDATE locations SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            cursor.execute(query, values)
            conn.commit()
        conn.close()

    def count_locations(self, confirmed_only: bool = False) -> int:
        """How many locations there are, for a pager that has not fetched them."""
        conn = self.catalog_db.get_connection()
        try:
            where = 'WHERE confirmed = 1' if confirmed_only else ''
            return conn.execute(
                f'SELECT COUNT(*) FROM locations {where}').fetchone()[0]
        except Exception:
            return 0
        finally:
            conn.close()

    def get_all_locations(self, confirmed_only: bool = False,
                          limit: int = None, offset: int = 0) -> List[Dict]:
        """
        Every location, with what its card needs.

        `confirmed_only` is what the event-tagging picker asks for. An
        unconfirmed location is a geocoder's guess the clustering workflow
        wrote, and offering 1,093 of those when somebody wants to tag one event
        would bury the handful they actually named.

        `file_count` and `cover_file_id` come from file_locations, which only
        exists because a photo now links to a location directly - before that,
        counting a location's photos meant joining through its events and a
        cover meant a second request per card. A proposed location has carried
        both since it was first drawn; a named one showing neither is what made
        the two kinds of card look like different things.

        LEFT JOIN, so a location nothing points at yet still appears with a
        count of zero rather than vanishing from the list.

        `limit`/`offset` page it. The LIMIT goes after the GROUP BY and the
        ORDER BY, so a page is a page of the SAME ordering the whole list
        would have had - confirmed first, then the biggest proposals. Paging a
        differently-ordered query would show some locations twice and others
        never.
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        try:
            where = 'WHERE l.confirmed = 1' if confirmed_only else ''
            # Built here rather than passed as parameters: SQLite does not
            # accept a placeholder for LIMIT in every build, and these are
            # already integers by the time they arrive.
            page = (f' LIMIT {int(limit)} OFFSET {int(offset)}'
                    if limit is not None else '')
            cursor.execute(f'''
                SELECT l.*,
                       COUNT(fl.file_id) AS file_count,
                       MIN(fl.file_id)   AS cover_file_id,
                       (SELECT c.id FROM location_clusters c
                         WHERE c.location_id = l.id ORDER BY c.id LIMIT 1)
                           AS cluster_id
                  FROM locations l
                  LEFT JOIN file_locations fl ON fl.location_id = l.id
                  LEFT JOIN file_contents fc ON fc.id = fl.file_id
                                             AND fc.removed_at IS NULL
                  {where}
                 GROUP BY l.id
                 -- Confirmed first, then the biggest proposals: the ones a
                 -- person has settled, then the ones most worth settling next.
                 ORDER BY l.confirmed DESC, COUNT(fl.file_id) DESC, l.name
                 {page}
            ''')
            rows = cursor.fetchall()
        except Exception:
            # A catalog that predates file_locations still lists its locations.
            cursor.execute('SELECT * FROM locations ORDER BY name' + page)
            rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_location_by_id(self, location_id: int) -> Optional[Dict]:
        """Get a specific location by ID"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM locations WHERE id = ?', (location_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def delete_location(self, location_id: int):
        """
        Delete a location and every reference to it.

        The references are removed EXPLICITLY. This used to delete the locations
        row alone and rely on ON DELETE CASCADE, which has never fired: the
        application does not enable PRAGMA foreign_keys, so the clause is inert
        and every deleted location left its location_events rows behind. Three
        tables point at a location now, and a dangling location_id in
        file_locations would show a photo a place that no longer exists.
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM location_events WHERE location_id = ?',
                       (location_id,))
        # A cluster whose place is gone goes back to being a proposal rather
        # than being deleted: the photos are still grouped, it just has no name.
        cursor.execute("UPDATE location_clusters SET status = 'proposed', "
                       'location_id = NULL, updated_at = CURRENT_TIMESTAMP '
                       'WHERE location_id = ?', (location_id,))
        cursor.execute('UPDATE file_locations SET location_id = NULL, '
                       'location_source = NULL, location_event_id = NULL, '
                       'location_share = NULL, updated_at = CURRENT_TIMESTAMP '
                       'WHERE location_id = ?', (location_id,))
        cursor.execute('DELETE FROM locations WHERE id = ?', (location_id,))
        conn.commit()
        conn.close()

    # ── Resolved per-file locations (location-spec.md 4.1, 6) ────────────────

    def get_file_location(self, file_id: int) -> Optional[Dict]:
        """
        What we know about one file's position and place, with the provenance
        of each.

        Returns the file_locations row joined to the location's name, or None
        when nothing has been resolved for this file. `location_share` is what
        distinguishes a place a person tagged on the event (NULL) from one its
        members voted for (a fraction) - the UI says different things for each.
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute('''
                SELECT fl.*, l.name AS location_name, e.name AS location_event_name
                  FROM file_locations fl
                  LEFT JOIN locations l ON l.id = fl.location_id
                  LEFT JOIN events e ON e.id = fl.location_event_id
                 WHERE fl.file_id = ?
            ''', (file_id,))
            row = cursor.fetchone()
        except Exception:
            # A catalog opened before this feature existed. Treated as "nothing
            # resolved" rather than an error: the photo page still has its own
            # EXIF coordinates to show.
            row = None
        conn.close()
        return dict(row) if row else None

    def set_file_location(self, file_id: int, location_id: Optional[int]):
        """
        Tag one photo with a location by hand, or clear that tag.

        Written as location_source='manual', the top of the place ladder, so the
        resolve workflow leaves it alone on every later run.
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        if location_id is None:
            cursor.execute(
                'UPDATE file_locations SET location_id = NULL, '
                'location_source = NULL, location_event_id = NULL, location_share = NULL, '
                "updated_at = CURRENT_TIMESTAMP WHERE file_id = ? "
                "AND location_source = 'manual'", (file_id,))
        else:
            cursor.execute('''
                INSERT INTO file_locations (file_id, location_id, location_source)
                VALUES (?, ?, 'manual')
                ON CONFLICT (file_id) DO UPDATE SET
                    location_id = excluded.location_id,
                    location_source = 'manual',
                    location_event_id = NULL,
                    location_share = NULL,
                    updated_at = CURRENT_TIMESTAMP
            ''', (file_id, location_id))
        conn.commit()
        conn.close()

    # ── Proposed locations (location-spec.md 4.4, 8.1) ───────────────────────

    def get_clusters(self, status: str = 'proposed', limit: int = 200,
                     offset: int = 0) -> List[Dict]:
        """
        Proposed locations, biggest first.

        `suggestions` is decoded here rather than left as a JSON string, so the
        page does not have to know it was stored that way. A cluster the
        geocoder could not be asked about has none, which is not an error - it
        is still a cluster a person can name (spec 7.2).
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        try:
            sql = ('''SELECT c.*, l.name AS location_name,
                             (SELECT lcf.file_id FROM location_cluster_files lcf
                               WHERE lcf.cluster_id = c.id
                               ORDER BY lcf.distance_metres LIMIT 1) AS cover_file_id
                        FROM location_clusters c
                        LEFT JOIN locations l ON l.id = c.location_id''')
            params: list = []
            if status and status != 'all':
                sql += ' WHERE c.status = ?'
                params.append(status)
            sql += ' ORDER BY c.file_count DESC, c.id LIMIT ? OFFSET ?'
            params += [limit, offset]
            cursor.execute(sql, params)
            rows = [dict(r) for r in cursor.fetchall()]
        except Exception:
            rows = []
        conn.close()
        for row in rows:
            raw = row.get('suggestions')
            try:
                row['suggestions'] = json.loads(raw) if raw else []
            except (ValueError, TypeError):
                row['suggestions'] = []
        return rows

    def count_clusters(self, status: str = 'proposed') -> int:
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        try:
            if status and status != 'all':
                cursor.execute('SELECT COUNT(*) FROM location_clusters '
                               'WHERE status = ?', (status,))
            else:
                cursor.execute('SELECT COUNT(*) FROM location_clusters')
            n = cursor.fetchone()[0]
        except Exception:
            n = 0
        conn.close()
        return n

    def reject_cluster(self, cluster_id: int,
                       delete_unconfirmed_location: bool = False):
        """
        Mark a cluster as not a location.

        The CLUSTER is kept, not deleted: the clustering workflow proposes
        whatever it finds, so a deleted rejection comes straight back on the
        next run. Same reason a rejected face candidate is recorded rather than
        forgotten.

        Its locations row is a different matter. An unconfirmed one is the
        guess being rejected and goes with it; a confirmed one is somebody's own
        name for the place, and rejecting the grouping that suggested it is not
        the same as deleting their work.
        """
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        if delete_unconfirmed_location:
            cursor.execute('''
                DELETE FROM locations WHERE confirmed = 0 AND id = (
                    SELECT location_id FROM location_clusters WHERE id = ?)
            ''', (cluster_id,))
            cursor.execute('''
                UPDATE file_locations SET location_id = NULL, location_source = NULL,
                       location_event_id = NULL, location_share = NULL,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE location_id NOT IN (SELECT id FROM locations)
            ''')
        cursor.execute("UPDATE location_clusters SET status = 'rejected', "
                       'location_id = NULL, updated_at = CURRENT_TIMESTAMP '
                       'WHERE id = ?', (cluster_id,))
        conn.commit()
        conn.close()

    def reopen_cluster(self, cluster_id: int):
        """Undo an accept or a reject, putting a cluster back in the queue."""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE location_clusters SET status = 'proposed', "
                       'location_id = NULL, updated_at = CURRENT_TIMESTAMP '
                       'WHERE id = ?', (cluster_id,))
        conn.commit()
        conn.close()

    # How many files got their location from each source is NOT here: it is
    # content_report._locations(), which is the one place that answers it and
    # the only thing that was asking. A second implementation on this model
    # was written, never called, and would have drifted - counting by source
    # is exactly the sort of thing two copies disagree about quietly.

    def add_location_event(self, location_id: int, event_id: int) -> int:
        """Associate a location with an event (ignore if already exists)"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT OR IGNORE INTO location_events (location_id, event_id)
            VALUES (?, ?)
        ''', (location_id, event_id))
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def remove_location_event(self, location_id: int, event_id: int):
        """Remove the association between a location and an event"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'DELETE FROM location_events WHERE location_id = ? AND event_id = ?',
            (location_id, event_id)
        )
        conn.commit()
        conn.close()

    def get_locations_for_event(self, event_id: int) -> List[Dict]:
        """Get all locations associated with an event"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT l.*
            FROM locations l
            JOIN location_events le ON l.id = le.location_id
            WHERE le.event_id = ?
            ORDER BY l.name
        ''', (event_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_events_for_location(self, location_id: int) -> List[Dict]:
        """Get all events associated with a specific location"""
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT e.*
            FROM events e
            JOIN location_events le ON e.id = le.event_id
            WHERE le.location_id = ?
            ORDER BY e.start_time
        ''', (location_id,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]


class UserSettingsModel:
    """Model for managing user settings"""
    def __init__(self, db: 'Database'):
        self.db = db
    
    def get_user_settings(self, user_id: Optional[int] = None) -> Optional[Dict]:
        """
        Settings for one account.

        These were written when airlock had a single implicit user, and returned
        "the first row" to whoever asked. With real accounts that showed one
        person's name, email, address and home coordinates to everybody, so the
        row is now selected by user_id.

        user_id=None keeps the old behaviour for callers that genuinely have no
        user - a background workflow asking for a home location, say - and is
        never used to answer a browser request.
        """
        conn = self.db.get_connection()
        cursor = conn.cursor()
        if user_id is None:
            cursor.execute('SELECT * FROM user_settings ORDER BY id LIMIT 1')
        else:
            cursor.execute('SELECT * FROM user_settings WHERE user_id = ? ORDER BY id LIMIT 1',
                           (user_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def update_user_settings(self, user_id: int, name: str, email: str, address: str,
                            city: str, state: str = '', country: str = '',
                            lat: Optional[float] = None,
                            long: Optional[float] = None,
                            birthdate: Optional[str] = None) -> Dict:
        """
        Create or update one account's settings.

        Previously this updated "the first row" whatever the caller, so a second
        user saving their settings overwrote the first user's - including the
        home coordinates that event extraction falls back to. It is now an upsert
        keyed on user_id.
        """
        conn = self.db.get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT id FROM user_settings WHERE user_id = ? ORDER BY id LIMIT 1',
                       (user_id,))
        row = cursor.fetchone()

        if row:
            row_id = row['id']
            cursor.execute('''
                UPDATE user_settings
                SET name = ?, email = ?, address = ?, city = ?, state = ?, country = ?,
                    lat = ?, long = ?, birthdate = ?, updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (name, email, address, city, state, country, lat, long,
                  birthdate, row_id))
        else:
            cursor.execute('''
                INSERT INTO user_settings
                    (user_id, name, email, address, city, state, country, lat, long,
                     birthdate)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, name, email, address, city, state, country, lat, long,
                  birthdate))
            row_id = cursor.lastrowid

        conn.commit()

        cursor.execute('SELECT * FROM user_settings WHERE id = ?', (row_id,))
        row = cursor.fetchone()
        conn.close()

        return dict(row) if row else None
