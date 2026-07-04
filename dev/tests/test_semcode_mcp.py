"""Semcode MCP tool availability and config tests.

Verifies that:
1. maintenance_config.json has valid semcode_mcp config
2. The semcode-mcp binary exists at the configured path
3. .semcode.db indexes exist for all kernel source trees referenced by test cases
4. _write_semcode_mcp_config() produces valid CLI-format MCP config
5. kernel_source_path from input.txt correctly overrides the semcode db path
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.backends import ClaudeCodeBackend
from agents.input_artifacts import parse_input_artifacts
from config import load_config


def _get_semcode_config() -> dict | None:
    """Load semcode_mcp from maintenance_config.json."""
    cfg = load_config("maintenance_config.json", fallback_to_claude_settings=False)
    return cfg.get("agents", {}).get("kernel_expert", {}).get("semcode_mcp")


def test_semcode_mcp_config_present():
    """semcode_mcp section must exist in maintenance_config.json."""
    sm = _get_semcode_config()
    assert sm is not None, "semcode_mcp not found in maintenance_config.json"


def test_semcode_mcp_has_command():
    """semcode_mcp must have a 'command' field."""
    sm = _get_semcode_config()
    assert sm.get("command"), "semcode_mcp.command is empty"


def test_semcode_mcp_has_db_arg():
    """semcode_mcp args must include -d with a db path."""
    sm = _get_semcode_config()
    args = sm.get("args", [])
    assert "-d" in args, "semcode_mcp.args must contain -d"
    db_idx = args.index("-d") + 1
    assert db_idx < len(args), "no path after -d in semcode_mcp.args"
    assert args[db_idx], "db path after -d is empty"


def test_semcode_mcp_binary_exists():
    """The semcode-mcp binary must exist at the configured path."""
    sm = _get_semcode_config()
    cmd = os.path.expanduser(sm["command"])
    assert os.path.exists(cmd), f"semcode-mcp binary not found: {cmd}"
    assert os.access(cmd, os.X_OK), f"semcode-mcp binary not executable: {cmd}"


def test_default_semcode_db_exists():
    """The default .semcode.db (OLK-6.6) must exist."""
    sm = _get_semcode_config()
    args = sm.get("args", [])
    db_idx = args.index("-d") + 1
    db_path = os.path.expanduser(args[db_idx])
    assert os.path.exists(db_path), f"Default semcode db not found: {db_path}"


def test_linux_next_semcode_db_exists():
    """The linux-next .semcode.db must exist (for kvm/btrfs test cases)."""
    db_path = os.path.expanduser("~/linux-next/.semcode.db")
    assert os.path.exists(db_path), (
        f"linux-next semcode db not found: {db_path}\n"
        f"  Run: cd ~/linux-next && semcode-index -s ."
    )


def test_input_artifacts_parses_kernel_source():
    """parse_input_artifacts must extract kernel_source 文件: from input.txt."""
    text = (
        "vmcore 文件: /tmp/test/vmcore.elf\n"
        "vmlinux 文件: /tmp/test/vmlinux\n"
        "kernel_source 文件: /home/liumingrui/linux-next\n"
    )
    artifacts = parse_input_artifacts(text, validate_paths=False)
    assert artifacts.kernel_source_path == "/home/liumingrui/linux-next", (
        f"Expected kernel_source_path='~/linux-next', got '{artifacts.kernel_source_path}'"
    )


def test_input_artifacts_kernel_source_absent_by_default():
    """kernel_source_path should be empty when input has no kernel_source line."""
    artifacts = parse_input_artifacts("vmcore 文件: /tmp/vmcore.elf\n", validate_paths=False)
    assert artifacts.kernel_source_path == "", (
        f"Expected empty kernel_source_path, got '{artifacts.kernel_source_path}'"
    )


def test_write_semcode_mcp_config_format():
    """_write_semcode_mcp_config must produce valid CLI-format MCP config."""
    backend = ClaudeCodeBackend(
        cli_command="claude",
        semcode_mcp={
            "command": "/home/liumingrui/semcode/target/release/semcode-mcp",
            "args": ["-d", "/home/liumingrui/code/OLK-6.6/.semcode.db"],
        },
    )
    mcp_path = backend._write_semcode_mcp_config()
    assert mcp_path, "_write_semcode_mcp_config returned empty path"
    assert os.path.exists(mcp_path), f"Temp MCP config not found: {mcp_path}"
    try:
        parsed = json.loads(Path(mcp_path).read_text())
        assert "mcpServers" in parsed, "Missing mcpServers key"
        assert "semcode" in parsed["mcpServers"], "Missing semcode key in mcpServers"
        sc = parsed["mcpServers"]["semcode"]
        assert "command" in sc, "Missing command in semcode MCP server config"
        assert "args" in sc, "Missing args in semcode MCP server config"
        assert "-d" in sc["args"], "Missing -d in semcode MCP server args"
        assert sc["command"] == "/home/liumingrui/semcode/target/release/semcode-mcp"
    finally:
        Path(mcp_path).unlink(missing_ok=True)


def test_write_semcode_mcp_config_empty_when_no_command():
    """_write_semcode_mcp_config returns '' when semcode_mcp has no command."""
    backend = ClaudeCodeBackend(semcode_mcp={})
    assert backend._write_semcode_mcp_config() == ""


def test_write_semcode_mcp_config_empty_when_binary_missing():
    """_write_semcode_mcp_config returns '' when binary doesn't exist."""
    backend = ClaudeCodeBackend(
        semcode_mcp={"command": "/nonexistent/semcode-mcp"},
    )
    assert backend._write_semcode_mcp_config() == ""


