"""工具专家 agent：根据 expert_type 执行对应的专业分析。

支持的知识库搜索专家现在会实际执行 RAG 检索，而非仅输出命令。
支持 crash_analysis/lock_analysis 专家使用工具调用执行 crash 命令。
使用静默模式执行，输出写入独立文件，避免并行输出交错。
"""

import os
import re
from pathlib import Path
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agents.contracts import ToolExpertOutput, model_to_dict
from agents.llm_display import call_llm_with_display, set_session_dir, get_expert_output_file, ensure_output_dir, _format_agent_header_text, _format_agent_footer_text
from agents.rag_integration import get_rag_context_for_query
from agents.semcode_path_analysis import create_semcode_tools
from llm_config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState, ToolExpertResult


def _extract_vmcore_paths(user_input: str) -> tuple[str | None, str | None]:
    """从用户输入中提取 vmcore 和 vmlinux 文件路径。

    Returns:
        (vmcore_path, vmlinux_path) 或 (None, None)
    """
    import re

    # Support both Chinese and English keywords before the path
    # Support space or colon separator (e.g., "vmcore 文件 ~/path" or "vmcore: ~/path")
    # Stop at common delimiters: space, comma (Chinese/English), semicolon, newline
    # Match paths starting with ~ or /
    path_capture = r'([~/][^,，;；\s]+)'

    vmcore_pattern = rf'vmcore\s*(?:文件|file|路径|path)?(?:\s*[：:])?\s*{path_capture}'
    vmlinux_pattern = rf'vmlinux\s*(?:文件|file|路径|path)?(?:\s*[：:])?\s*{path_capture}'

    vmcore_match = re.search(vmcore_pattern, user_input, re.IGNORECASE)
    vmlinux_match = re.search(vmlinux_pattern, user_input, re.IGNORECASE)

    vmcore_path = vmcore_match.group(1) if vmcore_match else None
    vmlinux_path = vmlinux_match.group(1) if vmlinux_match else None

    return vmcore_path, vmlinux_path


def _vmcore_paths_from_state(state: MaintenanceWorkflowState) -> tuple[str | None, str | None]:
    artifacts = state.get("input_artifacts_contract") or {}
    vmcore_path = artifacts.get("vmcore_path") or None
    vmlinux_path = artifacts.get("vmlinux_path") or None
    if vmcore_path or vmlinux_path:
        return vmcore_path, vmlinux_path
    return _extract_vmcore_paths(state.get("user_input", ""))


def _resolve_file_path(path: str | None, try_suffixes: list[str] | None = None) -> str | None:
    """Resolve a file path, handling ~ expansion and optional suffix fallbacks.

    Args:
        path: Raw path (may contain ~, may be None)
        try_suffixes: Optional list of suffixes to try if path doesn't exist
                      (e.g. ['.elf'] for vmcore files that may have .elf extension)

    Returns:
        Resolved absolute path, or None if path is None.
        Returns the first existing path found, or the expanded path if none exist.
    """
    if path is None:
        return None
    expanded = os.path.expanduser(path)
    if os.path.exists(expanded):
        return expanded
    if try_suffixes:
        for suffix in try_suffixes:
            if not expanded.endswith(suffix):
                candidate = expanded + suffix
                if os.path.exists(candidate):
                    return candidate
    return expanded


def _check_file_exists(path: str | None) -> bool:
    """Check if a file exists, trying .elf suffix for vmcore files.

    Shares resolution logic with _resolve_file_path.
    """
    if path is None:
        return False
    resolved = _resolve_file_path(path, try_suffixes=[".elf"])
    return os.path.exists(resolved)


def _log_tool_call(output_file: str, tool_name: str, tool_args: dict, expert_name: str):
    """Log tool execution to output file."""
    from pathlib import Path
    args_str = ", ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""
    with open(output_file, "a", encoding="utf-8") as f:
        f.write(f"\n[{expert_name}] 执行工具: {tool_name}({args_str})\n")
        f.write("等待输出...\n")


def _write_tool_call_output(output_file: str, content: str, expert_name: str):
    """Write final tool-calling output to file."""
    header = _format_agent_header_text(expert_name, "分析完成")
    footer = _format_agent_footer_text(expert_name)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(content + "\n")
        f.write(footer)


