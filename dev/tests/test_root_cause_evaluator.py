from pathlib import Path

from agents.root_cause_evaluator import evaluate_root_cause


def _state(tmp_path: Path) -> dict:
    source = tmp_path / "linux"
    source_file = source / "drivers" / "demo.c"
    source_file.parent.mkdir(parents=True)
    source_file.write_text(
        "void demo_open(void) { /* lock after free */ }\n",
        encoding="utf-8",
    )
    report = tmp_path / "report.txt"
    report.write_text(
        "BUG: KASAN: slab-use-after-free in demo_open\n"
        "Call Trace: demo_open do_dentry_open vfs_open\n",
        encoding="utf-8",
    )
    log = tmp_path / "console.log"
    log.write_text("demo_open: Read of size 1 after free\n", encoding="utf-8")
    repro = tmp_path / "user-repro.c"
    repro.write_text("this text must never be read by the evaluator\n", encoding="utf-8")
    return {
        "user_input": (
            f"entry_point: demo_open\n"
            f"crash_report: {report}\n"
            f"reproducer: {repro}\n"
        ),
        "input_artifacts_contract": {
            "crash_report_path": str(report),
            "log_path": str(log),
            "kernel_source_path": str(source),
            "expected_kernel_commit": "0123456789abcdef",
        },
        "kernel_contract": {
            "status": "ok",
            "root_cause": (
                "demo_open reads a lock from a stale object after its release; "
                "the lifetime/refcount race permits use-after-free."
            ),
            "root_cause_evidence": [{
                "function": "demo_open",
                "file": "drivers/demo.c",
                "line": 1,
                "source_domain": "kernel",
            }],
            "call_chain_oracle": {
                "required_top_frames": ["demo_open", "do_dentry_open", "vfs_open"],
                "fault_signatures": ["KASAN: slab-use-after-free"],
            },
            "original_call_chain": ["demo_open", "do_dentry_open", "vfs_open"],
        },
        "expert_results": [{
            "expert_type": "crash_analysis",
            "expert_name": "Crash",
            "analysis_output": "demo_open was called after release and caused KASAN UAF.",
            "structured_output": {"status": "ok", "evidence": []},
        }],
        "test_passed": False,
        "call_chain_consistent": False,
        "test_attempts": 2,
        "test_contract": {"status": "failed", "code": "FAILED_SIGNAL_NOT_FOUND"},
    }


def test_evaluator_scores_source_grounded_root_cause_and_ignores_repro(tmp_path):
    result = evaluate_root_cause(_state(tmp_path))

    assert result["root_cause"]["status"] == "supported"
    assert result["root_cause"]["accuracy_score"] >= 80
    assert result["case_evidence"]["fix_audit"]["available"] is False
    assert result["reproduction"]["test_passed"] is False
    assert result["tool_experts"][0]["root_cause_contribution_score"] > 0
    assert "this text must never be read" not in str(result)


def test_evaluator_preserves_failed_tool_as_unavailable(tmp_path):
    state = _state(tmp_path)
    state["expert_results"][0]["structured_output"] = {
        "status": "failed",
        "errors": ["provider rejected the artifact"],
    }

    result = evaluate_root_cause(state)

    assert result["tool_experts"][0]["role"] == "unavailable"
    assert result["tool_experts"][0]["accuracy_score"] <= 15
