"""
Agent module for MySQL Agent.
Handles agent creation and management.
"""

from typing import Any, List
from langchain_community.agent_toolkits import create_sql_agent
from langchain_community.agent_toolkits.sql.toolkit import SQLDatabaseToolkit
from langchain_community.tools import BaseTool
from langchain_community.tools.sql_database.tool import QuerySQLDatabaseTool

from config import AGENT_CONFIG, SYSTEM_PROMPT

class AgentManager:
    """Manages SQL agent creation and operations."""
    
    def __init__(self):
        self.agent = None
    
    def create_agent(self, db: Any, llm: Any) -> Any:
        """
        Creates a SQL agent with enhanced capabilities.
        
        Args:
            db: Database connection
            llm: Language model
            
        Returns:
            Any: The created SQL agent
        """
        try:
            print("\n🔄 Creating SQL toolkit...")
            toolkit = CustomSQLDatabaseToolkit(
                db=db,
                llm=llm,
                reduce_k_below_max_tokens=True,
                max_string_length=1000
            )
            print("✨ SQL toolkit created successfully")

            # Create agent with increased iterations
            print("🔄 Creating SQL agent...")
            self.agent = create_sql_agent(
                toolkit=toolkit,
                llm=llm,
                verbose=AGENT_CONFIG["verbose"],
                agent_type=AGENT_CONFIG["agent_type"],
                handle_parsing_errors=AGENT_CONFIG["handle_parsing_errors"],
                max_iterations=AGENT_CONFIG["max_iterations"],
                return_intermediate_steps=AGENT_CONFIG["return_intermediate_steps"],
                prefix=SYSTEM_PROMPT
            )
            
            print("🌟 The Oracle of Data has been successfully summoned!")
            return self.agent
            
        except Exception as e:
            print(f"❌ Error creating agent: {e}")
            raise

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