def _persist_extracted_kernel_log(output_file: Path, content: str) -> Path:
    """Atomically retain the raw log extracted from a vmcore."""
    log_file = output_file.with_name("kernel_log.raw.log")
    temporary = log_file.with_suffix(".tmp")
    temporary.write_text(content, encoding="utf-8", errors="replace")
    temporary.replace(log_file)
    return log_file.resolve()


def _make_tool_result(
    *,
    expert_type: str,
    expert_name: str,
    analysis_output: str,
    status: str = "degraded",
    evidence: list[dict] | None = None,
    artifacts: dict | None = None,
    errors: list[str] | None = None,
) -> ToolExpertResult:
    """Persist every expert result and pass its path as the inter-agent API."""
    output_file = get_expert_output_file(expert_type)
    # Most execution paths already write a rich transcript.  Early failures
    # (missing configuration, unavailable input) do not, so materialize their
    # result too.  This makes every tool expert independently reviewable and
    # prevents the kernel loop from consuming an in-memory-only summary.
    if not output_file.exists():
        output_file.parent.mkdir(parents=True, exist_ok=True)
        output_file.write_text(analysis_output + "\n", encoding="utf-8")
    result_artifacts = dict(artifacts or {})
    result_artifacts["expert_output_file"] = str(output_file.resolve())
    structured = ToolExpertOutput(
        expert_type=expert_type,
        expert_name=expert_name,
        status=status,
        summary=analysis_output[:1000],
        evidence=evidence or [],
        artifacts=result_artifacts,
        errors=errors or [],
    )
    return ToolExpertResult(
        expert_type=expert_type,
        expert_name=expert_name,
        analysis_output=analysis_output,
        structured_output=model_to_dict(structured),
    )


def _result_success(result) -> bool:
    return bool(getattr(result, "success", False))


def _result_output(result) -> str:
    return str(getattr(result, "output", "") or "")


def _run_crash_command_for_evidence(session, command: str) -> dict:
    """Run one crash command and normalize the result for structured evidence."""
    try:
        result = session.run_command(command)
        return {
            "command": command,
            "success": _result_success(result),
            "output": _result_output(result),
        }
    except Exception as exc:
        return {
            "command": command,
            "success": False,
            "output": "",
            "error": str(exc),
        }


def _collect_crash_evidence(session, expert_type: str) -> tuple[list[dict], dict, list[str], str]:
    """Collect a deterministic crash baseline before LLM interpretation."""
    commands = ["sys", "ps", "bt -a", "log | tail -n 200"]
    if expert_type == "lock_analysis":
        commands.extend(["waitq", "foreach bt"])

    command_results = [_run_crash_command_for_evidence(session, command) for command in commands]
    evidence = [_command_evidence(item) for item in command_results]
    evidence.extend(_parse_ps_evidence(_output_for(command_results, "ps")))
    evidence.extend(_parse_bt_evidence(_output_for(command_results, "bt -a")))
    evidence.extend(_parse_log_evidence(_output_for(command_results, "log | tail -n 200")))

    artifacts = {
        "crash_commands": ",".join(commands),
    }
    errors = [
        f"{item['command']}: {item.get('error') or item.get('output', '')[:200]}"
        for item in command_results
        if not item.get("success")
    ]
    prompt_context = _format_evidence_context(command_results, evidence)
    return evidence, artifacts, errors, prompt_context


def _command_evidence(command_result: dict) -> dict:
    """Build evidence for one crash command.

    output_full preserves the complete raw output for the human review pack
    (hint_review.md). It is intentionally NOT fed to the LLM — the LLM gets
    the compact structured evidence via _format_evidence_context and can pull
    raw details on demand through run_crash_command.
    """
    return {
        "kind": "crash_command",
        "command": command_result["command"],
        "success": command_result.get("success", False),
        "output_full": command_result.get("output", ""),
        "error": command_result.get("error", ""),
    }


def _output_for(command_results: list[dict], command: str) -> str:
    for item in command_results:
        if item.get("command") == command:
            return item.get("output", "")
    return ""


