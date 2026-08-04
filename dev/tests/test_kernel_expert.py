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
    _materialize_primary_log,
    _materialize_semcode_evidence,
    _semcode_evidence_is_complete,
    _resolve_primary_log_path,
    _read_primary_log_text,
    _codex_case_text,
    _extract_first_hand_log_hints,
    _stage_codex_evidence,
    _static_check_userspace_reproducer,
    _sync_codex_artifacts,
    _validate_kernel_contract_artifacts,
    _kernel_expert_contract_is_terminal,
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
    assert "in-kernel extension" in prompt
    assert "Test Expert owns" in prompt
    assert "qemu_recipe.extra_cmdline" in prompt
    assert "pressure_requirements" in prompt
    assert "Do not put an executable setting only in prose" in prompt
    assert '"type":"run_pressure"' in prompt
    assert '"type":"fault_injection"' in prompt
    assert "These steps must remain structured JSON" in prompt
    assert "Userspace correctness gate" in prompt
    assert "Mandatory reproducer code review" in prompt
    assert "every shared scalar/state field must use" in prompt
    assert "barrier participant count" in prompt
    assert "every `pthread_create` result" in prompt
    assert "joined before barrier/context destruction" in prompt
    assert "double-close" in prompt
    assert "LUMEN_GUEST_RUNTIME_INCOMPATIBLE:pthread_clone" in prompt
    assert "fork/process" in prompt
    assert "static_check.txt" in prompt
    assert "link/ABI usage" in prompt
    assert "compiler static-semantic analysis" in prompt
    assert "guest-process SIGSEGV" in prompt
    assert "userspace undefined behavior" in prompt
    assert "never relabel a userspace crash as a kernel pass" in prompt
    assert "Source-guarded trigger design" in prompt
    assert "guard predicates" in prompt
    assert "transmit versus receive" in prompt
    assert "socket-owned TX/session object" in prompt
    assert "such as `sendto`" in prompt
    assert "Preserve observable identifiers" in prompt
    assert "numeric protocol/address/PGN values" in prompt
    assert "copy them exactly" in prompt
    assert "generic load, random frames" in prompt
    assert "Transport direction and scheduler-context audit" in prompt
    assert "J1939_ECU_LOCAL_SRC" in prompt
    assert "j1939_session_get_by_addr(..., transmitter=true)" in prompt
    assert "reason (3)" in prompt
    assert "bounded userspace" in prompt
    assert "j1939_tp_cmd_recv()" in prompt
    assert "SA=peer/DA=local" in prompt
    assert "NLM_F_ACK" in prompt
    assert "SO_J1939_SEND_PRIO" in prompt
    assert "Userspace ABI layout audit" in prompt
    assert "reserved, padding, compat, and trailing fields" in prompt
    assert "_IOC_SIZE(command) == sizeof(payload)" in prompt
    assert "ENOTTY" in prompt
    assert "failed precondition" in prompt
    assert "OCFS2 userspace fixture" in prompt
    assert "absence of a pre-mounted OCFS2 directory" in prompt
    assert "mkfs.ocfs2 -M local" in prompt
    assert "source-only inline entries" in prompt
    assert "allowed_wrapper_frames" in prompt
    assert "required_top_frames" in prompt
    assert "context-sensitive" in prompt
    assert "must never be placed" in prompt
    assert "do not repeat them through interactive MCP" in prompt
    assert '"source_files": ["repro.c"]' not in prompt
    assert '"source_files": ["diagnostic_test.c"]' in prompt

def test_inline_report_annotations_are_preserved(tmp_path):
    report = tmp_path / "report.txt"
    report.write_text(
        "Call trace:\n helper fs/example.c:10 [inline]\n caller+0x1/0x2\n",
        encoding="utf-8",
    )
    data = model_to_dict(_contract(tmp_path))
    data["original_call_chain"] = ["helper", "caller"]
    data["call_chain_oracle"]["required_frames"] = ["helper", "caller"]
    enriched = _enrich_kernel_contract_from_runtime(
        KernelExpertOutput(**data),
        input_artifacts={"crash_report_path": str(report)},
        output_dir=tmp_path,
    )
    assert enriched.original_call_chain == ["helper [inline]", "caller"]
    assert enriched.call_chain_oracle.required_frames == ["helper [inline]", "caller"]


