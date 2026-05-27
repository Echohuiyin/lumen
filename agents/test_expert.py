from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def test_expert_node(state: MaintenanceWorkflowState) -> dict:
    """测试专家 agent：根据内核专家给出的复现用例进行问题复现验证。"""
    config = state.get("config", {})
    agent_config = config.get("agents", {}).get("test_expert", {})
    llm = get_llm_with_config(agent_config)
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/test_expert.md")
    )

    current_attempts = state.get("test_attempts", 0) + 1

    user_content = (
        f"用户输入:\n{state['user_input']}\n\n"
        f"## 内核专家构造的复现用例\n{state.get('reproduce_case', '')}\n\n"
        f"## 内核维测方案\n{state.get('kernel_diagnosis', '')}\n\n"
        f"## 完整内核分析\n{state.get('kernel_analysis', '')}\n\n"
        f"请根据以上信息验证问题是否可以复现。这是第 {current_attempts} 次验证。"
    )

    response = call_llm_with_display(
        "测试专家", f"复现验证（第{current_attempts}次）", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )

    text = response.content.strip()

    # 解析测试结果
    test_passed = "REPRODUCE: SUCCESS" in text or "复现成功" in text

    return {
        "test_result": text,
        "test_passed": test_passed,
        "test_attempts": current_attempts,
    }
