"""LLM 流式输出与 thinking 展示模块。

支持两种模式：
1. stream 模式：实时打印到 stdout（用于 validator、pm、kernel_expert）
2. silent 模式：收集输出到文件，不打印（用于并行执行的 tool_expert）
"""

import sys
import os
from pathlib import Path
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


def _format_agent_header_text(agent: str, phase: str) -> str:
    """Format agent header as text (for file output)."""
    label = f"[{agent}] {phase}" if phase else f"[{agent}]"
    separator = "━" * 60
    return f"\n┌{separator}┐\n│ {label} │\n└{separator}┘\n▼ 输出开始\n"


def _format_agent_footer_text(agent: str) -> str:
    """Format agent footer as text (for file output)."""
    return "\n▲ 输出结束\n"


# 输出目录：用于存储工具专家的独立输出文件
OUTPUT_DIR = Path("/tmp/lumen_outputs")


def ensure_output_dir() -> Path:
    """确保输出目录存在。"""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    return OUTPUT_DIR


def get_expert_output_file(expert_type: str) -> Path:
    """获取专家输出文件路径。"""
    return OUTPUT_DIR / f"{expert_type}.txt"


def clear_output_dir() -> None:
    """清空输出目录（每次 workflow 开始时调用）。"""
    if OUTPUT_DIR.exists():
        for f in OUTPUT_DIR.glob("*.txt"):
            f.unlink()


def call_llm_with_display(
    agent: str,
    phase: str,
    llm: Any,
    messages: list,
    silent: bool = False,
    output_file: Path | None = None,
) -> AIMessage:
    """调用 LLM 并处理输出。

    Args:
        agent: Agent 名称
        phase: 执行阶段
        llm: LLM 实例
        messages: 消息列表
        silent: 静默模式，不打印到 stdout
        output_file: 输出文件路径（静默模式下使用）

    Returns:
        AIMessage 响应

    模式说明：
    - silent=False: 实时流式打印到 stdout（validator、pm、kernel_expert）
    - silent=True: 收集输出到文件，不打印 stdout（并行 tool_expert）
    """
    gathered_parts: list[str] = []

    stream_fn = getattr(llm, "stream", None)

    if silent:
        # 静默模式：收集到文件，不打印 stdout
        ensure_output_dir()
        if output_file is None:
            # 使用默认文件名
            safe_name = agent.replace(" ", "_").replace("/", "_")
            output_file = OUTPUT_DIR / f"{safe_name}.txt"

        # 写入 header
        header = _format_agent_header_text(agent, phase)
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(header)

        if stream_fn is None:
            response = llm.invoke(messages)
            reasoning = _extract_reasoning(response)
            content = response.content
            content_str = content if isinstance(content, str) else ""
            if isinstance(content, list):
                content_str = "".join(
                    p.get("text", "") if isinstance(p, dict) else str(p)
                    for p in content
                )

            with open(output_file, "a", encoding="utf-8") as f:
                if reasoning:
                    f.write(f"[thinking] {reasoning}\n")
                f.write(content_str + "\n")
                f.write(_format_agent_footer_text(agent))

            return response

        # 流式收集
        reasoning_parts: list[str] = []
        content_parts: list[str] = []

        for chunk in stream_fn(messages):
            reasoning = _extract_reasoning(chunk)
            if reasoning:
                reasoning_parts.append(reasoning)

            content = getattr(chunk, "content", None)
            if isinstance(content, str):
                content_parts.append(content)
                gathered_parts.append(content)
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, str):
                        content_parts.append(part)
                        gathered_parts.append(part)
                    elif isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text", "")
                        content_parts.append(text)
                        gathered_parts.append(text)

        # 写入文件
        with open(output_file, "a", encoding="utf-8") as f:
            if reasoning_parts:
                f.write("[thinking]\n" + "".join(reasoning_parts) + "\n\n")
            f.write("".join(content_parts) + "\n")
            f.write(_format_agent_footer_text(agent))

        return AIMessage(content="".join(gathered_parts))

    else:
        # 实时打印模式
        _print_agent_header(agent, phase)

        if stream_fn is None:
            response = llm.invoke(messages)
            _print_static_reasoning(response)
            _print_static_content(response)
            _print_agent_footer(agent)
            return response

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


def display_expert_outputs(expert_results: list) -> None:
    """展示所有工具专家的输出文件。

    在内核专家开始前调用，统一展示所有并行专家的分析结果。
    """
    print("\n" + _c(BOLD + BLUE, "=" * 60), flush=True)
    print(_c(BOLD + BLUE, "│ 工具专家分析结果汇总"), flush=True)
    print(_c(BOLD + BLUE, "=" * 60), flush=True)

    for result in expert_results:
        expert_type = result.get("expert_type", "unknown")
        expert_name = result.get("expert_name", expert_type)

        # 尝试读取输出文件
        output_file = get_expert_output_file(expert_type)
        if output_file.exists():
            print(f"\n{_c(CYAN, f'── [{expert_name}] 输出文件: {output_file} ──')}", flush=True)
            with open(output_file, "r", encoding="utf-8") as f:
                content = f.read()
                # 限制输出长度，避免过长
                if len(content) > 5000:
                    print(content[:5000], flush=True)
                    print(_c(DIM, "... (输出过长，已截断)"), flush=True)
                else:
                    print(content, flush=True)
        else:
            # 文件不存在，从 state 中的 analysis_output 展示
            print(f"\n{_c(CYAN, f'── [{expert_name}] ──')}", flush=True)
            analysis_output = result.get("analysis_output", "")
            if len(analysis_output) > 5000:
                print(analysis_output[:5000], flush=True)
                print(_c(DIM, "... (输出过长，已截断)"), flush=True)
            else:
                print(analysis_output, flush=True)

    print("\n" + _c(BOLD + BLUE, "=" * 60), flush=True)
    print(flush=True)