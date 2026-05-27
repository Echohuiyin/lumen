from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def kernel_expert_node(state: MaintenanceWorkflowState) -> dict:
    """内核专家 agent：根据工具专家的输出，结合代码分析，构造必现用例并给出内核维测方案。"""
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("kernel_expert", {})
    llm = get_llm_with_config(agent_config)
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/kernel_expert.md")
    )

    # 汇总所有工具专家的分析结果
    expert_results = state.get("expert_results", [])
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

    response = call_llm_with_display(
        "内核专家", "分析构造用例", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
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
