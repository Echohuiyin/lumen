import shlex
import subprocess
import time
import uuid

import httpx
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage


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
        max_tokens: int = 2048,
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
