import re
from pathlib import Path

from langchain_core.messages import HumanMessage, SystemMessage

from agents.llm_display import call_llm_with_persistence
from config import get_llm_with_config, load_prompt_from_file
from graph.rn_state import MaintenanceWorkflowState


def pm_node(state: MaintenanceWorkflowState) -> dict:
    """PM agent：根据输入信息分类交给对应的工具专家，并创建 issue。

    1. 分析用户问题，判断需要哪些工具专家参与
    2. 创建 issue（打桩）
    """
    config = state.get("config", {})
    experts_config = config.get("tool_experts", [])

    rule_experts, routing_reason = _select_required_experts_by_rules(
        state.get("user_input", ""),
        experts_config,
    )
    if rule_experts:
        issue_id, issue_url = _create_issue_stub(state["user_input"])
        return {
            "required_experts": rule_experts,
            "pm_routing_reason": routing_reason,
            "issue_id": issue_id,
            "issue_url": issue_url,
        }

    agent_config = config.get("agents", {}).get("pm", {})
    default_config = config.get("default", {})
    llm = get_llm_with_config(agent_config, default_config=default_config, agent_name="pm")
    system_prompt = load_prompt_from_file(
        agent_config.get("prompt_file", "prompts/pm.md")
    )

    # 构建可用专家列表信息
    experts_desc = []
    for exp in experts_config:
        experts_desc.append(f"- {exp['type']}: {exp.get('name', exp['type'])} — {exp.get('description', '')}")

    user_content = (
        f"用户输入:\n{state['user_input']}\n\n"
        f"可用工具专家:\n" + "\n".join(experts_desc)
    )

    # 使用持久化版本
    response = call_llm_with_persistence(
        "pm", "分析分类", llm,
        [SystemMessage(content=system_prompt), HumanMessage(content=user_content)],
        persist_dir=Path("outputs"),
    )

    text = response.content.strip()

    # 解析需要的专家类型
    required_experts = _parse_required_experts(text, experts_config)

    # 创建 issue（打桩）
    issue_id, issue_url = _create_issue_stub(state["user_input"])

    return {
        "required_experts": required_experts,
        "pm_routing_reason": "llm_fallback",
        "issue_id": issue_id,
        "issue_url": issue_url,
    }


def _select_required_experts_by_rules(user_input: str, experts_config: list[dict]) -> tuple[list[str], str]:
    """Select tool experts with deterministic rules before falling back to LLM."""
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
    lock_issue = bool(re.search(r"deadlock|hung task|blocked for more than|soft lockup|hard lockup|lockdep|mutex|spinlock|rwsem|semaphore|d state", text))
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

    if not selected:
        for fallback in ("knowledge_search", "kernel_log_analysis", "crash_analysis"):
            if fallback in valid_types:
                add(fallback, "conservative_default")
                break

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
        # 回退：根据关键词匹配
        for exp in experts_config:
            exp_type = exp["type"]
            if exp_type in text:
                experts.append(exp_type)

    # Deduplication: crash_analysis and lock_analysis both use crash sessions.
    # If both are selected, keep only lock_analysis (it produces better lock analysis).
    if "crash_analysis" in experts and "lock_analysis" in experts:
        experts.remove("crash_analysis")

    filtered = []
    for expert in experts:
        if expert in valid_types and expert not in filtered:
            filtered.append(expert)

    return filtered if filtered else [experts_config[0]["type"]] if experts_config else []


def _create_issue_stub(user_input: str) -> tuple[str, str]:
    """创建 issue（打桩实现，后续补充具体逻辑）。"""
    import uuid
    issue_id = f"ISSUE-{uuid.uuid4().hex[:8]}"
    issue_url = f"https://example.com/issues/{issue_id}"
    print(f"[PM] 创建 issue（打桩）: {issue_id} — {issue_url}")
    return issue_id, issue_url
