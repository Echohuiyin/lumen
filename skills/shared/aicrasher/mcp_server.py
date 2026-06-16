"""Standard MCP Server exposing crash analysis tools via FastMCP."""

from __future__ import annotations

import atexit
import asyncio
import logging
import os
import signal
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict

from mcp.server.fastmcp import FastMCP

from .config import AppConfig, get_fresh_config
from .crash_session import CrashCommandResult, CrashSessionError, CrashSessionManager
from .knowledge_base import KnowledgeBase

LOG = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Fallback cleanup: ensure crash child processes are reaped even when the
# MCP server is killed abruptly (e.g. SIGTERM from mcporter keep-alive).
# The lifespan context manager only runs its cleanup when the asyncio event
# loop shuts down gracefully; a hard kill bypasses it entirely.
# ---------------------------------------------------------------------------

GLOBAL_SESSIONS: Dict[str, CrashSessionManager] = {}


def _force_cleanup_sessions() -> None:
    """Synchronously stop all active crash sessions (best-effort).

    Called from atexit or signal handlers where the asyncio loop may already
    be gone.  Each ``session.stop()`` sends ``exit`` to the crash child and
    falls back to SIGKILL.
    """
    for sid, session in list(GLOBAL_SESSIONS.items()):
        try:
            session.stop()
            LOG.info("Force-cleaned crash session %s", sid)
        except (OSError, RuntimeError):
            LOG.exception("Error force-cleaning session %s", sid)
    GLOBAL_SESSIONS.clear()


def _signal_handler(signum: int, frame: object) -> None:
    """Handle SIGTERM/SIGINT by cleaning up crash sessions then re-raising."""
    sig_name = signal.Signals(signum).name
    LOG.info("Received %s — cleaning up crash sessions before exit", sig_name)
    _force_cleanup_sessions()
    # Re-raise with default handler so the process exits with correct status
    signal.signal(signum, signal.SIG_DFL)
    os.kill(os.getpid(), signum)


# Register signal handlers (only in the main thread to avoid ValueError)
try:
    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
except ValueError:
    # Not in main thread — signal handlers can only be set in main thread
    pass

# atexit as a second safety net (runs on normal Python shutdown, not SIGKILL)
atexit.register(_force_cleanup_sessions)


def _result_to_dict(r: CrashCommandResult) -> dict:
    return {
        "command": r.command,
        "output": r.output,
        "output_chars": len(r.output),
        "success": r.success,
    }


@asynccontextmanager
async def _lifespan(server: FastMCP) -> AsyncIterator[Dict[str, Any]]:
    """Initialise shared state and clean up on shutdown."""
    global GLOBAL_SESSIONS
    config = AppConfig()
    sessions: Dict[str, CrashSessionManager] = {}
    # Share reference so signal/atexit handlers can reach active sessions
    GLOBAL_SESSIONS = sessions
    kb = KnowledgeBase(config)
    LOG.info("OC-AiCrash MCP server starting (knowledge base paths: %s)", config.knowledge_base_paths)

    yield {"config": config, "sessions": sessions, "kb": kb}

    # Cleanup all active sessions on shutdown (graceful path)
    for sid, session in list(sessions.items()):
        LOG.info("Shutting down crash session %s", sid)
        try:
            await asyncio.to_thread(session.stop)
        except (OSError, RuntimeError):
            LOG.exception("Error closing session %s during shutdown", sid)
    sessions.clear()
    GLOBAL_SESSIONS = {}
    LOG.info("OC-AiCrash MCP server stopped")


mcp = FastMCP("aicrasher", lifespan=_lifespan)


# ── Tools ──────────────────────────────────────────────────────────────


async def _create_session_common(
    sessions: Dict[str, CrashSessionManager],
    vmcore_path: str,
    vmlinux_path: str,
    cmd_log_path: str = "",
) -> tuple[str, str, CrashSessionManager]:
    """Shared logic for creating and registering a crash session.

    Returns:
        A (session_id, log_path_str, session) tuple.
    """
    config = get_fresh_config()
    vmcore = Path(vmcore_path)
    log_path = Path(cmd_log_path) if cmd_log_path else vmcore.parent / "cmd_log.jsonl"

    session = CrashSessionManager(
        vmcore_path=vmcore,
        vmlinux_path=Path(vmlinux_path),
        config=config,
        cmd_log_path=log_path,
    )
    await asyncio.to_thread(session.start)

    session_id = str(uuid.uuid4())
    sessions[session_id] = session
    return session_id, str(log_path), session


