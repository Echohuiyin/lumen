"""Rule-first validator tests."""

from pathlib import Path
import sys
import tempfile

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from agents.input_artifacts import parse_input_artifacts
from agents.validator import _validate_input_by_rules, validator_node


def test_empty_input_blocks():
    result = _validate_input_by_rules("")
    assert result.status == "blocked"
    assert result.validation_passed is False
    assert "problem_description" in result.missing_fields


def test_kernel_panic_passes_without_llm():
    result = _validate_input_by_rules(
        "内核 panic，vmcore: /tmp/vmcore，vmlinux: /tmp/vmlinux，Call Trace 显示 NULL pointer"
    )
    assert result.status == "ok"
    assert result.validation_passed is True
    assert "crash" in result.detected_signals


def test_deadlock_passes_without_llm():
    result = _validate_input_by_rules(
        "系统 hung task blocked for more than 120 seconds，怀疑 mutex deadlock"
    )
    assert result.status == "ok"
    assert result.validation_passed is True
    assert "lock" in result.detected_signals


def test_vague_input_blocks():
    result = _validate_input_by_rules("有问题，帮我看看")
    assert result.status == "blocked"
    assert result.validation_passed is False
    assert "problem_symptom" in result.missing_fields


def test_validator_node_returns_contract_for_rule_pass():
    state = {
        "user_input": "kernel panic with vmcore and vmlinux",
        "config_path": "config.json",
    }
    result = validator_node(state)
    assert result["validation_passed"] is True
    assert result["validation_contract"]["reason"] == "rule_detected_kernel_signals"
    assert "input_artifacts_contract" in result
    assert result["config"]


def test_parse_input_artifacts_extracts_paths_and_arch():
    contract = parse_input_artifacts(
        "arm64 panic vmcore: /tmp/vmcore vmlinux: /tmp/vmlinux "
        "boot_kernel: /linux/arch/arm64/boot/Image",
        validate_paths=False,
    )
    assert contract.status == "ok"
    assert contract.vmcore_path == "/tmp/vmcore"
    assert contract.vmlinux_path == "/tmp/vmlinux"
    assert contract.boot_kernel_path == "/linux/arch/arm64/boot/Image"
    assert contract.target_arch == "arm64"


def test_parse_input_artifacts_validates_paths_and_kernel_types():
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        vmcore = tmp_path / "vmcore"
        vmcore.write_bytes(b"CORE")
        vmlinux = tmp_path / "vmlinux"
        vmlinux.write_bytes(b"\x7fELF" + b"\x00" * 16)
        bzimage = tmp_path / "bzImage"
        bzimage.write_bytes(b"MZ" + b"\x00" * 16)

        contract = parse_input_artifacts(
            f"x86_64 vmcore: {vmcore} vmlinux: {vmlinux} boot_kernel: {bzimage}"
        )

    assert contract.status == "ok"
    assert not contract.errors
    checks = [item for item in contract.evidence if item.get("kind") == "input_artifact_check"]
    assert len(checks) == 3
    assert any(item.get("field") == "vmlinux_path" and item.get("kernel_type") == "elf" for item in checks)
    assert any(item.get("field") == "boot_kernel_path" and item.get("kernel_type") == "bzimage" for item in checks)


def test_parse_input_artifacts_degrades_when_boot_kernel_is_elf():
    with tempfile.TemporaryDirectory() as tmp:
        boot_kernel = Path(tmp) / "vmlinux"
        boot_kernel.write_bytes(b"\x7fELF" + b"\x00" * 16)

        contract = parse_input_artifacts(f"boot_kernel: {boot_kernel}")

    assert contract.status == "degraded"
    assert "boot_kernel_path points to ELF" in contract.errors[0]


if __name__ == "__main__":
    for test in [
        test_empty_input_blocks,
        test_kernel_panic_passes_without_llm,
        test_deadlock_passes_without_llm,
        test_vague_input_blocks,
        test_validator_node_returns_contract_for_rule_pass,
        test_parse_input_artifacts_extracts_paths_and_arch,
        test_parse_input_artifacts_validates_paths_and_kernel_types,
        test_parse_input_artifacts_degrades_when_boot_kernel_is_elf,
    ]:
        test()
    print("validator_rules OK")
