"""
The Oracle of Data: A MySQL Assistant
====================================

A powerful and elegant SQL agent that transforms complex database operations into simple conversations.
This agent is capable of handling any MySQL operation with grace and precision.
"""

import os
import sys
import time
from typing import Any, Dict, List, Optional, Union
import warnings
from dotenv import load_dotenv

from langchain_community.agent_toolkits import create_sql_agent
from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
from langchain_community.utilities.sql_database import SQLDatabase
from langchain_core.prompts import PromptTemplate
from langchain_google_genai import ChatGoogleGenerativeAI
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from langchain_community.tools import BaseTool
from langchain_community.tools.sql_database.tool import QuerySQLDatabaseTool

# Load environment variables
load_dotenv()

# =============================================================================
# Constants: The Sacred Numbers
# =============================================================================
MAX_RETRIES: int = 5  # Increased retries for rate limits
RETRY_DELAY: int = 5  # Increased delay between retries
MAX_QUERY_TIME: int = 30  # seconds
MAX_RESULTS: int = 1000
BATCH_SIZE: int = 100
MAX_ITERATIONS: int = 15  # Maximum number of steps the agent can take

# Database configuration from environment variables
DB_CONFIG: Dict[str, Union[str, int]] = {
    "username": os.getenv("DB_USERNAME", "dbuser"),
    "password": os.getenv("DB_PASSWORD", "DBuser123#"),
    "host": os.getenv("DB_HOST", "localhost"),
    "port": os.getenv("DB_PORT", "3306"),
    "database": os.getenv("DB_NAME", "mydb"),
    "pool_size": 5,
    "max_overflow": 10,
    "pool_timeout": 30,
    "pool_recycle": 3600,
    "connect_timeout": 10,
    "read_timeout": 30,
    "write_timeout": 30,
}

