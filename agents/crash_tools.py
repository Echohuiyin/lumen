"""Crash analysis tools for LangChain/LangGraph tool calling.

Provides LangChain StructuredTool wrappers for aicrasher CrashSessionManager,
enabling crash_analysis and lock_analysis experts to execute real crash commands.

Note: aicrasher path is already added to sys.path in config.py.

Shared session management:
  Uses a global registry to share crash sessions across experts that
  target the same vmcore+vmlinux, avoiding concurrent crash processes
  competing for the same binary.
"""

import json
import os
import re
import threading
from pathlib import Path
from typing import Any, Dict, List

from langchain_core.tools import StructuredTool
from pydantic import BaseModel, field_validator

from paths import PROJECT_ROOT

# ---------------------------------------------------------------------------
# Shared session registry — prevents multiple concurrent crash processes
# ---------------------------------------------------------------------------
_session_registry: Dict[str, Any] = {}
_session_lock = threading.Lock()


def _sniff_arch_from_elf(path: str) -> str:
    """Read e_machine from ELF header to detect target arch.

    Mirrors kernel_expert._sniff_arch_from_elf — kept local to avoid
    circular import. Returns 'x86_64' / 'arm64' / 'arm32' / ''.
    """
    _ELF_MACHINE_TO_ARCH = {
        62:  "x86_64",  # EM_X86_64
        3:   "x86_64",  # EM_386
        183: "arm64",   # EM_AARCH64
        40:  "arm32",   # EM_ARM
    }
    try:
        with open(path, "rb") as f:
            magic = f.read(6)
            if magic[:4] != b"\x7fELF":
                return ""
            ei_data = magic[5]
            f.seek(18)
            e_machine_bytes = f.read(2)
            if len(e_machine_bytes) != 2:
                return ""
            byteorder = "little" if ei_data == 1 else "big"
            e_machine = int.from_bytes(e_machine_bytes, byteorder)
        return _ELF_MACHINE_TO_ARCH.get(e_machine, "")
    except (OSError, IOError):
        return ""


def _select_crash_binary_for_arch(arch: str) -> str | None:
    """Pick the right crash binary for the target arch.

    Looks for crash_<arch> in Lumen's project-managed tool directory, then
    system locations. Returns None if no arch-specific binary is found —
    caller falls back to AppConfig's default detection.
    """
    if not arch:
        return None
    candidates = [
        PROJECT_ROOT / "Analysis-SKILL" / "tools" / "crash" / f"crash_{arch}",
        Path("/usr/local/bin") / f"crash_{arch}",
    ]
    for c in candidates:
        if c.is_file() and os.access(c, os.X_OK):
            return str(c)
    return None


def get_or_create_crash_session(vmcore_path: str, vmlinux_path: str) -> Any:
    """Return an existing shared session or create a new one.

    Sessions are keyed by (vmcore_path, vmlinux_path) so experts that
    target the same vmcore reuse the same crash process.

    Auto-selects an arch-specific crash binary (crash_arm64 / crash_x86_64)
    by sniffing vmlinux's ELF e_machine. This is required because crash is
    compiled with a single TARGET arch — an x86_64-targeted crash cannot
    parse an arm64 vmcore ("machine type mismatch" → "not a supported
    file format"). If no arch-specific binary is found, falls back to
    AppConfig's default detection (which may fail for cross-arch vmscores).
    """
    from aicrasher.crash_session import CrashSessionManager
    from aicrasher.config import AppConfig

    vmcore = Path(vmcore_path)
    vmlinux = Path(vmlinux_path)

    if not vmcore.exists():
        raise FileNotFoundError(f"vmcore not found: {vmcore_path}")
    if not vmlinux.exists():
        raise FileNotFoundError(f"vmlinux not found: {vmlinux_path}")

    key = f"{vmcore.resolve()}|{vmlinux.resolve()}"

    with _session_lock:
        if key in _session_registry:
            session, refcount = _session_registry[key]
            _session_registry[key] = (session, refcount + 1)
            return session

        arch = _sniff_arch_from_elf(str(vmlinux))
        arch_binary = _select_crash_binary_for_arch(arch)
        # Pass arch_binary directly to AppConfig instead of mutating the
        # process-global CRASH_BINARY env var. Concurrent sessions for
        # different arches (x86_64 + arm64) would otherwise overwrite each
        # other's binary path.
        if arch_binary:
            config = AppConfig(crash_binary=arch_binary)
        else:
            config = AppConfig()
        session = CrashSessionManager(
            vmcore_path=vmcore,
            vmlinux_path=vmlinux,
            config=config,
        )
        session.start()
        _session_registry[key] = (session, 1)
        return session


