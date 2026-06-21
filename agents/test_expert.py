"""测试专家 agent：根据内核专家给出的复现用例进行问题复现验证。

通过工具调用机制实际执行 QEMU 测试验证，与 tool_expert.py 的 crash_analysis 专家类似，
使用 LangChain StructuredTool 实现 QEMU 测试。
"""

__test__ = False  # LangGraph node module, not pytest tests

from pathlib import Path
import os

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agents.contracts import TestPlan, model_to_dict
from agents.llm_display import call_llm_with_persistence, call_llm_with_display, get_expert_output_file, ensure_output_dir, _format_agent_header_text, _format_agent_footer_text
from agents.qemu_tools import create_qemu_tools
from agents.test_runner import run_qemu_test_plan
from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def _extract_kernel_path(user_input: str) -> str | None:
    """从用户输入中提取可启动 kernel 文件路径，最后才回退 vmlinux。

    Returns:
        kernel_path 或 None
    """
    import re

    # Prefer bootable image labels over vmlinux, which is usually debug symbols.
    labels = ["boot_kernel", "boot kernel", "bzImage", "Image", "kernel", "vmlinux"]
    for label in labels:
        label_pattern = re.escape(label).replace(r"\ ", r"\s+")
        patterns = [
            rf'{label_pattern}\s*(?:文件|file|path)?\s*[：:]\s*([~/][^\s]+)',
            rf'{label_pattern}\s+[：:]?\s*([~/][^\s]+)',
        ]
        for pattern in patterns:
            match = re.search(pattern, user_input, re.IGNORECASE)
            if match:
                return match.group(1)

    return None


def _extract_target_arch(user_input: str) -> str:
    """Extract target architecture from user input.

    Return an empty string when unknown. The deterministic runner should not
    silently guess an architecture because the wrong QEMU binary can turn a
    contract error into a misleading test failure.
    """
    import re
    text = user_input.lower()
    if re.search(r"\b(aarch64|arm64)\b", text):
        return "arm64"
    if re.search(r"\b(arm32|armv7|armhf)\b", text):
        return "arm32"
    if re.search(r"\b(x86_64|amd64|x64)\b", text):
        return "x86_64"
    return ""


def _normalize_target_arch(arch: str | None) -> str:
    value = (arch or "").lower()
    aliases = {
        "x86": "x86_64",
        "x64": "x86_64",
        "amd64": "x86_64",
        "aarch64": "arm64",
        "arm": "arm32",
        "armv7": "arm32",
        "armhf": "arm32",
    }
    return aliases.get(value, value)


def _check_file_exists(path: str | None) -> bool:
    """Check if a file exists."""
    if path is None:
        return False
    expanded_path = os.path.expanduser(path)
    return os.path.exists(expanded_path)


def _detect_kernel_type(kernel_path: str) -> str:
    """Detect whether a kernel image is bootable (bzImage) or ELF debug symbols.

    Returns 'elf', 'bzimage', or 'unknown'.
    """
    try:
        with open(kernel_path, "rb") as f:
            header = f.read(4)
        if header[:4] == b"\x7fELF":
            return "elf"
        if header[:2] == b"MZ":
            # bzImage starts with a DOS MZ header (the 16-bit setup stub)
            return "bzimage"
        # Check for raw bzImage without setup header (starts with HdrS)
        if header == b"HdrS":
            return "bzimage"
        return "unknown"
    except Exception:
        return "unknown"


def _log_tool_call(output_file: str, tool_name: str, tool_args: dict, expert_name: str):
    """Log tool execution to output file."""
    args_str = ", ".join(f"{k}={v}" for k, v in tool_args.items()) if tool_args else ""
    with open(output_file, "a", encoding="utf-8") as f:
        f.write(f"\n[{expert_name}] 执行工具: {tool_name}({args_str})\n")
        f.write("等待输出...\n")


def _write_tool_call_output(output_file: str, content: str, expert_name: str):
    """Write final tool-calling output to file, preserving tool call logs."""
    footer = _format_agent_footer_text(expert_name)

    with open(output_file, "a", encoding="utf-8") as f:
        # Append final result after tool call logs
        f.write("\n\n## 最终结果\n\n")
        f.write(content + "\n")
        f.write(footer)


