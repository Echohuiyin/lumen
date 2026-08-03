"""Session artifact placement contracts."""

from pathlib import Path

from agents.session import create_session_dir


def test_session_root_can_be_relocated(monkeypatch, tmp_path: Path):
    root = tmp_path / "durable-sessions"
    monkeypatch.setenv("LUMEN_SESSION_ROOT", str(root))

    session_dir = create_session_dir("case-01")

    assert session_dir == root / "case-01"
    assert (session_dir / "session.json").is_file()


def test_default_expert_output_can_be_relocated(monkeypatch, tmp_path: Path):
    output_root = tmp_path / "expert-output"
    monkeypatch.setenv("LUMEN_OUTPUT_DIR", str(output_root))

    import agents.llm_display as display

    previous_session_dir = display._session_dir
    display.set_session_dir(None)
    try:
        assert display.get_expert_output_file("test_expert") == output_root / "test_expert.txt"
    finally:
        display.set_session_dir(previous_session_dir)
