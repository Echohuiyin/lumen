"""工具专家 agent：根据 expert_type 执行对应的专业分析。

支持的知识库搜索专家现在会实际执行 RAG 检索，而非仅输出命令。
支持 crash_analysis/lock_analysis 专家使用工具调用执行 crash 命令。
使用静默模式执行，输出写入独立文件，避免并行输出交错。
"""

import os
from langchain_core.messages import AIMessage, HumanMessage, SystemMessage, ToolMessage

from agents.llm_display import call_llm_with_display, get_expert_output_file, ensure_output_dir, _format_agent_header_text, _format_agent_footer_text
from agents.rag_integration import get_rag_context_for_query
from config import get_llm_with_config, load_prompt_from_file, create_crash_session
from graph.rn_state import MaintenanceWorkflowState, ToolExpertResult


def _extract_vmcore_paths(user_input: str) -> tuple[str | None, str | None]:
    """从用户输入中提取 vmcore 和 vmlinux 文件路径。

    Returns:
        (vmcore_path, vmlinux_path) 或 (None, None)
    """
    import re

    # 支持中文冒号 `：` 和英文冒号 `:`, 以及 `~` 开头的路径
    vmcore_pattern = r'vmcore\s*(?:文件)?[：:\s]+([~/\w\-\.]+)'
    vmlinux_pattern = r'vmlinux\s*(?:文件)?[：:\s]+([~/\w\-\.]+)'

    vmcore_match = re.search(vmcore_pattern, user_input, re.IGNORECASE)
    vmlinux_match = re.search(vmlinux_pattern, user_input, re.IGNORECASE)

    vmcore_path = vmcore_match.group(1) if vmcore_match else None
    vmlinux_path = vmlinux_match.group(1) if vmlinux_match else None

    return vmcore_path, vmlinux_path


def _check_file_exists(path: str | None) -> bool:
    """检查文件是否存在（用于 MCP 工具智能判断）。

    支持 ~ 符号展开，并尝试 vmcore.elf 后缀作为备选。
    """
    if path is None:
        return False
    expanded_path = os.path.expanduser(path)
    if os.path.exists(expanded_path):
        return True
    # 尝试添加 .elf 后缀（vmcore 文件常见格式）
    if expanded_path.endswith('vmcore') and not expanded_path.endswith('.elf'):
        elf_path = expanded_path + '.elf'
        if os.path.exists(elf_path):
            return True
    return False


def _resolve_vmcore_path(path: str | None) -> str | None:
    """解析 vmcore 路径，处理 ~ 展开和 .elf 后缀。

    Returns:
        实际存在的路径，或 None
    """
    if path is None:
        return None
    expanded_path = os.path.expanduser(path)
    if os.path.exists(expanded_path):
        return expanded_path
    # 尝试添加 .elf 后缀
    if expanded_path.endswith('vmcore') and not expanded_path.endswith('.elf'):
        elf_path = expanded_path + '.elf'
        if os.path.exists(elf_path):
            return elf_path
    return expanded_path  # 返回展开后的路径，即使不存在


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


