import re
from agents.llm_display import set_session_dir
from graph.rn_state import MaintenanceWorkflowState


def pm_node(state: MaintenanceWorkflowState) -> dict:
    """PM agent：根据输入信息分类交给对应的工具专家，并创建 issue。

    1. 分析用户问题，判断需要哪些工具专家参与
    2. 创建 issue（打桩）
    """
    set_session_dir(state.get("session_dir"))
    config = state.get("config", {})
    experts_config = config.get("tool_experts", [])

    rule_experts, routing_reason = _select_required_experts_by_rules(
        state.get("user_input", ""),
        experts_config,
    )
    # Routing is deterministic.  Unknown wording still gets the baseline
    # knowledge search, plus log analysis when a log is present; crash
    # analysis is added only for vmcore/vmlinux or explicit crash evidence.
    if not rule_experts:
        raise ValueError("No tool experts are configured; PM routing is blocked")
    issue_id, issue_url = _create_issue_stub(state["user_input"])
    return {
        "required_experts": rule_experts,
        "pm_routing_reason": routing_reason or "deterministic_default_by_available_evidence",
        "issue_id": issue_id,
        "issue_url": issue_url,
    }


def _select_required_experts_by_rules(user_input: str, experts_config: list[dict]) -> tuple[list[str], str]:
    """Select tool experts with deterministic rules."""
    valid_types = {exp["type"] for exp in experts_config}
    if not valid_types:
        return [], "no_tool_experts_configured"

    text = user_input.lower()
    selected: list[str] = []
    reasons: list[str] = []

    def add(expert_type: str, reason: str) -> None:
        if expert_type in valid_types and expert_type not in selected:
            selected.append(expert_type)
            reasons.append(reason)

    has_vmcore = bool(re.search(r"\bvmcore\b|/proc/vmcore|kdump", text))
    has_vmlinux = "vmlinux" in text
    has_log = bool(re.search(r"\bdmesg\b|\bconsole\b|\blog\b|call trace|stack trace|oops", text))
    lock_issue = bool(re.search(
        r"deadlock|hung task|hung_task|blocked for more than|soft lockup|soft_lockup|"
        r"hard lockup|hard_lockup|rcu stalled|rcu_stall|rcu_sched|lockdep|mutex|"
        r"spinlock|rwsem|semaphore|d state",
        text,
    ))
    crash_issue = bool(re.search(r"panic|oops|null pointer|unable to handle|kernel bug|bug:|crash|segfault|general protection fault", text))
    memory_issue = bool(re.search(r"oom|out of memory|page fault|slab|kmemleak|use-after-free|uaf|double free", text))

    if lock_issue:
        add("lock_analysis", "lock_or_hung_task_keywords")
        add("kernel_log_analysis", "lock_issues_need_log_timeline")
    if crash_issue or memory_issue or (has_vmcore and has_vmlinux):
        add("crash_analysis", "crash_or_vmcore_keywords")
    if has_log or lock_issue or crash_issue:
        add("kernel_log_analysis", "log_or_stack_keywords")

    add("knowledge_search", "always_include_historical_cases")

    if "crash_analysis" in selected and "lock_analysis" in selected:
        selected.remove("crash_analysis")
        reasons.append("dedupe_crash_when_lock_analysis_selected")

    return selected, ", ".join(reasons) if reasons else "no_rule_match"


def _parse_required_experts(text: str, experts_config: list[dict]) -> list[str]:
    """从 PM 输出中解析需要的专家类型列表。"""
    valid_types = {exp["type"] for exp in experts_config}
    experts: list[str] = []

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
        # 支持逐行、逗号分隔、JSON-like 列表等常见输出形式。
        raw_experts = re.split(r"[\n,，]+", rest)
        experts = [
            e.strip().strip("-*[]`'\"").strip()
            for e in raw_experts
            if e.strip().strip("-*[]`'\"").strip()
        ]
    else:
        # Structured marker is mandatory; do not infer routing from prose.
        return []

    # Deduplication: crash_analysis and lock_analysis both use crash sessions.
    # If both are selected, keep only lock_analysis (it produces better lock analysis).
    if "crash_analysis" in experts and "lock_analysis" in experts:
        experts.remove("crash_analysis")

    filtered = []
    for expert in experts:
        if expert in valid_types and expert not in filtered:
            filtered.append(expert)

    return filtered


def _create_issue_stub(user_input: str) -> tuple[str, str]:
    """创建 issue（打桩实现，后续补充具体逻辑）。"""
    import uuid
    issue_id = f"ISSUE-{uuid.uuid4().hex[:8]}"
    issue_url = f"https://example.com/issues/{issue_id}"
    print(f"[PM] 创建 issue（打桩）: {issue_id} — {issue_url}")
    return issue_id, issue_url
