import logging
import json
import os
from typing import Dict, Any, List
from sqlalchemy import create_engine, text, inspect
from langchain_community.utilities.sql_database import SQLDatabase

logger = logging.getLogger("schemapilot.db")

# Store configuration files in the user's home configuration directory (standard global CLI practice)
USER_CONFIG_DIR = os.path.expanduser("~/.config/schemapilot")
CONNECTIONS_FILE = os.path.join(USER_CONFIG_DIR, "connections.json")

class DatabaseManager:
    """Manages dynamic database connectivity, raw execution, and metadata inspection for MySQL, Postgres, and SQLite."""
    
    def __init__(self):
        self.engine = None
        self.langchain_db = None
        self.active_id = None
        self.connections = {}
        self.load_connections()
        self.auto_connect_first()

    def load_connections(self):
        """Loads saved connections from the JSON store."""
        os.makedirs(os.path.dirname(CONNECTIONS_FILE), exist_ok=True)
        if os.path.exists(CONNECTIONS_FILE):
            try:
                with open(CONNECTIONS_FILE, "r") as f:
                    self.connections = json.load(f)
            except Exception as e:
                logger.error(f"Failed to load connections file: {e}")
                self.connections = {}
        else:
            self.connections = {}

    def save_connections(self):
        """Saves current connection registry to JSON."""
        try:
            with open(CONNECTIONS_FILE, "w") as f:
                json.dump(self.connections, f, indent=4)
        except Exception as e:
            logger.error(f"Failed to save connections file: {e}")

    def add_connection(self, conn_id: str, config: Dict[str, Any]) -> str:
        """Saves connection profile config and returns the connection ID."""
        self.connections[conn_id] = config
        self.save_connections()
        return conn_id

    def delete_connection(self, conn_id: str):
        """Deletes a connection profile."""
        if conn_id in self.connections:
            del self.connections[conn_id]
            self.save_connections()
            if self.active_id == conn_id:
                self.engine = None
                self.langchain_db = None
                self.active_id = None

    def get_connections_list(self) -> List[Dict[str, Any]]:
        """Returns list of connection summaries."""
        return [
            {
                "id": cid,
                "name": cfg.get("name", cid),
                "db_type": cfg.get("db_type"),
                "host": cfg.get("host"),
                "port": cfg.get("port"),
                "database": cfg.get("database"),
                "username": cfg.get("username"),
                "is_active": self.active_id == cid
            }
            for cid, cfg in self.connections.items()
        ]

    def test_connection(self, config: Dict[str, Any]) -> tuple[bool, str]:
        """Tests database connection parameters without saving them."""
        db_type = config.get("db_type", "sqlite").lower()
        uri = self.build_uri(config)
        
        try:
            test_engine = create_engine(uri, connect_args={"connect_timeout": 5} if db_type != "sqlite" else {})
            with test_engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.commit()
            return True, "Connection successful"
        except Exception as e:
            return False, str(e)

    def select_connection(self, conn_id: str) -> bool:
        """Sets the active connection and initializes the engines."""
        if conn_id not in self.connections:
            return False
            
        config = self.connections[conn_id]
        uri = self.build_uri(config)
        db_type = config.get("db_type", "sqlite").lower()
        
        try:
            logger.info(f"Connecting to database '{conn_id}' of type: {db_type}")
            
            pool_args = {}
            if db_type != "sqlite":
                pool_args = {
                    "pool_size": 5,
                    "max_overflow": 5,
                    "pool_recycle": 1800
                }
                
            self.engine = create_engine(
                uri,
                connect_args={"connect_timeout": 5} if db_type != "sqlite" else {},
                **pool_args
            )
            
            with self.engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.commit()
            
            self.langchain_db = SQLDatabase(
                self.engine,
                sample_rows_in_table_info=3,
                max_string_length=1000
            )
            self.active_id = conn_id
            logger.info(f"Switched active database to: {conn_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to connect to database '{conn_id}': {e}")
            raise ConnectionError(f"Database connection failed: {e}")

    def build_uri(self, config: Dict[str, Any]) -> str:
        """Generates connection URIs based on dialect specifications."""
        db_type = config.get("db_type", "sqlite").lower()
        user = config.get("username", "")
        password = config.get("password", "")
        host = config.get("host", "localhost")
        port = config.get("port", "")
        database = config.get("database", "")
        
        if db_type == "postgres":
            port = port or 5432
            return f"postgresql+psycopg2://{user}:{password}@{host}:{port}/{database}"
        elif db_type == "mysql":
            port = port or 3306
            return f"mysql+pymysql://{user}:{password}@{host}:{port}/{database}"
        else:
            return f"sqlite:///{database or 'schemapilot.db'}"

    def auto_connect_first(self):
        """Automatically connects to the first saved connection if available."""
        if self.connections:
            first_conn_id = list(self.connections.keys())[0]
            try:
                self.select_connection(first_conn_id)
            except Exception:
                pass

    def get_tables(self) -> List[str]:
        """Gets list of tables for active connection."""
        if not self.langchain_db:
            return []
        return self.langchain_db.get_usable_table_names()

    def execute_query(self, query: str) -> Dict[str, Any]:
        """Executes a query on active connection and returns structured results."""
        if not self.engine:
            raise ConnectionError("No active database connection selected")
            
        with self.engine.connect() as conn:
            result = conn.execute(text(query))
            
            if not result.returns_rows:
                conn.commit()
                return {"message": "Query executed successfully. No rows returned.", "row_count": result.rowcount}
                
            columns = list(result.keys())
            rows = [dict(zip(columns, row)) for row in result.fetchall()]
            return {"columns": columns, "rows": rows}

    def get_schema_metadata(self) -> Dict[str, Any]:
        """Extracts schema definitions for active connection."""
        if not self.engine:
            raise ConnectionError("No active database connection selected")
            
        inspector = inspect(self.engine)
        tables_metadata = {}
        relationships = []
        
        table_names = inspector.get_table_names()
        for table in table_names:
            columns_info = []
            pk_cols = inspector.get_primary_keys(table)
            
            for col in inspector.get_columns(table):
                columns_info.append({
                    "name": col["name"],
                    "type": str(col["type"]),
                    "nullable": col["nullable"],
                    "is_primary": col["name"] in pk_cols
                })
            
            tables_metadata[table] = {
                "name": table,
                "columns": columns_info
            }
            
            # Extract foreign keys for relationship mapping
            fks = inspector.get_foreign_keys(table)
            for fk in fks:
                relationships.append({
                    "from_table": table,
                    "from_columns": fk["constrained_columns"],
                    "to_table": fk["referred_table"],
                    "to_columns": fk["referred_columns"]
                })
                
        return {"tables": tables_metadata, "relationships": relationships}

# Singleton instance
_db_manager = None

def get_db() -> DatabaseManager:
    global _db_manager
    if _db_manager is None:
        _db_manager = DatabaseManager()
    return _db_manager
