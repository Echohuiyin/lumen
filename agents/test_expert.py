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

    # 支持中文冒号和英文冒号，以及 ~ 开头的路径
    kernel_pattern = r'(?:vmlinux|kernel|Image)\s*(?:文件)?[：:\s]+([~/\w\-\.]+)'
    match = re.search(kernel_pattern, user_input, re.IGNORECASE)

    return match.group(1) if match else None


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
    """Write final tool-calling output to file."""
    header = _format_agent_header_text(expert_name, "验证完成")
    footer = _format_agent_footer_text(expert_name)

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
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

        context_info = f"""QEMU 测试验证环境:

{kernel_info}

你拥有以下 QEMU 测试工具:
- check_qemu_available: 检查 QEMU 是否安装及版本信息
- create_initramfs: 创建测试所需的 initramfs
- boot_kernel: 在 QEMU 中启动内核并捕获日志
- analyze_boot_log: 分析启动日志，检测 panic 和错误

建议执行流程:
1. 首先调用 check_qemu_available 验证 QEMU 环境
2. 根据 kernel_path 和 reproduce_case 创建测试脚本和 initramfs
3. 调用 boot_kernel 启动测试
4. 调用 analyze_boot_log 分析结果
5. 判断是否成功复现问题
"""

        user_content = f"""用户输入:
{user_input}

## 内核专家构造的复现用例
{reproduce_case}

## 内核维测方案
{kernel_diagnosis}

## 执行模式
当前执行模式: **real** (实际 QEMU 测试验证)
验证次数: 第 {current_attempts} 次（共 {max_attempts} 次机会）

请使用 QEMU 工具执行实际的验证测试。"""

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
    """根据已有分析结果生成改进建议。"""
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("test_expert", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="test_expert")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/test_expert.md")
    )

    expert_results = state.get("expert_results", [])
    expert_summaries = []
    for result in expert_results:
        expert_summaries.append(
            f"### {result['expert_name']}（{result['expert_type']}）\n{result['analysis_output']}"
        )

    user_content = (
        f"用户原始输入:\n{state['user_input']}\n\n"
        f"## 工具专家分析结果\n" + "\n\n".join(expert_summaries) + "\n\n"
        f"## 内核专家分析\n{state.get('kernel_analysis', '')}\n\n"
        f"## 复现用例\n{state.get('reproduce_case', '')}\n\n"
        f"## 最后一次测试结果\n{test_result}\n\n"
        f"经过多次尝试仍无法复现该问题。请从以下角度给出详细的改进建议：\n"
        f"1. 环境方面：可能缺少哪些环境条件或配置\n"
        f"2. 信息方面：还需要补充哪些信息才能更好地定位问题\n"
        f"3. 分析思路：建议调整哪些分析方向或尝试其他方法\n"
        f"4. 维测方案：建议添加哪些额外的调试手段"
    )

    response = call_llm_with_persistence(
        "测试专家", "生成改进建议", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
        persist_dir=Path("outputs"),
    )

    return response.content.strip()