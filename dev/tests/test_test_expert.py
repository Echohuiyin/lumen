"""Focused offline tests for the isolated Test Expert handoff."""

from pathlib import Path
import os
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import CallChainOracle, KernelExpertOutput, TestResultContract, UserspaceReproducer
from agents.persistent_qemu import PersistentQemuPaths
from agents.test_expert import _append_attempt_output, _attempt_runtime_root, _augment_kernel_feedback, _build_plan, _copy_base_image, _mount_detach_path_feedback, _promote_guest_capability_block, _semantic_review, test_expert_node
from agents.test_expert import _strict_call_chain_oracle


def _contract(root: Path) -> KernelExpertOutput:
    source = root / "repro"
    source.mkdir()
    (source / "repro.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    kernel = root / "bzImage"
    kernel.write_bytes(b"MZ\x00\x00")
    return KernelExpertOutput(
        status="ok", target_arch="x86_64", boot_kernel_path=str(kernel),
        root_cause="diagnostic userspace ABI triggers the verified path",
        expected_signal="target fault",
        call_chain_oracle=CallChainOracle(
            fault_signatures=["target fault"], required_frames=["target_frame"],
            target_subsystems=["target"],
        ),
        reproducer=UserspaceReproducer(
            source_dir=str(source), source_files=["repro.c"], entry_source="repro.c",
            output_binary="lumen-repro", run_args=["--once"],
        ),
    )


def test_kernel_feedback_includes_bounded_runtime_evidence(tmp_path):
    serial = tmp_path / "serial.log"
    serial.write_text(
        "boot noise\n"
        "LUMEN_REPRO_START:case:path\n"
        "[  1.0] lumen-diagnostic[1]: segfault in libc.so.6\n"
        "[  1.1] vcan0: j1939_xtp_rx_rts_session_active: connection exists\n"
        "[  1.2] ocfs2: Unknown parameter 'local'\n"
        "[  1.3] ocfs2: Invalid cluster_stack option\n",
        encoding="utf-8",
    )
    ssh_output = tmp_path / "ssh-command.log"
    ssh_output.write_text(
        "LUMEN_DIAGNOSTIC_NO_OCFS2_MOUNT: no mounted OCFS2 filesystem was available\n",
        encoding="utf-8",
    )
    result = TestResultContract(
        status="failed", code="FAILED_SIGNAL_NOT_FOUND", summary="no target signal",
        kernel_feedback="revise trigger",
        artifacts={"serial_log": str(serial), "ssh_output": str(ssh_output)},
    )

    feedback = _augment_kernel_feedback(result)

    assert "segfault" in feedback
    assert "INVALID_USERSPACE_CRASH" in feedback
    assert "j1939_xtp_rx_rts_session_active" in feedback
    assert "Unknown parameter 'local'" in feedback
    assert "Invalid cluster_stack option" in feedback
    assert "LUMEN_DIAGNOSTIC_NO_OCFS2_MOUNT" in feedback
    assert "boot noise" not in feedback


def test_test_expert_attempt_output_preserves_prior_rounds(tmp_path):
    output = tmp_path / "test_expert.txt"
    _append_attempt_output(output, "TRY-OUT: 1/10\nTEST STATUS: failed\n")
    _append_attempt_output(output, "TRY-OUT: 2/10\nTEST STATUS: passed\n")

    text = output.read_text(encoding="utf-8")
    assert text.count("TRY-OUT:") == 2
    assert "TRY-OUT: 1/10" in text
    assert "TRY-OUT: 2/10" in text


def test_mount_detach_feedback_preserves_dentry_for_gadgetfs(tmp_path):
    stage = tmp_path / "poc"
    source_dir = stage / "reproducer"
    source_dir.mkdir(parents=True)
    (source_dir / "gadgetfs_uaf.c").write_text(
        "mount(\"gadgetfs\", dir, \"gadgetfs\", 0, NULL);\n"
        "umount2(dir, MNT_DETACH);\n"
        "openat(AT_FDCWD, endpoint, O_RDWR);\n",
        encoding="utf-8",
    )
    result = TestResultContract(
        status="failed", code="FAILED_SIGNAL_NOT_FOUND", summary="no target signal",
        artifacts={"poc_stage": str(stage)},
    )

    feedback = _mount_detach_path_feedback(result)

    assert "MOUNT_DENTRY_LIFETIME_FEEDBACK" in feedback
    assert "O_PATH|O_DIRECTORY" in feedback
    assert "openat(dirfd, endpoint)" in feedback


def test_kernel_feedback_keeps_historical_userspace_crash_constraint():
    result = TestResultContract(
        status="failed", code="FAILED_CALL_CHAIN_MISMATCH", summary="target signal mismatch",
        kernel_feedback="Review the latest call-chain evidence.",
    )
    previous_rounds = [{
        "kernel_feedback": (
            "INVALID_USERSPACE_CRASH: the guest reproducer crashed in userspace; "
            "repair C safety. Runtime evidence: pthread_create segfault in libc.so.6"
        ),
    }]

    feedback = _augment_kernel_feedback(result, previous_rounds)

    assert "HISTORICAL_USERSPACE_CRASH_CONSTRAINT" in feedback
    assert "pthread_create segfault" in feedback


def test_guest_pthread_runtime_incompatibility_is_retryable_environment_evidence(tmp_path):
    ssh_output = tmp_path / "ssh-command.log"
    ssh_output.write_text(
        "LUMEN_GUEST_RUNTIME_INCOMPATIBLE:pthread_clone\n"
        "diagnostic-test: Segmentation fault\n",
        encoding="utf-8",
    )
    result = TestResultContract(
        status="failed", code="FAILED_SIGNAL_NOT_FOUND", summary="no target signal",
        kernel_feedback="revise trigger",
        artifacts={"ssh_output": str(ssh_output)},
    )

    promoted = _promote_guest_capability_block(result)
    assert promoted.status == "failed"
    assert promoted.code == "FAILED_GUEST_RUNTIME_INCOMPATIBLE"
    assert "environment evidence" in promoted.kernel_feedback

    feedback = _augment_kernel_feedback(promoted)
    assert "LUMEN_GUEST_RUNTIME_INCOMPATIBLE:pthread_clone" in feedback
    assert "environment evidence" in feedback
    assert "INVALID_USERSPACE_CRASH" not in feedback


def test_test_plan_compiles_userspace_source_inside_guest():
    with tempfile.TemporaryDirectory() as directory:
        plan = _build_plan(_contract(Path(directory)))
        assert plan.reproducer.language == "c"
        assert plan.reproducer.artifact_type == "userspace"
        assert plan.execution_steps[-1].type == "run_binary"
        assert plan.execution_steps[-1].path == "bin/lumen-repro"
        assert plan.call_chain_oracle.required_frames == ["target_frame"]


def test_unresolved_reproducer_arguments_are_blocked_before_qemu(tmp_path):
    contract = _contract(tmp_path)
    contract.reproducer.run_args = ["--bind-path", "<validated-driver-bind-path>"]
    try:
        _build_plan(contract)
    except ValueError as exc:
        assert "unresolved reproducer argument placeholder" in str(exc)
    else:
        raise AssertionError("unresolved guest argument must not reach QEMU")


def test_inline_source_frames_are_not_required_as_runtime_frames(tmp_path):
    contract = _contract(tmp_path)
    contract.original_call_chain = [
        "caller",
        "inline_helper [static inline, inlined into caller]",
        "leaf",
    ]
    contract.call_chain_oracle.required_frames = list(contract.original_call_chain)
    plan = _build_plan(contract)
    assert plan.original_call_chain == contract.original_call_chain
    assert plan.call_chain_oracle.required_frames == ["caller", "leaf"]


def test_architecture_wrappers_are_context_only(tmp_path):
    contract = _contract(tmp_path)
    contract.original_call_chain = [
        "ocfs2_block_group_set_bits",
        "ocfs2_move_extents",
        "__arm64_sys_ioctl",
        "el0t_64_sync",
    ]
    contract.call_chain_oracle.required_frames = list(contract.original_call_chain)
    oracle = _strict_call_chain_oracle(contract)
    assert oracle.required_frames == ["ocfs2_block_group_set_bits", "ocfs2_move_extents"]
    assert "__arm64_sys_ioctl" in oracle.allowed_wrapper_frames
    assert "el0t_64_sync" in oracle.allowed_wrapper_frames


def test_required_top_frames_bound_runtime_oracle(tmp_path):
    contract = _contract(tmp_path)
    contract.original_call_chain = ["fault", "caller", "context_sensitive_lower"]
    contract.call_chain_oracle.required_top_frames = ["fault", "caller"]
    contract.call_chain_oracle.required_frames = list(contract.original_call_chain)
    oracle = _strict_call_chain_oracle(contract)
    assert oracle.required_top_frames == ["fault", "caller"]
    assert oracle.required_frames == ["fault", "caller"]


def test_legacy_required_frames_use_only_bounded_prefix(tmp_path):
    contract = _contract(tmp_path)
    contract.original_call_chain = ["fault", "caller", "context_sensitive_lower", "syscall_wrapper"]
    contract.call_chain_oracle.required_frames = list(contract.original_call_chain)
    oracle = _strict_call_chain_oracle(contract)
    assert oracle.required_frames == ["fault", "caller", "context_sensitive_lower"]

def test_qemu_runtime_root_is_configurable(tmp_path, monkeypatch):
    scratch = tmp_path / "scratch"
    session = tmp_path / "session-id"
    monkeypatch.setenv("LUMEN_QEMU_RUNTIME_ROOT", str(scratch))

    actual = _attempt_runtime_root(str(session), 3)

    assert actual == scratch.resolve() / "session-id" / "tryouts" / "tryout-03" / "qemu-ssh"


def test_declared_rootfs_uses_co_located_ssh_key(tmp_path, monkeypatch):
    custom_dir = tmp_path / "case-root"
    custom_dir.mkdir()
    image = custom_dir / "debian.img"
    image.write_bytes(b"sparse-image-placeholder")
    sibling_key = custom_dir / "lumen_qemu_ed25519"
    sibling_key.write_text("case-specific-key\n", encoding="utf-8")

    base_dir = tmp_path / "base" / "x86_64"
    base_dir.mkdir(parents=True)
    base_image = base_dir / "debian.img"
    base_image.write_bytes(b"base-image")
    default_key = base_dir / "lumen_qemu_ed25519"
    default_key.write_text("deployment-default-key\n", encoding="utf-8")
    base = PersistentQemuPaths(
        arch="x86_64", image=base_image, ssh_key=default_key,
        runtime_dir=base_dir / "runtime",
    )

    def fake_paths(arch, *, runtime_root=None):
        if runtime_root is None:
            return base
        attempt_dir = Path(runtime_root) / "x86_64"
        return PersistentQemuPaths(
            arch="x86_64", image=attempt_dir / "debian.img",
            ssh_key=attempt_dir / "lumen_qemu_ed25519",
            runtime_dir=attempt_dir / "runtime",
        )

    monkeypatch.setattr("agents.test_expert.persistent_qemu_paths", fake_paths)
    artifacts = _copy_base_image(
        arch="x86_64", runtime_root=tmp_path / "attempt", source_image=str(image),
    )

    assert Path(artifacts["ssh_key_source"]) == sibling_key.resolve()
    assert Path(artifacts["attempt_ssh_key"]).read_text(encoding="utf-8") == "case-specific-key\n"


def test_byte_identical_declared_rootfs_reuses_deployment_key(tmp_path, monkeypatch):
    custom_dir = tmp_path / "case-root"
    custom_dir.mkdir()
    image = custom_dir / "debian.img"
    image.write_bytes(b"same-image-bytes")

    base_dir = tmp_path / "base" / "x86_64"
    base_dir.mkdir(parents=True)
    base_image = base_dir / "debian.img"
    base_image.write_bytes(b"same-image-bytes")
    default_key = base_dir / "lumen_qemu_ed25519"
    default_key.write_text("deployment-default-key\n", encoding="utf-8")
    base = PersistentQemuPaths(
        arch="x86_64", image=base_image, ssh_key=default_key,
        runtime_dir=base_dir / "runtime",
    )

    def fake_paths(arch, *, runtime_root=None):
        if runtime_root is None:
            return base
        attempt_dir = Path(runtime_root) / "x86_64"
        return PersistentQemuPaths(
            arch="x86_64", image=attempt_dir / "debian.img",
            ssh_key=attempt_dir / "lumen_qemu_ed25519",
            runtime_dir=attempt_dir / "runtime",
        )

    monkeypatch.setattr("agents.test_expert.persistent_qemu_paths", fake_paths)
    artifacts = _copy_base_image(
        arch="x86_64", runtime_root=tmp_path / "attempt", source_image=str(image),
    )

    assert Path(artifacts["ssh_key_source"]) == default_key.resolve()
    assert artifacts["ssh_key_resolution"] == "deployment-default-byte-identical-image"
    assert Path(artifacts["attempt_ssh_key"]).read_text(encoding="utf-8") == "deployment-default-key\n"


def test_declared_rootfs_without_co_located_ssh_key_is_blocked(tmp_path, monkeypatch):
    custom_dir = tmp_path / "case-root"
    custom_dir.mkdir()
    image = custom_dir / "debian.img"
    image.write_bytes(b"sparse-image-placeholder")
    base_dir = tmp_path / "base" / "x86_64"
    base_dir.mkdir(parents=True)
    base_image = base_dir / "debian.img"
    base_image.write_bytes(b"base-image")
    default_key = base_dir / "lumen_qemu_ed25519"
    default_key.write_text("deployment-default-key\n", encoding="utf-8")
    base = PersistentQemuPaths(
        arch="x86_64", image=base_image, ssh_key=default_key,
        runtime_dir=base_dir / "runtime",
    )

    def fake_paths(arch, *, runtime_root=None):
        if runtime_root is None:
            return base
        attempt_dir = Path(runtime_root) / "x86_64"
        return PersistentQemuPaths(
            arch="x86_64", image=attempt_dir / "debian.img",
            ssh_key=attempt_dir / "lumen_qemu_ed25519",
            runtime_dir=attempt_dir / "runtime",
        )

    monkeypatch.setattr("agents.test_expert.persistent_qemu_paths", fake_paths)
    try:
        _copy_base_image(arch="x86_64", runtime_root=tmp_path / "attempt", source_image=str(image))
    except FileNotFoundError as exc:
        assert "co-located SSH key" in str(exc)
    else:
        raise AssertionError("a custom rootfs without its SSH key must not use an unrelated fallback")


def test_invalid_contract_is_blocked_before_image_copy():
    result = test_expert_node({
        "session_dir": "", "kernel_contract": {}, "tryout_count": 0, "max_tryouts": 10,
        "test_rounds": [],
    })
    assert result["test_contract"]["status"] == "blocked"
    assert result["test_contract"]["code"] == "BLOCKED_INVALID_KERNEL_CONTRACT"
    assert result["tryout_count"] == 1


def test_semantic_review_never_overrides_missing_deterministic_match():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        failed = TestResultContract(status="failed", code="FAILED_CALL_CHAIN_MISMATCH")
        accepted, reason = _semantic_review(contract, failed)
        assert accepted is False
        assert "deterministic" in reason


def test_missing_gadgetfs_capability_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text("mount gadgetfs: No such device\n", encoding="utf-8")
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )

        blocked = _promote_guest_capability_block(failed)

    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_CAPABILITY_MISSING"
    assert "gadgetfs" in blocked.summary

