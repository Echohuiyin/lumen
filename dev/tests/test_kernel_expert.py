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
    _restore_cached_semcode_path_analysis,
    _semcode_evidence_covers_report_frames,
    _semcode_evidence_is_complete,
    _resolve_primary_log_path,
    _read_primary_log_text,
    _codex_case_text,
    _extract_first_hand_log_hints,
    _materialized_contract_response,
    _stage_codex_evidence,
    _static_check_userspace_reproducer,
    _sync_codex_artifacts,
    _validate_kernel_contract_artifacts,
    _kernel_expert_contract_is_terminal,
    _kernel_contract_has_handoff,
    _preserve_valid_contract_after_cli_failure,
    _recover_materialized_contract_after_cli_failure,
    _render_incremental_test_round_context,
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
        execution_steps=[
            {"type": "run_binary", "path": "bin/lumen-repro", "args": []},
        ],
    )



def test_cli_timeout_recovers_manifest_proven_contract(tmp_path):
    from paths import set_session_dir

    session = tmp_path / 'session'
    session.mkdir()
    contract = _contract(tmp_path)
    (session / 'repro.c').write_text(
        'int main(void) { return 0; }\n', encoding='utf-8',
    )
    (session / 'KERNEL_CONTRACT.json').write_text(
        json.dumps(model_to_dict(contract)), encoding='utf-8',
    )
    (session / '.codex_artifact_manifest.json').write_text(
        json.dumps({
            'session_output_dir': str(session.resolve()),
            'source_workdir': str(tmp_path / 'codex-workdir'),
            'copied_files': ['KERNEL_CONTRACT.json', 'repro.c'],
        }),
        encoding='utf-8',
    )

    set_session_dir(session)
    try:
        result = _recover_materialized_contract_after_cli_failure(
            state={'user_input': 'WARNING in target_frame'},
            error=RuntimeError('codex timed out'),
            error_message='kernel_expert CLI 超时',
            semcode_path_analysis=None,
            input_artifacts={'boot_kernel_path': str(tmp_path / 'bzImage')},
        )
    finally:
        set_session_dir(None)

    assert result['kernel_ready_for_test'] is True
    assert result['kernel_contract']['status'] == 'ok'
    assert result['kernel_contract']['reproducer']['source_dir'] == str(session.resolve())
    assert any(
        item.get('kind') == 'kernel_expert_cli_timeout_recovery'
        for item in result['kernel_contract']['evidence']
    )


def test_materialized_contract_response_requires_current_manifest(tmp_path):
    session = tmp_path / 'session'
    session.mkdir()
    contract = _contract(tmp_path)
    (session / 'KERNEL_CONTRACT.json').write_text(
        json.dumps(model_to_dict(contract)), encoding='utf-8',
    )
    (session / '.codex_artifact_manifest.json').write_text(
        json.dumps({
            'session_output_dir': str(session.resolve()),
            'source_workdir': str(tmp_path / 'codex-workdir'),
            'copied_files': ['KERNEL_CONTRACT.json'],
        }),
        encoding='utf-8',
    )

    response = _materialized_contract_response(session)
    assert response is not None
    parsed = _extract_kernel_contract(response.content)
    assert parsed is not None
    assert parsed.root_cause == contract.root_cause
    assert _materialized_contract_response(tmp_path / 'other') is None


def test_materialized_contract_can_replace_non_structured_retry_output(tmp_path):
    session = tmp_path / 'session'
    session.mkdir()
    contract = _contract(tmp_path)
    (session / 'KERNEL_CONTRACT.json').write_text(
        json.dumps(model_to_dict(contract)), encoding='utf-8',
    )
    (session / '.codex_artifact_manifest.json').write_text(
        json.dumps({
            'session_output_dir': str(session.resolve()),
            'source_workdir': str(tmp_path / 'codex-workdir'),
            'copied_files': ['KERNEL_CONTRACT.json'],
        }),
        encoding='utf-8',
    )

    materialized = _materialized_contract_response(session)
    parsed = _extract_kernel_contract(materialized.content if materialized else '')
    prompt = (PROJECT_ROOT / "prompts" / "kernel_expert.md").read_text(encoding="utf-8")
    assert "Linux kernel maintenance" in prompt
    assert "userspace C" in prompt
    assert "in-kernel extension" in prompt
    assert "Test Expert owns" in prompt
    assert "qemu_recipe.extra_cmdline" in prompt
    assert "pressure_requirements" in prompt
    assert "setup_requirements" in prompt
    assert "fault_injection_requirements" in prompt
    assert "allow-listed" in (PROJECT_ROOT / "prompts" / "kernel_expert_codex.md").read_text(encoding="utf-8")
    assert "Userspace correctness gate" in prompt
    assert "Mandatory reproducer code review" in prompt
    assert "every shared scalar/state field must use" in prompt
    assert "barrier participant count" in prompt
    assert "every `pthread_create` result" in prompt
    assert "joined before barrier/context destruction" in prompt
    assert "double-close" in prompt
    codex_prompt = (PROJECT_ROOT / "prompts" / "kernel_expert_codex.md").read_text(encoding="utf-8")
    assert "typed Lumen" in codex_prompt
    assert "Never invent fields such as `kind`" in codex_prompt
    assert "Operation-level evidence is part of the ABI" in codex_prompt
    assert "failed(directio)" in codex_prompt
    assert "`O_DIRECT` open/read/write" in codex_prompt
    assert "detected capacity change from 0 to" in codex_prompt
    assert "/dev/loop-control" in codex_prompt
    assert "return `blocked` rather than silently replacing it" in codex_prompt
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


def test_declared_operator_reproducer_is_materialized_and_reused(tmp_path):
    source = tmp_path / "syzbot.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    image = tmp_path / "bzImage"
    image.write_bytes(b"MZ\x00\x00")
    contract = _contract(tmp_path)
    enriched = _enrich_kernel_contract_from_runtime(
        contract,
        input_artifacts={
            "reproducer_path": str(source),
            "qemu_recipe": {"smp": "8", "cpu": "host", "timeout_sec": 300},
        },
        output_dir=tmp_path / "session",
    )
    assert enriched.reproducer.operator_supplied is True
    assert enriched.reproducer.source_files == ["operator-reproducer.c"]
    assert Path(enriched.reproducer.source_dir, "operator-reproducer.c").read_text() == source.read_text()
    assert enriched.qemu_recipe.smp == "8"
    assert enriched.qemu_recipe.cpu == "host"
    assert enriched.qemu_recipe.timeout_sec == 300
    assert _static_check_userspace_reproducer(enriched, tmp_path / "session")["status"] == "passed"


def test_persisted_kernel_contract_shape_is_adapted_without_fallback(tmp_path):
    """The persisted Codex schema must reach the runtime ABI verbatim."""
    rich = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "ready",
        "verified_invariant": {
            "statement": "The completion length must not exceed the tracked extent bytes.",
            "source_basis": ["fs/example.c:10: explicit source evidence"],
        },
        "finding": {"subsystem": "example completion"},
        "audit_call_chain": {
            "target_trigger_order": [
                "worker_thread (kernel/workqueue.c:10-20)",
                "finish_extent (fs/example.c:30-40)",
            ],
        },
        "core_oracle": {
            "ordered": [
                {"order": 1, "required_log": "Workqueue: example finish_extent"},
                {
                    "order": 2,
                    "required_functions": ["worker_thread", "finish_extent"],
                    "required_log": "extent bytes mismatch",
                },
            ],
        },
        "setup": {
            "steps": [
                {"type": "compile", "source": "diagnostic_test.c"},
                {"type": "run_binary", "binary": "diagnostic_test", "argv": []},
            ],
        },
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.status == "ok"
    assert contract.root_cause == rich["verified_invariant"]["statement"]
    assert contract.original_call_chain == ["worker_thread", "finish_extent"]
    assert contract.call_chain_oracle.required_frames == ["worker_thread", "finish_extent"]
    assert contract.call_chain_oracle.fault_signatures == [
        "Workqueue: example finish_extent", "extent bytes mismatch",
    ]
    assert [step.type for step in contract.execution_steps] == ["run_binary"]
    assert contract.execution_steps[0].path == "bin/diagnostic_test"

    source = tmp_path / "operator.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    image = tmp_path / "bzImage"
    image.write_bytes(b"MZ\x00\x00")
    enriched = _enrich_kernel_contract_from_runtime(
        contract,
        input_artifacts={
            "target_arch": "x86_64",
            "boot_kernel_path": str(image),
            "reproducer_path": str(source),
            "expected_signal": "extent bytes mismatch",
        },
        output_dir=tmp_path / "session",
    )
    assert enriched.reproducer.operator_supplied is True
    assert [step.type for step in enriched.execution_steps] == ["run_binary"]
    assert enriched.execution_steps[0].path == "bin/operator-repro"


