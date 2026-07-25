import os
from pydantic import BaseModel, Field
from dotenv import load_dotenv, find_dotenv

# Calculate project root of the installed package
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Load environment variables. The package-root .env supports editable/source checkouts;
# find_dotenv walks up from the current working directory so an installed `schemapilot`
# invoked from a project folder still picks that project's .env up.
load_dotenv(os.path.join(PROJECT_ROOT, ".env"))
load_dotenv(find_dotenv(usecwd=True), override=False)

class Settings(BaseModel):
    # Active Database Configuration
    DB_TYPE: str = Field(default_factory=lambda: os.getenv("DB_TYPE", "sqlite")) # 'mysql', 'postgres', or 'sqlite'
    DB_HOST: str = Field(default_factory=lambda: os.getenv("DB_HOST", "localhost"))
    DB_PORT: int = Field(default_factory=lambda: int(os.getenv("DB_PORT", "5432")))
    DB_USER: str = Field(default_factory=lambda: os.getenv("DB_USER", ""))
    DB_PASSWORD: str = Field(default_factory=lambda: os.getenv("DB_PASSWORD", ""))
    DB_NAME: str = Field(default_factory=lambda: os.getenv("DB_NAME", "schemapilot.db"))
    
    # Unified LLM Settings
    LLM_PROVIDER: str = Field(default_factory=lambda: os.getenv("LLM_PROVIDER", "google"))
    LLM_MODEL_NAME: str = Field(default_factory=lambda: os.getenv("LLM_MODEL_NAME", "gemini-2.0-flash"))
    LLM_API_KEY: str = Field(default_factory=lambda: os.getenv("LLM_API_KEY", ""))
    LLM_BASE_URL: str = Field(default_factory=lambda: os.getenv("LLM_BASE_URL", ""))
    # Deterministic by default: SQL generation is not a place for sampling variance.
    TEMPERATURE: float = Field(default_factory=lambda: float(os.getenv("LLM_TEMPERATURE", "0.0")))
    
    # Catalog / Prompt Budgets
    # CATALOG_MAX_TABLES caps how many tables introspection reflects (wide warehouses can hold
    # thousands); MAX_PROMPT_TABLES caps how many of them reach the LLM prompt.
    CATALOG_MAX_TABLES: int = Field(default_factory=lambda: int(os.getenv("CATALOG_MAX_TABLES", "60")))
    MAX_PROMPT_TABLES: int = Field(default_factory=lambda: int(os.getenv("MAX_PROMPT_TABLES", "12")))
    # ANALYST_SAMPLE_ROWS caps how many result rows the analyst prompt carries: summarising a
    # 50k-row result set does not need the whole set, just a sample plus the true row count.
    ANALYST_SAMPLE_ROWS: int = Field(default_factory=lambda: int(os.getenv("ANALYST_SAMPLE_ROWS", "30")))
    
    # Security Settings
    ALLOW_MUTATING_QUERIES: bool = Field(default_factory=lambda: os.getenv("ALLOW_MUTATING_QUERIES", "false").lower() == "true")

settings = Settings()
