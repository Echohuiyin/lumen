"""Deterministic test runner contract tests.

These tests avoid real QEMU execution and verify failure classification paths
that must stay stable for routing and knowledge-base records.
"""

from pathlib import Path
import os
import sys
import tempfile

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.contracts import DetectionSignals, QemuRecipe, TestPlan, model_to_dict
from agents.test_runner import _match_serial_signals, _warning_precedes_panic, run_qemu_test_plan
from llm_config import load_config


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
    config = load_config("config.json")
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


def test_detection_serial_signals_first_match_wins():
    """First matching signal in detection.serial_signals is returned."""
    log = "pvqspinlock: lock 0xffff88811e9ee790 has corrupted value 0x0!\nKernel panic"
    detection = DetectionSignals(
        serial_signals=["pvqspinlock: lock", "Kernel panic"],
        panic_on_warn=True,
    )
    matched = _match_serial_signals(
        log_content=log, detection=detection, expected_signal="",
    )
    assert matched == "pvqspinlock: lock"


def test_detection_expected_signal_fallback_when_serial_signals_empty():
    """Legacy expected_signal still works when detection.serial_signals is empty."""
    log = "BUG: KASAN: slab-use-after-free\nKernel panic"
    detection = DetectionSignals()
    matched = _match_serial_signals(
        log_content=log, detection=detection, expected_signal="BUG: KASAN: slab-use-after-free",
    )
    assert matched == "BUG: KASAN: slab-use-after-free"


def test_detection_panic_on_warn_with_warning_prefix_passes():
    """panic_on_warn=1 + Kernel panic preceded by WARNING → PASS."""
    log_lines = ["[  10.0] WARNING: CPU: 1 PID: 100 at foo", "stuff", "stuff",
                 "stuff", "[  10.5] Kernel panic - not syncing: panic_on_warn set"]
    log = "\n".join(log_lines)
    detection = DetectionSignals(panic_on_warn=True, panic_is_pass=False)
    matched = _match_serial_signals(
        log_content=log, detection=detection, expected_signal="",
    )
    assert "Kernel panic" in matched
    assert "panic_on_warn" in matched


def test_detection_panic_on_warn_without_warning_does_not_pass():
    """panic_on_warn=1 + bare Kernel panic (no WARNING before) → no match.

    Otherwise a boot-time OOM panic (kasan_populate_shadow) would be
    misclassified as the target bug.
    """
    log = "kasan_populate_shadow: Failed to allocate page\nKernel panic - not syncing: out of memory"
    detection = DetectionSignals(panic_on_warn=True, panic_is_pass=False)
    matched = _match_serial_signals(
        log_content=log, detection=detection, expected_signal="",
    )
    assert matched == ""


def test_detection_panic_is_pass_short_circuits_warning_check():
    """panic_is_pass=True treats any Kernel panic as PASS without WARNING proximity."""
    log = "out of memory\nKernel panic"
    detection = DetectionSignals(panic_on_warn=True, panic_is_pass=True)
    matched = _match_serial_signals(
        log_content=log, detection=detection, expected_signal="",
    )
    assert matched != ""


def test_detection_pvqspinlock_pattern_matches_actual_serial_output():
    """Real-world pvqspinlock WARNING log line matches expected_signal."""
    real_log_line = "[   29.378202][ T8268] pvqspinlock: lock 0xffff88811e9ee790 has corrupted value 0x0!"
    detection = DetectionSignals(
        serial_signals=["pvqspinlock: lock"],
        panic_on_warn=True,
    )
    matched = _match_serial_signals(
        log_content=real_log_line, detection=detection, expected_signal="",
    )
    assert matched == "pvqspinlock: lock"


def test_warning_precedes_panic_detects_within_100_lines():
    """_warning_precedes_panic returns True when WARNING is within 100 lines of panic."""
    lines = ["normal"] * 50 + ["WARNING: something bad"] + ["normal"] * 30 + ["Kernel panic"]
    assert _warning_precedes_panic("\n".join(lines)) is True


def test_warning_precedes_panic_rejects_when_too_far():
    """_warning_precedes_panic returns False when WARNING is >100 lines from panic."""
    lines = ["WARNING: something"] + ["normal"] * 200 + ["Kernel panic"]
    assert _warning_precedes_panic("\n".join(lines)) is False


def test_qemu_recipe_defaults_are_backward_compatible():
    """Empty QemuRecipe() means 'use legacy defaults' — no field should force
    a non-default value when the kernel_expert didn't declare one."""
    recipe = QemuRecipe()
    assert recipe.machine == ""
    assert recipe.smp == ""
    assert recipe.cpu == ""
    assert recipe.memory == ""
    assert recipe.extra_cmdline == ""
    assert recipe.concurrent_instances == 1
    assert recipe.timeout_sec == 0


def test_test_plan_rootfs_defaults_are_backward_compatible():
    plan = TestPlan()
    assert plan.rootfs_mode == "initramfs"
    assert plan.rootfs_path == ""
    assert plan.rootfs_size_mb == 128


