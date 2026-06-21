"""Kernel Expert contract parsing and routing tests."""

from pathlib import Path
import sys

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from agents.kernel_expert import (
    _extract_kernel_contract,
    _kernel_contract_from_markers,
    _kernel_contract_ready_for_test,
    _merge_kernel_contract,
)
from graph.rn_router import route_after_kernel


def test_extract_kernel_contract_json_with_nested_evidence():
    text = """
analysis text

KERNEL_CONTRACT:
```json
{
  "status": "ok",
  "target_arch": "x86_64",
  "vmlinux_path": "/tmp/vmlinux",
  "boot_kernel_path": "/tmp/bzImage",
  "reproducer_dir": "outputs/repro",
  "reproducer_module_path": "outputs/repro/repro.ko",
  "test_script_path": "outputs/repro/test.sh",
  "expected_signal": "Kernel panic",
  "build_status": "passed",
  "evidence": [{"kind": "file", "path": "outputs/repro/repro.c"}],
  "warnings": [],
  "blocked_reason": ""
}
```
"""
    contract = _extract_kernel_contract(text)
    assert contract.status == "ok"
    assert contract.boot_kernel_path == "/tmp/bzImage"
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
    primary = _extract_kernel_contract("no json")
    fallback = _kernel_contract_from_markers(
        target_arch="x86_64",
        boot_kernel_path="/tmp/bzImage",
        reproducer_dir="outputs/repro",
        reproducer_module_path="outputs/repro/repro.ko",
        test_script_path="outputs/repro/test.sh",
        expected_signal="Kernel panic",
    )
    merged = _merge_kernel_contract(primary, fallback)
    assert merged.status == "ok"
    assert merged.boot_kernel_path == "/tmp/bzImage"
    assert _kernel_contract_ready_for_test(merged) is True


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
        test_route_after_kernel_uses_contract,
    ]:
        test()
    print("kernel_contract OK")
