"""Tool expert structured evidence parser tests."""

from pathlib import Path
import sys

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.tool_expert import (
    _make_tool_result,
    _parse_bt_evidence,
    _parse_log_evidence,
    _parse_ps_evidence,
)


def test_parse_log_evidence_extracts_kernel_events():
    log = """
[  1.0] BUG: unable to handle kernel NULL pointer dereference
[  1.1] Call Trace:
[  1.2] Kernel panic - not syncing: fatal exception
[120.0] INFO: task worker:42 blocked for more than 120 seconds.
"""
    evidence = _parse_log_evidence(log)
    event_types = {item["event_type"] for item in evidence}
    assert "bug" in event_types
    assert "kernel_panic" in event_types
    assert "hung_task" in event_types
    assert "call_trace" in event_types


def test_parse_ps_evidence_extracts_interesting_tasks():
    ps = """
   PID    PPID  CPU       TASK        ST  %MEM     VSZ    RSS  COMM
>    0       0   0  ffffffff82016800  RU   0.0       0      0  swapper/0
   42       2   1  ffff888100200000  UN   0.0       0      0  worker
  100       1   0  ffff888100300000  IN   0.1    1000    100  insmod
"""
    evidence = _parse_ps_evidence(ps)
    states = {item["state"] for item in evidence}
    assert {"RU", "UN", "IN"}.issubset(states)
    assert any(item["pid"] == 42 and item["comm"] == "worker" for item in evidence)


def test_parse_bt_evidence_extracts_pid_and_frames():
    bt = """
PID: 42     TASK: ffff888100200000  CPU: 1   COMMAND: "worker"
 #0 [ffffc9000000bd00] __schedule at ffffffff81000000
 #1 [ffffc9000000bd80] mutex_lock at ffffffff81000100
PID: 100    TASK: ffff888100300000  CPU: 0   COMMAND: "insmod"
 #0 [ffffc9000000cd00] panic at ffffffff81000200
"""
    evidence = _parse_bt_evidence(bt)
    assert len(evidence) == 2
    assert evidence[0]["pid"] == 42
    assert "mutex_lock" in evidence[0]["frames"][1]


def test_make_tool_result_keeps_structured_evidence():
    result = _make_tool_result(
        expert_type="kernel_log_analysis",
        expert_name="log",
        analysis_output="summary",
        status="ok",
        evidence=[{"kind": "log_event", "event_type": "kernel_panic"}],
    )
    structured = result["structured_output"]
    assert structured["status"] == "ok"
    assert structured["evidence"][0]["event_type"] == "kernel_panic"


if __name__ == "__main__":
    for test in [
        test_parse_log_evidence_extracts_kernel_events,
        test_parse_ps_evidence_extracts_interesting_tasks,
        test_parse_bt_evidence_extracts_pid_and_frames,
        test_make_tool_result_keeps_structured_evidence,
    ]:
        test()
    print("tool_evidence OK")
