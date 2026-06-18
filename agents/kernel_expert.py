from pathlib import Path
import os

from langchain_core.messages import HumanMessage, SystemMessage, AIMessage

from agents.llm_display import call_llm_with_persistence, display_expert_outputs, get_expert_output_file, ensure_output_dir, _format_agent_header_text, _format_agent_footer_text
from agents.kernel_tools import create_kernel_tools
from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def _log_tool_call(output_file: str, tool_name: str, tool_args: dict, expert_name: str):
    """Log tool execution to output file."""
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


def _run_kernel_expert_with_tools(
    llm,
    system_prompt: str,
    user_content: str,
    expert_name: str,
    output_file: str,
    max_iterations: int = 15,
) -> AIMessage:
    """Execute kernel expert analysis with tool calling.

    Creates kernel tools, binds to LLM, runs tool-calling loop,
    and returns final AIMessage with analysis result.
    """
    # Write initial header
    header = _format_agent_header_text(expert_name, "分析构造用例（工具调用）")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
        f.write("执行模式: real (文件操作和编译工具)\n\n")

    try:
        # Create kernel tools
        tools = create_kernel_tools()

        # Build context info for LLM
        kernel_headers_path = f"/lib/modules/{os.uname().release}/build"
        kernel_headers_exist = os.path.exists(kernel_headers_path)

        context_info = f"""内核专家工具环境:

- Kernel Headers: {kernel_headers_path} ({'✓ 存在' if kernel_headers_exist else '✗ 不存在'})
- 当前内核版本: {os.uname().release}
- 架构: {os.uname().machine}

你拥有以下文件操作和编译工具:
- create_directory: 创建目录（用于创建复现器目录）
- write_file: 写入文件内容（用于创建源代码、Makefile、README等）
- read_file: 读取文件内容（用于检查已创建的文件）
- compile_module: 编译内核模块（用于验证复现器代码）
- check_file_exists: 检查文件是否存在（用于验证文件创建）

建议执行流程:
1. 分析问题根因，确定复现策略
2. 使用 create_directory 创建复现器目录（建议: outputs/<bug_type>_reproducer）
3. 使用 write_file 创建复现器源代码（.c 文件）
4. 使用 write_file 创建 Makefile
5. 使用 write_file 创建 README.md（使用说明）
6. 使用 compile_module 尝试编译验证（如果 kernel headers 存在）
7. 如果编译成功，说明复现器代码正确；如果失败，分析错误并修正代码
8. 输出最终的 REPRODUCE_CASE 和 KERNEL_DIAGNOSIS

注意事项:
- 源代码必须使用正确的内核 API（如 DECLARE_RWSEM 而不是 DEFINE_RWSEM）
- Makefile 必须使用正确的格式（Tab 缩进，不是空格）
- 编译失败时，分析错误信息并修正代码后重新编译
"""

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
            tools=tools,
            max_iterations=max_iterations,
            on_tool_call=lambda name, args: _log_tool_call(output_file, name, args, expert_name),
            verbose=False,
        )

        # Write final output
        _write_tool_call_output(output_file, response.content, expert_name)

        return response

    except Exception as e:
        # Execution failed
        error_msg = f"工具调用执行失败: {str(e)}"
        _write_tool_call_output(output_file, error_msg, expert_name)
        return AIMessage(content=error_msg)


def kernel_expert_node(state: MaintenanceWorkflowState) -> dict:
    """内核专家 agent：根据工具专家的输出，结合代码分析，构造必现用例并给出内核维测方案。

    支持两种执行模式：
    - simulation: 纯文本分析，由 LLM 描述复现策略（无实际文件创建）
    - real: 实际执行工具调用（创建文件、编译验证）

    real 模式使用工具调用机制，能够：
    - 实际创建目录和文件
    - 实际编译内核模块
    - 验证复现器代码正确性
    """
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("kernel_expert", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="kernel_expert")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/kernel_expert.md")
    )

    # 获取执行模式（默认为 real）
    execution_mode = state.get("execution_mode", "real")

    # 汇总所有工具专家的分析结果
    expert_results = state.get("expert_results", [])

    # 统一展示所有工具专家的输出（从文件读取）
    display_expert_outputs(expert_results)
    expert_summaries = []
    for result in expert_results:
        expert_summaries.append(
            f"### {result['expert_name']}（{result['expert_type']}）\n{result['analysis_output']}"
        )

    user_content = (
        f"用户输入:\n{state['user_input']}\n\n"
        f"## 工具专家分析结果\n" + "\n\n".join(expert_summaries)
    )

    # 如果是重试（测试未通过），附加测试反馈
    test_result = state.get("test_result", "")
    if test_result:
        user_content += f"\n\n## 上次测试结果（未成功复现）\n{test_result}\n请重新分析并调整复现用例。"

    # 确保输出目录存在
    ensure_output_dir()
    output_file = get_expert_output_file("kernel_expert")

    if execution_mode == "real":
        # === 工具调用路径 ===
        # 检查 kernel headers 是否存在
        kernel_headers_path = f"/lib/modules/{os.uname().release}/build"
        kernel_headers_exist = os.path.exists(kernel_headers_path)

        if not kernel_headers_exist:
            # kernel headers 不存在 → 降级为文本分析
            kernel_status = f"Kernel Headers: {kernel_headers_path} (✗ 不存在)"

            user_content_with_warning = user_content + f"""

## 执行模式
当前执行模式: **real** (实际文件操作)
⚠️ 注意: {kernel_status}，无法编译验证模块。

请创建复现器代码文件，但注意无法进行编译验证。"""

            response = call_llm_with_persistence(
                "内核专家", "分析构造用例（降级模式）", llm,
                [SystemMessage(content=system_prompt), HumanMessage(content=user_content_with_warning)],
                persist_dir=Path("outputs"),
            )
        else:
            # 执行工具调用
            response = _run_kernel_expert_with_tools(
                llm=llm,
                system_prompt=system_prompt,
                user_content=user_content,
                expert_name="内核专家",
                output_file=output_file,
                max_iterations=15,
            )
    else:
        # === 纯文本分析路径 ===
        user_content += f"\n\n## 执行模式\n当前执行模式: **{execution_mode}** (纯文本分析)\n\n"
        user_content += "请描述复现策略和代码设计思路，不需要实际创建文件。"

        response = call_llm_with_persistence(
            "内核专家", "分析构造用例", llm,
            [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
            persist_dir=Path("outputs"),
        )

    text = response.content.strip()

    # 解析必现用例和维测方案
    reproduce_case = _extract_section(text, "REPRODUCE_CASE")
    kernel_diagnosis = _extract_section(text, "KERNEL_DIAGNOSIS")

    return {
        "kernel_analysis": text,
        "reproduce_case": reproduce_case or text,
        "kernel_diagnosis": kernel_diagnosis or "",
    }


def _extract_section(text: str, marker: str) -> str:
    """从文本中提取标记段落。"""
    import re
    pattern = rf"{re.escape(marker)}:\s*\n?(.*?)(?:\n[A-Z_]+:|\Z)"
    match = re.search(pattern, text, re.DOTALL)
    return match.group(1).strip() if match else ""
