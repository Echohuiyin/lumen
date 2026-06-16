"""Crash session management for OC-AiCrash-Skill."""

from __future__ import annotations

import json
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pexpect

from .config import AppConfig


class _PexpectLogger:
    """Adapter that streams pexpect output into the standard logger."""

    def __init__(self, logger: logging.Logger, level: int = logging.DEBUG) -> None:
        self._logger = logger
        self._level = level

    def write(self, data: str) -> None:  # pragma: no cover - debug utility
        text = data.strip()
        if text:
            self._logger.log(self._level, "[crash stdout] %s", text)

    def flush(self) -> None:  # pragma: no cover - compatibility no-op
        pass


LOG = logging.getLogger(__name__)


class CrashSessionError(RuntimeError):
    """Raised when the crash utility reports an error."""


@dataclass(slots=True)
class CrashCommandResult:
    """Structured representation of crash command output."""

    command: str
    output: str
    success: bool = True


class CrashSessionManager:
    """Encapsulates a long-lived `crash` CLI session."""

    def __init__(
        self,
        vmcore_path: Path,
        vmlinux_path: Path,
        config: Optional[AppConfig] = None,
        prompt: str = "crash>",
        cmd_log_path: Optional[Path] = None,
    ) -> None:
        self.config = config or AppConfig()
        self.vmcore_path = vmcore_path
        self.vmlinux_path = vmlinux_path
        self.prompt = prompt
        self._child: Optional[pexpect.spawn] = None
        self._lock = threading.RLock()
        self._pexpect_logger = _PexpectLogger(LOG, level=logging.INFO)
        # --- Command log (Plan B: server-side auto-logging) ---
        self._cmd_log_path: Optional[Path] = cmd_log_path
        self._command_history: List[Dict[str, str]] = []

    # ------------------------------------------------------------------
    def __del__(self) -> None:
        """Last-resort cleanup: ensure the crash child process is terminated.

        This runs when the CrashSessionManager is garbage-collected.  It is
        NOT a substitute for calling stop() explicitly, but acts as a safety
        net to prevent zombie/orphan crash processes when the server is killed
        abruptly and the normal cleanup path is bypassed.
        """
        try:
            if self._child and self._child.isalive():
                LOG.warning(
                    "CrashSessionManager.__del__: crash child still alive — "
                    "force-killing (pid=%s)",
                    self._child.pid,
                )
                self._child.close(force=True)
        except (OSError, pexpect.ExceptionPexpect):
            pass  # __del__ must never raise

    # ------------------------------------------------------------------
    def start(self) -> None:
        """Start the crash session if not already active."""

        with self._lock:
            if self._child and self._child.isalive():
                return

            for path in (self.vmcore_path, self.vmlinux_path):
                if not Path(path).exists():
                    raise FileNotFoundError(f"Missing required crash artifact: {path}")

            cmd = [
                self.config.crash_binary,
                "-s",  # disable the crash pager to avoid hanging on long output
                str(self.vmlinux_path),
                str(self.vmcore_path),
            ]

            LOG.debug("Launching crash utility: %s", " ".join(cmd))
            env = {**os.environ, "TERM": "dumb", "NO_COLOR": "1"}
            self._child = pexpect.spawn(
                command=cmd[0],
                args=cmd[1:],
                timeout=self.config.crash_timeout_seconds,
                env=env,
                encoding="utf-8",
            )
            self._child.logfile = self._pexpect_logger

            try:
                LOG.info("Waiting for crash prompt '%s'", self.prompt)
                self._child.expect(self.prompt)
                LOG.info("Crash prompt detected; session initialised")
            except pexpect.TIMEOUT as exc:  # pragma: no cover - external binary
                self.stop()
                raise CrashSessionError(
                    "Timed out waiting for crash prompt during initialisation"
                ) from exc

    # ------------------------------------------------------------------
    def stop(self) -> None:
        """Terminate the crash session."""

        with self._lock:
            if not self._child:
                return

            if self._child.isalive():
                try:
                    self._child.sendline("exit")
                    self._child.expect(pexpect.EOF, timeout=5)
                except (OSError, pexpect.ExceptionPexpect):  # pragma: no cover - best effort cleanup
                    self._child.close(force=True)
            self._child = None

    # ------------------------------------------------------------------
    def run_command(self, command: str) -> CrashCommandResult:
        """Execute a crash command and capture the output."""

        with self._lock:
            if not self._child or not self._child.isalive():
                raise CrashSessionError("Crash session is not running")

            LOG.debug("Running crash command: %s", command)
            self._child.sendline(command)

            try:
                LOG.debug("Awaiting prompt after command '%s'", command)
                self._child.expect(self.prompt)
                LOG.debug("Prompt received for command '%s'", command)
            except pexpect.TIMEOUT as exc:  # pragma: no cover - external binary
                raise CrashSessionError(
                    f"Timed out executing crash command `{command}`"
                ) from exc

            raw_output = self._child.before
            normalized = raw_output.replace("\r\n", "\n").replace("\r", "\n")

            command_stripped = command.strip()
            cleaned_lines: List[str] = []
            skipping_leading = True
            for line in normalized.split("\n"):
                stripped_line = line.strip()
                if skipping_leading:
                    if not stripped_line:
                        continue
                    if stripped_line == command_stripped:
                        continue
                    skipping_leading = False
                cleaned_lines.append(line)

            cleaned = "\n".join(cleaned_lines).strip()

            output = cleaned
            limit = getattr(self.config, "crash_output_max_chars", 0)
            if limit and limit > 0 and len(cleaned) > limit:
                LOG.info(
                    "Crash output for `%s` exceeded %s chars; truncating to limit",
                    command,
                    limit,
                )
                truncated = cleaned[:limit].rstrip()
                notice = f"[output truncated to {limit} characters]"
                output = f"{truncated}\n{notice}" if truncated else notice

            LOG.debug("Crash output for `%s`: %s", command, output[:400])

            # Auto-log command result (Plan B: zero AI output tokens)
            result = CrashCommandResult(command=command, output=output)
            self._record_command(result)
            return result

    # ------------------------------------------------------------------
    def run_batch(self, commands: Iterable[str]) -> List[CrashCommandResult]:
        """Execute multiple crash commands sequentially.

        Respects ``config.crash_batch_output_max_chars`` — once the cumulative
        output exceeds that limit, subsequent command outputs are replaced with
        a truncation notice while the commands themselves are still executed.
        """

        batch_limit = getattr(self.config, "crash_batch_output_max_chars", 0)
        total_chars = 0

        results: List[CrashCommandResult] = []
        for cmd in commands:
            if not cmd.strip():
                continue
            try:
                result = self.run_command(cmd)
            except CrashSessionError as exc:
                result = CrashCommandResult(command=cmd, output=str(exc), success=False)

            if batch_limit and batch_limit > 0 and total_chars >= batch_limit:
                omitted_notice = (
                    f"[batch output limit reached ({total_chars} chars); "
                    f"output omitted — re-run this command individually if needed]"
                )
                result = CrashCommandResult(
                    command=result.command,
                    output=omitted_notice,
                    success=result.success,
                )
            else:
                total_chars += len(result.output)

            results.append(result)
        return results

    # ------------------------------------------------------------------
    def collect_baseline(self) -> List[CrashCommandResult]:
        """Gather a baseline set of diagnostic crash commands."""

        baseline_commands = [
            "sys",
            "bt",
            "log | tail -n 100",
        ]
        LOG.info("Collecting baseline crash diagnostics: %s", ", ".join(baseline_commands))
        return self.run_batch(baseline_commands)

    # ------------------------------------------------------------------
    # Command log helpers (Plan B: server-side auto-logging)
    # ------------------------------------------------------------------

    def _record_command(self, result: CrashCommandResult) -> None:
        """Record a command result to in-memory history and optionally to JSONL file.

        This runs inside the session lock so it is safe to call from run_command.
        Writing to disk is best-effort — a failure here must never break the
        crash command flow.
        """
        entry = {
            "cmd": result.command,
            "output": result.output,
            "success": result.success,
        }
        self._command_history.append(entry)

        if self._cmd_log_path:
            try:
                self._cmd_log_path.parent.mkdir(parents=True, exist_ok=True)
                with open(self._cmd_log_path, "a", encoding="utf-8") as fh:
                    fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:  # pragma: no cover - best effort
                LOG.warning(
                    "Failed to write command log entry for `%s` to %s",
                    result.command,
                    self._cmd_log_path,
                    exc_info=True,
                )

    def set_cmd_log_path(self, path: Optional[Path]) -> None:
        """Set or change the JSONL command log path at runtime.

        If *path* is provided and in-memory history already contains entries,
        they are flushed to the file immediately so nothing is lost.
        """
        self._cmd_log_path = path
        if path and self._command_history:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "a", encoding="utf-8") as fh:
                    for entry in self._command_history:
                        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
                LOG.info(
                    "Flushed %d historical entries to %s",
                    len(self._command_history),
                    path,
                )
            except OSError:  # pragma: no cover
                LOG.warning("Failed to flush history to %s", path, exc_info=True)

    def get_command_history(self) -> List[Dict[str, str]]:
        """Return a copy of the in-memory command history."""
        return list(self._command_history)

    def export_command_log(self, path: Path) -> int:
        """Export the full in-memory command history to a JSONL file.

        Returns the number of entries written.
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            for entry in self._command_history:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        count = len(self._command_history)
        LOG.info("Exported %d command log entries to %s", count, path)
        return count


__all__ = ["CrashSessionManager", "CrashSessionError", "CrashCommandResult"]
