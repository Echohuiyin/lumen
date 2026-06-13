from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def test_expert_node(state: MaintenanceWorkflowState) -> dict:
    """测试专家 agent：根据内核专家给出的复现用例进行问题复现验证。"""
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

    user_content = (
        f"用户输入:\n{state['user_input']}\n\n"
        f"## 内核专家构造的复现用例\n{state.get('reproduce_case', '')}\n\n"
        f"## 内核维测方案\n{state.get('kernel_diagnosis', '')}\n\n"
        f"## 完整内核分析\n{state.get('kernel_analysis', '')}\n\n"
        f"请根据以上信息验证问题是否可以复现。这是第 {current_attempts} 次验证。"
    )

    if is_last_attempt:
        user_content += (
            f"\n\n**这是最后一次验证机会（共 {max_attempts} 次）。"
            f"如果仍然无法复现，请给出详细的分析建议，帮助用户改进复现思路。**"
        )

    response = call_llm_with_display(
        "测试专家", f"复现验证（第{current_attempts}次）", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )

    text = response.content.strip()

    # 解析测试结果
    test_passed = "REPRODUCE: SUCCESS" in text or "复现成功" in text

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

    response = call_llm_with_display(
        "测试专家", "生成改进建议", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )

    return response.content.strip()