def _parse_ps_evidence(ps_output: str) -> list[dict]:
    evidence = []
    for line in ps_output.splitlines():
        match = re.match(
            r"^\s*>?\s*(?P<pid>\d+)\s+(?P<ppid>\d+)\s+(?P<cpu>\d+)\s+"
            r"(?P<task>[0-9a-fA-F]+)\s+(?P<state>\S+)\s+.*?\s(?P<comm>\S+)\s*$",
            line,
        )
        if not match:
            continue
        state = match.group("state")
        if state.upper() in {"UN", "RU", "IN"}:
            evidence.append({
                "kind": "task",
                "pid": int(match.group("pid")),
                "ppid": int(match.group("ppid")),
                "cpu": int(match.group("cpu")),
                "task": match.group("task"),
                "state": state,
                "comm": match.group("comm"),
            })
    return evidence


def _parse_bt_evidence(bt_output: str) -> list[dict]:
    evidence = []
    current: dict | None = None
    for line in bt_output.splitlines():
        pid_match = re.search(r"PID:\s*(\d+).*?COMMAND:\s*\"?([^\"\n]+)\"?", line)
        if pid_match:
            if current:
                evidence.append(current)
            current = {
                "kind": "backtrace",
                "pid": int(pid_match.group(1)),
                "comm": pid_match.group(2).strip(),
                "frames": [],
            }
            continue
        if current and re.search(r"#\d+\s+\[", line):
            current["frames"].append(line.strip())
    if current:
        evidence.append(current)
    for item in evidence:
        item["frames"] = item.get("frames", [])
    return evidence


def _parse_log_evidence(log_output: str) -> list[dict]:
    """Extract structured log events with ±3 lines of context.

    The context lines let the LLM see what surrounded a panic/hung_task/lockdep
    event (e.g. the Call Trace that follows a BUG: line) without dumping the
    full raw log into context.
    """
    patterns = [
        ("kernel_panic", r"kernel panic"),
        ("oops", r"\boops\b"),
        ("bug", r"\bBUG:|kernel BUG"),
        ("null_pointer", r"null pointer|unable to handle"),
        ("hung_task", r"blocked for more than|hung task"),
        ("lockdep", r"lockdep|circular locking|deadlock"),
        ("soft_lockup", r"soft lockup"),
        ("hard_lockup", r"hard lockup"),
        ("call_trace", r"call trace"),
    ]
    lines = log_output.splitlines()
    evidence = []
    for line_no, line in enumerate(lines, start=1):
        lowered = line.lower()
        for event_type, pattern in patterns:
            if re.search(pattern, lowered, re.IGNORECASE):
                start = max(0, line_no - 4)
                end = min(len(lines), line_no + 3)
                context = [
                    {"line": start + i + 1, "message": lines[start + i].strip()}
                    for i in range(end - start)
                ]
                evidence.append({
                    "kind": "log_event",
                    "event_type": event_type,
                    "line": line_no,
                    "message": line.strip(),
                    "context": context,
                })
                break
    return evidence


