"""Global configuration: prompt loading, input parsing.

User-facing configuration is handled by ``config.py`` (LLM backends, agent
defaults).  This module provides lower-level project I/O helpers.
"""

import json
import os
import re
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent


def _resolve_env_vars(text: str) -> str:
    """Resolve ${VAR:-default} and ${VAR} in text (same as llm_config.py).

    Supports ${VAR}, ${VAR:-default}, and simple $HOME/$USER/$KERNEL_SOURCE_DIR.
    """
    _SIMPLE_VARS = {"HOME", "USER", "KERNEL_SOURCE_DIR"}

    def _replace_one_braced(m: re.Match) -> str:
        inner = m.group(1)
        if ":-" in inner:
            var, default = inner.split(":-", 1)
            return os.environ.get(var.strip(), default.strip())
        elif "-" in inner and not inner.startswith("-"):
            var, default = inner.split("-", 1)
            val = os.environ.get(var.strip())
            return val if val else default.strip()
        else:
            return os.environ[inner.strip()]

    result = text
    prev = None
    while result != prev:
        prev = result
        result = re.sub(r"\$\{([^${}]+)\}", _replace_one_braced, result)
    result = re.sub(
        r"(?<!\$)\$(?!\$|\{)(" + "|".join(_SIMPLE_VARS) + r")",
        lambda m: os.environ.get(m.group(1), ""),
        result,
    )
    return result

# ---------------------------------------------------------------------------
# Prompt / path helpers
# ---------------------------------------------------------------------------


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
    "vmcore",
    "vmlinux",
    "log",
    "boot_kernel",
    "kernel_source",
}


def parse_input_file(file_path: str) -> dict[str, str]:
    """Parse a structured input file (key: value per line) into a field dict.

    Expected format (see ``input.txt.template``)::

        Bug Promote: <description+fault type>
        vmcore: <path>
        vmlinux: <path>
        log: <path>
        boot_kernel: <path>
        kernel_source: <path>

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
            fields[key.strip()] = _resolve_env_vars(value.strip())

    # Return only recognised fields so unknown keys don't leak through.
    return {k: v for k, v in fields.items() if k in INPUT_FILE_FIELDS}


def format_user_input(fields: dict[str, str]) -> str:
    """Format parsed input fields into a single user_input string for the workflow."""
    lines = [f"{k}: {v}" for k, v in fields.items() if v]
    return "\n".join(lines)
