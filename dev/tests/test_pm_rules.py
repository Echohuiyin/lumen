"""Deterministic PM routing rule tests."""

from pathlib import Path
import sys

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.pm import _select_required_experts_by_rules


FULL_EXPERTS = [
    {"type": "knowledge_search"},
    {"type": "lock_analysis"},
    {"type": "crash_analysis"},
    {"type": "kernel_log_analysis"},
]


def test_deadlock_prefers_lock_over_crash():
    experts, reason = _select_required_experts_by_rules(
        "系统 deadlock，两个线程 blocked for more than 120 seconds，有 vmcore 和 vmlinux",
        FULL_EXPERTS,
    )
    assert "lock_analysis" in experts
    assert "crash_analysis" not in experts
    assert "kernel_log_analysis" in experts
    assert "knowledge_search" in experts
    assert "dedupe_crash" in reason


def test_panic_with_vmcore_selects_crash_and_log():
    experts, _ = _select_required_experts_by_rules(
        "kernel panic with vmcore: /tmp/vmcore and vmlinux: /tmp/vmlinux, Call Trace shows Oops",
        FULL_EXPERTS,
    )
    assert "crash_analysis" in experts
    assert "kernel_log_analysis" in experts
    assert "knowledge_search" in experts


def test_unknown_input_does_not_guess_crash_expert():
    experts, reason = _select_required_experts_by_rules(
        "未知内核问题",
        [{"type": "crash_analysis"}],
    )
    assert experts == []


def test_lockup_and_rcu_stall_route_to_lock_without_crash():
    experts, _ = _select_required_experts_by_rules(
        "kernel panic after RCU stalled and soft lockup; crash log attached",
        [
            {"type": "knowledge_search"},
            {"type": "kernel_log_analysis"},
            {"type": "lock_analysis"},
            {"type": "crash_analysis"},
        ],
    )
    assert "lock_analysis" in experts
    assert "crash_analysis" not in experts


if __name__ == "__main__":
    for test in [
        test_deadlock_prefers_lock_over_crash,
        test_panic_with_vmcore_selects_crash_and_log,
        test_unknown_input_does_not_guess_crash_expert,
        test_lockup_and_rcu_stall_route_to_lock_without_crash,
    ]:
        test()
    print("pm_rules OK")