def release_crash_session(vmcore_path: str, vmlinux_path: str) -> None:
    """Release a reference to a shared session. Stops it when refcount hits 0."""
    vmcore = Path(vmcore_path)
    vmlinux = Path(vmlinux_path)
    key = f"{vmcore.resolve()}|{vmlinux.resolve()}"

    with _session_lock:
        if key not in _session_registry:
            return
        session, refcount = _session_registry[key]
        refcount -= 1
        if refcount <= 0:
            try:
                session.stop()
            except Exception:
                pass
            del _session_registry[key]
        else:
            _session_registry[key] = (session, refcount)


# ---------------------------------------------------------------------------
# Crash command validation
# ---------------------------------------------------------------------------

# Shell features that crash's internal pipe handling may not support well.
# Commands matching these patterns get sanitised or warned about.
_UNSAFE_SHELL_PATTERNS = [
    # grep with escaped pipe alternation (crash shell may not handle \|)
    (r'grep\s+.*\\\|', "grep with \\| alternation may not work in crash shell"),
    # Shell redirects (crash doesn't support >, >>, 2>, etc.)
    (r'(?<![|&])\s*[<>]+\s*\S', "shell redirect (<, >, >>) is unsupported in crash"),
    # Backtick command substitution
    (r'`[^`]+`', "backtick command substitution is unsupported in crash"),
    # $(...) command substitution
    (r'\$\([^)]+\)', "$(...) command substitution is unsupported in crash"),
    # Shell variables
    (r'\$\{?[A-Za-z_]', "shell variable expansion may not work in crash"),
]


def sanitize_crash_command(command: str) -> tuple[str, list[str]]:
    """Check and sanitize a crash command.

    Returns (sanitized_command, warnings_list).
    Crash8 supports simple pipes (|) and head/tail/wc/etc, but not
    full shell syntax like redirects, variable expansion, or grep \\|.

    Args:
        command: Raw command string from LLM tool call

    Returns:
        (sanitized_command, list_of_warning_strings)
    """
    warnings = []
    sanitized = command.strip()

    for pattern, warning in _UNSAFE_SHELL_PATTERNS:
        if re.search(pattern, sanitized):
            warnings.append(warning)

    return sanitized, warnings


def build_sanitized_description(warnings: list[str]) -> str:
    """Build a warning suffix to prepend to tool output if needed."""
    if not warnings:
        return ""
    lines = ["[WARNING: potential shell syntax issues detected]"]
    for w in warnings:
        lines.append(f"  - {w}")
    lines.append("  If the command produced no output, try without these shell features.")
    return "\n".join(lines) + "\n\n"


# ---------------------------------------------------------------------------
# Tool input schemas
# ---------------------------------------------------------------------------


class RunCrashCommandInput(BaseModel):
    """Input schema for run_crash_command."""
    command: str


class RunCrashCommandsInput(BaseModel):
    """Input schema for run_crash_commands."""
    commands: List[str]

    @field_validator("commands", mode="before")
    @classmethod
    def _coerce_to_list(cls, v):
        # Some model providers (DeepSeek/GLM/MiniMax) occasionally serialize
        # list-typed args as a JSON string. Coerce gracefully so the tool
        # call succeeds instead of failing Pydantic validation.
        if isinstance(v, str):
            s = v.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [str(x) for x in parsed]
                except json.JSONDecodeError:
                    pass
            return [s] if s else []
        if v is None:
            return []
        return v


class CollectBaselineInput(BaseModel):
    """Input schema for collect_baseline (no args needed)."""
    pass


class GetHistoryInput(BaseModel):
    """Input schema for get_command_history."""
    pass


# ---------------------------------------------------------------------------
# Session-bound tool functions
# ---------------------------------------------------------------------------


