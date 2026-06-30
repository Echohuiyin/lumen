"""Deterministic test runner contract tests.

These tests avoid real QEMU execution and verify failure classification paths
that must stay stable for routing and knowledge-base records.
"""

from pathlib import Path
import sys
import tempfile

project_root = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(project_root))

from agents.contracts import TestPlan, model_to_dict
from agents.test_runner import run_qemu_test_plan
from config import load_config


def test_missing_arch_blocks_without_guessing():
    with tempfile.NamedTemporaryFile() as f:
        f.write(b"MZ\x00\x00")
        f.flush()
        result = run_qemu_test_plan(
            TestPlan(target_arch="", boot_kernel_path=f.name),
            attempt=1,
        )
    assert result.code == "BLOCKED_NO_TARGET_ARCH"
    assert result.status == "blocked"
    assert result.test_passed is False


def test_missing_kernel_is_terminal_blocker():
    result = run_qemu_test_plan(
        TestPlan(target_arch="x86_64", boot_kernel_path="/tmp/missing-bzImage"),
        attempt=1,
    )
    assert result.code == "BLOCKED_BOOT_KERNEL_MISSING"
    assert result.status == "blocked"


def test_elf_vmlinux_rejected_before_qemu():
    with tempfile.NamedTemporaryFile() as f:
        f.write(b"\x7fELF")
        f.flush()
        result = run_qemu_test_plan(
            TestPlan(target_arch="x86_64", boot_kernel_path=f.name),
            attempt=1,
        )
    assert result.code == "BLOCKED_NOT_BOOTABLE_KERNEL"
    assert result.status == "blocked"
    assert result.steps == []


def test_legacy_config_name_falls_back_to_config_json():
    config = load_config("maintenance_config.json", fallback_to_claude_settings=False)
    assert "default" in config
    assert "backend" in config["default"]


def test_contract_serializes_to_dict():
    result = run_qemu_test_plan(
        TestPlan(target_arch="", boot_kernel_path=""),
        attempt=1,
    )
    data = model_to_dict(result)
    assert data["code"] == "BLOCKED_NO_BOOT_KERNEL"
    assert "steps" in data


if __name__ == "__main__":
    for test in [
        test_missing_arch_blocks_without_guessing,
        test_missing_kernel_is_terminal_blocker,
        test_elf_vmlinux_rejected_before_qemu,
        test_legacy_config_name_falls_back_to_config_json,
        test_contract_serializes_to_dict,
    ]:
        test()
    print("test_runner_contract OK")
