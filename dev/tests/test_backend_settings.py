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
            {"name": "one", "settings_file": str(paths[0]), "model": "sonnet"},
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
        (str(paths[0]), ""),
        (str(paths[1]), "opus"),
        (str(paths[2]), "haiku"),
        (str(paths[0]), ""),
    ]