def _format_evidence_context(command_results: list[dict], evidence: list[dict]) -> str:
    """Render structured evidence as compact, kind-grouped text for the LLM.

    Lossless on signal: no item count cap, no per-item char cap. Grouped by
    kind so the LLM can scan D-state tasks, backtraces, and log events
    separately. Raw output is NOT included — the LLM pulls specifics via
    run_crash_command when needed.
    """
    command_summary = "\n".join(
        f"- {item['command']}: {'ok' if item.get('success') else 'failed'}"
        for item in command_results
    )

    by_kind: dict[str, list[dict]] = {}
    for item in evidence:
        by_kind.setdefault(item.get("kind", "unknown"), []).append(item)

    sections: list[str] = []
    if by_kind.get("task"):
        lines = [f"  PID={t.get('pid')} comm={t.get('comm')} state={t.get('state')}"
                 for t in by_kind["task"]]
        sections.append(f"D-state / runnable tasks ({len(by_kind['task'])}):\n" + "\n".join(lines))

    if by_kind.get("backtrace"):
        bt_parts = [f"Backtraces ({len(by_kind['backtrace'])}):"]
        for bt in by_kind["backtrace"]:
            bt_parts.append(f"  PID={bt.get('pid')} comm={bt.get('comm')}:")
            for frame in bt.get("frames", []):
                bt_parts.append(f"    {frame}")
        sections.append("\n".join(bt_parts))

    if by_kind.get("log_event"):
        ev_parts = [f"Key log events ({len(by_kind['log_event'])}):"]
        for ev in by_kind["log_event"]:
            ev_parts.append(f"  [L{ev.get('line')}] ({ev.get('event_type')}) {ev.get('message')}")
            for ctx in ev.get("context", []):
                ev_parts.append(f"    [L{ctx.get('line')}] {ctx.get('message')}")
        sections.append("\n".join(ev_parts))

    if by_kind.get("crash_command"):
        cmd_parts = [f"Crash commands ({len(by_kind['crash_command'])}):"]
        for cmd in by_kind["crash_command"]:
            cmd_parts.append(f"  {cmd.get('command')}: {'ok' if cmd.get('success') else 'failed'}"
                             + (f" — {cmd.get('error')}" if cmd.get("error") else ""))
        sections.append("\n".join(cmd_parts))

    evidence_summary = "\n\n".join(sections) if sections else "(no structured evidence)"
    return f"""Deterministic crash baseline already collected.

Commands:
{command_summary}

Structured evidence (lossless, grouped by kind):
{evidence_summary}
"""


