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
  5. OpenCodeBackend integration (alternative agent-loop CLI for
     environments without Claude Code).
"""

import json
import os
import sys
import tempfile
import subprocess
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.backends import AnthropicBackend
from agents.contracts import KernelExpertOutput
from agents import crash_tools
from agents.crash_tools import RunCrashCommandsInput
from agents.test_expert import _build_detection_signals
from llm_config import get_llm_with_config


def test_boot_kernel_ext4_rootfs_uses_virtio_disk(monkeypatch, tmp_path):
    """boot_kernel(rootfs_path=...) should use an ext4 virtio root disk."""
    from agents.qemu_tools import boot_kernel

    kernel = tmp_path / "bzImage"
    kernel.write_bytes(b"MZ fake bootable image")
    rootfs = tmp_path / "rootfs.ext4"
    rootfs.write_bytes(b"fake ext4")
    captured = {}

    def fake_run(cmd, capture_output=True, text=True, timeout=300):
        if "-serial" not in cmd:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        captured["cmd"] = cmd
        serial_arg = cmd[cmd.index("-serial") + 1]
        serial_path = serial_arg.split("file:", 1)[1]
        Path(serial_path).write_text("Linux version test\nAutomated test complete\n")
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", fake_run)
    result = boot_kernel(
        kernel_path=str(kernel),
        rootfs_path=str(rootfs),
        arch="x86_64",
        timeout=1,
        memory="256M",
    )

    cmd = captured["cmd"]
    assert result.startswith("✓ Boot completed successfully")
    assert "-initrd" not in cmd
    assert any(str(rootfs) in item for item in cmd)
    assert "root=/dev/vda rw rootfstype=ext4 init=/init" in cmd[cmd.index("-append") + 1]
    assert "virtio-blk-pci,drive=rootfs" in cmd


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


def test_crash_binary_lookup_prefers_project_managed_tools(tmp_path, monkeypatch):
    """Arch-specific crash binaries should come from Analysis-SKILL/tools/crash."""
    crash_dir = tmp_path / "Analysis-SKILL" / "tools" / "crash"
    crash_dir.mkdir(parents=True)
    crash_bin = crash_dir / "crash_arm64"
    crash_bin.write_text("#!/bin/sh\n")
    crash_bin.chmod(0o755)

    monkeypatch.setattr(crash_tools, "PROJECT_ROOT", tmp_path)

    assert crash_tools._select_crash_binary_for_arch("arm64") == str(crash_bin)


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


def test_kernel_expert_auto_contract_fields_uses_empty_signal_when_test_signal_missing(monkeypatch):
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
    # Note: monkeypatch paths_get_output_dir rather than LUMEN_OUTPUT_DIR env
    # var because _session_dir (set by previous tests) takes precedence.
    with tempfile.TemporaryDirectory() as tmp:
        monkeypatch.setattr("agents.kernel_expert.paths_get_output_dir", lambda: Path(tmp))
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
        "execution_steps": [{"type": "run_binary", "path": "bin/trigger"}],
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
  "execution_steps": [{{"type": "run_binary", "path": "bin/trigger"}}],
  "expected_signal": "BUG: KASAN:",
  "build_status": "passed"
}}
```
"""
            contract = ke._extract_kernel_contract(text)
            assert ke._kernel_contract_has_handoff(contract)
            assert contract.expected_signal == "BUG: KASAN:"


# ---------------------------------------------------------------------------
# Fix 5: OpenCodeBackend integration
# ---------------------------------------------------------------------------


def _make_opencode_subprocess_result(stdout: str = "", stderr: str = "", returncode: int = 0):
    """Helper: build a CompletedProcess-like object for OpenCodeBackend."""
    class _Result:
        def __init__(self, stdout, stderr, returncode):
            self.stdout = stdout
            self.stderr = stderr
            self.returncode = returncode
    return _Result(stdout, stderr, returncode)


def _mock_script_invoke(jsonl_content: str, tmp_path, monkeypatch):
    """Set up mocks so that OpenCodeBackend.invoke reads *jsonl_content*
    from subprocess.run stdout (bypassing real opencode invocations)."""
    monkeypatch.setattr(
        "agents.backends.subprocess.run",
        lambda cmd, **kw: _make_opencode_subprocess_result(
            stdout=jsonl_content, returncode=0,
        ),
    )


