"""Offline unit tests for Chroma import (_import_to_chroma).

Covers success, failure, relative-path resolution (regression), and
graceful degradation when skill/venv are missing.
"""

import os
import sys
from pathlib import Path

project_root = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(project_root))
sys.path.insert(0, str(project_root / "Analysis-SKILL" / "src"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
from collections.abc import Generator
from contextlib import contextmanager
from unittest.mock import patch

import subprocess as _real_subprocess


@contextmanager
def _mock_chroma_deps(
    tmp_path,
    *,
    subprocess_returncode: int = 0,
    subprocess_stderr: str = "",
    custom_run=None,
    disable_skill: bool = False,
    disable_venv: bool = False,
) -> Generator:
    """Mock _import_to_chroma's external deps so tests run offline.

    Creates a fake skill/venv tree under ``tmp_path``, then patches
    ``resolve_best_skill_path`` and ``ANALYSIS_SKILL_PATH`` to point at it.
    """
    # Fake skill directory layout
    fake_skill = tmp_path / "skills" / "rag-case-retrieval"
    fake_scripts = fake_skill / "scripts"
    fake_scripts.mkdir(parents=True, exist_ok=True)
    fake_import_script = fake_scripts / "import_cases.py"
    fake_import_script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")

    fake_venv_dir = tmp_path / "Analysis-SKILL" / ".venv"
    if not disable_venv:
        fake_venv_bin = fake_venv_dir / "bin"
        fake_venv_bin.mkdir(parents=True, exist_ok=True)
        fake_python = fake_venv_bin / "python"
        fake_python.write_text("#!/bin/sh\nexec python3\n", encoding="utf-8")
        fake_python.chmod(0o755)
    else:
        fake_venv_dir.mkdir(parents=True, exist_ok=True)

    def _fake_skill_path(*args, **kwargs):
        if disable_skill:
            return None
        return fake_skill

    # Decide run implementation
    if custom_run is not None:
        run_impl = custom_run
    else:
        def _run_impl(*args, **kwargs):
            argv = args[0] if args else kwargs.get("args", [])
            return _real_subprocess.CompletedProcess(
                args=argv,
                returncode=subprocess_returncode,
                stdout="ok",
                stderr=subprocess_stderr,
            )
        run_impl = _run_impl

    patches = [
        patch("agents.knowledge_base.resolve_best_skill_path", _fake_skill_path),
        patch("agents.knowledge_base.ANALYSIS_SKILL_PATH", tmp_path / "Analysis-SKILL"),
        patch("subprocess.run", run_impl),
    ]

    for p in patches:
        p.start()
    try:
        yield
    finally:
        for p in patches:
            p.stop()


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_import_to_chroma_success(tmp_path):
    """_import_to_chroma: subprocess returns 0 → success."""
    from agents.knowledge_base import _import_to_chroma

    kb_file = tmp_path / "knowledge_test.md"
    kb_file.write_text("# Test knowledge content", encoding="utf-8")

    with _mock_chroma_deps(tmp_path, subprocess_returncode=0):
        success, msg = _import_to_chroma(str(kb_file))

    assert success, f"Should succeed, got: {msg}"
    assert "成功导入" in msg
    # temp json should be cleaned up
    assert not (tmp_path / "knowledge_test.json").exists()


def test_import_to_chroma_failure(tmp_path):
    """_import_to_chroma: subprocess returns non-zero → failure with error message."""
    from agents.knowledge_base import _import_to_chroma

    kb_file = tmp_path / "knowledge_test.md"
    kb_file.write_text("# Test knowledge content", encoding="utf-8")

    with _mock_chroma_deps(
        tmp_path,
        subprocess_returncode=1,
        subprocess_stderr="fake chroma error",
    ):
        success, msg = _import_to_chroma(str(kb_file))

    assert not success
    assert "导入失败" in msg
    assert "fake chroma error" in msg
    assert not (tmp_path / "knowledge_test.json").exists()


def test_import_to_chroma_relative_path(tmp_path):
    """_import_to_chroma: relative knowledge_file → resolved to absolute for subprocess.

    Regression test: the subprocess runs with a different cwd, so the temp
    JSON path must be absolute.
    """
    from agents.knowledge_base import _import_to_chroma

    kb_file = tmp_path / "knowledge_rel.md"
    kb_file.write_text("# Relative path test", encoding="utf-8")

    # Track the --file argument passed to subprocess
    captured_path = []

    def _capturing_run(*args, **kwargs):
        argv = args[0] if args else kwargs.get("args", [])
        for a, b in zip(argv, argv[1:]):
            if a == "--file":
                captured_path.append(b)
                break
        return _real_subprocess.CompletedProcess(
            args=argv, returncode=0, stdout="ok", stderr=""
        )

    # cd into tmp_path so that "knowledge_rel.md" is a valid relative path
    old_cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        with _mock_chroma_deps(tmp_path, custom_run=_capturing_run):
            success, msg = _import_to_chroma("knowledge_rel.md")
    finally:
        os.chdir(old_cwd)

    assert success, f"Should succeed with relative path, got: {msg}"
    assert captured_path, "subprocess should have been called with --file"
    assert os.path.isabs(captured_path[0]), (
        f"temp JSON path must be absolute, got: {captured_path[0]}"
    )
    assert captured_path[0].endswith(".json")
    assert not (tmp_path / "knowledge_rel.json").exists()


def test_import_to_chroma_missing_skill(tmp_path):
    """_import_to_chroma: rag-case-retrieval skill not found → graceful degradation."""
    from agents.knowledge_base import _import_to_chroma

    kb_file = tmp_path / "knowledge_test.md"
    kb_file.write_text("# content", encoding="utf-8")

    with _mock_chroma_deps(tmp_path, disable_skill=True):
        success, msg = _import_to_chroma(str(kb_file))

    assert not success
    assert "未找到" in msg or "skill" in msg.lower()


def test_import_to_chroma_missing_venv(tmp_path):
    """_import_to_chroma: venv python missing → graceful degradation."""
    from agents.knowledge_base import _import_to_chroma

    kb_file = tmp_path / "knowledge_test.md"
    kb_file.write_text("# content", encoding="utf-8")

    with _mock_chroma_deps(tmp_path, disable_venv=True):
        success, msg = _import_to_chroma(str(kb_file))

    assert not success
    assert "venv" in msg.lower() or "未找到" in msg
