import sqlite3
import os
import json as _json
from typing import Optional, List, Dict, Any
from datetime import datetime
from media_types import VIDEO_EXTENSIONS
from file_manager import (
    FileManager, FileWriteStrategy, LocalCopyStrategy, SCPStrategy,
    FTPStrategy, BoxStrategy, S3Strategy
)


def _parse_rungs(value):
    """
    A catalog's optional image rungs, as a list, from whatever the caller had.

    Accepts the JSON text the column stores, a list an API handler already
    parsed, or None. Anything unreadable becomes the empty list rather than
    raising: a malformed value must not make a catalog unopenable, and "no
    extra rungs" is the safe reading - the mandatory base is added downstream
    by utilities.rungs_for() and does not depend on this.
    """
    if not value:
        return []
    if isinstance(value, str):
        try:
            value = _json.loads(value)
        except (ValueError, TypeError):
            return []
    if not isinstance(value, (list, tuple)):
        return []
    # Deduplicated, order preserved: this is displayed to a person.
    out, seen = [], set()
    for item in value:
        if isinstance(item, str) and item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _dump_rungs(value):
    """The column form of _parse_rungs' output. Always valid JSON."""
    return _json.dumps(_parse_rungs(value))


def _migrate_routes_to_owner(cursor):
    """
    Convert installation-wide storage routes into catalog-owned ones.

    Runs once, at schema init, and is a no-op afterwards. It exists because the
    old model let a route be linked to any number of catalogs through
    catalog_storage_routes, which meant a route could outlive the catalog it was
    made for and feed catalogs it was never intended for.

    Three cases, and none of them touches a directory or a file:

      linked to exactly one catalog  that catalog becomes the owner
      linked to several              ONE COPY PER CATALOG, so no catalog loses
                                     access to a folder it was already using
      linked to none                 dropped, and named in the log - it is a
                                     name and a path with nothing to belong to

    SQLite cannot drop a UNIQUE constraint or add a NOT NULL column in place, so
    this rebuilds the table. The old one is renamed rather than deleted until
    the copy has succeeded.
    """
    cols = {r[1] for r in cursor.execute('PRAGMA table_info(storage_routes)')}
    if 'catalog_id' in cols or not cols:
        return                                  # already migrated, or brand new

    has_links = cursor.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='catalog_storage_routes'").fetchone()
    links = {}
    if has_links:
        for route_id, catalog_id in cursor.execute(
                'SELECT route_id, catalog_id FROM catalog_storage_routes '
                'WHERE catalog_id IN (SELECT id FROM catalog_meta) '
                'ORDER BY route_id, catalog_id'):
            links.setdefault(route_id, []).append(catalog_id)

    cursor.execute('SELECT * FROM storage_routes')
    names = [c[0] for c in cursor.description]
    old_rows = [dict(zip(names, row)) for row in cursor.fetchall()]

    cursor.execute('ALTER TABLE storage_routes RENAME TO storage_routes_old')
    cursor.execute("""
        CREATE TABLE storage_routes (
            id             INTEGER PRIMARY KEY AUTOINCREMENT,
            catalog_id     INTEGER NOT NULL,
            name           TEXT NOT NULL,
            kind           TEXT NOT NULL DEFAULT 'local',
            host_path      TEXT,
            container_path TEXT NOT NULL,
            read_only      INTEGER NOT NULL DEFAULT 1,
            created_by     INTEGER,
            created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (catalog_id, name)
        )
    """)

    # route_id -> {catalog_id: new_id}, so workflows pointing at the old id can
    # be repointed per catalog below.
    remap = {}
    orphaned, duplicated = [], []
    for row in old_rows:
        owners = links.get(row['id'], [])
        if not owners:
            orphaned.append(row['name'])
            continue
        if len(owners) > 1:
            duplicated.append((row['name'], len(owners)))
        for catalog_id in owners:
            name = row['name']
            suffix = 1
            while cursor.execute(
                    'SELECT 1 FROM storage_routes WHERE catalog_id = ? AND name = ?',
                    (catalog_id, name)).fetchone():
                suffix += 1
                name = f"{row['name']} ({suffix})"
            cursor.execute(
                'INSERT INTO storage_routes '
                '(catalog_id, name, kind, host_path, container_path, read_only, '
                ' created_by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (catalog_id, name, row.get('kind') or 'local', row.get('host_path'),
                 row['container_path'], row.get('read_only', 1),
                 row.get('created_by'), row.get('created_at')))
            remap.setdefault(row['id'], {})[catalog_id] = cursor.lastrowid

    cursor.execute('DROP TABLE storage_routes_old')
    if has_links:
        cursor.execute('DROP TABLE catalog_storage_routes')

    cursor.execute('SELECT id, target_directory FROM catalog_meta')
    _repoint_workflow_routes(remap, cursor.fetchall())

    total = cursor.execute('SELECT COUNT(*) FROM storage_routes').fetchone()[0]
    print(f'  storage routes are now owned by a catalog ({total} route(s))')
    for name, n in duplicated:
        print(f"    '{name}' fed {n} catalogs - one copy each, so none lost access")
    for name in orphaned:
        print(f"    '{name}' belonged to no catalog and was dropped "
              f"(the folder and its files are untouched)")


def _repoint_workflow_routes(remap, catalogs):
    """
    Point each catalog's workflows at its own copy of a duplicated route.

    Workflows live in the per-catalog database and reference a route by id, so
    splitting a shared route leaves those ids pointing at a row that no longer
    exists. Only rewrites within the catalog that owns the new copy, so a
    workflow can never end up aimed at another catalog's route.

    Never fatal: a catalog whose directory is unplugged keeps its old id, which
    resolve_source() already reports as unavailable rather than crashing.

    `catalogs` is (id, target_directory) rows read by the caller. It is passed
    in rather than fetched here because this runs inside schema init, where
    Catalog.get_all() would re-enter the initialiser that called it.
    """
    if not remap:
        return
    import sqlite3 as _sq
    for catalog_id, target in catalogs:
        if not target:
            continue
        db_path = os.path.join(target, 'workflow_results.db')
        if not os.path.exists(db_path):
            continue
        try:
            conn = _sq.connect(db_path, timeout=30)
            try:
                cols = {r[1] for r in conn.execute('PRAGMA table_info(workflows)')}
                if 'source_route_id' not in cols:
                    continue
                for old_id, by_catalog in remap.items():
                    new_id = by_catalog.get(catalog_id)
                    if new_id is None or new_id == old_id:
                        continue
                    conn.execute(
                        'UPDATE workflows SET source_route_id = ? '
                        'WHERE source_route_id = ?', (new_id, old_id))
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            print(f'  ! could not repoint workflows for catalog {catalog_id}: {exc}')