def _format_runner_result(result) -> str:
    """Format deterministic runner output for human-facing reports."""
    lines = [
        f"QEMU TEST STATUS: {result.status}",
        f"CODE: {result.code}",
        f"TEST PASSED: {result.test_passed}",
        f"SUMMARY: {result.summary}",
        "",
        "## Test Plan",
        f"- Target arch: {result.plan.target_arch or 'N/A'}",
        f"- Boot kernel: {result.plan.boot_kernel_path or 'N/A'}",
        f"- Reproducer dir: {result.plan.reproducer_dir or 'N/A'}",
        f"- Reproducer module: {result.plan.reproducer_module_path or 'N/A'}",
        f"- Test script: {result.plan.test_script_path or 'N/A'}",
        f"- Expected signal: {result.plan.expected_signal or 'N/A'}",
        "",
        "## Steps",
    ]
    for step in result.steps:
        lines.append(f"- {step.name}: {step.status} - {step.message}")
        if step.artifacts:
            artifact_text = ", ".join(f"{k}={v}" for k, v in step.artifacts.items())
            lines.append(f"  artifacts: {artifact_text}")
        if step.error:
            lines.append(f"  error: {step.error[:500]}")
    if result.artifacts:
        lines.extend(["", "## Artifacts"])
        for key, value in result.artifacts.items():
            lines.append(f"- {key}: {value}")
    return "\n".join(lines)


