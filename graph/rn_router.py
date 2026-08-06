from langgraph.graph import END
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
    """Send a validated userspace C contract to Test Expert."""
    contract = state.get("kernel_contract") or {}
    if contract.get("status") != "ok" or not state.get("kernel_ready_for_test"):
        return "knowledge_base"
    return "test_expert"


def route_after_test(state: MaintenanceWorkflowState):
    """Close, retry, or block the ten-try-out Kernel/Test Expert loop."""
    contract = state.get("test_attempt_contract") or state.get("test_contract") or {}
    # A raw signal/call-chain match is not sufficient: Test Expert also has
    # to approve the semantic/root-cause review.  In particular, a run may
    # observe the target signal while the ordered oracle or principle review
    # fails.  The old check closed the workflow on that intermediate state
    # because ``call_chain_consistent`` remained true in the result payload.
    # Only the complete Test Expert verdict is terminal success.
    if state.get("test_passed") or contract.get("test_passed"):
        return "knowledge_base"
    if contract.get("status") in {"blocked", "skipped"}:
        return "knowledge_base"
    if (
        contract.get("progress_kind") == "retracted"
        or contract.get("code") == "FAILED_REPRODUCER_REGRESSION"
        or int(contract.get("no_progress_streak", 0) or 0) >= 2
    ):
        return "knowledge_base"
    maximum = int(state.get("max_tryouts", 10) or 10)
    if int(state.get("tryout_count", state.get("test_attempts", 0)) or 0) >= maximum:
        return "knowledge_base"
    return "kernel_expert"


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
