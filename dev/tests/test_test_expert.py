"""Focused offline tests for the isolated Test Expert handoff."""

from pathlib import Path
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import CallChainOracle, KernelExpertOutput, UserspaceReproducer
from agents.test_expert import _build_plan, _semantic_review, test_expert_node


def _contract(root: Path) -> KernelExpertOutput:
    source = root / "repro"
    source.mkdir()
    (source / "repro.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    kernel = root / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00")
    return KernelExpertOutput(
        status="ok", target_arch="x86_64", boot_kernel_path=str(kernel),
        root_cause="diagnostic userspace ABI triggers the verified path",
        expected_signal="target fault",
        call_chain_oracle=CallChainOracle(
            fault_signatures=["target fault"], required_frames=["target_frame"],
            target_subsystems=["target"],
        ),
        reproducer=UserspaceReproducer(
            source_dir=str(source), source_files=["repro.c"], entry_source="repro.c",
            output_binary="lumen-repro", run_args=["--once"],
        ),
    )


def test_test_plan_compiles_userspace_source_inside_guest():
    with tempfile.TemporaryDirectory() as directory:
        plan = _build_plan(_contract(Path(directory)))
        assert plan.reproducer.language == "c"
        assert plan.reproducer.artifact_type == "userspace"
        assert plan.execution_steps[-1].type == "run_binary"
        assert plan.execution_steps[-1].path == "bin/lumen-repro"
        assert plan.call_chain_oracle.required_frames == ["target_frame"]


def test_invalid_contract_is_blocked_before_image_copy():
    result = test_expert_node({
        "session_dir": "", "kernel_contract": {}, "tryout_count": 0, "max_tryouts": 10,
        "test_rounds": [],
    })
    assert result["test_contract"]["status"] == "blocked"
    assert result["test_contract"]["code"] == "BLOCKED_INVALID_KERNEL_CONTRACT"
    assert result["tryout_count"] == 1


def test_semantic_review_never_overrides_missing_deterministic_match():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        from agents.contracts import TestResultContract
        failed = TestResultContract(status="failed", code="FAILED_CALL_CHAIN_MISMATCH")
        accepted, reason = _semantic_review(contract, failed)
        assert accepted is False
        assert "deterministic" in reason


if __name__ == "__main__":
    for test in (
        test_test_plan_compiles_userspace_source_inside_guest,
        test_invalid_contract_is_blocked_before_image_copy,
        test_semantic_review_never_overrides_missing_deterministic_match,
    ):
        test()
    print("test_expert OK")
