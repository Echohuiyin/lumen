import json
import os
import re
import sys
from pathlib import Path

from langchain_openai import ChatOpenAI

from agents.backends import AnthropicBackend, CLIBackend, ClaudeCodeBackend, HTTPBackend
from paths import PROJECT_ROOT, resolve_aicrasher_path

# Add aicrasher to Python path for crash session management (from submodule)
aicrasher_path = str(resolve_aicrasher_path())
if aicrasher_path not in sys.path:
    sys.path.insert(0, aicrasher_path)

# Agents that run in automated workflow and must NOT use CLI backend
AUTOMATION_AGENTS = [
    "validator",
    "pm",
    "kernel_expert",
    "test_expert",
    "knowledge_base",
    "evaluation",
    "improvement",
]

# Automation agents permitted to use the Claude Code CLI backend.
# These agents delegate to `claude -p` whose own agent loop is mature enough
# to run without interactive prompts (unlike raw CLIBackend).
CLAUDE_CODE_ALLOWED = {"kernel_expert"}

DEFAULT_CONFIG_PATH = "config.json"

# ---------------------------------------------------------------------------
# Code-side defaults — these are baked into the binary so config.json only
# needs the bare essentials (api_key, base_url, model_name).
# ---------------------------------------------------------------------------


def validate_agent_backend(agent_name: str, backend: str) -> None:
    """Validate that automation agents don't use raw CLI backend.

    CLI backend requires interactive user input and will block the workflow.
    Automation agents must use OpenAI, HTTP, or Claude Code backend for
    automatic execution. Claude Code (`claude -p`) is allowed for agents in
    CLAUDE_CODE_ALLOWED because it runs non-interactively with its own agent
    loop.
    """
    if agent_name in AUTOMATION_AGENTS and backend == "cli":
        raise ValueError(
            f"Agent '{agent_name}' is an automation agent and cannot use CLI backend. "
            f"CLI backend requires interactive user input and will block the workflow. "
            f"Please use 'openai', 'http', or 'claude_code' backend instead."
        )
    if agent_name in AUTOMATION_AGENTS and backend == "claude_code" and agent_name not in CLAUDE_CODE_ALLOWED:
        raise ValueError(
            f"Agent '{agent_name}' is not permitted to use claude_code backend. "
            f"Only agents in CLAUDE_CODE_ALLOWED may use it."
        )


def get_llm_with_config(agent_config: dict, *, default_config: dict | None = None, agent_name: str = ""):
    """Create LLM backend instance from agent-level config, falling back to default_config.

    Returns one of: ChatOpenAI, AnthropicBackend, CLIBackend, HTTPBackend
    depending on the 'backend' field in config.
    """
    defaults = default_config or {}
    backend = agent_config.get("backend") or defaults.get("backend", "anthropic")

    # Validate backend type for automation agents
    if agent_name:
        validate_agent_backend(agent_name, backend)

    if backend == "openai":
        return ChatOpenAI(
            model=agent_config.get("model_name") or defaults.get("model_name", "gpt-4o-mini"),
            api_key=agent_config.get("api_key") or defaults.get("api_key", ""),
            base_url=agent_config.get("base_url") or defaults.get("base_url"),
            temperature=float(agent_config.get("temperature") if agent_config.get("temperature") is not None else defaults.get("temperature", 0)),
        )
    elif backend == "anthropic":
        return AnthropicBackend(
            base_url=agent_config.get("base_url") or defaults.get("base_url", ""),
            api_key=agent_config.get("api_key") or defaults.get("api_key", ""),
            model_name=agent_config.get("model_name") or defaults.get("model_name", ""),
            temperature=float(agent_config.get("temperature") if agent_config.get("temperature") is not None else defaults.get("temperature", 0)),
            timeout=int(agent_config.get("http_timeout") if agent_config.get("http_timeout") is not None else defaults.get("http_timeout", 120)),
            max_tokens=int(agent_config.get("max_tokens") or defaults.get("max_tokens") or 8192),
        )
    elif backend == "cli":
        cli_command = agent_config.get("cli_command") or defaults.get("cli_command", "")
        if not cli_command:
            raise ValueError("CLI backend requires 'cli_command' in config")
        return CLIBackend(
            cli_command=cli_command,
            timeout=int(agent_config.get("cli_timeout", defaults.get("cli_timeout", 120))),
            cli_stdin=bool(agent_config.get("cli_stdin", defaults.get("cli_stdin", False))),
        )
    elif backend == "claude_code":
        return ClaudeCodeBackend(
            cli_command=agent_config.get("cli_command") or defaults.get("cli_command", "claude"),
            cli_timeout=int(agent_config.get("cli_timeout") if agent_config.get("cli_timeout") is not None else defaults.get("cli_timeout", 600)),
            model=agent_config.get("model") or agent_config.get("model_name") or defaults.get("model") or defaults.get("model_name", "sonnet"),
            permission_mode=agent_config.get("permission_mode") or defaults.get("permission_mode", "bypassPermissions"),
            max_turns=int(agent_config.get("max_turns") if agent_config.get("max_turns") is not None else defaults.get("max_turns", 100)),
            settings_file=agent_config.get("settings_file") or defaults.get("settings_file", ""),
            semcode_mcp=agent_config.get("semcode_mcp") or defaults.get("semcode_mcp", {}),
            disable_skills=bool(agent_config.get("disable_skills", defaults.get("disable_skills", False))),
        )
    elif backend == "http":
        url = agent_config.get("http_url") or defaults.get("http_url", "")
        if not url:
            raise ValueError("HTTP backend requires 'http_url' in config")
        return HTTPBackend(
            url=url,
            headers=agent_config.get("http_headers") or defaults.get("http_headers"),
            timeout=int(agent_config.get("http_timeout", defaults.get("http_timeout", 120))),
            model_name=agent_config.get("model_name") or defaults.get("model_name", ""),
            response_path=agent_config.get("http_response_path") or defaults.get("http_response_path", "choices.0.message.content"),
        )
    else:
        raise ValueError(f"Unknown backend type: {backend!r}. Expected 'openai', 'anthropic', 'cli', 'claude_code', or 'http'.")


