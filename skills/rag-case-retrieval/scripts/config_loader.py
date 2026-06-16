"""
Shared configuration loader for RAG skill scripts.

Resolves embedding configuration with priority:
  1. Environment variables (highest)
  2. config.json values
  3. Hardcoded defaults (lowest)
"""

import os
from pathlib import Path
from typing import Dict


def _try_load_dotenv():
    """Load .env from the project root if python-dotenv is available."""
    try:
        from dotenv import load_dotenv
        project_root = Path(__file__).resolve().parents[4]
        env_file = project_root / ".env"
        if env_file.exists():
            load_dotenv(env_file)
    except ImportError:
        pass


_try_load_dotenv()

# Mapping: env var name -> config key, default value
_EMBEDDING_ENV_MAP = {
    "EMBEDDING_BASE_URL":    ("base_url",     "http://localhost:11434/v1"),
    "EMBEDDING_MODEL":       ("model",        "bge-large-zh"),
    "EMBEDDING_API_KEY":     ("api_key",      "not-required"),
    "EMBEDDING_TIMEOUT":     ("timeout",      30),
    "EMBEDDING_BATCH_SIZE":  ("batch_size",   100),
    "EMBEDDING_DIMENSION":   ("dimension",    1024),
    "EMBEDDING_MAX_TOKENS":  ("max_tokens",   512),
    "EMBEDDING_MAX_RETRIES": ("max_retries",  3),
}


def get_embedding_config(config: Dict = None) -> Dict:
    """
    Return resolved embedding config.

    Priority: env var > config.json > hardcoded default.

    Args:
        config: The full config dict as read from config.json (may be None or empty).

    Returns:
        Dict with all embedding keys resolved.
    """
    embedding_from_file = (config or {}).get("embedding", {})

    resolved = {}
    for env_var, (key, default) in _EMBEDDING_ENV_MAP.items():
        env_value = os.environ.get(env_var)
        if env_value is not None:
            # Convert numeric types from string env vars
            if key in ("timeout", "batch_size", "dimension", "max_tokens", "max_retries"):
                try:
                    resolved[key] = int(env_value)
                except ValueError:
                    resolved[key] = default
            else:
                resolved[key] = env_value
        elif key in embedding_from_file:
            resolved[key] = embedding_from_file[key]
        else:
            resolved[key] = default

    return resolved