def test_persisted_kernel_contract_aliases_are_explicitly_adapted():
    rich = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "case": {"verified_invariant": "ordered completion invariant"},
        "audit_call_chain": {
            "forward_to_invariant": [
                {"function": "worker_thread"},
                {
                    "function": "finish_extent",
                    "role": "fault site (reported symbol finish_extent.isra.0)",
                },
            ],
        },
        "core_oracle": {
            "ordered_events": [
                {"required": True, "match": "ordered completion warning"},
            ],
        },
        "setup": {"steps": [{"type": "run_binary", "binary": "diagnostic_test"}]},
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.status == "ok"
    assert contract.root_cause == "ordered completion invariant"
    assert contract.original_call_chain == ["worker_thread", "finish_extent.isra.0"]
    assert contract.call_chain_oracle.required_frames == [
        "worker_thread", "finish_extent.isra.0",
    ]
    assert contract.call_chain_oracle.fault_signatures == ["ordered completion warning"]
    assert contract.execution_steps[0].args == []


def test_wrapped_persisted_contract_uses_target_and_audited_chain():
    rich = {
        "KERNEL_CONTRACT": {
            "version": 1,
            "status": "ok",
            "target": {
                "subsystem": "fs/example",
                "reported_warning": "len > tracked_bytes",
            },
            "verified_invariant": {
                "statement": "completion length must not exceed tracked bytes",
            },
            "audit_call_chain": [
                {"symbol": "worker_thread"},
                {"symbol": "finish_extent"},
            ],
            "core_oracle": {
                "must_match_in_order": [
                    {"order": 1, "match": "target warning"},
                    {"order": 2, "match": ["worker_thread", "finish_extent"]},
                ],
            },
            "execution_steps": [{"type": "run_binary", "binary": "operator-reproducer"}],
        },
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.status == "ok"
    assert contract.root_cause == "completion length must not exceed tracked bytes"
    assert contract.call_chain_oracle.target_subsystems == ["fs/example"]
    assert contract.call_chain_oracle.fault_signatures == ["target warning"]
    assert contract.original_call_chain == ["worker_thread", "finish_extent"]
    assert contract.call_chain_oracle.required_frames == contract.original_call_chain


def test_wrapped_array_core_contract_is_explicitly_adapted():
    """The persisted outer marker and array oracle must not become a block."""
    rich = {
        "KERNEL_CONTRACT": {
            "status": "ready",
            "case": {
                "subsystem": "btrfs ordered-data",
                "symptom": "len > ordered->bytes_left",
                "warning_site": "fs/btrfs/ordered-data.c:390",
            },
            "source": {
                "expected_commit": "deadbeef",
                "commit_verified": True,
            },
            "verified_invariant": "ordered completion length stays within bytes_left",
            "audit_call_chain": [
                {"function": "can_finish_ordered_extent.isra.0"},
                {"function": "btrfs_finish_ordered_extent"},
            ],
            "core_oracle": [
                {"order": 1, "assert": "len > ordered->bytes_left at fs/btrfs/ordered-data.c:390"},
                {"order": 2, "assert": "The ordered call trace follows the audited chain."},
            ],
            "reproducer": {
                "operator_supplied": True,
                "steps": [{"order": 1, "kind": "run_binary", "binary": "diagnostic_test"}],
            },
        },
    }
    contract = _extract_kernel_contract(json.dumps(rich))
    assert contract.status == "ok"
    assert contract.root_cause == "ordered completion length stays within bytes_left"
    assert contract.call_chain_oracle.fault_signatures == [
        "len > ordered->bytes_left at fs/btrfs/ordered-data.c:390",
    ]
    assert contract.call_chain_oracle.required_frames == [
        "can_finish_ordered_extent.isra.0", "btrfs_finish_ordered_extent",
    ]


def test_type_marked_contract_with_incremental_setup_is_adapted():
    rich = {
        "type": "KERNEL_CONTRACT",
        "status": "ready",
        "case": {
            "subsystem": "btrfs",
            "symptom": "len > ordered->bytes_left",
            "verified_invariant": "ordered completion length stays bounded",
        },
        "audit_call_chain": [
            {"symbol": "can_finish_ordered_extent.isra.0"},
            {"symbol": "btrfs_finish_ordered_extent"},
        ],
        "core_oracle": [
            {"order": 1, "condition": "target warning"},
        ],
        "reproducer": {"operator_supplied": True},
        "incremental_setup": {
            "steps": [
                {"order": 1, "kind": "run_binary", "binary": "diagnostic_test"},
            ],
        },
    }
    contract = _extract_kernel_contract(json.dumps(rich))
    assert contract.status == "ok"
    assert contract.root_cause == "ordered completion length stays bounded"
    assert contract.call_chain_oracle.fault_signatures == ["len > ordered->bytes_left"]
    assert contract.call_chain_oracle.required_frames == [
        "can_finish_ordered_extent.isra.0", "btrfs_finish_ordered_extent",
    ]
    assert contract.execution_steps[0].path == "bin/diagnostic_test"


def test_target_warning_chain_and_ordered_steps_are_mapped():
    rich = {
        "type": "KERNEL_CONTRACT",
        "status": "ready",
        "target": {"subsystem": "btrfs", "architecture": "x86_64"},
        "finding": {
            "signal": "WARNING: fs/btrfs/ordered-data.c:390",
            "verified_invariant": "len must not exceed ordered->bytes_left",
        },
        "audit_call_chain": {
            "target_warning_chain": [
                {"order": 1, "function": "worker_thread"},
                {"order": 2, "function": "can_finish_ordered_extent"},
            ],
        },
        "core_oracle": {
            "ordered_steps": [
                {"order": 1, "must_observe": "WARNING: fs/btrfs/ordered-data.c:390"},
                {"order": 2, "must_observe": "Kernel panic - not syncing"},
            ],
        },
        "reproducer": {"operator_supplied": True},
        "incremental_setup": {
            "steps": [{"kind": "run_binary", "binary": "diagnostic_test"}],
        },
    }
    contract = _extract_kernel_contract(json.dumps(rich))
    assert contract.status == "ok"
    assert contract.root_cause == "len must not exceed ordered->bytes_left"
    assert contract.original_call_chain == ["worker_thread", "can_finish_ordered_extent"]
    assert contract.call_chain_oracle.fault_signatures == [
        "WARNING: fs/btrfs/ordered-data.c:390",
        "Kernel panic - not syncing",
    ]
    assert contract.call_chain_oracle.required_frames == contract.original_call_chain


def test_wrapped_strict_order_contract_maps_named_warning_chain():
    rich = {
        "KERNEL_CONTRACT": {
            "version": 1,
            "status": "ready_for_test",
            "case": {
                "reported_failure": "len > ordered->bytes_left",
                "observed_effect": "WARNING: fs/btrfs/ordered-data.c:390",
            },
            "source": {"expected_commit": "deadbeef", "verified_commit": "deadbeef"},
            "verified_invariant": {
                "statement": "ordered completion length is bounded",
            },
            "audit_call_chain": {
                "strict_order": [
                    "worker_thread", "process_one_work", "can_finish_ordered_extent",
                ],
            },
            "core_oracle": {
                "ordered_steps": [
                    {"order": 1, "assert": "len > ordered->bytes_left at fs/btrfs/ordered-data.c:390"},
                    {"order": 2, "assert": "Kernel panic - not syncing"},
                ],
            },
            "reproducer": {"operator_supplied": True},
        },
    }
    contract = _extract_kernel_contract(json.dumps(rich))
    assert contract.status == "ok"
    assert contract.root_cause == "ordered completion length is bounded"
    assert contract.original_call_chain == [
        "worker_thread", "process_one_work", "can_finish_ordered_extent",
    ]
    assert contract.call_chain_oracle.required_frames == contract.original_call_chain


def test_completion_path_is_reversed_before_workqueue_context():
    rich = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "case": {"reported_invariant": "ordered length is bounded"},
        "audit_call_chain": {
            "completion_path_in_log_order": [
                "simple_end_io_work", "btrfs_bio_end_io", "can_finish_ordered_extent",
            ],
            "workqueue_context": ["process_one_work", "worker_thread"],
        },
        "core_oracle": {
            "required_sequence": [
                {"order": 1, "match": "len > ordered->bytes_left"},
            ],
        },
        "reproducer": {"operator_supplied": True},
    }
    contract = _extract_kernel_contract(json.dumps(rich))
    assert contract.original_call_chain == [
        "can_finish_ordered_extent", "btrfs_bio_end_io", "simple_end_io_work",
        "process_one_work", "worker_thread",
    ]
    assert contract.call_chain_oracle.required_frames == contract.original_call_chain