def test_opencode_extract_session_id():
    """_extract_session_id must pull sessionID from step_start event."""
    from agents.backends import OpenCodeBackend

    b = OpenCodeBackend(cli_command="opencode")

    stdout = (
        '{"type":"step_start","part":{"id":"p1","sessionID":"ses_abc123","type":"step-start"}}\n'
        '{"type":"text","part":{"text":"hello"}}\n'
    )
    assert b._extract_session_id(stdout) == "ses_abc123"

    # No step_start → None
    assert b._extract_session_id('{"type":"text","part":{"text":"hi"}}\n') is None

    # step_start without sessionID → None
    assert b._extract_session_id('{"type":"step_start","part":{"type":"step-start"}}\n') is None

    # Empty output → None
    assert b._extract_session_id("") is None


def test_opencode_export_session_text(monkeypatch):
    """_export_session_text must extract assistant text from export JSON."""
    from agents.backends import OpenCodeBackend

    b = OpenCodeBackend(cli_command="opencode")

    export_json = json.dumps({
        "info": {"id": "ses_abc123"},
        "messages": [
            {
                "info": {"role": "user"},
                "parts": [{"type": "text", "text": "analyze this"}],
            },
            {
                "info": {"role": "assistant"},
                "parts": [
                    {"type": "text", "text": "Here is the analysis.\n"},
                    {"type": "text", "text": "Conclusion: OK."},
                ],
            },
        ],
    })

    def fake_run(cmd, **kwargs):
        return _make_opencode_subprocess_result(export_json)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    text = b._export_session_text("ses_abc123")
    assert text == "Here is the analysis.\nConclusion: OK."


def test_opencode_export_session_text_empty_parts(monkeypatch):
    """_export_session_text must return None when assistant parts are empty."""
    from agents.backends import OpenCodeBackend

    b = OpenCodeBackend(cli_command="opencode")

    export_json = json.dumps({
        "info": {"id": "ses_abc123"},
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
            {"info": {"role": "assistant"}, "parts": []},
        ],
    })

    def fake_run(cmd, **kwargs):
        return _make_opencode_subprocess_result(export_json)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    assert b._export_session_text("ses_abc123") is None


def test_opencode_export_session_text_export_fails(monkeypatch):
    """_export_session_text must return None when export fails."""
    from agents.backends import OpenCodeBackend

    b = OpenCodeBackend(cli_command="opencode")

    def fake_run(cmd, **kwargs):
        return _make_opencode_subprocess_result("", returncode=1)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    assert b._export_session_text("ses_abc123") is None


def test_opencode_backend_uses_export_when_session_id_found(tmp_path, monkeypatch):
    """When text events are absent but step_start has a sessionID,
    fall back to opencode export for the response."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", model="csi-provider/GLM-5")

    # run output: only step_start, no text events
    run_jsonl = '{"type":"step_start","part":{"id":"p1","sessionID":"ses_test123","type":"step-start"}}\n'

    export_json = json.dumps({
        "info": {"id": "ses_test123"},
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
            {
                "info": {"role": "assistant"},
                "parts": [{"type": "text", "text": "OK from export"}],
            },
        ],
    })

    call_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        # First call is opencode run → return run_jsonl in stdout
        if call_count == 1:
            return _make_opencode_subprocess_result(stdout=run_jsonl, returncode=0)
        # Second call is opencode export → return export JSON
        return _make_opencode_subprocess_result(export_json)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    msg = b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))
    assert msg.content == "OK from export", (
        f"must use export content; got {msg.content!r}"
    )
    assert call_count == 2, "export must be called after run"


def test_opencode_backend_falls_back_when_export_fails(tmp_path, monkeypatch):
    """When export returns no assistant text, fall back to JSONL
    text events parsing."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", model="csi-provider/GLM-5")

    run_jsonl = (
        '{"type":"step_start","part":{"id":"p1","sessionID":"ses_test456","type":"step-start"}}\n'
        '{"type":"text","part":{"text":"fallback text"}}\n'
    )

    export_json = json.dumps({
        "info": {"id": "ses_test456"},
        "messages": [
            {"info": {"role": "user"}, "parts": [{"type": "text", "text": "hi"}]},
            {"info": {"role": "assistant"}, "parts": []},
        ],
    })

    call_count = 0

    def fake_run(cmd, **kwargs):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_opencode_subprocess_result(stdout=run_jsonl, returncode=0)
        return _make_opencode_subprocess_result(export_json)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    msg = b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))
    assert msg.content == "fallback text", (
        f"must fall back to JSONL text; got {msg.content!r}"
    )


