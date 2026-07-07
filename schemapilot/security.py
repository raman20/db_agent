from typing import Tuple
import sqlglot
from sqlglot import exp

def validate_sql_query(sql: str, dialect: str = "mysql", allow_mutation: bool = False) -> Tuple[bool, str]:
    """
    Parses and validates a SQL query using AST (Abstract Syntax Tree) analysis.
    Prevents SQL injection bypasses, filesystem reads, system commands, and unauthorized writes.
    """
    cleaned_sql = sql.strip().strip(";").strip()
    
    if not cleaned_sql:
        return False, "Empty query string provided."

    sg_dialect = "mysql"
    if dialect in ["postgres", "postgresql"]:
        sg_dialect = "postgres"
    elif dialect == "sqlite":
        sg_dialect = "sqlite"

    try:
        expression = sqlglot.parse_one(cleaned_sql, read=sg_dialect)
    except sqlglot.errors.ParseError as e:
        return False, f"SQL Syntax Error: Failed to parse query. Details: {str(e)}"
    except Exception as e:
        return False, f"Unexpected error during query parsing: {str(e)}"

    forbidden_mutation_types = (
        exp.Drop,       
        exp.Alter,      
        exp.Create,     
        exp.Insert,     
        exp.Update,     
        exp.Delete,     
        exp.Command,    
    )

    for node in expression.walk():
        if not allow_mutation and isinstance(node, forbidden_mutation_types):
            action = node.__class__.__name__.upper()
            return False, f"Security Violation: Mutating operation '{action}' is disabled in the current session."
        
        if isinstance(node, exp.Anonymous):
            func_name = node.name.lower()
            dangerous_functions = {
                "load_file", "system", "cmd_exec", "sys_exec", "sys_exec", "sys_eval", 
                "pg_read_file", "pg_write_file", "pg_ls_dir", "copy",
            }
            if func_name in dangerous_functions:
                return False, f"Security Violation: Invocation of blocked system function '{func_name}'."
                
        if isinstance(node, exp.Copy):
            return False, "Security Violation: COPY operations are strictly blocked."

    return True, "Query is safe to execute."