def test_runtime_ready_contract_uses_case_violation_and_chain_frames():
    rich = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "ready_for_runtime_test",
        "case": {
            "verified_invariant": "completion length must stay within bytes_left",
            "observed_violation": "len > ordered->bytes_left",
        },
        "audit_call_chain": [
            {"function": "worker_thread"},
            {"function": "finish_extent"},
        ],
        "core_oracle": {"ordered_checks": [{"order": 1, "check": "warning"}]},
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.status == "ok"
    assert contract.root_cause == "completion length must stay within bytes_left"
    assert contract.call_chain_oracle.fault_signatures == ["len > ordered->bytes_left"]
    assert contract.call_chain_oracle.required_frames == ["worker_thread", "finish_extent"]


def test_async_completion_chain_alias_is_bounded_and_ignores_panic_label():
    rich = {
        "contract": "KERNEL_CONTRACT",
        "status": "ok",
        "verified_invariant": {
            "statement": "ordered length is bounded",
            "observed_failure": "len > ordered->bytes_left",
        },
        "audit_call_chain": {
            "kernel_async_completion": [
                {"symbol": "process_one_work"},
                {"symbol": "finish_extent"},
                {"symbol": "WARN/panic escalation"},
            ],
        },
        "core_oracle": [],
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.call_chain_oracle.fault_signatures == ["len > ordered->bytes_left"]
    assert contract.call_chain_oracle.required_frames == [
        "process_one_work", "finish_extent",
    ]


def test_ordered_assertions_and_warning_stack_aliases_are_mapped():
    rich = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "ok",
        "case": {"reported_signal": "WARNING: example: len > bytes_left"},
        "verified_invariant": {"statement": "bytes remain bounded"},
        "audit_call_chain": {
            "kernel_warning_stack_top_down": [
                "fault_site", "finish_extent", "worker_thread",
            ],
        },
        "core_oracle": {
            "ordered_assertions": [
                {"order": 1, "pattern": "warning precursor"},
                {"order": 2, "must_match": ["finish_extent", "worker_thread"]},
            ],
        },
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.call_chain_oracle.fault_signatures == ["warning precursor"]
    assert contract.call_chain_oracle.required_frames == [
        "finish_extent", "worker_thread",
    ]


def test_source_evidence_inventory_supplies_frames_and_diagnostic_marker():
    rich = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready_for_test",
        "verified_invariant": {"statement": "ordered length is bounded"},
        "audit_call_chain": {"operator_to_kernel_trigger": ["operator workload"]},
        "source_evidence": [
            {"functions": ["finish_extent", "worker_thread"]},
            {"facts": ["first-hand log reports len > ordered->bytes_left"]},
        ],
        "core_oracle": {"ordered_steps": []},
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.status == "ok"
    assert contract.call_chain_oracle.fault_signatures == [
        "first-hand log reports len > ordered->bytes_left",
    ]
    assert contract.call_chain_oracle.required_frames == [
        "finish_extent", "worker_thread",
    ]


def test_target_completion_chain_and_reported_assertion_aliases_are_mapped():
    rich = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "ready",
        "case": {"reported_assertion": "len > ordered->bytes_left"},
        "verified_invariant": {"statement": "ordered completion is bounded"},
        "audit_call_chain": {
            "target_completion_chain": [
                {"function": "worker_thread"},
                {"function": "finish_extent"},
            ],
        },
        "core_oracle": {"ordered_checks": [{"order": 1, "check": "warning"}]},
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.call_chain_oracle.fault_signatures == ["len > ordered->bytes_left"]
    assert contract.call_chain_oracle.required_frames == [
        "worker_thread", "finish_extent",
    ]


def test_expected_regression_signature_alias_supplies_signal_gate():
    rich = {
        "contract": "KERNEL_CONTRACT",
        "status": "ok",
        "verified_invariant": {"statement": "ordered length is bounded"},
        "audit_call_chain": ["finish_extent", "worker_thread"],
        "core_oracle": {
            "expected_regression_signature": [
                "len > ordered->bytes_left", "WARNING: ordered-data.c:390",
            ],
        },
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.call_chain_oracle.fault_signatures == [
        "len > ordered->bytes_left", "WARNING: ordered-data.c:390",
    ]


def test_full_audit_and_strict_core_oracle_aliases_are_mapped():
    rich = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "verified_invariant": {
            "statement": "ordered completion is bounded",
            "observed_violation": "len > ordered->bytes_left",
        },
        "full_audit_call_chain": [
            {"function": "worker_thread"}, {"function": "finish_extent"},
        ],
        "strict_ordered_core_oracle": [
            {"order": 1, "assert": "warning"},
        ],
    }
    contract = _extract_kernel_contract("KERNEL_CONTRACT: " + json.dumps(rich))
    assert contract.call_chain_oracle.fault_signatures == ["len > ordered->bytes_left"]
    assert contract.call_chain_oracle.required_frames == [
        "worker_thread", "finish_extent",
    ]

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


def test_versioned_codex_contract_normalizes_entrypoint_and_binary_step(tmp_path):
    source = tmp_path / "diag.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    raw = {
        "schema": "KERNEL_CONTRACT",
        "status": "ready",
        "root_cause": {"verified_invariant": "the documented ABI reaches the target"},
        "original_call_chain": {"printed_frames": [{"function": "fault"}, {"function": "caller"}]},
        "call_chain_oracle": {
            "required_frames": [{"function": "fault"}, {"function": "caller"}],
            "required_log_signatures": ["BUG: target"],
        },
        "reproducer": {
            "language": "c",
            "artifact_type": "userspace",
            "source_dir": str(tmp_path),
            "source_files": [str(source)],
            "entrypoint": "main",
            "compiler": "cc",
            "flags": ["-std=c11"],
            "libraries": ["libc"],
            "arguments": [],
        },
    }
    contract = _extract_kernel_contract(
        "KERNEL_CONTRACT:\n```json\n" + json.dumps(raw) + "\n```"
    )
    assert contract.status == "ok"
    assert contract.reproducer.entry_source == "diag.c"
    assert contract.reproducer.output_binary == "lumen-repro"
    assert contract.call_chain_oracle.fault_signatures == ["BUG: target"]
    assert [step.type for step in contract.execution_steps] == ["run_binary"]
    assert contract.execution_steps[0].path == "bin/lumen-repro"


def test_contract_schema_block_is_repairable_but_capability_block_is_terminal(tmp_path):
    schema_block = _contract(tmp_path)
    schema_block.status = "blocked"
    schema_block.blocked_reason = "missing call_chain_oracle.fault_signatures"
    assert _kernel_expert_contract_is_terminal(schema_block) is False

    capability_block = KernelExpertOutput(**model_to_dict(schema_block))
    capability_block.status = "blocked"
    capability_block.blocked_reason = "documented userspace ABI requires CAP_SYS_ADMIN"
    assert _kernel_expert_contract_is_terminal(capability_block) is True


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


