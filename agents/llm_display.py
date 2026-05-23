import json
import sys
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage


def _to_ai_message(gathered: BaseMessage) -> AIMessage:
    if isinstance(gathered, AIMessage):
        return gathered
    return AIMessage(
        content=gathered.content or "",
        additional_kwargs=getattr(gathered, "additional_kwargs", {}) or {},
        response_metadata=getattr(gathered, "response_metadata", {}) or {},
        tool_calls=getattr(gathered, "tool_calls", None) or [],
    )


DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
RESET = "\033[0m"


def _supports_color() -> bool:
    return sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    if not _supports_color():
        return text
    return f"{code}{text}{RESET}"


def _extract_reasoning(chunk: BaseMessage) -> str:
    if not hasattr(chunk, "additional_kwargs"):
        return ""
    kwargs = chunk.additional_kwargs or {}
    for key in ("reasoning_content", "reasoning", "thinking"):
        value = kwargs.get(key)
        if value:
            return str(value)
    return ""


def _print_agent_header(agent: str, phase: str) -> None:
    label = f"[{agent}] {phase}" if phase else f"[{agent}]"
    print(f"\n{_c(CYAN, label)}", flush=True)
    print(_c(DIM, "-" * 50), flush=True)


def _stream_chunk(chunk: BaseMessage) -> None:
    """Stream chunk content/reasoning to stdout."""
    reasoning = _extract_reasoning(chunk)
    if reasoning:
        print(_c(YELLOW, reasoning), end="", flush=True)

    content = chunk.content
    if isinstance(content, str) and content:
        print(content, end="", flush=True)
    elif isinstance(content, list):
        for part in content:
            if isinstance(part, str):
                print(part, end="", flush=True)
            elif isinstance(part, dict) and part.get("type") == "text":
                print(part.get("text", ""), end="", flush=True)


def _print_static_reasoning(message: BaseMessage) -> None:
    reasoning = _extract_reasoning(message)
    if reasoning:
        print(_c(YELLOW, reasoning), flush=True)


def _print_static_content(message: BaseMessage) -> None:
    content = message.content
    if isinstance(content, str) and content:
        print(content, flush=True)
    elif isinstance(content, list):
        texts = [
            p.get("text", "") if isinstance(p, dict) else str(p)
            for p in content
        ]
        joined = "".join(texts).strip()
        if joined:
            print(joined, flush=True)


def call_llm_with_display(
    agent: str,
    phase: str,
    llm: Any,
    messages: list,
) -> AIMessage:
    """Stream LLM output and show thinking/reasoning in real time."""
    _print_agent_header(agent, phase)

    stream_fn = getattr(llm, "stream", None)
    if stream_fn is None:
        response = llm.invoke(messages)
        _print_static_reasoning(response)
        _print_static_content(response)
        print(flush=True)
        return response

    gathered = None
    for chunk in stream_fn(messages):
        _stream_chunk(chunk)
        gathered = chunk if gathered is None else gathered + chunk

    print(flush=True)
    if gathered is None:
        return AIMessage(content="")
    return _to_ai_message(gathered)


def print_tool_call(tool_name: str, args: dict) -> None:
    args_text = json.dumps(args, ensure_ascii=False)
    print(_c(GREEN, f"  -> 调用工具: {tool_name}({args_text})"), flush=True)


def print_tool_result(result: str, max_len: int = 300) -> None:
    display = result if len(result) <= max_len else result[:max_len] + "..."
    print(_c(DIM, f"  <- 工具结果: {display}"), flush=True)
