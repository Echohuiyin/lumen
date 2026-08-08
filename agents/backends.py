import json
import os
import re
import shutil
import shlex
import signal
import subprocess
import tempfile
from dataclasses import dataclass
from threading import Event, Lock, Thread
import time
import uuid
from pathlib import Path

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


# Debug dump toggle: set LUMEN_DEBUG_CLAUDE_CLI=1 to capture the full CLI
# command, prompts, and live stdout/stderr of every ClaudeCodeBackend.invoke
# call into /tmp/lumen_outputs/. Default off to avoid disk accumulation.
_DEBUG_CLI = os.environ.get("LUMEN_DEBUG_CLAUDE_CLI", "").strip() in {"1", "true", "yes"}
# Stream-json toggle (implies _DEBUG_CLI): set LUMEN_CLAUDE_STREAM_JSON=1 to
# run the CLI with --output-format stream-json --verbose so live.log captures
# each tool_use/tool_result/assistant event as it happens, instead of one
# final JSON blob at the end. Useful for diagnosing where kernel_expert's
# agent loop stalls.
_STREAM_JSON = os.environ.get("LUMEN_CLAUDE_STREAM_JSON", "").strip() in {"1", "true", "yes"}
_DUMP_DIR = Path("/tmp/lumen_outputs")


