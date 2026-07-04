"""Session management for workflow agent outputs.

Each workflow invocation creates a session directory under ``sessions/<session_id>/``,
where every agent's full input/output is saved to a separate markdown file.

A ``session.json`` metadata file tracks agent execution order and timing.
"""

import json
from datetime import datetime
from pathlib import Path
from typing import Any


def create_session_dir(session_id: str) -> Path:
    """Create and return a session directory, writing initial metadata."""
    session_dir = Path("sessions") / session_id
    session_dir.mkdir(parents=True, exist_ok=True)

    metadata: dict[str, Any] = {
        "session_id": session_id,
        "created_at": datetime.now().isoformat(),
        "agents": [],
    }
    _write_metadata(session_dir, metadata)
    return session_dir


def save_agent_file(
    session_dir: str | Path,
    step: int,
    agent_label: str,
    phase: str,
    messages: list,
    response: str,
    elapsed: float,
    model_name: str = "",
    usage: dict | None = None,
    reasoning: str = "",
) -> str:
    """Save an agent's full input/output to a file and update session metadata.

    Returns the absolute path of the written file.
    """
    session_dir = Path(session_dir)
    safe_label = _slugify(agent_label)
    safe_phase = _slugify(phase)
    filename = f"{step:03d}_{safe_label}_{safe_phase}.md"
    filepath = session_dir / filename

    # Build markdown content -------------------------------------------------
    parts: list[str] = [
        f"# {agent_label} — {phase}",
        "",
        f"**用时**: {elapsed:.1f} 秒",
        "",
    ]
    if model_name:
        parts += [f"**模型**: {model_name}", ""]
    if usage:
        u = usage
        prompt = u.get("prompt_tokens", 0)
        completion = u.get("completion_tokens", 0)
        total = u.get("total_tokens", 0)
        parts += [f"**Token**: {prompt}+{completion}={total}" if prompt and completion
                  else f"**Token**: {total}" if total else "", ""]

    parts += [
        "## 输入 Messages",
        "",
    ]
    for msg in messages:
        parts.append(f"### {type(msg).__name__}")
        parts.append("")
        parts.append(str(msg.content) if msg.content else "")
        parts.append("")

    if reasoning:
        parts.append("## 推理过程")
        parts.append("")
        parts.append(reasoning)
        parts.append("")

    parts.append("## 输出 Response")
    parts.append("")
    parts.append(response)

    filepath.write_text("\n".join(parts), encoding="utf-8")

    # Update session metadata ------------------------------------------------
    meta = _read_metadata(session_dir)
    meta["agents"].append({
        "step": step,
        "agent": agent_label,
        "phase": phase,
        "file": filename,
        "model": model_name,
        "usage": usage,
        "elapsed": round(elapsed, 1),
    })
    _write_metadata(session_dir, meta)

    return str(filepath)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _slugify(text: str) -> str:
    """Convert free-form text to a safe filename segment."""
    result = text.replace(" ", "_").replace("/", "_")
    return "".join(c for c in result if c.isalnum() or c in "_-")


def _read_metadata(session_dir: Path) -> dict[str, Any]:
    meta_path = session_dir / "session.json"
    if meta_path.exists():
        return json.loads(meta_path.read_text(encoding="utf-8"))
    return {"session_id": session_dir.name, "agents": []}


def _write_metadata(session_dir: Path, metadata: dict[str, Any]) -> None:
    (session_dir / "session.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
