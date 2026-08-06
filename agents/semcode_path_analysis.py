"""Deterministic, bounded UAF/refcount path analysis backed by semcode.

This module intentionally does not use an LLM or source-text fallback.  It
turns the semcode MCP responses for explicitly identified entry points into a
small, auditable event graph.  The graph is bounded to direct callees: callers
must not mistake it for a whole-kernel proof, so every boundary that cannot be
resolved at that depth is preserved in ``PathCoverage``.
"""

from __future__ import annotations

import os

from dataclasses import dataclass, field
import hashlib
import json
import queue
from pathlib import Path
import re
import shlex
import subprocess
import tempfile
import threading
import time
from typing import Any, Iterable
from pydantic import BaseModel, Field
from langchain_core.tools import StructuredTool

from agents.contracts import (
    PathAnalysisScope,
    PathCoverage,
    ReferenceEvent,
    RefcountPath,
    UafAnalysisContract,
)


_C_KEYWORDS = {
    "if", "for", "while", "switch", "return", "sizeof", "typeof",
    "likely", "unlikely", "do", "else", "case",
}
_GET_RE = re.compile(r"(?:^|_)(?:kref_get|refcount_inc(?:_not_zero)?|percpu_ref_get|atomic_inc|.*_get)$")
_PUT_RE = re.compile(r"(?:^|_)(?:kref_put|refcount_dec(?:_and_test)?|percpu_ref_put|atomic_dec|.*_put)$")
_TRANSFER_RE = re.compile(r"(?:^|_)(?:call_rcu|queue_work|schedule_work|task_work_add|queue_delayed_work)$")
_FREE_RE = re.compile(r"(?:^|_)(?:kfree|kvfree|vfree|kmem_cache_free|.*_release)$")
_ACCESS_RE = re.compile(r"(?:^|_)(?:.*_access|.*_use|.*_deref)$")
_ASYNC_RE = re.compile(r"\b(?:call_rcu|queue_(?:delayed_)?work|schedule_work|task_work_add)\s*\(")
_CONCURRENCY_RE = re.compile(r"\b(?:spin_lock|mutex_lock|rcu_read_lock|down_read|down_write|lockdep_)\w*\s*\(")
_ERROR_BRANCH_RE = re.compile(r"\b(?:goto\s+(?:err|out|fail)|return\s+-?[A-Z_0-9]+)")
_CALL_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
_FUNCTION_RE = re.compile(r"^Function:\s*([A-Za-z_][A-Za-z0-9_]*)", re.MULTILINE)
_LOCATION_RE = re.compile(r"^File:\s*(.+?):(\d+)(?:-(\d+))?", re.MULTILINE)
_DIRECT_CALL_RE = re.compile(r"^\s*\d+\.\s+([A-Za-z_][A-Za-z0-9_]*)\s*$", re.MULTILINE)
_ENTRY_FIELD_RE = re.compile(
    r"""(?ix)\b(?:function|func|entry(?:[ _-]?point)?|frame|symbol|caller|callee)\b
        [\"'`]?\s*[:=]\s*[\"'`]?([A-Za-z_][A-Za-z0-9_]*(?:\.(?:cold|isra|constprop|part)(?:\.\d+)*)?)"""
)
_TITLE_ENTRY_RE = re.compile(
    r"""(?ix)\btitle\s*[:=].*?\bin\s+([A-Za-z_][A-Za-z0-9_]*)"""
)

_STACK_FIELD_RE = re.compile(
    r"""(?ix)\b(?:top[_ -]?stack|stack[_ -]?(?:trace|frames?)|call[_ -]?trace)\b
        [\"'`]?\s*[:=]\s*\[([^\]]{0,2000})\]"""
)
_STACK_SYMBOL_RE = re.compile(
    r"(?<![A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_]*(?:\.(?:cold|isra|constprop|part)(?:\.\d+)*)?)"
)
_STACK_FRAME_RE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_]*(?:\.(?:cold|isra|constprop|part)(?:\.\d+)*)?)\+0x[0-9a-fA-F]+"
)

# These MCP tools resolve entities from a particular source snapshot.  Meta
# tools describe the index itself and deliberately do not accept git_sha.
_GIT_AWARE_TOOLS = {
    "find_function", "find_calls", "find_callees", "find_callers",
    "find_type", "find_callchain",
}


def configured_semcode_timeout_sec(default: int = 900) -> int:
    """Return the bounded Semcode query timeout from deployment environment."""
    raw = os.environ.get("LUMEN_SEMCODE_TIMEOUT_SEC", "").strip()
    try:
        value = int(raw) if raw else int(default)
    except (TypeError, ValueError):
        value = int(default)
    return max(30, value)


class SemcodePathAnalysisError(RuntimeError):
    """A semcode dependency/protocol failure that must block P2 analysis."""