def test_inline_report_annotations_are_preserved_in_required_top_frames(tmp_path):
    report = tmp_path / "report.txt"
    report.write_text("Call trace:\n helper fs/example.c:10 [inline]\n", encoding="utf-8")
    data = model_to_dict(_contract(tmp_path))
    data["original_call_chain"] = ["helper", "caller"]
    data["call_chain_oracle"]["required_top_frames"] = ["helper", "caller"]
    data["call_chain_oracle"]["required_frames"] = ["helper", "caller"]
    enriched = _enrich_kernel_contract_from_runtime(
        KernelExpertOutput(**data),
        input_artifacts={"crash_report_path": str(report)},
        output_dir=tmp_path,
    )
    assert enriched.call_chain_oracle.required_top_frames == ["helper [inline]", "caller"]


def test_codex_case_text_uses_maintenance_language():
    text = _codex_case_text(
        "Bug Promote: title; maintenance diagnosis only, not vulnerability research. "
        "reproducer: /tmp/repro.c; fresh userspace C trigger; do not write a kernel module"
    )
    assert "Authorized maintenance regression case:" in text
    assert "original ABI sample:" in text
    assert "withheld by the benchmark" in text
    assert "/tmp/repro.c" not in text
    assert "new userspace C regression test" in text
    assert "in-kernel extension" in text
    assert "vulnerability research" not in text


def test_first_hand_log_hints_preserve_trigger_prerequisites_without_repro_source():
    log = """
    unrelated boot line
    FAULT_INJECTION: forcing a failure.
    name failslab, interval 1, probability 0, times 1
    gadgetfs: bound to dummy_udc driver
    __arm64_sys_write+0x7c/0x90
    gadget_dev_open+0x50/0x1c4
    arbitrary text without an action marker
    """
    hints = _extract_first_hand_log_hints(log)
    assert "FAULT_INJECTION: forcing a failure." in hints
    assert "failslab" in hints
    assert "gadgetfs: bound to dummy_udc driver" in hints
    assert "__arm64_sys_write" in hints
    assert "gadget_dev_open" in hints
    assert "unrelated boot line" not in hints


def test_structured_kernel_contract_round_trips_without_module_build():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        text = "KERNEL_CONTRACT:\n```json\n" + json.dumps(model_to_dict(contract)) + "\n```"
        parsed = _extract_kernel_contract(text)
        validated = _validate_kernel_contract_artifacts(parsed)
        assert validated.status == "ok"
        assert _kernel_contract_ready_for_test(validated)
        assert validated.reproducer.output_binary == "lumen-repro"


def test_explicit_blocked_contract_is_terminal_but_empty_block_retries():
    blocked = KernelExpertOutput(
        status="blocked", blocked_reason="guest lacks the required MTD master",
    )
    empty_block = KernelExpertOutput(status="blocked")
    degraded = KernelExpertOutput(status="degraded", blocked_reason="incomplete")
    assert _kernel_expert_contract_is_terminal(blocked) is True
    assert _kernel_expert_contract_is_terminal(empty_block) is False
    assert _kernel_expert_contract_is_terminal(degraded) is False


def test_required_top_frames_round_trip_and_compatibility_alias(tmp_path):
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        contract.call_chain_oracle.required_top_frames = ["fault", "caller"]
        contract.call_chain_oracle.required_frames = ["fault", "caller"]
        text = "KERNEL_CONTRACT:\n```json\n" + json.dumps(model_to_dict(contract)) + "\n```"
        parsed = _extract_kernel_contract(text)
        assert parsed.call_chain_oracle.required_top_frames == ["fault", "caller"]
        assert parsed.call_chain_oracle.required_frames == ["fault", "caller"]


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