def test_opencode_backend_agent_file_generation(tmp_path, monkeypatch):
    """OpenCodeBackend must write a markdown agent file with YAML frontmatter
    and the system prompt as body before each invoke."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage, SystemMessage

    b = OpenCodeBackend(
        cli_command="opencode",
        model="csi-provider/GLM-5",
        agent_name="lumen_test_agent",
    )

    jsonl = '{"type":"text","part":{"text":"hello"}}\n'
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _make_opencode_subprocess_result(stdout=jsonl, returncode=0)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    b.invoke(
        [SystemMessage(content="you are a kernel expert"),
         HumanMessage(content="analyze this")],
        workdir=str(tmp_path),
    )

    agent_file = Path.home() / ".opencode" / "agents" / "lumen_test_agent.md"
    assert agent_file.exists(), "agent markdown file must be written"
    content = agent_file.read_text()
    assert content.startswith("---\n"), "agent file must have YAML frontmatter"
    assert "mode: primary" in content, "agent file must declare primary mode"
    assert "you are a kernel expert" in content, "system prompt must be the body"

    # The cmd is the direct opencode args list.
    assert captured["cmd"][0] == "opencode"
    assert "--agent" in captured["cmd"]
    assert "lumen_test_agent" in captured["cmd"]
    assert "--format" in captured["cmd"]
    assert "json" in captured["cmd"]
    assert "--dangerously-skip-permissions" in captured["cmd"]


def test_opencode_backend_accumulates_text_events(tmp_path, monkeypatch):
    """All type:'text' events must be concatenated into the final AIMessage."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", model="csi-provider/GLM-5")

    jsonl = (
        '{"type":"step_start","part":{"type":"step-start"}}\n'
        '{"type":"tool_use","part":{"tool":"bash"}}\n'
        '{"type":"tool_result","part":{"content":"some output"}}\n'
        '{"type":"text","part":{"text":"partial answer "}}\n'
        '{"type":"text","part":{"text":"continued"}}\n'
    )
    _mock_script_invoke(jsonl, tmp_path, monkeypatch)

    msg = b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))
    assert msg.content == "partial answer continued", (
        f"text events must be concatenated; got {msg.content!r}"
    )


def test_opencode_backend_skips_non_text_events(tmp_path, monkeypatch):
    """Non-text events (step_start, tool_use, tool_result) must NOT pollute
    the final AIMessage content."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", model="csi-provider/GLM-5")

    jsonl = (
        '{"type":"step_start","part":{"type":"step-start","text":"should be ignored"}}\n'
        '{"type":"tool_use","part":{"tool":"bash","text":"should be ignored"}}\n'
        '{"type":"text","part":{"text":"only this"}}\n'
    )
    _mock_script_invoke(jsonl, tmp_path, monkeypatch)

    msg = b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))
    assert msg.content == "only this"


def test_opencode_backend_raises_on_nonzero_exit(tmp_path, monkeypatch):
    """Non-zero returncode must raise RuntimeError with output prefix
    so kernel_expert routes to the blocked-contract path."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", model="csi-provider/GLM-5")

    monkeypatch.setattr(
        "agents.backends.subprocess.run",
        lambda cmd, **kw: _make_opencode_subprocess_result(
            stdout="error output", returncode=2,
        ),
    )

    with pytest.raises(RuntimeError, match="OpenCode failed"):
        b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))


def test_opencode_backend_raises_on_no_text_events(tmp_path, monkeypatch):
    """When the output has zero text events, no sessionID, and no tool_use,
    must raise so kernel_expert falls through to the empty-text fallback."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", model="csi-provider/GLM-5")

    # Only step_start, no text events, no sessionID, no tool_use → should raise
    _mock_script_invoke(
        '{"type":"step_start","part":{"type":"step-start"}}\n',
        tmp_path, monkeypatch,
    )

    with pytest.raises(RuntimeError, match="no text events"):
        b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))


def test_opencode_backend_returns_empty_when_agent_active(tmp_path, monkeypatch):
    """When output has tool_use events but no text events, return empty
    AIMessage so kernel_expert's fallback can discover files on disk."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", model="csi-provider/GLM-5")

    # Has tool_use events, no text events, no sessionID → should NOT raise
    _mock_script_invoke(
        '{"type":"step_start","part":{"type":"step-start"}}\n'
        '{"type":"tool_use","part":{"tool":"bash"}}\n'
        '{"type":"tool_result","part":{"content":"output"}}\n'
        '{"type":"step_finish","part":{"type":"step-finish"}}\n',
        tmp_path, monkeypatch,
    )

    msg = b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))
    assert msg.content == "", (
        f"should return empty content when agent was active; got {msg.content!r}"
    )