@mcp.tool()
async def create_crash_session(
    vmcore_path: str,
    vmlinux_path: str,
    cmd_log_path: str = "",
) -> dict:
    """Create a crash debugging session.

    Opens the crash utility with the given vmcore dump and vmlinux image
    and returns a session ID for subsequent commands.

    All commands executed in this session are automatically logged to a JSONL
    file (one JSON object per line with "cmd", "output", "success" fields).
    If *cmd_log_path* is not provided, the log is written to
    ``<vmcore_dir>/cmd_log.jsonl`` by default.

    Args:
        vmcore_path: Absolute path to the vmcore dump file.
        vmlinux_path: Absolute path to the uncompressed vmlinux image.
        cmd_log_path: Optional path for the JSONL command log file.
                      Defaults to ``<vmcore_dir>/cmd_log.jsonl``.

    Returns:
        A dict with the session_id and cmd_log_path.
    """
    ctx = mcp.get_context()
    sessions: Dict[str, CrashSessionManager] = ctx.request_context.lifespan_context["sessions"]

    session_id, log_path_str, _session = await _create_session_common(
        sessions, vmcore_path, vmlinux_path, cmd_log_path,
    )
    LOG.info("Created crash session %s (cmd_log: %s)", session_id, log_path_str)
    return {"session_id": session_id, "cmd_log_path": log_path_str}


@mcp.tool()
async def run_crash_command(session_id: str, command: str) -> dict:
    """Execute a crash CLI command in an existing session.

    Args:
        session_id: Session ID returned by create_crash_session.
        command: The crash command to run (e.g. "bt", "kmem -i", "log").

    Returns:
        A dict with command, output, and success fields.
    """
    ctx = mcp.get_context()
    sessions: Dict[str, CrashSessionManager] = ctx.request_context.lifespan_context["sessions"]

    session = sessions.get(session_id)
    if not session:
        return {"error": f"Session {session_id} not found"}

    try:
        result = await asyncio.to_thread(session.run_command, command)
    except CrashSessionError as exc:
        return {"command": command, "output": str(exc), "success": False}

    return _result_to_dict(result)


@mcp.tool()
async def run_crash_commands(session_id: str, commands: list[str]) -> dict:
    """Execute multiple crash CLI commands sequentially in an existing session.

    Use this instead of run_crash_command when you need to run several similar
    commands at once. don't run several commands with large output.
    Commands are executed in order; a failure in one command does not
    prevent subsequent commands from running.

    Args:
        session_id: Session ID returned by create_crash_session.
        commands: List of crash commands to run (e.g. ["bt", "kmem -i", "log"]).

    Returns:
        A dict with a results list, each containing command, output, and success.
    """
    ctx = mcp.get_context()
    sessions: Dict[str, CrashSessionManager] = ctx.request_context.lifespan_context["sessions"]

    session = sessions.get(session_id)
    if not session:
        return {"error": f"Session {session_id} not found"}

    results = await asyncio.to_thread(session.run_batch, commands)
    return {"results": [_result_to_dict(r) for r in results]}


@mcp.tool()
async def collect_baseline(session_id: str) -> dict:
    """Collect baseline diagnostic information from a crash session.

    Runs: sys, bt, log | tail -n 100.

    Args:
        session_id: Session ID returned by create_crash_session.

    Returns:
        A dict with a results list, each containing command/output/success.
    """
    ctx = mcp.get_context()
    sessions: Dict[str, CrashSessionManager] = ctx.request_context.lifespan_context["sessions"]

    session = sessions.get(session_id)
    if not session:
        return {"error": f"Session {session_id} not found"}

    try:
        results = await asyncio.to_thread(session.collect_baseline)
    except CrashSessionError as exc:
        return {"error": str(exc)}

    return {"results": [_result_to_dict(r) for r in results]}


