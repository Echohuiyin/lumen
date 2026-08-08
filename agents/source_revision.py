"""Validator-side exact kernel source/commit resolution.

The validator is the first workflow stage that can prevent a plausible but
wrong source tree from reaching any expert.  This module deliberately uses
the existing exact-commit resolver and never falls back to the moving HEAD.
"""

from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

from agents.contracts import ErrorEnvelope, SourceRevisionContract
from agents.semcode_path_analysis import (
    resolve_kernel_commit,
    resolve_kernel_source_for_commit,
)


_COMMIT_RE = re.compile(r"[0-9a-fA-F]{7,40}$")


def _error(
    *, category: str, code: str, message: str, cause: str = "",
    next_action: str = "", retryable: bool = False,
) -> ErrorEnvelope:
    return ErrorEnvelope(
        category=category,
        code=code,
        message=message,
        cause=cause,
        next_action=next_action,
        retryable=retryable,
    )


def _blocked(
    *, source: str, commit: str, error: ErrorEnvelope,
    evidence: list[dict] | None = None, source_clean: bool = False,
) -> SourceRevisionContract:
    return SourceRevisionContract(
        status="blocked",
        declared_source_path=source,
        declared_commit=commit,
        source_clean=source_clean,
        evidence=evidence or [],
        error=error,
    )


