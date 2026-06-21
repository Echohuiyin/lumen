import json
import sys
from pathlib import Path

from langchain_openai import ChatOpenAI

from agents.backends import AnthropicBackend, CLIBackend, HTTPBackend
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

DEFAULT_CONFIG_PATH = "config.json"
LEGACY_CONFIG_PATH = "maintenance_config.json"


def load_claude_settings(settings_file: str | None = None) -> dict:
    """Load Claude Code settings from ~/.claude/settings.json.

    Returns env vars that can be used as LLM configuration fallback.
    """
    if settings_file:
        settings_path = Path(settings_file).expanduser()
    else:
        settings_path = Path.home() / ".claude" / "settings.json"
    if not settings_path.exists():
        return {}

    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        env = settings.get("env", {})

        # Map Claude settings env vars to LLM config format
        model_name = (
            env.get("ANTHROPIC_MODEL", "")
            or settings.get("model", "")
            or env.get("ANTHROPIC_DEFAULT_OPUS_MODEL", "")
            or env.get("ANTHROPIC_DEFAULT_SONNET_MODEL", "")
            or env.get("ANTHROPIC_DEFAULT_HAIKU_MODEL", "")
        )
        return {
            "api_key": env.get("ANTHROPIC_AUTH_TOKEN", ""),
            "base_url": env.get("ANTHROPIC_BASE_URL", ""),
            "model_name": model_name,
        }
    except Exception:
        return {}


def validate_agent_backend(agent_name: str, backend: str) -> None:
    """Validate that automation agents don't use CLI backend.

    CLI backend requires interactive user input and will block the workflow.
    Automation agents must use OpenAI or HTTP backend for automatic execution.
    """
    if agent_name in AUTOMATION_AGENTS and backend == "cli":
        raise ValueError(
            f"Agent '{agent_name}' is an automation agent and cannot use CLI backend. "
            f"CLI backend requires interactive user input and will block the workflow. "
            f"Please use 'openai' or 'http' backend instead."
        )


def get_llm_with_config(agent_config: dict, *, default_config: dict | None = None, agent_name: str = ""):
    """Create LLM backend instance from agent-level config, falling back to default_config.

    Returns one of: ChatOpenAI, AnthropicBackend, CLIBackend, HTTPBackend
    depending on the 'backend' field in config.
    """
    defaults = default_config or {}
    backend = agent_config.get("backend") or defaults.get("backend", "openai")

    # Validate backend type for automation agents
    if agent_name:
        validate_agent_backend(agent_name, backend)

    if backend == "openai":
        return ChatOpenAI(
            model=agent_config.get("model_name") or defaults.get("model_name", "gpt-4o-mini"),
            api_key=agent_config.get("api_key") or defaults.get("api_key", ""),
            base_url=agent_config.get("base_url") or defaults.get("base_url"),
            temperature=float(agent_config.get("temperature", defaults.get("temperature", 0))),
        )
    elif backend == "anthropic":
        return AnthropicBackend(
            base_url=agent_config.get("base_url") or defaults.get("base_url", ""),
            api_key=agent_config.get("api_key") or defaults.get("api_key", ""),
            model_name=agent_config.get("model_name") or defaults.get("model_name", ""),
            temperature=float(agent_config.get("temperature", defaults.get("temperature", 0))),
            timeout=int(agent_config.get("http_timeout", defaults.get("http_timeout", 120))),
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
        raise ValueError(f"Unknown backend type: {backend!r}. Expected 'openai', 'anthropic', 'cli', or 'http'.")


def load_prompt_from_file(path: str) -> str:
    """Load prompt file from absolute or project-relative path."""
    p = Path(path) if Path(path).is_absolute() else PROJECT_ROOT / path
    return p.read_text(encoding="utf-8")


def _normalize_tool_experts_config(config: dict) -> dict:
    """Support legacy configs that store tool experts under agents.tool_expert."""
    if config.get("tool_experts"):
        return config

    legacy_tool_experts = config.get("agents", {}).get("tool_expert", {})
    if not isinstance(legacy_tool_experts, dict) or not legacy_tool_experts:
        return config

    names = {
        "knowledge_search": "历史知识库搜索专家",
        "lock_analysis": "锁分析专家",
        "crash_analysis": "Crash分析专家",
        "kernel_log_analysis": "内核日志分析专家",
    }
    descriptions = {
        "knowledge_search": "搜索历史知识库，查找与当前问题相似的历史案例和解决方案",
        "lock_analysis": "分析内核锁相关问题，包括死锁、锁竞争、锁顺序等",
        "crash_analysis": "分析 Crash 日志，定位崩溃原因和调用栈",
        "kernel_log_analysis": "分析内核日志，提取关键错误信息和异常模式",
    }

    config["tool_experts"] = [
        {
            "type": expert_type,
            "name": names.get(expert_type, expert_type),
            "description": descriptions.get(expert_type, ""),
            "agent": agent_config,
        }
        for expert_type, agent_config in legacy_tool_experts.items()
        if isinstance(agent_config, dict)
    ]
    return config


def load_config(config_path: str, fallback_to_claude_settings: bool = True) -> dict:
    """Load maintenance configuration JSON file.

    If config file has empty api_key/base_url/model_name, fallback to ~/.claude/settings.json.

    Args:
        config_path: Path to config JSON file
        fallback_to_claude_settings: Whether to use Claude settings as fallback (default True)

    Returns:
        Config dict with LLM settings resolved
    """
    p = Path(config_path) if Path(config_path).is_absolute() else PROJECT_ROOT / config_path
    if (
        not p.exists()
        and not Path(config_path).is_absolute()
        and config_path == LEGACY_CONFIG_PATH
        and (PROJECT_ROOT / DEFAULT_CONFIG_PATH).exists()
    ):
        p = PROJECT_ROOT / DEFAULT_CONFIG_PATH

    if not p.exists():
        # Config file doesn't exist, try to use Claude settings directly
        if fallback_to_claude_settings:
            claude_settings = load_claude_settings()
            if claude_settings.get("api_key"):
                return {
                    "default": {
                        "backend": "openai",
                        **claude_settings,
                        "temperature": 0,
                    }
                }
        return {}

    config = _normalize_tool_experts_config(json.loads(p.read_text(encoding="utf-8")))

    # Fill empty LLM config from Claude settings
    if fallback_to_claude_settings:
        default = config.get("default", {})
        claude_settings = load_claude_settings(default.get("settings_file"))

        if not default.get("api_key") and claude_settings.get("api_key"):
            default["api_key"] = claude_settings["api_key"]
        if not default.get("base_url") and claude_settings.get("base_url"):
            default["base_url"] = claude_settings["base_url"]
        if not default.get("model_name") and claude_settings.get("model_name"):
            default["model_name"] = claude_settings["model_name"]

        config["default"] = default

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