@mcp.tool()
async def close_crash_session(session_id: str) -> dict:
    """Close and clean up a crash debugging session.

    Args:
        session_id: Session ID returned by create_crash_session.

    Returns:
        A dict with status "closed" or an error.
    """
    ctx = mcp.get_context()
    sessions: Dict[str, CrashSessionManager] = ctx.request_context.lifespan_context["sessions"]

    session = sessions.pop(session_id, None)
    if not session:
        return {"error": f"Session {session_id} not found"}

    await asyncio.to_thread(session.stop)
    LOG.info("Closed crash session %s", session_id)
    return {"status": "closed"}


@mcp.tool()
async def search_knowledge_base(query_terms: list[str], limit: int = 5) -> dict:
    """Search the local knowledge base and Red Hat KB for relevant articles.

    Args:
        query_terms: List of search keywords (e.g. ["null pointer", "ext4"]).
        limit: Maximum number of results to return (default 5).

    Returns:
        A dict with a matches list containing title, summary, score, source.
    """
    ctx = mcp.get_context()
    kb: KnowledgeBase = ctx.request_context.lifespan_context["kb"]

    matches = await asyncio.to_thread(kb.search, query_terms, limit)
    return {
        "matches": [
            {
                "title": m.title,
                "summary": m.summary,
                "score": round(m.score, 3),
                "source": str(m.source),
            }
            for m in matches
        ]
    }


@mcp.tool()
async def analyze_crash(vmcore_path: str, vmlinux_path: str, cmd_log_path: str = "") -> dict:
    """One-shot crash analysis: create session + collect baseline diagnostics.

    This is a convenience tool that combines create_crash_session and
    collect_baseline into a single call.

    All commands are automatically logged to a JSONL file.

    Args:
        vmcore_path: Absolute path to the vmcore dump file.
        vmlinux_path: Absolute path to the uncompressed vmlinux image.
        cmd_log_path: Optional path for the JSONL command log file.
                      Defaults to ``<vmcore_dir>/cmd_log.jsonl``.

    Returns:
        A dict with session_id, cmd_log_path, and baseline results.
    """
    ctx = mcp.get_context()
    sessions: Dict[str, CrashSessionManager] = ctx.request_context.lifespan_context["sessions"]

    session_id, log_path_str, session = await _create_session_common(
        sessions, vmcore_path, vmlinux_path, cmd_log_path,
    )
    LOG.info("Created crash session %s (via analyze_crash, cmd_log: %s)", session_id, log_path_str)

    try:
        results = await asyncio.to_thread(session.collect_baseline)
    except CrashSessionError as exc:
        return {"session_id": session_id, "cmd_log_path": log_path_str, "error": str(exc)}

    return {
        "session_id": session_id,
        "cmd_log_path": log_path_str,
        "baseline": [_result_to_dict(r) for r in results],
    }


@mcp.tool()
async def export_command_log(session_id: str, output_path: str = "") -> dict:
    """Export all recorded crash commands and outputs to a JSONL file.

    Every command executed via run_crash_command / run_crash_commands /
    collect_baseline is automatically captured in memory.  This tool writes
    the full history to a JSONL file that can be consumed by the report
    generator (``@cmd[...]`` references).

    If *output_path* is omitted the session's default cmd_log_path is used.

    Args:
        session_id: Session ID returned by create_crash_session.
        output_path: Optional destination file path.  Defaults to the
                     session's cmd_log_path.

    Returns:
        A dict with the path and number of entries exported.
    """
    ctx = mcp.get_context()
    sessions: Dict[str, CrashSessionManager] = ctx.request_context.lifespan_context["sessions"]

    session = sessions.get(session_id)
    if not session:
        return {"error": f"Session {session_id} not found"}

    dest = Path(output_path) if output_path else session._cmd_log_path
    if not dest:
        return {"error": "No output_path specified and session has no default cmd_log_path"}

    count = await asyncio.to_thread(session.export_command_log, dest)
    return {"path": str(dest), "entries": count}


@mcp.tool()
async def list_sessions() -> dict:
    """List all active crash debugging sessions.

    Returns:
        A dict with a sessions list of session IDs.
    """
    ctx = mcp.get_context()
    sessions: Dict[str, CrashSessionManager] = ctx.request_context.lifespan_context["sessions"]
    return {"sessions": list(sessions.keys())}


# ── Entry point ────────────────────────────────────────────────────────


def main() -> None:
    """Run the MCP server with stdio transport."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