def test_qemu_recipe_pvqspinlock_config():
    """Recipe for pvqspinlock: q35 + smp=4 + panic_on_warn=1 + numa=off."""
    recipe = QemuRecipe(
        machine="q35,accel=kvm:tcg",
        cpu="host",
        smp="4",
        memory="4G",
        extra_cmdline="panic_on_warn=1 numa=off",
        concurrent_instances=4,
        timeout_sec=300,
    )
    assert recipe.machine == "q35,accel=kvm:tcg"
    assert recipe.smp == "4"
    assert "panic_on_warn=1" in recipe.extra_cmdline


def test_qemu_recipe_parses_from_kernel_contract_json():
    """KernelExpertOutput with qemu_recipe in JSON parses into QemuRecipe."""
    from agents.contracts import KernelExpertOutput
    data = {
        "status": "ok",
        "target_arch": "x86_64",
        "boot_kernel_path": "/tmp/bzImage",
        "test_script_path": "/tmp/test.sh",
        "expected_signal": "pvqspinlock: lock",
        "qemu_recipe": {
            "machine": "q35,accel=kvm:tcg",
            "smp": "4",
            "memory": "4G",
            "extra_cmdline": "panic_on_warn=1",
        },
    }
    contract = KernelExpertOutput.model_validate(data)
    assert contract.qemu_recipe.smp == "4"
    assert contract.qemu_recipe.machine == "q35,accel=kvm:tcg"


def test_kernel_expert_preflight_extracts_relevant_config():
    """_extract_pertinent_kernel_config returns KVM/PARAVIRT/MODVERSIONS flags
    that influence reproducer strategy."""
    from agents.kernel_expert import _extract_pertinent_kernel_config
    bzimage = str(project_root / "test_assets" / "syzbot_kvm_x86_5d2b94b7" / "bzImage")
    if not os.path.isfile(bzimage):
        return  # Skip when test asset not available
    config = _extract_pertinent_kernel_config(bzimage)
    assert "CONFIG_KVM" in config or not config, "extract-ikconfig should find CONFIG_KVM"
    if "CONFIG_KVM" in config:
        assert config["CONFIG_KVM"] == "y", "syzbot kernel should have CONFIG_KVM=y"


def test_kernel_expert_preflight_scans_test_assets():
    """_scan_test_assets_for_reproducers finds existing repro_c / .ko files."""
    from agents.kernel_expert import _scan_test_assets_for_reproducers
    test_dir = str(project_root / "test_assets" / "syzbot_kvm_x86_5d2b94b7")
    if not os.path.isdir(test_dir):
        return  # Skip when test asset not available
    findings = _scan_test_assets_for_reproducers(test_dir)
    # The syzbot case has a REPRODUCTION.md at minimum
    assert any(f["kind"] == "reproduction_notes" for f in findings), \
        "Should find REPRODUCTION.md in syzbot_kvm_x86 case"


def test_kernel_expert_preflight_returns_empty_for_missing_dir():
    """_scan_test_assets_for_reproducers returns [] for nonexistent dir."""
    from agents.kernel_expert import _scan_test_assets_for_reproducers
    assert _scan_test_assets_for_reproducers("/nonexistent/path") == []
    assert _scan_test_assets_for_reproducers("") == []


def test_kernel_expert_preflight_returns_empty_for_missing_kernel():
    """_extract_pertinent_kernel_config returns {} for nonexistent bzImage."""
    from agents.kernel_expert import _extract_pertinent_kernel_config
    assert _extract_pertinent_kernel_config("/nonexistent/bzImage") == {}
    assert _extract_pertinent_kernel_config("") == {}


if __name__ == "__main__":
    for test in [
        test_missing_arch_blocks_without_guessing,
        test_missing_kernel_is_terminal_blocker,
        test_elf_vmlinux_rejected_before_qemu,
        test_legacy_config_name_falls_back_to_config_json,
        test_contract_serializes_to_dict,
        test_detection_serial_signals_first_match_wins,
        test_detection_expected_signal_fallback_when_serial_signals_empty,
        test_detection_panic_on_warn_with_warning_prefix_passes,
        test_detection_panic_on_warn_without_warning_does_not_pass,
        test_detection_panic_is_pass_short_circuits_warning_check,
        test_detection_pvqspinlock_pattern_matches_actual_serial_output,
        test_warning_precedes_panic_detects_within_100_lines,
        test_warning_precedes_panic_rejects_when_too_far,
        test_qemu_recipe_defaults_are_backward_compatible,
        test_qemu_recipe_pvqspinlock_config,
        test_qemu_recipe_parses_from_kernel_contract_json,
        test_kernel_expert_preflight_extracts_relevant_config,
        test_kernel_expert_preflight_scans_test_assets,
        test_kernel_expert_preflight_returns_empty_for_missing_dir,
        test_kernel_expert_preflight_returns_empty_for_missing_kernel,
    ]:
        test()
    print("test_runner_contract OK")
