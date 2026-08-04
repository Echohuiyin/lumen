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



def test_empty_knowledge_summary_keeps_kernel_evidence_report(monkeypatch, tmp_path: Path):
    """A missed QEMU oracle still archives the real Kernel Expert diagnosis."""
    import agents.knowledge_base as knowledge_base

    monkeypatch.setattr(knowledge_base, "get_llm_with_config", lambda *args, **kwargs: object())

    class EmptySummary:
        content = ""

    monkeypatch.setattr(
        knowledge_base, "call_llm_with_display", lambda *args, **kwargs: EmptySummary(),
    )
    monkeypatch.setattr(
        knowledge_base, "_import_to_chroma", lambda path: (True, "imported"),
    )
    state = {
        "user_input": "maintenance case",
        "config": {"agents": {"knowledge_base": {"prompt_file": "prompts/knowledge_base.md"}}},
        "session_dir": str(tmp_path),
        "issue_id": "ISSUE-evidence",
        "issue_url": "https://example.invalid/ISSUE-evidence",
        "kernel_analysis": "Verified warning at target_fn(): the object is released before the callback.",
        "kernel_contract": {
            "status": "ok",
            "root_cause": "callback observes an object after its final put",
            "root_cause_evidence": [{"function": "target_fn", "file": "kernel/target.c", "line": 42}],
            "original_call_chain": ["target_fn", "callback"],
            "call_chain_oracle": {"fault_signatures": ["WARNING in target_fn"]},
        },
        "test_passed": False,
        "test_rounds": [{"round": 10, "status": "failed", "summary": "timer abort; target frame absent"}],
        "test_contract": {"missing_frames": ["target_fn"]},
    }
    result = knowledge_base.knowledge_base_node(state)
    content = Path(result["knowledge_file"]).read_text(encoding="utf-8")
    assert "Kernel root-cause analysis (evidence archive)" in content
    assert "callback observes an object after its final put" in content
    assert "Verified warning at target_fn()" in content
    assert "not reproduced" in content
    assert "timer abort; target frame absent" in content
    assert "Root-cause report:" in result["final_response"]
    assert "callback observes an object after its final put" in result["final_response"]
