import json

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def pm_node(state: MaintenanceWorkflowState) -> dict:
    """PM agent：根据输入信息分类交给对应的工具专家，并创建 issue。

    1. 分析用户问题，判断需要哪些工具专家参与
    2. 创建 issue（打桩）
    """
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("pm", {})
    llm = get_llm_with_config(agent_config)
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/pm.md")
    )

    # 构建可用专家列表信息
    experts_config = config.get("tool_experts", [])
    experts_desc = []
    for exp in experts_config:
        experts_desc.append(f"- {exp['type']}: {exp.get('name', exp['type'])} — {exp.get('description', '')}")

    user_content = (
        f"用户输入:\n{state['user_input']}\n\n"
        f"可用工具专家:\n" + "\n".join(experts_desc)
    )

    response = call_llm_with_display(
        "PM", "分析分类", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )

    text = response.content.strip()

    # 解析需要的专家类型
    required_experts = _parse_required_experts(text, experts_config)

    # 创建 issue（打桩）
    issue_id, issue_url = _create_issue_stub(state["user_input"])

    return {
        "required_experts": required_experts,
        "issue_id": issue_id,
        "issue_url": issue_url,
    }


def _parse_required_experts(text: str, experts_config: list[dict]) -> list[str]:
    """从 PM 输出中解析需要的专家类型列表。"""
    # 尝试从 REQUIRED_EXPERTS: 标记后解析
    marker = "REQUIRED_EXPERTS:"
    if marker in text:
        idx = text.find(marker) + len(marker)
        rest = text[idx:].strip()
        # 取到下一个标记或末尾
        for end_marker in ["\nISSUE:", "\n\n", "\n[A-Z]"]:
            end_idx = rest.find(end_marker)
            if end_idx > 0:
                rest = rest[:end_idx]
        # 解析为列表
        experts = [e.strip().strip("-").strip() for e in rest.split("\n") if e.strip()]
        # 验证专家类型是否在配置中
        valid_types = {exp["type"] for exp in experts_config}
        return [e for e in experts if e in valid_types]

    # 回退：根据关键词匹配
    valid_types = {exp["type"] for exp in experts_config}
    found = []
    for exp_type in valid_types:
        if exp_type in text:
            found.append(exp_type)
    return found if found else [experts_config[0]["type"]] if experts_config else []


def _create_issue_stub(user_input: str) -> tuple[str, str]:
    """创建 issue（打桩实现，后续补充具体逻辑）。"""
    import uuid
    issue_id = f"ISSUE-{uuid.uuid4().hex[:8]}"
    issue_url = f"https://example.com/issues/{issue_id}"
    print(f"[PM] 创建 issue（打桩）: {issue_id} — {issue_url}")
    return issue_id, issue_url