def test_kernel_source_override_changes_db_path():
    """Simulate the kernel_expert_node override logic for linux-next."""
    agent_config = {
        "semcode_mcp": {
            "command": "/home/liumingrui/semcode/target/release/semcode-mcp",
            "args": ["-d", "/home/liumingrui/code/OLK-6.6/.semcode.db"],
        }
    }
    kernel_source_path = "/home/liumingrui/linux-next"
    candidate_db = os.path.join(kernel_source_path, ".semcode.db")
    assert os.path.exists(candidate_db), (
        f"linux-next semcode db not found at {candidate_db}"
    )

    # Apply the same logic as kernel_expert_node
    if kernel_source_path and "semcode_mcp" in agent_config:
        if os.path.exists(candidate_db):
            agent_config = {**agent_config}
            agent_config["semcode_mcp"] = {
                **agent_config["semcode_mcp"],
                "args": ["-d", candidate_db],
            }

    args = agent_config["semcode_mcp"]["args"]
    assert "-d" in args
    assert args[args.index("-d") + 1] == candidate_db, (
        f"Expected db path '{candidate_db}', got '{args[args.index('-d') + 1]}'"
    )


def test_input_artifacts_validates_kernel_source():
    """parse_input_artifacts with validate_paths=True must detect linux-next source tree."""
    text = (
        "vmcore 文件: /home/liumingrui/lumen/test_assets/deadlock/vmcore.elf\n"
        "vmlinux 文件: /home/liumingrui/lumen/test_assets/deadlock/vmlinux\n"
        "kernel_source 文件: /home/liumingrui/linux-next\n"
    )
    artifacts = parse_input_artifacts(text, validate_paths=True)
    # linux-next is a valid Linux source tree, no errors expected
    assert artifacts.status != "degraded" or not artifacts.errors
    # Should have a check record for kernel_source_path
    checks = [
        e for e in artifacts.evidence
        if e.get("field") == "kernel_source_path"
        and e.get("kind") == "input_artifact_check"
    ]
    assert checks, "No input_artifact_check for kernel_source_path"
    assert checks[0].get("exists") is True, "kernel_source_path should exist"
    assert checks[0].get("is_linux_source_tree") is True, (
        "linux-next not recognized as a Linux source tree"
    )


if __name__ == "__main__":
    for test in [
        test_semcode_mcp_config_present,
        test_semcode_mcp_has_command,
        test_semcode_mcp_has_db_arg,
        test_semcode_mcp_binary_exists,
        test_default_semcode_db_exists,
        test_linux_next_semcode_db_exists,
        test_input_artifacts_parses_kernel_source,
        test_input_artifacts_kernel_source_absent_by_default,
        test_write_semcode_mcp_config_format,
        test_write_semcode_mcp_config_empty_when_no_command,
        test_write_semcode_mcp_config_empty_when_binary_missing,
        test_kernel_source_override_changes_db_path,
        test_input_artifacts_validates_kernel_source,
    ]:
        print(f"  {test.__name__}...", end=" ", flush=True)
        test()
        print("OK")
    print("semcode_mcp OK")
