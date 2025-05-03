"""
Configuration module for MySQL Agent.
Contains all constants, settings, and environment variables.
"""

import os
from typing import Dict, Union
from dotenv import load_dotenv

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

# Model configuration
MODEL_CONFIG = {
    "model_name": "gemini-2.0-flash",
    "temperature": 0.0,
    "max_output_tokens": 4096,
    "top_p": 1.0,
    "disable_streaming": False,
    "timeout": 120,
    "max_retries": 5,
    "convert_system_message_to_human": True
}

# Agent configuration
AGENT_CONFIG = {
    "verbose": True,
    "agent_type": "zero-shot-react-description",
    "handle_parsing_errors": True,
    "max_iterations": MAX_ITERATIONS,
    "return_intermediate_steps": True,
}

# System prompt for the agent
SYSTEM_PROMPT = """You are an agent designed to interact with a MySQL database.
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