# The strip across the top of a catalog card (card_decorators.py).
from card_decorators import (  # noqa: E402
    CATALOG_THEME as DECORATOR_THEME, DECORATORS_PER_THEME as DECORATOR_COUNT,
    random_decorator as _random_decorator,
)


def random_decorator(theme: str = DECORATOR_THEME, count: int = DECORATOR_COUNT) -> str:
    return _random_decorator(theme, count)


class Catalog:
    """
    Catalog class to manage storage configurations with SQLite persistence.
    Stores catalog information and all parameters needed for FileManager strategies.
    """
    
    # Database file path (in database directory)
    DB_PATH = os.path.join(os.path.dirname(__file__), 'database', 'catalogs.db')
    
    def __init__(self, name: str, owner_user_id: Optional[int] = None, description: str = "",
                 file_strategy: str = "local",
                 target_directory: Optional[str] = None,
                 catalog_id: Optional[int] = None,
                 # LocalCopy parameters (no additional params needed)
                 # SCP parameters
                 scp_username: Optional[str] = None,
                 scp_host: Optional[str] = None,
                 scp_port: int = 22,
                 # FTP parameters
                 ftp_host: Optional[str] = None,
                 ftp_username: Optional[str] = None,
                 ftp_password: Optional[str] = None,
                 ftp_port: int = 21,
                 # Box parameters
                 box_client_id: Optional[str] = None,
                 box_client_secret: Optional[str] = None,
                 box_access_token: Optional[str] = None,
                 # S3 parameters
                 s3_bucket_name: Optional[str] = None,
                 s3_aws_access_key_id: Optional[str] = None,
                 s3_aws_secret_access_key: Optional[str] = None,
                 s3_region_name: Optional[str] = None,
                 # Face recognition metric
                 similarity_metric: Optional[str] = None,
                 # Extra image sizes this catalog generates (list of rung keys)
                 image_rungs=None,
                 # Ingestion mode: 'copy' (default) or 'reference'
                 ingest_mode: Optional[str] = None,
                 source_root: Optional[str] = None,
                 # URL segment / DNS label for album deployments
                 slug: Optional[str] = None,
                 # The card's top strip, "<theme>/cardDeco<n>"; picked on save()
                 decorator: Optional[str] = None,
                 # Timestamp fields
                 created_at: Optional[str] = None,
                 updated_at: Optional[str] = None):
        """
        Initialize a Catalog instance
        
        Args:
            name: Catalog name
            owner_user_id: id of the user who owns this catalog (users.id in the
                           application database). Replaced a free-text `owner`
                           column that predated the application having users at
                           all; ownership is now a reference, not a name someone
                           typed.
            description: Catalog description
            file_strategy: File writing strategy ('local', 'scp', 'ftp', 'box', 's3')
            target_directory: Target directory for file storage (prepended to destination paths)
            catalog_id: Database ID (auto-assigned when saved)
            
            SCP parameters:
                scp_username: Remote username
                scp_host: Remote host address
                scp_port: SSH port (default: 22)
            
            FTP parameters:
                ftp_host: FTP server address
                ftp_username: FTP username
                ftp_password: FTP password
                ftp_port: FTP port (default: 21)
            
            Box parameters:
                box_client_id: Box application client ID
                box_client_secret: Box application client secret
                box_access_token: Box access token
            
            S3 parameters:
                s3_bucket_name: S3 bucket name
                s3_aws_access_key_id: AWS access key ID
                s3_aws_secret_access_key: AWS secret access key
                s3_region_name: AWS region name
        """
        self.id = catalog_id
        self.name = name
        self.owner_user_id = owner_user_id
        self.description = description
        self.file_strategy = file_strategy.lower()
        self.target_directory = target_directory
        
        # SCP parameters
        self.scp_username = scp_username
        self.scp_host = scp_host
        self.scp_port = scp_port
        
        # FTP parameters
        self.ftp_host = ftp_host
        self.ftp_username = ftp_username
        self.ftp_password = ftp_password
        self.ftp_port = ftp_port
        
        # Box parameters
        self.box_client_id = box_client_id
        self.box_client_secret = box_client_secret
        self.box_access_token = box_access_token
        
        # S3 parameters
        self.s3_bucket_name = s3_bucket_name
        self.s3_aws_access_key_id = s3_aws_access_key_id
        self.s3_aws_secret_access_key = s3_aws_secret_access_key
        self.s3_region_name = s3_region_name

        # Face recognition metric ('cosine' or 'contextual'; default cosine)
        self.similarity_metric = similarity_metric or 'cosine'

        # Extra image sizes generated at ingestion, ON TOP of the mandatory
        # 1200px base (documentation/specs/file-sizes-spec.md section 7.2).
        #
        # EXTRAS ONLY. The floor lives in code, in utilities.IMAGE_RUNGS, so no
        # bad write and no hand-edited row can produce a catalog with no base
        # rung - which would not degrade this application but break it: 41
        # queries and every stored face coordinate are in that image's space.
        #
        # A list on the object, JSON in the column. The text form never leaves
        # this module.
        self.image_rungs = _parse_rungs(image_rungs)

        # How this catalog handles the files it ingests
        # (documentation/specs/file-ingestion-spec.md):
        #
        #   'copy'      the catalog owns a dated, renamed copy of every file.
        #   'reference' the files stay where the user put them; only derivatives
        #               (thumbnails, alternates, proxies, face crops) are written
        #               here, and file_contents.file_path is relative to
        #               source_root rather than to target_directory.
        #
        # Defaulting to 'copy' is what leaves every existing catalog behaving
        # exactly as it does today.
        self.ingest_mode = (ingest_mode or 'copy').lower()
        # Absolute path to the library being indexed. A mount point on THIS
        # machine, which is why it lives here and not with the catalog's data -
        # /Volumes/Photos on a laptop is /mnt/photos on a server.
        self.source_root = source_root

        # The catalog's URL segment, and its DNS label under the subdomain form
        # (albums-enhancement-spec.md section 3.1). A deployment is addressed by
        # the PAIR <catalog-slug>/<deployment-slug>, which is what makes both
        # halves unique with no cross-catalog scan: this one is unique because
        # catalog_meta is one table, and the other because album_deployments is
        # one table per catalog. Derived from the name on save() when absent.
        self.slug = slug

        # The artwork across the top of this catalog's card (see
        # DECORATOR_THEME). Picked at random by save() when absent, then kept.
        self.decorator = decorator

        # Timestamp fields
        self.created_at = created_at
        self.updated_at = updated_at
        
        # Validate file strategy
        valid_strategies = ['local', 'scp', 'ftp', 'box', 's3']
        if self.file_strategy not in valid_strategies:
            raise ValueError(f"Invalid file_strategy. Must be one of: {', '.join(valid_strategies)}")

        # 'move' is copy mode for everything the catalog stores; it differs only
        # in what an import does to the source afterwards (ingest-move-spec.md).
        if self.ingest_mode not in ('copy', 'reference', 'move'):
            raise ValueError("Invalid ingest_mode. Must be 'copy', 'reference' or 'move'")

        if self.ingest_mode == 'move' and self.file_strategy != 'local':
            # A move deletes the source only after checking the catalog's copy,
            # which has to be a local file to be checked.
            raise ValueError('Moving files into the catalog is only available with '
                             'the local file strategy.')

        if self.ingest_mode == 'reference':
            # source_root is deliberately NOT required here. A catalog directory
            # carried from another machine is registered before anyone knows
            # where its library is on THIS one, and it is perfectly usable in
            # that state - previews, people, events and albums all live in the
            # directory (file-ingestion-spec.md section 5.4). Requiring it here
            # would also make Catalog.get_by_id() raise on such a row, which is
            # the wrong place to discover it. The requirement belongs to
            # CREATING a new reference catalog, and lives in the API layer.
            if self.file_strategy != 'local':
                # The remote strategies exist to push bytes somewhere else. A
                # catalog that writes no bytes for originals has nothing for them
                # to do, and the combination has no meaning worth defining.
                raise ValueError('Indexing files in place is only available with '
                                 'the local file strategy.')
        
        # Initialize FileManager instance with target_directory as base_folder
        self.file_manager = FileManager(base_folder=self.target_directory)
    
    @classmethod
    def init_db(cls):
        """Initialize the database and create the catalog_meta table if it doesn't exist"""
        conn = sqlite3.connect(cls.DB_PATH)
        cursor = conn.cursor()
        
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS catalog_meta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                -- Who owns this catalog: a reference to users.id. No REFERENCES
                -- clause, because this application never enables
                -- PRAGMA foreign_keys and an inert constraint reads as a
                -- guarantee that is not there.
                owner_user_id INTEGER,
                description TEXT,
                file_strategy TEXT NOT NULL,
                target_directory TEXT,
                
                -- SCP parameters
                scp_username TEXT,
                scp_host TEXT,
                scp_port INTEGER DEFAULT 22,
                
                -- FTP parameters
                ftp_host TEXT,
                ftp_username TEXT,
                ftp_password TEXT,
                ftp_port INTEGER DEFAULT 21,
                
                -- Box parameters
                box_client_id TEXT,
                box_client_secret TEXT,
                box_access_token TEXT,
                
                -- S3 parameters
                s3_bucket_name TEXT,
                s3_aws_access_key_id TEXT,
                s3_aws_secret_access_key TEXT,
                s3_region_name TEXT,
                
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Migration: add similarity_metric column if it doesn't exist yet
        cursor.execute("PRAGMA table_info(catalog_meta)")
        existing_cols = {row[1] for row in cursor.fetchall()}
        if 'similarity_metric' not in existing_cols:
            cursor.execute(
                "ALTER TABLE catalog_meta ADD COLUMN similarity_metric TEXT NOT NULL DEFAULT 'cosine'"
            )

        # Extra image derivative sizes, as a JSON list of rung keys. DEFAULT
        # '[]' is the entire backward-compatibility story: every existing
        # catalog reads back as "the base rung only", which is exactly what it
        # has today, with no migration to run.
        #
        # JSON in a text column rather than a table: the list is short, read
        # whole, and joined to by nothing. A table would also need an explicit
        # cascade on catalog delete - this application never enables
        # PRAGMA foreign_keys, so a REFERENCES clause would be inert and read as
        # a guarantee that is not there.
        if 'image_rungs' not in existing_cols:
            cursor.execute(
                "ALTER TABLE catalog_meta ADD COLUMN image_rungs TEXT NOT NULL DEFAULT '[]'"
            )

        # Ingestion mode and the library it indexes. DEFAULT 'copy' is the whole
        # backward-compatibility story: every existing row reads back as the
        # behaviour it already has, with no migration to run.
        if 'ingest_mode' not in existing_cols:
            cursor.execute(
                "ALTER TABLE catalog_meta ADD COLUMN ingest_mode TEXT NOT NULL DEFAULT 'copy'"
            )
        if 'source_root' not in existing_cols:
            cursor.execute('ALTER TABLE catalog_meta ADD COLUMN source_root TEXT')

        # Ownership was a free-text `owner` column from before this application
        # had users. It is now a reference to users.id.
        #
        # Backfilled from catalog_access rather than by parsing the old strings:
        # that table already records who owns what, as a real user id, and it is
        # what every permission check reads. The lowest user id wins when a
        # catalog has several owners - arbitrary but deterministic, and any of
        # them is a correct answer.
        if 'owner_user_id' not in existing_cols:
            cursor.execute('ALTER TABLE catalog_meta ADD COLUMN owner_user_id INTEGER')
            try:
                cursor.execute('''
                    UPDATE catalog_meta SET owner_user_id = (
                        SELECT MIN(user_id) FROM catalog_access
                        WHERE catalog_access.catalog_id = catalog_meta.id
                          AND role = 'owner')
                    WHERE owner_user_id IS NULL
                ''')
                # Anything still unowned - a catalog nobody was granted access to
                # - goes to the first admin, so it is reachable rather than
                # orphaned.
                cursor.execute('''
                    UPDATE catalog_meta SET owner_user_id = (
                        SELECT MIN(id) FROM users WHERE is_admin = 1)
                    WHERE owner_user_id IS NULL
                ''')
                print(f'  catalog ownership backfilled: '
                      f'{cursor.execute("SELECT count(*) FROM catalog_meta WHERE owner_user_id IS NOT NULL").fetchone()[0]} '
                      f'catalog(s) now reference a user')
            except sqlite3.OperationalError as exc:
                # catalog_access / users are created by auth.install(), which may
                # not have run yet on a brand-new database. Nothing to backfill
                # in that case.
                print(f'  catalog ownership backfill skipped: {exc}')

        # The legacy column goes once its replacement is populated. Kept as a
        # separate step so a half-applied migration - column added, backfill
        # failed - never destroys the old values.
        if 'owner' in existing_cols and 'owner_user_id' in {
                r[1] for r in cursor.execute('PRAGMA table_info(catalog_meta)')}:
            unowned = cursor.execute(
                'SELECT count(*) FROM catalog_meta WHERE owner_user_id IS NULL').fetchone()[0]
            if unowned:
                print(f'  keeping the legacy owner column: {unowned} catalog(s) '
                      f'still have no owner_user_id')
            else:
                cursor.execute('ALTER TABLE catalog_meta DROP COLUMN owner')
                print('  dropped the legacy catalog_meta.owner column')

        # ── Catalog slug ──────────────────────────────────────────────────
        # The catalog's URL segment, added for album deployments
        # (albums-enhancement-spec.md section 3.1). Nullable ONLY so this
        # migration can add the column before the backfill fills it; a catalog
        # without a slug cannot host a deployment and is told so, rather than
        # producing a URL with an empty segment in it.
        #
        # The UNIQUE index is the entire uniqueness story for the catalog half
        # of a deployment URL. SQLite allows repeated NULLs in a unique index,
        # which is what lets an un-backfilled row sit here harmlessly.
        if 'slug' not in existing_cols:
            cursor.execute('ALTER TABLE catalog_meta ADD COLUMN slug TEXT')
        cursor.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS idx_catalog_slug ON catalog_meta(slug)')

        # Backfill from the name, oldest catalog first so the shortest slug goes
        # to the catalog that has been here longest - if two catalogs slugify the
        # same way, the newer one gets the numeric suffix.
        try:
            import slugs as _slugs
            taken = {r[0] for r in cursor.execute(
                'SELECT slug FROM catalog_meta WHERE slug IS NOT NULL') if r[0]}
            missing = cursor.execute(
                'SELECT id, name FROM catalog_meta WHERE slug IS NULL OR slug = \'\' '
                'ORDER BY id').fetchall()
            for cat_id, cat_name in missing:
                new_slug = _slugs.unique_slug(cat_name, taken, fallback=f'catalog-{cat_id}')
                cursor.execute('UPDATE catalog_meta SET slug = ? WHERE id = ?',
                               (new_slug, cat_id))
                taken.add(new_slug)
                print(f"  catalog slug backfilled: '{cat_name}' -> {new_slug}")
        except Exception as exc:
            # Never fatal. A catalog left without a slug is simply one that
            # cannot host a deployment yet, which the UI says plainly.
            print(f'  catalog slug backfill skipped: {exc}')

        # ── Card decorator ────────────────────────────────────────────────
        # Which image tops this catalog's card. Existing catalogs are given one
        # at random, once; new ones get theirs in save().
        if 'decorator' not in existing_cols:
            cursor.execute('ALTER TABLE catalog_meta ADD COLUMN decorator TEXT')
        for (cat_id,) in cursor.execute(
                "SELECT id FROM catalog_meta WHERE decorator IS NULL OR decorator = ''"
        ).fetchall():
            cursor.execute('UPDATE catalog_meta SET decorator = ? WHERE id = ?',
                           (random_decorator(), cat_id))
        # Picks made under another theme move to the current one, keeping
        # their number: the same card, not a re-roll.
        for (cat_id, old_pick) in cursor.execute(
                "SELECT id, decorator FROM catalog_meta WHERE decorator NOT LIKE ? || '/%'",
                (DECORATOR_THEME,)).fetchall():
            cursor.execute('UPDATE catalog_meta SET decorator = ? WHERE id = ?',
                           (f"{DECORATOR_THEME}/{old_pick.split('/', 1)[-1]}", cat_id))

        # ── Storage routes ────────────────────────────────────────────────
        # Named folders a catalog draws input files from
        # (documentation/specs/local-storage-spec.md).
        #
        # A route BELONGS TO ONE CATALOG. It is created in a catalog's context,
        # stays with it, and is removed when that catalog is deleted - the
        # logical row only, never the directory or a single file inside it.
        # Routes used to be installation-wide and linked to catalogs through a
        # join table; that let one folder feed several catalogs and made a route
        # outlive the catalog it was made for. See _migrate_routes_to_owner().
        #
        # They still live in the APPLICATION database rather than the catalog
        # one, because a route records a HOST path that is a fact about this
        # machine. A catalog directory is portable - carried to another machine
        # it brings its photos, events and albums - and a host path would be
        # wrong there. That is the same reason catalog_access lives here and is
        # deleted with the catalog rather than travelling with the folder.
        #
        # No REFERENCES clause: this application never enables
        # PRAGMA foreign_keys, so it would be inert. Deleting a catalog removes
        # its routes explicitly, the way delete() revokes access grants.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS storage_routes (
                id             INTEGER PRIMARY KEY AUTOINCREMENT,
                -- The owning catalog. Not nullable: a route with no catalog is
                -- the concept this replaced.
                catalog_id     INTEGER NOT NULL,
                name           TEXT NOT NULL,
                kind           TEXT NOT NULL DEFAULT 'local',
                -- Both paths, deliberately. The user thinks in
                -- /Users/izelpi/Pictures; this process can only open
                -- /storage/Pictures. Storing one and guessing the other is the
                -- bug migration_utils.resolve_catalog_dir() exists to paper
                -- over. host_path is nullable: a future non-local kind has none.
                host_path      TEXT,
                container_path TEXT NOT NULL,
                read_only      INTEGER NOT NULL DEFAULT 1,
                created_by     INTEGER,
                created_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at     TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                -- Per catalog, not globally: two catalogs may each have a
                -- route called "Camera", which they could not when the name
                -- was unique across the installation.
                UNIQUE (catalog_id, name)
            )
        ''')
        # The migration must run BEFORE anything references catalog_id: on an
        # existing database the CREATE TABLE above is a no-op and the column
        # does not exist until the rebuild has happened.
        _migrate_routes_to_owner(cursor)
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_storage_routes_catalog '
                       'ON storage_routes(catalog_id)')

        # Backup cards (backup-backrest-spec.md): here, not in the catalog's
        # own database, for the storage routes' reason - a card names a
        # repository and holds this installation's encrypted secrets.
        import backups
        backups.ensure_schema(cursor)

        # ── Fold reference catalogs' source_root into routes ──────────────────
        # catalog_meta.source_root (file-ingestion-spec.md) is a storage route
        # wearing a different hat: an absolute host path a catalog reads from,
        # with the same mount problem and the same validation. Give each one a
        # real route and link it, so there is one concept and one availability
        # check instead of two (local-storage-spec.md section 7).
        #
        # The column stays. It is still what _original_root() resolves against,
        # and rewriting every reference catalog's path resolution is a larger
        # change than this migration should make; the route is what the UI and
        # the availability checks use from here.
        try:
            unrouted = cursor.execute(
                "SELECT id, name, source_root FROM catalog_meta "
                "WHERE ingest_mode = 'reference' AND source_root IS NOT NULL "
                "AND id NOT IN (SELECT catalog_id FROM storage_routes)"
            ).fetchall()
            for cat_id, cat_name, root in unrouted:
                import storage_routes as _sr
                container = _sr.to_container_path(root) or root
                # This catalog's own route. Routes are per-catalog now, so a
                # second reference catalog pointing at the same folder gets its
                # own row rather than sharing one - which is the whole point:
                # deleting one catalog must not disturb the other.
                if cursor.execute(
                        'SELECT 1 FROM storage_routes '
                        'WHERE catalog_id = ? AND container_path = ?',
                        (cat_id, container)).fetchone():
                    continue
                route_name = f'{cat_name} library'
                suffix, candidate = 1, route_name
                while cursor.execute(
                        'SELECT 1 FROM storage_routes WHERE catalog_id = ? AND name = ?',
                        (cat_id, candidate)).fetchone():
                    suffix += 1
                    candidate = f'{route_name} ({suffix})'
                cursor.execute(
                    'INSERT INTO storage_routes '
                    '(catalog_id, name, kind, host_path, container_path) '
                    "VALUES (?, ?, 'local', ?, ?)",
                    (cat_id, candidate, root, container))
                print(f"  storage route created for reference catalog '{cat_name}'")
        except sqlite3.OperationalError as exc:
            # A brand-new database where catalog_meta has not gained its columns
            # yet. Nothing to migrate.
            print(f'  source_root migration skipped: {exc}')

        conn.commit()
        conn.close()
        print(f"Database initialized at {cls.DB_PATH}")
    
    def save(self) -> int:
        """
        Save the catalog to the database (insert or update)
        
        Returns:
            int: The catalog ID
        """
        if not self.decorator:
            self.decorator = random_decorator()

        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()
        
        if self.id is None:
            # Insert new catalog
            cursor.execute('''
                INSERT INTO catalog_meta (
                    name, owner_user_id, description, file_strategy, target_directory,
                    scp_username, scp_host, scp_port,
                    ftp_host, ftp_username, ftp_password, ftp_port,
                    box_client_id, box_client_secret, box_access_token,
                    s3_bucket_name, s3_aws_access_key_id, s3_aws_secret_access_key, s3_region_name,
                    similarity_metric, ingest_mode, source_root, slug, image_rungs,
                    decorator
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                self.name, self.owner_user_id, self.description, self.file_strategy, self.target_directory,
                self.scp_username, self.scp_host, self.scp_port,
                self.ftp_host, self.ftp_username, self.ftp_password, self.ftp_port,
                self.box_client_id, self.box_client_secret, self.box_access_token,
                self.s3_bucket_name, self.s3_aws_access_key_id,
                self.s3_aws_secret_access_key, self.s3_region_name,
                self.similarity_metric, self.ingest_mode, self.source_root, self.slug,
                _dump_rungs(self.image_rungs), self.decorator
            ))
            self.id = cursor.lastrowid
            print(f"Catalog '{self.name}' created with ID {self.id}")
        else:
            # Update existing catalog
            cursor.execute('''
                UPDATE catalog_meta SET
                    name = ?, owner_user_id = ?, description = ?, file_strategy = ?, target_directory = ?,
                    scp_username = ?, scp_host = ?, scp_port = ?,
                    ftp_host = ?, ftp_username = ?, ftp_password = ?, ftp_port = ?,
                    box_client_id = ?, box_client_secret = ?, box_access_token = ?,
                    s3_bucket_name = ?, s3_aws_access_key_id = ?,
                    s3_aws_secret_access_key = ?, s3_region_name = ?,
                    similarity_metric = ?, ingest_mode = ?, source_root = ?, slug = ?,
                    image_rungs = ?, decorator = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (
                self.name, self.owner_user_id, self.description, self.file_strategy, self.target_directory,
                self.scp_username, self.scp_host, self.scp_port,
                self.ftp_host, self.ftp_username, self.ftp_password, self.ftp_port,
                self.box_client_id, self.box_client_secret, self.box_access_token,
                self.s3_bucket_name, self.s3_aws_access_key_id,
                self.s3_aws_secret_access_key, self.s3_region_name,
                self.similarity_metric, self.ingest_mode, self.source_root, self.slug,
                _dump_rungs(self.image_rungs), self.decorator,
                self.id
            ))
            print(f"Catalog '{self.name}' (ID {self.id}) updated")
        
        conn.commit()
        conn.close()
        return self.id if self.id is not None else -1
    
    @classmethod
    def get_by_id(cls, catalog_id: int) -> Optional['Catalog']:
        """
        Retrieve a catalog by ID
        
        Args:
            catalog_id: The catalog ID
            
        Returns:
            Catalog instance or None if not found
        """
        conn = sqlite3.connect(cls.DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM catalog_meta WHERE id = ?', (catalog_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            catalog = cls._from_row(row)
            # Store timestamps
            catalog.created_at = row['created_at']
            catalog.updated_at = row['updated_at']
            return catalog
        return None
    
    @classmethod
    def get_by_name(cls, name: str) -> Optional['Catalog']:
        """
        Retrieve a catalog by name
        
        Args:
            name: The catalog name
            
        Returns:
            Catalog instance or None if not found
        """
        conn = sqlite3.connect(cls.DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM catalog_meta WHERE name = ?', (name,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            catalog = cls._from_row(row)
            # Store timestamps
            catalog.created_at = row['created_at']
            catalog.updated_at = row['updated_at']
            return catalog
        return None
    
    @classmethod
    def get_all(cls) -> List['Catalog']:
        """
        Retrieve all catalogs
        
        Returns:
            List of Catalog instances
        """
        conn = sqlite3.connect(cls.DB_PATH)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM catalog_meta ORDER BY name')
        rows = cursor.fetchall()
        conn.close()
        
        catalogs = []
        for row in rows:
            catalog = cls._from_row(row)
            # Store timestamps
            catalog.created_at = row['created_at']
            catalog.updated_at = row['updated_at']
            catalogs.append(catalog)
        return catalogs
    
    def delete(self) -> bool:
        """
        Remove the catalog from the registry, with the access grants that go
        with it.

        This is a DETACH, not a destroy: the target directory and its
        workflow_results.db are left untouched on purpose. Add the same path back
        later and every artifact returns at once - photos, people, face
        detections, similarity scores, events and their covers - because all of
        that lives in the catalog directory rather than here.

        Two things are exceptions. DEPLOYMENTS are removed before this runs, by
        app.delete_catalog: a deployment is a decision to publish rather than an
        artifact of the files, so detaching revokes it instead of leaving it to
        republish itself when the folder is added back. That is done in the
        caller because it is the endpoint that owns the published directories.

        The access grants are the second, and they are deleted rather than
        left behind. They live in THIS database keyed on catalog_id, so they
        cannot come back with the directory: re-adding it mints a new catalog id.
        Rows for a catalog that no longer exists are unreachable state that grows
        with every delete, and nothing removes it - catalog_access declares no
        foreign key on catalog_id, and this application never enables
        PRAGMA foreign_keys anyway, so an ON DELETE CASCADE would not run.
        Deleting them here is what the constraint would have done.

        Returns:
            bool: True if successful, False otherwise
        """
        if self.id is None:
            print("Cannot delete catalog: not saved to database")
            return False

        conn = sqlite3.connect(self.DB_PATH)
        cursor = conn.cursor()

        # One transaction: a catalog that is gone must never leave grants behind,
        # and grants must never be dropped for a catalog that is still there.
        cursor.execute('DELETE FROM catalog_access WHERE catalog_id = ?', (self.id,))
        revoked = cursor.rowcount
        # The app-database `workflows` table predates workflows moving into each
        # catalog's own database. Empty in practice; cleared for the same reason.
        try:
            cursor.execute('DELETE FROM workflows WHERE catalog_id = ?', (self.id,))
        except sqlite3.OperationalError:
            pass
        cursor.execute('DELETE FROM catalog_meta WHERE id = ?', (self.id,))
        conn.commit()
        conn.close()

        print(f"Catalog '{self.name}' (ID {self.id}) deleted"
              + (f", {revoked} access grant(s) revoked" if revoked else ""))
        self.id = None
        return True
    
    @classmethod
    def _from_row(cls, row: sqlite3.Row) -> 'Catalog':
        """Create a Catalog instance from a database row"""
        keys = row.keys()
        return cls(
            catalog_id=row['id'],
            name=row['name'],
            owner_user_id=(row['owner_user_id'] if 'owner_user_id' in keys else None),
            description=row['description'],
            file_strategy=row['file_strategy'],
            ingest_mode=(row['ingest_mode'] if 'ingest_mode' in keys else None),
            source_root=(row['source_root'] if 'source_root' in keys else None),
            slug=(row['slug'] if 'slug' in keys else None),
            decorator=(row['decorator'] if 'decorator' in keys else None),
            target_directory=row['target_directory'],
            scp_username=row['scp_username'],
            scp_host=row['scp_host'],
            scp_port=row['scp_port'],
            ftp_host=row['ftp_host'],
            ftp_username=row['ftp_username'],
            ftp_password=row['ftp_password'],
            ftp_port=row['ftp_port'],
            box_client_id=row['box_client_id'],
            box_client_secret=row['box_client_secret'],
            box_access_token=row['box_access_token'],
            s3_bucket_name=row['s3_bucket_name'],
            s3_aws_access_key_id=row['s3_aws_access_key_id'],
            s3_aws_secret_access_key=row['s3_aws_secret_access_key'],
            s3_region_name=row['s3_region_name'],
            similarity_metric=row['similarity_metric'] if 'similarity_metric' in keys else 'cosine',
            image_rungs=(row['image_rungs'] if 'image_rungs' in keys else None),
        )
    
    def get_write_strategy(self) -> FileWriteStrategy:
        """
        Get the appropriate FileWriteStrategy based on the catalog's file_strategy
        
        Returns:
            FileWriteStrategy instance configured with the catalog's parameters
            
        Raises:
            ValueError: If required parameters are missing for the file strategy
        """
        if self.file_strategy == 'local':
            return LocalCopyStrategy()
        
        elif self.file_strategy == 'scp':
            if not self.scp_username or not self.scp_host:
                raise ValueError("SCP strategy requires username and host")
            # Type assertions after validation
            assert self.scp_username is not None
            assert self.scp_host is not None
            return SCPStrategy(
                username=self.scp_username,
                host=self.scp_host,
                port=self.scp_port
            )
        
        elif self.file_strategy == 'ftp':
            if not all([self.ftp_host, self.ftp_username, self.ftp_password]):
                raise ValueError("FTP strategy requires host, username, and password")
            # Type assertions after validation
            assert self.ftp_host is not None
            assert self.ftp_username is not None
            assert self.ftp_password is not None
            return FTPStrategy(
                host=self.ftp_host,
                username=self.ftp_username,
                password=self.ftp_password,
                port=self.ftp_port
            )
        
        elif self.file_strategy == 'box':
            if not all([self.box_client_id, self.box_client_secret]):
                raise ValueError("Box strategy requires client_id and client_secret")
            # Type assertions after validation
            assert self.box_client_id is not None
            assert self.box_client_secret is not None
            return BoxStrategy(
                client_id=self.box_client_id,
                client_secret=self.box_client_secret,
                access_token=self.box_access_token
            )
        
        elif self.file_strategy == 's3':
            if not self.s3_bucket_name:
                raise ValueError("S3 strategy requires bucket_name")
            return S3Strategy(
                bucket_name=self.s3_bucket_name,
                aws_access_key_id=self.s3_aws_access_key_id,
                aws_secret_access_key=self.s3_aws_secret_access_key,
                region_name=self.s3_region_name
            )
        
        else:
            raise ValueError(f"Unknown file strategy: {self.file_strategy}")
    
    def get_file_manager(self) -> FileManager:
        """
        Get a FileManager instance configured with this catalog's target_directory
        
        Returns:
            FileManager instance with target_directory set
        """
        return FileManager(base_folder=self.target_directory)
    
    def to_dict(self) -> Dict[str, Any]:
        """
        Convert catalog to dictionary representation
        
        Returns:
            Dictionary with catalog data
        """
        return {
            'id': self.id,
            'name': self.name,
            'owner_user_id': self.owner_user_id,
            'description': self.description,
            'file_strategy': self.file_strategy,
            'ingest_mode': self.ingest_mode,
            'source_root': self.source_root,
            'slug': self.slug,
            'decorator': self.decorator,
            'target_directory': self.target_directory,
            'scp_username': self.scp_username,
            'scp_host': self.scp_host,
            'scp_port': self.scp_port,
            'ftp_host': self.ftp_host,
            'ftp_username': self.ftp_username,
            'ftp_password': '***' if self.ftp_password else None,  # Mask password
            'ftp_port': self.ftp_port,
            'box_client_id': self.box_client_id,
            'box_client_secret': '***' if self.box_client_secret else None,  # Mask secret
            'box_access_token': '***' if self.box_access_token else None,  # Mask token
            's3_bucket_name': self.s3_bucket_name,
            's3_aws_access_key_id': self.s3_aws_access_key_id,
            's3_aws_secret_access_key': '***' if self.s3_aws_secret_access_key else None,  # Mask key
            's3_region_name': self.s3_region_name,
            'similarity_metric': self.similarity_metric,
            # A LIST, never the stored JSON text. Callers should not have to
            # know how this is persisted.
            'image_rungs': list(self.image_rungs),
            'created_at': self.created_at,
            'updated_at': self.updated_at
        }
    
    def __repr__(self) -> str:
        """String representation of the catalog"""
        return f"Catalog(id={self.id}, name='{self.name}', owner_user_id={self.owner_user_id}, file_strategy='{self.file_strategy}')"
    
    def __str__(self) -> str:
        """Human-readable string representation"""
        return f"Catalog '{self.name}' (Owner: user {self.owner_user_id}, Strategy: {self.file_strategy})"
    
    # FileManager delegation methods
    def write_file(self, source: str, destination: str,
                   strategy: Optional[FileWriteStrategy] = None) -> bool:
        """
        Write file from source to destination using specified strategy
        Delegates to the FileManager instance.
        
        Args:
            source: Source file path
            destination: Destination path
            strategy: FileWriteStrategy instance (defaults to catalog's configured strategy)
            
        Returns:
            bool: True if successful, False otherwise
        """
        if strategy is None:
            strategy = self.get_write_strategy()
        return self.file_manager.write_file(source, destination, strategy)
    
    def write_file_local(self, source: str, destination: str) -> bool:
        """
        Write file locally using system copy
        Delegates to the FileManager instance.
        
        Args:
            source: Source file path
            destination: Destination path or directory
            
        Returns:
            bool: True if successful, False otherwise
        """
        return self.file_manager.write_file_local(source, destination)
    
    def write_file_scp(self, source: str, destination: str,
                       username: str, host: str, port: int = 22) -> bool:
        """
        Write file to remote location via SCP
        Delegates to the FileManager instance.
        
        Args:
            source: Local source file path
            destination: Remote destination path
            username: Remote username
            host: Remote host address
            port: SSH port (default: 22)
            
        Returns:
            bool: True if successful, False otherwise
        """
        return self.file_manager.write_file_scp(source, destination, username, host, port)
    
    def write_file_ftp(self, source: str, destination: str,
                       host: str, username: str, password: str, port: int = 21) -> bool:
        """
        Write file to remote location via FTP
        Delegates to the FileManager instance.
        
        Args:
            source: Local source file path
            destination: Remote destination path
            host: FTP server address
            username: FTP username
            password: FTP password
            port: FTP port (default: 21)
            
        Returns:
            bool: True if successful, False otherwise
        """
        return self.file_manager.write_file_ftp(source, destination, host, username, password, port)
    
    def write_file_box(self, source: str, destination: str,
                       client_id: str, client_secret: str,
                       access_token: Optional[str] = None) -> bool:
        """
        Write file to Box cloud storage
        Delegates to the FileManager instance.
        
        Args:
            source: Local source file path
            destination: Remote destination path (folder_id/filename)
            client_id: Box application client ID
            client_secret: Box application client secret
            access_token: Box access token
            
        Returns:
            bool: True if successful, False otherwise
        """
        return self.file_manager.write_file_box(source, destination, client_id, client_secret, access_token)
    
    def write_file_s3(self, source: str, destination: str,
                      bucket_name: str, aws_access_key_id: Optional[str] = None,
                      aws_secret_access_key: Optional[str] = None,
                      region_name: Optional[str] = None) -> bool:
        """
        Write file to Amazon S3 bucket
        Delegates to the FileManager instance.
        
        Args:
            source: Local source file path
            destination: S3 object key (path within bucket)
            bucket_name: S3 bucket name
            aws_access_key_id: AWS access key ID (optional)
            aws_secret_access_key: AWS secret access key (optional)
            region_name: AWS region name (optional, e.g., 'us-east-1')
            
        Returns:
            bool: True if successful, False otherwise
        """
        return self.file_manager.write_file_s3(source, destination, bucket_name,
                                               aws_access_key_id, aws_secret_access_key, region_name)
    
    def read_file(self, filepath: str, mode: str = 'r', encoding: str = 'utf-8') -> Optional[str]:
        """
        Read file contents
        Delegates to the FileManager instance.
        
        Args:
            filepath: Path to file to read
            mode: File open mode ('r' for text, 'rb' for binary)
            encoding: Text encoding (default: utf-8)
            
        Returns:
            File contents as string (text mode) or bytes (binary mode), None if error
        """
        return self.file_manager.read_file(filepath, mode, encoding)
    
    def read_file_lines(self, filepath: str, encoding: str = 'utf-8') -> Optional[list]:
        """
        Read file contents as list of lines
        Delegates to the FileManager instance.
        
        Args:
            filepath: Path to file to read
            encoding: Text encoding (default: utf-8)
            
        Returns:
            List of lines, None if error
        """
        return self.file_manager.read_file_lines(filepath, encoding)
    
    def _get_file_type(self, filepath: str) -> str:
        """
        Determine the file type category based on file extension
        
        Args:
            filepath: Path to the file
            
        Returns:
            str: File type category ('Images', 'Videos', or 'Others')
        """
        # Define file extensions for each category
        image_extensions = {
            '.jpg', '.jpeg', '.png', '.gif', '.bmp', '.tiff', '.tif',
            '.webp', '.svg', '.ico', '.heic', '.heif', '.raw', '.cr2',
            '.cr3', '.nef', '.arw', '.dng'
        }
        
        # Shared with the ingestion workflows via media_types, so a file filed
        # under Videos/ is always one the workflows will make playable.
        video_extensions = VIDEO_EXTENSIONS
        
        # Get file extension (lowercase for case-insensitive comparison)
        _, ext = os.path.splitext(filepath)
        ext = ext.lower()
        
        # Categorize based on extension
        if ext in image_extensions:
            return 'Images'
        elif ext in video_extensions:
            return 'Videos'
        else:
            return 'Others'
    
    def calculateDir(self, filepath: str, creation_timestamp: Optional[str] = None,
                     type_from: Optional[str] = None) -> str:
        """
        Calculate the target directory path for a file based on its type and creation date.
        
        The directory structure will be:
        target_directory/FileType/YYYY/MM/DD
        
        Where:
        - FileType is one of: Images, Videos, Others
        - YYYY is the 4-digit year
        - MM is the 2-digit zero-padded month
        - DD is the 2-digit zero-padded day
        
        Args:
            filepath: Path to the source file
            creation_timestamp: Optional 14-digit timestamp string (YYYYMMDDHHmmss)
                              If provided, this will be used instead of file system timestamps
            
        Returns:
            str: Complete directory path where the file should be stored
            
        Raises:
            FileNotFoundError: If the source file doesn't exist
        """
        # Verify file exists
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Source file not found: {filepath}")
        
        # Get file type category. `type_from` is the name the file is stored
        # under when that differs from the source's - a video that arrived
        # named .JPG is filed under Videos/ by its corrected name.
        file_type = self._get_file_type(type_from or filepath)
        
        # Determine year, month, day from timestamp or file system
        # Initialize with current date as fallback
        now = datetime.now()
        year: str = f"{now.year:04d}"
        month: str = f"{now.month:02d}"
        day: str = f"{now.day:02d}"
        
        # Try to use provided timestamp first
        if creation_timestamp:
            # Parse the 14-digit timestamp: YYYYMMDDHHmmss
            try:
                if len(creation_timestamp) != 14:
                    raise ValueError(f"Timestamp must be 14 digits, got {len(creation_timestamp)}")
                year = creation_timestamp[0:4]
                month = creation_timestamp[4:6]
                day = creation_timestamp[6:8]
            except (IndexError, ValueError) as e:
                print(f"Warning: Invalid timestamp format '{creation_timestamp}', falling back to file system: {e}")
                creation_timestamp = None
        
        # Fall back to file system if timestamp not provided or invalid
        if not creation_timestamp:
            # Get file creation time (or modification time as fallback)
            try:
                # Try to get creation time (works on Windows and some Unix systems)
                stat_info = os.stat(filepath)
                # Use birth time if available (macOS), otherwise use modification time
                if hasattr(stat_info, 'st_birthtime'):
                    creation_time = stat_info.st_birthtime
                else:
                    # Fallback to modification time on systems without birth time
                    creation_time = stat_info.st_mtime
                
                # Convert to datetime object
                creation_date = datetime.fromtimestamp(creation_time)
                
                # Format date components with zero padding
                year = f"{creation_date.year:04d}"
                month = f"{creation_date.month:02d}"
                day = f"{creation_date.day:02d}"
            except Exception as e:
                print(f"Warning: Could not get file creation time, using current time: {e}")
                # Keep the initialized values (current time)
        
        # Build the directory path
        if self.target_directory:
            dir_path = os.path.join(
                self.target_directory,
                file_type,
                year,
                month,
                day
            )
        else:
            # If no target_directory, use relative path
            dir_path = os.path.join(
                file_type,
                year,
                month,
                day
            )
        
        return dir_path


def ensure_schema():
    """
    Create or migrate the application database.

    Called explicitly at startup, NOT at import. Importing a module must not
    create a database:

      * it made `python -c "import app"` - the image's own smoke test, whose
        whole job is to prove the source is present and importable - depend on
        writable storage, so a permissions problem on a fresh Linux host arrived
        as "cannot import app.py" with a hint about ModuleNotFoundError;
      * every migration script, every workflow subprocess and every unit test
        that imports catalog.py wrote to whatever DB_PATH happened to point at,
        which is why the tests have to redirect DB_PATH *before* the import.

    Idempotent, so calling it from more than one entry point is fine.
    """
    Catalog.init_db()


# Import-time initialisation, kept behind an environment variable for anything
# that still relies on it. Off by default: see ensure_schema() above.
if os.environ.get('AIRLOCK_INIT_DB_ON_IMPORT') == '1':
    ensure_schema()


# Example usage
if __name__ == "__main__":
    print("=== Catalog Management System ===\n")
    
    # Example 1: Create a local storage catalog
    print("Example 1: Creating local storage catalog")
    local_catalog = Catalog(
        name="My Local Backup",
        owner_user_id=1,
        description="Local backup storage for development files",
        file_strategy="local",
        target_directory="/backups/local"
    )
    local_catalog.save()
    print(f"Created: {local_catalog}\n")
    
    # Example 2: Create an S3 storage catalog
    print("Example 2: Creating S3 storage catalog")
    s3_catalog = Catalog(
        name="AWS Production Storage",
        owner_user_id=2,
        description="Production S3 bucket for application data",
        file_strategy="s3",
        target_directory="production/data",
        s3_bucket_name="my-production-bucket",
        s3_region_name="us-east-1"
    )
    s3_catalog.save()
    print(f"Created: {s3_catalog}\n")
    
    # Example 3: Create an SCP storage catalog
    print("Example 3: Creating SCP storage catalog")
    scp_catalog = Catalog(
        name="Remote Server Backup",
        owner_user_id=1,
        description="Remote server for backup files",
        file_strategy="scp",
        target_directory="/remote/backups",
        scp_username="admin",
        scp_host="backup.example.com",
        scp_port=22
    )
    scp_catalog.save()
    print(f"Created: {scp_catalog}\n")
    
    # Example 4: Retrieve all catalogs
    print("Example 4: Retrieving all catalogs")
    all_catalogs = Catalog.get_all()
    for catalog in all_catalogs:
        print(f"  - {catalog}")
    print()
    
    
    # Example 6: Get a catalog by name and use its FileManager
    print("Example 6: Using catalog's FileManager")
    catalog = Catalog.get_by_name("My Local Backup")
    if catalog:
        file_manager = catalog.get_file_manager()
        strategy = catalog.get_write_strategy()
        print(f"  Retrieved catalog: {catalog}")
        print(f"  Target directory: {catalog.target_directory}")
        print(f"  FileManager base_folder: {file_manager.base_folder}")
        print(f"  Strategy type: {type(strategy).__name__}")
    print()
    
    print("Examples complete. Check 'catalogs.db' for stored data.")
