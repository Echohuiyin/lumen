"""Deterministic, bounded UAF/refcount path analysis backed by semcode.

This module intentionally does not use an LLM or source-text fallback.  It
turns the semcode MCP responses for explicitly identified entry points into a
small, auditable event graph.  The graph is bounded to direct callees: callers
must not mistake it for a whole-kernel proof, so every boundary that cannot be
resolved at that depth is preserved in ``PathCoverage``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
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


class SemcodePathAnalysisError(RuntimeError):
    """A semcode dependency/protocol failure that must block P2 analysis."""


class SemcodeFunctionInput(BaseModel):
    name: str = Field(description="Kernel function or symbol name")


def create_semcode_tools(*, command: str, args: Iterable[str], kernel_source_path: str,
                         evidence_sink: list[dict[str, Any]] | None = None) -> list[StructuredTool]:
    """Expose bounded Semcode lookups to tool experts without exposing MCP/shell."""
    client = SemcodeMcpClient(command=command, args=args, kernel_source_path=kernel_source_path)

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
    """Small synchronous client for semcode's newline-delimited MCP transport."""

    def __init__(
        self,
        *,
        command: str,
        args: Iterable[str],
        kernel_source_path: str,
        timeout_sec: int = 120,
    ) -> None:
        self.command = command
        self.args = tuple(args)
        self.kernel_source_path = kernel_source_path
        self.timeout_sec = timeout_sec

    def find_function(self, name: str) -> SemcodeFunction:
        function_text = self._call("find_function", {"name": name})
        calls_text = self._call("find_calls", {"name": name})
        return _parse_semcode_function(function_text, calls_text, requested_name=name)

    def _call(self, tool_name: str, arguments: dict[str, Any]) -> str:
        db_path = Path(self.kernel_source_path) / ".semcode.db"
        command = [
            *shlex.split(self.command), *_without_database_args(self.args),
            "-d", str(db_path), "--git-repo", self.kernel_source_path,
        ]
        if not command:
            raise SemcodePathAnalysisError("semcode command is empty")
        request_id = 2
        messages = (
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2024-11-05", "capabilities": {},
                    "clientInfo": {"name": "lumen-p2", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized", "params": {}},
            {
                "jsonrpc": "2.0", "id": request_id, "method": "tools/call",
                "params": {"name": tool_name, "arguments": arguments},
            },
        )
        payload = "".join(json.dumps(message) + "\n" for message in messages)
        try:
            completed = subprocess.run(
                command, input=payload, capture_output=True, text=True,
                timeout=self.timeout_sec, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SemcodePathAnalysisError(
                f"semcode {tool_name} failed: {type(exc).__name__}: {exc}"
            ) from exc
        if completed.returncode != 0:
            detail = (completed.stderr or completed.stdout).strip()[-500:]
            raise SemcodePathAnalysisError(
                f"semcode {tool_name} exited {completed.returncode}: {detail}"
            )
        for line in completed.stdout.splitlines():
            try:
                response = json.loads(line)
            except json.JSONDecodeError:
                continue
            if response.get("id") != request_id:
                continue
            if "error" in response:
                raise SemcodePathAnalysisError(
                    f"semcode {tool_name} MCP error: {response['error']}"
                )
            content = response.get("result", {}).get("content", [])
            texts = [item.get("text", "") for item in content if item.get("type") == "text"]
            if texts:
                return "\n".join(texts)
        raise SemcodePathAnalysisError(
            f"semcode {tool_name} returned no parseable MCP response"
        )


def analyze_uaf_paths(
    *,
    kernel_source_path: str,
    entry_points: Iterable[str],
    semcode_command: str,
    semcode_args: Iterable[str] = (),
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
    normalized_entries = _unique_identifiers(entry_points)
    if not normalized_source or not Path(normalized_source).is_absolute():
        return _blocked("kernel_source from input.txt must be an absolute path")
    db_path = Path(normalized_source) / ".semcode.db"
    if not Path(normalized_source).is_dir():
        return _blocked(f"kernel_source does not exist: {normalized_source}")
    if not db_path.is_dir():
        return _blocked(f"semcode index missing: {db_path}")
    if not normalized_entries:
        return _blocked("semcode path analysis requires a function entry point from input or crash evidence")
    if not semcode_command:
        return _blocked("semcode_mcp.command is not configured")

    resolved_command = shlex.split(semcode_command)
    if not resolved_command or not Path(resolved_command[0]).expanduser().is_file():
        return _blocked(f"semcode executable is unavailable: {semcode_command}")
    if not Path(resolved_command[0]).expanduser().stat().st_mode & 0o111:
        return _blocked(f"semcode executable is not executable: {resolved_command[0]}")

    try:
        kernel_commit = _git_head(normalized_source)
        semcode = client or SemcodeMcpClient(
            command=semcode_command,
            args=semcode_args,
            kernel_source_path=normalized_source,
        )
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
    )
    paths: list[RefcountPath] = []
    unresolved: list[str] = []
    limitations = [
        "P2 traverses semcode direct callees only; transitive paths require a later bounded-depth expansion.",
        "Macro expansion, function pointers, callbacks and cross-subsystem aliasing are not proven by this event graph.",
    ]
    evidence: list[dict[str, Any]] = []
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
    explicit = re.findall(r"\b(?:function|func|entry(?:[ _-]?point)?|frame)\s*[:=]\s*([A-Za-z_][A-Za-z0-9_]*)", text or "", re.IGNORECASE)
    stack = re.findall(r"\b([A-Za-z_][A-Za-z0-9_]*)\+0x[0-9a-fA-F]+", text or "")
    return [*explicit, *stack]


def _unique_identifiers(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", normalized):
            continue
        if normalized.lower() in _C_KEYWORDS or normalized in result:
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
    """Ignore MCP-configured database arguments; input.txt is the sole source."""
    cleaned: list[str] = []
    iterator = iter(args)
    for arg in iterator:
        if arg == "-d":
            next(iterator, None)
            continue
        if str(arg).startswith("--database="):
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


def _blocked(reason: str) -> SemcodePathAnalysisResult:
    return SemcodePathAnalysisResult(
        status="blocked", blocked_reason=reason,
        evidence=[{"kind": "semcode_event_graph", "status": "blocked", "reason": reason}],
    )