def test_first_hand_log_hints_filter_crash_labels_and_generator_names():
    log = """
    KASAN: use-after-free in maintenance path
    kernel BUG at fs/example.c:10
    Call Trace:
      syz_executor+0x10/0x20
    mount /dev/loop0 at /tmp/lumen
    ioctl LOOP_SET_FD image
    """
    hints = _extract_first_hand_log_hints(log)
    assert "KASAN" not in hints
    assert "kernel BUG" not in hints
    assert "Call Trace" not in hints
    assert "syz_executor" not in hints
    assert "mount /dev/loop0" in hints
    assert "ioctl LOOP_SET_FD" in hints


def test_semcode_evidence_covers_report_frames_without_all_body_helpers():
    payload = {
        "status": "ok",
        "entries": [
            {
                "function": "fault_entry",
                "result": "Function: fault_entry\nCalls: 1 functions\n→ helper_call\nBody:\nhelper_call();\n",
            },
            {"function": "caller_frame", "result": "Function: caller_frame\nBody:\n"},
        ],
    }
    report = "fault_entry+0x10/0x20\ncaller_frame+0x2/0x8\n"

    assert not _semcode_evidence_is_complete(payload)
    assert _semcode_evidence_covers_report_frames(payload, report)


def test_semcode_evidence_uses_same_capped_frame_prefix_as_adapter():
    names = [f"frame_{index}" for index in range(40)]
    payload = {
        "status": "ok",
        "entries": [
            {
                "function": "frame_0",
                "result": "Function: frame_0\nBody:\nframe_1();\n",
            },
            *[
                {"function": name, "result": f"Function: {name}"}
                for name in names[1:32]
            ],
        ],
    }
    report = "\n".join(f"{name}+0x1/0x2" for name in names)

    assert _semcode_evidence_covers_report_frames(payload, report)


def test_structured_kernel_contract_round_trips_without_module_build():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        text = "KERNEL_CONTRACT:\n```json\n" + json.dumps(model_to_dict(contract)) + "\n```"
        parsed = _extract_kernel_contract(text)
        validated = _validate_kernel_contract_artifacts(parsed)
        assert validated.status == "ok"
        assert _kernel_contract_ready_for_test(validated)
        assert validated.reproducer.output_binary == "lumen-repro"


def test_declared_operator_source_gets_one_explicit_run_step(tmp_path):
    source = tmp_path / "official-repro.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    session = tmp_path / "session"
    session.mkdir()
    contract = _enrich_kernel_contract_from_runtime(
        _contract(tmp_path),
        input_artifacts={
            "reproducer_path": str(source),
            "boot_kernel_path": str(tmp_path / "bzImage"),
        },
        output_dir=session,
    )
    assert contract.reproducer.operator_supplied is True
    assert contract.reproducer.source_dir == str(session.resolve())
    assert contract.reproducer.source_files == ["operator-reproducer.c"]
    assert [step.type for step in contract.execution_steps] == ["run_binary"]
    assert contract.execution_steps[0].path == "bin/operator-repro"
    assert (session / "operator-reproducer.c").read_bytes() == source.read_bytes()


def test_contract_accepts_evidence_path_manifest_without_dropping_handoff():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        data = model_to_dict(contract)
        data["evidence"] = {
            "original_log": "evidence/original.log",
            "tool_expert_files": ["evidence/tool_expert_1.txt"],
        }
        text = "KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```"
        parsed = _extract_kernel_contract(text)
        assert parsed.status == "ok"
        assert _kernel_contract_ready_for_test(parsed)
        assert parsed.evidence == [{
            "kind": "artifact_manifest",
            "entries": data["evidence"],
        }]


def test_codex_versioned_contract_aliases_normalize_without_inventing_runtime_actions():
    data = {
        "contract": "KERNEL_CONTRACT",
        "schema_version": 1,
        "status": "blocked",
        "tryout": {"number": 1, "maximum": 10},
        "root_cause": {"verified_invariant": "label must remain NUL terminated"},
        "evidence": [{"source_domain": "kernel", "function": "strlen"}],
        "original_call_chain": {"observed_frames": ["strlen", "smack_log_callback"]},
        "call_chain_oracle": {
            "required_order_top_to_bottom": ["strlen", "smack_log_callback"],
            "required_signatures": ["RIP in strlen"],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["diagnostic_test.c"],
            "flags": ["-Wall"], "libraries": ["libc"],
            "arguments": [], "timeout_seconds": 5,
        },
        "pressure_requirements": {"iterations": 1, "processes": 1},
        "fault_injection_requirements": {"required": False},
        "blocked_reason": {"precise_limitation": "public ABI cannot invalidate label"},
    }
    parsed = _extract_kernel_contract("KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```")
    assert parsed.status == "blocked"
    assert parsed.tryout == 1
    assert parsed.root_cause == "label must remain NUL terminated"
    assert parsed.original_call_chain == ["strlen", "smack_log_callback"]
    assert parsed.call_chain_oracle.required_frames == ["strlen", "smack_log_callback"]
    assert parsed.reproducer.compiler_args == ["-Wall"]
    assert parsed.reproducer.link_libraries == ["c"]
    assert parsed.blocked_reason == "public ABI cannot invalidate label"
    assert parsed.pressure_requirements == []
    assert parsed.fault_injection_requirements == []


def test_codex_strict_required_frames_alias_is_handoff_ready_without_retry():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "root_cause": {"classification": "SMACK callback receives the wrong label"},
        "original_call_chain": {"observed_frames": ["strlen", "smack_log_callback"]},
        "call_chain_oracle": {
            "strict_required_frames": [
                {"order": 1, "frame": "smack_log_callback+0x105/0x1b0"},
                {"order": 2, "frame": "strlen+0x2c/0x70"},
            ],
            "concrete_log_signatures": ["RIP: 0010:strlen+0x2c/0x70"],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["diag.c"],
        },
    }
    parsed = _extract_kernel_contract("KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```")
    assert parsed.status == "ok"
    assert parsed.call_chain_oracle.required_frames == [
        "smack_log_callback+0x105/0x1b0", "strlen+0x2c/0x70",
    ]
    assert parsed.call_chain_oracle.required_frame_order == [[
        "smack_log_callback+0x105/0x1b0", "strlen+0x2c/0x70",
    ]]
    assert parsed.call_chain_oracle.fault_signatures == ["RIP: 0010:strlen+0x2c/0x70"]


def test_codex_strict_order_alias_preserves_explicit_fault_signature():
    data = {
        "schema": "KERNEL_CONTRACT",
        "status": "ready",
        "root_cause": {"classification": "SMACK audit callback path"},
        "original_call_chain": ["keyctl_watch_key", "strlen"],
        "call_chain_oracle": {
            "strict_order": [
                {"order": 1, "required_frame": "keyctl_watch_key", "signature": "watch install"},
                {"order": 2, "required_frame": "smack_log_callback", "signature": "audit callback"},
                {"order": 3, "required_frame": "strlen", "signature": "RIP at strlen with a KASAN wild-memory report"},
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["diag.c"],
        },
    }
    parsed = _extract_kernel_contract(
        "KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```"
    )
    assert parsed.status == "ok"
    assert parsed.call_chain_oracle.required_top_frames == [
        "keyctl_watch_key", "smack_log_callback", "strlen",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "RIP at strlen with a KASAN wild-memory report",
    ]


def test_codex_ordered_frames_top_to_bottom_preserves_blocked_rca():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": {
            "summary": "A read-only start leaves the btree update pool uninitialized.",
        },
        "original_call_chain": {
            "source": "evidence/original.log",
            "ordered_frames_top_to_bottom": [
                "mempool_alloc_noprof+0x1a4/0x510",
                "bch2_btree_update_start+0x51e/0x1480",
                "bch2_data_job+0x646/0x910",
            ],
        },
        "call_chain_oracle": {
            "fault_signatures": ["RIP: 0010:0x0"],
        },
    }
    parsed = _extract_kernel_contract(
        "KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```"
    )
    assert parsed.status == "blocked"
    assert parsed.root_cause == "A read-only start leaves the btree update pool uninitialized."
    assert parsed.original_call_chain == [
        "mempool_alloc_noprof+0x1a4/0x510",
        "bch2_btree_update_start+0x51e/0x1480",
        "bch2_data_job+0x646/0x910",
    ]
    assert parsed.call_chain_oracle.fault_signatures == ["RIP: 0010:0x0"]