def load_prompt_from_file(path: str) -> str:
    """Load prompt file from absolute or project-relative path."""
    p = Path(path) if Path(path).is_absolute() else PROJECT_ROOT / path
    return p.read_text(encoding="utf-8")


def _resolve_env_vars(text: str) -> str:
    """Resolve ${VAR:-default} and ${VAR} style environment variable references.

    Supports:
      ${VAR}            — must be set, raises KeyError if unset
      ${VAR:-default}   — uses VAR if set, else 'default'
      ${VAR-default}    — uses VAR if set and non-empty, else 'default'
      $HOME / $USER     — classic shell-style (simple form, no braced, only known
                           vars to avoid false positives)

    Uses iterative substitution so nested ${A:-${B:-x}} resolves correctly.
    The inner-most brace is resolved first on each pass.
    """
    # Known vars we allow for simple $VAR style (no braces)
    _SIMPLE_VARS = {"HOME", "USER", "LUMEN_OUTPUT_DIR", "KERNEL_SOURCE_DIR",
                    "SEMCODE_MCP_BIN", "CLAUDE_SETTINGS", "SEMCODE_DB_DIR"}

    def _resolve_simple(m: re.Match) -> str:
        return os.environ.get(m.group(1), "")

    def _replace_one_braced(m: re.Match) -> str:
        """Replace the innermost ${...} that contains no nested braces."""
        inner = m.group(1)
        if ":-" in inner:
            var, default = inner.split(":-", 1)
            return os.environ.get(var.strip(), default.strip())
        elif "-" in inner and not inner.startswith("-"):
            var, default = inner.split("-", 1)
            val = os.environ.get(var.strip())
            return val if val else default.strip()
        else:
            var = inner.strip()
            return os.environ[var]

    result = text
    # Iterate: on each pass, resolve only innermost (no nested braces) ${...}
    prev = None
    while result != prev:
        prev = result
        result = re.sub(
            r"\$\{([^${}]+)\}",  # no braces inside
            _replace_one_braced,
            result,
        )

    # Simple $VAR style for known vars
    result = re.sub(
        r"(?<!\$)\$(?!\$|\{)(" + "|".join(_SIMPLE_VARS) + r")",
        lambda m: os.environ.get(m.group(1), ""),
        result,
    )
    return result


def load_config(config_path: str = DEFAULT_CONFIG_PATH) -> dict:
    """Load configuration JSON file with environment variable resolution.

    Performs ${VAR} / ${VAR:-default} substitution from environment, then
    parses JSON. No fallbacks — the config file must be complete and valid.

    Args:
        config_path: Path to config JSON file (default "config.json")

    Returns:
        Parsed config dict

    Raises:
        FileNotFoundError: if config file doesn't exist
        ValueError: if config file has invalid JSON
    """
    p = Path(config_path) if Path(config_path).is_absolute() else PROJECT_ROOT / config_path
    if not p.exists():
        raise FileNotFoundError(
            f"Config file not found: {p}. "
            f"Copy config.json.template to config.json and fill in your API key."
        )

    config_raw = p.read_text(encoding="utf-8")
    config_raw = _resolve_env_vars(config_raw)

    try:
        config = json.loads(config_raw)
    except json.JSONDecodeError as e:
        raise ValueError(f"Failed to parse config file {p}: {e}")

    return config


def resolve_project_path(path: str) -> Path:
    """Resolve absolute or project-relative path."""
    p = Path(path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


def create_crash_session(vmcore_path: str, vmlinux_path: str) -> "CrashSessionManager":
    """Create crash analysis session using aicrasher.

    Args:
        vmcore_path: Path to vmcore dump file
        vmlinux_path: Path to vmlinux image with debug symbols

    Returns:
        CrashSessionManager instance (started and ready for commands)

    Raises:
        ImportError: If aicrasher not available
        FileNotFoundError: If vmcore or vmlinux doesn't exist
        RuntimeError: If crash session fails to start
    """
    from aicrasher.crash_session import CrashSessionManager
    from aicrasher.config import AppConfig

    vmcore = Path(vmcore_path)
    vmlinux = Path(vmlinux_path)

    if not vmcore.exists():
        raise FileNotFoundError(f"vmcore not found: {vmcore_path}")
    if not vmlinux.exists():
        raise FileNotFoundError(f"vmlinux not found: {vmlinux_path}")

    config = AppConfig()
    session = CrashSessionManager(
        vmcore_path=vmcore,
        vmlinux_path=vmlinux,
        config=config,
    )
    session.start()
    return session
