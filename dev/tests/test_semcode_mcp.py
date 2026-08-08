"""Semcode MCP tool availability and config tests.

Verifies that:
1. config.json has valid semcode_mcp config
2. The semcode-mcp binary exists at the configured path
3. .semcode.db path is derived from kernel_source in input text
4. Codex receives a required project-scoped Semcode MCP config
5. kernel_source_path from input.txt supplies the semcode db path
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

import pytest

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.backends import CodexBackend
from agents.input_artifacts import parse_input_artifacts
from llm_config import load_config


def _get_semcode_config() -> dict | None:
    """Load semcode_mcp from config.json."""
    names = {
        "ANTHROPIC_API_KEY": "static-contract-test-placeholder",
        "ANTHROPIC_BASE_URL": "https://static.invalid",
        "ANTHROPIC_MODEL": "static-contract-test-model",
    }
    previous = {name: os.environ.get(name) for name in names}
    for name, value in names.items():
        os.environ.setdefault(name, value)
    try:
        cfg = load_config("config.json")
    finally:
        for name, value in previous.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
    return cfg.get("agents", {}).get("kernel_expert", {}).get("semcode_mcp")


def test_semcode_mcp_config_present():
    """semcode_mcp section must exist in config.json."""
    sm = _get_semcode_config()
    assert sm is not None, "semcode_mcp not found in config.json"


def test_semcode_mcp_has_command():
    """semcode_mcp must have a 'command' field."""
    sm = _get_semcode_config()
    assert sm.get("command"), "semcode_mcp.command is empty"


def test_semcode_mcp_has_no_default_db_arg():
    """semcode_mcp should not hard-code a default db path."""
    sm = _get_semcode_config()
    args = sm.get("args", [])
    assert "-d" not in args, "semcode_mcp db path must come from input.txt kernel_source"


def test_semcode_mcp_binary_exists():
    """The semcode-mcp binary must exist at the configured path."""
    sm = _get_semcode_config()
    cmd = os.path.expanduser(sm["command"])
    assert os.path.exists(cmd), f"semcode-mcp binary not found: {cmd}"
    assert os.access(cmd, os.X_OK), f"semcode-mcp binary not executable: {cmd}"


def test_semcode_db_comes_from_input_kernel_source(tmp_path):
    """The semcode db path must be derived from input.txt kernel_source."""
    kernel_source = tmp_path / "linux"
    kernel_source.mkdir()
    semcode_db = kernel_source / ".semcode.db"
    semcode_db.mkdir()

    text = f"Bug Promote: kernel panic\nkernel_source: {kernel_source}\n"
    artifacts = parse_input_artifacts(text, validate_paths=False)

    assert artifacts.kernel_source_path == str(kernel_source)
    assert Path(artifacts.kernel_source_path, ".semcode.db").exists()


def test_input_artifacts_parses_kernel_source():
    """parse_input_artifacts must extract kernel_source 文件: from input.txt."""
    text = (
        "vmcore 文件: /tmp/test/vmcore.elf\n"
        "vmlinux 文件: /tmp/test/vmlinux\n"
        "kernel_source 文件: /tmp/test/linux-next\n"
    )
    artifacts = parse_input_artifacts(text, validate_paths=False)
    assert artifacts.kernel_source_path == "/tmp/test/linux-next", (
        f"Expected kernel_source_path='/tmp/test/linux-next', got '{artifacts.kernel_source_path}'"
    )


def test_input_artifacts_kernel_source_absent_by_default():
    """kernel_source_path should be empty when input has no kernel_source line."""
    artifacts = parse_input_artifacts("vmcore 文件: /tmp/vmcore.elf\n", validate_paths=False)
    assert artifacts.kernel_source_path == "", (
        f"Expected empty kernel_source_path, got '{artifacts.kernel_source_path}'"
    )


def test_codex_semcode_mcp_command_overrides(tmp_path):
    """Codex CLI config must require the declared Semcode MCP server."""
    fake_bin = tmp_path / "semcode-mcp"
    fake_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    fake_bin.chmod(0o755)

    backend = CodexBackend(
        cli_command="codex",
        semcode_mcp={
            "command": str(fake_bin),
            "args": ["-d", str(tmp_path / ".semcode.db")],
        },
    )
    cmd = backend._build_command(workdir=tmp_path, project_root=tmp_path)
    assert "mcp_servers.semcode.required=true" in cmd
    assert f'mcp_servers.semcode.command={json.dumps(str(fake_bin))}' in cmd
    assert any(item.startswith("mcp_servers.semcode.args=") and "-d" in item for item in cmd)


def test_codex_semcode_mcp_missing_command_is_blocking(tmp_path):
    backend = CodexBackend(semcode_mcp={})
    with pytest.raises(RuntimeError, match="Semcode MCP command is required"):
        backend._build_command(workdir=tmp_path, project_root=tmp_path)


def test_codex_semcode_mcp_missing_binary_is_blocking(tmp_path):
    backend = CodexBackend(
        semcode_mcp={"command": "/nonexistent/semcode-mcp"},
    )
    with pytest.raises(RuntimeError, match="Semcode MCP binary is missing"):
        backend._build_command(workdir=tmp_path, project_root=tmp_path)


def test_kernel_source_override_changes_db_path():
    """Simulate the kernel_expert_node override logic for linux-next.

    Uses a temp dir to fake a kernel_source_path with a .semcode.db inside,
    so the test runs on any machine without relying on real user paths.
    """
    import tempfile
    with tempfile.TemporaryDirectory() as tmp_kernel_source:
        candidate_db = os.path.join(tmp_kernel_source, ".semcode.db")
        Path(candidate_db).touch()

        agent_config = {
            "semcode_mcp": {
                "command": "/tmp/test/semcode-mcp",
                "args": ["-d", "/tmp/test/.semcode.db"],
            }
        }
        assert os.path.exists(candidate_db), (
            f"semcode db not found at {candidate_db}"
        )

        # Apply the same logic as kernel_expert_node
        if tmp_kernel_source and "semcode_mcp" in agent_config:
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


def test_input_artifacts_validates_kernel_source(tmp_path):
    """parse_input_artifacts with validate_paths=True must detect a Linux source tree.

    Creates a fake linux source tree by touching Makefile and include/linux/
    so the test runs anywhere without depending on a real user path.
    """
    fake_source = tmp_path / "fake-linux"
    fake_source.mkdir()
    (fake_source / "Makefile").write_text("# fake", encoding="utf-8")
    (fake_source / "Kconfig").write_text("# fake", encoding="utf-8")
    inc = fake_source / "include" / "linux"
    inc.mkdir(parents=True)
    (inc / "kernel.h").write_text("// fake", encoding="utf-8")
    init = fake_source / "init"
    init.mkdir()
    (init / "main.c").write_text("// fake", encoding="utf-8")

    fake_vmcore = tmp_path / "vmcore.elf"
    fake_vmcore.write_bytes(b"\x7fELF fake")
    fake_vmlinux = tmp_path / "vmlinux"
    fake_vmlinux.write_bytes(b"\x7fELF fake")

    text = (
        f"vmcore 文件: {fake_vmcore}\n"
        f"vmlinux 文件: {fake_vmlinux}\n"
        f"kernel_source 文件: {fake_source}\n"
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
    def _run_direct(test):
        if test.__name__ in {
            "test_semcode_db_comes_from_input_kernel_source",
            "test_write_semcode_mcp_config_format",
            "test_input_artifacts_validates_kernel_source",
        }:
            with tempfile.TemporaryDirectory() as tmp:
                return test(Path(tmp))
        return test()

    for test in [
        test_semcode_mcp_config_present,
        test_semcode_mcp_has_command,
        test_semcode_mcp_has_no_default_db_arg,
        test_semcode_mcp_binary_exists,
        test_semcode_db_comes_from_input_kernel_source,
        test_input_artifacts_parses_kernel_source,
        test_input_artifacts_kernel_source_absent_by_default,
        test_kernel_source_override_changes_db_path,
        test_input_artifacts_validates_kernel_source,
    ]:
        print(f"  {test.__name__}...", end=" ", flush=True)
        _run_direct(test)
        print("OK")
    print("semcode_mcp OK")