def test_codex_stack_order_alias_preserves_blocked_rca_contract():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": {
            "verified_invariant": "The update pool must be initialized before mempool_alloc.",
        },
        "original_call_chain": {
            "stack_order_top_to_bottom": [
                "mempool_alloc_noprof+0x1a4/0x510",
                "bch2_btree_update_start+0x549/0x1480",
                "bch2_data_job+0x646/0x910",
            ],
        },
        "call_chain_oracle": {
            "required_stack_order_top_to_bottom": [
                "mempool_alloc_noprof",
                "bch2_btree_update_start",
                "bch2_data_job",
            ],
            "required_signatures": [
                "#PF: supervisor instruction fetch in kernel mode",
                "RIP: 0010:0x0",
            ],
        },
    }
    parsed = _extract_kernel_contract(
        "KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```"
    )
    assert parsed.status == "blocked"
    assert parsed.root_cause == "The update pool must be initialized before mempool_alloc."
    assert parsed.original_call_chain == [
        "mempool_alloc_noprof+0x1a4/0x510",
        "bch2_btree_update_start+0x549/0x1480",
        "bch2_data_job+0x646/0x910",
    ]
    assert parsed.call_chain_oracle.required_frames == [
        "mempool_alloc_noprof", "bch2_btree_update_start", "bch2_data_job",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "#PF: supervisor instruction fetch in kernel mode", "RIP: 0010:0x0",
    ]


def test_codex_required_order_and_leaf_frames_preserve_blocked_rca():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": {
            "classification": "JFS cleanup reaches diFree without a state guard.",
        },
        "original_call_chain": {
            "raw_frames_leaf_to_user": [
                "diFree+0x13d/0x2dc0",
                "jfs_evict_inode+0x2c9/0x370",
                "jfs_mount+0x23f/0x3d0",
            ],
        },
        "call_chain_oracle": {
            "required_order": [
                "RIP: 0010:diFree+0x13d/0x2dc0",
                "jfs_evict_inode+0x2c9/0x370",
                "jfs_mount+0x23f/0x3d0",
            ],
            "required_signatures": ["#PF: supervisor read access in kernel mode"],
        },
    }
    parsed = _extract_kernel_contract(
        "KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```"
    )
    assert parsed.status == "blocked"
    assert parsed.root_cause == "JFS cleanup reaches diFree without a state guard."
    assert parsed.original_call_chain == [
        "diFree+0x13d/0x2dc0",
        "jfs_evict_inode+0x2c9/0x370",
        "jfs_mount+0x23f/0x3d0",
    ]
    assert parsed.call_chain_oracle.required_top_frames == [
        "diFree+0x13d/0x2dc0",
        "jfs_evict_inode+0x2c9/0x370",
        "jfs_mount+0x23f/0x3d0",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "#PF: supervisor read access in kernel mode",
    ]


def test_codex_ordered_required_frames_preserve_ready_smack_handoff():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "root_cause": {
            "verified_invariant": "audit_log_untrustedstring requires a readable NUL-terminated string.",
        },
        "call_chain_oracle": {
            "ordered_required_frames": [
                {"order": 1, "frame": "keyctl_watch_key", "signature": "keyctl_watch_key"},
                {"order": 2, "frame": "smack_watch_key", "signature": "smack_watch_key"},
                {"order": 3, "frame": "strlen", "signature": "strlen"},
            ],
            "concrete_log_signatures": ["RIP: 0010:strlen+0x2c/0x70"],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["diag.c"],
        },
    }
    parsed = _extract_kernel_contract(
        "KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```"
    )
    assert parsed.status == "ok"
    assert parsed.root_cause.startswith("audit_log_untrustedstring")
    assert parsed.call_chain_oracle.required_frames == [
        "keyctl_watch_key", "smack_watch_key", "strlen",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "RIP: 0010:strlen+0x2c/0x70",
    ]


def test_codex_logged_frames_and_fault_signatures_preserve_bcachefs_rca():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": "The source-backed symptom is a NULL instruction fetch in mempool_alloc_noprof.",
        "original_call_chain": {
            "fault_signatures": ["RIP: 0010:0x0"],
            "logged_frames_in_order": [
                {"order": 1, "frame": "mempool_alloc_noprof+0x1a4/0x510", "optional": False},
                {"order": 2, "frame": "bch2_btree_update_start+0x549/0x1480", "optional": False},
                {"order": 3, "frame": "bch2_data_job+0x646/0x910", "optional": False},
            ],
        },
        "call_chain_oracle": {
            "strict_ordered_core": [
                {"order": 1, "function": "mempool_alloc_noprof", "required_log_signature": "mempool_alloc_noprof+0x1a4/0x510"},
                {"order": 2, "function": "bch2_btree_update_start", "required_log_signature": "bch2_btree_update_start+0x549/0x1480"},
                {"order": 3, "function": "bch2_data_job", "required_log_signature": "bch2_data_job+0x646/0x910"},
            ],
            "required_fault_signatures": [
                "BUG: kernel NULL pointer dereference, address: 0000000000000000",
                "RIP: 0010:0x0",
            ],
        },
    }
    parsed = _extract_kernel_contract(
        "KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```"
    )
    assert parsed.status == "blocked"
    assert parsed.root_cause.startswith("The source-backed symptom")
    assert parsed.original_call_chain == [
        "mempool_alloc_noprof+0x1a4/0x510",
        "bch2_btree_update_start+0x549/0x1480",
        "bch2_data_job+0x646/0x910",
    ]
    assert parsed.call_chain_oracle.required_frames == [
        "mempool_alloc_noprof", "bch2_btree_update_start", "bch2_data_job",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "BUG: kernel NULL pointer dereference, address: 0000000000000000",
        "RIP: 0010:0x0",
    ]


def test_codex_required_trace_frames_preserve_bcachefs_handoff():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": "The documented BCH_IOCTL_DATA path is privileged-only.",
        "original_call_chain": {
            "required_trace_frames": [
                "mempool_alloc_noprof+0x1a4/0x510",
                "bch2_btree_update_start+0x549/0x1480",
                "bch2_data_thread+0x8f/0x1d0",
            ],
        },
        "call_chain_oracle": {
            "required_order": [
                "mempool_alloc_noprof+0x1a4/0x510",
                "bch2_btree_update_start+0x549/0x1480",
                "bch2_data_thread+0x8f/0x1d0",
            ],
            "required_fault_signatures": ["RIP: 0010:0x0"],
        },
    }
    parsed = _extract_kernel_contract(
        "KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```"
    )
    assert parsed.status == "blocked"
    assert parsed.root_cause == "The documented BCH_IOCTL_DATA path is privileged-only."
    assert parsed.original_call_chain == [
        "mempool_alloc_noprof+0x1a4/0x510",
        "bch2_btree_update_start+0x549/0x1480",
        "bch2_data_thread+0x8f/0x1d0",
    ]
    assert parsed.call_chain_oracle.required_frames == [
        "mempool_alloc_noprof+0x1a4/0x510",
        "bch2_btree_update_start+0x549/0x1480",
        "bch2_data_thread+0x8f/0x1d0",
    ]
    assert parsed.call_chain_oracle.fault_signatures == ["RIP: 0010:0x0"]


def test_codex_printed_frames_alias_preserves_blocked_rca_contract():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": {"verified_invariant": "strlen receives the reported audit label"},
        "original_call_chain": {
            "printed_frames": ["strlen+0x2c/0x70", "smack_log_callback+0x105/0x1b0"],
            "audit_call_chain_user_to_fault": ["keyctl_watch_key", "smack_watch_key", "strlen"],
        },
        "call_chain_oracle": {
            "required_report_signatures": ["RIP: 0010:strlen+0x2c/0x70"],
            "strict_required_frames": [
                {"order": 1, "frame": "smack_log_callback+0x105/0x1b0"},
                {"order": 2, "frame": "strlen+0x2c/0x70"},
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["diag.c"],
        },
        "blocked_reason": {"precise_limitation": "public ABI cannot corrupt the label"},
    }
    parsed = _extract_kernel_contract(
        "KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```"
    )
    assert parsed.status == "blocked"
    assert parsed.root_cause == "strlen receives the reported audit label"
    assert parsed.original_call_chain[0] == "strlen+0x2c/0x70"
    assert parsed.call_chain_oracle.required_frames == [
        "smack_log_callback+0x105/0x1b0", "strlen+0x2c/0x70",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "RIP: 0010:strlen+0x2c/0x70",
    ]
    assert parsed.blocked_reason == "public ABI cannot corrupt the label"