def create_session_bound_tools(session: Any) -> dict:
    """Create session-bound tool functions.

    Each function is bound to the provided CrashSessionManager instance.

    Args:
        session: Active CrashSessionManager instance

    Returns:
        dict mapping tool_name -> callable function
    """
    # Extract paths for Cache B (crash command output caching)
    vmcore_path = str(getattr(session, "vmcore_path", ""))
    vmlinux_path = str(getattr(session, "vmlinux_path", ""))
    _cache_available = bool(vmcore_path and vmlinux_path)

    def _cached_run(command: str) -> tuple[str, bool]:
        """Run a crash command with cache lookup/store. Returns (output, success)."""
        from agents.cache.crash_cache import lookup_command, store_command

        if _cache_available:
            cached = lookup_command(vmcore_path, vmlinux_path, command)
            if cached is not None:
                return cached

        result = session.run_command(command)
        output, success = result.output, result.success

        if _cache_available:
            store_command(vmcore_path, vmlinux_path, command, output, success)

        return output, success

    def run_crash_command(command: str) -> str:
        """Execute a single crash command and return output."""
        sanitized, warnings = sanitize_crash_command(command)
        try:
            output, success = _cached_run(sanitized)
            prefix = build_sanitized_description(warnings)
            if success:
                return prefix + output
            else:
                return prefix + f"Error: {output}"
        except Exception as e:
            return f"Error executing '{command}': {str(e)}"

    def run_crash_commands(commands: List[str]) -> str:
        """Execute multiple crash commands sequentially with per-command caching."""
        try:
            output_parts = []
            for cmd in commands:
                sanitized, _ = sanitize_crash_command(cmd)
                try:
                    output, success = _cached_run(sanitized)
                    if success:
                        output_parts.append(f"[{sanitized}]\n{output}")
                    else:
                        output_parts.append(f"[{sanitized}] Error: {output}")
                except Exception as e:
                    output_parts.append(f"[{sanitized}] Error: {str(e)}")
            return "\n\n".join(output_parts)
        except Exception as e:
            return f"Error executing commands: {str(e)}"

    def collect_baseline() -> str:
        """Collect baseline diagnostics (sys, bt, log)."""
        try:
            results = session.collect_baseline()
            output_parts = []
            for r in results:
                if r.success:
                    output_parts.append(f"[{r.command}]\n{r.output}")
                else:
                    output_parts.append(f"[{r.command}] Error: {r.output}")
            return "\n\n".join(output_parts)
        except Exception as e:
            return f"Error collecting baseline: {str(e)}"

    def get_command_history() -> str:
        """Get all executed commands and outputs summary."""
        try:
            history = session.get_command_history()
            lines = []
            for item in history:
                output_preview = item.get("output", "")[:200]
                if len(item.get("output", "")) > 200:
                    output_preview += "..."
                lines.append(f"{item['command']}: {output_preview}")
            return "\n".join(lines)
        except Exception as e:
            return f"Error getting history: {str(e)}"

    return {
        "run_crash_command": run_crash_command,
        "run_crash_commands": run_crash_commands,
        "collect_baseline": collect_baseline,
        "get_command_history": get_command_history,
    }


def create_crash_tools(session: Any) -> List[StructuredTool]:
    """Create LangChain StructuredTool instances bound to session.

    Args:
        session: Active CrashSessionManager instance

    Returns:
        List of StructuredTool instances for bind_tools()
    """
    tool_funcs = create_session_bound_tools(session)

    tools = [
        StructuredTool(
            name="run_crash_command",
            description=(
                "Execute a single crash command in the active session. "
                "Returns command output as text. "
                "Valid examples: 'bt', 'sys', 'log', 'ps', 'kmem -i', "
                "'bt <pid>', 'struct mutex.owner <addr>', 'dis <symbol>'. "
                "Simple pipes work: 'bt -a | grep panic'. "
                "Do NOT use: shell redirects, variable expansion, grep \\| alternation."
            ),
            func=tool_funcs["run_crash_command"],
            args_schema=RunCrashCommandInput,
        ),
        StructuredTool(
            name="run_crash_commands",
            description=(
                "Execute multiple crash commands sequentially. "
                "Use for batch operations like ['sys', 'bt', 'log']. "
                "Commands run in order; output for each command is labeled."
            ),
            func=tool_funcs["run_crash_commands"],
            args_schema=RunCrashCommandsInput,
        ),
        StructuredTool(
            name="collect_baseline",
            description=(
                "Collect baseline diagnostic information: sys (kernel info), "
                "bt (backtrace), log | tail -n 100 (kernel log). "
                "Should be called first after session creation."
            ),
            func=tool_funcs["collect_baseline"],
            args_schema=CollectBaselineInput,
        ),
        StructuredTool(
            name="get_command_history",
            description=(
                "Review all previously executed commands and their outputs. "
                "Useful for summarizing analysis progress and avoiding duplicate commands."
            ),
            func=tool_funcs["get_command_history"],
            args_schema=GetHistoryInput,
        ),
    ]

    return tools
