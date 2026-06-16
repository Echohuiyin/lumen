import json
import sys
from pathlib import Path

from langchain_openai import ChatOpenAI

from agents.backends import CLIBackend, HTTPBackend

PROJECT_ROOT = Path(__file__).resolve().parent

# 本地技能目录（替代外部技能路径）
SKILLS_DIR = PROJECT_ROOT / "skills"

# 本地aicrasher模块路径 - 需要将父目录加入sys.path才能导入aicrasher
AICRASHER_PATH = SKILLS_DIR / "shared" / "aicrasher"
AICRASHER_PARENT = SKILLS_DIR / "shared"
if AICRASHER_PARENT.exists() and str(AICRASHER_PARENT) not in sys.path:
    sys.path.insert(0, str(AICRASHER_PARENT))

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


def get_skill_path(skill_name: str) -> Path | None:
    """获取本地技能路径

    Args:
        skill_name: 技能名称 (e.g., "kernel-fault-injection")

    Returns:
        技能目录路径，不存在则返回None
    """
    local_path = SKILLS_DIR / skill_name
    return local_path if local_path.exists() else None


def get_skill_script_path(skill_name: str, script_name: str) -> Path | None:
    """获取技能脚本路径

    Args:
        skill_name: 技能名称
        script_name: 脚本文件名

    Returns:
        脚本路径，不存在则返回None
    """
    skill_path = get_skill_path(skill_name)
    if skill_path is None:
        return None

    script_path = skill_path / "scripts" / script_name
    return script_path if script_path.exists() else None


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

    Returns one of: ChatOpenAI, CLIBackend, HTTPBackend
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
        raise ValueError(f"Unknown backend type: {backend!r}. Expected 'openai', 'cli', or 'http'.")


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