def test_codex_audit_and_reported_frame_aliases_preserve_blocked_rca_contract():
    variants = [
        (
            "audit_path_root_to_fault",
            ["keyctl_watch_key", "smack_watch_key", "smack_log_callback", "strlen"],
            "SMACK audit path reaches strlen with an invalid label-derived pointer",
        ),
        (
            "reported_frames",
            [
                {"function": "diFree+0x13d/0x2dc0"},
                {"function": "jfs_evict_inode+0x2c9/0x370"},
                {"function": "jfs_mount+0x23f/0x3d0"},
            ],
            "JFS cleanup reaches diFree after malformed metadata",
        ),
    ]
    for field, frames, summary in variants:
        data = {
            "contract": "KERNEL_CONTRACT",
            "status": "blocked",
            "root_cause": {"summary": summary},
            "original_call_chain": {field: frames},
            "call_chain_oracle": {
                "fault_signatures": ["RIP: target_fault"],
            },
        }
        parsed = _extract_kernel_contract(json.dumps(data))
        assert parsed.status == "blocked"
        assert parsed.root_cause == summary
        expected = [
            item.get("function") if isinstance(item, dict) else item
            for item in frames
        ]
        assert parsed.original_call_chain == expected
        assert parsed.call_chain_oracle.fault_signatures == ["RIP: target_fault"]


def test_codex_root_cause_statement_alias_preserves_bcachefs_rca():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": {
            "statement": "The source proves a NULL indirect callback at mempool_alloc_noprof.",
            "verified_facts": ["RIP is 0x0"],
        },
        "original_call_chain": {"required_trace_frames": ["mempool_alloc_noprof", "bch2_data_thread"]},
        "call_chain_oracle": {"fault_signatures": ["RIP: 0010:0x0"]},
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.status == "blocked"
    assert parsed.root_cause == "The source proves a NULL indirect callback at mempool_alloc_noprof."
    assert parsed.original_call_chain == ["mempool_alloc_noprof", "bch2_data_thread"]
    assert parsed.call_chain_oracle.fault_signatures == ["RIP: 0010:0x0"]


def test_codex_nested_incremental_setup_change_is_preserved_for_retry_gate():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "ready",
        "tryout": 2,
        "previous_tryout": 1,
        "root_cause": "The public key-watch path reaches the SMACK hook.",
        "original_call_chain": ["keyctl_watch_key", "smack_watch_key", "strlen"],
        "call_chain_oracle": {
            "required_frames": ["keyctl_watch_key", "smack_watch_key", "strlen"],
            "fault_signatures": ["RIP: 0010:strlen+0x2c/0x70"],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["diag.c"],
        },
        "incremental_setup": {
            "prior_tryout": 1,
            "change_from_previous_tryout": (
                "Retain the exact-commit guest setup and add one explicit key-watch trigger."
            ),
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.status == "ok"
    assert parsed.change_from_previous_tryout == (
        "Retain the exact-commit guest setup and add one explicit key-watch trigger."
    )


def test_codex_incremental_change_list_is_preserved_for_retry_gate():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "tryout": 2,
        "root_cause": "The documented key-watch path reaches the SMACK hook.",
        "original_call_chain": ["keyctl_watch_key", "smack_watch_key", "strlen"],
        "call_chain_oracle": {
            "required_frames": ["keyctl_watch_key", "smack_watch_key", "strlen"],
            "fault_signatures": ["RIP: 0010:strlen+0x2c/0x70"],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["diag.c"],
        },
        "change_from_previous_tryout": {
            "previous_attempt": 1,
            "incremental_change": [
                "Add the notification pipe setup.",
                "Pass the approved key serial to KEYCTL_WATCH_KEY.",
            ],
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.status == "ok"
    assert parsed.change_from_previous_tryout == (
        "Add the notification pipe setup. Pass the approved key serial to KEYCTL_WATCH_KEY."
    )

def test_kernel_handoff_requires_an_explicit_guest_run_step():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        contract.execution_steps = []
        assert not _kernel_contract_has_handoff(contract)


def test_codex_flattened_contract_preserves_evidence_for_root_cause_scoring():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "root_cause": "smack_watch_key uses the wrong key blob base",
        "evidence": [{"function": "smack_watch_key", "file": "security/smack/smack_lsm.c", "line": "1-2"}],
        "call_chain_oracle": {"required_order_top_to_bottom": ["strlen", "smack_watch_key"], "required_signatures": ["RIP in strlen"]},
        "reproducer": {"language": "c", "artifact_type": "userspace", "source_files": ["diag.c"]},
    }
    parsed = _extract_kernel_contract("KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```")
    assert parsed.status == "ok"
    assert parsed.root_cause_evidence[0]["function"] == "smack_watch_key"


def test_codex_structured_frame_records_preserve_jfs_handoff_fields():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "root_cause": {"verified_invariant": "mount cleanup reaches diFree"},
        "original_call_chain": [
            {"order": 1, "frame": "diFree+0x13d/0x2dc0"},
            {"order": 2, "frame": "jfs_evict_inode+0x2c9/0x370"},
        ],
        "call_chain_oracle": {
            "strict_ordered_core": [
                {"order": 1, "required_signature": "RIP: 0010:diFree+0x13d/0x2dc0"},
                {"order": 2, "required_signature": "jfs_evict_inode+0x2c9/0x370"},
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/jfs-session", "source_files": ["jfs_mount_diag.c"],
            "flags": ["-Wall"], "arguments": [], "timeout_seconds": 10,
        },
    }
    parsed = _extract_kernel_contract("KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```")
    assert parsed.status == "ok"
    assert parsed.reproducer.source_dir == "/tmp/jfs-session"
    assert parsed.reproducer.entry_source == "jfs_mount_diag.c"
    assert parsed.original_call_chain == [
        "diFree+0x13d/0x2dc0", "jfs_evict_inode+0x2c9/0x370",
    ]
    assert parsed.call_chain_oracle.required_frames == [
        "diFree+0x13d/0x2dc0", "jfs_evict_inode+0x2c9/0x370",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "RIP: 0010:diFree+0x13d/0x2dc0",
    ]


def test_codex_contract_type_preserves_precise_blocked_reason_and_oracle():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": {"classification": "JFS cleanup reaches diFree"},
        "original_call_chain": {
            "direction": "fault_to_syscall_entry_as_printed",
            "frames": [{"symbol": "diFree+0x13d"}, {"symbol": "jfs_mount+0x23f"}],
        },
        "call_chain_oracle": {
            "direction": "syscall_entry_to_fault",
            "required_core": ["entry_SYSCALL_64_after_hwframe", "diFree+0x13d"],
            "required_signatures": ["RIP: 0010:diFree+0x13d/0x2dc0"],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/jfs-session",
            "source_files": [{"path": "/tmp/jfs-session/jfs_mount_abi_probe.c", "purpose": "probe"}],
            "flags": ["-Wall"], "arguments": [], "timeout_seconds": 5,
        },
        "blocked_reason": {
            "code": "NONPRIVILEGED_ABI_CANNOT_REACH_MOUNT",
            "precise_limitation": "CAP_SYS_ADMIN is required before do_new_mount().",
            "test_expert_action": "Report blocked.",
        },
    }
    parsed = _extract_kernel_contract("KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```")
    assert parsed.status == "blocked"
    assert parsed.root_cause == "JFS cleanup reaches diFree"
    assert parsed.reproducer.source_dir == "/tmp/jfs-session"
    assert parsed.reproducer.entry_source == "jfs_mount_abi_probe.c"
    assert parsed.reproducer.source_files == ["jfs_mount_abi_probe.c"]
    assert parsed.call_chain_oracle.required_frames == ["diFree+0x13d", "entry_SYSCALL_64_after_hwframe"]
    assert parsed.call_chain_oracle.fault_signatures == ["RIP: 0010:diFree+0x13d/0x2dc0"]
    assert "CAP_SYS_ADMIN" in parsed.blocked_reason


