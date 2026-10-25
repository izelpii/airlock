"""
Migration script to add state, country, lat, and long fields to user_settings table
"""
import sqlite3
import os

def migrate():
    # Get the project root directory
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    db_path = os.path.join(project_root, 'database', 'catalogs.db')
    
    print(f"Migrating database: {db_path}")
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    # Check if table exists
    cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_settings'")
    table_exists = cursor.fetchone() is not None
    
    if not table_exists:
        print("Creating user_settings table...")
        cursor.execute('''
            CREATE TABLE user_settings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT,
                address TEXT,
                city TEXT,
                state TEXT,
                country TEXT,
                lat REAL,
                long REAL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Insert default user
        cursor.execute('''
            INSERT INTO user_settings (name, email, address, city, state, country, lat, long)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ''', ('Default User', '', '', '', '', '', None, None))
        conn.commit()
        print("✓ Created user_settings table with new columns")
        conn.close()
        print("\n✓ Migration completed successfully!")
        return
    
    # Check if columns already exist
    cursor.execute("PRAGMA table_info(user_settings)")
    columns = [row[1] for row in cursor.fetchall()]
    
    # Add state column if it doesn't exist
    if 'state' not in columns:
        print("Adding 'state' column...")
        cursor.execute('ALTER TABLE user_settings ADD COLUMN state TEXT')
        print("✓ Added 'state' column")
    else:
        print("'state' column already exists")
    
    # Add country column if it doesn't exist
    if 'country' not in columns:
        print("Adding 'country' column...")
        cursor.execute('ALTER TABLE user_settings ADD COLUMN country TEXT')
        print("✓ Added 'country' column")
    else:
        print("'country' column already exists")
    
    # Add lat column if it doesn't exist
    if 'lat' not in columns:
        print("Adding 'lat' column...")
        cursor.execute('ALTER TABLE user_settings ADD COLUMN lat REAL')
        print("✓ Added 'lat' column")
    else:
        print("'lat' column already exists")
    
    # Add long column if it doesn't exist
    if 'long' not in columns:
        print("Adding 'long' column...")
        cursor.execute('ALTER TABLE user_settings ADD COLUMN long REAL')
        print("✓ Added 'long' column")
    else:
        print("'long' column already exists")
    
    conn.commit()
    conn.close()
    
    print("\n✓ Migration completed successfully!")

if __name__ == '__main__':
    migrate()
