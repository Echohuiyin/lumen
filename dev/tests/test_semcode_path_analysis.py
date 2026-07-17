"""P2 tests for deterministic semcode UAF/refcount event graphs."""

from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

import pytest
from langchain_core.messages import HumanMessage, SystemMessage

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))

from agents.contracts import KernelExpertOutput
from agents.kernel_expert import _apply_semcode_path_analysis
from agents.semcode_path_analysis import (
    SemcodeFunction,
    analyze_uaf_paths,
    extract_semcode_entry_points,
    render_semcode_analysis_context,
)
from agents.input_artifacts import parse_input_artifacts
from llm_config import get_llm_with_config, load_config


class _FixedSemcodeClient:
    """Dependency injection, not a fallback: fixture models a parsed MCP reply."""

    def find_function(self, name: str) -> SemcodeFunction:
        assert name == "foo_ioctl"
        return SemcodeFunction(
            name=name,
            location="drivers/foo.c:42",
            direct_calls=("kref_get", "queue_work", "kref_put", "kfree", "foo_access"),
            body="""
                kref_get(&foo->ref);
                spin_lock(&foo->lock);
                if (bad)
                    goto err_put;
                queue_work(foo_wq, &foo->work);
            err_put:
                kref_put(&foo->ref, foo_release);
                kfree(foo);
                foo_access(foo);
            """,
        )


def _source_tree(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "linux"
    source.mkdir()
    (source / ".semcode.db").mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    subprocess.run(
        ["git", "-C", str(source), "-c", "user.name=test", "-c", "user.email=test@example.invalid",
         "commit", "--allow-empty", "-qm", "fixture"],
        check=True,
    )
    executable = tmp_path / "semcode-mcp"
    executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    executable.chmod(0o755)
    return source, executable


def test_semcode_event_graph_calculates_deltas_and_declares_boundaries(tmp_path):
    source, executable = _source_tree(tmp_path)
    result = analyze_uaf_paths(
        kernel_source_path=str(source), entry_points=["foo_ioctl"],
        semcode_command=str(executable), client=_FixedSemcodeClient(),
    )

    assert result.status == "ok"
    assert result.analysis is not None
    assert result.analysis.coverage.normal_paths_considered is True
    assert result.analysis.coverage.error_paths_considered is True
    assert result.analysis.coverage.async_paths_considered is True
    assert result.analysis.coverage.concurrency_paths_considered is True
    assert any(path.terminal_state == "uaf" for path in result.analysis.paths)
    for path in result.analysis.paths:
        assert path.net_delta == sum(event.ref_delta for event in path.events)
    assert any("direct callees only" in item for item in result.analysis.coverage.limitations)

    contract = _apply_semcode_path_analysis(
        KernelExpertOutput(status="blocked", build_status="skipped"), result,
    )
    assert contract.path_analysis_required is True
    assert contract.uaf_analysis is not None
    assert contract.max_likely_path in contract.all_possible_paths
    assert contract.reproduction_target_path == contract.max_likely_path


def test_semcode_requires_explicit_entry_point_and_never_falls_back(tmp_path):
    source, executable = _source_tree(tmp_path)
    result = analyze_uaf_paths(
        kernel_source_path=str(source), entry_points=[], semcode_command=str(executable),
    )
    assert result.status == "blocked"
    assert "entry point" in result.blocked_reason
    assert result.analysis is None


def test_entry_point_extraction_accepts_only_explicit_function_evidence():
    entries = extract_semcode_entry_points(
        "function: foo_ioctl\nCall Trace: bar_release+0x1a/0x40",
        "ordinary prose should not create an entry point",
    )
    assert entries == ["foo_ioctl", "bar_release"]


def test_semcode_path_analysis_online_llm_roundtrip():
    """Live gate: semcode evidence must remain intelligible to the configured LLM."""
    input_file = os.environ.get("LUMEN_P2_ONLINE_INPUT", "")
    if not input_file:
        pytest.fail("LUMEN_P2_ONLINE_INPUT must point to an input.txt with kernel_source")
    text = Path(input_file).read_text(encoding="utf-8")
    artifacts = parse_input_artifacts(text, validate_paths=True)
    config = load_config("config.json")
    semcode = config["agents"]["kernel_expert"]["semcode_mcp"]
    result = analyze_uaf_paths(
        kernel_source_path=artifacts.kernel_source_path,
        entry_points=["kref_put"],
        semcode_command=semcode["command"], semcode_args=semcode.get("args", []),
    )
    assert result.status == "ok", result.blocked_reason
    assert result.analysis is not None
    llm = get_llm_with_config(config["default"], agent_name="p2_online_contract")
    response = llm.invoke([
        SystemMessage(content="Read the supplied JSON evidence. Return only its case_id."),
        HumanMessage(content=render_semcode_analysis_context(result)),
    ])
    assert result.analysis.case_id in (response.content or "")
