"""Offline Kernel Expert tests for the maintenance C-only boundary."""

from pathlib import Path
import json
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import CallChainOracle, KernelExpertOutput, UserspaceReproducer, model_to_dict
from agents.kernel_expert import _extract_kernel_contract, _kernel_contract_ready_for_test, _validate_kernel_contract_artifacts


def _contract(root: Path) -> KernelExpertOutput:
    image = root / "bzImage"
    image.write_bytes(b"MZ\x00\x00")
    source = root / "source"
    source.mkdir()
    (source / "repro.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    return KernelExpertOutput(
        status="ok", target_arch="x86_64", boot_kernel_path=str(image),
        root_cause="verified userspace ABI reaches the failing kernel path",
        call_chain_oracle=CallChainOracle(
            fault_signatures=["target fault"], required_frames=["target_frame"],
            target_subsystems=["target_subsystem"],
        ),
        reproducer=UserspaceReproducer(
            source_dir=str(source), source_files=["repro.c"], entry_source="repro.c",
            output_binary="lumen-repro",
        ),
    )


def test_prompt_states_maintenance_and_c_only_boundaries():
    prompt = (PROJECT_ROOT / "prompts" / "kernel_expert.md").read_text(encoding="utf-8")
    assert "Linux kernel maintenance" in prompt
    assert "userspace C" in prompt
    assert "kernel modules" in prompt
    assert "Test Expert, not you" in prompt


def test_structured_kernel_contract_round_trips_without_module_build():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        text = "KERNEL_CONTRACT:\n```json\n" + json.dumps(model_to_dict(contract)) + "\n```"
        parsed = _extract_kernel_contract(text)
        validated = _validate_kernel_contract_artifacts(parsed)
        assert validated.status == "ok"
        assert _kernel_contract_ready_for_test(validated)
        assert validated.reproducer.output_binary == "lumen-repro"


def test_bare_json_contract_survives_marker_word_in_string():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        data = model_to_dict(contract)
        data["change_from_previous_tryout"] = (
            "retry note mentions KERNEL_CONTRACT but is not a marker"
        )
        parsed = _extract_kernel_contract("最终分析结果\n" + json.dumps(data))
        assert parsed.status == "ok"
        assert parsed.call_chain_oracle.required_frames == ["target_frame"]

def test_contract_rejects_module_metadata_in_c_source_set():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        source = Path(contract.reproducer.source_dir) / "bad.ko"
        source.write_text("not a module", encoding="utf-8")
        contract.reproducer.source_files.append("bad.ko")
        validated = _validate_kernel_contract_artifacts(contract)
        assert validated.status == "blocked"
        assert "non-C reproducer source" in validated.blocked_reason


if __name__ == "__main__":
    for test in (
        test_prompt_states_maintenance_and_c_only_boundaries,
        test_structured_kernel_contract_round_trips_without_module_build,
        test_contract_rejects_module_metadata_in_c_source_set,
    ):
        test()
    print("kernel_expert OK")
