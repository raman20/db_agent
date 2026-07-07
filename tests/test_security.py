import pytest
from schemapilot.security import validate_sql_query

def test_safe_queries():
    # Standard SELECT queries should be verified as safe
    safe, msg = validate_sql_query("SELECT id, name FROM users LIMIT 10")
    assert safe
    assert "safe" in msg.lower()
    
    # Complicated Joins should be safe
    safe, msg = validate_sql_query(
        "SELECT u.name, o.total FROM users u JOIN orders o ON u.id = o.user_id WHERE o.total > 100"
    )
    assert safe

def test_mutations_blocked():
    # DROP queries must be blocked
    safe, msg = validate_sql_query("DROP TABLE users")
    assert not safe
    assert "violation" in msg.lower() or "disabled" in msg.lower()
    
    # INSERT queries must be blocked when allow_mutation is False
    safe, msg = validate_sql_query("INSERT INTO users (name) VALUES ('Test')", allow_mutation=False)
    assert not safe
    
    # UPDATE queries must be blocked when allow_mutation is False
    safe, msg = validate_sql_query("UPDATE users SET name = 'Test' WHERE id = 1", allow_mutation=False)
    assert not safe

def test_mutations_allowed_when_configured():
    # INSERT queries must pass when allow_mutation is True
    safe, msg = validate_sql_query("INSERT INTO users (name) VALUES ('Test')", allow_mutation=True)
    assert safe

def test_malicious_functions_blocked():
    # Block load_file function
    safe, msg = validate_sql_query("SELECT load_file('/etc/passwd')")
    assert not safe
    assert "blocked" in msg.lower() or "violation" in msg.lower()
    
    # Block COPY statement
    safe, msg = validate_sql_query("COPY users TO '/tmp/users.txt'")
    assert not safe
