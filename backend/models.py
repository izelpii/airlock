import sqlite3
import json
import os
from datetime import datetime
from typing import List, Dict, Optional

class Database:
    """Main database for workflows and catalog metadata"""
    def __init__(self, db_path: str = 'workflows.db'):
        self.db_path = db_path
        # Ensure the directory exists
        db_dir = os.path.dirname(self.db_path)
        if db_dir and not os.path.exists(db_dir):
            os.makedirs(db_dir, exist_ok=True)
        self.init_db()
    
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_db(self):
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Workflows table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS workflows (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                description TEXT,
                source_directory TEXT NOT NULL,
                catalog_id INTEGER NOT NULL,
                python_script TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (catalog_id) REFERENCES catalog_meta (id) ON DELETE CASCADE
            )
        ''')
        
        # Catalogs metadata table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS catalog_meta (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                owner TEXT NOT NULL,
                description TEXT,
                target_directory TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()

class CatalogDatabase:
    """Catalog-specific database for execution results and file tracking"""
    def __init__(self, target_directory: str):
        self.target_directory = target_directory
        self.db_path = os.path.join(target_directory, 'workflow_results.db')
        # Ensure the directory exists
        os.makedirs(target_directory, exist_ok=True)
        self.init_db()
    
    def get_connection(self):
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_db(self):
        """Initialize catalog-specific tables"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
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
                error_message TEXT
            )
        ''')
        
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
                added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (exif_data_id) REFERENCES exif_data (id) ON DELETE SET NULL
            )
        ''')
        
        # File operations table for tracking all file operations
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS file_operations (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                file_id INTEGER,
                operation_type TEXT NOT NULL,
                source_path TEXT,
                target_path TEXT,
                file_size INTEGER,
                status TEXT NOT NULL,
                details TEXT,
                operation_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
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
                extracted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (file_id) REFERENCES file_contents (id) ON DELETE CASCADE
            )
        ''')
        
        
        # Catalog metadata table (stores catalog info locally)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS catalog_meta (
                id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                owner TEXT NOT NULL,
                description TEXT,
                target_directory TEXT NOT NULL,
                created_at TIMESTAMP,
                updated_at TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()