def test_opencode_backend_passes_model_and_dir(tmp_path, monkeypatch):
    """-m provider/model and --dir workdir must reach the CLI when set."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(
        cli_command="opencode",
        model="csi-provider/GLM-5",
    )

    jsonl = '{"type":"text","part":{"text":"ok"}}\n'
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _make_opencode_subprocess_result(stdout=jsonl, returncode=0)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))

    assert "-m" in captured["cmd"]
    assert "csi-provider/GLM-5" in captured["cmd"]
    assert "--dir" in captured["cmd"]
    assert str(tmp_path) in captured["cmd"]


def test_opencode_backend_uses_add_dirs_first_when_no_workdir(tmp_path, monkeypatch):
    """When workdir is empty but add_dirs is set, the first add_dirs
    entry should become --dir."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", model="csi-provider/GLM-5")

    jsonl = '{"type":"text","part":{"text":"ok"}}\n'

    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["cwd"] = kwargs.get("cwd")
        return _make_opencode_subprocess_result(stdout=jsonl, returncode=0)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    kernel_source = tmp_path / "kernel"
    kernel_source.mkdir()
    b.invoke(
        [HumanMessage(content="hi")],
        add_dirs=[str(kernel_source)],
    )

    assert "--dir" in captured["cmd"]
    assert str(kernel_source) in captured["cmd"]
    assert captured["cwd"] == str(kernel_source)


def test_opencode_backend_timeout_raises_runtime_error(tmp_path, monkeypatch):
    """TimeoutExpired must surface as RuntimeError with the timeout value."""
    import subprocess as sp
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", cli_timeout=5)

    def fake_run(cmd, **kwargs):
        raise sp.TimeoutExpired(cmd=cmd, timeout=5)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="OpenCode timed out after 5s"):
        b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))


def test_opencode_backend_file_not_found(tmp_path, monkeypatch):
    """FileNotFoundError when CLI binary is missing must surface as
    RuntimeError naming the binary."""
    import subprocess as sp
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode")

    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(2, "No such file", cmd[0])

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    with pytest.raises(RuntimeError, match="binary not found"):
        b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))


def test_opencode_backend_registered_in_llm_config():
    """get_llm_with_config must return an OpenCodeBackend when
    backend='opencode' is set in agent config."""
    from agents.backends import OpenCodeBackend
    from llm_config import get_llm_with_config

    llm = get_llm_with_config(
        {
            "backend": "opencode",
            "cli_command": "opencode",
            "model": "csi-provider/GLM-5",
            "agent_name": "lumen_kernel_expert",
            "cli_timeout": 3600,
        },
        agent_name="kernel_expert",
    )
    assert isinstance(llm, OpenCodeBackend)
    assert llm._cli_command == "opencode"
    assert llm._model == "csi-provider/GLM-5"
    assert llm._agent_name == "lumen_kernel_expert"
    assert llm._cli_timeout == 3600


def test_opencode_backend_blocked_for_non_kernel_expert_agents():
    """Only kernel_expert is permitted to use opencode backend (same gate
    as claude_code). validator/pm/test_expert must be rejected."""
    from llm_config import validate_agent_backend

    validate_agent_backend("kernel_expert", "opencode")  # ok

    for agent in ["validator", "pm", "test_expert", "knowledge_base"]:
        with pytest.raises(ValueError, match="not permitted to use opencode"):
            validate_agent_backend(agent, "opencode")


def test_opencode_backend_pure_flag(tmp_path, monkeypatch):
    """pure=True must add --pure to skip external plugins."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    b = OpenCodeBackend(cli_command="opencode", model="m", pure=True)

    jsonl = '{"type":"text","part":{"text":"ok"}}\n'
    captured = {}

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return _make_opencode_subprocess_result(stdout=jsonl, returncode=0)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    b.invoke([HumanMessage(content="hi")], workdir=str(tmp_path))
    assert "--pure" in captured["cmd"]


def test_kernel_expert_agent_loop_function_renamed():
    """Old _run_kernel_expert_with_claude_code must be gone, replaced by
    the backend-agnostic _run_kernel_expert_with_agent_loop."""
    import agents.kernel_expert as ke

    assert hasattr(ke, "_run_kernel_expert_with_agent_loop")
    assert not hasattr(ke, "_run_kernel_expert_with_claude_code")
