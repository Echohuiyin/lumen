"""P1 tests: structured paths, causal reproduction, retry, and crash leases."""

from pathlib import Path
import sys
import tempfile

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.contracts import KernelExpertOutput, TestPlan
from agents.error_handling import retry_transient
from agents.kernel_expert import _validate_kernel_contract_artifacts
from agents.test_runner import _check_causal_reproduction


def _model_validate(model_cls, data):
    return model_cls.model_validate(data) if hasattr(model_cls, "model_validate") else model_cls.parse_obj(data)


def _valid_uaf_contract(kernel_path: str, test_script: str) -> KernelExpertOutput:
    summary = "ioctl -> get -> close -> put -> free -> ioctl access"
    return _model_validate(KernelExpertOutput, {
        "status": "ok", "target_arch": "x86_64", "boot_kernel_path": kernel_path,
        "test_script_path": test_script, "expected_signal": "BUG: KASAN",
        "path_analysis_required": True, "all_possible_paths": [summary],
        "max_likely_path": summary, "reproduction_target_path": summary,
        "path_analysis_scope": {
            "kernel_commit": "v6.12", "kernel_config": "CONFIG_KASAN=y",
            "entry_points": ["ioctl"], "object_type": "struct foo",
            "concurrency_model": "close races ioctl",
        },
        "uaf_analysis": {
            "case_id": "case-1",
            "paths": [{
                "id": "p1", "summary": summary,
                "events": [
                    {"kind": "get", "function": "foo_get", "ref_delta": 1},
                    {"kind": "put", "function": "foo_put", "ref_delta": -1},
                    {"kind": "free", "function": "foo_release", "ref_delta": 0},
                    {"kind": "access", "function": "foo_ioctl", "ref_delta": 0},
                ], "net_delta": 0, "terminal_state": "uaf",
            }],
            "coverage": {"normal_paths_considered": True, "error_paths_considered": True},
            "max_likely_path_id": "p1", "selection_rationale": "vmcore evidence",
            "reproduction_target_path_id": "p1", "target_contexts": ["foo_ioctl"],
        },
    })


def test_structured_uaf_path_contract_and_causal_markers_are_required():
    with tempfile.NamedTemporaryFile() as kernel, tempfile.NamedTemporaryFile(suffix=".sh") as script:
        kernel.write(b"MZ\x00\x00")
        kernel.flush()
        script.write(
            b"#!/bin/sh\n"
            b"echo LUMEN_REPRO_START:case-1:p1\n"
            b"echo run\n"
            b"echo LUMEN_REPRO_END:case-1:p1:done\n"
        )
        script.flush()
        validated = _validate_kernel_contract_artifacts(
            _valid_uaf_contract(kernel.name, script.name), path_analysis_required=True,
        )
        assert validated.status == "ok"
        assert validated.uaf_analysis.paths[0].id == "p1"


def test_structured_uaf_delta_mismatch_is_blocked():
    with tempfile.NamedTemporaryFile() as kernel, tempfile.NamedTemporaryFile(suffix=".sh") as script:
        kernel.write(b"MZ\x00\x00")
        kernel.flush()
        script.write(b"#!/bin/sh\necho LUMEN_REPRO_START:case-1:p1\necho LUMEN_REPRO_END:case-1:p1:done\n")
        script.flush()
        contract = _valid_uaf_contract(kernel.name, script.name)
        data = contract.model_dump() if hasattr(contract, "model_dump") else contract.dict()
        data["uaf_analysis"]["paths"][0]["net_delta"] = 1
        validated = _validate_kernel_contract_artifacts(
            _model_validate(KernelExpertOutput, data), path_analysis_required=True,
        )
        assert validated.status == "blocked"
        assert "net_delta" in validated.blocked_reason


def test_causal_reproduction_rejects_boot_signal_and_accepts_started_target_signal():
    plan = TestPlan(
        expected_signal="BUG: KASAN", reproduction_case_id="case-1", target_path_id="p1",
        target_contexts=["foo_ioctl"], require_causal_reproduction=True,
    )
    log = "BUG: KASAN boot noise\nLUMEN_REPRO_START:case-1:p1\nBUG: KASAN in foo_ioctl\n"
    result = _check_causal_reproduction(log, plan, "BUG: KASAN")
    assert result["reproducer_started"] is True
    assert result["signal_after_start"] is True
    assert result["target_context_matched"] is True


def test_retry_is_local_and_only_retries_transient(monkeypatch):
    import agents.error_handling as errors
    attempts = []
    monkeypatch.setattr(errors.time, "sleep", lambda _: None)

    def operation():
        attempts.append(1)
        if len(attempts) == 1:
            raise TimeoutError("temporary timeout")
        return "ok"

    assert retry_transient("crash startup", operation) == "ok"
    assert len(attempts) == 2


def test_crash_session_context_releases_on_exception(monkeypatch):
    import agents.crash_tools as crash_tools
    released = []
    marker = object()
    monkeypatch.setattr(crash_tools, "get_or_create_crash_session", lambda *_: marker)
    monkeypatch.setattr(crash_tools, "release_crash_session", lambda *args: released.append(args))
    try:
        with crash_tools.crash_session("vmcore", "vmlinux") as session:
            assert session is marker
            raise RuntimeError("injected")
    except RuntimeError:
        pass
    assert released == [("vmcore", "vmlinux")]