def _run_qemu_test_with_tools(
    llm,
    system_prompt: str,
    user_input: str,
    reproduce_case: str,
    kernel_diagnosis: str,
    kernel_path: str | None,
    target_arch: str,
    test_script_path: str | None,
    reproducer_dir: str | None,
    reproducer_module_path: str | None,
    expected_signal: str | None,
    expert_name: str,
    output_file: str,
    current_attempts: int,
    max_attempts: int,
    max_iterations: int = 10,
) -> AIMessage:
    """Execute QEMU test verification with tool calling.

    Creates QEMU tools, binds to LLM, runs tool-calling loop,
    and returns final AIMessage with verification result.
    """
    # Write initial header
    header = _format_agent_header_text(expert_name, f"验证中 (工具调用 - 第{current_attempts}次)")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(f"执行模式: real (QEMU 工具调用)\n")
        f.write(f"验证次数: {current_attempts}/{max_attempts}\n\n")

    try:
        # Create QEMU tools
        qemu_tools = create_qemu_tools()

        # Build context info for LLM
        kernel_info = ""
        if kernel_path:
            kernel_expanded = os.path.expanduser(kernel_path)
            if os.path.exists(kernel_expanded):
                kernel_info = f"- Kernel: {kernel_path} (✓ 存在)\n"
            else:
                kernel_info = f"- Kernel: {kernel_path} (✗ 不存在)\n"
        else:
            kernel_info = "- Kernel: 未指定 (需要用户提供)\n"

        test_script_info = ""
        if test_script_path:
            test_script_expanded = os.path.expanduser(test_script_path)
            if os.path.exists(test_script_expanded):
                test_script_info = f"- Test script: {test_script_path} (✓ 存在)\n"
            else:
                test_script_info = f"- Test script: {test_script_path} (✗ 不存在)\n"
        else:
            test_script_info = "- Test script: 未提供\n"

        modules_dir = reproducer_dir or ""
        if not modules_dir and reproducer_module_path:
            modules_dir = str(Path(os.path.expanduser(reproducer_module_path)).parent)

        home_dir = os.path.expanduser("~")
        context_info = f"""QEMU test verification environment:

- Home directory: {home_dir} (use this in paths, NOT /root)
{kernel_info}
{test_script_info}
- Target arch: {target_arch}
- Modules dir: {modules_dir or '未提供'}
- Reproducer module: {reproducer_module_path or '未提供'}
- Expected signal: {expected_signal or '未提供'}

You MUST use the following QEMU testing tools that are bound to you:
- check_qemu_available: Check if QEMU is installed. Call this FIRST and ONLY ONCE.
- create_initramfs: Create minimal initramfs for testing. Call ONCE and reuse the path.
- boot_kernel: Boot a kernel in QEMU and capture boot log. THIS IS THE KEY TOOL.
- analyze_boot_log: Analyze QEMU boot log for errors and patterns. Call ONCE per log.

You are in REAL execution mode. These tools execute actual commands and return real results.
Do NOT say you "cannot execute QEMU" — you CAN, by calling these tools.

EFFICIENCY RULES:
- Call each tool ONCE with the correct arguments. Do NOT retry with different args.
- Use the initramfs path returned by create_initramfs() directly in boot_kernel().
- If boot_kernel fails, check whether the kernel is a bootable bzImage or an ELF
  vmlinux (debug symbols only, not bootable). QEMU requires bzImage for x86_64.
- If the kernel is ELF vmlinux, report REPRODUCE: FAILED immediately — do not
  retry multiple times. Explain that a bootable bzImage is needed.
- Do NOT call create_initramfs more than once — use the first result.

REQUIRED execution flow:
1. Call check_qemu_available(arch="{target_arch}") to verify QEMU
2. Call create_initramfs(arch="{target_arch}") ONCE. If Test script exists, pass test_script_path={test_script_path or 'N/A'}.
   If Modules dir exists, pass modules_dir={modules_dir or 'N/A'} so the .ko is included in /modules.
3. Call boot_kernel() with arch="{target_arch}", kernel_path={kernel_path or 'N/A'} and the initramfs path
4. Call analyze_boot_log() on the resulting log
5. Based on actual tool outputs and Expected signal, determine if issue reproduced
"""

        user_content = f"""User input:
{user_input}

## Kernel expert reproducer case
{reproduce_case}

## Kernel diagnosis plan
{kernel_diagnosis}

## Structured test contract
- Target arch: {target_arch}
- Boot kernel path: {kernel_path or 'N/A'}
- Reproducer dir: {reproducer_dir or 'N/A'}
- Reproducer module path: {reproducer_module_path or 'N/A'}
- Test script path: {test_script_path or 'N/A'}
- Expected signal: {expected_signal or 'N/A'}

## Execution mode
Mode: **real** — you have real QEMU tools bound for execution.
Attempt: {current_attempts}/{max_attempts}

CRITICAL: You MUST call at least check_qemu_available() as your very first action.
Do not reply with text about what you "would" do — actually call the tools.
Do not say you "cannot execute" — the tools give you that ability."""

        # Create messages for tool-calling loop
        messages = create_tool_call_messages(
            system_prompt=system_prompt,
            user_input=user_content,
            context_info=context_info,
        )

        # Execute tool-calling loop
        response = execute_tool_calling_loop(
            llm=llm,
            messages=messages,
            tools=qemu_tools,
            max_iterations=max_iterations,
            on_tool_call=lambda name, args: _log_tool_call(output_file, name, args, expert_name),
            verbose=False,
        )

        # Check if any tools were actually called by checking for ToolMessage in messages
        from langchain_core.messages import ToolMessage as TM
        has_tool_calls = any(isinstance(m, TM) for m in messages)

        if not has_tool_calls:
            # LLM did not call any tools — force execute check_qemu_available
            # and inject result as HumanMessage (NOT ToolMessage, which would
            # require a matching AIMessage tool_calls that the API would reject)
            from agents.qemu_tools import check_qemu_available as check_qemu
            qemu_status = check_qemu(target_arch)

            force_msg = HumanMessage(content=f"""QEMU availability was checked automatically:

{qemu_status}

Based on this result, you MUST now proceed with the verification by calling the tools.
If QEMU is available, use create_initramfs(arch="{target_arch}", test_script_path="{test_script_path or ''}", modules_dir="{modules_dir or ''}") then boot_kernel(arch="{target_arch}") to test the kernel.
If QEMU is not available, report REPRODUCE: FAILED with the reason.
Do not describe what you would do — call the tools.""")
            messages.append(force_msg)

            response = execute_tool_calling_loop(
                llm=llm,
                messages=messages,
                tools=qemu_tools,
                max_iterations=max_iterations,
                on_tool_call=lambda name, args: _log_tool_call(output_file, name, args, expert_name),
                verbose=False,
            )

        # Check response quality — if empty or short, force a summary
        content = response.content or ""
        tool_calls_remaining = getattr(response, "tool_calls", None) or []

        needs_summary = (
            len(content) < 200
            or bool(tool_calls_remaining)
            or not content.strip()
            # Detect when LLM returns raw tool-call XML instead of analysis
            or "</invoke>" in content
            or "<｜｜DSML｜｜tool_calls>" in content
        )

        if needs_summary:
            summary_messages = list(messages) + [
                HumanMessage(content="""Based on the QEMU tool results above, generate a verification summary.

Report in this format:
- QEMU status: (available or not)
- Tools executed: (which tools were called and their results)
- Verification result: REPRODUCE: SUCCESS if kernel booted and issue was reproduced, or REPRODUCE: FAILED with reason
- Key findings: (specific observations from tool outputs)
- Recommendations: (next steps if verification failed)

Be CONCISE and reference actual tool output data."""),
            ]
            summary_response = llm.invoke(summary_messages)
            _write_tool_call_output(output_file, summary_response.content, expert_name)
            return summary_response

        # Write final output
        _write_tool_call_output(output_file, response.content, expert_name)

        return response

    except Exception as e:
        # Execution failed
        error_msg = f"QEMU 工具调用执行失败: {str(e)}"
        _write_tool_call_output(output_file, error_msg, expert_name)
        return AIMessage(content=error_msg)