def _run_tool_calling_analysis(
    llm,
    system_prompt: str,
    user_input: str,
    expert_type: str,
    vmcore_path: str,
    vmlinux_path: str,
    expert_name: str,
    output_file: str,
    max_iterations: int = 15,
) -> tuple[AIMessage, list[dict], dict, list[str]]:
    """Execute crash analysis with tool calling.

    Creates crash session, binds tools to LLM, runs tool-calling loop,
    and returns final AIMessage with analysis.
    """
    from agents.crash_tools import create_crash_tools, get_or_create_crash_session, release_crash_session
    from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages

    # Write initial header
    header = _format_agent_header_text(expert_name, "分析中 (工具调用)")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(f"Crash Session: {vmcore_path}\n")
        f.write(f"Vmlinux: {vmlinux_path}\n\n")

    session = None
    try:
        # Create or reuse shared crash session (prevents concurrent
        # crash processes competing for the same vmcore binary)
        session = get_or_create_crash_session(vmcore_path, vmlinux_path)

        evidence, artifacts, evidence_errors, evidence_context = _collect_crash_evidence(session, expert_type=expert_type)

        # Create session-bound tools plus bounded Semcode source lookups.
        crash_tools = create_crash_tools(session)
        artifacts = state.get("input_artifacts_contract") or {}
        source_path = artifacts.get("kernel_source_path", "")
        semcode_cfg = agent_config.get("semcode_mcp") or {}
        if source_path and semcode_cfg.get("command"):
            crash_tools.extend(create_semcode_tools(
                command=str(semcode_cfg["command"]),
                args=semcode_cfg.get("args", []) or [],
                kernel_source_path=source_path,
            ))

        # Build context info for LLM — emphasize data-driven analysis
        semcode_tool_text = ""
        if source_path and semcode_cfg.get("command"):
            semcode_tool_text = (
                "- semcode_find_function: locate a kernel function and direct callees\n"
                "- semcode_find_callers: list callers from the indexed kernel source\n"
            )
        context_info = f"""Crash analysis environment ready:
- vmcore: {vmcore_path}
- vmlinux: {vmlinux_path}

Available tools:
- collect_baseline: collect sys + bt + log (call FIRST)
- run_crash_command: execute a single crash command
- run_crash_commands: batch execute multiple commands
{semcode_tool_text}

{evidence_context}

INSTRUCTIONS:
1. Use the deterministic evidence above as ground truth
2. Call extra crash commands only when needed to fill gaps
3. Identify D-state (UN) processes from ps output — record their REAL PIDs and names
4. If backtraces show mutex_lock or similar, examine the mutex struct to find owners
5. Decode mutex.owner: counter & ~0x7 gives the task_struct pointer
6. Cross-reference: verify that the owner task from mutex matches an actual task in ps output

CRITICAL: Your final analysis MUST reference specific PIDs, addresses, function names,
and module names from the tool outputs. Never fabricate data."""

        # Override system prompt with a focused tool-calling version.
        # The prompt files (crash_analysis.md, lock_analysis.md) describe
        # complex MCP-based workflows that don't match the StructuredTool system.
        # Using them causes the LLM to describe workflow phases instead of
        # producing data-driven analysis.
        tool_focused_prompt = f"""You are a Linux kernel crash analyst with direct access to the crash tool.

You have crash command tools bound to you: collect_baseline, run_crash_command, run_crash_commands.
These tools execute REAL crash commands against the vmcore and return actual output.
You are NOT using MCP — you have direct tool bindings. No "phases", no "sessions".

WORKFLOW:
1. Call collect_baseline() to get sys, bt, and log
2. Analyze the output: find D-state (UN) processes, record their REAL PIDs and names
3. Get detailed backtraces: bt <pid> for each D-state process
4. If backtraces show lock functions (mutex_lock, down_write, etc.), examine the lock:
   - struct mutex.owner <addr> -x  to get owner task pointer
   - struct task_struct.pid,comm <decoded_addr> to identify the owner
   - Mutex owner decode: counter & ~0x7 yields task_struct pointer
5. For hung_task/deadlock: identify the lock dependency chain (who holds what, who waits for what)
6. For lock_analysis specifically: check mutex.wait_list to see blocked waiters
7. When Semcode tools are available, use them to verify the key functions from
   the crash stack. Include the returned source locations and caller/callee
   edges as evidence; do not infer file names or line numbers.

OUTPUT REQUIREMENTS:
- State the crash type based on sys/log output
- Quote REAL PIDs, process names, and addresses from the tool outputs
- Show the lock dependency chain with actual data
- Never fabricate PIDs (106/107 are real, use them; don't invent 1234 or 5678)
- Never invent call stacks (use what bt actually shows)
- If the data shows crash_deadlock module with mutex_a/mutex_b, analyze THAT, not ext4/jbd2

Remember: your tools return real data. Reference it precisely."""

        # Create messages for tool-calling loop
        messages = create_tool_call_messages(
            system_prompt=tool_focused_prompt,
            user_input=user_input,
            context_info=context_info,
        )

        # Execute tool-calling loop
        response = execute_tool_calling_loop(
            llm=llm,
            messages=messages,
            tools=crash_tools,
            max_iterations=max_iterations,
            on_tool_call=lambda name, args: _log_tool_call(output_file, name, args, expert_name),
            verbose=True,  # 启用 verbose 以便调试
        )

        # 检查是否需要强制生成总结
        # 条件1: 响应内容过短（<500字符）
        # 条件2: 响应仍包含 tool_calls（LLM 还想调用工具）
        # 条件3: 内容包含描述性语句而非实际分析
        needs_summary = False
        content = response.content or ""

        if len(content) < 500:
            needs_summary = True

        # 检查是否还在尝试调用工具
        tool_calls = getattr(response, "tool_calls", None) or []
        if tool_calls:
            needs_summary = True

        # Check if content is descriptive rather than analytical
        if any(phrase in content for phrase in ["让我开始", "我需要先", "首先调用", "执行分析流程", "阶段零"]):
            needs_summary = True

        # Detect raw tool-call XML in response (LLM returned syntax instead of analysis)
        if "</invoke>" in content or "<｜｜DSML｜｜tool_calls>" in content:
            needs_summary = True

        if needs_summary:
            # Force LLM to generate data-driven analysis summary
            summary_messages = list(messages) + [
                HumanMessage(content="""Based on the crash tool data collected above, generate a data-driven analysis report.

CRITICAL RULES:
1. Do NOT call any more tools
2. Every claim MUST cite specific data from the tool outputs above (PIDs, addresses, function names, module names)
3. Do NOT fabricate PIDs, process names, or call stacks that don't appear in the tool outputs
4. If the data shows PID 106 (insmod) and PID 107 (deadlock_thread), analyze THOSE processes — not systemd or jbd2
5. Use actual addresses from the tool output, not made-up ones

Report format:
- Crash type: (based on sys/log output)
- Key call stacks: (quote actual bt output, include real PIDs and function names)
- Lock analysis: (if mutex data collected, show actual owner PIDs decoded from owner.counter & ~0x7)
- Root cause: (based on the actual evidence, not speculation)
- Conclusion: (data-supported finding)""")
            ]
            summary_response = llm.invoke(summary_messages)
            _write_tool_call_output(output_file, summary_response.content, expert_name)
            return summary_response, evidence, artifacts, evidence_errors

        # Write final output
        _write_tool_call_output(output_file, response.content, expert_name)

        return response, evidence, artifacts, evidence_errors

    except Exception as e:
        # Session creation or execution failed
        error_msg = f"Crash session 执行失败: {str(e)}"
        _write_tool_call_output(output_file, error_msg, expert_name)
        return AIMessage(content=error_msg), [], {}, [error_msg]

    finally:
        # Release shared session reference (stops crash process
        # only when no more experts are using it)
        if session is not None:
            try:
                release_crash_session(vmcore_path, vmlinux_path)
            except Exception:
                pass  # Ignore cleanup errors


