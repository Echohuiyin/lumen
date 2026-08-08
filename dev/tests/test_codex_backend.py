"""Contract tests for the project-isolated Codex Kernel Expert backend."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

from agents.backends import CodexBackend, _codex_artifact_signature


def _fake_process(*, returncode=0, stdout="", stderr=""):
    class FakeProcess:
        pid = 4242

        def __init__(self):
            self.returncode = returncode

        def communicate(self, **kwargs):
            return stdout, stderr

        def kill(self):
            self.returncode = -9

    return FakeProcess()


def _fixture(tmp_path: Path) -> tuple[CodexBackend, Path, Path, Path]:
    project = tmp_path / "lumen"
    workdir = project / "sessions" / "case" / "outputs"
    workdir.mkdir(parents=True)
    skill = project / ".agents" / "skills" / "kernel-analysis"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: kernel-analysis\n---\n", encoding="utf-8")
    runtime_home = project / "runtime" / "codex-home"
    codex_home = runtime_home / ".codex"
    codex_home.mkdir(parents=True)
    auth = codex_home / "auth.json"
    auth.write_text("{}", encoding="utf-8")
    auth.chmod(0o600)
    semcode = project / "bin" / "semcode-mcp"
    semcode.parent.mkdir()
    semcode.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    semcode.chmod(0o755)
    backend = CodexBackend(
        cli_command="codex",
        cli_timeout=30,
        reasoning_effort="high",
        service_tier="priority",
        runtime_home=str(runtime_home),
        project_root=str(project),
        project_skills_dir=str(project / ".agents" / "skills"),
        semcode_mcp={"command": str(semcode), "args": ["-d", str(project / ".semcode.db")]},
    )
    return backend, project, workdir, runtime_home


def test_codex_backend_isolates_home_skills_and_mcp(tmp_path, monkeypatch):
    backend, project, workdir, runtime_home = _fixture(tmp_path)
    source = tmp_path / "linux-source"
    source.mkdir()
    captured = {}

    event = {
        "type": "item.completed",
        "item": {"type": "agent_message", "text": "KERNEL_CONTRACT\n{}"},
    }

    def fake_popen(cmd, **kwargs):
        captured.update(cmd=cmd, kwargs=kwargs)
        process = _fake_process(stdout=json.dumps(event) + "\n")
        original_communicate = process.communicate

        def communicate(**communicate_kwargs):
            captured["communicate_kwargs"] = communicate_kwargs
            return original_communicate(**communicate_kwargs)

        process.communicate = communicate
        return process

    monkeypatch.setattr("agents.backends.subprocess.Popen", fake_popen)
    response = backend.invoke(
        [SystemMessage(content="system contract"), HumanMessage(content="case input")],
        workdir=str(workdir),
        add_dirs=[str(source)],
    )

    assert response.content == "KERNEL_CONTRACT\n{}"
    cmd = captured["cmd"]
    assert cmd[:4] == ["codex", "--ask-for-approval", "never", "exec"]
    assert "--json" in cmd
    assert "--ephemeral" in cmd
    assert "--ignore-user-config" in cmd
    assert "--ignore-rules" in cmd
    assert cmd[cmd.index("--sandbox") + 1] == "workspace-write"
    assert 'service_tier="priority"' in cmd
    add_dir_index = cmd.index("--add-dir")
    assert cmd[add_dir_index + 1] == str(source.resolve())
    assert "mcp_servers.semcode.required=true" in cmd
    assert captured["kwargs"]["env"]["HOME"] == str(runtime_home)
    assert captured["kwargs"]["env"]["CODEX_HOME"] == str(runtime_home / ".codex")
    assert "Use only repository skills" in captured["communicate_kwargs"]["input"]
    assert str(project) in next(item for item in cmd if item.startswith("mcp_servers.semcode.cwd="))
    skills_link = workdir / ".agents"
    assert skills_link.is_dir()
    assert not skills_link.is_symlink()
    assert (skills_link / "skills" / "kernel-analysis" / "SKILL.md").read_text(
        encoding="utf-8"
    ) == (project / ".agents" / "skills" / "kernel-analysis" / "SKILL.md").read_text(
        encoding="utf-8"
    )


def test_codex_backend_default_timeout_matches_workflow_bound():
    assert CodexBackend()._cli_timeout == 600


def test_codex_artifact_signature_accepts_only_stable_userspace_handoff(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    source = workdir / "diag.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    (workdir / "KERNEL_CONTRACT.json").write_text(json.dumps({
        "status": "ok",
        "root_cause": "source-backed maintenance diagnosis",
        "call_chain_oracle": {"required_top_frames": ["fault"]},
        "reproducer": {
            "language": "c",
            "artifact_type": "userspace",
            "source_files": ["diag.c"],
            "entry_source": "diag.c",
        },
    }), encoding="utf-8")

    signature = _codex_artifact_signature(workdir)
    assert signature is not None
    assert any(item[0] == "KERNEL_CONTRACT.json" for item in signature)


def test_codex_artifact_signature_rejects_module_or_escape(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "KERNEL_CONTRACT.json").write_text(json.dumps({
        "status": "ok",
        "root_cause": "diagnosis",
        "call_chain_oracle": {"required_frames": ["fault"]},
        "reproducer": {
            "language": "c",
            "artifact_type": "userspace",
            "source_files": ["../bad.ko"],
            "entry_source": "../bad.ko",
        },
    }), encoding="utf-8")

    assert _codex_artifact_signature(workdir) is None


def test_codex_artifact_signature_accepts_precise_blocked_contract(tmp_path):
    workdir = tmp_path / "workdir"
    workdir.mkdir()
    (workdir / "KERNEL_CONTRACT.json").write_text(json.dumps({
        "contract": "KERNEL_CONTRACT",
        "status": "blocked",
        "blocked_reason": {"precise_limitation": "public ABI cannot invalidate label"},
    }), encoding="utf-8")

    assert _codex_artifact_signature(workdir) is not None


def test_codex_backend_can_use_complete_deterministic_evidence_without_mcp(tmp_path):
    backend, project, workdir, _runtime_home = _fixture(tmp_path)
    backend._semcode_mcp = {"disabled": True}

    command = backend._build_command(workdir=workdir, project_root=project, add_dirs=[])

    assert command[-1] == "-"
    assert not any(item.startswith("mcp_servers.semcode.") for item in command)


def test_codex_jsonl_parser_uses_last_agent_message():
    stream = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "first"}}),
            json.dumps({"type": "item.completed", "item": {"type": "command_execution", "command": "true"}}),
            json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "final"}}),
        ]
    )
    assert CodexBackend._parse_jsonl(stream) == "final"


def test_codex_jsonl_parser_surfaces_turn_failure():
    with pytest.raises(RuntimeError, match="Codex reported an error"):
        CodexBackend._parse_jsonl(json.dumps({"type": "turn.failed", "error": "quota"}))


def test_codex_backend_requires_isolated_auth(tmp_path, monkeypatch):
    backend, _project, workdir, runtime_home = _fixture(tmp_path)
    (runtime_home / ".codex" / "auth.json").unlink()
    monkeypatch.delenv("CODEX_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="isolated authentication is missing"):
        backend.invoke([], workdir=str(workdir))


def test_codex_backend_accepts_invocation_scoped_api_key(tmp_path, monkeypatch):
    backend, _project, workdir, runtime_home = _fixture(tmp_path)
    (runtime_home / ".codex" / "auth.json").unlink()
    monkeypatch.setenv("CODEX_API_KEY", "test-only")
    monkeypatch.setattr(
        "agents.backends.subprocess.Popen",
        lambda cmd, **kwargs: _fake_process(
            stdout=json.dumps({"type": "item.completed", "item": {"type": "agent_message", "text": "ok"}}),
        ),
    )
    assert backend.invoke([], workdir=str(workdir)).content == "ok"


def test_codex_backend_rejects_non_project_workdir(tmp_path):
    backend, _project, _workdir, _runtime_home = _fixture(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(RuntimeError, match="workdir must stay inside project_root"):
        backend.invoke([], workdir=str(outside))


def test_codex_backend_requires_project_skills(tmp_path):
    backend, project, workdir, _runtime_home = _fixture(tmp_path)
    (project / ".agents" / "skills" / "kernel-analysis" / "SKILL.md").unlink()
    with pytest.raises(RuntimeError, match="project skills are missing"):
        backend.invoke([], workdir=str(workdir))


def test_codex_backend_requires_semcode_binary(tmp_path):
    backend, project, workdir, _runtime_home = _fixture(tmp_path)
    backend._semcode_mcp = {"command": str(project / "missing-semcode"), "args": []}
    with pytest.raises(RuntimeError, match="Semcode MCP binary is missing"):
        backend.invoke([], workdir=str(workdir))


def test_codex_backend_timeout_is_blocking(tmp_path, monkeypatch):
    backend, _project, workdir, _runtime_home = _fixture(tmp_path)

    killed = []

    class TimeoutProcess:
        pid = 9898
        returncode = -9

        def communicate(self, **kwargs):
            if kwargs.get("timeout") is not None:
                raise subprocess.TimeoutExpired(["codex"], 1)
            return "", ""

        def kill(self):
            killed.append(("process", self.pid))

    monkeypatch.setattr("agents.backends.subprocess.Popen", lambda *args, **kwargs: TimeoutProcess())
    monkeypatch.setattr("agents.backends.os.killpg", lambda pid, sig: killed.append((pid, sig)))
    with pytest.raises(RuntimeError, match="Codex timed out"):
        backend.invoke([], workdir=str(workdir))
    assert killed and killed[0][0] == 9898


def test_codex_backend_nonzero_mcp_failure_is_tagged(tmp_path, monkeypatch):
    backend, _project, workdir, _runtime_home = _fixture(tmp_path)
    monkeypatch.setattr(
        "agents.backends.subprocess.Popen",
        lambda cmd, **kwargs: _fake_process(
            returncode=1,
            stdout="",
            stderr="required MCP server semcode failed to initialize",
        ),
    )
    with pytest.raises(RuntimeError, match=r"\[cli_startup_failure\].*Codex failed"):
        backend.invoke([], workdir=str(workdir))
