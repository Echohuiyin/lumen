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
    """Kernel Expert 后路由：结构化契约齐全时才进入 Test Expert。"""
    contract = state.get("kernel_contract") or {}
    if contract:
        required = ("target_arch", "boot_kernel_path", "test_script_path", "expected_signal")
        ok = contract.get("status") == "ok"
        fields_ok = all(contract.get(field) for field in required)
        path_contract_ok = _path_contract_ready_for_test(contract)
        if ok and fields_ok and path_contract_ok:
            return "test_expert"
        # diagnostic logging
        if ok is False:
            print(f"  [路由诊断] kernel_contract.status={contract.get('status')!r}", flush=True)
        missing = [f for f in required if not contract.get(f)]
        if missing:
            print(f"  [路由诊断] contract 缺少必填字段: {missing}", flush=True)
        if not path_contract_ok:
            print("  [路由诊断] UAF/refcount path contract 未通过，跳过 QEMU", flush=True)
        return "knowledge_base"

    if state.get("kernel_ready_for_test") is False:
        print("  [路由诊断] 无 kernel_contract 且 kernel_ready_for_test=False", flush=True)
        return "knowledge_base"

    if state.get("final_response") and not state.get("reproduce_case"):
        print("  [路由诊断] 有 final_response 但无 reproduce_case → END", flush=True)
        return END

    print("  [路由诊断] 无 contract 但 fallthrough → test_expert", flush=True)
    return "test_expert"


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


def route_after_test(state: MaintenanceWorkflowState):
    """测试专家后路由：复现成功或超限失败均归档，否则回到内核专家重新分析。"""
    max_attempts = state.get("config", {}).get("workflow", {}).get("max_test_attempts", 3)

    if state.get("test_passed"):
        return "knowledge_base"

    if state.get("test_attempts", 0) >= max_attempts:
        return "knowledge_base"

    return "kernel_expert"
