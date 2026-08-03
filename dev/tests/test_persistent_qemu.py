"""Offline contracts for isolated userspace-C QEMU try-outs."""

from pathlib import Path
import os
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import CallChainOracle, DetectionSignals, ExecutionStep, QemuRecipe, TestPlan as QemuTestPlan, UserspaceReproducer
from agents.persistent_qemu import (
    PersistentQemuManager,
    _check_call_chain_match,
    _render_execution_script,
    build_qemu_command,
    persistent_qemu_paths,
    run_persistent_qemu_test_plan,
)


def _plan(tmp_path: Path, *, arch: str = "x86_64") -> QemuTestPlan:
    kernel = tmp_path / ("Image" if arch == "arm64" else "bzImage")
    kernel.write_bytes(b"arm64-image" if arch == "arm64" else b"MZ\x00\x00kernel")
    source = tmp_path / "repro"
    source.mkdir()
    (source / "repro.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    return QemuTestPlan(
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


def test_call_chain_accepts_fault_leaf_from_rip_before_trace(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = ["leaf+0x13/0x20", "caller+0x22/0x40"]
    plan.call_chain_oracle.required_frames = list(plan.original_call_chain)
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] RIP: 0010:leaf+0x14/0x30",
        "[   1.1] Call Trace:",
        "[   1.2]  caller+0x25/0x40",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


def test_call_chain_keeps_part_symbols_distinct(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = ["iput.part.0", "iput"]
    plan.call_chain_oracle.required_frames = list(plan.original_call_chain)
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] Call Trace:",
        "[   1.1]  iput.part.0+0x4d8/0x7b0",
        "[   1.2]  iput+0x5c/0x80",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


def test_qemu_default_smp_is_deployment_configurable(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    plan.qemu_recipe.smp = ""
    paths = persistent_qemu_paths("x86_64", runtime_root=tmp_path / "guests")
    paths.image.parent.mkdir(parents=True)
    paths.image.write_bytes(b"rootfs")
    monkeypatch.setenv("LUMEN_QEMU_DEFAULT_SMP", "1")
    command, _ = build_qemu_command(plan, paths, ssh_port=10021)
    assert command[command.index("-smp") + 1] == "1"
    plan.qemu_recipe.smp = "2"
    command, _ = build_qemu_command(plan, paths, ssh_port=10021)
    assert command[command.index("-smp") + 1] == "2"


@pytest.mark.parametrize("value", ["0", "129", "one", "1 2"])
def test_qemu_default_smp_rejects_unsafe_values(tmp_path, monkeypatch, value):
    plan = _plan(tmp_path)
    plan.qemu_recipe.smp = ""
    paths = persistent_qemu_paths("x86_64", runtime_root=tmp_path / "guests")
    paths.image.parent.mkdir(parents=True)
    paths.image.write_bytes(b"rootfs")
    monkeypatch.setenv("LUMEN_QEMU_DEFAULT_SMP", value)
    with pytest.raises(ValueError, match="LUMEN_QEMU_DEFAULT_SMP"):
        build_qemu_command(plan, paths, ssh_port=10021)


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
    assert "LUMEN_GUEST_COMPONENT_MISSING:compiler:gcc" in script
    assert "/usr/bin/" not in script
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


def test_original_log_chain_cannot_be_shortened(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = ["strlen", "audit_log", "smack_log", "smk_access"]
    plan.call_chain_oracle.required_frames = ["smk_access"]
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] Call Trace:",
        "[   1.1]  smk_access+0x1/0x2",
    ])
    match = _check_call_chain_match(content, plan)
    assert "strlen" in match["missing_frames"]
    assert "audit_log" in match["missing_frames"]
    assert "smack_log" in match["missing_frames"]
    assert match["frame_order_matched"] is False


def test_call_chain_order_uses_trace_not_printk_or_question_mark_frames(tmp_path):
    plan = _plan(tmp_path)
    plan.call_chain_oracle.required_frames = ["leaf", "mid", "caller"]
    plan.call_chain_oracle.required_frame_order = [
        ["leaf", "mid"],
        ["mid", "can_receive"],
        ["can_receive", "caller"],
    ]
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "vcan0: mid: diagnostic printk before the stack",
        "BUG: target fault",
        "[   1.0] Call Trace:",
        "[   1.1]  leaf+0x1/0x2",
        "[   1.2]  ? caller+0x1/0x2",
        "[   1.3]  mid+0x1/0x2",
        "[   1.4]  can_receive+0x1/0x2",
        "[   1.5]  caller+0x1/0x2",
        "[   1.6]  ret_from_fork+0x1/0x2",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


