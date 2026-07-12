"""Test the agent-active-without-text → disk-contract fallback chain.

Background: Kernel_expert's OpenCode backend runs ``opencode run --format json``
as a tool-calling agent loop.  The agent may produce a valid reproducer with
all files (crash_uaf.ko, test.sh, kernel_contract.json) but the LLM's final
response may contain only tool calls with no ``type:"text"`` event --- the
model's agent loop finished on a tool-use instead of a text summary.  In that
case:

1. OpenCodeBackend.invoke() finds zero text events and zero export text.
2. It must return empty AIMessage (not raise RuntimeError) when the JSONL
   output proves the agent was active (``tool_use`` events present).
3. kernel_expert's ``_parse_kernel_expert_response`` receives empty text,
   skips the blocked-contract detection (text is falsy), enters the empty-text
   fallback, and discovers ``kernel_contract.json`` on disk.

These tests validate each link of that chain.
"""

import json
import os
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parent.parent.parent


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def session_dir(tmp_path):
    """A temporary session directory that doubles as the outputs dir."""
    d = tmp_path / "session"
    d.mkdir()
    return d


@pytest.fixture
def complete_contract(session_dir):
    """Write a valid kernel_contract.json to *session_dir* and return its
    path and content dict."""
    contract = {
        "status": "ok",
        "target_arch": "x86_64",
        "vmlinux_path": "/home/user/vmlinux",
        "boot_kernel_path": "/home/user/bzImage",
        "reproducer_dir": str(session_dir / "reproducer"),
        "reproducer_module_path": str(session_dir / "reproducer" / "crash_uaf.ko"),
        "test_script_path": str(session_dir / "reproducer" / "test.sh"),
        "expected_signal": "BUG: KASAN: slab-use-after-free",
        "build_status": "passed",
        "blocked_reason": "",
    }
    (session_dir / "reproducer").mkdir(parents=True, exist_ok=True)
    (session_dir / "reproducer" / "test.sh").write_text("#!/bin/sh\necho test")
    (session_dir / "reproducer" / "crash_uaf.ko").write_text("fake .ko")
    (session_dir / "kernel_contract.json").write_text(
        json.dumps(contract, indent=2), encoding="utf-8"
    )
    return session_dir / "kernel_contract.json", contract


# ---------------------------------------------------------------------------
# 1. OpenCodeBackend — agent active but no text
# ---------------------------------------------------------------------------

def _make_result(stdout="", stderr="", returncode=0):
    class _R:
        pass
    r = _R()
    r.stdout = stdout
    r.stderr = stderr
    r.returncode = returncode
    return r


@pytest.fixture
def mock_subprocess(monkeypatch):
    """Install a controllable subprocess.run mock that returns a default
    ``_make_result`` so the caller can override via monkeypatch.setattr."""
    import agents.backends as bk
    sentinel = {"call_count": 0}

    def fake_run(cmd, **kw):
        sentinel["call_count"] += 1
        return _make_result(returncode=0)

    monkeypatch.setattr(bk.subprocess, "run", fake_run)
    return sentinel


