"""Offline contracts for isolated userspace-C QEMU try-outs."""

from pathlib import Path
import sys

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import CallChainOracle, DetectionSignals, ExecutionStep, QemuRecipe, TestPlan, UserspaceReproducer
from agents.persistent_qemu import (
    PersistentQemuManager,
    _check_call_chain_match,
    _render_execution_script,
    build_qemu_command,
    persistent_qemu_paths,
    run_persistent_qemu_test_plan,
)


def _plan(tmp_path: Path, *, arch: str = "x86_64") -> TestPlan:
    kernel = tmp_path / ("Image" if arch == "arm64" else "bzImage")
    kernel.write_bytes(b"arm64-image" if arch == "arm64" else b"MZ\x00\x00kernel")
    source = tmp_path / "repro"
    source.mkdir()
    (source / "repro.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    return TestPlan(
        target_arch=arch, boot_kernel_path=str(kernel), reproducer_dir=str(source),
        reproducer=UserspaceReproducer(source_dir=str(source), source_files=["repro.c"], entry_source="repro.c", output_binary="trigger"),
        execution_steps=[ExecutionStep(type="run_binary", path="bin/trigger")],
        expected_signal="target fault",
        detection_signals=DetectionSignals(serial_signals=["target fault"]),
        qemu_recipe=QemuRecipe(smp="2"),
        reproduction_case_id="case", target_path_id="path", target_contexts=["target_subsystem"],
        require_causal_reproduction=True,
        call_chain_oracle=CallChainOracle(
            fault_signatures=["target fault"], required_frames=["target_frame"],
            required_frame_order=[], target_subsystems=["target_subsystem"],
        ),
    )


def test_x86_launch_recipe_uses_loopback_ssh_and_serial_log(tmp_path):
    plan = _plan(tmp_path)
    paths = persistent_qemu_paths("x86_64", runtime_root=tmp_path / "guests")
    paths.image.parent.mkdir(parents=True)
    paths.image.write_bytes(b"rootfs")
    command, _ = build_qemu_command(plan, paths, ssh_port=10021)
    rendered = " ".join(command)
    assert "hostfwd=tcp:127.0.0.1:10021-:22" in rendered
    assert f"file:{paths.serial_log}" in rendered
    assert "root=/dev/sda" in rendered


def test_arm64_launch_recipe_uses_expected_console_and_disk(tmp_path):
    plan = _plan(tmp_path, arch="arm64")
    paths = persistent_qemu_paths("arm64", runtime_root=tmp_path / "guests")
    paths.image.parent.mkdir(parents=True)
    paths.image.write_bytes(b"rootfs")
    command, _ = build_qemu_command(plan, paths, ssh_port=10023)
    rendered = " ".join(command)
    assert command[0] == "qemu-system-aarch64"
    assert "console=ttyAMA0" in rendered
    assert "root=/dev/vda" in rendered
    assert "virtio-blk-pci,drive=rootfs" in rendered
    assert "virtio-net-pci,netdev=net0" in rendered


def test_missing_guest_artifacts_are_blocked_without_reuse(tmp_path):
    result = run_persistent_qemu_test_plan(_plan(tmp_path), attempt=1, runtime_root=tmp_path / "guests")
    assert result.status == "blocked"
    assert result.code == "BLOCKED_PERSISTENT_QEMU"


def test_runner_compiles_c_and_never_loads_a_module(tmp_path):
    script = _render_execution_script(_plan(tmp_path), "LUMEN_REPRO_START:case:path")
    assert "gcc repro.c" in script
    assert "./bin/trigger" in script
    assert "insmod" not in script
    assert "load_module" not in script


def test_stage_poc_copies_declared_sources_only(tmp_path):
    plan = _plan(tmp_path)
    # A session source directory also contains prior try-out artifacts.  The
    # runner must not recursively copy that directory into the next stage.
    nested = Path(plan.reproducer_dir) / "tryouts" / "tryout-01" / "qemu-ssh"
    nested.mkdir(parents=True)
    (nested / "old-serial.log").write_text("old", encoding="utf-8")
    manager = PersistentQemuManager(plan, runtime_root=tmp_path / "guests")
    stage, _ = manager._stage_poc()
    assert (stage / "reproducer" / "repro.c").is_file()
    assert not (stage / "reproducer" / "tryouts").exists()


def test_fault_injection_is_allowlisted_and_rendered(tmp_path):
    plan = _plan(tmp_path)
    plan.execution_steps = [
        ExecutionStep(type="fault_injection", profile="fail_page_alloc", probability=25, interval=2, times=3),
        ExecutionStep(type="run_binary", path="bin/trigger"),
    ]
    script = _render_execution_script(plan, "LUMEN_REPRO_START:case:path")
    assert "/sys/kernel/debug/fail_page_alloc" in script
    assert "probability" in script


def test_call_chain_requires_post_start_frames_and_context(tmp_path):
    plan = _plan(tmp_path)
    content = "\n".join([
        "boot noise target_frame",
        "LUMEN_REPRO_START:case:path",
        "target fault",
        "target_subsystem: target_frame",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


def test_call_chain_accepts_leaf_to_caller_stack_orientation(tmp_path):
    plan = _plan(tmp_path)
    plan.call_chain_oracle.required_frames = ["leaf", "caller"]
    plan.call_chain_oracle.required_frame_order = [["caller", "leaf"]]
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "leaf",
        "caller",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True
    assert match["frame_order_direction"] == "reverse"


if __name__ == "__main__":
    import tempfile
    for test in (
        test_x86_launch_recipe_uses_loopback_ssh_and_serial_log,
        test_arm64_launch_recipe_uses_expected_console_and_disk,
        test_missing_guest_artifacts_are_blocked_without_reuse,
        test_runner_compiles_c_and_never_loads_a_module,
        test_fault_injection_is_allowlisted_and_rendered,
        test_call_chain_requires_post_start_frames_and_context,
    ):
        with tempfile.TemporaryDirectory() as directory:
            test(Path(directory))
    print("persistent_qemu OK")
