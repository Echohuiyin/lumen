import json
import os
from pathlib import Path

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI

load_dotenv()

PROJECT_ROOT = Path(__file__).resolve().parent


def _read_env(key: str, default: str | None = None) -> str | None:
    value = os.getenv(key)
    if value is None or value == "":
        return default
    return value


def get_llm_with_config(agent_config: dict) -> ChatOpenAI:
    """Create LLM instance from agent-level config (independent model/temperature/api_key)."""
    return ChatOpenAI(
        model=agent_config.get("model_name") or _read_env("MODEL_NAME", "gpt-4o-mini"),
        api_key=agent_config.get("api_key") or _read_env("OPENAI_API_KEY"),
        base_url=agent_config.get("base_url") or _read_env("OPENAI_BASE_URL"),
        temperature=float(agent_config.get("temperature", 0)),
    )


def load_prompt_from_file(path: str) -> str:
    """Load prompt file from absolute or project-relative path."""
    p = Path(path) if Path(path).is_absolute() else PROJECT_ROOT / path
    return p.read_text(encoding="utf-8")


def load_config(config_path: str) -> dict:
    """Load maintenance configuration JSON file."""
    p = Path(config_path) if Path(config_path).is_absolute() else PROJECT_ROOT / config_path
    return json.loads(p.read_text(encoding="utf-8"))


def resolve_project_path(path: str) -> Path:
    """Resolve absolute or project-relative path."""
    p = Path(path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p