def test_missing_guest_compiler_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text(
            "LUMEN_GUEST_COMPONENT_MISSING:compiler:gcc\\n",
            encoding="utf-8",
        )
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_COMPONENT_MISSING"
    assert "gcc" in blocked.summary


def test_missing_optional_guest_executable_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text(
            "LUMEN_BLOCKED: no executable mkfs.bcachefs\n",
            encoding="utf-8",
        )
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_COMPONENT_MISSING"
    assert "mkfs.bcachefs" in blocked.summary


def test_missing_target_usb_device_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text("diagnostic: target USB device not observed\n", encoding="utf-8")
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_USB_DEVICE_MISSING"
    assert "USB" in blocked.summary


def test_missing_target_dvb_device_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text(
            "open(/dev/dvb/adapter0/frontend0): No such file or directory\n",
            encoding="utf-8",
        )
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_USB_DEVICE_MISSING"


def test_missing_guest_bluetooth_hci_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text(
            "diagnostic: attempts=23796 connected=0 hci_down_ok=0 "
            "hci_up_ok=0\n"
            "diagnostic: first_hci_errno=19 (No such device)\n",
            encoding="utf-8",
        )
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_BLUETOOTH_HCI_MISSING"
    assert "HCI" in blocked.summary


def test_unavailable_guest_bluetooth_sco_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text(
            "lumen-diagnostic: vhci_seen=1 sco_connect_attempts=17807 "
            "successes=0 errors=17807\n",
            encoding="utf-8",
        )
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_BLUETOOTH_SCO_UNAVAILABLE"
    assert "SCO" in blocked.summary


