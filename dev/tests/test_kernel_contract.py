"""Kernel Expert userspace-C handoff and ten-try-out routing tests."""

from pathlib import Path
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import CallChainOracle, KernelExpertOutput, UserspaceReproducer
from agents.kernel_expert import _kernel_contract_ready_for_test, _validate_kernel_contract_artifacts
from graph.rn_router import route_after_kernel, route_after_test, route_after_validator
from graph.rn_workflow import build_maintenance_workflow


def _contract(root: Path) -> KernelExpertOutput:
    kernel = root / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00")
    source = root / "repro"
    source.mkdir()
    (source / "repro.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    return KernelExpertOutput(
        status="ok",
        target_arch="x86_64",
        boot_kernel_path=str(kernel),
        root_cause="A userspace ioctl reaches the verified failing maintenance path.",
        call_chain_oracle=CallChainOracle(
            fault_signatures=["BUG: KASAN: slab-use-after-free"],
            required_frames=["target_release", "target_access"],
            required_frame_order=[["target_release", "target_access"]],
            target_subsystems=["target_subsystem"],
            target_objects=["target_object"],
        ),
        reproducer=UserspaceReproducer(
            source_dir=str(source), source_files=["repro.c"], entry_source="repro.c",
            output_binary="lumen-repro",
        ),
        execution_steps=[
            {"type": "run_binary", "path": "bin/lumen-repro", "args": []},
        ],
    )


def test_userspace_contract_is_a_test_expert_handoff():
    with tempfile.TemporaryDirectory() as directory:
        validated = _validate_kernel_contract_artifacts(_contract(Path(directory)))
        assert validated.status == "ok"
        assert validated.expected_signal == "BUG: KASAN: slab-use-after-free"
        assert _kernel_contract_ready_for_test(validated)
        assert route_after_kernel({"kernel_contract": validated.model_dump() if hasattr(validated, "model_dump") else validated.dict(), "kernel_ready_for_test": True}) == "test_expert"


def test_legacy_kernel_module_field_is_rejected_at_contract_boundary():
    with tempfile.TemporaryDirectory() as directory:
        payload = _contract(Path(directory)).model_dump()
        payload["reproducer_module_path"] = "/tmp/repro.ko"
        try:
            KernelExpertOutput.model_validate(payload)
        except ValueError as exc:
            assert "reproducer_module_path" in str(exc)
        else:
            raise AssertionError("legacy kernel-module field must be rejected")


def test_missing_call_chain_oracle_is_rejected():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        contract.call_chain_oracle.required_frames = []
        validated = _validate_kernel_contract_artifacts(contract)
        assert validated.status == "blocked"
        assert "required_frames" in validated.blocked_reason


def test_explicit_kernel_block_is_not_routed_to_test_expert():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        contract.status = "blocked"
        contract.blocked_reason = "missing first-hand ABI evidence"
        validated = _validate_kernel_contract_artifacts(contract)
        assert validated.status == "blocked"
        assert validated.blocked_reason == "missing first-hand ABI evidence"
        assert not _kernel_contract_ready_for_test(validated)
        payload = validated.model_dump() if hasattr(validated, "model_dump") else validated.dict()
        assert route_after_kernel({"kernel_contract": payload, "kernel_ready_for_test": False}) == "knowledge_base"


def test_router_retries_until_tenth_mismatch_then_ends():
    failed = {"status": "failed", "call_chain_consistent": False}
    assert route_after_test({"tryout_count": 1, "max_tryouts": 10, "test_attempt_contract": failed}) == "kernel_expert"
    assert route_after_test({"tryout_count": 10, "max_tryouts": 10, "test_attempt_contract": failed}) == "knowledge_base"
    assert route_after_test({"tryout_count": 1, "max_tryouts": 10, "test_attempt_contract": {"status": "ok", "test_passed": True}}) == "knowledge_base"
    assert route_after_test({"tryout_count": 1, "max_tryouts": 10, "test_attempt_contract": {"status": "blocked"}}) == "knowledge_base"
    assert route_after_validator({"validation_passed": False}) == "__end__"
    build_maintenance_workflow()


if __name__ == "__main__":
    for test in (
        test_userspace_contract_is_a_test_expert_handoff,
        test_legacy_kernel_module_field_is_rejected_at_contract_boundary,
        test_missing_call_chain_oracle_is_rejected,
        test_explicit_kernel_block_is_not_routed_to_test_expert,
        test_router_retries_until_tenth_mismatch_then_ends,
    ):
        test()
    print("kernel_contract OK")
