"""Tests for Claude settings priority and quota failover."""

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
