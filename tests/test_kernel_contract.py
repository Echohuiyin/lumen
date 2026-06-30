"""Kernel Expert contract parsing and routing tests."""

from pathlib import Path
import sys
import tempfile

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from agents.kernel_expert import (
    _extract_kernel_contract,
    _kernel_contract_from_markers,
    _kernel_contract_ready_for_test,
    _merge_kernel_contract,
    _validate_kernel_contract_artifacts,
)
from graph.rn_router import route_after_kernel


def test_extract_kernel_contract_json_with_nested_evidence():
    with tempfile.NamedTemporaryFile() as kernel_file, tempfile.NamedTemporaryFile() as test_script:
        kernel_file.write(b"MZ\x00\x00")
        kernel_file.flush()
        test_script.write(b"#!/bin/sh\n")
        test_script.flush()
        text = """
analysis text

KERNEL_CONTRACT:
```json
{
  "status": "ok",
  "target_arch": "x86_64",
  "vmlinux_path": "/tmp/vmlinux",
  "boot_kernel_path": "__KERNEL__",
  "reproducer_dir": "outputs/repro",
  "reproducer_module_path": "outputs/repro/repro.ko",
  "test_script_path": "__SCRIPT__",
  "expected_signal": "Kernel panic",
  "build_status": "passed",
  "evidence": [{"kind": "file", "path": "outputs/repro/repro.c"}],
  "warnings": [],
  "blocked_reason": ""
}
```
""".replace("__KERNEL__", kernel_file.name).replace("__SCRIPT__", test_script.name)
        contract = _extract_kernel_contract(text)
        contract = _validate_kernel_contract_artifacts(contract)
        assert contract.status == "ok"
        assert contract.boot_kernel_path == kernel_file.name
        assert contract.evidence[0]["path"] == "outputs/repro/repro.c"
        assert _kernel_contract_ready_for_test(contract) is True


def test_marker_fallback_blocks_incomplete_handoff():
    contract = _kernel_contract_from_markers(
        target_arch="x86_64",
        boot_kernel_path="",
        reproducer_dir="outputs/repro",
        reproducer_module_path="outputs/repro/repro.ko",
        test_script_path="outputs/repro/test.sh",
        expected_signal="Kernel panic",
    )
    assert contract.status == "blocked"
    assert "boot_kernel_path" in contract.blocked_reason
    assert _kernel_contract_ready_for_test(contract) is False


def test_merge_fills_json_with_marker_fallback():
    with tempfile.NamedTemporaryFile() as kernel_file, tempfile.NamedTemporaryFile() as test_script:
        kernel_file.write(b"MZ\x00\x00")
        kernel_file.flush()
        test_script.write(b"#!/bin/sh\n")
        test_script.flush()
        primary = _extract_kernel_contract("no json")
        fallback = _kernel_contract_from_markers(
            target_arch="x86_64",
            boot_kernel_path=kernel_file.name,
            reproducer_dir="outputs/repro",
            reproducer_module_path="outputs/repro/repro.ko",
            test_script_path=test_script.name,
            expected_signal="Kernel panic",
        )
        merged = _merge_kernel_contract(primary, fallback)
        merged = _validate_kernel_contract_artifacts(merged)
        assert merged.status == "ok"
        assert merged.boot_kernel_path == kernel_file.name
        assert _kernel_contract_ready_for_test(merged) is True


def test_validate_kernel_contract_rejects_elf_vmlinux():
    with tempfile.NamedTemporaryFile() as kernel_file, tempfile.NamedTemporaryFile() as test_script:
        kernel_file.write(b"\x7fELF")
        kernel_file.flush()
        test_script.write(b"#!/bin/sh\n")
        test_script.flush()
        contract = _kernel_contract_from_markers(
            target_arch="x86_64",
            boot_kernel_path=kernel_file.name,
            reproducer_dir="",
            reproducer_module_path="",
            test_script_path=test_script.name,
            expected_signal="Kernel panic",
        )
        validated = _validate_kernel_contract_artifacts(contract)
        assert validated.status == "blocked"
        assert "ELF vmlinux" in validated.blocked_reason
        assert _kernel_contract_ready_for_test(validated) is False


def test_validate_kernel_contract_rejects_missing_expected_signal():
    with tempfile.NamedTemporaryFile() as kernel_file:
        kernel_file.write(b"MZ\x00\x00")
        kernel_file.flush()
        contract = _kernel_contract_from_markers(
            target_arch="x86_64",
            boot_kernel_path=kernel_file.name,
            reproducer_dir="",
            reproducer_module_path="",
            test_script_path="/tmp/missing-test-script.sh",
            expected_signal="",
        )
        validated = _validate_kernel_contract_artifacts(contract)
        assert validated.status == "blocked"
        assert "expected_signal" in validated.blocked_reason


def test_validate_kernel_contract_missing_script_generates_warning():
    with tempfile.NamedTemporaryFile() as kernel_file:
        kernel_file.write(b"MZ\x00\x00")
        kernel_file.flush()
        contract = _kernel_contract_from_markers(
            target_arch="x86_64",
            boot_kernel_path=kernel_file.name,
            reproducer_dir="",
            reproducer_module_path="",
            test_script_path="/tmp/missing-test-script.sh",
            expected_signal="Kernel panic",
        )
        validated = _validate_kernel_contract_artifacts(contract)
        # test_script_path is optional — missing script generates a warning
        # but does not block the contract (supports max_turns recovery)
        assert validated.status == "ok"
        assert any("test_script_path" in w for w in validated.warnings)


def test_route_after_kernel_uses_contract():
    ready_state = {
        "kernel_contract": {
            "status": "ok",
            "target_arch": "x86_64",
            "boot_kernel_path": "/tmp/bzImage",
            "test_script_path": "outputs/repro/test.sh",
            "expected_signal": "Kernel panic",
        }
    }
    blocked_state = {
        "kernel_contract": {
            "status": "blocked",
            "target_arch": "x86_64",
            "boot_kernel_path": "",
            "test_script_path": "outputs/repro/test.sh",
            "expected_signal": "Kernel panic",
        }
    }
    assert route_after_kernel(ready_state) == "test_expert"
    assert route_after_kernel(blocked_state) == "knowledge_base"


if __name__ == "__main__":
    for test in [
        test_extract_kernel_contract_json_with_nested_evidence,
        test_marker_fallback_blocks_incomplete_handoff,
        test_merge_fills_json_with_marker_fallback,
        test_validate_kernel_contract_rejects_elf_vmlinux,
        test_validate_kernel_contract_rejects_missing_expected_signal,
        test_validate_kernel_contract_missing_script_generates_warning,
        test_route_after_kernel_uses_contract,
    ]:
        test()
    print("kernel_contract OK")
