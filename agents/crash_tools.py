"""Crash analysis tools for LangChain/LangGraph tool calling.

Provides LangChain StructuredTool wrappers for aicrasher CrashSessionManager,
enabling crash_analysis and lock_analysis experts to execute real crash commands.
"""

from typing import List, Any

from langchain_core.tools import StructuredTool
from pydantic import BaseModel


class RunCrashCommandInput(BaseModel):
    """Input schema for run_crash_command."""
    command: str


class RunCrashCommandsInput(BaseModel):
    """Input schema for run_crash_commands."""
    commands: List[str]


class CollectBaselineInput(BaseModel):
    """Input schema for collect_baseline (no args needed)."""
    pass


class GetHistoryInput(BaseModel):
    """Input schema for get_command_history."""
    pass


def create_session_bound_tools(session: Any) -> dict:
    """Create session-bound tool functions.

    Each function is bound to the provided CrashSessionManager instance.

    Args:
        session: Active CrashSessionManager instance

    Returns:
        dict mapping tool_name -> callable function
    """
    def run_crash_command(command: str) -> str:
        """Execute a single crash command and return output."""
        try:
            result = session.run_command(command)
            if result.success:
                return result.output
            else:
                return f"Error: {result.output}"
        except Exception as e:
            return f"Error executing '{command}': {str(e)}"

    def run_crash_commands(commands: List[str]) -> str:
        """Execute multiple crash commands sequentially."""
        try:
            results = session.run_batch(commands)
            output_parts = []
            for r in results:
                if r.success:
                    output_parts.append(f"[{r.command}]\n{r.output}")
                else:
                    output_parts.append(f"[{r.command}] Error: {r.output}")
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
                "Examples: 'bt', 'sys', 'log | tail -n 100', 'kmem -i', 'ps', 'foreach bt'"
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