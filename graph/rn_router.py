from langgraph.types import Send
import re

from graph.rn_state import MaintenanceWorkflowState


def route_after_validator(state: MaintenanceWorkflowState):
    """Validator 后路由：校验通过交给 PM，不通过直接结束（要求用户补充信息）。"""
    if state.get("validation_passed"):
        return "pm"
    return END


def route_after_pm(state: MaintenanceWorkflowState):
    """PM 后路由：fan-out 到各工具专家。

    传递必要的状态字段给工具专家，确保每个专家都能访问配置和用户输入。
    """
    required_experts = state.get("required_experts", [])

    if not required_experts:
        return "kernel_expert"

    # 传递必要的状态字段给工具专家
    return [
        Send("tool_expert", {
            "expert_type": expert_type,
            "user_input": state.get("user_input", ""),
            "input_artifacts_contract": state.get("input_artifacts_contract", {}),
            "config": state.get("config", {}),
            "config_path": state.get("config_path", ""),
            "session_dir": state.get("session_dir", ""),
        })
        for expert_type in required_experts
    ]


def route_after_kernel(state: MaintenanceWorkflowState):
    """Archive every outcome after the single analysis/PoC/verification loop.

    The QEMU runner is invoked inside ``kernel_expert`` and its JSON contract
    is evidence, not a separate agent handoff.  Blocked and failed attempts
    must be archived as well, so no route silently retries with a different
    context or drops negative evidence.
    """
    return "knowledge_base"


def _path_contract_ready_for_test(contract: dict) -> bool:
    """Defence-in-depth before executing a reproducer for a path analysis."""
    if not contract.get("path_analysis_required"):
        return True
    candidates = contract.get("all_possible_paths") or []
    max_path = contract.get("max_likely_path") or ""
    target = contract.get("reproduction_target_path") or ""
    scope = contract.get("path_analysis_scope") or {}
    analysis = contract.get("uaf_analysis") or {}

    def normalise(value: str) -> str:
        return re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", str(value)).strip()

    candidate_set = {normalise(item) for item in candidates if normalise(item)}
    scope_complete = all(scope.get(field) for field in (
        "kernel_commit", "kernel_config", "entry_points", "object_type", "concurrency_model",
    ))
    structured_ready = bool(
        analysis
        and not analysis.get("legacy_unstructured", False)
        and analysis.get("case_id")
        and analysis.get("max_likely_path_id") == analysis.get("reproduction_target_path_id")
        and analysis.get("target_contexts")
    )
    return bool(
        candidate_set
        and normalise(max_path) in candidate_set
        and normalise(target) == normalise(max_path)
        and scope_complete
        and structured_ready
    )
