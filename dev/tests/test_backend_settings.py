"""Tests for Claude settings priority and quota failover."""

import pytest
from langchain_core.messages import AIMessage

from agents.backends import ClaudeCodeBackend


def test_settings_candidates_keep_glm_first(tmp_path, monkeypatch):
    glm = tmp_path / "settings.json_GLM"
    fallback = tmp_path / "settings.json"
    glm.write_text("{}", encoding="utf-8")
    fallback.write_text("{}", encoding="utf-8")

    backend = ClaudeCodeBackend(settings_file=f"{glm},{fallback}")

    assert backend._settings_candidates() == [str(glm), str(fallback)]


def test_invoke_fails_over_only_on_quota_error(tmp_path):
    glm = tmp_path / "settings.json_GLM"
    fallback = tmp_path / "settings.json"
    glm.write_text("{}", encoding="utf-8")
    fallback.write_text("{}", encoding="utf-8")
    backend = ClaudeCodeBackend(settings_file=f"{glm},{fallback}")
    calls = []

    def fake_invoke_once(messages, *, workdir="", add_dirs=None, settings_path=""):
        calls.append(settings_path)
        if settings_path == str(glm):
            raise RuntimeError("provider returned HTTP 429: quota exhausted")
        return AIMessage(content="fallback result")

    backend._invoke_once = fake_invoke_once
    result = backend.invoke([])

    assert result.content == "fallback result"
    assert calls == [str(glm), str(fallback)]


def test_non_quota_error_does_not_fail_over(tmp_path):
    glm = tmp_path / "settings.json_GLM"
    fallback = tmp_path / "settings.json"
    glm.write_text("{}", encoding="utf-8")
    fallback.write_text("{}", encoding="utf-8")
    backend = ClaudeCodeBackend(settings_file=f"{glm},{fallback}")
    calls = []

    def fake_invoke_once(messages, *, workdir="", add_dirs=None, settings_path=""):
        calls.append(settings_path)
        raise RuntimeError("invalid MCP configuration")

    backend._invoke_once = fake_invoke_once

    try:
        backend.invoke([])
    except RuntimeError as exc:
        assert "invalid MCP" in str(exc)
    else:
        raise AssertionError("non-quota errors must not silently switch credentials")
    assert calls == [str(glm)]


def test_model_pool_round_robin_preserves_settings_order(tmp_path):
    glm = tmp_path / "settings.json_GLM"
    fallback = tmp_path / "settings.json"
    glm.write_text("{}", encoding="utf-8")
    fallback.write_text("{}", encoding="utf-8")

    backend = ClaudeCodeBackend(
        settings_file=f"{glm},{fallback}",
        quota_preflight=False,
    )
    calls = []

    def fake_invoke_once(messages, *, workdir="", add_dirs=None, settings_path=""):
        calls.append(settings_path)
        return AIMessage(content=settings_path)

    backend._invoke_once = fake_invoke_once
    assert backend.invoke([]).content == str(glm)
    assert backend.invoke([]).content == str(fallback)
    assert calls == [str(glm), str(fallback)]


def test_quota_preflight_skips_exhausted_profile(tmp_path, monkeypatch):
    glm = tmp_path / "settings.json_GLM"
    fallback = tmp_path / "settings.json"
    glm.write_text("{}", encoding="utf-8")
    fallback.write_text("{}", encoding="utf-8")

    backend = ClaudeCodeBackend(
        settings_file=f"{glm},{fallback}",
        quota_preflight=True,
        quota_cooldown_seconds=60,
    )
    probes = []
    calls = []

    class ProbeResult:
        def __init__(self, path):
            self.returncode = 1 if path == str(glm) else 0
            self.stdout = "" if self.returncode else '{"is_error": false}'
            self.stderr = "429 quota exhausted" if self.returncode else ""

    def fake_run(cmd, **kwargs):
        path = cmd[cmd.index("--settings") + 1]
        probes.append(path)
        return ProbeResult(path)

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)

    def fake_invoke_once(messages, *, workdir="", add_dirs=None, settings_path=""):
        calls.append(settings_path)
        return AIMessage(content="ok")

    backend._invoke_once = fake_invoke_once
    assert backend.invoke([]).content == "ok"
    assert calls == [str(fallback)]
    assert probes == [str(glm), str(fallback)]


def test_explicit_model_pool_round_robin(tmp_path):
    paths = []
    for name in ("one.json", "two.json", "three.json"):
        path = tmp_path / name
        path.write_text("{}", encoding="utf-8")
        paths.append(path)

    backend = ClaudeCodeBackend(
        model_pool=[
            {"name": "one", "settings_file": str(paths[0]), "model": "test-model-a"},
            {"name": "two", "settings_file": str(paths[1]), "model": "opus"},
            {"name": "three", "settings_file": str(paths[2]), "model": "haiku"},
        ],
        quota_preflight=False,
    )
    calls = []

    def fake_invoke_once(messages, **kwargs):
        calls.append((kwargs["settings_path"], kwargs.get("model", "")))
        return AIMessage(content="ok")

    backend._invoke_once = fake_invoke_once
    for _ in range(4):
        backend.invoke([])

    assert calls == [
        (str(paths[0]), "test-model-a"),
        (str(paths[1]), "opus"),
        (str(paths[2]), "haiku"),
        (str(paths[0]), "test-model-a"),
    ]


def test_setting_sources_are_normalized():
    backend = ClaudeCodeBackend(setting_sources=["project", "project"])
    assert backend._setting_sources == "project"

    with pytest.raises(ValueError, match="unsupported source"):
        ClaudeCodeBackend(setting_sources="user,unknown")


def test_missing_explicit_settings_refuses_implicit_global(tmp_path):
    backend = ClaudeCodeBackend(
        settings_file=str(tmp_path / "missing-settings.json"),
        quota_preflight=False,
    )

    with pytest.raises(RuntimeError, match="implicit global settings"):
        backend._model_profiles()


def test_setting_sources_are_forwarded_to_cli(tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_text("{}", encoding="utf-8")
    backend = ClaudeCodeBackend(
        settings_file=str(settings),
        setting_sources="project",
        quota_preflight=False,
    )
    captured = {}

    class Result:
        returncode = 0
        stdout = '{"is_error": false, "result": "ok"}'
        stderr = ""

    def fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        return Result()

    monkeypatch.setattr("agents.backends.subprocess.run", fake_run)
    backend._invoke_once(
        [],
        workdir=str(tmp_path),
        settings_path=str(settings),
    )
    cmd = captured["cmd"]
    assert cmd[cmd.index("--settings") + 1] == str(settings)
    assert cmd[cmd.index("--setting-sources") + 1] == "project"
    assert "--model" not in cmd

def test_empty_settings_configuration_never_discovers_global(monkeypatch):
    monkeypatch.delenv("LUMEN_CLAUDE_SETTINGS_PRIORITY", raising=False)
    monkeypatch.delenv("CLAUDE_SETTINGS", raising=False)
    backend = ClaudeCodeBackend(quota_preflight=False)

    assert backend._settings_candidates() == []
    with pytest.raises(RuntimeError, match="implicit global settings"):
        backend._model_profiles()
