"""测试专家 agent：根据内核专家给出的复现用例进行问题复现验证。

通过工具调用机制实际执行 QEMU 测试验证，与 tool_expert.py 的 crash_analysis 专家类似，
使用 LangChain StructuredTool 实现 QEMU 测试。
"""

from pathlib import Path
import os

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agents.llm_display import call_llm_with_persistence, call_llm_with_display, get_expert_output_file, ensure_output_dir, _format_agent_header_text, _format_agent_footer_text
from agents.qemu_tools import create_qemu_tools
from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def _extract_kernel_path(user_input: str) -> str | None:
    """从用户输入中提取 vmlinux/kernel 文件路径。

    Returns:
        kernel_path 或 None
    """
    import re

    # Match patterns like "vmlinux 文件: ~/path" or "vmlinux 文件：~/path"
    # The colon may or may not have spaces around it; full-width colon (：) also supported
    kernel_pattern = r'(?:vmlinux|kernel|Image)\s*文件[：:\s]+([~/][^\s]+)'
    match = re.search(kernel_pattern, user_input, re.IGNORECASE)

    if match:
        return match.group(1)

    # 备用模式：不带"文件"关键词，但路径必须以 / 或 ~ 开头
    kernel_pattern2 = r'(?:vmlinux|kernel|Image)\s+[：:]?\s*([~/][^\s]+)'
    match2 = re.search(kernel_pattern2, user_input, re.IGNORECASE)

    return match2.group(1) if match2 else None


def _check_file_exists(path: str | None) -> bool:
    """检查文件是否存在。"""
    if path is None:
        return False
    expanded_path = os.path.expanduser(path)
    return os.path.exists(expanded_path)


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


def _run_qemu_test_with_tools(
    llm,
    system_prompt: str,
    user_input: str,
    reproduce_case: str,
    kernel_diagnosis: str,
    kernel_path: str | None,
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

        context_info = f"""QEMU test verification environment:

{kernel_info}

You MUST use the following QEMU testing tools that are bound to you:
- check_qemu_available: Check if QEMU is installed. Call this FIRST.
- create_initramfs: Create minimal initramfs for testing.
- boot_kernel: Boot a kernel in QEMU and capture boot log. THIS IS THE KEY TOOL.
- analyze_boot_log: Analyze QEMU boot log for errors and patterns.

You are in REAL execution mode. These tools execute actual commands and return real results.
Do NOT say you "cannot execute QEMU" — you CAN, by calling these tools.
Do NOT skip tool calls — the tools are your only way to produce a valid result.

REQUIRED execution flow:
1. Call check_qemu_available() to verify QEMU is installed
2. Use kernel_path={kernel_path or 'N/A'} and create an initramfs with create_initramfs()
3. Call boot_kernel() with the kernel and initramfs
4. Call analyze_boot_log() on the resulting log
5. Based on actual tool outputs, determine if issue reproduced
"""

        user_content = f"""User input:
{user_input}

## Kernel expert reproducer case
{reproduce_case}

## Kernel diagnosis plan
{kernel_diagnosis}

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
            qemu_status = check_qemu("x86_64")

            force_msg = HumanMessage(content=f"""QEMU availability was checked automatically:

{qemu_status}

Based on this result, you MUST now proceed with the verification by calling the tools.
If QEMU is available, use create_initramfs() then boot_kernel() to test the kernel.
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
    agent_config = config.get("agents", {}).get("test_expert", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="test_expert")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/test_expert.md")
    )

    current_attempts = state.get("test_attempts", 0) + 1
    max_attempts = config.get("workflow", {}).get("max_test_attempts", 3)
    is_last_attempt = current_attempts >= max_attempts

    # 确保输出目录存在
    ensure_output_dir()
    output_file = get_expert_output_file("test_expert")

    # 提取 kernel 路径
    kernel_path = _extract_kernel_path(state.get("user_input", ""))
    kernel_exists = _check_file_exists(kernel_path)

    # kernel 不存在时直接报错
    if not kernel_path or not kernel_exists:
        error_msg = f"ERROR: Kernel 文件不存在或未指定\n"
        error_msg += f"Kernel 路径: {kernel_path or '未指定'}\n"
        error_msg += f"状态: {'✗ 不存在' if kernel_path else '未提供'}\n\n"
        error_msg += "请提供有效的 kernel/vmlinux/Image 文件路径以执行 QEMU 测试验证。\n"
        error_msg += "示例格式: vmlinux: /path/to/vmlinux"

        header = _format_agent_header_text("测试专家", "验证失败")
        footer = _format_agent_footer_text("测试专家")
        with open(output_file, "w", encoding="utf-8") as f:
            f.write(header)
            f.write(error_msg + "\n")
            f.write(footer)

        # 解析为失败
        return {
            "test_result": error_msg,
            "test_passed": False,
            "test_attempts": current_attempts,
            "final_response": error_msg,
        }

    # 执行 QEMU 工具调用测试
    response = _run_qemu_test_with_tools(
        llm=llm,
        system_prompt=system_prompt,
        user_input=state.get("user_input", ""),
        reproduce_case=state.get("reproduce_case", ""),
        kernel_diagnosis=state.get("kernel_diagnosis", ""),
        kernel_path=kernel_path,
        expert_name="测试专家",
        output_file=output_file,
        current_attempts=current_attempts,
        max_attempts=max_attempts,
        max_iterations=10,
    )

    text = response.content.strip()

    # 解析测试结果
    test_passed = "REPRODUCE: SUCCESS" in text or "复现成功" in text or "✓ TEST PASSED" in text

    result = {
        "test_result": text,
        "test_passed": test_passed,
        "test_attempts": current_attempts,
    }

    # 超过最大尝试次数且未复现，生成最终建议
    if is_last_attempt and not test_passed:
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