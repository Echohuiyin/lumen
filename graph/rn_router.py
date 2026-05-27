from langgraph.graph import END
from langgraph.types import Send

from graph.rn_state import MaintenanceWorkflowState


def route_after_validator(state: MaintenanceWorkflowState):
    """Validator 后路由：校验通过交给 PM，不通过直接结束（要求用户补充信息）。"""
    if state.get("validation_passed"):
        return "pm"
    return END


def route_after_pm(state: MaintenanceWorkflowState):
    """PM 后路由：fan-out 到各工具专家。"""
    required_experts = state.get("required_experts", [])

    if not required_experts:
        return "kernel_expert"

    return [
        Send("tool_expert", {"expert_type": expert_type})
        for expert_type in required_experts
    ]


def route_after_test(state: MaintenanceWorkflowState):
    """测试专家后路由：复现成功交给知识库生成，失败则回到内核专家重新分析。"""
    max_attempts = state.get("config", {}).get("workflow", {}).get("max_test_attempts", 3)

    if state.get("test_passed"):
        return "knowledge_base"

    if state.get("test_attempts", 0) >= max_attempts:
        # 超过最大尝试次数，仍然交给知识库生成（标注未复现）
        return "knowledge_base"

    return "kernel_expert"
