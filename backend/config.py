"""
Fail-fast environment configuration.
All required variables raise at import time if missing.
"""

import os
from dotenv import load_dotenv

load_dotenv()


def _require(key: str) -> str:
    value = os.getenv(key)
    if not value:
        raise RuntimeError(
            f"Missing required environment variable: {key}\n"
            f"Copy .env.example to .env and fill in the values."
        )
    return value


AZURE_OPENAI_API_KEY        = _require("AZURE_OPENAI_API_KEY")
AZURE_OPENAI_ENDPOINT       = _require("AZURE_OPENAI_ENDPOINT")
AZURE_OPENAI_API_VERSION    = os.getenv("AZURE_OPENAI_API_VERSION", "2024-02-01")
AZURE_OPENAI_CHAT_DEPLOYMENT = _require("AZURE_OPENAI_CHAT_DEPLOYMENT")

API_PORT        = int(os.getenv("API_PORT", "3001"))
ALLOWED_ORIGINS = os.getenv("ALLOWED_ORIGINS", "http://localhost:3000").split(",")

CHECKPOINTS_DB  = os.getenv("CHECKPOINTS_DB", "./checkpoints.db")

# Interactive API docs (/docs, /openapi.json). On by default for local demos;
# set ENABLE_API_DOCS=false anywhere the API is reachable by others.
ENABLE_API_DOCS = os.getenv("ENABLE_API_DOCS", "true").strip().lower() in ("1", "true", "yes")
