"""Robustness regression tests for the 4 cross-model compatibility fixes.

Background: issues found when running the Lumen workflow with non-Claude
LLM backends (DeepSeek / GLM / MiniMax) revealed brittle defaults and
hardcoded fallbacks. These tests guard against regressions.

Covers:
  1. AnthropicBackend default max_tokens = 8192 (was 2048, truncated
     knowledge_base docs mid-stream).
  2. RunCrashCommandsInput coerces string args to list (LLMs occasionally
     serialize list-typed args as JSON strings).
  3. expected_signal fallbacks do NOT default to "blocked for more than"
     (would mismatch KASAN/UAF bugs and force-test-fail). Empty signal
     triggers broad detection pattern set in test_expert.
  4. kernel_expert reads outputs/kernel_contract.json when text-extracted
     contract is incomplete (Claude Code `--output-format json` returns
     only the final assistant turn, losing markers written mid-loop).
"""

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.backends import AnthropicBackend
from agents.contracts import KernelExpertOutput
from agents.crash_tools import RunCrashCommandsInput
from agents.test_expert import _build_detection_signals
from llm_config import get_llm_with_config


# ---------------------------------------------------------------------------
# Fix 1: max_tokens default raised to 8192
# ---------------------------------------------------------------------------


def test_anthropic_backend_default_max_tokens_is_8192():
    """Default max_tokens must be 8192, not 2048.

    2048 truncated knowledge_base docs mid-stream on DeepSeek/GLM/MiniMax.
    """
    import inspect

    sig = inspect.signature(AnthropicBackend.__init__)
    assert sig.parameters["max_tokens"].default == 8192


def test_anthropic_backend_instance_uses_8192_without_override():
    b = AnthropicBackend(base_url="http://x", api_key="k", model_name="m")
    assert b._max_tokens == 8192


def test_get_llm_with_config_uses_8192_when_config_silent():
    """knowledge_base / validator / pm agents don't set max_tokens in config,
    so they must inherit the 8192 default (not the old 2048)."""
    llm = get_llm_with_config(
        {
            "backend": "anthropic",
            "api_key": "k",
            "base_url": "http://x",
            "model_name": "m",
        },
        agent_name="knowledge_base",
    )
    assert llm._max_tokens == 8192


def test_get_llm_with_config_respects_explicit_max_tokens_override():
    """Agent config can still override the default for memory-tight models."""
    llm = get_llm_with_config(
        {
            "backend": "anthropic",
            "api_key": "k",
            "base_url": "http://x",
            "model_name": "m",
            "max_tokens": 4096,
        },
        agent_name="validator",
    )
    assert llm._max_tokens == 4096


def test_get_llm_with_config_uses_default_config_max_tokens():
    """Default_config.max_tokens is honored as the second-priority source."""
    llm = get_llm_with_config(
        {"backend": "anthropic", "api_key": "k", "base_url": "http://x", "model_name": "m"},
        default_config={"max_tokens": 16384},
        agent_name="pm",
    )
    assert llm._max_tokens == 16384


# ---------------------------------------------------------------------------
# Fix 2: RunCrashCommandsInput field_validator coerces string → list
# ---------------------------------------------------------------------------


def test_run_crash_commands_accepts_native_list():
    r = RunCrashCommandsInput(commands=["ps", "bt", "log"])
    assert r.commands == ["ps", "bt", "log"]


def test_run_crash_commands_accepts_json_string_list():
    """DeepSeek/GLM occasionally pass list-typed args as JSON strings."""
    r = RunCrashCommandsInput(commands='["ps", "bt"]')
    assert r.commands == ["ps", "bt"]


def test_run_crash_commands_accepts_single_command_string():
    """Some models pass a single command as a string instead of a 1-list."""
    r = RunCrashCommandsInput(commands="ps")
    assert r.commands == ["ps"]