def tool_expert_node(state: MaintenanceWorkflowState) -> dict:
    """工具专家 agent：根据 expert_type 执行对应的专业分析。

    支持的专家类型通过配置文件定义，目前包括：
    - knowledge_search: 历史知识库搜索（实际执行 RAG 检索）
    - lock_analysis: 锁分析（工具调用执行 crash 命令）
    - crash_analysis: Crash 分析（工具调用执行 crash 命令）
    - kernel_log_analysis: 内核日志分析

    使用静默模式执行，输出写入独立文件，避免并行输出交错。
    """
    set_session_dir(state.get("session_dir"))
    expert_type = state["expert_type"]
    config = state.get("config", {})
    user_input = state.get("user_input", "")

    # 确保输出目录存在
    ensure_output_dir()

    # 从配置中找到对应专家的配置
    experts_config = config.get("tool_experts", [])
    expert_config = None
    for exp in experts_config:
        if exp["type"] == expert_type:
            expert_config = exp
            break

    if expert_config is None:
        return {
            "expert_results": [_make_tool_result(
                expert_type=expert_type,
                expert_name=expert_type,
                analysis_output=f"未找到类型为 {expert_type} 的工具专家配置。",
                status="blocked",
                errors=[f"missing tool expert config: {expert_type}"],
            )],
        }

    agent_config = expert_config.get("agent", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config)
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", f"prompts/{expert_type}.md")
    )

    expert_name = expert_config.get("name", expert_type)
    output_file = get_expert_output_file(expert_type)

    # 根据专家类型构建不同的用户输入内容和执行方式
    if expert_type == "knowledge_search":
        # 知识库搜索专家：实际执行 RAG 检索
        query = user_input
        rag_context = get_rag_context_for_query(query, top_k=3)

        user_content = f"""用户输入:
{user_input}

---
以下是从历史知识库检索到的相似案例，请参考这些案例进行分析：

{rag_context}

请基于以上历史案例，结合当前问题特征，给出分析结论和建议。"""

        response = call_llm_with_display(
            expert_name, "分析中", llm,
            [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
            silent=True,
            output_file=output_file,
        )

        return {
            "expert_results": [_make_tool_result(
                expert_type=expert_type,
                expert_name=expert_name,
                analysis_output=response.content.strip(),
                status="ok",
            )],
        }

    elif expert_type in ("crash_analysis", "lock_analysis"):
        # Crash/锁分析专家：使用工具调用执行 crash 命令
        vmcore_path_raw, vmlinux_path_raw = _vmcore_paths_from_state(state)

        # Resolve paths (expand ~ and try .elf suffix for vmcore)
        vmcore_path = _resolve_file_path(vmcore_path_raw, try_suffixes=[".elf"])
        vmlinux_path = _resolve_file_path(vmlinux_path_raw) if vmlinux_path_raw else None

        vmcore_exists = _check_file_exists(vmcore_path_raw)
        vmlinux_exists = _check_file_exists(vmlinux_path_raw)

        # 检查必要文件是否存在
        if not vmcore_path_raw or not vmlinux_path_raw:
            # 缺少路径信息，降级为文本分析
            file_status = "未识别到 vmcore 或 vmlinux 文件路径"
            user_content = f"""用户输入:
{user_input}

⚠️ 注意: {file_status}，无法执行 crash 工具分析。
请基于已有文本信息进行初步分析，并说明需要的补充信息。"""

            response = call_llm_with_display(
                expert_name, "分析中", llm,
                [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
                silent=True,
                output_file=output_file,
            )

            return {
                "expert_results": [_make_tool_result(
                    expert_type=expert_type,
                    expert_name=expert_name,
                    analysis_output=response.content.strip(),
                    status="degraded",
                    errors=[file_status],
                )],
            }

        if not vmcore_exists or not vmlinux_exists:
            # 文件不存在，降级为文本分析
            file_status = f"""
vmcore 文件: {vmcore_path_raw} → {vmcore_path} ({'✓ 存在' if vmcore_exists else '✗ 不存在'})
vmlinux 文件: {vmlinux_path_raw} → {vmlinux_path} ({'✓ 存在' if vmlinux_exists else '✗ 不存在'})

⚠️ 注意: 必要文件不存在，无法执行 crash 工具分析。"""
            user_content = f"""用户输入:
{user_input}

{file_status}
请基于已有文本信息进行初步分析。"""

            response = call_llm_with_display(
                expert_name, "分析中", llm,
                [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
                silent=True,
                output_file=output_file,
            )

            return {
                "expert_results": [_make_tool_result(
                    expert_type=expert_type,
                    expert_name=expert_name,
                    analysis_output=response.content.strip(),
                    status="degraded",
                    artifacts={
                        "vmcore_path": vmcore_path or "",
                        "vmlinux_path": vmlinux_path or "",
                    },
                    errors=["required crash files missing"],
                )],
            }

        # === 工具调用路径 ===
        # 文件存在，创建 crash session 并执行工具调用循环
        response, evidence, evidence_artifacts, evidence_errors = _run_tool_calling_analysis(
            llm=llm,
            system_prompt=system_prompt,
            user_input=user_input,
            expert_type=expert_type,
            vmcore_path=vmcore_path,  # 使用展开后的路径
            vmlinux_path=vmlinux_path,  # 使用展开后的路径
            expert_name=expert_name,
            output_file=output_file,
            max_iterations=15,
        )

        return {
            "expert_results": [_make_tool_result(
                expert_type=expert_type,
                expert_name=expert_name,
                analysis_output=response.content.strip(),
                status="ok",
                evidence=evidence,
                artifacts={
                    "vmcore_path": vmcore_path or "",
                    "vmlinux_path": vmlinux_path or "",
                    "output_file": str(output_file),
                    **evidence_artifacts,
                },
                errors=evidence_errors,
            )],
        }

    elif expert_type == "kernel_log_analysis":
        # 内核日志分析专家：如果有 vmcore，使用 crash 提取日志
        vmcore_path_raw, vmlinux_path_raw = _vmcore_paths_from_state(state)
        vmcore_path = _resolve_file_path(vmcore_path_raw, try_suffixes=[".elf"])
        vmlinux_path = _resolve_file_path(vmlinux_path_raw) if vmlinux_path_raw else None
        vmcore_exists = _check_file_exists(vmcore_path_raw)
        vmlinux_exists = _check_file_exists(vmlinux_path_raw)

        if vmcore_path_raw and vmlinux_path_raw and vmcore_exists and vmlinux_exists:
            # 使用 crash 工具提取内核日志
            header = _format_agent_header_text(expert_name, "分析中 (工具调用)")
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(header)
                f.write(f"Crash Session: {vmcore_path}\n")
                f.write(f"Vmlinux: {vmlinux_path}\n\n")

            session = None
            try:
                from agents.crash_tools import get_or_create_crash_session, release_crash_session
                session = get_or_create_crash_session(vmcore_path, vmlinux_path)

                # Execute log command to extract kernel log
                log_result = session.run_command("log")
                if not _result_success(log_result):
                    error_msg = "从 vmcore 提取日志失败: crash log command failed"
                    _write_tool_call_output(output_file, error_msg, expert_name)
                    return {
                        "expert_results": [_make_tool_result(
                            expert_type=expert_type,
                            expert_name=expert_name,
                            analysis_output=error_msg,
                            status="failed",
                            evidence=[_command_evidence({
                                "command": "log", "success": False,
                                "output": _result_output(log_result),
                            })],
                            artifacts={
                                "vmcore_path": vmcore_path or "",
                                "vmlinux_path": vmlinux_path or "",
                                "output_file": str(output_file),
                            },
                            errors=[error_msg],
                        )],
                    }
                log_content = _result_output(log_result)
                raw_log_file = _persist_extracted_kernel_log(output_file, log_content)
                evidence = [_command_evidence({
                    "command": "log",
                    "success": _result_success(log_result),
                    "output": log_content,
                })]
                evidence.extend(_parse_log_evidence(log_content))

                # Build context with extracted log.
                # Override system prompt — the prompt file describes MCP-based
                # workflows that don't match direct crash session use.
                # Force the LLM to analyze the provided log content directly.
                log_analysis_prompt = """You are a kernel log analysis expert.
The crash tool has already extracted the kernel log from the vmcore for you.
The log content is provided below. Analyze it DIRECTLY — do NOT describe
how you would use MCP or crash tools, because the data is already in front of you.

Your task:
1. Extract key error messages, warnings, and anomalies from the log
2. Identify timing relationships between events
3. Match log entries to the reported hung task problem
4. Identify which processes are mentioned, what they were doing
5. Provide a data-driven analysis citing specific log lines

OUTPUT: Direct analysis of the log content. Reference specific timestamps,
process names, and error messages from the log."""

                context_info = f"""Kernel log extracted from vmcore:

## Kernel log content (from crash log command)
```
{log_content[:8000]}
```

Analyze the kernel log above, extracting key error information, anomaly patterns, and timing relationships."""

                messages = [
                    SystemMessage(content=log_analysis_prompt),
                    HumanMessage(content=f"User input:\n{user_input}\n\n{context_info}"),
                ]

                response = llm.invoke(messages)
                _write_tool_call_output(output_file, response.content, expert_name)

                return {
                    "expert_results": [_make_tool_result(
                        expert_type=expert_type,
                        expert_name=expert_name,
                        analysis_output=response.content.strip(),
                        status="ok",
                        evidence=evidence,
                        artifacts={
                            "vmcore_path": vmcore_path or "",
                            "vmlinux_path": vmlinux_path or "",
                            "output_file": str(output_file),
                            "raw_log_file": str(raw_log_file),
                        },
                    )],
                }

            except Exception as e:
                error_msg = f"从 vmcore 提取日志失败: {str(e)}"
                _write_tool_call_output(output_file, error_msg, expert_name)
                return {
                    "expert_results": [_make_tool_result(
                        expert_type=expert_type,
                        expert_name=expert_name,
                        analysis_output=error_msg,
                        status="failed",
                        artifacts={
                            "vmcore_path": vmcore_path or "",
                            "vmlinux_path": vmlinux_path or "",
                            "output_file": str(output_file),
                        },
                        errors=[error_msg],
                    )],
                }
            finally:
                if session is not None:
                    release_crash_session(vmcore_path, vmlinux_path)

        else:
            # 没有 vmcore，纯文本分析
            user_content = f"用户输入:\n{user_input}\n\n请基于用户输入中的内核日志信息进行分析。"
            evidence = _parse_log_evidence(user_input)

            response = call_llm_with_display(
                expert_name, "分析中", llm,
                [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
                silent=True,
                output_file=output_file,
            )

            return {
                "expert_results": [_make_tool_result(
                    expert_type=expert_type,
                    expert_name=expert_name,
                    analysis_output=response.content.strip(),
                    status="degraded",
                    evidence=evidence,
                )],
            }

    else:
        # 其他专家类型：纯文本分析
        user_content = f"用户输入:\n{user_input}"

        response = call_llm_with_display(
            expert_name, "分析中", llm,
            [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
            silent=True,
            output_file=output_file,
        )

        return {
            "expert_results": [_make_tool_result(
                expert_type=expert_type,
                expert_name=expert_name,
                analysis_output=response.content.strip(),
                status="degraded",
            )],
        }