def test_materialized_unmarked_contract_shape_is_still_normalized():
    data = {
        "status": "blocked",
        "root_cause": {"classification": "capability boundary"},
        "original_call_chain": {
            "required_primary_frames": ["fault_entry+0x10", "worker"],
            "crash_signatures": ["RIP: 0010:fault_entry+0x10", "CR2: 0"],
        },
        "call_chain_oracle": {
            "required_frames": [
                {"order": 1, "function": "fault_entry", "signature": "fault_entry+0x10"},
                {"order": 2, "function": "worker", "signature": "worker+0x20"},
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["probe.c"],
            "compiler": "cc with C11 support", "arguments": [], "timeout_seconds": 5,
        },
        "blocked_reason": "capability gate is source-proven",
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.status == "blocked"
    assert parsed.root_cause == "capability boundary"
    assert parsed.original_call_chain == ["fault_entry+0x10", "worker"]
    assert parsed.call_chain_oracle.required_frames == ["fault_entry+0x10", "worker+0x20"]
    assert parsed.call_chain_oracle.fault_signatures == [
        "RIP: 0010:fault_entry+0x10", "CR2: 0",
    ]
    assert parsed.reproducer.compiler == "cc"


def test_j1939_contract_maps_mixed_required_signatures_and_default_args():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "ready",
        "root_cause": {"verified_invariant": "session lifetime crosses abort cleanup"},
        "original_call_chain": {
            "fault_report_call_trace": ["j1939_sock_pending_del", "run_ksoftirqd"],
            "log_signatures": ["BUG: KASAN: use-after-free in j1939_sock_pending_del"],
        },
        "call_chain_oracle": {
            "required_signatures": [
                "BUG: KASAN: use-after-free in j1939_sock_pending_del",
                "j1939_sock_pending_del", "j1939_session_put", "run_ksoftirqd",
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/j1939", "source_files": ["j1939_uaf_diag.c"],
            "compiler": "cc", "arguments": {
                "default": ["./j1939_uaf_diag", "vcan0", "64"],
                "syntax": "j1939_uaf_diag [ifname] [iterations]",
            },
        },
    }
    parsed = _extract_kernel_contract("KERNEL_CONTRACT:\n```json\n" + json.dumps(data) + "\n```")
    assert parsed.status == "ok"
    assert parsed.original_call_chain == ["j1939_sock_pending_del", "run_ksoftirqd"]
    assert parsed.call_chain_oracle.required_frames == [
        "j1939_sock_pending_del", "j1939_session_put", "run_ksoftirqd",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "BUG: KASAN: use-after-free in j1939_sock_pending_del",
    ]
    assert parsed.reproducer.run_args == ["./j1939_uaf_diag", "vcan0", "64"]


def test_j1939_contract_accepts_fault_report_trace_alias():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "root_cause": "J1939 abort cleanup reaches the freed socket bookkeeping",
        "original_call_chain": {
            "fault_report_trace": [
                {"frame": "j1939_sock_pending_del+0x20"},
                {"frame": "j1939_session_put+0xd2"},
            ],
        },
        "call_chain_oracle": {
            "required_signatures": [
                "BUG: KASAN: use-after-free in j1939_sock_pending_del",
                "j1939_sock_pending_del", "j1939_session_put",
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/j1939", "source_files": ["probe.c"],
            "compiler": "cc",
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.original_call_chain == [
        "j1939_sock_pending_del+0x20", "j1939_session_put+0xd2",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "BUG: KASAN: use-after-free in j1939_sock_pending_del",
    ]


def test_j1939_contract_accepts_access_report_order_alias():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "ready",
        "root_cause": "J1939 receive-side abort releases session state too early",
        "original_call_chain": {
            "access_report_order": [
                "j1939_sock_pending_del", "j1939_session_put",
                "j1939_xtp_rx_abort_one",
            ],
        },
        "call_chain_oracle": {
            "required_signatures": [
                "BUG: KASAN: use-after-free in j1939_sock_pending_del",
                "j1939_sock_pending_del", "j1939_session_put",
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/j1939", "source_files": ["probe.c"],
            "compiler": "cc",
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.original_call_chain == [
        "j1939_sock_pending_del", "j1939_session_put", "j1939_xtp_rx_abort_one",
    ]


def test_codex_ready_for_test_and_indexed_tryout_are_normalized():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "ready_for_test",
        "tryout": {"index": 3, "maximum": 10},
        "root_cause": "J1939 session teardown accesses a dead socket",
        "original_call_chain": {
            "reported_trace_order_leaf_to_outer": [
                "j1939_sock_pending_del", "j1939_session_put",
            ],
        },
        "call_chain_oracle": {
            "required_order_leaf_to_outer": [
                "j1939_sock_pending_del", "j1939_session_put",
            ],
            "required_log_signatures": [
                "BUG: KASAN: use-after-free in j1939_sock_pending_del",
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/j1939", "source_files": ["probe.c"],
            "compiler": "cc",
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.status == "ok"
    assert parsed.tryout == 3
    assert parsed.original_call_chain == [
        "j1939_sock_pending_del", "j1939_session_put",
    ]
    assert parsed.call_chain_oracle.required_frames == [
        "j1939_sock_pending_del", "j1939_session_put",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "BUG: KASAN: use-after-free in j1939_sock_pending_del",
    ]


def test_codex_descriptive_pthread_library_is_linker_safe():
    data = {
        "contract": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": "public ABI cannot reach the internal branch",
        "original_call_chain": ["fault_fn"],
        "call_chain_oracle": {"fault_signatures": ["BUG: fault_fn"]},
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["probe.c"],
            "libraries": ["libpthread via -pthread"],
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.reproducer.link_libraries == ["pthread"]


def test_codex_kind_marker_preserves_blocked_source_evidence_contract():
    data = {
        "kind": "KERNEL_CONTRACT",
        "status": "blocked",
        "tryout": {"number": 1},
        "root_cause": {
            "classification": "verified invariant violation",
            "invariant": "completion requires an async-write buffer flag",
        },
        "source_evidence": [
            {"source_domain": "kernel", "function": "end_buffer_async_write"},
        ],
        "original_call_chain": {
            "frames_in_report_order": [
                "end_buffer_async_write+0x2db/0x340", "bio_endio+0x5af/0x6c0",
            ],
        },
        "call_chain_oracle": {
            "strict_ordered_core": [
                "end_buffer_async_write+0x2db/0x340 [fs/buffer.c:391 assertion site]",
                "bio_endio+0x5af/0x6c0 [block/bio.c:1576-1609]",
            ],
            "required_signatures": [
                "RIP: 0010:end_buffer_async_write+0x2db/0x340",
                "bio_endio+0x5af/0x6c0",
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["probe.c"],
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.status == "blocked"
    assert parsed.tryout == 1
    assert parsed.root_cause == "verified invariant violation"
    assert parsed.root_cause_evidence == data["source_evidence"]
    assert parsed.original_call_chain == [
        "end_buffer_async_write+0x2db/0x340", "bio_endio+0x5af/0x6c0",
    ]
    assert parsed.call_chain_oracle.required_frames == [
        "end_buffer_async_write+0x2db/0x340", "bio_endio+0x5af/0x6c0",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "RIP: 0010:end_buffer_async_write+0x2db/0x340",
    ]


def test_codex_blocked_lock_contract_preserves_log_order_and_signals():
    data = {
        "kind": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": {"verified_invariant": "lock graph contains a cycle"},
        "original_call_chain": {
            "log_order_top_to_bottom": ["dump_stack_lvl", "sco_conn_del"],
        },
        "call_chain_oracle": {
            "strict_ordered_core": [
                "sco_conn_del [lock held]", "lock_sock_nested",
            ],
            "required_log_signatures": [
                "WARNING: possible circular locking dependency detected",
                "sco_conn_del",
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["probe.c"],
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.original_call_chain == ["dump_stack_lvl", "sco_conn_del"]
    assert parsed.call_chain_oracle.required_frames == [
        "sco_conn_del", "lock_sock_nested",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "WARNING: possible circular locking dependency detected", "sco_conn_del",
    ]


def test_codex_blocked_sock_contract_preserves_observed_top_to_bottom_alias():
    data = {
        "contract_type": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": {
            "classification": "unverified socket lifetime inconsistency",
        },
        "original_call_chain": {
            "observed_top_to_bottom": [
                "sock_close+0xc5/0x260", "? sock_mmap+0x90/0x90",
                "__fput+0x34f/0x7b0", "task_work_run+0x137/0x1c0",
            ],
            "fault_signature": "KASAN: null-ptr-deref in range [0x10-0x17]",
        },
        "call_chain_oracle": {
            "required_signatures": [
                "KASAN: null-ptr-deref in range [0x10-0x17]",
                "RIP: sock_close+0xc5/0x260", "__fput",
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["probe.c"],
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.status == "blocked"
    assert parsed.original_call_chain == [
        "sock_close+0xc5/0x260", "? sock_mmap+0x90/0x90",
        "__fput+0x34f/0x7b0", "task_work_run+0x137/0x1c0",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "KASAN: null-ptr-deref in range [0x10-0x17]",
    ]


def test_codex_blocked_technisat_contract_preserves_fault_trace_and_report_signals():
    data = {
        "kind": "KERNEL_CONTRACT",
        "status": "blocked",
        "root_cause": {
            "classification": "verified unbounded kernel buffer read",
        },
        "original_call_chain": {
            "fault_trace_ordered": [
                "technisat_usb2_rc_query+0x5fa/0x660",
                "dvb_usb_read_remote_control+0xe5/0x1c0",
                "process_one_work+0x90f/0x1580",
            ],
            "allocation_trace_ordered": ["technisat_usb2_probe+0x82/0x2d0"],
        },
        "call_chain_oracle": {
            "strict_ordered_core": [
                {"required_frame": "technisat_usb2_rc_query",
                 "required_signature": "technisat_usb2_rc_query+0x5fa/0x660"},
                {"required_frame": "dvb_usb_read_remote_control",
                 "required_signature": "dvb_usb_read_remote_control+0xe5/0x1c0"},
            ],
            "required_report_signatures": [
                "KASAN: slab-out-of-bounds", "Read of size 1",
            ],
        },
        "reproducer": {
            "language": "c", "artifact_type": "userspace",
            "source_dir": "/tmp/session", "source_files": ["observer.c"],
            "flags": ["-std=c11"], "libraries": ["libc"],
        },
    }
    parsed = _extract_kernel_contract(json.dumps(data))
    assert parsed.status == "blocked"
    assert parsed.original_call_chain == [
        "technisat_usb2_rc_query+0x5fa/0x660",
        "dvb_usb_read_remote_control+0xe5/0x1c0",
        "process_one_work+0x90f/0x1580",
    ]
    assert parsed.call_chain_oracle.fault_signatures == [
        "KASAN: slab-out-of-bounds", "Read of size 1",
    ]
    assert parsed.reproducer.entry_source == "observer.c"



def test_explicit_blocked_contract_is_terminal_but_empty_block_retries():
    blocked = KernelExpertOutput(
        status="blocked", blocked_reason="guest lacks the required MTD master",
    )
    empty_block = KernelExpertOutput(status="blocked")
    degraded = KernelExpertOutput(status="degraded", blocked_reason="incomplete")
    assert _kernel_expert_contract_is_terminal(blocked) is True
    assert _kernel_expert_contract_is_terminal(empty_block) is False
    assert _kernel_expert_contract_is_terminal(degraded) is False


def test_cli_timeout_preserves_last_valid_root_cause_contract():
    with tempfile.TemporaryDirectory() as directory:
        contract = _contract(Path(directory))
        result = _preserve_valid_contract_after_cli_failure(
            state={
                "kernel_contract": model_to_dict(contract),
                "kernel_analysis": "source-backed diagnosis",
                "reproduce_case": "userspace C trigger",
                "kernel_diagnosis": "collect serial log",
            },
            error=RuntimeError("Codex timed out after 600s"),
            error_message="kernel_expert CLI 超时: Codex timed out after 600s",
            semcode_path_analysis=None,
        )
    retained = result["kernel_contract"]
    assert retained["status"] == "blocked"
    assert retained["root_cause"] == contract.root_cause
    assert result["kernel_ready_for_test"] is False
    assert "source-backed diagnosis" in result["kernel_analysis"]
    assert "timed out" in result["kernel_analysis"]
    assert retained["evidence"]


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


def test_codex_evidence_uses_declared_log_without_report_sibling(tmp_path):
    log = tmp_path / "original-crash.log"
    log.write_text(
        "BUG: kernel NULL pointer dereference\n"
        "Call Trace:\n"
        " target_fault+0x1/0x2\n"
        " target_caller+0x3/0x4\n"
        "---[ end trace 000 ]---\n",
        encoding="utf-8",
    )

    _stage_codex_evidence(tmp_path / "workdir", [("original.log", str(log))])

    staged = tmp_path / "workdir" / "evidence" / "original.log"
    assert "target_fault" in staged.read_text(encoding="utf-8")


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


def test_semcode_path_analysis_cache_requires_exact_commit_and_source(tmp_path):
    source = tmp_path / "linux"
    source.mkdir()
    commit = "b" * 40
    raw = {
        "status": "ok",
        "scope": {
            "kernel_commit": commit,
            "source_domains": [{"kind": "kernel", "root": str(source)}],
        },
        "analysis": {"case_id": "same-session-case"},
        "evidence": [],
    }

    restored = _restore_cached_semcode_path_analysis(
        raw,
        expected_commit=commit,
        kernel_source_path=str(source),
    )
    assert restored is not None
    assert restored.analysis.case_id == "same-session-case"
    assert restored.evidence[-1]["status"] == "reused_same_session_exact_source"

    assert _restore_cached_semcode_path_analysis(
        raw,
        expected_commit="c" * 40,
        kernel_source_path=str(source),
    ) is None
    other_source = tmp_path / "other-linux"
    other_source.mkdir()
    assert _restore_cached_semcode_path_analysis(
        raw,
        expected_commit=commit,
        kernel_source_path=str(other_source),
    ) is None


def test_semcode_evidence_reuses_exact_cached_identity(tmp_path, monkeypatch):
    output = tmp_path / "session"
    output.mkdir()
    source = tmp_path / "linux"
    source.mkdir()
    commit = "d" * 40
    evidence_path = output / "semcode-evidence.json"
    evidence_path.write_text(json.dumps({
        "status": "ok",
        "kernel_source": str(source.resolve()),
        "expected_kernel_commit": commit,
        "entries": [{"function": "target", "result": "cached"}],
        "failures": [],
    }), encoding="utf-8")

    class UnexpectedClient:
        def __init__(self, **_kwargs):
            raise AssertionError("valid exact evidence must be reused")

    monkeypatch.setattr("agents.kernel_expert.SemcodeMcpClient", UnexpectedClient)
    path = _materialize_semcode_evidence(
        output,
        source_path=str(source),
        expected_commit=commit,
        command="semcode-mcp",
        args=[],
        evidence_text="RIP: target+0x10/0x20",
    )
    assert Path(path) == evidence_path.resolve()
    assert json.loads(evidence_path.read_text(encoding="utf-8"))["entries"][0]["result"] == "cached"


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


def test_incremental_round_context_carries_setup_and_prefix():
    rendered = _render_incremental_test_round_context([
        {
            "attempts": 2,
            "status": "failed",
            "code": "FAILED_SIGNAL_NOT_FOUND",
            "verified_setup": ["LUMEN_REPRO_VCAN_CREATE"],
            "best_call_chain_prefix": ["j1939_sock_pending_del"],
            "missing_frames": ["j1939_session_put"],
            "kernel_feedback": "preserve setup",
        }
    ])
    assert "LUMEN_REPRO_VCAN_CREATE" in rendered
    assert "j1939_sock_pending_del" in rendered
    assert "Preserve every setup prerequisite" in rendered
