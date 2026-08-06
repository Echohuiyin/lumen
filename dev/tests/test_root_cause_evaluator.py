import json
from pathlib import Path

import agents.root_cause_evaluator as evaluator
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


def test_kasan_wild_pointer_is_not_scored_as_uaf(tmp_path):
    state = _state(tmp_path)
    log = Path(state["input_artifacts_contract"]["log_path"])
    log.write_text(
        "demo_open: wild pointer observed\n"
        "Warning: Permanently added '[127.0.0.1]:2222' (ED25519) to the list of known hosts.\n",
        encoding="utf-8",
    )
    report = Path(state["input_artifacts_contract"]["crash_report_path"])
    report.write_text(
        "Oops: general protection fault for non-canonical address\n"
        "KASAN: maybe wild-memory-access\n"
        "RIP: 0010:demo_open+0x2c/0x70\n",
        encoding="utf-8",
    )
    state["kernel_contract"]["root_cause"] = (
        "demo_open dereferences an invalid pointer address; the wild pointer "
        "reaches the diagnostic path without evidence of a freed object."
    )
    result = evaluate_root_cause(state)

    observed = result["case_evidence"]["observed"]
    assert observed["uaf_signal"] is False
    # Transport noise must not select the kernel-warning vocabulary.
    assert observed["warning_signal"] is False
    mechanism = next(
        item for item in result["root_cause"]["dimensions"]
        if item["id"] == "mechanism"
    )
    assert mechanism["score"] > 0


def test_evaluator_uses_semcode_pinned_source_scope(tmp_path, monkeypatch):
    state = _state(tmp_path)
    shared = Path(state["input_artifacts_contract"]["kernel_source_path"])
    pinned = tmp_path / "pinned-linux"
    (pinned / "drivers").mkdir(parents=True)
    (pinned / "drivers" / "demo.c").write_text(
        (shared / "drivers" / "demo.c").read_text(encoding="utf-8"),
        encoding="utf-8",
    )
    expected = "deadbeef" * 5
    state["input_artifacts_contract"]["expected_kernel_commit"] = expected
    session = tmp_path / "session"
    session.mkdir()
    state["session_dir"] = str(session)
    (session / "semcode-evidence.json").write_text(
        json.dumps({"status": "ok", "kernel_source": str(pinned), "entries": [{"function": "demo_open"}]}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        evaluator,
        "_git_head",
        lambda root: expected if Path(root).resolve() == pinned.resolve() else "",
    )

    result = evaluate_root_cause(state)

    assert result["case_evidence"]["source_path"] == str(pinned.resolve())
    assert result["case_evidence"]["declared_source_path"] == str(shared.resolve())
    assert result["case_evidence"]["source_commit_matches"] is True


def test_evaluator_preserves_failed_tool_as_unavailable(tmp_path):
    state = _state(tmp_path)
    state["expert_results"][0]["structured_output"] = {
        "status": "failed",
        "errors": ["provider rejected the artifact"],
    }

    result = evaluate_root_cause(state)

    assert result["tool_experts"][0]["role"] == "unavailable"
    assert result["tool_experts"][0]["accuracy_score"] <= 15

def test_evaluator_aligns_declared_fix_patch(tmp_path):
    state = _state(tmp_path)
    patch = tmp_path / "fix.patch"
    patch.write_text(
        "diff --git a/net/core/link_watch.c b/net/core/link_watch.c\n"
        "+ __dev_put(dev); /* after netdev_unlock_ops */\n",
        encoding="utf-8",
    )
    state["input_artifacts_contract"]["fix_commit"] = (
        "83b67cc9be9223183caf91826d9c194d7fb128fa"
    )
    state["input_artifacts_contract"]["fix_patch_path"] = str(patch)
    result = evaluate_root_cause(state)
    fix = result["case_evidence"]["fix_audit"]
    assert fix["available"] is True
    assert fix["fix_commits"] == ["83b67cc9be9223183caf91826d9c194d7fb128fa"]
    assert fix["patch_bytes"] > 0


def test_evaluator_uses_attested_external_snapshot(tmp_path):
    state = _state(tmp_path)
    snapshot = tmp_path / "snapshot"
    (snapshot / "drivers").mkdir(parents=True)
    (snapshot / "include" / "linux").mkdir(parents=True)
    (snapshot / "init").mkdir()
    (snapshot / "Makefile").write_text("all:\n", encoding="utf-8")
    (snapshot / "Kconfig").write_text("mainmenu \"Linux\"\n", encoding="utf-8")
    (snapshot / "include" / "linux" / "kernel.h").write_text("\n", encoding="utf-8")
    (snapshot / "init" / "main.c").write_text("\n", encoding="utf-8")
    (snapshot / "drivers" / "demo.c").write_text(
        "void demo_open(void) { /* lock after free */ }\n", encoding="utf-8"
    )
    expected = "deadbeef" * 5
    manifest = tmp_path / "source-snapshot.json"
    manifest.write_text(
        json.dumps({
            "verified": True,
            "expected_commit": expected,
            "tree": {"path": str(snapshot)},
        }),
        encoding="utf-8",
    )
    state["input_artifacts_contract"]["expected_kernel_commit"] = expected
    state["input_artifacts_contract"]["source_snapshot_manifest_path"] = str(manifest)

    result = evaluate_root_cause(state)

    assert result["case_evidence"]["source_path"] == str(snapshot.resolve())
