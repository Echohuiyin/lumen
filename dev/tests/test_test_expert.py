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
from agents.test_expert import _attempt_runtime_root, _build_plan, _copy_base_image, _promote_guest_capability_block, _semantic_review, test_expert_node


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


def test_test_plan_compiles_userspace_source_inside_guest():
    with tempfile.TemporaryDirectory() as directory:
        plan = _build_plan(_contract(Path(directory)))
        assert plan.reproducer.language == "c"
        assert plan.reproducer.artifact_type == "userspace"
        assert plan.execution_steps[-1].type == "run_binary"
        assert plan.execution_steps[-1].path == "bin/lumen-repro"
        assert plan.call_chain_oracle.required_frames == ["target_frame"]


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
