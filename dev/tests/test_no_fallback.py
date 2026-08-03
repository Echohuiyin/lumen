"""Strict workflow tests: model failures must not become synthetic success."""

from pathlib import Path

import pytest

from agents.error_handling import classify_error


def test_provider_balance_failure_is_classified_without_retry():
    error = classify_error(
        "Anthropic backend error 402: Insufficient Balance",
        operation="knowledge_base LLM summary",
    )
    assert error.category == "PERMANENT"
    assert error.code == "QUOTA_EXHAUSTED"
    assert error.retryable is False


def test_knowledge_base_llm_failure_is_terminal(monkeypatch, tmp_path: Path):
    import agents.knowledge_base as knowledge_base

    monkeypatch.setattr(
        knowledge_base,
        "get_llm_with_config",
        lambda *args, **kwargs: object(),
    )

    def fail_summary(*args, **kwargs):
        raise RuntimeError("simulated provider outage")

    monkeypatch.setattr(knowledge_base, "call_llm_with_display", fail_summary)

    state = {
        "config": {
            "agents": {
                "knowledge_base": {"prompt_file": "prompts/knowledge_base.md"},
            },
        },
        "user_input": "maintenance case",
        "session_dir": str(tmp_path),
    }

    result = knowledge_base.knowledge_base_node(state)
    assert result["knowledge_base_contract"]["status"] == "blocked"
    assert result["knowledge_base_contract"]["code"] == "BLOCKED_KNOWLEDGE_BASE_LLM"
    assert "simulated provider outage" in result["knowledge_base_contract"]["error"]["cause"]
    assert result["knowledge_file"] == ""
    assert "伪造摘要" in result["final_response"]
    assert not list(tmp_path.glob("*.md"))