def _run_tool_calling_analysis(
    llm,
    system_prompt: str,
    user_input: str,
    vmcore_path: str,
    vmlinux_path: str,
    expert_name: str,
    output_file: str,
    max_iterations: int = 15,
) -> AIMessage:
    """Execute crash analysis with tool calling.

    Creates crash session, binds tools to LLM, runs tool-calling loop,
    and returns final AIMessage with analysis.
    """
    from agents.crash_tools import create_crash_tools
    from agents.tool_calling_loop import execute_tool_calling_loop, create_tool_call_messages

    # Write initial header
    header = _format_agent_header_text(expert_name, "分析中 (工具调用)")
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(f"Crash Session: {vmcore_path}\n")
        f.write(f"Vmlinux: {vmlinux_path}\n\n")

    session = None
    try:
        # Create crash session
        session = create_crash_session(vmcore_path, vmlinux_path)

        # Create session-bound tools
        crash_tools = create_crash_tools(session)

        # Build context info for LLM
        context_info = f"""Crash 分析环境已就绪:
- vmcore: {vmcore_path}
- vmlinux: {vmlinux_path}

你拥有以下 crash 分析工具:
- run_crash_command: 执行单个 crash 命令 (如 'bt', 'sys', 'log')
- run_crash_commands: 执行多个命令批量收集信息
- collect_baseline: 收集基线诊断信息 (sys + bt + log)

请首先调用 collect_baseline 收集基本信息，然后根据需要执行其他命令进行深入分析。"""

        # Create messages for tool-calling loop
        messages = create_tool_call_messages(
            system_prompt=system_prompt,
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

        # 检查内容是否是描述性而非分析性
        if any(phrase in content for phrase in ["让我开始", "我需要先", "首先调用", "执行分析流程", "阶段零"]):
            needs_summary = True

        if needs_summary:
            # 强制要求 LLM 生成分析总结
            # 注意：messages 已包含所有 ToolMessage（工具调用结果）
            summary_messages = list(messages) + [
                HumanMessage(content="""基于以上工具调用收集的数据，请立即生成完整的问题分析报告。

要求：
1. 不要再调用任何工具
2. 直接输出分析结论
3. 报告格式：
   - Crash 类型判定
   - 关键调用栈分析
   - 锁持有关系（如有）
   - 根因分析
   - 初步结论""")
            ]
            summary_response = llm.invoke(summary_messages)
            _write_tool_call_output(output_file, summary_response.content, expert_name)
            return summary_response

        # Write final output
        _write_tool_call_output(output_file, response.content, expert_name)

        return response

    except Exception as e:
        # Session creation or execution failed
        error_msg = f"Crash session 执行失败: {str(e)}"
        _write_tool_call_output(output_file, error_msg, expert_name)
        return AIMessage(content=error_msg)

    finally:
        # Ensure session cleanup
        if session is not None:
            try:
                session.stop()
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
            "expert_results": [ToolExpertResult(
                expert_type=expert_type,
                expert_name=expert_type,
                analysis_output=f"未找到类型为 {expert_type} 的工具专家配置。",
            )],
        }

    agent_config = expert_config.get("agent", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config)
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", f"prompts/maintenance/{expert_type}.md")
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
            "expert_results": [ToolExpertResult(
                expert_type=expert_type,
                expert_name=expert_name,
                analysis_output=response.content.strip(),
            )],
        }

    elif expert_type in ("crash_analysis", "lock_analysis"):
        # Crash/锁分析专家：使用工具调用执行 crash 命令
        vmcore_path_raw, vmlinux_path_raw = _extract_vmcore_paths(user_input)

        # 解析路径（展开 ~ 并尝试 .elf 后缀）
        vmcore_path = _resolve_vmcore_path(vmcore_path_raw)
        vmlinux_path = _resolve_vmcore_path(vmlinux_path_raw) if vmlinux_path_raw else None

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
                "expert_results": [ToolExpertResult(
                    expert_type=expert_type,
                    expert_name=expert_name,
                    analysis_output=response.content.strip(),
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
                "expert_results": [ToolExpertResult(
                    expert_type=expert_type,
                    expert_name=expert_name,
                    analysis_output=response.content.strip(),
                )],
            }

        # === 工具调用路径 ===
        # 文件存在，创建 crash session 并执行工具调用循环
        response = _run_tool_calling_analysis(
            llm=llm,
            system_prompt=system_prompt,
            user_input=user_input,
            vmcore_path=vmcore_path,  # 使用展开后的路径
            vmlinux_path=vmlinux_path,  # 使用展开后的路径
            expert_name=expert_name,
            output_file=output_file,
            max_iterations=15,
        )

        return {
            "expert_results": [ToolExpertResult(
                expert_type=expert_type,
                expert_name=expert_name,
                analysis_output=response.content.strip(),
            )],
        }

    elif expert_type == "kernel_log_analysis":
        # 内核日志分析专家：如果有 vmcore，使用 crash 提取日志
        vmcore_path_raw, vmlinux_path_raw = _extract_vmcore_paths(user_input)
        vmcore_path = _resolve_vmcore_path(vmcore_path_raw)
        vmlinux_path = _resolve_vmcore_path(vmlinux_path_raw) if vmlinux_path_raw else None
        vmcore_exists = _check_file_exists(vmcore_path_raw)

        if vmcore_path_raw and vmlinux_path_raw and vmcore_exists:
            # 使用 crash 工具提取内核日志
            header = _format_agent_header_text(expert_name, "分析中 (工具调用)")
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(header)
                f.write(f"Crash Session: {vmcore_path}\n")
                f.write(f"Vmlinux: {vmlinux_path}\n\n")

            try:
                from agents.crash_tools import create_crash_tools
                session = create_crash_session(vmcore_path, vmlinux_path)
                crash_tools = create_crash_tools(session)

                # 执行 log 命令提取内核日志
                log_result = session.run_command("log")
                log_content = log_result.output if log_result.success else "无法提取内核日志"

                # 构建 context，包含提取的日志
                context_info = f"""内核日志已从 vmcore 提取:

## 内核日志内容（来自 crash log 命令）
```
{log_content[:8000]}
```

请分析以上内核日志，提取关键错误信息、异常模式和时序关系。"""

                messages = [
                    SystemMessage(content=system_prompt),
                    HumanMessage(content=f"用户输入:\n{user_input}\n\n{context_info}"),
                ]

                response = llm.invoke(messages)
                _write_tool_call_output(output_file, response.content, expert_name)

                session.stop()

                return {
                    "expert_results": [ToolExpertResult(
                        expert_type=expert_type,
                        expert_name=expert_name,
                        analysis_output=response.content.strip(),
                    )],
                }

            except Exception as e:
                error_msg = f"从 vmcore 提取日志失败: {str(e)}"
                _write_tool_call_output(output_file, error_msg, expert_name)
                return {
                    "expert_results": [ToolExpertResult(
                        expert_type=expert_type,
                        expert_name=expert_name,
                        analysis_output=error_msg,
                    )],
                }

        else:
            # 没有 vmcore，纯文本分析
            user_content = f"用户输入:\n{user_input}\n\n请基于用户输入中的内核日志信息进行分析。"

            response = call_llm_with_display(
                expert_name, "分析中", llm,
                [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
                silent=True,
                output_file=output_file,
            )

            return {
                "expert_results": [ToolExpertResult(
                    expert_type=expert_type,
                    expert_name=expert_name,
                    analysis_output=response.content.strip(),
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
            "expert_results": [ToolExpertResult(
                expert_type=expert_type,
                expert_name=expert_name,
                analysis_output=response.content.strip(),
            )],
        }