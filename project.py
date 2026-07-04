"""Global configuration: LLM initialization, prompt loading, input parsing.

All user-visible configuration is limited to:
  - base_url / api_key / model_name (for LLM)
  - workflow.max_test_attempts
  - knowledge_base.output_dir

Agent definitions (prompt files, tool expert types) are hardcoded here
as internal defaults rather than exposed in user-facing config.
"""

import json
from pathlib import Path

from langchain_openai import ChatOpenAI

PROJECT_ROOT = Path(__file__).resolve().parent

# ---------------------------------------------------------------------------
# Hardcoded agent & tool-expert definitions
# ---------------------------------------------------------------------------

DEFAULT_AGENT_CONFIGS: dict[str, dict] = {
    "validator": {
        "prompt_file": "prompts/validator.md",
    },
    "pm": {
        "prompt_file": "prompts/pm.md",
    },
    "kernel_expert": {
        "prompt_file": "prompts/kernel_expert.md",
    },
    "test_expert": {
        "prompt_file": "prompts/test_expert.md",
    },
    "knowledge_base": {
        "prompt_file": "prompts/knowledge_base.md",
    },
}

DEFAULT_TOOL_EXPERTS: list[dict] = [
    {
        "type": "knowledge_search",
        "name": "历史知识库搜索专家",
        "description": "搜索历史知识库，查找与当前问题相似的历史案例和解决方案",
        "prompt_file": "prompts/knowledge_search.md",
    },
    {
        "type": "lock_analysis",
        "name": "锁分析专家",
        "description": "分析内核锁相关问题，包括死锁、锁竞争、锁顺序等",
        "prompt_file": "prompts/lock_analysis.md",
    },
    {
        "type": "crash_analysis",
        "name": "Crash分析专家",
        "description": "分析 Crash 日志，定位崩溃原因和调用栈",
        "prompt_file": "prompts/crash_analysis.md",
    },
    {
        "type": "kernel_log_analysis",
        "name": "内核日志分析专家",
        "description": "分析内核日志（dmesg/logcat等），提取关键错误信息和异常模式",
        "prompt_file": "prompts/kernel_log_analysis.md",
    },
]

# ---------------------------------------------------------------------------
# LLM factory
# ---------------------------------------------------------------------------


def build_llm(config: dict) -> ChatOpenAI:
    """Create a ChatOpenAI instance from the unified config.

    Expects ``config`` to contain at minimum:
        base_url, api_key, model_name
    (all optional; missing values fall back to defaults).
    """
    return ChatOpenAI(
        model=config.get("model_name", "gpt-4o-mini"),
        api_key=config.get("api_key", ""),
        base_url=config.get("base_url") or None,
        temperature=float(config.get("temperature", 0)),
    )


# ---------------------------------------------------------------------------
# Config file I/O
# ---------------------------------------------------------------------------


def load_config(config_path: str) -> dict:
    """Load maintenance configuration JSON file."""
    p = Path(config_path) if Path(config_path).is_absolute() else PROJECT_ROOT / config_path
    return json.loads(p.read_text(encoding="utf-8"))


def load_prompt_from_file(path: str) -> str:
    """Load prompt file from absolute or project-relative path."""
    p = Path(path) if Path(path).is_absolute() else PROJECT_ROOT / path
    return p.read_text(encoding="utf-8")


def resolve_project_path(path: str) -> Path:
    """Resolve absolute or project-relative path."""
    p = Path(path)
    if p.is_absolute():
        return p
    return PROJECT_ROOT / p


# ---------------------------------------------------------------------------
# input.txt parser
# ---------------------------------------------------------------------------

INPUT_FILE_FIELDS = {
    "Bug Promote",
    "vmcore 文件",
    "vmlinux 文件",
    "boot_kernel 文件",
    "kernel_source 文件",
}


def parse_input_file(file_path: str) -> dict[str, str]:
    """Parse a structured input file (key: value per line) into a field dict.

    Expected format (see ``input.txt.template``)::

        Bug Promote: <description+fault type>
        vmcore 文件: <path>
        vmlinux 文件: <path>
        boot_kernel 文件: <path>
        kernel_source 文件: <path>

    Lines starting with ``#`` are ignored.  Leading/trailing whitespace is
    stripped from both keys and values.
    """
    p = Path(file_path)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    text = p.read_text(encoding="utf-8")

    fields: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if ":" in line:
            key, _, value = line.partition(":")
            fields[key.strip()] = value.strip()

    # Return only recognised fields so unknown keys don't leak through.
    return {k: v for k, v in fields.items() if k in INPUT_FILE_FIELDS}


def format_user_input(fields: dict[str, str]) -> str:
    """Format parsed input fields into a single user_input string for the workflow."""
    lines = [f"{k}: {v}" for k, v in fields.items() if v]
    return "\n".join(lines)