def _invoke_opencode(monkeypatch, stdout: str, cli_timeout=10):
    """Helper: create an OpenCodeBackend, mock subprocess.run, invoke."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    monkeypatch.setattr(
        "agents.backends.subprocess.run",
        lambda cmd, **kw: _make_result(stdout=stdout, returncode=0),
    )
    b = OpenCodeBackend(cli_command="opencode", cli_timeout=cli_timeout)
    return b.invoke([HumanMessage(content="hi")], workdir="/tmp")


def test_returns_empty_when_agent_active_no_text(monkeypatch):
    """JSONL with tool_use events but zero text events → empty AIMessage."""
    stdout = (
        '{"type":"step_start","part":{"id":"p1","sessionID":"ses_t1","type":"step-start"}}\n'
        '{"type":"tool_use","part":{"tool":"bash","id":"c1","input":"ls"}}\n'
        '{"type":"tool_result","part":{"content":"file.c","tool_use_id":"c1"}}\n'
        '{"type":"step_finish","part":{"type":"step-finish"}}\n'
    )
    msg = _invoke_opencode(monkeypatch, stdout)
    assert msg.content == "", f"expected empty, got {msg.content!r}"


def test_returns_export_text_when_session_id_found(monkeypatch):
    """When step_start has a sessionID, export is tried after text events fail."""
    from agents.backends import OpenCodeBackend
    from langchain_core.messages import HumanMessage

    run_stdout = (
        '{"type":"step_start","part":{"id":"p1","sessionID":"ses_export","type":"step-start"}}\n'
        '{"type":"tool_use","part":{"tool":"bash","id":"c1"}}\n'
    )
    export_json = json.dumps({
        "info": {"id": "ses_export"},
        "messages": [
            {"info": {"role": "assistant"}, "parts": [{"type": "text", "text": "exported text"}]},
        ],
    })
    call_count = 0

    def fake_run(cmd, **kw):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return _make_result(stdout=run_stdout, returncode=0)
        return _make_result(stdout=export_json, returncode=0)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)
    b = OpenCodeBackend(cli_command="opencode")
    msg = b.invoke([HumanMessage(content="hi")], workdir="/tmp")
    assert msg.content == "exported text"
    assert call_count == 2


def test_raises_when_no_activity_and_no_text(monkeypatch):
    """JSONL with step_start only (no tool_use, no text) → RuntimeError."""
    stdout = '{"type":"step_start","part":{"id":"p1","type":"step-start"}}\n'
    with pytest.raises(RuntimeError, match="no text events"):
        _invoke_opencode(monkeypatch, stdout)


def test_raises_when_no_json_at_all(monkeypatch):
    """Empty stdout with no JSONL → RuntimeError."""
    with pytest.raises(RuntimeError, match="no text events"):
        _invoke_opencode(monkeypatch, "")


def test_text_events_are_still_parsed_normally(monkeypatch):
    """Normal JSONL with text events → content returned as-is."""
    stdout = (
        '{"type":"step_start","part":{"type":"step-start"}}\n'
        '{"type":"text","part":{"text":"hello world"}}\n'
    )
    msg = _invoke_opencode(monkeypatch, stdout)
    assert msg.content == "hello world"


def test_multiple_text_events_concatenated(monkeypatch):
    """Multiple text events are concatenated in order."""
    stdout = (
        '{"type":"text","part":{"text":"part1 "}}\n'
        '{"type":"tool_use","part":{"tool":"bash"}}\n'
        '{"type":"text","part":{"text":"part2"}}\n'
    )
    msg = _invoke_opencode(monkeypatch, stdout)
    assert msg.content == "part1 part2"


# ---------------------------------------------------------------------------
# 2. kernel_expert — empty-text fallback discovers contract on disk
# ---------------------------------------------------------------------------

@pytest.fixture
def patch_session_dir(monkeypatch, session_dir):
    """Point paths.get_output_dir to *session_dir* so the fallback searches
    the right location."""
    monkeypatch.setattr(
        "agents.kernel_expert.paths_get_output_dir",
        lambda: session_dir,
    )
    return session_dir


def _parse_with_empty_text(monkeypatch, patch_session_dir, **contract_overrides):
    """Call ``_parse_kernel_expert_response`` with empty text and a fake
    expert_results / state.  If ``contract_overrides`` contains a
    ``_no_file`` key the contract file is not written.

    Creates real temp files for boot_kernel_path and reproducer so
    _validate_kernel_contract_artifacts passes.
    """
    from agents.kernel_expert import _parse_kernel_expert_response

    # Create real files for path validation
    boot_kernel = patch_session_dir / "bzImage"
    boot_kernel.write_text("fake bzImage")
    vmlinux = patch_session_dir / "vmlinux"
    vmlinux.write_text("fake vmlinux")

    expert_results = [{"label": "crash", "summary": "test"}]
    input_artifacts = {
        "vmlinux_path": str(patch_session_dir / "vmlinux"),
        "boot_kernel_path": str(boot_kernel),
    }
    state = {"user_input": "test"}

    # Write contract overrides if caller provided them
    no_file = contract_overrides.pop("_no_file", False)
    if not no_file:
        reproducer = patch_session_dir / "reproducer"
        reproducer.mkdir(parents=True, exist_ok=True)
        (reproducer / "test.sh").write_text(
            "#!/bin/sh\necho test\n# REPRODUCER_SIGNAL: BUG: KASAN: slab-use-after-free"
        )
        (reproducer / "crash_uaf.ko").write_text("fake ko")

        contract = {
            "status": "ok",
            "target_arch": "x86_64",
            "vmlinux_path": str(patch_session_dir / "vmlinux"),
            "boot_kernel_path": str(boot_kernel),
            "reproducer_dir": str(reproducer),
            "reproducer_module_path": str(reproducer / "crash_uaf.ko"),
            "test_script_path": str(reproducer / "test.sh"),
            "expected_signal": "BUG: KASAN: slab-use-after-free",
            "build_status": "passed",
            "blocked_reason": "",
            **contract_overrides,
        }
        (patch_session_dir / "kernel_contract.json").write_text(
            json.dumps(contract, indent=2), encoding="utf-8"
        )
    else:
        # Still create a reproducer dir so _find_actual_reproducer_path
        # returns something, but no contract file.
        (patch_session_dir / "reproducer").mkdir(parents=True, exist_ok=True)
        (patch_session_dir / "reproducer" / "test.sh").write_text(
            "#!/bin/sh\necho test"
        )

    return _parse_kernel_expert_response(
        text="",
        expert_results=expert_results,
        input_artifacts=input_artifacts,
        state=state,
    )


def test_empty_text_fallback_picks_up_file_contract(monkeypatch, patch_session_dir):
    """When text is empty and kernel_contract.json exists on disk, the
    fallback must use the file's contract (with expected_signal, passed
    build_status) instead of auto-generating an inferior one."""
    result = _parse_with_empty_text(monkeypatch, patch_session_dir)
    contract = result["kernel_contract"]

    assert contract["status"] == "ok"
    assert contract["expected_signal"] == "BUG: KASAN: slab-use-after-free"
    assert contract["build_status"] == "passed"
    assert contract["target_arch"] == "x86_64"
    assert contract["reproducer_dir"].endswith("reproducer")
    assert "/test.sh" in contract["test_script_path"]
    assert "/crash_uaf.ko" in contract["reproducer_module_path"]
    assert result["kernel_ready_for_test"] is True


def test_empty_text_fallback_auto_generates_when_no_file(monkeypatch, patch_session_dir):
    """When text is empty AND no kernel_contract.json exists, the fallback
    auto-generates a contract. Since the reproducer's test.sh has no
    detectable error signal, the validator blocks it."""
    result = _parse_with_empty_text(monkeypatch, patch_session_dir, _no_file=True)
    contract = result["kernel_contract"]

    assert contract["status"] == "blocked"
    assert "expected_signal" in contract.get("blocked_reason", "")
    assert result["kernel_ready_for_test"] is False




def test_file_contract_fills_missing_fields_when_text_incomplete(
    monkeypatch, patch_session_dir, tmp_path
):
    """When the on-disk contract has fields the text-extracted one lacks,
    the file-based recovery (line 671) fills them in."""
    from agents.kernel_expert import _parse_kernel_expert_response

    # Contract on disk (written by _parse_with_empty_text) has all fields
    result = _parse_with_empty_text(monkeypatch, patch_session_dir)
    assert result["kernel_contract"]["build_status"] == "passed"

    # Second call: non-empty text with minimal contract → the file-based
    # recovery should fill in the missing fields from the on-disk contract.
    bz = tmp_path / "bz"
    bz.write_text("")
    vmlinux = tmp_path / "vmlinux"
    vmlinux.write_text("")
    expert_results = [{"label": "crash", "summary": "test"}]
    input_artifacts = {"vmlinux_path": str(vmlinux), "boot_kernel_path": str(bz)}
    state = {"user_input": "test"}

    partial_text = (
        "Some analysis\n"
        "<<<KERNEL_CONTRACT>>>\n"
        '{"status":"ok","target_arch":"x86_64"}\n'
        "<</KERNEL_CONTRACT>>>\n"
    )
    result2 = _parse_kernel_expert_response(
        text=partial_text,
        expert_results=expert_results,
        input_artifacts=input_artifacts,
        state=state,
    )
    contract2 = result2["kernel_contract"]
    # The file-based fallback at line 671 should fill in the missing fields
    # from the on-disk contract (expected_signal, build_status).
    assert contract2.get("expected_signal") == "BUG: KASAN: slab-use-after-free"
    assert contract2.get("build_status") == "passed"


def test_empty_text_fallback_ignores_stale_file_contract(monkeypatch, patch_session_dir):
    """When the on-disk contract has status='blocked' (stale from a previous
    failed run), the fallback should NOT use it and should auto-generate."""
    result = _parse_with_empty_text(
        monkeypatch, patch_session_dir,
        status="blocked",
        blocked_reason="stale from prev run",
    )
    contract = result["kernel_contract"]
    # The auto-generated fallback should override with status='ok'
    assert contract["status"] == "ok"


def test_blocked_contract_still_triggers_on_explicit_failure_text(monkeypatch):
    """When text explicitly contains 'OpenCode 调用失败', the blocked-contract
    path still fires — this protects against stale reproducer dirs when the
    CLI genuinely failed to start."""
    from agents.kernel_expert import _parse_kernel_expert_response

    result = _parse_kernel_expert_response(
        text="OpenCode 调用失败: something went wrong",
        expert_results=[],
        input_artifacts={},
        state={},
    )
    assert result["kernel_contract"]["status"] == "blocked"
    assert result["kernel_ready_for_test"] is False


# ---------------------------------------------------------------------------
# 3. Integration-style: OpenCodeBackend + kernel_expert end-to-end
# ---------------------------------------------------------------------------

@pytest.fixture
def integration_env(monkeypatch, tmp_path):
    """Set up a realistic E2E environment:
    - session_dir as get_output_dir
    - reproducer with test.sh and crash_uaf.ko
    - kernel_contract.json with valid contract
    - mock subprocess.run to return tool-use-only JSONL (no text events)
    - patch kernel_expert paths
    - create real bzImage and vmlinux files for path validation
    """
    session_dir = tmp_path / "session"
    session_dir.mkdir()
    (session_dir / "bzImage").write_text("fake bzImage")
    (session_dir / "vmlinux").write_text("fake vmlinux")

    reproducer = session_dir / "reproducer"
    reproducer.mkdir()
    (reproducer / "test.sh").write_text(
        "#!/bin/sh\necho test\n# REPRODUCER_SIGNAL: BUG: KASAN: slab-use-after-free"
    )
    (reproducer / "crash_uaf.ko").write_text("fake ko")

    contract = {
        "status": "ok",
        "target_arch": "x86_64",
        "vmlinux_path": str(session_dir / "vmlinux"),
        "boot_kernel_path": str(session_dir / "bzImage"),
        "reproducer_dir": str(reproducer),
        "reproducer_module_path": str(reproducer / "crash_uaf.ko"),
        "test_script_path": str(reproducer / "test.sh"),
        "expected_signal": "BUG: KASAN: slab-use-after-free",
        "build_status": "passed",
        "blocked_reason": "",
    }
    (session_dir / "kernel_contract.json").write_text(
        json.dumps(contract, indent=2), encoding="utf-8"
    )

    monkeypatch.setattr(
        "agents.kernel_expert.paths_get_output_dir",
        lambda: session_dir,
    )

    # Mock opencode to return tool-use-only JSONL
    run_stdout = (
        '{"type":"step_start","part":{"id":"p1","sessionID":"ses_integration","type":"step-start"}}\n'
        '{"type":"tool_use","part":{"tool":"write","id":"c1","input":"create file"}}\n'
        '{"type":"tool_result","part":{"content":"done","tool_use_id":"c1"}}\n'
        '{"type":"step_finish","part":{"type":"step-finish"}}\n'
    )
    monkeypatch.setattr(
        "agents.backends.subprocess.run",
        lambda cmd, **kw: _make_result(stdout=run_stdout, returncode=0),
    )
    return session_dir


def test_integration_opencode_empty_text_disk_contract(monkeypatch, integration_env):
    """Full chain: OpenCodeBackend returns empty AIMessage → kernel_expert
    fallback picks up contract from disk → ready_for_test=True."""
    from agents.kernel_expert import kernel_expert_node

    state = {
        "session_dir": str(integration_env),
        "config": {
            "default": {
                "backend": "opencode",
                "api_key": "",
                "base_url": "",
                "model_name": "deepseek-v4-flash",
            },
            "agents": {
                "kernel_expert": {
                    "backend": "opencode",
                    "cli_command": "opencode",
                    "model": "deepseek/deepseek-v4-flash",
                    "cli_timeout": 3600,
                }
            },
            "workflow": {},
        },
        "user_input": "test",
        "vmlinux_path": str(integration_env / "vmlinux"),
        "boot_kernel_path": str(integration_env / "bzImage"),
        "kernel_source_path": "/home/user/kernel",
        "tool_experts": [],
        "artifacts": {
            "pm_issue_url": "",
            "vmcore_path": "vmcore",
            "vmlinux_path": str(integration_env / "vmlinux"),
        },
        "crash_tools_path": "crash",
    }

    result = kernel_expert_node(state)
    contract = result["kernel_contract"]

    assert contract["status"] == "ok"
    assert contract["expected_signal"] == "BUG: KASAN: slab-use-after-free"
    assert contract["build_status"] == "passed"
    assert result["kernel_ready_for_test"] is True
