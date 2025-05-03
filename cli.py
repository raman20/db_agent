"""
CLI module for MySQL Agent.
Handles command-line interface and user interaction.
"""

import sys
import re
from typing import Any, Tuple

class CLI:
    """Handles command-line interface and user interaction."""
    
    def __init__(self, agent: Any, db_manager: Any):
        self.agent = agent
        self.db_manager = db_manager
        
        # Define intent patterns
        self.intent_patterns = {
            'greeting': r'^(hi|hello|hey|greetings|good (morning|afternoon|evening))',
            'farewell': r'^(bye|goodbye|see you|farewell)',
            'thanks': r'^(thanks|thank you|thx|appreciate)',
            'help': r'^(help|what can you do|how do i use this|show commands|what can you help me with)',
            'tables': r'^(tables|show tables|list tables|what tables)',
            'schema': r'^(schema|show schema|describe|what columns)',
            'stats': r'^(stats|statistics|database stats|show stats)',
            'database_query': r'.*'  # Default catch-all for database queries
        }
    
    def detect_intent(self, query: str) -> Tuple[str, str]:
        """
        Detect the intent of the user's input.
        
        Args:
            query (str): The user's input
            
        Returns:
            Tuple[str, str]: (intent_type, cleaned_query)
        """
        query = query.lower().strip()
        
        for intent, pattern in self.intent_patterns.items():
            if re.match(pattern, query):
                # For schema queries, extract the table name
                if intent == 'schema':
                    match = re.match(r'^(schema|show schema|describe|what columns)\s+(.+)', query)
                    if match:
                        return intent, match.group(2)
                return intent, query
        
        return 'database_query', query
    
    def process_command(self, query: str) -> None:
        """
        Process a single command or query.
        
        Args:
            query (str): The command or query to process
        """
        if not query:
            return
            
        intent, processed_query = self.detect_intent(query)
        
        # Handle different intents
        if intent == 'greeting':
            print("\n👋 Hello! I'm Oracle, your friendly database assistant. How can I help you today?")
            return
            
        if intent == 'farewell':
            print("\n👋 Farewell! Feel free to return if you need more help with your database.")
            sys.exit(0)
            
        if intent == 'thanks':
            print("\n🙏 You're welcome! Is there anything else I can help you with?")
            return
            
        if intent == 'help':
            self._show_help()
            return
            
        if intent == 'tables':
            self._show_tables()
            return
            
        if intent == 'schema':
            self.db_manager.get_table_schema(processed_query)
            return
            
        if intent == 'stats':
            self.db_manager.get_database_stats()
            return
        
        # For database queries, use the agent
        print("\n💡 ", end="", flush=True)
        try:
            response = []
            for chunk in self.agent.stream({"input": processed_query}):
                if isinstance(chunk, dict):
                    if "output" in chunk:
                        response.append(chunk["output"])
                    elif "intermediate_steps" in chunk:
                        final_step = chunk["intermediate_steps"][-1]
                        if isinstance(final_step, tuple) and len(final_step) > 1:
                            response.append(final_step[1])
                        else:
                            response.append(str(chunk))
                else:
                    response.append(str(chunk))
            
            # Process and clean the response
            final_response = "".join(response).strip()
            if final_response == "I don't know":
                print("I'm not sure how to help with that. Would you like to know what I can do? Type 'help' to see my capabilities.")
            else:
                print(final_response)
                
        except Exception as e:
            print(f"❌ I encountered an error: {e}")
            print("Would you like to try rephrasing your question?")
    
    def _show_help(self) -> None:
        """Display help information."""
        print("\n📚 I can help you with your database in several ways:")
        print("\n1. Basic Commands:")
        print("   - tables: List all tables in the database")
        print("   - schema <table>: Show schema of a specific table")
        print("   - stats: Show database statistics")
        
        print("\n2. Ask Questions About Your Data:")
        print("   - 'Show me all customers'")
        print("   - 'How many orders do we have?'")
        print("   - 'What's the average order value?'")
        print("   - 'Find customers who haven't ordered in 30 days'")
        
        print("\n3. Data Analysis:")
        print("   - 'Show me sales trends by month'")
        print("   - 'What are our top selling products?'")
        print("   - 'Which customers have the highest lifetime value?'")
        
        print("\n4. Data Management:")
        print("   - 'Add a new customer'")
        print("   - 'Update customer information'")
        print("   - 'Delete inactive records'")
        
        print("\n🤖 I can also understand greetings and farewells!")
        print("\n💡 Just ask me anything about your database, and I'll do my best to help!")
    
    def _show_tables(self) -> None:
        """Display list of available tables."""
        tables = self.db_manager.get_tables()
        print("\n📊 Here are the tables in your database:")
        for table in tables:
            print(f"  - {table}")
        print("\n💡 You can ask me about any of these tables or type 'schema <table>' to see its structure.")
    
    def run_interactive(self) -> None:
        """Run the interactive command-line interface."""
        print("\n🌟 Welcome to Oracle, your intelligent database assistant!")
        print("I'm here to help you explore and manage your database.")
        print("Type 'help' to see what I can do, or ask me anything about your data.")
        
        while True:
            try:
                query = input("\n🤔 Your question: ").strip()
                self.process_command(query)
            except KeyboardInterrupt:
                print("\n\n👋 Farewell! Feel free to return if you need more help with your database.")
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