"""Contracts for the persistent SSH-QEMU execution boundary.

These tests deliberately do not start QEMU or patch subprocess.  They verify
the deterministic identity, launch recipe, and explicit blocked outcome when
deployment artifacts are absent.
"""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import DetectionSignals, ExecutionStep, QemuRecipe, TestPlan
from agents.persistent_qemu import (
    PersistentQemuManager,
    _render_execution_script,
    build_qemu_command,
    guest_identity,
    persistent_qemu_paths,
    run_persistent_qemu_test_plan,
)
from tools.run_persistent_qemu_poc import build_plan


def _plan(kernel: Path, *, arch: str = "x86_64") -> TestPlan:
    return TestPlan(
        target_arch=arch,
        boot_kernel_path=str(kernel),
        execution_steps=[{"type": "run_binary", "path": "bin/trigger"}],
        expected_signal="BUG: KASAN",
        detection_signals=DetectionSignals(serial_signals=["BUG: KASAN"]),
        qemu_recipe=QemuRecipe(smp="2"),
    )


def test_identity_binds_kernel_rootfs_arch_and_recipe(tmp_path):
    kernel = tmp_path / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00kernel")
    paths = persistent_qemu_paths("x86_64", runtime_root=tmp_path / "guests")
    paths.image.parent.mkdir(parents=True)
    paths.image.write_bytes(b"rootfs")
    paths.ssh_key.write_text("private", encoding="utf-8")
    first = guest_identity(_plan(kernel), paths)
    changed = guest_identity(_plan(kernel, arch="x86_64"), paths)
    assert first == changed
    kernel.write_bytes(b"MZ\x00\x00changed")
    assert guest_identity(_plan(kernel), paths)["kernel_sha256"] != first["kernel_sha256"]


def test_x86_launch_recipe_uses_loopback_ssh_and_serial_log(tmp_path):
    kernel = tmp_path / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00")
    paths = persistent_qemu_paths("x86_64", runtime_root=tmp_path / "guests")
    paths.image.parent.mkdir(parents=True)
    paths.image.write_bytes(b"rootfs")
    command, _ = build_qemu_command(_plan(kernel), paths, ssh_port=10021)
    rendered = " ".join(command)
    assert "hostfwd=tcp:127.0.0.1:10021-:22" in rendered
    assert f"file:{paths.serial_log}" in rendered
    assert "root=/dev/sda" in rendered
    assert "earlyprintk=serial" in rendered
    assert "-no-reboot" in command


def test_arm64_launch_recipe_matches_virt_serial_disk_and_ssh_contract(tmp_path):
    kernel = tmp_path / "Image"
    kernel.write_bytes(b"arm64-image")
    paths = persistent_qemu_paths("arm64", runtime_root=tmp_path / "guests")
    paths.image.parent.mkdir(parents=True)
    paths.image.write_bytes(b"rootfs")
    command, acceleration = build_qemu_command(_plan(kernel, arch="arm64"), paths, ssh_port=10023)
    rendered = " ".join(command)
    assert command[0] == "qemu-system-aarch64"
    assert "virt" in command[command.index("-machine") + 1]
    assert "cortex-a57" in command[command.index("-cpu") + 1]
    assert "console=ttyAMA0" in rendered
    assert "root=/dev/vda" in rendered
    assert "earlyprintk=serial" in rendered
    assert "virtio-blk-device,drive=rootfs" in rendered
    assert "hostfwd=tcp:127.0.0.1:10023-:22" in rendered
    assert acceleration in {"kvm", "tcg"}


def test_missing_guest_artifacts_are_blocked_without_ephemeral_fallback(tmp_path):
    kernel = tmp_path / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00")
    result = run_persistent_qemu_test_plan(_plan(kernel), attempt=1, runtime_root=tmp_path / "guests")
    assert result.status == "blocked"
    assert result.code == "BLOCKED_PERSISTENT_QEMU"
    assert "provision_command" in result.artifacts


def test_manager_does_not_treat_missing_image_as_a_launchable_guest(tmp_path):
    kernel = tmp_path / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00")
    step, state = PersistentQemuManager(_plan(kernel), runtime_root=tmp_path / "guests").ensure_running()
    assert step.status == "blocked"
    assert state == {}


def test_runner_owned_script_does_not_insmod_for_a_userspace_plan(tmp_path):
    kernel = tmp_path / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00")
    script = _render_execution_script(_plan(kernel), "LUMEN_REPRO_START:case:path")
    assert "insmod" not in script
    assert "./bin/trigger" in script


def test_execution_plan_rejects_module_path_for_userspace_action(tmp_path):
    kernel = tmp_path / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00")
    plan = _plan(kernel)
    plan.execution_steps = [ExecutionStep(type="run_binary", path="modules/reproducer.ko")]
    result = run_persistent_qemu_test_plan(plan, attempt=1, runtime_root=tmp_path / "guests")
    assert result.status == "blocked"
    assert result.code == "BLOCKED_EXECUTION_PLAN"


def test_runner_loads_module_only_when_the_plan_declares_it(tmp_path):
    kernel = tmp_path / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00")
    plan = _plan(kernel)
    plan.execution_steps = [ExecutionStep(type="load_module", path="modules/reproducer.ko")]
    script = _render_execution_script(plan, "LUMEN_REPRO_START:case:path")
    assert "insmod ./modules/reproducer.ko" in script


def test_contract_execution_steps_are_transferred_without_test_script():
    plan = build_plan({
        "target_arch": "x86_64",
        "boot_kernel_path": "/tmp/bzImage",
        "execution_steps": [{"type": "wait", "seconds": 5}],
    })
    assert len(plan.execution_steps) == 1
    assert plan.execution_steps[0].type == "wait"
    assert not hasattr(plan, "test_script_path")
