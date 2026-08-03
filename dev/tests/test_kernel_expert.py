"""Offline Kernel Expert tests for the maintenance C-only boundary."""

from pathlib import Path
import json
import sys
import tempfile

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agents.contracts import CallChainOracle, KernelExpertOutput, UserspaceReproducer, model_to_dict
from agents.kernel_expert import (
    _extract_kernel_contract,
    _enrich_kernel_contract_from_runtime,
    _kernel_contract_ready_for_test,
    _pin_semcode_mcp_to_source,
    _read_primary_log_text,
    _validate_kernel_contract_artifacts,
)


def _contract(root: Path) -> KernelExpertOutput:
    image = root / "bzImage"
    image.write_bytes(b"MZ\x00\x00")
    source = root / "source"
    source.mkdir()
    (source / "repro.c").write_text("int main(void) { return 0; }\n", encoding="utf-8")
    return KernelExpertOutput(
        status="ok", target_arch="x86_64", boot_kernel_path=str(image),
        root_cause="verified userspace ABI reaches the failing kernel path",
        call_chain_oracle=CallChainOracle(
            fault_signatures=["target fault"], required_frames=["target_frame"],
            target_subsystems=["target_subsystem"],
        ),
        reproducer=UserspaceReproducer(
            source_dir=str(source), source_files=["repro.c"], entry_source="repro.c",
            output_binary="lumen-repro",
        ),
    )


def test_prompt_states_maintenance_and_c_only_boundaries():
    prompt = (PROJECT_ROOT / "prompts" / "kernel_expert.md").read_text(encoding="utf-8")
    assert "Linux kernel maintenance" in prompt
    assert "userspace C" in prompt
    assert "kernel modules" in prompt
    assert "Test Expert, not you" in prompt


def test_structured_kernel_contract_round_trips_without_module_build():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        text = "KERNEL_CONTRACT:\n```json\n" + json.dumps(model_to_dict(contract)) + "\n```"
        parsed = _extract_kernel_contract(text)
        validated = _validate_kernel_contract_artifacts(parsed)
        assert validated.status == "ok"
        assert _kernel_contract_ready_for_test(validated)
        assert validated.reproducer.output_binary == "lumen-repro"


def test_bare_json_contract_survives_marker_word_in_string():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        data = model_to_dict(contract)
        data["change_from_previous_tryout"] = (
            "retry note mentions KERNEL_CONTRACT but is not a marker"
        )
        parsed = _extract_kernel_contract("最终分析结果\n" + json.dumps(data))
        assert parsed.status == "ok"
        assert parsed.call_chain_oracle.required_frames == ["target_frame"]

def test_semcode_mcp_is_pinned_to_declared_checkout(tmp_path):
    source = tmp_path / "kernel"
    (source / ".semcode.db").mkdir(parents=True)
    config = {"semcode_mcp": {"args": ["--legacy", "--lazy", "--database", "old", "--git-repo=old"]}}

    pinned = _pin_semcode_mcp_to_source(config, str(source))

    assert pinned["semcode_mcp"]["args"] == [
        "--legacy",
        "--lazy",
        "-d",
        str((source / ".semcode.db").resolve()),
        "--git-repo",
        str(source.resolve()),
    ]
    assert config["semcode_mcp"]["args"] == ["--legacy", "--lazy", "--database", "old", "--git-repo=old"]


def test_contract_rejects_module_metadata_in_c_source_set():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        source = Path(contract.reproducer.source_dir) / "bad.ko"
        source.write_text("not a module", encoding="utf-8")
        contract.reproducer.source_files.append("bad.ko")
        validated = _validate_kernel_contract_artifacts(contract)
        assert validated.status == "blocked"
        assert "non-C reproducer source" in validated.blocked_reason


if __name__ == "__main__":
    for test in (
        test_prompt_states_maintenance_and_c_only_boundaries,
        test_structured_kernel_contract_round_trips_without_module_build,
        test_contract_rejects_module_metadata_in_c_source_set,
    ):
        test()
    print("kernel_expert OK")


def test_primary_log_reader_preserves_first_hand_source_frames(tmp_path):
    log = tmp_path / "crash.log"
    log.write_text("Call Trace: j1939_sock_pending_del+0x1a/0x40\n", encoding="utf-8")
    assert "j1939_sock_pending_del+0x1a" in _read_primary_log_text(str(log))
    assert _read_primary_log_text(str(tmp_path / "missing.log")) == ""


def test_declared_qemu_cmdline_is_preserved_when_model_has_other_recipe(tmp_path):
    contract = _contract(tmp_path)
    data = model_to_dict(contract)
    data["qemu_recipe"] = {"extra_cmdline": "panic_on_warn=1"}
    contract = KernelExpertOutput(**data)

    enriched = _enrich_kernel_contract_from_runtime(
        contract,
        input_artifacts={"qemu_extra_cmdline": "no-kvmapf nosoftlockup"},
        output_dir=tmp_path,
    )

    assert enriched.qemu_recipe.extra_cmdline == (
        "panic_on_warn=1 no-kvmapf nosoftlockup"
    )


def test_declared_runtime_paths_override_model_case_paths(tmp_path):
    """The input image/kernel must win over a model's stale existing paths."""
    contract = _contract(tmp_path)
    stale_root = tmp_path / "stale-case"
    stale_root.mkdir()
    stale_boot = stale_root / "bzImage"
    stale_rootfs = stale_root / "old.img"
    stale_vmlinux = stale_root / "vmlinux"
    for path in (stale_boot, stale_rootfs, stale_vmlinux):
        path.write_bytes(b"stale")

    declared_boot = tmp_path / "declared-bzImage"
    declared_vmlinux = tmp_path / "declared-vmlinux"
    declared_rootfs = tmp_path / "declared-rootfs.img"
    for path in (declared_boot, declared_vmlinux, declared_rootfs):
        path.write_bytes(b"declared")

    data = model_to_dict(contract)
    data.update({
        "boot_kernel_path": str(stale_boot),
        "rootfs_path": str(stale_rootfs),
        "vmlinux_path": str(stale_vmlinux),
        "target_arch": "arm64",
    })
    model_contract = KernelExpertOutput(**data)
    enriched = _enrich_kernel_contract_from_runtime(
        model_contract,
        input_artifacts={
            "target_arch": "x86_64",
            "vmlinux_path": str(declared_vmlinux),
            "boot_kernel_path": str(declared_boot),
            "rootfs_path": str(declared_rootfs),
        },
        output_dir=tmp_path,
    )

    assert enriched.target_arch == "x86_64"
    assert enriched.vmlinux_path == str(declared_vmlinux)
    assert enriched.boot_kernel_path == str(declared_boot)
    assert enriched.rootfs_path == str(declared_rootfs)
