"""
Configuration utilities for the OC-AiCrash-Skill toolkit.

Simple environment-based configuration using python-dotenv.
No external dependencies beyond python-dotenv and pydantic.

Usage:
    from aicrasher.config import get_config
    config = get_config()
    
    # Check feature flags
    if config.feature_enabled("knowledge_search"):
        # Feature-specific logic
        pass
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

from dotenv import load_dotenv
from pydantic import BaseModel, Field, validator


def _load_dotenv_from_multiple_locations(override: bool = False) -> None:
    """Load .env from multiple possible locations.
    
    Search order:
    1. Current working directory (default behavior)
    2. Project root (where this module is located)
    3. User home directory
    
    Args:
        override: If True, override existing environment variables with new values.
                  If False (default), existing values are preserved.
    
    Note:
        When override=True, later files take precedence (project root > cwd).
        When override=False, earlier files take precedence (cwd > project root).
    """
    # 1. Current working directory (default)
    load_dotenv(override=override)
    
    # 2. Project root (aicrasher package directory's parent's parent)
    #    e.g., /path/to/oc-aicrash-skill/.env
    project_root = Path(__file__).resolve().parent.parent.parent
    project_env = project_root / ".env"
    if project_env.exists():
        load_dotenv(project_env, override=override)
    
    # 3. User home directory
    home_env = Path.home() / ".env"
    if home_env.exists():
        load_dotenv(home_env, override=override)


# Initial load at module import time
_load_dotenv_from_multiple_locations()


def _get_bool(key: str, default: bool = False) -> bool:
    """Get boolean from environment variable."""
    value = os.getenv(key, str(default)).lower()
    return value in ("true", "1", "yes", "on")


def _get_int(key: str, default: int) -> int:
    """Get integer from environment variable."""
    return int(os.getenv(key, str(default)))


def _get_str(key: str, default: str = "") -> str:
    """Get string from environment variable."""
    return os.getenv(key, default)


def _get_optional_str(key: str) -> Optional[str]:
    """Get optional string from environment variable."""
    value = os.getenv(key)
    return value if value else None


class AppConfig(BaseModel):
    """Application-level configuration loaded from environment variables.
    
    Note:
        Each time an AppConfig instance is created, the .env files are re-read
        with override=True. This allows configuration changes to take effect
        without restarting the MCP server - just create a new AppConfig instance
        (e.g., by starting a new crash session).
    """

    # ==================== LLM API Settings ====================
    openai_api_key: Optional[str] = Field(
        default_factory=lambda: _get_optional_str("OPENAI_API_KEY"),
        description="API key for OpenAI platform.",
    )
    openai_base_url: Optional[str] = Field(
        default_factory=lambda: _get_optional_str("OPENAI_BASE_URL"),
        description="Optional override for OpenAI API base URL.",
    )
    openai_model: str = Field(
        default_factory=lambda: _get_str("OPENAI_MODEL", "gpt-4o-mini"),
        description="Target OpenAI model.",
    )
    azure_openai_api_key: Optional[str] = Field(
        default_factory=lambda: _get_optional_str("AZURE_OPENAI_API_KEY"),
        description="Azure OpenAI API key.",
    )
    azure_openai_endpoint: Optional[str] = Field(
        default_factory=lambda: _get_optional_str("AZURE_OPENAI_ENDPOINT"),
        description="Azure OpenAI endpoint URL.",
    )
    azure_openai_api_version: str = Field(
        default_factory=lambda: _get_str("AZURE_OPENAI_API_VERSION", "2024-02-15-preview"),
        description="Azure OpenAI API version.",
    )
    azure_openai_deployment: Optional[str] = Field(
        default_factory=lambda: _get_optional_str("AZURE_OPENAI_DEPLOYMENT"),
        description="Azure OpenAI deployment name.",
    )

    # ==================== Application Settings ====================
    cli_tool: str = Field(
        default_factory=lambda: _get_str("AICRASHER_CLI_TOOL", "auto"),
        description="CLI tool preference: codebuddy | claude | mcporter | auto",
    )
    workspace_root: Path = Field(
        default_factory=lambda: Path(_get_str("AICRASHER_WORKSPACE") or Path.cwd()),
        description="Root directory for generated artifacts.",
    )

    # ==================== Crash Utility Settings ====================
    crash_binary: str = Field(
        default_factory=lambda: _get_str("CRASH_BINARY", "crash"),
        description="Path to the crash CLI utility.",
    )
    crash_timeout_seconds: int = Field(
        default_factory=lambda: _get_int("CRASH_TIMEOUT_SECONDS", 300),
        description="Maximum time to wait for a crash command.",
    )
    crash_output_max_chars: int = Field(
        default_factory=lambda: _get_int("CRASH_OUTPUT_MAX_CHARS", 16384),
        description="Max output characters for single crash command.",
    )
    crash_batch_output_max_chars: int = Field(
        default_factory=lambda: _get_int("CRASH_BATCH_OUTPUT_MAX_CHARS", 32768),
        description="Max output characters for batch commands.",
    )

    # ==================== AI Orchestration Settings ====================
    max_ai_rounds: int = Field(
        default_factory=lambda: _get_int("MAX_AI_ROUNDS", 12),
        description="Max AI planning iterations.",
    )
    ai_command_batch_size: int = Field(
        default_factory=lambda: _get_int("AI_COMMAND_BATCH_SIZE", 3),
        description="Max crash commands per AI round.",
    )

    # ==================== Knowledge Base Settings ====================
    knowledge_base_paths: List[Path] = Field(
        default_factory=lambda: [
            Path(p.strip())
            for p in _get_str("KNOWLEDGE_BASE_PATHS", "").split(os.pathsep)
            if p.strip()
        ],
        description="Local knowledge base paths.",
    )
    redhat_kb_enabled: bool = Field(
        default_factory=lambda: _get_bool("REDHAT_KB_ENABLED", True),
        description="Enable Red Hat KB integration.",
    )
    redhat_kb_base_url: str = Field(
        default_factory=lambda: _get_str(
            "REDHAT_KB_BASE_URL",
            "https://access.redhat.com/hydra/rest/search/platform/kbase",
        ),
        description="Red Hat KB API URL.",
    )
    redhat_kb_timeout_seconds: int = Field(
        default_factory=lambda: _get_int("REDHAT_KB_TIMEOUT_SECONDS", 10),
        description="Red Hat KB API timeout.",
    )
    redhat_kb_max_results: int = Field(
        default_factory=lambda: _get_int("REDHAT_KB_MAX_RESULTS", 5),
        description="Max Red Hat KB results.",
    )

    # ==================== Feature Flags ====================
    feature_basic_analysis: bool = Field(
        default_factory=lambda: _get_bool("FEATURE_BASIC_ANALYSIS", True),
    )
    feature_knowledge_search: bool = Field(
        default_factory=lambda: _get_bool("FEATURE_KNOWLEDGE_SEARCH", True),
    )
    feature_report_generation: bool = Field(
        default_factory=lambda: _get_bool("FEATURE_REPORT_GENERATION", True),
    )

    # ==================== Methods ====================
    def azure_enabled(self) -> bool:
        """Return True when Azure OpenAI is fully configured."""
        return bool(self.azure_openai_endpoint and self.azure_openai_deployment)

    def get_preferred_cli_tool(self) -> str:
        """
        Get the preferred CLI tool based on configuration.
        
        Returns:
            CLI tool name: "codebuddy", "claude", or "mcporter"
            Returns "auto" if no explicit preference (let caller detect).
        """
        if self.cli_tool and self.cli_tool != "auto":
            return self.cli_tool
        return "auto"

    def feature_enabled(self, feature_name: str) -> bool:
        """
        Check if a feature is enabled.
        
        Args:
            feature_name: Feature name (e.g., "knowledge_search")
        
        Returns:
            True if the feature is enabled.
        """
        attr_name = f"feature_{feature_name}"
        return getattr(self, attr_name, False)

    @validator("workspace_root", pre=True)
    def _expand_workspace(cls, value):
        if isinstance(value, str):
            return Path(value).expanduser().resolve() if value else Path.cwd()
        return value

    @validator("knowledge_base_paths", each_item=True, pre=True)
    def _expand_kb(cls, value):
        if isinstance(value, str):
            return Path(value).expanduser().resolve()
        return value

    class Config:
        arbitrary_types_allowed = True
        validate_assignment = True


# Global config instance (lazy loaded)
_app_config: Optional[AppConfig] = None


def get_config() -> AppConfig:
    """Get the global AppConfig instance."""
    global _app_config
    if _app_config is None:
        _app_config = AppConfig()
    return _app_config


def reload_config() -> AppConfig:
    """Force reload config from environment.
    
    Reloads .env files from all configured locations with override=True,
    then creates a new AppConfig instance.
    """
    global _app_config
    _load_dotenv_from_multiple_locations(override=True)
    _app_config = AppConfig()
    return _app_config


def get_fresh_config() -> AppConfig:
    """Get a fresh AppConfig instance with latest .env values.
    
    Unlike get_config() which returns a cached instance, this function
    always reloads .env files and creates a new AppConfig.
    
    Use this when you need to pick up .env changes without restarting
    the MCP server.
    """
    _load_dotenv_from_multiple_locations(override=True)
    return AppConfig()


__all__ = ["AppConfig", "get_config", "reload_config", "get_fresh_config"]