def _codex_artifact_signature(workdir: Path) -> tuple[tuple[str, int, int], ...] | None:
    """Return a stable signature once this invocation wrote a valid handoff.

    The Codex CLI can finish the contract and C source, then stall while
    producing its cosmetic final agent message.  This helper only observes
    the unique current workdir and accepts the handoff shape; the normal
    Kernel Expert contract/static gates remain authoritative after sync.
    """
    contract_path = workdir / "KERNEL_CONTRACT.json"
    try:
        payload = json.loads(contract_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("contract") not in {None, "KERNEL_CONTRACT"}:
        return None
    status = str(payload.get("status") or "")
    raw_reason = payload.get("blocked_reason")
    if status == "blocked":
        if isinstance(raw_reason, dict):
            raw_reason = raw_reason.get("precise_limitation") or raw_reason.get("consequence")
        if not str(raw_reason or "").strip():
            return None
    elif status not in {"ok", "ready"}:
        return None
    raw_root_cause = payload.get("root_cause")
    if isinstance(raw_root_cause, dict):
        raw_root_cause = (
            raw_root_cause.get("verified_path")
            or raw_root_cause.get("verified_invariant")
            or raw_root_cause.get("verified")
            or raw_root_cause.get("observed_violation")
            or raw_root_cause.get("summary")
            or raw_root_cause.get("source_reasoning")
        )
    if status in {"ok", "ready"} and not str(raw_root_cause or "").strip():
        return None
    oracle = payload.get("call_chain_oracle") or {}
    if not isinstance(oracle, dict):
        return None
    if status in {"ok", "ready"} and not (
        oracle.get("required_top_frames")
        or oracle.get("required_frames")
        or oracle.get("required_order_top_to_bottom")
        or oracle.get("strict_ordered_core")
    ):
        return None
    reproducer = payload.get("reproducer") or {}
    if not isinstance(reproducer, dict):
        return None
    source_files = reproducer.get("source_files") or []
    if isinstance(source_files, list):
        source_files = [
            item.get("path") if isinstance(item, dict) and item.get("path") else item
            for item in source_files
        ]
    entry_source = str(reproducer.get("entry_source") or "").strip()
    if status == "ready" and not entry_source and isinstance(source_files, list) and source_files:
        entry_source = str(source_files[0])
    if status in {"ok", "ready"} and (
        reproducer.get("language") != "c"
        or reproducer.get("artifact_type") != "userspace"
        or not isinstance(source_files, list)
        or not source_files
        or not entry_source
        or entry_source not in {str(item) for item in source_files}
    ):
        return None
    try:
        workdir_root = workdir.resolve()
        paths = [contract_path.resolve()]
        for raw_name in source_files:
            name = str(raw_name or "")
            source = (workdir / name).resolve()
            source.relative_to(workdir_root)
            if source.suffix.lower() not in {".c", ".h"} or not source.is_file():
                return None
            paths.append(source)
        return tuple(
            (str(path.relative_to(workdir_root)), path.stat().st_size, path.stat().st_mtime_ns)
            for path in paths
        )
    except (OSError, RuntimeError, TypeError, ValueError):
        return None


def _codex_artifact_grace_seconds() -> float:
    try:
        value = float(os.environ.get("LUMEN_CODEX_ARTIFACT_GRACE_SECONDS", "15"))
    except (TypeError, ValueError):
        value = 15.0
    return min(120.0, max(3.0, value))


def _format_stream_event(line: str) -> str:
    """Render a stream-json JSONL line as a readable single-line summary.

    Returns the original line (truncated) if parsing fails so the live log
    never silently drops output.
    """
    line = line.rstrip("\n")
    if not line:
        return ""
    try:
        evt = json.loads(line)
    except json.JSONDecodeError:
        return f"[raw] {line[:300]}"
    etype = evt.get("type", "?")
    subtype = evt.get("subtype", "")

    if etype == "system" and subtype == "init":
        return f"[init] session={evt.get('session_id','')[:8]} model={evt.get('model','')} tools={len(evt.get('tools',[]))}"
    if etype == "assistant":
        msg = evt.get("message", {}) or {}
        content = msg.get("content", []) or []
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                txt = (block.get("text") or "").replace("\n", " ")
                parts.append(f"text:{txt[:200]}")
            elif btype == "tool_use":
                name = block.get("name", "?")
                inp = block.get("input", {})
                # Compact JSON, capped to keep lines readable
                inp_str = json.dumps(inp, ensure_ascii=False)
                if len(inp_str) > 300:
                    inp_str = inp_str[:300] + "..."
                parts.append(f"tool_use:{name}({inp_str})")
            elif btype == "thinking":
                parts.append("thinking:(...)")
        return f"[assistant] {' | '.join(parts)}" if parts else "[assistant] (empty)"
    if etype == "tool_result":
        # tool_result events carry the tool output (stdout/stderr/...
        content = evt.get("content", "")
        if isinstance(content, list):
            content = " ".join(
                c.get("text", "") if isinstance(c, dict) else str(c)
                for c in content
            )
        txt = str(content).replace("\n", " ")
        return f"[tool_result] {txt[:300]}"
    if etype == "user":
        # Claude Code wraps tool results in a `user` message whose content
        # is a list of tool_result blocks. Unwrap so the live log shows the
        # actual tool output, not the raw envelope.
        msg = evt.get("message", {}) or {}
        content = msg.get("content", []) or []
        if isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tc = block.get("content", "")
                    if isinstance(tc, list):
                        tc = " ".join(
                            c.get("text", "") if isinstance(c, dict) else str(c)
                            for c in tc
                        )
                    txt = str(tc).replace("\n", " ")
                    is_err = block.get("is_error", False)
                    return f"[tool_result]{' ERROR' if is_err else ''} {txt[:300]}"
        return f"[user] {line[:300]}"
    if etype == "result":
        u = evt.get("usage") or {}
        usage_str = (
            f" input_tokens={u.get('input_tokens')} output_tokens={u.get('output_tokens')} "
            f"total_cost_usd={u.get('total_cost_usd')}"
            if u
            else ""
        )
        return (
            f"[result] subtype={subtype} is_error={evt.get('is_error')} "
            f"num_turns={evt.get('num_turns')} duration_ms={evt.get('duration_ms')}{usage_str} "
            f"result={str(evt.get('result',''))[:300]}"
        )
    return f"[{etype}/{subtype}] {line[:300]}"


class _ToolCallTracker:
    """Track per-tool-call wall-clock duration from stream-json events.

    Claude Code stream-json emits:
    - assistant events whose message.content contains tool_use blocks
      (each with an `id`) — recorded as the call's start time.
    - user events whose message.content contains tool_result blocks
      (each with `tool_use_id` matching a prior tool_use) — recorded
      as the call's end time.

    Wall clock is read when the line arrives from the CLI pipe. With
    bufsize=1 line-buffered stdout, arrival lag is <100ms, which is
    fine for spotting slow tools (seconds+) but not for micro-bench.
    Assistant events lack a timestamp field, so we can't use CLI's
    own clock; user events do carry one but only for the end side.
    """

    def __init__(self) -> None:
        # tool_use_id -> (tool_name, start_monotonic)
        self._pending: dict[str, tuple[str, float]] = {}
        # tool_name -> list of durations in seconds
        self._completed: dict[str, list[float]] = {}
        # tool_use_id -> (tool_name, duration_sec) for the detail log
        self._calls: list[dict] = []
        self._t0 = time.monotonic()

    def observe(self, evt: dict) -> None:
        etype = evt.get("type")
        if etype == "assistant":
            content = (evt.get("message") or {}).get("content") or []
            for block in content:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "tool_use":
                    tid = block.get("id", "")
                    name = block.get("name", "?")
                    if tid:
                        self._pending[tid] = (name, time.monotonic())
        elif etype == "user":
            content = (evt.get("message") or {}).get("content") or []
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    if block.get("type") == "tool_result":
                        tid = block.get("tool_use_id", "")
                        if not tid or tid not in self._pending:
                            continue
                        name, start = self._pending.pop(tid)
                        dur = time.monotonic() - start
                        self._completed.setdefault(name, []).append(dur)
                        self._calls.append({
                            "tool": name,
                            "tool_use_id": tid,
                            "duration_sec": round(dur, 3),
                            "is_error": bool(block.get("is_error", False)),
                        })

    def summary(self) -> dict:
        per_tool: list[dict] = []
        for name, durations in self._completed.items():
            if not durations:
                continue
            total = sum(durations)
            per_tool.append({
                "tool": name,
                "calls": len(durations),
                "total_sec": round(total, 3),
                "avg_sec": round(total / len(durations), 3),
                "max_sec": round(max(durations), 3),
                "min_sec": round(min(durations), 3),
            })
        per_tool.sort(key=lambda x: x["total_sec"], reverse=True)
        return {
            "wall_clock_sec": round(time.monotonic() - self._t0, 3),
            "total_calls": len(self._calls),
            "per_tool": per_tool,
            "calls": self._calls,
        }

    def render_summary_table(self) -> str:
        s = self.summary()
        if not s["per_tool"]:
            return "(no tool calls observed)"
        lines = [
            f"{'tool':<14}{'calls':>6}{'total_s':>10}{'avg_s':>8}{'max_s':>8}",
            "-" * 46,
        ]
        for row in s["per_tool"]:
            lines.append(
                f"{row['tool']:<14}{row['calls']:>6}"
                f"{row['total_sec']:>10.3f}{row['avg_sec']:>8.3f}{row['max_sec']:>8.3f}"
            )
        lines.append("")
        lines.append(f"total tool calls: {s['total_calls']}")
        lines.append(f"wall clock (this session): {s['wall_clock_sec']}s")
        return "\n".join(lines)


class CLIBackend:
    """LLM backend that calls a CLI tool via subprocess."""

    def __init__(
        self,
        cli_command: str | list[str],
        timeout: int = 120,
        cli_stdin: bool = False,
    ):
        if isinstance(cli_command, str):
            self._cmd_prefix = shlex.split(cli_command)
        else:
            self._cmd_prefix = list(cli_command)
        self._timeout = timeout
        self._cli_stdin = cli_stdin

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        prompt = self._build_prompt(messages)
        try:
            if self._cli_stdin:
                result = subprocess.run(
                    self._cmd_prefix,
                    input=prompt,
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                )
            else:
                result = subprocess.run(
                    self._cmd_prefix + [prompt],
                    capture_output=True,
                    text=True,
                    timeout=self._timeout,
                )
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"CLI backend timed out after {self._timeout}s")
        except FileNotFoundError:
            raise RuntimeError(f"CLI command not found: {self._cmd_prefix[0]}")

        if result.returncode != 0:
            raise RuntimeError(
                f"CLI backend failed (exit {result.returncode}): {result.stderr.strip()}"
            )
        return AIMessage(content=result.stdout)

    def stream(self, messages: list[BaseMessage]):
        """Yield AIMessage chunks line-by-line from subprocess stdout."""
        prompt = self._build_prompt(messages)
        try:
            if self._cli_stdin:
                proc = subprocess.Popen(
                    self._cmd_prefix,
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                proc.stdin.write(prompt)
                proc.stdin.close()
            else:
                proc = subprocess.Popen(
                    self._cmd_prefix + [prompt],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
        except FileNotFoundError:
            raise RuntimeError(f"CLI command not found: {self._cmd_prefix[0]}")

        try:
            for line in proc.stdout:
                yield AIMessage(content=line)
            proc.wait(timeout=self._timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError(f"CLI backend timed out after {self._timeout}s")

        if proc.returncode != 0:
            stderr = proc.stderr.read()
            raise RuntimeError(
                f"CLI backend failed (exit {proc.returncode}): {stderr.strip()}"
            )

    @staticmethod
    def _build_prompt(messages: list[BaseMessage]) -> str:
        """Combine SystemMessage + HumanMessage into a single prompt string."""
        parts = []
        for msg in messages:
            if isinstance(msg, SystemMessage):
                parts.append(f"[System Instructions]\n{msg.content}")
            elif isinstance(msg, HumanMessage):
                parts.append(f"[User Message]\n{msg.content}")
            else:
                parts.append(str(msg.content))
        return "\n\n".join(parts)


class HTTPBackend:
    """LLM backend that calls an HTTP API endpoint."""

    def __init__(
        self,
        url: str,
        headers: dict | None = None,
        timeout: int = 120,
        model_name: str = "",
        response_path: str = "choices.0.message.content",
    ):
        self._url = url
        self._headers = headers or {}
        self._timeout = timeout
        self._model_name = model_name
        self._response_path = response_path

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        payload = self._build_payload(messages)
        try:
            resp = httpx.post(
                self._url,
                json=payload,
                headers=self._headers,
                timeout=self._timeout,
            )
            resp.raise_for_status()
        except httpx.TimeoutException:
            raise RuntimeError(f"HTTP backend timed out after {self._timeout}s")
        except httpx.HTTPStatusError as e:
            raise RuntimeError(
                f"HTTP backend error {e.response.status_code}: {e.response.text}"
            )

        try:
            data = resp.json()
        except Exception:
            raise RuntimeError(
                f"HTTP backend returned non-JSON response (status {resp.status_code}): {resp.text[:500]}"
            )

        content = self._extract_content(data)
        if not content:
            raise RuntimeError(
                f"HTTP backend returned empty content (response_path={self._response_path!r}): {str(data)[:500]}"
            )
        return AIMessage(content=content)

    def stream(self, messages: list[BaseMessage]):
        """Non-streaming fallback: invoke and yield single chunk."""
        result = self.invoke(messages)
        yield result

    def _build_payload(self, messages: list[BaseMessage]) -> dict:
        """Build OpenAI-compatible request payload."""
        formatted = []
        for msg in messages:
            role = "user"
            if msg.type == "system":
                role = "system"
            elif msg.type == "ai":
                role = "assistant"
            formatted.append({"role": role, "content": msg.content})
        payload = {"messages": formatted}
        if self._model_name:
            payload["model"] = self._model_name
        return payload

    def _extract_content(self, data: dict) -> str:
        """Extract content from response using configurable path."""
        keys = self._response_path.split(".")
        current = data
        for key in keys:
            if isinstance(current, dict):
                current = current.get(key, "")
            elif isinstance(current, list) and key.isdigit():
                idx = int(key)
                current = current[idx] if idx < len(current) else ""
            else:
                return str(current)
        return str(current) if current else ""


class AnthropicBackend:
    """Minimal Anthropic-compatible Messages API backend with tool support."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model_name: str,
        timeout: int = 120,
        max_tokens: int = 8192,
        temperature: float = 0,
        max_retries: int = 5,
    ):
        self._base_url = base_url.rstrip("/")
        self._api_key = api_key
        self._model_name = model_name
        self._timeout = timeout
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._max_retries = max_retries
        self._tools: list | None = None  # bound tools (set via bind_tools)

    def bind_tools(self, tools):
        """Return a new instance with tools bound, following LangChain pattern."""
        new = AnthropicBackend(
            base_url=self._base_url,
            api_key=self._api_key,
            model_name=self._model_name,
            timeout=self._timeout,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            max_retries=self._max_retries,
        )
        new._tools = list(tools)
        return new

    def invoke(self, messages: list[BaseMessage]) -> AIMessage:
        url = f"{self._base_url}/v1/messages"
        payload = self._build_payload(messages)
        headers = {
            "x-api-key": self._api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(1, self._max_retries + 1):
            try:
                resp = httpx.post(url, json=payload, headers=headers, timeout=self._timeout)
                resp.raise_for_status()
                break
            except httpx.TimeoutException as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise RuntimeError(f"Anthropic backend timed out after {self._timeout}s") from exc
            except httpx.TransportError as exc:
                last_error = exc
                if attempt >= self._max_retries:
                    raise RuntimeError(f"Anthropic backend transport error: {exc}") from exc
            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status not in {408, 409, 429, 500, 502, 503, 504, 529} or attempt >= self._max_retries:
                    raise RuntimeError(
                        f"Anthropic backend error {status}: {exc.response.text[:1000]}"
                    ) from exc
                last_error = exc
            time.sleep(min(2 ** (attempt - 1), 8))
        else:
            raise RuntimeError(f"Anthropic backend failed: {last_error}")

        data = resp.json()
        return self._build_response(data)

    def stream(self, messages: list[BaseMessage]):
        result = self.invoke(messages)
        yield result

    def _build_response(self, data: dict) -> AIMessage:
        """Build AIMessage from Anthropic API response, handling tool_use blocks."""
        text_parts = []
        tool_calls = []

        for item in data.get("content", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "text":
                text_parts.append(item.get("text", ""))
            elif item.get("type") == "tool_use":
                tool_calls.append({
                    "name": item.get("name", ""),
                    "args": item.get("input", {}),
                    "id": item.get("id", str(uuid.uuid4())),
                    "type": "tool_call",
                })

        content = "".join(text_parts).strip()
        msg = AIMessage(content=content)
        if tool_calls:
            msg.tool_calls = tool_calls
            msg.additional_kwargs = {"tool_calls": tool_calls}
        return msg

    def _build_payload(self, messages: list[BaseMessage]) -> dict:
        system_parts = []
        formatted = []

        # Collect consecutive tool messages and merge into one user message
        pending_tool_results = []

        def _flush_tool_results():
            """Emit accumulated tool results as a single user message."""
            if not pending_tool_results:
                return
            formatted.append({
                "role": "user",
                "content": [dict(r) for r in pending_tool_results],
            })
            pending_tool_results.clear()

        for msg in messages:
            if isinstance(msg, SystemMessage) or msg.type == "system":
                system_parts.append(str(msg.content))
                continue

            if msg.type == "tool" or isinstance(msg, ToolMessage):
                # Accumulate tool result blocks — flush them together
                # as a single user message (Anthropic API requirement:
                # all tool_results must follow the tool_use immediately)
                tool_use_id = getattr(msg, "tool_call_id", "")
                content = str(msg.content) if msg.content else ""
                pending_tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": tool_use_id,
                    "content": content,
                })
                continue

            # Non-tool message: flush any pending tool results first
            _flush_tool_results()

            if msg.type == "ai":
                # AIMessage may contain tool_calls — format as tool_use blocks
                content_blocks = []
                text = str(msg.content) if msg.content else ""
                if text.strip():
                    content_blocks.append({"type": "text", "text": text})
                tool_calls = getattr(msg, "tool_calls", None) or []
                for tc in tool_calls:
                    content_blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id", str(uuid.uuid4())),
                        "name": tc.get("name", ""),
                        "input": tc.get("args", {}),
                    })
                if content_blocks:
                    formatted.append({"role": "assistant", "content": content_blocks})
                else:
                    formatted.append({"role": "assistant", "content": text or " "})

            elif isinstance(msg, HumanMessage):
                formatted.append({"role": "user", "content": str(msg.content)})

            else:
                # Fallback for any other message type
                formatted.append({"role": "user", "content": str(msg.content)})

        # Flush any trailing tool results
        _flush_tool_results()

        payload = {
            "model": self._model_name,
            "max_tokens": self._max_tokens,
            "temperature": self._temperature,
            "messages": formatted or [{"role": "user", "content": ""}],
        }

        # Add tools if bound
        if self._tools:
            payload["tools"] = self._tools_to_anthropic_format()

        if system_parts:
            payload["system"] = "\n\n".join(system_parts)

        return payload

    def _tools_to_anthropic_format(self) -> list[dict]:
        """Convert LangChain StructuredTool list to Anthropic tools format."""
        result = []
        for tool in self._tools:
            tool_def = {
                "name": tool.name,
                "description": tool.description or "",
            }
            if hasattr(tool, "args_schema") and tool.args_schema:
                try:
                    tool_def["input_schema"] = tool.args_schema.schema()
                except Exception:
                    tool_def["input_schema"] = {"type": "object", "properties": {}}
            else:
                tool_def["input_schema"] = {"type": "object", "properties": {}}
            result.append(tool_def)
        return result


@dataclass(frozen=True)
class ClaudeModelProfile:
    """One Claude settings/model slot in the kernel-expert model pool."""

    name: str
    settings_path: str = ""
    model: str = ""

class ClaudeCodeBackend:
    """LLM backend that drives the Claude Code CLI as a mature code agent.

    Delegates the tool-calling loop to `claude -p` (non-interactive print mode),
    letting Claude Code's own agent loop handle Read/Write/Edit/Bash/Grep/Glob
    instead of lumen's self-built tool_calling_loop. Returns the final text
    result parsed from the CLI's JSON output.
    """

    def __init__(
        self,
        cli_command: str = "claude",
        cli_timeout: int = 600,
        model: str = "",
        permission_mode: str = "bypassPermissions",
        max_turns: int = 100,
        settings_file: str = "",
        semcode_mcp: dict | None = None,
        disable_skills: bool = False,
        model_pool: list[dict | str] | None = None,
        quota_preflight: bool = False,
        quota_preflight_timeout: int = 15,
        quota_cooldown_seconds: int = 300,
        setting_sources: str | list[str] | tuple[str, ...] | None = None,
    ):
        self._cli_command = cli_command
        self._cli_timeout = cli_timeout
        self._model = model
        self._permission_mode = permission_mode
        self._max_turns = max_turns
        self._settings_file = settings_file
        self._disable_skills = disable_skills
        self._setting_sources = self._normalize_setting_sources(setting_sources)
        pool = [model_pool] if isinstance(model_pool, dict) else model_pool
        self._model_pool_config = list(pool) if pool else None
        # Kernel Expert is the only production caller of this backend and it
        # must use an explicit project settings file. Never silently attach
        # the invoking developer's global Claude account/settings.
        self._settings_required = True
        self._quota_preflight = bool(quota_preflight)
        self._quota_preflight_timeout = max(1, int(quota_preflight_timeout))
        self._quota_cooldown_seconds = max(0, int(quota_cooldown_seconds))
        self._pool_lock = Lock()
        self._pool_cursor = 0
        self._unavailable_until: dict[str, float] = {}
        # Inline MCP server config for semcode, e.g.
        # {"command": "/path/to/semcode-mcp", "args": ["-d", "/path/to/.semcode.db"]}
        # When set, the backend writes a temp mcp config file in CLI format
        # and passes it via --mcp-config. The db path is machine-specific and
        # belongs in config.json (gitignored). The -d arg is what
        # makes the semcode tools actually register — without it semcode-mcp
        # searches the cwd, finds no index, and the agent reports
        # "find_function not in my tool list".
        self._semcode_mcp = semcode_mcp or {}

    @staticmethod
    def _normalize_setting_sources(
        setting_sources: str | list[str] | tuple[str, ...] | None,
    ) -> str:
        """Normalize Claude setting-source allow-list for the CLI.

        "project" is the only source used by the production kernel expert
        configuration. Keeping the value explicit prevents Claude from
        silently loading user/local settings and skills.
        """
        if setting_sources is None:
            return ""
        if isinstance(setting_sources, (list, tuple)):
            value = ",".join(str(item).strip() for item in setting_sources if str(item).strip())
        else:
            value = str(setting_sources).strip()
        if not value:
            return ""
        sources = [item.strip() for item in value.split(",") if item.strip()]
        invalid = [item for item in sources if item not in {"user", "project", "local"}]
        if invalid:
            raise ValueError(
                "Claude setting_sources contains unsupported source(s): "
                + ", ".join(invalid)
            )
        return ",".join(dict.fromkeys(sources))

    def _append_setting_sources(self, cmd: list[str]) -> None:
        if self._setting_sources:
            cmd.extend(["--setting-sources", self._setting_sources])

    def bind_tools(self, tools):
        """No-op: Claude Code has its own built-in tools. Returns self so the
        backend can be used where `llm.bind_tools(tools)` is called."""
        return self

    def _dump_cli_artifacts(
        self,
        *,
        tag: str,
        cmd: list[str],
        system_prompt: str,
        user_prompt: str,
        workdir: str,
    ) -> dict[str, str] | None:
        """Write the CLI command, prompts, and workdir to /tmp/lumen_outputs/.

        Returns a dict of artifact paths (or None when debug is off). The
        command is written two ways: cmd.sh is a shell-quoted, copy-paste-
        rerunnable form (with prompts replaced by file references so the line
        stays readable), and cmd.raw.txt is the raw argv list.
        """
        if not (_DEBUG_CLI or _STREAM_JSON):
            return None
        try:
            _DUMP_DIR.mkdir(parents=True, exist_ok=True)
        except OSError:
            return None

        ts = time.strftime("%Y%m%d_%H%M%S")
        prefix = f"claude_cli_{tag}_{ts}"

        system_path = _DUMP_DIR / f"{prefix}_prompt_system.txt"
        user_path = _DUMP_DIR / f"{prefix}_prompt_user.txt"
        cmd_raw_path = _DUMP_DIR / f"{prefix}_cmd.raw.txt"
        cmd_sh_path = _DUMP_DIR / f"{prefix}_cmd.sh"
        live_log_path = _DUMP_DIR / f"{prefix}_live.log"
        # tools_stats.json only meaningful in stream-json mode (events
        # are emitted per tool call). Kept even in plain debug mode for
        # path stability, but stays empty there.
        tools_stats_path = _DUMP_DIR / f"{prefix}_tools_stats.json"

        try:
            system_path.write_text(system_prompt, encoding="utf-8")
            user_path.write_text(user_prompt, encoding="utf-8")
            cmd_raw_path.write_text(
                "\n".join(cmd) + "\n", encoding="utf-8"
            )

            # Rebuild a rerunnable shell command. Long prompts are replaced
            # with $(cat file) references so the line stays manageable; the
            # caller can rerun cmd.sh directly. We build the shell line
            # manually rather than shlex.quote-ing the whole list, because
            # the $(cat ...) expansion must stay unquoted for the shell to
            # splice in the file contents as a single argv.
            readable_parts: list[str] = []
            i = 0
            args = cmd
            while i < len(args):
                arg = args[i]
                if arg == "-p" and i + 1 < len(args):
                    readable_parts.append(shlex.quote(arg))
                    readable_parts.append(f"\"$(cat {str(user_path)})\"")
                    i += 2
                    continue
                if arg == "--system-prompt" and i + 1 < len(args):
                    readable_parts.append(shlex.quote(arg))
                    readable_parts.append(f"\"$(cat {str(system_path)})\"")
                    i += 2
                    continue
                readable_parts.append(shlex.quote(arg))
                i += 1

            shell_line = " ".join(readable_parts)
            header = (
                f"# Claude Code CLI invocation (tag={tag})\n"
                f"# Generated {ts}\n"
                f"# workdir: {workdir or '(inherit)'}\n"
                f"# system prompt: {system_path}\n"
                f"# user prompt:   {user_path}\n"
                f"# raw argv:      {cmd_raw_path}\n"
                f"# live log:      {live_log_path}\n"
                f"# tools stats:   {tools_stats_path}\n"
                f"# Run with:\n"
            )
            cwd_prefix = f"cd {shlex.quote(workdir)} && " if workdir else ""
            cmd_sh_path.write_text(
                header + cwd_prefix + shell_line + "\n",
                encoding="utf-8",
            )
            cmd_sh_path.chmod(0o755)
        except OSError:
            pass

        return {
            "system_prompt": str(system_path),
            "user_prompt": str(user_path),
            "cmd_raw": str(cmd_raw_path),
            "cmd_sh": str(cmd_sh_path),
            "live_log": str(live_log_path),
            "tools_stats": str(tools_stats_path),
        }

    def _settings_candidates(self) -> list[str]:
        """Return ordered Claude settings files without exposing credentials.

        ``settings_file`` accepts either one path or a comma/``os.pathsep``
        separated priority list.  When it is omitted, the maintenance
        workflow does not discover any global account. Missing files are
        ignored so the same config remains portable across hosts.
        """
        configured = self._settings_file
        if not configured:
            configured = os.environ.get("LUMEN_CLAUDE_SETTINGS_PRIORITY", "")
        if not configured:
            configured = os.environ.get("CLAUDE_SETTINGS", "")

        if isinstance(configured, (list, tuple)):
            raw_paths = [str(item) for item in configured]
        else:
            raw_paths = re.split(r"[,:\n]" if os.pathsep == ":" else r"[,;\n]", str(configured))

        candidates: list[str] = []
        seen: set[str] = set()
        for raw_path in raw_paths:
            path = os.path.expanduser(os.path.expandvars(raw_path.strip()))
            if not path or path in seen or not os.path.isfile(path):
                continue
            seen.add(path)
            candidates.append(path)
        return candidates

    def _model_profiles(self) -> list[ClaudeModelProfile]:
        """Build the configured model pool, filtering missing settings files."""
        configured = self._model_pool_config
        if isinstance(configured, dict):
            configured = [configured]

        profiles: list[ClaudeModelProfile] = []
        if configured:
            for index, item in enumerate(configured):
                if isinstance(item, str):
                    raw_path = item
                    name = ""
                    model = self._model
                elif isinstance(item, dict):
                    raw_path = (
                        item.get("settings_file")
                        or item.get("settings_path")
                        or item.get("path")
                        or ""
                    )
                    name = str(item.get("name") or "")
                    model = str(item.get("model") or item.get("model_name") or self._model)
                else:
                    continue
                settings_path = os.path.expanduser(os.path.expandvars(str(raw_path).strip()))
                if not settings_path or not os.path.isfile(settings_path):
                    continue
                if not name:
                    name = Path(settings_path).name if settings_path else f"profile-{index + 1}"
                profiles.append(ClaudeModelProfile(name=name, settings_path=settings_path, model=model))

        # Backward compatibility: an ordered settings_file list is itself a
        # model pool. Every entry is still explicit; no global account is
        # synthesized when the list is empty.
        if not profiles:
            profiles = [
                ClaudeModelProfile(
                    name=Path(path).name,
                    settings_path=path,
                    model=self._model,
                )
                for path in self._settings_candidates()
            ]
        if not profiles:
            raise RuntimeError(
                "Claude Code settings file is missing or unreadable; "
                "refusing to use implicit global settings"
            )
        return profiles

    @staticmethod
    def _profile_key(profile: ClaudeModelProfile) -> str:
        """Return a non-secret identity for cooldown bookkeeping."""
        return f"{profile.name}\x1f{profile.settings_path}"

    def _ordered_model_profiles(self) -> list[ClaudeModelProfile]:
        """Return profiles in round-robin order for the next analysis."""
        profiles = self._model_profiles()
        with self._pool_lock:
            start = self._pool_cursor % len(profiles)
            self._pool_cursor = (start + 1) % len(profiles)
        return profiles[start:] + profiles[:start]

    def _profile_in_cooldown(self, profile: ClaudeModelProfile) -> bool:
        key = self._profile_key(profile)
        now = time.monotonic()
        with self._pool_lock:
            until = self._unavailable_until.get(key, 0.0)
            if until and until <= now:
                self._unavailable_until.pop(key, None)
                return False
            return until > now

    def _mark_profile_unavailable(self, profile: ClaudeModelProfile) -> None:
        if self._quota_cooldown_seconds <= 0:
            return
        with self._pool_lock:
            self._unavailable_until[self._profile_key(profile)] = (
                time.monotonic() + self._quota_cooldown_seconds
            )

    def _quota_preflight_profile(
        self,
        profile: ClaudeModelProfile,
        *,
        workdir: str = "",
    ) -> bool:
        """Run a bounded, tool-free CLI probe before assigning a profile.

        The probe deliberately uses one turn and no project tools. A failed
        probe is treated as unavailable for this cooldown window, so a
        broken/empty account cannot consume the full kernel analysis budget.
        Probe output is never logged because provider responses can contain
        credential or account details.
        """
        if not self._quota_preflight:
            return True
        if self._profile_in_cooldown(profile):
            return False

        command = shlex.split(self._cli_command)
        if not command:
            self._mark_profile_unavailable(profile)
            return False
        cmd = command + [
            "-p",
            "Quota preflight only. Reply with OK. Do not inspect files or use tools.",
            "--output-format",
            "json",
            "--no-session-persistence",
            "--max-turns",
            "1",
            "--permission-mode",
            self._permission_mode,
            "--tools",
            "",
            "--disable-slash-commands",
        ]
        selected_model = profile.model or self._model
        if selected_model:
            cmd.extend(["--model", selected_model])
        if profile.settings_path:
            cmd.extend(["--settings", profile.settings_path])
        self._append_setting_sources(cmd)
        try:
            result = subprocess.run(
                cmd,
                cwd=workdir or None,
                capture_output=True,
                text=True,
                timeout=self._quota_preflight_timeout,
            )
        except (OSError, subprocess.TimeoutExpired):
            self._mark_profile_unavailable(profile)
            return False

        failed = result.returncode != 0
        if not failed and result.stdout.strip():
            try:
                failed = bool(json.loads(result.stdout).get("is_error"))
            except (TypeError, ValueError):
                failed = True
        elif not result.stdout.strip():
            failed = True
        if failed:
            self._mark_profile_unavailable(profile)
            return False
        return True

    @staticmethod
    def _is_quota_exhaustion(error: BaseException) -> bool:
        """Recognize provider-credit failures that are safe to fail over."""
        text = str(error).lower()
        markers = (
            "429", "402", "rate limit", "rate_limit", "too many requests",
            "quota", "insufficient_quota", "out of credits", "credit balance",
            "payment required", "billing", "额度", "余额",
        )
        return any(marker in text for marker in markers)

    def invoke(self, messages: list[BaseMessage], *, workdir: str = "", add_dirs: list[str] | None = None) -> AIMessage:
        """Assign one available model in round-robin order.

        A preflight probe can skip exhausted accounts before the expensive
        analysis. If an account still reports an explicit quota error during
        analysis, the next profile is tried, preserving GLM -> default
        settings failover semantics.
        """
        profiles = self._ordered_model_profiles()
        last_quota_error: RuntimeError | None = None
        for profile in profiles:
            if not self._quota_preflight_profile(profile, workdir=workdir):
                continue
            try:
                return self._invoke_profile(
                    messages,
                    profile,
                    workdir=workdir,
                    add_dirs=add_dirs,
                )
            except RuntimeError as exc:
                if not self._is_quota_exhaustion(exc):
                    raise
                self._mark_profile_unavailable(profile)
                last_quota_error = exc

        if last_quota_error is not None:
            raise last_quota_error
        raise RuntimeError(
            "Claude Code model pool exhausted: no profile passed quota preflight"
        )

    def _invoke_profile(
        self,
        messages: list[BaseMessage],
        profile: ClaudeModelProfile,
        *,
        workdir: str = "",
        add_dirs: list[str] | None = None,
    ) -> AIMessage:
        kwargs = {
            "workdir": workdir,
            "add_dirs": add_dirs,
            "settings_path": profile.settings_path,
        }
        # Keep compatibility with test doubles and callers that override
        # _invoke_once with the historical keyword-only signature.
        if profile.model and profile.model != self._model:
            kwargs["model"] = profile.model
        return self._invoke_once(messages, **kwargs)

    def _invoke_once(
        self,
        messages: list[BaseMessage],
        *,
        workdir: str = "",
        add_dirs: list[str] | None = None,
        settings_path: str = "",
        model: str = "",
    ) -> AIMessage:
        system_parts: list[str] = []
        user_parts: list[str] = []
        for msg in messages:
            if isinstance(msg, SystemMessage) or msg.type == "system":
                system_parts.append(str(msg.content))
            elif isinstance(msg, HumanMessage) or msg.type == "human":
                user_parts.append(str(msg.content))
            else:
                user_parts.append(str(msg.content))

        user_prompt = "\n\n".join(user_parts) or " "
        system_prompt = "\n\n".join(system_parts)

        cmd: list[str] = [
            self._cli_command,
            "-p", user_prompt,
            "--output-format", "stream-json" if _STREAM_JSON else "json",
            "--permission-mode", self._permission_mode,
            "--max-turns", str(self._max_turns),
        ]
        selected_model = model or self._model
        if selected_model:
            cmd.extend(["--model", selected_model])
        if _STREAM_JSON:
            # stream-json requires --verbose per CLI spec
            cmd.append("--verbose")
        if system_prompt.strip():
            cmd.extend(["--system-prompt", system_prompt])
        if add_dirs:
            for d in add_dirs:
                cmd.extend(["--add-dir", d])
        if settings_path:
            cmd.extend(["--settings", settings_path])
        self._append_setting_sources(cmd)
        if self._semcode_mcp:
            # Generate a temp mcp config in CLI format from the inline
            # semcode_mcp dict. This is what makes the semcode tools
            # actually register — without `-d`, semcode-mcp searches the
            # cwd for .semcode.db, finds nothing in outputs/, and the MCP
            # server starts but exposes no tools (init event shows ~26
            # tools instead of ~42, agent then says "find_function not in
            # my tool list").
            mcp_path = self._write_semcode_mcp_config()
            if mcp_path and os.path.exists(mcp_path):
                cmd.extend(["--mcp-config", mcp_path])

        # kernel_expert's prompt has no /<skill> slash commands, so the 57
        # skills auto-loaded from ~/.claude/skills/ waste ~23k input tokens
        # per invoke (Q2 experiment showed ~24% total token savings and 20%
        # fewer turns to converge, with no regression). Config-gated so other
        # claude_code agents can opt in later if they share the same property.
        if self._disable_skills:
            cmd.append("--disable-slash-commands")

        # Debug dump: write the full command + prompts to disk so the call
        # can be inspected or rerun. Tag includes a uuid suffix in case
        # multiple invokes happen within the same second.
        # _STREAM_JSON implies debug (it only makes sense when capturing logs).
        dump_enabled = _DEBUG_CLI or _STREAM_JSON
        tag = f"kernel_expert_{uuid.uuid4().hex[:6]}"
        dump = self._dump_cli_artifacts(
            tag=tag,
            cmd=cmd,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            workdir=workdir,
        ) if dump_enabled else None

        try:
            # When debug is on, stream stdout/stderr line-by-line to a live
            # log so `tail -f` can watch progress. Otherwise use the original
            # capture-once subprocess.run to keep existing behavior.
            if dump:
                stdout, stderr, returncode = self._run_with_live_logging(
                    cmd,
                    workdir=workdir or None,
                    live_log_path=dump["live_log"],
                    tools_stats_path=dump.get("tools_stats") if _STREAM_JSON else None,
                    stream_json=_STREAM_JSON,
                    timeout=self._cli_timeout,
                )
            else:
                result = subprocess.run(
                    cmd,
                    cwd=workdir or None,
                    capture_output=True,
                    text=True,
                    timeout=self._cli_timeout,
                )
                stdout, stderr, returncode = result.stdout, result.stderr, result.returncode
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Claude Code timed out after {self._cli_timeout}s") from exc
        except FileNotFoundError as exc:
            if exc.filename and exc.filename != self._cli_command:
                raise RuntimeError(
                    f"Claude Code workdir does not exist: {exc.filename}"
                ) from exc
            raise RuntimeError(f"Claude Code CLI not found: {self._cli_command}") from exc

        if returncode != 0:
            # Detect CLI startup failures (MCP config missing, CLI crash
            # before agent loop). These must NOT be swallowed into a fallback
            # stub by upstream — they indicate a broken environment, not an
            # analysis failure. Tag the message so kernel_expert_node can
            # route to a blocked contract instead of fabricating output.
            stderr_str = stderr.strip()[:500]
            is_startup_failure = bool(stderr_str) and (
                "MCP config" in stderr_str
                or "Invalid MCP" in stderr_str
                or "mcp" in stderr_str.lower()
            )
            # Also detect max_turns exhaustion: CLI exits 1 with a JSON
            # result on stdout (not stderr) containing
            # "errors":["Reached maximum number of turns (N)"]. The stderr
            # is empty so the generic startup-failure heuristic misses it.
            # Tag it so kernel_expert_node routes to a blocked contract
            # instead of fabricating a stale fallback.
            is_max_turns = "Reached maximum number of turns" in stdout
            if is_startup_failure:
                prefix = "[cli_startup_failure] "
            elif is_max_turns:
                prefix = "[cli_max_turns] "
            else:
                prefix = ""
            detail = stderr_str or (stdout[:500] if is_max_turns else "")
            raise RuntimeError(
                f"{prefix}Claude Code failed (exit {returncode}): {detail}"
            )

        stdout = stdout.strip()
        if not stdout:
            raise RuntimeError("Claude Code returned empty stdout")

        if _STREAM_JSON:
            # stdout is JSONL; find the final `result` event and parse it.
            # Earlier events (system/assistant/tool_use/tool_result) were
            # already rendered to the live log line-by-line.
            result_obj: dict | None = None
            parse_errors: list[str] = []
            for line in stdout.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    evt = json.loads(line)
                except json.JSONDecodeError as exc:
                    parse_errors.append(f"{line[:120]}: {exc}")
                    continue
                if isinstance(evt, dict) and evt.get("type") == "result":
                    result_obj = evt
            if result_obj is None:
                raise RuntimeError(
                    f"stream-json output had no result event. "
                    f"parse_errors={parse_errors[:3]}, tail={stdout[-300:]}"
                )
            data = result_obj
        else:
            try:
                data = json.loads(stdout)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    f"Claude Code returned non-JSON output: {stdout[:500]}"
                ) from exc

        if data.get("is_error"):
            raise RuntimeError(
                f"Claude Code reported error: {data.get('result', '')[:500]}"
            )

        content = data.get("result", "") or ""
        msg = AIMessage(content=content)
        # Detect max_turns exhaustion: CLI returns is_error=False with
        # num_turns == max_turns when the agent loop hit the turn budget
        # without a natural stop. The result text is partial/truncated.
        # Mark it so upstream can route to retry or warn — without this,
        # a truncated analysis silently flows to fallback stubs.
        num_turns = data.get("num_turns") or 0
        if num_turns and num_turns >= self._max_turns and not data.get("is_error"):
            msg.additional_kwargs = {
                "exhausted_max_turns": True,
                "num_turns": num_turns,
                "max_turns": self._max_turns,
            }
        return msg

    def _write_semcode_mcp_config(self) -> str:
        """Write a temp mcp config in CLI format from the inline semcode_mcp dict.

        The dict comes from config.json (agent-level semcode_mcp
        field), e.g. {"command": "/path/to/semcode-mcp", "args": ["-d", "/path/to/.semcode.db"]}.
        Renders to {"mcpServers": {"semcode": {...}}} and writes a temp file
        that --mcp-config can consume. Returns the temp path, or "" if the
        dict lacks a command or the command binary doesn't exist.
        """
        if not self._semcode_mcp or not self._semcode_mcp.get("command"):
            return ""
        command = os.path.expanduser(self._semcode_mcp["command"])
        if not os.path.exists(command):
            return ""
        server_cfg: dict = {"command": command}
        args = self._semcode_mcp.get("args")
        if args:
            server_cfg["args"] = [os.path.expanduser(a) for a in args]
        config = {"mcpServers": {"semcode": server_cfg}}
        tmp_dir = Path(tempfile.gettempdir())
        tmp_path = tmp_dir / f"lumen_semcode_mcp_{uuid.uuid4().hex[:8]}.json"
        tmp_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        return str(tmp_path)

    def _run_with_live_logging(
        self,
        cmd: list[str],
        *,
        workdir: str | None,
        live_log_path: str,
        tools_stats_path: str | None = None,
        stream_json: bool = False,
        timeout: int | None = None,
    ) -> tuple[str, str, int]:
        """Popen the CLI, tee stdout/stderr to live_log_path line-by-line.

        Returns (stdout, stderr, returncode) with full captured text so the
        caller can parse JSON exactly as before. stderr is also captured
        fully even though we stream it, because non-zero exits need the
        trimmed message for the RuntimeError.

        When `timeout` is set, the subprocess is killed when the deadline
        passes and subprocess.TimeoutExpired is raised so the caller can
        surface the same "Claude Code timed out after Ns" error as the
        non-debug path. Without this, the live-logging path would block
        forever on a hung CLI (proc.wait() with no timeout).

        When stream_json is True, stdout is JSONL (one event per line). Each
        line is formatted via _format_stream_event and written to the live
        log so `tail -f` shows tool_use/tool_result/assistant events as they
        happen. The full stdout buffer is reconstructed so the caller can
        still parse the final `result` event: we return the raw JSONL text
        and let invoke() handle extraction.

        When stream_json is True and tools_stats_path is set, each event is
        also fed to a _ToolCallTracker that pairs tool_use with tool_result
        by id; on exit the per-tool durations are written to
        tools_stats_path as JSON and a summary table is appended to the
        live log. Use this to spot slow tools (e.g. Grep over a huge tree)
        and target optimization (e.g. add a code search index).
        """
        live = open(live_log_path, "w", encoding="utf-8")
        tracker = _ToolCallTracker() if (stream_json and tools_stats_path) else None

        def _tee_raw(src, prefix: str, sink: list[str]) -> None:
            """Raw line tee — used for stderr and non-stream-json stdout."""
            if src is None:
                return
            for line in iter(src.readline, ""):
                if not line:
                    break
                sink.append(line)
                live.write(f"{prefix}{line}")
                live.flush()
            src.close()

        def _tee_stream_json(src, sink: list[str]) -> None:
            """JSONL tee — parse each stdout line as a stream event."""
            if src is None:
                return
            for line in iter(src.readline, ""):
                if not line:
                    break
                sink.append(line)
                # Keep the raw line in the buffer so the caller can find the
                # final result event; write the human-readable form to live.
                formatted = _format_stream_event(line)
                if formatted:
                    live.write(formatted + "\n")
                else:
                    live.write("(empty line)\n")
                live.flush()
                # Feed the parsed event to the tracker so it can pair
                # tool_use with tool_result and time the call.
                if tracker is not None:
                    try:
                        evt = json.loads(line)
                    except json.JSONDecodeError:
                        evt = {}
                    if isinstance(evt, dict):
                        tracker.observe(evt)
            src.close()

        try:
            live.write(
                f"=== Claude Code CLI live log ===\n"
                f"cmd: {' '.join(shlex.quote(a) for a in cmd)}\n"
                f"cwd: {workdir or '(inherit)'}\n"
                f"stream_json: {stream_json}\n"
                f"started: {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"--- stdout ---\n"
            )
            live.flush()

            proc = subprocess.Popen(
                cmd,
                cwd=workdir,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            stdout_parts: list[str] = []
            stderr_parts: list[str] = []

            # Stream stdout first (the JSON output is the bulk of it); read
            # stderr in parallel so neither pipe blocks the other.
            import threading

            stderr_thread = threading.Thread(
                target=_tee_raw,
                args=(proc.stderr, "[stderr] ", stderr_parts),
            )
            stderr_thread.start()
            if stream_json:
                _tee_stream_json(proc.stdout, stdout_parts)
            else:
                _tee_raw(proc.stdout, "", stdout_parts)
            stderr_thread.join()

            # Enforce the deadline: proc.wait() with no timeout would block
            # forever if the CLI hung. Kill the process tree so partial
            # stdout/stderr still gets returned for debugging.
            try:
                returncode = proc.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                proc.kill()
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                live.write(f"--- timed out after {timeout}s ---\n")
                live.flush()
                raise

            live.write(f"--- end (exit={returncode}) ---\n")

            # Append per-tool timing summary so `tail -f` viewers and
            # post-mortem readers see the slow tools at a glance.
            if tracker is not None and tools_stats_path:
                summary = tracker.summary()
                try:
                    Path(tools_stats_path).write_text(
                        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8",
                    )
                except OSError:
                    pass
                live.write("\n--- tool call timing ---\n")
                live.write(tracker.render_summary_table() + "\n")

            live.flush()

            return "".join(stdout_parts), "".join(stderr_parts), returncode
        finally:
            live.close()

    def stream(self, messages: list[BaseMessage]):
        """Non-streaming fallback: invoke and yield single chunk."""
        yield self.invoke(messages)


class CodexBackend:
    """Run the Kernel Expert through ``codex exec``.

    The backend deliberately isolates Codex from the invoking developer's
    HOME/configuration. Authentication is provisioned into a project-local,
    ignored runtime home by ``deploy.sh``; only repository skills under
    ``.agents/skills`` are in scope. Semcode is injected as a required MCP
    server so source-analysis failures stop the workflow instead of silently
    falling back to text search.
    """

    def __init__(
        self,
        cli_command: str = "codex",
        cli_timeout: int = 600,
        model: str = "",
        reasoning_effort: str = "",
        service_tier: str = "",
        sandbox_mode: str = "workspace-write",
        approval_policy: str = "never",
        runtime_home: str = "",
        project_root: str = "",
        project_skills_dir: str = "",
        semcode_mcp: dict | None = None,
        ephemeral: bool = True,
        ignore_user_config: bool = True,
        ignore_rules: bool = True,
    ):
        self._cli_command = cli_command
        self._cli_timeout = int(cli_timeout)
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._service_tier = service_tier
        self._sandbox_mode = sandbox_mode
        self._approval_policy = approval_policy
        self._runtime_home = runtime_home
        self._project_root = project_root
        self._project_skills_dir = project_skills_dir
        self._semcode_mcp = semcode_mcp or {}
        self._ephemeral = bool(ephemeral)
        self._ignore_user_config = bool(ignore_user_config)
        self._ignore_rules = bool(ignore_rules)

    def bind_tools(self, tools):
        """Codex owns its tool loop; LangChain tools are not rebound."""
        return self

    @staticmethod
    def _expanded_path(value: str, *, base: Path | None = None) -> Path:
        path = Path(os.path.expandvars(os.path.expanduser(str(value))))
        if not path.is_absolute() and base is not None:
            path = base / path
        return path.resolve()

    def _runtime_paths(self) -> tuple[Path, Path, Path, Path]:
        if not self._project_root:
            raise RuntimeError("Codex project_root is required; refusing implicit cwd discovery")
        project_root = self._expanded_path(self._project_root)
        if not project_root.is_dir():
            raise RuntimeError(f"Codex project_root does not exist: {project_root}")

        if not self._runtime_home:
            raise RuntimeError("Codex runtime_home is required for project-isolated execution")
        runtime_home = self._expanded_path(self._runtime_home, base=project_root)
        codex_home = runtime_home / ".codex"
        auth_file = codex_home / "auth.json"
        if not auth_file.is_file() and not os.environ.get("CODEX_API_KEY", "").strip():
            raise RuntimeError(
                "Codex isolated authentication is missing; run deploy.sh after `codex login` "
                f"or provide CODEX_API_KEY for this invocation: {auth_file}"
            )
        if auth_file.is_file() and auth_file.stat().st_mode & 0o077:
            raise RuntimeError(f"Codex auth file must have mode 600: {auth_file}")

        if not self._project_skills_dir:
            raise RuntimeError("Codex project_skills_dir is required")
        skills_dir = self._expanded_path(self._project_skills_dir, base=project_root)
        if not skills_dir.is_dir() or not any(skills_dir.glob("*/SKILL.md")):
            raise RuntimeError(
                f"Codex project skills are missing under {skills_dir}; run deploy.sh"
            )
        try:
            skills_dir.relative_to(project_root)
        except ValueError as exc:
            raise RuntimeError(
                f"Codex project_skills_dir must stay inside project_root: {skills_dir}"
            ) from exc
        return project_root, runtime_home, codex_home, skills_dir

    @staticmethod
    def _ensure_project_skills_link(workdir: Path, project_root: Path) -> None:
        """Materialize project skills inside the Codex workdir.

        Codex's ``workspace-write`` sandbox treats a symlink from the session
        directory to the repository as an out-of-workspace boundary.  That
        prevented both skill discovery and writes to a userspace diagnostic
        source.  Copy only the repository ``.agents/skills`` tree into the
        session instead; the target kernel checkout is supplied separately as
        a bounded additional directory for source inspection.
        """
        source = (project_root / ".agents").resolve()
        if not source.is_dir():
            raise RuntimeError(f"Codex project skills root is missing: {source}")
        link = workdir / ".agents"
        if link.is_symlink():
            link.unlink()
        if link.exists():
            if not link.is_dir():
                raise RuntimeError(f"Codex workdir .agents is not a directory: {link}")
            if not (link / "skills").is_dir():
                raise RuntimeError(f"Codex workdir .agents/skills is missing: {link}")
            return
        try:
            shutil.copytree(source / "skills", link / "skills", symlinks=False)
        except OSError as exc:
            raise RuntimeError(
                f"Codex could not materialize project skills in workdir: {link}"
            ) from exc

    def _resolve_mcp_command(self, project_root: Path) -> tuple[str, list[str]]:
        command_value = str(self._semcode_mcp.get("command") or "").strip()
        if not command_value:
            raise RuntimeError("Codex Semcode MCP command is required; no source fallback is allowed")
        expanded = os.path.expandvars(os.path.expanduser(command_value))
        if os.sep in expanded or (os.altsep and os.altsep in expanded):
            command_path = self._expanded_path(expanded, base=project_root)
            if not command_path.is_file() or not os.access(command_path, os.X_OK):
                raise RuntimeError(f"Codex Semcode MCP binary is missing or not executable: {command_path}")
            command = str(command_path)
        else:
            command = shutil.which(expanded) or ""
            if not command:
                raise RuntimeError(f"Codex Semcode MCP command not found on PATH: {expanded}")
        args = [os.path.expandvars(os.path.expanduser(str(item))) for item in self._semcode_mcp.get("args", [])]
        return command, args

    def _interactive_mcp_disabled(self) -> bool:
        """Return whether deterministic evidence is intentionally authoritative."""
        return bool(self._semcode_mcp.get("disabled"))

    @staticmethod
    def _parse_jsonl(stdout: str) -> str:
        final_text = ""
        errors: list[str] = []
        for raw_line in stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            if event.get("type") == "item.completed":
                item = event.get("item") or {}
                if item.get("type") == "agent_message" and item.get("text"):
                    final_text = str(item["text"])
            elif event.get("type") in {"error", "turn.failed"}:
                detail = event.get("message") or event.get("error") or event
                errors.append(str(detail))
        if not final_text and errors:
            raise RuntimeError(f"Codex reported an error: {errors[-1][:500]}")
        return final_text

    def _build_command(self, *, workdir: Path, project_root: Path, add_dirs: list[str] | None = None) -> list[str]:
        command = shlex.split(self._cli_command)
        if not command:
            raise RuntimeError("Codex CLI command is empty")
        mcp_disabled = self._interactive_mcp_disabled()
        if not mcp_disabled:
            mcp_command, mcp_args = self._resolve_mcp_command(project_root)
        cmd = command + [
            "--ask-for-approval", self._approval_policy,
            "exec",
            "--json",
            "--color", "never",
            "--skip-git-repo-check",
            "--sandbox", self._sandbox_mode,
            "-C", str(workdir),
        ]
        if self._ephemeral:
            cmd.append("--ephemeral")
        if self._ignore_user_config:
            cmd.append("--ignore-user-config")
        if self._ignore_rules:
            cmd.append("--ignore-rules")
        if self._model:
            cmd.extend(["--model", self._model])
        if self._reasoning_effort:
            cmd.extend(["-c", f"model_reasoning_effort={json.dumps(self._reasoning_effort)}"])
        if self._service_tier:
            cmd.extend(["-c", f"service_tier={json.dumps(self._service_tier)}"])
        cmd.extend(["--disable", "plugins", "--disable", "apps", "--disable", "multi_agent"])
        for directory in add_dirs or []:
            bounded = self._expanded_path(directory)
            if not bounded.is_dir():
                raise RuntimeError(f"Codex additional directory does not exist: {bounded}")
            cmd.extend(["--add-dir", str(bounded)])
        if not mcp_disabled:
            cmd.extend([
                "-c", f"mcp_servers.semcode.command={json.dumps(mcp_command)}",
                "-c", f"mcp_servers.semcode.args={json.dumps(mcp_args)}",
                "-c", f"mcp_servers.semcode.cwd={json.dumps(str(project_root))}",
                "-c", "mcp_servers.semcode.required=true",
            ])
        cmd.append("-")
        return cmd

    def invoke(
        self,
        messages: list[BaseMessage],
        *,
        workdir: str = "",
        add_dirs: list[str] | None = None,
    ) -> AIMessage:
        project_root, runtime_home, codex_home, _skills_dir = self._runtime_paths()
        if not workdir:
            raise RuntimeError("Codex workdir is required for Kernel Expert output isolation")
        workdir_path = self._expanded_path(workdir)
        if not workdir_path.is_dir():
            raise RuntimeError(f"Codex workdir does not exist: {workdir_path}")
        try:
            workdir_path.relative_to(project_root)
        except ValueError as exc:
            raise RuntimeError(
                "Codex workdir must stay inside project_root so repository skills are discoverable: "
                f"{workdir_path}"
            ) from exc
        self._ensure_project_skills_link(workdir_path, project_root)

        system_parts: list[str] = []
        user_parts: list[str] = []
        for message in messages:
            if isinstance(message, dict):
                role = str(message.get("role") or "user")
                content = str(message.get("content") or "")
            else:
                role = "system" if isinstance(message, SystemMessage) or message.type == "system" else "user"
                content = str(message.content)
            (system_parts if role == "system" else user_parts).append(content)

        prompt = (
            "# Lumen Kernel Expert instructions\n\n"
            + "\n\n".join(system_parts)
            + "\n\n# Runtime isolation\n\n"
            + "Use only repository skills discovered under the configured .agents/skills directory. "
              "Do not invoke user, admin, bundled-system, or plugin-provided skills. "
              "The target kernel source is read-only; write only inside the current session output directory.\n\n"
            + "# Case input\n\n"
            + ("\n\n".join(user_parts) or " ")
        )
        cmd = self._build_command(workdir=workdir_path, project_root=project_root, add_dirs=add_dirs)
        env = os.environ.copy()
        env["HOME"] = str(runtime_home)
        env["CODEX_HOME"] = str(codex_home)
        env["XDG_CONFIG_HOME"] = str(runtime_home / ".config")

        # Codex is a Node wrapper which launches a vendor binary.  A plain
        # subprocess.run(timeout=...) only kills the wrapper; the vendor child
        # can retain stdout/stderr and keep the workflow blocked indefinitely.
        # Own a process group so timeout cleanup is bounded and deterministic.
        try:
            process = subprocess.Popen(
                cmd,
                cwd=str(workdir_path),
                env=env,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=(os.name == "posix"),
            )
            handoff_ready = Event()
            stop_watchdog = Event()

            def watch_artifact_handoff() -> None:
                stable_signature = None
                stable_since = 0.0
                grace = _codex_artifact_grace_seconds()
                while not stop_watchdog.wait(1.0):
                    if getattr(process, "poll", lambda: None)() is not None:
                        return
                    signature = _codex_artifact_signature(workdir_path)
                    if signature is None:
                        stable_signature = None
                        stable_since = 0.0
                        continue
                    now = time.monotonic()
                    if signature != stable_signature:
                        stable_signature = signature
                        stable_since = now
                        continue
                    if now - stable_since < grace:
                        continue
                    # The same invocation has a complete, stable C-only handoff;
                    # stop only the stalled tail turn and let the normal sync and
                    # contract validators consume it.
                    handoff_ready.set()
                    try:
                        if os.name == "posix":
                            os.killpg(process.pid, signal.SIGTERM)
                        else:
                            process.terminate()
                    except (OSError, ProcessLookupError):
                        pass
                    if not stop_watchdog.wait(2.0):
                        try:
                            if os.name == "posix":
                                os.killpg(process.pid, signal.SIGKILL)
                            else:
                                process.kill()
                        except (OSError, ProcessLookupError):
                            pass
                    return

            watchdog = Thread(target=watch_artifact_handoff, daemon=True)
            watchdog.start()
            try:
                stdout, stderr = process.communicate(
                    input=prompt,
                    timeout=self._cli_timeout,
                )
            except subprocess.TimeoutExpired as exc:
                if os.name == "posix":
                    try:
                        os.killpg(process.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                else:
                    process.kill()
                stdout, stderr = process.communicate()
                raise RuntimeError(f"Codex timed out after {self._cli_timeout}s") from exc
            finally:
                stop_watchdog.set()
                watchdog.join(timeout=3.0)
        except FileNotFoundError as exc:
            raise RuntimeError(f"Codex CLI not found: {self._cli_command}") from exc

        if handoff_ready.is_set():
            try:
                (workdir_path / "codex_artifact_handoff.json").write_text(
                    json.dumps({
                        "status": "ready",
                        "reason": "stable KERNEL_CONTRACT.json and userspace C artifacts; stopped stalled final turn",
                        "contract": "KERNEL_CONTRACT.json",
                    }, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
            except OSError:
                pass
            contract_text = (workdir_path / "KERNEL_CONTRACT.json").read_text(encoding="utf-8")
            return AIMessage(content=f"KERNEL_CONTRACT:\n```json\n{contract_text}\n```")

        if process.returncode != 0:
            detail = (stderr or stdout or "").strip()[:1000]
            try:
                (workdir_path / "codex_cli_failure.log").write_text(
                    (stdout or "") + ("\n--- stderr ---\n" if stderr else "") + (stderr or ""),
                    encoding="utf-8",
                )
            except OSError:
                pass
            prefix = "[cli_startup_failure] " if "mcp" in detail.lower() else ""
            raise RuntimeError(f"{prefix}Codex failed (exit {process.returncode}): {detail}")
        content = self._parse_jsonl(stdout)
        if not content.strip():
            raise RuntimeError("Codex returned no final agent message")
        return AIMessage(content=content)

    def stream(self, messages: list[BaseMessage]):
        yield self.invoke(messages)


class OpenCodeBackend:
    """LLM backend that drives the OpenCode CLI as an agent loop.

    Mirrors ClaudeCodeBackend's contract: delegates the tool-calling loop
    to `opencode run --format json` so OpenCode's own agent handles
    Read/Write/Edit/Bash/Glob/Grep. Returns the final assistant text.

    Key differences from ClaudeCodeBackend:
      - System prompt is injected via a per-invoke agent markdown file at
        ~/.opencode/agents/<agent_name>.md (OpenCode has no --system-prompt
        flag). Rewritten every invoke because Lumen's system prompt embeds
        dynamic preflight context.
      - No --max-turns soft cap; cli_timeout is the only upper bound.
      - No --add-dir; only single --dir. When the caller passes add_dirs,
        the first entry is used as workdir (kernel source dir is the most
        useful one). If workdir is also set, workdir wins and add_dirs[0]
        is documented in the system prompt instead.
      - If JSONL text events are missing despite a successful run,
        ``opencode export <sessionID>`` serves as a secondary retrieval
        path (handles edge cases where the PTY output parsing is
        incomplete).
    """

    def __init__(
        self,
        cli_command: str = "opencode",
        cli_timeout: int = 3600,
        model: str = "",
        agent_name: str = "lumen_kernel_expert",
        permission_mode: str = "bypassPermissions",
        pure: bool = False,
        semcode_mcp: dict | None = None,
    ):
        self._cli_command = cli_command
        self._cli_timeout = cli_timeout
        self._model = model
        self._agent_name = agent_name
        self._permission_mode = permission_mode
        self._pure = pure
        self._semcode_mcp = semcode_mcp or {}

    def bind_tools(self, tools):
        """No-op: OpenCode has its own built-in tools. Returns self."""
        return self

    def _write_agent_file(self, system_prompt: str) -> str:
        """Write the agent markdown file with the given system prompt.

        OpenCode requires agents to be defined as files under
        ~/.opencode/agents/<name>.md with YAML frontmatter. The system
        prompt is the markdown body. Returns the agent name (without .md)
        so the caller can pass `--agent <name>` to the CLI.
        """
        agents_dir = Path.home() / ".opencode" / "agents"
        agents_dir.mkdir(parents=True, exist_ok=True)
        agent_path = agents_dir / f"{self._agent_name}.md"
        content = (
            "---\n"
            "description: Lumen kernel_expert agent (auto-generated by Lumen)\n"
            "mode: primary\n"
            "temperature: 0\n"
            "tools:\n"
            "  bash: true\n"
            "  read: true\n"
            "  edit: true\n"
            "  write: true\n"
            "  glob: true\n"
            "  grep: true\n"
            "---\n\n"
            f"{system_prompt}\n"
        )
        agent_path.write_text(content, encoding="utf-8")
        return self._agent_name

    def _extract_session_id(self, stdout: str) -> str | None:
        """Extract sessionID from the first ``step_start`` JSONL event."""
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(evt, dict) and evt.get("type") == "step_start":
                part = evt.get("part") or {}
                sid = part.get("sessionID") or evt.get("sessionID")
                if sid:
                    return sid
        return None

    def _export_session_text(self, session_id: str) -> str | None:
        """Run ``opencode export <sessionID>`` and extract assistant text."""
        try:
            export = subprocess.run(
                [self._cli_command, "export", session_id],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                timeout=30,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
            return None
        if export.returncode != 0:
            return None

        # Parse export JSON
        try:
            data = json.loads(export.stdout)
        except json.JSONDecodeError:
            return None

        # Messages are in order; collect text from the last assistant message
        messages = data.get("messages") or []
        text_parts: list[str] = []
        for msg in messages:
            info = msg.get("info") or {}
            if info.get("role") != "assistant":
                text_parts = []  # reset on non-assistant (start fresh for each assistant block)
                continue
            parts = msg.get("parts") or []
            for part in parts:
                if part.get("type") == "text":
                    txt = part.get("text", "")
                    if txt:
                        text_parts.append(txt)

        return "".join(text_parts) if text_parts else None

    @staticmethod
    def _build_oc_cmd(
        *,
        model: str,
        agent_name: str,
        target_dir: str,
        pure: bool,
        user_prompt: str,
    ) -> list[str]:
        """Build the opencode run argument list."""
        cmd = [
            "opencode", "run",
            "--format", "json",
            "--dangerously-skip-permissions",
            "--agent", agent_name,
        ]
        if model:
            cmd.extend(["-m", model])
        if target_dir:
            cmd.extend(["--dir", target_dir])
        if pure:
            cmd.append("--pure")
        cmd.append(user_prompt)
        return cmd

    def invoke(
        self,
        messages: list[BaseMessage],
        *,
        workdir: str = "",
        add_dirs: list[str] | None = None,
    ) -> AIMessage:
        system_parts: list[str] = []
        user_parts: list[str] = []
        for msg in messages:
            if isinstance(msg, SystemMessage) or msg.type == "system":
                system_parts.append(str(msg.content))
            else:
                user_parts.append(str(msg.content))

        system_prompt = "\n\n".join(system_parts)
        user_prompt = "\n\n".join(user_parts) or " "

        target_dir = workdir or (add_dirs[0] if add_dirs else "")
        if target_dir:
            target_dir = os.path.abspath(target_dir)
        agent_name = self._write_agent_file(system_prompt)

        oc_cmd = self._build_oc_cmd(
            model=self._model,
            agent_name=agent_name,
            target_dir=target_dir,
            pure=self._pure,
            user_prompt=user_prompt,
        )

        # Run opencode directly. No script(1) PTY wrapper needed —
        # opencode 1.14.51+ produces clean JSONL on stdout even with
        # pipes when capture_output=True (pipes both stdout and stderr).
        try:
            proc = subprocess.run(
                oc_cmd,
                cwd=target_dir or None,
                capture_output=True,
                text=True,
                timeout=self._cli_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"OpenCode timed out after {self._cli_timeout}s"
            ) from exc
        except FileNotFoundError as exc:
            raise RuntimeError(
                f"OpenCode binary not found: {exc.filename}"
            ) from exc

        stdout = proc.stdout

        stderr_text = (proc.stderr or "").strip()[:500]

        if proc.returncode != 0:
            detail = (stdout or proc.stdout or "").strip()[:500]
            raise RuntimeError(
                f"OpenCode failed (exit {proc.returncode}): {detail}"
                + (f" | stderr: {stderr_text}" if stderr_text else "")
            )

        # Parse JSONL text events (primary path).
        final_text = self._parse_text_events(stdout)

        if not final_text:
            # Fallback: extract sessionID and use opencode export.
            session_id = self._extract_session_id(stdout)
            if session_id:
                final_text = self._export_session_text(session_id)

        if not final_text:
            # Check if the agent was actually active (had tool_use events).
            # If so, the LLM may have only produced tool calls in its final
            # response without a text event. Return empty content so the
            # caller's fallback (e.g. reading kernel_contract.json from disk)
            # can still pick up the generated artifacts.
            if self._has_tool_use_events(stdout):
                final_text = ""
            else:
                extra = ""
                if not stdout and stderr_text:
                    extra = f" | stderr: {stderr_text}"
                if not stdout and not stderr_text:
                    extra = " | stdout and stderr both empty; CLI may have silently crashed"
                raise RuntimeError(
                    f"OpenCode returned no text events (exit 0)."
                    + extra
                )

        return AIMessage(content=final_text)

    @staticmethod
    def _has_tool_use_events(stdout: str) -> bool:
        """Check if the JSONL output contains any tool_use events."""
        return any('"type":"tool_use"' in line for line in stdout.splitlines())

    @staticmethod
    def _parse_text_events(stdout: str) -> str:
        """Accumulate ``type:\"text\"`` JSONL events into a single string."""
        parts: list[str] = []
        for line in stdout.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                evt = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(evt, dict):
                continue
            if evt.get("type") == "text":
                part = evt.get("part") or {}
                if isinstance(part, dict):
                    text = part.get("text", "")
                    if text:
                        parts.append(text)
        return "".join(parts)

    def stream(self, messages: list[BaseMessage]):
        """Non-streaming fallback: invoke and yield single chunk."""
        yield self.invoke(messages)
