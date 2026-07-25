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
    TEMPERATURE: float = 0.0
    
    # Security Settings
    ALLOW_MUTATING_QUERIES: bool = Field(default_factory=lambda: os.getenv("ALLOW_MUTATING_QUERIES", "false").lower() == "true")
    MAX_QUERY_LIMIT: int = 1000
    
    # Semantic Caching
    CACHE_ENABLED: bool = Field(default_factory=lambda: os.getenv("CACHE_ENABLED", "true").lower() == "true")
    CACHE_SIMILARITY_THRESHOLD: float = 0.92

settings = Settings()