def test_unavailable_guest_bluetooth_sco_errno_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text(
            "sco worker: connect=-1 errno=No route to host "
            "listen=-1 errno=File descriptor in bad state\n",
            encoding="utf-8",
        )
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_BLUETOOTH_SCO_UNAVAILABLE"


def test_missing_guest_kvm_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text(
            "KVM_DIAGNOSTIC: SKIP open(/dev/kvm): No such file or directory\n",
            encoding="utf-8",
        )
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_KVM_UNAVAILABLE"
    assert "/dev/kvm" in blocked.summary


def test_missing_guest_ocfs2_mount_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text("SKIP: no writable OCFS2 mount is visible\n", encoding="utf-8")
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_OCFS2_MOUNT_MISSING"
    assert "OCFS2" in blocked.summary


def test_missing_guest_ocfs2_tools_are_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text(
            "mkfs.ocfs2 is not installed in the guest\n"
            "diagnostic prerequisites unavailable; no kernel claim\n",
            encoding="utf-8",
        )
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_OCFS2_MOUNT_MISSING"


def test_missing_guest_sve_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text("SKIP: arm64 SVE is not exposed by the guest\n", encoding="utf-8")
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_SVE_UNAVAILABLE"
    assert "SVE" in blocked.summary


def test_missing_guest_sve_status_is_terminal():
    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text("rounds=512 asimd=1 sve=0 pid=11894\n", encoding="utf-8")
        failed = TestResultContract(
            status="failed", code="FAILED_SIGNAL_NOT_FOUND",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)
    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_SVE_UNAVAILABLE"



if __name__ == "__main__":
    for test in (
        test_test_plan_compiles_userspace_source_inside_guest,
        test_invalid_contract_is_blocked_before_image_copy,
        test_semantic_review_never_overrides_missing_deterministic_match,
        test_missing_gadgetfs_capability_is_terminal,
    ):
        test()
    print("test_expert OK")


def test_deep_suspend_platform_capability_is_terminal():
    import tempfile

    with tempfile.TemporaryDirectory() as directory:
        output = Path(directory) / "ssh-command.log"
        output.write_text(
            'mem_sleep current: s2idle\n'
            'write(/sys/power/mem_sleep, "deep") failed: Invalid argument\n'
            'write(/sys/power/state, "mem") failed: Invalid argument\n',
            encoding="utf-8",
        )
        failed = TestResultContract(
            status="failed", code="FAILED_CALL_CHAIN_MISMATCH",
            artifacts={"ssh_output": str(output)},
        )
        blocked = _promote_guest_capability_block(failed)

    assert blocked.status == "blocked"
    assert blocked.code == "BLOCKED_GUEST_PLATFORM_UNSUPPORTED"
    assert "s2idle" in blocked.summary
