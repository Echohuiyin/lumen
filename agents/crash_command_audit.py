"""Deterministic crash-command admission and append-only evidence ledger.

Crash's command interpreter is intentionally narrower than a shell.  Keeping
the admission policy and the audit record in one small module prevents the
tool expert from accidentally executing a model-supplied shell fragment or
claiming success after losing the raw command output.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import shlex
import threading
import time
from typing import Any


_CRASH_COMMANDS = {
    "bt", "dis", "dev", "eval", "files", "foreach", "help", "irq", "kmem",
    "list", "log", "mod", "mount", "net", "p", "ps", "rd", "runq", "set",
    "struct", "sym", "sys", "task", "timer", "tree", "vm", "waitq", "whatis",
}
_PIPE_FILTERS = {"grep", "head", "sort", "tail", "wc"}
_FORBIDDEN = (";", "&", "`", "$", "<", ">", "!", "\\n", "\\r")
_LOCKS: dict[str, threading.Lock] = {}
_LOCKS_GUARD = threading.Lock()


@dataclass(frozen=True)
class CommandAdmission:
    allowed: bool
    command: str
    errors: tuple[str, ...] = ()


def _lock_for(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def validate_crash_command(command: str) -> CommandAdmission:
    """Validate one read-only crash command without invoking a shell."""
    raw = str(command or "").strip()
    errors: list[str] = []
    if not raw:
        errors.append("empty command")
    if len(raw) > 256:
        errors.append("command exceeds 256 characters")
    if any(token in raw for token in _FORBIDDEN):
        errors.append("shell metacharacter or redirection is not allowed")
    if "||" in raw or "&&" in raw:
        errors.append("compound shell operators are not allowed")
    if raw.count("|") > 1:
        errors.append("only one read-only pipeline stage is allowed")

    stages = [stage.strip() for stage in raw.split("|") if stage.strip()]
    if not errors and not stages:
        errors.append("empty command")
    for index, stage in enumerate(stages):
        try:
            tokens = shlex.split(stage, posix=True)
        except ValueError as exc:
            errors.append(f"invalid quoting: {exc}")
            continue
        if not tokens:
            errors.append("empty pipeline stage")
            continue
        name = tokens[0]
        if index == 0:
            if name not in _CRASH_COMMANDS:
                errors.append(f"command '{name}' is not in the crash read-only allowlist")
        elif name not in _PIPE_FILTERS:
            errors.append(f"pipeline filter '{name}' is not in the read-only allowlist")
        for token in tokens:
            if any(ch in token for ch in (";", "&", "`", "$", "<", ">", "!")):
                errors.append("argument contains shell syntax")
                break
    return CommandAdmission(not errors, raw, tuple(dict.fromkeys(errors)))


def format_admission_error(admission: CommandAdmission) -> str:
    return "BLOCKED_INVALID_CRASH_COMMAND: " + "; ".join(admission.errors)


class CrashCommandLedger:
    """Append command results and lossless output files for one session."""

    def __init__(self, output_file: str | Path):
        output = Path(output_file).expanduser().resolve()
        self.root = output.parent
        self.ledger_path = self.root / "crash_commands.jsonl"
        self.output_dir = self.root / "crash-output"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.ledger_path.parent.mkdir(parents=True, exist_ok=True)

    def record(
        self,
        *,
        expert: str,
        command: str,
        success: bool,
        output: str = "",
        error: str = "",
        started_at: str | None = None,
        elapsed_ms: int | None = None,
    ) -> dict[str, Any]:
        raw_output = str(output or "")
        with _lock_for(self.ledger_path):
            sequence = 1
            if self.ledger_path.exists():
                try:
                    last = self.ledger_path.read_text(encoding="utf-8").splitlines()[-1]
                    sequence = int(json.loads(last).get("sequence", 0)) + 1
                except (OSError, ValueError, IndexError, json.JSONDecodeError):
                    sequence = 1
            output_path = self.output_dir / f"{sequence:04d}.txt"
            output_path.write_text(raw_output, encoding="utf-8", errors="replace")
            digest = hashlib.sha256(raw_output.encode("utf-8", errors="replace")).hexdigest()
            evidence_key = "\0".join(("run_crash_command", str(command), digest))
            evidence_id = "crash-" + hashlib.sha256(evidence_key.encode("utf-8")).hexdigest()[:24]
            entry = {
                "sequence": sequence,
                "evidence_id": evidence_id,
                "expert": str(expert),
                "tool": "run_crash_command",
                "command": str(command),
                "success": bool(success),
                "output_file": str(output_path),
                "output_sha256": digest,
                "started_at": started_at or datetime.now(timezone.utc).isoformat(),
                "elapsed_ms": int(elapsed_ms or 0),
            }
            if error:
                entry["error"] = str(error)
            with self.ledger_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
            return entry

    def run(self, session: Any, command: str, *, expert: str) -> tuple[str, bool, dict[str, Any]]:
        admission = validate_crash_command(command)
        if not admission.allowed:
            message = format_admission_error(admission)
            entry = self.record(expert=expert, command=command, success=False, error=message)
            return message, False, entry
        started = datetime.now(timezone.utc).isoformat()
        start = time.monotonic()
        try:
            result = session.run_command(admission.command)
            output = str(getattr(result, "output", "") or "")
            success = bool(getattr(result, "success", False))
            error = "" if success else output
        except Exception as exc:  # preserve the real failure in the ledger
            output, success, error = "", False, str(exc)
        entry = self.record(
            expert=expert,
            command=admission.command,
            success=success,
            output=output,
            error=error,
            started_at=started,
            elapsed_ms=int((time.monotonic() - start) * 1000),
        )
        return (output if success else error), success, entry