def test_expert_node(state: MaintenanceWorkflowState) -> dict:
    """测试专家 agent：根据内核专家给出的复现用例进行问题复现验证。

    通过工具调用机制实际执行 QEMU 测试验证。
    """
    config = state.get("config", {})
    current_attempts = state.get("test_attempts", 0) + 1
    max_attempts = config.get("workflow", {}).get("max_test_attempts", 3)

    # 确保输出目录存在
    ensure_output_dir()
    output_file = get_expert_output_file("test_expert")

    artifacts = state.get("input_artifacts_contract") or {}
    kernel_contract = state.get("kernel_contract") or {}

    # 提取 kernel 路径：优先使用内核专家产出的可启动内核路径，再回退统一输入解析结果和旧文本解析。
    kernel_path = (
        kernel_contract.get("boot_kernel_path")
        or state.get("boot_kernel_path")
        or artifacts.get("boot_kernel_path")
        or _extract_kernel_path(state.get("user_input", ""))
    )
    target_arch = _normalize_target_arch(
        kernel_contract.get("target_arch")
        or state.get("target_arch")
        or artifacts.get("target_arch")
        or _extract_target_arch(state.get("user_input", ""))
    )
    test_script_path = kernel_contract.get("test_script_path") or state.get("test_script_path", "")
    reproducer_dir = kernel_contract.get("reproducer_dir") or state.get("reproducer_dir", "")
    reproducer_module_path = kernel_contract.get("reproducer_module_path") or state.get("reproducer_module_path", "")
    expected_signal = kernel_contract.get("expected_signal") or state.get("expected_signal", "")

    # Deterministic QEMU execution path. The LLM is no longer responsible for
    # choosing or ordering QEMU tools.
    plan = TestPlan(
        target_arch=target_arch,
        boot_kernel_path=kernel_path or "",
        reproducer_dir=reproducer_dir,
        reproducer_module_path=reproducer_module_path,
        test_script_path=test_script_path,
        expected_signal=expected_signal,
    )
    runner_result = run_qemu_test_plan(plan, attempt=current_attempts)
    text = _format_runner_result(runner_result)

    header = _format_agent_header_text("测试专家", f"确定性验证 - 第{current_attempts}次")
    footer = _format_agent_footer_text("测试专家")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(text + "\n")
        f.write(footer)

    terminal_codes = {
        "BLOCKED_NO_BOOT_KERNEL",
        "BLOCKED_BOOT_KERNEL_MISSING",
        "BLOCKED_NOT_BOOTABLE_KERNEL",
        "SKIPPED_QEMU_MISSING",
    }
    recorded_attempts = max_attempts if runner_result.code in terminal_codes else current_attempts
    result = {
        "test_result": text,
        "test_passed": runner_result.test_passed,
        "test_attempts": recorded_attempts,
        "test_contract": model_to_dict(runner_result),
    }

    # 超过最大尝试次数且未复现，生成最终建议
    if recorded_attempts >= max_attempts and not runner_result.test_passed:
        if runner_result.status in {"blocked", "skipped"}:
            result["final_response"] = text
        else:
            improvement_suggestions = _generate_improvement_suggestions(state, text)
            result["final_response"] = (
                f"问题复现验证已达到最大尝试次数（{max_attempts} 次），未能成功复现。\n\n"
                f"## 已有分析\n"
                f"- 工具专家分析: {len(state.get('expert_results', []))} 项\n"
                f"- 内核专家分析: 已完成\n"
                f"- 测试验证: {max_attempts} 次均未成功复现\n\n"
                f"## 改进建议\n{improvement_suggestions}"
            )

    return result


def _generate_improvement_suggestions(state: MaintenanceWorkflowState, test_result: str) -> str:
    """Generate improvement suggestions based on analysis results."""
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("test_expert", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="test_expert")

    # Use a focused system prompt for improvement suggestions, not the full skill workflow
    improvement_prompt = """You are a kernel testing expert. Based on the analysis and test results provided,
generate CONCISE improvement suggestions. Focus on actionable recommendations.

Output format:
1. Environment: missing configs or conditions
2. Information: what additional data is needed
3. Analysis: alternative approaches to try
4. Debugging: additional instrumentation to add

Keep each section to 2-3 bullet points. Be specific and actionable. Do NOT describe what you "would do" - just give the recommendations."""

    expert_results = state.get("expert_results", [])
    expert_summaries = []
    for result in expert_results:
        expert_summaries.append(
            f"### {result['expert_name']}（{result['expert_type']}）\n{result['analysis_output'][:2000]}"
        )

    user_content = (
        f"Original problem:\n{state['user_input'][:500]}\n\n"
        f"## Expert analysis\n" + "\n\n".join(expert_summaries) + "\n\n"
        f"## Kernel expert analysis (summary)\n{state.get('kernel_analysis', '')[:2000]}\n\n"
        f"## Last test result\n{test_result[:2000]}\n\n"
        f"The issue could not be reproduced after multiple attempts. "
        f"Give concise, actionable improvement suggestions."
    )

    response = call_llm_with_persistence(
        "测试专家", "生成改进建议", llm,
        [SystemMessage(content=improvement_prompt), HumanMessage(content=user_content)],
        persist_dir=Path("outputs"),
    )

    return response.content.strip()
