import json
import logging
from typing import AsyncGenerator, List, Dict, Any
from langchain_core.messages import SystemMessage, HumanMessage

from schemapilot.config import settings
from schemapilot.db import get_db
from schemapilot.security import validate_sql_query
from schemapilot.llm import get_llm

logger = logging.getLogger("schemapilot.agent")

class SchemaPilotAgent:
    """Streamlined SQL agent that generates, validates, executes, and formats SQL queries in a single sequence."""
    
    def __init__(self):
        self.llm = None

    def _get_text_content(self, content: Any) -> str:
        """Safely extracts string content from response to handle multi-part lists in newer LangChains."""
        if isinstance(content, list):
            parts = []
            for part in content:
                if isinstance(part, str):
                    parts.append(part)
                elif isinstance(part, dict) and "text" in part:
                    parts.append(part["text"])
                elif hasattr(part, "text"):
                    parts.append(part.text)
            return "".join(parts)
        elif not isinstance(content, str):
            return str(content)
        return content

    async def generate_sql(self, query: str, schemas: str, dialect: str, previous_error: str = None) -> str:
        """Asks LLM to write a clean SQL query. Handles self-correction on database errors."""
        repair_context = ""
        if previous_error:
            repair_context = (
                f"\n\n--- CRITICAL: SELF-CORRECTION REQUIRED ---\n"
                f"Your previous SQL query failed with error: {previous_error}\n"
                "Please analyze the error, review column/table names, and output a corrected SQL query."
            )
            
        rule_instruction = (
            "Generate a clean, optimized SQL SELECT statement to retrieve the answer.\n"
            "Rules:\n"
            "1. Output ONLY the raw SQL query. Do not wrap in markdown or backticks.\n"
            "2. Only write SELECT statements. Modification commands are strictly forbidden.\n"
            "3. Reference tables and columns exactly as defined in the schema.\n"
            "4. Apply a limit (max 100) if no limit is specified."
        )
        
        if settings.ALLOW_MUTATING_QUERIES:
            rule_instruction = (
                "Generate a clean, optimized SQL statement to perform the requested operation.\n"
                "Rules:\n"
                "1. Output ONLY the raw SQL query. Do not wrap in markdown or backticks.\n"
                "2. You may write data modifying statements (INSERT, UPDATE, DELETE, etc.) if requested by the user.\n"
                "3. Reference tables and columns exactly as defined in the schema.\n"
                "4. For SELECT statements, apply a limit (max 100) if no limit is specified."
            )
            
        prompt = (
            f"You are SchemaPilot, an expert database assistant translating natural language to {dialect} SQL.\n"
            f"Database Schema:\n{schemas}\n\n"
            f"User Question: '{query}'{repair_context}\n\n"
            f"{rule_instruction}"
        )
        response = await self.llm.ainvoke([HumanMessage(content=prompt)])
        text_content = self._get_text_content(response.content)
        return text_content.strip().replace("```sql", "").replace("```", "").strip()

    async def analyze_and_format(self, query: str, sql: str, data: Dict[str, Any]) -> Dict[str, Any]:
        """Summarizes results and structures Recharts charts for the frontend visualization."""
        prompt = (
            "You are a database analyst. Review the user's question, the executed SQL, and the resulting dataset.\n"
            f"Question: '{query}'\n"
            f"Executed SQL: {sql}\n"
            f"Dataset (JSON): {json.dumps(data)}\n\n"
            "Tasks:\n"
            "1. Provide a clear natural language summary of the dataset. Format tables as markdown tables.\n"
            "2. Determine if the data can be plotted on a chart (bar, line, or pie).\n"
            "3. If a chart is useful, compile a Recharts configuration.\n\n"
            "Output your response ONLY as a JSON object matching this structure:\n"
            "{\n"
            "  \"summary\": \"Your markdown formatted analysis summary here.\",\n"
            "  \"chart\": {\n"
            "     \"type\": \"bar\" | \"line\" | \"pie\" | null,\n"
            "     \"title\": \"Chart Title\",\n"
            "     \"x_key\": \"column for x-axis/category slices\",\n"
            "     \"y_key\": \"column for numeric heights/values\",\n"
            "     \"data\": [] // Array of row dictionaries from the dataset\n"
            "  }\n"
            "}\n"
        )
        try:
            response = await self.llm.ainvoke([HumanMessage(content=prompt)])
            text_content = self._get_text_content(response.content)
            cleaned = text_content.strip().replace("```json", "").replace("```", "").strip()
            return json.loads(cleaned)
        except Exception as e:
            logger.error(f"Analysis failed: {e}")
            return {
                "summary": f"Query returned results: {str(data)}",
                "chart": {"type": None, "title": "", "x_key": "", "y_key": "", "data": []}
            }

    async def execute(self, query: str, history: List[Dict[str, Any]] = None, llm_config: Dict[str, Any] = None) -> AsyncGenerator[str, None]:
        """Runs the query lifecycle: schema discovery, SQL generation, validation, execution, and analysis."""
        try:
            self.llm = get_llm(llm_config)
        except Exception as e:
            yield json.dumps({"event": "error", "error": f"LLM Setup Error: {str(e)}"}) + "\n"
            return

        try:
            db = get_db()
            dialect = db.engine.dialect.name
        except Exception as e:
            yield json.dumps({"event": "error", "error": f"Database offline: {str(e)}"}) + "\n"
            return

        yield json.dumps({"event": "agent_message", "agent": "Architect", "message": "Reading schemas and generating optimized SQL query..."}) + "\n"
        
        try:
            tables = db.get_tables()
            if not tables:
                yield json.dumps({"event": "final_output", "text": "No tables detected. Verify your database connection."}) + "\n"
                return
            schemas = db.langchain_db.get_table_info(tables)
        except Exception as e:
            yield json.dumps({"event": "error", "error": f"Schema retrieval failed: {str(e)}"}) + "\n"
            return

        sql_query = ""
        previous_error = None
        attempts = 0
        max_attempts = 3
        
        while attempts < max_attempts:
            attempts += 1
            
            sql_query = await self.generate_sql(query, schemas, dialect, previous_error)
            
            yield json.dumps({
                "event": "agent_message", 
                "agent": "Programmer", 
                "message": f"Generated SQL Query (Attempt {attempts}):\n```sql\n{sql_query}\n```"
            }) + "\n"

            yield json.dumps({"event": "agent_message", "agent": "Sentry", "message": "Auditing SQL syntax and query safety..."}) + "\n"
            is_safe, security_msg = validate_sql_query(sql_query, dialect=dialect, allow_mutation=settings.ALLOW_MUTATING_QUERIES)
            if not is_safe:
                yield json.dumps({"event": "final_output", "text": f"Blocked Query Execution: {security_msg}"}) + "\n"
                return
            
            yield json.dumps({"event": "agent_message", "agent": "Executor", "message": "Executing SQL query..."}) + "\n"
            try:
                result = db.execute_query(sql_query)
                break
            except Exception as e:
                previous_error = str(e)
                yield json.dumps({
                    "event": "agent_message", 
                    "agent": "Executor", 
                    "message": f"⚠️ SQL execution failed: {previous_error}"
                }) + "\n"
                
                if attempts >= max_attempts:
                    yield json.dumps({"event": "error", "error": "Query failed after multiple self-correction repair attempts."}) + "\n"
                    return

        yield json.dumps({"event": "agent_message", "agent": "Analyst", "message": "Formatting database records..."}) + "\n"
        analysis = await self.analyze_and_format(query, sql_query, result)
        
        yield json.dumps({
            "event": "swarm_completed",
            "sql": sql_query,
            "summary": analysis.get("summary", ""),
            "chart": analysis.get("chart", {"type": None}),
            "raw_data": result
        }) + "\n"
