#!/usr/bin/env python3
"""
Migration script to add exif_data table and update file_contents table
"""
import sqlite3
import os

def migrate():
    db_path = 'workflows.db'
    
    if not os.path.exists(db_path):
        print(f"Database {db_path} not found. Skipping migration.")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    print("Starting migration to add EXIF data support...")
    
    try:
        # Check if exif_data table already exists
        cursor.execute("""
            SELECT name FROM sqlite_master 
            WHERE type='table' AND name='exif_data'
        """)
        
        if cursor.fetchone():
            print("exif_data table already exists. Skipping table creation.")
        else:
            # Create exif_data table
            print("Creating exif_data table...")
            cursor.execute('''
                CREATE TABLE exif_data (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    catalog_id INTEGER NOT NULL,
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
                    FOREIGN KEY (catalog_id) REFERENCES catalog_meta (id) ON DELETE CASCADE,
                    FOREIGN KEY (file_id) REFERENCES file_contents (id) ON DELETE CASCADE
                )
            ''')
            print("✓ exif_data table created successfully")
        
        # Check if file_contents has exif_data_id column
        cursor.execute("PRAGMA table_info(file_contents)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if 'exif_data_id' in columns:
            print("exif_data_id column already exists in file_contents. Skipping column addition.")
        else:
            # Add exif_data_id column to file_contents
            print("Adding exif_data_id column to file_contents table...")
            cursor.execute('''
                ALTER TABLE file_contents 
                ADD COLUMN exif_data_id INTEGER 
                REFERENCES exif_data(id) ON DELETE SET NULL
            ''')
            print("✓ exif_data_id column added successfully")
        
        conn.commit()
        print("\n✓ Migration completed successfully!")
        
    except Exception as e:
        conn.rollback()
        print(f"\n✗ Migration failed: {e}")
        raise
    finally:
        conn.close()

if __name__ == '__main__':
    migrate()

# Made with Bob
