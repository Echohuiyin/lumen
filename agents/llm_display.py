"""LLM 流式输出与 thinking 展示模块。

支持两种模式：
1. stream 模式：实时打印到 stdout（用于 validator、pm、kernel_expert）
2. silent 模式：收集输出到文件，不打印（用于并行执行的 tool_expert）

自动持久化：每个 agent 完成后自动保存输出到 outputs/ 目录
"""

import sys
import os
from pathlib import Path
from typing import Any
from datetime import datetime

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


# ── Session-aware output directories ────────────────────────────────────
# When _session_dir is set (by a node function), all outputs go into
# session_dir/outputs/ instead of the default locations.
_session_dir: Path | None = None


def set_session_dir(path: str | Path | None) -> None:
    """Override output directories to a per-session path."""
    global _session_dir
    _session_dir = Path(path) if path else None


def _get_output_dir() -> Path:
    """Return the active expert output directory (session-aware)."""
    if _session_dir:
        d = _session_dir / "outputs"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path("/tmp/lumen_outputs")


def _get_persist_base_dir() -> Path:
    """Return the active persistence base directory (session-aware)."""
    if _session_dir:
        d = _session_dir / "persist"
        d.mkdir(parents=True, exist_ok=True)
        return d
    return Path("outputs")



def ensure_output_dir() -> Path:
    """确保输出目录存在。"""
    d = _get_output_dir()
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_expert_output_file(expert_type: str) -> Path:
    """获取专家输出文件路径（session-aware）。"""
    return _get_output_dir() / f"{expert_type}.txt"


def clear_output_dir() -> None:
    """清空输出目录（每次 workflow 开始时调用）。"""
    d = _get_output_dir()
    if d.exists():
        for f in d.glob("*.txt"):
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
            output_file = _get_output_dir() / f"{safe_name}.txt"

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


# ===== Human hint injection support =====

def _hint_file() -> Path:
    return _get_output_dir() / "kernel_expert.hint"

def _hint_review_file() -> Path:
    return _get_output_dir() / "hint_review.md"

def _hint_continue_file() -> Path:
    return _get_output_dir() / "kernel_expert.continue"

DEFAULT_HINT_WAIT_SECONDS = 120


def wait_for_hint(timeout_seconds: int = DEFAULT_HINT_WAIT_SECONDS) -> str:
    """Poll for a human hint within a bounded window after the review pack is written.

    Returns the hint text (and deletes the hint file) if the human writes one
    within the window. Returns "" if the human skips via the continue sidecar
    file or the window times out — either way the workflow proceeds normally
    to test_expert.

    The window is necessary because write_hint_review_pack and the hint check
    would otherwise run in the same tick, giving the human no time to read
    the pack and write a hint.
    """
    import time
    interval = 2
    elapsed = 0
    while elapsed < timeout_seconds:
        if _hint_continue_file().exists():
            try:
                _hint_continue_file().unlink()
            except Exception:
                pass
            return ""
        hint = read_and_consume_hint()
        if hint:
            return hint
        time.sleep(interval)
        elapsed += interval
    return ""


def write_hint_review_pack(
    user_input: str,
    expert_results: list,
    kernel_expert_output: str,
) -> Path:
    """Write a human review pack before hint injection.

    Includes user input, each tool expert's full structured evidence (with
    complete raw crash output via output_full) and LLM summary, and the
    kernel_expert first-round full output. Lets a human decide whether to
    inject a corrective hint.
    """
    ensure_output_dir()
    parts: list[str] = []
    parts.append("# 内核专家人审阅包\n")
    parts.append("如需注入关键思路，写入以下文件后 workflow 会自动拾取并重跑内核专家：\n")
    parts.append(f"```\necho \"你的思路\" > {_hint_file()}\n```\n")
    parts.append("注入后本轮只重跑一次。不写则正常流转到测试专家。\n")
    parts.append("\n---\n")

    parts.append("## 用户输入\n\n")
    parts.append(user_input + "\n")
    parts.append("\n---\n")

    for result in expert_results:
        expert_name = result.get("expert_name", "unknown")
        expert_type = result.get("expert_type", "")
        analysis_output = result.get("analysis_output", "")
        evidence_list = result.get("evidence") or (result.get("structured_output") or {}).get("evidence") or []

        parts.append(f"\n## 工具专家：{expert_name}（{expert_type}）\n")

        parts.append("\n### LLM 总结\n\n")
        parts.append(analysis_output + "\n")

        parts.append("\n### 过程数据（结构化 evidence + 完整 raw 输出）\n\n")
        if not evidence_list:
            parts.append("（无结构化 evidence）\n")
            continue

        for ev in evidence_list:
            kind = ev.get("kind", "")
            parts.append(f"\n#### [{kind}] \n")
            if kind == "crash_command":
                parts.append(f"- command: `{ev.get('command', '')}`\n")
                parts.append(f"- success: {ev.get('success', False)}\n")
                if ev.get("error"):
                    parts.append(f"- error: {ev.get('error')}\n")
                parts.append("\n完整输出：\n")
                parts.append("```\n")
                parts.append(ev.get("output_full", "") or "(empty)")
                parts.append("\n```\n")
            elif kind == "task":
                parts.append(f"- PID={ev.get('pid')} comm={ev.get('comm')} state={ev.get('state')} ppid={ev.get('ppid')} cpu={ev.get('cpu')}\n")
            elif kind == "backtrace":
                parts.append(f"- PID={ev.get('pid')} comm={ev.get('comm')}\n")
                for frame in ev.get("frames", []):
                    parts.append(f"  {frame}\n")
            elif kind == "log_event":
                parts.append(f"- [L{ev.get('line')}] ({ev.get('event_type')}) {ev.get('message')}\n")
                for ctx in ev.get("context", []):
                    parts.append(f"  [L{ctx.get('line')}] {ctx.get('message')}\n")
            else:
                parts.append(f"```\n{ev}\n```\n")

        parts.append("\n---\n")

    parts.append("\n## 内核专家第 1 轮输出（完整原文）\n\n")
    parts.append(kernel_expert_output + "\n")

    _hint_review_file().write_text("\n".join(parts), encoding="utf-8")
    return _hint_review_file()


