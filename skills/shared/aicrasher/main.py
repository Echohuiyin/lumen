"""CLI entry points for AI-assisted vmcore analysis."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import List

import httpx
import typer
from rich.console import Console
from rich.panel import Panel

from .ai_orchestrator import AIOrchestrator, CrashCommandPlan
from .config import AppConfig
from .knowledge_base import KnowledgeBase


LOG = logging.getLogger(__name__)
console = Console()
app = typer.Typer(add_completion=False)


class MCPClient:
    """HTTP client targeting the MCP server."""

    def __init__(self, base_url: str) -> None:
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(timeout=120)

    def create_session(self, vmcore: Path, vmlinux: Path) -> str:
        response = self._client.post(
            f"{self.base_url}/session",
            json={"vmcore_path": str(vmcore), "vmlinux_path": str(vmlinux)},
        )
        response.raise_for_status()
        return response.json()["session_id"]

    def collect_baseline(self, session_id: str) -> List[dict]:
        response = self._client.get(f"{self.base_url}/session/{session_id}/baseline")
        response.raise_for_status()
        return response.json().get("results", [])

    def run_command(self, session_id: str, command: str) -> dict:
        response = self._client.post(
            f"{self.base_url}/session/{session_id}/command",
            json={"command": command},
        )
        response.raise_for_status()
        payload = response.json()
        payload.setdefault("command", command)
        return payload

    def close_session(self, session_id: str) -> None:
        try:
            self._client.delete(f"{self.base_url}/session/{session_id}")
        except (httpx.HTTPError, OSError):
            LOG.exception("Failed to close session %s", session_id)


class AnalysisEngine:
    """Runs the end-to-end AI driven crash workflow."""

    def __init__(
        self,
        config: AppConfig,
        orchestrator: AIOrchestrator,
        mcp_client: MCPClient,
        knowledge_base: KnowledgeBase,
    ) -> None:
        self.config = config
        self.orchestrator = orchestrator
        self.mcp_client = mcp_client
        self.knowledge_base = knowledge_base

    def _render_and_append(self, transcript: List[str], entry: dict) -> None:
        command = entry.get("command")
        output = entry.get("output", "")
        success = entry.get("success", True)
        caption = f"crash> {command}" if command else "crash output"
        console.print(Panel.fit(output or "(no output)", title=caption, border_style="green" if success else "red"))
        transcript.append(f"$ {command}\n{output}\n")

    def _collect_terms_from_plan(self, plan: CrashCommandPlan) -> List[str]:
        terms: List[str] = []
        if plan.references:
            for ref in plan.references:
                terms.extend(ref.split())
        if not terms and plan.reasoning:
            terms.extend(plan.reasoning.split())
        # keep unique lowercase tokens longer than 4 chars
        unique = []
        for term in terms:
            sanitized = term.strip(".,:;()[]{}\"'").lower()
            if len(sanitized) < 5:
                continue
            if sanitized not in unique:
                unique.append(sanitized)
        return unique[:5]

    def run(self, vmcore: Path, vmlinux: Path) -> None:
        session_id = self.mcp_client.create_session(vmcore, vmlinux)
        console.print(f"[bold blue]Session[/bold blue] {session_id} started.")

        executed_commands: List[str] = []
        transcript: List[str] = []
        all_matches: List[str] = []

        try:
            baseline = self.mcp_client.collect_baseline(session_id)
            for entry in baseline:
                self._render_and_append(transcript, entry)
                executed_commands.append(entry.get("command", ""))

            for round_idx in range(self.config.max_ai_rounds):
                console.rule(f"AI planning round {round_idx + 1}")
                transcript_text = "\n".join(transcript[-10:])  # limit prompt size
                plan = self.orchestrator.plan_next_commands(
                    crash_transcript=transcript_text,
                    prior_commands=executed_commands,
                    knowledge_summaries=all_matches[-3:],
                )
                console.print(Panel(plan.reasoning or "(no reasoning)", title="AI reasoning", border_style="cyan"))

                if plan.verdict:
                    console.print(f"[bold green]AI verdict[/bold green]: {plan.verdict}")
                    break

                commands = plan.commands[: self.config.ai_command_batch_size]
                if not commands:
                    console.print("[yellow]AI did not propose further commands. Stopping.[/yellow]")
                    break

                for command in commands:
                    entry = self.mcp_client.run_command(session_id, command)
                    self._render_and_append(transcript, entry)
                    executed_commands.append(command)

                kb_terms = self._collect_terms_from_plan(plan)
                matches = self.knowledge_base.search(kb_terms)
                for match in matches:
                    formatted = match.format_brief()
                    if formatted not in all_matches:
                        all_matches.append(formatted)
                if matches:
                console.print(
                    Panel(
                        "\n".join(m.format_brief() for m in matches),
                        title="Knowledge base hits",
                        border_style="magenta",
                    )
                )

            else:
                console.print("[yellow]Reached maximum AI rounds without verdict.[/yellow]")

            if executed_commands:
                hypothesis = plan.verdict or "Undetermined"
                summary = self.orchestrator.craft_final_report(
                    crash_transcript="\n".join(transcript[-50:]),
                    knowledge_matches=all_matches,
                    hypothesis=hypothesis,
                )
                console.print(Panel(summary, title="AI final report", border_style="white"))

        finally:
            self.mcp_client.close_session(session_id)
            console.print(f"Closed session {session_id}")


@app.command()
def analyze(
    vmcore: str = typer.Argument(..., help="Path to vmcore file"),
    vmlinux: str = typer.Argument(..., help="Path to vmlinux file"),
    server_url: str = typer.Option("http://127.0.0.1:8000", help="Base URL of the MCP server"),
    log_level: str = typer.Option("INFO", help="Python logging level"),
) -> None:
    """Run AI-guided vmcore analysis against the MCP server."""

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    LOG.debug(
        "Analyze command invoked with vmcore=%s, vmlinux=%s, server_url=%s, log_level=%s",
        vmcore, vmlinux, server_url, log_level,
    )
    config = AppConfig()
    LOG.debug(
        "Loaded AppConfig: crash_binary=%s, workspace=%s, kb_paths=%s",
        config.crash_binary, config.workspace_root, config.knowledge_base_paths,
    )
    orchestrator = AIOrchestrator(config)
    knowledge_base = KnowledgeBase(config)
    client = MCPClient(server_url)
    engine = AnalysisEngine(config, orchestrator, client, knowledge_base)
    LOG.debug(
        "AnalysisEngine initialized; max_ai_rounds=%s, batch_size=%s",
        config.max_ai_rounds, config.ai_command_batch_size,
    )

    console.print(Panel.fit(json.dumps(config.model_dump(), indent=2, default=str), title="Configuration"))
    LOG.debug("Starting analysis engine run")
    engine.run(Path(vmcore), Path(vmlinux))
    LOG.debug("Analysis engine run completed")


@app.command()
def server(
    host: str = typer.Option("0.0.0.0", help="Server bind address"),
    port: int = typer.Option(8000, help="Server port"),
    log_level: str = typer.Option("info", help="uvicorn log level"),
) -> None:
    """Launch the MCP server as a standalone process."""

    try:
        import uvicorn

        from .rest_server import create_app
    except ImportError as exc:  # pragma: no cover - runtime dependency
        console.print(f"[red]Missing dependency to run server: {exc}[/red]")
        raise typer.Exit(code=1)

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    app_config = AppConfig()
    uvicorn.run(create_app(app_config), host=host, port=port, log_level=log_level)


@app.command(name="mcp")
def mcp_serve(
    transport: str = typer.Option("stdio", help="MCP transport: stdio or sse"),
    log_level: str = typer.Option("INFO", help="Python logging level"),
) -> None:
    """Launch the MCP server for Claude Desktop / Claude Code."""

    logging.basicConfig(
        level=getattr(logging, log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    from .mcp_server import mcp as mcp_app

    mcp_app.run(transport=transport)


if __name__ == "__main__":
    app()
