import sys
from typing import Any

from langchain_core.messages import AIMessage, BaseMessage


def _to_ai_message(gathered: BaseMessage) -> AIMessage:
    if isinstance(gathered, AIMessage):
        return gathered
    return AIMessage(
        content=getattr(gathered, "content", str(gathered)) or "",
        additional_kwargs=getattr(gathered, "additional_kwargs", {}) or {},
        response_metadata=getattr(gathered, "response_metadata", {}) or {},
        tool_calls=getattr(gathered, "tool_calls", None) or [],
    )


DIM = "\033[2m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
GREEN = "\033[32m"
BLUE = "\033[34m"
RESET = "\033[0m"
BOLD = "\033[1m"


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
    """Print agent header with clear separator."""
    label = f"[{agent}] {phase}" if phase else f"[{agent}]"
    # Use box-style separator for better visual distinction
    separator = "━" * 60
    print(f"\n{_c(BOLD + CYAN, '┌' + separator + '┐')}", flush=True)
    print(f"{_c(BOLD + CYAN, '│')} {_c(GREEN, label)} {_c(BOLD + CYAN, '│')}", flush=True)
    print(f"{_c(BOLD + CYAN, '└' + separator + '┘')}", flush=True)
    print(_c(DIM, "▼ 输出开始"), flush=True)


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


def _print_agent_footer(agent: str) -> None:
    """Print agent footer to mark end of output."""
    print(_c(DIM, "▲ 输出结束"), flush=True)


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
        _print_agent_footer(agent)
        return response

    gathered_parts: list[str] = []
    for chunk in stream_fn(messages):
        _stream_chunk(chunk)
        content = getattr(chunk, "content", None)
        if isinstance(content, str):
            gathered_parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, str):
                    gathered_parts.append(part)
                elif isinstance(part, dict) and part.get("type") == "text":
                    gathered_parts.append(part.get("text", ""))

    print(flush=True)
    _print_agent_footer(agent)
    return AIMessage(content="".join(gathered_parts))
