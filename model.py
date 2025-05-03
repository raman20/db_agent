"""
Model module for MySQL Agent.
Handles language model setup and configuration.
"""

import os
import sys
from langchain_google_genai import ChatGoogleGenerativeAI

from config import MODEL_CONFIG

class ModelManager:
    """Manages language model setup and configuration."""
    
    def __init__(self):
        self.model = None
    
    def create_model(self) -> ChatGoogleGenerativeAI:
        """
        Creates a language model with enhanced configuration for better performance.
        
        Returns:
            ChatGoogleGenerativeAI: The configured language model
            
        Raises:
            SystemExit: If the API key is missing or invalid
        """
        # Get API key from environment variable
        api_key = os.getenv("GOOGLE_API_KEY")
        if not api_key:
            print("❌ Error: GOOGLE_API_KEY environment variable is not set")
            print("Please set your Google API key using:")
            print("  export GOOGLE_API_KEY='your-api-key-here'")
            sys.exit(1)
        
        try:
            print("🔄 Initializing Gemini 2.0 Flash model...")
            self.model = ChatGoogleGenerativeAI(
                model=MODEL_CONFIG["model_name"],
                temperature=MODEL_CONFIG["temperature"],
                max_output_tokens=MODEL_CONFIG["max_output_tokens"],
                top_p=MODEL_CONFIG["top_p"],
                disable_streaming=MODEL_CONFIG["disable_streaming"],
                timeout=MODEL_CONFIG["timeout"],
                max_retries=MODEL_CONFIG["max_retries"],
                google_api_key=api_key,
                convert_system_message_to_human=MODEL_CONFIG["convert_system_message_to_human"]
            )
            print("✨ Model initialized successfully")
            return self.model
        except Exception as e:
            print(f"❌ Error creating language model: {e}")
            print("Please check your Google API key and try again")
            sys.exit(1) 