def test_call_chain_accepts_one_member_of_an_alternative_group(tmp_path):
    plan = _plan(tmp_path)
    plan.call_chain_oracle.required_frames = [
        "j1939_sock_pending_del", "j1939_session_put", "j1939_session_destroy",
        "j1939_xtp_rx_abort_one",
    ]
    plan.call_chain_oracle.required_frame_alternatives = [[
        "j1939_session_put", "j1939_session_destroy",
    ]]
    plan.call_chain_oracle.required_frame_order = [[
        "j1939_xtp_rx_abort_one", "j1939_session_put",
    ]]
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "BUG: KASAN: use-after-free in j1939_sock_pending_del",
        "j1939_sock_pending_del",
        "j1939_session_destroy",
        "j1939_xtp_rx_abort_one",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


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


def test_shutdown_force_kills_stuck_qemu(tmp_path, monkeypatch):
    plan = _plan(tmp_path)
    manager = PersistentQemuManager(plan, runtime_root=tmp_path / "guests")
    manager.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    manager.paths.state_file.write_text('{"pid": 4242}', encoding="utf-8")

    alive = {"value": True}
    signals = []
    clock = {"value": 0.0}

    def fake_alive(pid):
        return alive["value"]

    def fake_kill(pid, signal):
        signals.append(signal)
        if signal == 9:
            alive["value"] = False

    def fake_monotonic():
        clock["value"] += 11.0
        return clock["value"]

    monkeypatch.setattr("agents.persistent_qemu._pid_is_live", fake_alive)
    monkeypatch.setattr("agents.persistent_qemu.os.kill", fake_kill)
    monkeypatch.setattr("agents.persistent_qemu.time.monotonic", fake_monotonic)

    result = manager.shutdown()

    assert result.status == "ok"
    assert signals == [15, 9]
    assert "SIGKILL" in result.message


def test_shutdown_reaps_manager_owned_qemu_without_pid_probe(tmp_path):
    plan = _plan(tmp_path)
    manager = PersistentQemuManager(plan, runtime_root=tmp_path / "guests")
    manager.paths.state_file.parent.mkdir(parents=True, exist_ok=True)
    manager.paths.state_file.write_text('{"pid": 4242}', encoding="utf-8")

    class FakeProcess:
        pid = 4242

        def __init__(self):
            self.returncode = None
            self.terminated = False

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = 0

        def wait(self, timeout=None):
            return self.returncode

    process = FakeProcess()
    manager._process = process

    result = manager.shutdown()

    assert result.status == "ok"
    assert process.terminated is True
    assert manager._process is None


def test_call_chain_order_keeps_question_marked_fault_leaf(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = ["strlen", "audit_log", "caller"]
    plan.call_chain_oracle.required_frames = ["caller"]
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] Call Trace:",
        "[   1.1]  ? strlen+0x2c/0x70",
        "[   1.2]  audit_log+0x1/0x2",
        "[   1.3]  caller+0x1/0x2",
        "[   1.4]  </TASK>",
        "[   1.5]  RIP: 0010:strlen+0x2c/0x70",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


def test_call_chain_order_ignores_reversed_duplicate_contract_pairs(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = ["leaf", "mid", "caller"]
    plan.call_chain_oracle.required_frames = ["leaf", "mid", "caller"]
    plan.call_chain_oracle.required_frame_order = [
        ["caller", "mid"],
        ["mid", "leaf"],
    ]
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] Call Trace:",
        "[   1.1]  leaf+0x1/0x2",
        "[   1.2]  mid+0x1/0x2",
        "[   1.3]  caller+0x1/0x2",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


