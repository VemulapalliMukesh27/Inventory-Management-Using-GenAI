"""
config.py

This module loads environment variables and configures API keys for the application.
"""

import os
from dataclasses import dataclass

import google.generativeai as genai
from dotenv import load_dotenv

# Shared default for all Gemini call sites. Override with GEMINI_MODEL_NAME.
# gemini-1.5-flash is shut down; gemini-3.5-flash is the current Flash GA model.
DEFAULT_GEMINI_MODEL = os.getenv("GEMINI_MODEL_NAME", "gemini-3.5-flash")


@dataclass(frozen=True)
class Settings:
    google_api_key: str | None
    pandasai_api_key: str | None

    @property
    def missing_credentials(self) -> list[str]:
        missing = []
        if not self.google_api_key:
            missing.append("GOOGLE_API_KEY")
        if not self.pandasai_api_key:
            missing.append("PANDASAI_API_KEY")
        return missing


def _read_env(name: str) -> str | None:
    value = os.getenv(name)
    return value if isinstance(value, str) and value else None


# Load environment variables from the .env file
load_dotenv()

SETTINGS = Settings(
    google_api_key=_read_env("GOOGLE_API_KEY"),
    pandasai_api_key=_read_env("PANDASAI_API_KEY"),
)

# Configure Google API key for Gemini model
GOOGLE_API_KEY = SETTINGS.google_api_key
if GOOGLE_API_KEY:
    genai.configure(api_key=GOOGLE_API_KEY)

# Configure PandasAI API key
PANDASAI_API_KEY = SETTINGS.pandasai_api_key
if PANDASAI_API_KEY:
    os.environ["PANDASAI_API_KEY"] = PANDASAI_API_KEY

MISSING_CREDENTIALS = SETTINGS.missing_credentials
