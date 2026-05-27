from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState, ToolExpertResult


def tool_expert_node(state: MaintenanceWorkflowState) -> dict:
    """工具专家 agent：根据 expert_type 执行对应的专业分析。

    支持的专家类型通过配置文件定义，目前包括：
    - knowledge_search: 历史知识库搜索
    - lock_analysis: 锁分析
    - crash_analysis: Crash 分析
    - kernel_log_analysis: 内核日志分析
    """
    expert_type = state["expert_type"]
    config = state.get("config", {})

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
    llm = get_llm_with_config(agent_config)
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", f"prompts/maintenance/{expert_type}.md")
    )

    user_content = f"用户输入:\n{state['user_input']}"

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
