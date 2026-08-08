"""Validator exact kernel source revision tests."""

import subprocess
from pathlib import Path

from agents.input_artifacts import parse_input_artifacts
from agents.source_revision import resolve_source_revision
from agents.validator import validator_node


def _git(source: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(source), *args], text=True).strip()


def _source_repo(tmp_path: Path) -> tuple[Path, str, str]:
    source = tmp_path / "linux"
    source.mkdir()
    subprocess.run(["git", "init", "-q", str(source)], check=True)
    (source / "Makefile").write_text("VERSION = 6\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "Makefile"], check=True)
    subprocess.run([
        "git", "-C", str(source), "-c", "user.name=test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "base",
    ], check=True)
    base = _git(source, "rev-parse", "HEAD")
    # Keep the index outside Git so a detached worktree can bind it without
    # treating the generated database as a checked-out source artifact.
    (source / ".semcode.db").write_text("fixture index\n", encoding="utf-8")
    (source / "later.c").write_text("int later(void) { return 0; }\n", encoding="utf-8")
    subprocess.run(["git", "-C", str(source), "add", "later.c"], check=True)
    subprocess.run([
        "git", "-C", str(source), "-c", "user.name=test",
        "-c", "user.email=test@example.invalid", "commit", "-qm", "later",
    ], check=True)
    head = _git(source, "rev-parse", "HEAD")
    return source, base, head


def test_validator_resolves_commit_alias():
    contract = parse_input_artifacts("commit: deadbeef\nkernel_source: /tmp/linux", validate_paths=False)
    assert contract.expected_kernel_commit == "deadbeef"


def test_missing_commit_is_structured_block(tmp_path: Path):
    source, _, _ = _source_repo(tmp_path)
    result = resolve_source_revision(str(source), "", workspace_root=str(tmp_path / "work"))
    assert result.status == "blocked"
    assert result.error is not None
    assert result.error.code == "KERNEL_COMMIT_REQUIRED"


def test_validator_switches_to_detached_exact_commit(tmp_path: Path):
    source, base, head = _source_repo(tmp_path)
    result = resolve_source_revision(str(source), base, workspace_root=str(tmp_path / "work"))
    assert result.status == "switched"
    assert result.switch_method == "detached_worktree"
    assert result.resolved_commit == base
    assert result.resolved_source_path != str(source.resolve())
    assert _git(Path(result.resolved_source_path), "rev-parse", "HEAD") == base
    assert head != base


def test_dirty_source_is_rejected_before_switch(tmp_path: Path):
    source, _, head = _source_repo(tmp_path)
    (source / "Makefile").write_text("dirty\n", encoding="utf-8")
    result = resolve_source_revision(str(source), head, workspace_root=str(tmp_path / "work"))
    assert result.status == "blocked"
    assert result.error is not None
    assert result.error.code == "KERNEL_SOURCE_DIRTY"


def test_validator_exposes_resolved_source_revision(tmp_path: Path):
    source, _, head = _source_repo(tmp_path)
    log = tmp_path / "kernel.log"
    log.write_text("Kernel panic - not syncing\n", encoding="utf-8")
    result = validator_node({
        "user_input": (
            f"kernel panic\nlog: {log}\nkernel_source: {source}\n"
            f"expected_kernel_commit: {head}\n"
        ),
        "config_path": "config.json",
    })
    assert result["validation_passed"] is True
    artifacts = result["input_artifacts_contract"]
    assert artifacts["source_revision"]["status"] == "resolved"
    assert artifacts["expected_kernel_commit"] == head
    assert artifacts["kernel_source_path"] == str(source.resolve())