def _git_status(source: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            ["git", "-C", source, "status", "--porcelain", "--untracked-files=no"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)
    if result.returncode != 0:
        return False, (result.stderr or result.stdout).strip()[-500:]
    return not result.stdout.strip(), result.stdout.strip()[-500:]


def _git_head(source: str) -> tuple[str, str]:
    try:
        result = subprocess.run(
            ["git", "-C", source, "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=20, check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return "", str(exc)
    if result.returncode != 0:
        return "", (result.stderr or result.stdout).strip()[-500:]
    return result.stdout.strip().lower(), ""


def resolve_source_revision(
    kernel_source_path: str,
    expected_kernel_commit: str,
    *,
    workspace_root: str = "",
    source_snapshot_manifest_path: str = "",
) -> SourceRevisionContract:
    """Resolve and verify an exact source revision for the Validator.

    Missing/invalid commits are input errors.  A commit absent from the
    declared repository, a dirty checkout, or a failed detached-worktree
    switch is reported as a structured block.  No source tree is mutated.
    """
    raw_source = str(kernel_source_path or "").strip()
    source = str(Path(os.path.expanduser(raw_source)).resolve()) if raw_source else ""
    declared = str(expected_kernel_commit or "").strip().lower()
    base_evidence = [{
        "kind": "kernel_source_revision",
        "declared_source_path": source,
        "declared_commit": declared,
    }]

    if not source:
        return _blocked(
            source=source, commit=declared,
            error=_error(
                category="INVALID_INPUT", code="KERNEL_SOURCE_REQUIRED",
                message="kernel_source_path is required before commit validation.",
                next_action="Declare an absolute Linux source tree in input.txt.",
            ), evidence=base_evidence,
        )
    if not Path(source).is_dir():
        return _blocked(
            source=source, commit=declared,
            error=_error(
                category="UNAVAILABLE", code="KERNEL_SOURCE_UNAVAILABLE",
                message=f"kernel source directory is unavailable: {source}",
                next_action="Make the declared kernel source tree readable and rerun Validator.",
            ), evidence=base_evidence,
        )
    if not declared:
        return _blocked(
            source=source, commit=declared,
            error=_error(
                category="INVALID_INPUT", code="KERNEL_COMMIT_REQUIRED",
                message="expected_kernel_commit is required; Validator will not infer a moving HEAD.",
                next_action="Add expected_kernel_commit (7-40 hex characters) to input.txt.",
            ), evidence=base_evidence,
        )
    if not _COMMIT_RE.fullmatch(declared):
        return _blocked(
            source=source, commit=declared,
            error=_error(
                category="INVALID_INPUT", code="KERNEL_COMMIT_INVALID",
                message=f"expected_kernel_commit is not a 7-40 character hexadecimal object id: {declared}",
                next_action="Replace expected_kernel_commit with the kernel commit SHA or a unique prefix.",
            ), evidence=base_evidence,
        )

    manifest = str(source_snapshot_manifest_path or "").strip()
    if not manifest:
        clean, status_detail = _git_status(source)
        if not clean:
            head, head_error = _git_head(source)
            cause = status_detail or head_error or "git status failed"
            return _blocked(
                source=source, commit=declared, source_clean=False,
                error=_error(
                    category="INVALID_INPUT", code="KERNEL_SOURCE_DIRTY",
                    message="declared kernel source checkout has local modifications.",
                    cause=cause,
                    next_action="Use a clean checkout or declare a separate source tree; Validator never analyzes dirty source.",
                ), evidence=[*base_evidence, {"kind": "git_status", "head": head, "clean": False, "detail": cause}],
            )
        try:
            resolved_commit = resolve_kernel_commit(source, declared)
        except Exception as exc:
            message = str(exc)
            code = "KERNEL_COMMIT_NOT_FOUND" if "cannot uniquely resolve" in message else "KERNEL_COMMIT_RESOLUTION_FAILED"
            return _blocked(
                source=source, commit=declared, source_clean=True,
                error=_error(
                    category="INVALID_INPUT", code=code,
                    message=f"cannot resolve expected kernel commit in the declared source: {declared}",
                    cause=message,
                    next_action="Fetch the required commit into kernel_source or correct expected_kernel_commit.",
                ), evidence=[*base_evidence, {"kind": "git_object", "verified": False, "detail": message}],
            )
    else:
        # Attested snapshots are allowed to be non-Git trees.  The existing
        # resolver checks the manifest's exact commit and verification bit.
        resolved_commit = declared

    try:
        resolved_source = resolve_kernel_source_for_commit(
            source, resolved_commit,
            workspace_root=workspace_root,
            source_snapshot_manifest_path=manifest,
        )
    except Exception as exc:
        message = str(exc)
        code = "SOURCE_SNAPSHOT_INVALID" if manifest else "KERNEL_SOURCE_SWITCH_FAILED"
        category = "INVALID_INPUT" if manifest else "UNAVAILABLE"
        return _blocked(
            source=source, commit=declared, source_clean=not manifest,
            error=_error(
                category=category, code=code,
                message="kernel source could not be switched to the requested commit.",
                cause=message,
                next_action=(
                    "Repair the attested source snapshot manifest/tree."
                    if manifest else
                    "Ensure the source commit and Semcode index are available, then rerun Validator."
                ),
            ), evidence=[*base_evidence, {"kind": "source_switch", "status": "blocked", "detail": message}],
        )

    resolved_source = str(Path(resolved_source).resolve())
    method = "attested_snapshot" if manifest else (
        "same_checkout" if resolved_source == source else "detached_worktree"
    )
    if not manifest:
        actual_head, head_error = _git_head(resolved_source)
        if actual_head != resolved_commit:
            return _blocked(
                source=source, commit=declared, source_clean=True,
                error=_error(
                    category="INTERNAL_BUG", code="KERNEL_SOURCE_COMMIT_MISMATCH",
                    message="resolved source HEAD does not match the requested kernel commit.",
                    cause=f"expected={resolved_commit} actual={actual_head or '<unreadable>'} {head_error}",
                    next_action="Discard the invalid worktree and rerun source preparation.",
                ), evidence=[*base_evidence, {"kind": "source_switch", "resolved_source_path": resolved_source, "actual_head": actual_head, "expected_head": resolved_commit}],
            )
    return SourceRevisionContract(
        status="switched" if method == "detached_worktree" else "resolved",
        declared_source_path=source,
        resolved_source_path=resolved_source,
        declared_commit=declared,
        resolved_commit=resolved_commit,
        switch_method=method,
        source_clean=True,
        evidence=[*base_evidence, {
            "kind": "source_switch", "status": "ok",
            "resolved_source_path": resolved_source,
            "resolved_commit": resolved_commit,
            "switch_method": method,
        }],
    )
