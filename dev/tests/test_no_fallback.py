"""Strict workflow tests: model failures must not become synthetic success."""

from pathlib import Path

import pytest


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

    with pytest.raises(RuntimeError, match="knowledge_base LLM summary failed"):
        knowledge_base.knowledge_base_node(state)
    assert not list(tmp_path.glob("*.md"))