def test_call_chain_ignores_non_adjacent_reversed_contract_pair(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = ["leaf", "middle", "caller"]
    plan.call_chain_oracle.required_frames = list(plan.original_call_chain)
    plan.call_chain_oracle.required_frame_order = [["caller", "leaf"]]
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] Call Trace:",
        "[   1.1]  leaf+0x1/0x2",
        "[   1.2]  middle+0x1/0x2",
        "[   1.3]  caller+0x1/0x2",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


def test_call_chain_ignores_mixed_supplementary_order_edges(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = ["leaf", "caller"]
    plan.call_chain_oracle.required_frames = ["leaf", "caller", "supplement"]
    plan.call_chain_oracle.required_frame_order = [["supplement", "caller"]]
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] Call Trace:",
        "[   1.1]  leaf+0x1/0x2",
        "[   1.2]  caller+0x1/0x2",
        "[   1.3]  supplement+0x1/0x2",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


def test_call_chain_selects_complete_later_trace_block(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = ["leaf", "caller"]
    plan.call_chain_oracle.required_frames = ["caller"]
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] Call Trace:",
        "[   1.1]  leaf+0x1/0x2",
        "[   1.2]  </TASK>",
        "intermediate diagnostic output",
        "[   2.0] Call Trace:",
        "[   2.1]  leaf+0x1/0x2",
        "[   2.2]  caller+0x1/0x2",
        "[   2.3]  </TASK>",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["required_frames_found"] == ["leaf", "caller"]
    assert match["frame_order_matched"] is True

def test_call_chain_matches_symbol_offsets_from_different_build(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = ["diFree+0x13d/0x2dc0", "jfs_evict_inode+0x2c9/0x370"]
    plan.call_chain_oracle.required_frames = list(plan.original_call_chain)
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] Call Trace:",
        "[   1.1]  diFree+0x143/0x2d70",
        "[   1.2]  jfs_evict_inode+0x2c3/0x360",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["frame_order_matched"] is True


def test_call_chain_strips_source_location_annotations(tmp_path):
    plan = _plan(tmp_path)
    plan.original_call_chain = [
        "mempool_alloc_noprof mm/mempool.c:402",
        "bch2_data_thread fs/bcachefs/chardev.c:315",
    ]
    plan.call_chain_oracle.required_frames = list(plan.original_call_chain)
    content = "\n".join([
        "LUMEN_REPRO_START:case:path",
        "[   1.0] Call Trace:",
        "[   1.1]  mempool_alloc_noprof+0x10/0x20",
        "[   1.2]  bch2_data_thread+0x30/0x40",
    ])
    match = _check_call_chain_match(content, plan)
    assert match["missing_frames"] == []
    assert match["required_frames_found"] == [
        "mempool_alloc_noprof", "bch2_data_thread",
    ]
    assert match["frame_order_matched"] is True


def test_qemu_deployment_inputs_are_configurable(tmp_path, monkeypatch):
    image_root = tmp_path / "qemu-images"
    monkeypatch.setenv("LUMEN_QEMU_IMAGE_ROOT", str(image_root))
    paths = persistent_qemu_paths("x86_64")
    assert paths.image == image_root / "x86_64" / "debian.img"

    plan = _plan(tmp_path)
    monkeypatch.setenv("LUMEN_QEMU_SSH_USER", "lumen")
    manager = PersistentQemuManager(plan, runtime_root=tmp_path / "runtime")
    assert manager._ssh_base(10021)[-1] == "lumen@127.0.0.1"

    monkeypatch.setenv("LUMEN_QEMU_GUEST_WORKDIR", "/var/tmp/lumen-poc")
    script = _render_execution_script(plan, "LUMEN_REPRO_START:case:path")
    assert "mkdir -p /var/tmp/lumen-poc/bin" in script
    assert "cd /var/tmp/lumen-poc/reproducer" in script
    assert "cd /var/tmp/lumen-poc" in script


@pytest.mark.parametrize("name", ["/", "relative/path", "/tmp/../escape", "/tmp/space dir"])
def test_qemu_guest_workdir_rejects_unsafe_values(tmp_path, monkeypatch, name):
    plan = _plan(tmp_path)
    monkeypatch.setenv("LUMEN_QEMU_GUEST_WORKDIR", name)
    with pytest.raises(ValueError, match="LUMEN_QEMU_GUEST_WORKDIR"):
        _render_execution_script(plan, "LUMEN_REPRO_START:case:path")
