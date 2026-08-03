"""Display tests for structured tool-expert statuses."""

from agents.llm_display import display_expert_outputs


def test_display_uses_structured_blocked_status(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr("agents.llm_display.get_expert_output_file", lambda _name: tmp_path / "expert.txt")
    display_expert_outputs([
        {
            "expert_type": "crash_analysis",
            "expert_name": "crash_analysis",
            "analysis_output": "provider quota unavailable",
            "structured_output": {"status": "blocked"},
        },
    ])
    output = capsys.readouterr().out
    assert "⚠" in output
    assert "✓" not in output
