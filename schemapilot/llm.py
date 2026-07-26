import os
import json
import logging
from typing import Dict, Any, List

from schemapilot.paths import USER_CONFIG_DIR, ensure_config_dir, write_credential_json

logger = logging.getLogger("schemapilot.llm")

# USER_CONFIG_DIR is owned by schemapilot.paths; it is re-exported here only so existing
# monkeypatch targets keep resolving. models.json holds plaintext API keys, so it goes through
# the same 0700-directory / atomic-0600-file plumbing as connections.json.
MODELS_FILE = os.path.join(USER_CONFIG_DIR, "models.json")

class ModelProfileManager:
    """Manages saved LLM configuration profiles and active model selection."""
    
    def __init__(self):
        self.profiles = {}
        self.active_id = None
        self.load_profiles()
        self.auto_activate_first()

    def load_profiles(self):
        """Loads saved profiles from models.json."""
        ensure_config_dir(os.path.dirname(MODELS_FILE))
        if os.path.exists(MODELS_FILE):
            try:
                with open(MODELS_FILE, "r") as f:
                    self.profiles = json.load(f)
                # Resolve active profile
                for pid, cfg in self.profiles.items():
                    if cfg.get("is_active"):
                        self.active_id = pid
                        break
            except Exception as e:
                logger.error(f"Failed to load models file: {e}")
                self.profiles = {}
        else:
            self.profiles = {}

    def save_profiles(self):
        """Saves LLM profiles to models.json.

        models.json holds plaintext LLM API keys, so it is written atomically with 0600 rather
        than inheriting the process umask (which typically yields world-readable 0644).
        """
        try:
            write_credential_json(MODELS_FILE, self.profiles)
        except Exception as e:
            logger.error(f"Failed to save models file: {e}")

    def add_profile(self, profile_id: str, config: Dict[str, Any]):
        """Adds or updates an LLM profile."""
        config["is_active"] = False
        self.profiles[profile_id] = config
        self.save_profiles()

    def delete_profile(self, profile_id: str):
        """Deletes a saved LLM profile."""
        if profile_id in self.profiles:
            del self.profiles[profile_id]
            self.save_profiles()
            if self.active_id == profile_id:
                self.active_id = None
                self.auto_activate_first()

    def select_profile(self, profile_id: str) -> bool:
        """Sets the active LLM profile."""
        if profile_id not in self.profiles:
            return False
        
        for pid in self.profiles:
            self.profiles[pid]["is_active"] = (pid == profile_id)
            
        self.active_id = profile_id
        self.save_profiles()
        return True

    def auto_activate_first(self):
        """Auto-activates the first model if none is active."""
        if self.profiles and not self.active_id:
            first_id = list(self.profiles.keys())[0]
            self.select_profile(first_id)

    def get_profiles_list(self) -> List[Dict[str, Any]]:
        """Returns list of all configured model profiles."""
        return [
            {
                "id": pid,
                "provider": cfg.get("provider"),
                "model_name": cfg.get("model_name"),
                "api_key": cfg.get("api_key"),
                "base_url": cfg.get("base_url"),
                "is_active": self.active_id == pid
            }
            for pid, cfg in self.profiles.items()
        ]

    def get_active_profile(self) -> Dict[str, Any]:
        """Gets active configuration dictionary, or falls back to env defaults."""
        if self.active_id and self.active_id in self.profiles:
            return self.profiles[self.active_id]
        
        # Env Fallback
        from schemapilot.config import settings
        return {
            "provider": settings.LLM_PROVIDER,
            "model_name": settings.LLM_MODEL_NAME,
            "api_key": settings.LLM_API_KEY,
            "base_url": settings.LLM_BASE_URL
        }

# Singleton instance
_model_manager = None

def get_model_manager() -> ModelProfileManager:
    global _model_manager
    if _model_manager is None:
        _model_manager = ModelProfileManager()
    return _model_manager

def get_llm(config_override: Dict[str, Any] = None):
    """
    Model Factory: Instantiates LLM client.
    Resolves configuration in order:
    1. Runtime `config_override` dictionary.
    2. Active profile from `models.json`.
    3. Default `.env` settings.
    """
    manager = get_model_manager()
    active_profile = manager.get_active_profile()
    
    config = {**active_profile}
    
    # Apply command overrides
    if config_override:
        for key in ["provider", "model_name", "api_key", "base_url"]:
            if config_override.get(key) or config_override.get(f"llm_{key}"):
                val = config_override.get(key) or config_override.get(f"llm_{key}")
                config[key] = val

    provider = str(config.get("provider", "google")).lower().strip().replace("_", "-")
    model_name = str(config.get("model_name", "gemini-2.0-flash"))
    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "")
    from schemapilot.config import settings
    temp = settings.TEMPERATURE

    if provider == "google":
        from langchain_google_genai import ChatGoogleGenerativeAI
        actual_key = api_key or os.getenv("GOOGLE_API_KEY")
        if not actual_key:
            raise ValueError("Google API key is missing. Specify it in settings, profiles, or set GOOGLE_API_KEY.")
        return ChatGoogleGenerativeAI(
            model=model_name,
            temperature=temp,
            google_api_key=actual_key
        )

    elif provider == "openai":
        from langchain_openai import ChatOpenAI
        actual_key = api_key or os.getenv("OPENAI_API_KEY")
        if not actual_key:
            raise ValueError("OpenAI API key is missing. Specify it in settings, profiles, or set OPENAI_API_KEY.")
        return ChatOpenAI(
            model=model_name,
            temperature=temp,
            api_key=actual_key
        )

    elif provider == "anthropic":
        from langchain_anthropic import ChatAnthropic
        actual_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not actual_key:
            raise ValueError("Anthropic API key is missing. Specify it in settings, profiles, or set ANTHROPIC_API_KEY.")
        return ChatAnthropic(
            model=model_name,
            temperature=temp,
            api_key=actual_key
        )

    elif provider in ["local", "ollama", "openai-compatible"]:
        from langchain_openai import ChatOpenAI
        actual_url = base_url or os.getenv("LLM_BASE_URL")
        if not actual_url:
            raise ValueError("Base URL is missing for local/openai-compatible provider. Specify it in settings, profiles, or set LLM_BASE_URL.")
        
        actual_key = api_key or os.getenv("LLM_API_KEY", "ollama-dummy-key")
        
        return ChatOpenAI(
            model=model_name,
            temperature=temp,
            api_key=actual_key,
            base_url=actual_url
        )

    else:
        raise ValueError(
            f"Unsupported LLM provider '{provider}'. "
            "Supported providers: google, openai, anthropic, openai-compatible, local, ollama"
        )