# =============================================================================
# Database Connection: Opening the Gates to the Data Realm
# =============================================================================
def create_database_connection() -> SQLDatabase:
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
            
            engine = create_engine(
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
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
                conn.commit()
            
            # Create SQLDatabase instance with enhanced configuration
            db = SQLDatabase.from_uri(
                connection_string,
                include_tables=None,  # Include all tables
                sample_rows_in_table_info=3,  # Include sample data
                max_string_length=1000  # Truncate long strings
            )
            
            print("🌟 Successfully connected to the database!")
            return db
            
        except SQLAlchemyError as e:
            if attempt < MAX_RETRIES - 1:
                print(f"🌀 Attempt {attempt + 1}/{MAX_RETRIES} to connect failed: {e}")
                print(f"⏳ Waiting {RETRY_DELAY} seconds before trying again...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"❌ Failed to connect to database after {MAX_RETRIES} attempts: {e}")
                sys.exit(1)

# =============================================================================
# Language Model: Creating the Mind of the Oracle
# =============================================================================
def create_language_model() -> ChatGoogleGenerativeAI:
    """
    Creates a language model with enhanced configuration for better performance.
    
    Returns:
        ChatGoogleGenerativeAI: Our oracle's mind
        
    Raises:
        SystemExit: If the API key is missing or invalid
    """
    # Get API key from environment variable
    api_key = os.getenv("GOOGLE_API_KEY")
    if not api_key:
        print("❌ Error: GOOGLE_API_KEY environment variable is not set")
        print("Please set your Google API key using:")
        print("  export GOOGLE_API_KEY='your-api-key-here'")
        sys.exit(1)
    
    try:
        print("🔄 Initializing Gemini 2.0 Flash model...")
        model = ChatGoogleGenerativeAI(
            model="gemini-2.0-flash",  # Using Gemini 2.0 Flash model
            temperature=0.0,  # Use greedy sampling for more consistent outputs
            max_output_tokens=4096,  # Allow longer responses for complex queries
            top_p=1.0,  # Use full probability mass for more precise responses
            streaming=False,
            timeout=120,  # Increased timeout for complex queries
            max_retries=5,  # Increased retries
            google_api_key=api_key,
            convert_system_message_to_human=True  # Better handling of system messages
        )
        print("✨ Model initialized successfully")
        return model
    except Exception as e:
        print(f"❌ Error creating language model: {e}")
        print("Please check your Google API key and try again")
        sys.exit(1)

# =============================================================================
# Database Management: Cleaning and Maintenance
# =============================================================================
def get_database_stats(db: SQLDatabase) -> None:
    """
    Gets and displays statistics about the database tables.
    
    Args:
        db (SQLDatabase): The database connection
    """
    try:
        print("\n📈 Database Statistics:")
        
        # Create a new engine for direct SQL operations
        engine = create_engine(
            f"mysql+pymysql://{DB_CONFIG['username']}:{DB_CONFIG['password']}@"
            f"{DB_CONFIG['host']}:{DB_CONFIG['port']}/{DB_CONFIG['database']}"
        )
        
        with engine.connect() as conn:
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

# =============================================================================
# Agent Creation: Summoning our oracle
# =============================================================================
class CustomSQLDatabaseToolkit(SQLDatabaseToolkit):
    """Extended toolkit that supports all MySQL operations."""
    
    def get_tools(self) -> List[BaseTool]:
        """
        Get the tools in the toolkit with additional DML/DDL support.
        
        Returns:
            List[BaseTool]: List of tools including base SQL tools and DML/DDL tool
        """
        # Get base tools
        tools = super().get_tools()
        
        # Add DML/DDL tool
        dml_ddl_tool_description = (
            "Execute any MySQL operation including DML (INSERT, UPDATE, DELETE) and DDL (CREATE, ALTER, DROP) statements. "
            "Input should be a valid MySQL query. Use with caution as these operations modify the database."
        )
        dml_ddl_tool = QuerySQLDatabaseTool(
            db=self.db,
            description=dml_ddl_tool_description
        )
        
        return tools + [dml_ddl_tool]

def summon_oracle(db: SQLDatabase) -> Any:
    """
    Summons the Oracle of Data with enhanced capabilities and error handling.
    
    Args:
        db (SQLDatabase): The connection to our data realm
        
    Returns:
        Any: Our summoned oracle
        
    Raises:
        SystemExit: If the oracle cannot be summoned after MAX_RETRIES attempts
    """
    for attempt in range(MAX_RETRIES):
        try:
            print("\n🔄 Initializing the Oracle...")
            llm = create_language_model()
            print("✨ Language model initialized successfully")
            
            print("🔄 Creating SQL toolkit...")
            toolkit = CustomSQLDatabaseToolkit(
                db=db,
                llm=llm,
                reduce_k_below_max_tokens=True,
                max_string_length=1000
            )
            print("✨ SQL toolkit created successfully")

            # Create agent with increased iterations
            print("🔄 Creating SQL agent...")
            oracle = create_sql_agent(
                toolkit=toolkit,
                llm=llm,
                verbose=True,
                agent_type="zero-shot-react-description",
                handle_parsing_errors=True,
                max_iterations=MAX_ITERATIONS,  # Increased from 5 to allow more complex operations
                return_intermediate_steps=True,
                prefix="""You are an agent designed to interact with a MySQL database.
Given an input question or command, create a syntactically correct MySQL query to run, then look at the results of the query and return the answer.
You can perform any MySQL operation including:
- Data Querying (SELECT)
- Data Manipulation (INSERT, UPDATE, DELETE)
- Data Definition (CREATE, ALTER, DROP)
- Data Control (GRANT, REVOKE)
- Transaction Control (COMMIT, ROLLBACK)

You have access to tools for interacting with the database.
Only use the below tools. Only use the information returned by the below tools to construct your final answer.
You MUST double check your query before executing it. If you get an error while executing a query, rewrite the query and try again.

For complex operations that might require multiple steps:
1. Plan your steps in advance
2. Execute one operation at a time
3. Verify the success of each operation
4. Continue with the next step
5. Provide clear feedback about what was accomplished

If the question does not seem related to the database, just return "I don't know" as the answer.
"""
            )
            
            print("🌟 The Oracle of Data has been successfully summoned!")
            return oracle
            
        except Exception as e:
            if attempt < MAX_RETRIES - 1:
                print(f"🌀 Attempt {attempt + 1}/{MAX_RETRIES} to summon the oracle failed: {e}")
                print(f"⏳ Waiting {RETRY_DELAY} seconds before trying again...")
                time.sleep(RETRY_DELAY)
            else:
                print(f"❌ Failed to summon the oracle after {MAX_RETRIES} attempts: {e}")
                sys.exit(1)

def main() -> None:
    """
    The main interface for interacting with the Oracle of Data.
    
    This function:
    1. Initializes the database connection
    2. Summons the Oracle
    3. Handles either command-line queries or interactive mode
    
    Raises:
        SystemExit: If there's a fatal error during execution
    """
    try:
        # Initialize database connection
        db = create_database_connection()
        
        # Summon the oracle
        oracle = summon_oracle(db)
        
        # Check if query is provided as command line argument
        if len(sys.argv) > 1:
            query = " ".join(sys.argv[1:])
            try:
                response = oracle.invoke({"input": query})
                if isinstance(response, dict):
                    if "output" in response:
                        print("\n💡 Result:", response["output"])
                    elif "intermediate_steps" in response:
                        final_step = response["intermediate_steps"][-1]
                        if isinstance(final_step, tuple) and len(final_step) > 1:
                            print("\n💡 Result:", final_step[1])
                        else:
                            print("\n💡 Result:", response)
                else:
                    print("\n💡 Result:", response)
            except Exception as e:
                print(f"\n❌ Error: {e}")
                sys.exit(1)
        else:
            # Interactive chat interface
            print("\n🌟 Welcome to the Oracle of Data!")
            print("Type 'exit' to quit, 'help' for available commands, or ask your question.")
            
            while True:
                try:
                    query = input("\n🤔 Your question: ").strip()
                    
                    if not query:
                        continue
                        
                    if query.lower() == 'exit':
                        print("\n👋 Farewell, seeker of knowledge!")
                        break
                        
                    if query.lower() == 'help':
                        print("\n📚 Available commands:")
                        print("  - exit: Quit the program")
                        print("  - help: Show this help message")
                        print("  - tables: List all tables in the database")
                        print("  - schema <table>: Show schema of a specific table")
                        print("  - stats: Show database statistics")
                        print("\n💡 You can also ask any question about the data!")
                        continue
                        
                    if query.lower() == 'tables':
                        tables = db.get_usable_table_names()
                        print("\n📊 Available tables:")
                        for table in tables:
                            print(f"  - {table}")
                        continue
                        
                    if query.lower().startswith('schema '):
                        table = query[7:].strip()
                        try:
                            schema = db.get_table_info([table])
                            print(f"\n📑 Schema for {table}:")
                            print(schema)
                        except Exception as e:
                            print(f"\n❌ Error: {e}")
                        continue
                        
                    if query.lower() == 'stats':
                        get_database_stats(db)
                        continue
                    
                    # Process the query
                    response = oracle.invoke({"input": query})
                    if isinstance(response, dict):
                        if "output" in response:
                            print("\n💡 Result:", response["output"])
                        elif "intermediate_steps" in response:
                            final_step = response["intermediate_steps"][-1]
                            if isinstance(final_step, tuple) and len(final_step) > 1:
                                print("\n💡 Result:", final_step[1])
                            else:
                                print("\n💡 Result:", response)
                    else:
                        print("\n💡 Result:", response)
                        
                except KeyboardInterrupt:
                    print("\n\n👋 Farewell, seeker of knowledge!")
                    break
                except Exception as e:
                    print(f"\n❌ Error: {e}")
                    continue
                
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=UserWarning)
    main()
