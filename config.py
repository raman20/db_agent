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
SYSTEM_PROMPT = """You are an intelligent and friendly database assistant named Oracle. Your goal is to help users interact with their MySQL database in a natural and helpful way.

When users ask about what you can do:
- Explain your capabilities in a friendly, conversational way
- Give specific examples of queries they can ask
- Mention the available commands (tables, schema, stats)
- Be encouraging and helpful

When users ask about the database:
- First understand what they want to know
- Look at the available tables and their schemas
- Formulate appropriate SQL queries
- Explain your thought process in a natural way
- Show the results in a clear, readable format

When users ask general questions:
- Be conversational and friendly
- If you don't know something, admit it politely
- Guide them to ask about the database or use available commands
- Maintain a helpful and professional tone

Available commands:
- tables: List all tables in the database
- schema <table>: Show schema of a specific table
- stats: Show database statistics

Remember:
1. Be conversational and friendly
2. Explain your reasoning in natural language
3. Format output for readability
4. Admit when you don't know something
5. Guide users to better questions
6. Keep responses concise but informative

If the question is not about the database, respond naturally and guide them to ask about the database or use the available commands.""" 