def test_run_crash_commands_accepts_empty_string():
    r = RunCrashCommandsInput(commands="")
    assert r.commands == []


def test_run_crash_commands_accepts_none():
    r = RunCrashCommandsInput(commands=None)
    assert r.commands == []


def test_run_crash_commands_rejects_malformed_json_falls_back_to_single():
    """Bad JSON like '[bad json' is treated as a single command, not an error."""
    r = RunCrashCommandsInput(commands="[bad json")
    assert r.commands == ["[bad json"]


def test_run_crash_commands_coerces_non_string_elements():
    """List with non-string elements (e.g. integers) coerced to strings."""
    r = RunCrashCommandsInput(commands='["ps", 42, true]')
    assert r.commands == ["ps", "42", "True"]


# ---------------------------------------------------------------------------
# Fix 3: expected_signal fallback must NOT default to "blocked for more than"
# ---------------------------------------------------------------------------


def test_build_detection_signals_empty_signal_uses_broad_fallback_set():
    """When expected_signal is empty, test_expert must provide a broad set of
    kernel-error patterns (not the old single hung_task-specific signal that
    would force-fail KASAN/UAF tests)."""
    d = _build_detection_signals({}, "")
    assert d.serial_signals, "fallback serial_signals must not be empty"
    # Must cover KASAN/UAF/WARNING/hung_task — the major kernel error classes
    # we expect to see in a real reproduction boot log.
    patterns = " ".join(d.serial_signals).lower()
    assert "kasan" in patterns
    assert "bug:" in patterns
    assert "warning:" in patterns
    assert "hung_task" in patterns
    assert "blocked for more than" in patterns


def test_build_detection_signals_empty_signal_excludes_bare_kernel_panic():
    """Bare 'kernel panic' as a fallback pattern would false-positive on
    boot-time panics (kasan_populate_shadow OOM, missing rootfs). The fallback
    set must NOT contain 'kernel panic' as a substring pattern."""
    d = _build_detection_signals({}, "")
    for sig in d.serial_signals:
        assert sig.lower() != "kernel panic"


def test_build_detection_signals_explicit_signal_takes_priority():
    """When kernel_expert provides a specific expected_signal, the fallback
    set must NOT pollute serial_signals."""
    d = _build_detection_signals({}, "BUG: KASAN: slab-use-after-free")
    assert d.serial_signals == ["BUG: KASAN: slab-use-after-free"]


def test_build_detection_signals_empty_signal_disables_panic_on_warn():
    """Fallback path must not enable panic_on_warn — that's a kernel cmdline
    decision that only kernel_expert should declare."""
    d = _build_detection_signals({}, "")
    assert d.panic_on_warn is False
    assert d.panic_is_pass is False


def test_kernel_expert_auto_contract_fields_uses_empty_signal_when_test_signal_missing():
    """_generate_auto_contract_fields must NOT fall back to a hung_task
    signal. Empty expected_signal is the correct fallback so test_expert's
    broad detection set kicks in."""
    from agents.kernel_expert import _generate_auto_contract_fields

    # Empty contract + no reproducer dir → no test_signal inferred
    contract = KernelExpertOutput(
        status="ok",
        target_arch="",
        boot_kernel_path="",
        reproducer_dir="",
        reproducer_module_path="",
        test_script_path="",
        expected_signal="",  # what we're testing
    )
    # Use a non-existent outputs dir so _find_actual_reproducer_path returns
    # empty — but _generate_auto_contract_fields should still fill target_arch
    # and boot_kernel_path from input_artifacts, NOT expected_signal.
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["LUMEN_OUTPUT_DIR"] = tmp
        fields = _generate_auto_contract_fields(
            contract,
            {
                "vmlinux_path": "",
                "boot_kernel_path": "",
            },
        )
    assert "expected_signal" in fields
    assert fields["expected_signal"] == ""


# ---------------------------------------------------------------------------
# Fix 4: kernel_contract.json file read path
# ---------------------------------------------------------------------------