def resolve_kernel_commit(
    kernel_source_path: str,
    expected_kernel_commit: str,
) -> str:
    """Resolve a declared full or abbreviated commit to its unique full SHA.

    Input rows may carry a 7-40 character Git object-id prefix. Git rev-parse
    verify proves that the prefix resolves to exactly one commit in the
    declared repository. The full 40-character object id is returned so later
    Semcode queries and durable contracts use the same immutable snapshot;
    absent or ambiguous prefixes stay blocked and never fall back to HEAD.
    """
    source = str(Path(kernel_source_path).expanduser().resolve()) if kernel_source_path else ""
    declared = str(expected_kernel_commit or "").strip().lower()
    if not source or not Path(source).is_dir():
        raise SemcodePathAnalysisError(f"kernel_source does not exist: {source}")
    if not re.fullmatch(r"[0-9a-f]{7,40}", declared):
        raise SemcodePathAnalysisError(
            "expected_kernel_commit must be a 7-40 character hexadecimal "
            f"git object id: {expected_kernel_commit}"
        )
    try:
        completed = subprocess.run(
            ["git", "-C", source, "rev-parse", "--verify", f"{declared}^{{commit}}"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SemcodePathAnalysisError(f"cannot resolve kernel commit: {exc}") from exc
    resolved = completed.stdout.strip().lower()
    if (
        completed.returncode != 0
        or not re.fullmatch(r"[0-9a-f]{40}", resolved)
        or not resolved.startswith(declared)
    ):
        detail = (completed.stderr or completed.stdout).strip()[-300:]
        raise SemcodePathAnalysisError(
            f"kernel source cannot uniquely resolve expected commit {declared}: "
            f"{detail or 'git returned no commit'}"
        )
    return resolved


def resolve_kernel_source_for_commit(
    kernel_source_path: str,
    expected_kernel_commit: str,
    *,
    workspace_root: str = "",
) -> str:
    """Return a source checkout whose default HEAD is the declared commit.

    A shared checkout may be at a newer HEAD while a benchmark row points at
    an older commit.  Semcode's git-aware calls are correct only when the
    repository passed to the MCP server is itself pinned. The pinned worktree
    must be fully checked out because Kernel Expert reads the C source directly;
    the Semcode database remains shared through a symlink.
    """
    source = str(Path(kernel_source_path).expanduser().resolve()) if kernel_source_path else ""
    if not source or not Path(source).is_dir():
        raise SemcodePathAnalysisError(f"kernel_source does not exist: {source}")
    target = resolve_kernel_commit(source, expected_kernel_commit)
    try:
        head = _git_head(source)
    except SemcodePathAnalysisError:
        head = ""
    if head.lower() == target:
        return source

    # A deployment may provide a high-capacity, lifecycle-managed filesystem
    # for detached source worktrees.  Prefer that explicit location even when
    # callers pass a session directory; otherwise a large kernel checkout can
    # exhaust the project filesystem during an otherwise valid analysis.
    configured_root = os.environ.get("LUMEN_KERNEL_SOURCE_WORKTREE_ROOT", "").strip()
    root = Path(configured_root).expanduser() if configured_root else (
        Path(workspace_root).expanduser() if workspace_root else Path(tempfile.gettempdir())
    )
    worktree = (root / "kernel-source-worktrees" / hashlib.sha256(
        f"{source}\0{target}".encode("utf-8")
    ).hexdigest()[:24]).resolve()
    worktree.parent.mkdir(parents=True, exist_ok=True)
    if worktree.exists():
        try:
            existing = _git_head(str(worktree))
        except SemcodePathAnalysisError:
            existing = ""
        if existing.lower() != target:
            raise SemcodePathAnalysisError(
                f"existing kernel source worktree is not pinned to {target}: {worktree}"
            )
    else:
        try:
            completed = subprocess.run(
                ["git", "-C", source, "worktree", "add", "--detach",
                 str(worktree), target],
                capture_output=True, text=True, timeout=120, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SemcodePathAnalysisError(f"cannot create pinned kernel worktree: {exc}") from exc
        if completed.returncode != 0:
            raise SemcodePathAnalysisError(
                f"cannot create pinned kernel worktree {worktree}: "
                f"{(completed.stderr or completed.stdout).strip()[-500:]}"
            )

    source_db = Path(source) / ".semcode.db"
    target_db = worktree / ".semcode.db"
    if not source_db.exists():
        raise SemcodePathAnalysisError(f"semcode index missing: {source_db}")
    if target_db.exists() or target_db.is_symlink():
        if target_db.is_symlink() and target_db.resolve() == source_db.resolve():
            return str(worktree)
        raise SemcodePathAnalysisError(f"pinned worktree has an unexpected .semcode.db: {target_db}")
    try:
        target_db.symlink_to(source_db, target_is_directory=source_db.is_dir())
    except OSError as exc:
        raise SemcodePathAnalysisError(f"cannot bind Semcode index into {worktree}: {exc}") from exc
    return str(worktree)


class SemcodeFunctionInput(BaseModel):
    name: str = Field(description="Kernel function or symbol name")


def create_semcode_tools(*, command: str, args: Iterable[str], kernel_source_path: str,
                         expected_kernel_commit: str = "",
                         evidence_sink: list[dict[str, Any]] | None = None) -> list[StructuredTool]:
    """Expose bounded Semcode lookups to tool experts without exposing MCP/shell."""
    client = SemcodeMcpClient(
        command=command,
        args=args,
        kernel_source_path=kernel_source_path,
        git_sha=expected_kernel_commit,
    )

    def find_function(name: str) -> str:
        try:
            function = client.find_function(name)
            if evidence_sink is not None:
                evidence_sink.append({"kind": "semcode_function", "function": function.name,
                                      "location": function.location,
                                      "direct_callees": list(function.direct_calls)})
            return json.dumps({
                "function": function.name, "location": function.location,
                "direct_callees": list(function.direct_calls), "body": function.body,
            }, ensure_ascii=False)
        except Exception as exc:
            return json.dumps({"status": "blocked", "function": name, "error": str(exc)}, ensure_ascii=False)

    def find_callers(name: str) -> str:
        try:
            result = client._call("find_callers", {"name": name})
            if evidence_sink is not None:
                evidence_sink.append({"kind": "semcode_callers", "function": name, "result": result[:4000]})
            return result
        except Exception as exc:
            return json.dumps({"status": "blocked", "function": name, "error": str(exc)}, ensure_ascii=False)

    def find_callees(name: str) -> str:
        return find_function(name)

    def find_type(name: str) -> str:
        try:
            result = client._call("find_type", {"name": name})
            if evidence_sink is not None:
                evidence_sink.append({"kind": "semcode_type", "type": name, "result": result[:4000]})
            return result
        except Exception as exc:
            return json.dumps({"status": "blocked", "type": name, "error": str(exc)}, ensure_ascii=False)

    def find_callchain(name: str) -> str:
        try:
            result = client._call("find_callchain", {"name": name})
            if evidence_sink is not None:
                evidence_sink.append({"kind": "semcode_callchain", "function": name, "result": result[:4000]})
            return result
        except Exception as exc:
            return json.dumps({"status": "blocked", "function": name, "error": str(exc)}, ensure_ascii=False)

    return [
        StructuredTool.from_function(
            func=find_function, name="semcode_find_function",
            description="Locate a kernel function with source location, body, and direct callees.",
            args_schema=SemcodeFunctionInput,
        ),
        StructuredTool.from_function(
            func=find_callers, name="semcode_find_callers",
            description="List callers of a kernel function from the indexed source tree.",
            args_schema=SemcodeFunctionInput,
        ),
        StructuredTool.from_function(
            func=find_callees, name="semcode_find_callees",
            description="List direct callees of a kernel function from the indexed source tree.",
            args_schema=SemcodeFunctionInput,
        ),
        StructuredTool.from_function(
            func=find_type, name="semcode_find_type",
            description="Locate a kernel struct or type in the indexed source tree.",
            args_schema=SemcodeFunctionInput,
        ),
        StructuredTool.from_function(
            func=find_callchain, name="semcode_find_callchain",
            description="Resolve a bounded call chain for a kernel function.",
            args_schema=SemcodeFunctionInput,
        ),
    ]


@dataclass(frozen=True)
class SemcodeFunction:
    name: str
    location: str
    body: str
    direct_calls: tuple[str, ...]


@dataclass(frozen=True)
class SemcodePathAnalysisResult:
    """P2 result, including a serialisable failure for workflow archival."""

    status: str
    analysis: UafAnalysisContract | None = None
    scope: PathAnalysisScope = field(default_factory=PathAnalysisScope)
    evidence: list[dict[str, Any]] = field(default_factory=list)
    blocked_reason: str = ""

    def as_dict(self) -> dict[str, Any]:
        analysis = self.analysis
        if analysis is not None:
            analysis = analysis.model_dump() if hasattr(analysis, "model_dump") else analysis.dict()
        scope = self.scope.model_dump() if hasattr(self.scope, "model_dump") else self.scope.dict()
        return {
            "status": self.status,
            "analysis": analysis,
            "scope": scope,
            "evidence": self.evidence,
            "blocked_reason": self.blocked_reason,
        }


class SemcodeMcpClient:
    """Synchronous client for semcode's newline-delimited MCP transport.

    Semcode starts commit indexing in a background task.  Keeping the MCP
    process alive while that task finishes is therefore part of correctness:
    closing stdin after the first query aborts the index and makes every retry
    start from the same incomplete database.
    """

    def __init__(
        self,
        *,
        command: str,
        args: Iterable[str],
        kernel_source_path: str,
        git_sha: str = "",
        timeout_sec: int | None = None,
    ) -> None:
        self.command = command
        self.args = tuple(args)
        self.kernel_source_path = kernel_source_path
        self.git_sha = git_sha.strip()
        self.timeout_sec = (
            configured_semcode_timeout_sec() if timeout_sec is None else max(int(timeout_sec), 1)
        )

    def find_function(self, name: str) -> SemcodeFunction:
        function_text = self._call("find_function", {"name": name})
        calls_text = self._call("find_calls", {"name": name})
        return _parse_semcode_function(function_text, calls_text, requested_name=name)

    def find_functions(self, names: Iterable[str]) -> list[SemcodeFunction]:
        """Resolve several functions with two MCP processes instead of one per symbol.

        Semcode's stdio server accepts multiple JSON-RPC requests before EOF.
        Keeping the function and direct-call queries in two batches preserves
        the exact git_sha on every request while avoiding repeated index
        startup for every stack frame in a crash report.
        """
        requested = [str(name).strip() for name in names if str(name).strip()]
        if not requested:
            return []
        try:
            function_texts = self._call_many([
                ("find_function", {"name": name}) for name in requested
            ])
            calls_texts = self._call_many([
                ("find_calls", {"name": name}) for name in requested
            ])
        except SemcodePathAnalysisError as batch_error:
            # Some semcode releases lose individual responses when several
            # requests are outstanding on one stdio session.  A single
            # request is still authoritative and avoids treating a transport
            # limitation as a missing exact-commit index.  Do not recover
            # from an explicit indexing failure: that remains fail-closed.
            if "no parseable MCP response" not in str(batch_error):
                raise
            recovered: list[SemcodeFunction] = []
            for name in requested:
                try:
                    recovered.append(self.find_function(name))
                except SemcodePathAnalysisError:
                    # Generated wrappers and syscall aliases may not have a
                    # standalone definition; preserve the existing behavior
                    # of retaining any other exact-commit functions.
                    continue
            if recovered:
                return recovered
            raise batch_error
        functions: list[SemcodeFunction] = []
        for name, function_text, calls_text in zip(requested, function_texts, calls_texts):
            try:
                functions.append(
                    _parse_semcode_function(function_text, calls_text, requested_name=name)
                )
            except SemcodePathAnalysisError:
                # Syscall-table entries, generated wrappers and architecture
                # aliases can legitimately have no standalone Semcode
                # definition.  They are unresolved context, not evidence that
                # every other exact-commit function lookup failed.
                continue
        if not functions:
            raise SemcodePathAnalysisError(
                "semcode returned no parseable functions for requested entries"
            )
        return functions

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        return self._call_many([(tool_name, arguments)])[0]

    @staticmethod
    def _response_text(response: dict[str, Any]) -> str:
        if response.get("error") is not None:
            raise SemcodePathAnalysisError(
                f"semcode MCP error: {response.get('error')}"
            )
        result = response.get("result") or {}
        content = result.get("content") or []
        texts = [
            str(item.get("text", ""))
            for item in content
            if isinstance(item, dict) and item.get("type") == "text"
        ]
        if not texts:
            raise SemcodePathAnalysisError(
                "semcode MCP response contained no parseable text content"
            )
        return "\n".join(texts)

    @staticmethod
    def _indexing_response_is_transient(text: str) -> bool:
        """Return true only for the server's explicit warming/indexing states."""
        return (
            "Database is currently being indexed" in text
            or "Database is empty. Background indexing hasn't started yet" in text
            or "Status: InProgress" in text
            or "Status: Analyzing " in text
        )

    @staticmethod
    def _indexing_response_failed(text: str) -> bool:
        return (
            "Database indexing failed:" in text
            or "Status: Failed:" in text
        )

    @staticmethod
    def _read_responses(
        output_queue: queue.Queue[str | None],
        request_ids: Iterable[int],
        deadline: float,
    ) -> dict[int, dict[str, Any]]:
        expected = set(request_ids)
        responses: dict[int, dict[str, Any]] = {}
        while expected - responses.keys():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                line = output_queue.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if line is None:
                break
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                # Semcode notifications and diagnostic output are not query
                # responses; malformed lines must not satisfy a request id.
                continue
            response_id = response.get("id")
            if response_id in expected:
                responses[response_id] = response
        missing = sorted(expected - responses.keys())
        if missing:
            raise SemcodePathAnalysisError(
                "semcode batch returned no parseable MCP response for request "
                f"ids {missing}"
            )
        return responses

    @staticmethod
    def _write_messages(process: subprocess.Popen[str], messages: Iterable[dict[str, Any]]) -> None:
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        try:
            if process.stdin is None:
                raise BrokenPipeError("semcode stdin is unavailable")
            process.stdin.write(payload)
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            raise SemcodePathAnalysisError(
                f"semcode MCP process stdin failed: {type(exc).__name__}: {exc}"
            ) from exc

    def _call_many(self, requests: Iterable[tuple[str, dict[str, Any]]]) -> list[str]:
        """Send a batch of MCP tool calls through one exact-commit server.

        The process remains alive while Semcode's background indexer warms the
        database.  Explicit transient index responses are retried in the same
        session; all other errors remain fail-closed.
        """
        request_items = list(requests)
        if not request_items:
            return []
        normalized_requests: list[tuple[str, dict[str, Any]]] = []
        for tool_name, arguments in request_items:
            if tool_name in _GIT_AWARE_TOOLS:
                if not self.git_sha:
                    raise SemcodePathAnalysisError(
                        f"semcode {tool_name} requires an explicit expected kernel commit"
                    )
                arguments = {**arguments, "git_sha": self.git_sha}
            normalized_requests.append((tool_name, arguments))
        db_path = Path(self.kernel_source_path) / ".semcode.db"
        command = [
            *shlex.split(self.command), *_without_database_args(self.args),
            "-d", str(db_path), "--git-repo", self.kernel_source_path,
        ]
        if not command:
            raise SemcodePathAnalysisError("semcode command is empty")
        initialize_messages = [
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "lumen-p2", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
        ]

        def tool_message(request_id: int, tool_name: str, arguments: dict[str, Any]) -> dict[str, Any]:
            return {
                "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            }

        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            raise SemcodePathAnalysisError(
                f"semcode batch failed: {type(exc).__name__}: {exc}"
            ) from exc

        output_queue: queue.Queue[str | None] = queue.Queue()
        stderr_tail: list[str] = []

        def drain_stdout() -> None:
            if process.stdout is None:
                output_queue.put(None)
                return
            try:
                for line in process.stdout:
                    output_queue.put(line)
            finally:
                output_queue.put(None)

        def drain_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                stderr_tail.append(line.rstrip())
                del stderr_tail[:-20]

        stdout_thread = threading.Thread(target=drain_stdout, name="semcode-stdout", daemon=True)
        stderr_thread = threading.Thread(target=drain_stderr, name="semcode-stderr", daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        deadline = time.monotonic() + max(float(self.timeout_sec), 1.0)
        next_request_id = 2
        try:
            self._write_messages(process, initialize_messages)
            request_ids = list(range(next_request_id, next_request_id + len(normalized_requests)))
            next_request_id += len(normalized_requests)
            while True:
                self._write_messages(
                    process,
                    [
                        tool_message(request_id, tool_name, arguments)
                        for request_id, (tool_name, arguments) in zip(request_ids, normalized_requests)
                    ],
                )
                responses = self._read_responses(output_queue, request_ids, deadline)
                texts = [self._response_text(responses[request_id]) for request_id in request_ids]
                if any(self._indexing_response_failed(text) for text in texts):
                    failed = next(text for text in texts if self._indexing_response_failed(text))
                    raise SemcodePathAnalysisError(f"semcode indexing failed: {failed[-500:]}")
                if not any(self._indexing_response_is_transient(text) for text in texts):
                    return texts

                if time.monotonic() >= deadline:
                    raise SemcodePathAnalysisError(
                        "semcode indexing did not become ready before timeout"
                    )
                status_id = next_request_id
                next_request_id += 1
                self._write_messages(
                    process, [tool_message(status_id, "indexing_status", {})]
                )
                status_response = self._read_responses(output_queue, [status_id], deadline)[status_id]
                status_text = self._response_text(status_response)
                if self._indexing_response_failed(status_text):
                    raise SemcodePathAnalysisError(
                        f"semcode indexing failed: {status_text[-500:]}"
                    )
                time.sleep(min(0.5, max(0.0, deadline - time.monotonic())))
                request_ids = list(range(next_request_id, next_request_id + len(normalized_requests)))
                next_request_id += len(normalized_requests)
        except SemcodePathAnalysisError:
            raise
        except (OSError, ValueError) as exc:
            detail = "; ".join(stderr_tail[-3:])
            raise SemcodePathAnalysisError(
                f"semcode batch failed: {type(exc).__name__}: {exc}{': ' + detail if detail else ''}"
            ) from exc
        finally:
            try:
                if process.stdin is not None:
                    process.stdin.close()
            except OSError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=2)


def verify_semcode_target(
    *,
    kernel_source_path: str,
    expected_kernel_commit: str,
    semcode_command: str,
    semcode_args: Iterable[str] = (),
    client: Any | None = None,
) -> dict[str, Any]:
    """Prove that Semcode can answer against the requested kernel snapshot.

    ``git_sha`` on an individual query is not sufficient: the MCP server can
    silently answer from its default HEAD when that snapshot is absent from
    the index.  The deployment therefore has to expose an indexed branch whose
    tip is the exact requested commit.  This function is a read-only gate and
    returns evidence for the durable contract; callers must block on failure.
    """
    source = str(Path(kernel_source_path).expanduser().resolve()) if kernel_source_path else ""
    declared_target = str(expected_kernel_commit or "").strip().lower()
    if not source or not Path(source).is_dir():
        return {"status": "blocked", "blocked_reason": f"kernel_source does not exist: {source}"}
    if not declared_target:
        return {
            "status": "blocked",
            "blocked_reason": "expected_kernel_commit is required for source verification",
        }
    try:
        target = resolve_kernel_commit(source, declared_target)
    except SemcodePathAnalysisError as exc:
        return {"status": "blocked", "blocked_reason": str(exc)}

    semcode = client or SemcodeMcpClient(
        command=semcode_command,
        args=semcode_args,
        kernel_source_path=source,
        git_sha=target,
    )
    try:
        branches = semcode._call("list_branches", {})
        indexing = semcode._call("indexing_status", {})
    except (SemcodePathAnalysisError, AttributeError) as exc:
        return {"status": "blocked", "blocked_reason": f"cannot verify Semcode target index: {exc}"}

    branch_re = re.compile(
        r"^[ \t]*(?P<branch>\S+)[ \t]+\((?P<tip>[0-9a-fA-F]{8,40})\)[ \t]*\n"
        r"(?P<details>(?:[ \t]+.*\n?)*)",
        re.MULTILINE,
    )
    indexed_branch = ""
    indexed_tip = ""
    indexed_details = ""
    for match in branch_re.finditer(branches or ""):
        tip = match.group("tip").lower()
        if target.startswith(tip) and "Status: up-to-date" in match.group("details"):
            indexed_branch = match.group("branch")
            indexed_tip = tip
            indexed_details = match.group("details").strip()
            break
    if not indexed_branch:
        return {
            "status": "blocked",
            "blocked_reason": (
                f"Semcode target commit {target} is not proven by an up-to-date indexed branch; "
                "refusing default-HEAD or direct-source fallback"
            ),
            "evidence": [{
                "kind": "semcode_source_verification",
                "kernel_source": source,
                "expected_commit": target,
                "git_object_verified": True,
                "indexed_branches": branches[:4000],
                "indexing_status": indexing[:1000],
            }],
        }
    return {
        "status": "ok",
        "resolved_commit": target,
        "evidence": [{
            "kind": "semcode_source_verification",
            "kernel_source": source,
            "expected_commit": target,
            "declared_commit": declared_target,
            "git_object_verified": True,
            "indexed_branch": indexed_branch,
            "indexed_tip_prefix": indexed_tip,
            "indexed_branch_details": indexed_details,
            "indexing_status": indexing[:1000],
            "default_head": _git_head(source),
        }],
    }


def analyze_uaf_paths(
    *,
    kernel_source_path: str,
    entry_points: Iterable[str],
    semcode_command: str,
    semcode_source_path: str = "",
    semcode_args: Iterable[str] = (),
    expected_kernel_commit: str = "",
    cached_evidence: dict[str, Any] | None = None,
    object_type: str = "unknown-with-rationale: object type is not derivable from a bounded call graph",
    concurrency_model: str = "unknown-with-rationale: bounded analysis does not prove interleavings",
    client: Any | None = None,
) -> SemcodePathAnalysisResult:
    """Build direct-call event paths for a UAF/refcount investigation.

    No source scanning or language-model fallback is used here.  Missing input,
    index, executable, or parseable MCP result is returned as an explicit
    ``blocked`` result so the caller can retain it in the final archive.
    """
    normalized_source = str(Path(kernel_source_path).expanduser()) if kernel_source_path else ""
    normalized_semcode_source = str(
        Path(semcode_source_path or kernel_source_path).expanduser()
    ) if (semcode_source_path or kernel_source_path) else ""
    normalized_entries = _unique_identifiers(entry_points)
    if not normalized_source or not Path(normalized_source).is_absolute():
        return _blocked("kernel_source from input.txt must be an absolute path")
    if not normalized_semcode_source or not Path(normalized_semcode_source).is_absolute():
        return _blocked("semcode_source_path must be an absolute path")
    db_path = Path(normalized_semcode_source) / ".semcode.db"
    if not Path(normalized_source).is_dir():
        return _blocked(f"kernel_source does not exist: {normalized_source}")
    if not Path(normalized_semcode_source).is_dir():
        return _blocked(f"semcode source does not exist: {normalized_semcode_source}")
    if not db_path.is_dir():
        return _blocked(f"semcode index missing: {db_path}")
    if not normalized_entries:
        return _blocked("semcode path analysis requires a function entry point from input or crash evidence")
    expected_kernel_commit = expected_kernel_commit.strip()
    if not expected_kernel_commit:
        return _blocked(
            "expected_kernel_commit is required; refusing Semcode default-HEAD resolution"
        )
    if not semcode_command:
        return _blocked("semcode_mcp.command is not configured")

    resolved_command = shlex.split(semcode_command)
    if not resolved_command or not Path(resolved_command[0]).expanduser().is_file():
        return _blocked(f"semcode executable is unavailable: {semcode_command}")
    if not Path(resolved_command[0]).expanduser().stat().st_mode & 0o111:
        return _blocked(f"semcode executable is not executable: {resolved_command[0]}")

    try:
        verification = verify_semcode_target(
            kernel_source_path=normalized_semcode_source,
            expected_kernel_commit=expected_kernel_commit,
            semcode_command=semcode_command,
            semcode_args=semcode_args,
            client=client,
        )
        if verification["status"] != "ok":
            return _blocked(
                verification["blocked_reason"],
                evidence=verification.get("evidence", []),
            )
        kernel_commit = str(
            verification.get("resolved_commit") or expected_kernel_commit
        ).strip().lower()
        semcode = client or SemcodeMcpClient(
            command=semcode_command,
            args=semcode_args,
            kernel_source_path=normalized_semcode_source,
            git_sha=kernel_commit,
        )
        functions = _functions_from_cached_evidence(
            cached_evidence,
            source_path=normalized_source,
            expected_commit=kernel_commit,
            requested=normalized_entries,
        )
        if functions is None:
            batch_finder = getattr(semcode, 'find_functions', None)
            if callable(batch_finder):
                functions = batch_finder(normalized_entries)
            else:
                # Keep compatibility with injected test doubles that implement
                # only the original single-function interface.
                functions = [semcode.find_function(entry) for entry in normalized_entries]
    except SemcodePathAnalysisError as exc:
        return _blocked(str(exc))
    except (OSError, ValueError) as exc:
        return _blocked(f"semcode path analysis failed: {type(exc).__name__}: {exc}")

    scope = PathAnalysisScope(
        kernel_commit=kernel_commit,
        kernel_config="unknown-with-rationale: input.txt does not provide a target .config",
        entry_points=[function.name for function in functions],
        object_type=object_type,
        concurrency_model=concurrency_model,
        source_domains=[{
            "kind": "kernel", "root": normalized_source, "analysis": "semcode",
        }],
    )
    paths: list[RefcountPath] = []
    unresolved: list[str] = []
    limitations = [
        "P2 traverses semcode direct callees only; transitive paths require a later bounded-depth expansion.",
        "Macro expansion, function pointers, callbacks and cross-subsystem aliasing are not proven by this event graph.",
    ]
    evidence: list[dict[str, Any]] = [*verification.get("evidence", [])]
    for function in functions:
        generated, function_unresolved, function_evidence, function_limits = _paths_for_function(function)
        paths.extend(generated)
        unresolved.extend(function_unresolved)
        evidence.extend(function_evidence)
        limitations.extend(function_limits)

    if not paths:
        return _blocked("semcode returned no analyzable entry-point functions")
    max_path = _select_max_likely_path(paths)
    analysis = UafAnalysisContract(
        case_id=_stable_case_id(normalized_source, kernel_commit, normalized_entries),
        paths=paths,
        coverage=PathCoverage(
            normal_paths_considered=True,
            error_paths_considered=True,
            transfer_paths_considered=True,
            async_paths_considered=True,
            concurrency_paths_considered=True,
            unresolved_indirect_calls=sorted(set(unresolved)),
            limitations=_unique_text(limitations),
        ),
        max_likely_path_id=max_path.id,
        selection_rationale=(
            "P2 deterministic ranking: free followed by access, then non-zero net_delta, "
            "then transfer/concurrency candidates. Human/LLM evidence may refine the rationale but must retain these paths."
        ),
        reproduction_target_path_id=max_path.id,
        target_contexts=[function.name for function in functions],
    )
    evidence.append({
        "kind": "semcode_event_graph",
        "kernel_source": normalized_source,
        "kernel_commit": kernel_commit,
        "entry_points": scope.entry_points,
        "path_count": len(paths),
        "traversal": "direct_callees_only",
    })
    return SemcodePathAnalysisResult(
        status="ok", analysis=analysis, scope=scope, evidence=evidence,
    )


def extract_semcode_entry_points(*texts: str, expert_results: Iterable[dict[str, Any]] = ()) -> list[str]:
    """Extract only explicit function/stack-frame identifiers from case evidence."""
    candidates: list[str] = []
    for text in texts:
        candidates.extend(_extract_identifiers(text))
    for result in expert_results:
        candidates.extend(_extract_identifiers(str(result.get("analysis_output", ""))))
        candidates.extend(_extract_identifiers(json.dumps(result, ensure_ascii=False, default=str)))
        structured = result.get("structured_output") or {}
        all_evidence = [*(result.get("evidence", []) or []), *(structured.get("evidence", []) or [])]
        for evidence in all_evidence:
            for frame in evidence.get("frames", []) or []:
                candidates.extend(_extract_identifiers(str(frame)))
            for key in ("function", "caller", "callee"):
                candidates.extend(_unique_identifiers([str(evidence.get(key, ""))]))
    return _unique_identifiers(candidates)


def render_semcode_analysis_context(result: SemcodePathAnalysisResult) -> str:
    """Render immutable deterministic evidence for the Kernel Expert prompt."""
    if result.status != "ok" or result.analysis is None:
        return f"## semcode P2 自动路径分析\n状态：blocked\n原因：{result.blocked_reason}"
    return (
        "## semcode P2 自动路径分析（不可删除的确定性证据）\n"
        "以下事件图只覆盖直接 callee；所有 limitations 必须随最终 contract 归档。"
        "可结合现场证据补充解释，但不得删除或改写其路径/事件/net_delta。\n"
        f"```json\n{json.dumps(result.as_dict(), ensure_ascii=False, indent=2)}\n```"
    )


def _parse_semcode_function(function_text: str, calls_text: str, *, requested_name: str) -> SemcodeFunction:
    name_match = _FUNCTION_RE.search(function_text)
    if not name_match:
        raise SemcodePathAnalysisError(f"semcode find_function returned no function for {requested_name}")
    location_match = _LOCATION_RE.search(function_text)
    location = ""
    if location_match:
        location = f"{location_match.group(1)}:{location_match.group(2)}"
    body_marker = "Body:\n"
    body = function_text.split(body_marker, 1)[1] if body_marker in function_text else ""
    if not body.strip():
        raise SemcodePathAnalysisError(f"semcode find_function returned no body for {requested_name}")
    direct_calls = tuple(_unique_identifiers(_DIRECT_CALL_RE.findall(calls_text)))
    return SemcodeFunction(name=name_match.group(1), location=location, body=body, direct_calls=direct_calls)


def _functions_from_cached_evidence(
    payload: dict[str, Any] | None,
    *,
    source_path: str,
    expected_commit: str,
    requested: Iterable[str],
) -> list[SemcodeFunction] | None:
    """Reuse a complete exact-commit find_function batch.

    Kernel Expert materializes this batch immediately before P2 path analysis.
    Re-querying the same checkout is both redundant and expensive on large
    kernel indexes. Cache reuse is deliberately fail-closed: any status,
    failure, source, commit, or entry mismatch leaves the original MCP query
    path untouched.
    """
    if not isinstance(payload, dict) or payload.get('status') != 'ok':
        return None
    if payload.get('failures'):
        return None
    cached_source = str(payload.get('kernel_source') or '').strip()
    if not cached_source or Path(cached_source).resolve() != Path(source_path).resolve():
        return None
    cached_commit = str(payload.get('expected_kernel_commit') or '').strip().lower()
    if cached_commit != str(expected_commit or '').strip().lower():
        return None
    entries = payload.get('entries')
    if not isinstance(entries, list) or not entries:
        return None
    by_name: dict[str, dict[str, Any]] = {}
    for item in entries:
        if not isinstance(item, dict):
            continue
        name = str(item.get('function') or '').strip()
        if name and name not in by_name:
            by_name[name] = item
    functions: list[SemcodeFunction] = []
    for requested_name in requested:
        item = by_name.get(str(requested_name).strip())
        if item is None:
            continue
        result = str(item.get('result') or '')
        if not result.strip():
            continue
        try:
            # find_function already includes its numbered direct-call
            # section, so the cached response is sufficient for the bounded
            # event graph; no second find_calls MCP batch is needed.
            functions.append(
                _parse_semcode_function(result, result, requested_name=str(requested_name))
            )
        except SemcodePathAnalysisError:
            continue
    return functions or None


def _paths_for_function(function: SemcodeFunction) -> tuple[list[RefcountPath], list[str], list[dict[str, Any]], list[str]]:
    evidence = [{
        "kind": "semcode_function", "function": function.name,
        "location": function.location, "direct_calls": list(function.direct_calls),
    }]
    # ``find_calls`` is the graph authority.  Function bodies can contain
    # macro-expanded helper names or text from generated wrappers, so use a
    # lexical body scan only when semcode could not provide direct edges.
    body_calls = _unique_identifiers(_CALL_RE.findall(function.body))
    calls = list(function.direct_calls) or body_calls
    events = [ReferenceEvent(
        kind="access", function=function.name, location=function.location,
        evidence=[{"kind": "semcode", "role": "entry_point", "location": function.location}],
    )]
    for called in calls:
        kind, delta = _classify_event(called)
        if kind == "unknown":
            continue
        events.append(ReferenceEvent(
            kind=kind, function=called, location=function.location, ref_delta=delta,
            evidence=[{"kind": "semcode", "caller": function.name, "edge": f"{function.name}->{called}"}],
        ))
    unresolved = _indirect_call_markers(function.body, function.name)
    limits: list[str] = []
    if not any(event.kind != "access" for event in events):
        limits.append(f"{function.name}: no recognized refcount event among direct callees")
    paths = [_make_path(function.name, "normal", events, function.location)]
    if _ERROR_BRANCH_RE.search(function.body):
        paths.append(_make_path(function.name, "error", events, function.location,
                                unknowns=["error branch detected; direct-call event ordering is not path-sensitive"]))
    else:
        limits.append(f"{function.name}: no explicit goto err/out or error return found; macro-mediated error paths remain unresolved")
    if _ASYNC_RE.search(function.body):
        transfer_events = [event for event in events if event.kind in {"access", "transfer"}]
        paths.append(_make_path(function.name, "async", transfer_events, function.location,
                                unknowns=["async callback body is outside direct-callee traversal"]))
    else:
        limits.append(f"{function.name}: no recognized async transfer primitive among direct callees")
    if _CONCURRENCY_RE.search(function.body):
        paths.append(_make_path(function.name, "concurrency", events, function.location,
                                unknowns=["lock/RCU primitive detected; schedule interleavings are not enumerated"]))
    else:
        limits.append(f"{function.name}: no recognized lock/RCU primitive; external concurrency remains unresolved")
    return paths, unresolved, evidence, limits


def _make_path(
    entry: str,
    path_kind: str,
    events: list[ReferenceEvent],
    location: str,
    *,
    unknowns: list[str] | None = None,
) -> RefcountPath:
    event_text = " -> ".join(f"{event.kind}:{event.function}" for event in events)
    summary = f"{entry} [{path_kind}] -> {event_text}"
    terminal = _terminal_state(events)
    return RefcountPath(
        id=f"semcode-{hashlib.sha256(summary.encode('utf-8')).hexdigest()[:12]}",
        summary=summary,
        events=events,
        net_delta=sum(event.ref_delta for event in events),
        terminal_state=terminal,
        evidence=[{"kind": "semcode_path", "entry": entry, "path_kind": path_kind, "location": location}],
        unknowns=unknowns or [],
    )


def _terminal_state(events: list[ReferenceEvent]) -> str:
    free_positions = [index for index, event in enumerate(events) if event.kind == "free"]
    if free_positions and any(event.kind == "access" for event in events[free_positions[0] + 1:]):
        return "uaf"
    net_delta = sum(event.ref_delta for event in events)
    if net_delta > 0:
        return "leaked"
    if free_positions:
        return "released"
    return "live" if net_delta == 0 else "unknown"


def _select_max_likely_path(paths: list[RefcountPath]) -> RefcountPath:
    rank = {"uaf": 4, "leaked": 3, "released": 2, "live": 1, "unknown": 0}
    return max(paths, key=lambda path: (rank[path.terminal_state], abs(path.net_delta), -len(path.unknowns), path.id))


def _classify_event(function_name: str) -> tuple[str, int]:
    name = function_name.lower()
    if _GET_RE.fullmatch(name):
        return "get", 1
    if _PUT_RE.fullmatch(name):
        return "put", -1
    if _TRANSFER_RE.fullmatch(name):
        return "transfer", 0
    if _FREE_RE.fullmatch(name):
        return "free", 0
    if _ACCESS_RE.fullmatch(name):
        return "access", 0
    return "unknown", 0


def _extract_identifiers(text: str) -> list[str]:
    """Extract kernel symbols from explicit and structured crash evidence."""
    value = text or ""
    explicit = _ENTRY_FIELD_RE.findall(value)
    title_entries = _TITLE_ENTRY_RE.findall(value)
    structured_stack: list[str] = []
    for block in _STACK_FIELD_RE.findall(value):
        structured_stack.extend(_STACK_SYMBOL_RE.findall(block))
    stack = _STACK_FRAME_RE.findall(value)
    return [*explicit, *title_entries, *structured_stack, *stack]


def _unique_identifiers(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        normalized = re.sub(r"\.(?:cold|isra|constprop|part)(?:\.\d+)*$", "", normalized)
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized):
            continue
        if normalized.lower() in _C_KEYWORDS or normalized.lower() in {
            "unknown", "none", "null", "n/a", "unresolved", "anonymous",
        } or normalized in result:
            continue
        result.append(normalized)
    return result


def _unique_text(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        if value and value not in result:
            result.append(value)
    return result


def _without_database_args(args: Iterable[str]) -> list[str]:
    """Ignore MCP-configured index arguments; the declared source is authoritative."""
    cleaned: list[str] = []
    iterator = iter(args)
    for arg in iterator:
        if arg in ("-d", "--database", "--git-repo"):
            next(iterator, None)
            continue
        if str(arg).startswith("--database=") or str(arg).startswith("--git-repo="):
            continue
        cleaned.append(str(arg))
    return cleaned


def _indirect_call_markers(body: str, function_name: str) -> list[str]:
    markers: list[str] = []
    if re.search(r"(?:->\s*\w+|\(\s*\*\s*\w+\s*\)\s*\()", body):
        markers.append(f"{function_name}: indirect/function-pointer call")
    return markers


def _git_head(kernel_source_path: str) -> str:
    try:
        completed = subprocess.run(
            ["git", "-C", kernel_source_path, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=10, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SemcodePathAnalysisError(f"cannot determine kernel commit: {exc}") from exc
    if completed.returncode != 0 or not completed.stdout.strip():
        raise SemcodePathAnalysisError("kernel_source is not a readable git worktree")
    return completed.stdout.strip()


def _stable_case_id(kernel_source_path: str, kernel_commit: str, entry_points: Iterable[str]) -> str:
    material = "\0".join([kernel_source_path, kernel_commit, *entry_points])
    return f"semcode-{hashlib.sha256(material.encode('utf-8')).hexdigest()[:16]}"


def _blocked(
    reason: str,
    *,
    evidence: Iterable[dict[str, Any]] = (),
) -> SemcodePathAnalysisResult:
    return SemcodePathAnalysisResult(
        status="blocked", blocked_reason=reason,
        evidence=[
            {"kind": "semcode_event_graph", "status": "blocked", "reason": reason},
            *list(evidence),
        ],
    )