class WorkflowModel:
    def __init__(self, db: Database):
        self.db = db
    
    def create(self, name: str, description: str, source_directory: str,
               catalog_id: int, python_script: str) -> int:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workflows (name, description, source_directory,
                                 catalog_id, python_script)
            VALUES (?, ?, ?, ?, ?)
        ''', (name, description, source_directory, catalog_id, python_script))
        workflow_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return workflow_id
    
    def get_all(self) -> List[Dict]:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM workflows ORDER BY created_at DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_by_id(self, workflow_id: int) -> Optional[Dict]:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM workflows WHERE id = ?', (workflow_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def update(self, workflow_id: int, **kwargs):
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        fields = []
        values = []
        for key, value in kwargs.items():
            if key in ['name', 'description', 'source_directory',
                      'catalog_id', 'python_script']:
                fields.append(f"{key} = ?")
                values.append(value)
        
        if fields:
            values.append(workflow_id)
            query = f"UPDATE workflows SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            cursor.execute(query, values)
            conn.commit()
        
        conn.close()
    
    def delete(self, workflow_id: int):
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM workflows WHERE id = ?', (workflow_id,))
        conn.commit()
        conn.close()

class ExecutionModel:
    """Model for workflow executions in catalog-specific databases"""
    def __init__(self, catalog_db: CatalogDatabase):
        self.catalog_db = catalog_db
    
    def create(self, workflow_id: int, workflow_name: str) -> int:
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO workflow_executions (workflow_id, workflow_name, status)
            VALUES (?, ?, 'running')
        ''', (workflow_id, workflow_name))
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
                error_message: Optional[str] = None, skipped_files: int = 0):
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE workflow_executions
            SET status = ?, completed_at = CURRENT_TIMESTAMP, error_message = ?, skipped_files = ?
            WHERE id = ?
        ''', (status, error_message, skipped_files, execution_id))
        conn.commit()
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
    
    def get_by_id(self, execution_id: int) -> Optional[Dict]:
        conn = self.catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM workflow_executions WHERE id = ?',
                      (execution_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

# Made with Bob


class CatalogModel:
    def __init__(self, db: Database):
        self.db = db
    
    def create(self, name: str, owner: str, description: str, target_directory: str) -> int:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO catalog_meta (name, owner, description, target_directory)
            VALUES (?, ?, ?, ?)
        ''', (name, owner, description, target_directory))
        catalog_id = cursor.lastrowid
        conn.commit()
        conn.close()
        
        # Initialize catalog-specific database
        catalog_db = CatalogDatabase(target_directory)
        catalog_conn = catalog_db.get_connection()
        catalog_cursor = catalog_conn.cursor()
        catalog_cursor.execute('''
            INSERT OR REPLACE INTO catalog_meta (id, name, owner, description, target_directory, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        ''', (catalog_id, name, owner, description, target_directory))
        catalog_conn.commit()
        catalog_conn.close()
        
        return catalog_id
    
    def get_catalog_db(self, catalog_id: int) -> Optional[CatalogDatabase]:
        """Get catalog-specific database instance"""
        catalog = self.get_by_id(catalog_id)
        if not catalog:
            return None
        return CatalogDatabase(catalog['target_directory'])
    
    def get_all(self) -> List[Dict]:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM catalog_meta ORDER BY created_at DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_by_id(self, catalog_id: int) -> Optional[Dict]:
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM catalog_meta WHERE id = ?', (catalog_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def update(self, catalog_id: int, **kwargs):
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        fields = []
        values = []
        for key, value in kwargs.items():
            if key in ['name', 'owner', 'description', 'target_directory']:
                fields.append(f"{key} = ?")
                values.append(value)
        
        if fields:
            values.append(catalog_id)
            query = f"UPDATE catalog_meta SET {', '.join(fields)}, updated_at = CURRENT_TIMESTAMP WHERE id = ?"
            cursor.execute(query, values)
            conn.commit()
        
        conn.close()
    
    def delete(self, catalog_id: int):
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM catalog_meta WHERE id = ?', (catalog_id,))
        conn.commit()
        conn.close()
    
    def add_file(self, catalog_id: int, file_path: str, file_name: str,
                 file_size: int, file_type: str, file_permissions: Optional[str] = None,
                 sha256_hash: Optional[str] = None, exif_data_id: Optional[int] = None):
        """Add file to catalog-specific database"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            raise Exception(f"Catalog not found: {catalog_id}")
        
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO file_contents (file_path, file_name, file_size, file_type, file_permissions, sha256_hash, exif_data_id)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (file_path, file_name, file_size, file_type, file_permissions, sha256_hash, exif_data_id))
        file_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return file_id
    
    def file_exists_by_hash(self, catalog_id: int, sha256_hash: str) -> Optional[Dict]:
        """Check if a file with the same hash already exists in the catalog"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            return None
        
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM file_contents
            WHERE sha256_hash = ?
            LIMIT 1
        ''', (sha256_hash,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def file_exists(self, catalog_id: int, file_path: str) -> bool:
        """Check if a file already exists in the catalog by path"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            return False
        
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) as count FROM file_contents
            WHERE file_path = ?
        ''', (file_path,))
        row = cursor.fetchone()
        conn.close()
        return row['count'] > 0 if row else False
    
    def add_file_operation(self, catalog_id: int, file_id: Optional[int],
                          operation_type: str, source_path: Optional[str],
                          target_path: Optional[str], file_size: Optional[int],
                          status: str, details: Optional[str] = None):
        """Add a file operation record to catalog-specific database"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            raise Exception(f"Catalog not found: {catalog_id}")
        
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO file_operations (file_id, operation_type, source_path, target_path, file_size, status, details)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (file_id, operation_type, source_path, target_path, file_size, status, details))
        operation_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return operation_id
    
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
                ed.raw_exif_json
            FROM file_contents fc
            LEFT JOIN exif_data ed ON fc.id = ed.file_id
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
    
    def add_exif_data(self, catalog_id: int, file_id: int, file_name: str,
                     creation_date: Optional[str] = None, latitude: Optional[float] = None,
                     longitude: Optional[float] = None, altitude: Optional[float] = None,
                     camera_make: Optional[str] = None, camera_model: Optional[str] = None,
                     lens_model: Optional[str] = None, focal_length: Optional[str] = None,
                     aperture: Optional[str] = None, iso: Optional[str] = None,
                     shutter_speed: Optional[str] = None, flash: Optional[str] = None,
                     orientation: Optional[int] = None, width: Optional[int] = None,
                     height: Optional[int] = None, raw_exif_json: Optional[str] = None):
        """Add EXIF data to catalog-specific database"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            raise Exception(f"Catalog not found: {catalog_id}")
        
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO exif_data
            (file_id, file_name, creation_date, latitude, longitude,
             altitude, camera_make, camera_model, lens_model, focal_length,
             aperture, iso, shutter_speed, flash, orientation, width, height, raw_exif_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (file_id, file_name, creation_date, latitude, longitude,
              altitude, camera_make, camera_model, lens_model, focal_length,
              aperture, iso, shutter_speed, flash, orientation, width, height, raw_exif_json))
        exif_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return exif_id
    
    def update_file_exif_link(self, catalog_id: int, file_id: int, exif_data_id: int):
        """Update file_contents record to link to exif_data"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            raise Exception(f"Catalog not found: {catalog_id}")
        
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE file_contents
            SET exif_data_id = ?
            WHERE id = ?
        ''', (exif_data_id, file_id))
        conn.commit()
        conn.close()
    
    def get_exif_data(self, catalog_id: int) -> List[Dict]:
        """Get all EXIF data from catalog-specific database"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            return []
        
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM exif_data
            ORDER BY extracted_at DESC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def get_exif_by_file_id(self, catalog_id: int, file_id: int) -> Optional[Dict]:
        """Get EXIF data for a specific file"""
        catalog_db = self.get_catalog_db(catalog_id)
        if not catalog_db:
            return None
        
        conn = catalog_db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM exif_data
            WHERE file_id = ?
        ''', (file_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None
    
    def get_operations(self, catalog_id: int) -> List[Dict]:
        """Get operations - kept for backward compatibility, returns empty list"""
        return []

# Made with Bob
