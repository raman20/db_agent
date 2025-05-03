"""
CLI module for MySQL Agent.
Handles command-line interface and user interaction.
"""

import sys
from typing import Any

class CLI:
    """Handles command-line interface and user interaction."""
    
    def __init__(self, agent: Any, db_manager: Any):
        self.agent = agent
        self.db_manager = db_manager
    
    def process_command(self, query: str) -> None:
        """
        Process a single command or query.
        
        Args:
            query (str): The command or query to process
        """
        if not query:
            return
            
        if query.lower() == 'exit':
            print("\n👋 Farewell, seeker of knowledge!")
            sys.exit(0)
            
        if query.lower() == 'help':
            self._show_help()
            return
            
        if query.lower() == 'tables':
            self._show_tables()
            return
            
        if query.lower().startswith('schema '):
            table = query[7:].strip()
            self.db_manager.get_table_schema(table)
            return
            
        if query.lower() == 'stats':
            self.db_manager.get_database_stats()
            return
        
        # Process the query with streaming
        print("\n💡 Result: ", end="", flush=True)
        for chunk in self.agent.stream({"input": query}):
            if isinstance(chunk, dict):
                if "output" in chunk:
                    print(chunk["output"], end="", flush=True)
                elif "intermediate_steps" in chunk:
                    final_step = chunk["intermediate_steps"][-1]
                    if isinstance(final_step, tuple) and len(final_step) > 1:
                        print(final_step[1], end="", flush=True)
                    else:
                        print(chunk, end="", flush=True)
            else:
                print(chunk, end="", flush=True)
        print()  # New line after streaming
    
    def _show_help(self) -> None:
        """Display help information."""
        print("\n📚 Available commands:")
        print("  - exit: Quit the program")
        print("  - help: Show this help message")
        print("  - tables: List all tables in the database")
        print("  - schema <table>: Show schema of a specific table")
        print("  - stats: Show database statistics")
        print("\n💡 You can also ask any question about the data!")
    
    def _show_tables(self) -> None:
        """Display list of available tables."""
        tables = self.db_manager.get_tables()
        print("\n📊 Available tables:")
        for table in tables:
            print(f"  - {table}")
    
    def run_interactive(self) -> None:
        """Run the interactive command-line interface."""
        print("\n🌟 Welcome to the Oracle of Data!")
        print("Type 'exit' to quit, 'help' for available commands, or ask your question.")
        
        while True:
            try:
                query = input("\n🤔 Your question: ").strip()
                self.process_command(query)
            except KeyboardInterrupt:
                print("\n\n👋 Farewell, seeker of knowledge!")
                break
            except Exception as e:
                print(f"\n❌ Error: {e}")
                continue
    
    def run_command_line(self, query: str) -> None:
        """
        Run a single command from the command line.
        
        Args:
            query (str): The query to process
        """
        try:
            self.process_command(query)
        except Exception as e:
            print(f"\n❌ Error: {e}")
            sys.exit(1) 