def test_kernel_contract_file_is_preferred_over_text_extraction(tmp_path, monkeypatch):
    """When the text-extracted contract is incomplete but outputs/kernel_contract.json
    exists, kernel_expert_node should read the file first.

    This guards against Claude Code `--output-format json` returning only the
    final assistant turn, losing markers written in earlier turns."""
    from agents import kernel_expert as ke

    monkeypatch.setattr(ke, "paths_get_output_dir", lambda: tmp_path)

    contract_data = {
        "status": "ok",
        "target_arch": "arm64",
        "boot_kernel_path": "/tmp/Image",
        "reproducer_dir": "/tmp/repro",
        "reproducer_module_path": "/tmp/repro/x.ko",
        "test_script_path": "/tmp/repro/test.sh",
        "expected_signal": "BUG: KASAN:",
        "binaries_dir": "",
        "build_status": "passed",
    }
    contract_file = tmp_path / "kernel_contract.json"
    contract_file.write_text(json.dumps(contract_data))

    # Simulate text extraction returning an empty contract (no KERNEL_CONTRACT
    # marker in LLM output because it was written mid-loop and lost).
    text_contract = ke._extract_kernel_contract("LLM said: done. No contract in text.")
    assert not ke._kernel_contract_has_handoff(text_contract)

    # Reproduce the file-fallback logic from kernel_expert_node
    kernel_contract = text_contract
    if not ke._kernel_contract_has_handoff(kernel_contract):
        cf = tmp_path / "kernel_contract.json"
        if cf.exists():
            data = json.loads(cf.read_text())
            if isinstance(data, dict):
                file_contract = ke._model_validate(KernelExpertOutput, data)
                if ke._kernel_contract_has_handoff(file_contract):
                    kernel_contract = file_contract

    assert ke._kernel_contract_has_handoff(kernel_contract)
    assert kernel_contract.target_arch == "arm64"
    assert kernel_contract.expected_signal == "BUG: KASAN:"


def test_kernel_contract_text_extraction_still_works_without_file(tmp_path, monkeypatch):
    """When no kernel_contract.json file exists, the text-extraction path
    must still function (backward compatibility for LLMs that emit
    KERNEL_CONTRACT in their final response)."""
    from agents import kernel_expert as ke

    monkeypatch.setattr(ke, "paths_get_output_dir", lambda: tmp_path)

    # Write a kernel-style boot image header so _validate_kernel_contract_artifacts
    # doesn't reject boot_kernel_path as ELF.
    with tempfile.NamedTemporaryFile(suffix="-bzImage") as bz:
        bz.write(b"MZ\x00\x00")
        bz.flush()
        with tempfile.NamedTemporaryFile(suffix="-test.sh") as ts:
            ts.write(b"#!/bin/sh\ntrue\n")
            ts.flush()
            text = f"""
REPRODUCE_CASE: uaf trigger
KERNEL_DIAGNOSIS: kasan

TARGET_ARCH: x86_64
BOOT_KERNEL_PATH: {bz.name}
REPRODUCER_DIR: {tmp_path}
REPRODUCER_MODULE_PATH: {tmp_path}/x.ko
TEST_SCRIPT_PATH: {ts.name}
EXPECTED_SIGNAL: BUG: KASAN:
BINARIES_DIR:

KERNEL_CONTRACT:
```json
{{
  "status": "ok",
  "target_arch": "x86_64",
  "boot_kernel_path": "{bz.name}",
  "reproducer_dir": "{tmp_path}",
  "reproducer_module_path": "{tmp_path}/x.ko",
  "test_script_path": "{ts.name}",
  "expected_signal": "BUG: KASAN:",
  "build_status": "passed"
}}
```
"""
            contract = ke._extract_kernel_contract(text)
            assert ke._kernel_contract_has_handoff(contract)
            assert contract.expected_signal == "BUG: KASAN:"
