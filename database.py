"""
Database module for MySQL Agent.
Handles database connection and operations.
"""

import time
import sys
from typing import Optional
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from langchain_community.utilities.sql_database import SQLDatabase

from config import DB_CONFIG, MAX_RETRIES, RETRY_DELAY

class DatabaseManager:
    """Manages database connections and operations."""
    
    def __init__(self):
        self.db: Optional[SQLDatabase] = None
        self.engine = None
        
    def connect(self) -> SQLDatabase:
        """
        Creates a connection to the MySQL database with enhanced error handling and retries.
        
        Returns:
            SQLDatabase: A connection to our data realm
            
        Raises:
            SystemExit: If the connection cannot be established after MAX_RETRIES attempts
        """
        for attempt in range(MAX_RETRIES):
            try:
                # Create SQLAlchemy engine with connection pooling and timeouts
                connection_string = (
                    f"mysql+pymysql://{DB_CONFIG['username']}:{DB_CONFIG['password']}@"
                    f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
                )
                
                self.engine = create_engine(
                    connection_string,
                    pool_size=DB_CONFIG['pool_size'],
                    max_overflow=DB_CONFIG['max_overflow'],
                    pool_timeout=DB_CONFIG['pool_timeout'],
                    pool_recycle=DB_CONFIG['pool_recycle'],
                    connect_args={
                        "connect_timeout": DB_CONFIG['connect_timeout'],
                        "read_timeout": DB_CONFIG['read_timeout'],
                        "write_timeout": DB_CONFIG['write_timeout'],
                    }
                )
                
                # Test connection
                with self.engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                    conn.commit()
                
                # Create SQLDatabase instance with enhanced configuration
                self.db = SQLDatabase.from_uri(
                    connection_string,
                    include_tables=None,  # Include all tables
                    sample_rows_in_table_info=3,  # Include sample data
                    max_string_length=1000  # Truncate long strings
                )
                
                print("🌟 Successfully connected to the database!")
                return self.db
                
            except SQLAlchemyError as e:
                if attempt < MAX_RETRIES - 1:
                    print(f"🌀 Attempt {attempt + 1}/{MAX_RETRIES} to connect failed: {e}")
                    print(f"⏳ Waiting {RETRY_DELAY} seconds before trying again...")
                    time.sleep(RETRY_DELAY)
                else:
                    print(f"❌ Failed to connect to database after {MAX_RETRIES} attempts: {e}")
                    sys.exit(1)
    
    def get_database_stats(self) -> None:
        """
        Gets and displays statistics about the database tables.
        """
        if not self.engine:
            print("❌ Error: Database not connected")
            return
            
        try:
            print("\n📈 Database Statistics:")
            
            with self.engine.connect() as conn:
                # Get all tables
                result = conn.execute(text("SHOW TABLES"))
                tables = [row[0] for row in result]
                
                if not tables:
                    print("  No tables found in the database.")
                    return
                    
                # Get row count for each table
                for table in tables:
                    try:
                        count_result = conn.execute(text(f"SELECT COUNT(*) FROM {table}"))
                        count = count_result.scalar()
                        print(f"  - {table}: {count} rows")
                    except SQLAlchemyError as e:
                        print(f"  - {table}: Error getting count ({e})")
                        
        except SQLAlchemyError as e:
            print(f"❌ Error getting database statistics: {e}")
    
    def get_table_schema(self, table_name: str) -> None:
        """
        Gets and displays the schema of a specific table.
        
        Args:
            table_name (str): Name of the table to get schema for
        """
        if not self.db:
            print("❌ Error: Database not connected")
            return
            
        try:
            schema = self.db.get_table_info([table_name])
            print(f"\n📑 Schema for {table_name}:")
            print(schema)
        except Exception as e:
            print(f"\n❌ Error: {e}")
    
    def get_tables(self) -> list:
        """
        Gets a list of all tables in the database.
        
        Returns:
            list: List of table names
        """
        if not self.db:
            print("❌ Error: Database not connected")
            return []
            
        return self.db.get_usable_table_names() 