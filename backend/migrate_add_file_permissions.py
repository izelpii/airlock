import sqlite3
import os

def migrate_database(db_path='workflows.db'):
    """
    Migration script to add file_permissions column to file_contents table
    """
    if not os.path.exists(db_path):
        print(f"Database not found at {db_path}")
        return
    
    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # Check if the column already exists
        cursor.execute("PRAGMA table_info(file_contents)")
        columns = [column[1] for column in cursor.fetchall()]
        
        if 'file_permissions' not in columns:
            print("Adding file_permissions column to file_contents table...")
            cursor.execute('''
                ALTER TABLE file_contents 
                ADD COLUMN file_permissions TEXT
            ''')
            conn.commit()
            print("Migration completed successfully!")
        else:
            print("file_permissions column already exists. No migration needed.")
    
    except Exception as e:
        print(f"Error during migration: {str(e)}")
        conn.rollback()
    
    finally:
        conn.close()

if __name__ == '__main__':
    # Migrate the main database
    migrate_database('workflows.db')
    print("\nMigration script completed.")

# Made with Bob
