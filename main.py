"""
Main module for MySQL Agent.
The entry point of the application.
"""

import sys
import warnings

from database import DatabaseManager
from model import ModelManager
from agent import AgentManager
from cli import CLI

def main() -> None:
    """
    The main entry point of the application.
    """
    try:
        # Initialize managers
        db_manager = DatabaseManager()
        model_manager = ModelManager()
        agent_manager = AgentManager()
        
        # Connect to database
        db = db_manager.connect()
        
        # Create language model
        llm = model_manager.create_model()
        
        # Create agent
        agent = agent_manager.create_agent(db, llm)
        
        # Create CLI
        cli = CLI(agent, db_manager)
        
        # Check if query is provided as command line argument
        if len(sys.argv) > 1:
            query = " ".join(sys.argv[1:])
            cli.run_command_line(query)
        else:
            cli.run_interactive()
            
    except Exception as e:
        print(f"\n❌ Fatal error: {e}")
        sys.exit(1)

if __name__ == "__main__":
    warnings.filterwarnings("ignore", category=UserWarning)
    main() 