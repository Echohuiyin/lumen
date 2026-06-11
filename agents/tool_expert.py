"""工具专家 agent：根据 expert_type 执行对应的专业分析。

支持的知识库搜索专家现在会实际执行 RAG 检索，而非仅输出命令。
"""

import os
from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from agents.rag_integration import get_rag_context_for_query
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState, ToolExpertResult


def _extract_vmcore_paths(user_input: str) -> tuple[str | None, str | None]:
    """从用户输入中提取 vmcore 和 vmlinux 文件路径。

    Returns:
        (vmcore_path, vmlinux_path) 或 (None, None)
    """
    import re

    vmcore_pattern = r'vmcore\s*(?:文件)?[:\s]+([/\w\-\.]+)'
    vmlinux_pattern = r'vmlinux\s*(?:文件)?[:\s]+([/\w\-\.]+)'

    vmcore_match = re.search(vmcore_pattern, user_input, re.IGNORECASE)
    vmlinux_match = re.search(vmlinux_pattern, user_input, re.IGNORECASE)

    vmcore_path = vmcore_match.group(1) if vmcore_match else None
    vmlinux_path = vmlinux_match.group(1) if vmlinux_match else None

    return vmcore_path, vmlinux_path


def _check_file_exists(path: str | None) -> bool:
    """检查文件是否存在（用于 MCP 工具智能判断）。"""
    if path is None:
        return False
    return os.path.exists(path)


def tool_expert_node(state: MaintenanceWorkflowState) -> dict:
    """工具专家 agent：根据 expert_type 执行对应的专业分析。

    支持的专家类型通过配置文件定义，目前包括：
    - knowledge_search: 历史知识库搜索（实际执行 RAG 检索）
    - lock_analysis: 锁分析（检查 vmcore 存在后才建议 MCP）
    - crash_analysis: Crash 分析（检查 vmcore 存在后才建议 MCP）
    - kernel_log_analysis: 内核日志分析
    """
    expert_type = state["expert_type"]
    config = state.get("config", {})
    user_input = state.get("user_input", "")

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

    # 根据专家类型构建不同的用户输入内容
    if expert_type == "knowledge_search":
        # 知识库搜索专家：实际执行 RAG 检索
        # 从用户输入中提取关键词
        query = user_input

        # 执行 RAG 搜索
        rag_context = get_rag_context_for_query(query, top_k=3)

        # 构建包含 RAG 结果的用户输入
        user_content = f"""用户输入:
{user_input}

---
以下是从历史知识库检索到的相似案例，请参考这些案例进行分析：

{rag_context}

请基于以上历史案例，结合当前问题特征，给出分析结论和建议。"""

    elif expert_type in ("crash_analysis", "lock_analysis"):
        # Crash/锁分析专家：检查 vmcore 文件是否存在
        vmcore_path, vmlinux_path = _extract_vmcore_paths(user_input)
        vmcore_exists = _check_file_exists(vmcore_path)
        vmlinux_exists = _check_file_exists(vmlinux_path)

        # 添加文件状态提示
        file_status = ""
        if vmcore_path:
            file_status += f"\nvmcore 文件: {vmcore_path}"
            file_status += f" ({'✓ 存在' if vmcore_exists else '✗ 不存在'})"
        if vmlinux_path:
            file_status += f"\nvmlinux 文件: {vmlinux_path}"
            file_status += f" ({'✓ 存在' if vmlinux_exists else '✗ 不存在'})"

        if vmcore_path and not vmcore_exists:
            file_status += "\n\n⚠️ 注意: vmcore 文件不存在，无法使用 MCP 工具进行深度分析。"
            file_status += "请基于日志信息和代码分析进行初步诊断。"
        elif vmlinux_path and not vmlinux_exists:
            file_status += "\n\n⚠️ 注意: vmlinux 文件不存在，无法使用 MCP 工具进行符号解析。"
            file_status += "请基于日志信息进行初步诊断。"

        user_content = f"""用户输入:
{user_input}

{file_status}"""

    else:
        # 其他专家：使用原始用户输入
        user_content = f"用户输入:\n{user_input}"

    response = call_llm_with_display(
        expert_config.get("name", expert_type), "分析中", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )

    return {
        "expert_results": [ToolExpertResult(
            expert_type=expert_type,
            expert_name=expert_config.get("name", expert_type),
            analysis_output=response.content.strip(),
        )],
    }