def test_codex_evidence_extract_keeps_call_chain_and_drops_operational_noise(tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    (case / "report.txt").write_text(
        "WARNING: drivers/mtd/mtdcore.c:719 at add_mtd_device+0x56c/0x14cc\n"
        "Unable to handle kernel paging request\n"
        "CPU: 0 Comm: syz.0.1\n"
        "Call trace:\n"
        " target_entry+0x1/0x2\n"
        " target_release+0x3/0x4\n"
        "---[ end trace 000 ]---\n",
        encoding="utf-8",
    )
    log = case / "console.log"
    log.write_text("syz-executor ioctl setup\n", encoding="utf-8")
    tool = case / "tool.txt"
    tool.write_text("root cause summary\nsyzkaller syscall sequence\n", encoding="utf-8")

    _stage_codex_evidence(
        tmp_path / "workdir",
        [("original.log", str(log)), ("tool_expert_1.txt", str(tool))],
    )
    staged = (tmp_path / "workdir" / "evidence" / "original.log").read_text()
    assert "WARNING: drivers/mtd/mtdcore.c:719" in staged
    assert "target_entry" in staged and "target_release" in staged
    assert "syz" not in staged.lower()
    assert "ioctl" not in (tmp_path / "workdir" / "evidence" / "tool_expert_1.txt").read_text().lower()


def test_codex_evidence_preserves_semcode_json(tmp_path):
    source = tmp_path / "semcode-evidence.json"
    payload = {
        "status": "ok",
        "expected_kernel_commit": "a" * 40,
        "entries": [{"function": "add_mtd_device", "result": "exact source"}],
        "note": "do not use source-text fallback",
    }
    source.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    _stage_codex_evidence(
        tmp_path / "workdir",
        [("semcode-evidence.json", str(source))],
    )

    staged = tmp_path / "workdir" / "evidence" / "semcode-evidence.json"
    assert json.loads(staged.read_text(encoding="utf-8")) == payload


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


def test_crash_report_is_preferred_over_boot_console_log(tmp_path):
    console = tmp_path / "console.log"
    report = tmp_path / "report.txt"
    console.write_text("boot only\n", encoding="utf-8")
    report.write_text("KASAN: target report\n", encoding="utf-8")
    assert _resolve_primary_log_path({
        "log_path": str(console),
        "crash_report_path": str(report),
    }, []) == str(report)


def test_primary_log_is_materialized_inside_codex_workspace(tmp_path):
    source = tmp_path / "source-crash.log"
    source.write_text("Call Trace: exact_frame+0x1\n", encoding="utf-8")
    output = tmp_path / "session"
    output.mkdir()

    copied = _materialize_primary_log(output, str(source))

    assert copied == str((output / "original-crash.log").resolve())
    assert Path(copied).read_text(encoding="utf-8") == source.read_text(encoding="utf-8")


def test_semcode_evidence_file_preserves_commit_and_blocked_state(tmp_path):
    output = tmp_path / "session"
    output.mkdir()
    path = _materialize_semcode_evidence(
        output,
        source_path="/missing/linux",
        expected_commit="deadbeef" * 5,
        command="",
        args=[],
        evidence_text="RIP: target_frame+0x10/0x20",
    )
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["status"] == "blocked"
    assert data["expected_kernel_commit"] == "deadbeef" * 5


def test_semcode_evidence_captures_inline_frames_and_rejects_indexing(tmp_path, monkeypatch):
    class FakeClient:
        requested_names = []

        def __init__(self, **_kwargs):
            pass

        def _call_many(self, requests):
            type(self).requested_names.extend(
                arguments["name"] for _, arguments in requests
            )
            assert any(arguments["name"] == "task_fpsimd_load" for _, arguments in requests)
            return [
                (
                    "Database is currently being indexed (Analyzing files). Please wait"
                    if arguments["name"] == "task_fpsimd_load"
                    else "Function: fpsimd_restore_current_state (git SHA: " + "a" * 40 + ")"
                )
                for _, arguments in requests
            ]

    monkeypatch.setattr("agents.kernel_expert.SemcodeMcpClient", FakeClient)
    monkeypatch.setattr("agents.kernel_expert.time.sleep", lambda _seconds: None)
    output = tmp_path / "session"
    output.mkdir()
    path = _materialize_semcode_evidence(
        output,
        source_path="/tmp/linux",
        expected_commit="a" * 40,
        command="semcode-mcp",
        args=[],
        evidence_text=(
            "pc : task_fpsimd_load arch/arm64/kernel/fpsimd.c:370 [inline]\n"
            " fpsimd_restore_current_state+0x4cc/0x708 arch/arm64/kernel/fpsimd.c:1746\n"
        ),
    )
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert data["status"] == "blocked"
    assert any(entry["function"] == "fpsimd_restore_current_state" for entry in data["entries"])
    assert any(failure["function"] == "task_fpsimd_load" for failure in data["failures"])
    assert set(FakeClient.requested_names) <= {
        "fpsimd_restore_current_state", "task_fpsimd_load",
    }


def test_partial_semcode_evidence_keeps_interactive_mcp_available():
    partial = {
        "status": "ok",
        "entries": [{
            "function": "technisat_usb2_rc_query",
            "result": (
                "Function: technisat_usb2_rc_query\n"
                "Calls: 2 functions\n"
                "Called by: 0 functions\n"
                "Body:\n"
                "static int technisat_usb2_rc_query(struct dvb_usb_device *d) {\n"
                "  technisat_usb2_get_ir(d);\n"
                "  technisat_usb2_set_led(d, 1, 0);\n"
                "}\n"
            ),
        }],
        "failures": [],
    }
    assert not _semcode_evidence_is_complete(partial)

    complete = {
        **partial,
        "entries": partial["entries"] + [
            {"function": "technisat_usb2_get_ir", "result": "Function: technisat_usb2_get_ir"},
            {"function": "technisat_usb2_set_led", "result": "Function: technisat_usb2_set_led"},
        ],
    }
    assert _semcode_evidence_is_complete(complete)




def test_semcode_evidence_retries_cold_index(tmp_path, monkeypatch):
    class FakeClient:
        calls = 0

        def __init__(self, **_kwargs):
            pass

        def _call_many(self, requests):
            type(self).calls += 1
            if self.calls == 1:
                return ["Database is currently being indexed"] * len(requests)
            return ["Function: task_fpsimd_load (git SHA: " + "a" * 40 + ")"] * len(requests)

    monkeypatch.setattr("agents.kernel_expert.SemcodeMcpClient", FakeClient)
    monkeypatch.setattr("agents.kernel_expert.time.sleep", lambda _seconds: None)
    output = tmp_path / "session"
    output.mkdir()
    path = _materialize_semcode_evidence(
        output,
        source_path="/tmp/linux",
        expected_commit="a" * 40,
        command="semcode-mcp",
        args=[],
        evidence_text="pc : task_fpsimd_load arch/arm64/kernel/fpsimd.c:370 [inline]\n",
    )
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    assert FakeClient.calls == 2
    assert data["status"] == "ok"
    assert not data["failures"]


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


def test_codex_evidence_extracts_uppercase_crash_signatures(tmp_path):
    case = tmp_path / "case"
    case.mkdir()
    log = case / "crash.log"
    log.write_text(
        "BUG: kernel NULL pointer dereference, address: 0000000000000000\n"
        "Oops: 0000 [#1] SMP NOPTI\n"
        "RIP: 0010:target_fault+0x1/0x2\n"
        "Call Trace:\n"
        " target_syscall_frame+0x3/0x4\n"
        " target_release+0x5/0x6\n"
        "---[ end trace 000 ]---\n",
        encoding="utf-8",
    )

    _stage_codex_evidence(
        tmp_path / "workdir",
        [("original.log", str(log))],
    )

    staged = (tmp_path / "workdir" / "evidence" / "original.log").read_text()
    assert "BUG: kernel NULL pointer dereference" in staged
    assert "Oops: 0000" in staged
    assert "RIP: 0010:target_fault" in staged
    assert "Call Trace:" in staged
    assert "target_syscall_frame" in staged
    assert "target_release" in staged



def test_static_userspace_preflight_requires_warning_clean_c(tmp_path):
    contract = _contract(tmp_path)
    result = _static_check_userspace_reproducer(contract, tmp_path)
    assert result["status"] == "passed", result
    assert (tmp_path / "static_check.txt").is_file()

    source = Path(contract.reproducer.source_dir) / "repro.c"
    source.write_text("#warning reject this diagnostic\nint main(void) { return 0; }\n", encoding="utf-8")
    rejected = _static_check_userspace_reproducer(contract, tmp_path)
    assert rejected["status"] == "failed", rejected
    audit = (tmp_path / "static_check.txt").read_text(encoding="utf-8")
    assert "SYNTAX_RETURN_CODE:" in audit
    assert "SYNTAX_STATUS: failed" in audit



def test_static_userspace_preflight_checks_link_and_semantics(tmp_path):
    contract = _contract(tmp_path)
    source = Path(contract.reproducer.source_dir) / "repro.c"

    source.write_text(
        "extern int missing_api(void); int main(void) { return missing_api(); }\n",
        encoding="utf-8",
    )
    link_result = _static_check_userspace_reproducer(contract, tmp_path)
    assert link_result["status"] == "failed", link_result
    link_audit = (tmp_path / "static_check.txt").read_text(encoding="utf-8")
    assert "LINK_STATUS: failed" in link_audit

    source.write_text(
        "#include <stdlib.h>\n"
        "int main(void) { int *p = NULL; return *p; }\n",
        encoding="utf-8",
    )
    semantic_result = _static_check_userspace_reproducer(contract, tmp_path)
    assert semantic_result["status"] == "failed", semantic_result
    semantic_audit = (tmp_path / "static_check.txt").read_text(encoding="utf-8")
    assert "SEMANTIC_STATUS: failed" in semantic_audit



def test_static_userspace_preflight_supports_multiple_translation_units(tmp_path):
    contract = _contract(tmp_path)
    source_dir = Path(contract.reproducer.source_dir)
    (source_dir / "helper.c").write_text("int helper(void) { return 0; }\n", encoding="utf-8")
    (source_dir / "repro.c").write_text(
        "int helper(void); int main(void) { return helper(); }\n", encoding="utf-8",
    )
    contract.reproducer.source_files.append("helper.c")
    result = _static_check_userspace_reproducer(contract, tmp_path)
    assert result["status"] == "passed", result
    audit = (tmp_path / "static_check.txt").read_text(encoding="utf-8")
    assert "SEMANTIC_STATUS: passed" in audit
    assert "SEMANTIC_2_STATUS: passed" in audit


def test_ephemeral_reproducer_dir_remaps_only_from_current_sync(tmp_path):
    workdir = tmp_path / "codex-workdir"
    session = tmp_path / "durable-session"
    workdir.mkdir()
    session.mkdir()
    (workdir / "diagnostic_test.c").write_text(
        "int main(void) { return 0; }", encoding="utf-8"
    )
    _sync_codex_artifacts(workdir, session)

    contract = _contract(tmp_path)
    data = model_to_dict(contract)
    data["reproducer"]["source_dir"] = str(workdir / "removed-after-codex")
    data["reproducer"]["source_files"] = ["diagnostic_test.c"]
    enriched = _enrich_kernel_contract_from_runtime(
        KernelExpertOutput(**data), input_artifacts={}, output_dir=session,
    )
    assert enriched.reproducer.source_dir == str(session.resolve())
    assert any("ephemeral Codex workdir" in warning for warning in enriched.warnings)

    (session / ".codex_artifact_manifest.json").unlink()
    unchanged = _enrich_kernel_contract_from_runtime(
        KernelExpertOutput(**data), input_artifacts={}, output_dir=session,
    )
    assert unchanged.reproducer.source_dir == str((workdir / "removed-after-codex").resolve())
