#!/usr/bin/env python3
"""Generate relocatable fresh-E2E ``input.txt`` files from a case manifest.

The benchmark cache is intentionally kept out of the source tree. This helper
only stages evidence that a fresh Kernel Expert is allowed to see: the
report-time log, matching boot image/debug symbols, kernel source, config, and
the configured guest rootfs. It never copies or declares a historical
reproducer. Host-specific locations are supplied through environment variables
or the optional case configuration JSON.

Required deployment inputs:

``LUMEN_BENCHMARK_ASSET_ROOT``
    Directory containing ``<issue_id>/bzImage`` and ``vmlinux``.
``LUMEN_BENCHMARK_KERNEL_SOURCE``
    Default source checkout for the manifest's target commits.
``LUMEN_BENCHMARK_ROOTFS``
    Default compiler/SSH-capable guest image.
``LUMEN_BENCHMARK_ARTIFACT_ROOT``
    Directory containing ``<issue_id>/crash.log`` and cached text artifacts.

Case overrides are data, not code. The JSON passed to ``--case-config`` may
contain ``{"cases": {"<issue_id>": {"kernel_source": "...",
"rootfs": "...", "qemu_extra_cmdline": "..."}}}``; values may themselves
reference environment variables.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MANIFEST = PROJECT_ROOT / "runtime" / "benchmark_v0.2_real" / "source_artifacts" / "manifest.json"
DEFAULT_OUTPUT = PROJECT_ROOT / "runtime" / "benchmark_v0.2_real" / "fresh_inputs"

_COMMIT_RE = re.compile(r"^[0-9a-fA-F]{7,40}$")


def _expand_path(raw: str) -> Path:
    value = os.path.expanduser(os.path.expandvars(str(raw).strip()))
    path = Path(value)
    return path if path.is_absolute() else (PROJECT_ROOT / path).resolve()


def _env_value(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise ValueError(f"required deployment variable is unset: {name}")
    return value


def _join_ref(base: str, *parts: str) -> str:
    suffix = "/".join(str(part).strip("/") for part in parts if str(part).strip("/"))
    return str(base).rstrip("/") + (f"/{suffix}" if suffix else "")


def _require_file(raw: str, label: str) -> None:
    path = _expand_path(raw)
    if not path.is_file():
        raise ValueError(f"{label} is missing or not a file: {raw} (resolved {path})")


def _resolve_commit(source_path: Path, commit: str, issue_id: str) -> str:
    """Resolve a manifest ref to the full commit present in its source tree."""
    try:
        completed = subprocess.run(
            ["git", "-C", str(source_path), "rev-parse", "--verify", f"{commit}^{{commit}}"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ValueError(f"{issue_id}: cannot resolve kernel commit {commit!r}: {exc}") from exc
    resolved = completed.stdout.strip().lower()
    if completed.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", resolved):
        detail = (completed.stderr or completed.stdout).strip()[-300:]
        raise ValueError(
            f"{issue_id}: kernel commit {commit!r} is not a resolvable full commit in "
            f"{source_path}: {detail}"
        )
    return resolved


def _load_case_config(path: str | None) -> dict[str, dict[str, str]]:
    if not path:
        return {}
    config_path = _expand_path(path)
    if not config_path.is_file():
        raise ValueError(f"case config does not exist: {config_path}")
    data = json.loads(config_path.read_text(encoding="utf-8"))
    cases = data.get("cases", data) if isinstance(data, dict) else None
    if not isinstance(cases, dict):
        raise ValueError("case config must be an object or an object with a 'cases' object")
    result: dict[str, dict[str, str]] = {}
    for issue_id, raw in cases.items():
        if not isinstance(raw, dict):
            raise ValueError(f"case config entry {issue_id!r} must be an object")
        result[str(issue_id)] = {
            str(key): str(value)
            for key, value in raw.items()
            if value is not None
        }
    return result


def _manifest_artifact(issue: dict[str, Any], kinds: tuple[str, ...]) -> dict[str, Any] | None:
    for artifact in issue.get("artifacts", []):
        if artifact.get("kind") in kinds and artifact.get("path"):
            return artifact
    return None


def _description(issue: dict[str, Any]) -> str:
    """Return only the human-facing kernel fault description.

    ``Bug Promote`` is the problem statement, not a transport for benchmark
    metadata or workflow policy.  Keep source paths, commit, architecture,
    and maintenance constraints in their dedicated input fields below so the
    Kernel Expert receives a concise, non-leaky problem prompt.
    """
    title = str(issue.get("title") or "").strip()
    if title:
        return title
    evidence = issue.get("report_evidence")
    if isinstance(evidence, dict):
        summary = str(evidence.get("crash_summary") or "").strip()
        if summary:
            return summary
    return "Linux kernel fault report"


def _build_case(
    issue: dict[str, Any],
    *,
    artifact_root_ref: str,
    asset_root_ref: str,
    default_source_ref: str,
    default_rootfs_ref: str,
    override: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    issue_id = str(issue.get("issue_id", "")).strip()
    if not issue_id:
        raise ValueError("manifest issue is missing issue_id")
    commit = str(issue.get("kernel_commit", "")).strip()
    if not _COMMIT_RE.fullmatch(commit):
        raise ValueError(f"{issue_id}: manifest kernel_commit is invalid: {commit!r}")
    arch = str(issue.get("arch", "")).strip() or "x86_64"

    log_artifact = _manifest_artifact(issue, ("crash_log",))
    if not log_artifact:
        raise ValueError(f"{issue_id}: manifest has no crash_log path")
    log_ref = _join_ref(artifact_root_ref, log_artifact["path"])
    _require_file(log_ref, f"{issue_id} crash log")

    asset_dir_ref = _join_ref(asset_root_ref, issue_id)
    boot_ref = _join_ref(asset_dir_ref, "bzImage")
    vmlinux_ref = _join_ref(asset_dir_ref, "vmlinux")
    _require_file(boot_ref, f"{issue_id} boot kernel")
    _require_file(vmlinux_ref, f"{issue_id} vmlinux")

    source_ref = override.get("kernel_source", default_source_ref).strip()
    rootfs_ref = override.get("rootfs", default_rootfs_ref).strip()
    if not source_ref or not rootfs_ref:
        raise ValueError(f"{issue_id}: kernel_source and rootfs must be configured")
    source_path = _expand_path(source_ref)
    if not source_path.is_dir():
        raise ValueError(f"{issue_id} kernel source is missing: {source_ref}")
    commit = _resolve_commit(source_path, commit, issue_id)
    _require_file(rootfs_ref, f"{issue_id} rootfs")

    lines = [
        f"Bug Promote: {_description(issue)}",
        f"log: {log_ref}",
        f"vmlinux: {vmlinux_ref}",
        f"boot_kernel: {boot_ref}",
        f"kernel_source: {source_ref}",
        f"rootfs: {rootfs_ref}",
        f"target_arch: {arch}",
        f"expected_kernel_commit: {commit}",
        "maintenance_notes: Read the raw crash.log as authoritative evidence. "
        "Construct and validate a userspace C reproducer only; do not use or "
        "declare any historical reproducer. Test Expert must inject only the "
        "configured pressure/fault steps and accept a pass only when the "
        "runtime call chain matches the original log.",
    ]
    qemu_extra = override.get("qemu_extra_cmdline", "").strip()
    if qemu_extra:
        lines.append(f"qemu_extra_cmdline: {qemu_extra}")

    # A config is useful evidence when the cache contains one. Do not turn a
    # missing config into a guessed path or silently substitute an alternate.
    config_artifact = _manifest_artifact(issue, ("kernel_config", "kernel_config_alternate"))
    if config_artifact:
        candidate = _join_ref(artifact_root_ref, config_artifact["path"])
        if _expand_path(candidate).is_file():
            lines.insert(5, f"kernel_config: {candidate}")

    metadata = {
        "issue_id": issue_id,
        "target_arch": arch,
        "expected_kernel_commit": commit,
        "input_path": "",
        "log_path": log_ref,
        "boot_kernel_path": boot_ref,
        "vmlinux_path": vmlinux_ref,
        "kernel_source_path": source_ref,
        "rootfs_path": rootfs_ref,
        "qemu_extra_cmdline": qemu_extra,
        "historical_reproducer_declared": False,
    }
    return "\n".join(lines) + "\n", metadata


def generate(args: argparse.Namespace) -> int:
    manifest_path = _expand_path(args.manifest)
    if not manifest_path.is_file():
        raise ValueError(f"manifest does not exist: {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    issues = manifest.get("issues") if isinstance(manifest, dict) else None
    if not isinstance(issues, list) or not issues:
        raise ValueError("manifest must contain a non-empty 'issues' list")

    artifact_root = _env_value("LUMEN_BENCHMARK_ARTIFACT_ROOT")
    asset_root = _env_value("LUMEN_BENCHMARK_ASSET_ROOT")
    kernel_source = _env_value("LUMEN_BENCHMARK_KERNEL_SOURCE")
    rootfs = _env_value("LUMEN_BENCHMARK_ROOTFS")
    # Verify deployment roots before creating any output, so a partial input
    # set can never be mistaken for a runnable benchmark.
    if not _expand_path(artifact_root).is_dir():
        raise ValueError(f"artifact root does not exist: {artifact_root}")
    if not _expand_path(asset_root).is_dir():
        raise ValueError(f"asset root does not exist: {asset_root}")
    if not _expand_path(kernel_source).is_dir():
        raise ValueError(f"kernel source does not exist: {kernel_source}")
    _require_file(rootfs, "default rootfs")

    case_config = _load_case_config(args.case_config)
    selected = set(args.issue or [])
    if selected:
        unknown = selected - {str(issue.get("issue_id", "")) for issue in issues}
        if unknown:
            raise ValueError(f"unknown issue id(s): {', '.join(sorted(unknown))}")
        issues = [issue for issue in issues if str(issue.get("issue_id", "")) in selected]

    output_root = _expand_path(args.output_root)
    output_root.mkdir(parents=True, exist_ok=True)
    index: list[dict[str, Any]] = []
    for issue in issues:
        issue_id = str(issue.get("issue_id", ""))
        text, metadata = _build_case(
            issue,
            artifact_root_ref="${LUMEN_BENCHMARK_ARTIFACT_ROOT}",
            asset_root_ref="${LUMEN_BENCHMARK_ASSET_ROOT}",
            default_source_ref="${LUMEN_BENCHMARK_KERNEL_SOURCE}",
            default_rootfs_ref="${LUMEN_BENCHMARK_ROOTFS}",
            override=case_config.get(issue_id, {}),
        )
        case_dir = output_root / issue_id
        case_dir.mkdir(parents=True, exist_ok=True)
        input_path = case_dir / "input.txt"
        input_path.write_text(text, encoding="utf-8")
        metadata["input_path"] = str(input_path)
        index.append(metadata)
    (output_root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"generated {len(index)} fresh benchmark inputs under {output_root}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", default=str(DEFAULT_MANIFEST))
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT))
    parser.add_argument("--case-config", default="")
    parser.add_argument("--issue", action="append", help="generate only this issue id (repeatable)")
    args = parser.parse_args()
    try:
        return generate(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        parser.error(str(exc))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