def read_and_consume_hint() -> str:
    """Non-blocking check for a human hint file.

    Returns the hint text if present and non-empty, then deletes the file so
    the next retry cycle doesn't re-inject the same hint. Returns "" when no
    hint is pending.
    """
    if not _hint_file().exists():
        return ""
    try:
        content = _hint_file().read_text(encoding="utf-8").strip()
    except Exception:
        return ""
    if not content:
        try:
            _hint_file().unlink()
        except Exception:
            pass
        return ""
    try:
        _hint_file().unlink()
    except Exception:
        pass
    return content


# ===== 输出持久化功能 =====

# Agent 到子目录的映射
AGENT_DIR_MAP = {
    "validator": "validation",
    "pm": "validation",
    "kernel_expert": "kernel_expert",
    "test_expert": "test_expert",
}


def get_persist_dir(base_dir: Path | None = None) -> Path:
    """获取持久化输出目录（session-aware）。"""
    if base_dir is None:
        base_dir = _get_persist_base_dir()
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir


def get_agent_output_dir(agent: str, base_dir: Path | None = None) -> Path:
    """获取特定 agent 的输出目录。"""
    persist_dir = get_persist_dir(base_dir)
    subdir = AGENT_DIR_MAP.get(agent, "experts")
    agent_dir = persist_dir / subdir
    agent_dir.mkdir(parents=True, exist_ok=True)
    return agent_dir


def _persist_agent_output(
    base_dir: Path,
    agent: str,
    phase: str,
    content: str,
) -> Path:
    """保存 agent 输出到文件。

    Args:
        base_dir: 输出根目录
        agent: Agent 名称
        phase: 执行阶段
        content: 输出内容

    Returns:
        输出文件路径
    """
    agent_dir = get_agent_output_dir(agent, base_dir)

    # 使用时间戳创建唯一文件名
    timestamp = datetime.now().strftime("%H%M%S")
    # 清理 phase 中的特殊字符
    safe_phase = phase.replace("/", "_").replace(" ", "_")
    filename = f"{agent}_output_{safe_phase}_{timestamp}.txt"

    output_file = agent_dir / filename

    # 写入内容
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(f"[{agent}] {phase}\n")
        f.write(f"Timestamp: {datetime.now().isoformat()}\n\n")
        f.write(content)

    return output_file


def call_llm_with_persistence(
    agent: str,
    phase: str,
    llm: Any,
    messages: list,
    silent: bool = False,
    output_file: Path | None = None,
    persist_dir: Path | None = None,
) -> AIMessage:
    """调用 LLM 并自动持久化输出。

    这是 call_llm_with_display 的增强版本，自动保存输出到 outputs/ 目录。

    Args:
        agent: Agent 名称
        phase: 执行阶段
        llm: LLM 实例
        messages: 消息列表
        silent: 静默模式
        output_file: 输出文件路径（静默模式）
        persist_dir: 持久化目录（默认 outputs/）

    Returns:
        AIMessage 响应
    """
    # 使用原有函数执行 LLM 调用
    response = call_llm_with_display(
        agent=agent,
        phase=phase,
        llm=llm,
        messages=messages,
        silent=silent,
        output_file=output_file,
    )

    # 自动持久化输出
    if persist_dir is None:
        persist_dir = _get_persist_base_dir()
    else:
        # When a session dir is active, override explicit persist_dir too
        session_base = _get_persist_base_dir()
        if session_base != Path("outputs"):
            persist_dir = session_base

    try:
        saved_file = _persist_agent_output(
            base_dir=persist_dir,
            agent=agent,
            phase=phase,
            content=response.content,
        )
        # 不打印保存信息，避免输出干扰
        # 实际使用时可通过日志记录
    except Exception as e:
        # 持久化失败不影响主流程
        pass

    return response