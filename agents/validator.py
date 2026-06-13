from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_display
from config import get_llm_with_config, load_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def validator_node(state: MaintenanceWorkflowState) -> dict:
    """校验用户输入信息是否完备。

    只负责判断信息是否完整，不完整则要求用户补充，完整则交给 PM。
    """
    config = load_config(state["config_path"])
    agent_config = config.get("agents", {}).get("validator", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="validator")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/maintenance/validator.md")
    )

    user_content = f"用户输入:\n{state['user_input']}"

    response = call_llm_with_display(
        "Validator", "校验输入", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
    )

    text = response.content.strip()

    # 解析校验结果
    if "VALIDATION: PASSED" in text:
        return {
            "validation_passed": True,
            "validation_feedback": "",
            "config": config,
        }
    else:
        # 提取反馈信息
        feedback = text
        if "VALIDATION: FAILED" in text:
            # 取 FAILED 标记之后的内容作为反馈
            idx = text.find("VALIDATION: FAILED")
            feedback = text[idx + len("VALIDATION: FAILED"):].strip()
            if not feedback:
                feedback = text

        return {
            "validation_passed": False,
            "validation_feedback": feedback,
            "config": config,
        }
